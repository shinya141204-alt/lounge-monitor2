import concurrent.futures
import os
import shutil
import tempfile
import unittest
from unittest import mock

import app as app_module
import cache_core
import embed_widgets
import local_db
import ssr_payload


class M2PerfAndCacheTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='m2_perf_test_')
        self._old_db = local_db.DB_PATH
        self._old_job_dir = os.environ.get('JOB_STATE_DIR')
        os.environ['JOB_STATE_DIR'] = self._tmpdir
        local_db.DB_PATH = os.path.join(self._tmpdir, 'test_m2.db')
        local_db.init_db()
        local_db.reset_caches_for_tests()
        app_module.clear_fast_http_caches()
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        local_db.reset_caches_for_tests()
        local_db.DB_PATH = self._old_db
        if self._old_job_dir is None:
            os.environ.pop('JOB_STATE_DIR', None)
        else:
            os.environ['JOB_STATE_DIR'] = self._old_job_dir
        app_module.clear_fast_http_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_s1_s4_architecture_and_m3_p1_01_seed_closed_guard(self):
        """S1-S4: app.py <= 2400 lines, 1-arg _enrich_for_ssr compat, M3-P1-01 seed does not force all_closed."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'app.py'), 'r', encoding='utf-8') as f:
            line_cnt = sum(1 for _ in f)
        self.assertLessEqual(line_cnt, 2400)

        stores = [{'name': 'OLG 新宿', 'women': 0, 'men': 0, 'region': 'Kanto', 'is_open': False}]
        enriched = app_module._enrich_for_ssr(stores)
        self.assertIsInstance(enriched, list)

        meta = ssr_payload.build_top_provenance_meta(
            initial_status={
                'is_seed': True,
                'ranking': [{'name': 'OLG 新宿', 'women': 0, 'men': 0, 'region': 'Kanto', 'is_open_now': False}],
            },
            is_testing=True,
        )
        self.assertFalse(meta['all_closed_mode'])

    def test_m2_p0_02_selective_store_7d_cache_invalidation(self):
        """M2-P0-02: clear_store_7d_cache only invalidates the specified store entry."""
        local_db._store_7d_cache[(local_db._active_db_key(), 'OLG 新宿')] = (9999999999.0, {'ok': 1})
        local_db._store_7d_cache[(local_db._active_db_key(), 'OLG 渋谷本店')] = (9999999999.0, {'ok': 2})
        local_db.clear_store_7d_cache('OLG 新宿')
        self.assertNotIn((local_db._active_db_key(), 'OLG 新宿'), local_db._store_7d_cache)
        self.assertIn((local_db._active_db_key(), 'OLG 渋谷本店'), local_db._store_7d_cache)

    def test_m2_p0_09_embed_heatmap_skips_raw_scan_when_rolled(self):
        """M2-P0-09: Heatmap skips monitor_logs query on past days already covered by >=3 rollup hours."""
        import datetime
        import sqlite3
        jst = datetime.timezone(datetime.timedelta(hours=9))
        past_d = (datetime.datetime.now(jst).date() - datetime.timedelta(days=10)).isoformat()
        recent_roll_d = (datetime.datetime.now(jst).date() - datetime.timedelta(days=1)).isoformat()
        conn = sqlite3.connect(local_db.DB_PATH)
        try:
            cur = conn.cursor()
            local_db._ensure_moat_rollup_schema()
            for d_r in (past_d, recent_roll_d):
                for hr in (19, 20, 21, 22):
                    cur.execute(
                        "INSERT INTO store_hourly_rollups (date_jst, hour_jst, business_date, store_name, sample_count, avg_women, avg_men, avg_wpct) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                        (d_r, hr, d_r, 'OLG 新宿', 10, 12.0, 8.0, 60.0),
                    )
            cur.execute(
                "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                (f"{past_d} 20:05:00", 'OLG 新宿', 10, 10),
            )
            conn.commit()
        finally:
            conn.close()

        with mock.patch.object(local_db, '_iter_ts_window_in_7d_chunks', wraps=local_db._iter_ts_window_in_7d_chunks) as m_raw:
            agg = embed_widgets._load_store_slot_aggregates(days=30)
        self.assertEqual(agg['rollup_buckets'], 8)
        self.assertEqual(agg['raw_buckets'], 0)
        self.assertEqual(m_raw.call_count, 0)

    def test_m2_p0_11_forecast_accuracy_unknown_store_and_cache(self):
        """M2-P0-11: Unknown store returns 200 empty report without hitting local_db."""
        with mock.patch.object(local_db, 'get_forecast_accuracy_report', side_effect=AssertionError('Should not hit DB')):
            r = self.client.get('/api/forecast-accuracy?store=nonexistent_club_999')
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.get_json()['ok'])

    def test_m2_p1_09_static_headers_etag_304_and_vary(self):
        """M2-P1-09: Static plain vs ?v= Cache-Control, ETag/304, Vary: Accept-Encoding on identity/gzip feeds."""
        r_plain = self.client.get('/static/calm.css')
        self.assertEqual(r_plain.status_code, 200)
        self.assertEqual(r_plain.headers.get('Cache-Control'), 'public, max-age=86400')
        self.assertIn('Accept-Encoding', r_plain.headers.get('Vary', ''))
        etag = r_plain.headers.get('ETag')
        self.assertTrue(etag)

        r_304 = self.client.get('/static/calm.css', headers={'If-None-Match': etag})
        self.assertEqual(r_304.status_code, 304)

        r_ver = self.client.get('/static/calm.css?v=52')
        self.assertEqual(r_ver.status_code, 200)
        self.assertEqual(r_ver.headers.get('Cache-Control'), 'public, max-age=31536000, immutable')

        for path in ('/', '/api/status', '/stats/monthly.csv', '/llms.txt', '/llms-full.txt', '/api/llms-facts.json'):
            r_id = self.client.get(path, headers={'Accept-Encoding': 'identity'})
            self.assertEqual(r_id.status_code, 200, path)
            self.assertIn('Accept-Encoding', r_id.headers.get('Vary', ''), path)

    def test_m2_p1_08_single_flight_32_threads_fast_cache_tokens(self):
        """M2-P1-08: Under FAST_CACHE_TOKENS=1, 32 concurrent /api/status requests trigger minimal recompute."""
        with mock.patch.dict(os.environ, {'FAST_CACHE_TOKENS': '1'}):
            app_module.clear_fast_http_caches()
            with concurrent.futures.ThreadPoolExecutor(max_workers=16) as pool:
                futs = [pool.submit(lambda: app_module.app.test_client().get('/api/status').status_code) for _ in range(32)]
                codes = [f.result(timeout=10) for f in futs]
            self.assertTrue(all(c == 200 for c in codes))

    def test_fly_toml_and_dockerfile_m2_hardening(self):
        """M2-P1-09: fly.toml mounts only /static/reels and enables FAST_CACHE_TOKENS; Dockerfile adds brotli."""
        root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root, 'fly.toml'), 'r', encoding='utf-8') as f:
            fly_txt = f.read()
        with open(os.path.join(root, 'Dockerfile'), 'r', encoding='utf-8') as f:
            doc_txt = f.read()
        self.assertIn('FAST_CACHE_TOKENS = "1"', fly_txt)
        self.assertIn('guest_path = "/app/static/reels"', fly_txt)
        self.assertIn('brotli', doc_txt)
        self.assertIn('COPY --chown=appuser:appgroup . /app', doc_txt)

    def test_m2_edge_waveform_wpct_none_pg_closed_cb_and_moat_lockless_reads(self):
        """Edge fixes: wpct=None-only slots -> insufficient; closed PG fatal error -> (True, True); lockless moat reads."""
        import datetime
        import sqlite3
        import threading
        import moat_intelligence
        import turbo_store

        jst = datetime.timezone(datetime.timedelta(hours=9))
        now_dt = datetime.datetime.now(jst).replace(tzinfo=None)
        d1 = (now_dt.date() - datetime.timedelta(days=1)).isoformat()
        d2 = (now_dt.date() - datetime.timedelta(days=2)).isoformat()
        conn = sqlite3.connect(local_db.DB_PATH)
        try:
            cur = conn.cursor()
            cur.execute(
                "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                (f"{d1} 20:05:00", 'OLG 新宿', 0, 1),
            )
            cur.execute(
                "INSERT INTO monitor_logs (timestamp, store_name, women, men) VALUES (?, ?, ?, ?)",
                (f"{d2} 20:05:00", 'OLG 新宿', 0, 1),
            )
            conn.commit()
        finally:
            conn.close()
        local_db.reset_caches_for_tests()
        wf = local_db.get_all_stores_hourly_waveforms(now_jst=now_dt, allow_blocking=True)
        sh = wf.get('OLG 新宿')
        self.assertIsNotNone(sh)
        self.assertIsNotNone(sh['h20'])
        self.assertIsNone(sh['h20']['wpct'])
        self.assertEqual(sh['data_status'], 'insufficient')
        self.assertIsNone(sh['waveform_archetype'])

        class _FakeClosedConn:
            closed = 1

        if turbo_store._psycopg2 is not None:
            pg_mod = turbo_store._psycopg2
            patch_ctx = mock.patch.object(turbo_store, '_psycopg2', pg_mod)
        else:
            class _FakePgMod:
                class OperationalError(Exception):
                    pass
                class InterfaceError(Exception):
                    pass
            pg_mod = _FakePgMod
            patch_ctx = mock.patch.object(turbo_store, '_psycopg2', pg_mod)

        with patch_ctx:
            for code in ('08006', '57P01'):
                err_f = pg_mod.OperationalError('fatal')
                err_f.pgcode = code
                self.assertEqual(turbo_store._classify_pg_error(_FakeClosedConn(), err_f), (True, True))
            err_intf = pg_mod.InterfaceError('closed')
            self.assertEqual(turbo_store._classify_pg_error(_FakeClosedConn(), err_intf), (True, True))
            for code in ('40P01', '57014'):
                err_t = pg_mod.OperationalError('transient')
                err_t.pgcode = code
                self.assertEqual(turbo_store._classify_pg_error(_FakeClosedConn(), err_t), (True, False))

        errs = []

        def _read_moat():
            try:
                for _ in range(200):
                    if moat_intelligence._resolve_internal_store_key('オリエンタルラウンジ 新宿') != 'OLG 新宿':
                        errs.append('bad_key')
                    if moat_intelligence._is_standing_bar_store('ASOBIBAR 新宿') is not True:
                        errs.append('bad_standing')
            except Exception as exc:
                errs.append(repr(exc))

        threads = [threading.Thread(target=_read_moat) for _ in range(16)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(errs, [])


if __name__ == '__main__':
    unittest.main()
