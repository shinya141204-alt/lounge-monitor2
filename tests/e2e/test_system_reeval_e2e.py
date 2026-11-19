# -*- coding: utf-8 -*-
"""
tests/e2e/test_system_reeval_e2e.py

要件ベース 4層E2E・非汚染検証スイート（F1〜F7 & R1〜R2）
- Tier 1: Feature Coverage (F1〜F7 各5件以上, 計40件)
- Tier 2: Boundary / Corner / Security / Non-Contamination (F1〜F7 各5件以上, 計41件)
- Tier 3: Cross-Feature Combinations (8件)
- Tier 4: Real-World Workloads, Perf Benchmark & Quality Gates (5件)

重要制約:
- `tests/e2e/__init__.py` は作成しない（`unittest discover -s tests` への混入防止）。
- 本番DB/Redis/Sheets、ローカル永続ファイル、`tests/test_*.py`、`.agents/reports/` を一切汚染しない。
- 実行コマンド:
  DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest discover -s tests/e2e -v
"""

import atexit
import base64
import copy
import csv
import datetime
import gzip
import hashlib
import hmac
import importlib.util
import io
import json
import os
import re
import shutil
import sqlite3
import ssl
import statistics
import sys
import tempfile
import threading
import time
import tracemalloc
import unittest
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from unittest.mock import MagicMock, patch

# =====================================================================
# Import前 完全隔離・非汚染ハーネス
# =====================================================================
REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_E2E_TMP_DIR = tempfile.mkdtemp(prefix="aiseki_reeval_e2e_")
atexit.register(lambda: shutil.rmtree(_E2E_TMP_DIR, ignore_errors=True))

_SAVED_ENV_KEYS = (
    "APP_ROLE",
    "DISABLE_SCHEDULER",
    "DISABLE_SHEETS",
    "FLASK_ENV",
    "LOCAL_DB_PATH",
    "JOB_STATE_DIR",
    "APP_LOG_PATH",
    "LATEST_STATUS_SNAPSHOT_FILE",
    "SECRET_KEY",
    "STAFF_PASSCODE",
    "MASTER_PASSCODE",
    "ADMIN_API_SECRET",
    "DATABASE_URL",
    "REDIS_URL",
    "ALERT_WEBHOOK_URL",
)
_SAVED_ENV = {k: os.environ.get(k) for k in _SAVED_ENV_KEYS}


def _apply_isolated_env():
    os.environ["DISABLE_SCHEDULER"] = "1"
    os.environ["DISABLE_SHEETS"] = "1"
    os.environ["FLASK_ENV"] = "testing"
    os.environ["LOCAL_DB_PATH"] = os.path.join(_E2E_TMP_DIR, "e2e_lounge.db")
    os.environ["JOB_STATE_DIR"] = os.path.join(_E2E_TMP_DIR, "job_state")
    os.environ["APP_LOG_PATH"] = os.path.join(_E2E_TMP_DIR, "e2e_app.log")
    os.environ["LATEST_STATUS_SNAPSHOT_FILE"] = os.path.join(_E2E_TMP_DIR, "latest_status_cache.json")
    os.environ.setdefault("SECRET_KEY", "e2e-test-secret-key-20261119")
    os.environ.setdefault("STAFF_PASSCODE", "5555")
    os.environ.setdefault("MASTER_PASSCODE", "2918")
    os.environ.setdefault("ADMIN_API_SECRET", "lounge2026")
    os.environ.pop("DATABASE_URL", None)
    os.environ.pop("REDIS_URL", None)
    os.environ.pop("ALERT_WEBHOOK_URL", None)
    os.makedirs(os.environ["JOB_STATE_DIR"], exist_ok=True)


_apply_isolated_env()

import analytics  # noqa: E402
import app as app_module  # noqa: E402
import arena_calendar  # noqa: E402
import contact_form  # noqa: E402
import events  # noqa: E402
import health  # noqa: E402
import job_state  # noqa: E402
import local_db  # noqa: E402
import ops_governance  # noqa: E402
import push_watch  # noqa: E402
import request_guards  # noqa: E402
import store_master  # noqa: E402
import turbo_store  # noqa: E402

from cryptography.hazmat.primitives import serialization  # noqa: E402
from cryptography.hazmat.primitives.asymmetric import ec  # noqa: E402

_ORIG_DB_PATH = local_db.DB_PATH
_ORIG_EVENTS_CACHE_FILE = events.CACHE_FILE
_ORIG_ARENA_CACHE_FILE = arena_calendar.ARENA_CACHE_FILE

_JST = datetime.timezone(datetime.timedelta(hours=9))
_HUMAN_UA = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
_AUDIT_UA = "AisekiNavi-OpsSentinel/1.0 (curl/8.0; staff_test)"


def setUpModule():
    _apply_isolated_env()
    local_db.DB_PATH = os.environ["LOCAL_DB_PATH"]
    events.CACHE_FILE = os.path.join(_E2E_TMP_DIR, "events_cache.json")
    arena_calendar.ARENA_CACHE_FILE = os.path.join(_E2E_TMP_DIR, "arena_schedules_cache.json")
    local_db.reset_caches_for_tests()
    local_db.init_db()
    local_db._ensure_moat_rollup_schema()


def tearDownModule():
    local_db.DB_PATH = _ORIG_DB_PATH
    events.CACHE_FILE = _ORIG_EVENTS_CACHE_FILE
    arena_calendar.ARENA_CACHE_FILE = _ORIG_ARENA_CACHE_FILE
    local_db.reset_caches_for_tests()
    for k, val in _SAVED_ENV.items():
        if val is None:
            os.environ.pop(k, None)
        else:
            os.environ[k] = val


def _load_script_module(rel_path: str, mod_name: str):
    path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _make_valid_push_keys():
    priv = ec.generate_private_key(ec.SECP256R1())
    pub_bytes = priv.public_key().public_bytes(
        encoding=serialization.Encoding.X962,
        format=serialization.PublicFormat.UncompressedPoint,
    )
    p256dh = base64.urlsafe_b64encode(pub_bytes).rstrip(b"=").decode("ascii")
    auth = base64.urlsafe_b64encode(os.urandom(16)).rstrip(b"=").decode("ascii")
    return p256dh, auth


def _jst_now_naive() -> datetime.datetime:
    return datetime.datetime.now(_JST).replace(tzinfo=None)


class _IsolatedE2EBase(unittest.TestCase):
    """全テストケース共通の完全隔離・自動復元ベースクラス"""

    def setUp(self):
        super().setUp()
        local_db.DB_PATH = os.environ["LOCAL_DB_PATH"]
        app_module.app.config["TESTING"] = True
        app_module.app.config["ENFORCE_AFFILIATE_HUMAN_PROOF"] = None
        self.client = app_module.app.test_client()

        with app_module.data_lock:
            self._saved_latest = copy.deepcopy(app_module.latest_data)

        self._reset_all_runtime_state()
        self._clear_isolated_db_tables()

        bg_patcher = patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None)
        bg_patcher.start()
        self.addCleanup(bg_patcher.stop)

    def tearDown(self):
        self._clear_isolated_db_tables()
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        self._reset_all_runtime_state()
        for fname in ("job_state.json",):
            p = os.path.join(os.environ["JOB_STATE_DIR"], fname)
            if os.path.exists(p):
                try:
                    os.remove(p)
                except OSError:
                    pass
        super().tearDown()

    def _reset_all_runtime_state(self):
        local_db.reset_caches_for_tests()
        with local_db._votes_lock:
            local_db._memory_votes.clear()
        local_db.clear_monthly_stats_rows_cache()
        local_db.clear_whitepaper_cache()
        turbo_store.reset_for_tests()
        with turbo_store._redis_lock:
            turbo_store._redis_client = None
            turbo_store._redis_url_cached = None
            turbo_store._redis_failed_at = 0.0
        analytics.reset_for_tests()
        job_state.reset_cache_for_tests()
        arena_calendar.reset_for_tests()
        ops_governance.invalidate_governance_cache()
        health.reset_for_tests()
        with app_module._login_failures_lock:
            app_module._login_failures.clear()
        with contact_form._rl_lock:
            contact_form._ip_hits.clear()
            contact_form._global_hits.clear()
        events._events_memory_cache.clear()
        if hasattr(events, "_store_events_memo") and isinstance(events._store_events_memo, dict):
            events._store_events_memo.clear()
        app_module.clear_fast_http_caches(clear_editorial=True)
        pb_mod = sys.modules.get("routes.portal_bp")
        if pb_mod is not None and hasattr(pb_mod, "clear_monthly_csv_cache"):
            pb_mod.clear_monthly_csv_cache()

    def _clear_isolated_db_tables(self):
        db_path = local_db.DB_PATH
        if not db_path or not os.path.exists(db_path):
            return
        with local_db._db_lock:
            conn = sqlite3.connect(db_path)
            try:
                cur = conn.cursor()
                for tbl in (
                    "monitor_logs",
                    "crowd_votes",
                    "store_watch_intents",
                    "web_push_subscriptions",
                    "store_hourly_rollups",
                    "day_context_logs",
                    "forecast_accuracy_logs",
                    "store_pos_telemetry_logs",
                    "external_context_logs",
                ):
                    try:
                        cur.execute(f"DELETE FROM {tbl}")
                    except sqlite3.OperationalError:
                        pass
                conn.commit()
            finally:
                conn.close()

    def _seed_live_stores(self, count=None, women_override=None, men_override=None, minutes_ago=1):
        master = store_master.load() or {}
        names = list(master.keys())
        if count is not None:
            names = names[:count]
        ts_str = (_jst_now_naive() - datetime.timedelta(minutes=minutes_ago)).strftime("%Y-%m-%d %H:%M:%S")
        items = []
        for idx, sname in enumerate(names):
            info = master.get(sname, {})
            brand = (info.get("brand") or "OLG").upper()
            source = "oriental" if brand in ("OLG", "AG") else brand.lower()
            w = women_override if women_override is not None else max(4, 32 - (idx % 20))
            m = men_override if men_override is not None else max(3, 24 - (idx % 16))
            items.append({
                "name": sname,
                "women": w,
                "men": m,
                "source": source,
                "region": info.get("region") or app_module.detect_region(sname),
                "women_diff": 2,
                "men_diff": 1,
                "votes": {"smooth": 0, "waiting": 0, "lively": 0},
            })
        items.sort(key=lambda x: (x.get("region") != "Korea", x["women"], x["men"]), reverse=True)
        with app_module.data_lock:
            app_module.latest_data["full_data"] = items
            app_module.latest_data["top_store"] = items[0] if items else None
            app_module.latest_data["last_updated"] = ts_str
            app_module.latest_data["is_seed"] = False
        app_module.clear_fast_http_caches(clear_editorial=False)
        return items, ts_str

    def _login_as(self, role: str = "master", client=None):
        c = client or self.client
        code = app_module.MASTER_PASSCODE if role == "master" else app_module.STAFF_PASSCODE
        return c.post("/staff", data={"passcode": code}, follow_redirects=False)


# =====================================================================
# Tier 1: Feature Coverage Tests (F1〜F7, 40 tests)
# =====================================================================
class TestTier1F1WebCoreAndEdge(_IsolatedE2EBase):
    def test_t1_f1_01_app_py_line_limit_under_2800(self):
        lines = (REPO_ROOT / "app.py").read_text(encoding="utf-8").splitlines()
        self.assertLessEqual(len(lines), 2800, f"app.py exceeded 2800 lines: {len(lines)}")

    def test_t1_f1_02_baseline_security_headers_present(self):
        self._seed_live_stores(count=15)
        for path in ("/", "/api/health"):
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(resp.headers.get("X-Content-Type-Options"), "nosniff")
            self.assertEqual(resp.headers.get("X-Frame-Options"), "SAMEORIGIN")
            self.assertEqual(resp.headers.get("Referrer-Policy"), "strict-origin-when-cross-origin")
            self.assertIn("geolocation=(self)", resp.headers.get("Permissions-Policy", ""))
            csp = resp.headers.get("Content-Security-Policy", "")
            self.assertIn("base-uri 'self'", csp)
            self.assertIn("object-src 'none'", csp)
            self.assertIn("frame-ancestors 'self'", csp)

    def test_t1_f1_03_cache_control_static_immutable_and_sw_status_no_store(self):
        self._seed_live_stores(count=10)
        with self.client.get("/static/calm.css?v=52") as r_static_v:
            self.assertEqual(r_static_v.status_code, 200)
            self.assertEqual(r_static_v.headers.get("Cache-Control"), "public, max-age=31536000, immutable")

        with self.client.get("/static/calm.css") as r_static_plain:
            self.assertEqual(r_static_plain.status_code, 200)
            self.assertEqual(r_static_plain.headers.get("Cache-Control"), "public, max-age=86400")

        for no_store_path in ("/sw.js", "/api/status"):
            with self.client.get(no_store_path) as r:
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.headers.get("Cache-Control"), "no-cache, no-store, must-revalidate")

    def test_t1_f1_04_public_edge_cache_s_maxage_and_vary_cookie(self):
        self._seed_live_stores(count=10)
        with patch.dict(os.environ, {"ENABLE_PUBLIC_EDGE_CACHE": "1"}):
            resp = self.client.get("/", headers={"User-Agent": _AUDIT_UA})
            self.assertEqual(resp.status_code, 200)
            self.assertEqual(
                resp.headers.get("Cache-Control"),
                "public, max-age=0, s-maxage=15, stale-while-revalidate=45",
            )
            self.assertIn("Cookie", resp.headers.get("Vary", ""))

    def test_t1_f1_05_gzip_compression_and_api_status_etag_304(self):
        self._seed_live_stores(count=20)
        r_gz = self.client.get("/api/status", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r_gz.status_code, 200)
        self.assertEqual(r_gz.headers.get("Content-Encoding"), "gzip")
        etag = r_gz.headers.get("ETag")
        self.assertTrue(etag and etag.startswith('W/"'))
        parsed = json.loads(gzip.decompress(r_gz.data).decode("utf-8"))
        self.assertEqual(parsed["status"], "success")
        self.assertGreaterEqual(len(parsed["ranking"]), 15)

        r_304 = self.client.get("/api/status", headers={"If-None-Match": etag})
        self.assertEqual(r_304.status_code, 304)
        self.assertEqual(r_304.data, b"")
        self.assertEqual(r_304.headers.get("ETag"), etag)

    def test_t1_f1_06_fly_toml_architecture_and_staged_snapshot_around(self):
        fly_txt = (REPO_ROOT / "fly.toml").read_text(encoding="utf-8")
        self.assertIn('primary_region = "nrt"', fly_txt)
        self.assertIn("gunicorn", fly_txt)
        self.assertIn("worker.py", fly_txt)
        self.assertIn('path = "/api/health"', fly_txt)

        calls = []
        fake_db = MagicMock()

        def _fake_snap(min_ago, max_ago):
            calls.append(max_ago)
            if max_ago == 180:
                return {f"S{i}": {"men": 1, "women": 2} for i in range(12)}
            return {f"S{i}": {"men": 1, "women": 2} for i in range(50)}

        fake_db.get_snapshot_around.side_effect = _fake_snap
        res = request_guards.staged_snapshot_around(fake_db, windows=(180, 1440, 10080), min_stores=45)
        self.assertEqual(len(res), 50)
        self.assertEqual(calls, [180, 1440])


class TestTier1F2AuthAndStaffUI(_IsolatedE2EBase):
    def test_t1_f2_01_staff_login_get_and_post_staff_master_auth_gen(self):
        r_get = self.client.get("/staff")
        self.assertEqual(r_get.status_code, 200)
        self.assertIn("パスコード", r_get.get_data(as_text=True))

        r_staff = self._login_as("staff")
        self.assertEqual(r_staff.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get("is_staff"))
            self.assertFalse(sess.get("is_master"))
            self.assertTrue(sess.get("auth_gen"))

        c_master = app_module.app.test_client()
        r_master = self._login_as("master", client=c_master)
        self.assertEqual(r_master.status_code, 302)
        with c_master.session_transaction() as sess:
            self.assertTrue(sess.get("is_staff"))
            self.assertTrue(sess.get("is_master"))
            self.assertTrue(sess.get("auth_gen"))

    def test_t1_f2_02_staff_view_headers_and_no_adsense_rendering(self):
        self._seed_live_stores(count=12)
        self._login_as("staff")
        resp = self.client.get("/staff")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.headers.get("X-Staff-View"), "1")
        self.assertEqual(resp.headers.get("Cache-Control"), "private, no-cache, no-store, must-revalidate")
        html = resp.get_data(as_text=True)
        self.assertIn("isStaff: true", html)
        self.assertNotIn("pagead2.googlesyndication.com", html)

    def test_t1_f2_03_master_apis_via_session_and_x_admin_secret(self):
        self._seed_live_stores(count=10)
        hdr = {"X-Admin-Secret": app_module.ADMIN_API_SECRET}
        for ep in ("/api/ops/governance", "/api/data-audit", "/api/debug_ps", "/api/thread_status"):
            r = self.client.get(ep, headers=hdr)
            self.assertEqual(r.status_code, 200, f"{ep} failed with X-Admin-Secret")

        self._login_as("master")
        r_sess = self.client.get("/api/ops/governance")
        self.assertEqual(r_sess.status_code, 200)
        self.assertTrue(r_sess.get_json()["ok"])

    def test_t1_f2_04_force_logout_staff_revokes_staff_keeps_master(self):
        staff_client = app_module.app.test_client()
        master_client = app_module.app.test_client()
        self._login_as("staff", client=staff_client)
        self._login_as("master", client=master_client)

        res = ops_governance.force_logout_all_staff()
        self.assertTrue(res.get("ok"))

        r_staff_after = staff_client.get("/staff")
        self.assertEqual(r_staff_after.status_code, 200)
        self.assertIn("パスコード", r_staff_after.get_data(as_text=True))
        with staff_client.session_transaction() as sess:
            self.assertFalse(sess.get("is_staff"))

        r_master_after = master_client.get("/api/ops/governance")
        self.assertEqual(r_master_after.status_code, 200)

    def test_t1_f2_05_staff_logout_clears_auth_flags_and_redirects(self):
        self._login_as("master")
        resp = self.client.get("/staff/logout", follow_redirects=False)
        if resp.status_code == 405:
            resp = self.client.post("/staff/logout", headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
        self.assertEqual(resp.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get("is_staff"))
            self.assertFalse(sess.get("is_master"))

    def test_t1_f2_06_master_pages_redirect_non_master_staff_to_switch(self):
        self._login_as("staff")
        for ep in ("/staff/pdca", "/staff/sns-editorial", "/staff/analytics"):
            r = self.client.get(ep, follow_redirects=False)
            self.assertEqual(r.status_code, 302, f"{ep} should redirect non-master staff")
            self.assertIn("switch=1", r.headers.get("Location", ""))


class TestTier1F3NonContamAndAffiliate(_IsolatedE2EBase):
    def test_t1_f3_01_human_pv_and_api_track_sets_an_hp_cookie(self):
        self._seed_live_stores(count=10)
        with patch.object(analytics, "track") as mock_track:
            r_pv = self.client.get("/", headers={"User-Agent": _HUMAN_UA})
            self.assertEqual(r_pv.status_code, 200)
            r_trk = self.client.post(
                "/api/track",
                json={"event": "human_pv", "label": "/"},
                headers={"User-Agent": _HUMAN_UA},
            )
            self.assertEqual(r_trk.status_code, 204)
            set_cookies = " ".join(r_trk.headers.getlist("Set-Cookie"))
            self.assertIn("an_hp=1", set_cookies)
            tracked_events = [c.args[0] for c in mock_track.call_args_list]
            self.assertIn("page_view", tracked_events)
            self.assertIn("human_pv", tracked_events)

    def test_t1_f3_02_link_redirect_with_an_hp_cookie_sets_noindex_nostore(self):
        app_module.app.config["ENFORCE_AFFILIATE_HUMAN_PROOF"] = True
        self.client.set_cookie("an_hp", "1")
        with patch.object(analytics, "track") as mock_track:
            resp = self.client.get("/link/dating?from=/&s=x_post", headers={"User-Agent": _HUMAN_UA})
            self.assertEqual(resp.status_code, 302)
            self.assertIn("http", resp.headers.get("Location", ""))
            self.assertEqual(resp.headers.get("X-Robots-Tag"), "noindex, nofollow")
            self.assertEqual(resp.headers.get("Cache-Control"), "private, no-store")
            tracked_events = [c.args[0] for c in mock_track.call_args_list]
            self.assertIn("affiliate_click", tracked_events)

    def test_t1_f3_03_link_post_with_valid_csrf_clk_issues_303_and_an_hp(self):
        import affiliate_manager

        app_module.app.config["ENFORCE_AFFILIATE_HUMAN_PROOF"] = True
        r_interstitial = self.client.get("/link/dating?from=/", headers={"User-Agent": _HUMAN_UA})
        self.assertEqual(r_interstitial.status_code, 200)
        html = r_interstitial.get_data(as_text=True)
        m = re.search(r'name="csrf_clk"\s+value="([^"]+)"', html)
        self.assertIsNotNone(m)
        tok = m.group(1)
        self.assertTrue(affiliate_manager.verify_click_confirm_token(str(app_module.app.secret_key), "dating", tok))

        r_post = self.client.post(
            "/link/dating?from=/",
            data={"csrf_clk": tok},
            headers={"User-Agent": _HUMAN_UA},
            follow_redirects=False,
        )
        self.assertEqual(r_post.status_code, 303)
        self.assertIn("an_hp=1", " ".join(r_post.headers.getlist("Set-Cookie")))

    def test_t1_f3_04_api_vote_records_crowd_vote_and_wait_minutes(self):
        self._seed_live_stores(count=20)
        r = self.client.post(
            "/api/vote",
            json={"store_name": "OLG 新宿", "vote_type": "smooth", "voter_id": "v-e2e-1", "wait_minutes": 15},
            headers={"User-Agent": _HUMAN_UA},
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "success")
        self.assertGreaterEqual(data["votes"]["smooth"], 1)
        with app_module.data_lock:
            shinjuku = next(s for s in app_module.latest_data["full_data"] if s["name"] == "OLG 新宿")
            self.assertGreaterEqual(shinjuku["votes"]["smooth"], 1)

    def test_t1_f3_05_api_watch_intent_registers_condition_and_count(self):
        self._seed_live_stores(count=20)
        r = self.client.post(
            "/api/watch-intent",
            json={"store_name": "OLG 新宿", "target_cond": "female_55", "voter_id": "w-e2e-1"},
            headers={"User-Agent": _HUMAN_UA},
        )
        self.assertEqual(r.status_code, 200)
        data = r.get_json()
        self.assertEqual(data["status"], "success")
        self.assertGreaterEqual(data["watching_count"], 1)

    def test_t1_f3_06_affiliate_audit_compliance_passes(self):
        aff_mod = _load_script_module(".agents/skills/affiliate/scripts/parse_and_audit_affiliate.py", "e2e_parse_aff")
        rc = aff_mod.run_audit()
        self.assertEqual(rc, 0)


class TestTier1F4DbAndDataMoat(_IsolatedE2EBase):
    def test_t1_f4_01_contiguous_date_ranges_splits_30_days_into_7d_batches(self):
        base = datetime.date(2026, 10, 1)
        dates = [(base + datetime.timedelta(days=i)).isoformat() for i in range(30)]
        ranges = local_db._contiguous_date_ranges(dates, max_span_days=7)
        self.assertEqual(len(ranges), 5)
        for d0, d1 in ranges:
            span = (datetime.date.fromisoformat(d1) - datetime.date.fromisoformat(d0)).days + 1
            self.assertLessEqual(span, 7)

    def test_t1_f4_02_iter_ts_window_in_7d_chunks_bounded_spans(self):
        base_ts = (_jst_now_naive() - datetime.timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S")
        local_db.insert_logs([{"name": "OLG 新宿", "men": 10, "women": 15, "source": "oriental"}], base_ts)
        end_ts = _jst_now_naive().strftime("%Y-%m-%d %H:%M:%S")
        start_ts = (_jst_now_naive() - datetime.timedelta(days=28)).strftime("%Y-%m-%d %H:%M:%S")
        chunks = list(
            local_db._iter_ts_window_in_7d_chunks(
                "SELECT timestamp, store_name, men, women FROM monitor_logs WHERE timestamp >= ? AND timestamp <= ?",
                start_ts,
                end_ts,
            )
        )
        self.assertGreaterEqual(len(chunks), 4)
        total_rows = sum(len(c) for c in chunks)
        self.assertEqual(total_rows, 1)

    def test_t1_f4_03_rollup_store_hourly_stats_aggregates_logs_and_votes(self):
        target_dt = (_jst_now_naive() - datetime.timedelta(days=1)).replace(hour=21, minute=10, second=0, microsecond=0)
        ts_str = target_dt.strftime("%Y-%m-%d %H:%M:%S")
        local_db.insert_logs(
            [
                {"name": "OLG 新宿", "men": 12, "women": 18, "source": "oriental"},
                {"name": "JIS SHINJUKU", "men": 8, "women": 14, "source": "jis"},
            ],
            ts_str,
        )
        res = local_db.rollup_store_hourly_stats(target_dates=[target_dt.strftime("%Y-%m-%d")])
        self.assertTrue(res.get("ok"))
        self.assertGreaterEqual(res.get("upserted_rollups", 0), 2)
        rows = local_db._fetch_all_rows(
            "SELECT store_name, max_women, max_men FROM store_hourly_rollups WHERE date_jst = ?",
            (target_dt.strftime("%Y-%m-%d"),),
        )
        by_store = {r[0]: (r[1], r[2]) for r in rows}
        self.assertEqual(by_store.get("OLG 新宿"), (18, 12))

    def test_t1_f4_04_prune_old_logs_keeps_rollups_and_anonymizes_14d_votes(self):
        now_dt = _jst_now_naive()
        old_95d_ts = (now_dt - datetime.timedelta(days=95)).replace(hour=21, minute=0, second=0).strftime("%Y-%m-%d %H:%M:%S")
        old_16d_ts = (now_dt - datetime.timedelta(days=16)).strftime("%Y-%m-%d %H:%M:%S")
        local_db.insert_logs([{"name": "OLG 新宿", "men": 10, "women": 20, "source": "oriental"}], old_95d_ts)

        with local_db._db_lock:
            conn = local_db.get_connection()
            try:
                conn.execute(
                    "INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash, wait_minutes) VALUES (?, ?, ?, ?, ?)",
                    (old_16d_ts, "OLG 新宿", "smooth", "hash_old_ip", 0),
                )
                conn.execute(
                    "INSERT INTO store_watch_intents (timestamp, store_name, target_cond, ip_hash) VALUES (?, ?, ?, ?)",
                    (old_16d_ts, "OLG 新宿", "female_55", "hash_old_watch"),
                )
                conn.commit()
            finally:
                conn.close()

        res = local_db.prune_old_logs(monitor_days=90, vote_days=14)
        self.assertTrue(res["rollup_ok"])
        self.assertEqual(res["deleted_monitor_logs"], 1)
        self.assertEqual(res["deleted_crowd_votes"], 0)
        self.assertGreaterEqual(res["anonymized_crowd_votes"], 1)

        rollups = local_db._fetch_all_rows("SELECT COUNT(*) FROM store_hourly_rollups WHERE store_name = ?", ("OLG 新宿",))
        self.assertGreaterEqual(rollups[0][0], 1)
        votes = local_db._fetch_all_rows("SELECT ip_hash FROM crowd_votes WHERE store_name = ?", ("OLG 新宿",))
        self.assertEqual(len(votes), 1)
        self.assertEqual(votes[0][0], "")
        watches = local_db._fetch_all_rows("SELECT ip_hash FROM store_watch_intents WHERE store_name = ?", ("OLG 新宿",))
        self.assertEqual(len(watches), 1)
        self.assertEqual(watches[0][0], "")

    def test_t1_f4_05_turbo_store_memory_bus_snapshot_and_ai_cache(self):
        turbo_store.enable_memory_bus_for_tests(True)
        stores = [{"name": "OLG 新宿", "women": 25, "men": 15, "source": "oriental"}]
        self.assertTrue(turbo_store.publish_live_snapshot(stores, "2026-11-19 21:00:00"))
        snap = turbo_store.get_live_snapshot(bypass_l1=True)
        self.assertIsNotNone(snap)
        self.assertEqual(snap["full_data"][0]["women"], 25)

        self.assertTrue(turbo_store.set_ai_cache("e2e_ns", ("k1", 1), {"draft": "ok"}, ttl_sec=60))
        cached = turbo_store.get_ai_cache("e2e_ns", ("k1", 1))
        self.assertEqual(cached, {"draft": "ok"})

    def test_t1_f4_06_check_data_integrity_clean_on_isolated_db(self):
        now_dt = _jst_now_naive()
        ts_recent = (now_dt - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        local_db.insert_logs([{"name": "OLG 新宿", "men": 10, "women": 16, "source": "oriental"}], ts_recent)
        local_db.rollup_store_hourly_stats(days_back=2)
        cdi = _load_script_module(".agents/skills/ops-sentinel/scripts/check_data_integrity.py", "e2e_cdi")
        report = cdi.run_integrity_checks(days=90)
        self.assertIn(report["overall"], ("ok", "warn"))
        self.assertNotEqual(report["overall"], "error")


class TestTier1F5PortalAndSeoFeeds(_IsolatedE2EBase):
    def test_t1_f5_01_public_ssr_routes_return_200(self):
        self._seed_live_stores()
        routes = (
            "/",
            "/concierge",
            "/for-women",
            "/price-comparison",
            "/areas",
            "/stats",
            "/stats/weekend-index",
            "/stats/year-end-2026",
            "/columns",
            "/oriental-lounge",
            "/ag",
            "/jis",
            "/asobibar",
            "/store/oriental-lounge-shinjuku",
        )
        for path in routes:
            resp = self.client.get(path)
            self.assertEqual(resp.status_code, 200, f"GET {path} returned {resp.status_code}")

    def test_t1_f5_02_stats_monthly_csv_headers_columns_and_no_direct_link(self):
        for off_m in (10, 20, 30):
            ts_recent = (_jst_now_naive() - datetime.timedelta(minutes=off_m)).strftime("%Y-%m-%d %H:%M:%S")
            local_db.insert_logs(
                [
                    {"name": "OLG 新宿", "men": 12, "women": 20, "source": "oriental"},
                    {"name": "JIS SHINJUKU", "men": 10, "women": 16, "source": "jis"},
                ],
                ts_recent,
            )
        resp = self.client.get("/stats/monthly.csv?days=30")
        self.assertEqual(resp.status_code, 200)
        self.assertIn("text/csv", resp.content_type)
        self.assertIn("creativecommons.org/licenses/by/4.0/", resp.headers.get("Link", ""))
        self.assertEqual(resp.headers.get("Access-Control-Allow-Origin"), "*")
        body = resp.get_data(as_text=True)
        self.assertNotIn("/link/", body)
        rows = list(csv.DictReader(io.StringIO(body)))
        self.assertGreaterEqual(len(rows), 2)
        expected_cols = {
            "store_slug", "store_name", "brand_name", "area_label", "window_days",
            "sample_count", "avg_women", "peak_women", "avg_men", "peak_men",
            "avg_female_ratio_pct", "store_url",
        }
        self.assertEqual(set(rows[0].keys()), expected_cols)

    def test_t1_f5_03_llms_facts_json_schema_v21_and_llms_txt_endpoints(self):
        self._seed_live_stores()
        r_facts = self.client.get("/api/llms-facts.json")
        self.assertEqual(r_facts.status_code, 200)
        raw_txt = r_facts.get_data(as_text=True)
        self.assertNotIn("/link/", raw_txt)
        payload = r_facts.get_json()
        self.assertEqual(payload.get("schema_version"), "2.1")
        self.assertGreaterEqual(len(payload.get("stores") or []), 45)

        for p in ("/llms.txt", "/llms-full.txt"):
            r = self.client.get(p)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("Access-Control-Allow-Origin"), "*")

    def test_t1_f5_04_robots_txt_all_ai_bots_and_sitemap_xml_coverage(self):
        r_rob = self.client.get("/robots.txt")
        self.assertEqual(r_rob.status_code, 200)
        rob_txt = r_rob.get_data(as_text=True)
        sentinel_mod = _load_script_module(".agents/skills/ops-sentinel/scripts/run_ops_sentinel.py", "e2e_sentinel_rob")
        ok_rob, missing = sentinel_mod._check_robots_disallow_link(rob_txt)
        self.assertTrue(ok_rob, f"robots.txt missing Disallow: /link/ for {missing}")
        self.assertIn("Disallow: /staff", rob_txt)

        r_sm = self.client.get("/sitemap.xml")
        self.assertEqual(r_sm.status_code, 200)
        sm_txt = r_sm.get_data(as_text=True)
        urls = re.findall(r"<loc>(.*?)</loc>", sm_txt)
        self.assertGreaterEqual(len(urls), 50)
        for req_suf in ("/stats", "/stats/year-end-2026", "/price-comparison", "/columns"):
            self.assertTrue(any(u.endswith(req_suf) for u in urls), f"Missing {req_suf} in sitemap.xml")

    def test_t1_f5_05_legacy_brand_price_and_store_slug_301_redirects(self):
        r_brand = self.client.get("/brand/oriental-lounge?s=x_post", follow_redirects=False)
        self.assertEqual(r_brand.status_code, 301)
        self.assertTrue(r_brand.headers["Location"].endswith("/oriental-lounge?s=x_post"))

        r_price = self.client.get("/price?s=ig", follow_redirects=False)
        self.assertEqual(r_price.status_code, 301)
        self.assertTrue(r_price.headers["Location"].endswith("/price-comparison?s=ig"))

        r_old_store = self.client.get("/store/ag-shinjuku", follow_redirects=False)
        self.assertEqual(r_old_store.status_code, 301)
        self.assertTrue(r_old_store.headers["Location"].endswith("/area/shinjuku"))

    def test_t1_f5_06_concierge_api_and_embed_heatmap_cors_csp_headers(self):
        self._seed_live_stores()
        r_con = self.client.get("/api/concierge?area=shinjuku&audience=men&timing=now")
        self.assertEqual(r_con.status_code, 200)
        con_json = r_con.get_json()
        self.assertTrue(con_json.get("ok"))
        self.assertIn("primary", con_json)
        self.assertIn("area_summary", con_json)

        r_emb = self.client.get("/embed/heatmap?scope=all")
        self.assertEqual(r_emb.status_code, 200)
        self.assertIn("frame-ancestors *", r_emb.headers.get("Content-Security-Policy", ""))
        self.assertIn("creativecommons.org/licenses/by/4.0/", r_emb.headers.get("Link", ""))

        r_emb_json = self.client.get("/api/embed/heatmap.json?scope=all")
        self.assertEqual(r_emb_json.status_code, 200)
        self.assertEqual(r_emb_json.headers.get("Access-Control-Allow-Origin"), "*")


class TestTier1F6FrontendSwAndUiSync(_IsolatedE2EBase):
    def test_t1_f6_01_dynamic_cache_version_sync_across_templates_and_sw(self):
        ui_mod = _load_script_module(".agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py", "e2e_ui_sync")
        issues = ui_mod.check_cache_version_sync()
        self.assertEqual(issues, [], f"Cache version sync issues: {issues}")

        sw_txt = (REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        m_calm = re.search(r"/static/calm\.css\?v=[^\"'\s]+", sw_txt)
        self.assertIsNotNone(m_calm)
        with self.client.get(m_calm.group(0)) as r_asset:
            self.assertEqual(r_asset.status_code, 200)
            self.assertEqual(r_asset.headers.get("Cache-Control"), "public, max-age=31536000, immutable")

    def test_t1_f6_02_calm_css_scroll_safety_and_no_undefined_css_vars(self):
        ui_mod = _load_script_module(".agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py", "e2e_ui_css")
        self.assertEqual(ui_mod.check_calm_css_scroll_safety(), [])
        self.assertEqual(ui_mod.check_undefined_css_vars(), [])

    def test_t1_f6_03_no_ai_emoji_neon_gradients_or_guide_white_inline_colors(self):
        ui_mod = _load_script_module(".agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py", "e2e_ui_ai")
        self.assertEqual(ui_mod.check_guide_light_mode_inline_colors(), [])
        self.assertEqual(ui_mod.check_python_ui_badge_emojis(), [])
        for p in sorted((REPO_ROOT / "templates").glob("*.html")):
            if p.name.startswith("staff_"):
                continue
            res = ui_mod.scan_file_for_ai_patterns(str(p))
            self.assertEqual(res["control_emoji_count"], 0, f"{res['file']} has control emojis: {res['sample_emojis']}")
            self.assertEqual(res["neon_gradient_count"], 0, f"{res['file']} has neon gradient")

    def test_t1_f6_04_public_rendered_html_zero_ng_brand_abbreviations(self):
        self._seed_live_stores()
        ng_re = re.compile(r"\bOLG\b|\bORIENTAL\b(?!\s+LOUNGE)|\bYATAKOI\b|オリラジ|オリエンタル(?!ラウンジ|ホテル|カフェ|ビル|スタイル)")
        for path in ("/", "/stats", "/price-comparison", "/store/oriental-lounge-shinjuku"):
            html = self.client.get(path).get_data(as_text=True)
            vis = re.sub(r"<script\b[^>]*>.*?</script>|<style\b[^>]*>.*?</style>|<[^>]+>", " ", html, flags=re.I | re.S)
            m = ng_re.search(vis)
            self.assertIsNone(m, f"{path} leaked NG brand token: {m.group(0) if m else ''}")

    def test_t1_f6_05_service_worker_bypass_paths_and_api_503_offline_fallback(self):
        sw_txt = (REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        for token in ("/staff", "/link/", "/api/", "/stats/monthly.csv"):
            self.assertIn(token, sw_txt)
        self.assertIn("status: 503", sw_txt)
        self.assertIn("application/json", sw_txt)


class TestTier1F7WorkerSnsPushLine(_IsolatedE2EBase):
    def test_t1_f7_01_worker_import_prevents_app_scheduler_double_start(self):
        orig_app_role = os.environ.get("APP_ROLE")
        orig_sched_started = app_module._scheduler_started
        had_worker = "worker" in sys.modules

        def _restore_worker_env():
            if orig_app_role is None:
                os.environ.pop("APP_ROLE", None)
            else:
                os.environ["APP_ROLE"] = orig_app_role
            app_module._scheduler_started = orig_sched_started
            if not had_worker:
                sys.modules.pop("worker", None)

        self.addCleanup(_restore_worker_env)
        os.environ.pop("APP_ROLE", None)
        if had_worker:
            sys.modules.pop("worker", None)
        import worker as worker_module

        self.assertEqual(os.environ.get("APP_ROLE"), "worker")
        self.assertTrue(app_module._scheduler_started)
        self.assertTrue(callable(worker_module._x_scheduled_job))

    def test_t1_f7_02_sns_job_enqueue_poll_and_drain_once(self):
        turbo_store.enable_memory_bus_for_tests(True)
        r_enq = self.client.post(
            "/api/sns-jobs/enqueue",
            json={"job_type": "x_post", "params": {"slot": "22:00", "force": True}, "worker_only": True},
            headers={"X-Admin-Secret": app_module.ADMIN_API_SECRET},
        )
        self.assertEqual(r_enq.status_code, 202)
        body = r_enq.get_json()
        self.assertTrue(body["queued"])
        job_id = body["job_id"]

        r_poll1 = self.client.get(body["poll_url"], headers={"X-Admin-Secret": app_module.ADMIN_API_SECRET})
        self.assertEqual(r_poll1.status_code, 200)
        self.assertEqual(r_poll1.get_json()["job"]["status"], "queued")

        with patch.object(app_module, "run_scheduled_x_post", return_value={"status": "success", "tweet_id": "e2e-123"}):
            done = app_module.drain_sns_job_queue_once(timeout_sec=0.1)
        self.assertIsNotNone(done)
        self.assertEqual(done["status"], "succeeded")

        r_poll2 = self.client.get(body["poll_url"], headers={"X-Admin-Secret": app_module.ADMIN_API_SECRET})
        self.assertEqual(r_poll2.get_json()["job"]["status"], "succeeded")

    def test_t1_f7_03_ogp_live_and_top3_images_render_png(self):
        self._seed_live_stores(count=15)
        for ep in ("/ogp/live.png", "/ogp/top3.png"):
            r = self.client.get(ep)
            self.assertEqual(r.status_code, 200, f"{ep} returned {r.status_code}")
            self.assertIn("image/png", r.content_type)
            self.assertTrue(r.data.startswith(b"\x89PNG\r\n\x1a\n"))

    def test_t1_f7_04_web_push_vapid_subscribe_and_unsubscribe_flow(self):
        self._seed_live_stores(count=15)
        r_vapid = self.client.get("/api/push/vapid-public-key")
        self.assertEqual(r_vapid.status_code, 200)
        self.assertEqual(r_vapid.headers.get("Cache-Control"), "private, no-store")
        self.assertTrue(r_vapid.get_json()["public_key"])

        p256dh, auth = _make_valid_push_keys()
        ep_url = "https://fcm.googleapis.com/fcm/send/e2e-sub-token-001"
        r_sub = self.client.post(
            "/api/push/subscribe",
            json={
                "store_name": "OLG 新宿",
                "target_cond": "female_55",
                "subscription": {"endpoint": ep_url, "keys": {"p256dh": p256dh, "auth": auth}},
            },
            headers={"User-Agent": _HUMAN_UA},
        )
        self.assertEqual(r_sub.status_code, 200)
        self.assertTrue(r_sub.get_json()["ok"])

        r_unsub = self.client.post(
            "/api/push/unsubscribe",
            json={"endpoint": ep_url, "store_name": "OLG 新宿"},
        )
        self.assertEqual(r_unsub.status_code, 200)
        self.assertGreaterEqual(r_unsub.get_json()["removed"], 1)

    def test_t1_f7_05_line_webhook_valid_hmac_signature_replies_200(self):
        self._seed_live_stores(count=15)
        channel_secret = "line-e2e-channel-secret"
        raw_body = json.dumps(
            {
                "events": [
                    {
                        "type": "message",
                        "replyToken": "reply-tok-e2e-1",
                        "message": {"type": "text", "text": "新宿"},
                    }
                ]
            },
            ensure_ascii=False,
        ).encode("utf-8")
        sig = base64.b64encode(hmac.new(channel_secret.encode("utf-8"), raw_body, hashlib.sha256).digest()).decode("ascii")
        with patch.dict(
            os.environ,
            {"LINE_CHANNEL_SECRET": channel_secret, "LINE_CHANNEL_ACCESS_TOKEN": "line-e2e-access-token"},
        ):
            with patch.object(push_watch, "send_line_reply", return_value=True) as mock_reply:
                r = self.client.post(
                    "/api/line/webhook",
                    data=raw_body,
                    content_type="application/json",
                    headers={"X-Line-Signature": sig},
                )
                self.assertEqual(r.status_code, 200)
                self.assertEqual(r.get_json(), {"status": "ok", "replied": 1})
                mock_reply.assert_called_once()


# =====================================================================
# Tier 2: Boundary / Corner / Security / Non-Contamination (41 tests)
# =====================================================================
class TestTier2F1SecurityAndGuards(_IsolatedE2EBase):
    def test_t2_f1_01_body_limit_256kb_public_vs_2mb_admin_and_staff_xyz_boundary(self):
        big_public = b'{"event":"human_pv","label":"' + (b"a" * (260 * 1024)) + b'"}'
        r_pub = self.client.post("/api/track", data=big_public, content_type="application/json", headers={"User-Agent": _HUMAN_UA})
        self.assertEqual(r_pub.status_code, 413)
        self.assertEqual(r_pub.get_json()["max_bytes"], request_guards.MAX_JSON_BODY_BYTES)

        r_boundary = self.client.post("/staff-xyz", data=b"x" * (300 * 1024), headers={"Accept": "application/json"})
        self.assertEqual(r_boundary.status_code, 413)

        big_admin = b'{"action":"unknown","pad":"' + (b"b" * (300 * 1024)) + b'"}'
        r_adm = self.client.post(
            "/api/ops/governance",
            data=big_admin,
            content_type="application/json",
            headers={"X-Admin-Secret": app_module.ADMIN_API_SECRET},
        )
        self.assertEqual(r_adm.status_code, 400)  # passed 256KB guard, reached handler

    def test_t2_f1_02_proxyfix_spoofed_x_forwarded_host_ignored_and_onrender_301(self):
        r_spoof = self.client.get("/api/health", headers={"X-Forwarded-Host": "evil.example.com"})
        self.assertEqual(r_spoof.status_code, 200)

        r_render = self.client.get("/stats?days=30", headers={"Host": "aiseki-navi.onrender.com"}, follow_redirects=False)
        self.assertEqual(r_render.status_code, 301)
        self.assertEqual(r_render.headers.get("Location"), "https://aiseki-navi.com/stats?days=30")

    def test_t2_f1_03_parse_jst_ts_malformed_and_boundary_inputs_never_raise(self):
        for bad in (None, "", "   ", "21:30", "2026-99-99 25:61:00", "x" * 65):
            self.assertIsNone(request_guards.parse_jst_ts(bad), f"Expected None for {bad!r}")
        dt_utc = request_guards.parse_jst_ts("2026-11-19T00:00:00Z")
        self.assertEqual(dt_utc, datetime.datetime(2026, 11, 19, 9, 0, 0))

    def test_t2_f1_04_csp_includes_form_action_directive(self):
        """[Baseline Requirement] CSP must include form-action directive to block cross-origin form exfiltration."""
        self._seed_live_stores(count=5)
        resp = self.client.get("/")
        self.assertEqual(resp.status_code, 200)
        csp = resp.headers.get("Content-Security-Policy", "").lower()
        self.assertIn("form-action", csp)

    def test_t2_f1_05_non_gzip_response_includes_vary_accept_encoding(self):
        """[Baseline Requirement] Compressible responses must emit Vary: Accept-Encoding even on non-gzip requests."""
        self._seed_live_stores(count=15)
        for ep in ("/", "/api/status"):
            resp = self.client.get(ep, headers={"Accept-Encoding": "identity"})
            self.assertEqual(resp.status_code, 200)
            vary = (resp.headers.get("Vary") or "").lower()
            self.assertIn("accept-encoding", vary, f"{ep} missing Vary: Accept-Encoding on non-gzip response")

    def test_t2_f1_06_health_deep_stale_returns_503_and_hides_detail_when_unauth(self):
        self._seed_live_stores(count=10, minutes_ago=60)
        r_unauth = self.client.get("/api/health/deep")
        self.assertEqual(r_unauth.status_code, 503)
        j_unauth = r_unauth.get_json()
        self.assertEqual(j_unauth["status"], "degraded")
        self.assertNotIn("detail", j_unauth)
        self.assertNotIn("turbo", j_unauth)

        r_auth = self.client.get("/api/health/deep", headers={"X-Admin-Secret": app_module.ADMIN_API_SECRET})
        self.assertEqual(r_auth.status_code, 503)
        self.assertIn("detail", r_auth.get_json())
        self.assertIn("turbo", r_auth.get_json())

    def test_t2_f1_07_hsts_only_over_https_not_plain_http(self):
        r_http = self.client.get("/api/health")
        self.assertNotIn("Strict-Transport-Security", r_http.headers)

        r_https = self.client.get("/api/health", headers={"X-Forwarded-Proto": "https"})
        self.assertEqual(r_https.headers.get("Strict-Transport-Security"), "max-age=31536000; includeSubDomains")


class TestTier2F2AuthAndCsrfBoundaries(_IsolatedE2EBase):
    def test_t2_f2_01_legacy_query_passcode_ignored_and_redirects(self):
        for code in (app_module.STAFF_PASSCODE, app_module.MASTER_PASSCODE):
            c = app_module.app.test_client()
            r = c.get(f"/staff?passcode={code}", follow_redirects=False)
            self.assertEqual(r.status_code, 302)
            with c.session_transaction() as sess:
                self.assertFalse(sess.get("is_staff"))
                self.assertFalse(sess.get("is_master"))

    def test_t2_f2_02_query_secret_rejected_401_only_header_accepted(self):
        sec = app_module.ADMIN_API_SECRET
        for ep in ("/api/ops/governance", "/api/debug_ps", "/api/data-audit"):
            r_qs = self.client.get(f"{ep}?secret={sec}")
            self.assertEqual(r_qs.status_code, 401, f"{ep}?secret= should be 401")
            r_hdr = self.client.get(ep, headers={"X-Admin-Secret": sec})
            self.assertEqual(r_hdr.status_code, 200)

    def test_t2_f2_03_staff_login_rate_limited_429_after_8_failures(self):
        for _ in range(8):
            r = self.client.post("/staff", data={"passcode": "0000"})
            self.assertEqual(r.status_code, 200)
        r_9th = self.client.post("/staff", data={"passcode": app_module.MASTER_PASSCODE})
        self.assertEqual(r_9th.status_code, 429)

    def test_t2_f2_04_prod_env_without_passcode_uses_random_secret_not_5555(self):
        with patch.dict(os.environ, {"FLY_APP_NAME": "aiseki-navi-tokyo"}):
            os.environ.pop("STAFF_PASSCODE", None)
            val1 = app_module._load_secure_config("STAFF_PASSCODE", "5555", is_sensitive=True)
            val2 = app_module._load_secure_config("STAFF_PASSCODE", "5555", is_sensitive=True)
            self.assertNotEqual(val1, "5555")
            self.assertGreaterEqual(len(val1), 32)
            self.assertNotEqual(val1, val2)

    def test_t2_f2_05_master_post_cross_site_and_origin_mismatch_blocked_403(self):
        self._login_as("master")
        for hdrs in (
            {"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
            {"Sec-Fetch-Site": "same-origin", "Origin": "https://evil.example"},
        ):
            r_gov = self.client.post("/api/ops/governance", json={"action": "force_logout_staff"}, headers=hdrs)
            self.assertEqual(r_gov.status_code, 403)
            r_sns = self.client.post("/api/sns-jobs/enqueue", json={"job_type": "x_post"}, headers=hdrs)
            self.assertEqual(r_sns.status_code, 403)

    def test_t2_f2_06_staff_pdca_all_target_blank_have_rel_noopener(self):
        """[Baseline Requirement] All target=_blank links in staff_pdca.html must include rel=noopener."""
        src = (REPO_ROOT / "templates" / "staff_pdca.html").read_text(encoding="utf-8")
        tags = re.findall(r"<a\b[^>]*target=[\"']_blank[\"'][^>]*>", src, flags=re.I)
        self.assertGreater(len(tags), 0)
        missing = [t for t in tags if not re.search(r'rel=["\'][^"\']*\bnoopener\b', t, flags=re.I)]
        self.assertEqual(missing, [], f"target=_blank without rel=noopener in staff_pdca.html: {missing}")

    def test_t2_f2_07_logout_clears_auth_gen(self):
        """[Baseline Requirement] /staff/logout must clear auth_gen (alongside is_staff and is_master) from session."""
        self._login_as("master")
        r = self.client.get("/staff/logout", follow_redirects=False)
        if r.status_code == 405:
            r = self.client.post("/staff/logout", headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"}, follow_redirects=False)
        self.assertEqual(r.status_code, 302)
        with self.client.session_transaction() as sess:
            self.assertNotIn("auth_gen", sess)
            self.assertNotIn("is_staff", sess)
            self.assertNotIn("is_master", sess)
        self.assertEqual(self.client.get("/api/ops/governance").status_code, 401)


class TestTier2F3NonContamAndRateLimits(_IsolatedE2EBase):
    def test_t2_f3_01_staff_test_and_audit_bot_ua_never_record_page_view(self):
        self._seed_live_stores(count=10)
        with patch.object(analytics, "track") as mock_track:
            self.client.get("/?staff_test=1", headers={"User-Agent": _HUMAN_UA})
            self.client.get("/guide", headers={"User-Agent": _AUDIT_UA})
            self.client.get("/", headers={"User-Agent": "curl/8.0"})
            self.client.get("/", headers={"User-Agent": _HUMAN_UA, "X-Sw-Prefetch": "1"})
            mock_track.assert_not_called()

    def test_t2_f3_02_api_track_blocks_staff_test_bots_and_disallowed_events(self):
        with patch.object(analytics, "track") as mock_track:
            self.assertEqual(
                self.client.post("/api/track?staff_test=1", json={"event": "human_pv"}, headers={"User-Agent": _HUMAN_UA}).status_code,
                204,
            )
            self.assertEqual(
                self.client.post(
                    "/api/track",
                    json={"event": "human_pv"},
                    headers={"User-Agent": _HUMAN_UA, "Referer": "https://aiseki-navi.com/?staff_test=1"},
                ).status_code,
                204,
            )
            self.assertEqual(
                self.client.post("/api/track", json={"event": "human_pv"}, headers={"User-Agent": _AUDIT_UA}).status_code,
                204,
            )
            self.assertEqual(
                self.client.post("/api/track", json={"event": "evil_event"}, headers={"User-Agent": _HUMAN_UA}).status_code,
                204,
            )
            mock_track.assert_not_called()

    def test_t2_f3_03_link_shields_bots_and_prefetch_to_internal_guides(self):
        with patch.object(analytics, "track") as mock_track:
            r_men = self.client.get("/link/dating", headers={"User-Agent": "GPTBot/1.0"})
            self.assertEqual(r_men.status_code, 302)
            self.assertTrue(r_men.headers["Location"].endswith("/grooming"))

            r_women = self.client.get("/link/women_beauty_clinic", headers={"User-Agent": _HUMAN_UA, "X-Sw-Prefetch": "1"})
            self.assertEqual(r_women.status_code, 302)
            self.assertTrue(r_women.headers["Location"].endswith("/for-women"))
            mock_track.assert_not_called()

    def test_t2_f3_04_link_staff_test_probe_resolves_destination_without_tracking(self):
        app_module.app.config["ENFORCE_AFFILIATE_HUMAN_PROOF"] = True
        with patch.object(analytics, "track") as mock_track:
            r = self.client.get("/link/dating?staff_test=1", headers={"User-Agent": _AUDIT_UA})
            self.assertEqual(r.status_code, 302)
            self.assertIn("http", r.headers.get("Location", ""))
            mock_track.assert_not_called()

    def test_t2_f3_05_watch_intent_rate_limits_21st_429_and_staff_test_exempt(self):
        self._seed_live_stores(count=10)
        for i in range(20):
            r = self.client.post(
                "/api/watch-intent",
                json={"store_name": "OLG 新宿", "target_cond": "female_55", "voter_id": f"rl-{i}"},
                headers={"User-Agent": _HUMAN_UA},
            )
            self.assertEqual(r.status_code, 200)
        r_21 = self.client.post(
            "/api/watch-intent",
            json={"store_name": "OLG 新宿", "target_cond": "female_55", "voter_id": "rl-20"},
            headers={"User-Agent": _HUMAN_UA},
        )
        self.assertEqual(r_21.status_code, 429)
        self.assertEqual(r_21.get_json()["reason"], "rate_limited")

        r_st = self.client.post(
            "/api/watch-intent?staff_test=1",
            json={"store_name": "OLG 新宿", "target_cond": "female_55", "voter_id": "rl-staff"},
            headers={"User-Agent": _AUDIT_UA},
        )
        self.assertEqual(r_st.status_code, 200)
        self.assertEqual(r_st.get_json()["reason"], "staff_test_noop")

    def test_t2_f3_06_vote_rejects_invalid_store_or_type_and_push_sub_429(self):
        self._seed_live_stores(count=10)
        r_noop = self.client.post(
            "/api/vote?staff_test=1",
            json={"store_name": "OLG 新宿", "vote_type": "smooth"},
            headers={"User-Agent": _AUDIT_UA},
        )
        self.assertEqual(r_noop.status_code, 200)
        self.assertEqual(r_noop.get_json()["reason"], "staff_test_noop")
        self.assertEqual(local_db.get_recent_votes("OLG 新宿", hours=4)["smooth"], 0)

        self.assertEqual(self.client.post("/api/vote", json={"store_name": "UNKNOWN_STORE", "vote_type": "smooth"}).status_code, 400)
        self.assertEqual(self.client.post("/api/vote", json={"store_name": "OLG 新宿", "vote_type": "invalid"}).status_code, 400)

        p256dh, auth = _make_valid_push_keys()
        for i in range(15):
            r_ok = self.client.post(
                "/api/push/subscribe",
                json={
                    "store_name": "OLG 新宿",
                    "target_cond": "female_55",
                    "subscription": {"endpoint": f"https://fcm.googleapis.com/fcm/send/e2e-rl-{i}", "keys": {"p256dh": p256dh, "auth": auth}},
                },
            )
            self.assertEqual(r_ok.status_code, 200)
        r_16 = self.client.post(
            "/api/push/subscribe",
            json={
                "store_name": "OLG 新宿",
                "target_cond": "female_55",
                "subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/e2e-rl-15", "keys": {"p256dh": p256dh, "auth": auth}},
            },
        )
        self.assertEqual(r_16.status_code, 429)


class TestTier2F4DbSecurityAndBoundaries(_IsolatedE2EBase):
    def test_t2_f4_01_live_backends_blocked_under_unittest_without_opt_in(self):
        with patch.dict(os.environ, {"REDIS_URL": "rediss://x:y@host:6379", "DATABASE_URL": "postgres://u:p@h:5432/db"}):
            os.environ.pop("ALLOW_LIVE_DB_IN_TESTS", None)
            self.assertTrue(turbo_store._live_backends_blocked())
            self.assertEqual(turbo_store.get_redis_url(), "")
            self.assertEqual(turbo_store.get_database_url(), "")

    def test_t2_f4_02_redis_tls_does_not_disable_ssl_cert_verification(self):
        """[Baseline Requirement] rediss:// connections must not disable TLS certificate verification (ssl_cert_reqs=None)."""
        fake_client = MagicMock()
        fake_redis_lib = MagicMock()
        fake_redis_lib.Redis.from_url.return_value = fake_client
        with patch.object(turbo_store, "_live_backends_blocked", return_value=False):
            with patch.object(turbo_store, "_redis_lib", fake_redis_lib):
                with patch.dict(os.environ, {"REDIS_URL": "rediss://default:secret@upstash.example:6379"}):
                    turbo_store.reset_for_tests()
                    client = turbo_store.get_redis_client()
                    self.assertIsNotNone(client)
                    _, kwargs = fake_redis_lib.Redis.from_url.call_args
                    if "ssl_cert_reqs" in kwargs:
                        cert_reqs = kwargs["ssl_cert_reqs"]
                        self.assertIsNotNone(cert_reqs)
                        normalized = cert_reqs.lower() if isinstance(cert_reqs, str) else cert_reqs
                        self.assertIn(
                            normalized,
                            (ssl.CERT_REQUIRED, "required"),
                            f"rediss:// must require TLS cert verification, got ssl_cert_reqs={cert_reqs!r}",
                        )

    def test_t2_f4_03_prune_aborts_raw_delete_when_rollup_fails(self):
        old_95d_ts = (_jst_now_naive() - datetime.timedelta(days=95)).strftime("%Y-%m-%d %H:%M:%S")
        local_db.insert_logs([{"name": "OLG 新宿", "men": 10, "women": 15, "source": "oriental"}], old_95d_ts)
        with patch.object(local_db, "rollup_store_hourly_stats", return_value={"ok": False, "error": "boom"}):
            res = local_db.prune_old_logs(monitor_days=90, vote_days=14)
            self.assertFalse(res["rollup_ok"])
            self.assertEqual(res["deleted_monitor_logs"], 0)
        cnt = local_db._fetch_all_rows("SELECT COUNT(*) FROM monitor_logs")[0][0]
        self.assertEqual(cnt, 1)

    def test_t2_f4_04_prune_anonymizes_15d_rows_and_preserves_13d_ip_hash(self):
        now_dt = _jst_now_naive()
        ts_15d = (now_dt - datetime.timedelta(days=15)).strftime("%Y-%m-%d %H:%M:%S")
        ts_13d = (now_dt - datetime.timedelta(days=13)).strftime("%Y-%m-%d %H:%M:%S")
        with local_db._db_lock:
            conn = local_db.get_connection()
            try:
                conn.execute(
                    "INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash, wait_minutes) VALUES (?, ?, ?, ?, ?)",
                    (ts_15d, "OLG 新宿", "smooth", "ip_old_15", 0),
                )
                conn.execute(
                    "INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash, wait_minutes) VALUES (?, ?, ?, ?, ?)",
                    (ts_13d, "OLG 新宿", "lively", "ip_fresh_13", 15),
                )
                conn.commit()
            finally:
                conn.close()
        local_db.prune_old_logs(monitor_days=90, vote_days=14)
        rows = local_db._fetch_all_rows("SELECT vote_type, ip_hash FROM crowd_votes ORDER BY timestamp ASC")
        self.assertEqual(len(rows), 2)
        self.assertEqual(rows[0][1], "")
        self.assertEqual(rows[1][1], "ip_fresh_13")

    def test_t2_f4_05_check_data_integrity_detects_future_ts_and_rogue_store(self):
        future_ts = (_jst_now_naive() + datetime.timedelta(minutes=45)).strftime("%Y-%m-%d %H:%M:%S")
        local_db.insert_logs([{"name": "TEST_ROGUE_STORE_E2E", "men": 5, "women": 5, "source": "test"}], future_ts)
        cdi = _load_script_module(".agents/skills/ops-sentinel/scripts/check_data_integrity.py", "e2e_cdi_err")
        report = cdi.run_integrity_checks(days=90)
        self.assertEqual(report["overall"], "error")
        by_name = {c["check"]: c["status"] for c in report["checks"]}
        self.assertEqual(by_name.get("future_timestamp_pollution"), "error")
        self.assertEqual(by_name.get("unregistered_store_pollution"), "error")

    def test_t2_f4_06_pg_pool_max_clamping_and_postgres_scheme_normalization(self):
        with patch.object(turbo_store, "_live_backends_blocked", return_value=False):
            with patch.dict(os.environ, {"DATABASE_URL": "postgres://user:pw@db.internal:5432/app"}):
                self.assertEqual(turbo_store.get_database_url(), "postgresql://user:pw@db.internal:5432/app")

            fake_pg_pool = MagicMock()
            with patch.object(turbo_store, "_psycopg2", MagicMock()):
                with patch.object(turbo_store, "_pg_pool", fake_pg_pool):
                    for idx, (raw_max, expected) in enumerate((("1", 4), ("999", 20))):
                        turbo_store.reset_for_tests()
                        turbo_store._pg_conn_pool = None
                        turbo_store._pg_url_cached = None
                        with patch.dict(os.environ, {"DATABASE_URL": f"postgresql://u:p@h/d_{idx}", "PG_POOL_MAX": raw_max}):
                            turbo_store._get_pg_pool()
                            _, kwargs = fake_pg_pool.ThreadedConnectionPool.call_args
                            self.assertEqual(kwargs["maxconn"], expected)


class TestTier2F5PortalEdgeAndErrors(_IsolatedE2EBase):
    def test_t2_f5_01_monthly_csv_empty_db_uses_no_cache_not_long_ttl(self):
        r_empty = self.client.get("/stats/monthly.csv?days=30")
        self.assertEqual(r_empty.status_code, 200)
        self.assertEqual(r_empty.headers.get("Cache-Control"), "no-cache, max-age=0")

    def test_t2_f5_02_hidden_store_excluded_from_all_public_apis_and_feeds(self):
        self._seed_live_stores()
        for off_m in (10, 20, 30):
            ts_recent = (_jst_now_naive() - datetime.timedelta(minutes=off_m)).strftime("%Y-%m-%d %H:%M:%S")
            local_db.insert_logs([{"name": "OLG 新宿", "men": 10, "women": 18, "source": "oriental"}], ts_recent)

        r_hide = self.client.post(
            "/api/ops/governance",
            json={"action": "toggle_store", "store_name": "OLG 新宿", "hidden": True},
            headers={"X-Admin-Secret": app_module.ADMIN_API_SECRET},
        )
        self.assertEqual(r_hide.status_code, 200)

        status_names = [s["name"] for s in self.client.get("/api/status").get_json()["ranking"]]
        self.assertNotIn("オリエンタルラウンジ 新宿", status_names)
        self.assertNotIn("OLG 新宿", status_names)
        self.assertNotIn("oriental-lounge-shinjuku", self.client.get("/stats/monthly.csv").get_data(as_text=True))
        self.assertNotIn("oriental-lounge-shinjuku", self.client.get("/api/llms-facts.json").get_data(as_text=True))
        self.assertEqual(self.client.get("/api/votes/OLG%20%E6%96%B0%E5%AE%BF").status_code, 404)
        self.assertEqual(self.client.get("/api/analysis/OLG%20%E6%96%B0%E5%AE%BF").status_code, 404)
        self.assertEqual(self.client.get("/store/oriental-lounge-shinjuku").status_code, 410)

    def test_t2_f5_03_embed_heatmap_121st_rate_limited_429_staff_test_exempt(self):
        for _ in range(120):
            self.assertEqual(self.client.get("/embed/heatmap?scope=all").status_code, 200)
        r_121 = self.client.get("/embed/heatmap?scope=all")
        self.assertEqual(r_121.status_code, 429)
        self.assertEqual(r_121.headers.get("Cache-Control"), "no-store")
        self.assertEqual(r_121.headers.get("Retry-After"), "60")

        r_st_anon = self.client.get("/embed/heatmap?scope=all&staff_test=1")
        self.assertEqual(r_st_anon.status_code, 429)

        r_st_admin = self.client.get(
            "/embed/heatmap?scope=all&staff_test=1",
            headers={"X-Admin-Secret": app_module.ADMIN_API_SECRET},
        )
        self.assertEqual(r_st_admin.status_code, 200)

    def test_t2_f5_04_invalid_monthly_ym_and_unknown_embed_store_return_404(self):
        self.assertEqual(self.client.get("/stats/monthly/not-a-month").status_code, 404)
        self.assertEqual(self.client.get("/embed/heatmap?store=nonexistent-store-slug").status_code, 404)
        self.assertEqual(self.client.get("/api/embed/heatmap.json?store=nonexistent-store-slug").status_code, 404)

    def test_t2_f5_05_control_chars_and_overlong_store_name_return_400_not_500(self):
        for ep_prefix, expected_codes in (("/api/votes/", (400,)), ("/api/analysis/", (400, 404))):
            r_ctrl = self.client.get(ep_prefix + "OLG%00%E6%96%B0%E5%AE%BF")
            self.assertIn(r_ctrl.status_code, expected_codes)
            r_long = self.client.get(ep_prefix + ("a" * 85))
            self.assertIn(r_long.status_code, expected_codes)


class TestTier2F6UiSecurityAndSyncBoundaries(_IsolatedE2EBase):
    def test_t2_f6_01_sw_js_excludes_staff_and_private_no_store_from_cache(self):
        sw_txt = (REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        self.assertIn("isStaffOrPrivateNoStore", sw_txt)
        self.assertIn("X-Staff-View", sw_txt)
        self.assertIn("no-store", sw_txt)

    def test_t2_f6_02_store_detail_no_inline_onclick_escapejs_dom_xss(self):
        """[Baseline Requirement] store_detail.html dynamic templates must not interpolate escapeJs() into inline onclick= attributes."""
        sd_txt = (REPO_ROOT / "templates" / "store_detail.html").read_text(encoding="utf-8")
        bad_matches = re.findall(r'onclick="[^"]*\$\{escapeJs\([^)]*\)\}[^"]*"', sd_txt)
        self.assertEqual(bad_matches, [], f"Unsafe inline onclick+escapeJs patterns found: {bad_matches}")

    def test_t2_f6_03_guide_templates_zero_dark_white_inline_colors_or_body_overflow_x(self):
        ui_mod = _load_script_module(".agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py", "e2e_ui_guide")
        issues = ui_mod.check_guide_light_mode_inline_colors()
        self.assertEqual(issues, [])

    def test_t2_f6_04_nav_loader_ignores_link_staff_api_csv_prefetch(self):
        nav_txt = (REPO_ROOT / "templates" / "_nav_loader.html").read_text(encoding="utf-8")
        self.assertIn("isIgnoredPath", nav_txt)
        self.assertTrue("/link/" in nav_txt or "'/li' + 'nk/'" in nav_txt)
        for pfx in ("/staff", "/api/", "/stats/monthly.csv"):
            self.assertIn(pfx, nav_txt)

    def test_t2_f6_05_calm_css_forbids_html_overflow_and_legacy_female_var(self):
        calm_txt = (REPO_ROOT / "static" / "calm.css").read_text(encoding="utf-8")
        self.assertNotIn("--calm-female", calm_txt)
        ui_mod = _load_script_module(".agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py", "e2e_ui_calm")
        self.assertEqual(ui_mod.check_calm_css_scroll_safety(), [])


class TestTier2F7CryptoAndWorkerBoundaries(_IsolatedE2EBase):
    def test_t2_f7_01_job_state_token_uses_authenticated_encryption_detects_tampering(self):
        """[Baseline Requirement] job_state token:* storage must use MAC/AEAD authenticated encryption and reject tampered ciphertexts."""
        secret_plain = "tok_live_secret_value_98765"
        job_state.set_value("token:e2e_aead_test", secret_plain)
        stored_json = json.loads(Path(job_state._resolve_local_file()).read_text(encoding="utf-8"))
        raw_blob = (stored_json.get("kv") or {}).get("token:e2e_aead_test", "")
        self.assertTrue(raw_blob)
        self.assertNotIn(secret_plain, raw_blob)

        # Flip 1 bit in the ciphertext payload; authenticated encryption (MAC/AEAD) must reject it
        prefix, body = raw_blob.split(":", 1) if ":" in raw_blob else ("", raw_blob)
        if prefix == "obf1":
            raw_bytes = bytearray(base64.urlsafe_b64decode(body.encode("ascii")))
            raw_bytes[-1] ^= 0x01
            tampered = f"{prefix}:{base64.urlsafe_b64encode(bytes(raw_bytes)).decode('ascii')}"
        else:
            idx = max(0, len(body.rstrip("=")) - 2)
            flipped_ch = "B" if body[idx] != "B" else "C"
            tampered = f"{prefix}:{body[:idx]}{flipped_ch}{body[idx + 1:]}" if prefix else f"{body[:idx]}{flipped_ch}{body[idx + 1:]}"
        decoded = job_state._deobfuscate_secret(tampered)
        self.assertEqual(decoded, "", f"Tampered token blob was accepted without MAC/AEAD rejection: {decoded!r}")

    def test_t2_f7_02_job_state_corrupt_obf1_never_leaks_raw_blob(self):
        res = job_state._deobfuscate_secret("obf1:!!!not-valid-base64!!!")
        self.assertEqual(res, "")

    def test_t2_f7_03_line_webhook_rejects_bad_sig_403_oversize_413_bad_json_400_unconfig_503(self):
        with patch.dict(os.environ, {"LINE_CHANNEL_SECRET": "", "LINE_CHANNEL_ACCESS_TOKEN": ""}):
            self.assertEqual(self.client.post("/api/line/webhook", data=b"{}").status_code, 503)

        sec = "line-secret-e2e"
        with patch.dict(os.environ, {"LINE_CHANNEL_SECRET": sec, "LINE_CHANNEL_ACCESS_TOKEN": "line-tok"}):
            self.assertEqual(
                self.client.post("/api/line/webhook", data=b'{"events":[]}', headers={"X-Line-Signature": "bad"}).status_code,
                403,
            )
            oversize = b"{" + (b" " * 66000) + b"}"
            self.assertEqual(self.client.post("/api/line/webhook", data=oversize).status_code, 413)

            bad_json = b"{not_json"
            good_sig = base64.b64encode(hmac.new(sec.encode("utf-8"), bad_json, hashlib.sha256).digest()).decode("ascii")
            self.assertEqual(
                self.client.post("/api/line/webhook", data=bad_json, headers={"X-Line-Signature": good_sig}).status_code,
                400,
            )

    def test_t2_f7_04_push_subscribe_blocks_http_ssrf_and_invalid_p256dh_keys(self):
        self._seed_live_stores(count=5)
        p256dh, auth = _make_valid_push_keys()
        for bad_ep in ("http://fcm.googleapis.com/fcm/send/1", "https://127.0.0.1/push", "https://evil.example/push"):
            r = self.client.post(
                "/api/push/subscribe",
                json={"store_name": "OLG 新宿", "target_cond": "female_55", "subscription": {"endpoint": bad_ep, "keys": {"p256dh": p256dh, "auth": auth}}},
            )
            self.assertEqual(r.status_code, 400, f"Endpoint {bad_ep} should be 400")

        r_bad_key = self.client.post(
            "/api/push/subscribe",
            json={"store_name": "OLG 新宿", "target_cond": "female_55", "subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/1", "keys": {"p256dh": "short", "auth": auth}}},
        )
        self.assertEqual(r_bad_key.status_code, 400)

    def test_t2_f7_05_sns_jobs_rejects_unauth_401_unknown_type_400_missing_id_404(self):
        self.assertEqual(self.client.post("/api/sns-jobs/enqueue", json={"job_type": "x_post"}).status_code, 401)
        hdr = {"X-Admin-Secret": app_module.ADMIN_API_SECRET}
        self.assertEqual(self.client.post("/api/sns-jobs/enqueue", json={"job_type": "shell_exec"}, headers=hdr).status_code, 400)
        self.assertEqual(self.client.get("/api/sns-jobs/nonexistent_job_999", headers=hdr).status_code, 404)


# =====================================================================
# Tier 3: Cross-Feature Combination Workflows (8 tests)
# =====================================================================
class TestTier3CrossFeatureWorkflows(_IsolatedE2EBase):
    def test_t3_01_f1_f2_f6_staff_login_no_cache_no_adsense_then_force_logout_revokes(self):
        self._seed_live_stores(count=15)
        staff_c = app_module.app.test_client()
        master_c = app_module.app.test_client()
        self.assertEqual(self._login_as("staff", client=staff_c).status_code, 302)
        self.assertEqual(self._login_as("master", client=master_c).status_code, 302)

        for p in ("/staff", "/"):
            r = staff_c.get(p)
            self.assertEqual(r.status_code, 200)
            self.assertEqual(r.headers.get("X-Staff-View"), "1")
            self.assertEqual(r.headers.get("Cache-Control"), "private, no-cache, no-store, must-revalidate")
            self.assertNotIn("pagead2.googlesyndication.com", r.get_data(as_text=True))

        r_force = master_c.post(
            "/api/ops/governance",
            json={"action": "force_logout_staff"},
            headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(r_force.status_code, 200)

        r_after = staff_c.get("/staff")
        self.assertEqual(r_after.status_code, 200)
        self.assertIn("パスコード", r_after.get_data(as_text=True))
        with staff_c.session_transaction() as sess:
            self.assertFalse(sess.get("is_staff"))

    def test_t3_02_f1_f3_f5_sns_inflow_track_link_human_proof_vs_staff_test_non_contam(self):
        self._seed_live_stores(count=15)
        app_module.app.config["ENFORCE_AFFILIATE_HUMAN_PROOF"] = True
        with patch.object(analytics, "track") as mock_track:
            r_home = self.client.get("/?s=x_post", headers={"User-Agent": _HUMAN_UA})
            self.assertEqual(r_home.status_code, 200)
            self.assertIn("an_src=", " ".join(r_home.headers.getlist("Set-Cookie")))

            r_trk = self.client.post("/api/track", json={"event": "cushion_click", "label": "dating"}, headers={"User-Agent": _HUMAN_UA})
            self.assertEqual(r_trk.status_code, 204)

            r_inter = self.client.get("/link/dating?from=/&s=x_post", headers={"User-Agent": _HUMAN_UA})
            if r_inter.status_code == 200:
                tok = re.search(r'name="csrf_clk"\s+value="([^"]+)"', r_inter.get_data(as_text=True)).group(1)
                r_go = self.client.post("/link/dating?from=/&s=x_post", data={"csrf_clk": tok}, headers={"User-Agent": _HUMAN_UA})
                self.assertEqual(r_go.status_code, 303)
            else:
                self.assertEqual(r_inter.status_code, 302)

            before_calls = len(mock_track.call_args_list)
            self.assertGreater(before_calls, 0)

            probe_c = app_module.app.test_client()
            probe_c.get("/?s=x_post&staff_test=1", headers={"User-Agent": _AUDIT_UA})
            probe_c.post("/api/track?staff_test=1", json={"event": "cushion_click", "label": "dating"}, headers={"User-Agent": _AUDIT_UA})
            probe_c.get("/link/dating?staff_test=1", headers={"User-Agent": _AUDIT_UA})
            self.assertEqual(len(mock_track.call_args_list), before_calls)

    def test_t3_03_f3_f4_f5_vote_watch_14d_anonymize_90d_rollup_and_csv_integrity(self):
        self._seed_live_stores(count=15)
        self.assertEqual(
            self.client.post("/api/vote", json={"store_name": "OLG 新宿", "vote_type": "smooth", "voter_id": "t3-v"}, headers={"User-Agent": _HUMAN_UA}).status_code,
            200,
        )
        self.assertEqual(
            self.client.post("/api/watch-intent", json={"store_name": "OLG 新宿", "target_cond": "zero_wait", "voter_id": "t3-w"}, headers={"User-Agent": _HUMAN_UA}).status_code,
            200,
        )

        now_dt = _jst_now_naive()
        ts_95d = (now_dt - datetime.timedelta(days=95)).strftime("%Y-%m-%d %H:%M:%S")
        ts_16d = (now_dt - datetime.timedelta(days=16)).strftime("%Y-%m-%d %H:%M:%S")
        ts_13d = (now_dt - datetime.timedelta(days=13)).strftime("%Y-%m-%d %H:%M:%S")
        local_db.insert_logs([{"name": "OLG 新宿", "men": 12, "women": 22, "source": "oriental"}], ts_95d)
        for off_m in (10, 20, 30):
            ts_1d = (now_dt - datetime.timedelta(days=1, minutes=off_m)).strftime("%Y-%m-%d %H:%M:%S")
            local_db.insert_logs([{"name": "OLG 新宿", "men": 14, "women": 24, "source": "oriental"}], ts_1d)

        with local_db._db_lock:
            conn = local_db.get_connection()
            try:
                conn.execute("UPDATE crowd_votes SET timestamp = ? WHERE store_name = ?", (ts_16d, "OLG 新宿"))
                conn.execute("UPDATE store_watch_intents SET timestamp = ? WHERE store_name = ?", (ts_16d, "OLG 新宿"))
                conn.execute(
                    "INSERT INTO crowd_votes (timestamp, store_name, vote_type, ip_hash, wait_minutes) VALUES (?, ?, ?, ?, ?)",
                    (ts_13d, "OLG 新宿", "lively", "keep_13d_vote", 10),
                )
                conn.execute(
                    "INSERT INTO store_watch_intents (timestamp, store_name, target_cond, ip_hash) VALUES (?, ?, ?, ?)",
                    (ts_13d, "OLG 新宿", "female_55", "keep_13d_watch"),
                )
                conn.commit()
            finally:
                conn.close()

        pruned = local_db.prune_old_logs(monitor_days=90, vote_days=14)
        self.assertTrue(pruned["rollup_ok"])
        self.assertEqual(pruned["deleted_monitor_logs"], 1)
        self.assertGreaterEqual(pruned["anonymized_crowd_votes"], 1)

        vote_rows = [
            tuple(r)
            for r in local_db._fetch_all_rows(
                "SELECT timestamp, ip_hash FROM crowd_votes WHERE store_name = ? ORDER BY timestamp ASC",
                ("OLG 新宿",),
            )
        ]
        self.assertEqual(len(vote_rows), 2)
        self.assertEqual(vote_rows[0], (ts_16d, ""))
        self.assertEqual(vote_rows[1], (ts_13d, "keep_13d_vote"))

        watch_rows = [
            tuple(r)
            for r in local_db._fetch_all_rows(
                "SELECT timestamp, ip_hash FROM store_watch_intents WHERE store_name = ? ORDER BY timestamp ASC",
                ("OLG 新宿",),
            )
        ]
        self.assertEqual(len(watch_rows), 2)
        self.assertEqual(watch_rows[0], (ts_16d, ""))
        self.assertEqual(watch_rows[1], (ts_13d, "keep_13d_watch"))

        csv_resp = self.client.get("/stats/monthly.csv?days=30")
        self.assertEqual(csv_resp.status_code, 200)
        self.assertIn("oriental-lounge-shinjuku", csv_resp.get_data(as_text=True))

        cdi = _load_script_module(".agents/skills/ops-sentinel/scripts/check_data_integrity.py", "e2e_t3_cdi")
        rep = cdi.run_integrity_checks(days=90)
        self.assertNotEqual(rep["overall"], "error")
        by_check = {c["check"]: c["status"] for c in rep["checks"]}
        self.assertEqual(by_check.get("privacy_14d_anonymization"), "ok")

    def test_t3_04_f1_f4_f7_worker_publishes_snapshot_web_syncs_and_updates_etag(self):
        turbo_store.enable_memory_bus_for_tests(True)
        stores_v1 = [{"name": "OLG 新宿", "women": 18, "men": 12, "source": "oriental"}]
        turbo_store.publish_live_snapshot(stores_v1, "2026-11-19 21:00:00")

        with patch.dict(os.environ, {"APP_ROLE": "web"}):
            self.assertTrue(app_module.sync_latest_data_from_shared_bus(force_l1=True))
            r1 = self.client.get("/api/status")
            etag1 = r1.headers.get("ETag")
            self.assertEqual(r1.get_json()["ranking"][0]["women"], 18)

            stores_v2 = [{"name": "OLG 新宿", "women": 31, "men": 15, "source": "oriental"}]
            turbo_store.publish_live_snapshot(stores_v2, "2026-11-19 21:01:00")
            self.assertTrue(app_module.sync_latest_data_from_shared_bus(force_l1=True))
            r2 = self.client.get("/api/status")
            etag2 = r2.headers.get("ETag")
            self.assertNotEqual(etag1, etag2)
            self.assertEqual(r2.get_json()["ranking"][0]["women"], 31)

    def test_t3_05_f2_f5_f1_ops_governance_hide_store_purges_caches_and_restores(self):
        self._seed_live_stores()
        for off_m in (10, 20, 30):
            ts_1h = (_jst_now_naive() - datetime.timedelta(hours=1, minutes=off_m)).strftime("%Y-%m-%d %H:%M:%S")
            local_db.insert_logs([{"name": "JIS SHINJUKU", "men": 10, "women": 18, "source": "jis"}], ts_1h)

        # Warm caches before hiding
        self.assertEqual(self.client.get("/api/status").status_code, 200)
        self.assertIn("jis-shinjuku", self.client.get("/stats/monthly.csv").get_data(as_text=True))

        hdr = {"X-Admin-Secret": app_module.ADMIN_API_SECRET}
        self.assertEqual(
            self.client.post("/api/ops/governance", json={"action": "toggle_store", "store_name": "JIS SHINJUKU", "hidden": True}, headers=hdr).status_code,
            200,
        )
        self.assertNotIn("jis-shinjuku", self.client.get("/stats/monthly.csv").get_data(as_text=True))
        self.assertNotIn("jis-shinjuku", self.client.get("/api/llms-facts.json").get_data(as_text=True))
        self.assertEqual(self.client.get("/store/jis-shinjuku").status_code, 410)

        self.assertEqual(
            self.client.post("/api/ops/governance", json={"action": "toggle_store", "store_name": "JIS SHINJUKU", "hidden": False}, headers=hdr).status_code,
            200,
        )
        self.assertIn("jis-shinjuku", self.client.get("/stats/monthly.csv").get_data(as_text=True))
        self.assertEqual(self.client.get("/store/jis-shinjuku").status_code, 200)

    def test_t3_06_f2_f7_master_async_sns_job_enqueue_drain_poll_completed(self):
        turbo_store.enable_memory_bus_for_tests(True)
        self._login_as("master")
        r_enq = self.client.post(
            "/api/sns-jobs/enqueue",
            json={"job_type": "prewarm_bundle", "params": {"include_column": False}, "worker_only": True},
            headers={"Origin": "http://localhost", "Sec-Fetch-Site": "same-origin"},
        )
        self.assertEqual(r_enq.status_code, 202)
        poll_url = r_enq.get_json()["poll_url"]

        with patch.object(app_module, "precompute_editorial_and_media_bundle", return_value={"ok": True, "slots": 4}):
            done = app_module.drain_sns_job_queue_once(timeout_sec=0.1)
        self.assertEqual(done["status"], "succeeded")

        r_poll = self.client.get(poll_url)
        self.assertEqual(r_poll.status_code, 200)
        self.assertEqual(r_poll.get_json()["job"]["result"], {"ok": True, "slots": 4})

    def test_t3_07_f3_f5_f7_push_subscribe_normal_vs_staff_test_watch_intent_isolation(self):
        self._seed_live_stores(count=10)
        p256dh, auth = _make_valid_push_keys()

        r_probe = self.client.post(
            "/api/push/subscribe?staff_test=1",
            json={
                "store_name": "OLG 新宿",
                "target_cond": "female_55",
                "subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/e2e-probe", "keys": {"p256dh": p256dh, "auth": auth}},
            },
            headers={"User-Agent": _AUDIT_UA},
        )
        self.assertEqual(r_probe.status_code, 200)
        cnt_after_probe = local_db._fetch_all_rows("SELECT COUNT(*) FROM store_watch_intents WHERE store_name = ?", ("OLG 新宿",))[0][0]
        self.assertEqual(cnt_after_probe, 0)

        r_human = self.client.post(
            "/api/push/subscribe",
            json={
                "store_name": "OLG 新宿",
                "target_cond": "female_55",
                "subscription": {"endpoint": "https://fcm.googleapis.com/fcm/send/e2e-human", "keys": {"p256dh": p256dh, "auth": auth}},
            },
            headers={"User-Agent": _HUMAN_UA},
        )
        self.assertEqual(r_human.status_code, 200)
        cnt_after_human = local_db._fetch_all_rows("SELECT COUNT(*) FROM store_watch_intents WHERE store_name = ?", ("OLG 新宿",))[0][0]
        self.assertEqual(cnt_after_human, 1)

    def test_t3_08_f5_f6_legacy_301_redirects_and_sw_bypass_headers_alignment(self):
        self._seed_live_stores()
        r_301 = self.client.get("/brand/oriental-lounge?s=x_post", follow_redirects=False)
        self.assertEqual(r_301.status_code, 301)
        loc = r_301.headers["Location"]
        target_path = urllib.parse.urlparse(loc).path + "?" + urllib.parse.urlparse(loc).query
        r_dest = self.client.get(target_path)
        self.assertEqual(r_dest.status_code, 200)

        sw_txt = (REPO_ROOT / "static" / "sw.js").read_text(encoding="utf-8")
        for must_bypass in ("/stats/monthly.csv", "/link/", "/staff", "/api/"):
            self.assertIn(must_bypass, sw_txt)


# =====================================================================
# Tier 4: Workloads, Performance Regression & Quality Gates (5 tests)
# =====================================================================
class TestTier4WorkloadsAndGates(_IsolatedE2EBase):
    def test_t4_01_f4_f5_r1_512mb_pg_oom_prevention_chunked_rollup_prune_tracemalloc(self):
        master = list((store_master.load() or {}).keys())[:50]
        now_dt = _jst_now_naive().replace(minute=0, second=0, microsecond=0)

        expired_days = (99, 98, 97, 96, 95, 94, 93, 92, 91)  # 9 expired days -> [:7] batch cap leaves 2 days for pass 2
        retained_days = (30, 20, 10, 2, 1)
        hours = (20, 21, 22)
        rows = []
        for day_off in expired_days + retained_days:
            for hr in hours:
                ts = (now_dt - datetime.timedelta(days=day_off)).replace(hour=hr).strftime("%Y-%m-%d %H:%M:%S")
                for idx, sname in enumerate(master):
                    rows.append((ts, sname, 10 + (idx % 10), 15 + (idx % 12), "oriental"))
        with local_db._db_lock:
            conn = local_db.get_connection()
            try:
                conn.executemany(
                    "INSERT INTO monitor_logs (timestamp, store_name, men, women, source) VALUES (?, ?, ?, ?, ?)",
                    rows,
                )
                conn.commit()
            finally:
                conn.close()

        start_ts = (now_dt - datetime.timedelta(days=100)).strftime("%Y-%m-%d %H:%M:%S")
        end_ts = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        tracemalloc.start()
        try:
            chunk_iter = local_db._iter_ts_window_in_7d_chunks(
                "SELECT timestamp, store_name, men, women FROM monitor_logs WHERE timestamp >= ? AND timestamp <= ?",
                start_ts,
                end_ts,
            )
            chunk_count = 0
            total_seen = 0
            max_chunk_rows = 0
            for ch in chunk_iter:
                chunk_count += 1
                total_seen += len(ch)
                if len(ch) > max_chunk_rows:
                    max_chunk_rows = len(ch)
            prune_res_1 = local_db.prune_old_logs(monitor_days=90, vote_days=14)
            _, peak_bytes = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()

        self.assertGreaterEqual(chunk_count, 14)
        self.assertEqual(total_seen, len(rows))
        self.assertLess(max_chunk_rows, len(rows))
        self.assertTrue(prune_res_1["rollup_ok"])
        per_day_rows = len(master) * len(hours)
        self.assertEqual(prune_res_1["deleted_monitor_logs"], per_day_rows * 7)
        self.assertLess(peak_bytes, 12 * 1024 * 1024)

        cutoff_90d = (now_dt.date() - datetime.timedelta(days=90)).strftime("%Y-%m-%d 00:00:00")
        rem_expired_1 = local_db._fetch_all_rows("SELECT COUNT(*) FROM monitor_logs WHERE timestamp < ?", (cutoff_90d,))[0][0]
        self.assertEqual(rem_expired_1, per_day_rows * 2)

        prune_res_2 = local_db.prune_old_logs(monitor_days=90, vote_days=14)
        self.assertTrue(prune_res_2["rollup_ok"])
        self.assertEqual(prune_res_2["deleted_monitor_logs"], per_day_rows * 2)
        rem_expired_2 = local_db._fetch_all_rows("SELECT COUNT(*) FROM monitor_logs WHERE timestamp < ?", (cutoff_90d,))[0][0]
        self.assertEqual(rem_expired_2, 0)
        rem_total = local_db._fetch_all_rows("SELECT COUNT(*) FROM monitor_logs")[0][0]
        self.assertEqual(rem_total, per_day_rows * len(retained_days))

    def test_t4_02_f1_f5_f6_r1_warm_cache_zero_recompute_etag_gzip_and_median_latency(self):
        import concierge_matcher

        self._seed_live_stores()
        # 1. Deterministic zero-recompute verification on `/` and `/api/concierge`
        self.assertEqual(self.client.get("/", headers={"Accept-Encoding": "gzip"}).status_code, 200)
        with patch.object(app_module, "render_template", wraps=app_module.render_template) as spy_render:
            r_home_warm = self.client.get("/", headers={"Accept-Encoding": "gzip"})
            self.assertEqual(r_home_warm.status_code, 200)
            self.assertEqual(spy_render.call_count, 0)

        self.assertEqual(self.client.get("/api/concierge?area=shinjuku&audience=men").status_code, 200)
        with patch.object(
            concierge_matcher,
            "diagnose_tonight_plan",
            wraps=concierge_matcher.diagnose_tonight_plan,
        ) as spy_conc:
            r_conc_warm = self.client.get("/api/concierge?area=shinjuku&audience=men")
            self.assertEqual(r_conc_warm.status_code, 200)
            self.assertEqual(spy_conc.call_count, 0)

        # 2. Gzip compression ratio & ETag 304 & small payload (<500B) uncompressed boundary
        r_raw = self.client.get("/api/status", headers={"Accept-Encoding": "identity"})
        r_gz = self.client.get("/api/status", headers={"Accept-Encoding": "gzip"})
        self.assertEqual(r_gz.headers.get("Content-Encoding"), "gzip")
        self.assertLess(len(r_gz.data), len(r_raw.data) * 0.65)
        self.assertEqual(gzip.decompress(r_gz.data), r_raw.data)
        self.assertEqual(self.client.get("/api/status", headers={"If-None-Match": r_gz.headers["ETag"]}).status_code, 304)
        self.assertNotIn("Content-Encoding", self.client.get("/api/health", headers={"Accept-Encoding": "gzip"}).headers)

        # 3. Warmup + N=15 median latency SLA (non-flaky, zero JSON output to disk)
        for _ in range(2):
            self.client.get("/api/status", headers={"Accept-Encoding": "gzip"})
        samples_ms = []
        for _ in range(15):
            t0 = time.perf_counter()
            r = self.client.get("/api/status", headers={"Accept-Encoding": "gzip"})
            samples_ms.append((time.perf_counter() - t0) * 1000.0)
            self.assertEqual(r.status_code, 200)
        self.assertLess(statistics.median(samples_ms), 100.0)

    def test_t4_03_f1_f3_f4_f5_concurrent_burst_requests_no_500_or_race(self):
        self._seed_live_stores()
        ts_recent = (_jst_now_naive() - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")
        local_db.insert_logs([{"name": "OLG 新宿", "men": 12, "women": 20, "source": "oriental"}], ts_recent)

        errors = []

        def _worker(idx: int):
            c = app_module.app.test_client()
            try:
                r1 = c.get("/")
                r2 = c.get("/api/status")
                r3 = c.get("/api/weekly-report")
                r4 = c.post(
                    "/api/watch-intent?staff_test=1",
                    json={"store_name": "OLG 新宿", "target_cond": "female_55", "voter_id": f"th-{idx}"},
                    headers={"User-Agent": _AUDIT_UA},
                )
                for resp in (r1, r2, r3, r4):
                    if resp.status_code >= 500:
                        errors.append(f"idx={idx} status={resp.status_code}")
            except Exception as exc:
                errors.append(f"idx={idx} exc={exc}")

        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(_worker, range(24)))
        self.assertEqual(errors, [])

    def test_t4_04_f2_f3_f4_f5_f6_r2_quality_gates_ui_affiliate_store_integrity(self):
        import subprocess

        safe_env = {
            **os.environ,
            "DISABLE_SHEETS": "1",
            "DISABLE_SCHEDULER": "1",
            "LOCAL_DB_PATH": os.environ["LOCAL_DB_PATH"],
            "JOB_STATE_DIR": os.environ["JOB_STATE_DIR"],
            "PYTHONPATH": str(REPO_ROOT),
        }
        p_ui = subprocess.run(
            [sys.executable, ".agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py", "--quick"],
            cwd=str(REPO_ROOT),
            env=safe_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_ui.returncode, 0, p_ui.stdout + p_ui.stderr)

        p_aff = subprocess.run(
            [sys.executable, ".agents/skills/affiliate/scripts/parse_and_audit_affiliate.py", "--audit"],
            cwd=str(REPO_ROOT),
            env=safe_env,
            capture_output=True,
            text=True,
        )
        self.assertEqual(p_aff.returncode, 0, p_aff.stdout + p_aff.stderr)

        import data_audit

        store_audit_mod = _load_script_module(".agents/skills/store-audit/scripts/run_full_store_audit.py", "e2e_store_audit")
        rep = data_audit.run_data_audit(send_alerts=False)
        self.assertNotEqual(rep["status"], "error", rep.get("issues"))
        extra_issues, _ = store_audit_mod.check_master_slugs_and_adjacent_specs()
        self.assertEqual(extra_issues, [])

        cdi = _load_script_module(".agents/skills/ops-sentinel/scripts/check_data_integrity.py", "e2e_t4_cdi")
        db_rep = cdi.run_integrity_checks(days=90)
        self.assertNotEqual(db_rep["overall"], "error")

    def test_t4_05_f1_f3_f5_f2_r2_ops_sentinel_live_probe_non_contam_contract(self):
        sentinel_mod = _load_script_module(".agents/skills/ops-sentinel/scripts/run_ops_sentinel.py", "e2e_sentinel_t4")
        seen_requests = []

        valid_robots = (
            "User-agent: *\nDisallow: /link/\nDisallow: /staff\n\n"
            "User-agent: GPTBot\nDisallow: /link/\nDisallow: /staff\n"
        )
        valid_sitemap = "<urlset>" + "".join(
            f"<url><loc>https://aiseki-navi.com/p/{i}</loc></url>" for i in range(48)
        ) + "".join(
            f"<url><loc>https://aiseki-navi.com{p}</loc></url>"
            for p in ("/stats", "/stats/year-end-2026", "/price-comparison", "/columns")
        ) + "</urlset>"
        valid_csv = "store_slug,store_name\noriental-lounge-shinjuku,オリエンタルラウンジ 新宿\n"

        def _make_mock_urlopen(overrides=None):
            ov = overrides or {}

            def _fake_urlopen(req, timeout=15):
                url = req.full_url
                ua = req.get_header("User-agent") or ""
                seen_requests.append((url, ua))
                path = urllib.parse.urlsplit(url).path
                if path == "/api/status":
                    body = ov.get("status", json.dumps({"ranking": [{"name": f"s{i}"} for i in range(50)], "timestamp": "2026-11-19 21:00:00"}))
                elif path == "/api/llms-facts.json":
                    body = ov.get("llms", json.dumps({"schema_version": "2.1", "stores": []}))
                elif path == "/stats/monthly.csv":
                    body = ov.get("csv", valid_csv)
                elif path == "/robots.txt":
                    body = ov.get("robots", valid_robots)
                elif path == "/sitemap.xml":
                    body = ov.get("sitemap", valid_sitemap)
                else:
                    body = ""
                resp = MagicMock()
                resp.status = 200
                resp.headers = {"Cache-Control": "public, max-age=60"}
                resp.read.return_value = body.encode("utf-8")
                ctx = MagicMock()
                ctx.__enter__.return_value = resp
                ctx.__exit__.return_value = False
                return ctx

            return _fake_urlopen

        with patch.object(sentinel_mod.urllib.request, "urlopen", side_effect=_make_mock_urlopen()):
            ok_res = sentinel_mod.run_live_probes("https://mock.aiseki-navi.local")
        self.assertEqual(ok_res["overall"], "ok", ok_res["errors"])
        self.assertEqual(len(seen_requests), 5)
        for req_url, req_ua in seen_requests:
            self.assertIn("staff_test=1", req_url)
            self.assertEqual(req_ua, sentinel_mod.AUDIT_UA)

        bad_cases = (
            {"status": json.dumps({"ranking": [{"name": "only_one"}]})},
            {"llms": json.dumps({"schema_version": "1.0"})},
            {"csv": "store_slug,url\ns1,/link/dating\n"},
            {"robots": "User-agent: *\nAllow: /\n"},
            {"sitemap": "<urlset><url><loc>https://aiseki-navi.com/</loc></url></urlset>"},
            {"sitemap": "<urlset>" + "".join(f"<url><loc>https://aiseki-navi.com/p/{i}</loc></url>" for i in range(55)) + "</urlset>"},
        )
        for bad_ov in bad_cases:
            with patch.object(sentinel_mod.urllib.request, "urlopen", side_effect=_make_mock_urlopen(bad_ov)):
                bad_res = sentinel_mod.run_live_probes("https://mock.aiseki-navi.local")
            self.assertEqual(bad_res["overall"], "error")


if __name__ == "__main__":
    unittest.main()
