"""AdSense 再申請準備（/about・/contact・サイトマップ・店舗ページの固有分析文）の回帰テスト。"""
import datetime
import os
import sys
import tempfile
import time
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix='aiseki_adsense_test_')
os.environ.setdefault('DISABLE_SCHEDULER', '1')
os.environ.setdefault('DISABLE_SHEETS', '1')
os.environ.setdefault('LOCAL_DB_PATH', os.path.join(_TMP, 'test.db'))
os.environ.setdefault('JOB_STATE_DIR', os.path.join(_TMP, 'state'))
os.environ.setdefault('APP_LOG_PATH', os.path.join(_TMP, 'app.log'))
os.environ.setdefault('LATEST_STATUS_SNAPSHOT_FILE', os.path.join(_TMP, 'latest_status_cache.json'))
os.environ.pop('ALERT_WEBHOOK_URL', None)
os.environ.setdefault('SECRET_KEY', 'test-secret-key-12345')

import app as app_module  # noqa: E402
import contact_form  # noqa: E402
import store_insights  # noqa: E402


def _synthetic_rows(store_name, days=21, base_w=10, late_factor=0.8, weekend_boost=2.0, start=None):
    """Deterministic monitor_logs-like rows (every 15 minutes from 18:00 to 04:45)."""
    start = start or datetime.datetime(2026, 10, 1)
    rows = []
    for d in range(days):
        day = start + datetime.timedelta(days=d)
        boost = weekend_boost if day.weekday() in (4, 5) else 1.0
        for slot in range(44):
            ts = day + datetime.timedelta(hours=18, minutes=15 * slot)
            h = ts.hour
            if 21 <= h <= 22:
                w = base_w
            elif h in (0, 1):
                w = base_w * late_factor
            elif h >= 18:
                w = base_w * 0.5
            else:
                w = base_w * late_factor * 0.6
            w = int(round(w * boost))
            m = int(round(w * 0.8))
            rows.append({'ts': ts.strftime('%Y-%m-%d %H:%M:%S'), 'name': store_name, 'women': w, 'men': m})
    return rows


class AboutContactPageTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()

    def setUp(self):
        contact_form.reset_rate_limits_for_tests()

    def test_about_page_renders_operator_and_method(self):
        r = self.client.get('/about')
        self.assertEqual(r.status_code, 200)
        html = r.data.decode('utf-8')
        for needle in ('運営者情報', 'データの取得方法', '更新頻度', '掲載方針', '非公式', '/contact', '/privacy',
                       'rel="canonical" href="https://aiseki-navi.com/about"'):
            self.assertIn(needle, html)

    def test_footer_and_privacy_link_to_about_and_contact(self):
        for path in ('/privacy', '/columns', '/'):
            html = self.client.get(path).data.decode('utf-8')
            self.assertIn('href="/contact"', html, path)
            self.assertIn('href="/about"', html, path)

    def test_contact_get_has_csrf_token_and_honeypot(self):
        r = self.client.get('/contact')
        self.assertEqual(r.status_code, 200)
        html = r.data.decode('utf-8')
        self.assertIn('name="csrf_token"', html)
        self.assertIn('name="website"', html)
        self.assertIn('no-store', r.headers.get('Cache-Control', ''))

    def _valid_form(self, **over):
        token = contact_form.issue_token(app_module.app.secret_key, now=time.time() - 30)
        data = {'csrf_token': token, 'category': 'correction', 'name': 'テスト',
                'email': 'user@example.com', 'message': '営業時間の表記が公式サイトと違っています。', 'website': ''}
        data.update(over)
        return data

    def test_contact_post_valid_is_stored_via_existing_channels(self):
        with mock.patch.object(contact_form, 'store_inquiry', return_value={'sheets': True, 'webhook': True}) as st:
            r = self.client.post('/contact', data=self._valid_form(),
                                 headers={'Origin': 'http://localhost'})
        self.assertEqual(r.status_code, 303)
        self.assertIn('/contact?sent=1', r.headers['Location'])
        st.assert_called_once()
        saved = st.call_args[0][0]
        self.assertEqual(saved['category'], 'correction')
        self.assertEqual(saved['email'], 'user@example.com')

    def test_contact_post_rejects_missing_or_forged_token(self):
        with mock.patch.object(contact_form, 'store_inquiry') as st:
            r1 = self.client.post('/contact', data=self._valid_form(csrf_token=''))
            r2 = self.client.post('/contact', data=self._valid_form(csrf_token='123.deadbeef'))
        self.assertEqual(r1.status_code, 400)
        self.assertEqual(r2.status_code, 400)
        st.assert_not_called()

    def test_contact_post_rejects_too_fast_and_expired_tokens(self):
        now = time.time()
        self.assertEqual(contact_form.verify_token('k', contact_form.issue_token('k', now=now), now=now), 'too_fast')
        self.assertEqual(contact_form.verify_token('k', contact_form.issue_token('k', now=now - 3 * 3600), now=now), 'expired')
        self.assertEqual(contact_form.verify_token('k', contact_form.issue_token('k', now=now - 60), now=now), '')

    def test_contact_post_rejects_cross_origin(self):
        with mock.patch.object(contact_form, 'store_inquiry') as st:
            r = self.client.post('/contact', data=self._valid_form(), headers={'Origin': 'https://evil.example'})
        self.assertEqual(r.status_code, 400)
        st.assert_not_called()

    def test_contact_honeypot_pretends_success_without_storing(self):
        with mock.patch.object(contact_form, 'store_inquiry') as st:
            r = self.client.post('/contact', data=self._valid_form(website='http://spam.example'))
        self.assertEqual(r.status_code, 303)
        st.assert_not_called()

    def test_contact_silent_spam_filter_drops_seo_scam_and_keeps_legitimate_inquiries(self):
        scam_msg = (
            "Feature aiseki-navi.com in Google's Search Index and have it show up in web search results!\n\n"
            "Submit aiseki-navi.com today: domainindex.live"
        )
        backlink_spam = (
            "Hi team, we offer high DA backlinks to help you rank #1 on Google within 30 days."
        )
        cyrillic_spam = "Привет! Заходите на наш сайт example-bonus.ru прямо сейчас"
        with mock.patch.object(contact_form, 'store_inquiry') as st:
            for msg in (scam_msg, backlink_spam, cyrillic_spam):
                contact_form.reset_rate_limits_for_tests()
                r = self.client.post(
                    '/contact',
                    data=self._valid_form(category='other', message=msg),
                    headers={'Origin': 'http://localhost'},
                )
                self.assertEqual(r.status_code, 303)
                self.assertIn('/contact?sent=1', r.headers['Location'])
            st.assert_not_called()

        # 正規の日本語問い合わせ（Googleに言及していても誤遮断ゼロ）＋正規の英語問い合わせは通常保存されること
        legit_jp = 'Google検索の時間表記と公式サイトの営業時間が異なっているため訂正をお願いします。'
        legit_en = 'Hello, I visited Oriental Lounge Shibuya last night and noticed a small typo on the map.'
        with mock.patch.object(contact_form, 'store_inquiry', return_value={'sheets': True, 'webhook': True}) as st_ok:
            for msg in (legit_jp, legit_en):
                contact_form.reset_rate_limits_for_tests()
                r = self.client.post(
                    '/contact',
                    data=self._valid_form(category='correction', message=msg),
                    headers={'Origin': 'http://localhost'},
                )
                self.assertEqual(r.status_code, 303)
            self.assertEqual(st_ok.call_count, 2)

    def test_contact_length_limits_and_validation(self):
        with mock.patch.object(contact_form, 'store_inquiry') as st:
            r_long = self.client.post('/contact', data=self._valid_form(message='あ' * 2001))
            r_short = self.client.post('/contact', data=self._valid_form(message='短い'))
            r_cat = self.client.post('/contact', data=self._valid_form(category='unknown'))
            r_mail = self.client.post('/contact', data=self._valid_form(email='not-an-email'))
            r_huge = self.client.post('/contact', data=self._valid_form(message='a' * 40000))
        for r in (r_long, r_short, r_cat, r_mail):
            self.assertEqual(r.status_code, 400)
        self.assertIn(r_huge.status_code, (400, 413))
        st.assert_not_called()

    def test_contact_rate_limit_per_ip(self):
        with mock.patch.object(contact_form, 'store_inquiry', return_value={}) as st:
            codes = [self.client.post('/contact', data=self._valid_form()).status_code for _ in range(4)]
        self.assertEqual(codes[:3], [303, 303, 303])
        self.assertEqual(codes[3], 429)
        self.assertEqual(st.call_count, 3)

    def test_store_inquiry_uses_sheets_and_webhook(self):
        import health
        import sheets_store
        with mock.patch.object(sheets_store, 'is_disabled', return_value=False), \
                mock.patch.object(sheets_store, 'append_rows_async') as app_rows, \
                mock.patch.object(health, 'send_alert', return_value=True) as alert:
            routes = contact_form.store_inquiry(
                {'category': 'other', 'name': '', 'email': '', 'message': 'テストのお問い合わせ本文です。'}, 'abc123')
        self.assertEqual(routes, {'sheets': True, 'webhook': True})
        title, header, rows = app_rows.call_args[0]
        self.assertEqual(title, contact_form.SHEET_TITLE)
        self.assertEqual(rows[0][4], 'テストのお問い合わせ本文です。')
        alert.assert_called_once()


class SitemapTests(unittest.TestCase):
    def test_sitemap_excludes_grooming_and_lists_about_contact(self):
        client = app_module.app.test_client()
        with app_module._aux_doc_cache_lock:
            app_module._sitemap_xml_cache['key'] = None
        xml = client.get('/sitemap.xml').data.decode('utf-8')
        self.assertNotIn('https://aiseki-navi.com/grooming<', xml)
        self.assertIn('https://aiseki-navi.com/about<', xml)
        self.assertIn('https://aiseki-navi.com/contact<', xml)
        # The page itself stays reachable.
        self.assertEqual(client.get('/grooming').status_code, 200)


class StoreInsightTests(unittest.TestCase):
    def setUp(self):
        store_insights.clear_cache()

    def test_insufficient_data_returns_none(self):
        self.assertIsNone(store_insights.compute_store_stats('X', rows=[]))
        few = _synthetic_rows('X', days=2)
        self.assertIsNone(store_insights.compute_store_stats('X', rows=few))
        self.assertIsNone(store_insights.build_store_insight('X', 'X店', stats_fn=lambda n: None))

    def test_stats_reflect_input_data(self):
        st = store_insights.compute_store_stats('A', rows=_synthetic_rows('A', base_w=10, late_factor=0.8))
        self.assertIsNotNone(st)
        self.assertEqual(st['retention_pct'], 80)
        self.assertEqual(st['weekend_avg_peak_w'], 20.0)
        self.assertEqual(st['weekday_avg_peak_w'], 10.0)
        best = max(st['weekday_table'], key=lambda r: r['avg_peak_w'])
        self.assertIn(best['wd'], (4, 5))

    def test_narrative_differs_between_stores_and_uses_only_real_numbers(self):
        data = {
            'A': store_insights.compute_store_stats('A', rows=_synthetic_rows('A', base_w=10, late_factor=0.4, weekend_boost=2.0)),
            'B': store_insights.compute_store_stats('B', rows=_synthetic_rows('B', base_w=30, late_factor=1.2, weekend_boost=1.1)),
        }
        ia = store_insights.build_store_insight('A', 'A店', 'テスト', ['A', 'B'], stats_fn=data.get)
        ib = store_insights.build_store_insight('B', 'B店', 'テスト', ['A', 'B'], stats_fn=data.get)
        ta = ''.join(s['text'] for s in ia['sections'])
        tb = ''.join(s['text'] for s in ib['sections'])
        self.assertIn('40%', ta)           # A: 0-1時台は21-22時台の4割
        self.assertIn('120%', tb)          # B: 終電後に増える
        self.assertIn('終電の前に女性の多くが帰る', ta)
        self.assertIn('むしろ増える', tb)
        self.assertIn('2番目', ta)
        self.assertIn('エリアで最も人が集まる', tb)
        self.assertNotEqual(ta, tb)

    def test_store_page_renders_insight_block(self):
        st = store_insights.compute_store_stats('OLG 新宿', rows=_synthetic_rows('OLG 新宿'))
        client = app_module.app.test_client()
        app_module.clear_fast_http_caches()
        with mock.patch.object(store_insights, 'get_store_stats_cached', side_effect=lambda n, days=28: st if n == 'OLG 新宿' else None):
            html = client.get('/store/oriental-lounge-shinjuku').data.decode('utf-8')
        self.assertIn('記録データから見る傾向', html)
        self.assertIn('終電後に女性がどれだけ残るか', html)
        self.assertIn('曜日別の記録', html)

    def test_store_page_without_data_has_no_insight_block(self):
        client = app_module.app.test_client()
        app_module.clear_fast_http_caches()
        with mock.patch.object(store_insights, 'get_store_stats_cached', return_value=None):
            html = client.get('/store/asobibar-shibuya').data.decode('utf-8')
        self.assertNotIn('記録データから見る傾向', html)

    def test_nearby_comparison_trimmed_to_current_and_nearest(self):
        comp = {'stores': [
            {'name': 'OLG 福岡', 'is_current_store': False, 'is_best_in_cluster': False},
            {'name': 'JIS FUKUOKA', 'is_current_store': False, 'is_best_in_cluster': False},
            {'name': 'OLG 鹿児島', 'is_current_store': False, 'is_best_in_cluster': True},
            {'name': 'OLG 熊本', 'is_current_store': False, 'is_best_in_cluster': False},
            {'name': 'OLG 長崎', 'is_current_store': True, 'is_best_in_cluster': False},
            {'name': 'OLG 小倉', 'is_current_store': False, 'is_best_in_cluster': False},
        ]}
        out = app_module._trim_nearby_comparison('OLG 長崎', comp, 4)
        names = [s['name'] for s in out['stores']]
        self.assertEqual(len(names), 4)
        self.assertEqual(names[0], 'OLG 長崎')
        expected = app_module._nearest_store_names('OLG 長崎', ['OLG 福岡', 'JIS FUKUOKA', 'OLG 鹿児島', 'OLG 熊本', 'OLG 小倉'], 3)
        self.assertEqual(names[1:], expected)
        self.assertEqual(out['total_cluster_stores'], 6)

    def test_daily_rows_leave_missing_days_blank(self):
        rows = [r for r in _synthetic_rows('A', days=21) if not r['ts'].startswith('2026-10-05')
                and not r['ts'].startswith('2026-10-06 0')]
        st = store_insights.compute_store_stats('A', rows=rows)
        by_label = {d['label']: d for d in st['daily_rows']}
        self.assertFalse(by_label['10/5(月)']['has_data'])
        self.assertNotIn('peak_w', by_label['10/5(月)'])
        self.assertTrue(by_label['10/6(火)']['has_data'])
        ins = store_insights.build_store_insight('A', 'A店', stats_fn=lambda n: st)
        self.assertEqual(len(ins['daily_recent']), 14)
        self.assertEqual(len(ins['daily_recent']) + len(ins['daily_older']), len(st['daily_rows']))

    def test_nearest_section_mentions_distance_and_neighbor(self):
        data = {
            'A': store_insights.compute_store_stats('A', rows=_synthetic_rows('A', base_w=10)),
            'B': store_insights.compute_store_stats('B', rows=_synthetic_rows('B', base_w=20)),
        }
        ins = store_insights.build_store_insight('A', 'A店', nearest=[('B', 0.34)], stats_fn=data.get,
                                                 seo_name_fn=lambda n: n + '店')
        near = [s for s in ins['sections'] if s['heading'] == '最寄りの店舗との比較']
        self.assertEqual(len(near), 1)
        self.assertIn('B店（直線約350m）', near[0]['text'])
        self.assertIn('多く', near[0]['text'])


if __name__ == '__main__':
    unittest.main()
