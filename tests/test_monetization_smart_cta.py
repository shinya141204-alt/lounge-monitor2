# -*- coding: utf-8 -*-
"""
tests/test_monetization_smart_cta.py - 店舗詳細リアルタイム状況連動スマートCTA ＆ インプレッション/CTR計測テスト
"""

import copy
import datetime
import json
import unittest
from unittest.mock import patch

import affiliate_manager
import analytics
import app as app_module

JST = datetime.timezone(datetime.timedelta(hours=9))


class MonetizationSmartCtaTests(unittest.TestCase):
    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        with app_module.data_lock:
            self._saved_latest = copy.deepcopy(app_module.latest_data)
        analytics.reset_for_tests()
        affiliate_manager.reset_cache_for_tests()
        app_module.clear_fast_http_caches()

    def tearDown(self):
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        analytics.reset_for_tests()
        affiliate_manager.reset_cache_for_tests()
        app_module.clear_fast_http_caches()

    def test_build_store_smart_cta_four_modes(self):
        """4つの店舗状況（男性先行・営業時間外/少人数/ソロ制限・21時女性優位・23時深夜女性優位）で最適な枠へ出し分けられること"""
        nearby = [
            {"seo_name": "ag 渋谷", "slug": "ag-shibuya", "women": 18, "men": 10, "is_open_now": True},
        ]

        # 1. 男性先行 (men_glut)
        cta_glut = affiliate_manager.build_store_smart_cta(
            {
                "name": "OLG 渋谷本店",
                "seo_name": "オリエンタルラウンジ 渋谷本店",
                "slug": "oriental-shibuya",
                "women": 12,
                "men": 25,
                "is_open_now": True,
            },
            nearby_stores=nearby,
            area_slug="shibuya",
            now_jst=datetime.datetime(2026, 11, 6, 21, 15, tzinfo=JST),
        )
        self.assertEqual(cta_glut["mode"], "men_glut")
        self.assertEqual(cta_glut["slot_id"], "dating_instant")
        self.assertEqual(cta_glut["href"], "/link/dating_instant?from=/store/oriental-shibuya")
        self.assertEqual(cta_glut["sublink_href"], "/store/ag-shibuya")
        self.assertIn("ag 渋谷", cta_glut["sublink_label"])
        self.assertTrue(cta_glut["tracking_pixel"].startswith("https://www15.a8.net/0.gif"))

        # 2. 営業時間外・ソロ制限店 (off_or_low)
        cta_off = affiliate_manager.build_store_smart_cta(
            {
                "name": "OLG 恵比寿",
                "seo_name": "オリエンタルラウンジ 恵比寿",
                "slug": "oriental-ebisu",
                "women": 0,
                "men": 0,
                "is_open_now": False,
            },
            nearby_stores=[],
            area_slug="shibuya",
            now_jst=datetime.datetime(2026, 11, 6, 15, 0, tzinfo=JST),
        )
        self.assertEqual(cta_off["mode"], "off_or_low")
        self.assertEqual(cta_off["slot_id"], "dating_bachelor")
        self.assertTrue(cta_off["is_solo_restricted"])
        self.assertIn("2名以上", cta_off["sub"])
        self.assertEqual(cta_off["sublink_href"], "/guide/lounge-vs-screened-date")

        # 3. 20:30 女性優位・入店直前 (prime_grooming)
        cta_prime = affiliate_manager.build_store_smart_cta(
            {
                "name": "OLG 新宿",
                "seo_name": "オリエンタルラウンジ 新宿",
                "slug": "oriental-shinjuku",
                "women": 22,
                "men": 14,
                "is_open_now": True,
            },
            nearby_stores=[],
            area_slug="shinjuku",
            now_jst=datetime.datetime(2026, 11, 6, 20, 30, tzinfo=JST),
        )
        self.assertEqual(cta_prime["mode"], "prime_grooming")
        self.assertEqual(cta_prime["slot_id"], "mens_skincare_clinic")
        self.assertEqual(cta_prime["sublink_href"], "/grooming")

        # 4. 23:10 女性優位・深夜2軒目帯 (late_second_venue)
        cta_late = affiliate_manager.build_store_smart_cta(
            {
                "name": "OLG 新宿",
                "seo_name": "オリエンタルラウンジ 新宿",
                "slug": "oriental-shinjuku",
                "women": 24,
                "men": 18,
                "is_open_now": True,
            },
            nearby_stores=[],
            area_slug="shinjuku",
            now_jst=datetime.datetime(2026, 11, 6, 23, 10, tzinfo=JST),
        )
        self.assertEqual(cta_late["mode"], "late_second_venue")
        self.assertEqual(cta_late["slot_id"], "second_venue_reservation")
        self.assertEqual(cta_late["sublink_href"], "#store-second-venue-section")

    def test_impression_tracking_ctr_and_unmonetized_detection(self):
        """インプレッション計測の正規化・不正スロット拒否・CTR集計・タグなし直URL検知が正確に動作すること"""
        with patch.object(analytics.sheets_store, "read_rows", return_value=[]):
            # 不正・未知スロット名は拒否されること
            self.assertFalse(analytics.track("affiliate_impression", "unknown_spam_slot @ /"))
            # エイリアスや個別店舗パスは正規化されること
            self.assertTrue(analytics.track("affiliate_impression", "dating_instant @ /store/oriental-shibuya"))
            self.assertTrue(analytics.track("affiliate_impression", "dating @ /store/ag-shinjuku"))
            self.assertTrue(analytics.track("affiliate_impression", "women_dating @ /for-women"))
            self.assertTrue(analytics.track("affiliate_click", "dating_instant @ /store/oriental-shibuya"))

            data = analytics.summary(days=7)
            self.assertEqual(data["funnel"]["aff_impressions_total"], 3)
            self.assertEqual(data["funnel"]["aff_total"], 1)
            self.assertAlmostEqual(data["funnel"]["aff_slot_ctr_pct"], 33.33, places=2)

            ctr_map = {(r["slot_id"], r["surface"]): r for r in data["cta_surface_ctr"]}
            self.assertIn(("dating_instant", "/store/*"), ctr_map)
            st_row = ctr_map[("dating_instant", "/store/*")]
            self.assertEqual(st_row["impressions"], 2)
            self.assertEqual(st_row["clicks"], 1)
            self.assertEqual(st_row["ctr_pct"], 50.0)

            aff_st = affiliate_manager.get_affiliate_slots_status(analytics_data=data, allow_blocking=False)
            slots_by_id = {s["slot_id"]: s for s in aff_st["slots"]}
            self.assertEqual(slots_by_id["dating_instant"]["impressions"], 2)
            self.assertEqual(slots_by_id["dating_instant"]["clicks"], 1)
            self.assertEqual(slots_by_id["dating_instant"]["ctr_pct"], 50.0)
            self.assertFalse(slots_by_id["dating_instant"]["is_unmonetized_url"])
            self.assertTrue(slots_by_id["women_dating_bachelor"]["is_unmonetized_url"])
            self.assertTrue(slots_by_id["women_beauty_clinic"]["is_unmonetized_url"])
            self.assertGreaterEqual(aff_st["unmonetized_slot_count"], 2)

    def test_store_detail_renders_smart_cta_for_public_and_hides_for_staff(self):
        """店舗詳細ページで一般閲覧者には混雑連動スマートCTAとcta_tracker.jsが描画され、スタッフ時は非表示になること"""
        import store_master

        shibuya_slug = store_master.slug_of("OLG 渋谷本店")
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        with app_module.data_lock:
            app_module.latest_data["last_updated"] = now_str
            app_module.latest_data["full_data"] = [
                {"name": "OLG 渋谷本店", "women": 11, "men": 26, "total": 37},
                {"name": "OLG ag渋谷", "women": 19, "men": 12, "total": 31},
            ]
        app_module.clear_fast_http_caches()

        resp = self.client.get(f"/store/{shibuya_slug}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn('id="store-smart-cta-card"', html)
        self.assertIn('data-context-mode="men_glut"', html)
        self.assertIn('data-women-variant=', html)
        self.assertIn(f'/link/dating_instant?from=/store/{shibuya_slug}', html)
        self.assertIn('/static/cta_tracker.js?v=3', html)

        # /api/track でブラウザからのインプレッション送信が受理されること
        tr_resp = self.client.post(
            "/api/track",
            data=json.dumps({"event": "affiliate_impression", "label": f"dating_instant @ /store/{shibuya_slug}"}),
            content_type="application/json",
            headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"},
        )
        self.assertEqual(tr_resp.status_code, 204)

        # スタッフセッション時はスマートCTAもcta_tracker.jsも出力されないこと
        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True
            sess["auth_gen"] = app_module._compute_session_auth_gen(True)
        resp_staff = self.client.get(f"/store/{shibuya_slug}")
        self.assertEqual(resp_staff.status_code, 200)
        staff_html = resp_staff.get_data(as_text=True)
        self.assertNotIn('id="store-smart-cta-card"', staff_html)
        self.assertNotIn('/static/cta_tracker.js?v=3', staff_html)

    def test_women_variant_and_for_women_top_picks_auto_promotion(self):
        """未設定直URLの間は稼働中ASP枠が選ばれ、women_beauty_clinic にASPタグが登録された瞬間に自動昇格すること"""
        cta_default = affiliate_manager.build_store_smart_cta(
            {
                "name": "OLG 新宿",
                "seo_name": "オリエンタルラウンジ 新宿",
                "slug": "oriental-lounge-shinjuku",
                "women": 10,
                "men": 18,
                "is_open_now": True,
            },
            now_jst=datetime.datetime(2026, 11, 6, 20, 30, tzinfo=JST),
        )
        wv_def = cta_default["women_variant"]
        self.assertIn("/link/women_second_venue?from=/store/oriental-lounge-shinjuku", wv_def["href"])

        top_def = affiliate_manager.build_for_women_top_picks(allow_blocking=False)
        self.assertEqual(len(top_def), 2)
        self.assertIn("/link/women_second_venue?from=/for-women", top_def[0]["href"])
        self.assertIn("/link/women_cosme_salon?from=/for-women", top_def[1]["href"])

        # レジーナ/リゼ等のASPタグが設定された状態をシミュレート
        with patch.dict(affiliate_manager.os.environ, {"AFFILIATE_URL_WOMEN_BEAUTY_CLINIC": "https://px.a8.net/svt/ejp?a8mat=TEST+REGINA"}):
            affiliate_manager.reset_cache_for_tests()
            self.assertTrue(affiliate_manager.is_slot_monetized("women_beauty_clinic"))
            top_promoted = affiliate_manager.build_for_women_top_picks(allow_blocking=False)
            self.assertIn("/link/women_beauty_clinic?from=/for-women", top_promoted[1]["href"])

            cta_promoted = affiliate_manager.build_store_smart_cta(
                {
                    "name": "OLG 新宿",
                    "seo_name": "オリエンタルラウンジ 新宿",
                    "slug": "oriental-lounge-shinjuku",
                    "women": 10,
                    "men": 18,
                    "is_open_now": True,
                },
                now_jst=datetime.datetime(2026, 11, 6, 20, 30, tzinfo=JST),
            )
            self.assertIn("/link/women_beauty_clinic?from=/store/oriental-lounge-shinjuku", cta_promoted["women_variant"]["href"])

    def test_cushion_click_tracking_and_pdca_bridge_recognition(self):
        """2ステップ導線の cushion_click が厳密正規化され、自己アンカーや外部URLを拒否し、PDCAが8%超の記事を健全判定すること"""
        import pdca_engine

        with patch.object(analytics.sheets_store, "read_rows", return_value=[]), \
             patch.object(analytics.turbo_store, "analytics_add_batch", return_value=False):
            # 不正ターゲット・自己ページアンカー・外部パスは拒否
            self.assertFalse(analytics.track("cushion_click", "https://evil.example @ /guide/brand-comparison"))
            self.assertFalse(analytics.track("cushion_click", "/grooming#skin-beard @ /grooming"))
            self.assertFalse(analytics.track("cushion_click", "/for-women#clinic @ /for-women"))
            self.assertFalse(analytics.track("cushion_click", "/grooming @ //evil.example"))
            self.assertFalse(analytics.track("cushion_click", "/grooming @ /staff/pdca"))

            # 正規の2ステップ遷移はアンカー保持で記録される
            self.assertTrue(analytics.track("cushion_click", "/grooming#backup-apps @ /guide/brand-comparison"))
            self.assertTrue(analytics.track("cushion_click", "/for-women#live-board @ /guide/brand-comparison"))
            self.assertTrue(analytics.track("cushion_click", "/grooming#sebum-care @ /guide/low-pv-sample"))
            for _ in range(18):
                analytics.track("page_view", "/guide/brand-comparison")
            for _ in range(5):
                analytics.track("page_view", "/guide/low-pv-sample")

            data = analytics.summary(days=7)
            self.assertEqual(data["funnel"]["cushion_clicks_total"], 3)
            self.assertEqual(data["cushion_by_source"].get("/guide/brand-comparison"), 2)

            # 18 PV (>=8 PV) 中 2件のクッション遷移（11.1% >= 8.0%）があればギャップ除外、5 PV (<8 PV) は除外しない
            gaps = pdca_engine._detect_monetization_gaps(
                {"/guide/brand-comparison": 18, "/guide/weekend-strategy": 15, "/guide/low-pv-sample": 5},
                {},
                [
                    {"path": "/guide/brand-comparison", "title": "3大ブランド比較"},
                    {"path": "/guide/weekend-strategy", "title": "週末攻略"},
                    {"path": "/guide/low-pv-sample", "title": "少数PVサンプル"},
                ],
                cushion_by_ref_path=data["cushion_by_source"],
            )
            gap_paths = [g["path"] for g in gaps]
            self.assertNotIn("/guide/brand-comparison", gap_paths)
            self.assertIn("/guide/weekend-strategy", gap_paths)
            self.assertIn("/guide/low-pv-sample", gap_paths)

    def test_sitemap_lastmod_and_llms_facts_store_enrichment(self):
        """sitemap.xml の実テンプレート参照修正と llms-facts.json の店舗別営業時間・料金・ソロ規定の返却検証"""
        import importlib
        _seo = importlib.import_module("routes.seo_feeds_bp")

        smap_resp = self.client.get("/sitemap.xml")
        self.assertEqual(smap_resp.status_code, 200)
        smap_xml = smap_resp.get_data(as_text=True)
        lm_cmp = _seo._template_lastmod("guide_comparison.html")
        lm_pe = _seo._template_lastmod("guide_price_comparison.html")
        self.assertIn(f"<loc>https://aiseki-navi.com/guide/brand-comparison</loc>\n    <lastmod>{lm_cmp}</lastmod>", smap_xml)
        self.assertIn(f"<loc>https://aiseki-navi.com/guide/price-event-comparison</loc>\n    <lastmod>{lm_pe}</lastmod>", smap_xml)

        facts_resp = self.client.get("/api/llms-facts.json")
        self.assertEqual(facts_resp.status_code, 200)
        facts = facts_resp.get_json()
        self.assertTrue(len(facts.get("stores") or []) >= 50)
        st0 = facts["stores"][0]
        self.assertIn("business_hours", st0)
        self.assertIn("male_rate_20_22_per_10m", st0)
        self.assertIn("solo_entry_policy", st0)
        self.assertTrue(bool(st0["business_hours"]))

        # 純粋2ステップ記事に /link/ 直貼りが混入していないこと
        for pure_col in ("/guide/brand-comparison", "/guide/lounge-vs-screened-date", "/guide/dress-code-cleanliness", "/guide/weekend-strategy", "/guide/shinjuku", "/guide/last-train-cliff"):
            c_resp = self.client.get(pure_col)
            self.assertEqual(c_resp.status_code, 200)
            c_html = c_resp.get_data(as_text=True)
            self.assertIn("editorial-bridge-box", c_html)
            self.assertNotIn("/link/", c_html, f"Unexpected direct /link/ found in {pure_col}")

    def test_high_impact_three_moves_bridges_surfaces_and_zero_direct_guide_links(self):
        """高インパクト3手（①トップ→/store/<slug>橋＋SNS夜着地＋正直コピー ②高意図3面スマートCTA ③女性0円遮断＋全コラム直/link/ゼロ＋拡張クッション計測）の統合検証"""
        import glob
        import os

        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        with app_module.data_lock:
            app_module.latest_data["timestamp"] = now_str
            app_module.latest_data["last_updated"] = now_str
            app_module.latest_data["full_data"] = [
                {"name": "OLG 渋谷本店", "women": 11, "men": 26, "total": 37, "region": "Kanto"},
                {"name": "OLG 新宿", "women": 32, "men": 14, "total": 46, "region": "Kanto"},
            ]
        app_module.clear_fast_http_caches()

        # 【手1】トップ `/` に `/store/<slug>` 橋と `#modal-store-detail-link` があり、?s= 着地で hasSnsInflow: true になること
        home_def = self.client.get("/").get_data(as_text=True)
        self.assertIn('id="modal-store-detail-link"', home_def)
        self.assertIn('data-cushion="1"', home_def)
        self.assertIn("hasSnsInflow: false", home_def)
        self.assertIn("bachelorSharesInstant: true", home_def)
        self.assertNotIn("日程調整だけで1対1が確定する審査制", home_def)

        home_sns = self.client.get("/?s=x22_hub_gap", headers={"User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X)"}).get_data(as_text=True)
        self.assertIn("hasSnsInflow: true", home_sns)

        # 【手2】高意図面（/price-comparison, /stats, 4ブランドページ）に `#surface-smart-cta-card` が描画され、スタッフ時は非表示になること
        for surface_path in ("/price-comparison", "/stats", "/oriental-lounge", "/ag", "/jis", "/asobibar"):
            r_surf = self.client.get(surface_path)
            self.assertEqual(r_surf.status_code, 200, surface_path)
            s_html = r_surf.get_data(as_text=True)
            self.assertIn('id="surface-smart-cta-card"', s_html, surface_path)
            self.assertIn(f"?from={surface_path}", s_html, surface_path)
            self.assertIn('data-women-variant=', s_html, surface_path)

        surf_glut = affiliate_manager.build_surface_smart_cta(
            [
                {"name": "OLG 渋谷本店", "women": 8, "men": 25, "is_open_now": True},
                {"name": "JIS SHINJUKU", "women": 10, "men": 22, "is_open_now": True},
            ],
            source_path="/price-comparison",
            surface_label="料金比較対象",
            now_jst=datetime.datetime(2026, 11, 6, 21, 30, tzinfo=JST),
            allow_blocking=False,
        )
        self.assertEqual(surf_glut["mode"], "men_glut")
        self.assertEqual(surf_glut["href"], "/link/dating_instant?from=/price-comparison")

        surf_late = affiliate_manager.build_surface_smart_cta(
            [{"name": "OLG 新宿", "women": 30, "men": 14, "is_open_now": True}],
            source_path="/stats",
            surface_label="全国平均",
            now_jst=datetime.datetime(2026, 11, 6, 23, 45, tzinfo=JST),
            allow_blocking=False,
        )
        self.assertEqual(surf_late["mode"], "late_second_venue")
        self.assertEqual(surf_late["href"], "/link/second_venue_reservation?from=/stats")

        # スタッフログイン時は高意図面のスマートCTAが完全に非表示となること
        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = False
            sess["auth_gen"] = app_module._compute_session_auth_gen(False)
        for surface_path in ("/price-comparison", "/stats", "/oriental-lounge"):
            staff_s = self.client.get(surface_path).get_data(as_text=True)
            self.assertNotIn('id="surface-smart-cta-card"', staff_s, surface_path)
        with self.client.session_transaction() as sess:
            sess.clear()

        # 【手3】全 `templates/guide_*.html` に直 `/link/` が 0件であること ＆ 拡張 `cushion_click` 計測の検証
        guide_files = sorted(glob.glob(os.path.join(os.path.dirname(os.path.dirname(__file__)), "templates", "guide_*.html")))
        self.assertTrue(len(guide_files) >= 15)
        for gf in guide_files:
            with open(gf, "r", encoding="utf-8") as fp:
                g_txt = fp.read()
            self.assertNotIn("/link/", g_txt, f"Direct /link/ leaked in {os.path.basename(gf)}")

        with patch.object(analytics.sheets_store, "read_rows", return_value=[]), \
             patch.object(analytics.turbo_store, "analytics_add_batch", return_value=False):
            self.assertTrue(analytics.track("cushion_click", "/store/oriental-lounge-shinjuku @ /"))
            self.assertTrue(analytics.track("cushion_click", "/concierge @ /guide/male-lead-mechanics"))
            self.assertTrue(analytics.track("cushion_click", "/price-comparison @ /guide/price-event-comparison"))
            # 自己ページ遷移やパストラバーサルは厳格に拒否
            self.assertFalse(analytics.track("cushion_click", "/store/oriental-lounge-shinjuku @ /store/oriental-lounge-shinjuku"))
            self.assertFalse(analytics.track("cushion_click", "/store/../staff @ /"))
            self.assertEqual(analytics.normalize_cta_surface("/stats"), "/stats")


if __name__ == "__main__":
    unittest.main()
