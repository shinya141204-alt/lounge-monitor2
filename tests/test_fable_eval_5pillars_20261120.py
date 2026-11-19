# -*- coding: utf-8 -*-
"""
tests/test_fable_eval_5pillars_20261120.py
Claude Fable 5.2 本番サイト評価（2026-11-20）に基づく5本柱改修の包括回帰テスト
"""
import copy
import datetime
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

from bs4 import BeautifulSoup

os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["DISABLE_SHEETS"] = "1"

import analytics
import app as app_module
import job_state
import pdca_engine
import store_master
import weekly_report
import worker as worker_module
import x_poster

JST = datetime.timezone(datetime.timedelta(hours=9))
BANNED_BRAND_RE = re.compile(
    r"オリエンタル(?!ラウンジ)|(?<![A-Za-z])OLG(?![A-Za-z])|(?<![A-Za-z])YATAKOI(?![A-Za-z])|オリラジ|\bORIENTAL\b(?!\s*LOUNGE)",
    re.IGNORECASE,
)


class FableEvalFivePillarsTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self._tmpdir = tempfile.mkdtemp(prefix="fable_5p_")
        self._orig_db = getattr(app_module.local_db, "DB_PATH", None)
        app_module.local_db.DB_PATH = os.path.join(self._tmpdir, "test.db")
        app_module.local_db.init_db()
        with app_module.data_lock:
            self._saved_latest = copy.deepcopy(app_module.latest_data)
        self._saved_job_mem = dict(getattr(job_state, "_mem_cache", {}))
        pdca_engine.reset_pdca_cache_for_tests()
        analytics.reset_for_tests()
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
        analytics.reset_for_tests()
        app_module.clear_fast_http_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    # ------------------------------------------------------------------
    # Pillar 1: Measurement Truthfulness & Event Tracking Fixes
    # ------------------------------------------------------------------
    def test_pillar1_beacon_allowlist_and_tracking_hooks(self):
        for ev in ("map_cafe", "map_bar", "official_click", "co_view", "sister_bridge_impression", "sister_bridge_click"):
            self.assertIn(ev, app_module._BEACON_ALLOWED_EVENTS)

        # Normal human browser POST records events cleanly
        ua = {"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}
        r1 = self.client.post("/api/track", json={"event": "map_cafe", "label": "OLG ag広島"}, headers=ua)
        r2 = self.client.post("/api/track", json={"event": "official_click", "label": "JIS MATSUYAMA"}, headers=ua)
        self.assertEqual(r1.status_code, 204)
        self.assertEqual(r2.status_code, 204)

        # Passive co_view must NOT mint an_hp human-proof cookie
        fresh_client = app_module.app.test_client()
        r_cv = fresh_client.post("/api/track", json={"event": "co_view", "label": "OLG 新宿|JIS SHINJUKU"}, headers=ua)
        self.assertEqual(r_cv.status_code, 204)
        self.assertNotIn("an_hp=", str(r_cv.headers.getlist("Set-Cookie")))

        # staff_test=1 must NOT pollute analytics
        r3 = self.client.post("/api/track?staff_test=1", json={"event": "official_click", "label": "POLLUTION"}, headers=ua)
        self.assertEqual(r3.status_code, 204)

        summ = analytics.summary(days=1, allow_blocking=True)
        self.assertGreaterEqual(summ["by_event"].get("map_cafe", 0), 1)
        self.assertGreaterEqual(summ["by_event"].get("official_click", 0), 1)
        self.assertNotIn("POLLUTION", summ["all_by_label"].get("official_click", {}))

        # Check JS hook coverage in cta_tracker.js & app.js
        root = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
        with open(os.path.join(root, "static", "cta_tracker.js"), encoding="utf-8") as fp:
            cta_js = fp.read()
        for tok in ("detail-map-cafe-btn", "detail-map-bar-btn", "detail-map-shisha-btn", "detail-map-yokocho-btn", "official_click", "detail-second-venue-cards"):
            self.assertIn(tok, cta_js)
        with open(os.path.join(root, "static", "app.js"), encoding="utf-8") as fp:
            app_js = fp.read()
        for tok in ("modal-map-shisha-btn", "modal-map-yokocho-btn", "modal-second-venue-cards"):
            self.assertIn(tok, app_js)

    def test_pillar1_pdca_human_pv_truth_and_honest_revenue_and_gaps(self):
        now_d = datetime.datetime.now(JST).date()
        d_curr = now_d.strftime("%Y-%m-%d")
        d_prev = (now_d - datetime.timedelta(days=9)).strftime("%Y-%m-%d")

        # Reproduce real production trap: huge raw crawler PV (2800 / 14d = 200/d) & 1700 article views,
        # but real human_pv started 7 days ago (`d_hpv_start`..`d_curr` = 7d span, 210 total -> 30.0/d < 100!)
        # and clicks_7d = 0 (old 14d bot clicks = 180).
        d_hpv_start = (now_d - datetime.timedelta(days=6)).strftime("%Y-%m-%d")
        mock_rows = [
            [d_prev, "page_view", "/", "600"],
            [d_prev, "page_view", "/guide/last-train-cliff", "900"],
            [d_prev, "affiliate_click", "dating_instant @ /", "180"],
            [d_hpv_start, "human_pv", "/", "70"],
            [d_curr, "page_view", "/", "500"],
            [d_curr, "page_view", "/guide/last-train-cliff", "800"],
            [d_curr, "page_view", "/guide/second-venue", "25"],
            [d_curr, "human_pv", "/", "70"],
            [d_curr, "human_pv", "/store/ag-hiroshima", "70"],
            [d_curr, "store_open", "OLG ag広島", "12"],
            [d_curr, "sns_inflow", "ref_x", "40"],
            [d_curr, "sns_inflow", "ref_google", "20"],
        ]
        fake_loop = {
            "gsc_diagnostics": {
                "data_status": "ok",
                "total_impressions": 1462,
                "total_clicks": 86,
                "w1_clicks": 29,
                "w2_clicks": 35,
                "opportunity_queries": [
                    {"query": "ag hiroshima レビュー", "impressions": 54, "clicks": 3, "position": 9.8, "page": "/store/ag-hiroshima"}
                ],
            },
            "youtube_shorts_diagnostics": {"data_status": "ok", "total_views": 3000},
        }
        pdca_engine.update_pdca_settings(growth_phase_mode="auto", auto_optimize_sns=True)
        with mock.patch("sheets_store.read_rows", return_value=mock_rows), \
             mock.patch("sns_insights_loop.compute_loop_feedback", return_value=fake_loop), \
             mock.patch("sns_editorial.get_gemini_api_key", return_value=""):
            diag = pdca_engine.analyze_site_performance(live_stores=[], days=14, allow_blocking=True)

        gp = diag["growth_phase"]
        ns = diag["inflow_north_star"]
        self.assertEqual(gp["pv_basis"], "human_pv")
        self.assertEqual(ns["pv_basis"], "human_pv")
        # 210 human_pv in 7d -> 30.0 PV/day (must NOT declare 100 PV/day achieved!)
        self.assertAlmostEqual(gp["daily_pv"], 30.0, places=1)
        self.assertAlmostEqual(ns["daily_pv"], 30.0, places=1)
        self.assertGreater(ns["raw_daily_pv"], 150.0)
        self.assertNotIn("100 PV/日達成", gp["next_phase_hint"])
        self.assertIn("実ブラウザPV基準", gp["next_phase_hint"])

        # GSC WoW surfaced cleanly (29 -> 35 = +20.7%)
        self.assertIsNotNone(ns["gsc_wow"])
        self.assertEqual(ns["gsc_wow"]["prev_7d"], 29)
        self.assertEqual(ns["gsc_wow"]["curr_7d"], 35)
        self.assertAlmostEqual(ns["gsc_wow"]["wow_pct"], 20.7, places=1)

        # Engaged retention axis must NOT award 10/10 off crawler article views
        ret_axis = next(a for a in diag["score_axes"] if a["key"] == "engaged_retention")
        self.assertLess(ret_axis["score"], 10)

        # Revenue honesty: clicks_7d == 0 -> est_monthly_pace_yen must be None (no fake ¥42,064/mo)
        self.assertIsNone(diag["revenue"]["est_monthly_pace_yen"])
        self.assertEqual(diag["revenue"]["revenue_status"], "unverified_accumulating")

        # Offer pitches must not contain unverified hype claims
        for oc in pdca_engine.OFFER_CATEGORIES:
            for hype in ("鉄板の勝ち筋", "高成約率が出ます", "高CTRが出ます", "最強です"):
                self.assertNotIn(hype, oc["winning_pitch"])

        # Monetization gaps for <50 PV pages in Phase 1 (<50 human PV/d) must be deferred, NOT "🔥 最優先"
        low_pv_gaps = [g for g in diag["monetization_gaps"] if g["pv"] < 50]
        for g in low_pv_gaps:
            self.assertEqual(g["severity"], "deferred_phase1")
            self.assertNotIn("最優先", g["severity_badge"])

        # M1 anti-spike check: a 1-day burst of 350 human_pv must NOT auto-promote Phase 1 -> Phase 2
        burst_rows = [
            [d_curr, "page_view", "/", "400"],
            [d_curr, "human_pv", "/", "350"],
        ]
        with mock.patch("sheets_store.read_rows", return_value=burst_rows), \
             mock.patch("sns_insights_loop.compute_loop_feedback", return_value=fake_loop), \
             mock.patch("sns_editorial.get_gemini_api_key", return_value=""):
            burst_diag = pdca_engine.analyze_site_performance(live_stores=[], days=14, allow_blocking=True)
        self.assertEqual(burst_diag["growth_phase"]["active_phase"], "phase1_inflow")
        self.assertAlmostEqual(burst_diag["growth_phase"]["daily_pv"], 50.0, places=1)
        self.assertEqual(burst_diag["inflow_north_star"]["human_pv_span_days"], 7)

    # ------------------------------------------------------------------
    # Pillar 2: 63 Store Detail Pages SEO + FAQPage + Operating Status + llms-facts
    # ------------------------------------------------------------------
    def test_pillar2_all_63_stores_romanized_aliases_clean(self):
        master = store_master.load()
        self.assertGreaterEqual(len(master), 60)
        for sname in master.keys():
            rom = store_master.romanized_name(sname)
            aliases = store_master.search_aliases(sname)
            if rom:
                self.assertIsNone(BANNED_BRAND_RE.search(rom), f"Banned token in romanized_name({sname}): {rom}")
            for al in aliases:
                self.assertIsNone(BANNED_BRAND_RE.search(al), f"Banned token in search_aliases({sname}): {al}")

        self.assertEqual(store_master.romanized_name("OLG ag広島"), "ag HIROSHIMA")
        self.assertEqual(store_master.romanized_name("OLG ag梅田"), "ag UMEDA")
        self.assertEqual(store_master.romanized_name("JIS SHINJUKU"), "JIS SHINJUKU")
        self.assertEqual(store_master.romanized_name("JIS MATSUYAMA"), "JIS MATSUYAMA")
        self.assertEqual(store_master.romanized_name("ASOBIBAR 新宿"), "ASOBIBAR SHINJUKU")
        self.assertEqual(store_master.romanized_name("YATAKOI UMEDA"), "")

    def test_pillar2_store_detail_seo_faq_bridges_and_official_tracking(self):
        with app_module.data_lock:
            app_module.latest_data["full_data"] = [
                {"name": "OLG ag広島", "women": 18, "men": 12, "region": "Chugoku", "source": "oriental"},
                {"name": "JIS MATSUYAMA", "women": 0, "men": 0, "region": "Shikoku", "source": "jis"},
                {"name": "OLG 新宿", "women": 24, "men": 18, "region": "Kanto", "source": "oriental"},
            ]
            app_module.latest_data["last_updated"] = "2026-11-20 21:30:00"
            app_module.latest_data["is_seed"] = False

        for slug, expected_alias in (
            ("ag-hiroshima", "ag HIROSHIMA"),
            ("jis-matsuyama", "JIS MATSUYAMA"),
            ("oriental-lounge-shinjuku", "オリエンタルラウンジ 新宿"),
        ):
            resp = self.client.get(f"/store/{slug}")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            soup = BeautifulSoup(html, "html.parser")

            self.assertIn(expected_alias, soup.title.string)
            self.assertIn("営業時間・稼働状況", html)
            self.assertIn("いまの店が合わないとき、徒歩圏の現在人数を見る", html)
            self.assertIn("このあと落ち着いて話せる店を徒歩圏で探す", html)
            self.assertIn('data-track-event="official_click"', html)
            self.assertIn('id="store-faq-section"', html)
            self.assertNotIn("平均女性None", html)
            self.assertNotIn("（None時点", html)
            self.assertNotIn("枠が/10分", html)

            # Verify valid JSON-LD with LocalBusiness.alternateName (no boilerplate FAQPage JSON-LD; visible FAQ section only)
            ld_scripts = soup.find_all("script", attrs={"type": "application/ld+json"})
            found_lb_alt = False
            for sc in ld_scripts:
                parsed = json.loads(sc.string)
                nodes = parsed if isinstance(parsed, list) else [parsed]
                for node in nodes:
                    t = node.get("@type")
                    self.assertNotEqual(t, "FAQPage")
                    if isinstance(t, list) and "LocalBusiness" in t and node.get("alternateName"):
                        found_lb_alt = True
            self.assertTrue(found_lb_alt, f"LocalBusiness alternateName missing on /store/{slug}")

            faq_sec = soup.find(id="store-faq-section")
            self.assertIsNotNone(faq_sec)
            self.assertEqual(len(faq_sec.select(".store-faq-item")), 3)
            self.assertNotIn("/link/", str(faq_sec))

    def test_pillar2_all_63_stores_render_zero_none_in_visible_text(self):
        """H-1 GO gate: render ALL master stores (including Seoul) and confirm zero literal 'None' in visible UI text."""
        master = store_master.load()
        self.assertGreaterEqual(len(master), 60)
        with app_module.data_lock:
            app_module.latest_data["full_data"] = []
            app_module.latest_data["last_updated"] = "2026-11-20 21:30:00"
            app_module.latest_data["is_seed"] = False
        for sname in master.keys():
            slug = store_master.slug_of(sname)
            resp = self.client.get(f"/store/{slug}?staff_test=1")
            self.assertEqual(resp.status_code, 200, f"/store/{slug} returned {resp.status_code}")
            soup = BeautifulSoup(resp.get_data(as_text=True), "html.parser")
            for tag in soup(["script", "style"]):
                tag.decompose()
            vis_text = soup.get_text(" ", strip=True)
            self.assertNotRegex(
                vis_text,
                r"\bNone\b",
                f"Literal 'None' leaked into visible text on /store/{slug}",
            )

    def test_pillar2_and_5_llms_facts_json_schema_v21_enhancements(self):
        resp = self.client.get("/api/llms-facts.json?staff_test=1")
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data["schema_version"], "2.1")
        self.assertIn("measurement_unit_note", data["dataset_summary"])
        stores = data.get("stores") or []
        self.assertGreaterEqual(len(stores), 50)
        ag_h = next((s for s in stores if s.get("slug") == "ag-hiroshima"), None)
        self.assertIsNotNone(ag_h)
        self.assertEqual(ag_h["romanized_name"], "ag HIROSHIMA")
        self.assertIn(ag_h["operating_status"], ("live_now", "measured_within_7d", "unverified"))
        self.assertIn("last_measured_date", ag_h)
        self.assertIn(ag_h["waveform_status"], ("ok", "insufficient"))
        self.assertIn("hourly_waveform_30d", ag_h)
        self.assertIn("ag HIROSHIMA", ag_h["search_aliases"])

        # H4 check: when latest_data.is_seed is True, store detail must NOT assert active_open
        with app_module.data_lock:
            app_module.latest_data["is_seed"] = True
            app_module.latest_data["full_data"] = [
                {"name": "OLG ag広島", "women": 20, "men": 10, "region": "Chugoku", "source": "oriental"},
            ]
        try:
            seed_resp = self.client.get("/store/ag-hiroshima?staff_test=1")
            self.assertEqual(seed_resp.status_code, 200)
            seed_html = seed_resp.get_data(as_text=True)
            self.assertNotIn("計測中（営業時間内）", seed_html)
            self.assertNotIn("夜間コア帯", seed_html)
        finally:
            with app_module.data_lock:
                app_module.latest_data["is_seed"] = False

    # ------------------------------------------------------------------
    # Pillar 3: X Peak Weekend Search-Catcher Expansion
    # ------------------------------------------------------------------
    def test_pillar3_x_weekend_search_catcher_slots_and_copy(self):
        sched = worker_module.build_worker_scheduler()
        jobs_by_id = {j.id: j for j in sched.get_jobs()}
        self.assertIn("x_search_2015", jobs_by_id)
        self.assertIn("x_search_2200", jobs_by_id)
        self.assertIn("x_search_2330", jobs_by_id)
        self.assertIn("stats_cache_prewarm_20m", jobs_by_id)
        self.assertIn("day_of_week='sat'", str(jobs_by_id["x_search_2015"].trigger))
        self.assertIn("day_of_week='fri,sat'", str(jobs_by_id["x_search_2330"].trigger))

        sample_stores = [
            {"name": "JIS SHINJUKU", "women": 26, "men": 14, "region": "Kanto"},
            {"name": "OLG ag渋谷", "women": 20, "men": 12, "region": "Kanto"},
            {"name": "OLG 梅田", "women": 22, "men": 15, "region": "Kinki"},
        ]
        for slot_lbl, expected_tag in (
            ("20:15", "x20_search_20261121"),
            ("22:00", "x22_search_20261121"),
            ("23:30", "x23_search_20261121"),
            ("金土22:00", "x22_search_20261121"),
        ):
            hh, mm = (22, 0) if "金土" in slot_lbl else tuple(map(int, slot_lbl.split(":")))
            dt = datetime.datetime(2026, 11, 21, hh, mm, tzinfo=JST)
            # Test all 3 observation branches (female_lead, male_lead, balanced) with long store names
            for branch_stores in (
                sample_stores,
                [
                    {"name": "OLG 札幌", "women": 10, "men": 26, "region": "Hokkaido"},
                    {"name": "ASOBIBAR 茶屋町", "women": 12, "men": 24, "region": "Kinki"},
                    {"name": "OLG 新宿", "women": 14, "men": 22, "region": "Kanto"},
                ],
                [
                    {"name": "OLG 渋谷", "women": 16, "men": 15, "region": "Kanto"},
                    {"name": "OLG 梅田", "women": 14, "men": 14, "region": "Kinki"},
                    {"name": "JIS SHINJUKU", "women": 12, "men": 12, "region": "Kanto"},
                ],
            ):
                main_txt, rep_txt = x_poster.build_x_search_catcher_post(branch_stores, slot_label=slot_lbl, now_jst=dt)
                self.assertLessEqual(x_poster.compute_twitter_weight(main_txt), 272)
                self.assertNotIn("#", main_txt)
                self.assertNotIn("http", main_txt)
                self.assertIsNone(BANNED_BRAND_RE.search(main_txt))
                self.assertIn(expected_tag, rep_txt)

        # M7 GO gate: confirm x20_search / x22_search / x23_search classify into channel 'x'
        import analytics as _an
        for raw_tag, expected_hr in (
            ("x20_search_20261121", "20:00"),
            ("x22_search_20261121", "22:00"),
            ("x23_search_20261121", "23:00"),
        ):
            norm = _an.normalize_inflow_source(raw_tag)
            ch, hr, st = pdca_engine._classify_sns_tag(norm)
            self.assertEqual(ch, "x")
            self.assertEqual(hr, expected_hr)
            self.assertEqual(st, "search")

    # ------------------------------------------------------------------
    # Pillar 4 & 5: /stats Warmup & Log-Count Unit Consistency
    # ------------------------------------------------------------------
    def test_pillar4_weekly_report_no_unbound_and_stats_200(self):
        rep = weekly_report.generate_weekly_report()
        self.assertIsInstance(rep, dict)
        r = self.client.get("/stats?staff_test=1")
        self.assertEqual(r.status_code, 200)

        # H-2 GO gate: verify Redis JSON round-trip deserialized report renders /stats with HTTP 200
        sample_redis_rep = json.loads(json.dumps({
            "period": "2026-11-12〜2026-11-19",
            "generated_at": "2026-11-20 08:00",
            "total_records": 1200,
            "stores": [
                {
                    "name": "OLG 新宿",
                    "seo_name": "オリエンタルラウンジ 新宿",
                    "slug": "oriental-lounge-shinjuku",
                    "area": "新宿",
                    "avg_women": 24.5,
                    "avg_men": 19.2,
                    "female_ratio": 56.1,
                    "max_women": 42,
                    "peak_hour": "22時台",
                }
            ],
            "daily_trend": [],
            "hourly_slots": [],
            "brand_comparison": [],
        }))
        old_cache = dict(weekly_report._report_cache)
        old_dbp = weekly_report._report_cache_db_path
        try:
            weekly_report._report_cache["data"] = None
            weekly_report._report_cache["last_fetched"] = None
            with mock.patch.dict(os.environ, {"DISABLE_SHEETS": "0", "TESTING": "0"}), \
                 mock.patch("turbo_store.get_ai_cache", return_value={"generated_at_epoch": 1763596800.0, "report": sample_redis_rep}):
                got = weekly_report.generate_weekly_report()
            self.assertEqual(got["total_records"], 1200)
            r_redis = self.client.get("/stats?staff_test=1")
            self.assertEqual(r_redis.status_code, 200)
        finally:
            weekly_report._report_cache.clear()
            weekly_report._report_cache.update(old_cache)
            weekly_report._report_cache_db_path = old_dbp

    def test_pillar5_public_pages_log_count_unit_consistency(self):
        for path in ("/columns", "/guide", "/about"):
            r = self.client.get(f"{path}?staff_test=1")
            self.assertEqual(r.status_code, 200)
            html = r.get_data(as_text=True)
            if path == "/about":
                self.assertIn("記録件数の表記単位について", html)
            if path == "/guide":
                self.assertNotIn("98万件の実測データ", html)


if __name__ == "__main__":
    unittest.main()
