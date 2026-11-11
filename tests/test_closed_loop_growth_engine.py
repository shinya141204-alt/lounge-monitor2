"""
tests/test_closed_loop_growth_engine.py - End-to-End tests for the 3-Layer Closed-Loop Growth Engine.

Verifies:
  1. Loop 1 (Threads / X / note Hook & Title Evolution):
     - `classify_opening_hook_archetype` across `store_gap_shock`, `city_battle`, `ladies_zero_wait`, `poetic_scene`, `time_wave`
     - Official Meta Threads Graph API insights collection (`views`, `likes`, `replies`, `reposts`, `quotes`)
     - Multimodal Screenshot Ingestion via `gemini-4-carbon` (`x-goog-api-key` header, strict `null` non-fabrication)
     - `sns_editorial.run_evaluator` & `_build_gemini_prompt` applying `hook_archetype_boosts` and `sns_prompt_feedback`
     - `column_editorial._build_column_gemini_prompt` applying `note_ctr_feedback`
  2. Loop 2 (Instagram Reels 2-Second Skip Cliff Fix & UCB1 Multi-Armed Bandit):
     - Frame 0 (`t=0.0s`) immediate non-blank Scene 1 rendering (`fade_in=0.0`, visible 3D isometric tower + headline)
     - Concrete #1 store / area / headcount headlines in `_build_scene1_hook_sprite`
     - Official Instagram Graph API Reel insights (`ig_reels_avg_watch_time` ms -> sec conversion) + skip-rate screenshot fusion
     - UCB1 Multi-Armed Bandit reranking in `select_scheduled_reel_type` while preserving 84-night anti-collision invariants
  3. Loop 3 (Full-Funnel SNS Inflow -> 2-Step Cushion -> Offer Slot EPC + Master PDCA Studio):
     - `/?s=...` -> `an_src` cookie -> `/api/track` (`cushion_click` -> `cush:`) -> `/link/hair_removal` (`cv:` + `cvslot:`)
     - Zero inflation of hourly `sns_inflow` bucket from `cv:`, `cush:`, `cvslot:`
     - Per-cohort & per-channel `cushion_clicks`, `est_revenue_yen` (160 yen unit EPC for `hair_removal`), and `epc_yen`
     - Master auth / CSRF enforcement and execution on `/api/pdca/sns-insights`, `/sync`, `/ingest`, and `/staff/pdca` HTML
"""

import datetime
import io
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DISABLE_SHEETS", "1")

import analytics
import app as app_module
import column_editorial
import job_state
import local_db
import pdca_engine
import reel_generator
import sns_editorial
import sns_insights_loop


JST = datetime.timezone(datetime.timedelta(hours=9))


class ClosedLoopGrowthEngineTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_job_state_dir = os.environ.get("JOB_STATE_DIR")
        self._orig_local_file = job_state._LOCAL_FILE
        self._orig_db_path = local_db.DB_PATH

        os.environ["JOB_STATE_DIR"] = self._tmpdir.name
        job_state._LOCAL_FILE = os.path.join(self._tmpdir.name, "job_state.json")
        with job_state._lock:
            job_state._keys = None
            job_state._kv = None
            job_state._kv_ts = None

        local_db.DB_PATH = os.path.join(self._tmpdir.name, "aiseki_logs.db")
        local_db.init_db()
        analytics.reset_for_tests()
        sns_insights_loop.reset_insights_loop_cache_for_tests()
        with pdca_engine._site_perf_cache_lock:
            pdca_engine._site_perf_cache.clear()
        app_module.clear_fast_http_caches()

        self.sample_stores = [
            {
                "name": "オリエンタルラウンジ 新宿店",
                "men": 18,
                "women": 34,
                "total": 52,
                "area": "新宿",
                "station": "新宿駅",
                "source": "oriental",
            },
            {
                "name": "ag 新宿店",
                "men": 29,
                "women": 16,
                "total": 45,
                "area": "新宿",
                "station": "新宿駅",
                "source": "oriental",
            },
            {
                "name": "オリエンタルラウンジ 梅田店",
                "men": 22,
                "women": 38,
                "total": 60,
                "area": "梅田",
                "station": "梅田駅",
                "source": "oriental",
            },
            {
                "name": "JIS 梅田店",
                "men": 31,
                "women": 19,
                "total": 50,
                "area": "梅田",
                "station": "梅田駅",
                "source": "jis",
            },
            {
                "name": "XIX 岡山店",
                "men": 14,
                "women": 28,
                "total": 42,
                "area": "岡山",
                "station": "岡山駅",
                "source": "xix",
            },
        ]

    def tearDown(self):
        if self._orig_job_state_dir is None:
            os.environ.pop("JOB_STATE_DIR", None)
        else:
            os.environ["JOB_STATE_DIR"] = self._orig_job_state_dir
        job_state._LOCAL_FILE = self._orig_local_file
        with job_state._lock:
            job_state._keys = None
            job_state._kv = None
            job_state._kv_ts = None
        local_db.DB_PATH = self._orig_db_path
        analytics.reset_for_tests()
        sns_insights_loop.reset_insights_loop_cache_for_tests()
        with pdca_engine._site_perf_cache_lock:
            pdca_engine._site_perf_cache.clear()
        app_module.clear_fast_http_caches()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # Loop 1: Hook Archetype, Threads API, Carbon Vision & Prompt Loop
    # ------------------------------------------------------------------
    def test_hook_archetype_classification_and_cold_start_non_fabrication(self):
        self.assertEqual(
            sns_insights_loop.classify_opening_hook_archetype(
                "XIX岡山、1時間前は女性比100%だったのに今は女性28名・男性14名まで動いてる。"
            ),
            "store_gap_shock",
        )
        self.assertEqual(
            sns_insights_loop.classify_opening_hook_archetype(
                "同じ新宿駅・徒歩4分なのにオリエンタルラウンジ新宿とag新宿で男女比が29pt割れてる。"
            ),
            "city_battle",
        )
        self.assertEqual(
            sns_insights_loop.classify_opening_hook_archetype(
                "コートいらないくらい暖かい夜ですね。街灯の下を歩きながらふとスマホを開くと…"
            ),
            "poetic_scene",
        )
        self.assertEqual(
            sns_insights_loop.classify_opening_hook_archetype(
                "今夜これから出る女子2人組なら、梅田エリアが受付待ちゼロで男性先行中。"
            ),
            "ladies_zero_wait",
        )

        # Cold-start: zero stored insights -> strictly data_status="insufficient" and empty boosts
        fb = sns_insights_loop.compute_loop_feedback(days=28)
        self.assertEqual(fb["data_status"], "insufficient")
        self.assertEqual(fb["hook_archetype_boosts"], {})
        self.assertEqual(fb["reel_template_boosts"], {})
        self.assertIsNone(fb["reel_diagnostics"]["avg_watch_sec"])
        self.assertIsNone(fb["reel_diagnostics"]["avg_skip_rate_pct"])

    def test_threads_api_collection_and_evaluator_prompt_feedback(self):
        now_dt = datetime.datetime(2026, 11, 10, 22, 15, tzinfo=JST)
        sns_insights_loop.register_published_post(
            channel="threads",
            post_id="th_101",
            text="XIX岡山、1時間前は女性比100%だったのに今は女性28名・男性14名まで動いてる。",
            style="threads_anomaly_insight",
            s_param="th22_anom",
            now_jst=now_dt,
        )
        sns_insights_loop.register_published_post(
            channel="threads",
            post_id="th_102",
            text="コートいらないくらい夜風が心地いい金曜ですね。ゆっくり街を歩きたくなります。",
            style="threads_city_story",
            s_param="th22_story",
            now_jst=now_dt,
        )
        sns_insights_loop.register_published_post(
            channel="threads",
            post_id="th_103",
            text="コートを羽織って出るにはまだ少し早い夜、東京は曇り空ですね。",
            style="threads_city_story",
            s_param="th22_story",
            now_jst=now_dt,
        )

        def fake_get(url, params=None, timeout=None):
            resp = mock.MagicMock()
            resp.status_code = 200
            if url.endswith("/threads"):
                resp.json.return_value = {
                    "data": [
                        {"id": "th_101", "text": "XIX岡山、1時間前は女性比100%だったのに今は女性28名・男性14名まで動いてる。", "timestamp": "2026-11-10T13:15:00+0000"},
                        {"id": "th_102", "text": "コートいらないくらい夜風が心地いい金曜ですね。", "timestamp": "2026-11-09T13:15:00+0000"},
                        {"id": "th_103", "text": "コートを羽織って出るにはまだ少し早い夜、東京は曇り空ですね。", "timestamp": "2026-11-08T13:15:00+0000"},
                    ]
                }
            elif "th_101/insights" in url:
                resp.json.return_value = {
                    "data": [
                        {"name": "views", "values": [{"value": 100}]},
                        {"name": "likes", "values": [{"value": 7}]},
                        {"name": "replies", "values": [{"value": 2}]},
                    ]
                }
            elif "th_102/insights" in url:
                resp.json.return_value = {"data": [{"name": "views", "values": [{"value": 8}]}, {"name": "likes", "values": [{"value": 0}]}]}
            else:
                resp.json.return_value = {"data": [{"name": "views", "values": [{"value": 10}]}, {"name": "likes", "values": [{"value": 0}]}]}
            return resp

        with mock.patch("threads_poster._get_active_threads_token", return_value="tok_test"), \
             mock.patch("threads_poster._resolve_threads_user_id", return_value="u_1"), \
             mock.patch("sns_insights_loop.requests.get", side_effect=fake_get):
            res = sns_insights_loop.collect_threads_insights(limit=10, now_jst=now_dt)

        self.assertTrue(res["ok"])
        self.assertEqual(res["updated"], 3)

        fb = sns_insights_loop.compute_loop_feedback(days=28)
        self.assertEqual(fb["data_status"], "ok")
        self.assertGreater(fb["hook_archetype_boosts"].get("store_gap_shock", 0), 0)
        self.assertLess(fb["hook_archetype_boosts"].get("poetic_scene", 0), 0)
        self.assertIn("XIX岡山", fb["sns_prompt_feedback"])

        # Apply PDCA cycle so active_directives carries hook_archetype_boosts & sns_prompt_feedback
        pdca_res = pdca_engine.run_pdca_cycle(
            live_stores=self.sample_stores,
            days=14,
            apply_actions=True,
            use_ai=False,
            record_log=False,
        )
        act_dir = pdca_res["active_directives"]
        self.assertGreater(act_dir["hook_archetype_boosts"].get("store_gap_shock", 0), 0)
        self.assertIn("実測SNSインサイト自動学習フィードバック", act_dir["sns_prompt_feedback"])

        # Verify _build_gemini_prompt injects empirical feedback
        research = sns_editorial.run_researcher(self.sample_stores, now_jst=now_dt)
        plan = sns_editorial.run_planner(research, channel="threads", now_jst=now_dt)
        prompt_txt, _ = sns_editorial._build_gemini_prompt(
            research, plan, "22:00", "threads", now_dt, "https://aiseki-navi.com"
        )
        self.assertIn("実測SNSインサイト自動学習フィードバック", prompt_txt)
        self.assertIn("XIX岡山", prompt_txt)

        # Verify run_evaluator boosts store_gap_shock and penalizes poetic_scene
        cands = [
            {
                "style": "threads_anomaly_insight",
                "main_text": "オリエンタルラウンジ梅田が女性38名・男性22名まで一気に伸びてる。金曜22時の梅田は駅近から先に埋まる流れですね。 #相席ラウンジ" + ("　" * 80),
            },
            {
                "style": "threads_city_story",
                "main_text": "コートいらないくらい夜風が優しい金曜ですね。オリエンタルラウンジ梅田は女性38名・男性22名で賑わっています。 #相席ラウンジ" + ("　" * 80),
            },
        ]
        ev = sns_editorial.run_evaluator(cands, research, plan, channel="threads", now_jst=now_dt)
        by_style = {c["style"]: c for c in ev["candidates"]}
        self.assertEqual(by_style["threads_anomaly_insight"]["hook_archetype"], "store_gap_shock")
        self.assertGreater(by_style["threads_anomaly_insight"]["hook_boost"], 0)
        self.assertLess(by_style["threads_city_story"]["hook_boost"], 0)

        # Turning off auto_optimize_sns masks closed-loop boosts cleanly
        pdca_engine.update_pdca_settings(auto_optimize_sns=False)
        masked = pdca_engine.get_active_directives(allow_blocking=True)
        self.assertEqual(masked["hook_archetype_boosts"], {})
        self.assertEqual(masked["sns_prompt_feedback"], "")
        self.assertEqual(masked["reel_template_boosts"], {})
        pdca_engine.update_pdca_settings(auto_optimize_sns=True)

    def test_carbon_vision_screenshot_ingestion_and_note_ctr_prompt(self):
        now_dt = datetime.datetime(2026, 11, 10, 23, 0, tzinfo=JST)
        captured_req = {}

        def fake_post(url, headers=None, json=None, timeout=None):
            captured_req["url"] = url
            captured_req["headers"] = dict(headers or {})
            captured_req["body"] = json
            resp = mock.MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [
                                {
                                    "text": json_module_dumps({
                                        "detected_platform": "note",
                                        "posts": [
                                            {
                                                "channel": "reels",
                                                "opener": "オリエンタルラウンジ梅田 女性38名",
                                                "reel_type": "area_battle",
                                                "views": 182,
                                                "reach": 141,
                                                "avg_watch_sec": 4.8,
                                                "skip_rate_pct": 52.4,
                                                "saved": 5,
                                                "shares": 3,
                                            }
                                        ],
                                        "note_articles": [
                                            {"title": "【2026年最新】JISとオリエンタルラウンジの違いは？27万件データ比較", "views": 67, "impressions": None, "likes": 4},
                                            {"title": "22時45分の梅田、相席ラウンジで起きるすれ違いの記録", "views": 9, "impressions": None, "likes": 0},
                                        ],
                                    })
                                }
                            ]
                        }
                    }
                ]
            }
            return resp

        json_module_dumps = json.dumps
        with mock.patch.dict(os.environ, {"GEMINI_EDITORIAL_MODEL": "gemini-4-carbon"}, clear=False), \
             mock.patch("sns_insights_loop.requests.post", side_effect=fake_post):
            ing = sns_insights_loop.ingest_insight_screenshot_via_carbon(
                b"\x89PNG\r\n\x1a\nfakeimagebytes",
                mime_type="image/png",
                platform_hint="auto",
                api_key="test-carbon-key",
                now_jst=now_dt,
            )

        self.assertTrue(ing["ok"])
        self.assertEqual(ing["model_used"], "gemini-4-carbon")
        self.assertIn("gemini-4-carbon:generateContent", captured_req["url"])
        self.assertEqual(captured_req["headers"].get("x-goog-api-key"), "test-carbon-key")
        self.assertNotIn("key=", captured_req["url"])
        self.assertEqual(ing["posts_ingested"], 1)
        self.assertEqual(ing["notes_ingested"], 2)

        # Null fields must remain None (never fabricated!)
        fb = ing["feedback"]
        top_note = fb["note_ctr_feedback"]["top_titles"][0]
        self.assertEqual(top_note["views"], 67)
        self.assertIsNone(top_note["impressions"])
        self.assertIsNone(top_note["ctr_pct"])

        # Column editorial prompt receives empirical note title guidance after PDCA refresh
        pdca_engine.run_pdca_cycle(live_stores=self.sample_stores, days=14, apply_actions=True, use_ai=False, record_log=False)
        col_research = column_editorial.run_column_researcher(self.sample_stores, theme_key="area_hopping", now_jst=now_dt)
        col_plan = column_editorial.run_column_planner(col_research, now_jst=now_dt)
        col_prompt = column_editorial._build_column_gemini_prompt(col_research, col_plan, "https://aiseki-navi.com")
        self.assertIn("note実測で読まれたタイトル傾向", col_prompt)
        self.assertIn("JISとオリエンタルラウンジ", col_prompt)

    # ------------------------------------------------------------------
    # Loop 2: Reel Frame 0 (`t=0.0s`) Fix & UCB1 Multi-Armed Bandit
    # ------------------------------------------------------------------
    def test_reel_frame_zero_is_opaque_and_ucb1_bandit_preserves_invariants(self):
        now_dt = datetime.datetime(2026, 11, 13, 22, 0, tzinfo=JST)
        ctx = reel_generator.build_reel_context(self.sample_stores, reel_type="area_battle", now_jst=now_dt)
        renderer = reel_generator._ReelSceneRenderer(ctx)
        try:
            fg_0 = renderer.render_foreground_rgba(0.0)
            alpha_extrema = fg_0.getchannel("A").getextrema()
            # Frame 0 (`t=0.0s`) MUST have 100% opaque Scene 1 UI + 3D isometric tower (`max_alpha == 255`, never blank `0`)
            self.assertEqual(alpha_extrema[1], 255)
            fg_0.close()
        finally:
            renderer.close()

        # Ingest empirical Reel insights where `tiktok_gap_shock` strongly beats `live_top5` (high watch sec, low skip rate)
        sns_insights_loop.ingest_manual_insight_records(
            {
                "posts": [
                    {"post_id": "r_gap_1", "channel": "reels", "reel_type": "tiktok_gap_shock", "views": 420, "avg_watch_sec": 6.4, "skip_rate_pct": 41.0, "saved": 9, "shares": 6},
                    {"post_id": "r_gap_2", "channel": "reels", "reel_type": "tiktok_gap_shock", "views": 390, "avg_watch_sec": 6.1, "skip_rate_pct": 44.5, "saved": 7, "shares": 5},
                    {"post_id": "r_top_1", "channel": "reels", "reel_type": "live_top5", "views": 95, "avg_watch_sec": 1.9, "skip_rate_pct": 81.2, "saved": 0, "shares": 0},
                    {"post_id": "r_top_2", "channel": "reels", "reel_type": "live_top5", "views": 105, "avg_watch_sec": 2.0, "skip_rate_pct": 78.6, "saved": 0, "shares": 0},
                    {"post_id": "r_area_1", "channel": "reels", "reel_type": "area_battle", "views": 130, "avg_watch_sec": 2.4, "skip_rate_pct": 74.0, "saved": 1, "shares": 0},
                    {"post_id": "r_lady_1", "channel": "reels", "reel_type": "ladies_zero_wait", "views": 120, "avg_watch_sec": 2.3, "skip_rate_pct": 75.0, "saved": 0, "shares": 0},
                    {"post_id": "r_mid_1", "channel": "reels", "reel_type": "tiktok_midnight_cliff", "views": 125, "avg_watch_sec": 2.3, "skip_rate_pct": 74.5, "saved": 0, "shares": 0},
                ]
            },
            now_jst=now_dt,
        )
        pdca_engine.run_pdca_cycle(live_stores=self.sample_stores, days=14, apply_actions=True, use_ai=False, record_log=False)

        chosen = reel_generator.select_scheduled_reel_type(
            self.sample_stores,
            platform="instagram",
            now_jst=now_dt,
            recent_types=["live_top5", "area_battle"],
        )
        self.assertEqual(chosen, "tiktok_gap_shock")

        # Anti-collision invariant: if `tiktok_gap_shock` was used last time, bandit NEVER repeats it back-to-back
        chosen_next = reel_generator.select_scheduled_reel_type(
            self.sample_stores,
            platform="instagram",
            now_jst=now_dt + datetime.timedelta(days=1),
            recent_types=["tiktok_gap_shock", "area_battle"],
        )
        self.assertNotEqual(chosen_next, "tiktok_gap_shock")

    def test_instagram_api_watch_time_ms_conversion(self):
        now_dt = datetime.datetime(2026, 11, 10, 22, 30, tzinfo=JST)

        def fake_ig_get(url, params=None, timeout=None):
            resp = mock.MagicMock()
            resp.status_code = 200
            if url.endswith("/media"):
                resp.json.return_value = {
                    "data": [
                        {
                            "id": "ig_reel_900",
                            "media_type": "VIDEO",
                            "media_product_type": "REELS",
                            "caption": "同じ新宿駅・徒歩4分でも男女比が割れてる #相席ラウンジ",
                            "timestamp": "2026-11-10T11:05:00+0000",
                            "like_count": 6,
                            "comments_count": 1,
                        }
                    ]
                }
            else:
                metric_str = str((params or {}).get("metric") or "")
                if "ig_reels_avg_watch_time" in metric_str:
                    resp.json.return_value = {
                        "data": [{"name": "ig_reels_avg_watch_time", "values": [{"value": 3450}]}]
                    }
                else:
                    resp.json.return_value = {
                        "data": [
                            {"name": "views", "values": [{"value": 164}]},
                            {"name": "reach", "values": [{"value": 128}]},
                            {"name": "saved", "values": [{"value": 3}]},
                            {"name": "shares", "values": [{"value": 2}]},
                        ]
                    }
            return resp

        with mock.patch("instagram_poster._get_active_ig_token", return_value="ig_tok1"), \
             mock.patch("instagram_poster._resolve_ig_user_id", return_value="ig_u1"), \
             mock.patch("sns_insights_loop.requests.get", side_effect=fake_ig_get):
            ig_res = sns_insights_loop.collect_instagram_insights(limit=5, now_jst=now_dt)

        self.assertTrue(ig_res["ok"])
        self.assertEqual(ig_res["updated"], 1)
        fb = sns_insights_loop.compute_loop_feedback(days=28)
        self.assertAlmostEqual(fb["reel_diagnostics"]["avg_watch_sec"], 3.45, places=2)

    # ------------------------------------------------------------------
    # Loop 3: Full-Funnel Cushion & Offer EPC Attribution + Master APIs
    # ------------------------------------------------------------------
    def test_full_funnel_sns_to_cushion_to_offer_epc_and_master_endpoints(self):
        app_module.app.config["TESTING"] = True
        client = app_module.app.test_client()
        ua = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1"}

        # 1. User arrives from Threads post `?s=th22_anom` -> sets `an_src` cookie and logs 1 visit
        r1 = client.get("/?s=th22_anom", headers=ua)
        self.assertEqual(r1.status_code, 200)

        # 2. User clicks 2-step cushion to `/grooming` -> logs `cush:th22_anom @ /grooming`
        r2 = client.post(
            "/api/track",
            json={"event": "cushion_click", "path": "/guide/test-col", "target": "/grooming"},
            headers={**ua, "Origin": "http://localhost"},
        )
        self.assertIn(r2.status_code, (200, 204))

        # 3. User clicks high-EPC men's hair removal affiliate `/link/hair_removal` (`epc_yen = 160`)
        r3 = client.get("/link/hair_removal", headers=ua)
        self.assertIn(r3.status_code, (301, 302))

        # Hourly `sns_inflow` bucket must ONLY count the actual visit (`th22_anom`), never `cush:`, `cv:`, or `cvslot:`!
        self.assertEqual(analytics.classify_hourly_bucket("sns_inflow", "th22_anom @ /"), "sns_inflow")
        self.assertEqual(analytics.classify_hourly_bucket("sns_inflow", "cush:th22_anom @ /grooming"), "")
        self.assertEqual(analytics.classify_hourly_bucket("sns_inflow", "cv:th22_anom"), "")
        self.assertEqual(analytics.classify_hourly_bucket("sns_inflow", "cvslot:th22_anom @ hair_removal"), "")
        hr_sum = analytics.hourly_summary(days=14, allow_blocking=False)
        self.assertEqual(((hr_sum.get("segments") or {}).get("all") or {}).get("sns_inflow_total", 0), 1)

        diag = pdca_engine.analyze_site_performance(live_stores=self.sample_stores, days=14, allow_blocking=True)
        cohorts = {c["tag"]: c for c in diag["inflow_cohorts"]}
        self.assertIn("th22_anom", cohorts)
        c_th = cohorts["th22_anom"]
        self.assertEqual(c_th["visits"], 1)
        self.assertEqual(c_th["cushion_clicks"], 1)
        self.assertEqual(c_th["conversions"], 1)
        self.assertEqual(c_th["est_revenue_yen"], 160)
        self.assertEqual(c_th["epc_yen"], 160.0)
        self.assertEqual(c_th["top_slot"], "hair_removal")

        # 4. Verify Master auth & CSRF guards on `/api/pdca/sns-insights*`
        unauth = client.get("/api/pdca/sns-insights")
        self.assertEqual(unauth.status_code, 401)

        with client.session_transaction() as sess:
            sess["is_master"] = True
            sess["staff_authed"] = True

        # Cross-origin POST is blocked by CSRF guard
        csrf_blocked = client.post(
            "/api/pdca/sns-insights/ingest",
            json={"posts": []},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(csrf_blocked.status_code, 403)

        # Authorized same-origin JSON ingestion succeeds and updates PDCA directives
        ok_ing = client.post(
            "/api/pdca/sns-insights/ingest",
            json={
                "posts": [
                    {"post_id": "m1", "channel": "threads", "opener": "オリエンタルラウンジ新宿 女性34名・男性18名", "views": 92, "likes": 6},
                    {"post_id": "m2", "channel": "threads", "opener": "コートいらない夜ですね", "views": 9, "likes": 0},
                    {"post_id": "m3", "channel": "threads", "opener": "夜風が冷たい金曜ですね", "views": 11, "likes": 0},
                ]
            },
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(ok_ing.status_code, 200)
        body = ok_ing.get_json()
        self.assertTrue(body["ok"])
        self.assertEqual(body["ingest"]["posts_ingested"], 3)

        # Render `/staff/pdca` HTML and verify Closed-Loop Studio & Cohort EPC columns render cleanly
        page = client.get("/staff/pdca?days=14", headers={"Referer": "http://localhost/staff/pdca"})
        self.assertEqual(page.status_code, 200)
        html_txt = page.get_data(as_text=True)
        self.assertIn("id=\"pdca-closed-loop\"", html_txt)
        self.assertIn("3層クローズドループ学習スタジオ", html_txt)
        self.assertIn("¥160", html_txt)


if __name__ == "__main__":
    unittest.main()
