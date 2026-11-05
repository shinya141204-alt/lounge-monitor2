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
        self.assertIn(f'/link/dating_instant?from=/store/{shibuya_slug}', html)
        self.assertIn('/static/cta_tracker.js?v=1', html)

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
        self.assertNotIn('/static/cta_tracker.js?v=1', staff_html)


if __name__ == "__main__":
    unittest.main()
