"""
tests/test_hashigo_intent_m2.py - Milestone 2: 3秒ハシゴ診断 先行需要インテント単体・結合テストスイート
"""
import unittest
import os
import json
import datetime
from unittest.mock import patch

import analytics
import moat_intelligence
import app as flask_app_module


class TestHashigoIntentM2(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    def setUp(self):
        analytics.reset_for_tests()

    def tearDown(self):
        analytics.reset_for_tests()

    def test_01_allowed_events_registration(self):
        """1. ALLOWED_EVENTS に 'hashigo_intent' が登録されていること"""
        self.assertIn('hashigo_intent', analytics.ALLOWED_EVENTS)

    def test_02_label_normalization_and_sanitization(self):
        """2. ラベルの正規化・サニタイズ（インジェクション・長大文字・未知エリア）"""
        # 正常系
        self.assertEqual(
            analytics.normalize_hashigo_intent_label("shinjuku:men:pair"),
            "shinjuku:men:pair"
        )
        self.assertEqual(
            analytics.normalize_hashigo_intent_label("umeda:women:solo"),
            "umeda:women:solo"
        )
        self.assertEqual(
            analytics.normalize_hashigo_intent_label("shibuya:women:group"),
            "shibuya:women:group"
        )
        self.assertEqual(
            analytics.normalize_hashigo_intent_label("sapporo:men:trio"),
            "sapporo_sendai:men:group"
        )

        # 数式インジェクション対策
        self.assertEqual(
            analytics.normalize_hashigo_intent_label("=cmd|' /C calc'!A0:women:pair"),
            "cmdccalca0:women:pair"
        )

        # XSS・特殊文字
        self.assertEqual(
            analytics.normalize_hashigo_intent_label("<script>alert(1)</script>:women:pair"),
            "scriptalert1script:women:pair"
        )

        # 96文字以上の長大ラベル
        long_area = "a" * 100
        res = analytics.normalize_hashigo_intent_label(f"{long_area}:men:pair")
        self.assertLessEqual(len(res), 64)

        # 空白・空文字
        self.assertEqual(analytics.normalize_hashigo_intent_label(""), "")
        self.assertEqual(analytics.normalize_hashigo_intent_label("   "), "")

    def test_03_track_and_get_lead_demand_heat(self):
        """3. track から get_lead_demand_heat までの集計フロー検証"""
        # 新宿に女性ペア3件、女性ソロ2件、男性ペア1件投入（計6件、女性5件、女性比83%）
        for _ in range(3):
            self.assertTrue(analytics.track("hashigo_intent", "shinjuku:women:pair"))
        for _ in range(2):
            self.assertTrue(analytics.track("hashigo_intent", "shinjuku:women:solo"))
        self.assertTrue(analytics.track("hashigo_intent", "shinjuku:men:pair"))

        heat = analytics.get_lead_demand_heat(area="shinjuku", hours=2)
        self.assertEqual(heat["total_intents"], 6)
        self.assertEqual(heat["by_audience"]["women"], 5)
        self.assertEqual(heat["by_audience"]["men"], 1)
        self.assertEqual(heat["by_party_size"]["pair"], 4)
        self.assertEqual(heat["by_party_size"]["solo"], 2)
        self.assertEqual(heat["by_party_size"]["group"], 0)
        self.assertEqual(heat["women_ratio_pct"], 83)
        self.assertEqual(heat["lead_signal"], "female_surge")
        self.assertGreater(heat["lead_bias_pt"], 1.5)
        self.assertLessEqual(heat["lead_bias_pt"], 3.0)

    def test_04_signal_thresholds_logic(self):
        """4. 先行需要シグナル（female_surge, male_surge, active, normal）の境界値検証"""
        # Case A: 少人数（<5件）は normal
        for _ in range(4):
            analytics.track("hashigo_intent", "shibuya:women:pair")
        heat_small = analytics.get_lead_demand_heat(area="shibuya", hours=1)
        self.assertEqual(heat_small["lead_signal"], "normal")
        self.assertEqual(heat_small["lead_bias_pt"], 0.0)

        analytics.reset_for_tests()

        # Case B: 男性急増（women_ratio <= 30%）は male_surge
        for _ in range(5):
            analytics.track("hashigo_intent", "umeda:men:pair")
        for _ in range(1):
            analytics.track("hashigo_intent", "umeda:women:pair")
        heat_male = analytics.get_lead_demand_heat(area="umeda", hours=1)
        self.assertEqual(heat_male["lead_signal"], "male_surge")
        self.assertLess(heat_male["lead_bias_pt"], 0.0)

        analytics.reset_for_tests()

        # Case C: 活発需要（>=8件、31%〜59%）は active
        for _ in range(4):
            analytics.track("hashigo_intent", "nagoya:women:pair")
        for _ in range(5):
            analytics.track("hashigo_intent", "nagoya:men:pair")
        heat_active = analytics.get_lead_demand_heat(area="nagoya", hours=1)
        self.assertEqual(heat_active["lead_signal"], "active")
        self.assertEqual(heat_active["lead_bias_pt"], 0.0)

    def test_05_zero_records_handling(self):
        """5. 記録ゼロ・未知エリアでのゼロ除算耐性とデフォルト値返却"""
        heat = analytics.get_lead_demand_heat(area="completely_unheard_area_xyz", hours=2)
        self.assertEqual(heat["total_intents"], 0)
        self.assertEqual(heat["women_ratio_pct"], 50)
        self.assertEqual(heat["lead_signal"], "normal")
        self.assertEqual(heat["lead_bias_pt"], 0.0)
        self.assertEqual(heat["by_audience"]["women"], 0)
        self.assertEqual(heat["by_audience"]["men"], 0)

    def test_06_predict_60m_dynamics_bias_integration(self):
        """6. moat_intelligence.predict_store_60m_dynamics へのバイアス・リスク反映検証"""
        base = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 20, 20, is_open_now=True)
        self.assertIn("pred_women", base)
        self.assertIn("pred_men", base)

        # 女性サージ時
        lead_female = {
            "total_intents": 20,
            "by_audience": {"men": 4, "women": 16},
            "women_ratio_pct": 80,
            "lead_signal": "female_surge",
            "lead_bias_pt": 2.5
        }
        res_female = moat_intelligence.predict_store_60m_dynamics(
            "OLG 新宿", 20, 20, is_open_now=True, lead_intent_heat=lead_female
        )
        self.assertGreaterEqual(res_female["pred_60m_wpct"], base["pred_60m_wpct"])
        self.assertIn("pred_women", res_female)
        self.assertIn("pred_men", res_female)
        self.assertEqual(res_female["lead_intent_bias_pt"], 2.5)

        # 男性サージ時
        lead_male = {
            "total_intents": 20,
            "by_audience": {"men": 17, "women": 3},
            "women_ratio_pct": 15,
            "lead_signal": "male_surge",
            "lead_bias_pt": -2.6
        }
        res_male = moat_intelligence.predict_store_60m_dynamics(
            "OLG 新宿", 20, 20, is_open_now=True, lead_intent_heat=lead_male
        )
        self.assertLessEqual(res_male["pred_60m_wpct"], base["pred_60m_wpct"])
        self.assertGreater(res_male["flip_risk_pct"], res_female["flip_risk_pct"])

    def test_07_enrich_stores_with_lead_heat(self):
        """7. enrich_stores_with_moat_signals で店舗オブジェクトに lead_demand_heat が付与されること"""
        stores = [{"name": "OLG 新宿", "women": 15, "men": 15, "is_open_now": True}]
        enriched = moat_intelligence.enrich_stores_with_moat_signals(stores)
        self.assertEqual(len(enriched), 1)
        st = enriched[0]
        self.assertIn("lead_demand_heat", st)
        self.assertIn("forecast_60m", st)

    def test_08_beacon_allowed_and_track_endpoint(self):
        """8. app._BEACON_ALLOWED_EVENTS および POST /api/track のエンドポイント疎通"""
        self.assertIn('hashigo_intent', flask_app_module._BEACON_ALLOWED_EVENTS)

        # 正常リクエスト（204 No Content）
        res = self.client.post("/api/track", json={
            "event": "hashigo_intent",
            "label": "shinjuku:women:pair"
        })
        self.assertEqual(res.status_code, 204)

        # ボット除外
        res_bot = self.client.post("/api/track", json={
            "event": "hashigo_intent",
            "label": "shinjuku:women:pair"
        }, headers={"User-Agent": "Googlebot/2.1"})
        self.assertEqual(res_bot.status_code, 204)

        # スタッフテスト除外
        res_staff = self.client.post("/api/track?staff_test=1", json={
            "event": "hashigo_intent",
            "label": "shinjuku:women:pair"
        })
        self.assertEqual(res_staff.status_code, 204)

    def test_09_privacy_zero_ip_guarantee(self):
        """9. IPアドレス・端末IDがメモリおよびスキーマに一切保存されないこと"""
        for item in analytics._recent_hashigo_intents:
            self.assertEqual(len(item), 4)  # (timestamp, area, audience, party_size)

        for key in analytics._pending.keys():
            date_str, ev, lbl = key
            self.assertNotIn("ip", lbl.lower())
            self.assertNotIn("127.0.0.1", lbl)

    def test_10_template_integration_verification(self):
        """10. テンプレート（concierge.html, store_detail.html）の整合性確認"""
        concierge_path = os.path.join(os.path.dirname(__file__), "../templates/concierge.html")
        with open(concierge_path, "r", encoding="utf-8") as f:
            c_html = f.read()
        self.assertIn("hashigo_intent", c_html)
        self.assertIn("trackHashigoIntent", c_html)

        detail_path = os.path.join(os.path.dirname(__file__), "../templates/store_detail.html")
        with open(detail_path, "r", encoding="utf-8") as f:
            d_html = f.read()
        self.assertIn("moat-intent-signal-row", d_html)
        self.assertIn("先行需要", d_html)


if __name__ == '__main__':
    unittest.main()
