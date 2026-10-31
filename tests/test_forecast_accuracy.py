import datetime
import os
import re
import tempfile
import time
import unittest
from unittest import mock

# Ensure test DB isolation before importing app/local_db
_TMP_DIR = tempfile.mkdtemp(prefix="aiseki_fc_test_")
os.environ.setdefault("LOCAL_DB_PATH", os.path.join(_TMP_DIR, "boot_test.db"))
os.environ.setdefault("DISABLE_SCHEDULER", "1")

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
        # Extract all /store/<slug> links rendered on /stats and verify none 404
        hrefs = sorted(set(re.findall(r'href="(/store/[^"#?]+)"', html_stats)))
        self.assertGreaterEqual(len(hrefs), 1)
        for href in hrefs:
            r_link = self.client.get(href)
            self.assertIn(r_link.status_code, (200, 301, 302), f"Broken store link {href} returned {r_link.status_code}")

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

        # Query by brand-safe display name AND by URL slug -> both resolve to canonical 'OLG 渋谷本店'
        for q in ("オリエンタルラウンジ 渋谷本店", "oriental-lounge-shibuya-honten", "OLG 渋谷本店"):
            resp = self.client.get(f"/api/forecast-accuracy?days=7&store={q}")
            self.assertEqual(resp.status_code, 200)
            js = resp.get_json()
            self.assertEqual(js["store_filter"], "OLG 渋谷本店")
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


if __name__ == "__main__":
    unittest.main()

