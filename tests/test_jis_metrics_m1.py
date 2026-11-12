import datetime
import json
import os
import shutil
import tempfile
import unittest
from unittest import mock

import monitor
import local_db
import app as app_module

_TMP_DB_DIR = None
_ORIG_DB_PATH = None
_ORIG_DATABASE_URL = None


def setUpModule():
    global _TMP_DB_DIR, _ORIG_DB_PATH, _ORIG_DATABASE_URL
    _TMP_DB_DIR = tempfile.mkdtemp(prefix="aiseki_jis_m1_")
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


class TestJisMetricsCollection(unittest.TestCase):
    """monitor.get_jis_data のメトリクス算出・型安全性・フォールトアイソレーション検証"""

    def test_jis_metrics_comprehensive_calculation(self):
        mock_payload = {
            "sapporo_b1": {
                "shared": {
                    "mens_customer_num": 28,
                    "mens_group_num": 12,
                    "ladys_customer_num": 34,
                    "ladys_group_num": 18,
                    "normal_mens_group_num": 10,
                    "normal_mens_share_num": 10,
                    "vip_mens_group_num": 2,
                    "vip_mens_share_num": 2,
                    "ladys_waiting_num": 0,
                    "mens_waiting_num": 0,
                }
            },
            "shinjuku": {
                "shared": {
                    "mens_customer_num": 5,
                    "mens_group_num": 5,
                    "ladys_customer_num": 4,
                    "ladys_group_num": 3,
                    "normal_mens_group_num": 5,
                    "normal_mens_share_num": 4,
                    "vip_mens_group_num": 0,
                    "vip_mens_share_num": 0,
                    "ladys_waiting_num": 3,
                    "ladys_waiting_customer_num": 6,
                    "mens_waiting_num": 1,
                    "mens_waiting_customer_num": 2,
                }
            },
            "fukuoka": {
                "shared": {
                    "mens_customer_num": 12,
                    "mens_group_num": 4,
                    "ladys_customer_num": 15,
                    "ladys_group_num": 5,
                    "normal_mens_group_num": 4,
                    "normal_mens_share_num": 2,
                    "vip_mens_group_num": 0,
                    "vip_mens_share_num": 0,
                    "ladys_waiting_num": 0,
                    "mens_waiting_num": 2,
                }
            },
            "umeda": {
                "shared": {
                    "mens_customer_num": 0,
                    "mens_group_num": 0,
                    "ladys_customer_num": 0,
                    "ladys_group_num": 0,
                    "normal_mens_group_num": 0,
                    "normal_mens_share_num": 0,
                    "vip_mens_group_num": 0,
                    "vip_mens_share_num": 0,
                    "ladys_waiting_num": 0,
                    "mens_waiting_num": 0,
                }
            }
        }
        fake_html = f"<html><script>var datas = {json.dumps(mock_payload)};</script></html>"
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = fake_html
        fake_resp.content = fake_html.encode("utf-8")
        fake_resp.raise_for_status.return_value = None

        with mock.patch("monitor._get_http_session") as mock_sess:
            mock_sess.return_value.get.return_value = fake_resp
            stores = monitor.get_jis_data()

        store_map = {s["name"]: s["pos_telemetry"] for s in stores}

        # sapporo_b1: ペア中心 (2.07)、VIP成立率100%, 一般100%, 差分0.0pt
        sp = store_map["JIS SAPPORO"]
        self.assertEqual(sp["avg_party_size_men"], 2.33)
        self.assertEqual(sp["avg_party_size_women"], 1.89)
        self.assertEqual(sp["avg_party_size_total"], 2.07)
        self.assertEqual(sp["party_style"], "ペア中心")
        self.assertEqual(sp["normal_match_rate"], 100.0)
        self.assertEqual(sp["vip_match_rate"], 100.0)
        self.assertEqual(sp["match_rate_diff"], 0.0)
        self.assertFalse(sp["is_women_waiting"])
        self.assertFalse(sp["is_reverse_waiting"])

        # shinjuku: ソロ多め (1.12)、一般80%, VIP None, 女性待ち逆転発生 (3組 > 1組)
        sj = store_map["JIS SHINJUKU"]
        self.assertEqual(sj["avg_party_size_total"], 1.12)
        self.assertEqual(sj["party_style"], "ソロ多め")
        self.assertEqual(sj["normal_match_rate"], 80.0)
        self.assertIsNone(sj["vip_match_rate"])
        self.assertIsNone(sj["match_rate_diff"])
        self.assertTrue(sj["is_women_waiting"])
        self.assertTrue(sj["is_reverse_waiting"])

        # fukuoka: グループ中心 (3.0)、通常男性待ち
        fk = store_map["JIS FUKUOKA"]
        self.assertEqual(fk["avg_party_size_total"], 3.0)
        self.assertEqual(fk["party_style"], "グループ中心")
        self.assertEqual(fk["normal_match_rate"], 50.0)
        self.assertFalse(fk["is_women_waiting"])
        self.assertFalse(fk["is_reverse_waiting"])

        # umeda: 営業時間外全ゼロ
        um = store_map["JIS UMEDA"]
        self.assertIsNone(um["avg_party_size_total"])
        self.assertEqual(um["party_style"], "")
        self.assertIsNone(um["normal_match_rate"])
        self.assertIsNone(um["vip_match_rate"])
        self.assertIsNone(um["match_rate_diff"])
        self.assertFalse(um["is_women_waiting"])
        self.assertFalse(um["is_reverse_waiting"])

    def test_schema_breakage_isolation(self):
        """不正な型が含まれる店舗のみ安全にスキップされ、正常な店舗が処理されること"""
        broken_payload = {
            "shinjuku": {"shared": {"mens_customer_num": "broken_string_not_int"}},
            "umeda": {"shared": {"mens_customer_num": 10, "mens_group_num": 5}},
        }
        fake_html = f"<html><script>var datas = {json.dumps(broken_payload)};</script></html>"
        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.text = fake_html
        fake_resp.content = fake_html.encode("utf-8")
        fake_resp.raise_for_status.return_value = None

        with mock.patch("monitor._get_http_session") as mock_sess:
            mock_sess.return_value.get.return_value = fake_resp
            stores = monitor.get_jis_data()

        self.assertEqual(len(stores), 1)
        self.assertEqual(stores[0]["name"], "JIS UMEDA")
        self.assertEqual(stores[0]["pos_telemetry"]["avg_party_size_men"], 2.0)


class TestJisRollupMetrics(unittest.TestCase):
    """local_db のマイグレーション冪等性およびロールアップ集計検証"""

    def setUp(self):
        local_db.init_db()

    def test_migration_idempotency(self):
        """_migrate_jis_metrics_columns が複数回実行されても安全に完了すること"""
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            local_db._migrate_jis_metrics_columns(conn, cur)
            local_db._migrate_jis_metrics_columns(conn, cur)
            cur.execute("PRAGMA table_info(store_hourly_rollups);")
            cols = {row[1]: row[2] for row in cur.fetchall()}
            self.assertIn("avg_women_waiting_groups", cols)
            self.assertIn("max_women_waiting_groups", cols)
            self.assertIn("women_waiting_detected", cols)
            self.assertIn("avg_party_size", cols)
            self.assertIn("avg_normal_match_rate", cols)
            self.assertIn("avg_vip_match_rate", cols)
            self.assertIn("avg_match_rate_diff", cols)
        finally:
            conn.close()

    def test_jis_hourly_rollup_aggregation(self):
        """JIS POSテレメトリから7つの新指標が正しくロールアップされること"""
        d_str = "2026-11-10"
        ts_1 = f"{d_str} 22:10:00"
        ts_2 = f"{d_str} 22:40:00"
        store = "JIS SHINJUKU"

        pos_1 = {
            "telemetry_type": "jis_pos",
            "men_total_guests": 20, "women_total_guests": 20,
            "men_total_groups": 10, "women_total_groups": 10,
            "men_waiting_groups": 1, "men_waiting_guests": 2,
            "women_waiting_groups": 3, "women_waiting_guests": 6,
            "matched_groups": 8, "vip_guests": 2,
            "avg_party_size_total": 2.0,
            "normal_match_rate": 80.0,
            "vip_match_rate": 100.0,
            "match_rate_diff": 20.0,
            "is_women_waiting": True,
            "is_reverse_waiting": True,
        }
        pos_2 = {
            "telemetry_type": "jis_pos",
            "men_total_guests": 24, "women_total_guests": 22,
            "men_total_groups": 12, "women_total_groups": 11,
            "men_waiting_groups": 2, "men_waiting_guests": 4,
            "women_waiting_groups": 1, "women_waiting_guests": 2,
            "matched_groups": 9, "vip_guests": 4,
            "avg_party_size_total": 2.0,
            "normal_match_rate": 70.0,
            "vip_match_rate": 90.0,
            "match_rate_diff": 20.0,
            "is_women_waiting": True,
            "is_reverse_waiting": False,
        }

        local_db.insert_logs([
            {"name": store, "men": 20, "women": 20, "source": "jis", "pos_telemetry": pos_1},
        ], ts_1)
        local_db.insert_logs([
            {"name": store, "men": 24, "women": 22, "source": "jis", "pos_telemetry": pos_2},
        ], ts_2)

        res = local_db.rollup_store_hourly_stats(target_dates=[d_str])
        self.assertTrue(res["ok"])

        rows = local_db._fetch_all_rows(
            "SELECT avg_women_waiting_groups, max_women_waiting_groups, women_waiting_detected, "
            "avg_party_size, avg_normal_match_rate, avg_vip_match_rate, avg_match_rate_diff "
            "FROM store_hourly_rollups WHERE date_jst = ? AND hour_jst = 22 AND store_name = ?",
            (d_str, store),
        )
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(float(r[0]), 2.0)    # (3 + 1) / 2
        self.assertEqual(int(r[1]), 3)        # max(3, 1)
        self.assertEqual(int(r[2]), 1)        # detected flag
        self.assertEqual(float(r[3]), 2.0)    # avg party size
        self.assertEqual(float(r[4]), 75.0)   # (80 + 70) / 2
        self.assertEqual(float(r[5]), 95.0)   # (100 + 90) / 2
        self.assertEqual(float(r[6]), 20.0)   # (20 + 20) / 2

    def test_non_jis_store_null_safety(self):
        """非JIS店舗のロールアップで新指標がNULL/0として安全に格納されること"""
        d_str = "2026-11-10"
        ts = f"{d_str} 21:15:00"
        store = "ASOBIBAR 新宿"
        local_db.insert_logs([
            {"name": store, "men": 15, "women": 15, "source": "asobibar"},
        ], ts)
        res = local_db.rollup_store_hourly_stats(target_dates=[d_str])
        self.assertTrue(res["ok"])

        rows = local_db._fetch_all_rows(
            "SELECT avg_women_waiting_groups, max_women_waiting_groups, women_waiting_detected, avg_party_size "
            "FROM store_hourly_rollups WHERE date_jst = ? AND hour_jst = 21 AND store_name = ?",
            (d_str, store),
        )
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertIsNone(r[0])
        self.assertIsNone(r[1])
        self.assertEqual(int(r[2]), 0)
        self.assertIsNone(r[3])


class TestJisUiRendering(unittest.TestCase):
    """test_client を用いたルーティング・JISメトリクスUI描画検証"""

    def setUp(self):
        self.app = app_module.app
        self.app.config["TESTING"] = True
        self.client = self.app.test_client()

    def test_store_detail_jis_panel_via_client(self):
        """JIS店舗詳細画面でPOS実測アナリティクスパネルが正常にレンダリングされること"""
        jis_pos = {
            "telemetry_type": "jis_pos",
            "avg_party_size_total": 2.15,
            "avg_party_size_men": 2.2,
            "avg_party_size_women": 2.1,
            "party_style": "ペア中心",
            "normal_match_rate": 80.0,
            "vip_match_rate": 100.0,
            "match_rate_diff": 20.0,
            "is_women_waiting": True,
            "is_reverse_waiting": True,
            "women_waiting_groups": 3,
            "women_waiting_guests": 6,
            "men_waiting_groups": 1,
            "men_waiting_guests": 2,
            "vip_mens_group_num": 2,
            "vip_mens_share_num": 2,
            "normal_mens_group_num": 10,
            "normal_mens_share_num": 8,
        }
        mock_raw_stores = [
            {
                "name": "JIS SHINJUKU",
                "women": 25,
                "men": 20,
                "source": "jis",
                "pos_telemetry": jis_pos,
            }
        ]
        with app_module.data_lock:
            old_full = app_module.latest_data.get("full_data")
            old_time = app_module.latest_data.get("last_updated")
            app_module.latest_data["full_data"] = mock_raw_stores
            app_module.latest_data["last_updated"] = "22:30"

        try:
            resp = self.client.get("/store/jis-shinjuku?staff_test=1")
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode("utf-8")
            self.assertIn("JIS公式POS実測アナリティクス", html)
            self.assertIn("id=\"jis-pos-panel\"", html)
            self.assertIn("女性待ち逆転発生中（男性即相席チャンス）", html)
            self.assertIn("ペア中心", html)
            self.assertIn("VIP効率 +20.0pt", html)
            self.assertIn("VIP 100.0%", html)
            self.assertIn("一般 80.0%", html)
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = old_full
                app_module.latest_data["last_updated"] = old_time

    def test_store_detail_non_jis_hides_panel_via_client(self):
        """非JIS店舗（オリエンタルラウンジ）でJISパネルDOM要素が表示されないこと"""
        mock_raw_stores = [
            {
                "name": "OLG 渋谷本店",
                "women": 30,
                "men": 25,
                "source": "olg",
                "pos_telemetry": None,
            }
        ]
        with app_module.data_lock:
            old_full = app_module.latest_data.get("full_data")
            old_time = app_module.latest_data.get("last_updated")
            app_module.latest_data["full_data"] = mock_raw_stores
            app_module.latest_data["last_updated"] = "22:30"

        try:
            resp = self.client.get("/store/oriental-lounge-shibuya-honten?staff_test=1")
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode("utf-8")
            self.assertNotIn('id="jis-pos-panel"', html)
            self.assertNotIn('class="jis-pos-panel"', html)
            self.assertNotIn('class="jis-pos-title"', html)
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = old_full
                app_module.latest_data["last_updated"] = old_time

    def test_stats_report_jis_section_via_client(self):
        """統計ページ /stats でJIS全10店舗KPIサマリー＆リアルタイムテーブルが表示されること"""
        jis_pos = {
            "telemetry_type": "jis_pos",
            "avg_party_size_total": 2.05,
            "avg_party_size_men": 2.1,
            "avg_party_size_women": 2.0,
            "party_style": "ペア中心",
            "normal_match_rate": 78.0,
            "vip_match_rate": 95.0,
            "match_rate_diff": 17.0,
            "is_women_waiting": True,
            "is_reverse_waiting": True,
            "women_waiting_groups": 2,
        }
        mock_raw_stores = [
            {
                "name": "JIS SHINJUKU",
                "women": 25,
                "men": 20,
                "source": "jis",
                "pos_telemetry": jis_pos,
            }
        ]
        with app_module.data_lock:
            old_full = app_module.latest_data.get("full_data")
            old_time = app_module.latest_data.get("last_updated")
            app_module.latest_data["full_data"] = mock_raw_stores
            app_module.latest_data["last_updated"] = "22:30"

        try:
            resp = self.client.get("/stats?staff_test=1")
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode("utf-8")
            self.assertIn("JIS公式POS実測データ解析（全10店舗 組人数・VIP効率・女性待ち傾向）", html)
            self.assertIn("全10店舗 平均組人数", html)
            self.assertIn("VIP席 相席成立効率差", html)
            self.assertIn("女性待ち逆転ステータス", html)
            self.assertIn("JIS SHINJUKU", html)
            self.assertIn('/store/jis-shinjuku', html)
            self.assertIn("逆転（女性2組待ち）", html)
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = old_full
                app_module.latest_data["last_updated"] = old_time

    def test_stats_report_closed_jis_zero_dummy_and_valid_slugs(self):
        """営業時間外（0名）のJIS店舗では推測値（1.88 / 78.4 / 58.2）や偽の『即案内中』を一切表示せず、店舗リンクslugも正常生成されること"""
        import app as app_module

        closed_jis = [
            {
                "name": "JIS SHINJUKU",
                "women": 0,
                "men": 0,
                "source": "jis",
                "pos_telemetry": {
                    "telemetry_type": "jis_pos",
                    "avg_party_size_total": None,
                    "normal_match_rate": None,
                    "vip_match_rate": None,
                    "is_women_waiting": False,
                    "is_reverse_waiting": False,
                    "women_waiting_groups": 0,
                },
            }
        ]
        with app_module.data_lock:
            old_full = app_module.latest_data.get("full_data")
            old_time = app_module.latest_data.get("last_updated")
            app_module.latest_data["full_data"] = closed_jis
            app_module.latest_data["last_updated"] = "09:15"

        try:
            resp = self.client.get("/stats?staff_test=1")
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode("utf-8")
            self.assertIn('/store/jis-shinjuku', html)
            self.assertNotIn('href="/store/"', html)
            self.assertNotIn("1.88", html)
            self.assertNotIn("78.4", html)
            self.assertNotIn("58.2", html)
            self.assertNotIn("即案内中（待ち0）", html)
            self.assertIn("営業時間外・実測ログ待機中", html)
        finally:
            with app_module.data_lock:
                app_module.latest_data["full_data"] = old_full
                app_module.latest_data["last_updated"] = old_time

    def test_fable_audit_guards_static_events_and_intent_whitelist(self):
        """STATIC_CITY_EVENTSの全日程が公式URLを持ち、analyticsのhashigo_intentが未知エリアで爆発しないこと"""
        import arena_calendar
        import analytics

        for venue in arena_calendar.STATIC_CITY_EVENTS:
            def_src = str(venue.get("source_url") or "")
            def_ver = str(venue.get("verified_at") or "")
            for ev in venue.get("events", []):
                src = str(ev.get("source_url") or def_src)
                ver = str(ev.get("verified_at") or def_ver)
                self.assertTrue(src.startswith(("http://", "https://")), f"Missing source_url on {ev}")
                self.assertNotIn(src.rstrip("/"), ("https://www.student-baseball.or.jp", "https://hanshintigers.jp", "https://www.nissan-stadium.jp", "https://www.jleague.jp", "https://www.yodohanabi.com", "https://www.sumidagawa-hanabi.com"))
                self.assertTrue(bool(ver), f"Missing verified_at on {ev}")
                self.assertNotEqual(ev.get("event_date"), "2026-11-10")

        bucket = analytics.classify_hourly_bucket("hashigo_intent", "attacker_random_area_999:men:pair")
        self.assertEqual(bucket, "intent:other")


if __name__ == "__main__":
    unittest.main()
