"""
tests/test_adversarial_m3_stress.py

Milestone 3 実証的アドバーサリアル・ストレステスト＆ファジングスイート
- シナリオ1: calculate_turnover_momentum_index への極限入力ファジング（負数、10万人、境界、異常型、ゼロ除算）
- シナリオ2: 気象寒暖差（_fetch_open_meteo_all_cities & weather_brief）の境界値・欠損・極限気温検証
- シナリオ3: 都市イベントマスタ（STATIC_CITY_EVENTS & MAJOR_ARENA_HUBS）の整合性＆外部通信ゼロ実証
- シナリオ4: 極限入力下での Flask テンプレート統合レンダリング耐性
"""

import datetime
import os
import socket
import time
import unittest
from unittest.mock import patch, MagicMock

import arena_calendar
import moat_intelligence


class TestAdversarialTurnoverMomentum(unittest.TestCase):
    """calculate_turnover_momentum_index への極限入力ファジング＆アドバーサリアル攻撃"""

    def test_negative_and_none_and_string_headcounts(self):
        """負数・None・文字列・非有限浮動小数点数（NaN, Inf）の入力耐性"""
        bad_inputs = [
            (-10, -20),
            (None, None),
            ("invalid", "string"),
            ("", ""),
            (float("nan"), float("inf")),
            (-99999, 99999),
        ]
        for w, m in bad_inputs:
            with self.subTest(w=w, m=m):
                res = moat_intelligence.calculate_turnover_momentum_index(
                    store_name="OLG 新宿",
                    women=w,
                    men=m,
                    is_open_now=True,
                )
                self.assertIsInstance(res, dict)
                self.assertIn("score", res)
                self.assertIn("label", res)

    def test_headcount_five_vs_six_boundary(self):
        """客数5名（境界・判定対象外） vs 客数6名（境界・判定対象）の検証"""
        # 5名: women=3, men=2 -> total < 6 のため判定対象外 (score is None, label='判定対象外')
        res_5 = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿", women=3, men=2, is_open_now=True
        )
        self.assertIsNone(res_5.get("score"))
        self.assertEqual(res_5.get("label"), "判定対象外")

        # 6名: women=3, men=3 -> 判定対象となりスコア（10〜95）算出
        res_6 = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿", women=3, men=3, is_open_now=True
        )
        self.assertIsNotNone(res_6.get("score"))
        self.assertTrue(10 <= res_6["score"] <= 95)

    def test_closed_store_and_closing_suppression(self):
        """営業時間外および閉店30分前の退店抑制挙動"""
        # 営業時間外
        res_closed = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿", women=20, men=20, is_open_now=False
        )
        self.assertIsNone(res_closed.get("score"))
        self.assertEqual(res_closed.get("label"), "判定対象外")

        # 閉店30分前（通常05:00閉店店舗の04:45時点）
        now_closing = datetime.datetime(2026, 11, 10, 4, 45)
        res_closing = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=20,
            men=20,
            is_open_now=True,
            women_diff=-10,
            men_diff=-10,
            now_jst=now_closing,
        )
        self.assertIsNotNone(res_closing.get("score"))
        self.assertLessEqual(res_closing["score"], 60, "閉店間際は score <= 60 に抑制されるべき")

    def test_zero_and_negative_p99_capacity_zero_division(self):
        """p99_cap=0 や負数、異常文字列によるゼロ除算攻撃・クラッシュの防御"""
        bad_caps = [0, -1, -100, None, "zero", 0.0]
        for cap in bad_caps:
            with self.subTest(cap=cap):
                res = moat_intelligence.calculate_turnover_momentum_index(
                    store_name="OLG 新宿",
                    women=30,
                    men=30,
                    is_open_now=True,
                    women_diff=5,
                    men_diff=5,
                    p99_cap=cap,
                )
                self.assertIsInstance(res, dict)
                self.assertIsNotNone(res.get("score"))
                self.assertGreaterEqual(res["score"], 10)
                self.assertLessEqual(res["score"], 95)

    def test_massive_headcounts_and_flux_overflow(self):
        """100万人単位の巨大客数・巨大フラックスに対するスコア飽和テスト（10〜95のクランプ）"""
        res = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=1000000,
            men=1000000,
            is_open_now=True,
            women_diff=500000,
            men_diff=500000,
            p99_cap=120,
        )
        self.assertIsInstance(res, dict)
        self.assertEqual(res["score"], 95, "上限95に正しくクランプされること")

    def test_malformed_lag_context(self):
        """壊れた lag_context（不正な構造・型違い・None等）の注入"""
        bad_lags = [
            "not a dict",
            {"lag_15m": "not a tuple"},
            {"lag_15m": [None]},
            {"lag_15m": ["invalid", "bad"]},
            {"lag_30m": 12345},
            {"lag_15m": (), "lag_30m": ()},
            {"lag_15m": (None, None), "lag_30m": (None, None)},
        ]
        for bl in bad_lags:
            with self.subTest(lag=repr(bl)):
                res = moat_intelligence.calculate_turnover_momentum_index(
                    store_name="OLG 新宿",
                    women=20,
                    men=20,
                    is_open_now=True,
                    lag_context=bl,
                )
                self.assertIsInstance(res, dict)
                self.assertIn("score", res)

    def test_malformed_pos_telemetry(self):
        """壊れた pos_telemetry の注入"""
        bad_pos = [
            "not a dict",
            {"is_reverse_waiting": "yes", "women_waiting_groups": "lots"},
            {"is_reverse_waiting": True, "women_waiting_groups": None},
            {"is_reverse_waiting": False, "women_waiting_groups": -5},
            {"unknown_key": 999},
        ]
        for bp in bad_pos:
            with self.subTest(pos=repr(bp)):
                res = moat_intelligence.calculate_turnover_momentum_index(
                    store_name="JIS SHINJUKU",
                    women=20,
                    men=20,
                    is_open_now=True,
                    pos_telemetry=bp,
                )
                self.assertIsInstance(res, dict)
                self.assertIn("score", res)

    def test_high_frequency_performance_dos_defense(self):
        """5,000回連続呼び出し時の高速性（マイクロ秒単位での処理、アルゴリズム計算量 O(1) 検証）"""
        start = time.perf_counter()
        for i in range(5000):
            moat_intelligence.calculate_turnover_momentum_index(
                store_name="OLG 新宿",
                women=20 + (i % 30),
                men=20 + (i % 25),
                is_open_now=True,
                women_diff=(i % 5) - 2,
                men_diff=(i % 5) - 2,
                p99_cap=120,
            )
        elapsed = time.perf_counter() - start
        self.assertLess(elapsed, 0.5, f"5000 executions took too long: {elapsed:.4f}s")


class TestAdversarialWeatherApi(unittest.TestCase):
    """Open-Meteo 気象レスポンス破損・異常気象耐性テスト"""

    def test_broken_api_payloads(self):
        """APIが空辞書、None、空リスト、不整合リストを返した場合の防衛"""
        broken_payloads = [
            {},
            [],
            [None] * 10,
            [{"current": None, "hourly": None}] * 10,
            [{"current": {}, "hourly": {"time": ["2026-11-09T20:00"], "temperature_2m": []}}] * 10,
            [{"current": {"temperature_2m": "invalid"}}] * 10,
        ]
        for payload in broken_payloads:
            with self.subTest(payload=repr(payload)):
                with patch("requests.get") as mock_get:
                    mock_res = MagicMock()
                    mock_res.status_code = 200
                    mock_res.json.return_value = payload
                    mock_get.return_value = mock_res

                    parsed = moat_intelligence._fetch_open_meteo_all_cities()
                    self.assertIsInstance(parsed, dict)

    def test_cold_surge_exact_threshold_minus_2_9_vs_3_0(self):
        """寒暖差 -2.9℃（通常） vs -3.0℃（急冷サージ発動境界）の厳密検証"""
        # -2.9℃ -> is_cold_surge=False
        mock_normal = [{
            "current": {"temperature_2m": 12.1, "precipitation": 0.0, "weather_code": 0, "wind_speed_10m": 3.0},
            "hourly": {
                "time": ["2026-11-09T20:00", "2026-11-09T21:00", "2026-11-09T22:00", "2026-11-09T23:00"],
                "temperature_2m": [15.0] * 4,
            },
        }] * 10

        # -3.0℃ -> is_cold_surge=True
        mock_surge = [{
            "current": {"temperature_2m": 12.0, "precipitation": 0.0, "weather_code": 0, "wind_speed_10m": 3.0},
            "hourly": {
                "time": ["2026-11-09T20:00", "2026-11-09T21:00", "2026-11-09T22:00", "2026-11-09T23:00"],
                "temperature_2m": [15.0] * 4,
            },
        }] * 10

        with patch("requests.get") as mock_get:
            mock_res = MagicMock()
            mock_res.status_code = 200
            mock_get.return_value = mock_res

            # -2.9℃
            mock_res.json.return_value = mock_normal
            parsed_n = moat_intelligence._fetch_open_meteo_all_cities()
            tokyo_n = parsed_n.get("tokyo", {})
            self.assertEqual(tokyo_n.get("temp_diff_c"), -2.9)
            self.assertFalse(tokyo_n.get("is_cold_surge"))

            # -3.0℃
            mock_res.json.return_value = mock_surge
            parsed_s = moat_intelligence._fetch_open_meteo_all_cities()
            tokyo_s = parsed_s.get("tokyo", {})
            self.assertEqual(tokyo_s.get("temp_diff_c"), -3.0)
            self.assertTrue(tokyo_s.get("is_cold_surge"))
            self.assertEqual(tokyo_s.get("temp_diff_label"), "急冷")
            self.assertIn("急冷", tokyo_s.get("temp_surge_badge", ""))

    def test_extreme_and_special_temperatures(self):
        """極端な気温（-50℃, +50℃）および小数点丸め精度"""
        mock_payload = [{
            "current": {"temperature_2m": -45.678, "precipitation": 0.0, "weather_code": 0, "wind_speed_10m": 0.0},
            "hourly": {
                "time": ["2026-11-09T20:00"],
                "temperature_2m": [-40.123],
            },
        }] * 10
        with patch("requests.get") as mock_get:
            mock_res = MagicMock()
            mock_res.status_code = 200
            mock_res.json.return_value = mock_payload
            mock_get.return_value = mock_res

            parsed = moat_intelligence._fetch_open_meteo_all_cities()
            tokyo = parsed.get("tokyo", {})
            self.assertEqual(tokyo.get("temp_c"), -45.7)
            self.assertEqual(tokyo.get("temp_prev_c"), -40.1)
            self.assertEqual(tokyo.get("temp_diff_c"), -5.6)
            self.assertTrue(tokyo.get("is_cold_surge"))


class TestAdversarialCityEvents(unittest.TestCase):
    """都市イベント静的マスタに対する異常入力耐性＆外部通信ゼロ検証"""

    def test_static_city_events_date_integrity_and_no_duplicates(self):
        """STATIC_CITY_EVENTS の全日程が実在する有効な日付（YYYY-MM-DD）であり、重複がないこと"""
        events_master = getattr(arena_calendar, "STATIC_CITY_EVENTS", [])
        self.assertGreater(len(events_master), 0)

        for v_info in events_master:
            vid = v_info.get("venue_id")
            seen_dates = set()
            for ev in v_info.get("events", []):
                ds = ev.get("event_date")
                # 実在日付検証
                try:
                    dt = datetime.date.fromisoformat(ds)
                    self.assertIsInstance(dt, datetime.date)
                except ValueError as ex:
                    self.fail(f"Invalid date in static master {vid}: {ds} ({ex})")

                # 重複日検証
                self.assertNotIn(ds, seen_dates, f"Duplicate date for {vid}: {ds}")
                seen_dates.add(ds)

    def test_bizarre_dates(self):
        """極端な日付（過去1900年、未来2100年、閏年2月29日、不正文字列）の入力"""
        bad_dates = [
            "1900-01-01",
            "2100-12-31",
            "2024-02-29",
            "not-a-date",
            "",
            None,
            123456789,
            datetime.datetime(2026, 8, 8, 23, 59, 59),
        ]
        for dt in bad_dates:
            with self.subTest(dt=dt):
                res = arena_calendar.get_arena_events_for_date(dt)
                self.assertIsInstance(res, dict)

    def test_zero_network_communication_enforcement(self):
        """get_arena_events_for_date および detect_city_event_surge が外部ネットワーク接続を一切行わないことの実証"""
        def forbidden_connect(*args, **kwargs):
            raise ConnectionRefusedError("ADVERSARIAL ATTACK: Network communication is strictly prohibited!")

        with patch.object(socket.socket, "connect", side_effect=forbidden_connect):
            # 1. arena_calendar.get_arena_events_for_date（公式確定日程 2026-11-19 第57回明治神宮野球大会）
            evs = arena_calendar.get_arena_events_for_date(datetime.date(2026, 11, 19))
            self.assertIsInstance(evs, dict)
            self.assertIn("meiji_jingu_stadium", evs)

            # 2. moat_intelligence.detect_city_event_surge
            stores = [
                {"name": "OLG 渋谷本店", "city_key": "tokyo", "women": 20, "men": 20},
                {"name": "OLG 大阪駅前", "city_key": "osaka", "women": 25, "men": 25},
            ]
            surges = moat_intelligence.detect_city_event_surge(
                stores, now_jst=datetime.datetime(2026, 11, 10, 21, 15)
            )
            self.assertIsInstance(surges, dict)


class TestAdversarialTemplateRendering(unittest.TestCase):
    """極限入力データ下での Flask テンプレート統合レンダリング耐性"""

    def setUp(self):
        os.environ["DISABLE_SHEETS"] = "1"
        os.environ["DISABLE_SCHEDULER"] = "1"
        import app
        self.app = app.app

    def test_store_card_rendering_with_extreme_turnover_and_cold_surge(self):
        """極限の turnover_momentum および cold_surge を流し込んだ店舗カードレンダリング"""
        extreme_stores = [
            {
                "id": "st-1",
                "name": "OLG 新宿",
                "ssr_title": "OLG 新宿",
                "city_key": "tokyo",
                "women": 50000,
                "men": 50000,
                "usual_w_diff": 0,
                "usual_m_diff": 0,
                "usual_slot_label": "平日",
                "usual_women": 30,
                "usual_men": 30,
                "is_open_now": True,
                "turnover_momentum": {
                    "score": 95,
                    "label": "⚡️高回転",
                    "tone": "fast",
                    "reason": "極限フラックス",
                    "turnover_momentum_index": 95,
                    "turnover_label": "⚡️高回転",
                    "turnover_tone": "fast",
                },
                "weather_brief": {
                    "city": "東京",
                    "condition": "晴れ",
                    "temp_c": -40.0,
                    "temp_diff_c": -50.0,
                    "is_cold_surge": True,
                    "temp_diff_label": "急冷",
                    "temp_surge_badge": "前日比 -50.0℃ 急冷",
                },
            },
            {
                "id": "st-2",
                "name": "JIS SHINJUKU",
                "ssr_title": "JIS SHINJUKU",
                "city_key": "tokyo",
                "women": 0,
                "men": 0,
                "usual_w_diff": 0,
                "usual_m_diff": 0,
                "usual_slot_label": "平日",
                "usual_women": 0,
                "usual_men": 0,
                "is_open_now": False,
                "turnover_momentum": {
                    "score": None,
                    "label": "判定対象外",
                    "tone": "neutral",
                    "turnover_momentum_index": None,
                    "turnover_label": "判定対象外",
                    "turnover_tone": "neutral",
                },
                "weather_brief": {
                    "city": "東京",
                    "condition": "晴れ",
                    "temp_c": None,
                    "temp_diff_c": None,
                    "is_cold_surge": False,
                    "temp_diff_label": "平年並み",
                    "temp_surge_badge": None,
                },
            },
        ]

        client = self.app.test_client()
        mock_payload = {
            "ranking": extreme_stores,
            "status": "success",
            "timestamp": "2026-11-10 21:00",
        }
        with patch("app.get_initial_status_payload", return_value=mock_payload):
            res = client.get("/")
            self.assertEqual(res.status_code, 200)
            html = res.get_data(as_text=True)
            self.assertIn("急冷サージ", html)
            self.assertIn("高回転", html)


if __name__ == "__main__":
    unittest.main()
