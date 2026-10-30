"""Unit and integration tests for Milestone M4 (Performance & Optimization).

Covers:
- F5.1 (BUG-03): `local_db.get_snapshot_around` window function optimization
- F5.2 (ISSUE-01): `app.py` after_request consolidation & Fly.io edge cache headers
- F5.3 (ISSUE-07): `/concierge` and `/api/concierge` short-term memoization caches
- F5.4: `fly.toml` static asset direct-delivery configuration
- F5.5 (BUG-08): `local_db._get_read_connection` inode check throttling
"""

import os
import sys
import unittest
from unittest import mock
import sqlite3
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import app as app_module
import local_db


class M4PerformanceOptimizationTests(unittest.TestCase):

    def setUp(self):
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()

    def test_f51_window_function_get_snapshot_around(self):
        """F5.1 (BUG-03): get_snapshot_around fetches only the latest row per store via SQL window function."""
        # Ensure SQLite has monitor_logs table
        conn = local_db.get_connection()
        try:
            conn.execute("""
                CREATE TABLE IF NOT EXISTS monitor_logs (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    timestamp TEXT NOT NULL,
                    store_name TEXT NOT NULL,
                    men INTEGER DEFAULT 0,
                    women INTEGER DEFAULT 0
                )
            """)
            # Insert older and newer entries for test stores
            t_old = "2026-10-30 00:00:00"
            t_mid = "2026-10-30 00:10:00"
            t_new = "2026-10-30 00:20:00"

            conn.execute("DELETE FROM monitor_logs WHERE store_name IN ('__TestStoreA__', '__TestStoreB__')")
            conn.execute("INSERT INTO monitor_logs (timestamp, store_name, men, women) VALUES (?, ?, ?, ?)",
                         (t_old, '__TestStoreA__', 10, 15))
            conn.execute("INSERT INTO monitor_logs (timestamp, store_name, men, women) VALUES (?, ?, ?, ?)",
                         (t_new, '__TestStoreA__', 25, 30))
            conn.execute("INSERT INTO monitor_logs (timestamp, store_name, men, women) VALUES (?, ?, ?, ?)",
                         (t_mid, '__TestStoreB__', 5, 8))
            conn.commit()

            # Query with a window spanning from 100 days ago to now
            res = local_db.get_snapshot_around(0, 500000)
            self.assertIn('__TestStoreA__', res)
            self.assertIn('__TestStoreB__', res)
            # Latest entry for StoreA should be t_new (men=25, women=30)
            self.assertEqual(res['__TestStoreA__']['men'], 25)
            self.assertEqual(res['__TestStoreA__']['women'], 30)
            self.assertEqual(res['__TestStoreA__']['ts'], t_new)
            # Latest entry for StoreB should be t_mid
            self.assertEqual(res['__TestStoreB__']['men'], 5)
            self.assertEqual(res['__TestStoreB__']['women'], 8)
        finally:
            try:
                conn.execute("DELETE FROM monitor_logs WHERE store_name IN ('__TestStoreA__', '__TestStoreB__')")
                conn.commit()
            except Exception:
                pass
            conn.close()

    def test_f55_read_connection_inode_throttling(self):
        """F5.5 (BUG-08): _get_read_connection reuses connection and throttles os.stat within 2.0s."""
        conn1 = local_db._get_read_connection()
        t1 = getattr(local_db._read_tls, 'last_check', 0.0)
        self.assertGreater(t1, 0.0)

        # Immediate subsequent calls should reuse connection without modifying last_check
        conn2 = local_db._get_read_connection()
        t2 = getattr(local_db._read_tls, 'last_check', 0.0)
        self.assertIs(conn1, conn2)
        self.assertEqual(t1, t2)

    def test_f52_edge_cache_headers_when_enabled(self):
        """F5.2 (ISSUE-01): When ENABLE_PUBLIC_EDGE_CACHE=1, public HTML returns s-maxage=15 header."""
        with mock.patch.dict(os.environ, {'ENABLE_PUBLIC_EDGE_CACHE': '1'}):
            resp = self.client.get('/price-comparison')
            self.assertEqual(resp.status_code, 200)
            cache_ctrl = resp.headers.get('Cache-Control', '')
            self.assertIn('s-maxage=15', cache_ctrl)
            self.assertIn('stale-while-revalidate=45', cache_ctrl)
            self.assertIn('public', cache_ctrl)

    def test_f52_edge_cache_headers_when_disabled(self):
        """F5.2 (ISSUE-01): When ENABLE_PUBLIC_EDGE_CACHE is unset, public HTML returns no-cache."""
        with mock.patch.dict(os.environ, {'ENABLE_PUBLIC_EDGE_CACHE': '0'}):
            resp = self.client.get('/price-comparison')
            self.assertEqual(resp.status_code, 200)
            cache_ctrl = resp.headers.get('Cache-Control', '')
            self.assertIn('private, no-cache, must-revalidate, max-age=0', cache_ctrl)

    def test_f52_staff_cache_control_headers(self):
        """F5.2 (ISSUE-01): Staff access returns private, no-cache, no-store, must-revalidate."""
        # 1. Staff session
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
        resp = self.client.get('/price-comparison')
        self.assertEqual(resp.status_code, 200)
        cache_ctrl = resp.headers.get('Cache-Control', '')
        self.assertIn('no-store', cache_ctrl)
        self.assertIn('no-cache', cache_ctrl)
        self.assertIn('private', cache_ctrl)
        self.assertEqual(resp.headers.get('X-Staff-View'), '1')

        # 2. Staff URL path
        resp_staff = self.client.get('/staff')
        s_cache = resp_staff.headers.get('Cache-Control', '')
        self.assertIn('no-store', s_cache)
        self.assertIn('no-cache', s_cache)

    def test_f53_concierge_cached_endpoints(self):
        """F5.3 (ISSUE-07): /concierge and /api/concierge return fast cached responses."""
        # 1. JSON endpoint
        r_api = self.client.get('/api/concierge?area=shinjuku&audience=men')
        self.assertEqual(r_api.status_code, 200)
        data = r_api.get_json()
        self.assertTrue(data.get('ok'))
        self.assertIn('primary', data)
        self.assertIn('backup', data)

        # Subsequent call should hit cache
        r_api_cached = self.client.get('/api/concierge?area=shinjuku&audience=men')
        self.assertEqual(r_api_cached.status_code, 200)
        self.assertEqual(r_api_cached.get_json(), data)

        # 2. HTML page endpoint
        r_html = self.client.get('/concierge?area=shinjuku&audience=men')
        self.assertEqual(r_html.status_code, 200)
        html_txt = r_html.data.decode('utf-8')
        self.assertIn('3秒穴場＆ハシゴ診断', html_txt)

        # Subsequent HTML call should hit cache
        r_html_cached = self.client.get('/concierge?area=shinjuku&audience=men')
        self.assertEqual(r_html_cached.status_code, 200)
        self.assertEqual(r_html_cached.data, r_html.data)

    def test_f54_fly_toml_statics_configuration(self):
        """F5.4: fly.toml configures [[statics]] section for edge direct delivery."""
        fly_path = os.path.join(ROOT, 'fly.toml')
        self.assertTrue(os.path.exists(fly_path))
        with open(fly_path, 'r', encoding='utf-8') as f:
            content = f.read()
        self.assertIn('[[statics]]', content)
        self.assertIn('guest_path = "/app/static"', content)
        self.assertIn('url_prefix = "/static"', content)


if __name__ == '__main__':
    unittest.main()
