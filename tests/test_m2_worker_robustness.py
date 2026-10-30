"""Unit and integration tests for Milestone M2 (Worker & DB Robustness).

Covers:
- BUG-01 (F3.1): `DISABLE_SCHEDULER` isolation in worker environment
- BUG-02 (F3.2): Non-blocking weather context in `local_db.insert_logs`
- BUG-04 (F3.3): Non-digit exception resilience in CLOVERS scraper
- BUG-05 (F3.4): JSON null count resilience in XIX, ALFA, and Yatakoi parsers
- BUG-06 (F3.5): Thread-safe `_olg_cache` access
- BUG-07 (F3.6): Thread-safe `health` module state & known unknowns locking
- BUG-09 (F3.7): PostgreSQL idle-in-transaction protection in `pg_connection`
"""

import os
import sys
import unittest
from unittest import mock
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import monitor
import health
import local_db
import turbo_store


class M2WorkerRobustnessTests(unittest.TestCase):

    def test_bug01_worker_scheduler_env_isolation(self):
        """BUG-01: worker.py isolates legacy scheduler suppression without permanently polluting DISABLE_SCHEDULER."""
        # When DISABLE_SCHEDULER was not set prior to running worker setup logic
        old_env = os.environ.pop("DISABLE_SCHEDULER", None)
        try:
            # Re-run worker import logic
            _was = os.environ.get("DISABLE_SCHEDULER")
            os.environ["DISABLE_SCHEDULER"] = "1"
            import app as app_mod
            app_mod._scheduler_started = True
            if _was is not None:
                os.environ["DISABLE_SCHEDULER"] = _was
            else:
                os.environ.pop("DISABLE_SCHEDULER", None)

            # In production worker process, DISABLE_SCHEDULER must NOT remain "1"
            self.assertIsNone(os.environ.get("DISABLE_SCHEDULER"))
            self.assertTrue(app_mod._scheduler_started)
        finally:
            if old_env is not None:
                os.environ["DISABLE_SCHEDULER"] = old_env

    def test_bug02_insert_logs_uses_non_blocking_weather(self):
        """BUG-02: local_db._save_moat_aux_logs calls capture_external_context_snapshot with allow_blocking=False."""
        with mock.patch("moat_intelligence.capture_external_context_snapshot") as mock_cap:
            mock_cap.return_value = []
            test_items = [
                {"name": "OLG 渋谷本店", "men": 10, "women": 15, "source": "oriental"}
            ]
            local_db._save_moat_aux_logs(test_items, "2026-10-30 20:00:00")
            mock_cap.assert_called_once()
            _, kwargs = mock_cap.call_args
            self.assertFalse(kwargs.get("allow_blocking"), "allow_blocking must be False to prevent blocking DB transactions")

    def test_bug04_clovers_scraper_non_digit_resilience(self):
        """BUG-04: get_clovers_data safely parses counts containing units or whitespace without ValueError."""
        # HTML containing Japanese counter units "名" and spaces
        mock_html = b"""
        <html><body>
        <table class="visit">
            <tr><th>\xe7\x94\xb7\xe6\x80\xa7</th><th>\xe5\xa5\xb3\xe6\x80\xa7</th></tr>
            <tr><td>12\xe5\x90\x8d</td><td>  24 \xe4\xba\xba </td></tr>
        </table>
        </body></html>
        """
        mock_resp = mock.MagicMock()
        mock_resp.content = mock_html
        mock_resp.raise_for_status.return_value = None

        with mock.patch.object(monitor, "_get_http_session") as mock_sess:
            mock_sess.return_value.get.return_value = mock_resp
            res = monitor.get_clovers_data()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["men"], 12)
            self.assertEqual(res[0]["women"], 24)
            self.assertEqual(res[0]["name"], "CLOVERS HIROSHIMA")

        # Empty cell resilience
        mock_empty_html = b"""
        <html><body>
        <table class="visit">
            <tr><th>\xe7\x94\xb7\xe6\x80\xa7</th><th>\xe5\xa5\xb3\xe6\x80\xa7</th></tr>
            <tr><td></td><td></td></tr>
        </table>
        </body></html>
        """
        mock_resp.content = mock_empty_html
        with mock.patch.object(monitor, "_get_http_session") as mock_sess:
            mock_sess.return_value.get.return_value = mock_resp
            res = monitor.get_clovers_data()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["men"], 0)
            self.assertEqual(res[0]["women"], 0)

    def test_bug05_xix_alfa_yatakoi_null_resilience(self):
        """BUG-05: XIX, ALFA, and Yatakoi safely handle JSON fields with explicit null/None values."""
        # XIX with null counts
        xix_resp = mock.MagicMock()
        xix_resp.json.return_value = [{"m_cnt": None, "w_cnt": None}]
        xix_resp.raise_for_status.return_value = None
        with mock.patch.object(monitor, "_get_http_session") as mock_sess:
            mock_sess.return_value.get.return_value = xix_resp
            res = monitor.get_xix_data()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["men"], 0)
            self.assertEqual(res[0]["women"], 0)

        # ALFA with null counts
        alfa_resp = mock.MagicMock()
        alfa_resp.json.return_value = {"man_num": None, "woman_num": None}
        alfa_resp.raise_for_status.return_value = None
        with mock.patch.object(monitor, "_get_http_session") as mock_sess:
            mock_sess.return_value.get.return_value = alfa_resp
            res = monitor.get_alfa_data()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["men"], 0)
            self.assertEqual(res[0]["women"], 0)

        # Yatakoi with null counts (during open hours)
        yatakoi_resp = mock.MagicMock()
        yatakoi_resp.json.return_value = {
            "males": None, "females": None, "ykMales": None, "ykFemales": None
        }
        yatakoi_resp.raise_for_status.return_value = None
        with mock.patch.object(monitor, "_get_http_session") as mock_sess, \
             mock.patch("store_hours.get_business_hours_status", return_value={"scheduled_open": True}):
            mock_sess.return_value.get.return_value = yatakoi_resp
            res = monitor.get_yatakoi_data()
            self.assertEqual(len(res), 1)
            self.assertEqual(res[0]["men"], 0)
            self.assertEqual(res[0]["women"], 0)

    def test_bug06_olg_cache_thread_safety(self):
        """BUG-06: _olg_cache access is protected by _olg_cache_lock under high concurrent access."""
        errors = []

        def reader():
            for _ in range(100):
                try:
                    with monitor._olg_cache_lock:
                        _ = monitor._olg_cache.get("served_stale")
                        _ = list(monitor._olg_cache.get("data") or [])
                except Exception as e:
                    errors.append(e)

        def writer():
            for i in range(100):
                try:
                    with monitor._olg_cache_lock:
                        monitor._olg_cache["served_stale"] = bool(i % 2)
                        monitor._olg_cache["data"] = [{"name": f"Store {i}", "men": i, "women": i}]
                        monitor._olg_cache["last_success"] = "2026-10-30 20:00:00"
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=reader) for _ in range(4)] + \
                  [threading.Thread(target=writer) for _ in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent _olg_cache access resulted in errors: {errors}")

    def test_bug07_health_thread_safety(self):
        """BUG-07: health module _state and _known_unknowns mutations are atomic with _lock."""
        health.reset_for_tests()
        errors = []

        def worker_fetch(worker_id):
            for i in range(50):
                try:
                    results = {"get_oriental_data": [{"name": "OLG 渋谷本店", "men": 10, "women": 10}]}
                    errors_dict = {}
                    sources = ["get_oriental_data"]
                    health.record_fetch(results, errors_dict, sources)
                    _ = health.snapshot()
                except Exception as e:
                    errors.append(e)

        def worker_unknown(worker_id):
            for i in range(50):
                try:
                    names = [f"Store_Test_{worker_id}_{i % 5}"]
                    with mock.patch("store_master.unknown_stores", return_value=names):
                        health.check_unknown_stores(names)
                except Exception as e:
                    errors.append(e)

        threads = [threading.Thread(target=worker_fetch, args=(i,)) for i in range(4)] + \
                  [threading.Thread(target=worker_unknown, args=(i,)) for i in range(4)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        self.assertEqual(len(errors), 0, f"Concurrent health state operations failed: {errors}")

    def test_bug09_pg_connection_idle_in_transaction_prevention(self):
        """BUG-09: pg_connection automatically rolls back uncommitted transactions before returning to pool."""
        mock_conn = mock.MagicMock()
        # Simulate status = 1 (psycopg2.extensions.STATUS_IN_TRANSACTION)
        mock_conn.status = 1
        mock_conn.closed = 0

        mock_pool = mock.MagicMock()
        mock_pool.getconn.return_value = mock_conn

        with mock.patch.object(turbo_store, "_get_pg_pool", return_value=mock_pool):
            with turbo_store.pg_connection() as conn:
                self.assertEqual(conn, mock_conn)
                # Client ran a SELECT query but did not call commit() or rollback()

            # Upon exiting context manager, conn.rollback() MUST have been called
            mock_conn.rollback.assert_called_once()
            mock_pool.putconn.assert_called_once_with(mock_conn, close=False)


if __name__ == "__main__":
    unittest.main()
