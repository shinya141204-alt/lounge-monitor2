"""
Adversarial Stress Test for Milestone 2: 3秒ハシゴ診断 先行需要インテント
"""
import unittest
import datetime
import threading
import math

import analytics
import moat_intelligence
import app as flask_app_module


class TestAdversarialHashigoIntentM2(unittest.TestCase):
    def setUp(self):
        analytics.reset_for_tests()

    def tearDown(self):
        analytics.reset_for_tests()

    def test_adv_01_label_normalization_extreme_cases(self):
        """極端な入力値・攻撃文字列に対する normalize_hashigo_intent_label の耐性"""
        # None, 空白
        self.assertEqual(analytics.normalize_hashigo_intent_label(None), "")
        self.assertEqual(analytics.normalize_hashigo_intent_label(""), "")
        self.assertEqual(analytics.normalize_hashigo_intent_label("   "), "")

        # 連続した数式記号
        self.assertEqual(
            analytics.normalize_hashigo_intent_label("==++--@@cmd|'calc':women:pair"),
            "cmdcalc:women:pair"
        )
        self.assertEqual(analytics.normalize_hashigo_intent_label("====="), "")

        # 日本語・マルチバイト文字（re.sub で a-zA-Z0-9_:- 以外は消去され、エリア名が空になるため安全に拒絶 ''）
        res_ja = analytics.normalize_hashigo_intent_label("新宿:女性:ペア")
        self.assertEqual(res_ja, "")

        # コロン無しの文字列
        res_no_colon = analytics.normalize_hashigo_intent_label("shinjuku")
        self.assertEqual(res_no_colon, "shinjuku:men:pair")

        # コロンが10個以上連続する文字列
        res_many_colons = analytics.normalize_hashigo_intent_label("shinjuku::::::::::pair")
        self.assertEqual(res_many_colons, "shinjuku:men:pair")

        # 不正な性別・人数組
        res_bad_aud = analytics.normalize_hashigo_intent_label("shinjuku:robot:alien")
        self.assertEqual(res_bad_aud, "shinjuku:men:pair")

        # 長大な制御文字やnullバイト
        res_null = analytics.normalize_hashigo_intent_label("shinjuku\x00:women:pair")
        self.assertEqual(res_null, "shinjuku:women:pair")

    def test_adv_02_lead_demand_heat_parameters(self):
        """get_lead_demand_heat のパラメータ境界値（hours 異常値、エリア大文字小文字など）"""
        # hours=0 の時は falsy のためデフォルト 2 に安全にフォールバック
        h_zero = analytics.get_lead_demand_heat(area="shinjuku", hours=0)
        self.assertEqual(h_zero["hours"], 2)

        # 負数のときは max(1, ...) で 1 にクランプ
        h_neg = analytics.get_lead_demand_heat(area="shinjuku", hours=-10)
        self.assertEqual(h_neg["hours"], 1)

        # hours > 24
        h_huge = analytics.get_lead_demand_heat(area="shinjuku", hours=100)
        self.assertEqual(h_huge["hours"], 24)  # clamped to 24

        # エリアの大文字小文字混在・前後の空白
        analytics.track("hashigo_intent", "shinjuku:women:pair")
        h_upper = analytics.get_lead_demand_heat(area="  SHINJUKU  ", hours=2)
        self.assertEqual(h_upper["total_intents"], 1)

        # area=None のときは全エリア合計が集計されるか
        analytics.track("hashigo_intent", "umeda:men:pair")
        h_all = analytics.get_lead_demand_heat(area=None, hours=2)
        self.assertEqual(h_all["total_intents"], 2)

    def test_adv_03_time_cutoff_filtering(self):
        """時間経過（cutoff_ts）による古いインテントの除外検証"""
        base_time = datetime.datetime(2026, 11, 10, 12, 0, 0, tzinfo=analytics._JST)
        # 3時間前のイベント
        old_time = base_time - datetime.timedelta(hours=3)
        analytics.track("hashigo_intent", "shinjuku:women:pair", _now_jst=old_time)

        # 30分前のイベント
        recent_time = base_time - datetime.timedelta(minutes=30)
        analytics.track("hashigo_intent", "shinjuku:women:pair", _now_jst=recent_time)

        # 直近2時間の集計（old_time は除外され、recent_time のみカウントされるべき）
        heat = analytics.get_lead_demand_heat(area="shinjuku", hours=2, _now_jst=base_time)
        self.assertEqual(heat["total_intents"], 1)

        # 直近4時間の集計（両方カウントされるべき）
        heat_4h = analytics.get_lead_demand_heat(area="shinjuku", hours=4, _now_jst=base_time)
        self.assertEqual(heat_4h["total_intents"], 2)

    def test_adv_04_concurrency_and_ring_buffer_overflow(self):
        """2000件上限を超える大量書き込みと並行スレッドアクセスの耐性"""
        def worker(thread_id):
            for i in range(100):
                aud = "women" if (i + thread_id) % 2 == 0 else "men"
                analytics.track("hashigo_intent", f"shinjuku:{aud}:pair")

        threads = [threading.Thread(target=worker, args=(t,)) for t in range(25)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 25 * 100 = 2500件投入。dequeのmaxlen=2000なので2000件に抑えられているはず
        self.assertEqual(len(analytics._recent_hashigo_intents), 2000)

        # 集計が例外なく正常終了すること
        heat = analytics.get_lead_demand_heat(area="shinjuku", hours=2)
        self.assertEqual(heat["total_intents"], 2000)
        self.assertIn(heat["lead_signal"], ["female_surge", "male_surge", "active", "normal"])

    def test_adv_05_predict_store_60m_dynamics_corrupted_inputs(self):
        """predict_store_60m_dynamics に壊れた lead_intent_heat が渡された時の堅牢性"""
        # None
        r1 = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 15, 15, is_open_now=True, lead_intent_heat=None)
        self.assertIsNone(r1["lead_intent_heat"])
        self.assertEqual(r1["lead_intent_bias_pt"], 0.0)

        # 空dict
        r2 = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 15, 15, is_open_now=True, lead_intent_heat={})
        self.assertEqual(r2["lead_intent_bias_pt"], 0.0)

        # 型違い（文字列、リスト）
        r3 = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 15, 15, is_open_now=True, lead_intent_heat="invalid")
        self.assertEqual(r3["lead_intent_bias_pt"], 0.0)

        # 異常値・NaN・巨大値
        corrupted_heat = {
            "total_intents": 10,
            "lead_signal": "female_surge",
            "lead_bias_pt": 99999.9  # 巨大値
        }
        r4 = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 15, 15, is_open_now=True, lead_intent_heat=corrupted_heat)
        # クランプされているため最大でも +3.0pt に収まるはず
        self.assertLessEqual(r4["lead_intent_bias_pt"], 3.0)

        corrupted_male_heat = {
            "total_intents": 10,
            "lead_signal": "male_surge",
            "lead_bias_pt": -99999.9  # 負の巨大値
        }
        r5 = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 15, 15, is_open_now=True, lead_intent_heat=corrupted_male_heat)
        # クランプされているため最小でも -3.0pt に収まるはず
        self.assertGreaterEqual(r5["lead_intent_bias_pt"], -3.0)


if __name__ == '__main__':
    unittest.main()
