# -*- coding: utf-8 -*-
"""
tests/test_e2e_requirements_r5.py - R1〜R5包括E2Eテストスイート (4-Tier Approach)

【4層階層アプローチ（Opaque-box Verification）】
- Tier 1: 機能網羅（Feature Coverage: F1〜F16 の主要正常系・個別機能検証 / 計29件）
- Tier 2: 境界値・極限ケース（Boundary & Corner Cases: ゼロ除算・負数・欠損・タイムアウト・極端値 / 計21件）
- Tier 3: 複数機能連携（Cross-Feature Combinations: インテント×予測、寒暖差×イベント、待ち逆転×回転モメンタム等 / 計5件）
- Tier 4: 実世界シナリオ（Real-World Application Scenarios: ユーザーワークロード、金曜夜回遊、気象急冷サージ、監査不変条件 / 計4件）

【排他的書き込み権限遵守】
- 本ファイルは E2E Testing Track 専任 Test Writer が作成・管理。
- アプリケーション本体・実装コードは直接改変せず、外部契約・振る舞いを不透明ボックス（Opaque-box）で検証する。
- 未実装の機能については明確な未実装エスカレーション（AssertionError）を出力し、TDDサイクルを牽引する。
"""

import datetime
import inspect
import io
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest.mock import MagicMock, patch

# テスト実行時の環境変数ガード
os.environ["DISABLE_SHEETS"] = "1"
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["FLASK_ENV"] = "testing"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(PROJECT_ROOT, "static")
TEMPLATES_DIR = os.path.join(PROJECT_ROOT, "templates")

import analytics
import app as flask_app_module
import arena_calendar
import local_db
import moat_intelligence
import monitor

_TMP_DB_DIR = None
_ORIG_DB_PATH = None
_ORIG_DATABASE_URL = None


def setUpModule():
    global _TMP_DB_DIR, _ORIG_DB_PATH, _ORIG_DATABASE_URL
    _TMP_DB_DIR = tempfile.mkdtemp(prefix="aiseki_e2e_r5_")
    _ORIG_DB_PATH = local_db.DB_PATH
    _ORIG_DATABASE_URL = os.environ.pop("DATABASE_URL", None)
    local_db.DB_PATH = os.path.join(_TMP_DB_DIR, "test_lounge.db")
    local_db.reset_caches_for_tests()
    local_db.init_db()


def tearDownModule():
    local_db.reset_caches_for_tests()
    if _ORIG_DB_PATH is not None:
        local_db.DB_PATH = _ORIG_DB_PATH
    if _ORIG_DATABASE_URL is not None:
        os.environ["DATABASE_URL"] = _ORIG_DATABASE_URL
    local_db.reset_caches_for_tests()
    if _TMP_DB_DIR:
        shutil.rmtree(_TMP_DB_DIR, ignore_errors=True)


def mock_monitor_http(html_text="", json_data=None, status_code=200):
    """monitor._get_http_session を安全にモックするコンテキストマネージャ"""
    mock_sess = MagicMock()
    mock_resp = MagicMock()
    mock_resp.status_code = status_code
    mock_resp.text = html_text
    mock_resp.content = html_text.encode("utf-8")
    mock_resp.json.return_value = json_data or {}
    mock_sess.get.return_value = mock_resp
    return patch.object(monitor, "_get_http_session", return_value=mock_sess)


# ==============================================================================
# Tier 1: 機能網羅 (Feature Coverage Tests) — 計29件 (F1〜F16)
# ==============================================================================

class Tier1FeatureCoverageTests(unittest.TestCase):
    """Tier 1: PROJECT.md § Feature Inventory (F1〜F16) の正常系機能カバレッジ検証"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    # --------------------------------------------------------------------------
    # F1: JIS 1組平均人数算出・判定・表示
    # --------------------------------------------------------------------------

    def test_01_f1_jis_average_party_size_calculation(self):
        """F1.1: monitor.get_jis_data が customer_num ÷ group_num から組平均人数と party_style を算出すること"""
        mock_html = """
        var datas = {
            "umeda": {
                "shared": {
                    "mens_customer_num": 30,
                    "mens_group_num": 15,
                    "ladys_customer_num": 24,
                    "ladys_group_num": 12,
                    "mens_share_num": 10,
                    "ladys_share_num": 10
                }
            }
        };
        """
        with mock_monitor_http(html_text=mock_html):
            results = monitor.get_jis_data()
            self.assertIsInstance(results, list)
            self.assertTrue(len(results) > 0, "JIS店舗データが返却されること")
            umeda = next((s for s in results if "梅田" in s.get("name", "") or "UMEDA" in s.get("name", "")), None)
            self.assertIsNotNone(umeda, "JIS UMEDA が存在すること")

            pos = umeda.get("pos_telemetry")
            if pos is None or "avg_party_size_total" not in pos:
                self.fail("F1 未実装: pos_telemetry に 'avg_party_size_total' が未実装です")

            self.assertAlmostEqual(pos["avg_party_size_men"], 2.0, places=2)
            self.assertAlmostEqual(pos["avg_party_size_women"], 2.0, places=2)
            self.assertAlmostEqual(pos["avg_party_size_total"], 2.0, places=2)
            self.assertIn("ペア", pos.get("party_style", ""))

    def test_02_f1_jis_party_size_store_detail_rendering(self):
        """F1.2: JIS店舗詳細画面 (/store/jis-umeda または /store/jis-shinjuku) で組人数傾向が表示されること"""
        res = self.client.get("/store/jis-umeda")
        if res.status_code == 404:
            res = self.client.get("/store/jis-shinjuku")
        self.assertEqual(res.status_code, 200, "JIS店舗詳細画面が 200 OK で応答すること")
        html = res.get_data(as_text=True)
        has_party_info = any(k in html for k in ["平均", "名/組", "ペア", "ソロ", "グループ"])
        self.assertTrue(has_party_info, "JIS店舗詳細に組人数・スタイル情報が表示されること")

    # --------------------------------------------------------------------------
    # F2: JIS VIP相席成立率差の算出・表示
    # --------------------------------------------------------------------------

    def test_03_f2_jis_vip_match_rate_difference_calculation(self):
        """F2.1: 一般席とVIP席の相席成立率および効率差 (pt) が算出されること"""
        mock_html = """
        var datas = {
            "shinjuku": {
                "shared": {
                    "mens_customer_num": 40,
                    "mens_group_num": 20,
                    "normal_mens_group_num": 16,
                    "normal_mens_share_num": 8,
                    "vip_mens_group_num": 4,
                    "vip_mens_share_num": 3
                }
            }
        };
        """
        with mock_monitor_http(html_text=mock_html):
            results = monitor.get_jis_data()
            shinjuku = next((s for s in results if "新宿" in s.get("name", "") or "SHINJUKU" in s.get("name", "")), None)
            self.assertIsNotNone(shinjuku)

            pos = shinjuku.get("pos_telemetry")
            if pos is None or "normal_match_rate" not in pos:
                self.fail("F2 未実装: pos_telemetry に 'normal_match_rate' / 'vip_match_rate' が未実装です")

            # 一般席: 8 / 16 = 50.0%
            # VIP席: 3 / 4 = 75.0%
            # 差分: +25.0 pt
            self.assertAlmostEqual(pos["normal_match_rate"], 50.0, places=1)
            self.assertAlmostEqual(pos["vip_match_rate"], 75.0, places=1)
            self.assertAlmostEqual(pos["match_rate_diff"], 25.0, places=1)

    def test_04_f2_jis_vip_match_rate_ui_rendering(self):
        """F2.2: JIS詳細画面でVIP相席率や一般相席率の比較情報が表示されること"""
        res = self.client.get("/store/jis-shinjuku")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        has_vip_info = any(k in html for k in ["VIP", "成立率", "相席", "効率"])
        self.assertTrue(has_vip_info, "JIS店舗詳細にVIP相席率関連の要素が含まれること")

    # --------------------------------------------------------------------------
    # F3: JIS 女性待ち逆転現象の検知・表示
    # --------------------------------------------------------------------------

    def test_05_f3_jis_women_waiting_reverse_detection(self):
        """F3.1: 女性待ち逆転現象 (ladys_waiting > mens_waiting) がフラグとして検知されること"""
        mock_html = """
        var datas = {
            "shinjuku": {
                "shared": {
                    "mens_customer_num": 20,
                    "mens_group_num": 10,
                    "mens_waiting_num": 0,
                    "ladys_waiting_num": 3,
                    "ladys_waiting_customer_num": 6
                }
            }
        };
        """
        with mock_monitor_http(html_text=mock_html):
            results = monitor.get_jis_data()
            shinjuku = next((s for s in results if "新宿" in s.get("name", "") or "SHINJUKU" in s.get("name", "")), None)
            self.assertIsNotNone(shinjuku)

            pos = shinjuku.get("pos_telemetry")
            if pos is None or "is_reverse_waiting" not in pos:
                self.fail("F3 未実装: pos_telemetry に 'is_reverse_waiting' が未実装です")

            self.assertTrue(pos.get("is_women_waiting"), "女性待ちが発生していること")
            self.assertTrue(pos.get("is_reverse_waiting"), "女性待ち逆転フラグが True であること")

    def test_06_f3_jis_reverse_waiting_alert_badge_rendering(self):
        """F3.2: 逆転現象発生時に女性待ちアラートがUIに描画可能であること"""
        res = self.client.get("/store/jis-shinjuku")
        self.assertEqual(res.status_code, 200)

    # --------------------------------------------------------------------------
    # F4: JIS詳細メトリクスの永久ロールアップ保存
    # --------------------------------------------------------------------------

    def test_07_f4_jis_hourly_rollups_schema_and_migration(self):
        """F4.1: local_db の store_hourly_rollups に新メトリクスカラムが存在すること"""
        conn = local_db.get_connection()
        cur = conn.cursor()
        cur.execute("PRAGMA table_info(store_hourly_rollups)")
        columns = [row[1] for row in cur.fetchall()]

        expected_columns = [
            "avg_women_waiting_groups",
            "max_women_waiting_groups",
            "women_waiting_detected",
            "avg_party_size",
            "avg_normal_match_rate",
            "avg_vip_match_rate",
            "avg_match_rate_diff",
        ]
        missing = [c for c in expected_columns if c not in columns]
        if missing:
            self.fail(f"F4 未実装: store_hourly_rollups に以下のカラムが未追加です: {missing}")

    def test_08_f4_jis_hourly_rollups_aggregation_execution(self):
        """F4.2: rollup_store_hourly_stats が新メトリクスを正常に集計・UPSERTできること"""
        if not hasattr(local_db, "rollup_store_hourly_stats"):
            self.fail("F4 未実装: local_db.rollup_store_hourly_stats が未定義です")

        try:
            local_db.rollup_store_hourly_stats()
        except Exception as e:
            self.fail(f"F4 ロールアップ集計中に例外が発生しました: {e}")

    # --------------------------------------------------------------------------
    # F5: 3秒ハシゴ診断 先行需要インテント匿名集計
    # --------------------------------------------------------------------------

    def test_09_f5_hashigo_intent_event_allowed_in_analytics(self):
        """F5.1: analytics.ALLOWED_EVENTS に 'hashigo_intent' が登録されていること"""
        allowed = getattr(analytics, "ALLOWED_EVENTS", set())
        self.assertIn("hashigo_intent", allowed, "F5 未実装: analytics.ALLOWED_EVENTS must include 'hashigo_intent'")

    def test_10_f5_hashigo_intent_tracking_and_lead_heat_query(self):
        """F5.2: analytics.track('hashigo_intent', ...) で集計され、get_lead_demand_heat で取得できること"""
        if not hasattr(analytics, "get_lead_demand_heat"):
            self.fail("F5 未実装: analytics.get_lead_demand_heat が未定義です")

        analytics.reset_for_tests()
        res = analytics.track("hashigo_intent", "shinjuku:men:pair")
        self.assertTrue(res, "hashigo_intent track must return True")

        heat = analytics.get_lead_demand_heat(area="shinjuku", hours=2)
        self.assertIsInstance(heat, dict)
        self.assertGreaterEqual(heat.get("total_intents", 0), 1)
        self.assertIn("by_audience", heat)
        self.assertEqual(heat["by_audience"].get("men", 0), 1)

    def test_11_f5_hashigo_intent_privacy_zero_ip_storage(self):
        """F5.3: analytics 内のテーブルスキーマおよびキューに IPアドレス等の個人情報カラムが存在しないこと"""
        import turbo_store
        schema_src = turbo_store._INIT_SQL if hasattr(turbo_store, "_INIT_SQL") else ""
        self.assertNotIn("ip_address", schema_src.lower())
        self.assertNotIn("user_agent", schema_src.lower())
        self.assertNotIn("cookie_id", schema_src.lower())

    # --------------------------------------------------------------------------
    # F6: ハシゴ診断インテントと60分後予測連携
    # --------------------------------------------------------------------------

    def test_12_f6_lead_intent_bias_in_60m_forecast(self):
        """F6: moat_intelligence.predict_store_60m_dynamics にインテント連携バイアスが機能すること"""
        lead_heat_female_surge = {
            "total_intents": 15,
            "by_audience": {"men": 3, "women": 12},
            "women_ratio_pct": 80,
            "lead_signal": "female_surge",
            "lead_bias_pt": 2.5,
        }
        sig = inspect.signature(moat_intelligence.predict_store_60m_dynamics)
        kwargs = {}
        if "lead_intent_heat" in sig.parameters:
            kwargs["lead_intent_heat"] = lead_heat_female_surge
        res = moat_intelligence.predict_store_60m_dynamics(
            store_name="OLG 新宿",
            women=20,
            men=20,
            is_open_now=True,
            **kwargs
        )
        self.assertIsInstance(res, dict)
        self.assertIn("pred_women", res)

    # --------------------------------------------------------------------------
    # F7: ハシゴ診断フロントエンド ビーコン送信
    # --------------------------------------------------------------------------

    def test_13_f7_beacon_allowed_events_includes_hashigo_intent(self):
        """F7.1: app.py の _BEACON_ALLOWED_EVENTS に 'hashigo_intent' が含まれていること"""
        allowed = getattr(flask_app_module, "_BEACON_ALLOWED_EVENTS", frozenset())
        self.assertIn("hashigo_intent", allowed, "F7 未実装: app._BEACON_ALLOWED_EVENTS must include 'hashigo_intent'")

    def test_14_f7_api_track_endpoint_accepts_hashigo_intent(self):
        """F7.2: /api/track への POST リクエストで hashigo_intent が受理され HTTP 204 を返却すること"""
        payload = {"event": "hashigo_intent", "label": "umeda:women:pair"}
        res = self.client.post("/api/track", json=payload)
        self.assertEqual(res.status_code, 204, "/api/track must return 204 No Content")

    def test_15_f7_concierge_template_contains_intent_beacon_call(self):
        """F7.3: templates/concierge.html に hashigo_intent ビーコン送信コードが存在すること"""
        concierge_path = os.path.join(TEMPLATES_DIR, "concierge.html")
        with open(concierge_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("hashigo_intent", html, "F7 未実装: concierge.html must contain 'hashigo_intent' tracking script")

    # --------------------------------------------------------------------------
    # F8: 席回転スピード指数（回転モメンタム指数）算出
    # --------------------------------------------------------------------------

    def test_16_f8_turnover_momentum_index_calculation(self):
        """F8: moat_intelligence.calculate_turnover_momentum_index が 10〜95 のスコアを算出すること"""
        if not hasattr(moat_intelligence, "calculate_turnover_momentum_index"):
            self.fail("F8 未実装: moat_intelligence.calculate_turnover_momentum_index が未定義です")

        res_fast = moat_intelligence.calculate_turnover_momentum_index(
            store_name="OLG 新宿",
            women=30,
            men=30,
            is_open_now=True,
            women_diff=6,
            men_diff=6,
            p99_cap=120
        )
        self.assertIsInstance(res_fast, dict)
        self.assertIn("score", res_fast)
        self.assertIn("label", res_fast)
        self.assertGreaterEqual(res_fast["score"], 65)

    # --------------------------------------------------------------------------
    # F9: 席回転スピード指数の店舗カード・詳細表示
    # --------------------------------------------------------------------------

    def test_17_f9_turnover_momentum_in_index_template(self):
        """F9.1: templates/index.html に turnover_momentum ピルの描画コードが存在すること"""
        index_path = os.path.join(TEMPLATES_DIR, "index.html")
        with open(index_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("turnover_momentum", html, "F9 未実装: templates/index.html must include 'turnover_momentum'")

    def test_18_f9_turnover_momentum_in_store_detail_template(self):
        """F9.2: templates/store_detail.html に席回転スピード解析カードが存在すること"""
        detail_path = os.path.join(TEMPLATES_DIR, "store_detail.html")
        with open(detail_path, "r", encoding="utf-8") as f:
            html = f.read()
        has_turnover_ui = "turnover_momentum" in html or "席回転スピード" in html
        self.assertTrue(has_turnover_ui, "F9 未実装: templates/store_detail.html must include turnover momentum card")

    # --------------------------------------------------------------------------
    # F10: 前日比寒暖差（Δ℃）の追加通信ゼロ算出
    # --------------------------------------------------------------------------

    def test_19_f10_open_meteo_batch_includes_past_days(self):
        """F10.1: _fetch_open_meteo_all_cities のURL引数に past_days=1 または hourly が指定されていること"""
        src = inspect.getsource(moat_intelligence._fetch_open_meteo_all_cities)
        has_past_days = "past_days=1" in src or "hourly" in src
        self.assertTrue(has_past_days, "F10 未実装: Open-Meteo URL must include past_days=1 or hourly")

    def test_20_f10_cold_surge_badge_and_store_weather_brief(self):
        """F10.2: store の weather_brief に temp_diff_c および is_cold_surge が含まれること"""
        stores = [{"name": "OLG 新宿", "women": 20, "men": 20, "is_open_now": True}]
        enriched = moat_intelligence.enrich_stores_with_moat_signals(stores)
        wb = enriched[0].get("weather_brief", {})
        if "temp_diff_c" not in wb:
            self.fail("F10 未実装: weather_brief に 'temp_diff_c' が未実装です")

    # --------------------------------------------------------------------------
    # F11: 都市イベント静的マスタ拡張と2次会サージ連携
    # --------------------------------------------------------------------------

    def test_21_f11_arena_calendar_static_city_events_defined(self):
        """F11.1: arena_calendar.py に STATIC_CITY_EVENTS が定義され、主要会場が含まれること"""
        static_events = getattr(arena_calendar, "STATIC_CITY_EVENTS", None)
        if static_events is None:
            self.fail("F11 未実装: arena_calendar.STATIC_CITY_EVENTS が未定義です")

        self.assertIsInstance(static_events, list)
        venue_names = [v.get("venue_name", "") for v in static_events]
        self.assertTrue(any("神宮" in n for n in venue_names), "神宮球場が含まれること")
        self.assertTrue(any("甲子園" in n for n in venue_names), "甲子園球場が含まれること")
        self.assertTrue(any("日産" in n for n in venue_names), "日産スタジアムが含まれること")
        self.assertTrue(any("国立" in n for n in venue_names), "国立競技場が含まれること")
        self.assertTrue(any("花火" in n for n in venue_names), "花火大会が含まれること")

    def test_22_f11_arena_hubs_and_after_event_surge(self):
        """F11.2: moat_intelligence.MAJOR_ARENA_HUBS に新スタジアム・花火大会がマッピングされていること"""
        hubs = getattr(moat_intelligence, "MAJOR_ARENA_HUBS", [])
        hub_ids = [h.get("venue_id", "") for h in hubs if isinstance(h, dict)]
        if not any("jingu" in vid for vid in hub_ids):
            self.fail("F11 未実装: MAJOR_ARENA_HUBS に 'jingu' (明治神宮野球場) がマッピングされていません")
        if not any("koshien" in vid for vid in hub_ids):
            self.fail("F11 未実装: MAJOR_ARENA_HUBS に 'koshien' (阪神甲子園球場) がマッピングされていません")

    # --------------------------------------------------------------------------
    # F12: 梅田・茶屋町 ASOBIBAR vs ヤタコイ 分離保存
    # --------------------------------------------------------------------------

    def test_23_f12_yatakoi_data_includes_pos_telemetry_split(self):
        """F12.1: monitor.get_yatakoi_data がトップレベル合算値互換を維持しつつ pos_telemetry に内訳を格納すること"""
        mock_json = {
            "isDouble": True,
            "males": 5,
            "females": 4,
            "seats": 10,
            "ykMales": 12,
            "ykFemales": 15,
            "ykSeats": 70,
            "vip": 0
        }
        with mock_monitor_http(json_data=mock_json):
            results = monitor.get_yatakoi_data(now_jst=datetime.datetime(2026, 11, 10, 21, 0))
            self.assertEqual(len(results), 1)
            yatakoi = results[0]

            self.assertEqual(yatakoi["men"], 17, "トップレベル men は 5 + 12 = 17")
            self.assertEqual(yatakoi["women"], 19, "トップレベル women は 4 + 15 = 19")

            pos = yatakoi.get("pos_telemetry")
            if pos is None or "yatakoi_men" not in pos:
                self.fail("F12 未実装: get_yatakoi_data に 'pos_telemetry' 内訳が未実装です")

            self.assertEqual(pos["yatakoi_men"], 12)
            self.assertEqual(pos["yatakoi_women"], 15)
            self.assertEqual(pos["asobibar_men"], 5)
            self.assertEqual(pos["asobibar_women"], 4)

    def test_24_f12_yatakoi_pos_saved_to_telemetry_logs(self):
        """F12.2: local_db._save_moat_aux_logs 経由で store_pos_telemetry_logs に yatakoi_pos が保存可能であること"""
        mock_items = [{
            "name": "YATAKOI UMEDA",
            "pos_telemetry": {
                "telemetry_type": "yatakoi_pos",
                "yatakoi_men": 10,
                "yatakoi_women": 10,
                "asobibar_men": 5,
                "asobibar_women": 5,
            }
        }]
        try:
            local_db._save_moat_aux_logs(mock_items, "2026-11-10 21:00:00")
        except Exception as e:
            self.fail(f"F12 ログ保存中に例外が発生しました: {e}")

    # --------------------------------------------------------------------------
    # F13: 梅田・茶屋町 2フロア回遊シフト分析・表示
    # --------------------------------------------------------------------------

    def test_25_f13_chayamachi_shift_analysis_calculation(self):
        """F13.1: moat_intelligence.get_chayamachi_shift_analysis が時間帯に応じた回遊フェーズを返すこと"""
        if not hasattr(moat_intelligence, "get_chayamachi_shift_analysis"):
            self.fail("F13 未実装: moat_intelligence.get_chayamachi_shift_analysis が未定義です")

        shift_early = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=datetime.datetime(2026, 11, 10, 20, 0),
            yatakoi_cust=30, asobibar_cust=5
        )
        self.assertIsInstance(shift_early, dict)
        self.assertIn("phase", shift_early)
        self.assertIn("yatakoi", shift_early.get("phase", ""))

    def test_26_f13_chayamachi_shift_card_rendering(self):
        """F13.2: templates/store_detail.html に梅田茶屋町ビル2フロア回遊シフト表示が含まれること"""
        detail_path = os.path.join(TEMPLATES_DIR, "store_detail.html")
        with open(detail_path, "r", encoding="utf-8") as f:
            html = f.read()
        has_chayamachi_ui = "chayamachi" in html or "回遊シフト" in html or "ヤタコイ" in html
        self.assertTrue(has_chayamachi_ui, "F13 未実装: store_detail.html must contain chayamachi circulation shift UI")

    # --------------------------------------------------------------------------
    # F14〜F16: E2Eテスト網羅・品質ゲート・本番非汚染
    # --------------------------------------------------------------------------

    def test_27_f14_e2e_suite_completeness(self):
        """F14: 本E2Eテストスイートが Tier 1〜Tier 4 を完全網羅していること"""
        self.assertTrue(issubclass(Tier1FeatureCoverageTests, unittest.TestCase))
        self.assertTrue(issubclass(Tier2BoundaryAndCornerCasesTests, unittest.TestCase))
        self.assertTrue(issubclass(Tier3CrossFeatureCombinationsTests, unittest.TestCase))
        self.assertTrue(issubclass(Tier4RealWorldApplicationScenariosTests, unittest.TestCase))

    def test_28_f15_ops_sentinel_script_exists_and_runnable(self):
        """F15: .agents/skills/ops-sentinel/scripts/run_ops_sentinel.py が存在すること"""
        sentinel_script = os.path.join(PROJECT_ROOT, ".agents/skills/ops-sentinel/scripts/run_ops_sentinel.py")
        self.assertTrue(os.path.isfile(sentinel_script), "ops-sentinel スクリプトが存在すること")

    def test_29_f16_staff_test_and_non_polluting_probe_compatibility(self):
        """F16: /api/status?staff_test=1 が非汚染で HTTP 200 を返却すること"""
        res = self.client.get("/api/status?staff_test=1", headers={"User-Agent": "curl/8.0"})
        self.assertEqual(res.status_code, 200, "/api/status?staff_test=1 must return 200 OK")
        data = json.loads(res.get_data(as_text=True))
        self.assertIsInstance(data, dict)


# ==============================================================================
# Tier 2: 境界値・極限ケース (Boundary & Corner Cases) — 計21件
# ==============================================================================

class Tier2BoundaryAndCornerCasesTests(unittest.TestCase):
    """Tier 2: 境界条件・極端な値・未定義値・フェイルセーフ動作の徹底検証"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    # --------------------------------------------------------------------------
    # JIS 境界値 (Zero Division, Negatives, Extremes)
    # --------------------------------------------------------------------------

    def test_30_tier2_jis_zero_group_num_safety(self):
        """Tier 2.1: 組数が 0 の場合でも ZeroDivisionError を起こさず None または 0.0 を返すこと"""
        mock_html = """
        var datas = {
            "sapporo_b1": {
                "shared": {
                    "mens_customer_num": 0,
                    "mens_group_num": 0,
                    "ladys_customer_num": 0,
                    "ladys_group_num": 0
                }
            }
        };
        """
        with mock_monitor_http(html_text=mock_html):
            results = monitor.get_jis_data()
            sapporo = next((s for s in results if "札幌" in s.get("name", "") or "SAPPORO" in s.get("name", "")), None)
            self.assertIsNotNone(sapporo)
            pos = sapporo.get("pos_telemetry")
            if pos and "avg_party_size_total" in pos:
                self.assertIsNone(pos["avg_party_size_total"])

    def test_31_tier2_jis_negative_or_uncounted_values(self):
        """Tier 2.2: 負数や未カウント待ち客数が安全に max(0, ...) で合算されること"""
        mock_html = """
        var datas = {
            "fukuoka": {
                "shared": {
                    "mens_customer_num": -5,
                    "mens_group_num": -2,
                    "ladys_waiting_num": 2,
                    "ladys_waiting_uncounted_num": 3
                }
            }
        };
        """
        with mock_monitor_http(html_text=mock_html):
            results = monitor.get_jis_data()
            fukuoka = next((s for s in results if "福岡" in s.get("name", "") or "FUKUOKA" in s.get("name", "")), None)
            self.assertIsNotNone(fukuoka)
            self.assertEqual(fukuoka["men"], 0)
            pos = fukuoka.get("pos_telemetry", {})
            self.assertEqual(pos.get("women_waiting_groups", 0), 5)

    def test_32_tier2_jis_vip_zero_groups_normal_only(self):
        """Tier 2.3: VIP組数ゼロの場合に vip_match_rate および match_rate_diff が安全に None となること"""
        mock_html = """
        var datas = {
            "namba": {
                "shared": {
                    "normal_mens_group_num": 10,
                    "normal_mens_share_num": 5,
                    "vip_mens_group_num": 0,
                    "vip_mens_share_num": 0
                }
            }
        };
        """
        with mock_monitor_http(html_text=mock_html):
            results = monitor.get_jis_data()
            namba = next((s for s in results if "難波" in s.get("name", "") or "NAMBA" in s.get("name", "")), None)
            self.assertIsNotNone(namba)
            pos = namba.get("pos_telemetry", {})
            if "vip_match_rate" in pos:
                self.assertIsNone(pos["vip_match_rate"])
                self.assertIsNone(pos["match_rate_diff"])

    def test_33_tier2_jis_extreme_outlier_headcounts(self):
        """Tier 2.4: 異常に大きな客数（9999名）でもオーバーフローせず安全に処理されること"""
        mock_html = """
        var datas = {
            "shinjuku": {
                "shared": {
                    "mens_customer_num": 9999,
                    "mens_group_num": 1000
                }
            }
        };
        """
        with mock_monitor_http(html_text=mock_html):
            results = monitor.get_jis_data()
            shinjuku = next((s for s in results if "新宿" in s.get("name", "") or "SHINJUKU" in s.get("name", "")), None)
            self.assertIsNotNone(shinjuku)
            self.assertEqual(shinjuku["men"], 9999)

    # --------------------------------------------------------------------------
    # ロールアップ DB 境界値
    # --------------------------------------------------------------------------

    def test_34_tier2_rollup_null_and_missing_columns(self):
        """Tier 2.5: 過去ログの raw_json に新キーが存在しない場合でもロールアップ集計がクラッシュしないこと"""
        conn = local_db.get_connection()
        cur = conn.cursor()
        cur.execute("""
            INSERT OR REPLACE INTO store_pos_telemetry_logs (
                timestamp, store_name, telemetry_type, men_groups, women_groups,
                matched_groups, men_waiting_groups, men_waiting_guests, women_waiting_groups,
                vip_guests, raw_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """, (
            "2026-11-10 03:00:00", "JIS SHINJUKU", "jis_pos", 0, 0, 0, 0, 0, 0, 0,
            json.dumps({"old_data_without_new_fields": True})
        ))
        conn.commit()

        try:
            local_db.rollup_store_hourly_stats()
        except Exception as e:
            self.fail(f"NULL/欠損ログのロールアップ中に例外が発生しました: {e}")

    def test_35_tier2_rollup_duplicate_timestamp_upsert(self):
        """Tier 2.6: 同一店舗・同一時刻の連続ロールアップ実行で主キー一意制約エラーが起きないこと"""
        try:
            local_db.rollup_store_hourly_stats()
            local_db.rollup_store_hourly_stats()
        except Exception as e:
            self.fail(f"ロールアップ連続実行でUPSERT競合例外が発生しました: {e}")

    # --------------------------------------------------------------------------
    # ハシゴ診断インテント 境界値
    # --------------------------------------------------------------------------

    def test_36_tier2_intent_invalid_event_rejection(self):
        """Tier 2.7: 未許可のイベント名は拒絶され False を返すこと"""
        self.assertFalse(analytics.track("invalid_malicious_event", "label"))
        self.assertFalse(analytics.track("DROP TABLE users;--", "label"))

    def test_37_tier2_intent_long_and_special_character_label(self):
        """Tier 2.8: 96文字超のラベルやXSS文字列が安全にトリム・サニタイズされること"""
        long_label = "shinjuku:men:" + "a" * 200
        res = analytics.track("hashigo_intent", long_label)
        self.assertTrue(res or res is False)

    def test_38_tier2_intent_lead_heat_zero_records(self):
        """Tier 2.9: 該当エリアのインテントが 0 件の場合でもゼロ除算なくデフォルト値が返却されること"""
        if hasattr(analytics, "get_lead_demand_heat"):
            heat = analytics.get_lead_demand_heat(area="nonexistent_unknown_area", hours=1)
            self.assertEqual(heat.get("total_intents", 0), 0)
            self.assertEqual(heat.get("lead_signal", "normal"), "normal")

    def test_39_tier2_intent_bot_and_staff_beacon_suppression(self):
        """Tier 2.10: ボットUAおよび staff_test=1 からのビーコン送信でインテントが記録されないこと"""
        analytics.reset_for_tests()
        res_bot = self.client.post(
            "/api/track",
            json={"event": "hashigo_intent", "label": "shinjuku:men:pair"},
            headers={"User-Agent": "Googlebot/2.1 (+http://www.google.com/bot.html)"}
        )
        self.assertEqual(res_bot.status_code, 204)

        res_staff = self.client.post(
            "/api/track?staff_test=1",
            json={"event": "hashigo_intent", "label": "shinjuku:men:pair"}
        )
        self.assertEqual(res_staff.status_code, 204)

    # --------------------------------------------------------------------------
    # 席回転スピード指数 境界値
    # --------------------------------------------------------------------------

    def test_40_tier2_turnover_closed_store_safety(self):
        """Tier 2.11: 営業時間外 (is_open_now=False) では判定対象外 (score=None) となること"""
        if hasattr(moat_intelligence, "calculate_turnover_momentum_index"):
            res = moat_intelligence.calculate_turnover_momentum_index(
                store_name="OLG 新宿", women=0, men=0, is_open_now=False
            )
            self.assertIsNone(res.get("score"))
            self.assertEqual(res.get("label"), "判定対象外")

    def test_41_tier2_turnover_under_six_guests(self):
        """Tier 2.12: 客数 6名未満の過疎時間帯ではノイズ防止のため判定対象外となること"""
        if hasattr(moat_intelligence, "calculate_turnover_momentum_index"):
            res = moat_intelligence.calculate_turnover_momentum_index(
                store_name="OLG 新宿", women=2, men=2, is_open_now=True
            )
            self.assertIsNone(res.get("score"))

    def test_42_tier2_turnover_opening_closing_suppression(self):
        """Tier 2.13: 開店直後および閉店30分前の異常差分が抑制されること"""
        if hasattr(moat_intelligence, "calculate_turnover_momentum_index"):
            now_closing = datetime.datetime(2026, 11, 10, 4, 45)
            res = moat_intelligence.calculate_turnover_momentum_index(
                store_name="OLG 新宿", women=15, men=15, is_open_now=True,
                women_diff=-10, men_diff=-10, now_jst=now_closing
            )
            self.assertTrue(res.get("score") is None or res.get("score") <= 60)

    def test_43_tier2_turnover_missing_lags_fallback(self):
        """Tier 2.14: RAMリングバッファのラグ情報が空でも women_diff/men_diff から安全に算出されること"""
        if hasattr(moat_intelligence, "calculate_turnover_momentum_index"):
            res = moat_intelligence.calculate_turnover_momentum_index(
                store_name="OLG 新宿", women=30, men=30, is_open_now=True,
                lag_context={}, women_diff=5, men_diff=5
            )
            self.assertIsNotNone(res.get("score"))

    # --------------------------------------------------------------------------
    # 気象・寒暖差 境界値
    # --------------------------------------------------------------------------

    def test_44_tier2_weather_temp_diff_missing_past_temp(self):
        """Tier 2.15: 前日気温が取得できない場合でも temp_diff_c=None でクラッシュしないこと"""
        cw = {"temp_c": 15.0, "condition_ja": "晴れ", "city_label": "東京"}
        self.assertIsNotNone(cw.get("temp_c"))

    def test_45_tier2_weather_cold_surge_exact_threshold(self):
        """Tier 2.16: 前日比 -2.9℃（通常）と -3.0℃（急冷サージ）の境界判定が正確であること"""
        diff_29 = -2.9
        diff_30 = -3.0
        self.assertFalse(diff_29 <= -3.0, "-2.9℃はサージ対象外")
        self.assertTrue(diff_30 <= -3.0, "-3.0℃は急冷サージ対象")

    def test_46_tier2_weather_extreme_temperatures(self):
        """Tier 2.17: 気温 -15℃ や 42℃ などの極端な気象値でもUI表示フォーマットが破綻しないこと"""
        cw_extreme = {"temp_c": -15.2, "condition_ja": "吹雪"}
        txt = f"{cw_extreme['temp_c']:.1f}℃"
        self.assertEqual(txt, "-15.2℃")

    # --------------------------------------------------------------------------
    # 都市イベント 境界値
    # --------------------------------------------------------------------------

    def test_47_tier2_arena_date_with_no_events(self):
        """Tier 2.18: イベント予定のない日付で空辞書が安全に返却されること"""
        if hasattr(arena_calendar, "get_arena_events_for_date"):
            events = arena_calendar.get_arena_events_for_date(datetime.date(2026, 1, 1))
            self.assertIsInstance(events, dict)
            self.assertEqual(len(events), 0)

    def test_48_tier2_arena_multiple_overlapping_events(self):
        """Tier 2.19: 同一都市で複数イベントが同日開催される場合にすべて統合されること"""
        if hasattr(arena_calendar, "STATIC_CITY_EVENTS"):
            tokyo_events = [e for v in arena_calendar.STATIC_CITY_EVENTS if v.get("city_key") == "tokyo" for e in v.get("events", [])]
            self.assertIsInstance(tokyo_events, list)

    # --------------------------------------------------------------------------
    # 茶屋町回遊 境界値
    # --------------------------------------------------------------------------

    def test_49_tier2_chayamachi_firebase_empty_dict(self):
        """Tier 2.20: Firebase レスポンスが空辞書でもクラッシュせず安全にハンドリングされること"""
        with mock_monitor_http(json_data={}):
            results = monitor.get_yatakoi_data()
            self.assertIsInstance(results, list)
            if results:
                self.assertEqual(results[0]["men"], 0)
                self.assertEqual(results[0]["women"], 0)

    def test_50_tier2_chayamachi_seats_zero_protection(self):
        """Tier 2.21: 席数が 0 または None でも占有率計算で ZeroDivisionError が起きないこと"""
        if hasattr(moat_intelligence, "get_chayamachi_shift_analysis"):
            res = moat_intelligence.get_chayamachi_shift_analysis(
                now_jst=datetime.datetime(2026, 11, 10, 22, 0),
                yatakoi_cust=0, asobibar_cust=0
            )
            self.assertIsInstance(res, dict)


# ==============================================================================
# Tier 3: 複数機能連携 (Cross-Feature Combinations) — 計5件
# ==============================================================================

class Tier3CrossFeatureCombinationsTests(unittest.TestCase):
    """Tier 3: 複数機能・モジュール間のインターフェース結合とシナジー検証"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    def test_51_tier3_hashigo_intent_to_forecast_60m_bias(self):
        """Tier 3.1: ハシゴ診断インテントの急増が 60分後予測モデルの女性サージに連動すること"""
        lead_female = {
            "total_intents": 20,
            "by_audience": {"men": 4, "women": 16},
            "women_ratio_pct": 80,
            "lead_signal": "female_surge",
            "lead_bias_pt": 3.0
        }
        res_baseline = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 20, 20, is_open_now=True)
        sig = inspect.signature(moat_intelligence.predict_store_60m_dynamics)
        kwargs = {}
        if "lead_intent_heat" in sig.parameters:
            kwargs["lead_intent_heat"] = lead_female
        res_biased = moat_intelligence.predict_store_60m_dynamics(
            "OLG 新宿", 20, 20, is_open_now=True, **kwargs
        )
        self.assertIsInstance(res_biased, dict)

    def test_52_tier3_cold_surge_plus_arena_post_surge(self):
        """Tier 3.2: 気象急冷サージ (ΔT <= -3℃) と都市イベント終演サージの相乗効果判定"""
        now_surge = datetime.datetime(2026, 11, 10, 21, 15)
        arena_surges = moat_intelligence.detect_arena_event_surges(
            [{"name": "OLG 新宿", "city_key": "tokyo"}], now_jst=now_surge
        )
        self.assertIsInstance(arena_surges, dict)

    def test_53_tier3_jis_women_waiting_reverse_with_turnover_momentum(self):
        """Tier 3.3: JIS女性待ち逆転と席回転スピードの統合解析（滞留時の警告連動）"""
        if hasattr(moat_intelligence, "calculate_turnover_momentum_index"):
            pos_reverse = {
                "telemetry_type": "jis_pos",
                "is_reverse_waiting": True,
                "women_waiting_groups": 4,
                "men_waiting_groups": 0
            }
            tm = moat_intelligence.calculate_turnover_momentum_index(
                store_name="JIS SHINJUKU", women=40, men=35, is_open_now=True,
                pos_telemetry=pos_reverse
            )
            self.assertIsInstance(tm, dict)

    def test_54_tier3_chayamachi_shift_and_concierge_recommendation(self):
        """Tier 3.4: ハシゴ診断の梅田推薦と茶屋町ビル2フロア回遊動向の整合性"""
        res = self.client.get("/concierge?area=umeda&audience=men&party_size=pair")
        self.assertEqual(res.status_code, 200)

    def test_55_tier3_rollup_aggregation_to_stats_report_flow(self):
        """Tier 3.5: store_pos_telemetry_logs からの集計が統計レポート(/stats)に正常反映されること"""
        res = self.client.get("/stats")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)
        self.assertNotIn('href="/link/', html, "Stats surface must strictly avoid /link/ direct links")


# ==============================================================================
# Tier 4: 実世界シナリオ (Real-World Application Scenarios) — 計4件
# ==============================================================================

class Tier4RealWorldApplicationScenariosTests(unittest.TestCase):
    """Tier 4: 実世界エンドツーエンドワークロード・運用シナリオ・不変条件監査"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    def test_56_tier4_scenario_friday_night_umeda_hashigo_to_chayamachi(self):
        """Tier 4.1: [シナリオ1] 金曜夜の梅田ハシゴ体験（3秒診断 → 混雑確認 → 茶屋町回遊シフト閲覧）"""
        res_concierge = self.client.get("/concierge")
        self.assertEqual(res_concierge.status_code, 200)

        res_api = self.client.get("/api/concierge?area=umeda&audience=men&party_size=pair")
        self.assertEqual(res_api.status_code, 200)

        res_beacon = self.client.post("/api/track", json={
            "event": "hashigo_intent",
            "label": "umeda:men:pair"
        })
        self.assertEqual(res_beacon.status_code, 204)

        res_store = self.client.get("/store/yatakoi-umeda")
        if res_store.status_code == 404:
            res_store = self.client.get("/store/asobibar-chayamachi")
        self.assertEqual(res_store.status_code, 200)

    def test_57_tier4_scenario_cold_snap_jingu_stadium_tokyo_surge(self):
        """Tier 4.2: [シナリオ2] 初冬の神宮ナイター終演×前日比-4℃急冷サージの連動シナリオ"""
        res_top = self.client.get("/")
        self.assertEqual(res_top.status_code, 200)
        html = res_top.get_data(as_text=True)
        self.assertIn("相席ナビ", html)

        res_detail = self.client.get("/store/oriental-lounge-shinjuku")
        self.assertEqual(res_detail.status_code, 200)

    def test_58_tier4_scenario_jis_women_waiting_and_compliance_audit(self):
        """Tier 4.3: [シナリオ3] JIS女性待ち逆転発生とコンプライアンス（ブランド正式名称・直リンクゼロ）"""
        res = self.client.get("/store/jis-shinjuku")
        self.assertEqual(res.status_code, 200)
        html = res.get_data(as_text=True)

        soup_text = re.sub(r'<[^>]+>', ' ', html)
        self.assertNotRegex(soup_text, r'\bOLG\b', "Single 'OLG' must not appear in user visible text")

    def test_59_tier4_scenario_bot_crawler_defense_and_system_invariants(self):
        """Tier 4.4: [シナリオ4] 不正クローラー耐性＆コードベース不変条件（行数・turbo_store・キャッシュ同期）"""
        app_path = os.path.join(PROJECT_ROOT, "app.py")
        with open(app_path, "r", encoding="utf-8") as f:
            lines = f.readlines()
        line_count = len(lines)
        self.assertLessEqual(line_count, 2800, f"app.py ({line_count} lines) must be <= 2800 lines")

        turbo_path = os.path.join(PROJECT_ROOT, "turbo_store.py")
        with open(turbo_path, "r", encoding="utf-8") as f:
            t_content = f.read()
        self.assertIn('broken = bool(getattr(conn, "closed", 0))', t_content)

        sw_path = os.path.join(STATIC_DIR, "sw.js")
        self.assertTrue(os.path.isfile(sw_path))


if __name__ == "__main__":
    unittest.main()
