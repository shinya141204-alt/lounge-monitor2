import datetime
import gzip
import io
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

import app as app_module
import contact_form
import embed_widgets
import job_state
import local_db
import ops_governance
import request_guards
import turbo_store
import worker


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class M4FixesRegressionTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="m4_fixes_")
        self._old_env = {
            "JOB_STATE_DIR": os.environ.get("JOB_STATE_DIR"),
            "LOCAL_DB_PATH": os.environ.get("LOCAL_DB_PATH"),
        }
        os.environ["JOB_STATE_DIR"] = self._tmpdir
        self._test_db_path = os.path.join(self._tmpdir, "test_m4.db")
        os.environ["LOCAL_DB_PATH"] = self._test_db_path
        self._old_local_db_path = local_db.DB_PATH
        local_db.DB_PATH = self._test_db_path
        local_db.init_db()
        job_state.reset_cache_for_tests()
        turbo_store.reset_for_tests()
        ops_governance.invalidate_governance_cache()
        with app_module._login_failures_lock:
            app_module._login_failures.clear()
        with local_db._votes_lock:
            local_db._memory_votes.clear()
        with local_db._watch_intents_lock:
            local_db._memory_watch_intents.clear()
        self._old_testing = app_module.app.config.get("TESTING")
        self._old_propagate = app_module.app.config.get("PROPAGATE_EXCEPTIONS")
        app_module.app.config["TESTING"] = True
        app_module.app.config["PROPAGATE_EXCEPTIONS"] = False
        self.client = app_module.app.test_client()

    def tearDown(self):
        app_module.app.config["TESTING"] = self._old_testing
        app_module.app.config["PROPAGATE_EXCEPTIONS"] = self._old_propagate
        with app_module._login_failures_lock:
            app_module._login_failures.clear()
        with local_db._votes_lock:
            local_db._memory_votes.clear()
        with local_db._watch_intents_lock:
            local_db._memory_watch_intents.clear()
        local_db.DB_PATH = self._old_local_db_path
        for k, v in self._old_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        job_state.reset_cache_for_tests()
        turbo_store.reset_for_tests()
        ops_governance.invalidate_governance_cache()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _login_master(self):
        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True
            sess["auth_gen"] = app_module._compute_session_auth_gen(True)

    # 1. P0: Dockerfile + app.py worker stdout-only logging under FLY_APP_NAME
    def test_p0_fly_worker_skips_file_log_handler_and_dockerfile_owns_app(self):
        with open(os.path.join(REPO_ROOT, "Dockerfile"), "r", encoding="utf-8") as f:
            df_txt = f.read()
        self.assertIn("chown appuser:appgroup /app", df_txt)

        env = os.environ.copy()
        env["PYTHONPATH"] = REPO_ROOT
        env["DISABLE_SHEETS"] = "1"
        env["DISABLE_SCHEDULER"] = "1"
        env["FLY_APP_NAME"] = "aiseki-navi-tokyo"
        env["APP_ROLE"] = "worker"
        env["LOCAL_DB_PATH"] = self._test_db_path
        env["JOB_STATE_DIR"] = self._tmpdir
        env.pop("APP_LOG_PATH", None)
        # Verify importing app under FLY_APP_NAME + APP_ROLE=worker opens no RotatingFileHandler
        code = (
            "import logging, logging.handlers, app; "
            "handlers = [type(h).__name__ for h in logging.getLogger('').handlers]; "
            "assert 'RotatingFileHandler' not in handlers, handlers; "
            "assert app._is_fly_stdout_only is True; "
            "print('OK')"
        )
        proc = subprocess.run(
            [sys.executable, "-c", code],
            cwd=self._tmpdir,
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
        self.assertEqual(proc.returncode, 0, f"stderr: {proc.stderr}\nstdout: {proc.stdout}")
        self.assertIn("OK", proc.stdout)

    # 2. P1: Public POST JSON 4 routes return 400 (not 500) on non-dict JSON
    def test_p1_public_json_routes_reject_non_dict_json_with_400(self):
        endpoints = (
            "/api/vote",
            "/api/watch-intent",
            "/api/push/subscribe",
            "/api/push/unsubscribe",
        )
        non_dict_bodies = ("[1, 2, 3]", "[]", '"hello"', "123", "true")
        for ep in endpoints:
            for body in non_dict_bodies:
                resp = self.client.post(ep, data=body, content_type="application/json")
                self.assertEqual(
                    resp.status_code,
                    400,
                    f"{ep} with {body!r} returned {resp.status_code}: {resp.get_data(as_text=True)}",
                )
                data = resp.get_json()
                self.assertIsInstance(data, dict)
                self.assertIn("error", data)
            # Empty / JSON null preserves 400 validation error
            for empty_body in ("", "null"):
                resp_empty = self.client.post(ep, data=empty_body, content_type="application/json")
                self.assertEqual(resp_empty.status_code, 400)

    # 3. P1: /api/pdca/sns-insights/ingest* allow >2MB uploads up to spec, reject 8MB with 413
    def test_p1_pdca_insight_upload_limits_and_413(self):
        self.assertEqual(app_module.app.config.get("MAX_CONTENT_LENGTH"), 2 * 1024 * 1024)
        self._login_master()
        headers = {"Origin": "http://localhost"}

        # 2.5MB screenshot to /api/pdca/sns-insights/ingest must NOT hit 413
        payload_2_5m = b"\x89PNG\r\n\x1a\n" + (b"A" * (2560 * 1024))
        with mock.patch("sns_insights_loop.ingest_insight_screenshot_via_carbon", return_value={"ok": True, "ingested_posts": 1}), \
             mock.patch("pdca_engine.run_pdca_cycle", return_value={"active_directives": {}, "closed_loop_feedback": {}, "diagnostics": {}}):
            r_img_ok = self.client.post(
                "/api/pdca/sns-insights/ingest",
                data={"file": (io.BytesIO(payload_2_5m), "shot.png"), "platform": "threads"},
                content_type="multipart/form-data",
                headers=headers,
            )
        self.assertEqual(r_img_ok.status_code, 200, r_img_ok.get_data(as_text=True))

        # 2.5MB CSV to /api/pdca/sns-insights/ingest-csv must NOT hit 413
        csv_2_5m = ("query,clicks,impressions\n" + ("test,1,10\n" * 240000)).encode("utf-8")
        self.assertGreater(len(csv_2_5m), 2 * 1024 * 1024)
        self.assertLess(len(csv_2_5m), 4 * 1024 * 1024)
        with mock.patch("sns_insights_loop.ingest_csv_insights", return_value={"ok": True, "detected_platform": "gsc", "queries_ingested": 1, "pages_ingested": 0}), \
             mock.patch("pdca_engine.run_pdca_cycle", return_value={"active_directives": {}, "closed_loop_feedback": {}, "diagnostics": {}}):
            r_csv_ok = self.client.post(
                "/api/pdca/sns-insights/ingest-csv",
                data={"csv_file": (io.BytesIO(csv_2_5m), "gsc.csv"), "platform": "auto"},
                content_type="multipart/form-data",
                headers=headers,
            )
        self.assertEqual(r_csv_ok.status_code, 200, r_csv_ok.get_data(as_text=True))

        # 8MB payload to /api/pdca/sns-insights/ingest and ingest-csv must return 413
        payload_8m = b"B" * (8 * 1024 * 1024)
        for ep in ("/api/pdca/sns-insights/ingest", "/api/pdca/sns-insights/ingest-csv"):
            r_big = self.client.post(
                ep,
                data={"file": (io.BytesIO(payload_8m), "huge.bin")},
                content_type="multipart/form-data",
                headers=headers,
            )
            self.assertEqual(r_big.status_code, 413, f"{ep} should reject 8MB with 413")

    # 4. P2: templates/index.html partial dict with only usual_w_diff / usual_m_diff does not crash
    def test_p2_index_template_usual_is_defined_guard(self):
        tpl = app_module.app.jinja_env.get_template("index.html")
        partial_stores = [
            {
                "name": "OLG 新宿",
                "seo_name": "オリエンタルラウンジ 新宿",
                "slug": "oriental-lounge-shinjuku",
                "women": 0,
                "men": 0,
                "is_preopen_zero": True,
                "is_open_now": False,
                "usual_w_diff": 5,
            },
            {
                "name": "OLG 渋谷本店",
                "seo_name": "オリエンタルラウンジ 渋谷本店",
                "slug": "oriental-lounge-shibuya",
                "women": 0,
                "men": 0,
                "is_preopen_zero": True,
                "is_open_now": False,
                "usual_m_diff": -3,
            },
        ]
        with app_module.app.test_request_context("/"):
            rendered = tpl.render(
                initial_status={
                    "ranking": partial_stores,
                    "all_closed_mode": False,
                    "preopen_domestic_count": 2,
                },
                is_staff=False,
                is_master=False,
                last_updated="2026/11/19 14:00",
                events=[],
                areas=[],
                brands=[],
                meta_description="test",
                canonical_url="https://aiseki-navi.com/",
            )
        self.assertIn("deferredHeadPool", rendered)
        self.assertIn('"uwd": 5', rendered)
        self.assertIn('"uw": null', rendered)
        self.assertIn('"umd": -3', rendered)
        self.assertIn('"um": null', rendered)

    # 5. P2: /sw.js Vary: Accept-Encoding + gzip compression
    def test_p2_sw_js_vary_and_gzip(self):
        r_gz = self.client.get("/sw.js?v=121", headers={"Accept-Encoding": "gzip"})
        try:
            self.assertEqual(r_gz.status_code, 200)
            self.assertIn("Accept-Encoding", r_gz.headers.get("Vary", ""))
            self.assertEqual(r_gz.headers.get("Content-Encoding"), "gzip")
            self.assertEqual(r_gz.headers.get("X-Content-Type-Options"), "nosniff")
            cc = r_gz.headers.get("Cache-Control", "")
            self.assertIn("no-cache", cc)
            self.assertIn("no-store", cc)
            self.assertIn("must-revalidate", cc)
            decompressed = gzip.decompress(r_gz.data).decode("utf-8")
            self.assertIn("CACHE_NAME = 'aiseki-navi-v121'", decompressed)
        finally:
            r_gz.close()

        # Without gzip header, Vary is still present and body is plain text
        r_plain = self.client.get("/sw.js")
        try:
            self.assertEqual(r_plain.status_code, 200)
            self.assertIn("Accept-Encoding", r_plain.headers.get("Vary", ""))
            self.assertIsNone(r_plain.headers.get("Content-Encoding"))
            self.assertIn("CACHE_NAME = 'aiseki-navi-v121'", r_plain.data.decode("utf-8"))
        finally:
            r_plain.close()

    # 6. P2: GET /staff/logout cross-site CSRF blocked with 403 while direct/same-origin preserved
    def test_p2_get_staff_logout_blocks_cross_site_csrf(self):
        self._login_master()
        r_cross = self.client.get("/staff/logout", headers={"Sec-Fetch-Site": "cross-site", "Referer": "https://evil.com/"})
        self.assertEqual(r_cross.status_code, 403)
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get("is_master"))

        r_cross_no_ref = self.client.get("/staff/logout", headers={"Sec-Fetch-Site": "cross-site"})
        self.assertEqual(r_cross_no_ref.status_code, 403)
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get("is_master"))

        r_ext_ref = self.client.get("/staff/logout", headers={"Referer": "https://evil.com/csrf"})
        self.assertEqual(r_ext_ref.status_code, 403)
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get("is_master"))

        # Direct navigation (Sec-Fetch-Site: none) and same-origin GET are allowed
        r_direct = self.client.get("/staff/logout", headers={"Sec-Fetch-Site": "none"})
        self.assertEqual(r_direct.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get("is_master"))

    # 7. P2: contact_form.is_same_origin port matching
    def test_p2_is_same_origin_strict_port_matching(self):
        self.assertFalse(contact_form.is_same_origin("http://localhost:9999", None, "localhost:5001", strict=True))
        self.assertFalse(contact_form.is_same_origin("http://localhost:9999", None, "localhost", strict=True))
        self.assertFalse(contact_form.is_same_origin("http://localhost", None, "localhost:5001", strict=True))
        self.assertTrue(contact_form.is_same_origin("http://localhost:5001", None, "localhost:5001", strict=True))
        self.assertTrue(contact_form.is_same_origin("https://aiseki-navi.com", None, "aiseki-navi.com", strict=True))
        self.assertTrue(contact_form.is_same_origin("https://aiseki-navi.com:443", None, "aiseki-navi.com", strict=True))
        self.assertFalse(contact_form.is_same_origin("https://aiseki-navi.com@evil.com", None, "aiseki-navi.com", strict=True))
        self.assertTrue(contact_form.is_same_origin("http://[::1]:5001", None, "[::1]:5001", strict=True))
        self.assertFalse(contact_form.is_same_origin("http://[::1]:9999", None, "[::1]:5001", strict=True))

    # 8. P2: Accept-Encoding q=0 respected in serve_static_asset & compress_response
    def test_p2_accept_encoding_q0_excluded(self):
        self.assertEqual(app_module._parse_accept_encoding("br;q=0, gzip"), {"gzip"})
        self.assertEqual(app_module._parse_accept_encoding("gzip; q=0.0"), set())

        # Create a temporary .br and .gz sibling inside a temp static dir to verify q=0 selection
        tmp_static = os.path.join(self._tmpdir, "static")
        os.makedirs(tmp_static, exist_ok=True)
        js_path = os.path.join(tmp_static, "sample.js")
        raw_bytes = b"console.log('hello');\n" * 40
        with open(js_path, "wb") as f:
            f.write(raw_bytes)
        with open(js_path + ".br", "wb") as f:
            f.write(b"FAKE_BROTLI")
        with open(js_path + ".gz", "wb") as f:
            f.write(gzip.compress(raw_bytes))

        old_static_folder = app_module.app.static_folder
        try:
            app_module.app.static_folder = tmp_static
            r_br_q0 = self.client.get("/static/sample.js", headers={"Accept-Encoding": "br;q=0, gzip"})
            try:
                self.assertEqual(r_br_q0.status_code, 200)
                self.assertEqual(r_br_q0.headers.get("Content-Encoding"), "gzip")
            finally:
                r_br_q0.close()

            r_both_q0 = self.client.get("/static/sample.js", headers={"Accept-Encoding": "br;q=0, gzip;q=0"})
            try:
                self.assertEqual(r_both_q0.status_code, 200)
                self.assertIsNone(r_both_q0.headers.get("Content-Encoding"))
            finally:
                r_both_q0.close()
        finally:
            app_module.app.static_folder = old_static_folder

        r_sw_q0 = self.client.get("/sw.js", headers={"Accept-Encoding": "gzip;q=0"})
        try:
            self.assertEqual(r_sw_q0.status_code, 200)
            self.assertIsNone(r_sw_q0.headers.get("Content-Encoding"))
        finally:
            r_sw_q0.close()

    # 9. P2: worker._rescue_orphaned_sns_jobs handles UTC ISO timestamps accurately
    def test_p2_worker_rescue_orphaned_sns_jobs_utc_iso(self):
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        recent_utc_iso = (now_utc - datetime.timedelta(minutes=3)).isoformat()
        stale_utc_z = (now_utc - datetime.timedelta(minutes=20)).strftime("%Y-%m-%dT%H:%M:%SZ")

        fake_jobs = [
            {"job_id": "job_recent_utc", "status": "running", "started_at": recent_utc_iso},
            {"job_id": "job_stale_utc", "status": "running", "started_at": stale_utc_z},
        ]
        completed_ids = []
        with mock.patch.object(turbo_store, "list_recent_sns_jobs", return_value=fake_jobs), \
             mock.patch.object(turbo_store, "complete_sns_job", side_effect=lambda jid, **kw: completed_ids.append(jid)):
            rescued = worker._rescue_orphaned_sns_jobs(max_age_minutes=15)
        self.assertEqual(rescued, 1)
        self.assertEqual(completed_ids, ["job_stale_utc"])

    # 10. P2: embed_widgets supplements raw monitor_logs when >3d old date has partial rollup
    def test_p2_embed_widgets_partial_rollup_supplements_missing_stores(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        target_date = (datetime.datetime.now(jst).date() - datetime.timedelta(days=10))
        ds = target_date.isoformat()

        conn = sqlite3.connect(self._test_db_path)
        try:
            local_db._ensure_moat_rollup_schema()
            # Store A has 3 hours of rollups on ds (>3 days ago)
            for hr in (20, 21, 22):
                conn.execute(
                    "INSERT OR REPLACE INTO store_hourly_rollups "
                    "(date_jst, hour_jst, business_date, store_name, sample_count, avg_women, avg_men, avg_wpct) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                    (ds, hr, ds, "OLG 新宿", 4, 20.0, 10.0, 66.7),
                )
            # Store A also has raw logs at 22:00 (must NOT be double-counted) and Store B has raw logs at 22:00 (MUST be included)
            conn.execute(
                "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                (f"{ds} 22:15:00", "OLG 新宿", 20, 10),
            )
            conn.execute(
                "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                (f"{ds} 22:15:00", "JIS SHINJUKU", 14, 16),
            )
            conn.commit()
        finally:
            conn.close()

        with embed_widgets._CACHE_LOCK:
            embed_widgets._CACHE.clear()
        agg = embed_widgets._load_store_slot_aggregates(days=30)
        stores = agg.get("stores") or {}
        self.assertIn("OLG 新宿", stores)
        self.assertIn("JIS SHINJUKU", stores)
        # Ensure OLG 新宿 hour 22 was not double-counted (only 1 bucket / 4 snapshots from rollup)
        slot_22 = (target_date.weekday(), 22)
        self.assertEqual(stores["OLG 新宿"][slot_22][1], 4)
        self.assertEqual(stores["JIS SHINJUKU"][slot_22][1], 1)

    # 11. P2: staff_pdca /@ guard & requirements.txt certifi
    def test_p2_staff_pdca_at_prefix_guard_and_requirements_certifi(self):
        with open(os.path.join(REPO_ROOT, "templates", "staff_pdca.html"), "r", encoding="utf-8") as f:
            pdca_html = f.read()
        self.assertIn("not p.startswith('/@')", pdca_html)
        self.assertIn("p.startsWith('/@')", pdca_html)

        with open(os.path.join(REPO_ROOT, "requirements.txt"), "r", encoding="utf-8") as f:
            req_txt = f.read()
        self.assertIn("certifi>=", req_txt)

    # 12. P0: worker._sns_queue_consumer_loop imports time and drains queued job cleanly
    def test_p0_worker_sns_queue_consumer_loop_drains_job_without_nameerror(self):
        import threading
        turbo_store.enable_memory_bus_for_tests(True)
        stop_ev = threading.Event()
        job = turbo_store.enqueue_sns_job("x_post", params={"slot": "20:00"})
        self.assertIsInstance(job, dict)
        executed = []

        def _fake_exec(jtype, jparams):
            executed.append((jtype, jparams))
            stop_ev.set()
            return {"status": "success", "ok": True}

        with mock.patch.object(app_module, "execute_sns_job_payload", side_effect=_fake_exec):
            t = threading.Thread(target=worker._sns_queue_consumer_loop, args=(stop_ev,), daemon=True)
            t.start()
            t.join(timeout=5.0)
        self.assertFalse(t.is_alive())
        self.assertEqual(len(executed), 1)
        finished = turbo_store.get_sns_job(job["job_id"])
        self.assertEqual(finished.get("status"), "succeeded")

    # 13. P1: rollup_store_hourly_stats non-ok still invokes prune_old_logs so 14d anonymization never halts
    def test_p1_rollup_non_ok_still_runs_prune_old_logs_for_14d_anonymization(self):
        with mock.patch.object(turbo_store, "acquire_distributed_lock", return_value=True), \
             mock.patch.object(local_db, "rollup_store_hourly_stats", return_value={"ok": False, "error": "db_busy"}), \
             mock.patch.object(local_db, "prune_old_logs") as prune_mock, \
             mock.patch.object(turbo_store, "prune_old_analytics_hourly"):
            worker._daily_token_and_db_maintenance_job()
        prune_mock.assert_called_once_with(monitor_days=90, vote_days=14)

        with mock.patch.object(turbo_store, "acquire_distributed_lock", return_value=True), \
             mock.patch.object(local_db, "rollup_store_hourly_stats", side_effect=RuntimeError("rollup_boom")), \
             mock.patch.object(local_db, "prune_old_logs") as prune_err_mock, \
             mock.patch.object(turbo_store, "prune_old_analytics_hourly"):
            worker._daily_token_and_db_maintenance_job()
        prune_err_mock.assert_called_once_with(monitor_days=90, vote_days=14)

        with mock.patch.object(turbo_store, "acquire_distributed_lock", return_value=True), \
             mock.patch.object(local_db, "rollup_store_hourly_stats", return_value={"ok": True, "upserted": 10}), \
             mock.patch.object(local_db, "prune_old_logs") as prune_ok_mock, \
             mock.patch.object(turbo_store, "prune_old_analytics_hourly"):
            worker._daily_token_and_db_maintenance_job()
        prune_ok_mock.assert_called_once_with(monitor_days=90, vote_days=14)

    # 14. P1: /api/push/subscribe & /api/vote non-pollution (Referer, payload, staff session, bool/inf wait_minutes)
    def test_p1_push_subscribe_and_vote_staff_non_pollution(self):
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec
        _pub_bytes = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        p256dh = base64.urlsafe_b64encode(_pub_bytes).decode("ascii").rstrip("=")
        auth = base64.urlsafe_b64encode(b"A" * 16).decode("ascii").rstrip("=")
        with app_module.data_lock:
            saved_full = list(app_module.latest_data.get("full_data") or [])
            app_module.latest_data["full_data"] = [
                {"name": "OLG 新宿", "women": 20, "men": 12, "total": 32, "region": "Kanto"},
            ]
        try:
            sub_body = {
                "store_name": "OLG 新宿",
                "target_cond": "female_55",
                "subscription": {
                    "endpoint": "https://fcm.googleapis.com/fcm/send/m4_test_ep",
                    "keys": {"p256dh": p256dh, "auth": auth},
                },
            }
            # Referer with ?staff_test=true must NOT record watch intent
            r_ref = self.client.post(
                "/api/push/subscribe",
                json=sub_body,
                headers={"Referer": "https://aiseki-navi.com/store/oriental-lounge-shinjuku?staff_test=true"},
            )
            self.assertEqual(r_ref.status_code, 200)
            self.assertEqual(local_db.get_watch_intent_counts("OLG 新宿", hours=6)["watching_count"], 0)

            # Staff session vote must be noop and not write crowd_votes
            with self.client.session_transaction() as sess:
                sess["is_staff"] = True
                sess["is_master"] = False
                sess["auth_gen"] = app_module._compute_session_auth_gen(False)
            r_staff_vote = self.client.post("/api/vote", json={"store_name": "OLG 新宿", "vote_type": "smooth"})
            self.assertEqual(r_staff_vote.status_code, 200)
            self.assertEqual(r_staff_vote.get_json().get("reason"), "staff_test_noop")
            self.assertEqual(local_db.get_recent_votes("OLG 新宿", hours=4)["smooth"], 0)

            # Clear staff session and verify bool / OverflowError wait_minutes don't 500 or record bool True->1
            with self.client.session_transaction() as sess:
                sess.clear()
            r_inf = self.client.post("/api/vote", data='{"store_name":"OLG 新宿","vote_type":"smooth","wait_minutes":1e400}', content_type="application/json")
            self.assertEqual(r_inf.status_code, 200)
            r_bool = self.client.post("/api/vote", json={"store_name": "OLG 新宿", "vote_type": "waiting", "wait_minutes": False, "voter_id": "v_bool"})
            self.assertEqual(r_bool.status_code, 200)
            self.assertEqual(r_bool.get_json()["votes"].get("reported_wait_samples", 0), 0)
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = saved_full

    # 15. P1: Rate limit cannot be bypassed via ?staff_test=1, IPv6 /64 normalized, contact priority capped
    def test_p1_rate_limit_no_staff_test_bypass_ipv6_and_contact_priority_cap(self):
        turbo_store.enable_memory_bus_for_tests(True)
        # /api/forecast-accuracy enforces rate limit even with ?staff_test=1 when not TESTING
        old_testing = app_module.app.testing
        try:
            app_module.app.testing = False
            with mock.patch.object(turbo_store, "check_sliding_rate_limit", return_value=False):
                r_fc = self.client.get("/api/forecast-accuracy?staff_test=1")
                self.assertEqual(r_fc.status_code, 429)
                r_emb = self.client.get("/embed/heatmap?scope=all&staff_test=1")
                self.assertEqual(r_emb.status_code, 429)
                # Authenticated admin is allowed
                r_adm = self.client.get("/api/forecast-accuracy?staff_test=1", headers={"X-Admin-Secret": app_module.ADMIN_API_SECRET})
                self.assertEqual(r_adm.status_code, 200)
        finally:
            app_module.app.testing = old_testing

        # IPv6 /64 normalization shares bucket across addresses in same /64
        turbo_store.clear_rate_limit("ipv6_test")
        for i in range(3):
            self.assertTrue(turbo_store.check_sliding_rate_limit("ipv6_test", f"2001:db8:1:2::{i + 1}", limit=3, window_sec=60))
        self.assertFalse(turbo_store.check_sliding_rate_limit("ipv6_test", "2001:db8:1:2::ffff", limit=3, window_sec=60))

        # Contact priority global cap
        contact_form.reset_rate_limits_for_tests()
        try:
            for i in range(contact_form.PRIORITY_GLOBAL_LIMIT):
                self.assertTrue(contact_form.check_rate_limit(f"198.51.100.{i}", category="removal"))
            self.assertFalse(contact_form.check_rate_limit("198.51.101.1", category="removal"))
        finally:
            contact_form.reset_rate_limits_for_tests()

    # 16. P1: Hidden brand opt-out in CSV/weekly-report/events/llms & toggle cache purge
    def test_p1_hidden_brand_opt_out_across_csv_weekly_events_llms(self):
        import weekly_report
        app_module.clear_fast_http_caches(clear_editorial=True)
        with app_module.data_lock:
            saved_full = list(app_module.latest_data.get("full_data") or [])
            app_module.latest_data["full_data"] = [
                {"name": "OLG 新宿", "women": 22, "men": 12, "total": 34, "region": "Kanto"},
                {"name": "JIS SHINJUKU", "women": 16, "men": 18, "total": 34, "region": "Kanto"},
            ]
        sample_rows = [
            {"store_slug": "oriental-lounge-shinjuku", "store_name": "オリエンタルラウンジ 新宿", "brand_name": "オリエンタルラウンジ", "area_label": "新宿", "window_days": 30, "sample_count": 10, "avg_women": 20, "peak_women": 30, "avg_men": 10, "peak_men": 20, "avg_female_ratio_pct": 66.7, "store_url": "https://aiseki-navi.com/store/oriental-lounge-shinjuku"},
            {"store_slug": "jis-shinjuku", "store_name": "JIS（ジス）新宿", "brand_name": "JIS（ジス）", "area_label": "新宿", "window_days": 30, "sample_count": 10, "avg_women": 15, "peak_women": 25, "avg_men": 15, "peak_men": 25, "avg_female_ratio_pct": 50.0, "store_url": "https://aiseki-navi.com/store/jis-shinjuku"},
        ]
        fake_weekly = {
            "generated_at": "2026-11-19 15:00:00",
            "stores": [{"name": "オリエンタルラウンジ 新宿", "brand": "オリエンタルラウンジ", "avg_women": 20}, {"name": "JIS（ジス）新宿", "brand": "JIS", "avg_women": 15}],
            "anaba_stores": [{"name": "JIS（ジス）新宿", "brand": "JIS", "avg_women": 15}],
            "by_brand": {"オリエンタルラウンジ": {"store_count": 1}, "JIS": {"store_count": 1}},
        }
        try:
            with mock.patch.dict(os.environ, {"HIDDEN_BRANDS": "jis"}, clear=False):
                ops_governance.invalidate_governance_cache()
                app_module.clear_fast_http_caches(clear_editorial=True)
                with mock.patch.object(local_db, "get_monthly_store_stats_rows", return_value=sample_rows):
                    csv_txt = self.client.get("/stats/monthly.csv?days=30").get_data(as_text=True)
                    self.assertIn("oriental-lounge-shinjuku", csv_txt)
                    self.assertNotIn("jis-shinjuku", csv_txt)

                weekly_report._report_cache["data"] = fake_weekly
                weekly_report._report_cache["last_fetched"] = datetime.datetime.now()
                r_wr = self.client.get("/api/weekly-report").get_json()
                self.assertEqual(len(r_wr["data"]["stores"]), 1)
                self.assertNotIn("JIS", r_wr["data"]["by_brand"])

                facts = self.client.get("/api/llms-facts.json").get_json()
                brand_names = [b["brand_name"] for b in (facts.get("brands") or [])]
                self.assertFalse(any("JIS" in bn for bn in brand_names))
        finally:
            weekly_report._report_cache["data"] = None
            weekly_report._report_cache["last_fetched"] = None
            with app_module.data_lock:
                app_module.latest_data["full_data"] = saved_full
            ops_governance.invalidate_governance_cache()
            app_module.clear_fast_http_caches(clear_editorial=True)

    # 17. P1: /api/moshimo-reel-queue rejects empty action & /api/pdca/run returns 409 on lock conflict
    def test_p1_moshimo_rejects_empty_action_and_pdca_run_lock_409(self):
        staff_bp_mod = sys.modules["routes.staff_bp"]
        self._login_master()
        hdr = {"Origin": "http://localhost"}
        r_empty_act = self.client.post("/api/moshimo-reel-queue", json={}, headers=hdr)
        self.assertEqual(r_empty_act.status_code, 400)

        acquired = staff_bp_mod._pdca_run_lock.acquire(blocking=False)
        self.assertTrue(acquired)
        try:
            r_409 = self.client.post("/api/pdca/run", json={"days": 14}, headers=hdr)
            self.assertEqual(r_409.status_code, 409)
        finally:
            staff_bp_mod._pdca_run_lock.release()

    # 18. P1: APP_ROLE=web never scrapes; enqueue_sns_job refuses fallback on FLY_APP_NAME & returns 503
    def test_p1_web_role_no_scrape_and_prod_enqueue_503(self):
        with mock.patch.dict(os.environ, {"APP_ROLE": "web"}, clear=False), \
             mock.patch.object(app_module, "sync_latest_data_from_shared_bus", return_value=False), \
             mock.patch.object(app_module, "update_job") as upd_mock:
            app_module.trigger_background_fetch_if_needed()
            upd_mock.assert_not_called()

        self._login_master()
        with mock.patch.dict(os.environ, {"FLY_APP_NAME": "aiseki-navi-tokyo"}, clear=False), \
             mock.patch.object(turbo_store, "_mem_bus_enabled", False), \
             mock.patch.object(turbo_store, "get_redis_client", return_value=None):
            res_q = turbo_store.enqueue_sns_job("x_post", {})
            self.assertFalse(res_q)
            r_503 = self.client.post("/api/sns-jobs/enqueue", json={"job_type": "x_post"}, headers={"Origin": "http://localhost"})
            self.assertEqual(r_503.status_code, 503)

    # 19. P1: SSR jis_wait_m_grp=0 vs None, open 0:0, local_db hi_bound clamp, named_mutex owner token
    def test_p1_ssr_facts_hi_bound_and_named_mutex_owner_token(self):
        import ssr_payload
        # jis_wait_m_grp=0 must NOT fall back to est_men_wait_groups=4
        item_zero = {
            "name": "JIS SHINJUKU",
            "source": "jis",
            "women": 20,
            "men": 15,
            "is_open_now": True,
            "pos_telemetry": {"telemetry_type": "jis_pos", "men_waiting_groups": 0},
            "wait_estimate": {"est_wait_men_min": 15, "est_men_wait_groups": 4},
        }
        ssr_payload.attach_card_ux_metadata(item_zero)
        self.assertEqual(item_zero["jis_wait_m_grp"], 0)
        self.assertEqual(item_zero["plan2_signal_text"], "いま待ち0組（公式POS実測）")

        # Open store with 0:0 shows 営業中（0 : 0）, not 営業時間外
        item_open_zero = {"name": "OLG 新宿", "source": "oriental", "women": 0, "men": 0, "is_open_now": True}
        ssr_payload.attach_card_ux_metadata(item_open_zero)
        self.assertEqual(item_open_zero["plan2_signal_text"], "営業中（0 : 0）")

        # Missing wait_estimate does NOT claim 即相席圏（推定待ち0分）
        item_no_we = {"name": "OLG 新宿", "source": "oriental", "women": 12, "men": 10, "is_open_now": True}
        ssr_payload.attach_card_ux_metadata(item_no_we)
        self.assertNotIn("推定待ち0分", item_no_we["plan2_signal_text"])

        # Named mutex cannot be released by another thread without owner token
        turbo_store.enable_memory_bus_for_tests(True)
        self.assertTrue(turbo_store.acquire_named_mutex("test_owner_m4", ttl_sec=30))
        rel_other = []
        t = __import__("threading").Thread(target=lambda: rel_other.append(turbo_store.release_named_mutex("test_owner_m4")))
        t.start()
        t.join(timeout=3.0)
        self.assertEqual(rel_other, [False])
        self.assertTrue(turbo_store.is_named_mutex_held("test_owner_m4"))
        self.assertTrue(turbo_store.release_named_mutex("test_owner_m4"))
        self.assertFalse(turbo_store.is_named_mutex_held("test_owner_m4"))

        # staff_pdca.html |default(0, true) handles None without TypeError
        tpl_pdca = app_module.app.jinja_env.get_template("staff_pdca.html")
        rendered_snip = app_module.app.jinja_env.from_string("{{ (v|default(0, true) > 0) }}").render(v=None)
        self.assertEqual(rendered_snip, "False")
        self.assertIsNotNone(tpl_pdca)

    # 20. M4 Fix3 (1): worker & scheduler_jobs always run 14d anonymization even when rollup fails
    def test_m4_fix3_rollup_failure_preserves_raw_logs_but_executes_14d_anonymization(self):
        import scheduler_jobs

        jst = datetime.timezone(datetime.timedelta(hours=9))
        now_jst = datetime.datetime.now(jst).replace(tzinfo=None)
        ts_95d = (now_jst - datetime.timedelta(days=95)).strftime("%Y-%m-%d %H:%M:%S")
        ts_16d = (now_jst - datetime.timedelta(days=16)).strftime("%Y-%m-%d %H:%M:%S")
        ts_5d = (now_jst - datetime.timedelta(days=5)).strftime("%Y-%m-%d %H:%M:%S")
        ts_future = (now_jst + datetime.timedelta(days=10)).strftime("%Y-%m-%d %H:%M:%S")

        conn = sqlite3.connect(self._test_db_path)
        try:
            conn.execute(
                "INSERT INTO monitor_logs (timestamp, store_name, men, women, source) VALUES (?, ?, ?, ?, ?)",
                (ts_95d, "OLG 新宿", 10, 15, "oriental"),
            )
            conn.execute(
                "INSERT INTO crowd_votes (store_name, vote_type, ip_hash, timestamp) VALUES (?, ?, ?, ?)",
                ("OLG 新宿", "smooth", "hash_old_vote", ts_16d),
            )
            conn.execute(
                "INSERT INTO crowd_votes (store_name, vote_type, ip_hash, timestamp) VALUES (?, ?, ?, ?)",
                ("OLG 新宿", "smooth", "hash_keep_vote", ts_5d),
            )
            conn.execute(
                "INSERT INTO store_watch_intents (store_name, target_cond, ip_hash, timestamp) VALUES (?, ?, ?, ?)",
                ("OLG 新宿", "female_55", "hash_old_watch", ts_16d),
            )
            conn.execute(
                "INSERT INTO store_watch_intents (store_name, target_cond, ip_hash, timestamp) VALUES (?, ?, ?, ?)",
                ("OLG 新宿", "female_55", "hash_keep_watch", ts_5d),
            )
            conn.execute(
                "INSERT INTO web_push_subscriptions (endpoint, p256dh, auth, store_name, target_cond, ip_hash, created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                ("https://fcm.googleapis.com/fcm/send/old", "p1", "a1", "OLG 新宿", "female_55", "hash_old_push", ts_16d, ts_future),
            )
            conn.commit()
        finally:
            conn.close()

        # Worker calls prune_old_logs even when rollup_store_hourly_stats returns ok=False;
        # raw 95d monitor_logs stay intact while 16d ip_hash rows are anonymized.
        with mock.patch.object(turbo_store, "acquire_distributed_lock", return_value=True), \
             mock.patch.object(local_db, "rollup_store_hourly_stats", return_value={"ok": False, "error": "forced_fail"}), \
             mock.patch.object(turbo_store, "prune_old_analytics_hourly"):
            worker._daily_token_and_db_maintenance_job()

        conn = sqlite3.connect(self._test_db_path)
        try:
            mon_cnt = conn.execute("SELECT COUNT(*) FROM monitor_logs WHERE timestamp = ?", (ts_95d,)).fetchone()[0]
            vote_old = conn.execute("SELECT ip_hash FROM crowd_votes WHERE timestamp = ?", (ts_16d,)).fetchone()[0]
            vote_new = conn.execute("SELECT ip_hash FROM crowd_votes WHERE timestamp = ?", (ts_5d,)).fetchone()[0]
            watch_old = conn.execute("SELECT ip_hash FROM store_watch_intents WHERE timestamp = ?", (ts_16d,)).fetchone()[0]
            watch_new = conn.execute("SELECT ip_hash FROM store_watch_intents WHERE timestamp = ?", (ts_5d,)).fetchone()[0]
            push_old = conn.execute("SELECT ip_hash FROM web_push_subscriptions WHERE endpoint LIKE '%/old'").fetchone()[0]
        finally:
            conn.close()

        self.assertEqual(mon_cnt, 1, "raw monitor_logs older than 90d must NOT be deleted when rollup fails")
        self.assertEqual(vote_old, "")
        self.assertEqual(vote_new, "hash_keep_vote")
        self.assertEqual(watch_old, "")
        self.assertEqual(watch_new, "hash_keep_watch")
        self.assertEqual(push_old, "")

        # Verify scheduler_jobs._daily_token_and_db_maintenance_job also calls prune_old_logs on non-ok rollup
        captured_jobs = []

        class _FakeScheduler:
            def __init__(self, *args, **kwargs):
                pass
            def add_job(self, func=None, **kwargs):
                captured_jobs.append(func)
            def start(self):
                pass

        with mock.patch.object(app_module, "BackgroundScheduler", _FakeScheduler), \
             mock.patch.object(app_module, "restore_latest_data_if_empty"), \
             mock.patch.object(app_module, "trigger_background_fetch_if_needed"), \
             mock.patch.object(local_db, "refresh_forecast_caches_async"), \
             mock.patch.object(local_db, "trigger_forecast_backfill_async"), \
             mock.patch("atexit.register"), \
             mock.patch("threading.Thread") as th_mock:
            th_mock.return_value.start.return_value = None
            scheduler_jobs.boot_legacy_scheduler(app_module)

        maint_fn = next((fn for fn in captured_jobs if getattr(fn, "__name__", "") == "_daily_token_and_db_maintenance_job"), None)
        self.assertIsNotNone(maint_fn)
        with mock.patch.object(local_db, "rollup_store_hourly_stats", return_value={"ok": False}), \
             mock.patch.object(local_db, "prune_old_logs") as sched_prune_mock:
            maint_fn()
        sched_prune_mock.assert_called_once_with(monitor_days=90, vote_days=14)

    # 21. M4 Fix3 (2): IPv4-mapped non-collision + IPv6 /64 ip_hash normalization across vote/watch/push
    def test_m4_fix3_ipv4_mapped_and_ipv6_slash64_ip_hash_normalization(self):
        import base64
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import ec

        self.assertEqual(turbo_store.normalize_ip_for_rate_limit("::ffff:1.2.3.4"), "1.2.3.4")
        self.assertEqual(turbo_store.normalize_ip_for_rate_limit("::ffff:5.6.7.8"), "5.6.7.8")
        self.assertNotEqual(
            turbo_store.normalize_ip_for_rate_limit("::ffff:1.2.3.4"),
            turbo_store.normalize_ip_for_rate_limit("::ffff:5.6.7.8"),
        )
        self.assertEqual(
            turbo_store.normalize_ip_for_rate_limit("2001:db8:abcd:0001::1"),
            "2001:db8:abcd:1::/64",
        )
        self.assertEqual(
            turbo_store.normalize_ip_for_rate_limit("2001:db8:abcd:0001::dead:beef"),
            "2001:db8:abcd:1::/64",
        )
        self.assertEqual(
            turbo_store.normalize_ip_for_rate_limit("::ffff:1.2.3.4|Mozilla"),
            "1.2.3.4|Mozilla",
        )

        _pub_bytes = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
            encoding=serialization.Encoding.X962,
            format=serialization.PublicFormat.UncompressedPoint,
        )
        p256dh = base64.urlsafe_b64encode(_pub_bytes).decode("ascii").rstrip("=")
        auth = base64.urlsafe_b64encode(b"B" * 16).decode("ascii").rstrip("=")

        with app_module.data_lock:
            saved_full = list(app_module.latest_data.get("full_data") or [])
            app_module.latest_data["full_data"] = [
                {"name": "OLG 新宿", "women": 20, "men": 12, "total": 32, "region": "Kanto"},
            ]
        try:
            # 1) /api/vote (without voter_id so DB stores ip_hash):
            #    same /64 prefix -> duplicate on same vote_type & identical ip_hash on different vote_type;
            #    ::ffff:1.2.3.4 == 1.2.3.4 != ::ffff:5.6.7.8
            r_v1 = self.client.post(
                "/api/vote",
                json={"store_name": "OLG 新宿", "vote_type": "smooth"},
                environ_base={"REMOTE_ADDR": "2001:db8:99:1::10"},
            )
            self.assertEqual(r_v1.status_code, 200)
            self.assertEqual(r_v1.get_json()["status"], "success")

            r_v1_dup = self.client.post(
                "/api/vote",
                json={"store_name": "OLG 新宿", "vote_type": "smooth"},
                environ_base={"REMOTE_ADDR": "2001:db8:99:1::cafe"},
            )
            self.assertEqual(r_v1_dup.status_code, 200)
            self.assertEqual(r_v1_dup.get_json()["status"], "duplicate")

            r_v2 = self.client.post(
                "/api/vote",
                json={"store_name": "OLG 新宿", "vote_type": "lively"},
                environ_base={"REMOTE_ADDR": "2001:db8:99:1::cafe"},
            )
            self.assertEqual(r_v2.status_code, 200)
            self.assertEqual(r_v2.get_json()["status"], "success")

            r_v3 = self.client.post(
                "/api/vote",
                json={"store_name": "OLG 新宿", "vote_type": "smooth"},
                environ_base={"REMOTE_ADDR": "::ffff:1.2.3.4"},
            )
            self.assertEqual(r_v3.status_code, 200)
            self.assertEqual(r_v3.get_json()["status"], "success")

            r_v3_dup = self.client.post(
                "/api/vote",
                json={"store_name": "OLG 新宿", "vote_type": "smooth"},
                environ_base={"REMOTE_ADDR": "1.2.3.4"},
            )
            self.assertEqual(r_v3_dup.status_code, 200)
            self.assertEqual(r_v3_dup.get_json()["status"], "duplicate")

            r_v4 = self.client.post(
                "/api/vote",
                json={"store_name": "OLG 新宿", "vote_type": "lively"},
                environ_base={"REMOTE_ADDR": "1.2.3.4"},
            )
            self.assertEqual(r_v4.status_code, 200)
            self.assertEqual(r_v4.get_json()["status"], "success")

            r_v5 = self.client.post(
                "/api/vote",
                json={"store_name": "OLG 新宿", "vote_type": "waiting"},
                environ_base={"REMOTE_ADDR": "::ffff:5.6.7.8"},
            )
            self.assertEqual(r_v5.status_code, 200)
            self.assertEqual(r_v5.get_json()["status"], "success")

            # 2) /api/watch-intent (without voter_id so DB stores ip_hash):
            #    same /64 shares ip_hash; ::ffff:1.2.3.4 == 1.2.3.4 != ::ffff:5.6.7.8
            self.assertEqual(
                self.client.post(
                    "/api/watch-intent",
                    json={"store_name": "OLG 新宿", "target_cond": "female_55"},
                    environ_base={"REMOTE_ADDR": "2001:db8:99:2::1"},
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.post(
                    "/api/watch-intent",
                    json={"store_name": "OLG 新宿", "target_cond": "zero_wait"},
                    environ_base={"REMOTE_ADDR": "2001:db8:99:2::beef"},
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.post(
                    "/api/watch-intent",
                    json={"store_name": "OLG 新宿", "target_cond": "female_55"},
                    environ_base={"REMOTE_ADDR": "::ffff:1.2.3.4"},
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.post(
                    "/api/watch-intent",
                    json={"store_name": "OLG 新宿", "target_cond": "zero_wait"},
                    environ_base={"REMOTE_ADDR": "1.2.3.4"},
                ).status_code,
                200,
            )
            self.assertEqual(
                self.client.post(
                    "/api/watch-intent",
                    json={"store_name": "OLG 新宿", "target_cond": "evening_peak"},
                    environ_base={"REMOTE_ADDR": "::ffff:5.6.7.8"},
                ).status_code,
                200,
            )

            conn = sqlite3.connect(self._test_db_path)
            try:
                watch_hashes = [r[0] for r in conn.execute("SELECT ip_hash FROM store_watch_intents ORDER BY id ASC").fetchall()]
            finally:
                conn.close()

            # 3) /api/push/subscribe: same /64 shares ip_hash; ::ffff:1.2.3.4 == 1.2.3.4 != ::ffff:5.6.7.8
            for ep_sfx, rem_ip in (
                ("p6a", "2001:db8:99:3::1"),
                ("p6b", "2001:db8:99:3::ffff"),
                ("p4m1", "::ffff:1.2.3.4"),
                ("p4p1", "1.2.3.4"),
                ("p4m2", "::ffff:5.6.7.8"),
            ):
                r_sub = self.client.post(
                    "/api/push/subscribe",
                    json={
                        "store_name": "OLG 新宿",
                        "target_cond": "female_55",
                        "subscription": {
                            "endpoint": f"https://fcm.googleapis.com/fcm/send/ip_norm_{ep_sfx}",
                            "keys": {"p256dh": p256dh, "auth": auth},
                        },
                    },
                    environ_base={"REMOTE_ADDR": rem_ip},
                )
                self.assertEqual(r_sub.status_code, 200)

            conn = sqlite3.connect(self._test_db_path)
            try:
                vote_hashes = [r[0] for r in conn.execute("SELECT ip_hash FROM crowd_votes ORDER BY id ASC").fetchall()]
                push_hashes = {
                    r[0].rsplit("_", 1)[-1]: r[1]
                    for r in conn.execute("SELECT endpoint, ip_hash FROM web_push_subscriptions").fetchall()
                }
            finally:
                conn.close()

            # crowd_votes has 5 inserted rows across 3 normalized IP buckets (2001:db8:99:1::/64, 1.2.3.4, 5.6.7.8)
            self.assertEqual(len(vote_hashes), 5)
            self.assertEqual(vote_hashes[0], vote_hashes[1])
            self.assertEqual(vote_hashes[2], vote_hashes[3])
            self.assertEqual(len(set(vote_hashes)), 3)
            self.assertEqual(len(watch_hashes), 5)
            self.assertEqual(watch_hashes[0], watch_hashes[1])
            self.assertEqual(watch_hashes[2], watch_hashes[3])
            self.assertEqual(len(set(watch_hashes)), 3)
            self.assertEqual(push_hashes["p6a"], push_hashes["p6b"])
            self.assertEqual(push_hashes["p4m1"], push_hashes["p4p1"])
            self.assertNotEqual(push_hashes["p4m1"], push_hashes["p4m2"])
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = saved_full

    # 22. M4 Fix3 (3): /api/events cached path count matches filtered events length
    def test_m4_fix3_api_events_cached_path_count_matches_filtered_events(self):
        import events

        raw_events = [
            {"id": "ev1", "title": "OLGフェア", "brand": "OLG", "store_name": "OLG 新宿"},
            {"id": "ev2", "title": "JIS企画", "brand": "JIS", "store_name": "JIS SHINJUKU"},
            {"id": "ev3", "title": "合同企画", "brand": "OLG", "target_stores": ["JIS SHINJUKU"]},
        ]
        old_cache = dict(events._events_memory_cache)
        try:
            events._events_memory_cache["updated_at"] = "2026-11-19 16:00:00"
            events._events_memory_cache["events"] = list(raw_events)
            with mock.patch.dict(os.environ, {"HIDDEN_BRANDS": "jis"}, clear=False):
                ops_governance.invalidate_governance_cache()
                app_module.clear_fast_http_caches(clear_editorial=False)
                # 1st call populates _cached_json_response; 2nd call hits HTTP cache
                for _ in range(2):
                    r = self.client.get("/api/events")
                    self.assertEqual(r.status_code, 200)
                    data = r.get_json()
                    self.assertEqual(data["status"], "success")
                    self.assertEqual(len(data["events"]), 1)
                    self.assertEqual(data["count"], len(data["events"]))
                    self.assertEqual(data["events"][0]["brand"], "オリエンタルラウンジ")
        finally:
            events._events_memory_cache.clear()
            events._events_memory_cache.update(old_cache)
            ops_governance.invalidate_governance_cache()
            app_module.clear_fast_http_caches(clear_editorial=False)

    # 23. M4 Fix3 (4): store_detail live_data_missing avoids 0:0 during open hours & monthly.csv None returns 503
    def test_m4_fix3_store_detail_missing_live_and_monthly_csv_none_503(self):
        import store_hours
        portal_bp_mod = sys.modules["routes.portal_bp"]

        jst = datetime.timezone(datetime.timedelta(hours=9))
        dt_open = datetime.datetime(2026, 11, 19, 21, 30, tzinfo=jst)
        orig_hours_fn = store_hours.get_business_hours_status

        with app_module.data_lock:
            saved_full = list(app_module.latest_data.get("full_data") or [])
            # OLG 新宿 is intentionally absent from live_map while OLG 渋谷本店 has genuine 0:0
            app_module.latest_data["full_data"] = [
                {"name": "OLG 渋谷本店", "women": 0, "men": 0, "total": 0, "region": "Kanto"},
            ]
        try:
            app_module.clear_fast_http_caches(clear_editorial=True)
            with mock.patch(
                "store_hours.get_business_hours_status",
                side_effect=lambda s, now_jst=None, total_guests=0: orig_hours_fn(s, now_jst=dt_open, total_guests=total_guests),
            ):
                r_missing = self.client.get("/store/oriental-lounge-shinjuku")
                self.assertEqual(r_missing.status_code, 200)
                html_missing = r_missing.get_data(as_text=True)
                hero_missing = html_missing.split('<article class="live-hero-card"', 1)[1].split("</article>", 1)[0]
                self.assertIn("ライブデータ取得待機中", hero_missing)
                self.assertIn("取得待機中", hero_missing)
                self.assertNotIn("営業中｜現在 0 : 0", hero_missing)
                self.assertNotIn("営業中（0 : 0）", hero_missing)
                self.assertNotIn("0 : 0（空席）", hero_missing)

                r_zero = self.client.get("/store/oriental-lounge-shibuya-honten")
                self.assertEqual(r_zero.status_code, 200)
                html_zero = r_zero.get_data(as_text=True)
                hero_zero = html_zero.split('<article class="live-hero-card"', 1)[1].split("</article>", 1)[0]
                self.assertIn("営業中｜現在 0 : 0", hero_zero)
                self.assertIn("営業中（0 : 0）", hero_zero)
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = saved_full
            app_module.clear_fast_http_caches(clear_editorial=True)

        # stats_monthly_csv (H-1): real DB failure paths (_fetch_all_rows exception & _db_available=False)
        # -> 503 without last_good, last_good 200 fallback when cached, 200 header-only CSV on genuine 0 rows.
        portal_bp_mod.clear_monthly_csv_cache()
        local_db.clear_monthly_stats_rows_cache()
        with mock.patch.object(local_db, "_db_available", return_value=True), \
             mock.patch.object(local_db, "_fetch_all_rows", side_effect=RuntimeError("simulated db failure")):
            self.assertEqual(local_db.get_monthly_store_stats_rows(days=30), [])
            r_503_exc = self.client.get("/stats/monthly.csv?days=30")
            self.assertEqual(r_503_exc.status_code, 503)
            self.assertEqual(r_503_exc.headers.get("Retry-After"), "30")
            self.assertEqual(r_503_exc.headers.get("Cache-Control"), "no-store")

        portal_bp_mod.clear_monthly_csv_cache()
        local_db.clear_monthly_stats_rows_cache()
        with mock.patch.object(local_db, "_db_available", return_value=False):
            self.assertEqual(local_db.get_monthly_store_stats_rows(days=30), [])
            r_503_unavail = self.client.get("/stats/monthly.csv?days=30")
            self.assertEqual(r_503_unavail.status_code, 503)
            self.assertEqual(r_503_unavail.headers.get("Retry-After"), "30")
            self.assertEqual(r_503_unavail.headers.get("Cache-Control"), "no-store")

        # Seed _MONTHLY_CSV_LAST_GOOD via a real _fetch_all_rows query, then expire primary TTL cache only
        sample_db_rows = [("OLG 新宿", 12, 10.5, 22, 8.0, 16, 56.8)]
        portal_bp_mod.clear_monthly_csv_cache()
        local_db.clear_monthly_stats_rows_cache()
        with mock.patch.object(local_db, "_db_available", return_value=True), \
             mock.patch.object(local_db, "_fetch_all_rows", return_value=sample_db_rows):
            r_good = self.client.get("/stats/monthly.csv?days=30")
            self.assertEqual(r_good.status_code, 200)
            good_csv = r_good.get_data(as_text=True)
            self.assertIn("oriental-lounge-shinjuku", good_csv)

        with portal_bp_mod._MONTHLY_CSV_LOCK:
            portal_bp_mod._MONTHLY_CSV_CACHE.clear()
        local_db.clear_monthly_stats_rows_cache()
        with mock.patch.object(local_db, "_db_available", return_value=True), \
             mock.patch.object(local_db, "_fetch_all_rows", side_effect=RuntimeError("db down after warm")):
            r_fb_exc = self.client.get("/stats/monthly.csv?days=30")
            self.assertEqual(r_fb_exc.status_code, 200)
            self.assertEqual(r_fb_exc.get_data(as_text=True), good_csv)

        with portal_bp_mod._MONTHLY_CSV_LOCK:
            portal_bp_mod._MONTHLY_CSV_CACHE.clear()
        local_db.clear_monthly_stats_rows_cache()
        with mock.patch.object(local_db, "_db_available", return_value=False):
            r_fb_unavail = self.client.get("/stats/monthly.csv?days=30")
            self.assertEqual(r_fb_unavail.status_code, 200)
            self.assertEqual(r_fb_unavail.get_data(as_text=True), good_csv)

        # Genuine 0 rows from healthy DB returns 200 header-only CSV (not 503)
        portal_bp_mod.clear_monthly_csv_cache()
        local_db.clear_monthly_stats_rows_cache()
        with mock.patch.object(local_db, "_db_available", return_value=True), \
             mock.patch.object(local_db, "_fetch_all_rows", return_value=[]):
            r_200 = self.client.get("/stats/monthly.csv?days=30")
            self.assertEqual(r_200.status_code, 200)
            self.assertEqual(r_200.headers.get("Cache-Control"), "no-cache, max-age=0")
            csv_txt = r_200.get_data(as_text=True)
            self.assertTrue(csv_txt.startswith("store_slug,store_name,brand_name,"))
        portal_bp_mod.clear_monthly_csv_cache()
        local_db.clear_monthly_stats_rows_cache()

    # 24. M4 Fix4: staff_pdca.html renders HTTP 200 on empty diagnostics={} and None-valued fields
    def test_m4_fix4_staff_pdca_none_and_empty_diagnostics_no_500(self):
        import pdca_engine

        self._login_master()
        empty_diag_payload = {"diagnostics": {}}
        none_fields_payload = {
            "settings": {"auto_optimize_sns": False, "auto_replenish_columns": False},
            "briefing": {"is_ai": False, "text": None, "pending_async": False},
            "gemini_status": None,
            "active_directives": {
                "sns_style_boosts": {"news": None, "tip": 0},
                "priority_column_paths": None,
                "recommended_custom_topic": None,
            },
            "diagnostics": {
                "overall_score": None,
                "upcoming_count": None,
                "sleeping_count": None,
                "demand_gap": {"top_hub": None},
                "funnel": {"pv_total": None, "article_views": None, "aff_total": None, "aff_ctr_pct": None},
                "revenue": {"est_monthly_pace_yen": None, "est_total_yen": None},
                "score_breakdown": {
                    "traffic_engagement": None,
                    "monetization_efficiency": None,
                    "sns_diversity_inflow": None,
                    "content_pipeline_health": None,
                },
                "proposed_directives": None,
                "offer_breakdown": [
                    {
                        "rank": None,
                        "category_key": "dating",
                        "label": None,
                        "clicks": None,
                        "share_pct": None,
                        "trend_7d": None,
                        "trend_7d_label": None,
                        "bar_pct": None,
                        "top_source_path": None,
                        "est_revenue_yen": None,
                    }
                ],
                "monetization_gaps": [
                    {
                        "path": None,
                        "title": None,
                        "severity": None,
                        "severity_badge": None,
                        "pv": None,
                        "aff_clicks": None,
                        "cvr_pct": None,
                        "target_cvr_pct": None,
                        "recommended_funnel": None,
                        "placement_position": None,
                        "action_proposal": None,
                    }
                ],
                "sns_channel_efficiency": {
                    "summary_insight": None,
                    "total_sns_visits": None,
                    "total_sns_conversions": None,
                    "total_search_visits": None,
                    "channels": [
                        {
                            "rank": None,
                            "channel_key": "x",
                            "label": None,
                            "visits": None,
                            "conversions": None,
                            "cvr_pct": None,
                            "bar_pct": None,
                            "status_badge": None,
                            "best_time_slot": None,
                            "best_style_label": None,
                        }
                    ],
                    "reels_recommendations": [
                        {"idea_no": None, "best_post_time": None, "title": None, "hook_0_3s": None, "body_outline": None, "cta_route": None}
                    ],
                },
                "closed_loop_feedback": {
                    "sample_counts": {"text_posts_with_views": None, "gsc_queries": None},
                    "hook_archetype_stats": [
                        {"label": None, "sample_count": None, "avg_views": None, "lift_ratio": None, "score_boost": None}
                    ],
                    "reel_diagnostics": {
                        "avg_watch_sec": None,
                        "sample_count": None,
                        "bandit_table": [
                            {"label": None, "sample_count": None, "avg_watch_sec": None, "avg_skip_rate_pct": None, "ucb_score": None, "bandit_boost": None}
                        ],
                    },
                    "youtube_shorts_diagnostics": {
                        "avg_stayed_to_watch_pct": None,
                        "sample_count": None,
                        "avg_pct_viewed": None,
                        "top_shorts": [
                            {"title": None, "views": None, "stayed_to_watch_pct": None, "avg_pct_viewed": None, "avg_watch_sec": None, "quality_score": None}
                        ],
                    },
                    "gsc_diagnostics": {
                        "total_impressions": None,
                        "finalized_through_date": "2026-11-17",
                        "window_start_date": None,
                        "w1_clicks": None,
                        "w2_clicks": None,
                        "top_queries": [
                            {"query": None, "impressions": None, "clicks": None, "ctr_pct": None, "position": None}
                        ],
                    },
                    "note_ctr_feedback": {"top_titles": [{"title": None, "views": None, "likes": None}]},
                },
                "flywheel_status": {
                    "moshimo_queue": None,
                    "youtube_shorts": None,
                    "search_console": None,
                    "column_pipeline": None,
                },
                "inflow_cohorts": [
                    {"label": None, "tag": None, "visits": None, "cushion_clicks": None, "cushion_rate_pct": None, "conversions": None, "cvr_pct": None, "est_revenue_yen": None, "epc_yen": None}
                ],
                "article_perf": [
                    {"path": None, "title": None, "status_badge": None, "pv": None, "aff_clicks": None, "ctr_pct": None}
                ],
            },
            "history": [
                {"timestamp": None, "overall_score": None, "focus_hub": None, "boosted_styles": None, "recommended_topic": None}
            ],
        }

        for label, payload in (("empty_diagnostics", empty_diag_payload), ("none_fields", none_fields_payload)):
            with mock.patch.object(pdca_engine, "get_active_directives", return_value={}), \
                 mock.patch.object(pdca_engine, "run_pdca_cycle", return_value=payload):
                resp = self.client.get("/staff/pdca?days=14")
                self.assertEqual(resp.status_code, 200, f"{label} returned HTTP {resp.status_code}")
                html_out = resp.get_data(as_text=True)
                self.assertIn("自律PDCA作戦参謀・グロース統括ボード", html_out)

    # 25. M4 Fix4: store_detail.html blocks client /api/track beacon for ?staff_test=true as well as ?staff_test=1
    def test_m4_fix4_store_detail_staff_test_true_no_track_beacon(self):
        from urllib.parse import urlsplit
        import store_hours

        r = self.client.get("/store/oriental-lounge-shinjuku?staff_test=true")
        self.assertEqual(r.status_code, 200)
        html_txt = r.get_data(as_text=True)
        self.assertNotIn("qsRaw.indexOf('staff_test=1') !== -1", html_txt)
        self.assertIn("staff_test=(?:1|true)", html_txt)

        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            return

        orig_hours_fn = store_hours.get_business_hours_status
        dt_open = datetime.datetime(2026, 11, 19, 21, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
        with app_module.data_lock:
            saved_full = list(app_module.latest_data.get("full_data") or [])
            app_module.latest_data["full_data"] = [
                {"name": "OLG 新宿", "women": 0, "men": 0, "total": 0, "is_open_now": False, "region": "Kanto"},
            ]
        try:
            app_module.clear_fast_http_caches(clear_editorial=True)
            with mock.patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None), \
                 mock.patch("store_hours.get_business_hours_status", side_effect=lambda s, now_jst=None, total_guests=0: orig_hours_fn(s, now_jst=dt_open, total_guests=total_guests)):
                with sync_playwright() as p:
                    browser = p.chromium.launch(headless=True)
                    try:
                        def _run_probe(path_qs):
                            tracked_events = []
                            ctx = browser.new_context(viewport={"width": 1280, "height": 800}, service_workers="block")
                            ctx.add_init_script("Object.defineProperty(navigator, 'webdriver', { get: () => false });")

                            def _handle_route(route):
                                u = urlsplit(route.request.url)
                                if u.netloc != "aiseki.local":
                                    route.abort()
                                    return
                                if u.path == "/api/track" and route.request.method == "POST":
                                    try:
                                        tracked_events.append(json.loads(route.request.post_data or "{}"))
                                    except Exception:
                                        tracked_events.append({})
                                    route.fulfill(status=204, body=b"")
                                    return
                                req_path = u.path + (f"?{u.query}" if u.query else "")
                                resp = self.client.open(req_path, method=route.request.method, headers=dict(route.request.headers))
                                hdrs = {k.lower(): v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
                                route.fulfill(status=resp.status_code, headers=hdrs, body=resp.get_data())

                            ctx.route("**/*", _handle_route)
                            page = ctx.new_page()
                            page.goto("http://aiseki.local" + path_qs, wait_until="domcontentloaded")
                            page.wait_for_timeout(150)
                            ctx.close()
                            return tracked_events

                        ev_staff_true = _run_probe("/store/oriental-lounge-shinjuku?staff_test=true")
                        self.assertEqual(ev_staff_true, [], f"Expected 0 /api/track requests on ?staff_test=true, got {ev_staff_true}")

                        ev_normal = _run_probe("/store/oriental-lounge-shinjuku")
                        self.assertTrue(
                            any(e.get("event") == "sister_bridge_impression" for e in ev_normal),
                            f"Positive control failed: expected sister_bridge_impression without staff_test, got {ev_normal}",
                        )
                    finally:
                        browser.close()
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = saved_full
            app_module.clear_fast_http_caches(clear_editorial=True)


if __name__ == "__main__":
    unittest.main()
