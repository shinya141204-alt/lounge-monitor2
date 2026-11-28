import datetime
import importlib.util
import json
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import affiliate_manager
import analytics
import app as app_module
import concierge_matcher
import cross_site_bridge
import local_db
import pdca_engine
import turbo_store


def _load_daily_pv_report_module():
    path = REPO_ROOT / ".agents" / "skills" / "growth-pdca" / "scripts" / "daily_pv_report.py"
    spec = importlib.util.spec_from_file_location("daily_pv_report_audit", str(path))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class SwarmFullSiteAuditWave2Tests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        app_module.app.config["TESTING"] = True
        cls.client = app_module.app.test_client()

    def setUp(self):
        analytics.reset_for_tests()
        app_module.clear_fast_http_caches()
        cross_site_bridge.clear_caches()
        turbo_store.clear_rate_limit("aff_clk_ip")
        turbo_store.clear_rate_limit("aff_clk_human")

    def tearDown(self):
        analytics.reset_for_tests()
        app_module.clear_fast_http_caches()
        cross_site_bridge.clear_caches()
        app_module.app.config["ENFORCE_AFFILIATE_HUMAN_PROOF"] = None

    def test_seo_feeds_cache_headers_and_llmo_endpoints(self):
        """robots.txt / ads.txt / sitemap.xml に Cache-Control が付き、LLMOフィードに新統計URLが網羅されること"""
        r_rob = self.client.get("/robots.txt")
        self.assertEqual(r_rob.status_code, 200)
        self.assertIn("max-age=", r_rob.headers.get("Cache-Control", ""))
        self.assertIn("Disallow: /link/", r_rob.get_data(as_text=True))

        r_ads = self.client.get("/ads.txt")
        self.assertEqual(r_ads.status_code, 200)
        self.assertIn("max-age=", r_ads.headers.get("Cache-Control", ""))

        r_smap = self.client.get("/sitemap.xml")
        self.assertEqual(r_smap.status_code, 200)
        self.assertIn("max-age=", r_smap.headers.get("Cache-Control", ""))
        smap_txt = r_smap.get_data(as_text=True)
        self.assertIn("https://aiseki-navi.com/stats/weekend-index", smap_txt)
        for ym in cross_site_bridge.list_measured_monthly_snapshots(limit=12):
            self.assertIn(f"https://aiseki-navi.com/stats/monthly/{ym}", smap_txt)

        llms_txt = self.client.get("/llms.txt").get_data(as_text=True)
        llms_full = self.client.get("/llms-full.txt").get_data(as_text=True)
        facts = json.loads(self.client.get("/api/llms-facts.json").get_data(as_text=True))
        for doc in (llms_txt, llms_full):
            self.assertIn("https://aiseki-navi.com/stats/weekend-index", doc)
            self.assertIn("https://aiseki-navi.com/api/exchange/area-night.v1.json", doc)
            self.assertIn("XIX（ジックス）・ALFA（アルファ）・CLOVERS（クローバーズ）", doc)
            self.assertNotIn("3秒", doc)
        cur_ym = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m")
        if f"/stats/monthly/{cur_ym}" in llms_txt:
            self.assertIn("集計中（速報）", llms_txt)
        od = facts.get("open_datasets") or {}
        self.assertEqual(od.get("weekend_index_url"), "https://aiseki-navi.com/stats/weekend-index")
        self.assertEqual(od.get("weekend_index_json_url"), "https://aiseki-navi.com/stats/weekend-index.json")
        self.assertEqual(od.get("area_night_exchange_url"), "https://aiseki-navi.com/api/exchange/area-night.v1.json")

        with mock.patch.object(local_db, "_fetch_all_rows", return_value=[(datetime.date(2026, 11, 1), 12), ("2026-10", 5)]) as m_rows:
            cross_site_bridge.clear_caches()
            yms = cross_site_bridge.list_measured_monthly_snapshots(limit=12)
            self.assertIn("2026-11", yms)
            sql_called, params_called = m_rows.call_args.args
            self.assertIn("LIKE ?", sql_called)
            self.assertEqual(params_called, ("%ソウル%",))

        # M-2: DBエラー・0件時は短時間負キャッシュ（45秒）が効き、45秒以内は再クエリせず、45秒超過後に再クエリされること
        clock = [1000.0]
        with mock.patch.object(cross_site_bridge.time, "monotonic", side_effect=lambda: clock[0]):
            cross_site_bridge.clear_caches()
            with mock.patch.object(local_db, "_fetch_all_rows", side_effect=RuntimeError("pg timeout")) as m_fail:
                self.assertEqual(cross_site_bridge.list_measured_monthly_snapshots(limit=12), [])
                clock[0] = 1044.0
                self.assertEqual(cross_site_bridge.list_measured_monthly_snapshots(limit=12), [])
                self.assertEqual(m_fail.call_count, 1)
                clock[0] = 1046.0
                self.assertEqual(cross_site_bridge.list_measured_monthly_snapshots(limit=12), [])
                self.assertEqual(m_fail.call_count, 2)

            cross_site_bridge.clear_caches()
            clock[0] = 2000.0
            db_returns = [[], [("2026-11", 3)]]
            with mock.patch.object(local_db, "_fetch_all_rows", side_effect=lambda *_a, **_kw: db_returns.pop(0)) as m_empty:
                self.assertEqual(cross_site_bridge.list_measured_monthly_snapshots(limit=12), [])
                clock[0] = 2044.0
                self.assertEqual(cross_site_bridge.list_measured_monthly_snapshots(limit=12), [])
                self.assertEqual(m_empty.call_count, 1)
                clock[0] = 2046.0
                self.assertEqual(cross_site_bridge.list_measured_monthly_snapshots(limit=12), ["2026-11"])
                self.assertEqual(m_empty.call_count, 2)

        r_monthly = self.client.get(f"/stats/monthly/{cur_ym}")
        if r_monthly.status_code == 200:
            import re as _re
            m_html = r_monthly.get_data(as_text=True)
            ld_match = _re.search(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', m_html, _re.DOTALL)
            self.assertIsNotNone(ld_match)
            ld_items = json.loads(ld_match.group(1))
            dataset_items = [x for x in ld_items if isinstance(x, dict) and x.get("@type") == "Dataset"]
            self.assertEqual(len(dataset_items), 1)
            self.assertNotIn("distribution", dataset_items[0])

    def test_no_3sec_copy_and_analytics_page_titles_complete(self):
        """バックエンド診断ラベル・スマートCTAから『3秒』が除去され、統計サブページのタイトル定義が揃っていること"""
        for path in ("/stats/weekend-index", "/stats/year-end-2026", "/embed/heatmap", "/about", "/contact"):
            self.assertIn(path, analytics._PAGE_TITLES)
            self.assertNotIn("3秒", analytics._PAGE_TITLES[path])
        self.assertIn("月次一次統計アーカイブ", analytics.describe_label("page_view", "/stats/monthly/2026-11")["title"])
        self.assertEqual(analytics.describe_label("page_view", "/stats/monthly/2026-11<script>")["href"], "/stats")
        self.assertNotIn("3秒", analytics._CTA_SURFACE_LABELS["/concierge"])
        self.assertNotIn("3秒", analytics._HOURLY_PV_SURFACE_LABELS["pv_concierge"])

        flat_stores = [
            {"name": "ASOBIBAR SHINJUKU", "women": 8, "men": 22, "region": "Kanto"},
            {"name": "ASOBIBAR SHIBUYA", "women": 10, "men": 20, "region": "Kanto"},
        ]
        cta_flat = affiliate_manager.build_surface_smart_cta(
            flat_stores, source_path="/asobibar", surface_label="ASOBIBAR（アソビバー）全店", allow_blocking=False
        )
        self.assertNotIn("10分330円", cta_flat["title"])
        self.assertNotIn("3秒", cta_flat["sublink_label"])

        evening_stores = [{"name": "OLG 新宿", "women": 25, "men": 12, "region": "Kanto"}]
        cta_even = affiliate_manager.build_surface_smart_cta(
            evening_stores,
            source_path="/oriental-lounge",
            surface_label="オリエンタルラウンジ全店",
            now_jst=datetime.datetime(2026, 11, 28, 19, 30, tzinfo=datetime.timezone(datetime.timedelta(hours=9))),
            allow_blocking=False,
        )
        self.assertNotIn("会話継続率が変わります", cta_even["sub"])

        res_c = concierge_matcher.diagnose_tonight_plan(evening_stores, area="shinjuku", audience="men")
        sac = res_c.get("smart_action_card") or {}
        self.assertNotIn("会話継続率が変わります", sac.get("body", ""))
        self.assertNotIn("/link/orbis", sac.get("primary_url", ""))

    def test_affiliate_click_canonicalizes_legacy_ids_and_enforces_signed_hp(self):
        """レガシー/link/IDクリックが正規スロットIDへ統合され、未署名 an_hp=1 のみではバイパスできないこと"""
        human_ua = "Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15"
        app_module.app.config["ENFORCE_AFFILIATE_HUMAN_PROOF"] = True

        # 1) Unsigned raw an_hp=1 cookie MUST NOT bypass the interstitial
        self.client.set_cookie("an_hp", "1")
        r_forged = self.client.get("/link/orbis?from=/grooming", headers={"User-Agent": human_ua})
        self.assertEqual(r_forged.status_code, 200)
        self.assertIn("csrf_clk", r_forged.get_data(as_text=True))

        # 2) Valid signed an_hp_sig bypasses directly and logs canonical slot ID
        sig = app_module.issue_human_proof_cookie(str(app_module.app.secret_key))
        self.client.set_cookie("an_hp_sig", sig)
        r_ok = self.client.get("/link/orbis?from=/grooming", headers={"User-Agent": human_ua})
        self.assertEqual(r_ok.status_code, 302)
        r_hr = self.client.get("/link/hair_removal?from=/grooming", headers={"User-Agent": human_ua})
        self.assertEqual(r_hr.status_code, 302)

        snap = analytics.summary(days=7, allow_blocking=False)
        clicks = snap.get("all_by_label", {}).get("affiliate_click", {})
        self.assertEqual(clicks.get("mens_skincare_clinic @ /grooming"), 1)
        self.assertNotIn("orbis @ /grooming", clicks)
        self.assertEqual(clicks.get("mens_hair_removal @ /grooming"), 1)
        self.assertNotIn("hair_removal @ /grooming", clicks)

    def test_redirect_to_custom_domain_strips_control_chars(self):
        """onrender/www リダイレクト時にクエリ文字列から制御文字を除去すること"""
        r = self.client.get("/?s=x20_test%0d%0a", headers={"Host": "www.aiseki-navi.com"})
        self.assertEqual(r.status_code, 301)
        loc = r.headers.get("Location", "")
        self.assertTrue(loc.startswith("https://aiseki-navi.com/"))
        self.assertNotIn("\r", loc)
        self.assertNotIn("\n", loc)

    def test_daily_pv_report_human_pv_and_sample_gate(self):
        """daily_pv_report が human_pv 基準列を含み、click>0 でもPR表示300未満では判定保留を返し、パス正規化＆span平均を行うこと"""
        dpr = _load_daily_pv_report_module()
        today = datetime.date.today().strftime("%Y-%m-%d")
        yesterday = (datetime.date.today() - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        fake_rows = [
            [yesterday, "page_view", "/", "80"],
            [today, "page_view", "/", "120"],
            [today, "human_pv", "/", "40"],
            [today, "page_view", "/grooming/?s=x20", "30"],
            [today, "human_pv", "/grooming", "12"],
            [today, "affiliate_impression", "mens_skincare_clinic @ /grooming", "25"],
            [today, "affiliate_click", "mens_skincare_clinic @ /grooming", "1"],
        ]
        with mock.patch("sheets_store.read_rows", return_value=fake_rows), \
             mock.patch("sheets_store.read_hourly_rows", return_value=[]):
            rep = dpr.build_daily_report(today)
        self.assertEqual(rep["current"]["human_pv"], 52)
        self.assertEqual(rep["window_7d"]["human_pv_span_days"], 1)
        self.assertEqual(rep["window_7d"]["human_pv_avg_daily"], 52.0)
        self.assertEqual(rep["top_pages_day"][0]["human_pv"], 40)
        self.assertEqual(rep["top_subpages_7d"][0]["path"], "/grooming")
        self.assertEqual(rep["top_subpages_7d"][0]["human_pv_7d"], 12)
        self.assertEqual(len(rep["aff_breakdown"]), 1)
        self.assertFalse(rep["aff_breakdown"][0]["sample_sufficient"])
        self.assertIn("判定保留", rep["aff_breakdown"][0]["verdict"])
        md = dpr.format_markdown(rep)
        self.assertIn("human_pv", md)
        self.assertIn("判定保留", md)
        self.assertIn("1日実測平均", md)

    def test_pdca_engine_canonical_slot_classification_and_dedup(self):
        """pdca_engine が正規スロットID（mens_skincare_clinic等）を正しくカテゴリ突合し二重計上しないこと"""
        self.assertEqual(pdca_engine._classify_offer_category("mens_skincare_clinic"), "orbis")
        self.assertEqual(pdca_engine._classify_offer_category("mens_hair_removal"), "hair_removal")
        self.assertEqual(pdca_engine._classify_offer_category("spu_styleup"), "spu")
        self.assertEqual(pdca_engine._classify_offer_category("women_hair_removal"), "women")
        fake_summary = {
            "all_by_label": {"affiliate_click": {"mens_skincare_clinic @ /grooming": 2}},
            "by_event": {"aff_click:orbis": 2},
        }
        cat_clicks, _ = pdca_engine._extract_aff_category_stats(fake_summary)
        self.assertEqual(cat_clicks.get("orbis"), 2)

        # M-1: 旧新 aff_click:<id> が14日窓で混在しても過少計上・cat_refs 過大計上の双方を起こさないこと
        mixed_summary = {
            "all_by_label": {
                "affiliate_click": {"mens_skincare_clinic @ /grooming": 2},
                "aff_click:orbis": {"/concierge": 1},
                "aff_click:mens_skincare_clinic": {"/grooming": 2},
            },
            "by_event": {
                "aff_click:orbis": 1,
                "aff_click:mens_skincare_clinic": 2,
            },
        }
        cat_mixed, refs_mixed = pdca_engine._extract_aff_category_stats(mixed_summary)
        self.assertEqual(cat_mixed.get("orbis"), 3)
        self.assertEqual(dict(refs_mixed.get("orbis") or {}), {"/grooming": 2, "/concierge": 1})
        self.assertLessEqual(sum((refs_mixed.get("orbis") or {}).values()), cat_mixed.get("orbis", 0))

        # Verifier 例: orbis @ /grooming: 2 + aff_click:mens_skincare_clinic(/grooming: 3) -> delta=1 のとき /grooming=3（5へ過大計上しない）
        delta_cap_summary = {
            "all_by_label": {
                "affiliate_click": {"orbis @ /grooming": 2},
                "aff_click:mens_skincare_clinic": {"/grooming": 3},
            },
            "by_event": {"aff_click:mens_skincare_clinic": 3},
        }
        cat_cap, refs_cap = pdca_engine._extract_aff_category_stats(delta_cap_summary)
        self.assertEqual(cat_cap.get("orbis"), 3)
        self.assertEqual(dict(refs_cap.get("orbis") or {}), {"/grooming": 3})

        # M-4: surplus_total > delta の最大剰余法按分分岐（増分計が厳密に delta=2 に収まり総計が cat_clicks を超えないこと）
        prop_summary = {
            "all_by_label": {
                "affiliate_click": {"mens_skincare_clinic @ /a": 2},
                "aff_click:mens_skincare_clinic": {"/b": 3, "/c": 3},
            },
            "by_event": {"aff_click:mens_skincare_clinic": 4},
        }
        cat_prop, refs_prop = pdca_engine._extract_aff_category_stats(prop_summary)
        self.assertEqual(cat_prop.get("orbis"), 4)
        orbis_refs = dict(refs_prop.get("orbis") or {})
        self.assertEqual(orbis_refs.get("/a"), 2)
        self.assertEqual(orbis_refs.get("/b"), 1)
        self.assertEqual(orbis_refs.get("/c"), 1)
        self.assertEqual(sum(orbis_refs.values()), 4)
        self.assertLessEqual(sum(orbis_refs.values()), cat_prop.get("orbis", 0))

    def test_prewarm_and_chunked_queries_no_duplicate_stats(self):
        """prewarm の /stats 重複解消と get_store_history / turbo_store チャンク読込・境界上限が正常動作すること"""
        import inspect
        pw_src = inspect.getsource(app_module.prewarm_ssr_and_status_caches)
        self.assertIn("'/llms.txt'", pw_src)
        self.assertIn("'/api/llms-facts.json'", pw_src)
        self.assertIn("aiseki-internal-prewarm-bot/1.0", pw_src)
        self.assertEqual(pw_src.count("'/stats'"), 1)

        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        with mock.patch.object(local_db, "_iter_ts_window_in_7d_chunks", return_value=[[("2026-11-28 21:00:00", "OLG 新宿", 10, 15)]]) as m_chunk:
            rows = local_db.get_store_history("OLG 新宿", days=30)
            self.assertEqual(len(rows), 1)
            self.assertEqual(rows[0]["women"], 15)
            m_chunk.assert_called_once()
            self.assertTrue(m_chunk.call_args.kwargs.get("half_open"))

            rows_bad = local_db.get_store_history("OLG 新宿", days="invalid")
            self.assertEqual(len(rows_bad), 1)
            bad_dt = datetime.datetime.strptime(m_chunk.call_args.args[1], "%Y-%m-%d %H:%M:%S")
            self.assertTrue(29 <= (now_jst - bad_dt).days <= 30)

            local_db.get_store_history("OLG 新宿", days=9999)
            start_dt = datetime.datetime.strptime(m_chunk.call_args.args[1], "%Y-%m-%d %H:%M:%S")
            self.assertTrue(89 <= (now_jst - start_dt).days <= 90)

        chunks = list(turbo_store._iter_date_chunks_since((datetime.date.today() - datetime.timedelta(days=25)).isoformat(), chunk_days=14))
        self.assertGreaterEqual(len(chunks), 2)


if __name__ == "__main__":
    unittest.main()
