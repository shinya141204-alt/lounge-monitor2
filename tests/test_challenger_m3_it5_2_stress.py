"""
tests/test_challenger_m3_it5_2_stress.py - Empirical Challenger Stress & Concurrency Suite (M3 Iteration 5)

Coverage:
1. High-concurrency race condition and throughput testing (64 to 128 threads)
2. Fuzzing permutations of inputs (dicts with non-string keys, lists with None, float('nan'), string integers, corrupted objects)
3. Safety and exception leakage checks for _safe_dict, _safe_int, and _safe_float
4. Reproducible PoC for detect_arena_event_surges float('nan') / float('inf') crash bug
"""

import concurrent.futures
import datetime
import math
import unittest
from unittest.mock import patch

import moat_intelligence
from moat_intelligence import (
    calculate_turnover_momentum_index,
    detect_arena_event_surges,
    predict_store_60m_dynamics,
    estimate_store_wait_and_pos,
    calibrate_from_jis_pos,
    enrich_stores_with_moat_signals,
    capture_external_context_snapshot,
    _safe_dict,
    _safe_int,
    _safe_float,
    _safe_round_int,
    JST,
)


class TestChallengerM3It5ConcurrencyAndStress(unittest.TestCase):
    def setUp(self):
        self.jst = JST
        self.now = datetime.datetime(2026, 11, 10, 21, 0, tzinfo=self.jst)
        self.stores = [
            "OLG 新宿",
            "OLG 渋谷本店",
            "JIS SHINJUKU",
            "ASOBIBAR 新宿",
            "YATAKOI UMEDA",
            "OLG 上野",
            "JIS UMEDA",
            "OLG 難波",
        ]

    # -------------------------------------------------------------------------
    # 1. High-Concurrency Stress Testing (64 to 128 threads)
    # -------------------------------------------------------------------------
    def test_high_concurrency_64_to_128_threads_throughput(self):
        """64〜128スレッドの超高並行環境下でデータレース・デッドロック・例外なく完走すること。"""
        for thread_count in (64, 128):
            iterations = 40
            errors = []

            def worker_fn(tid):
                for i in range(iterations):
                    sname = self.stores[(tid + i) % len(self.stores)]
                    w = (tid * 7 + i * 3) % 80
                    m = (tid * 5 + i * 4) % 80
                    try:
                        # 1. calculate_turnover_momentum_index
                        r1 = calculate_turnover_momentum_index(
                            store_name=sname,
                            women=w,
                            men=m,
                            is_open_now=True,
                            women_diff=(i % 15) - 7,
                            men_diff=(i % 11) - 5,
                            lag_context={"lag_15m": (w - 2, m - 1), "lag_30m": (w - 5, m - 3)},
                            pos_telemetry={"women_waiting_groups": i % 3},
                            now_jst=self.now,
                        )
                        self.assertIsInstance(r1, dict)

                        # 2. estimate_store_wait_and_pos
                        r2 = estimate_store_wait_and_pos(
                            store_name=sname,
                            women=w,
                            men=m,
                            is_open_now=True,
                            pos_telemetry={
                                "telemetry_type": "jis_pos" if "JIS" in sname else "general",
                                "men_waiting_groups": i % 4,
                                "women_waiting_groups": i % 2,
                                "matched_groups": i % 12,
                                "men_total_groups": max(1, i % 10),
                                "women_total_groups": max(1, i % 10),
                            },
                        )
                        self.assertIsInstance(r2, dict)

                        # 3. predict_store_60m_dynamics
                        r3 = predict_store_60m_dynamics(
                            store_name=sname,
                            women=w,
                            men=m,
                            is_open_now=True,
                            now_jst=self.now,
                            weather_map={"tokyo": {"condition_ja": "晴れ", "is_rainy": False}},
                            arena_signal={"venue_id": "tokyo_dome_budokan", "is_surge_now": True, "label_short": "東京ドーム終演"},
                        )
                        self.assertIsInstance(r3, dict)

                        # 4. detect_arena_event_surges (clean inputs)
                        live_st = [
                            {"name": s, "women": 20, "men": 20, "women_diff": 4}
                            for s in self.stores
                        ]
                        r4 = detect_arena_event_surges(live_st, now_jst=self.now)
                        self.assertIsInstance(r4, dict)

                        # Concurrent calibration read/write
                        if i % 10 == 0:
                            calibrate_from_jis_pos(live_st)

                    except Exception as exc:
                        errors.append((tid, i, type(exc).__name__, str(exc)))
                        break

            with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as ex:
                futures = [ex.submit(worker_fn, t) for t in range(thread_count)]
                concurrent.futures.wait(futures)

            self.assertEqual(len(errors), 0, f"{thread_count} スレッド並行実行でエラー発生: {errors[:5]}")

    # -------------------------------------------------------------------------
    # 2. Fuzzing Permutations & Dirty Types
    # -------------------------------------------------------------------------
    def test_fuzzing_permutations_calculate_turnover_momentum_index(self):
        """calculate_turnover_momentum_index に非標準・破損・極限値を投入しても安全にフォールバックすること。"""
        fuzz_values = [
            float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf",
            None, "invalid_str", 12345, -999, 0, 100000, [1, 2], {"a": 1}, True, False
        ]
        for v in fuzz_values:
            res = calculate_turnover_momentum_index(
                store_name="OLG 新宿",
                women=v,
                men=v,
                women_diff=v,
                men_diff=v,
                p99_cap=v,
                lag_context={1: v, "lag_15m": (v, v), "lag_30m": [v, v]},
                pos_telemetry={None: v, "women_waiting_groups": v, "is_reverse_waiting": v},
                now_jst=self.now,
            )
            self.assertIsInstance(res, dict)
            self.assertIn("score", res)
            self.assertIn("label", res)

    def test_fuzzing_permutations_predict_store_60m_dynamics(self):
        """predict_store_60m_dynamics に非標準・破損・極限値を投入しても安全にフォールバックすること。"""
        fuzz_values = [
            float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf",
            None, "invalid_str", 12345, -999, 0, 100000, [1, 2], {"a": 1}, True, False
        ]
        for v in fuzz_values:
            res = predict_store_60m_dynamics(
                store_name="OLG 新宿",
                women=v,
                men=v,
                women_diff=v,
                men_diff=v,
                usual_w_diff=v,
                now_jst=self.now,
                weather_map={123: v, "tokyo": {"is_rainy": v, "city_label": v, "condition_ja": v}},
                arena_signal={None: v, "is_surge_now": v, "label_short": v, "phase": v},
                lead_intent_heat={1: v, "total_intents": v, "lead_signal": v, "lead_bias_pt": v},
                peer_context={"peer_women": v, "peer_men": v, "peer_stores": v},
                usual_pair_override=(v, v, v, v, v),
            )
            self.assertIsInstance(res, dict)
            self.assertIn("pred_60m_wpct", res)

    def test_fuzzing_permutations_estimate_store_wait_and_pos(self):
        """estimate_store_wait_and_pos に非標準・破損・極限値を投入しても安全にフォールバックすること。"""
        fuzz_values = [
            float("nan"), float("inf"), float("-inf"), "nan", "inf", "-inf",
            None, "invalid_str", 12345, -999, 0, 100000, [1, 2], {"a": 1}, True, False
        ]
        for v in fuzz_values:
            res = estimate_store_wait_and_pos(
                store_name="JIS SHINJUKU",
                women=v,
                men=v,
                women_diff=v,
                men_diff=v,
                pos_telemetry={
                    "telemetry_type": "jis_pos",
                    "men_waiting_groups": v,
                    "men_waiting_guests": v,
                    "women_waiting_groups": v,
                    "matched_groups": v,
                    "men_total_groups": v,
                    "women_total_groups": v,
                    "vip_guests": v,
                },
            )
            self.assertIsInstance(res, dict)
            self.assertIn("est_men_wait_min", res)

    # -------------------------------------------------------------------------
    # 3. Safety Helpers Validation (_safe_dict, _safe_int, _safe_float)
    # -------------------------------------------------------------------------
    def test_safe_helpers_built_in_types(self):
        """_safe_dict, _safe_int, _safe_float が標準的な全Python型に対して例外なく値を正規化すること。"""
        # _safe_dict
        self.assertEqual(_safe_dict({1: 2}), {1: 2})
        self.assertEqual(_safe_dict(None), {})
        self.assertEqual(_safe_dict("corrupted"), {})
        self.assertEqual(_safe_dict([1, 2]), {})
        self.assertEqual(_safe_dict(12345), {})
        self.assertEqual(_safe_dict(float("nan")), {})

        # _safe_int
        self.assertEqual(_safe_int("42"), 42)
        self.assertEqual(_safe_int("-10", min_val=0), 0)
        self.assertEqual(_safe_int("999", max_val=100), 100)
        self.assertEqual(_safe_int(float("nan"), default=7), 7)
        self.assertEqual(_safe_int(float("inf"), default=8), 8)
        self.assertEqual(_safe_int("invalid", default=-1), -1)
        self.assertEqual(_safe_int(None, default=0), 0)
        self.assertEqual(_safe_int([1, 2], default=0), 0)

        # _safe_float
        self.assertEqual(_safe_float("12.345", ndigits=2), 12.35)
        self.assertIsNone(_safe_float(float("nan"), default=None))
        self.assertIsNone(_safe_float(float("inf"), default=None))
        self.assertIsNone(_safe_float(float("-inf"), default=None))
        self.assertIsNone(_safe_float("invalid", default=None))
        self.assertIsNone(_safe_float(None, default=None))
        self.assertEqual(_safe_float("42.0", default=0.0), 42.0)

        # _safe_round_int
        self.assertEqual(_safe_round_int(10.4), 10)
        self.assertEqual(_safe_round_int(10.6), 11)
        self.assertEqual(_safe_round_int("-5.8", default=0, min_val=0), 0)
        self.assertEqual(_safe_round_int("99.4", max_val=50), 50)
        self.assertEqual(_safe_round_int(float("nan"), default=42), 42)
        self.assertEqual(_safe_round_int(float("inf"), default=-1), -1)
        self.assertEqual(_safe_round_int("corrupted", default=0), 0)
        self.assertEqual(_safe_round_int(None, default=99), 99)

    # -------------------------------------------------------------------------
    # 4. VERIFICATION OF SAFE HANDLING FOR NaN/Inf IN usual_w_diff (FIX CONFIRMATION)
    # -------------------------------------------------------------------------
    def test_poc_detect_arena_event_surges_nan_crash(self):
        """
        [FIX VERIFIED] detect_arena_event_surges が usual_w_diff=float('nan') かつ women_diff>=6 であっても
        ValueError でクラッシュせず安全にフォールバックすること。
        """
        live_stores = [
            {
                "name": "OLG 新宿",
                "women": 20,
                "usual_w_diff": float("nan"),
                "women_diff": 8,
            }
        ]
        res = detect_arena_event_surges(live_stores, now_jst=self.now)
        self.assertIsInstance(res, dict)

    def test_poc_nan_in_usual_w_diff_crashes_detect_arena(self):
        """Alias verification: detect_arena_event_surges survives NaN in usual_w_diff."""
        live_stores = [
            {
                "name": "OLG 新宿",
                "women": 20,
                "usual_w_diff": float("nan"),
                "women_diff": 8,
            }
        ]
        res = detect_arena_event_surges(live_stores, now_jst=self.now)
        self.assertIsInstance(res, dict)

    def test_poc_detect_arena_event_surges_inf_crash(self):
        """
        [FIX VERIFIED] detect_arena_event_surges が usual_w_diff=float('inf') であっても
        OverflowError でクラッシュせず安全にフォールバックすること。
        """
        live_stores = [
            {
                "name": "OLG 新宿",
                "women": 20,
                "usual_w_diff": float("inf"),
                "women_diff": 0,
            }
        ]
        res = detect_arena_event_surges(live_stores, now_jst=self.now)
        self.assertIsInstance(res, dict)

    def test_poc_enrich_stores_with_moat_signals_nan_cascade_crash(self):
        """
        [FIX VERIFIED] enrich_stores_with_moat_signals に usual_w_diff=float('nan') が含まれても
        カスケードクラッシュせず全店シグナルが正常生成されること。
        """
        stores = [
            {
                "name": "OLG 新宿",
                "women": 20,
                "men": 20,
                "usual_w_diff": float("nan"),
                "women_diff": 8,
            }
        ]
        res = enrich_stores_with_moat_signals(stores, now_jst=self.now)
        self.assertIsInstance(res, list)
        self.assertEqual(len(res), 1)

    def test_poc_nan_in_usual_w_diff_crashes_enrich_stores_with_moat_signals(self):
        """Alias verification: enrich_stores_with_moat_signals survives NaN in usual_w_diff."""
        stores = [
            {
                "name": "OLG 新宿",
                "women": 20,
                "men": 20,
                "usual_w_diff": float("nan"),
                "women_diff": 8,
            }
        ]
        res = enrich_stores_with_moat_signals(stores, now_jst=self.now)
        self.assertIsInstance(res, list)

    def test_poc_nan_in_usual_w_diff_crashes_capture_external_context_snapshot(self):
        """
        [FIX VERIFIED] capture_external_context_snapshot に usual_w_diff=float('nan') の店舗が含まれても
        クラッシュせずリストを返却すること。
        """
        stores = [
            {
                "name": "OLG 新宿",
                "women": 20,
                "usual_w_diff": float("nan"),
                "women_diff": 8,
            }
        ]
        res = capture_external_context_snapshot(live_stores=stores, allow_blocking=False, now_jst=self.now)
        self.assertIsInstance(res, list)


if __name__ == "__main__":
    unittest.main()
