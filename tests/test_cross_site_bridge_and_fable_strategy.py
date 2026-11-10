# -*- coding: utf-8 -*-
"""Unit tests for Fable 5.2 Strategy Top 5 (Track A) and Cross-Site Architecture P0+P1 (Track B)."""

import os
import unittest

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

import affiliate_manager
import analytics
import app as app_mod
import cross_site_bridge
import pdca_engine


class TestCrossSiteBridgeAndFableStrategy(unittest.TestCase):
    def setUp(self):
        self.client = app_mod.app.test_client()
        cross_site_bridge.clear_caches()

    def test_analytics_co_view_and_sister_bridge_normalization(self):
        self.assertIn("co_view", analytics.ALLOWED_EVENTS)
        self.assertIn("sister_bridge_impression", analytics.ALLOWED_EVENTS)
        self.assertIn("sister_bridge_click", analytics.ALLOWED_EVENTS)

        valid_co = analytics.normalize_co_view_label(
            "oriental-lounge-shinjuku|jis-shinjuku"
        )
        self.assertEqual(valid_co, "OLG 新宿|JIS SHINJUKU")
        self.assertEqual(
            analytics.normalize_co_view_label(
                "oriental-lounge-shinjuku|oriental-lounge-shinjuku"
            ),
            "",
        )
        self.assertEqual(analytics.normalize_co_view_label("unknown-a|unknown-b"), "")

        valid_sb = analytics.normalize_sister_bridge_label("shinjuku:men_glut:men")
        self.assertEqual(valid_sb, "shinjuku:men_glut:men")
        self.assertEqual(
            analytics.normalize_sister_bridge_label("shinjuku:bad:men"),
            "shinjuku:other:men",
        )

    def test_women_offer_category_and_smart_cta_internal_href(self):
        women_cat = next(
            c for c in pdca_engine.OFFER_CATEGORIES if c.get("category_key") == "women"
        )
        self.assertEqual(
            women_cat["primary_link"],
            "/link/women_second_venue",
        )
        cta = affiliate_manager.build_store_smart_cta(
            {"slug": "oriental-lounge-shinjuku", "name": "オリエンタルラウンジ 新宿"},
            {"men": 25, "women": 18, "total": 43},
        )
        self.assertIn("primary_internal_href", cta)
        self.assertTrue(str(cta["primary_internal_href"]).startswith("/"))
        self.assertTrue(bool(cta.get("primary_internal_label")))

    def test_exchange_area_night_feed_and_weekend_index_routes(self):
        r_ex = self.client.get("/api/exchange/area-night.v1.json?staff_test=1")
        self.assertEqual(r_ex.status_code, 200)
        ex_data = r_ex.get_json()
        self.assertEqual(ex_data["schema_version"], "1.0")
        self.assertEqual(ex_data["feed"], "aiseki_navi.area_night")
        self.assertEqual(len(ex_data["areas"]), 10)
        self.assertNotIn("/link/", r_ex.get_data(as_text=True))

        r_di_html = self.client.get("/stats/weekend-index?staff_test=1")
        self.assertEqual(r_di_html.status_code, 200)
        di_text = r_di_html.get_data(as_text=True)
        self.assertIn("application/ld+json", di_text)
        self.assertIn("https://machinavi-data.com/data/exchange/area-evening.v1.json", di_text)
        self.assertNotIn("/link/", di_text)

        r_di_json = self.client.get("/stats/weekend-index.json?staff_test=1")
        self.assertEqual(r_di_json.status_code, 200)
        di_json = r_di_json.get_json()
        self.assertEqual(di_json["schema_version"], "1.0")
        self.assertNotIn("/link/", r_di_json.get_data(as_text=True))

        r_di_csv = self.client.get("/stats/weekend-index.csv?staff_test=1")
        self.assertEqual(r_di_csv.status_code, 200)
        csv_text = r_di_csv.get_data(as_text=True)
        self.assertIn("area_id,area_label", csv_text)
        self.assertNotIn("/link/", csv_text)

    def test_monthly_snapshot_and_cushion_adsense_isolation(self):
        r_m = self.client.get("/stats/monthly/2026-11?staff_test=1")
        self.assertEqual(r_m.status_code, 200)
        m_text = r_m.get_data(as_text=True)
        self.assertIn("2026年11月", m_text)
        self.assertNotIn("/link/", m_text)

        r_bad = self.client.get("/stats/monthly/2020-13?staff_test=1")
        self.assertEqual(r_bad.status_code, 404)

        for path in ("/grooming", "/for-women"):
            resp = self.client.get(f"{path}?staff_test=1")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_data(as_text=True)
            self.assertNotIn("adsbygoogle.js", body, path)

        r_pc = self.client.get("/price-comparison?staff_test=1")
        self.assertEqual(r_pc.status_code, 200)
        pc_body = r_pc.get_data(as_text=True)
        self.assertIn("/concierge", pc_body)
        self.assertIn("https://machinavi-data.com/?from=aiseki#diagnose", pc_body)


if __name__ == "__main__":
    unittest.main()
