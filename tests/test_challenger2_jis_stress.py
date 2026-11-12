"""
Milestone 1: JIS詳細メトリクス 実証ストレステスター Challenger 2
DBスキーマ・ロールアップ集計および画面レンダリングの実証的ストレステストスイート
"""

import concurrent.futures
import json
import os
import shutil
import sqlite3
import tempfile
import unittest
from unittest import mock

# プロジェクトルートのモジュールをインポート
import local_db
import monitor
import store_master
import store_hours
import app as app_module

_TMP_DB_DIR = None
_ORIG_DB_PATH = None
_ORIG_DATABASE_URL = None


def setUpModule():
    global _TMP_DB_DIR, _ORIG_DB_PATH, _ORIG_DATABASE_URL
    _TMP_DB_DIR = tempfile.mkdtemp(prefix="aiseki_jis_stress_")
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


class TestDbMigrationStress(unittest.TestCase):
    """1. DBマイグレーション冪等性・トランザクションテスト"""

    def test_migrate_jis_columns_in_memory_10x_consecutive(self):
        """インメモリSQLiteに対して _migrate_jis_metrics_columns を連続10回実行し、
        重複エラー、例外が発生せず、正しくスキーマが拡張されることを実証"""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()

        # 旧バージョンの store_hourly_rollups テーブルを作成（新7カラムなし）
        cur.execute("""
            CREATE TABLE store_hourly_rollups (
                date_jst TEXT NOT NULL,
                hour_jst INTEGER NOT NULL,
                business_date TEXT NOT NULL,
                store_name TEXT NOT NULL,
                sample_count INTEGER NOT NULL DEFAULT 0,
                avg_women REAL,
                max_women INTEGER,
                avg_men REAL,
                max_men INTEGER,
                avg_wpct REAL,
                avg_men_waiting_groups REAL,
                max_men_waiting_groups INTEGER,
                avg_men_waiting_guests REAL,
                avg_matched_groups REAL,
                avg_vip_guests REAL,
                precip_mm_max REAL,
                temp_c_avg REAL,
                is_rainy INTEGER NOT NULL DEFAULT 0,
                transit_disrupted INTEGER NOT NULL DEFAULT 0,
                arena_event_active INTEGER NOT NULL DEFAULT 0,
                campaign_count INTEGER NOT NULL DEFAULT 0,
                fc_resolved_count INTEGER NOT NULL DEFAULT 0,
                fc_mae_wpct REAL,
                votes_smooth INTEGER NOT NULL DEFAULT 0,
                votes_waiting INTEGER NOT NULL DEFAULT 0,
                votes_lively INTEGER NOT NULL DEFAULT 0,
                avg_reported_wait_min REAL,
                PRIMARY KEY (date_jst, hour_jst, store_name)
            )
        """)
        conn.commit()

        # 連続10回実行
        for i in range(10):
            try:
                local_db._migrate_jis_metrics_columns(conn, cur)
            except Exception as e:
                self.fail(f"Iteration {i+1} failed with exception: {e}")

        # スキーマ検証
        cur.execute("PRAGMA table_info(store_hourly_rollups);")
        cols = {row[1]: row[2] for row in cur.fetchall()}

        expected_cols = [
            "avg_women_waiting_groups",
            "max_women_waiting_groups",
            "women_waiting_detected",
            "avg_party_size",
            "avg_normal_match_rate",
            "avg_vip_match_rate",
            "avg_match_rate_diff",
        ]
        for col in expected_cols:
            self.assertIn(col, cols, f"Column {col} missing after 10 migrations")

        conn.close()

    def test_migrate_jis_columns_file_db_10x_consecutive(self):
        """ファイルベースSQLite (WALモード) に対しても連続10回実行し例外なく完了することを実証"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name

        try:
            conn = sqlite3.connect(db_path)
            conn.execute("PRAGMA journal_mode = WAL;")
            cur = conn.cursor()

            # 旧バージョンテーブル作成
            cur.execute("""
                CREATE TABLE store_hourly_rollups (
                    date_jst TEXT NOT NULL,
                    hour_jst INTEGER NOT NULL,
                    business_date TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    avg_women REAL,
                    max_women INTEGER,
                    avg_men REAL,
                    max_men INTEGER,
                    avg_wpct REAL,
                    PRIMARY KEY (date_jst, hour_jst, store_name)
                )
            """)
            conn.commit()

            # 連続10回実行
            for i in range(10):
                local_db._migrate_jis_metrics_columns(conn, cur)

            cur.execute("PRAGMA table_info(store_hourly_rollups);")
            cols = {row[1]: row[2] for row in cur.fetchall()}
            conn.close()

            expected_cols = [
                "avg_women_waiting_groups",
                "max_women_waiting_groups",
                "women_waiting_detected",
                "avg_party_size",
                "avg_normal_match_rate",
                "avg_vip_match_rate",
                "avg_match_rate_diff",
            ]
            for col in expected_cols:
                self.assertIn(col, cols)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)

    def test_migrate_jis_columns_concurrent_threads(self):
        """マルチスレッドからの並行マイグレーション実行に対する堅牢性検証"""
        with tempfile.NamedTemporaryFile(suffix=".db", delete=False) as tf:
            db_path = tf.name

        try:
            init_conn = sqlite3.connect(db_path)
            init_conn.execute("PRAGMA journal_mode = WAL;")
            init_cur = init_conn.cursor()
            init_cur.execute("""
                CREATE TABLE store_hourly_rollups (
                    date_jst TEXT NOT NULL,
                    hour_jst INTEGER NOT NULL,
                    business_date TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    sample_count INTEGER NOT NULL DEFAULT 0,
                    PRIMARY KEY (date_jst, hour_jst, store_name)
                )
            """)
            init_conn.commit()
            init_conn.close()

            def worker_migration(worker_id):
                thread_conn = sqlite3.connect(db_path, timeout=10.0)
                thread_cur = thread_conn.cursor()
                try:
                    for _ in range(5):
                        local_db._migrate_jis_metrics_columns(thread_conn, thread_cur)
                    return True
                finally:
                    thread_conn.close()

            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as executor:
                futures = [executor.submit(worker_migration, i) for i in range(4)]
                for f in concurrent.futures.as_completed(futures):
                    self.assertTrue(f.result())

            verify_conn = sqlite3.connect(db_path)
            verify_cur = verify_conn.cursor()
            verify_cur.execute("PRAGMA table_info(store_hourly_rollups);")
            cols = {row[1]: row[2] for row in verify_cur.fetchall()}
            verify_conn.close()

            self.assertIn("avg_party_size", cols)
            self.assertIn("avg_match_rate_diff", cols)
        finally:
            if os.path.exists(db_path):
                os.remove(db_path)


class TestRollupStoreHourlyStatsStress(unittest.TestCase):
    """2. rollup_store_hourly_stats のストレステスト（NULL・欠損・破損データ・非JIS混在・連続UPSERT）"""

    def setUp(self):
        local_db.init_db()

    def test_rollup_with_corrupted_and_missing_telemetry(self):
        """破損JSON、None値、空dict、異常値、非JIS店舗が混在してもクラッシュせず正常集計・UPSERTされること"""
        d_str = "2026-11-15"
        ts_base = f"{d_str} 23:00:00"

        # 1. 破損したraw_json・異常なpos_telemetryを持つログ
        corrupted_logs = [
            # 完全に破損したJSON文字列
            ("2026-11-15 23:05:00", "JIS SHINJUKU", "jis_pos", 0, 0, 0, 0, 0, "INVALID_JSON{{}"),
            # 空dict
            ("2026-11-15 23:10:00", "JIS SHINJUKU", "jis_pos", 0, 0, 0, 0, 0, "{}"),
            # raw_json が None
            ("2026-11-15 23:15:00", "JIS SHINJUKU", "jis_pos", 2, 4, 1, 0, 1, None),
            # 各種キーが None の JSON
            ("2026-11-15 23:20:00", "JIS SHINJUKU", "jis_pos", 1, 2, 1, 0, 0, json.dumps({
                "avg_party_size_total": None,
                "normal_match_rate": None,
                "vip_match_rate": None,
                "match_rate_diff": None,
                "is_women_waiting": None,
                "is_reverse_waiting": None,
                "women_waiting_groups": None,
            })),
            # 各種キーが文字列や負数などの異常値
            ("2026-11-15 23:25:00", "JIS SHINJUKU", "jis_pos", 1, 2, 1, 0, 0, json.dumps({
                "avg_party_size_total": "2.5",
                "normal_match_rate": "75.0",
                "vip_match_rate": "100.0",
                "match_rate_diff": "25.0",
                "women_waiting_groups": "2",
                "women_waiting_guests": "4",
            })),
            # 正常なJISデータ
            ("2026-11-15 23:30:00", "JIS SHINJUKU", "jis_pos", 2, 4, 2, 1, 3, json.dumps({
                "telemetry_type": "jis_pos",
                "avg_party_size_total": 2.2,
                "normal_match_rate": 80.0,
                "vip_match_rate": 100.0,
                "match_rate_diff": 20.0,
                "is_women_waiting": True,
                "is_reverse_waiting": True,
                "women_waiting_groups": 3,
            })),
        ]

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            # monitor_logs にモニタリングログを投入 (重複防止のため INSERT OR REPLACE)
            for ts, sname, *_ in corrupted_logs:
                cur.execute(
                    "INSERT OR REPLACE INTO monitor_logs (timestamp, store_name, women, men, source) VALUES (?, ?, ?, ?, ?)",
                    (ts, sname, 20, 20, "jis")
                )
            # pos_telemetry_logs に投入
            for ts, sname, tel_type, mwg, mwp, mg, vip, wwg, raw_j in corrupted_logs:
                cur.execute(
                    "INSERT OR REPLACE INTO store_pos_telemetry_logs (timestamp, store_name, telemetry_type, "
                    "men_waiting_groups, men_waiting_guests, matched_groups, vip_guests, women_waiting_groups, raw_json) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (ts, sname, tel_type, mwg, mwp, mg, vip, wwg, raw_j)
                )
            # 非JIS店舗のログも同時に投入
            non_jis_stores = ["OLG 新宿", "OLG ag 札幌", "ASOBIBAR 梅田", "YATAKOI 茶屋町", "UNKNOWN STORE"]
            for sname in non_jis_stores:
                cur.execute(
                    "INSERT OR REPLACE INTO monitor_logs (timestamp, store_name, women, men, source) VALUES (?, ?, ?, ?, ?)",
                    (f"{d_str} 23:20:00", sname, 15, 15, "web")
                )
            conn.commit()
        finally:
            conn.close()

        # ロールアップ実行
        res = local_db.rollup_store_hourly_stats(target_dates=[d_str])
        self.assertTrue(res["ok"], f"Rollup failed: {res}")
        self.assertGreater(res["upserted_rollups"], 0)

        # 集計結果の検証
        rows = local_db._fetch_all_rows(
            "SELECT avg_women_waiting_groups, max_women_waiting_groups, women_waiting_detected, "
            "avg_party_size, avg_normal_match_rate, avg_vip_match_rate, avg_match_rate_diff "
            "FROM store_hourly_rollups WHERE date_jst = ? AND hour_jst = 23 AND store_name = 'JIS SHINJUKU'",
            (d_str,),
        )
        self.assertEqual(len(rows), 1)
        r = rows[0]
        # クラッシュせず数値が算出されていること
        self.assertIsNotNone(r[0])  # avg_women_waiting_groups
        self.assertGreaterEqual(r[1], 3)  # max_women_waiting_groups
        self.assertEqual(r[2], 1)  # women_waiting_detected

        # 非JIS店舗のロールアップ検証（NULL安全性）
        for sname in non_jis_stores:
            non_rows = local_db._fetch_all_rows(
                "SELECT avg_women_waiting_groups, max_women_waiting_groups, women_waiting_detected, avg_party_size "
                "FROM store_hourly_rollups WHERE date_jst = ? AND hour_jst = 23 AND store_name = ?",
                (d_str, sname),
            )
            self.assertEqual(len(non_rows), 1)
            nr = non_rows[0]
            self.assertIsNone(nr[0], f"Expected None avg_wwg for {sname}")
            self.assertIsNone(nr[1], f"Expected None max_wwg for {sname}")
            self.assertEqual(nr[2], 0, f"Expected 0 ww_detected for {sname}")
            self.assertIsNone(nr[3], f"Expected None avg_party_size for {sname}")

    def test_rollup_repeated_upsert_idempotency(self):
        """同一日付・同一時間・同一店舗に対して連続10回ロールアップを実行してもデータ整合性が維持されること"""
        d_str = "2026-11-16"
        ts = f"{d_str} 22:30:00"
        store = "JIS UMEDA"

        local_db.insert_logs([
            {
                "name": store,
                "men": 30,
                "women": 28,
                "source": "jis",
                "pos_telemetry": {
                    "telemetry_type": "jis_pos",
                    "men_total_guests": 30,
                    "women_total_guests": 28,
                    "men_total_groups": 15,
                    "women_total_groups": 14,
                    "men_waiting_groups": 2,
                    "women_waiting_groups": 0,
                    "avg_party_size_total": 2.0,
                    "normal_match_rate": 85.0,
                    "vip_match_rate": 100.0,
                    "match_rate_diff": 15.0,
                    "is_women_waiting": False,
                    "is_reverse_waiting": False,
                }
            }
        ], ts)

        # 連続10回ロールアップ実行
        for i in range(10):
            res = local_db.rollup_store_hourly_stats(target_dates=[d_str])
            self.assertTrue(res["ok"], f"Rollup run {i+1} failed")

        rows = local_db._fetch_all_rows(
            "SELECT sample_count, avg_party_size, avg_normal_match_rate, avg_vip_match_rate, avg_match_rate_diff "
            "FROM store_hourly_rollups WHERE date_jst = ? AND hour_jst = 22 AND store_name = ?",
            (d_str, store),
        )
        self.assertEqual(len(rows), 1, "Duplicate primary key rows generated!")
        r = rows[0]
        self.assertEqual(r[0], 1)  # sample_count = 1
        self.assertEqual(float(r[1]), 2.0)
        self.assertEqual(float(r[2]), 85.0)
        self.assertEqual(float(r[3]), 100.0)
        self.assertEqual(float(r[4]), 15.0)


class TestHtmlRenderingStress(unittest.TestCase):
    """3. HTMLレンダリング実証テスト（JIS全10店舗生データ & 非JIS店舗 & エッジケース）"""

    @classmethod
    def setUpClass(cls):
        cls.app = app_module.app
        cls.app.config["TESTING"] = True
        cls.client = cls.app.test_client()

    def _build_full_store_obj(self, sname, brand_label, pos_telemetry=None):
        hours_info = store_hours.get_business_hours_status(sname, total_guests=20)
        store_obj = {
            "name": sname,
            "seo_name": store_master.seo_name(sname),
            "slug": store_master.slug_of(sname),
            "brand_label": brand_label,
            "brand_slug": store_master.brand_key_of(sname),
            "area_label": "テスト地域",
            "women": 20,
            "men": 20,
            "total": 40,
            "women_ratio": 50,
            "wpct": 50,
            "status_text": "営業中",
            "status_color": "#4caf50",
            "pos_telemetry": pos_telemetry,
            "address": "テスト住所",
            "business_hours": hours_info.get("hours_label", "18:00〜翌6:00"),
            "hours_label": hours_info.get("hours_label", "18:00〜翌6:00"),
            "today_hours_label": hours_info.get("today_hours_label", "本日営業中"),
            "hours_spec": hours_info.get("schedule_spec", "毎日 18:00〜翌6:00"),
            "opening_hours_specification": hours_info.get("opening_hours_specification") or [],
            "pricing": "男性10分200円〜",
            "website": "https://jis.bar",
            "official_url": "https://jis.bar",
            "map_url": "https://maps.google.com",
            "crowd_comment": "男女比1:1",
            "is_open_now": True,
            "lat": 35.69,
            "lng": 139.70,
        }
        app_module._attach_store_benchmarks(store_obj, sname, 20, 20, {})
        return store_obj

    @staticmethod
    def _create_jis_10_stores_fixture():
        """JIS全10店舗の完全な実測構造フィクスチャ（ネットワーク障害・タイムアウト時のフォールバック）"""
        stores = [
            "JIS SAPPORO", "JIS OMIYA", "JIS SHINJUKU", "JIS NISHISHINJUKU",
            "JIS UMEDA", "JIS NAMBA", "JIS CHAYAMACHI", "JIS FUKUOKA",
            "JIS KUMAMOTO", "JIS MATSUYAMA"
        ]
        fixture = []
        for idx, sname in enumerate(stores):
            fixture.append({
                "name": sname,
                "men": 20 + idx,
                "women": 18 + idx,
                "source": "jis",
                "pos_telemetry": {
                    "telemetry_type": "jis_pos",
                    "men_total_guests": 20 + idx,
                    "women_total_guests": 18 + idx,
                    "men_total_groups": 10 + (idx // 2),
                    "women_total_groups": 9 + (idx // 2),
                    "matched_groups": 7,
                    "women_matched_groups": 7,
                    "men_matched_guests": 14,
                    "women_matched_guests": 14,
                    "men_waiting_groups": 1,
                    "men_waiting_guests": 2,
                    "women_waiting_groups": 0,
                    "women_waiting_guests": 0,
                    "standing_guests": 0,
                    "vip_guests": 2,
                    "ex_guests": 0,
                    "avg_party_size_men": 2.0,
                    "avg_party_size_women": 2.0,
                    "avg_party_size_total": 2.0,
                    "party_style": "ペア中心",
                    "normal_mens_group_num": 8,
                    "normal_mens_share_num": 6,
                    "normal_match_rate": 75.0,
                    "vip_mens_group_num": 2,
                    "vip_mens_share_num": 2,
                    "vip_match_rate": 100.0,
                    "match_rate_diff": 25.0,
                    "is_women_waiting": False,
                    "is_reverse_waiting": False,
                }
            })
        return fixture

    def _get_reliable_jis_stores(self):
        """外部サイトからの取得を試み、ネットワーク切断・遅延・タイムアウト等で10店舗未満の場合は
        JIS全10店舗の完全なテレメトリフィクスチャでフォールバックしてテストを安定化"""
        try:
            stores = monitor.get_jis_data()
            if isinstance(stores, list) and len(stores) == 10:
                return stores
        except Exception:
            pass
        return self._create_jis_10_stores_fixture()

    def test_render_all_10_real_jis_stores_via_client(self):
        """Flask test_client 経由で JIS全10店舗の /store/<slug> をリクエストし、
        HTTP 200 OK かつ JIS POS実測パネルが描画されることを実証"""
        jis_stores = self._get_reliable_jis_stores()
        self.assertEqual(len(jis_stores), 10, f"Expected 10 JIS stores, got {len(jis_stores)}")

        with mock.patch("monitor.get_jis_data", return_value=jis_stores):
            for s in jis_stores:
                sname = s["name"]
                slug = store_master.slug_of(sname)
                resp = self.client.get(f"/store/{slug}")
                self.assertEqual(resp.status_code, 200, f"Failed GET /store/{slug} for {sname}")
                body = resp.data.decode("utf-8")
                self.assertIn("jis-pos-panel", body, f"jis-pos-panel missing for {sname}")
                self.assertIn("JIS公式POS実測アナリティクス", body)

    def test_render_all_10_real_jis_stores_direct(self):
        """実際に monitor.get_jis_data() で取得した本番生データ全10店舗（または安定フォールバック）を直接 render_template し、
        Jinja2エラーが0件かつJISアナリティクスパネルが正常生成されることを実証"""
        jis_stores = self._get_reliable_jis_stores()
        self.assertEqual(len(jis_stores), 10)

        with self.app.test_request_context():
            for store_data in jis_stores:
                sname = store_data["name"]
                pos = store_data.get("pos_telemetry", {})
                store_obj = self._build_full_store_obj(sname, "JIS", pos)

                try:
                    html = app_module.render_template(
                        "store_detail.html",
                        store=store_obj,
                        store_analytics={},
                        store_insight={},
                        store_dossier={},
                        has_sufficient_store_data=True,
                        store_smart_cta={},
                        store_second_venues=[],
                        store_votes={},
                        watch_counts={},
                        area_info={"name": "テストエリア", "slug": "test-area"},
                        area_slug="test-area",
                        nearby_stores=[],
                        nearby_comparison={},
                        related_guides=[],
                        jp_holidays=[],
                        last_updated="2026-11-10 01:00",
                        is_staff=False,
                    )
                except Exception as e:
                    self.fail(f"Jinja2 rendering error on store {sname}: {e}")

                self.assertIn("jis-pos-panel", html, f"JIS POS panel missing in {sname}")
                self.assertIn("JIS公式POS実測アナリティクス", html)

    def test_render_jis_extreme_edge_cases(self):
        """逆転待機、ゼロ除算、極端な外れ値、欠損値などエッジケース下でのレンダリングテスト"""
        edge_cases = [
            # 1. 女性待ち逆転発生（女性5組待機 > 男性1組待機）
            {
                "telemetry_type": "jis_pos",
                "is_reverse_waiting": True,
                "is_women_waiting": True,
                "women_waiting_groups": 5,
                "women_waiting_guests": 10,
                "men_waiting_groups": 1,
                "men_waiting_guests": 2,
                "avg_party_size_total": 2.0,
                "party_style": "ペア中心",
                "normal_match_rate": 80.0,
                "vip_match_rate": 100.0,
                "match_rate_diff": 20.0,
            },
            # 2. 通常女性待ち（女性待機あり、男性待機の方が多いか等しい）
            {
                "telemetry_type": "jis_pos",
                "is_reverse_waiting": False,
                "is_women_waiting": True,
                "women_waiting_groups": 2,
                "women_waiting_guests": 4,
                "men_waiting_groups": 3,
                "men_waiting_guests": 6,
                "avg_party_size_total": 1.3,
                "party_style": "ソロ多め",
                "normal_match_rate": 70.0,
                "vip_match_rate": None,
                "match_rate_diff": None,
            },
            # 3. 営業時間外・全ゼロ・全None
            {
                "telemetry_type": "jis_pos",
                "is_reverse_waiting": False,
                "is_women_waiting": False,
                "women_waiting_groups": 0,
                "women_waiting_guests": 0,
                "men_waiting_groups": 0,
                "men_waiting_guests": 0,
                "avg_party_size_total": None,
                "party_style": "",
                "normal_match_rate": None,
                "vip_match_rate": None,
                "match_rate_diff": None,
            },
            # 4. VIP相席率が一般席より低い（match_rate_diff < 0）
            {
                "telemetry_type": "jis_pos",
                "avg_party_size_total": 3.8,
                "party_style": "グループ中心",
                "normal_match_rate": 90.0,
                "vip_match_rate": 75.0,
                "match_rate_diff": -15.0,
            },
            # 5. 極端な巨大外れ値
            {
                "telemetry_type": "jis_pos",
                "avg_party_size_total": 99.99,
                "party_style": "グループ中心",
                "normal_match_rate": 100.0,
                "vip_match_rate": 100.0,
                "match_rate_diff": 0.0,
                "women_waiting_groups": 999,
                "women_waiting_guests": 9999,
            },
            # 6. 空dict（キーがtelemetry_typeのみ）
            {
                "telemetry_type": "jis_pos",
            },
        ]

        with self.app.test_request_context():
            for idx, pt in enumerate(edge_cases):
                store_obj = self._build_full_store_obj("JIS SHINJUKU", "JIS", pt)

                try:
                    html = app_module.render_template(
                        "store_detail.html",
                        store=store_obj,
                        store_analytics={},
                        store_insight={},
                        store_dossier={},
                        has_sufficient_store_data=True,
                        store_smart_cta={},
                        store_second_venues=[],
                        store_votes={},
                        watch_counts={},
                        area_info={"name": "新宿", "slug": "shinjuku"},
                        area_slug="shinjuku",
                        nearby_stores=[],
                        nearby_comparison={},
                        related_guides=[],
                        jp_holidays=[],
                        last_updated="2026-11-10 01:00",
                        is_staff=False,
                    )
                except Exception as e:
                    self.fail(f"Edge case {idx+1} raised rendering exception: {e}")

                self.assertIn("jis-pos-panel", html)
                if pt.get("is_reverse_waiting"):
                    self.assertIn("女性待ち逆転発生中", html)
                elif pt.get("is_women_waiting"):
                    self.assertIn("女性席待ち発生中", html)

    def test_render_non_jis_stores_no_panel(self):
        """オリエンタルラウンジ、ag、ASOBIBAR、ヤタコイ等の非JIS店舗でJIS POSパネルが絶対に描画されないこと"""
        non_jis_stores = [
            {"name": "オリエンタルラウンジ 新宿", "brand_label": "オリエンタルラウンジ"},
            {"name": "OLG 渋谷", "brand_label": "オリエンタルラウンジ"},
            {"name": "OLG ag 札幌", "brand_label": "ag"},
            {"name": "ASOBIBAR 梅田", "brand_label": "ASOBIBAR"},
            {"name": "YATAKOI 茶屋町", "brand_label": "ヤタコイ"},
        ]

        with self.app.test_request_context():
            for store in non_jis_stores:
                store_obj = self._build_full_store_obj(store["name"], store["brand_label"], pos_telemetry=None)

                html = app_module.render_template(
                    "store_detail.html",
                    store=store_obj,
                    store_analytics={},
                    store_insight={},
                    store_dossier={},
                    has_sufficient_store_data=True,
                    store_smart_cta={},
                    store_second_venues=[],
                    store_votes={},
                    watch_counts={},
                    area_info={"name": "テストエリア", "slug": "test"},
                    area_slug="test",
                    nearby_stores=[],
                    nearby_comparison={},
                    related_guides=[],
                    jp_holidays=[],
                    last_updated="2026-11-10 01:00",
                    is_staff=False,
                )

                self.assertNotIn('id="jis-pos-panel"', html, f"JIS panel leaked into non-JIS store: {store['name']}")
                self.assertNotIn('class="jis-pos-head"', html, f"JIS head leaked into non-JIS store: {store['name']}")

    def test_render_name_and_brand_label_variations(self):
        """brand_label が欠損していても name が JIS で始まる場合、および brand_label が JIS で name が異なる場合の両方で安全に描画されること"""
        test_cases = [
            # brand_label なし、name が "JIS UMEDA"
            {"name": "JIS UMEDA", "brand_label": None, "pos": {"telemetry_type": "jis_pos", "avg_party_size_total": 2.1}},
            # brand_label が "JIS"、name が "UMEDA" (接頭辞なし)
            {"name": "UMEDA", "brand_label": "JIS", "pos": {"telemetry_type": "jis_pos", "avg_party_size_total": 1.8}},
            # name に JIS 接頭辞がないが brand_label が "JIS"
            {"name": "BAR UMEDA", "brand_label": "JIS", "pos": {"telemetry_type": "jis_pos", "avg_party_size_total": 2.0}},
        ]
        with self.app.test_request_context():
            for tc in test_cases:
                store_obj = self._build_full_store_obj(tc["name"] or "JIS DUMMY", tc["brand_label"], tc["pos"])
                store_obj["name"] = tc["name"]
                store_obj["brand_label"] = tc["brand_label"]
                html = app_module.render_template(
                    "store_detail.html",
                    store=store_obj,
                    store_analytics={},
                    store_insight={},
                    store_dossier={},
                    has_sufficient_store_data=True,
                    store_smart_cta={},
                    store_second_venues=[],
                    store_votes={},
                    watch_counts={},
                    area_info={"name": "テストエリア", "slug": "test"},
                    area_slug="test",
                    nearby_stores=[],
                    nearby_comparison={},
                    related_guides=[],
                    jp_holidays=[],
                    last_updated="2026-11-10 01:00",
                    is_staff=False,
                )
                self.assertIn('id="jis-pos-panel"', html)


class TestAdversarialExtremeStress(unittest.TestCase):
    """4. アドバーサリアル極限ストレステスト（部分的マイグレーション＆大規模混在ロールアップ）"""

    def test_migrate_partial_existing_columns(self):
        """7カラムのうち一部のカラムが既に存在する状態でマイグレーションを実行しても安全に追加されること"""
        conn = sqlite3.connect(":memory:")
        cur = conn.cursor()
        cur.execute("""
            CREATE TABLE store_hourly_rollups (
                date_jst TEXT NOT NULL,
                hour_jst INTEGER NOT NULL,
                store_name TEXT NOT NULL,
                avg_party_size REAL,
                women_waiting_detected INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (date_jst, hour_jst, store_name)
            )
        """)
        conn.commit()

        # 連続5回実行
        for _ in range(5):
            local_db._migrate_jis_metrics_columns(conn, cur)

        cur.execute("PRAGMA table_info(store_hourly_rollups);")
        cols = {row[1]: row[2] for row in cur.fetchall()}
        expected_cols = [
            "avg_women_waiting_groups",
            "max_women_waiting_groups",
            "women_waiting_detected",
            "avg_party_size",
            "avg_normal_match_rate",
            "avg_vip_match_rate",
            "avg_match_rate_diff",
        ]
        for c in expected_cols:
            self.assertIn(c, cols)
        conn.close()

    def test_rollup_large_scale_batch_stress(self):
        """100件以上の多様なログ（JIS10店舗、非JIS店舗、境界値）を一括ロールアップしてもクラッシュせず正確に計算されること"""
        local_db.init_db()
        d_str = "2026-11-20"
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            jis_stores = [
                "JIS SAPPORO", "JIS OMIYA", "JIS SHINJUKU", "JIS NISHISHINJUKU",
                "JIS UMEDA", "JIS NAMBA", "JIS CHAYAMACHI", "JIS FUKUOKA",
                "JIS KUMAMOTO", "JIS MATSUYAMA"
            ]
            # 10店舗 × 各10サンプル = 100件のJISログ
            for s_idx, sname in enumerate(jis_stores):
                for sample_i in range(10):
                    ts = f"{d_str} 21:{sample_i*5:02d}:00"
                    cur.execute(
                        "INSERT OR REPLACE INTO monitor_logs (timestamp, store_name, women, men, source) VALUES (?, ?, ?, ?, ?)",
                        (ts, sname, 20 + sample_i, 15 + sample_i, "jis")
                    )
                    pos_dict = {
                        "telemetry_type": "jis_pos",
                        "avg_party_size_total": 2.0 + (sample_i * 0.1),
                        "normal_match_rate": 70.0 + sample_i,
                        "vip_match_rate": 85.0 + sample_i,
                        "match_rate_diff": 15.0,
                        "is_women_waiting": bool(sample_i % 2 == 0),
                        "is_reverse_waiting": bool(sample_i == 0),
                        "women_waiting_groups": sample_i,
                    }
                    cur.execute(
                        "INSERT OR REPLACE INTO store_pos_telemetry_logs (timestamp, store_name, telemetry_type, "
                        "men_waiting_groups, men_waiting_guests, matched_groups, vip_guests, women_waiting_groups, raw_json) "
                        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                        (ts, sname, "jis_pos", 1, 2, 5, 2, sample_i, json.dumps(pos_dict))
                    )
            conn.commit()
        finally:
            conn.close()

        res = local_db.rollup_store_hourly_stats(target_dates=[d_str])
        self.assertTrue(res["ok"])
        self.assertGreaterEqual(res["upserted_rollups"], 10)

        # 全10店舗のロールアップ行が正しく生成されていることを検証
        for sname in jis_stores:
            rows = local_db._fetch_all_rows(
                "SELECT sample_count, avg_party_size, avg_normal_match_rate, avg_vip_match_rate, avg_match_rate_diff, women_waiting_detected "
                "FROM store_hourly_rollups WHERE date_jst = ? AND hour_jst = 21 AND store_name = ?",
                (d_str, sname),
            )
            self.assertEqual(len(rows), 1, f"Missing rollup for {sname}")
            r = rows[0]
            self.assertEqual(r[0], 10)  # sample_count = 10
            self.assertAlmostEqual(float(r[1]), 2.45, places=2)  # avg of 2.0..2.9
            self.assertAlmostEqual(float(r[2]), 74.5, places=1)  # avg of 70..79
            self.assertAlmostEqual(float(r[3]), 89.5, places=1)  # avg of 85..94
            self.assertEqual(float(r[4]), 15.0)  # avg diff
            self.assertEqual(int(r[5]), 1)  # detected


if __name__ == "__main__":
    unittest.main()

