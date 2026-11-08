"""Regression test suite for the Fable 5.1 full-codebase review & Opus 5.5 implementation (2026-11-08)."""
import datetime
import json
import os
import re
import tempfile
import unittest
from unittest import mock

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

import analytics
import app as app_module
import column_editorial
import events
import sns_editorial
import ig_carousel_generator
import job_state
import local_db
import ogp_generator
import ops_governance
import reel_generator
import request_guards
import ssr_payload
import store_analysis
import store_master
import visual_qa_gate
import weekly_report
import x_poster

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class TestFableOpusAuditOptimizations(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._orig_db = local_db.DB_PATH
        local_db.DB_PATH = os.path.join(self._tmp.name, "test_fable_opus.db")
        local_db.init_db()
        if hasattr(local_db, "reset_caches_for_tests"):
            local_db.reset_caches_for_tests()
        app_module.clear_fast_http_caches()
        ops_governance.invalidate_governance_cache()
        self.client = app_module.app.test_client()

    def tearDown(self):
        local_db.DB_PATH = self._orig_db
        if hasattr(local_db, "reset_caches_for_tests"):
            local_db.reset_caches_for_tests()
        app_module.clear_fast_http_caches()
        ops_governance.invalidate_governance_cache()
        # analytics.track() in tests buffers pending rows; never leak them into other test modules
        analytics.reset_for_tests()
        with events._store_events_memo_lock:
            events._store_events_memo.clear()
        self._tmp.cleanup()

    def test_unmeasured_benchmarks_never_emit_dummy_numbers(self):
        """Batch 1 C-1 / Batch 2 H1-H3 / Batch 4 C1: Unmeasured stores emit None, never 82/60/58/55/56/240000."""
        store_obj = {"name": "OLG 渋谷本店", "women": 12, "men": 8}
        app_module._attach_store_benchmarks(store_obj, "OLG 渋谷本店", 12, 8, {})
        self.assertIsNone(store_obj["core_time_short"])
        self.assertIsNone(store_obj["core_time_wpct"])
        self.assertIsNone(store_obj["midnight_retention_pct"])
        self.assertIsNone(store_obj["midnight_label_short"])
        self.assertFalse(store_obj["has_measured_core"])
        self.assertFalse(store_obj["has_measured_midnight"])

        pool = ssr_payload.build_deferred_card_pool({"ranking": [store_obj]}, start_idx=0)
        self.assertEqual(len(pool), 1)
        self.assertNotIn("mr", pool[0])
        self.assertNotIn("ct", pool[0])

        s7 = local_db.get_store_7day_analytics("OLG 渋谷本店")
        self.assertIsNone(s7["best_slot_pct"])
        self.assertIsNone(s7["best_wd_pct"])
        self.assertIsNone(s7["midnight_retention_pct"])

        with mock.patch("local_db.get_store_history", return_value=[
            {"timestamp": "2026-11-06 18:00:00", "women": 2, "men": 2},
            {"timestamp": "2026-11-07 18:00:00", "women": 3, "men": 2},
            {"timestamp": "2026-11-08 18:00:00", "women": 2, "men": 3},
        ]):
            sa, sa_status = store_analysis.compute_store_analysis_payload(
                "OLG 渋谷本店", {}, allow_sheets_fallback=False
            )
        self.assertEqual(sa_status, 200)
        self.assertIsNone(sa.get("core_time_wpct"))
        self.assertIsNone(sa.get("midnight_retention_pct"))

        ig_ctx = ig_carousel_generator.build_forecast_context(live_stores=[])
        self.assertEqual(ig_ctx.get("data_status"), "insufficient")
        self.assertEqual(ig_ctx.get("top_stores"), [])

        reel_ctx = reel_generator.build_reel_context([], reel_type="live_top5")
        self.assertEqual(reel_ctx.get("data_status"), "insufficient")
        self.assertEqual(reel_ctx.get("stores"), [])
        cap = reel_generator.build_reel_caption([], reel_type="live_top5")
        self.assertIsNone(re.search(r"\d+\s*名|\d+(?:\.\d+)?\s*%", cap), f"Dummy numbers found in empty caption: {cap}")

    def test_hidden_store_excluded_from_llms_feeds_and_zero_count_ratio_is_null(self):
        """Batch 1 C-2 & M-3: Hidden stores are filtered out of llms-full.txt & llms-facts.json; 0-guest ratio is null."""
        with app_module.data_lock:
            saved_full = list(app_module.latest_data.get("full_data") or [])
            app_module.latest_data["full_data"] = [
                {"name": "OLG 新宿", "women": 30, "men": 20, "total": 50, "region": "Kanto"},
                {"name": "OLG 恵比寿", "women": 0, "men": 0, "total": 0, "region": "Kanto"},
            ]
        try:
            with mock.patch.dict(os.environ, {"HIDDEN_STORES": "OLG 新宿"}, clear=False):
                ops_governance.invalidate_governance_cache()
                app_module.clear_fast_http_caches()

                r_full = self.client.get("/llms-full.txt")
                self.assertEqual(r_full.status_code, 200)
                full_txt = r_full.data.decode("utf-8")
                self.assertNotIn("/store/oriental-lounge-shinjuku", full_txt)

                r_facts = self.client.get("/api/llms-facts.json")
                self.assertEqual(r_facts.status_code, 200)
                facts = r_facts.get_json()
                slugs = {s.get("slug") for s in (facts.get("stores") or [])}
                self.assertNotIn("oriental-lounge-shinjuku", slugs)
                ebisu = next((s for s in (facts.get("stores") or []) if s.get("slug") == "oriental-lounge-ebisu"), None)
                self.assertIsNotNone(ebisu)
                self.assertIsNone(ebisu.get("female_ratio_pct"))
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = saved_full
            ops_governance.invalidate_governance_cache()
            app_module.clear_fast_http_caches()

    def test_request_body_max_content_length_413_and_parse_jst_ts(self):
        """Batch 1 H-1 & M-2: Oversized JSON POST returns 413; parse_jst_ts handles ISO and malformed inputs."""
        big_body = '{"event":"page_view","pad":"' + ("x" * (300 * 1024)) + '"}'
        r = self.client.post("/api/track", data=big_body, content_type="application/json")
        self.assertEqual(r.status_code, 413)

        self.assertIsNotNone(request_guards.parse_jst_ts("2026-11-08 22:30:00"))
        self.assertIsNotNone(request_guards.parse_jst_ts("2026-11-08T22:30:00.123456+09:00"))
        self.assertIsNone(request_guards.parse_jst_ts("not-a-timestamp"))
        self.assertIsNone(request_guards.parse_jst_ts(None))

    def test_forecast_accuracy_query_validation_and_votes_hidden_guard(self):
        """Batch 1 H-2 & M-10: Overlong/control-char store query returns 400; hidden store votes returns 404."""
        r_long = self.client.get("/api/forecast-accuracy?store=" + ("a" * 85))
        self.assertEqual(r_long.status_code, 400)
        r_ctrl = self.client.get("/api/forecast-accuracy?store=abc%00def")
        self.assertEqual(r_ctrl.status_code, 400)

        r_vote_long = self.client.get("/api/votes/" + ("x" * 85))
        self.assertEqual(r_vote_long.status_code, 400)
        with mock.patch.dict(os.environ, {"HIDDEN_STORES": "OLG 新宿"}, clear=False):
            ops_governance.invalidate_governance_cache()
            r_vote_hidden = self.client.get("/api/votes/OLG 新宿")
            self.assertEqual(r_vote_hidden.status_code, 404)

    def test_analytics_allows_vote_wait_and_watch_intent_and_events_memoizes(self):
        """Batch 3 H2 & H3: vote_wait and watch_intent are tracked; get_events_for_store hits memo cache."""
        self.assertIn("vote_wait", analytics.ALLOWED_EVENTS)
        self.assertIn("watch_intent", analytics.ALLOWED_EVENTS)
        self.assertTrue(analytics.track("vote_wait", "OLG 新宿"))
        self.assertTrue(analytics.track("watch_intent", "OLG 新宿"))

        sample_evs = [{"id": "ev1", "store": "新宿", "brand": "oriental", "title": "秋の感謝祭", "url": "https://oriental-lounge.com/events/1"}]
        with events._store_events_memo_lock:
            events._store_events_memo.clear()
        with mock.patch.object(events, "_filter_events_for_store", wraps=events._filter_events_for_store) as filt:
            r1 = events.get_events_for_store("OLG 新宿", store_only=True, allow_blocking=False, preloaded_events=sample_evs)
            r2 = events.get_events_for_store("OLG 新宿", store_only=True, allow_blocking=False, preloaded_events=sample_evs)
        self.assertEqual(r1, r2)
        # 2nd call must be served from the memo (filter executed only once)
        self.assertEqual(filt.call_count, 1)
        with events._store_events_memo_lock:
            self.assertGreaterEqual(len(events._store_events_memo), 1)
        # Memo hits hand out copies: mutating one result never corrupts the memo or later results
        r1.append({"id": "injected"})
        r3 = events.get_events_for_store("OLG 新宿", store_only=True, allow_blocking=False, preloaded_events=sample_evs)
        self.assertEqual(r3, r2)
        self.assertNotIn({"id": "injected"}, r3)

    def test_fetch_all_events_returns_shallow_copy_of_memory_cache(self):
        """fetch_all_events must not hand out the shared cache list (caller sort()/append() corruption)."""
        saved_cache = events._events_memory_cache
        now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M:%S")
        cached_list = [
            {"id": "evA", "title": "A", "brand": "oriental"},
            {"id": "evB", "title": "B", "brand": "ag"},
        ]
        events._events_memory_cache = {"updated_at": now_str, "events": cached_list}
        try:
            for blocking in (True, False):
                got = events.fetch_all_events(allow_blocking=blocking)
                self.assertIsNot(got, cached_list)
                self.assertEqual(got, cached_list)
                got.append({"id": "evX"})
                got.sort(key=lambda e: e["id"], reverse=True)
                self.assertEqual([e["id"] for e in events._events_memory_cache["events"]], ["evA", "evB"])
        finally:
            events._events_memory_cache = saved_cache
            with events._store_events_memo_lock:
                events._store_events_memo.clear()

    def test_column_candidates_never_leak_none_percent_and_guard_saves(self):
        """column_editorial: unmeasured midnight_retention_pct never renders as 'None%'; s2 != s1; save guards."""
        live = [
            {"name": "OLG 新宿", "women": 40, "men": 25, "region": "Kanto"},
            {"name": "JIS SHINJUKU", "women": 22, "men": 30, "region": "Kanto"},
            {"name": "OLG ag渋谷", "women": 35, "men": 20, "region": "Kanto"},
            {"name": "OLG 福岡", "women": 28, "men": 18, "region": "Kyushu"},
        ]
        base_research = column_editorial.run_column_researcher(live_stores=live, theme_key="area_hopping")
        for st in base_research.get("featured_stores") or []:
            st["midnight_retention_pct"] = None
        self.assertTrue(base_research.get("featured_stores"))

        leak_re = re.compile(r"None\s*[%％]|集計中\s*[%％]")
        for theme_key in column_editorial.COLUMN_THEMES:
            for tone in sns_editorial.EDITORIAL_TONE_MODES:
                research = dict(base_research, theme_key=theme_key)
                plan = column_editorial.run_column_planner(research=research)
                cands = column_editorial._build_deterministic_candidates(
                    research, plan, site_url="https://aiseki-navi.com", tone_mode=tone
                )
                self.assertEqual(len(cands), 2)
                for c in cands:
                    blob = json.dumps(c, ensure_ascii=False)
                    self.assertIsNone(leak_re.search(blob), f"None% leaked ({theme_key}/{tone})")
                    self.assertIsNone(leak_re.search(c.get("x_main_text") or ""), f"None% in X text ({theme_key}/{tone})")
                    self.assertNotIn("None", c.get("x_main_text") or "")

        # Single measured store: s2 must not be the same store as s1, and the placeholder draft is not publishable
        one_research = dict(base_research, featured_stores=list(base_research["featured_stores"][:1]), local_leaders=[])
        one_plan = column_editorial.run_column_planner(research=one_research)
        one_cands = column_editorial._build_deterministic_candidates(one_research, one_plan, site_url="https://aiseki-navi.com")
        s1_name = one_research["featured_stores"][0]["ja_name"]
        for c in one_cands:
            self.assertNotIn(f"『{s1_name}』と『{s1_name}』", json.dumps(c, ensure_ascii=False))
            self.assertEqual(c.get("data_status"), "insufficient")
            self.assertFalse(column_editorial.save_editorial_column(c, publish_mode="schedule").get("ok"))

        # research.data_status=insufficient propagates to every candidate and blocks saving
        ins_research = dict(base_research, data_status="insufficient")
        ins_plan = column_editorial.run_column_planner(research=ins_research)
        ins_cands = column_editorial._build_deterministic_candidates(ins_research, ins_plan, site_url="https://aiseki-navi.com")
        for c in ins_cands:
            self.assertEqual(c.get("data_status"), "insufficient")
            res = column_editorial.save_editorial_column(c, publish_mode="schedule")
            self.assertFalse(res.get("ok"))

        # Explicit None% in any publishable field is refused even when data_status is ok
        leaky = {"title": "深夜維持None%の店", "desc": "要約", "data_status": "ok", "sections": []}
        self.assertFalse(column_editorial.save_editorial_column(leaky).get("ok"))
        leaky_sec = {
            "title": "正常タイトル", "desc": "要約", "data_status": "ok",
            "sections": [{"heading": "h", "paragraphs": ["終電後キープ目安はNone%に達し"]}],
        }
        self.assertFalse(column_editorial.save_editorial_column(leaky_sec).get("ok"))
        leaky_x = {"title": "正常タイトル", "desc": "要約", "x_main_text": "深夜維持None%", "sections": []}
        self.assertFalse(column_editorial.save_editorial_column(leaky_x).get("ok"))

    def test_public_body_limit_256kb_but_admin_paths_allow_up_to_2mb(self):
        """request_guards: app-wide hard cap 2MB; public POST >256KB -> 413; admin paths are not cut at 256KB."""
        self.assertEqual(app_module.app.config.get("MAX_CONTENT_LENGTH"), 2 * 1024 * 1024)
        body_300k = '{"pad":"' + ("x" * (300 * 1024)) + '"}'
        r_pub = self.client.post("/api/track", data=body_300k, content_type="application/json")
        self.assertEqual(r_pub.status_code, 413)
        r_admin = self.client.post("/api/column-editorial/publish", data=body_300k, content_type="application/json")
        self.assertNotEqual(r_admin.status_code, 413)  # unauthenticated -> 401/403, but never body-size 413
        body_3m = '{"pad":"' + ("x" * (3 * 1024 * 1024)) + '"}'
        r_huge = self.client.post("/api/column-editorial/publish", data=body_3m, content_type="application/json")
        self.assertEqual(r_huge.status_code, 413)
        self.assertTrue(request_guards._is_admin_body_path("/staff"))
        self.assertTrue(request_guards._is_admin_body_path("/api/pdca/run"))
        self.assertFalse(request_guards._is_admin_body_path("/staff-login-fake"))
        self.assertFalse(request_guards._is_admin_body_path("/api/track"))

    def test_staged_snapshot_requires_45_stores_before_short_circuit(self):
        """request_guards.staged_snapshot_around: a 10-store early-morning snapshot must not stop the search."""
        calls = []

        class _FakeDB:
            @staticmethod
            def get_snapshot_around(_offset, minutes):
                calls.append(minutes)
                n = {180: 10, 1440: 30, 10080: 60}[minutes]
                return {f"S{i}": {} for i in range(n)}

        snap = request_guards.staged_snapshot_around(_FakeDB)
        self.assertEqual(len(snap), 60)
        self.assertEqual(calls, [180, 1440, 10080])

    def test_job_state_secret_key_compat_and_corrupted_token_safety(self):
        """Batch 3 H1: job_state uses SECRET_KEY, decodes legacy obfuscated tokens, and rejects broken obf1 strings."""
        with mock.patch.dict(os.environ, {"SECRET_KEY": "unit-test-secret-key-xyz"}, clear=False):
            enc = job_state._obfuscate_secret("my-access-token-123")
            self.assertTrue(enc.startswith("obf1:"))
            self.assertEqual(job_state._deobfuscate_secret(enc), "my-access-token-123")
            self.assertEqual(job_state._deobfuscate_secret("obf1:corrupted_invalid_hex"), "")

    def test_media_qa_ogp_failopen_x_weight_and_eyecatch_traversal_guard(self):
        """Batch 4 H1, H4, H5, H6, M5: OGP logger fail-open, QA template normalization, -7h night shift, bare domain weight, path guard."""
        self.assertTrue(hasattr(ogp_generator, "logger"))
        with mock.patch("visual_qa_gate.inspect_image_visual_quality", side_effect=RuntimeError("boom")):
            qa_res = ogp_generator.inspect_ogp_visual_quality(b"\x89PNG\r\n\x1a\n")
            self.assertIsInstance(qa_res, dict)

        with mock.patch(
            "visual_qa_gate.inspect_reel_visual_quality",
            return_value={"pass": False, "overall_score": 30, "recommended_template": "../../etc/passwd", "issues": ["bad"]},
        ):
            final_rt, _qa_meta = visual_qa_gate.enforce_reel_visual_qa([], "live_top5")
            self.assertIn(final_rt, reel_generator.REEL_TEMPLATES)

        jst = datetime.timezone(datetime.timedelta(hours=9))
        sun_01am = datetime.datetime(2026, 11, 8, 1, 0, tzinfo=jst)  # Saturday night business hours
        theme = ig_carousel_generator._resolve_day_theme(sun_01am, "11/7(土)・11/8(日)")
        self.assertEqual(theme["theme_key"], "weekend")

        w_bare = x_poster.compute_twitter_weight("詳細はこちら aiseki-navi.com/guide/crowd-rush-paradox")
        w_http = x_poster.compute_twitter_weight("詳細はこちら https://aiseki-navi.com/guide/crowd-rush-paradox")
        self.assertEqual(w_bare, w_http)

        img_bytes, _mime = x_poster.load_column_eyecatch_bytes({"eyecatch_jpg": "../../.env"})
        self.assertIsNone(img_bytes)

    def test_frontend_singleflight_abort_safe_url_and_etag_contracts(self):
        """Batch 5 H1, H2, H3, M1, M3, M8: Static JS/template contracts for singleflight, abort, safeHttpUrl, and ETag."""
        with open(os.path.join(PROJECT_ROOT, "static", "app.js"), "r", encoding="utf-8") as fp:
            app_js = fp.read()
        self.assertIn("_statusInFlight", app_js)
        self.assertIn("_modalAbort", app_js)
        self.assertIn("safeHttpUrl", app_js)
        self.assertIn("data-copy-text", app_js)
        self.assertNotIn('onclick="copySecondVenueText', app_js)
        self.assertNotIn("function escapeJsString", app_js)
        self.assertNotIn("* 0.42", app_js)

        with open(os.path.join(PROJECT_ROOT, "templates", "store_detail.html"), "r", encoding="utf-8") as fp:
            sd_html = fp.read()
        self.assertIn("If-None-Match", sd_html)
        self.assertNotIn("/api/status?t=", sd_html)


if __name__ == "__main__":
    unittest.main()
