"""
tests/test_moat_integration_fuzz_safety.py
Milestone 3 Iteration 6: Data Moat Integration Fuzz & Adversarial Boundary Immunity Test Suite
"""
import unittest
import unittest.mock as mock
import math
import datetime
import json
import moat_intelligence
import ssr_payload


class TestMoatIntegrationFuzzSafety(unittest.TestCase):
    def setUp(self):
        self.dt_peak = datetime.datetime(2026, 11, 10, 21, 0, tzinfo=moat_intelligence.JST)

    # Category A: usual_w_diff Fuzz Matrix
    def test_fuzz_usual_w_diff_matrix(self):
        fuzz_values = [
            float("nan"), float("inf"), -float("inf"),
            "NaN", "inf", "-inf", "corrupted",
            10**309, -10**309, None, -50, 0, 15.5
        ]
        for val in fuzz_values:
            with self.subTest(usual_w_diff=val):
                stores = [
                    {"name": "OLG 新宿", "women": 25, "men": 15, "women_diff": 8, "usual_w_diff": val},
                    {"name": "JIS SHINJUKU", "women": 20, "men": 10, "women_diff": 2, "usual_w_diff": val},
                ]
                # enrich_stores_with_moat_signals がクラッシュしないこと
                res = moat_intelligence.enrich_stores_with_moat_signals(stores, now_jst=self.dt_peak)
                self.assertIsInstance(res, list)

                # capture_external_context_snapshot がクラッシュしないこと
                snap = moat_intelligence.capture_external_context_snapshot(stores, allow_blocking=False, now_jst=self.dt_peak)
                self.assertIsInstance(snap, list)

                # 下流の ssr_payload がクラッシュしないこと
                pool = ssr_payload.build_deferred_card_pool({"ranking": res}, start_idx=0)
                self.assertIsInstance(pool, list)

    # Category B: women & men Fuzz Matrix
    def test_fuzz_headcount_matrix(self):
        fuzz_counts = [
            float("nan"), float("inf"), -float("inf"),
            "invalid_str", 10**309, -100, None, -1, 0, 25, 1000000
        ]
        for val in fuzz_counts:
            with self.subTest(headcount=val):
                stores = [{"name": "OLG 新宿", "women": val, "men": val, "women_diff": 2}]
                res = moat_intelligence.enrich_stores_with_moat_signals(stores, now_jst=self.dt_peak)
                self.assertIsInstance(res, list)
                s = res[0]
                self.assertIsInstance(s["women"], int)
                self.assertGreaterEqual(s["women"], 0)
                self.assertLessEqual(s["women"], 100000)

    # Category C: women_diff & men_diff Fuzz Matrix
    def test_fuzz_diff_matrix(self):
        fuzz_diffs = [float("nan"), 10**309, -10**309, None, "bad_diff", 12]
        for val in fuzz_diffs:
            with self.subTest(women_diff=val):
                stores = [{"name": "OLG 新宿", "women": 20, "men": 15, "women_diff": val, "men_diff": val}]
                res = moat_intelligence.enrich_stores_with_moat_signals(stores, now_jst=self.dt_peak)
                self.assertIsInstance(res, list)
                # v1 旧予測ベンチマークでも OverflowError が発生しないこと
                v1 = moat_intelligence._predict_store_60m_v1_legacy("OLG 新宿", women=20, men=15, women_diff=val)
                self.assertIsInstance(v1, dict)

    # Category D: pos_telemetry Fuzz Matrix
    def test_fuzz_pos_telemetry_matrix(self):
        fuzz_pos = [
            None, "string", 12345, [], {},
            {"vip_guests": float("nan")},
            {"telemetry_type": "jis_pos", "vip_guests": float("nan"), "men_waiting_groups": float("inf")},
            {"telemetry_type": "jis_pos", "men_total_guests": 10**309, "men_total_groups": 0},
            {"telemetry_type": "yatakoi_pos", "men_guests": "invalid"},
        ]
        for pos in fuzz_pos:
            with self.subTest(pos_telemetry=pos):
                stores = [{"name": "JIS SHINJUKU", "women": 20, "men": 15, "pos_telemetry": pos}]
                res = moat_intelligence.enrich_stores_with_moat_signals(stores, now_jst=self.dt_peak)
                self.assertIsInstance(res, list)

    # Category E: weather_map Fuzz Matrix
    def test_fuzz_weather_map_matrix(self):
        fuzz_weathers = [
            None, {}, "corrupted_string", 12345,
            {"tokyo": None},
            {"tokyo": {"temp_c": float("nan"), "precip_mm": float("inf"), "weather_code": "bad", "wind_kmh": -float("inf")}},
        ]
        for wmap in fuzz_weathers:
            with self.subTest(weather_map=wmap):
                # get_store_weather_brief
                brief = moat_intelligence.get_store_weather_brief("OLG 新宿", wmap)
                self.assertIsInstance(brief, dict)
                self.assertIn("condition", brief)

                # capture_external_context_snapshot with corrupted weather
                with mock.patch("moat_intelligence.get_city_weather_map", return_value=wmap):
                    snap = moat_intelligence.capture_external_context_snapshot([], allow_blocking=False, now_jst=self.dt_peak)
                    self.assertIsInstance(snap, list)
                    for row in snap:
                        self.assertTrue(row["temp_c"] is None or math.isfinite(row["temp_c"]))
                        self.assertTrue(row["precip_mm"] is None or math.isfinite(row["precip_mm"]))

    # Category F: context_stores Fuzz Matrix
    def test_fuzz_context_stores_matrix(self):
        corrupted_contexts = [
            None, [], "not_a_list", 12345, {},
            [None, 123, "corrupted", {}],
            [
                {"name": "OLG 新宿", "women": 20, "men": 15, "women_diff": 8, "usual_w_diff": float("nan")},
                {"name": "ASOBIBAR 新宿", "women": float("inf"), "pos_telemetry": "bad"},
            ]
        ]
        valid_stores = [{"name": "OLG 渋谷本店", "women": 20, "men": 15, "women_diff": 1}]
        for ctx in corrupted_contexts:
            with self.subTest(context_stores=ctx):
                res = moat_intelligence.enrich_stores_with_moat_signals(valid_stores, context_stores=ctx, now_jst=self.dt_peak)
                self.assertIsInstance(res, list)

    # Category G: End-to-End Rendering & DB Invariant
    def test_e2e_dirty_store_rendering_and_db_logging(self):
        dirty_stores = [
            {"name": "OLG 新宿", "women": float("nan"), "men": "invalid", "women_diff": 10**309, "usual_w_diff": float("nan")},
            {"name": "JIS SHINJUKU", "women": 25, "men": 20, "women_diff": 8, "usual_w_diff": "inf", "pos_telemetry": {"vip_guests": float("nan")}},
        ]
        enriched = moat_intelligence.enrich_stores_with_moat_signals(dirty_stores, now_jst=self.dt_peak)

        # 1. SSR Payload生成がクラッシュしないこと
        pool = ssr_payload.build_deferred_card_pool({"ranking": enriched}, start_idx=0)
        self.assertEqual(len(pool), 2)
        for card in pool:
            self.assertIsInstance(card["w"], int)
            self.assertIsInstance(card["m"], int)
            self.assertIsInstance(card["wd"], int)
            if "uwd" in card:
                self.assertIsInstance(card["uwd"], int)

        # 2. SQLite / PG 挿入用パラメータに Python 巨大整数や NaN が存在しないこと
        for card in pool:
            self.assertLessEqual(card["w"], 100000)
            self.assertLessEqual(card["wd"], 100000)


if __name__ == "__main__":
    unittest.main()
