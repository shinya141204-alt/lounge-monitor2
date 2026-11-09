"""
tests/test_m3_it5_unified_fixes.py - Milestone 3 Iteration 5 統一修正検証テストスイート

検証対象:
1. _safe_dict ユーティリティの完全な型防御性
2. 花火大会・季節イベントの冬季スケジュール未登録時における野良サージ抑制
3. 夏季花火大会スケジュール登録時の正常発動（事前予告・終演サージ）
4. 公式スケジュールハブがスケジュール未登録ハブによって上書きされない厳格タイブレーク保護
5. estimate_store_wait_and_pos における non-dict pos_telemetry 防御性
6. predict_store_60m_dynamics における non-dict arena_signal および label_short 欠損 KeyError 防御性
7. predict_store_60m_dynamics における non-dict weather_map および non-dict city_w 防御性
8. capture_external_context_snapshot における不完全辞書・直接キー参照防御性
9. build_city_peer_context における非コレクション・非添字要素防御性
10. calibrate_from_jis_pos における非コレクション入力防御性
"""

import datetime
import unittest
from unittest.mock import patch

import moat_intelligence
from moat_intelligence import (
    _safe_dict,
    estimate_store_wait_and_pos,
    predict_store_60m_dynamics,
    detect_arena_event_surges,
    capture_external_context_snapshot,
    build_city_peer_context,
    calibrate_from_jis_pos,
    JST,
)


class TestM3It5UnifiedFixes(unittest.TestCase):
    def setUp(self):
        self.jst = JST

    def test_safe_dict_utility(self):
        """_safe_dict が dict 以外のあらゆる型に対して安全に {} を返すこと。"""
        self.assertEqual(_safe_dict({"a": 1}), {"a": 1})
        self.assertEqual(_safe_dict(None), {})
        self.assertEqual(_safe_dict("corrupted_string"), {})
        self.assertEqual(_safe_dict(12345), {})
        self.assertEqual(_safe_dict([1, 2, 3]), {})
        self.assertEqual(_safe_dict((1, 2)), {})
        self.assertEqual(_safe_dict(True), {})
        self.assertEqual(_safe_dict(False), {})
        self.assertEqual(_safe_dict(3.1415), {})

    def test_fireworks_rogue_surge_suppression_in_winter(self):
        """冬季（11月）でスケジュールが空の場合、夜間急増があっても花火大会の野良サージが発動しないこと。"""
        winter_dt = datetime.datetime(2026, 11, 7, 20, 30, tzinfo=self.jst)
        live_stores = [
            {"name": "OLG 上野", "women": 24, "men": 18, "women_diff": 8, "is_open_now": True},
            {"name": "OLG ag上野", "women": 22, "men": 16, "women_diff": 7, "is_open_now": True},
            {"name": "OLG 錦糸町", "women": 20, "men": 15, "women_diff": 6, "is_open_now": True},
            {"name": "OLG 新宿", "women": 30, "men": 25, "women_diff": 9, "is_open_now": True},
        ]
        with patch("arena_calendar.get_arena_events_for_date", return_value={}):
            surges = detect_arena_event_surges(live_stores, now_jst=winter_dt)
            for sname, sig in surges.items():
                self.assertNotEqual(
                    sig.get("venue_id"),
                    "sumida_fireworks",
                    f"{sname} で冬期スケジュールなしの隅田川花火大会サージが誤発動してはならない"
                )
                self.assertNotEqual(
                    sig.get("venue_id"),
                    "yodogawa_fireworks",
                    f"{sname} で冬期スケジュールなしの淀川花火大会サージが誤発動してはならない"
                )

    def test_fireworks_surge_when_scheduled_in_summer(self):
        """夏季の開催日（公式スケジュール登録あり）には花火大会サージが正常に発動すること。"""
        summer_dt = datetime.datetime(2026, 7, 25, 20, 30, tzinfo=self.jst)
        live_stores = [
            {"name": "OLG 上野", "women": 25, "men": 20, "women_diff": 8, "is_open_now": True},
            {"name": "OLG ag上野", "women": 22, "men": 18, "women_diff": 6, "is_open_now": True},
        ]
        mock_sched = {
            "sumida_fireworks": [{
                "venue_id": "sumida_fireworks",
                "venue_name": "隅田川花火大会",
                "event_title": "第49回 隅田川花火大会",
                "est_end_hour": 21,
                "is_explicit_schedule": True,
            }]
        }
        with patch("arena_calendar.get_arena_events_for_date", return_value=mock_sched):
            surges = detect_arena_event_surges(live_stores, now_jst=summer_dt)
            self.assertIn("OLG 上野", surges)
            sig = surges["OLG 上野"]
            self.assertEqual(sig["venue_id"], "sumida_fireworks")
            self.assertTrue(sig["is_explicit_schedule"])
            self.assertEqual(sig["phase"], "verified_surge")

    def test_strict_tie_break_explicit_beats_unexplicit(self):
        """公式スケジュール登録ハブが未登録ハブ（野良サージ）によって上書きされないこと。"""
        dt = datetime.datetime(2026, 11, 7, 20, 30, tzinfo=self.jst)
        live_stores = [
            {"name": "OLG 新宿", "women": 28, "men": 20, "women_diff": 7, "is_open_now": True},
            {"name": "OLG 上野", "women": 25, "men": 18, "women_diff": 7, "is_open_now": True},
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
        with patch("arena_calendar.get_arena_events_for_date", return_value=mock_sched):
            surges = detect_arena_event_surges(live_stores, now_jst=dt)
            self.assertIn("OLG 新宿", surges)
            self.assertEqual(surges["OLG 新宿"]["venue_id"], "tokyo_dome_budokan")
            self.assertTrue(surges["OLG 新宿"]["is_explicit_schedule"])

    def test_estimate_store_wait_and_pos_resilience(self):
        """estimate_store_wait_and_pos に非辞書 pos_telemetry を渡してもクラッシュしないこと。"""
        for bad_pos in ("corrupted_str", 12345, [1, 2], True, False, 3.14):
            res = estimate_store_wait_and_pos("ASOBIBAR 新宿", women=20, men=20, pos_telemetry=bad_pos)
            self.assertIsInstance(res, dict)
            self.assertFalse(res["is_measured"])
            self.assertEqual(res["est_men_wait_groups"], 0)

    def test_predict_store_60m_dynamics_arena_signal_resilience(self):
        """predict_store_60m_dynamics に非辞書 arena_signal や欠損辞書を渡してもクラッシュしないこと。"""
        for bad_sig in ("corrupted", 123, [1, 2, 3], True):
            res = predict_store_60m_dynamics("OLG 新宿", women=20, men=20, arena_signal=bad_sig)
            self.assertIsInstance(res, dict)
            self.assertEqual(res["arena_signal_label"], "")

        # label_short キー欠損の辞書
        now = datetime.datetime(2026, 11, 10, 21, 0, tzinfo=self.jst)
        res_missing_key = predict_store_60m_dynamics(
            "OLG 新宿", women=50, men=50, now_jst=now, arena_signal={"is_surge_now": True}
        )
        self.assertIsInstance(res_missing_key, dict)
        self.assertNotIn("【", res_missing_key["reason_short"])

    def test_predict_store_60m_dynamics_weather_map_resilience(self):
        """predict_store_60m_dynamics に非辞書 weather_map や非辞書 city_w を渡してもクラッシュしないこと。"""
        for bad_w in ("corrupted", 12345, ["a", "b"]):
            res = predict_store_60m_dynamics("OLG 新宿", women=20, men=20, weather_map=bad_w)
            self.assertIsInstance(res, dict)
            self.assertFalse(res["is_rain_shelter"])

        res_bad_city = predict_store_60m_dynamics(
            "OLG 新宿", women=20, men=20, weather_map={"tokyo": "not_a_dict"}
        )
        self.assertIsInstance(res_bad_city, dict)
        self.assertFalse(res_bad_city["is_rain_shelter"])

    def test_capture_external_context_snapshot_resilience(self):
        """capture_external_context_snapshot における不完全辞書・直接キー参照の防御性。"""
        with patch("moat_intelligence.get_city_weather_map", return_value={"tokyo": {"city_label": "東京"}}):
            with patch("moat_intelligence.detect_arena_event_surges", return_value={"OLG 新宿": {"is_surge_now": True}}):
                rows = capture_external_context_snapshot([], allow_blocking=False)
                self.assertIsInstance(rows, list)
                self.assertTrue(len(rows) >= 1)
                tokyo_row = next((r for r in rows if r["city_key"] == "tokyo"), None)
                self.assertIsNotNone(tokyo_row)
                self.assertEqual(tokyo_row["city_label"], "東京")
                self.assertIsNone(tokyo_row["temp_c"])

    def test_build_city_peer_context_resilience(self):
        """build_city_peer_context に非コレクション・非添字要素を渡してもクラッシュしないこと。"""
        self.assertEqual(build_city_peer_context(None), {})
        self.assertEqual(build_city_peer_context(12345), {})
        self.assertEqual(build_city_peer_context("string"), {})
        res1 = build_city_peer_context({"OLG 新宿": 12345})
        self.assertIsInstance(res1, dict)
        res2 = build_city_peer_context([123, "broken", None])
        self.assertIsInstance(res2, dict)

    def test_calibrate_from_jis_pos_resilience(self):
        """calibrate_from_jis_pos に非コレクション入力を渡してもクラッシュしないこと。"""
        self.assertIsInstance(calibrate_from_jis_pos(None), dict)
        self.assertIsInstance(calibrate_from_jis_pos(12345), dict)
        self.assertIsInstance(calibrate_from_jis_pos("string"), dict)


if __name__ == "__main__":
    unittest.main()
