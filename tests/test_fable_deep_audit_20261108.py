"""
tests/test_fable_deep_audit_20261108.py - Claude Fable 5.1 (1M Context) バックエンド横断監査 回帰テストスイート

検証対象モジュール（Workstream 4）:
  - ops_governance.py (SEO名正規化フィルタ・SSRF厳格検証・マルチスレッド直列化ロック)
  - concierge_matcher.py (非表示店舗除外・全店非表示時耐障害性・未計測時の推測統計詐称根絶)
  - pdca_engine.py (同カテゴリ別スロット差分・複数SNSタグ流入・/stats除外・事前分布ラベル・配信ヘルス連携)
  - routes/staff_bp.py & routes/sns_jobs_bp.py (偽X-Admin-Secret遮断CSRF・クロスサイトGET防御・入力型安全・180sウォッチドッグ)
  - routes/push_line_bp.py & push_watch.py (非表示店舗購読拒否・staff_test=1非汚染・LINE 64KB 413・VAPID上書き保護)
  - monitor.py & local_db.py (OLG 20分ステール期限＆DB非汚染・JIS店舗単位例外分離＆負数ガード)
  - moat_intelligence.py & local_db.py (気象NULL保持・アリーナ急増デルタ・深夜ゴースト閾値分離・ASOBIBAR待ち組非汚染・空キャッシュ汚染防止・予測精度レポート分離＆非表示除外)
  - scheduled_articles.py (不正日時フェイルクローズ・ストレージ不健全時の重複X投稿抑止)
"""

import base64
import datetime
import json
import os
import shutil
import socket
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("SECRET_KEY", "test-fable-audit-secret-20261108")

import analytics  # noqa: E402
import app as app_module  # noqa: E402
import concierge_matcher  # noqa: E402
import job_state  # noqa: E402
import local_db  # noqa: E402
import moat_intelligence  # noqa: E402
import monitor  # noqa: E402
import ops_governance  # noqa: E402
import pdca_engine  # noqa: E402
import push_watch  # noqa: E402
import scheduled_articles  # noqa: E402
import turbo_store  # noqa: E402
import routes.sns_jobs_bp  # noqa: E402, F401
sns_jobs_mod = sys.modules["routes.sns_jobs_bp"]


def _generate_valid_push_keys():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    p256dh = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode("ascii")
    auth = base64.urlsafe_b64encode(b"0123456789abcdef").rstrip(b"=").decode("ascii")
    return p256dh, auth


class TestFableDeepBackendAudit20261108(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls._tmp_root = tempfile.mkdtemp(prefix="aiseki_fable_audit_")
        cls._prev_db_path = local_db.DB_PATH
        cls._prev_job_dir = os.environ.get("JOB_STATE_DIR")
        cls._job_dir = os.path.join(cls._tmp_root, "job_state")
        os.makedirs(cls._job_dir, exist_ok=True)
        os.environ["JOB_STATE_DIR"] = cls._job_dir

        local_db.DB_PATH = os.path.join(cls._tmp_root, "fable_audit.db")
        local_db.reset_caches_for_tests()
        local_db.init_db()
        job_state.reset_cache_for_tests()

        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()

    @classmethod
    def tearDownClass(cls):
        local_db.reset_caches_for_tests()
        local_db.DB_PATH = cls._prev_db_path
        local_db.reset_caches_for_tests()
        if cls._prev_job_dir is None:
            os.environ.pop("JOB_STATE_DIR", None)
        else:
            os.environ["JOB_STATE_DIR"] = cls._prev_job_dir
        job_state.reset_cache_for_tests()
        shutil.rmtree(cls._tmp_root, ignore_errors=True)

    def setUp(self):
        # Reset isolated job_state file & in-memory caches before every test
        state_file = os.path.join(self._job_dir, "job_state.json")
        if os.path.exists(state_file):
            os.remove(state_file)
        job_state.reset_cache_for_tests()
        local_db.reset_caches_for_tests()
        pdca_engine.reset_pdca_cache_for_tests()
        analytics.reset_for_tests()
        app_module.clear_fast_http_caches()
        try:
            turbo_store.clear_rate_limit("push_sub_ip")
            turbo_store.clear_rate_limit("push_unsub_ip")
        except Exception:
            pass

        conn = local_db.get_connection()
        try:
            local_db._ensure_moat_rollup_schema()
            for tbl in (
                "monitor_logs",
                "store_pos_telemetry_logs",
                "external_context_logs",
                "store_hourly_rollups",
                "day_context_logs",
                "forecast_accuracy_logs",
                "web_push_subscriptions",
                "store_watch_intents",
                "crowd_votes",
            ):
                conn.execute(f"DELETE FROM {tbl}")
            conn.commit()
        finally:
            conn.close()

        with monitor._olg_cache_lock:
            self._saved_olg_cache = {
                "data": list(monitor._olg_cache.get("data") or []),
                "last_success": monitor._olg_cache.get("last_success"),
            }

        with app_module.data_lock:
            self._saved_latest_data = dict(app_module.latest_data)
            app_module.latest_data["full_data"] = [
                {"name": "OLG 新宿", "women": 22, "men": 14, "region": "Kanto", "is_open_now": True},
                {"name": "JIS SHINJUKU", "women": 18, "men": 12, "region": "Kanto", "is_open_now": True},
                {"name": "JIS NISHISHINJUKU", "women": 12, "men": 10, "region": "Kanto", "is_open_now": True},
                {"name": "ASOBIBAR 新宿", "women": 15, "men": 13, "region": "Kanto", "is_open_now": True},
                {"name": "OLG 渋谷本店", "women": 20, "men": 15, "region": "Kanto", "is_open_now": True},
            ]
            app_module.latest_data["last_updated"] = "21:30"

    def tearDown(self):
        with monitor._olg_cache_lock:
            monitor._olg_cache.clear()
            monitor._olg_cache.update(self._saved_olg_cache)
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest_data)
        ops_governance._invalidate_unread_memo()
        app_module.clear_fast_http_caches()

    # ------------------------------------------------------------------
    # 1. ops_governance: SEO名フィルタ・SSRF厳格防御・並行ロック安全性
    # ------------------------------------------------------------------
    def test_ops_governance_seo_name_filter_ssrf_and_thread_safety(self):
        res_hide = ops_governance.set_store_hidden("オリエンタルラウンジ新宿", True)
        self.assertTrue(res_hide["ok"])
        self.assertEqual(res_hide["store_name"], "OLG 新宿")
        self.assertTrue(ops_governance.is_store_hidden("OLG 新宿"))
        self.assertTrue(ops_governance.is_store_hidden("オリエンタルラウンジ新宿"))

        mixed_list = [
            {"name": "オリエンタルラウンジ新宿", "women": 20, "men": 10},
            {"name": "JIS SHINJUKU", "women": 18, "men": 12},
        ]
        filtered = ops_governance.filter_visible_stores(mixed_list)
        self.assertEqual([s["name"] for s in filtered], ["JIS SHINJUKU"])

        # SSRF validation checks: credentials, non-443 port, non-global IP literals, DNS-resolved loopback/6PN
        self.assertFalse(ops_governance.set_webhook_url("https://user:secret@hooks.slack.com/services/T/B/X")["ok"])
        self.assertFalse(ops_governance.set_webhook_url("https://hooks.slack.com:8443/services/T/B/X")["ok"])
        self.assertFalse(ops_governance.set_webhook_url("https://0.0.0.0/hook")["ok"])
        self.assertFalse(ops_governance.set_webhook_url("https://100.64.0.1/hook")["ok"])

        with mock.patch("ops_governance.socket.getaddrinfo") as mock_gai:
            mock_gai.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("127.0.0.1", 443))]
            self.assertFalse(ops_governance.set_webhook_url("https://rebind.attacker.example/hook")["ok"])

            mock_gai.return_value = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("fdaa:0:1234::1", 443, 0, 0))]
            self.assertFalse(ops_governance.set_webhook_url("https://internal-6pn.attacker.example/hook")["ok"])

            mock_gai.return_value = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("93.184.216.34", 443))]
            ok_res = ops_governance.set_webhook_url("https://hooks.example.com/services/valid123456")
            self.assertTrue(ok_res["ok"])

        # Thread-safe concurrent inquiry writes must not drop entries
        barrier = threading.Barrier(10)

        def _worker(idx: int):
            barrier.wait(timeout=5.0)
            ops_governance.record_inquiry(
                {"category": "correction", "name": f"User{idx}", "email": "u@example.com", "message": f"msg-{idx}"},
                received_at="2026-11-08 21:00:00",
                ip_hash=f"hash{idx:02d}",
            )

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)

        saved_inquiries = ops_governance.list_inquiries(limit=50, allow_blocking=True)
        self.assertEqual(len(saved_inquiries), 10)

    # ------------------------------------------------------------------
    # 2. concierge_matcher: 非表示店舗除外・全店非表示耐性・虚偽統計表現の根絶
    # ------------------------------------------------------------------
    def test_concierge_hidden_store_exclusion_empty_area_resilience_and_no_fabricated_stats(self):
        now_jst = datetime.datetime(2026, 11, 7, 21, 30, tzinfo=concierge_matcher.JST)
        ops_governance.set_store_hidden("OLG 新宿", True)

        with mock.patch("local_db.get_all_stores_live_benchmarks", return_value={}):
            plan = concierge_matcher.diagnose_tonight_plan(
                live_stores=app_module.latest_data["full_data"],
                area="shinjuku",
                audience="men",
                timing="prime_21",
                party_size="pair",
                vibe="any",
                now_jst=now_jst,
            )

        all_returned = [plan["primary"]["display_name"]]
        if plan["backup"]:
            all_returned.append(plan["backup"]["display_name"])
        all_returned.extend(c["display_name"] for c in plan["other_candidates"])
        self.assertTrue(all("オリエンタルラウンジ新宿" not in nm and "OLG 新宿" not in nm for nm in all_returned))

        # Unmeasured stores (`benchmarks={}`) must never fabricate 30-day empirical numbers
        joined_reasons = " / ".join([plan["primary"]["headline_reason"]] + plan["primary"]["reasons"])
        self.assertNotIn("過去30日ログ統計", joined_reasons)
        self.assertNotIn("定着率は84%", joined_reasons)
        self.assertNotIn("女性16名（比率57%）", joined_reasons)

        # Hide every store in sapporo_sendai and verify zero crash across engine + HTTP routes
        for sname in concierge_matcher.CONCIERGE_AREAS["sapporo_sendai"]["stores"]:
            ops_governance.set_store_hidden(sname, True)

        empty_plan = concierge_matcher.diagnose_tonight_plan(
            live_stores=[],
            area="sapporo_sendai",
            now_jst=now_jst,
        )
        self.assertTrue(empty_plan["ok"])
        self.assertEqual(empty_plan["primary"]["raw_name"], "")
        self.assertIsNone(empty_plan["backup"])

        r_html = self.client.get("/concierge?area=sapporo_sendai")
        self.assertEqual(r_html.status_code, 200)
        r_api = self.client.get("/api/concierge?area=sapporo_sendai")
        self.assertEqual(r_api.status_code, 200)
        self.assertTrue(r_api.get_json()["ok"])

    # ------------------------------------------------------------------
    # 3. pdca_engine: 同カテゴリ別スロット集計・複数SNSタグ・/stats除外・事前分布ラベル・配信ヘルス
    # ------------------------------------------------------------------
    def test_pdca_category_slot_diff_sns_tags_stats_exclusion_and_delivery_health(self):
        fake_stats = {
            "all_by_label": {
                "page_view": {
                    "/stats": 40,
                    "/guide/second-venue": 25,
                },
                "affiliate_click": {
                    "dating_instant @ /grooming": 3,
                },
                "cushion_click": {},
                "sns_inflow": {
                    "x20_hub_gap @ /": 5,
                },
                "store_open": {},
            },
            "by_event": {
                "aff_click:dating_instant": 3,
                "aff_click:dating_bachelor": 4,
                "sns_inflow:x20_hub_gap": 5,
                "sns_inflow:x22_surge_focus": 7,
            },
            "funnel": {
                "pv_total": 65,
                "aff_total": 7,
                "aff_ctr_pct": 10.7,
                "engagement_rate_pct": 22.0,
                "store_open_total": 8,
                "map_total": 2,
            },
        }
        cat_clicks, _ = pdca_engine._extract_aff_category_stats(fake_stats)
        self.assertEqual(cat_clicks["dating"], 7)

        fake_deliv_health = {
            "threads": {
                "label": "Threads",
                "state": "error",
                "last_error": "Token expired (HTTP 401)",
            }
        }
        with mock.patch("analytics.summary", return_value=fake_stats), mock.patch(
            "local_db.get_all_stores_hourly_waveforms", return_value={}
        ), mock.patch("sns_editorial.get_sns_delivery_health", return_value=fake_deliv_health):
            diag = pdca_engine.analyze_site_performance(live_stores=[], days=14, allow_blocking=False)

        # Sibling SNS tags (`x20_hub_gap`=5 + `x22_surge_focus`=7) must both be counted under X (=12)
        x_ch = next(c for c in diag["sns_channel_efficiency"]["channels"] if c["channel_key"] == "x")
        self.assertEqual(x_ch["visits"], 12)

        # Public open-data page `/stats` must never be flagged as a monetization gap
        gap_paths = [g["path"] for g in diag["monetization_gaps"]]
        self.assertNotIn("/stats", gap_paths)
        self.assertIn("/guide/second-venue", gap_paths)

        # Off-hours prior fallback (`live_stores=[]`, `waveforms={}`) must mark Reels idea #1 as `prior_estimate`
        reel1 = diag["sns_channel_efficiency"]["reels_recommendations"][0]
        self.assertEqual(reel1["data_status"], "prior_estimate")
        self.assertNotIn("実測ギャップ", reel1["body_outline"])

        # Delivery health error from `sns_editorial.get_sns_delivery_health` must surface in PDCA issues
        issue_titles = " | ".join(it["title"] for it in diag["issues"])
        self.assertIn("SNS配信ヘルス警告", issue_titles)

    # ------------------------------------------------------------------
    # 4. staff_bp & sns_jobs_bp: 偽造X-Admin-Secret遮断・クロスサイトGET防御・入力型安全・180sウォッチドッグ
    # ------------------------------------------------------------------
    def test_staff_and_sns_csrf_forged_secret_cross_site_get_and_consumer_watchdog(self):
        with mock.patch.dict(os.environ, {"ADMIN_API_SECRET": "real-master-secret-999"}):
            with self.client.session_transaction() as sess:
                sess["is_staff"] = True
                sess["is_master"] = True

            cross_origin_forged_headers = {
                "Origin": "https://evil.attacker.example",
                "Sec-Fetch-Site": "cross-site",
                "X-Admin-Secret": "forged-wrong-secret",
            }
            for endpoint, body in (
                ("/api/ops/governance", {"action": "toggle_brand", "brand_key": "jis", "hidden": True}),
                ("/api/pdca/run", {"days": 7}),
                ("/api/sns-jobs/enqueue", {"job_type": "x_post", "worker_only": True}),
                ("/api/trigger-x-post?async=1&worker_only=1", {}),
            ):
                resp = self.client.post(endpoint, json=body, headers=cross_origin_forged_headers)
                self.assertEqual(resp.status_code, 403, msg=f"Expected 403 for forged secret on {endpoint}")

            # Valid machine-to-machine X-Admin-Secret must succeed without Origin header
            m2m_resp = self.client.post(
                "/api/sns-jobs/enqueue",
                json={"job_type": "x_post", "worker_only": True},
                headers={"X-Admin-Secret": "real-master-secret-999"},
            )
            self.assertEqual(m2m_resp.status_code, 202)

            # Cross-site GET with `?refresh_ai=1` must suppress `force_ai_refresh`
            with mock.patch("pdca_engine.run_pdca_cycle", return_value={"ok": True}) as mock_pdca:
                r_cs = self.client.get(
                    "/api/pdca?refresh_ai=1",
                    headers={"Sec-Fetch-Site": "cross-site"},
                )
                self.assertEqual(r_cs.status_code, 200)
                self.assertFalse(mock_pdca.call_args.kwargs.get("force_ai_refresh"))

            # Malformed `days` or non-dict JSON on POST `/api/pdca/run` must not 500
            same_origin_headers = {"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}
            with mock.patch("pdca_engine.run_pdca_cycle", return_value={"ok": True}) as mock_run:
                r_bad_days = self.client.post(
                    "/api/pdca/run",
                    json={"days": "not-an-integer"},
                    headers=same_origin_headers,
                )
                self.assertEqual(r_bad_days.status_code, 200)
                self.assertEqual(mock_run.call_args.kwargs.get("days"), 14)

                r_empty_list_json = self.client.post(
                    "/api/pdca/run",
                    data="[]",
                    content_type="application/json",
                    headers=same_origin_headers,
                )
                self.assertEqual(r_empty_list_json.status_code, 200)

                r_nonempty_list_json = self.client.post(
                    "/api/pdca/run",
                    data='["unexpected-array"]',
                    content_type="application/json",
                    headers=same_origin_headers,
                )
                self.assertEqual(r_nonempty_list_json.status_code, 400)

        # Inline SNS consumer 180s hang watchdog recovery test
        hung_dummy = mock.MagicMock()
        hung_dummy.is_alive.return_value = True
        with sns_jobs_mod._inline_sns_consumer_lock:
            sns_jobs_mod._inline_sns_consumer_running = True
            sns_jobs_mod._inline_sns_consumer_started_at = time.monotonic() - 250.0
            sns_jobs_mod._inline_sns_consumer_thread = hung_dummy

        with mock.patch.object(app_module, "drain_sns_job_queue_once", return_value=None):
            sns_jobs_mod._ensure_inline_sns_consumer_started()
            self.assertIsNot(sns_jobs_mod._inline_sns_consumer_thread, hung_dummy)
            new_thread = sns_jobs_mod._inline_sns_consumer_thread
        if new_thread is not None:
            new_thread.join(timeout=2.0)

    # ------------------------------------------------------------------
    # 5. push_line_bp & push_watch: 非表示店舗拒否・staff_test=1非汚染・LINE 413・VAPID保護
    # ------------------------------------------------------------------
    def test_push_hidden_store_staff_test_non_pollution_line_413_and_vapid_guard(self):
        p256dh, auth = _generate_valid_push_keys()
        ops_governance.set_store_hidden("JIS SHINJUKU", True)

        # Hidden store subscription must be rejected with 400
        r_hidden = self.client.post(
            "/api/push/subscribe",
            json={
                "store_name": "JIS SHINJUKU",
                "target_cond": "female_55",
                "subscription": {
                    "endpoint": "https://fcm.googleapis.com/fcm/send/hidden-tok-1",
                    "keys": {"p256dh": p256dh, "auth": auth},
                },
            },
        )
        self.assertEqual(r_hidden.status_code, 400)

        # `?staff_test=1` subscription must NOT increment `store_watch_intents`
        r_staff_probe = self.client.post(
            "/api/push/subscribe?staff_test=1",
            json={
                "store_name": "OLG 渋谷本店",
                "target_cond": "female_55",
                "subscription": {
                    "endpoint": "https://fcm.googleapis.com/fcm/send/staff-probe-tok-1",
                    "keys": {"p256dh": p256dh, "auth": auth},
                },
            },
        )
        self.assertEqual(r_staff_probe.status_code, 200)
        intent_cnt_after_probe = local_db._fetch_all_rows("SELECT COUNT(*) FROM store_watch_intents")[0][0]
        self.assertEqual(intent_cnt_after_probe, 0)

        # Real visitor subscription DOES increment `store_watch_intents`
        r_visitor = self.client.post(
            "/api/push/subscribe",
            json={
                "store_name": "OLG 渋谷本店",
                "target_cond": "female_55",
                "subscription": {
                    "endpoint": "https://fcm.googleapis.com/fcm/send/real-visitor-tok-2",
                    "keys": {"p256dh": p256dh, "auth": auth},
                },
            },
        )
        self.assertEqual(r_visitor.status_code, 200)
        intent_cnt_after_visitor = local_db._fetch_all_rows("SELECT COUNT(*) FROM store_watch_intents")[0][0]
        self.assertEqual(intent_cnt_after_visitor, 1)

        # LINE webhook >64KB payload must be rejected with HTTP 413 before HMAC signature verification
        with mock.patch("push_watch.is_line_bot_configured", return_value=True), mock.patch(
            "push_watch.verify_line_signature"
        ) as mock_verify:
            oversized_body = b"x" * 70000
            r_line = self.client.post(
                "/api/line/webhook",
                data=oversized_body,
                content_type="application/json",
                headers={"X-Line-Signature": "dummy"},
            )
            self.assertEqual(r_line.status_code, 413)
            mock_verify.assert_not_called()

        # VAPID key rotation protection during transient storage outage
        prev_cached = push_watch._cached_vapid_keys
        try:
            push_watch._cached_vapid_keys = None
            fake_modules = {k: v for k, v in sys.modules.items() if k not in ("unittest", "pytest")}
            with mock.patch.dict(os.environ, {"VAPID_PUBLIC_KEY": "", "VAPID_PRIVATE_KEY_PEM": "", "PUSH_TEST_ISOLATED": "0"}), mock.patch.dict(
                sys.modules, fake_modules, clear=True
            ), mock.patch("job_state.get_value", return_value=None), mock.patch(
                "job_state.is_storage_healthy", return_value=False
            ), mock.patch("job_state.set_value") as mock_set_val:
                ephemeral = push_watch.get_or_create_vapid_keys(allow_blocking=True)
                self.assertTrue(ephemeral.get("public_key"))
                mock_set_val.assert_not_called()
                self.assertIsNone(push_watch._cached_vapid_keys)
        finally:
            push_watch._cached_vapid_keys = prev_cached

    # ------------------------------------------------------------------
    # 6. monitor & local_db: OLG 20分ステール期限・DB非汚染・JIS店舗単位例外分離
    # ------------------------------------------------------------------
    def test_monitor_stale_20m_expiry_db_non_pollution_and_jis_store_isolation(self):
        now_naive = monitor._now_jst_naive()

        # 5-minute-old cache -> returned with `stale=True`
        with monitor._olg_cache_lock:
            monitor._olg_cache["data"] = [{"name": "OLG 新宿", "men": 11, "women": 19, "source": "oriental"}]
            monitor._olg_cache["last_success"] = (now_naive - datetime.timedelta(minutes=5)).strftime("%Y-%m-%d %H:%M:%S")
            fresh_stale = monitor._fresh_stale_olg_items_unlocked()
        self.assertEqual(len(fresh_stale), 1)
        self.assertTrue(fresh_stale[0].get("stale"))

        # 25-minute-old cache (>20m) -> expired and discarded (`[]`)
        with monitor._olg_cache_lock:
            monitor._olg_cache["last_success"] = (now_naive - datetime.timedelta(minutes=25)).strftime("%Y-%m-%d %H:%M:%S")
            expired_stale = monitor._fresh_stale_olg_items_unlocked()
        self.assertEqual(expired_stale, [])

        # `local_db.insert_logs` must drop `stale=True` rows while persisting genuinely fresh rows
        ts_str = "2026-11-08 21:10:00"
        inserted = local_db.insert_logs(
            [
                {"name": "OLG 新宿", "men": 11, "women": 19, "source": "oriental", "stale": True},
                {"name": "JIS SHINJUKU", "men": 14, "women": 22, "source": "jis"},
            ],
            ts_str,
        )
        self.assertEqual(inserted, 1)
        saved_names = [r[0] for r in local_db._fetch_all_rows("SELECT store_name FROM monitor_logs")]
        self.assertEqual(saved_names, ["JIS SHINJUKU"])

        # `monitor.get_jis_data` must isolate per-store schema breakage and clamp negative numbers to 0
        jis_payload = {
            "shinjuku": {"shared": {"mens_customer_num": "broken_not_int", "ladys_customer_num": 10}},
            "umeda": {
                "shared": {
                    "mens_customer_num": -5,
                    "ladys_customer_num": 28,
                    "mens_waiting_num": -3,
                    "mens_waiting_uncounted_num": 1,
                }
            },
        }
        fake_html = f"<html><script>var datas = {json.dumps(jis_payload)};</script></html>"
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = fake_html
        fake_resp.content = fake_html.encode("utf-8")
        fake_resp.raise_for_status.return_value = None

        with mock.patch("monitor.requests.get", return_value=fake_resp):
            parsed_jis = monitor.get_jis_data()
        self.assertEqual(len(parsed_jis), 1)
        self.assertEqual(parsed_jis[0]["name"], "JIS UMEDA")
        self.assertEqual(parsed_jis[0]["men"], 0)
        self.assertEqual(parsed_jis[0]["women"], 28)
        self.assertEqual(parsed_jis[0]["pos_telemetry"]["men_waiting_groups"], 0)

    # ------------------------------------------------------------------
    # 7. moat_intelligence & local_db: 気象NULL保持・アリーナ急増デルタ・深夜ゴースト・ASOBIBAR非汚染・予測精度レポート
    # ------------------------------------------------------------------
    def test_moat_null_weather_arena_surge_midnight_ghost_asobibar_rollup_and_forecast_report(self):
        # 7a. Open-Meteo `None` temperature/precip must remain SQL NULL, never coerced to 0.0C
        fake_ctx = [
            {
                "city_key": "tokyo",
                "temp_c": None,
                "precip_mm": None,
                "weather_code": None,
                "wind_kmh": None,
                "is_rainy": False,
                "active_arenas_json": "[]",
                "transit_disrupted": 0,
                "transit_lines_json": "[]",
            }
        ]
        with mock.patch("moat_intelligence.capture_external_context_snapshot", return_value=fake_ctx):
            local_db.insert_logs(
                [{"name": "OLG 渋谷本店", "men": 12, "women": 18, "source": "oriental"}],
                "2026-11-07 21:00:00",
            )
        w_row = local_db._fetch_all_rows(
            "SELECT temp_c, precip_mm, weather_code, wind_kmh FROM external_context_logs WHERE city_key = 'tokyo'"
        )[0]
        self.assertIsNone(w_row[0])
        self.assertIsNone(w_row[1])
        self.assertIsNone(w_row[2])
        self.assertIsNone(w_row[3])

        # 7b. `detect_arena_event_surges`: negative `usual_w_diff` must not clobber positive `women_diff`
        concert_dt = datetime.datetime(2026, 11, 7, 21, 15, tzinfo=moat_intelligence.JST)
        with mock.patch("arena_calendar.get_arena_events_for_date", return_value={}):
            surges = moat_intelligence.detect_arena_event_surges(
                [{"name": "OLG 横浜", "women": 20, "men": 10, "usual_w_diff": -3, "women_diff": 8}],
                now_jst=concert_dt,
            )
        self.assertIn("OLG 横浜", surges)
        self.assertEqual(surges["OLG 横浜"]["surge_women_delta"], 8)

        # 7c. `_is_ghost_multistore_batch`: 20-store snapshot with 5 open stores & 48 guests
        # is legitimate on a 01:10 weekday night (kept -> False), whereas at 21:10 prime time it is a ghost batch (dropped -> True)
        closed_padding = {f"OLG 閉店テスト{i}": (0, 0) for i in range(15)}
        late_multistore_smap = {
            "OLG 新宿": (6, 4),
            "JIS SHINJUKU": (5, 3),
            "OLG 渋谷本店": (6, 5),
            "JIS UMEDA": (7, 4),
            "OLG 福岡": (5, 3),
            **closed_padding,
        }
        self.assertFalse(local_db._is_ghost_multistore_batch(datetime.datetime(2026, 11, 4, 1, 10, 0), late_multistore_smap))
        self.assertTrue(local_db._is_ghost_multistore_batch(datetime.datetime(2026, 11, 4, 21, 10, 0), late_multistore_smap))

        # 7d. `asobibar_pos` must NOT pollute `avg_men_waiting_groups` with 0.0 in `rollup_store_hourly_stats`
        ts_rollup = "2026-11-06 22:10:00"
        local_db.insert_logs(
            [
                {
                    "name": "ASOBIBAR 新宿",
                    "men": 16,
                    "women": 18,
                    "source": "asobibar",
                    "pos_telemetry": {
                        "telemetry_type": "asobibar_pos",
                        "men_total_guests": 16,
                        "women_total_guests": 18,
                        "men_waiting_groups": 0,
                    },
                },
                {
                    "name": "JIS SHINJUKU",
                    "men": 25,
                    "women": 20,
                    "source": "jis",
                    "pos_telemetry": {
                        "telemetry_type": "jis_pos",
                        "men_total_guests": 25,
                        "women_total_guests": 20,
                        "men_waiting_groups": 4,
                        "men_waiting_guests": 8,
                        "matched_groups": 9,
                        "vip_guests": 2,
                    },
                },
            ],
            ts_rollup,
        )
        r_roll = local_db.rollup_store_hourly_stats(target_dates=["2026-11-06"])
        self.assertTrue(r_roll["ok"])
        asobi_roll = local_db._fetch_all_rows(
            "SELECT avg_men_waiting_groups FROM store_hourly_rollups WHERE date_jst = '2026-11-06' AND hour_jst = 22 AND store_name = 'ASOBIBAR 新宿'"
        )[0]
        jis_roll = local_db._fetch_all_rows(
            "SELECT avg_men_waiting_groups FROM store_hourly_rollups WHERE date_jst = '2026-11-06' AND hour_jst = 22 AND store_name = 'JIS SHINJUKU'"
        )[0]
        self.assertIsNone(asobi_roll[0])
        self.assertEqual(float(jis_roll[0]), 4.0)

        # 7e. Transient DB failure must NOT poison `_benchmark_base_cache` with empty base
        local_db._benchmark_base_cache.clear()
        with mock.patch("local_db._fetch_ts_window_in_7d_chunks", side_effect=RuntimeError("simulated sqlite busy")):
            local_db.get_all_stores_live_benchmarks(allow_blocking=True, force_refresh=True)
        self.assertEqual(len(local_db._benchmark_base_cache), 0)

        # 7f. `get_forecast_accuracy_report`: shared cache across `limit_recent`, live vs backfill split, hidden store exclusion
        now_fc = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            for idx in range(5):
                p_ts = (now_fc - datetime.timedelta(minutes=180 - idx * 10)).strftime("%Y-%m-%d %H:%M:%S")
                r_ts = (now_fc - datetime.timedelta(minutes=120 - idx * 10)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct, v1_pred_wpct,
                        resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt,
                        v1_abs_error_wpct_pt, error_women, error_men, is_backfill
                    ) VALUES (?, ?, ?, ?, 20, 15, 57, 22, 16, 58, 56, ?, 21, 16, 57, 1, 1, 2, 1, 0, 0)
                    """,
                    (p_ts, r_ts, "OLG 渋谷本店", moat_intelligence.FORECAST_MODEL_VERSION, r_ts),
                )
            bf_pred = (now_fc - datetime.timedelta(minutes=75)).strftime("%Y-%m-%d %H:%M:%S")
            bf_res = (now_fc - datetime.timedelta(minutes=15)).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute(
                """
                INSERT INTO forecast_accuracy_logs (
                    predicted_at, target_at, store_name, model_version,
                    start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct, v1_pred_wpct,
                    resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt,
                    v1_abs_error_wpct_pt, error_women, error_men, is_backfill
                ) VALUES (?, ?, ?, ?, 18, 12, 60, 19, 13, 59, 55, ?, 18, 14, 56, 3, 3, 4, 1, -1, 1)
                """,
                (bf_pred, bf_res, "JIS SHINJUKU", moat_intelligence.FORECAST_MODEL_VERSION, bf_res),
            )
            conn.commit()
        finally:
            conn.close()

        local_db.clear_store_7d_cache()
        rep_a = local_db.get_forecast_accuracy_report(days=14, limit_recent=5, allow_blocking=True, auto_backfill=False)
        rep_b = local_db.get_forecast_accuracy_report(days=14, limit_recent=20, allow_blocking=True, auto_backfill=False)
        self.assertEqual(len(local_db._forecast_acc_report_cache), 1)
        self.assertEqual(len(rep_a["recent_resolved"]), 5)
        self.assertEqual(len(rep_b["recent_resolved"]), 6)
        self.assertEqual(rep_b["summary"]["live_resolved_count"], 5)
        self.assertEqual(rep_b["summary"]["backfill_count"], 1)

        ops_governance.set_store_hidden("JIS SHINJUKU", True)
        local_db.clear_store_7d_cache()
        rep_hidden = local_db.get_forecast_accuracy_report(days=14, limit_recent=20, allow_blocking=True, auto_backfill=False)
        self.assertEqual(rep_hidden["summary"]["resolved_count"], 5)
        self.assertTrue(all("JIS新宿" not in r["store_name"] and "JIS SHINJUKU" not in r["store_name"] for r in rep_hidden["recent_resolved"]))

    # ------------------------------------------------------------------
    # 8. scheduled_articles: 不正日時フェイルクローズ・ストレージ不健全時の重複X投稿防止
    # ------------------------------------------------------------------
    def test_scheduled_articles_malformed_publish_at_and_unhealthy_storage_guard(self):
        parsed_bad = scheduled_articles.parse_publish_at("not-a-valid-timestamp")
        self.assertEqual(parsed_bad.year, 9999)
        self.assertFalse(
            scheduled_articles.is_published(
                {"slug": "draft-broken", "publish_at": "not-a-valid-timestamp"},
                now_jst=datetime.datetime(2026, 11, 8, 20, 0, 0),
            )
        )

        with mock.patch("job_state.is_storage_healthy", return_value=False), mock.patch("x_poster.post_to_x") as mock_x:
            res = scheduled_articles.check_and_announce_newly_published(
                app_url="https://aiseki-navi.com",
                now_jst=datetime.datetime(2026, 11, 8, 13, 0, 0),
            )
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["result"]["status"], "skipped")
            mock_x.assert_not_called()

    # ------------------------------------------------------------------
    # 9. turbo_store & affiliate_manager: PGバッチ行数合算・PoolError分離・レート制限上限・アフィリ保存保護
    # ------------------------------------------------------------------
    def test_turbo_store_pg_batch_rate_limit_cap_and_affiliate_guard(self):
        import affiliate_manager

        # 9a. `execute_pg_values_batch` must sum `cur.rowcount` across multi-page chunks
        fake_cur = mock.MagicMock()

        def _fake_exec_values(cur, sql, chunk, page_size=500):
            cur.rowcount = len(chunk)

        fake_conn = mock.MagicMock()
        fake_conn.cursor.return_value.__enter__.return_value = fake_cur

        @mock.MagicMock()
        def _unused():
            pass

        import contextlib

        @contextlib.contextmanager
        def _fake_pg_conn():
            yield fake_conn

        with mock.patch.object(turbo_store, "is_postgres_active", return_value=True), mock.patch.object(
            turbo_store, "pg_connection", _fake_pg_conn
        ), mock.patch.object(turbo_store, "_pg_extras") as mock_extras:
            mock_extras.execute_values.side_effect = _fake_exec_values
            total_ins = turbo_store.execute_pg_values_batch(
                "INSERT INTO t VALUES %s", [(1,), (2,), (3,), (4,), (5,)], page_size=2
            )
        self.assertEqual(total_ins, 5)

        # 9b. `pg_connection` must NOT call `_mark_pg_failed` on transient `PoolError`
        if turbo_store._pg_pool is not None:
            fake_pool = mock.MagicMock()
            fake_pool.getconn.side_effect = turbo_store._pg_pool.PoolError("pool exhausted")
            with mock.patch.object(turbo_store, "_get_pg_pool", return_value=fake_pool), mock.patch.object(
                turbo_store, "_mark_pg_failed"
            ) as mock_mark_failed:
                with self.assertRaises(RuntimeError):
                    with turbo_store.pg_connection():
                        pass
                mock_mark_failed.assert_not_called()

        # 9c. In-memory `_mem_rate_limits` must cap around 4096 keys even under active-window IP flood
        try:
            now_fixed = 1800000000.0
            with mock.patch.object(turbo_store, "get_redis_client", return_value=None):
                for idx in range(4150):
                    turbo_store.record_rate_limit_hit("flood_ns", f"ip_{idx}", window_sec=600, now=now_fixed)
            self.assertLessEqual(len(turbo_store._mem_rate_limits), 4096)
        finally:
            turbo_store.clear_rate_limit("flood_ns")

        # 9d. `set_affiliate_slot_override` must block writes when storage is unhealthy and preserve `_mem_overrides`
        affiliate_manager.reset_cache_for_tests()
        with affiliate_manager._lock:
            affiliate_manager._mem_overrides = {
                "dating_instant": {"url": "https://px.a8.net/svt/ejp?a8mat=EXISTING1", "memo": "keep", "updated_at": "2026-11-08 07:00"}
            }
        try:
            with mock.patch("job_state.is_storage_healthy", return_value=False), mock.patch(
                "job_state.set_value"
            ) as mock_aff_save:
                res_blocked = affiliate_manager.set_affiliate_slot_override(
                    "spu_styleup", "https://h.accesstrade.net/sp/cc?rk=0100oo8500p144", memo="attempt"
                )
                self.assertFalse(res_blocked["ok"])
                mock_aff_save.assert_not_called()
            with affiliate_manager._lock:
                self.assertIn("dating_instant", affiliate_manager._mem_overrides)
        finally:
            affiliate_manager.reset_cache_for_tests()

    # ------------------------------------------------------------------
    # 10. 新設7エリア記事・63店舗カルテ・staff_test=1完全非汚染・ランダム?s=遮断・非表示店舗API防御
    # ------------------------------------------------------------------
    def test_area_pillars_store_dossiers_staff_test_non_pollution_and_cache_s_guard(self):
        import re
        import store_dossiers
        import store_master
        import x_poster

        # 10a. All 63 master stores must have complete 5-field editorial dossiers and render on `/store/<slug>`
        master = store_master.load()
        self.assertGreaterEqual(len(master), 63)
        for sname in master:
            d = store_dossiers.get_store_dossier(sname)
            self.assertIsNotNone(d, msg=f"Missing store dossier for {sname}")
            for req_key in ("catch", "street_vibe", "mens_tactic", "womens_tactic", "hopping_note"):
                self.assertTrue(d.get(req_key), msg=f"Empty {req_key} in dossier for {sname}")

        r_store = self.client.get("/store/oriental-lounge-shinjuku?staff_test=1")
        self.assertEqual(r_store.status_code, 200)
        store_html = r_store.get_data(as_text=True)
        self.assertIn('id="store-editorial-dossier"', store_html)

        # 10b. All 7 newly launched regional pillar columns return 200, contain 2-step cushion CTAs, zero direct `/link/`, and valid X weight
        new_slugs = (
            "shibuya-ebisu-comparison",
            "ueno-kinshicho-machida",
            "yokohama-omiya-kanto",
            "nagoya-sakae-nishiki",
            "fukuoka-tenjin-kyushu",
            "hiroshima-okayama-matsuyama",
            "sapporo-sendai-north",
        )
        promo_by_slug = {it["slug"]: it for it in x_poster.COLUMN_PROMO_LIST}
        for slug in new_slugs:
            r_col = self.client.get(f"/guide/{slug}?staff_test=1")
            self.assertEqual(r_col.status_code, 200, msg=f"/guide/{slug} failed")
            col_html = r_col.get_data(as_text=True)
            self.assertNotIn("/link/", col_html, msg=f"Direct /link/ found in /guide/{slug}")
            self.assertIn('data-cushion="1"', col_html, msg=f"Missing 2-step cushion CTA in /guide/{slug}")
            self.assertIsNone(re.search(r"オリエンタル(?!ラウンジ)", col_html), msg=f"Bare オリエンタル in /guide/{slug}")
            self.assertIn(slug, promo_by_slug)
            self.assertLessEqual(x_poster.compute_twitter_weight(promo_by_slug[slug]["main_text"]), 270)

        # 10c. Browser UA with `?staff_test=1` (and `/stats/monthly.csv`) must NEVER pollute analytics, votes, or watch_intents
        analytics.reset_for_tests()
        browser_headers = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15"}
        self.assertEqual(self.client.get("/?staff_test=1", headers=browser_headers).status_code, 200)
        self.assertEqual(self.client.get("/stats/monthly.csv?days=7", headers=browser_headers).status_code, 200)
        self.assertEqual(
            self.client.post(
                "/api/track",
                json={"event": "store_open", "label": "OLG 新宿"},
                headers={**browser_headers, "Referer": "https://aiseki-navi.com/?staff_test=1"},
            ).status_code,
            204,
        )
        self.assertEqual(
            self.client.post(
                "/api/vote?staff_test=1",
                json={"store_name": "OLG 渋谷本店", "vote_type": "smooth", "voter_id": "probe1"},
                headers=browser_headers,
            ).status_code,
            200,
        )
        self.assertEqual(
            self.client.post(
                "/api/watch-intent?staff_test=1",
                json={"store_name": "OLG 渋谷本店", "target_cond": "female_55", "voter_id": "probe1"},
                headers=browser_headers,
            ).status_code,
            200,
        )
        self.assertEqual(len(analytics._pending), 0)
        self.assertEqual(local_db._fetch_all_rows("SELECT COUNT(*) FROM crowd_votes")[0][0], 0)
        self.assertEqual(local_db._fetch_all_rows("SELECT COUNT(*) FROM store_watch_intents")[0][0], 0)

        # 10d. Random `?s=<garbage>` must NOT flip `hasSnsInflow` or fragment SSR cache, and hidden store `/api/analysis` returns 404
        garbage_html = self.client.get("/?s=random_fuzz_99999&staff_test=1", headers=browser_headers).get_data(as_text=True)
        self.assertNotIn('"hasSnsInflow": true', garbage_html)
        self.assertNotIn("random_fuzz_99999", garbage_html)

        ops_governance.set_store_hidden("OLG 新宿", True)
        self.assertEqual(self.client.get("/api/analysis/OLG%20%E6%96%B0%E5%AE%BF").status_code, 404)
        self.assertEqual(
            self.client.post("/api/vote", json={"store_name": "OLG 新宿", "vote_type": "smooth"}).status_code,
            400,
        )
        self.assertEqual(
            self.client.post("/api/watch-intent", json={"store_name": "OLG 新宿", "target_cond": "female_55"}).status_code,
            400,
        )


if __name__ == "__main__":
    unittest.main()
