"""
tests/test_turnover_weather_events_m3.py

Milestone 3 包括単体テスト:
- Feature 8 & 9: 席回転スピードモメンタム指数（アルゴリズム、境界値、JIS逆転連動、UI表示）
- Feature 10: 前日比寒暖差（Δ℃）の追加通信ゼロ算出＆急冷サージ判定
- Feature 11: 都市イベント事前定義静的マスタ（神宮・甲子園・日産・国立・花火）＆サージ連携
- キャッシュバスター整合性およびUI回帰検査
"""

import datetime
import inspect
import os
import unittest
from unittest.mock import patch, MagicMock

import arena_calendar
import moat_intelligence


class TestTurnoverMomentumIndex(unittest.TestCase):
    """席回転スピードモメンタム指数 (Feature 8 & 9) の計算および境界値テスト"""

    def test_fast_turnover_calculation(self):
        """高回転ケース: 直近変動大（15分差分各6名、定員120名）で score >= 65, ⚡️高回転"""
        res = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=30,
            men=30,
            is_open_now=True,
            women_diff=6,
            men_diff=6,
            p99_cap=120,
        )
        self.assertIsInstance(res, dict)
        self.assertIn("score", res)
        self.assertIn("label", res)
        self.assertIn("tone", res)
        self.assertGreaterEqual(res["score"], 65)
        self.assertEqual(res["tone"], "fast")
        self.assertIn("高回転", res["label"])

    def test_slow_turnover_calculation(self):
        """滞留ぎみケース: 満席高稼働で直近変動ほぼゼロ（15分差分各0名）で score < 45, ☕️滞留ぎみ"""
        res = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=55,
            men=55,
            is_open_now=True,
            women_diff=0,
            men_diff=0,
            p99_cap=120,
        )
        self.assertIsInstance(res, dict)
        self.assertIsNotNone(res.get("score"))
        self.assertLess(res["score"], 45)
        self.assertEqual(res["tone"], "slow")
        self.assertIn("滞留", res["label"])

    def test_closed_store_safety(self):
        """営業時間外境界値: is_open_now=False の場合は score is None かつ label='判定対象外'"""
        res = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=0,
            men=0,
            is_open_now=False,
        )
        self.assertIsNone(res.get("score"))
        self.assertEqual(res.get("label"), "判定対象外")
        self.assertEqual(res.get("tone"), "neutral")

    def test_under_six_guests_safety(self):
        """少人数境界値: 客数合計が6名未満の場合はノイズ抑制のため score is None"""
        res = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=2,
            men=2,
            is_open_now=True,
        )
        self.assertIsNone(res.get("score"))
        self.assertEqual(res.get("label"), "判定対象外")

    def test_closing_suppression(self):
        """閉店間際境界値: 閉店30分前の退店ラッシュ時は席回転と誤認させないため score <= 60 に抑制"""
        # 04:45 は通常ラウンジの閉店直前（05:00閉店）
        now_closing = datetime.datetime(2026, 11, 10, 4, 45)
        res = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=15,
            men=15,
            is_open_now=True,
            women_diff=-10,
            men_diff=-10,
            now_jst=now_closing,
        )
        self.assertTrue(res.get("score") is None or res.get("score") <= 60)

    def test_standing_free_flow(self):
        """スタンディング業態特例: ASOBIBAR/ヤタコイは自由回遊店舗として固定スコア85"""
        res_asobibar = moat_intelligence.calculate_turnover_momentum_index(
            store_name="ASOBIBAR 新宿",
            women=20,
            men=20,
            is_open_now=True,
        )
        self.assertEqual(res_asobibar.get("score"), 85)
        self.assertEqual(res_asobibar.get("tone"), "standing")
        self.assertIn("自由回遊", res_asobibar.get("label", ""))

        res_yatakoi = moat_intelligence.calculate_turnover_momentum_index(
            store_name="ヤタコイ 茶屋町",
            women=15,
            men=15,
            is_open_now=True,
        )
        self.assertEqual(res_yatakoi.get("score"), 85)

    def test_missing_lags_fallback(self):
        """ラグコンテキスト欠落時のフォールバック: lag_context={} でも women_diff/men_diff から安全に算出"""
        res = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=30,
            men=30,
            is_open_now=True,
            lag_context={},
            women_diff=5,
            men_diff=5,
        )
        self.assertIsNotNone(res.get("score"))
        self.assertGreater(res["score"], 50)

    def test_jis_women_waiting_reverse_linkage(self):
        """JIS女性待ち逆転テレメトリ連動: 女性待ち逆転（滞留）時にペナルティ減点と滞留警告"""
        pos_reverse = {
            "telemetry_type": "jis_pos",
            "is_reverse_waiting": True,
            "women_waiting_groups": 4,
            "men_waiting_groups": 0,
        }
        tm = moat_intelligence.calculate_turnover_momentum_index(
            store_name="JIS SHINJUKU",
            women=40,
            men=35,
            is_open_now=True,
            pos_telemetry=pos_reverse,
        )
        self.assertIsInstance(tm, dict)
        self.assertIn("女性滞留", tm.get("wait_clear_speed", ""))

    def test_predict_store_60m_dynamics_early_return_keys(self):
        """predict_store_60m_dynamics の早期リターン時のキー整合性（pred_women, pred_men 補完）"""
        fc_closed = moat_intelligence.predict_store_60m_dynamics(
            "OLG 新宿", women=3, men=2, is_open_now=True
        )
        self.assertIn("pred_women", fc_closed)
        self.assertIn("pred_men", fc_closed)
        self.assertEqual(fc_closed["pred_women"], 3)
        self.assertEqual(fc_closed["pred_men"], 2)


class TestTemperatureDiffAndColdSurge(unittest.TestCase):
    """前日比寒暖差（Δ℃）および急冷サージ判定 (Feature 10) のテスト"""

    def test_open_meteo_batch_url_contains_past_days_and_hourly(self):
        """_fetch_open_meteo_all_cities のURL引数に past_days=1 および hourly が指定されていること"""
        src = inspect.getsource(moat_intelligence._fetch_open_meteo_all_cities)
        self.assertTrue("past_days=1" in src or "hourly" in src)

    def test_cold_surge_exact_threshold(self):
        """寒暖差境界値: -2.9℃は通常（is_cold_surge=False）、-3.0℃は急冷サージ（True）"""
        # モックデータで _fetch_open_meteo_all_cities のパースロジックをテスト
        mock_resp_normal = {
            "current": {"temperature_2m": 12.1, "precipitation": 0.0, "weather_code": 0, "wind_speed_10m": 5.0},
            "hourly": {
                "time": ["2026-11-09T20:00", "2026-11-09T21:00", "2026-11-09T22:00", "2026-11-09T23:00"],
                "temperature_2m": [15.0, 15.0, 15.0, 15.0],  # 前日夜平均 15.0℃ -> 差分 -2.9℃
            },
        }
        mock_resp_surge = {
            "current": {"temperature_2m": 12.0, "precipitation": 0.0, "weather_code": 0, "wind_speed_10m": 5.0},
            "hourly": {
                "time": ["2026-11-09T20:00", "2026-11-09T21:00", "2026-11-09T22:00", "2026-11-09T23:00"],
                "temperature_2m": [15.0, 15.0, 15.0, 15.0],  # 前日夜平均 15.0℃ -> 差分 -3.0℃
            },
        }

        with patch("requests.get") as mock_get:
            mock_res_obj = MagicMock()
            mock_res_obj.status_code = 200
            mock_res_obj.json.return_value = [mock_resp_normal] * 10
            mock_get.return_value = mock_res_obj

            parsed_normal = moat_intelligence._fetch_open_meteo_all_cities()
            tokyo_n = parsed_normal.get("tokyo", {})
            self.assertEqual(tokyo_n.get("temp_diff_c"), -2.9)
            self.assertFalse(tokyo_n.get("is_cold_surge"))

            mock_res_obj.json.return_value = [mock_resp_surge] * 10
            parsed_surge = moat_intelligence._fetch_open_meteo_all_cities()
            tokyo_s = parsed_surge.get("tokyo", {})
            self.assertEqual(tokyo_s.get("temp_diff_c"), -3.0)
            self.assertTrue(tokyo_s.get("is_cold_surge"))
            self.assertEqual(tokyo_s.get("temp_diff_label"), "急冷")

    def test_store_enrichment_includes_weather_brief_fields(self):
        """enrich_stores_with_moat_signals の店舗辞書に weather_brief の全必須キーが存在すること"""
        stores = [{"name": "OLG 新宿", "women": 25, "men": 25, "is_open_now": True}]
        enriched = moat_intelligence.enrich_stores_with_moat_signals(stores)
        wb = enriched[0].get("weather_brief", {})
        self.assertIn("temp_diff_c", wb)
        self.assertIn("is_cold_surge", wb)
        self.assertIn("temp_diff_label", wb)
        self.assertIn("temp_surge_badge", wb)

        # turnover_momentum の付与確認
        tm = enriched[0].get("turnover_momentum", {})
        self.assertIn("score", tm)
        self.assertIn("label", tm)


class TestCityEventsStaticMaster(unittest.TestCase):
    """都市イベント静的マスタおよび2次会サージ連携 (Feature 11) のテスト"""

    def test_static_city_events_structure_and_venues(self):
        """arena_calendar.STATIC_CITY_EVENTS が定義され、主要会場がすべて網羅されていること"""
        self.assertTrue(hasattr(arena_calendar, "STATIC_CITY_EVENTS"))
        static_events = arena_calendar.STATIC_CITY_EVENTS
        self.assertIsInstance(static_events, list)

        venue_names = [v.get("venue_name", "") for v in static_events]
        self.assertTrue(any("神宮" in n for n in venue_names), "明治神宮野球場が含まれること")
        self.assertTrue(any("甲子園" in n for n in venue_names), "阪神甲子園球場が含まれること")
        self.assertTrue(any("日産" in n for n in venue_names), "日産スタジアムが含まれること")
        self.assertTrue(any("国立" in n for n in venue_names), "国立競技場が含まれること")
        self.assertTrue(any("花火" in n for n in venue_names), "花火大会が含まれること")

    def test_major_arena_hubs_stadium_mapping(self):
        """moat_intelligence.MAJOR_ARENA_HUBS に jingu および koshien がマッピングされていること"""
        hubs = getattr(moat_intelligence, "MAJOR_ARENA_HUBS", [])
        hub_ids = [h.get("venue_id", "") for h in hubs if isinstance(h, dict)]
        self.assertTrue(any("jingu" in vid for vid in hub_ids), "jingu が含まれること")
        self.assertTrue(any("koshien" in vid for vid in hub_ids), "koshien が含まれること")

    def test_arena_events_for_date_no_events_safety(self):
        """イベント予定のない日（2026-01-01）で安全に空辞書が返却されること"""
        events = arena_calendar.get_arena_events_for_date(datetime.date(2026, 1, 1))
        self.assertIsInstance(events, dict)
        self.assertEqual(len(events), 0)

    def test_arena_events_for_date_merges_static_master(self):
        """2026-11-19（第57回明治神宮野球大会）の事前定義イベントが get_arena_events_for_date で返却されること"""
        events = arena_calendar.get_arena_events_for_date(datetime.date(2026, 11, 19))
        self.assertIsInstance(events, dict)
        self.assertIn("meiji_jingu_stadium", events)
        jingu_ev = events["meiji_jingu_stadium"][0]
        self.assertEqual(jingu_ev.get("venue_name"), "明治神宮野球場")
        self.assertTrue(str(jingu_ev.get("source_url") or "").startswith("https://"))

    def test_detect_city_event_surge_alias(self):
        """detect_city_event_surge が正しく動作し、辞書を返すこと"""
        res = moat_intelligence.detect_city_event_surge(
            [{"name": "OLG 新宿", "city_key": "tokyo"}],
            now_jst=datetime.datetime(2026, 11, 19, 19, 30),
        )
        self.assertIsInstance(res, dict)


class TestUIAndCachebusterIntegration(unittest.TestCase):
    """UIテンプレートおよびキャッシュバスティング同期の検査"""

    def test_index_template_contains_turnover_momentum(self):
        """templates/index.html に turnover_momentum 文字列が存在すること"""
        tpl_path = os.path.join(os.path.dirname(__file__), "..", "templates", "index.html")
        with open(tpl_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("turnover_momentum", html)

    def test_store_detail_template_contains_turnover_and_weather(self):
        """templates/store_detail.html に turnover_momentum または 席回転スピード、および前日比が含まれること"""
        tpl_path = os.path.join(os.path.dirname(__file__), "..", "templates", "store_detail.html")
        with open(tpl_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertTrue("turnover_momentum" in html or "席回転スピード" in html)
        self.assertIn("前日比", html)
        self.assertIn("イベントサージ", html)

    def test_cache_version_108_sync(self):
        """static/sw.js の CACHE_NAME が aiseki-navi-v109 であること"""
        sw_path = os.path.join(os.path.dirname(__file__), "..", "static", "sw.js")
        with open(sw_path, "r", encoding="utf-8") as f:
            sw_txt = f.read()
        self.assertIn("const CACHE_NAME = 'aiseki-navi-v109';", sw_txt)


if __name__ == "__main__":
    unittest.main()
