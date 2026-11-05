import datetime
import os
import re
import tempfile
import time
import unittest
from unittest import mock

# Ensure test DB & Sheets isolation before importing app/local_db
_TMP_DIR = tempfile.mkdtemp(prefix="aiseki_fc_test_")
os.environ.setdefault("LOCAL_DB_PATH", os.path.join(_TMP_DIR, "boot_test.db"))
os.environ.setdefault("JOB_STATE_DIR", os.path.join(_TMP_DIR, "job_state"))
os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault('SECRET_KEY', 'test-secret-key-12345')
os.environ.setdefault('STAFF_PASSCODE', '5555')
os.environ.setdefault('MASTER_PASSCODE', '2918')
os.environ.setdefault('ADMIN_API_SECRET', 'lounge2026')

import app as app_module
import local_db
import moat_intelligence

JST = datetime.timezone(datetime.timedelta(hours=9))


class TestForecastV2AndAccuracyTracking(unittest.TestCase):
    def setUp(self):
        self._orig_db_path = local_db.DB_PATH
        self._tmp_dir = tempfile.TemporaryDirectory(prefix="fc_acc_")
        self.db_path = os.path.join(self._tmp_dir.name, "test_forecast.db")
        local_db.DB_PATH = self.db_path
        local_db.clear_store_7d_cache(clear_benchmarks=True)
        local_db.init_db()
        app_module.app.config["TESTING"] = True
        self.client = app_module.app.test_client()
        app_module.clear_fast_http_caches()

    def tearDown(self):
        local_db.DB_PATH = self._orig_db_path
        local_db.clear_store_7d_cache(clear_benchmarks=True)
        self._tmp_dir.cleanup()

    def test_edge_cases_quiet_closed_and_korea_exclusion(self):
        """Empty, low-headcount (<6), closed, and negative/None inputs behave safely without division-by-zero."""
        t_22 = datetime.datetime(2026, 10, 30, 22, 0, tzinfo=JST)
        # 1) 0:0 empty store
        r_zero = moat_intelligence.predict_store_60m_dynamics("OLG 渋谷本店", 0, 0, is_open_now=True, now_jst=t_22)
        self.assertIsNone(r_zero["pred_60m_wpct"])
        self.assertEqual(r_zero["alert_code"], "quiet")

        # 2) Boundary total=5 (<6) vs total=6 (>=6)
        r_five = moat_intelligence.predict_store_60m_dynamics("OLG 渋谷本店", 3, 2, is_open_now=True, now_jst=t_22)
        self.assertIsNone(r_five["pred_60m_wpct"])

        r_six = moat_intelligence.predict_store_60m_dynamics("OLG 渋谷本店", 4, 2, is_open_now=True, now_jst=t_22)
        self.assertIsNotNone(r_six["pred_60m_wpct"])
        self.assertGreaterEqual(r_six["pred_60m_wpct"], 8)
        self.assertLessEqual(r_six["pred_60m_wpct"], 92)

        # 3) Closed store even if stale counts passed
        r_closed = moat_intelligence.predict_store_60m_dynamics("OLG 渋谷本店", 20, 15, is_open_now=False, now_jst=t_22)
        self.assertIsNone(r_closed["pred_60m_wpct"])

        # 4) Negative / None defensive clamping
        r_neg = moat_intelligence.predict_store_60m_dynamics("OLG 渋谷本店", -5, None, is_open_now=True, now_jst=t_22)
        self.assertIsNone(r_neg["pred_60m_wpct"])
        self.assertEqual(r_neg["pred_60m_women"], 0)
        self.assertEqual(r_neg["pred_60m_men"], 0)

        # 5) Korea stores are excluded from forecast_accuracy_logs recording
        local_db.insert_logs(
            [
                {"name": "OLG ソウル カンナム", "women": 25, "men": 20, "source": "oriental"},
                {"name": "OLG 新宿", "women": 24, "men": 16, "source": "oriental"},
            ],
            "2026-10-30 21:00:00",
        )
        rows = local_db._fetch_all_rows("SELECT store_name FROM forecast_accuracy_logs")
        recorded_names = [r[0] for r in rows]
        self.assertIn("OLG 新宿", recorded_names)
        self.assertNotIn("OLG ソウル カンナム", recorded_names)

    def test_continuous_knot_interpolation_and_closing_hazard(self):
        """v2.1 eliminates abrupt top-of-the-hour jumps and decays headcounts smoothly near official closing time."""
        # Continuity across 21:59 -> 22:01
        t_2159 = datetime.datetime(2026, 10, 30, 21, 59, tzinfo=JST)
        t_2201 = datetime.datetime(2026, 10, 30, 22, 1, tzinfo=JST)
        f1 = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 30, 22, True, 2, 2, now_jst=t_2159, weather_map={})
        f2 = moat_intelligence.predict_store_60m_dynamics("OLG 新宿", 30, 22, True, 2, 2, now_jst=t_2201, weather_map={})
        self.assertLessEqual(abs(f1["pred_60m_wpct"] - f2["pred_60m_wpct"]), 2)
        self.assertLessEqual(abs(f1["pred_60m_women"] - f2["pred_60m_women"]), 2)

        # Closing hazard: OLG 上野 closes at 01:00 (25:00) on Wednesday night (Mon-Thu 18-25)
        wed_0035 = datetime.datetime(2026, 10, 29, 0, 35, tzinfo=JST)  # Thu 00:35 belongs to Wed shift -> 25m before close
        wed_2130 = datetime.datetime(2026, 10, 28, 21, 30, tzinfo=JST)
        fc_open = moat_intelligence.predict_store_60m_dynamics("OLG 上野", 20, 16, True, 0, 0, now_jst=wed_2130, weather_map={})
        fc_closing = moat_intelligence.predict_store_60m_dynamics("OLG 上野", 20, 16, True, 0, 0, now_jst=wed_0035, weather_map={})
        self.assertLess(
            fc_closing["pred_60m_women"] + fc_closing["pred_60m_men"],
            fc_open["pred_60m_women"] + fc_open["pred_60m_men"],
        )
        self.assertLess(fc_closing["pred_60m_women"], 20)

    def test_record_and_auto_resolve_60m_lifecycle_and_empty_room_safety(self):
        """Forecasts recorded at t=21:00 remain pending at t=21:30 and resolve accurately at t=22:02, including 0:0 safety."""
        d_str = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            # 1) t = 21:00 -> record 2 stores
            local_db.insert_logs(
                [
                    {"name": "OLG 渋谷本店", "women": 30, "men": 20, "women_diff": 2, "men_diff": 4, "source": "oriental"},
                    {"name": "JIS SHINJUKU", "women": 12, "men": 10, "women_diff": -1, "men_diff": 0, "source": "jis"},
                ],
                f"{d_str} 21:00:00",
            )
            rep_pending = local_db.get_forecast_accuracy_report(days=7)
            self.assertEqual(rep_pending["summary"]["pending_count"], 2)
            self.assertEqual(rep_pending["summary"]["resolved_count"], 0)

            # 2) t = 21:30 (only 30m later) -> must NOT resolve the 21:00 forecasts yet
            local_db.insert_logs(
                [
                    {"name": "OLG 渋谷本店", "women": 33, "men": 25, "source": "oriental"},
                    {"name": "JIS SHINJUKU", "women": 6, "men": 5, "source": "jis"},
                ],
                f"{d_str} 21:30:00",
            )
            rep_mid = local_db.get_forecast_accuracy_report(days=7)
            self.assertEqual(rep_mid["summary"]["resolved_count"], 0)
            self.assertEqual(rep_mid["summary"]["pending_count"], 4)

            # 3) t = 22:02 (62m after 21:00): Shibuya has 34:28 (55%), JIS emptied to 0:0!
            local_db.insert_logs(
                [
                    {"name": "OLG 渋谷本店", "women": 34, "men": 28, "source": "oriental"},
                    {"name": "JIS SHINJUKU", "women": 0, "men": 0, "source": "jis"},
                ],
                f"{d_str} 22:02:00",
            )

        rows_2100 = local_db._fetch_all_rows(
            "SELECT store_name, resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt "
            "FROM forecast_accuracy_logs WHERE predicted_at = ? ORDER BY store_name",
            (f"{d_str} 21:00:00",),
        )
        self.assertEqual(len(rows_2100), 2)
        by_name = {r[0]: r for r in rows_2100}

        # JIS emptied to 0:0 -> resolved_at is populated, actual_wpct is None (never corrupts ratio MAE!)
        jis_row = by_name["JIS SHINJUKU"]
        self.assertEqual(jis_row[1], f"{d_str} 22:02:00")
        self.assertEqual(jis_row[2], 0)
        self.assertEqual(jis_row[3], 0)
        self.assertIsNone(jis_row[4])
        self.assertIsNone(jis_row[5])

        # Shibuya 34:28 -> actual_wpct = 55%, ratio error recorded cleanly
        shib_row = by_name["OLG 渋谷本店"]
        self.assertEqual(shib_row[1], f"{d_str} 22:02:00")
        self.assertEqual(shib_row[2], 34)
        self.assertEqual(shib_row[3], 28)
        self.assertEqual(shib_row[4], 55)
        self.assertIsNotNone(shib_row[5])

        rep_after = local_db.get_forecast_accuracy_report(days=7)
        self.assertEqual(rep_after["summary"]["resolved_count"], 1)
        self.assertIsNotNone(rep_after["summary"]["mae_wpct"])

    def test_overdue_pending_resolution_from_monitor_logs_after_restart(self):
        """If worker restarts and misses the live [+0m..+25m] window, overdue pending rows resolve from monitor_logs."""
        d_str = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            local_db.insert_logs(
                [{"name": "OLG 梅田", "women": 28, "men": 20, "source": "oriental"}],
                f"{d_str} 20:00:00",
            )
            # Simulate monitor_logs at 21:00 inserted via batch sync while worker was restarting
            local_db.insert_logs_with_ts(
                [{"ts": f"{d_str} 21:01:00", "name": "OLG 梅田", "women": 32, "men": 26, "source": "oriental"}]
            )
            # Next live tick arrives at 22:00 (60m overdue for the 21:00 target_at)
            res = local_db.resolve_due_forecasts_only(
                [{"name": "OLG 梅田", "women": 30, "men": 30}],
                f"{d_str} 22:00:00",
            )
            self.assertEqual(res["resolved"], 1)

        row = local_db._fetch_all_rows(
            "SELECT resolved_at, actual_women, actual_men, actual_wpct FROM forecast_accuracy_logs WHERE store_name = 'OLG 梅田'"
        )[0]
        self.assertEqual(row[0], f"{d_str} 21:01:00")
        self.assertEqual(row[1], 32)
        self.assertEqual(row[2], 26)
        self.assertEqual(row[3], round(32 / 58 * 100))

    def test_online_residual_bias_feedback_and_store_accuracy_badge(self):
        """Consistent over-prediction residuals produce negative online adjustment and populate forecast_60m['accuracy']."""
        now_dt = datetime.datetime.now(JST).replace(minute=15, second=0, microsecond=0)
        base_dt = now_dt - datetime.timedelta(days=2)
        base_dt = base_dt.replace(hour=22, minute=0, second=0)

        # Insert 8 resolved rows where model over-predicted by +10pt (pred=62%, actual=52%)
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            for i in range(8):
                p_dt = base_dt + datetime.timedelta(minutes=15 * i)
                t_dt = p_dt + datetime.timedelta(minutes=60)
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, v1_pred_wpct,
                        flip_risk_pct, alert_code,
                        resolved_at, actual_women, actual_men, actual_wpct,
                        error_wpct_pt, abs_error_wpct_pt, v1_abs_error_wpct_pt,
                        error_women, error_men
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        p_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        t_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        "OLG 京都",
                        moat_intelligence.FORECAST_MODEL_VERSION,
                        25, 18, 58,
                        28, 17, 62, 66,
                        18, "steady",
                        t_dt.strftime("%Y-%m-%d %H:%M:%S"),
                        21, 19, 52,
                        10, 10, 14,
                        7, -2,
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        local_db._invalidate_forecast_caches()
        adj = local_db.get_cached_forecast_residual_adjustment("OLG 京都", 22)
        self.assertLess(adj, -1.0, "Positive prediction error (+10pt) must yield negative bias correction")

        eval_dt = datetime.datetime(2026, 10, 30, 22, 15, tzinfo=JST)
        fc_no_bias = moat_intelligence.predict_store_60m_dynamics(
            "OLG 京都", 25, 18, True, 0, 0, now_jst=eval_dt, weather_map={}, apply_online_bias=False
        )
        fc_with_bias = moat_intelligence.predict_store_60m_dynamics(
            "OLG 京都", 25, 18, True, 0, 0, now_jst=eval_dt, weather_map={}, apply_online_bias=True
        )
        self.assertLessEqual(fc_with_bias["pred_60m_wpct"], fc_no_bias["pred_60m_wpct"])
        self.assertLess(fc_with_bias["online_bias_adj_pt"], 0.0)

        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            enriched = moat_intelligence.enrich_stores_with_moat_signals(
                [{"name": "OLG 京都", "women": 25, "men": 18, "is_open_now": True}],
                now_jst=eval_dt,
            )
        acc_badge = enriched[0]["forecast_60m"].get("accuracy")
        self.assertIsNotNone(acc_badge)
        self.assertEqual(acc_badge["resolved_count"], 8)
        self.assertAlmostEqual(acc_badge["mae_wpct"], 10.0, places=1)

    def test_backfill_api_stats_ui_and_brand_compliance(self):
        """Historical backfill populates forecast_accuracy_logs and exposes brand-compliant /api/forecast-accuracy & /stats."""
        now_naive = datetime.datetime.now(JST).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        start_t = now_naive - datetime.timedelta(hours=5)
        batch_logs = []
        for step in range(10):
            ts_s = (start_t + datetime.timedelta(minutes=30 * step)).strftime("%Y-%m-%d %H:%M:%S")
            batch_logs.append({"ts": ts_s, "name": "OLG 渋谷本店", "women": 28 + step, "men": 22 + step, "source": "oriental"})
            batch_logs.append({"ts": ts_s, "name": "OLG ag梅田", "women": 24, "men": 18, "source": "oriental"})
        local_db.insert_logs_with_ts(batch_logs)

        bf = local_db.backfill_forecast_accuracy_from_history(days=3, min_existing=5, max_rows=200, force=True)
        self.assertGreaterEqual(bf["inserted"], 8)

        # Verify /api/forecast-accuracy JSON endpoint
        resp_api = self.client.get("/api/forecast-accuracy?days=7")
        self.assertEqual(resp_api.status_code, 200)
        payload = resp_api.get_json()
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["model_version"], moat_intelligence.FORECAST_MODEL_VERSION)
        self.assertGreaterEqual(payload["summary"]["resolved_count"], 8)
        self.assertIsNotNone(payload["summary"]["mae_wpct"])

        # Strict brand rule check on all display_name fields in JSON
        for item in payload["recent_resolved"] + payload["by_store"]:
            dname = item["display_name"]
            self.assertNotIn("OLG", dname)
            self.assertEqual(re.findall(r"オリエンタル(?!ラウンジ)", dname), [])
            self.assertNotIn("オリラジ", dname)

        # Verify public /stats page contains #forecast-accuracy and obeys brand rule
        app_module.clear_fast_http_caches()
        resp_stats = self.client.get("/stats")
        self.assertEqual(resp_stats.status_code, 200)
        html_stats = resp_stats.data.decode("utf-8")
        self.assertIn('id="forecast-accuracy"', html_stats)
        self.assertIn("60分後AI推移予測の「答え合わせ」", html_stats)
        self.assertNotIn("OLG", html_stats)
        self.assertEqual(re.findall(r"オリエンタル(?!ラウンジ)", html_stats), [])

        # Verify master /staff/analytics renders #forecast-accuracy-audit
        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True
        with mock.patch("affiliate_manager.get_affiliate_slots_status", return_value=None), \
             mock.patch("archive_data.get_retention_status", return_value=None):
            resp_staff = self.client.get("/staff/analytics")
        self.assertEqual(resp_staff.status_code, 200)
        self.assertIn("forecast-accuracy-audit", resp_staff.data.decode("utf-8"))

    def test_63_store_enrichment_sub_millisecond_budget(self):
        """63-store v2.1 enrichment stays fast (<5ms per batch even on cold Python loop)."""
        stores = [
            {
                "name": name,
                "women": 22 + (i % 15),
                "men": 18 + (i % 12),
                "women_diff": (i % 5) - 2,
                "men_diff": (i % 7) - 3,
                "is_open_now": True,
            }
            for i, name in enumerate(list(moat_intelligence.STORE_P99_CAPACITY.keys())[:63])
        ]
        dt = datetime.datetime(2026, 10, 30, 22, 20, tzinfo=JST)
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            # Warmup once
            moat_intelligence.enrich_stores_with_moat_signals(stores, now_jst=dt)
            t0 = time.perf_counter()
            for _ in range(10):
                moat_intelligence.enrich_stores_with_moat_signals(stores, now_jst=dt)
            avg_ms = ((time.perf_counter() - t0) / 10.0) * 1000.0
        self.assertLess(avg_ms, 8.0, f"Expected <8ms for 63 stores, got {avg_ms:.2f}ms")

    def test_postgres_dialect_pyformat_escaping_and_full_lifecycle(self):
        """Under _is_pg()=True, psycopg2 pyformat ('%' escaping + '%s' placeholders) works across schema, record, backfill, and report."""
        import contextlib
        import sqlite3

        class _Psycopg2StrictCursor:
            def __init__(self, raw_cur):
                self._cur = raw_cur

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                self._cur.close()
                return False

            @property
            def rowcount(self):
                return self._cur.rowcount

            @staticmethod
            def _to_sqlite_sql(sql, params_given):
                assert "?" not in sql, f"Unconverted '?' placeholder leaked into PG SQL: {sql}"
                s = sql.replace("BIGSERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                s = s.replace("SERIAL PRIMARY KEY", "INTEGER PRIMARY KEY AUTOINCREMENT")
                if params_given:
                    s = s.replace("%%", "\x00PCT\x00").replace("%s", "?").replace("\x00PCT\x00", "%")
                return s

            def execute(self, sql, params=None):
                if params is not None:
                    # Exact psycopg2 pyformat check: raises ValueError/TypeError on any unescaped '%'
                    _ = sql % tuple("X" for _ in params)
                    if "ANY(%s)" in sql and len(params) == 1 and isinstance(params[0], (list, tuple)):
                        ids = list(params[0])
                        qmarks = ",".join("?" for _ in ids)
                        s = sql.replace("= ANY(%s)", f"IN ({qmarks})")
                        return self._cur.execute(s, tuple(ids))
                    return self._cur.execute(self._to_sqlite_sql(sql, True), tuple(params))
                return self._cur.execute(self._to_sqlite_sql(sql, False))

            def executemany(self, sql, seq_of_params):
                seq = [tuple(p) for p in seq_of_params]
                for p in seq:
                    _ = sql % tuple("X" for _ in p)
                return self._cur.executemany(self._to_sqlite_sql(sql, True), seq)

            def fetchall(self):
                return self._cur.fetchall()

            def fetchone(self):
                return self._cur.fetchone()

        class _Psycopg2StrictConn:
            def __init__(self, db_file):
                self._conn = sqlite3.connect(db_file)

            def cursor(self):
                return _Psycopg2StrictCursor(self._conn.cursor())

            def commit(self):
                return self._conn.commit()

            def rollback(self):
                return self._conn.rollback()

            def close(self):
                return self._conn.close()

        @contextlib.contextmanager
        def _strict_pg_connection():
            c = _Psycopg2StrictConn(self.db_path)
            try:
                yield c
            finally:
                c.close()

        def _strict_execute_pg_values_batch(sql, rows, page_size=500):
            assert "?" not in sql, f"Unconverted '?' leaked into execute_values SQL: {sql}"
            assert sql.count("%s") == 1, f"execute_values SQL must contain exactly one '%s': {sql}"
            _ = sql % ("()",)  # Raises if any stray unescaped '%' exists
            if not rows:
                return 0
            ncols = len(rows[0])
            placeholders = "(" + ", ".join("?" for _ in range(ncols)) + ")"
            sqlite_sql = sql.replace("%s", placeholders)
            conn = sqlite3.connect(self.db_path)
            try:
                cur = conn.cursor()
                cur.executemany(sqlite_sql, [tuple(r) for r in rows])
                conn.commit()
                return cur.rowcount if cur.rowcount and cur.rowcount > 0 else len(rows)
            finally:
                conn.close()

        # Seed >=12 canonical rows + Korea rows first so backfill threshold (len(raw_rows) >= 10) passes
        now_naive = datetime.datetime.now(JST).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        t0 = now_naive - datetime.timedelta(hours=4)
        seed_logs = []
        for step in range(4):
            ts_s = (t0 + datetime.timedelta(minutes=60 * step)).strftime("%Y-%m-%d %H:%M:%S")
            seed_logs.append({"ts": ts_s, "name": "OLG 新宿", "women": 24 + step, "men": 18 + step, "source": "oriental"})
            seed_logs.append({"ts": ts_s, "name": "OLG 福岡", "women": 22 + step, "men": 16 + step, "source": "oriental"})
            seed_logs.append({"ts": ts_s, "name": "JIS SHINJUKU", "women": 18 + step, "men": 14 + step, "source": "jis"})
            seed_logs.append({"ts": ts_s, "name": "OLG ソウル カンナム", "women": 30, "men": 25, "source": "oriental"})
        local_db.insert_logs_with_ts(seed_logs)

        local_db._forecast_schema_ready.clear()
        local_db._invalidate_forecast_caches()
        try:
            with mock.patch.object(local_db, "_is_pg", return_value=True), \
                 mock.patch.object(local_db.turbo_store, "pg_connection", side_effect=_strict_pg_connection), \
                 mock.patch.object(local_db.turbo_store, "execute_pg_values_batch", side_effect=_strict_execute_pg_values_batch), \
                 mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
                local_db._ensure_forecast_schema()
                bf = local_db.backfill_forecast_accuracy_from_history(days=3, min_existing=50, max_rows=100, force=True)
                self.assertGreaterEqual(bf["inserted"], 6)

                y_str = (now_naive - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
                # Also record an old unresolvable row at 17:30 so 22:10 exercises PG ANY(%s) stale deletion (>180m)
                local_db.record_and_resolve_forecasts(
                    [{"name": "OLG 梅田", "women": 18, "men": 12}], f"{y_str} 18:00:00", resolve_only=False
                )
                rec = local_db.record_and_resolve_forecasts(
                    [{"name": "OLG 渋谷本店", "women": 26, "men": 19}], f"{y_str} 21:10:00", resolve_only=False
                )
                self.assertEqual(rec["recorded"], 1)
                res = local_db.record_and_resolve_forecasts(
                    [{"name": "OLG 渋谷本店", "women": 28, "men": 22}], f"{y_str} 22:10:00", resolve_only=True
                )
                self.assertGreaterEqual(res["resolved"], 1)

                rep = local_db.get_forecast_accuracy_report(days=7)
                self.assertGreaterEqual(rep["summary"]["resolved_count"], 7)
                store_names = {r["store_name"] for r in rep["recent_resolved"]}
                self.assertNotIn("OLG ソウル カンナム", store_names)
        finally:
            local_db._forecast_schema_ready.clear()
            local_db._ensure_forecast_schema()

    def test_stale_fetched_at_padding_and_premature_54m_tick_rejected(self):
        """5-minute ticks do not resolve 60m forecasts early at 54m, and unscraped stale-padded items never corrupt actuals."""
        d_str = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            local_db.insert_logs(
                [
                    {"name": "OLG 渋谷本店", "women": 30, "men": 20, "fetched_at": 1000.0},
                    {"name": "OLG 新宿", "women": 26, "men": 18, "fetched_at": 1000.0},
                ],
                f"{d_str} 21:00:00",
            )

            # 1) 21:54:30 (54.5m elapsed, diff_min = -5.5m) -> must NOT resolve prematurely!
            r_early = local_db.resolve_due_forecasts_only(
                [
                    {"name": "OLG 渋谷本店", "women": 99, "men": 1, "fetched_at": 4270.0},
                    {"name": "OLG 新宿", "women": 99, "men": 1, "fetched_at": 4270.0},
                ],
                f"{d_str} 21:54:30",
            )
            self.assertEqual(r_early["resolved"], 0)

            # 2) 22:00:30: Shibuya is freshly scraped (fetched_at=4630.0), Shinjuku is stale-padded (fetched_at=0.0)
            r_due = local_db.resolve_due_forecasts_only(
                [
                    {"name": "OLG 渋谷本店", "women": 28, "men": 22, "fetched_at": 4630.0},
                    {"name": "OLG 新宿", "women": 30, "men": 20, "fetched_at": 0.0},
                ],
                f"{d_str} 22:00:30",
            )
            self.assertEqual(r_due["resolved"], 1)

            rows = dict(
                local_db._fetch_all_rows(
                    "SELECT store_name, resolved_at FROM forecast_accuracy_logs WHERE predicted_at = ?",
                    (f"{d_str} 21:00:00",),
                )
            )
            self.assertEqual(rows["OLG 渋谷本店"], f"{d_str} 22:00:30")
            self.assertIsNone(rows["OLG 新宿"], "Stale fetched_at=0.0 item must not resolve forecast!")

            # 3) Fresh Shinjuku scrape arrives at 22:05:00 -> resolves cleanly
            r_fresh = local_db.resolve_due_forecasts_only(
                [{"name": "OLG 新宿", "women": 24, "men": 21, "fetched_at": 4900.0}],
                f"{d_str} 22:05:00",
            )
            self.assertEqual(r_fresh["resolved"], 1)

    def test_non_canonical_seed_rejection_and_zero_404_store_links(self):
        """Synthetic non-canonical store names ('ag渋谷', 'オリエンタルラウンジ 渋谷本店') are excluded and all /stats links resolve."""
        now_naive = datetime.datetime.now(JST).replace(tzinfo=None, minute=0, second=0, microsecond=0)
        t_seed = (now_naive - datetime.timedelta(hours=4)).strftime("%Y-%m-%d %H:%M:%S")
        t_seed_60 = (now_naive - datetime.timedelta(hours=3)).strftime("%Y-%m-%d %H:%M:%S")
        t_clean = (now_naive - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")
        t_clean_60 = (now_naive - datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S")

        local_db.insert_logs_with_ts(
            [
                # Tainted synthetic seed timestamps (even mixed with a valid store in same batch -> batch tainted!)
                {"ts": t_seed, "name": "ag渋谷", "women": 20, "men": 15, "source": "oriental"},
                {"ts": t_seed, "name": "オリエンタルラウンジ 渋谷本店", "women": 25, "men": 20, "source": "oriental"},
                {"ts": t_seed, "name": "OLG 京都", "women": 25, "men": 20, "source": "oriental"},
                {"ts": t_seed_60, "name": "ag渋谷", "women": 22, "men": 18, "source": "oriental"},
                {"ts": t_seed_60, "name": "オリエンタルラウンジ 渋谷本店", "women": 24, "men": 22, "source": "oriental"},
                {"ts": t_seed_60, "name": "OLG 京都", "women": 24, "men": 22, "source": "oriental"},
                # Genuine canonical timestamps (including legacy canonical OLG 梅田 which 301-redirects)
                {"ts": t_clean, "name": "OLG 渋谷本店", "women": 28, "men": 22, "source": "oriental"},
                {"ts": t_clean, "name": "OLG 梅田", "women": 24, "men": 18, "source": "oriental"},
                {"ts": t_clean, "name": "OLG 新宿", "women": 26, "men": 20, "source": "oriental"},
                {"ts": t_clean_60, "name": "OLG 渋谷本店", "women": 30, "men": 26, "source": "oriental"},
                {"ts": t_clean_60, "name": "OLG 梅田", "women": 22, "men": 20, "source": "oriental"},
                {"ts": t_clean_60, "name": "OLG 新宿", "women": 25, "men": 21, "source": "oriental"},
            ]
        )
        local_db.backfill_forecast_accuracy_from_history(days=3, min_existing=10, max_rows=100, force=True)

        recorded_stores = {r[0] for r in local_db._fetch_all_rows("SELECT DISTINCT store_name FROM forecast_accuracy_logs")}
        self.assertNotIn("ag渋谷", recorded_stores)
        self.assertNotIn("オリエンタルラウンジ 渋谷本店", recorded_stores)
        self.assertNotIn("OLG 京都", recorded_stores, "Batch tainted by synthetic seed store must be discarded")
        self.assertIn("OLG 渋谷本店", recorded_stores)
        self.assertIn("OLG 梅田", recorded_stores)

        app_module.clear_fast_http_caches()
        html_stats = self.client.get("/stats").data.decode("utf-8")
        # Extract all /store/<slug> links rendered on /stats and verify none 404/410
        hrefs = sorted(set(re.findall(r'href="(/store/[^"#?]+)"', html_stats)))
        self.assertGreaterEqual(len(hrefs), 1)
        for href in hrefs:
            r_link = self.client.get(href)
            self.assertIn(r_link.status_code, (200, 301, 302), f"Broken store link {href} returned {r_link.status_code}")

        # Even if a store is temporarily hidden (410 Gone), /stats must never emit a broken 410 link to it
        with mock.patch("ops_governance.get_hidden_stores", return_value={"OLG 新宿", "JIS SHINJUKU"}):
            app_module.clear_fast_http_caches()
            html_hidden = self.client.get("/stats").data.decode("utf-8")
            hidden_hrefs = set(re.findall(r'href="(/store/[^"#?]+)"', html_hidden))
            self.assertNotIn("/store/oriental-lounge-shinjuku", hidden_hrefs)
            self.assertNotIn("/store/jis-shinjuku", hidden_hrefs)

        # Verify Light Mode high-contrast overrides are present on /stats
        self.assertIn("body.theme-light .fc-err-pill.good", html_stats)

    def test_store_query_normalization_uncapped_pending_and_antistampede(self):
        """/api/forecast-accuracy?store=... resolves brand/slug aliases, pending_count is uncapped (>60), and backfill is single-flight."""
        import threading

        d_str = (datetime.datetime.now(JST) - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            local_db.insert_logs(
                [{"name": "OLG 渋谷本店", "women": 28, "men": 20}],
                f"{d_str} 21:00:00",
            )
            local_db.insert_logs(
                [{"name": "OLG 渋谷本店", "women": 26, "men": 24}],
                f"{d_str} 22:00:00",
            )

        # Query by brand-safe display name AND by URL slug -> both resolve to canonical brand-safe 'オリエンタルラウンジ 渋谷本店'
        for q in ("オリエンタルラウンジ 渋谷本店", "oriental-lounge-shibuya-honten", "OLG 渋谷本店"):
            resp = self.client.get(f"/api/forecast-accuracy?days=7&store={q}")
            self.assertEqual(resp.status_code, 200)
            js = resp.get_json()
            self.assertEqual(js["store_filter"], "オリエンタルラウンジ 渋谷本店")
            self.assertEqual(js["summary"]["resolved_count"], 1)

        # Insert 65 pending rows across distinct timestamps -> pending_count must equal 66 (1 at 22:00 + 65), not capped at 60/40
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            base_p = datetime.datetime.now(JST).replace(tzinfo=None, second=0, microsecond=0) - datetime.timedelta(minutes=50)
            for i in range(65):
                p_ts = (base_p + datetime.timedelta(seconds=20 * i)).strftime("%Y-%m-%d %H:%M:%S")
                t_ts = (base_p + datetime.timedelta(minutes=60, seconds=20 * i)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (p_ts, t_ts, "OLG 新宿", moat_intelligence.FORECAST_MODEL_VERSION, 20, 15, 57, 22, 16, 58),
                )
            conn.commit()
        finally:
            conn.close()
        local_db._invalidate_forecast_caches()
        rep_many = local_db.get_forecast_accuracy_report(days=7)
        self.assertEqual(rep_many["summary"]["pending_count"], 66)
        self.assertEqual(len(rep_many["pending_forecasts"]), 30)

        # Anti-stampede check: 1) ?store=unknown with TESTING=False never triggers backfill
        app_module.app.config["TESTING"] = False
        try:
            with mock.patch.object(local_db, "trigger_forecast_backfill_async", return_value=False) as m_trig:
                r_unk = self.client.get("/api/forecast-accuracy?store=unknown_nonexistent_xyz")
                self.assertEqual(r_unk.status_code, 200)
                m_trig.assert_not_called()
        finally:
            app_module.app.config["TESTING"] = True

        # 2) Concurrent trigger_forecast_backfill_async calls spawn at most 1 worker while in-flight
        gate = threading.Event()
        entered = threading.Event()
        call_counter = []

        def _slow_backfill(**kwargs):
            call_counter.append(1)
            entered.set()
            gate.wait(timeout=2.0)
            return {"inserted": 0}

        with mock.patch.object(local_db, "backfill_forecast_accuracy_from_history", side_effect=_slow_backfill):
            results = [local_db.trigger_forecast_backfill_async(days=7, min_existing=999999) for _ in range(8)]
            entered.wait(timeout=1.0)
            gate.set()
            # Wait briefly for daemon thread cleanup
            for _ in range(20):
                if not local_db._forecast_backfill_in_flight:
                    break
                time.sleep(0.01)
        self.assertEqual(sum(1 for x in results if x), 1)
        self.assertEqual(len(call_counter), 1)

    def test_v22_utc_normalization_unilateral_rounding_and_signed_ui(self):
        """UTC-aware datetimes convert to JST accurately, unilateral 8:0 crowds never round to 1:1, and negative reduction avoids '--'."""
        dt_jst = datetime.datetime(2026, 10, 30, 22, 15, tzinfo=JST)
        dt_utc = dt_jst.astimezone(datetime.timezone.utc)  # 13:15 UTC == 22:15 JST

        fc_jst = moat_intelligence.predict_store_60m_dynamics("OLG 渋谷本店", 30, 20, True, 2, 3, now_jst=dt_jst, weather_map={})
        fc_utc = moat_intelligence.predict_store_60m_dynamics("OLG 渋谷本店", 30, 20, True, 2, 3, now_jst=dt_utc, weather_map={})
        self.assertEqual(fc_jst["pred_60m_wpct"], fc_utc["pred_60m_wpct"])
        self.assertEqual(fc_jst["pred_60m_women"], fc_utc["pred_60m_women"])
        self.assertEqual(fc_jst["flip_risk_pct"], fc_utc["flip_risk_pct"])

        # Unilateral headcount: 6 women, 0 men near closing -> if pred_60m_wpct >= 75, pred_60m_men must not become phantom 1 against 1 woman
        wed_0035 = datetime.datetime(2026, 10, 29, 0, 35, tzinfo=JST)
        fc_uni = moat_intelligence.predict_store_60m_dynamics("OLG 上野", 6, 0, True, 0, 0, now_jst=wed_0035, weather_map={})
        if fc_uni["pred_60m_wpct"] >= 75:
            self.assertGreater(fc_uni["pred_60m_women"], fc_uni["pred_60m_men"])

        # Negative mae_reduction_pct never renders double-minus '--' on /stats or /staff/analytics
        fake_rep = local_db.get_forecast_accuracy_report(days=7)
        fake_rep["summary"]["mae_wpct"] = 9.5
        fake_rep["summary"]["v1_mae_wpct"] = 8.0
        fake_rep["summary"]["mae_reduction_pct"] = -18.8
        app_module.clear_fast_http_caches()
        with mock.patch("local_db.get_forecast_accuracy_report", return_value=fake_rep):
            html_s = self.client.get("/stats").data.decode("utf-8")
            self.assertNotIn("--18.8%", html_s)
            self.assertIn("+18.8%", html_s)

            with self.client.session_transaction() as sess:
                sess["is_staff"] = True
                sess["is_master"] = True
            with mock.patch("affiliate_manager.get_affiliate_slots_status", return_value=None), \
                 mock.patch("archive_data.get_retention_status", return_value=None):
                html_staff = self.client.get("/staff/analytics").data.decode("utf-8")
            self.assertNotIn("--18.8%", html_staff)
            self.assertIn("+18.8%", html_staff)

    def test_v30_seo_store_name_normalization_and_city_peer_spillover(self):
        """SEO display names resolve to identical v3.0 priors/capacities as internal keys, and city peer context responds directionally and safely."""
        dt = datetime.datetime(2026, 10, 30, 22, 15, tzinfo=JST)

        for raw_key, seo_alias in (
            ("OLG 渋谷本店", "オリエンタルラウンジ 渋谷本店"),
            ("OLG ag梅田", "ag（アグ）梅田"),
            ("OLG ag仙台", "ag 仙台"),
            ("YATAKOI UMEDA", "ヤタコイ 梅田"),
        ):
            fc_raw = moat_intelligence.predict_store_60m_dynamics(
                raw_key, 28, 20, True, 2, 3, now_jst=dt, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=()
            )
            fc_seo = moat_intelligence.predict_store_60m_dynamics(
                seo_alias, 28, 20, True, 2, 3, now_jst=dt, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=()
            )
            self.assertEqual(fc_raw["model_version"], "v3.0-statespace")
            self.assertEqual(fc_raw["pred_60m_wpct"], fc_seo["pred_60m_wpct"], f"Mismatch for {seo_alias}")
            self.assertEqual(fc_raw["pred_60m_women"], fc_seo["pred_60m_women"])
            self.assertEqual(fc_raw["pred_60m_men"], fc_seo["pred_60m_men"])
            self.assertEqual(fc_raw["flip_risk_pct"], fc_seo["flip_risk_pct"])

        # City peer spillover directional check: female-rich peers vs male-saturated peers in Tokyo
        stores_fem_peers = [
            {"name": "オリエンタルラウンジ 渋谷本店", "women": 24, "men": 24, "is_open_now": True},
            {"name": "オリエンタルラウンジ 新宿", "women": 95, "men": 25, "is_open_now": True},
            {"name": "オリエンタルラウンジ 恵比寿", "women": 85, "men": 25, "is_open_now": True},
        ]
        stores_male_peers = [
            {"name": "オリエンタルラウンジ 渋谷本店", "women": 24, "men": 24, "is_open_now": True},
            {"name": "オリエンタルラウンジ 新宿", "women": 25, "men": 95, "is_open_now": True},
            {"name": "オリエンタルラウンジ 恵比寿", "women": 25, "men": 85, "is_open_now": True},
        ]
        pmap_f = moat_intelligence.build_city_peer_context(stores_fem_peers)
        pmap_m = moat_intelligence.build_city_peer_context(stores_male_peers)
        self.assertEqual(pmap_f["OLG 渋谷本店"]["peer_women"], 180)
        self.assertEqual(pmap_f["OLG 渋谷本店"]["peer_stores"], 2)

        fc_peer_f = moat_intelligence.predict_store_60m_dynamics(
            "オリエンタルラウンジ 渋谷本店", 24, 24, True, 0, 0, now_jst=dt, weather_map={},
            apply_online_bias=False, lag_context={}, peer_context=pmap_f["OLG 渋谷本店"], usual_pair_override=(),
        )
        fc_peer_m = moat_intelligence.predict_store_60m_dynamics(
            "オリエンタルラウンジ 渋谷本店", 24, 24, True, 0, 0, now_jst=dt, weather_map={},
            apply_online_bias=False, lag_context={}, peer_context=pmap_m["OLG 渋谷本店"], usual_pair_override=(),
        )
        self.assertGreater(fc_peer_f["pred_60m_wpct"], fc_peer_m["pred_60m_wpct"])

        # Boundary safety: tiny/empty/single-store city peer context has zero effect and never errors
        fc_base = moat_intelligence.predict_store_60m_dynamics(
            "OLG 長崎", 14, 12, True, 0, 0, now_jst=dt, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=()
        )
        for edge_peer in ({}, {"peer_women": 0, "peer_men": 0, "peer_stores": 0}, {"peer_women": 6, "peer_men": 5, "peer_stores": 1}):
            fc_edge = moat_intelligence.predict_store_60m_dynamics(
                "OLG 長崎", 14, 12, True, 0, 0, now_jst=dt, weather_map={},
                apply_online_bias=False, lag_context={}, peer_context=edge_peer, usual_pair_override=(),
            )
            self.assertEqual(fc_edge["pred_60m_wpct"], fc_base["pred_60m_wpct"])

    def test_v30_kalman_trajectory_ring_lags_and_continuous_usual_interpolation(self):
        """In-memory trajectory ring supplies t-15/30/60m Kalman state anchors, and 30-day usual curve interpolates smoothly across hour boundaries."""
        t0 = datetime.datetime(2026, 10, 30, 22, 0, 0, tzinfo=JST)
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            local_db.insert_logs([{"name": "OLG 渋谷本店", "women": 42, "men": 14}], "2026-10-30 21:00:00")
            local_db.insert_logs([{"name": "OLG 渋谷本店", "women": 40, "men": 16}], "2026-10-30 21:30:00")
            local_db.insert_logs([{"name": "OLG 渋谷本店", "women": 38, "men": 18}], "2026-10-30 21:45:00")

        lags = local_db.get_cached_recent_lags_map(t0)
        self.assertIn("OLG 渋谷本店", lags)
        self.assertEqual(lags["OLG 渋谷本店"]["lag_15m"], (38, 18))
        self.assertEqual(lags["OLG 渋谷本店"]["lag_30m"], (40, 16))
        self.assertEqual(lags["OLG 渋谷本店"]["lag_60m"], (42, 14))

        # Kalman state smoothing: transient 22:22 dip after sustained female-dominant history predicts higher wpct than after chronic male glut
        fc_after_fem = moat_intelligence.predict_store_60m_dynamics(
            "OLG 渋谷本店", 22, 22, True, 0, 0, now_jst=t0, weather_map={},
            apply_online_bias=False, lag_context=lags["OLG 渋谷本店"], usual_pair_override=(),
        )
        fc_after_male = moat_intelligence.predict_store_60m_dynamics(
            "OLG 渋谷本店", 22, 22, True, 0, 0, now_jst=t0, weather_map={},
            apply_online_bias=False,
            lag_context={"lag_15m": (22, 22), "lag_30m": (14, 38), "lag_60m": (12, 40)},
            usual_pair_override=(),
        )
        self.assertGreater(fc_after_fem["pred_60m_wpct"], fc_after_male["pred_60m_wpct"])

        # Edge-case lag tuples (zeros, <6 total, None) never raise or divide by zero
        fc_zero_lag = moat_intelligence.predict_store_60m_dynamics(
            "OLG 渋谷本店", 20, 15, True, 1, 1, now_jst=t0, weather_map={},
            apply_online_bias=False, lag_context={"lag_30m": (0, 0), "lag_60m": (2, 1)}, usual_pair_override=(),
        )
        self.assertIsNotNone(fc_zero_lag["pred_60m_wpct"])

        # Continuous minute interpolation across 21:59 -> 22:01 on 30-day usual pairs
        hist_rows = []
        base_fri = datetime.datetime(2026, 10, 9, 0, 0, 0)
        for wk in range(3):
            d_s = (base_fri + datetime.timedelta(days=7 * wk)).strftime("%Y-%m-%d")
            hist_rows.append({"ts": f"{d_s} 21:15:00", "name": "OLG 新宿", "women": 44, "men": 16, "source": "oriental"})
            hist_rows.append({"ts": f"{d_s} 22:15:00", "name": "OLG 新宿", "women": 24, "men": 32, "source": "oriental"})
            hist_rows.append({"ts": f"{d_s} 23:15:00", "name": "OLG 新宿", "women": 16, "men": 30, "source": "oriental"})
        local_db.insert_logs_with_ts(hist_rows)
        local_db.get_all_stores_live_benchmarks(now_jst=datetime.datetime(2026, 10, 30, 21, 59, 0), allow_blocking=True, force_refresh=True)

        u_2159 = local_db.get_cached_store_usual_pair("OLG 新宿", datetime.datetime(2026, 10, 30, 21, 59, 0, tzinfo=JST))
        u_2201 = local_db.get_cached_store_usual_pair("OLG 新宿", datetime.datetime(2026, 10, 30, 22, 1, 0, tzinfo=JST))
        self.assertIsNotNone(u_2159)
        self.assertIsNotNone(u_2201)
        # Across 2 minutes (21:59 -> 22:01), interpolated usual women at t and t+60m must vary smoothly (<1.0 headcount), not jump by 20!
        self.assertLess(abs(u_2159[0] - u_2201[0]), 1.0)
        self.assertLess(abs(u_2159[2] - u_2201[2]), 1.0)

    def test_v30_pdt_ghost_batch_surgical_removal_and_legacy_backfill_migration(self):
        """PDT (-16h) ghost batches (both 04:15 closed-hour ghosts and 5s-apart mid-evening spikes) are removed, and legacy v2.2 rows auto-migrate."""
        stores_20 = [
            "OLG 渋谷本店", "OLG 新宿", "OLG 恵比寿", "OLG 上野", "OLG 錦糸町",
            "OLG 柏", "OLG 町田", "OLG 横浜", "OLG 大宮", "OLG 宇都宮",
            "OLG 高崎", "OLG 梅田", "OLG 心斎橋", "OLG 難波", "OLG 京都",
            "OLG 神戸", "OLG 岡山", "OLG 福岡", "OLG 熊本", "JIS SHINJUKU",
        ]
        # Use a fixed past weekday (Wednesday night -> Thursday early morning) within the last 7 days
        today = datetime.datetime.now(JST).date()
        # Find most recent Wednesday strictly in the past (at least 1 day ago)
        wed_date = today - datetime.timedelta(days=1)
        while wed_date.weekday() != 2:
            wed_date -= datetime.timedelta(days=1)
        thu_date = wed_date + datetime.timedelta(days=1)
        w_str = wed_date.strftime("%Y-%m-%d")
        th_str = thu_date.strftime("%Y-%m-%d")

        logs = []
        # 1) Genuine evening trajectory at 20:45, 21:00, and 22:00 (~30 women, ~22 men)
        for ts_real, base_w, base_m in (
            (f"{w_str} 20:45:00", 28, 18),
            (f"{w_str} 21:00:00", 30, 22),
            (f"{w_str} 22:00:00", 26, 24),
        ):
            for idx, s in enumerate(stores_20):
                logs.append({"ts": ts_real, "name": s, "women": base_w + (idx % 3), "men": base_m + (idx % 2), "source": "oriental"})

        # 2) Rogue PDT ghost batch injected just 5 SECONDS after 21:00:00 (21:00:05 -> near-empty morning ghost: 1:0 in 18 stores, 8:0 in 2)
        #    Old 90s merge window would have overwritten 21:00:00 with these corrupted counts!
        for idx, s in enumerate(stores_20):
            logs.append({"ts": f"{w_str} 21:00:05", "name": s, "women": 8 if idx < 2 else 0, "men": 0, "source": "oriental"})

        # 3) Rogue early-morning PDT ghost batch at Thu 04:15:00 (20:15 PDT evening opening: closed weekday stores Ueno/Kashiwa/Machida active & women-only m=0)
        for idx, s in enumerate(stores_20):
            logs.append({"ts": f"{th_str} 04:15:00", "name": s, "women": 14, "men": 0, "source": "oriental"})
            logs.append({"ts": f"{th_str} 05:15:00", "name": s, "women": 18, "men": 4, "source": "oriental"})

        local_db.insert_logs_with_ts(logs)

        # Verify 04:15 ghost batch is excluded from 30-day live benchmarks too
        bm_04 = local_db.get_all_stores_live_benchmarks(
            now_jst=datetime.datetime.strptime(f"{th_str} 04:20:00", "%Y-%m-%d %H:%M:%S"),
            allow_blocking=True,
            force_refresh=True,
        )
        self.assertIsNone(bm_04.get("OLG 上野", {}).get("usual_women"))

        # Seed legacy v2.2 resolved rows + 1 live pending row in forecast_accuracy_logs
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            for i in range(60):
                p_s = f"{w_str} 19:{i:02d}:00"
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct,
                        resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt
                    ) VALUES (?, ?, ?, 'v2.2-statespace', 10, 10, 50, 10, 10, 50, ?, 1, 19, 5, 45, 45)
                    """,
                    (p_s, f"{w_str} 20:{i:02d}:00", "OLG 渋谷本店", f"{w_str} 20:{i:02d}:00"),
                )
            # Pending row (resolved_at IS NULL) must NOT be deleted during migration
            cur.execute(
                """
                INSERT INTO forecast_accuracy_logs (
                    predicted_at, target_at, store_name, model_version,
                    start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct
                ) VALUES (?, ?, 'OLG 渋谷本店', 'v2.2-statespace', 25, 20, 56, 24, 21, 53)
                """,
                (f"{th_str} 23:30:00", f"{th_str} 23:59:00"),
            )
            conn.commit()
        finally:
            conn.close()

        # Legacy v2.2 residuals must NOT contaminate v3.0 online bias cache
        local_db._invalidate_forecast_caches()
        self.assertEqual(local_db.get_cached_forecast_residual_adjustment("OLG 渋谷本店", 19), 0.0)

        # Backfill with force=False and min_existing=40 must auto-migrate because v3.0 resolved count is 0
        bf = local_db.backfill_forecast_accuracy_from_history(days=10, min_existing=40, max_rows=200, force=False)
        self.assertFalse(bf.get("skipped"))
        self.assertEqual(bf["inserted"], 20)

        # Verify: 1) Legacy resolved v2.2 rows removed, 2) Pending row preserved, 3) 21:00:00 start_women=30 (not 21:00:05 ghost!), 4) 04:15 ghost absent
        res_vers = local_db._fetch_all_rows(
            "SELECT DISTINCT model_version FROM forecast_accuracy_logs WHERE resolved_at IS NOT NULL"
        )
        self.assertEqual([r[0] for r in res_vers], ["v3.0-statespace"])
        pend_cnt = local_db._fetch_all_rows("SELECT COUNT(*) FROM forecast_accuracy_logs WHERE resolved_at IS NULL")[0][0]
        self.assertEqual(pend_cnt, 1)

        shib_21 = local_db._fetch_all_rows(
            "SELECT start_women, start_men, actual_women, actual_men FROM forecast_accuracy_logs WHERE store_name = 'OLG 渋谷本店' AND resolved_at IS NOT NULL"
        )
        self.assertEqual(len(shib_21), 1)
        self.assertEqual(tuple(shib_21[0]), (30, 22, 26, 24))

        # Intra-hour 6-row burst in a single hour (eff_n = 1 + 0.55*5 = 3.75 < 5.0) must NOT trigger premature online bias
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            for i in range(6):
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct,
                        resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt
                    ) VALUES (?, ?, 'OLG 長崎', ?, 20, 15, 57, 22, 14, 61, ?, 14, 20, 41, 20, 20)
                    """,
                    (f"{th_str} 21:{10 + i * 5:02d}:00", f"{th_str} 22:{10 + i * 5:02d}:00", moat_intelligence.FORECAST_MODEL_VERSION, f"{th_str} 22:{10 + i * 5:02d}:00"),
                )
            conn.commit()
        finally:
            conn.close()
        local_db._invalidate_forecast_caches()
        self.assertEqual(local_db.get_cached_forecast_residual_adjustment("OLG 長崎", 21), 0.0)

    def test_opening_and_closing_ramps_preserved_while_stuck_ghost_and_ring_spike_evicted(self):
        """Genuine 18:10 opening & 06:00 closing ramps survive Stage-1/Stage-2 & live ring, while 17h/18h stuck ghosts and interleaved spikes are removed."""
        stores_20 = [
            "OLG 渋谷本店", "OLG 新宿", "OLG 恵比寿", "OLG 上野", "OLG 錦糸町",
            "OLG 横浜", "OLG 大宮", "OLG 柏", "OLG 町田", "OLG 大阪駅前",
            "OLG 心斎橋", "OLG 難波", "OLG 京都", "OLG 神戸", "OLG 名古屋 栄",
            "OLG 福岡", "OLG 札幌", "JIS SHINJUKU", "JIS UMEDA", "ALFA HIROSHIMA",
        ]
        d_sun = "2026-10-18"  # Sunday
        raw_rows = []
        # 1) 17:45 PDT stuck-counter ghost: ALFA HIROSHIMA 44:39 while all other 19 stores are 0:0
        for s in stores_20:
            w, m = (44, 39) if s == "ALFA HIROSHIMA" else (0, 0)
            raw_rows.append((f"{d_sun} 17:45:00", s, w, m))
        # 2) Genuine 17:55 early open -> 18:10 opening surge (bc_prev > 0.45!) -> 18:25 continuation
        for idx_t, (ts_hm, mult) in enumerate([("17:55:00", 1), ("18:10:00", 3), ("18:25:00", 4)]):
            for idx_s, s in enumerate(stores_20):
                if idx_t == 0:
                    w, m = (8, 6) if idx_s < 8 else (2, 2)
                else:
                    w, m = (6 * mult + (idx_s % 3), 4 * mult + (idx_s % 2))
                raw_rows.append((f"{d_sun} {ts_hm}", s, w, m))
        # 3) Genuine Monday 06:00 closing tail: 2 mild stragglers (OLG 心斎橋 2:2, OLG 難波 2:1 at mtc=-60), others legitimate/empty
        d_mon = "2026-10-19"
        for s in stores_20:
            if s in ("OLG 心斎橋", "OLG 難波"):
                w, m = (2, 2)
            elif s in ("JIS SHINJUKU", "JIS UMEDA"):  # open until 06:00 -> mtc == 0
                w, m = (8, 6)
            else:
                w, m = (0, 0)
            raw_rows.append((f"{d_mon} 06:00:00", s, w, m))

        cleaned = local_db._extract_clean_snapshot_batches(raw_rows, check_taint=False, exclude_korea=True)
        ts_kept = [b[1] for b in cleaned]
        self.assertNotIn(f"{d_sun} 17:45:00", ts_kept)
        self.assertIn(f"{d_sun} 17:55:00", ts_kept)
        self.assertIn(f"{d_sun} 18:10:00", ts_kept)
        self.assertIn(f"{d_sun} 18:25:00", ts_kept)
        self.assertIn(f"{d_mon} 06:00:00", ts_kept)

        # Live trajectory ring: 18:10 opening jump is NOT dropped, whereas an interleaved 21:08 spike IS retroactively evicted at 21:15
        local_db.clear_store_7d_cache(clear_benchmarks=True)
        for ts_hm, mult in [("17:55:00", 1), ("18:10:00", 3)]:
            items = [
                {"name": s, "women": (8 if mult == 1 and i < 8 else (2 if mult == 1 else 6 * mult)), "men": (6 if mult == 1 and i < 8 else (2 if mult == 1 else 4 * mult))}
                for i, s in enumerate(stores_20)
            ]
            local_db._record_snapshot_to_trajectory_ring(items, f"{d_sun} {ts_hm}")
        lags_1825 = local_db.get_cached_recent_lags_map(datetime.datetime(2026, 10, 18, 18, 25, tzinfo=JST))
        self.assertEqual(lags_1825["OLG 渋谷本店"]["lag_15m"], (18, 12))

        local_db.clear_store_7d_cache(clear_benchmarks=True)
        real_2100 = [{"name": s, "women": 24 + (i % 3), "men": 18 + (i % 2)} for i, s in enumerate(stores_20)]
        spike_2108 = [{"name": s, "women": 3, "men": 14} for s in stores_20]
        real_2115 = [{"name": s, "women": 25 + (i % 3), "men": 19 + (i % 2)} for i, s in enumerate(stores_20)]
        local_db._record_snapshot_to_trajectory_ring(real_2100, f"{d_sun} 21:00:00")
        local_db._record_snapshot_to_trajectory_ring(spike_2108, f"{d_sun} 21:08:00")
        local_db._record_snapshot_to_trajectory_ring(real_2115, f"{d_sun} 21:16:00")
        lags_2125 = local_db.get_cached_recent_lags_map(datetime.datetime(2026, 10, 18, 21, 25, tzinfo=JST))
        # 21:08 spike (dt=17m ago) was evicted when 21:16 reconnected to 21:00, so lag_15m at 21:25 comes from 21:16 (dt=9m), NOT 21:08!
        self.assertEqual(lags_2125["OLG 渋谷本店"]["lag_15m"], (25, 19))

    def test_api_status_closed_wait_tone_and_store_detail_context_parity(self):
        """`/api/status` marks 0:0 off-hours stores closed, and `/store/<slug>` uses full `raw_stores` context for peer & arena signals."""
        # 1) Off-hours 0:0 store in /api/status must have is_open_now=False and wait_tone='closed'
        with app_module.data_lock:
            app_module.latest_data["full_data"] = [
                {"name": "OLG 渋谷本店", "women": 0, "men": 0, "source": "oriental", "region": "Kanto"},
                {"name": "OLG 新宿", "women": 32, "men": 18, "women_diff": 8, "men_diff": 1, "source": "oriental", "region": "Kanto"},
                {"name": "JIS SHINJUKU", "women": 28, "men": 16, "women_diff": 7, "men_diff": 0, "source": "jis", "region": "Kanto"},
            ]
            app_module.latest_data["last_updated"] = "2026-10-30 14:00:00"
        app_module.clear_fast_http_caches()

        fake_closed = {"is_open_now": False, "scheduled_open": False, "hours_label": "18:00〜翌5:00", "today_hours_label": "18:00〜翌5:00", "schedule_spec": {}}
        with mock.patch("store_hours.get_business_hours_status", side_effect=lambda name, total_guests=0, now_jst=None: (
            fake_closed if int(total_guests or 0) == 0 else {**fake_closed, "is_open_now": True}
        )), mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            r_st = self.client.get("/api/status")
            self.assertEqual(r_st.status_code, 200)
            ranking = r_st.get_json()["ranking"]
            shib = next(x for x in ranking if "渋谷本店" in x["name"])
            self.assertFalse(shib["is_open_now"])
            self.assertEqual(shib["wait_estimate"]["wait_tone"], "closed")

        # 2) Store detail context parity: enrich([store_obj], context_stores=full_list) == enrich(full_list)
        t_21 = datetime.datetime(2026, 10, 30, 21, 15, tzinfo=JST)
        full_tokyo = [
            {"name": "OLG 新宿", "women": 34, "men": 20, "women_diff": 9, "men_diff": 1, "usual_w_diff": 10, "is_open_now": True},
            {"name": "JIS SHINJUKU", "women": 30, "men": 18, "women_diff": 8, "men_diff": 1, "usual_w_diff": 9, "is_open_now": True},
            {"name": "OLG 渋谷本店", "women": 36, "men": 14, "women_diff": 2, "men_diff": 1, "is_open_now": True},
        ]
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            batch_res = moat_intelligence.enrich_stores_with_moat_signals([dict(x) for x in full_tokyo], now_jst=t_21)
            single_res = moat_intelligence.enrich_stores_with_moat_signals([dict(full_tokyo[0])], now_jst=t_21, context_stores=full_tokyo)
        self.assertEqual(single_res[0]["forecast_60m"]["pred_60m_wpct"], batch_res[0]["forecast_60m"]["pred_60m_wpct"])
        self.assertEqual(single_res[0]["forecast_60m"]["arena_signal_label"], batch_res[0]["forecast_60m"]["arena_signal_label"])
        self.assertTrue(len(single_res[0]["forecast_60m"]["arena_signal_label"]) > 0)

    def test_seo_store_names_arena_surge_and_brand_phase_fallbacks(self):
        """SEO store names trigger arena surge detection, and unmapped ag/YATAKOI/XIX branches resolve to their own brand priors."""
        t_21 = datetime.datetime(2026, 10, 30, 21, 0, tzinfo=JST)
        seo_stores = [
            {"name": "オリエンタルラウンジ 新宿", "women": 35, "men": 18, "women_diff": 9, "usual_w_diff": 10},
            {"name": "JIS（ジス）新宿", "women": 28, "men": 16, "women_diff": 8, "usual_w_diff": 9},
        ]
        arena_map = moat_intelligence.detect_arena_event_surges(seo_stores, now_jst=t_21)
        self.assertIn("オリエンタルラウンジ 新宿", arena_map)
        self.assertIn("OLG 新宿", arena_map)

        # Unmapped branch resolution & brand phase defaults
        self.assertEqual(moat_intelligence._resolve_internal_store_key("ag（アグ）横浜"), "OLG ag横浜")
        self.assertEqual(moat_intelligence._brand_phase_default("OLG ag横浜"), moat_intelligence._V3_BRAND_PHASE_DEFAULTS["AG"])
        self.assertEqual(moat_intelligence._resolve_internal_store_key("ヤタコイ 新宿"), "YATAKOI 新宿")
        self.assertEqual(moat_intelligence._brand_phase_default("YATAKOI 新宿"), moat_intelligence._V3_BRAND_PHASE_DEFAULTS["YATA"])
        self.assertEqual(moat_intelligence._brand_phase_default("XIX SHINJUKU"), moat_intelligence._V3_BRAND_PHASE_DEFAULTS["XIX"])

    def test_synthetic_seed_taint_guard_overdue_ghost_filter_and_cold_usual_pair(self):
        """Synthetic SEO seed batches are rejected from live recording, overdue resolution strips PDT ghosts, and cold usual_pair warms."""
        # 1) Synthetic SEO seed batch must NOT record into forecast_accuracy_logs
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            res_seed = local_db.record_and_resolve_forecasts(
                [
                    {"name": "オリエンタルラウンジ 渋谷本店", "women": 45, "men": 30},
                    {"name": "JIS SHINJUKU", "women": 25, "men": 20},
                    {"name": "ag渋谷", "women": 18, "men": 15},
                ],
                "2026-10-30 21:00:00",
            )
        self.assertEqual(res_seed, {"resolved": 0, "recorded": 0})
        cnt_fc = local_db._fetch_all_rows("SELECT COUNT(*) FROM forecast_accuracy_logs")[0][0]
        self.assertEqual(cnt_fc, 0)

        # 2) Overdue resolution strips multi-store PDT ghost at 21:00:00 and resolves against genuine 21:05:00 batch
        d_wed = "2026-10-28"
        stores_20 = [
            "OLG 渋谷本店", "OLG 新宿", "OLG 恵比寿", "OLG 上野", "OLG 錦糸町",
            "OLG 横浜", "OLG 大宮", "OLG 柏", "OLG 町田", "OLG 大阪駅前",
            "OLG 心斎橋", "OLG 難波", "OLG 京都", "OLG 神戸", "OLG 名古屋 栄",
            "OLG 福岡", "OLG 札幌", "JIS SHINJUKU", "JIS UMEDA", "ALFA HIROSHIMA",
        ]
        with mock.patch("moat_intelligence.get_city_weather_map", return_value={}):
            local_db.insert_logs([{"name": "OLG 渋谷本店", "women": 28, "men": 20}], f"{d_wed} 20:00:00")
            # Insert a prime-time Stage-1 PDT ghost at 21:00:00 (19 stores 0:0, only OLG 渋谷本店 2:1 -> tot_all < 100) + real 21:05:00 batch
            ov_logs = []
            for s in stores_20:
                ov_logs.append({"ts": f"{d_wed} 21:00:00", "name": s, "women": (4 if s == "OLG 渋谷本店" else 0), "men": (16 if s == "OLG 渋谷本店" else 0)})
                ov_logs.append({"ts": f"{d_wed} 21:05:00", "name": s, "women": (30 if s == "OLG 渋谷本店" else 14), "men": (20 if s == "OLG 渋谷本店" else 10)})
            local_db.insert_logs_with_ts(ov_logs)
            res_ov = local_db.resolve_due_forecasts_only([{"name": "OLG 渋谷本店", "women": 22, "men": 22}], f"{d_wed} 22:00:00")
            self.assertEqual(res_ov["resolved"], 1)

        row_ov = local_db._fetch_all_rows(
            "SELECT resolved_at, actual_women, actual_men FROM forecast_accuracy_logs WHERE predicted_at = ?",
            (f"{d_wed} 20:00:00",),
        )[0]
        self.assertEqual(tuple(row_ov), (f"{d_wed} 21:05:00", 30, 20))

        # 3) Cold get_cached_store_usual_pair auto-warms _benchmark_base_cache and resolves SEO store names
        base_night = datetime.datetime.now(JST).replace(tzinfo=None, hour=21, minute=15, second=0, microsecond=0)
        hist_u = []
        for d_back in range(1, 7):
            dt_base = base_night - datetime.timedelta(days=d_back)
            hist_u.append({"ts": dt_base.strftime("%Y-%m-%d %H:%M:%S"), "name": "OLG 渋谷本店", "women": 32, "men": 20})
            hist_u.append({"ts": (dt_base + datetime.timedelta(hours=1)).strftime("%Y-%m-%d %H:%M:%S"), "name": "OLG 渋谷本店", "women": 36, "men": 24})
        local_db.insert_logs_with_ts(hist_u)
        local_db.clear_store_7d_cache(clear_benchmarks=True)
        # Without calling get_all_stores_live_benchmarks first, cold lookup + SEO name must still succeed!
        up = local_db.get_cached_store_usual_pair("オリエンタルラウンジ 渋谷本店", base_night.replace(tzinfo=JST))
        self.assertIsNotNone(up)
        self.assertGreaterEqual(up[4], 5)

    def test_v31_unbiased_residual_feedback_opening_mask_and_brier_metrics(self):
        """Online calibration uses unadjusted raw_pred_wpct (zero double-counting), masks 0:0 opening jumps, and reports Brier/calibration."""
        import store_master

        # 0) SEO alias slug resolution never produces broken Japanese slugs like 'ag-アグ-上野'
        self.assertEqual(store_master.slug_of("ag（アグ）上野"), "ag-ueno")
        self.assertEqual(store_master.slug_of("JIS（ジス）新宿"), "jis-shinjuku")
        self.assertEqual(store_master.slug_of("オリエンタルラウンジ 渋谷本店"), "oriental-lounge-shibuya-honten")

        # 1) Unbiased feedback equivalence: rows recorded WITH online_bias_adj=-3.0 (pred=59, raw=62, actual=52, err=+7)
        #    must yield the EXACT SAME residual adjustment as unadjusted rows (pred=62, raw=62, actual=52, err=+10)!
        now_dt = datetime.datetime.now(JST).replace(tzinfo=None, minute=10, second=0, microsecond=0)
        d1 = (now_dt - datetime.timedelta(days=1)).replace(hour=21)
        d2 = (now_dt - datetime.timedelta(days=2)).replace(hour=22)

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            for idx, b_dt in enumerate((d1, d2)):
                for k in range(3):
                    p_ts = (b_dt + datetime.timedelta(minutes=15 * k)).strftime("%Y-%m-%d %H:%M:%S")
                    t_ts = (b_dt + datetime.timedelta(minutes=60 + 15 * k)).strftime("%Y-%m-%d %H:%M:%S")
                    # Store A: unadjusted rows (raw=62, pred=62, actual=52, err=10, bias=0.0)
                    cur.execute(
                        """
                        INSERT INTO forecast_accuracy_logs (
                            predicted_at, target_at, store_name, model_version,
                            start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct, flip_risk_pct,
                            resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt,
                            raw_pred_wpct, online_bias_adj
                        ) VALUES (?, ?, 'OLG 心斎橋', ?, 24, 16, 60, 26, 16, 62, 18, ?, 21, 19, 52, 10, 10, 62, 0.0)
                        """,
                        (p_ts, t_ts, moat_intelligence.FORECAST_MODEL_VERSION, t_ts),
                    )
                    # Store B: already bias-corrected rows (raw=62, pred=59, actual=52, err=7, bias=-3.0)
                    cur.execute(
                        """
                        INSERT INTO forecast_accuracy_logs (
                            predicted_at, target_at, store_name, model_version,
                            start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct, flip_risk_pct,
                            resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt,
                            raw_pred_wpct, online_bias_adj
                        ) VALUES (?, ?, 'OLG 難波', ?, 24, 16, 60, 25, 17, 59, 20, ?, 21, 19, 52, 7, 7, 62, -3.0)
                        """,
                        (p_ts, t_ts, moat_intelligence.FORECAST_MODEL_VERSION, t_ts),
                    )
            # Single-hour 12-row burst on OLG 岡山 must stay strictly < 2.0 effective samples -> 0.0 adjustment
            for k in range(12):
                p_ts = d1.replace(minute=k * 4).strftime("%Y-%m-%d %H:%M:%S")
                t_ts = (d1.replace(minute=k * 4) + datetime.timedelta(minutes=60)).strftime("%Y-%m-%d %H:%M:%S")
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct, flip_risk_pct,
                        resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt,
                        raw_pred_wpct, online_bias_adj
                    ) VALUES (?, ?, 'OLG 岡山', ?, 20, 15, 57, 24, 14, 63, 72, ?, 14, 21, 40, 23, 23, 63, 0.0)
                    """,
                    (p_ts, t_ts, moat_intelligence.FORECAST_MODEL_VERSION, t_ts),
                )
            # Censored closing row (<4 guests at t+60m) + male-glut flip rows for Brier/precision/recall verification
            c_ts = d1.replace(hour=0, minute=30).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute(
                """
                INSERT INTO forecast_accuracy_logs (
                    predicted_at, target_at, store_name, model_version,
                    start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct, flip_risk_pct,
                    resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt
                ) VALUES (?, ?, 'OLG 上野', ?, 12, 10, 55, 6, 5, 55, 35, ?, 1, 1, NULL, NULL, NULL)
                """,
                (c_ts, c_ts, moat_intelligence.FORECAST_MODEL_VERSION, c_ts),
            )
            m_ts = d1.replace(hour=23, minute=10).strftime("%Y-%m-%d %H:%M:%S")
            cur.execute(
                """
                INSERT INTO forecast_accuracy_logs (
                    predicted_at, target_at, store_name, model_version,
                    start_women, start_men, start_wpct, pred_women, pred_men, pred_wpct, flip_risk_pct,
                    resolved_at, actual_women, actual_men, actual_wpct, error_wpct_pt, abs_error_wpct_pt
                ) VALUES (?, ?, 'OLG 神戸', ?, 14, 18, 44, 13, 19, 41, 78, ?, 12, 20, 38, 3, 3)
                """,
                (m_ts, m_ts, moat_intelligence.FORECAST_MODEL_VERSION, m_ts),
            )
            conn.commit()
        finally:
            conn.close()

        local_db._invalidate_forecast_caches()
        adj_unadj = local_db.get_cached_forecast_residual_adjustment("OLG 心斎橋", 21)
        adj_already = local_db.get_cached_forecast_residual_adjustment("OLG 難波", 21)
        self.assertLess(adj_unadj, -1.5)
        self.assertEqual(adj_unadj, adj_already, "Feedback loop must learn from unadjusted raw_pred_wpct without decay!")
        self.assertEqual(local_db.get_cached_forecast_residual_adjustment("OLG 岡山", 21), 0.0)

        # 2) Opening 0:0 jump mask: at 18:10 (10m after 18:00 open), lag_15m=(0, 0) must NOT extrapolate +16/+10 momentum
        t_open = datetime.datetime(2026, 10, 28, 18, 10, tzinfo=JST)
        fc_open_clean = moat_intelligence.predict_store_60m_dynamics(
            "OLG 渋谷本店", 16, 10, True, 0, 0, now_jst=t_open, weather_map={},
            apply_online_bias=False, lag_context={}, usual_pair_override=(),
        )
        fc_open_zero_lag = moat_intelligence.predict_store_60m_dynamics(
            "OLG 渋谷本店", 16, 10, True, 0, 0, now_jst=t_open, weather_map={},
            apply_online_bias=False, lag_context={"lag_15m": (0, 0)}, usual_pair_override=(),
        )
        self.assertEqual(fc_open_zero_lag["pred_60m_women"], fc_open_clean["pred_60m_women"])
        self.assertEqual(fc_open_zero_lag["pred_60m_wpct"], fc_open_clean["pred_60m_wpct"])

        # 3) Smooth closing cap across mtc=46 -> 45 -> 44 AND mtc=16 -> 14 (zero cliff at mtc=45 or mtc=15) & honest pre-close guidance
        t_mtc46 = datetime.datetime(2026, 10, 29, 0, 14, tzinfo=JST)  # OLG 上野 closes 01:00 -> mtc=46
        t_mtc45 = datetime.datetime(2026, 10, 29, 0, 15, tzinfo=JST)  # mtc=45
        t_mtc44 = datetime.datetime(2026, 10, 29, 0, 16, tzinfo=JST)  # mtc=44
        fc_46 = moat_intelligence.predict_store_60m_dynamics("OLG 上野", 18, 14, True, 0, 0, now_jst=t_mtc46, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=())
        fc_45 = moat_intelligence.predict_store_60m_dynamics("OLG 上野", 18, 14, True, 0, 0, now_jst=t_mtc45, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=())
        fc_44 = moat_intelligence.predict_store_60m_dynamics("OLG 上野", 18, 14, True, 0, 0, now_jst=t_mtc44, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=())
        self.assertLessEqual(abs(fc_46["pred_60m_women"] - fc_45["pred_60m_women"]), 1)
        self.assertLessEqual(abs(fc_45["pred_60m_women"] - fc_44["pred_60m_women"]), 1)

        t_mtc16 = datetime.datetime(2026, 10, 29, 0, 44, tzinfo=JST)  # OLG 上野 closes 01:00 -> mtc=16
        t_mtc14 = datetime.datetime(2026, 10, 29, 0, 46, tzinfo=JST)  # mtc=14
        fc_16 = moat_intelligence.predict_store_60m_dynamics("OLG 上野", 18, 14, True, 0, 0, now_jst=t_mtc16, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=())
        fc_14 = moat_intelligence.predict_store_60m_dynamics("OLG 上野", 18, 14, True, 0, 0, now_jst=t_mtc14, weather_map={}, apply_online_bias=False, lag_context={}, usual_pair_override=())
        self.assertLessEqual(abs(fc_16["pred_60m_women"] - fc_14["pred_60m_women"]), 2)
        self.assertIn("閉店時刻が近い", fc_14["reason_short"])

        # 4) Brier score, precision/recall, calibration bins, and closing_censored_count in report
        rep = local_db.get_forecast_accuracy_report(days=7)
        s = rep["summary"]
        self.assertEqual(s["closing_censored_count"], 1)
        self.assertIsNotNone(s["flip_brier_score"])
        self.assertGreaterEqual(s["flip_brier_score"], 0.0)
        self.assertLess(s["flip_brier_score"], 0.25)
        self.assertEqual(s["flip_alert_precision_pct"], 100.0)
        self.assertEqual(s["flip_alert_recall_pct"], 100.0)
        self.assertGreaterEqual(len(rep["flip_calibration_bins"]), 2)


if __name__ == "__main__":
    unittest.main()



