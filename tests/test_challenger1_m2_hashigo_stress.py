"""
tests/test_challenger1_m2_hashigo_stress.py - Milestone 2 ハシゴ診断インテント 実証ストレステスト

Empirical Challenger 1 による敵対的ストレステスト・境界値アタック・ファジング検証
- ファジング・極限入力アタック（SQLi, XSS, 数式注入, 長大文字列, 特殊文字, 異常型）
- 予測バイアス境界値検証（100%女性/男性, 0件, 異常bias_pt, NaN, Inf, 店舗極限人数）
- マルチスレッド並行アクセス・レースコンディション検証
- /api/track エンドポイントのクラッシュ耐性検証
- プライバシー保護・個人情報リーク検証
"""
import unittest
import math
import datetime
import threading
import random
import os
import sys

import analytics
import moat_intelligence
import app as flask_app_module


class TestChallenger1FuzzingAndExtremeInputs(unittest.TestCase):
    """1. analytics.track および get_lead_demand_heat に対する極限入力・ファジング検証"""

    def setUp(self):
        analytics.reset_for_tests()

    def tearDown(self):
        analytics.reset_for_tests()

    def test_01_track_hashigo_intent_fuzzing_labels(self):
        """様々な悪意ある文字列・境界値ラベルを track に注入し、クラッシュ・未捕捉例外が0件であること"""
        fuzz_labels = [
            # 空・空白・制御文字
            "", "   ", "\t\r\n", "\0", "\x00\x01\x1f",
            # 数式インジェクション（CSV / Excel / Sheets injection）
            "=cmd|'/C calc'!A0", "+123456", "-99999", "@SUM(A1:A10)", "===123",
            "=1+1", "@eval(1)", "+cmd|'/c powershell'!'A1'",
            # SQLインジェクション
            "' OR '1'='1", "'; DROP TABLE events; --", "1' UNION SELECT * FROM users --",
            "\" OR \"\"=\"", "' OR 1=1#",
            # XSS・HTMLインジェクション
            "<script>alert(1)</script>", "<img src=x onerror=alert(1)>",
            "<svg/onload=alert(1)>", "javascript:alert(1)",
            "<iframe src='javascript:alert(1)'>",
            # コロン構文の極限パターン
            ":", "::", ":::", ":::::",
            "shinjuku:", ":women:", "::pair",
            "shinjuku:women:", ":men:pair",
            "shinjuku:women:pair:extra:trailing:parts",
            "::::", "::::women::::",
            # 未知エリア・異常な性別・人数
            "atlantis_underwater:women:pair",
            "shinjuku:alien:pair",
            "shinjuku:men:huge_crowd",
            "mars:cyborg:quad",
            "other:unknown:solo",
            # Unicode・サロゲートペア・絵文字・RTL
            "🍻🎉🔥:women:pair",
            "新宿\u200b:women:pair",  # ゼロ幅スペース
            "\u202eRTL_OVERRIDE:men:solo",
            "𠮷野家:women:group",
            # 長大文字列
            "a" * 500 + ":women:pair",
            "shinjuku:" + "b" * 500 + ":pair",
            "shinjuku:women:" + "c" * 500,
            "x" * 10000,
            "y" * 100000,
        ]

        for idx, lbl in enumerate(fuzz_labels):
            try:
                res = analytics.track("hashigo_intent", lbl)
                # trackはTrueまたはFalse（無効ラベルの場合はFalse）を返し、決して例外を投げてはならない
                self.assertIsInstance(res, bool, f"Failed on idx {idx}: label={lbl!r}")
            except Exception as e:
                self.fail(f"Fuzzing crashed on label [{lbl!r}] with exception: {e}")

    def test_02_track_non_string_types(self):
        """label に非文字列型（None, int, float, bool, list, dict, object）を渡してもクラッシュしないこと"""
        weird_types = [
            None,
            12345,
            -1,
            0,
            3.14159265,
            float("nan"),
            float("inf"),
            True,
            False,
            [],
            {},
            {"area": "shinjuku"},
            ["shinjuku", "women", "pair"],
            object(),
            b"shinjuku:women:pair",
        ]

        for item in weird_types:
            try:
                res = analytics.track("hashigo_intent", item)
                self.assertIsInstance(res, bool)
            except Exception as e:
                self.fail(f"track crashed on non-string type [{type(item)}] with exception: {e}")

    def test_03_get_lead_demand_heat_extreme_arguments(self):
        """get_lead_demand_heat に極限・異常パラメータを渡してもクラッシュせず堅牢に辞書を返すこと"""
        test_cases = [
            # areaの極限
            {"area": None, "hours": 2},
            {"area": "", "hours": 2},
            {"area": "   ", "hours": 2},
            {"area": "unknown_fantasy_kingdom", "hours": 2},
            {"area": "shinjuku", "hours": 2},
            {"area": "=cmd|'calc'!A0", "hours": 2},
            {"area": "<script>alert(1)</script>", "hours": 2},
            {"area": "x" * 5000, "hours": 2},
            # hoursの極限（負数、ゼロ、極大値、小数、異常型）
            {"area": "shinjuku", "hours": -10},
            {"area": "shinjuku", "hours": 0},
            {"area": "shinjuku", "hours": 1},
            {"area": "shinjuku", "hours": 24},
            {"area": "shinjuku", "hours": 25},
            {"area": "shinjuku", "hours": 999999},
            {"area": "shinjuku", "hours": 1.5},
            {"area": "shinjuku", "hours": None},
            # _now_jst の極限
            {"area": "shinjuku", "hours": 2, "_now_jst": datetime.datetime(1970, 1, 1)},
            {"area": "shinjuku", "hours": 2, "_now_jst": datetime.datetime(2099, 12, 31, 23, 59, 59)},
            {"area": "shinjuku", "hours": 2, "_now_jst": datetime.datetime.now(datetime.timezone.utc)},
        ]

        for idx, tc in enumerate(test_cases):
            try:
                res = analytics.get_lead_demand_heat(**tc)
                self.assertIsInstance(res, dict, f"Case {idx} must return dict")
                # 必須キーの存在検証
                required_keys = (
                    "area", "hours", "total_intents", "by_audience", "by_party_size",
                    "area_intents", "women_ratio_pct", "lead_signal", "lead_bias_pt"
                )
                for k in required_keys:
                    self.assertIn(k, res, f"Case {idx} missing key: {k}")

                # 型・値域の検証
                self.assertGreaterEqual(res["hours"], 1, f"Case {idx}: hours must be clamped >= 1")
                self.assertLessEqual(res["hours"], 24, f"Case {idx}: hours must be clamped <= 24")
                self.assertGreaterEqual(res["total_intents"], 0)
                self.assertGreaterEqual(res["women_ratio_pct"], 0)
                self.assertLessEqual(res["women_ratio_pct"], 100)
                self.assertIn(res["lead_signal"], ("normal", "female_surge", "male_surge", "active"))
                self.assertIsInstance(res["lead_bias_pt"], float)
            except Exception as e:
                self.fail(f"get_lead_demand_heat crashed on case {idx} {tc} with exception: {e}")

    def test_04_ring_buffer_massive_volume_and_overflow(self):
        """リングバッファ maxlen=2000 を大幅に超える5,000件の投入ストレステスト"""
        areas = ["shinjuku", "shibuya", "umeda", "nanba", "nagoya", "other"]
        audiences = ["men", "women"]
        party_sizes = ["solo", "pair", "group"]

        # 5,000件投入
        for i in range(5000):
            a = areas[i % len(areas)]
            aud = audiences[i % len(audiences)]
            ps = party_sizes[i % len(party_sizes)]
            label = f"{a}:{aud}:{ps}"
            ok = analytics.track("hashigo_intent", label)
            self.assertTrue(ok)

        # リングバッファが2000件にクランプされていること
        self.assertEqual(len(analytics._recent_hashigo_intents), 2000)

        # get_lead_demand_heat がクラッシュせず集計可能であること
        heat_shinjuku = analytics.get_lead_demand_heat("shinjuku", hours=2)
        self.assertGreater(heat_shinjuku["total_intents"], 0)
        self.assertLessEqual(heat_shinjuku["total_intents"], 2000)

        heat_all = analytics.get_lead_demand_heat(None, hours=2)
        self.assertEqual(heat_all["total_intents"], 2000)


class TestChallenger1PredictDynamicsStress(unittest.TestCase):
    """2. moat_intelligence.predict_store_60m_dynamics に対する予測バイアス境界値検証"""

    def test_01_lead_intent_heat_boundary_signals(self):
        """女性100%、男性100%、インテント0件、異常bias_ptなど極限データでの予測安定性"""
        extreme_heats = [
            # 1. 極限女性サージ（100%女性、超大量インテント）
            {
                "total_intents": 10000,
                "by_audience": {"men": 0, "women": 10000},
                "women_ratio_pct": 100,
                "lead_signal": "female_surge",
                "lead_bias_pt": 3.0,
            },
            # 2. 極限男性サージ（100%男性、超大量インテント）
            {
                "total_intents": 10000,
                "by_audience": {"men": 10000, "women": 0},
                "women_ratio_pct": 0,
                "lead_signal": "male_surge",
                "lead_bias_pt": -3.0,
            },
            # 3. 0件インテント
            {
                "total_intents": 0,
                "by_audience": {"men": 0, "women": 0},
                "women_ratio_pct": 50,
                "lead_signal": "normal",
                "lead_bias_pt": 0.0,
            },
            # 4. total_intents が 3未満（反映境界値）
            {
                "total_intents": 2,
                "by_audience": {"men": 0, "women": 2},
                "women_ratio_pct": 100,
                "lead_signal": "female_surge",
                "lead_bias_pt": 3.0,
            },
            # 5. 異常な巨大・極小バイアス値（クリッピング検証）
            {
                "total_intents": 50,
                "by_audience": {"men": 0, "women": 50},
                "women_ratio_pct": 100,
                "lead_signal": "female_surge",
                "lead_bias_pt": 999999.0,
            },
            {
                "total_intents": 50,
                "by_audience": {"men": 50, "women": 0},
                "women_ratio_pct": 0,
                "lead_signal": "male_surge",
                "lead_bias_pt": -999999.0,
            },
            # 6. NaN / Inf バイアス
            {
                "total_intents": 20,
                "lead_signal": "female_surge",
                "lead_bias_pt": float("nan"),
            },
            # 7. 不正なシグナル名
            {
                "total_intents": 20,
                "lead_signal": "apocalyptic_surge",
                "lead_bias_pt": 2.5,
            },
            # 8. 空辞書
            {},
        ]

        store_conditions = [
            ("OLG 新宿", 0, 0),      # 客ゼロ
            ("OLG 新宿", 0, 50),     # 男のみ
            ("OLG 新宿", 50, 0),     # 女のみ
            ("OLG 新宿", 20, 20),    # 均等
            ("OLG 新宿", 1, 1),      # 最小
            ("OLG 新宿", 200, 200),  # 大規模店
        ]

        for heat in extreme_heats:
            for store_name, w, m in store_conditions:
                try:
                    res = moat_intelligence.predict_store_60m_dynamics(
                        store_name, w, m, is_open_now=True, lead_intent_heat=heat
                    )
                    self.assertIsInstance(res, dict)
                    # 予測値が破綻していないか
                    wpct = res.get("pred_60m_wpct")
                    if wpct is not None:
                        self.assertGreaterEqual(wpct, 0, f"wpct negative for heat={heat}, w={w}, m={m}")
                        self.assertLessEqual(wpct, 100, f"wpct > 100 for heat={heat}, w={w}, m={m}")
                        # 通常リターン時の必須互換キー
                        self.assertIn("pred_women", res)
                        self.assertIn("pred_men", res)
                        self.assertGreaterEqual(res["pred_women"], 0)
                        self.assertGreaterEqual(res["pred_men"], 0)
                        self.assertIn("lead_intent_bias_pt", res)
                        self.assertIn("lead_intent_heat", res)
                    else:
                        # total < 6 または営業時間外の早期リターン時
                        self.assertTrue(w + m < 6 or not res.get("is_open_now", True))
                        self.assertIn("pred_60m_women", res)
                        self.assertIn("pred_60m_men", res)

                    # 男余り転落リスク (flip_risk_pct) の健全性
                    self.assertIn("flip_risk_pct", res)
                    if wpct is not None:
                        self.assertGreaterEqual(res["flip_risk_pct"], 3)
                        self.assertLessEqual(res["flip_risk_pct"], 94)
                    else:
                        self.assertEqual(res["flip_risk_pct"], 0)

                except Exception as e:
                    self.fail(f"predict_store_60m_dynamics crashed on store=({store_name}, {w}, {m}) heat={heat}: {e}")

    def test_02_lead_intent_heat_non_dict_types(self):
        """lead_intent_heat に dict 以外の不正型（None, int, str, list, bool）を渡してもクラッシュしないこと"""
        bad_types = [None, "female_surge", 123, [1, 2, 3], False, True, object()]
        for bt in bad_types:
            try:
                res = moat_intelligence.predict_store_60m_dynamics(
                    "OLG 新宿", 15, 15, is_open_now=True, lead_intent_heat=bt
                )
                self.assertIsInstance(res, dict)
                self.assertIn("pred_women", res)
                self.assertIn("pred_men", res)
            except Exception as e:
                self.fail(f"predict_store_60m_dynamics crashed on lead_intent_heat={type(bt)}: {e}")

    def test_03_female_vs_male_surge_z_flip_divergence(self):
        """同一条件下で female_surge 時の男余りリスクが male_surge 時よりも確実に低下（抑制）されていること"""
        lead_f = {
            "total_intents": 20,
            "lead_signal": "female_surge",
            "lead_bias_pt": 2.5
        }
        lead_m = {
            "total_intents": 20,
            "lead_signal": "male_surge",
            "lead_bias_pt": -2.5
        }

        res_f = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 20, 20, is_open_now=True, lead_intent_heat=lead_f)
        res_m = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 20, 20, is_open_now=True, lead_intent_heat=lead_m)

        # 女性サージ時は予測女性比率が高く、男余り転落リスクが低い
        self.assertGreaterEqual(res_f["pred_60m_wpct"], res_m["pred_60m_wpct"])
        self.assertLess(res_f["flip_risk_pct"], res_m["flip_risk_pct"])


class TestChallenger1ConcurrencyAndRaceCondition(unittest.TestCase):
    """3. マルチスレッド環境での並行書き込み・走査耐性（競合・デッドロック検証）"""

    def setUp(self):
        analytics.reset_for_tests()

    def tearDown(self):
        analytics.reset_for_tests()

    def test_concurrent_tracking_and_querying(self):
        """10スレッドから同時に合計2,000件のtrackと200回のget_lead_demand_heatを実行"""
        errors = []
        threads = []

        def worker_track(tid):
            areas = ["shinjuku", "shibuya", "umeda", "nanba"]
            for i in range(200):
                lbl = f"{areas[i % 4]}:women:pair"
                try:
                    analytics.track("hashigo_intent", lbl)
                except Exception as e:
                    errors.append(f"Track thread {tid} error: {e}")

        def worker_query(tid):
            for _ in range(20):
                try:
                    heat = analytics.get_lead_demand_heat("shinjuku", hours=2)
                    self.assertIsInstance(heat, dict)
                except Exception as e:
                    errors.append(f"Query thread {tid} error: {e}")

        for i in range(10):
            t_track = threading.Thread(target=worker_track, args=(i,))
            t_query = threading.Thread(target=worker_query, args=(i,))
            threads.extend([t_track, t_query])

        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrency errors occurred: {errors}")


class TestChallenger1ApiTrackEndpointStress(unittest.TestCase):
    """4. POST /api/track エンドポイントに対するリクエストファジングアタック"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    def setUp(self):
        analytics.reset_for_tests()

    def tearDown(self):
        analytics.reset_for_tests()

    def test_api_track_malformed_requests(self):
        """不正ペイロード、空JSON、巨大文字列をPOSTしても500 Internal Server Error が発生しないこと"""
        attacks = [
            {},
            {"event": "hashigo_intent"},
            {"event": "hashigo_intent", "label": None},
            {"event": "hashigo_intent", "label": "=cmd|'calc'!A0"},
            {"event": "hashigo_intent", "label": "<script>alert(1)</script>"},
            {"event": "hashigo_intent", "label": "a" * 10000},
            {"event": "hashigo_intent", "label": ":::women:pair"},
            {"event": "unauthorized_secret_event", "label": "exploit"},
            {"event": 12345, "label": "shinjuku:women:pair"},
        ]

        for idx, payload in enumerate(attacks):
            res = self.client.post("/api/track", json=payload)
            # 500エラーは絶対NG（204 または 400等で安全に処理されること）
            self.assertNotEqual(res.status_code, 500, f"Payload {idx} caused 500 Internal Server Error: {payload}")


class TestChallenger1PrivacyLeakVerification(unittest.TestCase):
    """5. プライバシー保護・データ漏洩検証（IPアドレスや生クエリがメモリに存在しないこと）"""

    def setUp(self):
        analytics.reset_for_tests()

    def tearDown(self):
        analytics.reset_for_tests()

    def test_zero_ip_or_pii_in_memory_after_fuzzing(self):
        """悪意ある注入後でも、保存データにIPアドレスや危険文字が残存していないこと"""
        ip_payloads = [
            "192.168.1.1:women:pair",
            "10.0.0.1:men:solo",
            "shinjuku:192.168.0.1:pair",
            "shinjuku:women:127.0.0.1",
            "shinjuku:women:pair?ip=203.0.113.195",
        ]

        for p in ip_payloads:
            analytics.track("hashigo_intent", p)

        # リングバッファの走査
        for item in analytics._recent_hashigo_intents:
            ts, area, aud, ps = item
            self.assertNotIn("192.168", area)
            self.assertNotIn("10.0.0", area)
            self.assertNotIn("127.0.0.1", area)
            self.assertNotIn("127.0.0.1", aud)
            self.assertNotIn("127.0.0.1", ps)
            self.assertNotIn("?", area)
            self.assertNotIn("?", aud)
            self.assertNotIn("?", ps)

        # _pending の走査
        for (d_str, ev, lbl), count in analytics._pending.items():
            if ev == "hashigo_intent":
                self.assertNotIn("192.168", lbl)
                self.assertNotIn("10.0.0", lbl)
                self.assertNotIn("127.0.0.1", lbl)
                self.assertNotIn("?", lbl)


if __name__ == '__main__':
    unittest.main()
