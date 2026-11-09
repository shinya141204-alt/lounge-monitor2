"""
tests/test_challenger_m3_it6_2_concurrency_stress.py - Milestone 3 Iteration 6 Challenger 2 Empirical Stress & Concurrency Suite

Coverage:
1. Mathematical Boundary Limits & Extreme Floats:
   - 1e308, -1e308, float('nan'), float('inf'), float('-inf'), 1e-300, -0.0, 1e309, -1e309
   - String representations ('nan', 'inf', '-inf', '1e308'), complex numbers, corrupted objects
   - Boundary clamping (min_val, max_val) and fallback behavior under extreme values
2. High-Concurrency Stress Testing (64 to 128 threads):
   - detect_arena_event_surges, calculate_turnover_momentum_index, predict_store_60m_dynamics, enrich_stores_with_moat_signals
   - Fuzzing permutations with randomized dirty data
   - Concurrently shared mutable store dictionary access
3. Global Structure Integrity & Thread Safety:
   - _STORE_INTERNAL_KEY_MEMO eviction and .clear() under 128 concurrent threads
   - Lock safety and absence of deadlocks or memory leaks
"""

import concurrent.futures
import datetime
import gc
import math
import random
import resource
import time
import unittest

import moat_intelligence
from moat_intelligence import (
    _safe_round_int,
    _safe_float,
    _safe_int,
    _safe_dict,
    _resolve_internal_store_key,
    _STORE_INTERNAL_KEY_MEMO,
    detect_arena_event_surges,
    calculate_turnover_momentum_index,
    predict_store_60m_dynamics,
    enrich_stores_with_moat_signals,
    JST,
)


class TestExtremeFloatsAndBoundaryImmunity(unittest.TestCase):
    """数学的極限値および非標準浮動小数点数に対する耐性検証"""

    def test_safe_round_int_extreme_floats_and_boundaries(self):
        """1e308, -1e308, nan, inf, -inf, 1e-300, -0.0 等の極限浮動小数点数でクラッシュせず期待値を返すこと。"""
        # 1. 極限値 (clamped)
        self.assertEqual(_safe_round_int(1e308, default=0, min_val=0, max_val=100000), 100000)
        self.assertEqual(_safe_round_int(-1e308, default=0, min_val=0, max_val=100000), 0)

        # 2. 非有限値 (NaN / Inf / -Inf) -> default へ安全フォールバック
        self.assertEqual(_safe_round_int(float("nan"), default=42), 42)
        self.assertEqual(_safe_round_int(float("inf"), default=-1), -1)
        self.assertEqual(_safe_round_int(float("-inf"), default=-1), -1)

        # 3. 微小値・符号付きゼロ
        self.assertEqual(_safe_round_int(1e-300, default=0), 0)
        self.assertEqual(_safe_round_int(-0.0, default=0), 0)

        # 4. IEEE 754 オーバーフロー (1e309 は float('inf') と同等)
        self.assertEqual(_safe_round_int(1e309, default=0), 0)
        self.assertEqual(_safe_round_int(-1e309, default=0), 0)

        # 5. 文字列表現
        self.assertEqual(_safe_round_int("1e308", default=0, min_val=0, max_val=100000), 100000)
        self.assertEqual(_safe_round_int("nan", default=99), 99)
        self.assertEqual(_safe_round_int("inf", default=-99), -99)
        self.assertEqual(_safe_round_int("-inf", default=-99), -99)
        self.assertEqual(_safe_round_int("invalid_str", default=10), 10)

        # 6. 特殊型
        self.assertEqual(_safe_round_int(complex(1, 2), default=0), 0)
        self.assertEqual(_safe_round_int([1, 2, 3], default=0), 0)
        self.assertEqual(_safe_round_int({"a": 1}, default=0), 0)
        self.assertEqual(_safe_round_int(None, default=123), 123)

    def test_safe_round_int_min_max_clamps(self):
        """min_val / max_val クランプが正確に動作すること。"""
        self.assertEqual(_safe_round_int(15.7, min_val=0, max_val=10), 10)
        self.assertEqual(_safe_round_int(-5.2, min_val=0, max_val=10), 0)
        self.assertEqual(_safe_round_int(5.4, min_val=0, max_val=10), 5)
        self.assertEqual(_safe_round_int(5.6, min_val=0, max_val=10), 6)


class TestMultiThreadConcurrencyAndRandomDirtyFuzz(unittest.TestCase):
    """64〜128スレッド超高並行およびランダム汚染データ耐性検証"""

    def setUp(self):
        self.now = datetime.datetime(2026, 11, 10, 21, 30, tzinfo=JST)
        self.dirty_values = [
            float("nan"), float("inf"), float("-inf"),
            1e308, -1e308, 1e-300, -0.0, 1e309, -1e309,
            "nan", "inf", "-inf", "1e308", "corrupted", "",
            None, 0, -1, -999, 1000000, 42,
            [1, 2], {"nested": "val"}, (1 + 2j), True, False,
        ]
        self.store_names = [
            "OLG 新宿", "OLG 渋谷本店", "JIS SHINJUKU", "JIS UMEDA",
            "ASOBIBAR 新宿", "YATAKOI UMEDA", "XIX OKAYAMA",
            "UNKNOWN_STORE_NAME", "", None, 12345,
        ]

    def _make_dirty_store(self):
        return {
            "name": random.choice(self.store_names),
            "women": random.choice(self.dirty_values),
            "men": random.choice(self.dirty_values),
            "women_diff": random.choice(self.dirty_values),
            "men_diff": random.choice(self.dirty_values),
            "usual_w_diff": random.choice(self.dirty_values),
            "usual_m_diff": random.choice(self.dirty_values),
            "pos_telemetry": random.choice([
                None, {}, "corrupted", 123,
                {"women_waiting_groups": random.choice(self.dirty_values)},
                {"telemetry_type": "jis_pos", "vip_guests": random.choice(self.dirty_values)},
                {"is_reverse_waiting": random.choice([True, False, None, "bad"])},
            ]),
            "is_open_now": random.choice([True, False, None, "yes", 0, 1]),
        }

    def test_concurrency_64_and_128_threads_randomized_dirty_data(self):
        """64および128スレッドのマルチスレッド並行環境下でランダム汚染データを投入しても例外・クラッシュが発生しないこと。"""
        for n_threads in (64, 128):
            errors = []

            def worker_task(tid):
                local_err = []
                for iteration in range(30):
                    try:
                        # 1. detect_arena_event_surges
                        batch = [self._make_dirty_store() for _ in range(random.randint(1, 6))]
                        r1 = detect_arena_event_surges(batch, now_jst=self.now)
                        if not isinstance(r1, dict):
                            local_err.append(f"T{tid}-it{iteration}: detect_arena not dict")

                        # 2. calculate_turnover_momentum_index
                        r2 = calculate_turnover_momentum_index(
                            store_name=random.choice(self.store_names),
                            women=random.choice(self.dirty_values),
                            men=random.choice(self.dirty_values),
                            women_diff=random.choice(self.dirty_values),
                            men_diff=random.choice(self.dirty_values),
                            pos_telemetry=random.choice([None, {}, {"women_waiting_groups": random.choice(self.dirty_values)}]),
                            now_jst=self.now,
                        )
                        if not isinstance(r2, dict):
                            local_err.append(f"T{tid}-it{iteration}: turnover_momentum not dict")

                        # 3. predict_store_60m_dynamics
                        r3 = predict_store_60m_dynamics(
                            store_name=random.choice(self.store_names),
                            women=random.choice(self.dirty_values),
                            men=random.choice(self.dirty_values),
                            women_diff=random.choice(self.dirty_values),
                            men_diff=random.choice(self.dirty_values),
                            usual_w_diff=random.choice(self.dirty_values),
                            weather_map=random.choice([None, {}, {"tokyo": {"temp_c": random.choice(self.dirty_values)}}]),
                            arena_signal=random.choice([None, {}, {"is_surge_now": True, "venue_id": random.choice(self.dirty_values)}]),
                            lead_intent_heat=random.choice([None, {}, {"total_intents": random.choice(self.dirty_values), "lead_signal": random.choice(["male_surge", "female_surge", "normal", None])}]),
                            now_jst=self.now,
                        )
                        if not isinstance(r3, dict):
                            local_err.append(f"T{tid}-it{iteration}: predict_store_60m not dict")

                        # 4. enrich_stores_with_moat_signals
                        batch2 = [self._make_dirty_store() for _ in range(random.randint(1, 5))]
                        r4 = enrich_stores_with_moat_signals(batch2, now_jst=self.now)
                        if not isinstance(r4, list):
                            local_err.append(f"T{tid}-it{iteration}: enrich_stores not list")

                    except Exception as e:
                        local_err.append(f"T{tid}-it{iteration}: {type(e).__name__}: {e}")
                return local_err

            with concurrent.futures.ThreadPoolExecutor(max_workers=n_threads) as executor:
                futures = [executor.submit(worker_task, tid) for tid in range(n_threads)]
                for f in concurrent.futures.as_completed(futures):
                    errs = f.result()
                    if errs:
                        errors.extend(errs)

            self.assertEqual(len(errors), 0, f"{n_threads}スレッド並行汚染テストでエラー発生: {errors[:5]}")

    def test_shared_mutable_store_dicts_concurrency(self):
        """同一の店舗辞書オブジェクトを複数スレッドから同時に参照・更新してもクラッシュしないこと。"""
        shared_stores = [
            {"name": "OLG 新宿", "women": 20, "men": 15, "women_diff": 5, "men_diff": 2, "usual_w_diff": 12.0},
            {"name": "JIS SHINJUKU", "women": 30, "men": 25, "women_diff": -3, "men_diff": 1, "usual_w_diff": float("nan")},
            {"name": "ASOBIBAR 新宿", "women": 10, "men": 10, "women_diff": 0, "men_diff": 0, "usual_w_diff": float("inf")},
        ]
        errors = []

        def worker_fn(tid):
            local_err = []
            for _ in range(50):
                try:
                    res = enrich_stores_with_moat_signals(shared_stores, now_jst=self.now)
                    if len(res) != 3:
                        local_err.append(f"T{tid}: expected 3 items, got {len(res)}")
                except Exception as e:
                    local_err.append(f"T{tid}: {type(e).__name__}: {e}")
            return local_err

        with concurrent.futures.ThreadPoolExecutor(max_workers=64) as ex:
            futures = [ex.submit(worker_fn, tid) for tid in range(64)]
            for f in concurrent.futures.as_completed(futures):
                errs = f.result()
                if errs:
                    errors.extend(errs)

        self.assertEqual(len(errors), 0, f"共有辞書並行アクセスでエラー発生: {errors[:5]}")


class TestGlobalStructureIntegrityAndMemory(unittest.TestCase):
    """グローバル構造体の整合性、ロック競合、メモリ安定性の検証"""

    def test_store_internal_key_memo_eviction_under_128_threads(self):
        """_STORE_INTERNAL_KEY_MEMO の 512エントリ超過時 .clear() が 128スレッド並行下で安全に実行されること。"""
        errors = []

        def memo_worker(tid):
            local_err = []
            for i in range(50):
                key = f"Adversarial Store {tid}_{i}_{random.randint(1, 2000)}"
                try:
                    resolved = _resolve_internal_store_key(key)
                    if not isinstance(resolved, str):
                        local_err.append(f"T{tid}: non-str resolved")
                except Exception as e:
                    local_err.append(f"T{tid}: {type(e).__name__}: {e}")
            return local_err

        with concurrent.futures.ThreadPoolExecutor(max_workers=128) as ex:
            futures = [ex.submit(memo_worker, tid) for tid in range(128)]
            for f in concurrent.futures.as_completed(futures):
                errs = f.result()
                if errs:
                    errors.extend(errs)

        self.assertEqual(len(errors), 0, f"Memo並行更新でエラー発生: {errors[:5]}")
        self.assertLessEqual(len(_STORE_INTERNAL_KEY_MEMO), 550)

    def test_memory_rss_stability(self):
        """多数の推論実行後もメモリRSSが線形増大せず平坦化（メモリリーク皆無）すること。"""
        def get_mem_mb():
            return resource.getrusage(resource.RUSAGE_SELF).ru_maxrss / (1024 * 1024)

        now = datetime.datetime.now(JST)
        gc.collect()
        m0 = get_mem_mb()

        stores = [
            {"name": "OLG 新宿", "women": 20, "men": 15, "women_diff": 2, "usual_w_diff": 10.0},
            {"name": "JIS SHINJUKU", "women": 30, "men": 25, "women_diff": -3, "usual_w_diff": float("nan")},
        ]

        # 3000イテレーション実行
        for _ in range(3000):
            batch = [dict(s) for s in stores]
            detect_arena_event_surges(batch, now_jst=now)
            calculate_turnover_momentum_index("OLG 新宿", 20, 15, 2, 1, now_jst=now)
            predict_store_60m_dynamics("OLG 新宿", 20, 15, 2, 1, now_jst=now)
            enrich_stores_with_moat_signals(batch, now_jst=now)

        gc.collect()
        m1 = get_mem_mb()
        delta_mb = m1 - m0
        # 3000回の実行でRSS増加は許容範囲内（50MB以下）
        self.assertLess(delta_mb, 50.0, f"急激なメモリ増大を検知: +{delta_mb:.2f} MB")


if __name__ == "__main__":
    unittest.main()
