# -*- coding: utf-8 -*-
"""
tests/test_second_venues.py - 2軒目案内（2nd Party Venue Guide）単体テスト
"""

import json
import os
import unittest
import second_venues


class TestSecondVenues(unittest.TestCase):
    def setUp(self):
        self.master = second_venues.load_second_venues_master(force_reload=True)

    def test_master_json_structure(self):
        """static/second_venues.json の基本構造と主要11エリアの存在を検証"""
        self.assertIn("venues_by_area", self.master)
        self.assertIn("situation_tips", self.master)
        self.assertIn("timing_rules", self.master)

        areas = self.master["venues_by_area"]
        expected_areas = [
            "shibuya", "shinjuku", "ebisu", "ginza", "yokohama",
            "namba", "shinsaibashi", "umeda", "sakae", "tenjin", "susukino"
        ]
        for ea in expected_areas:
            self.assertIn(ea, areas, f"Area '{ea}' should exist in venues_by_area")
            spots = areas[ea]
            self.assertGreaterEqual(len(spots), 3, f"Area '{ea}' should have at least 3 spots")

    def test_spot_fields_integrity(self):
        """各スポットに必須フィールド（id, name, category, walk_minutes, budget, features, map_query, comment）が含まれるか検証"""
        required_fields = ["id", "name", "category", "category_label", "walk_minutes", "budget", "features", "map_query", "comment"]
        valid_categories = {"cafe", "bar", "shisha", "yokocho"}

        for area, spots in self.master["venues_by_area"].items():
            for sp in spots:
                for rf in required_fields:
                    self.assertIn(rf, sp, f"Field '{rf}' missing in spot {sp.get('id', 'unknown')}")
                self.assertIn(sp["category"], valid_categories, f"Invalid category '{sp['category']}' in {sp['id']}")
                self.assertIsInstance(sp["features"], list)
                self.assertGreaterEqual(len(sp["features"]), 1)

    def test_situation_tips_and_timing_rules(self):
        """シチュエーションTipsとタイミングルールの内容検証"""
        tips = self.master["situation_tips"]
        self.assertGreaterEqual(len(tips), 4)
        for tip in tips:
            self.assertIn("id", tip)
            self.assertIn("situation", tip)
            self.assertIn("tag", tip)
            self.assertIn("phrase", tip)
            self.assertIn("point", tip)

        rules = self.master["timing_rules"]
        self.assertGreaterEqual(len(rules), 3)
        for r in rules:
            self.assertIn("rule", r)
            self.assertIn("desc", r)

    def test_get_area_key_for_store(self):
        """主要店舗名から正しいエリアキーが判定されるか検証"""
        cases = [
            ("OLG 渋谷本店", "shibuya"),
            ("ag渋谷", "shibuya"),
            ("OLG 恵比寿", "ebisu"),
            ("OLG 新宿", "shinjuku"),
            ("OLG 銀座", "ginza"),
            ("OLG 横浜", "yokohama"),
            ("OLG 難波", "namba"),
            ("OLG 心斎橋", "shinsaibashi"),
            ("OLG 梅田", "umeda"),
            ("OLG 栄", "sakae"),
            ("OLG 福岡天神", "tenjin"),
            ("OLG 札幌", "susukino"),
        ]
        for store_name, expected_area in cases:
            area_key = second_venues.get_area_key_for_store(store_name)
            self.assertEqual(area_key, expected_area, f"Failed area key for '{store_name}'")

    def test_get_second_venues_for_registered_store(self):
        """登録済みエリア店舗（例: OLG 渋谷本店）でのスポット取得・URL生成検証"""
        res = second_venues.get_second_venues_for_store("OLG 渋谷本店")
        self.assertFalse(res["is_fallback"])
        self.assertEqual(res["area_key"], "shibuya")
        self.assertGreaterEqual(len(res["spots"]), 4)
        for sp in res["spots"]:
            self.assertIn("https://www.google.com/maps/search/?api=1&query=", sp["map_url"])
            self.assertIn("https://line.me/R/msg/text/?", sp["line_share_url"])

    def test_get_second_venues_for_unregistered_store_fallback(self):
        """未登録地方店舗（例: OLG 熊本、OLG 仙台）でスマートフォールバックが正常機能するか検証"""
        res = second_venues.get_second_venues_for_store("OLG 熊本")
        self.assertTrue(res["is_fallback"])
        self.assertEqual(len(res["spots"]), 4)
        categories = [sp["category"] for sp in res["spots"]]
        self.assertEqual(categories, ["cafe", "bar", "shisha", "yokocho"])
        for sp in res["spots"]:
            self.assertTrue(sp.get("is_dynamic"))
            self.assertIn("https://www.google.com/maps/search/?api=1&query=", sp["map_url"])
            self.assertIn("熊本", sp["name"])

    def test_brand_rule_compliance_in_master_and_code(self):
        """公開文言において単体の『オリエンタル』『OLG』『オリラジ』が存在しないことを検証"""
        raw_json_path = os.path.join(second_venues.BASE_DIR, "static", "second_venues.json")
        with open(raw_json_path, "r", encoding="utf-8") as f:
            raw_text = f.read()

        clean = raw_text.replace("オリエンタルラウンジ", "")
        self.assertNotIn("オリエンタル", clean, "Found forbidden single 'オリエンタル' in second_venues.json")
        self.assertNotIn("OLG", clean, "Found forbidden 'OLG' in second_venues.json")
        self.assertNotIn("オリラジ", clean, "Found forbidden 'オリラジ' in second_venues.json")

    def test_no_gcp_paid_api_usage(self):
        """Google Cloud 有料API（Places API, Maps JavaScript SDK等）のエンドポイントが存在しないことを検証"""
        self.assertNotIn("maps.googleapis.com/maps/api/place", second_venues.get_google_maps_intent_url("test"))
        self.assertNotIn("maps.googleapis.com", second_venues.get_google_maps_intent_url("test"))
        self.assertTrue(second_venues.get_google_maps_intent_url("test").startswith("https://www.google.com/maps/search/?api=1&query="))


if __name__ == "__main__":
    unittest.main()
