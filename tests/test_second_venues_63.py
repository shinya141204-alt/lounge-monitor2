"""
tests/test_second_venues_63.py
------------------------------------------------------------------------
マイルストーンM2（要件R3）全63店舗 実在2軒目ガイド＆UI統合の網羅的単体テスト

検証項目:
1. 全63店舗（国内61店舗＋ソウル2店舗）の完全網羅（is_fallback=False, is_store_curated=True, len(spots)>=2）
2. 全スポットの必須フィールド（徒歩5分圏内・同ビル0分含む、深夜営業、座席雰囲気等）の完全性
3. GCP有料API（Places API等）の完全不使用（Google公式無料WebインテントURL）
4. ブランド表記規約（単体『OLG』『オリエンタル』『オリラジ』禁止）の厳格遵守
5. 直 /link/ 混入防止ガード（コラム/stats混入ゼロ、store詳細の2ステップ導線）
6. コンシェルジュ診断（Seoul含む）の連動（primary_second_venues包含検証）
7. 防御的境界値テスト（store_name=None, 空文字等での非クラッシュ検証）
"""

import os
import re
import unittest
import json
import urllib.parse

import store_master
import second_venues
import concierge_matcher


class TestSecondVenues63(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        # アクティブ全63店舗のスラッグ一覧を収集
        cls.active_slugs = [
            slug for sname, slug in store_master.STORE_SLUGS.items()
            if slug not in store_master.LEGACY_SLUG_REDIRECTS
        ]
        # 重複除外
        cls.active_slugs = sorted(list(set(cls.active_slugs)))

    def test_active_stores_count_is_63(self):
        """アクティブ店舗数が厳密に63店舗（国内61＋ソウル2）であることを検証"""
        self.assertEqual(len(self.active_slugs), 63, f"Expected 63 active stores, got {len(self.active_slugs)}")

    def test_all_63_stores_have_curated_second_venues(self):
        """全63店舗それぞれで店舗固有の実在2軒目スポットが2件以上取得できることを検証"""
        for slug in self.active_slugs:
            with self.subTest(slug=slug):
                res = second_venues.get_second_venues_for_store(slug)
                self.assertFalse(res["is_fallback"], f"Store {slug} fell back to generic search")
                self.assertTrue(res["is_store_curated"], f"Store {slug} is not marked as store curated")
                spots = res.get("spots", [])
                self.assertGreaterEqual(
                    len(spots), 2, f"Store {slug} has less than 2 spots ({len(spots)})"
                )

    def test_spot_attributes_completeness_and_realism(self):
        """全スポットが実在情報に基づき、徒歩5分圏内・深夜営業等の要件を満たしているか検証"""
        required_fields = [
            "id", "name", "category", "category_label", "walk_minutes",
            "budget", "features", "map_query", "comment", "map_url",
            "reservation_url", "line_share_url"
        ]

        total_spot_count = 0
        for slug in self.active_slugs:
            res = second_venues.get_second_venues_for_store(slug)
            for sp in res["spots"]:
                total_spot_count += 1
                for field in required_fields:
                    val = sp.get(field)
                    self.assertIsNotNone(val, f"Field '{field}' is None in spot {sp.get('name')} (store: {slug})")
                    self.assertNotEqual(val, "", f"Field '{field}' is empty in spot {sp.get('name')} (store: {slug})")

                # 徒歩5分圏内（同ビル内0分も許容）
                self.assertLessEqual(
                    sp["walk_minutes"], 5,
                    f"Spot {sp['name']} walk_minutes exceeds 5 ({sp['walk_minutes']})"
                )
                self.assertGreaterEqual(sp["walk_minutes"], 0)

                # 特徴タグが1個以上
                self.assertIsInstance(sp["features"], list)
                self.assertGreaterEqual(len(sp["features"]), 1)

                # Google公式無料WebインテントURL
                self.assertTrue(
                    sp["map_url"].startswith("https://www.google.com/maps/search/?api=1&query="),
                    f"Invalid map_url in {sp['name']}: {sp['map_url']}"
                )
                self.assertNotIn("maps.googleapis.com", sp["map_url"])
                self.assertNotIn("key=", sp["map_url"].lower())

                # 予約URL
                expected_from = f"/store/{slug}"
                self.assertIn(
                    f"from={expected_from}", sp["reservation_url"],
                    f"Invalid reservation_url for {slug}: {sp['reservation_url']}"
                )

                # LINE共有URL
                self.assertTrue(
                    sp["line_share_url"].startswith("https://line.me/R/msg/text/?"),
                    f"Invalid line_share_url for {slug}: {sp['line_share_url']}"
                )

        # 63店舗 × 2件以上 = 126件以上
        self.assertGreaterEqual(total_spot_count, 126)

    def test_brand_rule_strict_compliance(self):
        """static/second_venues.json 内に単体の『OLG』『オリエンタル』『オリラジ』が存在しないことを検証"""
        json_path = os.path.join(second_venues.BASE_DIR, "static", "second_venues.json")
        with open(json_path, "r", encoding="utf-8") as f:
            content = f.read()

        re_isolated_olg = re.compile(r"\bOLG\b")
        re_isolated_oriental = re.compile(r"オリエンタル(?!ラウンジ)")
        re_oriradi = re.compile(r"オリラジ")

        for line_no, line in enumerate(content.splitlines(), start=1):
            self.assertFalse(
                re_isolated_olg.search(line),
                f"Line {line_no} contains forbidden 'OLG': {line.strip()}"
            )
            self.assertFalse(
                re_isolated_oriental.search(line),
                f"Line {line_no} contains forbidden isolated 'オリエンタル': {line.strip()}"
            )
            self.assertFalse(
                re_oriradi.search(line),
                f"Line {line_no} contains forbidden 'オリラジ': {line.strip()}"
            )

    def test_no_direct_link_in_editorial_and_stats(self):
        """編集コラム (/guide/*) および統計 (/stats) に直 /link/ が存在しないことを検証"""
        templates_dir = os.path.join(second_venues.BASE_DIR, "templates")
        for fname in os.listdir(templates_dir):
            if fname.startswith("guide_") or "stats" in fname:
                fpath = os.path.join(templates_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "r", encoding="utf-8") as f:
                        body = f.read()
                    # href="/link/..." のような直リンクがないこと（クッション導線や正規サーフェス経由であること）
                    self.assertNotIn('href="/link/', body, f"Direct affiliate link found in editorial/stats: {fname}")

    def test_concierge_seoul_and_primary_second_venues(self):
        """コンシェルジュ診断にソウルが含まれ、診断結果に実在2軒目ルートが付与されることを検証"""
        self.assertIn("seoul", concierge_matcher.CONCIERGE_AREAS)
        diag = concierge_matcher.diagnose_tonight_plan([], area="seoul")
        self.assertEqual(diag.get("area_key"), "seoul")
        self.assertIn("primary", diag)
        self.assertIn("primary_second_venues", diag)
        p_sv = diag["primary_second_venues"]
        self.assertIsNotNone(p_sv)
        self.assertIsInstance(p_sv, list)
        self.assertGreaterEqual(len(p_sv), 2)
        for sp in p_sv:
            self.assertTrue(sp["map_url"].startswith("https://www.google.com/maps/search/?api=1&query="))

    def test_store_slug_resolution_integrity(self):
        """SEO名称や表示名からスラッグが正しく解決されることを検証"""
        self.assertEqual(
            second_venues.resolve_store_slug("オリエンタルラウンジ 渋谷本店"),
            "oriental-lounge-shibuya-honten"
        )
        self.assertEqual(
            second_venues.resolve_store_slug("ag 渋谷"),
            "ag-shibuya"
        )
        self.assertEqual(
            second_venues.resolve_store_slug("JIS NAMBA"),
            "jis-namba"
        )
        self.assertEqual(
            second_venues.resolve_store_slug("oriental-lounge-seoul-gangnam"),
            "oriental-lounge-seoul-gangnam"
        )

    def test_defensive_none_and_empty_inputs(self):
        """store_nameがNoneまたは空文字の境界値でもクラッシュしないことを検証（E2E耐性）"""
        res_none = second_venues.get_second_venues_for_store(None)
        self.assertTrue(res_none["is_fallback"])
        self.assertEqual(len(res_none["spots"]), 4)

        res_empty = second_venues.get_second_venues_for_store("")
        self.assertTrue(res_empty["is_fallback"])
        self.assertEqual(len(res_empty["spots"]), 4)


if __name__ == "__main__":
    unittest.main()
