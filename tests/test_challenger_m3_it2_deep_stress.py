"""
tests/test_challenger_m3_it2_deep_stress.py

Milestone 3 Iteration 2: Empirical Challenger Deep Stress & Adversarial Test Suite
- 極限境界値（float('-inf'), float('nan'), float('inf'), complex, 巨大負数/巨大浮動小数点数）
- 特殊Unicode文字列・不正型注入
- detect_arena_event_surges の非数値客数・破損データ耐性
- DoS耐性・極限入力下でのO(1)計算速度ベンチマーク
- 新たに実証された未捕捉例外クラッシュ（2件）の再現テスト
"""

import cmath
import datetime
import math
import os
import sys
import time
import unittest
from unittest.mock import patch, MagicMock

import arena_calendar
import moat_intelligence


class TestAdversarialExtremeTypesAndValues(unittest.TestCase):
    """極限数値・非数値型・Unicode文字列に対する例外安全性テスト"""

    def setUp(self):
        # 1e308 を除く極限スカラー群（正常耐性確認用）
        self.stable_extreme_scalars = [
            float("-inf"),
            float("inf"),
            float("nan"),
            -10**100,
            10**100,
            complex(1, 2),
            complex(0, -99),
            "   ",
            "",
            "\x00\x01\x02\x1f",
            "𠮷野家\U0001F600\u200B\uFEFF",
            "１２３４５",  # 全角数字
            "---",
            "--",
            "unknown",
            "None",
            "null",
            "NaN",
            "Infinity",
            "-Infinity",
            [1, 2, 3],
            {"bad": "data"},
            object(),
            b"bytes_data",
        ]

    def test_safe_float_and_safe_int_extreme_inputs(self):
        """_safe_float および _safe_int があらゆる極限入力で例外を投げずフォールバックすること"""
        all_scalars = self.stable_extreme_scalars + [1e308, -1e308]
        for val in all_scalars:
            with self.subTest(val=repr(val)):
                # _safe_float
                res_f = moat_intelligence._safe_float(val, ndigits=2, default=-999.0)
                self.assertTrue(
                    res_f == -999.0 or (isinstance(res_f, float) and math.isfinite(res_f)),
                    f"_safe_float failed on {repr(val)}: returned {res_f}",
                )

                # _safe_int
                res_i = moat_intelligence._safe_int(val, default=-999)
                self.assertTrue(
                    res_i == -999 or isinstance(res_i, int),
                    f"_safe_int failed on {repr(val)}: returned {res_i}",
                )

    def test_turnover_momentum_index_stable_extremes(self):
        """calculate_turnover_momentum_index に対する安定極限入力の組み合わせファジング"""
        stores = ["OLG 新宿", "JIS SHINJUKU", "ASOBIBAR 新宿", "未知の店舗名XYZ"]
        for val in self.stable_extreme_scalars:
            for sname in stores:
                with self.subTest(store=sname, input=repr(val)):
                    try:
                        res = moat_intelligence.calculate_turnover_momentum_index(
                            store_name=sname,
                            women=val,
                            men=val,
                            is_open_now=True,
                            women_diff=val,
                            men_diff=val,
                            p99_cap=val if not isinstance(val, (list, dict, object)) else 120,
                            pos_telemetry={"women_waiting_groups": val, "men_waiting_groups": val},
                        )
                    except Exception as exc:
                        self.fail(f"calculate_turnover_momentum_index crashed on input {repr(val)}: {exc}")

                    self.assertIsInstance(res, dict)
                    self.assertIn("score", res)
                    self.assertIn("label", res)
                    self.assertIn("tone", res)
                    self.assertIn("pill_class", res)
                    self.assertIn("badge_class", res)
                    if res["score"] is not None:
                        self.assertGreaterEqual(res["score"], 10)
                        self.assertLessEqual(res["score"], 95)

    def test_detect_arena_event_surges_dirty_inputs(self):
        """detect_arena_event_surges に対する不正客数（'--', 'unknown', None, 巨大値, 異常型）の完全耐性"""
        dirty_stores = [
            {"name": "OLG 新宿", "women": "--", "men": "--", "usual_w_diff": "unknown", "women_diff": "none"},
            {"name": "OLG 渋谷本店", "women": "unknown", "men": None, "usual_w_diff": "--", "women_diff": None},
            {"name": "JIS SHINJUKU", "women": float("nan"), "men": float("inf"), "usual_w_diff": complex(1, 2)},
            {"name": "OLG 上野", "women": -10**50, "men": 10**50, "usual_w_diff": "１２３", "women_diff": "???"},
            {"name": "ASOBIBAR 難波", "women": 1e308, "men": 1e308, "usual_w_diff": 1e308, "women_diff": 1e308},
            {"name": "OLG 名古屋栄", "women": float("-inf"), "men": float("inf")},
            None,
            "not_a_store_dict",
            {},
        ]
        test_times = [
            datetime.datetime(2026, 11, 10, 21, 30),  # イベント終演コア時間
            datetime.datetime(2026, 11, 10, 15, 0),   # 告知フェーズ
            datetime.datetime(2026, 11, 10, 3, 0),    # 深夜時間帯
        ]
        for t in test_times:
            with self.subTest(time=t):
                try:
                    res = moat_intelligence.detect_arena_event_surges(dirty_stores, now_jst=t)
                except Exception as exc:
                    self.fail(f"detect_arena_event_surges crashed on dirty stores at {t}: {exc}")
                self.assertIsInstance(res, dict)

    def test_detect_city_event_surge_alias_dirty_inputs(self):
        """detect_city_event_surge (detect_arena_event_surges の公開エイリアス) の耐性"""
        dirty_stores = [
            {"name": "OLG 新宿", "women": "--", "men": "unknown"},
            {"name": "OLG 渋谷本店", "women": float("-inf"), "men": float("inf")},
        ]
        try:
            res = moat_intelligence.detect_city_event_surge(dirty_stores)
        except Exception as exc:
            self.fail(f"detect_city_event_surge crashed: {exc}")
        self.assertIsInstance(res, dict)

    def test_get_store_weather_brief_string_inputs_and_broken_maps(self):
        """get_store_weather_brief に対する各種文字列および破損マップの耐性"""
        str_inputs = ["OLG 新宿", "tokyo", "UNKNOWN_CITY", "", "   ", "𠮷野家"]
        broken_maps = [None, {}, {"tokyo": None}, {"tokyo": "not_a_dict"}, {"tokyo": {"temp_c": "invalid"}}]
        for s in str_inputs:
            for bm in broken_maps:
                with self.subTest(s=s, bm=repr(bm)):
                    try:
                        res = moat_intelligence.get_store_weather_brief(s, weather_map=bm)
                    except Exception as exc:
                        self.fail(f"get_store_weather_brief crashed on ({s}, {bm}): {exc}")

                    self.assertIsInstance(res, dict)
                    self.assertIn("city", res)
                    self.assertIn("condition", res)
                    self.assertIn("is_cold_surge", res)
                    self.assertIn("temp_diff_c", res)


class TestVulnerabilitiesFixed(unittest.TestCase):
    """[VULNERABILITIES FIXED] 修正された未捕捉例外クラッシュが安全にフォールバックすることの恒久検証"""

    def test_vulnerability_1e308_overflow_in_turnover_momentum_fixed(self):
        """【脆弱性1 修正確認】1e308 等の巨大浮動小数点数・多倍長整数が渡されても OverflowError を起こさず安全返却されること"""
        res1 = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿", women=10, men=10, is_open_now=True,
            women_diff=1e308, men_diff=1e308, p99_cap=120
        )
        self.assertIsInstance(res1, dict)
        self.assertIn("score", res1)

        res2 = moat_intelligence.calculate_turnover_momentum_index(
            store_name="ASOBIBAR 新宿", women=10, men=10, is_open_now=True,
            women_diff=1e308, men_diff=1e308
        )
        self.assertIsInstance(res2, dict)
        self.assertEqual(res2["score"], 85)

        res3 = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿", women=10, men=10, is_open_now=True,
            women_diff=5, men_diff=5, p99_cap=10**309
        )
        self.assertIsInstance(res3, dict)
        self.assertIn("score", res3)

    def test_vulnerability_non_string_store_name_in_weather_brief_fixed(self):
        """【脆弱性2 修正確認】get_store_weather_brief に非文字列オブジェクトが渡されても AttributeError を起こさず安全デフォルト返却されること"""
        non_string_inputs = [float("-inf"), float("nan"), 12345, object()]
        for nsi in non_string_inputs:
            with self.subTest(nsi=repr(nsi)):
                res = moat_intelligence.get_store_weather_brief(nsi)
                self.assertIsInstance(res, dict)
                self.assertEqual(res["condition"], "晴れ")
                self.assertFalse(res["is_cold_surge"])


class TestAdversarialPerformanceAndDoS(unittest.TestCase):
    """極限入力下での DoS 耐性および O(1) 計算時間検証"""

    def test_turnover_momentum_dos_with_poisoned_inputs(self):
        """巨大数値・非有限数を含む毒入り入力での 10,000 回連続実行レイテンシ検証"""
        poisoned_inputs = [
            {"women": float("inf"), "men": float("-inf"), "w_diff": 10**50, "p99": -100},
            {"women": "---", "men": "unknown", "w_diff": "bad", "p99": "zero"},
            {"women": 10**100, "men": 10**100, "w_diff": 10**50, "p99": 10**50},
            {"women": None, "men": None, "w_diff": None, "p99": None},
            {"women": 35, "men": 35, "w_diff": 5, "p99": 120},
        ]
        start = time.perf_counter()
        count = 10000
        for i in range(count):
            inp = poisoned_inputs[i % len(poisoned_inputs)]
            moat_intelligence.calculate_turnover_momentum_index(
                store_name="OLG 新宿",
                women=inp["women"],
                men=inp["men"],
                is_open_now=True,
                women_diff=inp["w_diff"],
                men_diff=inp["w_diff"],
                p99_cap=inp["p99"],
            )
        elapsed = time.perf_counter() - start
        avg_us = (elapsed / count) * 1_000_000
        self.assertLess(
            elapsed, 0.5,
            f"10,000 poisoned executions took {elapsed:.4f}s (avg {avg_us:.2f}μs) - threshold 0.5s",
        )

    def test_detect_arena_event_surges_dos_with_large_store_list(self):
        """2,000 店舗の巨大リスト（非数値データ混入）に対する検知処理時間検証"""
        large_stores = []
        for i in range(2000):
            large_stores.append({
                "name": f"OLG 新宿_{i}" if i % 2 == 0 else f"Unknown_Store_{i}",
                "women": "--" if i % 3 == 0 else (i % 50),
                "men": "unknown" if i % 4 == 0 else (i % 40),
                "usual_w_diff": "none" if i % 5 == 0 else 5.0,
                "women_diff": float("nan") if i % 7 == 0 else 2,
            })

        start = time.perf_counter()
        res = moat_intelligence.detect_arena_event_surges(
            large_stores, now_jst=datetime.datetime(2026, 11, 10, 21, 30)
        )
        elapsed = time.perf_counter() - start
        self.assertIsInstance(res, dict)
        self.assertLess(
            elapsed, 0.1,
            f"detect_arena_event_surges on 2000 stores took {elapsed:.4f}s - threshold 0.1s",
        )


if __name__ == "__main__":
    unittest.main()
