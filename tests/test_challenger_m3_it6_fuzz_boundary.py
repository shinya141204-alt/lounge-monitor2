"""
tests/test_challenger_m3_it6_fuzz_boundary.py

Empirical Challenger 1 Stress & Boundary Fuzzing Test Suite (Milestone 3 Iteration 6)
Targeting comprehensive NaN, Inf, -Inf, huge numbers, complex nested structures,
corrupt objects, zero-division, overflow, and type safety across:
  - detect_arena_event_surges
  - estimate_store_wait_and_pos
  - calculate_turnover_momentum_index
  - predict_store_60m_dynamics
  - enrich_stores_with_moat_signals
  - capture_external_context_snapshot
"""

import concurrent.futures
import datetime
import json
import math
import random
import unittest
from unittest import mock

import moat_intelligence
from moat_intelligence import (
    JST,
    _safe_dict,
    _safe_float,
    _safe_int,
    _safe_round_int,
    calculate_turnover_momentum_index,
    capture_external_context_snapshot,
    detect_arena_event_surges,
    enrich_stores_with_moat_signals,
    estimate_store_wait_and_pos,
    predict_store_60m_dynamics,
)
import ssr_payload


class TestChallengerM3It6FuzzBoundary(unittest.TestCase):
    def setUp(self):
        self.dt_now = datetime.datetime(2026, 11, 10, 21, 30, tzinfo=JST)
        self.sample_stores = [
            "OLG 新宿",
            "OLG 渋谷本店",
            "JIS SHINJUKU",
            "JIS UMEDA",
            "ASOBIBAR 新宿",
            "YATAKOI UMEDA",
            "ag SAPPORO",
            "CLOVERS",
            "NONEXISTENT_HUB_XYZ",
        ]

        self.adversarial_numbers = [
            float("nan"),
            float("inf"),
            -float("inf"),
            1e308,
            -1e308,
            1e400,
            -1e400,
            10**50,
            -10**50,
            10**300,
            -10**300,
            "nan",
            "NaN",
            "NAN",
            "inf",
            "-inf",
            "Infinity",
            "-Infinity",
            "1e400",
            "1000000000000000000000000000000000",
            "-9999999999999999999999999999999999",
            "bad_string",
            "",
            None,
            True,
            False,
            [],
            {},
            set(),
            object(),
            -100,
            -1,
            0,
            0.0,
            -0.0,
            1,
            1.5,
            4,
            6,
            10,
            16,
            25,
            100,
            100000,
        ]

    # =========================================================================
    # 1. Base Helper Functions: _safe_float, _safe_int, _safe_round_int, _safe_dict
    # =========================================================================
    def test_helpers_adversarial_immunity(self):
        """安全ヘルパーが非有限値・不正型・極限値に対して決して例外を送出しないこと。"""
        for val in self.adversarial_numbers:
            # 1. _safe_float
            f = _safe_float(val, ndigits=2, default=-99.0)
            self.assertTrue(f is None or (isinstance(f, (int, float)) and math.isfinite(f)))

            # 2. _safe_int
            i = _safe_int(val, default=-1, min_val=-10, max_val=100)
            self.assertIsInstance(i, int)
            self.assertGreaterEqual(i, -10)
            self.assertLessEqual(i, 100)

            # 3. _safe_round_int
            ri = _safe_round_int(val, default=42, min_val=0, max_val=1000)
            self.assertIsInstance(ri, int)
            self.assertGreaterEqual(ri, 0)
            self.assertLessEqual(ri, 1000)

            # 4. _safe_dict
            d = _safe_dict(val)
            self.assertIsInstance(d, dict)

    # =========================================================================
    # 2. detect_arena_event_surges Fuzzing
    # =========================================================================
    def test_detect_arena_event_surges_boundary_fuzz(self):
        """detect_arena_event_surges に極限値・破損辞書・不正日時を注入しても完走すること。"""
        # A. live_stores が非リスト型
        for bad_stores in [None, {}, "invalid", 12345, True]:
            res = detect_arena_event_surges(bad_stores, now_jst=self.dt_now)
            self.assertIsInstance(res, dict)

        # B. 各店舗要素の各属性に対する総当たりファジング
        for val in self.adversarial_numbers:
            store_payload = [
                {
                    "name": "OLG 新宿",
                    "women": val,
                    "men": val,
                    "women_diff": val,
                    "usual_w_diff": val,
                },
                {
                    "name": "JIS SHINJUKU",
                    "women": val,
                    "usual_w_diff": val,
                    "women_diff": val,
                },
                {"name": None, "women": val},
                None,
                123,
                "bad_store",
            ]
            res = detect_arena_event_surges(store_payload, now_jst=self.dt_now)
            self.assertIsInstance(res, dict)
            for k, v in res.items():
                self.assertIsInstance(k, str)
                self.assertIsInstance(v, dict)
                self.assertIsInstance(v.get("surge_women_delta"), int)
                self.assertGreaterEqual(v.get("surge_women_delta"), 0)

        # C. 外部モック（arena_calendar, job_state）破損時
        with mock.patch("job_state.get_value", return_value='{"date": "2026-11-10", "venues": ["tokyo_dome", null, 123]}'):
            with mock.patch(
                "arena_calendar.get_arena_events_for_date",
                return_value={
                    "tokyo_dome": [
                        {"est_end_hour": float("nan"), "event_title": None},
                        {"est_end_hour": float("inf"), "event_title": "Huge Concert"},
                        {"est_end_hour": 10**50},
                        {"est_end_hour": -999},
                        None,
                        "invalid_event",
                    ]
                },
            ):
                stores = [{"name": "OLG 新宿", "women": 25, "usual_w_diff": 15.0}]
                res = detect_arena_event_surges(stores, now_jst=self.dt_now)
                self.assertIsInstance(res, dict)

    # =========================================================================
    # 3. estimate_store_wait_and_pos Fuzzing
    # =========================================================================
    def test_estimate_store_wait_and_pos_boundary_fuzz(self):
        """estimate_store_wait_and_pos に極限値・破損POSを注入しても完走すること。"""
        pos_templates = [
            None,
            {},
            "bad_pos",
            [],
            12345,
            {"telemetry_type": "jis_pos"},
            {"telemetry_type": "yatakoi_pos"},
            {"telemetry_type": "general"},
        ]

        for sname in self.sample_stores:
            for val in self.adversarial_numbers:
                for base_pos in pos_templates:
                    pos = dict(base_pos) if isinstance(base_pos, dict) else base_pos
                    if isinstance(pos, dict):
                        pos.update({
                            "men_waiting_groups": val,
                            "men_waiting_guests": val,
                            "women_waiting_groups": val,
                            "matched_groups": val,
                            "men_total_groups": val,
                            "women_total_groups": val,
                            "vip_guests": val,
                        })

                    res = estimate_store_wait_and_pos(
                        store_name=sname,
                        women=val,
                        men=val,
                        is_open_now=True,
                        women_diff=val,
                        men_diff=val,
                        pos_telemetry=pos,
                    )
                    self.assertIsInstance(res, dict)
                    self.assertIsInstance(res.get("est_men_wait_min"), int)
                    self.assertIsInstance(res.get("est_women_wait_min"), int)
                    self.assertIsInstance(res.get("est_men_wait_groups"), int)
                    self.assertIsInstance(res.get("occupancy_pct"), int)
                    self.assertGreaterEqual(res.get("occupancy_pct"), 0)
                    self.assertLessEqual(res.get("occupancy_pct"), 100)

        # キャリブレーション破損時（NaN/Infが事前分布に入った場合）
        bad_calib = {
            "minutes_per_wait_group": float("nan"),
            "avg_men_party_size": float("inf"),
            "excess_to_wait_group_ratio": -float("inf"),
            "high_occ_threshold": 10**50,
        }
        with mock.patch("moat_intelligence.get_jis_calibration_snapshot", return_value=bad_calib):
            res = estimate_store_wait_and_pos("OLG 新宿", women=20, men=35)
            self.assertIsInstance(res, dict)
            self.assertIsInstance(res.get("est_men_wait_min"), int)
            self.assertGreaterEqual(res.get("est_men_wait_min"), 0)

    # =========================================================================
    # 4. calculate_turnover_momentum_index Fuzzing
    # =========================================================================
    def test_calculate_turnover_momentum_index_boundary_fuzz(self):
        """calculate_turnover_momentum_index に極限値・破損ラグを注入しても完走すること。"""
        lag_contexts = [
            None,
            {},
            "invalid_lag",
            {"lag_15m": None, "lag_30m": None},
            {"lag_15m": (float("nan"), float("inf")), "lag_30m": (-float("inf"), 10**50)},
            {"lag_15m": (10**300, -10**300)},
            {"lag_15m": ("bad", "bad")},
            {"lag_15m": [1]},
            {"lag_15m": (5, 5), "lag_30m": (10, 10)},
        ]

        for sname in self.sample_stores:
            for val in self.adversarial_numbers:
                for lag in lag_contexts:
                    res = calculate_turnover_momentum_index(
                        store_name=sname,
                        women=val,
                        men=val,
                        is_open_now=True,
                        women_diff=val,
                        men_diff=val,
                        lag_context=lag,
                        pos_telemetry={"women_waiting_groups": val, "is_reverse_waiting": val},
                        p99_cap=val,
                        now_jst=self.dt_now,
                    )
                    self.assertIsInstance(res, dict)
                    score = res.get("score")
                    if score is not None:
                        self.assertIsInstance(score, int)
                        self.assertGreaterEqual(score, 10)
                        self.assertLessEqual(score, 95)
                    self.assertIsInstance(res.get("volatility"), (int, float))
                    self.assertTrue(math.isfinite(res.get("volatility")))
                    self.assertIsInstance(res.get("flux_rate"), (int, float))
                    self.assertTrue(math.isfinite(res.get("flux_rate")))

    # =========================================================================
    # 5. predict_store_60m_dynamics Fuzzing
    # =========================================================================
    def test_predict_store_60m_dynamics_boundary_fuzz(self):
        """predict_store_60m_dynamics に極限値・破損インテント・破損ピアを注入しても完走すること。"""
        lead_intents = [
            None,
            {},
            "bad_intent",
            {"total_intents": float("nan"), "lead_signal": "female_surge", "lead_bias_pt": float("inf")},
            {"total_intents": float("inf"), "lead_signal": "male_surge", "lead_bias_pt": -float("inf")},
            {"total_intents": 10**50, "lead_signal": None, "lead_bias_pt": 10**50},
            {"total_intents": -10, "lead_signal": "normal", "lead_bias_pt": -99.9},
            {"total_intents": 5, "lead_signal": "female_surge", "lead_bias_pt": 2.5},
            {"total_intents": 5, "lead_signal": "male_surge", "lead_bias_pt": -2.0},
        ]

        peer_contexts = [
            None,
            {},
            "bad_peer",
            {"peer_women": float("nan"), "peer_men": float("inf"), "peer_stores": -1},
            {"peer_women": 10**50, "peer_men": 10**50, "peer_stores": 10**50},
            {"peer_women": 40, "peer_men": 30, "peer_stores": 3},
        ]

        usual_pairs = [
            None,
            (),
            (float("nan"), float("inf"), -float("inf"), 10**50, None),
            (1, 2, 3),  # short
            ("bad", "bad", "bad", "bad", "bad"),
            (20.0, 15.0, 25.0, 18.0, 15.0),
        ]

        for sname in self.sample_stores:
            for val in self.adversarial_numbers:
                for lint in lead_intents:
                    res = predict_store_60m_dynamics(
                        store_name=sname,
                        women=val,
                        men=val,
                        is_open_now=True,
                        women_diff=val,
                        men_diff=val,
                        usual_w_diff=val,
                        now_jst=self.dt_now,
                        weather_map={"tokyo": {"is_rainy": True, "temp_c": val}},
                        arena_signal={"label_short": val, "is_surge_now": True},
                        apply_online_bias=True,
                        lag_context={"lag_15m": (val, val)},
                        peer_context=peer_contexts[0],
                        usual_pair_override=usual_pairs[0],
                        lead_intent_heat=lint,
                    )
                    self.assertIsInstance(res, dict)
                    wpct = res.get("pred_60m_wpct")
                    if wpct is not None:
                        self.assertIsInstance(wpct, int)
                        self.assertGreaterEqual(wpct, 0)
                        self.assertLessEqual(wpct, 100)
                    self.assertIsInstance(res.get("flip_risk_pct"), int)
                    self.assertGreaterEqual(res.get("flip_risk_pct"), 0)
                    self.assertLessEqual(res.get("flip_risk_pct"), 100)
                    self.assertIsInstance(res.get("pred_women"), int)
                    self.assertIsInstance(res.get("pred_men"), int)

    # =========================================================================
    # 6. enrich_stores_with_moat_signals & In-Place Sanitization Fuzzing
    # =========================================================================
    def test_enrich_stores_with_moat_signals_and_ssr_downstream(self):
        """enrich_stores_with_moat_signals が全異常値をインプレースサニタイズし、下流SSRを保護すること。"""
        for val in self.adversarial_numbers:
            stores = [
                {
                    "name": "OLG 新宿",
                    "women": val,
                    "men": val,
                    "women_diff": val,
                    "men_diff": val,
                    "usual_w_diff": val,
                    "usual_m_diff": val,
                    "pos_telemetry": {"vip_guests": val},
                },
                {
                    "name": "JIS SHINJUKU",
                    "women": val,
                    "men": val,
                    "women_diff": val,
                    "men_diff": val,
                    "usual_w_diff": val,
                    "pos_telemetry": {"telemetry_type": "jis_pos", "men_waiting_groups": val},
                },
                {
                    "name": "ASOBIBAR 新宿",
                    "women": val,
                    "men": val,
                },
                None,
                12345,
                {},
            ]

            res = enrich_stores_with_moat_signals(stores, now_jst=self.dt_now)
            self.assertIsInstance(res, list)

            # サニタイズのインプレース検証
            for s in res:
                if not isinstance(s, dict) or not s.get("name"):
                    continue
                self.assertIsInstance(s["women"], int)
                self.assertGreaterEqual(s["women"], 0)
                self.assertIsInstance(s["men"], int)
                self.assertGreaterEqual(s["men"], 0)
                self.assertIsInstance(s["women_diff"], int)
                self.assertIsInstance(s["men_diff"], int)
                if "usual_w_diff" in s:
                    self.assertTrue(s["usual_w_diff"] is None or isinstance(s["usual_w_diff"], (int, float)))
                    if s["usual_w_diff"] is not None:
                        self.assertTrue(math.isfinite(s["usual_w_diff"]))

                # wait_estimate / forecast_60m / turnover_momentum の構造検証
                self.assertIsInstance(s.get("wait_estimate"), dict)
                self.assertIsInstance(s.get("forecast_60m"), dict)
                self.assertIsInstance(s.get("turnover_momentum"), dict)

            # 下流の ssr_payload.build_deferred_card_pool の実行検証
            pool = ssr_payload.build_deferred_card_pool({"ranking": res}, start_idx=0)
            self.assertIsInstance(pool, list)

    # =========================================================================
    # 7. capture_external_context_snapshot Fuzzing
    # =========================================================================
    def test_capture_external_context_snapshot_fuzz(self):
        """capture_external_context_snapshot が非シリアライズ可能オブジェクト・NaN・Infから完全防御すること。"""
        for val in self.adversarial_numbers:
            stores = [
                {"name": "OLG 新宿", "women": val, "usual_w_diff": val, "women_diff": val},
                {"name": "JIS SHINJUKU", "women": val, "usual_w_diff": val},
            ]

            # 破損した気象マップ
            corrupted_weather = {
                "tokyo": {
                    "city_label": "東京",
                    "temp_c": val,
                    "precip_mm": val,
                    "weather_code": val,
                    "wind_kmh": val,
                    "condition_ja": None,
                    "is_rainy": val,
                }
            }
            # 破損した交通マップ（文字列・数値・None・極限値含む）
            corrupted_transit = {
                "tokyo": {
                    "disrupted": val,
                    "delayed_lines": ["山手線", str(val), "中央線"],
                }
            }

            with mock.patch("moat_intelligence.get_city_weather_map", return_value=corrupted_weather):
                with mock.patch("arena_calendar.get_city_transit_status", return_value=corrupted_transit):
                    rows = capture_external_context_snapshot(stores, allow_blocking=False, now_jst=self.dt_now)
                    self.assertIsInstance(rows, list)
                    for r in rows:
                        self.assertIsInstance(r, dict)
                        # temp_c, precip_mm, wind_kmh は有限またはNone
                        self.assertTrue(r["temp_c"] is None or math.isfinite(r["temp_c"]))
                        self.assertTrue(r["precip_mm"] is None or math.isfinite(r["precip_mm"]))
                        self.assertTrue(r["wind_kmh"] is None or math.isfinite(r["wind_kmh"]))
                        # JSON 文字列として有効
                        parsed_arenas = json.loads(r["active_arenas_json"])
                        self.assertIsInstance(parsed_arenas, list)
                        parsed_lines = json.loads(r["transit_lines_json"])
                        self.assertIsInstance(parsed_lines, list)

    # =========================================================================
    # 8. High-Volume Monte Carlo Stochastic Stress Harness (2000 random iterations)
    # =========================================================================
    def test_high_volume_monte_carlo_stochastic_fuzz(self):
        """2,000回の完全ランダム確率的ファジングで未捕捉例外（Crash）が0件であること。"""
        iterations = 2000
        error_count = 0
        exceptions_logged = []

        pool = list(self.adversarial_numbers) + [
            -99999999, 99999999, -0.0000001, 0.0000001, 1e-15, -1e-15,
            float("nan"), float("inf"), -float("inf")
        ]

        for i in range(iterations):
            try:
                sname = random.choice(self.sample_stores)
                w = random.choice(pool)
                m = random.choice(pool)
                wd = random.choice(pool)
                md = random.choice(pool)
                uwd = random.choice(pool)

                # 1. detect_arena_event_surges
                detect_arena_event_surges([{"name": sname, "women": w, "usual_w_diff": uwd, "women_diff": wd}])

                # 2. estimate_store_wait_and_pos
                estimate_store_wait_and_pos(
                    store_name=sname,
                    women=w,
                    men=m,
                    is_open_now=random.choice([True, False, None]),
                    women_diff=wd,
                    men_diff=md,
                    pos_telemetry={
                        "telemetry_type": random.choice(["jis_pos", "yatakoi_pos", "general", None]),
                        "men_waiting_groups": random.choice(pool),
                        "women_waiting_groups": random.choice(pool),
                        "vip_guests": random.choice(pool),
                        "men_total_groups": random.choice(pool),
                    },
                )

                # 3. calculate_turnover_momentum_index
                calculate_turnover_momentum_index(
                    store_name=sname,
                    women=w,
                    men=m,
                    is_open_now=random.choice([True, False]),
                    women_diff=wd,
                    men_diff=md,
                    p99_cap=random.choice(pool),
                    lag_context={"lag_15m": (random.choice(pool), random.choice(pool))},
                    pos_telemetry={"women_waiting_groups": random.choice(pool)},
                )

                # 4. predict_store_60m_dynamics
                predict_store_60m_dynamics(
                    store_name=sname,
                    women=w,
                    men=m,
                    is_open_now=True,
                    women_diff=wd,
                    men_diff=md,
                    usual_w_diff=uwd,
                    lead_intent_heat={
                        "total_intents": random.choice(pool),
                        "lead_signal": random.choice(["female_surge", "male_surge", "normal", None]),
                        "lead_bias_pt": random.choice(pool),
                    },
                )

                # 5. enrich_stores_with_moat_signals
                enrich_stores_with_moat_signals([
                    {"name": sname, "women": w, "men": m, "women_diff": wd, "usual_w_diff": uwd}
                ])

                # 6. capture_external_context_snapshot
                capture_external_context_snapshot(
                    live_stores=[{"name": sname, "women": w, "usual_w_diff": uwd, "women_diff": wd}],
                    allow_blocking=False,
                )
            except Exception as e:
                error_count += 1
                exceptions_logged.append((type(e).__name__, str(e)))

        self.assertEqual(error_count, 0, f"Encountered {error_count} unhandled exceptions during Monte Carlo fuzzing: {exceptions_logged[:5]}")

    # =========================================================================
    # 9. Ultra-Concurrent Multi-Threaded Fuzz Stress (64 threads x 25 iters)
    # =========================================================================
    def test_concurrent_fuzz_stress_64_threads(self):
        """64スレッド並行環境下でランダム異常値・境界値を全6関数に一斉注入してもデータ競合・例外なく完走すること。"""
        thread_count = 64
        iters_per_thread = 25
        errors = []

        pool = list(self.adversarial_numbers)

        def worker_fn(tid):
            for i in range(iters_per_thread):
                sname = self.sample_stores[(tid + i) % len(self.sample_stores)]
                val = pool[(tid * 3 + i * 7) % len(pool)]
                try:
                    # 1. detect_arena_event_surges
                    detect_arena_event_surges([{"name": sname, "women": val, "usual_w_diff": val, "women_diff": val}])

                    # 2. estimate_store_wait_and_pos
                    estimate_store_wait_and_pos(
                        store_name=sname,
                        women=val,
                        men=val,
                        is_open_now=True,
                        women_diff=val,
                        men_diff=val,
                        pos_telemetry={"telemetry_type": "jis_pos", "men_waiting_groups": val, "vip_guests": val},
                    )

                    # 3. calculate_turnover_momentum_index
                    calculate_turnover_momentum_index(
                        store_name=sname,
                        women=val,
                        men=val,
                        is_open_now=True,
                        women_diff=val,
                        men_diff=val,
                        p99_cap=val,
                        lag_context={"lag_15m": (val, val)},
                        pos_telemetry={"women_waiting_groups": val},
                    )

                    # 4. predict_store_60m_dynamics
                    predict_store_60m_dynamics(
                        store_name=sname,
                        women=val,
                        men=val,
                        is_open_now=True,
                        women_diff=val,
                        men_diff=val,
                        usual_w_diff=val,
                        lead_intent_heat={"total_intents": val, "lead_signal": "female_surge", "lead_bias_pt": val},
                    )

                    # 5. enrich_stores_with_moat_signals
                    res = enrich_stores_with_moat_signals([
                        {"name": sname, "women": val, "men": val, "women_diff": val, "usual_w_diff": val}
                    ])
                    # SSR下流検証
                    ssr_payload.build_deferred_card_pool({"ranking": res}, start_idx=0)

                    # 6. capture_external_context_snapshot
                    capture_external_context_snapshot(
                        live_stores=[{"name": sname, "women": val, "usual_w_diff": val, "women_diff": val}],
                        allow_blocking=False,
                    )
                except Exception as e:
                    errors.append((type(e).__name__, str(e), tid, i))

        with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(worker_fn, tid) for tid in range(thread_count)]
            concurrent.futures.wait(futures)

        self.assertEqual(len(errors), 0, f"Concurrent fuzz stress encountered {len(errors)} errors: {errors[:5]}")


if __name__ == "__main__":
    unittest.main()

