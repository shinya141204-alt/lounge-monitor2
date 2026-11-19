import copy
import datetime
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import analytics
import app as app_module
import job_state
import pdca_engine

JST = datetime.timezone(datetime.timedelta(hours=9))
REPO_ROOT = Path(__file__).resolve().parents[1]


class TestPdcaInflowFirstV2(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self._tmpdir = tempfile.mkdtemp(prefix="pdca_inflow_v2_")
        self._orig_db = getattr(app_module.local_db, "DB_PATH", None)
        app_module.local_db.DB_PATH = os.path.join(self._tmpdir, "test.db")
        app_module.local_db.init_db()
        with app_module.data_lock:
            self._saved_latest = copy.deepcopy(app_module.latest_data)
        self._saved_job_mem = dict(getattr(job_state, "_mem_cache", {}))
        pdca_engine.reset_pdca_cache_for_tests()
        self.client = app_module.app.test_client()

    def tearDown(self):
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        if hasattr(job_state, "_mem_cache"):
            job_state._mem_cache.clear()
            job_state._mem_cache.update(self._saved_job_mem)
        if self._orig_db is not None:
            app_module.local_db.DB_PATH = self._orig_db
        pdca_engine.reset_pdca_cache_for_tests()
        app_module.clear_fast_http_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _login_master(self):
        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True
            sess["auth_gen"] = app_module._compute_session_auth_gen(True)

    def test_growth_phase_auto_thresholds_and_manual_override_api(self):
        """PV規模に応じた3フェーズ自動遷移（<300 / 300-999 / 1000+）と /api/pdca/settings の手動切替・不正値拒否"""
        p1 = pdca_engine.resolve_growth_phase(45.0, "auto")
        self.assertEqual(p1["active_phase"], "phase1_inflow")
        self.assertTrue(p1["is_auto"])
        self.assertEqual(p1["step1_pct"], 45)
        self.assertEqual(p1["milestone_pct"], 15)
        self.assertEqual(p1["min_pv_for_funnel_action"], 50)

        p2 = pdca_engine.resolve_growth_phase(420.0, "auto")
        self.assertEqual(p2["active_phase"], "phase2_engagement")
        self.assertEqual(p2["min_pv_for_funnel_action"], 20)

        p3 = pdca_engine.resolve_growth_phase(1250.0, "auto")
        self.assertEqual(p3["active_phase"], "phase3_monetize")
        self.assertEqual(p3["min_pv_for_funnel_action"], 5)

        p_manual = pdca_engine.resolve_growth_phase(15.0, "phase3_monetize")
        self.assertEqual(p_manual["active_phase"], "phase3_monetize")
        self.assertFalse(p_manual["is_auto"])

        self._login_master()
        r_bad = self.client.post(
            "/api/pdca/settings",
            json={"growth_phase_mode": "invalid_phase"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r_bad.status_code, 400)

        r_ok = self.client.post(
            "/api/pdca/settings",
            json={"growth_phase_mode": "phase2_engagement", "auto_optimize_sns": True},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r_ok.status_code, 200)
        j_ok = r_ok.get_json()
        self.assertEqual(j_ok["settings"]["growth_phase_mode"], "phase2_engagement")
        self.assertEqual(j_ok["active_directives"]["growth_phase"], "phase2_engagement")

    def test_inflow_north_star_wow_and_inflow_first_boosts_and_actions(self):
        """Phase 1で純新規流入・WoW成長率・検索/ショート動画優先・高PVゲート（50PV未満は収益提案除外）が機能すること"""
        now_d = datetime.datetime.now(JST).date()
        d_curr = now_d.strftime("%Y-%m-%d")
        d_prev = (now_d - datetime.timedelta(days=9)).strftime("%Y-%m-%d")

        mock_rows = [
            # Prior week (10 inflows: 6 search, 4 short video; 80 PV)
            [d_prev, "page_view", "/", "80"],
            [d_prev, "sns_inflow", "ref_google", "6"],
            [d_prev, "sns_inflow", "short_bio", "4"],
            # Current week (25 inflows -> +150.0% WoW!; 210 PV -> 15 PV/day over 14d = Phase 1)
            [d_curr, "page_view", "/", "180"],
            [d_curr, "page_view", "/guide/second-venue", "30"],  # 30 PV < 50 PV gate in Phase 1!
            [d_curr, "sns_inflow", "ref_google", "10"],
            [d_curr, "sns_inflow", "short_bio", "7"],
            [d_curr, "sns_inflow", "x22_surge_focus", "7"],  # 7 pure visits, 0 affiliate clicks
            [d_curr, "sns_inflow", "x20_ladies_night", "1"],  # 1 visit + 1 fluke affiliate click
            [d_curr, "sns_inflow", "cv:x20_ladies_night", "1"],
        ]
        fake_loop_fb = {
            "gsc_diagnostics": {
                "data_status": "ok",
                "total_impressions": 640,
                "total_clicks": 22,
                "opportunity_queries": [
                    {
                        "query": "オリエンタルラウンジ 新宿 男女比",
                        "impressions": 85,
                        "clicks": 0,
                        "position": 6.4,
                        "page": "/guide/shinjuku",
                    }
                ],
            },
            "youtube_shorts_diagnostics": {"data_status": "ok", "total_views": 1800},
        }

        pdca_engine.update_pdca_settings(growth_phase_mode="auto", auto_optimize_sns=True)
        with analytics._lock:
            analytics._pending.clear()
            analytics._flushed.clear()
            analytics._sheet_rows_cache["ts"] = 0.0
            analytics._sheet_rows_cache["rows"] = None

        with mock.patch("sheets_store.read_rows", return_value=mock_rows), \
             mock.patch("sns_insights_loop.compute_loop_feedback", return_value=fake_loop_fb), \
             mock.patch("sns_editorial.get_gemini_api_key", return_value=""):
            diag = pdca_engine.analyze_site_performance(live_stores=[], days=14, allow_blocking=True)

        self.assertEqual(diag["growth_phase"]["active_phase"], "phase1_inflow")
        ns = diag["inflow_north_star"]
        # Window 14d external inflows = 10 + 25 = 35 (cv:x20_ladies_night must NOT be counted as a visit!)
        self.assertEqual(ns["external_inflow_total"], 35)
        self.assertEqual(ns["search_inflow_total"], 16)
        self.assertEqual(ns["short_video_inflow_total"], 11)
        self.assertEqual(ns["inflow_wow"]["curr_7d"], 25)
        self.assertEqual(ns["inflow_wow"]["prev_7d"], 10)
        self.assertEqual(ns["inflow_wow"]["wow_pct"], 150.0)

        # Score axes in Phase 1 sum max_score == 100 (35 + 30 + 25 + 10)
        self.assertEqual(sum(ax["max_score"] for ax in diag["score_axes"]), 100)

        # High pure-visit style (`surge_focus`, 7 visits) MUST beat 1-visit/1-CV fluke (`ladies_night`)
        boosts = diag["proposed_directives"]["sns_style_boosts"]
        self.assertEqual(boosts.get("surge_focus"), 4)
        self.assertGreater(boosts.get("surge_focus", 0), boosts.get("ladies_night", 0))

        # GSC opportunity guide (`/guide/shinjuku`) injected into priority_column_paths
        self.assertIn("/guide/shinjuku", diag["proposed_directives"]["priority_column_paths"])

        # TOP 3 actions in Phase 1 (<50 PV per page): zero `funnel_tuning`, led by `seo_ctr_boost`, `column_theme`, `reels_video`
        act_types = [a["action_type"] for a in diag["weekly_growth_actions"]]
        self.assertNotIn("funnel_tuning", act_types)
        self.assertEqual(act_types[0], "seo_ctr_boost")
        self.assertIn("オリエンタルラウンジ 新宿 男女比", diag["weekly_growth_actions"][0]["title"])

        # Now raise `/guide/second-venue` PV to 65 (>= 50 PV gate) with 0 clicks -> `funnel_tuning` becomes eligible!
        mock_rows_high_pv = list(mock_rows) + [[d_curr, "page_view", "/guide/second-venue", "35"]]
        with analytics._lock:
            analytics._sheet_rows_cache["ts"] = 0.0
            analytics._sheet_rows_cache["rows"] = None
        with mock.patch("sheets_store.read_rows", return_value=mock_rows_high_pv), \
             mock.patch("sns_insights_loop.compute_loop_feedback", return_value=fake_loop_fb):
            diag_high = pdca_engine.analyze_site_performance(live_stores=[], days=14, allow_blocking=True)
        act_types_high = [a["action_type"] for a in diag_high["weekly_growth_actions"]]
        self.assertIn("funnel_tuning", act_types_high)

    def test_staff_pdca_ui_and_growth_pdca_skill_script_phase1_alignment(self):
        """/staff/pdca HTML（<progress>・フェーズ切替ラジオ・インラインon*=ゼロ）と rank_opportunities.py の整合"""
        self._login_master()
        r = self.client.get("/staff/pdca?days=14")
        self.assertEqual(r.status_code, 200)
        html = r.get_data(as_text=True)
        self.assertIn('class="phase-progress"', html)
        self.assertIn('name="growth_phase_mode"', html)
        self.assertIn("Phase 1｜流入拡大最優先", html)
        self.assertIn("外部純新規流入（前週比）", html)

        # Verify rank_opportunities.py defers affiliate_upgrade out of TOP 3 in Phase 1
        sig_path = os.path.join(self._tmpdir, "signals_test.json")
        out_md = os.path.join(self._tmpdir, "pdca_out.md")
        sample_signals = {
            "collected_at": "2026-11-20 02:00:00 JST",
            "window_days": 14,
            "store_stats_top20": [{"area_label": "新宿", "store_name": "オリエンタルラウンジ 新宿", "peak_women": 55}],
            "guide_coverage": {"total_guide_templates": 38, "city_mention_counts_in_guides": {"新宿": 3}},
            "upcoming_arena_events": [{"venue_name": "東京ドーム", "title": "DOME TOUR", "event_date": "2026-11-28", "category": "music"}],
            "gsc_top_queries": [{"query": "相席ラウンジ 新宿 評判", "impressions": 60, "clicks": 0, "position": 8.2}],
            "affiliate_slot_optimization_candidates": [{"slot_id": "women_beauty_clinic", "is_unmonetized_url": True}],
            "pdca_engine_summary": {
                "growth_phase": {"active_phase": "phase1_inflow", "label": "Phase 1｜流入拡大最優先", "min_pv_for_funnel_action": 50},
                "inflow_north_star": {"external_inflow_total": 18, "daily_pv": 24.5, "short_video_inflow_total": 6, "inflow_wow": {"short_label": "前週比 +80.0%"}},
            },
        }
        with open(sig_path, "w", encoding="utf-8") as fp:
            json.dump(sample_signals, fp, ensure_ascii=False)

        script_path = REPO_ROOT / ".agents" / "skills" / "growth-pdca" / "scripts" / "rank_opportunities.py"
        proc = subprocess.run(
            [sys.executable, str(script_path), "--in", sig_path, "--out", out_md, "--json"],
            capture_output=True,
            text=True,
            check=True,
        )
        parsed = json.loads(proc.stdout)
        top3_types = [item["type"] for item in parsed["top3"]]
        self.assertNotIn("affiliate_upgrade", top3_types)
        self.assertIn("seo_rewrite_or_new", top3_types)


if __name__ == "__main__":
    unittest.main()
