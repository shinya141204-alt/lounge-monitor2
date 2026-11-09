"""
tests/test_challenger_m3_it5_verification.py - Empirical Challenger Milestone 3 Iteration 5 Stress Harness

Independent adversarial stress tests verifying:
1. Re-test all previous crash cases from Iteration 4:
   - estimate_store_wait_and_pos with non-dict pos_telemetry ('string', 12345, True, False, [1, 2, 3], None, etc.)
   - predict_store_60m_dynamics with non-dict arena_signal ('string', 12345, False, True, [])
   - predict_store_60m_dynamics with arena_signal missing 'label_short' or empty string or non-string
   - predict_store_60m_dynamics with non-dict weather_map or non-dict values inside weather_map
2. Regression fix validation:
   - Tokyo Dome schedule is NOT overwritten by Sumida fireworks in detect_arena_event_surges
   - Seasonal/fireworks events without explicit schedule do NOT fire rogue surges in winter
   - Explicit schedule events strictly take precedence over unexplicit/rogue surges
   - Full regression test test_data_moat_v1.py passes
3. Numeric extremes (big integers 10**309, 10**500, 10**1000, NaN, Inf, -Inf):
   - predict_store_60m_dynamics
   - estimate_store_wait_and_pos
   - calculate_turnover_momentum_index
4. Multi-threaded concurrent stress test (64 threads x 50 iterations = 3200 ops)
5. Project constraints verification (app.py <= 2800 lines, turbo_store.py unchanged, no direct /link/ in guides/stats)
"""

import concurrent.futures
import datetime
import math
import os
import re
import unittest
from unittest.mock import patch

import moat_intelligence
from moat_intelligence import (
    JST,
    _safe_dict,
    _safe_float,
    _safe_int,
    calculate_turnover_momentum_index,
    capture_external_context_snapshot,
    detect_arena_event_surges,
    enrich_stores_with_moat_signals,
    estimate_store_wait_and_pos,
    get_store_weather_brief,
    predict_store_60m_dynamics,
)


class ChallengerM3It5VerificationTestCase(unittest.TestCase):
    def setUp(self):
        self.jst = JST
        self.dt_peak = datetime.datetime(2026, 11, 10, 21, 0, tzinfo=self.jst)
        self.fake_weather = {
            "tokyo": {
                "city_label": "東京",
                "temp_c": 14.5,
                "precip_mm": 0.0,
                "weather_code": 1,
                "wind_kmh": 8.0,
                "condition_ja": "晴れ",
                "is_rainy": False,
            }
        }

    # =========================================================================
    # 1. Previous Crash Cases Re-test
    # =========================================================================

    def test_pos_telemetry_non_dict_across_all_store_categories(self):
        """Non-dict pos_telemetry must NOT crash for flat standing, JIS, or standard stores."""
        bad_inputs = [
            "corrupted_str",
            "",
            "None",
            12345,
            -999,
            0,
            True,
            False,
            [1, 2, 3],
            [],
            (1, 2),
            {"vip_guests": "not_an_int"},
            {"vip_guests": float("nan")},
            {"vip_guests": float("inf")},
            {"vip_guests": 10**500},
            10**500,
            float("nan"),
            float("inf"),
            float("-inf"),
            None,
        ]
        stores_to_test = [
            "ASOBIBAR 新宿",   # Standing flat (Case A)
            "ヤタコイ梅田",    # Standing flat (Case A)
            "JIS SHINJUKU",   # JIS pos (Case B)
            "OLG 新宿",        # Standard (Case C)
            "OLG 渋谷本店",    # Standard (Case C)
        ]

        for store in stores_to_test:
            for bad in bad_inputs:
                res = estimate_store_wait_and_pos(
                    store_name=store,
                    women=20,
                    men=20,
                    pos_telemetry=bad,
                )
                self.assertIsInstance(res, dict, f"Failed for store={store}, pos_telemetry={bad!r}")
                self.assertIn("est_men_wait_min", res)
                self.assertIn("wait_badge_men", res)
                self.assertIn("wait_badge_women", res)

    def test_arena_signal_non_dict_and_corrupted(self):
        """Non-dict arena_signal must safely default without crashing."""
        bad_signals = [
            "corrupted",
            "",
            "None",
            12345,
            -1,
            0,
            True,
            False,
            [],
            [1, 2, 3],
            (1, 2),
            set([1, 2]),
            10**500,
            float("nan"),
            float("inf"),
        ]

        for bad in bad_signals:
            res = predict_store_60m_dynamics(
                store_name="OLG 新宿",
                women=25,
                men=20,
                now_jst=self.dt_peak,
                arena_signal=bad,
            )
            self.assertIsInstance(res, dict)
            self.assertEqual(res["arena_signal_label"], "")
            self.assertEqual(res["arena_phase"], "")
            self.assertEqual(res["arena_event_title"], "")

    def test_arena_signal_missing_label_short_or_corrupted_key(self):
        """Missing or malformed 'label_short' must NOT throw KeyError or TypeError."""
        bad_label_dicts = [
            {"is_surge_now": True},
            {"is_surge_now": True, "label_short": None},
            {"is_surge_now": True, "label_short": ""},
            {"is_surge_now": True, "label_short": "   "},
            {"is_surge_now": True, "label_short": 12345},
            {"is_surge_now": True, "label_short": True},
            {"is_surge_now": True, "label_short": [1, 2]},
            {"is_surge_now": True, "label_short": {"inner": "dict"}},
            {"is_surge_now": False, "label_short": None},
        ]

        # Trigger conditions that enter the alert_code branch in lines 2742-2747
        for bad_dict in bad_label_dicts:
            # Steady / jackpot conditions: women >= 18, high ratios
            res = predict_store_60m_dynamics(
                store_name="OLG 新宿",
                women=50,
                men=50,
                now_jst=self.dt_peak,
                arena_signal=bad_dict,
            )
            self.assertIsInstance(res, dict)
            self.assertIsInstance(res["reason_short"], str)
            # If label_short was empty or non-string, reason_short must not have empty bracket 【】
            if bad_dict.get("label_short") in (None, "", "   "):
                self.assertNotIn("【】", res["reason_short"])

    def test_weather_map_non_dict_and_inner_corrupted(self):
        """Non-dict weather_map or non-dict values inside weather_map must NOT crash."""
        bad_maps = [
            "corrupted",
            "",
            12345,
            -1,
            True,
            False,
            [],
            [1, 2],
            {"tokyo": "not_a_dict"},
            {"tokyo": 12345},
            {"tokyo": None},
            {"tokyo": []},
            {"tokyo": True},
            {"tokyo": {"is_rainy": "not_bool", "temp_c": "invalid", "city_label": None}},
            {"tokyo": {"is_rainy": True, "condition_ja": None, "city_label": None}},
        ]

        for bad in bad_maps:
            res = predict_store_60m_dynamics(
                store_name="OLG 新宿",
                women=20,
                men=20,
                now_jst=self.dt_peak,
                weather_map=bad,
            )
            self.assertIsInstance(res, dict)
            # Weather brief helper as well
            brief = get_store_weather_brief("OLG 新宿", bad)
            self.assertIsInstance(brief, dict)
            self.assertIn("condition", brief)
            self.assertIn("temp_diff_label", brief)

    # =========================================================================
    # 2. Regression Fix & Event Precedence
    # =========================================================================

    def test_tokyo_dome_not_overwritten_by_sumida_fireworks(self):
        """Tokyo Dome explicit schedule must NOT be overwritten by Sumida fireworks."""
        winter_dt = datetime.datetime(2026, 11, 7, 20, 30, tzinfo=self.jst)
        stores = [
            {"name": "OLG 新宿", "women": 26, "men": 19, "women_diff": 7, "is_open_now": True},
            {"name": "OLG 上野", "women": 22, "men": 16, "women_diff": 6, "is_open_now": True},
            {"name": "OLG ag上野", "women": 20, "men": 15, "women_diff": 6, "is_open_now": True},
            {"name": "OLG 錦糸町", "women": 18, "men": 14, "women_diff": 6, "is_open_now": True},
        ]
        mock_sched = {
            "tokyo_dome_budokan": [{
                "venue_id": "tokyo_dome_budokan",
                "venue_name": "東京ドーム",
                "event_title": "WINTER DOME LIVE 2026",
                "est_end_hour": 21,
                "is_explicit_schedule": True,
            }]
        }
        with patch("arena_calendar.get_arena_events_for_date", return_value=mock_sched), \
             patch("moat_intelligence.get_city_weather_map", return_value=self.fake_weather):
            sig = detect_arena_event_surges(stores, now_jst=winter_dt)
            self.assertIn("OLG 新宿", sig)
            self.assertEqual(sig["OLG 新宿"]["venue_id"], "tokyo_dome_budokan")
            self.assertTrue(sig["OLG 新宿"]["is_explicit_schedule"])

            # Verify snapshot contains tokyo_dome_budokan and NOT sumida_fireworks
            snap = capture_external_context_snapshot(stores, allow_blocking=False, now_jst=winter_dt)
            tokyo_row = next(r for r in snap if r["city_key"] == "tokyo")
            self.assertIn("tokyo_dome_budokan", tokyo_row["active_arenas_json"])
            self.assertNotIn("sumida_fireworks", tokyo_row["active_arenas_json"])

    def test_fireworks_rogue_surge_suppression_when_no_schedule(self):
        """Seasonal fireworks must NOT fire rogue surges when no explicit schedule exists."""
        dt = datetime.datetime(2026, 11, 10, 20, 30, tzinfo=self.jst)
        stores = [
            {"name": "OLG 大阪駅前", "women": 40, "men": 30, "women_diff": 15, "is_open_now": True},
            {"name": "JIS UMEDA", "women": 35, "men": 25, "women_diff": 12, "is_open_now": True},
            {"name": "OLG 天満", "women": 30, "men": 20, "women_diff": 10, "is_open_now": True},
        ]
        with patch("arena_calendar.get_arena_events_for_date", return_value={}):
            surges = detect_arena_event_surges(stores, now_jst=dt)
            for sname, s_sig in surges.items():
                self.assertNotEqual(s_sig.get("venue_id"), "yodogawa_fireworks")
                self.assertNotEqual(s_sig.get("venue_id"), "sumida_fireworks")

    def test_explicit_schedule_takes_precedence_over_unexplicit_surges(self):
        """Scheduled events (is_explicit_schedule=True) strictly take precedence."""
        dt = datetime.datetime(2026, 11, 10, 20, 30, tzinfo=self.jst)
        stores = [
            {"name": "OLG 渋谷本店", "women": 35, "men": 25, "women_diff": 10, "is_open_now": True},
            {"name": "OLG ag渋谷", "women": 30, "men": 20, "women_diff": 8, "is_open_now": True},
            {"name": "OLG 新宿", "women": 35, "men": 25, "women_diff": 10, "is_open_now": True},
        ]
        mock_sched = {
            "shibuya_nhk_yoyogi": [{
                "venue_id": "shibuya_nhk_yoyogi",
                "venue_name": "国立代々木競技場・NHKホール",
                "event_title": "SPECIAL ARENA TOUR 2026",
                "est_end_hour": 21,
                "is_explicit_schedule": True,
            }]
        }
        with patch("arena_calendar.get_arena_events_for_date", return_value=mock_sched):
            surges = detect_arena_event_surges(stores, now_jst=dt)
            self.assertIn("OLG 渋谷本店", surges)
            self.assertEqual(surges["OLG 渋谷本店"]["venue_id"], "shibuya_nhk_yoyogi")
            self.assertTrue(surges["OLG 渋谷本店"]["is_explicit_schedule"])

    # =========================================================================
    # 3. Numeric Extremes (Big Ints, NaN, Inf, -Inf)
    # =========================================================================

    def test_predict_store_60m_dynamics_numeric_extremes(self):
        """predict_store_60m_dynamics must survive big integers (10**309, 10**500, 10**1000) and NaN/Inf."""
        extremes = [
            10**309,
            10**500,
            10**1000,
            -10**500,
            float("nan"),
            float("inf"),
            float("-inf"),
            1e308,
            -1e308,
        ]

        for ext in extremes:
            res = predict_store_60m_dynamics(
                store_name="OLG 新宿",
                women=ext,
                men=ext,
                women_diff=ext,
                men_diff=ext,
                lag_context={"lag_15m": (ext, ext), "lag_30m": (ext, ext), "lag_60m": (ext, ext)},
                peer_context={"peer_women": ext, "peer_men": ext, "peer_stores": ext},
                usual_pair_override=(ext, ext, ext, ext, ext),
                lead_intent_heat={"total_intents": ext, "lead_bias_pt": ext, "lead_signal": "male_surge"},
                now_jst=self.dt_peak,
            )
            self.assertIsInstance(res, dict)
            self.assertTrue(math.isfinite(res["pred_60m_women"]))
            self.assertTrue(math.isfinite(res["pred_60m_men"]))
            self.assertTrue(math.isfinite(res["pred_delta_pt"]))
            self.assertTrue(math.isfinite(res["flip_risk_pct"]))
            self.assertTrue(0 <= res["flip_risk_pct"] <= 100)

    def test_estimate_store_wait_and_pos_numeric_extremes(self):
        """estimate_store_wait_and_pos must survive big integers and NaN/Inf."""
        extremes = [
            10**309,
            10**500,
            10**1000,
            -10**500,
            float("nan"),
            float("inf"),
            float("-inf"),
        ]

        for ext in extremes:
            res = estimate_store_wait_and_pos(
                store_name="OLG 新宿",
                women=ext,
                men=ext,
                women_diff=ext,
                men_diff=ext,
                pos_telemetry={
                    "telemetry_type": "jis_pos",
                    "men_waiting_groups": ext,
                    "women_waiting_groups": ext,
                    "men_total_groups": ext,
                    "women_total_groups": ext,
                    "vip_guests": ext,
                },
            )
            self.assertIsInstance(res, dict)
            self.assertTrue(math.isfinite(res["est_men_wait_min"]))
            self.assertTrue(math.isfinite(res["est_women_wait_min"]))
            self.assertTrue(0.0 <= res["occupancy_pct"] <= 100.0)

    def test_calculate_turnover_momentum_index_numeric_extremes(self):
        """calculate_turnover_momentum_index must survive big integers and NaN/Inf."""
        extremes = [
            10**309,
            10**500,
            10**1000,
            -10**500,
            float("nan"),
            float("inf"),
            float("-inf"),
        ]

        for ext in extremes:
            res = calculate_turnover_momentum_index(
                store_name="OLG 新宿",
                women=ext,
                men=ext,
                is_open_now=True,
                women_diff=ext,
                men_diff=ext,
                lag_context={"lag_15m": (ext, ext), "lag_30m": (ext, ext)},
                pos_telemetry={"is_reverse_waiting": True, "women_waiting_groups": ext, "men_waiting_groups": ext},
                p99_cap=ext,
                now_jst=self.dt_peak,
            )
            self.assertIsInstance(res, dict)
            if res["score"] is not None:
                self.assertTrue(10 <= res["score"] <= 95)
                self.assertTrue(math.isfinite(res["score"]))

    # =========================================================================
    # 4. Multi-Threaded Concurrent Stress Testing
    # =========================================================================

    def test_multithreaded_concurrent_stress_64_threads(self):
        """64 concurrent threads hammering all core functions with random and extreme inputs."""
        num_threads = 64
        iters_per_thread = 50
        errors = []

        stores_sample = ["OLG 新宿", "ASOBIBAR 新宿", "JIS SHINJUKU", "OLG 渋谷本店", "ヤタコイ梅田"]

        def worker_task(thread_id: int):
            for i in range(iters_per_thread):
                mode = (thread_id + i) % 5
                try:
                    sname = stores_sample[i % len(stores_sample)]
                    if mode == 0:
                        # predict_store_60m_dynamics with malformed or extreme inputs
                        predict_store_60m_dynamics(
                            store_name=sname,
                            women=20 if i % 2 == 0 else 10**309,
                            men=20 if i % 2 == 0 else float("inf"),
                            arena_signal="corrupted" if i % 3 == 0 else {"is_surge_now": True},
                            weather_map={"tokyo": "bad"} if i % 4 == 0 else {},
                            now_jst=self.dt_peak,
                        )
                    elif mode == 1:
                        # estimate_store_wait_and_pos with malformed pos_telemetry
                        estimate_store_wait_and_pos(
                            store_name=sname,
                            women=15 if i % 2 == 0 else -100,
                            men=25 if i % 2 == 0 else 10**500,
                            pos_telemetry="bad_string" if i % 3 == 0 else {"vip_guests": 10**500},
                        )
                    elif mode == 2:
                        # calculate_turnover_momentum_index
                        calculate_turnover_momentum_index(
                            store_name=sname,
                            women=30,
                            men=25,
                            is_open_now=True,
                            women_diff=10**500 if i % 2 == 0 else 5,
                            men_diff=-10**500 if i % 2 == 0 else 2,
                            p99_cap=120,
                            now_jst=self.dt_peak,
                        )
                    elif mode == 3:
                        # detect_arena_event_surges
                        detect_arena_event_surges([
                            {"name": "OLG 新宿", "women": 25, "men": 20, "women_diff": 7, "is_open_now": True},
                            {"name": "ASOBIBAR 新宿", "women": 15, "men": 15, "women_diff": 2, "is_open_now": True},
                        ], now_jst=self.dt_peak)
                    elif mode == 4:
                        # enrich_stores_with_moat_signals
                        enrich_stores_with_moat_signals([
                            {"name": "OLG 新宿", "women": 25, "men": 20, "women_diff": 7, "is_open_now": True},
                            {"name": "ASOBIBAR 新宿", "women": 15, "men": 15, "women_diff": 2, "is_open_now": True},
                        ], allow_weather_blocking=False, now_jst=self.dt_peak)
                except Exception as ex:
                    errors.append((thread_id, i, mode, str(ex)))

        with concurrent.futures.ThreadPoolExecutor(max_workers=num_threads) as executor:
            futures = [executor.submit(worker_task, tid) for tid in range(num_threads)]
            for f in concurrent.futures.as_completed(futures):
                f.result()

        self.assertEqual(len(errors), 0, f"Encountered {len(errors)} errors in concurrency test: {errors[:5]}")

    # =========================================================================
    # 4b. Deep Edge Cases for Helper and Core Functions
    # =========================================================================

    def test_deep_edge_cases_detect_arena_event_surges(self):
        """detect_arena_event_surges with completely pathological live_stores and scheduled_by_venue."""
        pathological_stores = [
            None,
            123,
            "corrupted",
            True,
            {},
            {"name": None},
            {"name": ""},
            {"name": "   "},
            {"name": "OLG 新宿", "women": "bad_str", "men": None, "women_diff": 10**500, "usual_w_diff": float("nan")},
            {"name": "OLG 上野", "women": 10**500, "men": -10**500, "women_diff": float("inf"), "usual_w_diff": "not_a_float"},
            {"name": "ASOBIBAR 新宿", "women": [1, 2], "men": {"a": 1}},
        ]
        pathological_schedules = [
            None,
            12345,
            "not_a_dict",
            True,
            [],
            {"tokyo_dome_budokan": "not_a_list"},
            {"tokyo_dome_budokan": [None, 123, "string", True]},
            {"tokyo_dome_budokan": [{"event_title": None, "est_end_hour": "invalid_int", "venue_name": None}]},
            {"tokyo_dome_budokan": [{"event_title": 12345, "est_end_hour": 10**500, "venue_name": 123}]},
        ]

        for bad_sched in pathological_schedules:
            with patch("arena_calendar.get_arena_events_for_date", return_value=bad_sched if isinstance(bad_sched, dict) else {}):
                res = detect_arena_event_surges(pathological_stores, now_jst=self.dt_peak)
                self.assertIsInstance(res, dict)

    def test_deep_edge_cases_get_store_weather_brief(self):
        """get_store_weather_brief must never crash with arbitrary inputs."""
        test_keys = [None, 12345, "", "   ", True, False, [1], {"a": 1}, "UNKNOWN_STORE", "OLG 新宿", "tokyo", "sapporo"]
        test_maps = [None, 12345, "bad", [], {"tokyo": None}, {"tokyo": "bad"}, {"tokyo": {"temp_c": "invalid"}}]
        for k in test_keys:
            for m in test_maps:
                res = get_store_weather_brief(k, m)
                self.assertIsInstance(res, dict)
                self.assertIn("condition", res)
                self.assertIn("is_cold_surge", res)
                self.assertIn("temp_diff_label", res)

    def test_deep_edge_cases_enrich_stores_with_moat_signals(self):
        """enrich_stores_with_moat_signals must survive heavily corrupted store lists."""
        corrupted_stores = [
            None,
            123,
            "not_a_dict",
            {},
            {"name": None},
            {"name": "OLG 新宿", "women": "invalid", "men": None, "pos_telemetry": "bad_pos"},
            {"name": "ASOBIBAR 新宿", "women": float("nan"), "men": 10**500, "pos_telemetry": {"vip_guests": "invalid"}},
            {"name": "JIS SHINJUKU", "women": -10, "men": 10, "pos_telemetry": {"telemetry_type": "jis_pos", "men_waiting_groups": "bad"}},
        ]
        res = enrich_stores_with_moat_signals(corrupted_stores, allow_weather_blocking=False, now_jst=self.dt_peak)
        self.assertIsInstance(res, list)
        for s in res:
            if isinstance(s, dict) and s.get("name"):
                self.assertIn("wait_estimate", s)
                self.assertIn("forecast_60m", s)
                self.assertIn("turnover_momentum", s)
                self.assertIn("weather_brief", s)

    # =========================================================================
    # 5. Project Invariants and Constraints
    # =========================================================================

    def test_app_py_lines_under_2800(self):
        """app.py must be <= 2,800 lines strictly."""
        app_path = os.path.join(os.path.dirname(__file__), "..", "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            lines = len(f.readlines())
        self.assertLessEqual(lines, 2800, f"app.py has {lines} lines, exceeding 2800 lines limit")

    def test_turbo_store_line_302_unchanged(self):
        """turbo_store.py connection check invariant remains intact."""
        ts_path = os.path.join(os.path.dirname(__file__), "..", "turbo_store.py")
        with open(ts_path, "r", encoding="utf-8") as f:
            t_content = f.read()
        self.assertIn('broken = bool(getattr(conn, "closed", 0))', t_content)

    def test_no_direct_link_in_guides_and_stats(self):
        """Direct /link/ in templates/guide* and templates/stats* must be zero."""
        tmpl_dir = os.path.join(os.path.dirname(__file__), "..", "templates")
        violating_files = []
        pattern = re.compile(r'href=["\']/link/[^"\']*["\']')

        for fname in os.listdir(tmpl_dir):
            if fname.startswith("guide") or fname.startswith("stats"):
                fpath = os.path.join(tmpl_dir, fname)
                if os.path.isfile(fpath):
                    with open(fpath, "r", encoding="utf-8") as f:
                        content = f.read()
                        if pattern.search(content):
                            violating_files.append(fname)

        self.assertEqual(len(violating_files), 0, f"Found direct /link/ in templates: {violating_files}")


if __name__ == "__main__":
    unittest.main()
