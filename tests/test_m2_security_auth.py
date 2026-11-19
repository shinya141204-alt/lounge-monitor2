import json
import os
import shutil
import tempfile
import threading
import unittest
from io import BytesIO
from unittest import mock

import app as app_module
import affiliate_manager
import job_state
import ops_governance
import request_guards
import turbo_store


class M2SecurityAndAuthTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix='m2_sec_test_')
        self._old_job_dir = os.environ.get('JOB_STATE_DIR')
        os.environ['JOB_STATE_DIR'] = self._tmpdir
        job_state.reset_cache_for_tests()
        turbo_store.reset_for_tests()
        affiliate_manager.reset_cache_for_tests()
        ops_governance.invalidate_governance_cache()
        with app_module._login_failures_lock:
            app_module._login_failures.clear()
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def tearDown(self):
        with app_module._login_failures_lock:
            app_module._login_failures.clear()
        if self._old_job_dir is None:
            os.environ.pop('JOB_STATE_DIR', None)
        else:
            os.environ['JOB_STATE_DIR'] = self._old_job_dir
        job_state.reset_cache_for_tests()
        turbo_store.reset_for_tests()
        affiliate_manager.reset_cache_for_tests()
        ops_governance.invalidate_governance_cache()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_m2_p0_01_distributed_lock_fail_closed_with_mem_bus(self):
        """M2-P0-01: When Redis is down and fail_open=False, return False (not fallback to mem_bus)."""
        with mock.patch.object(turbo_store, 'get_redis_client', return_value=None), \
             mock.patch.object(turbo_store, '_mem_bus_enabled', False):
            self.assertFalse(turbo_store.acquire_distributed_lock('cron:test_lock', ttl_sec=60, fail_open=False))
            self.assertTrue(turbo_store.acquire_distributed_lock('cron:test_lock', ttl_sec=60, fail_open=True))

    def test_m2_p0_03_moshimo_reel_queue_action_and_json_validation(self):
        """M2-P0-03: Unknown action or non-dict JSON returns HTTP 400."""
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
            sess['auth_gen'] = app_module._compute_session_auth_gen(True)
        r_unk = self.client.post(
            '/api/moshimo-reel-queue',
            json={'action': 'drop_database'},
            headers={'Origin': 'http://localhost'},
        )
        self.assertEqual(r_unk.status_code, 400)
        r_arr = self.client.post(
            '/api/moshimo-reel-queue',
            data=json.dumps(['not', 'a', 'dict']),
            content_type='application/json',
            headers={'Origin': 'http://localhost'},
        )
        self.assertEqual(r_arr.status_code, 400)

    def test_m2_p0_04_login_failure_isolation_secret_auth_and_logout_revocation(self):
        """M2-P0-04: Staff success does not clear master failures; X-Admin-Secret works; logout clears auth_gen + rotates master epoch."""
        for _ in range(7):
            r_fail = self.client.post('/staff', data={'passcode': 'wrong_pass'})
            self.assertEqual(r_fail.status_code, 200)
        # Regular staff success clears staff counter ONLY
        r_staff_ok = self.client.post('/staff', data={'passcode': app_module.STAFF_PASSCODE})
        self.assertEqual(r_staff_ok.status_code, 302)
        # 2 more failures push master counter past 8 -> 429
        self.client.post('/staff', data={'passcode': 'wrong_pass'})
        r_429 = self.client.post('/staff', data={'passcode': app_module.MASTER_PASSCODE})
        self.assertEqual(r_429.status_code, 429)

        with app_module._login_failures_lock:
            app_module._login_failures.clear()

        # X-Admin-Secret grants access to master pages
        r_sec = self.client.get('/staff/analytics', headers={'X-Admin-Secret': app_module.ADMIN_API_SECRET})
        self.assertEqual(r_sec.status_code, 200)
        # Regular staff session cannot access master pages
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = False
            sess['auth_gen'] = app_module._compute_session_auth_gen(False)
        r_deny = self.client.get('/staff/analytics')
        self.assertEqual(r_deny.status_code, 302)

        # Master login -> logout clears session & rotates master_session_epoch
        r_mlog = self.client.post('/staff', data={'passcode': app_module.MASTER_PASSCODE})
        self.assertEqual(r_mlog.status_code, 302)
        old_epoch = ops_governance.get_master_session_epoch()
        r_out = self.client.get('/staff/logout')
        self.assertEqual(r_out.status_code, 302)
        new_epoch = ops_governance.get_master_session_epoch()
        self.assertNotEqual(old_epoch, new_epoch)
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get('is_master'))
            self.assertFalse(sess.get('is_staff'))
            self.assertIsNone(sess.get('auth_gen'))

    def test_m2_p0_05_obf2_crypto_and_strict_deobfuscate(self):
        """M2-P0-05: job_state uses obf2: AEAD, preserves obf1: decode, and strict decode returns None on corruption."""
        obf = job_state._obfuscate_secret('super-secret-value-123', key_name='token:test_key')
        self.assertTrue(obf.startswith('obf2:'))
        self.assertEqual(job_state._deobfuscate_secret(obf, key_name='token:test_key'), 'super-secret-value-123')
        self.assertIsNone(job_state._deobfuscate_secret_strict('obf2:corrupted_invalid', key_name='token:test_key'))
        self.assertEqual(job_state._deobfuscate_secret('obf2:corrupted_invalid', key_name='token:test_key'), '')

    def test_m2_p0_06_07_08_turbo_tls_classifier_and_prod_guard(self):
        """M2-P0-06/07/08: TLS opts, transient PG errors don't trip breaker, FLY_APP_NAME fail-closed without backends."""
        if turbo_store._psycopg2 is not None:
            class _FakeConn:
                closed = 0
            err_to = turbo_store._psycopg2.OperationalError('statement timeout')
            with mock.patch.object(err_to, 'pgcode', '57014', create=True):
                broken, trip = turbo_store._classify_pg_error(_FakeConn(), err_to)
                self.assertFalse(broken)
                self.assertFalse(trip)
            err_conn = turbo_store._psycopg2.OperationalError('connection closed')
            with mock.patch.object(err_conn, 'pgcode', '08006', create=True):
                broken2, trip2 = turbo_store._classify_pg_error(_FakeConn(), err_conn)
                self.assertTrue(broken2)
                self.assertTrue(trip2)
        with mock.patch.dict(os.environ, {'FLY_APP_NAME': 'test-app'}, clear=False):
            os.environ.pop('DISABLE_TURBO_STORE', None)
            self.assertFalse(turbo_store._live_backends_blocked())
        with mock.patch.dict(os.environ, {'FLY_APP_NAME': 'test-app', 'DISABLE_TURBO_STORE': '1'}, clear=False):
            self.assertTrue(turbo_store._live_backends_blocked())

    def test_m2_p1_02_strict_origin_and_get_cache_only(self):
        """M2-P1-02: Master POST without Origin or Origin: null returns 403; GET /api/sns-editorial defaults ai_cache_only=True."""
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
        r_no_orig = self.client.post('/api/ops/governance', json={'action': 'force_logout_staff'})
        self.assertEqual(r_no_orig.status_code, 403)
        r_null = self.client.post(
            '/api/ops/governance',
            json={'action': 'force_logout_staff'},
            headers={'Origin': 'null'},
        )
        self.assertEqual(r_null.status_code, 403)

        import sns_editorial
        with mock.patch.object(sns_editorial, 'run_editorial_pipeline', return_value={'ok': True}) as m_pipe:
            r_get = self.client.get('/api/sns-editorial?channel=x&slot=22:00')
            self.assertEqual(r_get.status_code, 200)
            self.assertTrue(m_pipe.call_args.kwargs.get('ai_cache_only'))

    def test_m2_p1_05_bot_shield_signed_an_hp_and_rate_limit_shield(self):
        """M2-P1-05: Unauth GPTBot + ?staff_test=1 shielded; 11th+ /link/* shields ASP URL to /grooming."""
        r_bot = self.client.get('/link/orbis?staff_test=1', headers={'User-Agent': 'GPTBot/1.0'})
        self.assertEqual(r_bot.status_code, 302)
        self.assertEqual(r_bot.headers.get('Location'), '/grooming')

        # 10 allowed clicks, 11th+ redirects to /grooming instead of ASP
        ua = 'Mozilla/5.0 M2RateLimitTest'
        for _ in range(10):
            r_ok = self.client.get('/link/orbis', headers={'User-Agent': ua})
            self.assertEqual(r_ok.status_code, 302)
            self.assertIn('px.a8.net', r_ok.headers.get('Location', ''))
        r_shield = self.client.get('/link/orbis', headers={'User-Agent': ua})
        self.assertEqual(r_shield.status_code, 302)
        self.assertEqual(r_shield.headers.get('Location'), '/grooming')

        # Signed an_hp_sig verification
        tok = app_module.issue_human_proof_cookie(str(app_module.app.secret_key))
        self.assertTrue(app_module.verify_human_proof_cookie(str(app_module.app.secret_key), tok))
        self.assertFalse(app_module.verify_human_proof_cookie(str(app_module.app.secret_key), '1700000000.000000000000000000000000'))

    def test_m2_p1_06_body_size_limit_and_empty_file_guard(self):
        """M2-P1-06: Oversized public POST returns 413; empty FileStorage filename returns 400."""
        oversized = b'{"event":"human_pv","label":"' + (b'a' * (300 * 1024)) + b'"}'
        r_413 = self.client.post('/api/track', data=oversized, content_type='application/json')
        self.assertEqual(r_413.status_code, 413)

        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
        r_empty = self.client.post(
            '/api/pdca/sns-insights/ingest-csv',
            data={'csv_file': (BytesIO(b''), '')},
            content_type='multipart/form-data',
            headers={'Origin': 'http://localhost'},
        )
        self.assertEqual(r_empty.status_code, 400)

    def test_m2_p1_07_2_04_generic_errors_and_affiliate_rlock_ascii(self):
        """M2-P1-07/M2-P2-04: Public API hides internal exceptions; affiliate validates ASCII and uses RLock safely."""
        with mock.patch('weekly_report.generate_weekly_report', side_effect=RuntimeError('SECRET_DSN_LEAK')):
            r_wr = self.client.get('/api/weekly-report')
            self.assertEqual(r_wr.status_code, 500)
            self.assertNotIn('SECRET_DSN_LEAK', r_wr.get_data(as_text=True))

        ok, msg = affiliate_manager.validate_destination_url('https://example.com/日本語')
        self.assertFalse(ok)
        self.assertIn('非ASCII', msg)

        errors = []

        def _worker(idx):
            try:
                res = affiliate_manager.set_affiliate_slot_override('dating_instant', f'https://example.com/u{idx}', memo=f'm{idx}')
                if not res.get('ok'):
                    errors.append(res)
            except Exception as e:
                errors.append(e)

        threads = [threading.Thread(target=_worker, args=(i,)) for i in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=5.0)
        self.assertEqual(errors, [])

    def test_m2_p1_12_csp_report_only_and_sink(self):
        """M2-P1-12: CSP enforces form-action without script-src-attr; Report-Only includes script-src-attr & report-uri."""
        r_home = self.client.get('/')
        csp = r_home.headers.get('Content-Security-Policy', '')
        csp_ro = r_home.headers.get('Content-Security-Policy-Report-Only', '')
        self.assertIn("form-action 'self'", csp)
        self.assertNotIn("script-src-attr 'none'", csp)
        self.assertIn("script-src-attr 'none'", csp_ro)
        self.assertIn('report-uri /api/csp-report', csp_ro)

        r_rep = self.client.post(
            '/api/csp-report',
            json={'csp-report': {'violated-directive': 'script-src-attr', 'blocked-uri': 'inline'}},
        )
        self.assertEqual(r_rep.status_code, 204)

    def test_m2_p1_04_public_reel_mp4_range_bypass_blocked_429_and_cached_hit(self):
        """M2-P1-04: Uncached Range/If-Range requests cannot bypass media_render_ip (429); cached hits serve 206 freely."""
        import reel_generator

        sample_mp4 = b'\x00\x00\x00\x18ftypmp42' + (b'M2_REEL_' * 200)
        old_testing = app_module.app.config.get('TESTING', True)
        app_module.app.config['TESTING'] = False
        try:
            with tempfile.TemporaryDirectory() as tmp_reel_dir, \
                 mock.patch.object(reel_generator, 'REEL_TMP_CACHE_DIR', tmp_reel_dir), \
                 mock.patch.object(turbo_store, 'get_media_blob', return_value=None), \
                 mock.patch('reel_generator.render_reel_mp4_bytes', return_value=sample_mp4) as m_render:
                with reel_generator._MEM_CACHE_LOCK:
                    reel_generator._MEM_MP4_CACHE.clear()
                with reel_generator._PINNED_SLOT_LOCK:
                    reel_generator._PINNED_SLOT_CACHE.clear()

                # 12 uncached Range+If-Range requests allowed; 13th must return 429 (not 200/503)
                for i in range(12):
                    r = self.client.get(
                        '/ogp/reel/live_top5.mp4',
                        headers={'Range': 'bytes=1-64', 'If-Range': 'x'},
                    )
                    self.assertIn(r.status_code, (200, 206), f'req #{i+1} got {r.status_code}')
                r_13 = self.client.get(
                    '/ogp/reel/live_top5.mp4',
                    headers={'Range': 'bytes=1-64', 'If-Range': 'x'},
                )
                self.assertEqual(r_13.status_code, 429)
                self.assertEqual(r_13.headers.get('Retry-After'), '60')
                self.assertIn('no-store', r_13.headers.get('Cache-Control', ''))
                self.assertEqual(m_render.call_count, 12)

            # Cached MP4 via get_cached_reel_mp4_bytes serves 206 without hitting render or rate limit
            with mock.patch('reel_generator.get_cached_reel_mp4_bytes', return_value=sample_mp4), \
                 mock.patch('reel_generator.render_reel_mp4_bytes', side_effect=AssertionError('Should not render on cache hit')):
                r_cached = self.client.get('/ogp/reel/live_top5.mp4', headers={'Range': 'bytes=0-31'})
                self.assertEqual(r_cached.status_code, 206)
                self.assertEqual(r_cached.data, sample_mp4[:32])
        finally:
            app_module.app.config['TESTING'] = old_testing

    def test_m2_p1_03_non_ascii_hmac_no_500_all_paths(self):
        """M2-P1-03: Non-ASCII inputs to safe_compare_digest, /contact, and OAuth callback never raise 500."""
        import contact_form

        self.assertTrue(request_guards.safe_compare_digest('abcあ', 'abcあ'))
        self.assertTrue(request_guards.safe_compare_digest(b'xyz\xff', bytearray(b'xyz\xff')))
        self.assertFalse(request_guards.safe_compare_digest(None, ''))
        self.assertFalse(request_guards.safe_compare_digest('', None))
        self.assertFalse(request_guards.safe_compare_digest(123, '123'))
        self.assertFalse(request_guards.safe_compare_digest('\ud800', 'abc'))
        self.assertEqual(contact_form.verify_token('secret', '١٧٠٠٠٠٠٠٠٠.' + ('a' * 40)), 'invalid')

        old_testing = app_module.app.config.get('TESTING', True)
        app_module.app.config['TESTING'] = False
        try:
            r_contact = self.client.post(
                '/contact',
                data={
                    'csrf_token': '1700000000.あ',
                    'category': 'other',
                    'name': 'tester',
                    'email': 'a@example.com',
                    'message': 'テストメッセージ12345',
                },
                headers={'Origin': 'http://localhost'},
            )
            self.assertEqual(r_contact.status_code, 400)

            with self.client.session_transaction() as sess:
                sess['is_staff'] = True
                sess['is_master'] = True
                sess['auth_gen'] = app_module._compute_session_auth_gen(True)
                sess['yt_oauth_state'] = 'expected-ascii-state'
                sess['yt_oauth_state_ts'] = 9999999999
            r_oauth = self.client.get('/staff/youtube/oauth/callback?state=あ&code=dummy')
            self.assertEqual(r_oauth.status_code, 302)
            self.assertIn('yt_error=', r_oauth.headers.get('Location', ''))
        finally:
            app_module.app.config['TESTING'] = old_testing

    def test_m2_p0_03_p1_02_strict_json_origin_null_and_logout_post(self):
        """M2-P0-03/P1-02: Reject text/plain & malformed JSON on admin APIs, block Origin: null + Referer, and guard POST /staff/logout."""
        import contact_form
        import moshimo_reel_queue

        self.assertFalse(contact_form.is_same_origin('null', 'http://localhost/staff', 'localhost', strict=True))
        self.assertFalse(contact_form.is_same_origin('NULL', 'http://localhost/staff', 'localhost', strict=True))
        self.assertTrue(contact_form.is_same_origin('', 'http://localhost/staff', 'localhost', strict=True))
        self.assertFalse(contact_form.is_same_origin('', '', 'localhost', strict=True))

        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
            sess['auth_gen'] = app_module._compute_session_auth_gen(True)

        hdr_same = {'Origin': 'http://localhost'}
        with mock.patch.object(moshimo_reel_queue, 'publish_moshimo_episode_to_instagram', side_effect=AssertionError('Must not publish')):
            r_txt = self.client.post(
                '/api/moshimo-reel-queue',
                data='{"action":"unknown"}',
                content_type='text/plain',
                headers=hdr_same,
            )
            self.assertEqual(r_txt.status_code, 400)

        r_bad_json = self.client.post(
            '/api/moshimo-reel-queue',
            data='{broken',
            content_type='application/json',
            headers=hdr_same,
        )
        self.assertEqual(r_bad_json.status_code, 400)

        for ep, body in (
            ('/api/sns-jobs/enqueue', '{"job_type":"x_post"}'),
            ('/api/staff/sns-reel/settings', '{"auto_schedule_enabled":false}'),
            ('/api/pdca/youtube/disconnect', '{}'),
            ('/api/ops/governance', '{"action":"force_logout_staff"}'),
        ):
            r_plain = self.client.post(ep, data=body, content_type='text/plain', headers=hdr_same)
            self.assertEqual(r_plain.status_code, 400, f'{ep} should reject text/plain')

        # Truly empty POST body remains compatible
        r_empty_ok = self.client.post('/api/staff/sns-reel/settings', data=b'', headers=hdr_same)
        self.assertEqual(r_empty_ok.status_code, 200)

        # POST /staff/logout enforces Same-Origin and sets Cache-Control: private, no-store
        r_out_bad = self.client.post('/staff/logout', headers={'Origin': 'https://evil.example'})
        self.assertEqual(r_out_bad.status_code, 403)
        with self.client.session_transaction() as sess:
            self.assertTrue(sess.get('is_master'))

        r_out_ok = self.client.post('/staff/logout', headers=hdr_same)
        self.assertEqual(r_out_ok.status_code, 302)
        self.assertIn('private', r_out_ok.headers.get('Cache-Control', ''))
        self.assertIn('no-store', r_out_ok.headers.get('Cache-Control', ''))
        with self.client.session_transaction() as sess:
            self.assertFalse(sess.get('is_master'))


if __name__ == '__main__':
    unittest.main()
