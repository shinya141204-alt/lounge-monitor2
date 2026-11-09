"""
tests/test_challenger_m3_it3_extreme_adversarial.py

Milestone 3 Iteration 3: Empirical Challenger Extreme Adversarial Test Suite
- 超巨大多倍長整数 (10**500, -10**500, 10**1000)
- 極限浮動小数点数 (1e308, -1e308, float('inf'), float('-inf'), float('nan'))
- 異常型オブジェクト (dict, list, set, tuple, object, function)
- 悪意ある文字列 (超長文字列, nullバイト, RTL制御文字, Zalgo, 全角数字)
- Milestone 3 新設・改修API（calculate_turnover_momentum_index, get_store_weather_brief,
  city_key_for_store, detect_arena_event_surges, build_city_peer_context,
  _safe_int, _safe_float）に対する例外安全性と完全縮退の実証
"""

import datetime
import math
import unittest

import moat_intelligence


class TestAdversarialExtremeAdversarialInputs(unittest.TestCase):
    """極限数値・非数値型・悪意ある文字列に対する完全耐性検証"""

    def setUp(self):
        # 浮動小数点数変換でオーバーフローを起こす超巨大多倍長整数群
        self.overflowing_integers = [
            10**309,
            -10**309,
            10**500,
            -10**500,
            10**1000,
            -10**1000,
        ]
        # 通常の有限整数群
        self.normal_extreme_integers = [
            2**63 - 1,
            -2**63,
            2**64,
            -2**64,
        ]
        self.all_extreme_integers = self.overflowing_integers + self.normal_extreme_integers

        self.extreme_floats = [
            1e308,
            -1e308,
            1e-324,
            -1e-324,
            float("inf"),
            float("-inf"),
            float("nan"),
        ]
        self.corrupt_objects = [
            {},
            {"a": 1},
            [],
            [1, "2", None],
            (1, 2, 3),
            {1, 2},
            object(),
            lambda x: x,
            None,
        ]
        self.adversarial_strings = [
            "",
            "   ",
            "\x00\x00\x00",
            "\x00hello\x00world\x00",
            "A" * 100_000,
            "𠮷野家\U0001F600\u200B\uFEFF",
            "\u202E\u202D\u200E\u200F",
            "Z̡̢̛a̸̧l̷̡ģ̷o̴̧",
            "１２３４５６７８９０",
            "true",
            "false",
            "null",
            "undefined",
            "NaN",
            "Infinity",
            "-Infinity",
            "1e308",
            "10**500",
        ]

    def test_safe_int_with_hyper_large_ints_and_objects(self):
        """_safe_int が 10**500 や任意オブジェクトで OverflowError/TypeError を出さず安全にクランプ・フォールバックすること"""
        # 超巨大整数（min_val / max_val クランプ）
        for val in self.all_extreme_integers:
            res = moat_intelligence._safe_int(val, default=0, min_val=-1000, max_val=1000)
            self.assertIsInstance(res, int)
            self.assertTrue(-1000 <= res <= 1000, f"_safe_int failed clamp on {val}")

        # 極限float
        for val in self.extreme_floats:
            res = moat_intelligence._safe_int(val, default=-1, min_val=0, max_val=100)
            self.assertIsInstance(res, int)
            self.assertTrue(res == -1 or 0 <= res <= 100, f"_safe_int failed float {val}")

        # 腐敗オブジェクト
        for val in self.corrupt_objects:
            res = moat_intelligence._safe_int(val, default=42)
            self.assertEqual(res, 42)

    def test_safe_float_with_hyper_large_and_objects(self):
        """_safe_float が オーバーフロー整数や任意オブジェクトで OverflowError を出さず安全に None/default を返すこと"""
        for val in self.overflowing_integers:
            res = moat_intelligence._safe_float(val, default=-999.0)
            self.assertIsInstance(res, float)
            self.assertEqual(res, -999.0)

        for val in self.extreme_floats:
            res = moat_intelligence._safe_float(val, default=-999.0)
            if math.isfinite(val):
                self.assertIsInstance(res, float)
            else:
                self.assertEqual(res, -999.0)

        for val in self.corrupt_objects:
            res = moat_intelligence._safe_float(val, default=-999.0)
            self.assertEqual(res, -999.0)

    def test_city_key_for_store_with_all_possible_types(self):
        """city_key_for_store が文字列以外の全データ型・巨大文字列・特殊文字列に対して安全に動作すること"""
        all_bad_inputs = (
            self.all_extreme_integers
            + self.extreme_floats
            + self.corrupt_objects
            + self.adversarial_strings
        )
        for val in all_bad_inputs:
            with self.subTest(val=repr(val)[:40]):
                res = moat_intelligence.city_key_for_store(val)
                self.assertIsInstance(res, str)
                self.assertIn(res, list(moat_intelligence.WEATHER_CITIES.keys()) + [""])

    def test_get_store_weather_brief_extreme_types_and_nested_corruption(self):
        """get_store_weather_brief がキー・マップ双方に異常値・破損辞書が渡されても安全に動作すること"""
        keys_to_test = (
            self.all_extreme_integers
            + self.extreme_floats
            + self.corrupt_objects
            + self.adversarial_strings
        )
        corrupted_maps = [
            None,
            {},
            {10**500: "corrupted"},
            {"tokyo": 10**500},
            {"tokyo": {"temp_c": 10**500, "past_temp_c": float("nan")}},
            {"tokyo": {"temp_c": float("inf"), "is_cold_surge": object()}},
            {"tokyo": {"condition": ["bad", "list"]}},
            {"tokyo": None},
        ]
        for key in keys_to_test:
            for wmap in corrupted_maps:
                res = moat_intelligence.get_store_weather_brief(key, weather_map=wmap)
                self.assertIsInstance(res, dict)
                self.assertIn("condition", res)
                self.assertIn("is_cold_surge", res)
                self.assertIn("temp_diff_c", res)
                self.assertIn("temp_c", res)

    def test_calculate_turnover_momentum_index_hyper_extremes(self):
        """calculate_turnover_momentum_index に 10**500, 10**1000, 巨大負数、悪意ある辞書を投入してもクラッシュしないこと"""
        extreme_values = [
            10**500,
            -10**500,
            10**1000,
            1e308,
            -1e308,
            float("inf"),
            float("-inf"),
            float("nan"),
            "10**500",
            "A" * 1000,
            object(),
            {"bad": 1},
        ]
        test_stores = [
            "OLG 新宿",
            "JIS SHINJUKU",
            "ASOBIBAR 難波",
            "ヤタコイ 天神",
            "未知店舗 123",
            10**500,
            None,
        ]

        for sname in test_stores:
            for ev in extreme_values:
                with self.subTest(store=repr(sname)[:30], val=repr(ev)[:30]):
                    lag_ctx = {
                        "lag_15m": [ev, ev, ev],
                        "lag_30m": [ev, ev],
                        "bad_key": ev,
                    }
                    pos_tel = {
                        "women_waiting_groups": ev,
                        "men_waiting_groups": ev,
                        "is_reverse_waiting": ev,
                    }
                    res = moat_intelligence.calculate_turnover_momentum_index(
                        store_name=sname,
                        women=ev,
                        men=ev,
                        is_open_now=True,
                        women_diff=ev,
                        men_diff=ev,
                        p99_cap=ev,
                        lag_context=lag_ctx,
                        pos_telemetry=pos_tel,
                        now_jst=datetime.datetime(2026, 11, 10, 22, 0),
                    )
                    self.assertIsInstance(res, dict)
                    self.assertIn("score", res)
                    self.assertIn("label", res)
                    self.assertIn("volatility", res)
                    self.assertIn("flux_rate", res)
                    if res["score"] is not None:
                        self.assertGreaterEqual(res["score"], 10)
                        self.assertLessEqual(res["score"], 95)
                    self.assertTrue(
                        res["volatility"] is None or isinstance(res["volatility"], (int, float))
                    )
                    self.assertTrue(
                        res["flux_rate"] is None or isinstance(res["flux_rate"], (int, float))
                    )

    def test_build_city_peer_context_extreme_inputs(self):
        """build_city_peer_context に 10**500 や float('nan') のペア辞書・店舗リストを投入しても安全に集計されること"""
        corrupted_map = {
            "OLG 新宿": [10**500, -10**500],
            "JIS SHINJUKU": [float("nan"), float("inf")],
            "ASOBIBAR 難波": [1e308, 1e308],
            "未知店舗": None,
            "不正タプル": (),
            10**500: [20, 25],
        }
        res_map = moat_intelligence.build_city_peer_context(corrupted_map)
        self.assertIsInstance(res_map, dict)

        corrupted_list = [
            {"name": "OLG 新宿", "women": 10**500, "men": 1e308, "is_open_now": True},
            {"name": "JIS SHINJUKU", "women": "unknown", "men": None, "is_open_now": True},
            {"name": None, "women": 10, "men": 10},
            {"name": "   ", "women": 10, "men": 10},
            {"name": "ASOBIBAR 難波", "women": 50, "men": 50, "is_open_now": False},
            None,
            "not_a_dict",
            {},
        ]
        res_list = moat_intelligence.build_city_peer_context(corrupted_list)
        self.assertIsInstance(res_list, dict)

    def test_detect_arena_event_surges_hyper_extremes(self):
        """detect_arena_event_surges に 10**500 や 1e308 の客数・異常店舗リストを投入しても安全に検知されること"""
        extreme_stores = [
            {"name": "OLG 新宿", "women": 10**500, "men": 10**500, "women_diff": 1e308, "usual_w_diff": 10**500},
            {"name": "JIS SHINJUKU", "women": -10**500, "men": float("inf"), "women_diff": float("-inf")},
            {"name": "OLG 渋谷本店", "women": "１２３４５", "men": "---", "women_diff": None},
            {"name": "ASOBIBAR 新宿", "women": float("nan"), "men": complex(1, 2)},
            None,
            {},
            "invalid_record",
        ]
        res = moat_intelligence.detect_arena_event_surges(
            extreme_stores,
            now_jst=datetime.datetime(2026, 11, 10, 21, 30),
        )
        self.assertIsInstance(res, dict)

    def test_enrich_stores_with_moat_signals_valid_dict_list_with_extreme_values(self):
        """全店舗エンリッチメントパイプラインに極限値が詰まった店舗辞書リストを投入し完全走破すること"""
        adversarial_store_list = [
            {
                "name": "OLG 新宿",
                "women": 10**500,
                "men": 10**500,
                "women_diff": 1e308,
                "men_diff": 1e308,
                "is_open_now": True,
                "p99_capacity": 10**500,
            },
            {
                "name": "ASOBIBAR 新宿",
                "women": float("nan"),
                "men": float("inf"),
                "women_diff": float("-inf"),
                "men_diff": "not_a_number",
                "is_open_now": True,
            },
            {
                "name": "JIS SHINJUKU",
                "women": -100,
                "men": -50,
                "women_diff": None,
                "men_diff": object(),
                "pos_telemetry": {"women_waiting_groups": 10**500, "is_reverse_waiting": True},
                "is_open_now": True,
            },
            {
                "name": "未知の店舗\x00\x01\x02",
                "women": 15,
                "men": 15,
                "is_open_now": True,
            },
        ]

        enriched = moat_intelligence.enrich_stores_with_moat_signals(
            adversarial_store_list,
            now_jst=datetime.datetime(2026, 11, 10, 22, 0),
        )
        self.assertIsInstance(enriched, list)
        self.assertEqual(len(enriched), len(adversarial_store_list))

        for r in enriched:
            self.assertIn("turnover_momentum", r)
            self.assertIn("weather_brief", r)
            self.assertIn("turnover_momentum_index", r)
            self.assertIn("turnover_label", r)


if __name__ == "__main__":
    unittest.main()
