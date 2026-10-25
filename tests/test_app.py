"""
相席ナビ 自動テスト（追加ライブラリ不要・標準の unittest のみ）

実行方法（プロジェクト直下で）:
    python3 -m unittest discover -s tests -v

外部への通信（公式サイト・Google Sheets・X）は一切行いません。
"""
import os
import sys
import json
import shutil
import tempfile
import datetime
import unittest
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix='aiseki_test_')
os.environ['DISABLE_SCHEDULER'] = '1'
os.environ['DISABLE_SHEETS'] = '1'
os.environ['LOCAL_DB_PATH'] = os.path.join(_TMP, 'test.db')
os.environ['JOB_STATE_DIR'] = os.path.join(_TMP, 'state')
os.environ['APP_LOG_PATH'] = os.path.join(_TMP, 'app.log')
os.environ.pop('ALERT_WEBHOOK_URL', None)

import app as app_module  # noqa: E402
import local_db  # noqa: E402
import store_master  # noqa: E402
import health  # noqa: E402
import job_state  # noqa: E402
import analytics  # noqa: E402

local_db.init_db()

# 2026-10-23 時点で本番 /api/status が返した実店舗名
LIVE_STORE_NAMES = [
    'ALFA HIROSHIMA', 'OLG 渋谷本店', 'OLG 恵比寿', 'OLG ag渋谷', 'OLG 新宿', 'OLG 上野', 'OLG ag上野',
    'OLG 錦糸町', 'OLG 柏', 'OLG 町田', 'OLG 横浜', 'OLG 大宮', 'OLG 宇都宮', 'OLG 高崎', 'OLG 大阪駅前',
    'OLG ag梅田', 'OLG 天満', 'OLG 心斎橋', 'OLG 難波', 'OLG 京都', 'OLG 神戸', 'OLG 岡山', 'OLG ag広島',
    'OLG ag名古屋', 'OLG 名古屋 錦', 'OLG 名古屋 栄', 'OLG 静岡', 'OLG 浜松', 'OLG ag金沢', 'OLG ag仙台',
    'OLG 福岡', 'OLG 小倉', 'OLG 長崎', 'OLG 大分', 'OLG 熊本', 'OLG 宮崎', 'OLG 鹿児島', 'OLG ag沖縄',
    'OLG ソウル カンナム', 'OLG ソウル ホンデ', 'JIS SAPPORO', 'JIS OMIYA', 'JIS SHINJUKU',
    'JIS NISHISHINJUKU', 'JIS UMEDA', 'JIS NAMBA', 'JIS CHAYAMACHI', 'JIS FUKUOKA', 'JIS KUMAMOTO',
    'JIS MATSUYAMA', 'XIX OKAYAMA', 'YATAKOI UMEDA', 'CLOVERS HIROSHIMA',
]
REGIONS = {'Hokkaido', 'Tohoku', 'Kanto', 'Chubu', 'Kinki', 'Chugoku', 'Shikoku', 'Kyushu', 'Korea'}


def _fake_stores():
    return [{'name': n, 'men': 10, 'women': 12, 'source': 'test',
             'region': app_module.detect_region(n)} for n in LIVE_STORE_NAMES]


def _clear_votes():
    with local_db._votes_lock:
        local_db._memory_votes.clear()
    conn = local_db.get_connection()
    conn.execute('DELETE FROM crowd_votes')
    conn.commit()
    conn.close()


class PageRouteTests(unittest.TestCase):
    """主要ページが正常に表示されるか"""

    @classmethod
    def setUpClass(cls):
        cls.client = app_module.app.test_client()
        with app_module.data_lock:
            app_module.latest_data['full_data'] = _fake_stores()
            app_module.latest_data['last_updated'] = datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')

    def test_public_pages_return_200(self):
        paths = ['/', '/columns', '/areas', '/area/shinjuku', '/area/nagoya', '/area/tokyo-east',
                 '/oriental-lounge', '/ag', '/jis',
                 '/store/oriental-lounge-shinjuku', '/store/ag-shibuya', '/store/jis-shinjuku',
                 '/grooming', '/privacy', '/guide', '/guide/brand-comparison', '/guide/weekday-strategy',
                 '/guide/shinjuku', '/guide/solo-vs-pair', '/guide/second-venue', '/guide/first-10-minutes',
                 '/robots.txt', '/sitemap.xml', '/api/status', '/api/health', '/static/store_master.json']
        with mock.patch('weekly_report.generate_weekly_report', return_value={'error': 'offline'}):
            paths.append('/stats')
            for p in paths:
                with self.subTest(path=p):
                    r = self.client.get(p, headers={'User-Agent': 'Mozilla/5.0 test'})
                    try:
                        self.assertEqual(r.status_code, 200, p)
                    finally:
                        r.close()

    def test_staff_analytics_requires_login(self):
        r = self.client.get('/staff/analytics')
        self.assertEqual(r.status_code, 302)

    def test_staff_analytics_after_login(self):
        with self.client.session_transaction() as s:
            s['is_staff'] = True
        r = self.client.get('/staff/analytics')
        self.assertEqual(r.status_code, 200)
        self.assertIn('アクセス集計'.encode('utf-8'), r.data)
        with self.client.session_transaction() as s:
            s.pop('is_staff', None)

    def test_deep_health_ok_with_fresh_data(self):
        health.reset_for_tests()
        r = self.client.get('/api/health/deep')
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['store_count'], len(LIVE_STORE_NAMES))

    def test_area_page_tokyo_east_contains_kinshicho_and_nagoya_does_not(self):
        r = self.client.get('/area/tokyo-east')
        self.assertIn('OLG 錦糸町'.encode('utf-8'), r.data)
        r = self.client.get('/area/nagoya')
        self.assertNotIn('OLG 錦糸町'.encode('utf-8'), r.data)

    def test_ads_txt_and_adsense_tag_on_public_only(self):
        r_ads = self.client.get('/ads.txt')
        self.assertEqual(r_ads.status_code, 200)
        self.assertIn(b'google.com, pub-3043764334238627, DIRECT, f08c47fec0942fa0', r_ads.data)

        r_pub = self.client.get('/')
        self.assertIn(b'ca-pub-3043764334238627', r_pub.data)

        with self.client.session_transaction() as s:
            s['is_staff'] = True
        r_staff = self.client.get('/staff')
        self.assertEqual(r_staff.status_code, 200)
        self.assertNotIn(b'ca-pub-3043764334238627', r_staff.data)
        with self.client.session_transaction() as s:
            s.pop('is_staff', None)


class StoreMasterTests(unittest.TestCase):
    """店舗マスタの整合性"""

    def test_every_live_store_is_in_master(self):
        self.assertEqual(store_master.unknown_stores(LIVE_STORE_NAMES), [])

    def test_master_entries_are_complete(self):
        for name, info in store_master.load().items():
            with self.subTest(store=name):
                self.assertIn(info['region'], REGIONS)
                self.assertIsInstance(info['lat'], (int, float))
                self.assertIsInstance(info['lng'], (int, float))
                self.assertTrue(info['map_anchor'].strip())
                self.assertTrue(info['area_label'].strip())
                if info['area_slug'] is not None:
                    self.assertIn(info['area_slug'], app_module.AREA_GUIDES)

    def test_kinshicho_is_kanto_not_chubu(self):
        # 以前は '錦'（名古屋）にマッチして中部扱いになっていた
        self.assertEqual(app_module.detect_region('OLG 錦糸町'), 'Kanto')
        self.assertEqual(store_master.get('OLG 錦糸町')['area_label'], '東京・錦糸町')

    def test_unknown_store_falls_back_to_keywords(self):
        self.assertEqual(app_module.detect_region('OLG 新店舗 梅田'), 'Kinki')


class CrowdVoteTests(unittest.TestCase):
    """現場投票の重複防止・レート制限"""

    def setUp(self):
        _clear_votes()

    def test_same_device_cannot_vote_twice_within_30min(self):
        self.assertEqual(local_db.add_crowd_vote('OLG 新宿', 'smooth', 'ip1', 'dev1'), (True, 'ok'))
        self.assertEqual(local_db.add_crowd_vote('OLG 新宿', 'smooth', 'ip1', 'dev1'), (False, 'already_voted'))

    def test_different_devices_on_shared_mobile_ip_can_both_vote(self):
        self.assertTrue(local_db.add_crowd_vote('OLG 新宿', 'smooth', 'shared_ip', 'devA')[0])
        self.assertTrue(local_db.add_crowd_vote('OLG 新宿', 'smooth', 'shared_ip', 'devB')[0])
        self.assertEqual(local_db.get_recent_votes('OLG 新宿')['smooth'], 2)

    def test_rate_limit_per_device(self):
        stores = LIVE_STORE_NAMES[:local_db.VOTE_LIMIT_PER_VOTER + 1]
        results = [local_db.add_crowd_vote(s, 'lively', 'ipX', 'spammer')[1] for s in stores]
        self.assertEqual(results[-1], 'rate_limited')
        self.assertTrue(all(r == 'ok' for r in results[:-1]))

    def test_rate_limit_per_ip(self):
        n = local_db.VOTE_LIMIT_PER_IP
        for i in range(n):
            local_db.add_crowd_vote(LIVE_STORE_NAMES[i % len(LIVE_STORE_NAMES)], ['smooth', 'waiting', 'lively'][i % 3], 'botip', f'd{i}')
        self.assertEqual(local_db.add_crowd_vote('OLG 新宿', 'waiting', 'botip', 'fresh')[1], 'rate_limited')

    def test_api_rejects_unknown_store(self):
        with app_module.data_lock:
            app_module.latest_data['full_data'] = _fake_stores()
        c = app_module.app.test_client()
        r = c.post('/api/vote', json={'store_name': '存在しない店', 'vote_type': 'smooth', 'voter_id': 'x'})
        self.assertEqual(r.status_code, 400)
        r = c.post('/api/vote', json={'store_name': 'OLG 新宿', 'vote_type': 'smooth', 'voter_id': 'x'})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.get_json()['votes']['smooth'], 1)


class HealthAlertTests(unittest.TestCase):
    """データ取得の異常検知"""

    SOURCES = ['get_oriental_data', 'get_jis_data']

    def setUp(self):
        health.reset_for_tests()
        os.environ['ALERT_ALWAYS'] = '1'

    def tearDown(self):
        os.environ.pop('ALERT_ALWAYS', None)

    def _ok(self):
        return {'get_oriental_data': [{}] * 40, 'get_jis_data': [{}] * 10}

    def test_alert_after_consecutive_failures_and_recovery(self):
        for _ in range(health.FAIL_THRESHOLD - 1):
            health.record_fetch({'get_jis_data': [{}] * 10}, {'get_oriental_data': 'timeout'}, self.SOURCES)
        self.assertFalse(any(a['key'].startswith('down:') for a in health.snapshot()['recent_alerts']))
        health.record_fetch({'get_jis_data': [{}] * 10}, {'get_oriental_data': 'timeout'}, self.SOURCES)
        keys = [a['key'] for a in health.snapshot()['recent_alerts']]
        self.assertIn('down:get_oriental_data', keys)
        health.record_fetch(self._ok(), {}, self.SOURCES)
        keys = [a['key'] for a in health.snapshot()['recent_alerts']]
        self.assertIn('recover:get_oriental_data', keys)

    def test_store_count_drop_alert(self):
        health.record_fetch({'get_oriental_data': [{}] * 5, 'get_jis_data': [{}] * 10}, {}, self.SOURCES)
        keys = [a['key'] for a in health.snapshot()['recent_alerts']]
        self.assertIn('shrink:get_oriental_data', keys)

    def test_unknown_store_alert(self):
        self.assertEqual(health.check_unknown_stores(['OLG 新宿', 'OLG 新店舗']), ['OLG 新店舗'])
        self.assertEqual(health.check_unknown_stores(['OLG 新店舗']), [])  # 同じ店舗は2回通知しない

    def test_cooldown_suppresses_duplicate_alerts(self):
        self.assertTrue(health.send_alert('k', 'msg'))
        self.assertFalse(health.send_alert('k', 'msg'))

    def test_stale_olg_cache_counts_as_failure(self):
        import monitor
        monitor._olg_cache['data'] = [{'name': 'OLG 新宿', 'men': 1, 'women': 1, 'source': 'oriental'}]
        with mock.patch('monitor.requests.get', side_effect=Exception('network down')):
            data = monitor.get_oriental_data()
        self.assertTrue(data)  # キャッシュで表示は継続
        self.assertTrue(monitor._olg_cache['served_stale'])  # ただし監視上は失敗扱い


class JobStateTests(unittest.TestCase):
    """自動投稿の二重投稿防止"""

    def setUp(self):
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        job_state.reset_cache_for_tests()

    def test_mark_done_survives_reload(self):
        self.assertFalse(job_state.is_done('x_post:2026-10-23_20:00'))
        job_state.mark_done('x_post:2026-10-23_20:00')
        job_state.reset_cache_for_tests()  # 再起動を模擬
        self.assertTrue(job_state.is_done('x_post:2026-10-23_20:00'))

    def test_scheduled_x_post_runs_once_per_slot(self):
        with app_module.data_lock:
            app_module.latest_data['full_data'] = _fake_stores()
        with mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as post:
            r1 = app_module.run_scheduled_x_post('22:00')
            r2 = app_module.run_scheduled_x_post('22:00')
        self.assertEqual(r1['status'], 'success')
        self.assertEqual(r2['status'], 'skipped')
        self.assertEqual(post.call_count, 1)

    def test_failed_post_is_not_marked_done(self):
        with app_module.data_lock:
            app_module.latest_data['full_data'] = _fake_stores()
        with mock.patch('x_poster.post_to_x', return_value={'status': 'error', 'detail': '503'}) as post:
            app_module.run_scheduled_x_post('20:00')
            app_module.run_scheduled_x_post('20:00')
        self.assertEqual(post.call_count, 2)  # 失敗時は次回リトライできる

    def test_tip_post_runs_once(self):
        import tips_poster
        with mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as post:
            tips_poster.run_scheduled_tip_post('12:00')
            r = tips_poster.run_scheduled_tip_post('12:00')
        self.assertEqual(r['status'], 'skipped')
        self.assertEqual(post.call_count, 1)

    def test_note_first_run_does_not_repost_old_articles(self):
        import note_poster
        old = {'title': '古い記事', 'link': 'https://note.com/x/n/old', 'pub_date': 'Mon, 01 Sep 2026 10:00:00 +0900', 'guid': 'old'}
        with mock.patch.object(note_poster, 'STATE_FILE', os.path.join(_TMP, 'notes.json')), \
             mock.patch('note_poster.fetch_latest_note_articles', return_value=[old]), \
             mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as post:
            r = note_poster.check_and_post_new_note()
        self.assertEqual(r['status'], 'skipped')
        self.assertEqual(post.call_count, 0)


class AnalyticsTests(unittest.TestCase):
    """自前アクセス集計"""

    def setUp(self):
        analytics.reset_for_tests()
        self.client = app_module.app.test_client()

    def test_only_allowed_events_are_counted(self):
        self.assertTrue(analytics.track('map_cafe', 'OLG 新宿'))
        self.assertFalse(analytics.track('evil_event', 'x'))
        self.assertEqual(analytics.summary(1)['by_event'], {'map_cafe': 1})

    def test_affiliate_redirect_is_tracked(self):
        r = self.client.get('/link/orbis', headers={'User-Agent': 'Mozilla/5.0', 'Referer': 'https://aiseki-navi.com/grooming'})
        self.assertEqual(r.status_code, 302)
        labels = dict(analytics.summary(1)['by_label']['affiliate_click'])
        self.assertEqual(labels.get('orbis @ /grooming'), 1)

    def test_bots_are_not_counted(self):
        self.client.get('/columns', headers={'User-Agent': 'Googlebot/2.1'})
        self.client.post('/api/track', json={'event': 'map_bar'}, headers={'User-Agent': 'curl/8'})
        self.assertEqual(analytics.summary(1)['by_event'], {})

    def test_page_view_and_beacon(self):
        self.client.get('/guide/shinjuku', headers={'User-Agent': 'Mozilla/5.0'})
        self.client.post('/api/track', json={'event': 'store_open', 'label': 'JIS SHINJUKU'}, headers={'User-Agent': 'Mozilla/5.0'})
        ev = analytics.summary(1)['by_event']
        self.assertEqual(ev.get('page_view'), 1)
        self.assertEqual(ev.get('store_open'), 1)

    def test_describe_label_resolves_store_and_new_articles(self):
        d_store_pv = analytics.describe_label('page_view', '/store/oriental-lounge-shinjuku')
        self.assertIn('オリエンタルラウンジ 新宿', d_store_pv['title'])
        d_store_open = analytics.describe_label('store_open', 'OLG 新宿')
        self.assertIn('オリエンタルラウンジ 新宿', d_store_open['title'])
        self.assertEqual(d_store_open['href'], '/store/oriental-lounge-shinjuku')
        d_new_guide = analytics.describe_label('page_view', '/guide/male-lead-mechanics')
        self.assertIn('男性先行', d_new_guide['title'])
        d_area = analytics.describe_label('page_view', '/area/shibuya')
        self.assertIn('渋谷・恵比寿エリア', d_area['title'])

    def test_all_column_routes_return_200(self):
        routes = [
            '/guide',
            '/guide/brand-comparison',
            '/guide/weekday-strategy',
            '/guide/weekend-strategy',
            '/guide/shinjuku',
            '/guide/solo-vs-pair',
            '/guide/second-venue',
            '/guide/first-10-minutes',
            '/guide/club-area-midnight',
            '/guide/beauty-area-stage',
            '/guide/male-lead-mechanics',
            '/guide/price-event-comparison',
        ]
        for path in routes:
            with self.subTest(path=path):
                r = self.client.get(path)
                self.assertEqual(r.status_code, 200)


class ScrapeIntervalTests(unittest.TestCase):
    """営業時間外は取得間隔を落として公式サイトへの負荷を減らす"""

    def test_interval(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        self.assertEqual(app_module.fetch_interval_sec(datetime.datetime(2026, 10, 23, 10, 0, tzinfo=jst)), 600)
        self.assertEqual(app_module.fetch_interval_sec(datetime.datetime(2026, 10, 23, 21, 0, tzinfo=jst)), 60)
        self.assertEqual(app_module.fetch_interval_sec(datetime.datetime(2026, 10, 24, 2, 0, tzinfo=jst)), 60)


class MomentumTests(unittest.TestCase):
    """15分前比の人数増減（women_diff / men_diff）計算とSSR表示の検証"""

    def test_snapshot_around_and_ssr_momentum(self):
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        ts_15m_ago = (now_jst - datetime.timedelta(minutes=15)).strftime('%Y-%m-%d %H:%M:%S')
        local_db.insert_logs_with_ts([
            {'ts': ts_15m_ago, 'name': 'OLG 恵比寿', 'men': 20, 'women': 40, 'source': 'oriental'},
            {'ts': ts_15m_ago, 'name': 'OLG 新宿', 'men': 30, 'women': 50, 'source': 'oriental'},
        ])
        snap = local_db.get_snapshot_around(10, 25)
        self.assertIn('OLG 恵比寿', snap)
        self.assertEqual(snap['OLG 恵比寿']['women'], 40)
        self.assertEqual(snap['OLG 恵比寿']['men'], 20)

        # Verify SSR renders both .momentum-tag and .stat-diff when women_diff / men_diff > 0
        with app_module.data_lock:
            for s in app_module.latest_data['full_data']:
                if s['name'] == 'OLG 恵比寿':
                    s['women'] = 55
                    s['men'] = 28
                    s['women_diff'] = 15
                    s['men_diff'] = 8
                    break
        client = app_module.app.test_client()
        r = client.get('/')
        html = r.data.decode('utf-8')
        self.assertIn('↗ +15名', html)
        self.assertIn('+15</span>', html)
        self.assertIn('+8</span>', html)


class FrontendSyntaxTests(unittest.TestCase):
    """app.js の構文チェック（node または macOS jsc）"""

    def test_app_js_syntax(self):
        import subprocess
        js_path = os.path.join(ROOT, 'static', 'app.js')
        node = shutil.which('node') or os.environ.get('NODE_BIN')
        if node:
            r = subprocess.run([node, '--check', js_path], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stderr)
            return
        jsc = '/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc'
        if os.path.exists(jsc):
            r = subprocess.run([jsc, '-e', f"new Function(readFile({json.dumps(js_path)}));"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)
            return
        self.skipTest('node / jsc が見つからないためスキップ')

    def test_store_master_json_is_valid(self):
        with open(os.path.join(ROOT, 'static', 'store_master.json'), encoding='utf-8') as f:
            self.assertGreaterEqual(len(json.load(f)['stores']), 53)

    def test_affiliate_config_js_syntax(self):
        import subprocess
        js_path = os.path.join(ROOT, 'static', 'affiliate_config.js')
        jsc = '/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc'
        if os.path.exists(jsc):
            r = subprocess.run([jsc, '-e', f"new Function(readFile({json.dumps(js_path)}));"], capture_output=True, text=True)
            self.assertEqual(r.returncode, 0, r.stdout + r.stderr)


class ScheduledArticlesTests(unittest.TestCase):
    """日時指定の予約公開システム（事前404秘匿・社内プレビュー・自動解禁・X自動告知・文字数制限）"""

    def setUp(self):
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        job_state.reset_cache_for_tests()
        self.client = app_module.app.test_client()

    def test_before_publish_hidden_from_public_and_visible_to_staff(self):
        import scheduled_articles
        import tips_poster
        before_dt = datetime.datetime(2026, 10, 25, 12, 0, 0)
        with mock.patch.object(scheduled_articles, 'now_jst_naive', return_value=before_dt):
            for slug in ('local-vs-urban', 'cost-break-even', 'rain-payday'):
                r = self.client.get(f'/guide/{slug}')
                self.assertEqual(r.status_code, 404, slug)

            col_html = self.client.get('/columns').data.decode('utf-8')
            sm_xml = self.client.get('/sitemap.xml').data.decode('utf-8')
            idx_html = self.client.get('/').data.decode('utf-8')
            for slug in ('local-vs-urban', 'cost-break-even', 'rain-payday'):
                self.assertNotIn(f'/guide/{slug}', col_html)
                self.assertNotIn(f'/guide/{slug}', sm_xml)
                self.assertNotIn(f'/guide/{slug}', idx_html)

            # Tip replies fall back safely to /columns before release
            for tip_idx in (58, 59, 60):
                self.assertIn('/columns', tips_poster.get_tip_reply_text(tip_index=tip_idx))

            # Logged-in staff can preview all 3 upcoming articles with noindex protection
            with self.client.session_transaction() as s:
                s['is_staff'] = True
            for slug in ('local-vs-urban', 'cost-break-even', 'rain-payday'):
                r_prev = self.client.get(f'/guide/{slug}')
                self.assertEqual(r_prev.status_code, 200, slug)
                self.assertEqual(r_prev.headers.get('X-Robots-Tag'), 'noindex, nofollow')
                prev_html = r_prev.data.decode('utf-8')
                self.assertIn('社内限定プレビュー', prev_html)
                self.assertNotIn('ca-pub-3043764334238627', prev_html)
                self.assertNotIn('/link/', prev_html)

            staff_dash = self.client.get('/staff').data.decode('utf-8')
            self.assertIn('予約待機中', staff_dash)
            with self.client.session_transaction() as s:
                s.pop('is_staff', None)

    def test_auto_release_and_x_announcement_once(self):
        import scheduled_articles
        import tips_poster
        # Step 1: 1st article release time arrives (2026-10-28 19:05 JST)
        first_dt = datetime.datetime(2026, 10, 28, 19, 5, 0)
        with mock.patch.object(scheduled_articles, 'now_jst_naive', return_value=first_dt):
            r1 = self.client.get('/guide/local-vs-urban')
            self.assertEqual(r1.status_code, 200)
            self.assertIsNone(r1.headers.get('X-Robots-Tag'))
            self.assertNotIn('社内限定プレビュー', r1.data.decode('utf-8'))

            # 2nd and 3rd articles remain 404
            self.assertEqual(self.client.get('/guide/cost-break-even').status_code, 404)
            self.assertEqual(self.client.get('/guide/rain-payday').status_code, 404)

            # /columns, /, and /sitemap.xml list only the released 1st article
            self.assertIn('/guide/local-vs-urban', self.client.get('/columns').data.decode('utf-8'))
            self.assertIn('/guide/local-vs-urban', self.client.get('/sitemap.xml').data.decode('utf-8'))
            self.assertNotIn('/guide/cost-break-even', self.client.get('/sitemap.xml').data.decode('utf-8'))

            # Auto-announcement posts once and deduplicates via job_state
            with mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as mock_post:
                res_first = scheduled_articles.check_and_announce_newly_published()
                res_second = scheduled_articles.check_and_announce_newly_published()
            self.assertEqual(len(res_first), 1)
            self.assertEqual(res_first[0]['slug'], 'local-vs-urban')
            self.assertEqual(res_second, [])
            self.assertEqual(mock_post.call_count, 1)

        # Step 2: All 3 articles released (2026-11-05 19:10 JST)
        all_dt = datetime.datetime(2026, 11, 5, 19, 10, 0)
        with mock.patch.object(scheduled_articles, 'now_jst_naive', return_value=all_dt):
            for slug in ('local-vs-urban', 'cost-break-even', 'rain-payday'):
                self.assertEqual(self.client.get(f'/guide/{slug}').status_code, 200)
            self.assertIn('/guide/local-vs-urban', tips_poster.get_tip_reply_text(tip_index=58))
            self.assertIn('/guide/cost-break-even', tips_poster.get_tip_reply_text(tip_index=59))
            self.assertIn('/guide/rain-payday', tips_poster.get_tip_reply_text(tip_index=60))

    def test_scheduled_tweets_and_all_tips_within_280_weight(self):
        import scheduled_articles
        import x_poster
        import tips_poster
        for item in scheduled_articles.SCHEDULED_ARTICLES:
            w = x_poster.compute_twitter_weight(item['x_main_text'])
            self.assertLessEqual(w, 280, f"{item['slug']} weight={w}")
        for i in range(len(tips_poster.TIPS_LIST)):
            tip_text = tips_poster.get_tip_for_slot(tip_index=i)
            w = x_poster.compute_twitter_weight(tip_text)
            self.assertLessEqual(w, 280, f"tip[{i}] weight={w}")


class SeoAndXMediaUpgradeTests(unittest.TestCase):
    """Verify off-hours 0-count SEO burn-in prevention and native X image attachment."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_store_detail_off_hours_vs_live_hours_seo_and_json_ld(self):
        import re
        fake_7d = {
            'has_history': True,
            'sample_count': 120,
            'daily_logs': [],
            'max_7d_women': 48,
            'best_slot_name': '20:00〜21:30頃（早めの時間帯）',
            'best_slot_pct': 62,
            'best_wd_name': '水曜・木曜の夜',
            'best_wd_pct': 60,
            'strong_midnight': True,
        }
        slug = store_master.slug_of('OLG 新宿')
        # 1) Off-hours (0 women, 0 men) -> must NOT burn in 0名/0% into title, meta, or JSON-LD
        with app_module.data_lock:
            old_data = list(app_module.latest_data.get('full_data') or [])
            app_module.latest_data['full_data'] = [
                {'name': 'OLG 新宿', 'women': 0, 'men': 0, 'total': 0, 'region': 'Kanto'}
            ]
        try:
            with mock.patch('local_db.get_store_7day_analytics', return_value=fake_7d):
                resp = self.client.get(f'/store/{slug}')
                self.assertEqual(resp.status_code, 200)
                html = resp.data.decode('utf-8')
                self.assertNotIn('女性0名', html)
                self.assertNotIn('男性0名', html)
                self.assertNotIn('比率0%', html)
                self.assertIn('直近最高：女性48名', html)
                self.assertIn('62%', html)
                ld_blocks = re.findall(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', html, re.S)
                self.assertGreaterEqual(len(ld_blocks), 1)
                for block in ld_blocks:
                    json.loads(block)

            # 2) Live business hours (35 women, 22 men) -> shows live counts accurately
            with app_module.data_lock:
                app_module.latest_data['full_data'] = [
                    {'name': 'OLG 新宿', 'women': 35, 'men': 22, 'total': 57, 'region': 'Kanto'}
                ]
            with mock.patch('local_db.get_store_7day_analytics', return_value=fake_7d):
                resp_live = self.client.get(f'/store/{slug}')
                html_live = resp_live.data.decode('utf-8')
                self.assertIn('現在：女性35名／男性22名', html_live)
                for block in re.findall(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', html_live, re.S):
                    json.loads(block)
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = old_data

    def test_brand_page_off_hours_hides_zero_count_in_json_ld(self):
        import re
        with app_module.data_lock:
            old_data = list(app_module.latest_data.get('full_data') or [])
            app_module.latest_data['full_data'] = [
                {'name': 'OLG 新宿', 'women': 0, 'men': 0, 'total': 0, 'region': 'Kanto'}
            ]
        try:
            resp = self.client.get('/oriental-lounge')
            self.assertEqual(resp.status_code, 200)
            html = resp.data.decode('utf-8')
            self.assertNotIn('女性0名・男性0名が来店中', html)
            for block in re.findall(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', html, re.S):
                json.loads(block)
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = old_data

    def test_x_media_upload_and_graceful_fallback(self):
        import x_poster
        env_vars = {
            'X_API_KEY': 'k',
            'X_API_SECRET': 's',
            'X_ACCESS_TOKEN': 't',
            'X_ACCESS_TOKEN_SECRET': 'ts',
        }
        with mock.patch.dict(os.environ, env_vars):
            # Case A: Upload succeeds -> media_ids attached to tweet payload
            upload_resp = mock.MagicMock(status_code=200)
            upload_resp.json.return_value = {'media_id_string': '9988776655'}
            tweet_resp = mock.MagicMock(status_code=201)
            tweet_resp.json.return_value = {'data': {'id': '111222'}}

            with mock.patch('x_poster.requests.post', side_effect=[upload_resp, tweet_resp]) as m_post:
                res = x_poster.post_to_x('テスト本文', media_bytes=b'\x89PNG_FAKE')
                self.assertEqual(res['status'], 'success')
                self.assertEqual(res['media_id'], '9988776655')
                self.assertEqual(m_post.call_count, 2)
                self.assertIn('upload.twitter.com/1.1/media/upload.json', m_post.call_args_list[0][0][0])
                tweet_payload = m_post.call_args_list[1][1]['json']
                self.assertEqual(tweet_payload['media'], {'media_ids': ['9988776655']})

            # Case B: Upload fails (HTTP 403) -> safely falls back to text-only tweet
            fail_upload = mock.MagicMock(status_code=403, text='Forbidden')
            with mock.patch('x_poster.requests.post', side_effect=[fail_upload, tweet_resp]) as m_post2:
                res2 = x_poster.post_to_x('フォールバック本文', media_bytes=b'\x89PNG_FAKE')
                self.assertEqual(res2['status'], 'success')
                self.assertNotIn('media_id', res2)
                tweet_payload2 = m_post2.call_args_list[1][1]['json']
                self.assertNotIn('media', tweet_payload2)

    def test_scheduled_x_post_attaches_png_and_column_eyecatches_exist(self):
        import x_poster
        import scheduled_articles
        sample_stores = [
            {'name': 'OLG 梅田茶屋町', 'women': 42, 'men': 25, 'region': 'Kinki'},
            {'name': 'JIS UMEDA', 'women': 14, 'men': 28, 'region': 'Kinki'},
            {'name': 'OLG 渋谷本店', 'women': 38, 'men': 24, 'region': 'Kanto'},
            {'name': 'JIS SHINJUKU', 'women': 30, 'men': 20, 'region': 'Kanto'},
        ]
        import re
        for slot in ('20:00', '22:00', '00:00', '02:00'):
            txt = x_poster.build_scheduled_tweet_text(sample_stores, slot_label=slot)
            self.assertLessEqual(x_poster.compute_twitter_weight(txt), 280, f"slot={slot} txt={txt}")
            self.assertIsNone(re.search(r'女\d', txt), f"Found standalone 女 in {txt}")
            self.assertIn('女性', txt)

        with app_module.data_lock:
            old_data = list(app_module.latest_data.get('full_data') or [])
            app_module.latest_data['full_data'] = sample_stores
        try:
            with mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as m_xpost:
                app_module.run_scheduled_x_post(slot_label='22:00', force=True)
                self.assertEqual(m_xpost.call_count, 1)
                passed_bytes = m_xpost.call_args[1].get('media_bytes')
                self.assertIsNotNone(passed_bytes)
                self.assertTrue(passed_bytes.startswith(b'\x89PNG'))
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = old_data

        for item in x_poster.COLUMN_PROMO_LIST + scheduled_articles.SCHEDULED_ARTICLES:
            img_bytes, mime = x_poster.load_column_eyecatch_bytes(item)
            self.assertIsNotNone(img_bytes, f"Missing eyecatch file for {item['slug']}")
            self.assertGreater(len(img_bytes), 1000)
            self.assertEqual(mime, 'image/jpeg')

    def test_note_creator_id_migration_and_dedup_across_id_change(self):
        import note_poster
        html = self.client.get('/').data.decode('utf-8')
        self.assertIn('https://note.com/aiseki_navi', html)
        self.assertNotIn('huge_finch7078', html)

        with mock.patch.dict(os.environ, {'NOTE_RSS_URL': 'https://note.com/huge_finch7078/rss'}):
            self.assertEqual(note_poster._resolve_note_rss_url(), 'https://note.com/aiseki_navi/rss')

        # Existing article recorded under old creator ID must NOT be re-posted under new creator ID
        posted_set = {'https://note.com/huge_finch7078/n/n987654321ab'}
        self.assertTrue(
            note_poster._is_note_already_posted('https://note.com/aiseki_navi/n/n987654321ab', posted_set)
        )
        self.assertFalse(
            note_poster._is_note_already_posted('https://note.com/aiseki_navi/n/n_brand_new_article', posted_set)
        )


class GrowthFeaturesTests(unittest.TestCase):
    """Tests for X bonus-time guerrilla alerts and /staff/sns-kit IG/Threads generator."""

    def setUp(self):
        self.client = app_module.app.test_client()

    def test_price_comparison_board_and_store_detail_embed(self):
        import re
        with app_module.data_lock:
            old_data = list(app_module.latest_data.get('full_data') or [])
            app_module.latest_data['full_data'] = [
                {'name': 'OLG 新宿', 'women': 28, 'men': 22, 'total': 50, 'region': 'Kanto'},
                {'name': 'OLG ag新宿', 'women': 24, 'men': 16, 'total': 40, 'region': 'Kanto'},
                {'name': 'JIS SHINJUKU', 'women': 12, 'men': 24, 'total': 36, 'region': 'Kanto'},
                {'name': 'JIS NISHISHINJUKU', 'women': 20, 'men': 14, 'total': 34, 'region': 'Kanto'},
            ]
        try:
            resp = self.client.get('/price-comparison?area=shinjuku')
            self.assertEqual(resp.status_code, 200)
            self.assertIn('no-store', resp.headers.get('Cache-Control', ''))
            html = resp.data.decode('utf-8')
            self.assertIn('店舗別料金（待機・相席単価・1名入店可否）＆開催中イベント', html)
            self.assertIn('オリエンタルラウンジ 新宿', html)
            self.assertIn('ag（アグ）渋谷', html)
            self.assertIn('JIS（ジス）新宿', html)
            self.assertIn('330円 / 10分', html)
            self.assertIn('平日990円 / 金土1,500円', html)  # OLG 恵比寿 24:00~Close rate
            self.assertIn('2名以上必須（男性1名入店不可）', html)
            self.assertIn('全国全51店舗の「4時間帯別相席料金・単独（待機）料金・1名入店可否」完全一覧表', html)
            self.assertIn('initLivePriceComparisonSync', html)
            self.assertNotIn('0円〜220円', html)
            self.assertNotIn('ロス0円', html)
            self.assertNotIn('/link/', html)
            ld_blocks = re.findall(r'<script type="application/ld\+json">\s*(.*?)\s*</script>', html, re.S)
            self.assertGreaterEqual(len(ld_blocks), 2)
            for block in ld_blocks:
                json.loads(block)

            # Verify all 51 domestic stores in store_master match STORE_PRICE_TABLE 1-to-1 with accurate solo/wait & 4-slot rules
            import price_comparison
            domestic_master = {k for k, v in store_master.load().items() if v.get('region') != 'Korea'}
            self.assertEqual(set(price_comparison.STORE_PRICE_TABLE.keys()), domestic_master)
            self.assertEqual(len(domestic_master), 51)
            for sname in domestic_master:
                prof = price_comparison.get_store_price_profile(sname)
                self.assertNotIn('0円〜', prof['wait_fee_str'], f"{sname} has ambiguous 0円〜 wait_fee_str")
                self.assertNotIn('ロス0円', prof['verdict_badge'])
                if prof.get('is_flat_rate'):
                    self.assertEqual(sname, 'YATAKOI UMEDA')
                    self.assertFalse(prof['has_wait_fee'])
                else:
                    self.assertGreater(prof['wait_10m_yen'], 0, f"{sname} wait_10m_yen must be > 0")
                    self.assertTrue(prof['has_wait_fee'])
                for slot_key in ('slot_18_20', 'slot_20_22', 'slot_22_24', 'slot_24_last'):
                    self.assertTrue(prof.get(slot_key), f"{sname} missing {slot_key}")
                self.assertTrue(prof['solo_rule'])
                self.assertTrue(prof['active_slot_rate'])

            # Spot-check verified official store-specific pricing & solo entry rules
            self.assertEqual(price_comparison.get_store_price_profile('ALFA HIROSHIMA')['wait_fee_str'], '220円 / 10分（税込・飲み放題込）')
            self.assertEqual(price_comparison.get_store_price_profile('CLOVERS HIROSHIMA')['wait_fee_str'], '220円 / 10分（税込・飲み放題込）')
            self.assertEqual(price_comparison.get_store_price_profile('XIX OKAYAMA')['wait_fee_str'], '220円 / 10分（税込・飲み放題込）')
            self.assertEqual(price_comparison.get_store_price_profile('OLG ag金沢')['wait_10m_yen'], 330)
            self.assertEqual(price_comparison.get_store_price_profile('OLG ag仙台')['wait_10m_yen'], 220)
            self.assertIn('平日のみ1名OK', price_comparison.get_store_price_profile('OLG ag仙台')['solo_rule'])
            self.assertIn('2名以上必須', price_comparison.get_store_price_profile('OLG ag渋谷')['solo_rule'])
            self.assertIn('2名以上必須', price_comparison.get_store_price_profile('OLG ag上野')['solo_rule'])
            self.assertIn('2名以上必須', price_comparison.get_store_price_profile('OLG ag名古屋')['solo_rule'])
            self.assertIn('2名以上必須', price_comparison.get_store_price_profile('OLG 大阪駅前')['solo_rule'])
            self.assertEqual(price_comparison.get_store_price_profile('JIS SAPPORO')['slot_20_22'], '平日660円 / 金土770円')
            self.assertEqual(price_comparison.get_store_price_profile('JIS MATSUYAMA')['slot_22_24'], '平日550円 / 金土660円')
            self.assertEqual(price_comparison.get_store_price_profile('OLG 名古屋 栄')['slot_22_24'], '平日770円 / 金土990円')
            self.assertEqual(price_comparison.get_store_price_profile('OLG 心斎橋')['slot_20_22'], '平日660円 / 金土770円')
            self.assertEqual(price_comparison.get_store_price_profile('OLG 宮崎')['slot_18_20'], '平日550円 / 金土660円')

            # Verify time-slot detection across all 4 brackets (19h, 21h, 23h, 01h JST)
            jst = datetime.timezone(datetime.timedelta(hours=9))
            self.assertEqual(price_comparison.get_current_time_slot_info(datetime.datetime(2026, 10, 30, 19, 0, tzinfo=jst))['slot_key'], 'slot_18_20')
            self.assertEqual(price_comparison.get_current_time_slot_info(datetime.datetime(2026, 10, 30, 21, 0, tzinfo=jst))['slot_key'], 'slot_20_22')
            self.assertEqual(price_comparison.get_current_time_slot_info(datetime.datetime(2026, 10, 30, 23, 0, tzinfo=jst))['slot_key'], 'slot_22_24')
            self.assertEqual(price_comparison.get_current_time_slot_info(datetime.datetime(2026, 10, 31, 1, 30, tzinfo=jst))['slot_key'], 'slot_24_last')

            # Verify all 53 store detail pages render HTTP 200 without template or JSON-LD errors
            for sname in store_master.load().keys():
                sslug = store_master.slug_of(sname)
                sr = self.client.get(f'/store/{sslug}')
                self.assertEqual(sr.status_code, 200, f"/store/{sslug} failed")

            # Verify store detail page also embeds the nearby price & event comparison table + live sync JS + no-store header
            s_resp = self.client.get('/store/oriental-lounge-shinjuku')
            self.assertEqual(s_resp.status_code, 200)
            self.assertIn('no-store', s_resp.headers.get('Cache-Control', ''))
            s_html = s_resp.data.decode('utf-8')
            self.assertIn('近隣ライバル店の料金・開催中イベント・現在人数比較', s_html)
            self.assertIn('JIS（ジス）新宿', s_html)
            self.assertIn('JIS（ジス）西新宿', s_html)
            self.assertIn('initStoreDetailLiveSync', s_html)

            sw_resp = self.client.get('/sw.js')
            try:
                self.assertEqual(sw_resp.status_code, 200)
                self.assertIn('no-store', sw_resp.headers.get('Cache-Control', ''))
                sw_txt = sw_resp.data.decode('utf-8')
                self.assertIn("aiseki-navi-v45", sw_txt)
            finally:
                sw_resp.close()

            # Check inline JS syntax in /price-comparison and /store/oriental-lounge-shinjuku via node --check or macOS jsc -s
            node_bin = shutil.which('node')
            jsc_bin = '/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc'
            js_checker = ([node_bin, '--check'] if node_bin else ([jsc_bin, '-s'] if os.path.exists(jsc_bin) else None))
            if js_checker:
                import subprocess
                for label, page_html in (('price_comparison', html), ('store_detail', s_html)):
                    inline_scripts = re.findall(r'<script(?![^>]*type="application/ld\+json")(?![^>]*\bsrc=)[^>]*>(.*?)</script>', page_html, re.S)
                    self.assertGreaterEqual(len(inline_scripts), 1, label)
                    for idx_s, js_code in enumerate(inline_scripts):
                        tmp_js = os.path.join(_TMP, f'inline_{label}_{idx_s}.js')
                        with open(tmp_js, 'w', encoding='utf-8') as f:
                            f.write('function __syntax_check__() {\n' + js_code + '\n}\n')
                        proc = subprocess.run(js_checker + [tmp_js], capture_output=True, text=True)
                        self.assertEqual(proc.returncode, 0, f"JS syntax error in {label}#{idx_s}: {proc.stderr}{proc.stdout}")

            # Verify Hiroshima & Okayama independent regional brands (ALFA, CLOVERS, XIX) events & comparison
            h_resp = self.client.get('/price-comparison?area=hiroshima-okayama')
            self.assertEqual(h_resp.status_code, 200)
            h_html = h_resp.data.decode('utf-8')
            self.assertIn('相席ラウンジALFA 広島', h_html)
            self.assertIn('相席CLOVERS 広島', h_html)
            self.assertIn('相席ラウンジXIX 岡山', h_html)
            self.assertIn('日〜木22時までの入店で男性相席料金が半額', h_html)

            clovers_resp = self.client.get('/store/clovers-hiroshima')
            self.assertEqual(clovers_resp.status_code, 200)
            clovers_html = clovers_resp.data.decode('utf-8')
            self.assertIn('相席ラウンジALFA 広島', clovers_html)
            self.assertIn('ag（アグ）広島', clovers_html)
            self.assertIn('QUOカード1,000円分', clovers_html)

            smap = self.client.get('/sitemap.xml').data.decode('utf-8')
            self.assertIn('https://aiseki-navi.com/price-comparison', smap)
            self.assertIn('料金・開催中イベント比較', analytics.describe_label('page_view', '/price-comparison').get('title', ''))
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = old_data

    def test_no_duplicate_column_within_same_day_across_all_days(self):
        import tips_poster
        import job_state

        jst = datetime.timezone(datetime.timedelta(hours=9))
        base_dt = datetime.datetime(2026, 1, 1, 12, 0, tzinfo=jst)
        for day_offset in range(366):
            dt = base_dt + datetime.timedelta(days=day_offset)
            idx_12 = tips_poster._select_tip_index(slot_label="12:00", now_jst=dt)
            idx_18 = tips_poster._select_tip_index(slot_label="18:00", now_jst=dt)
            self.assertNotEqual(idx_12, idx_18, f"Same tip index on {dt.date()}")

            _, col_12 = tips_poster.resolve_tip_column(slot_label="12:00", now_jst=dt)
            _, col_18 = tips_poster.resolve_tip_column(slot_label="18:00", now_jst=dt)
            self.assertNotEqual(col_12, col_18, f"Duplicate column {col_12} on {dt.date()}")

        # Also verify job_state lock (`x_col_used:YYYY-MM-DD:<path>`) forces a different column
        test_dt = datetime.datetime(2099, 12, 31, 12, 0, tzinfo=jst)
        _, default_col = tips_poster.resolve_tip_column(slot_label="12:00", now_jst=test_dt)
        job_state.mark_done(f"x_col_used:2099-12-31:{default_col}")
        _, next_col = tips_poster.resolve_tip_column(slot_label="12:00", now_jst=test_dt)
        self.assertNotEqual(default_col, next_col)

    def test_bonus_alert_detection_window_dedup_and_png(self):
        import io
        import re
        from PIL import Image
        import bonus_alert

        jst = datetime.timezone(datetime.timedelta(hours=9))
        # Daytime blocked
        ok_day, _ = bonus_alert.is_within_alert_window(datetime.datetime(2026, 10, 30, 14, 0, tzinfo=jst))
        self.assertFalse(ok_day)
        # Near 22:00 scheduled slot blocked
        ok_near, _ = bonus_alert.is_within_alert_window(datetime.datetime(2026, 10, 30, 21, 50, tzinfo=jst))
        self.assertFalse(ok_near)
        # 21:15 valid guerrilla window
        valid_dt = datetime.datetime(2026, 10, 30, 21, 15, tzinfo=jst)
        ok_valid, _ = bonus_alert.is_within_alert_window(valid_dt)
        self.assertTrue(ok_valid)

        stores = [
            {'name': 'OLG 渋谷本店', 'women': 38, 'men': 15, 'women_diff': 4, 'region': 'Kanto'},
            {'name': 'JIS SHINJUKU', 'women': 20, 'men': 25, 'region': 'Kanto'},
        ]
        with mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as m_post:
            r1 = bonus_alert.check_and_post_bonus_alert(stores, now_jst=valid_dt)
            r2 = bonus_alert.check_and_post_bonus_alert(stores, now_jst=valid_dt)

        self.assertEqual(r1['status'], 'success')
        self.assertEqual(r1['mode'], 'female_lead')
        self.assertEqual(r1['store_name'], 'OLG 渋谷本店')
        self.assertEqual(r2['status'], 'skipped')
        self.assertEqual(m_post.call_count, 1)

        import x_poster
        self.assertLessEqual(x_poster.compute_twitter_weight(r1['tweet_text']), 280)
        self.assertIsNone(re.search(r'女\d', r1['tweet_text']))
        png_bytes = m_post.call_args[1].get('media_bytes')
        self.assertIsNotNone(png_bytes)
        with Image.open(io.BytesIO(png_bytes)) as im:
            self.assertEqual(im.size, (1200, 630))

    def test_staff_sns_kit_requires_login_and_renders_5_portrait_slides_and_zip(self):
        import io
        import zipfile
        from PIL import Image

        # Unauthenticated redirects to /staff login
        self.assertEqual(self.client.get('/staff/sns-kit').status_code, 302)
        self.assertEqual(self.client.get('/staff/sns-kit/slide/1.png').status_code, 302)
        self.assertEqual(self.client.get('/staff/sns-kit/download.zip').status_code, 302)

        with self.client.session_transaction() as s:
            s['is_staff'] = True
        try:
            kit_resp = self.client.get('/staff/sns-kit')
            self.assertEqual(kit_resp.status_code, 200)
            self.assertIn('週末狙い目予報', kit_resp.data.decode('utf-8'))

            for idx in range(1, 6):
                s_resp = self.client.get(f'/staff/sns-kit/slide/{idx}.png')
                self.assertEqual(s_resp.status_code, 200)
                self.assertEqual(s_resp.mimetype, 'image/png')
                with Image.open(io.BytesIO(s_resp.data)) as im:
                    self.assertEqual(im.size, (1080, 1350))

            z_resp = self.client.get('/staff/sns-kit/download.zip')
            self.assertEqual(z_resp.status_code, 200)
            with zipfile.ZipFile(io.BytesIO(z_resp.data), 'r') as zf:
                names = set(zf.namelist())
                self.assertEqual(len(names), 6)  # 5 PNGs + captions.txt
                self.assertIn('captions.txt', names)
                cap_txt = zf.read('captions.txt').decode('utf-8')
                self.assertIn('#相席ナビ', cap_txt)
        finally:
            with self.client.session_transaction() as s:
                s.pop('is_staff', None)

    def test_min_headcount_threshold_official_notice_and_calm_theme_unification(self):
        import subprocess
        import price_comparison

        self.assertEqual(price_comparison.MIN_WOMEN_FOR_RATIO_EVAL, 10)
        self.assertEqual(price_comparison.MIN_WOMEN_FOR_TOP_PICK, 14)

        # Case 1: Sub-10 store with 100% female ratio (8W / 0M) vs solid-headcount store (16W / 12M = 57%)
        live_mix = [
            {'name': 'OLG 新宿', 'women': 8, 'men': 0, 'total': 8},
            {'name': 'OLG ag新宿', 'women': 9, 'men': 2, 'total': 11},
            {'name': 'JIS SHINJUKU', 'women': 16, 'men': 12, 'total': 28},
            {'name': 'JIS NISHISHINJUKU', 'women': 0, 'men': 0, 'total': 0},
        ]
        res = price_comparison.build_area_comparison_clusters(live_mix)
        shinjuku = next(c for c in res['clusters'] if c['id'] == 'shinjuku')
        self.assertIsNotNone(shinjuku['best_store'])
        self.assertEqual(shinjuku['best_store']['name'], 'JIS SHINJUKU')

        olg_prof = next(s for s in shinjuku['stores'] if s['name'] == 'OLG 新宿')
        self.assertFalse(olg_prof['meets_min_headcount'])
        self.assertFalse(olg_prof['is_best_in_cluster'])
        self.assertIn('少人数', olg_prof['verdict_badge'])
        self.assertEqual(olg_prof['official_url'], 'https://oriental-lounge.com/')

        # Case 2: All stores below minimum headcount (<10 women, e.g. 9W & 7W) -> best_store must be held back (None)
        low_only = [
            {'name': 'OLG 新宿', 'women': 9, 'men': 0, 'total': 9},
            {'name': 'OLG ag新宿', 'women': 7, 'men': 3, 'total': 10},
        ]
        res_low = price_comparison.build_area_comparison_clusters(low_only)
        shinjuku_low = next(c for c in res_low['clusters'] if c['id'] == 'shinjuku')
        self.assertIsNone(shinjuku_low['best_store'])
        self.assertTrue(all(not s['is_best_in_cluster'] for s in shinjuku_low['stores']))

        # Official website verification disclaimer & calm.css?v=7 across all public pages and columns
        public_paths = [
            '/',
            '/price-comparison',
            '/store/oriental-lounge-shinjuku',
            '/areas',
            '/area/shinjuku',
            '/oriental-lounge',
            '/ag',
            '/jis',
            '/columns',
            '/grooming',
            '/stats',
            '/privacy',
            '/guide',
            '/guide/brand-comparison',
            '/guide/shinjuku',
            '/guide/weekday-strategy',
            '/guide/weekend-strategy',
            '/guide/solo-vs-pair',
            '/guide/second-venue',
            '/guide/first-10-minutes',
            '/guide/club-area-midnight',
            '/guide/beauty-area-stage',
            '/guide/male-lead-mechanics',
            '/guide/price-event-comparison',
        ]
        for p in public_paths:
            r = self.client.get(p)
            self.assertEqual(r.status_code, 200, f"Public path {p} returned {r.status_code}")
            body = r.data.decode('utf-8')
            self.assertIn('/static/calm.css?v=9', body, f"{p} missing /static/calm.css?v=9")
            self.assertIn('公式ホームページ', body, f"{p} missing official website verification notice")

        # Also check scheduled preview templates via staff session
        with self.client.session_transaction() as s:
            s['is_staff'] = True
        try:
            for sp in ('/guide/local-vs-urban', '/guide/cost-break-even', '/guide/rain-payday'):
                sr = self.client.get(sp)
                self.assertEqual(sr.status_code, 200, f"Scheduled path {sp} returned {sr.status_code}")
                sbody = sr.data.decode('utf-8')
                self.assertIn('/static/calm.css?v=9', sbody, f"{sp} missing /static/calm.css?v=9")
                self.assertIn('公式ホームページ', sbody, f"{sp} missing official website verification notice")
        finally:
            with self.client.session_transaction() as s:
                s.pop('is_staff', None)

        # Syntax check static/app.js and static/sw.js (wrapped in function for macOS jsc -s syntax-only evaluation)
        node_bin = shutil.which('node')
        jsc_bin = '/System/Library/Frameworks/JavaScriptCore.framework/Versions/Current/Helpers/jsc'
        js_checker = ([node_bin, '--check'] if node_bin else ([jsc_bin, '-s'] if os.path.exists(jsc_bin) else None))
        if js_checker:
            repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
            for idx_js, rel_js in enumerate(('static/app.js', 'static/sw.js')):
                with open(os.path.join(repo_dir, rel_js), 'r', encoding='utf-8') as rf:
                    src_js = rf.read()
                tmp_target = os.path.join(_TMP, f'static_check_{idx_js}.js')
                with open(tmp_target, 'w', encoding='utf-8') as wf:
                    wf.write('function __syntax_check__() {\n' + src_js + '\n}\n')
                proc = subprocess.run(js_checker + [tmp_target], capture_output=True, text=True)
                self.assertEqual(proc.returncode, 0, f"JS syntax error in {rel_js}: {proc.stderr}{proc.stdout}")

    def test_official_business_hours_and_open_zero_zero_display(self):
        from unittest.mock import patch
        import store_master
        import store_hours
        import price_comparison

        # 1. Every store in store_master must have an explicit official business hours spec
        master = store_master.load()
        for sname in master.keys():
            self.assertIn(sname, store_hours.STORE_BUSINESS_HOURS, f"{sname} missing from STORE_BUSINESS_HOURS")

        jst = datetime.timezone(datetime.timedelta(hours=9))

        # 2. Early opening stores at 17:30 JST Wednesday (2026-10-28)
        dt_wed_1730 = datetime.datetime(2026, 10, 28, 17, 30, tzinfo=jst)
        self.assertTrue(store_hours.get_business_hours_status('OLG ag梅田', now_jst=dt_wed_1730, total_guests=0)['is_open_now'])
        self.assertTrue(store_hours.get_business_hours_status('OLG 名古屋 栄', now_jst=dt_wed_1730, total_guests=0)['is_open_now'])
        self.assertFalse(store_hours.get_business_hours_status('OLG 梅田', now_jst=dt_wed_1730, total_guests=0)['is_open_now'])
        self.assertFalse(store_hours.get_business_hours_status('OLG 新宿', now_jst=dt_wed_1730, total_guests=0)['is_open_now'])

        # 3. Regular closed day vs pre-holiday override for OLG 宇都宮 (Wednesday closed)
        dt_wed_2100 = datetime.datetime(2026, 10, 28, 21, 0, tzinfo=jst)
        u_closed = store_hours.get_business_hours_status('OLG 宇都宮', now_jst=dt_wed_2100, total_guests=0)
        self.assertFalse(u_closed['is_open_now'])
        self.assertEqual(u_closed['today_hours_label'], '定休日')
        # 2025-03-19 (Wed) is the day before 2025-03-20 (Thu, Vernal Equinox Day = JP holiday) -> open!
        dt_prehol_wed = datetime.datetime(2025, 3, 19, 21, 0, tzinfo=jst)
        u_prehol = store_hours.get_business_hours_status('OLG 宇都宮', now_jst=dt_prehol_wed, total_guests=0)
        self.assertTrue(u_prehol['is_open_now'])

        # 4. Late-night Monday 03:30 AM JST (belongs to Sunday night's shift: 27:30)
        dt_mon_0330 = datetime.datetime(2026, 10, 26, 3, 30, tzinfo=jst)
        # OLG 新宿 is open until 05:00 -> open `0 : 0`
        p_shinjuku_open = price_comparison.get_store_price_profile(
            'OLG 新宿', live_store={'name': 'OLG 新宿', 'women': 0, 'men': 0}, now_jst=dt_mon_0330
        )
        self.assertTrue(p_shinjuku_open['is_open_now'])
        self.assertIn('0 : 0', p_shinjuku_open['verdict_badge'])
        self.assertNotIn('営業時間外', p_shinjuku_open['verdict_badge'])

        # OLG 錦糸町 closes at 02:00 on Sun-Thu, JIS NISHISHINJUKU closes at 01:00 on Sun -> off-hours!
        p_kinshicho_closed = price_comparison.get_store_price_profile(
            'OLG 錦糸町', live_store={'name': 'OLG 錦糸町', 'women': 0, 'men': 0}, now_jst=dt_mon_0330
        )
        self.assertFalse(p_kinshicho_closed['is_open_now'])
        self.assertEqual(p_kinshicho_closed['verdict_badge'], '営業時間外')

        p_nishishinjuku_closed = price_comparison.get_store_price_profile(
            'JIS NISHISHINJUKU', live_store={'name': 'JIS NISHISHINJUKU', 'women': 0, 'men': 0}, now_jst=dt_mon_0330
        )
        self.assertFalse(p_nishishinjuku_closed['is_open_now'])
        self.assertEqual(p_nishishinjuku_closed['verdict_badge'], '営業時間外')

        # 5. SSR HTML rendering check for `0 : 0` during open hours vs `営業時間外` during daytime
        orig_fn = store_hours.get_business_hours_status
        with app_module.data_lock:
            old_full = list(app_module.latest_data.get('full_data') or [])
            app_module.latest_data['full_data'] = [
                {'name': 'OLG 新宿', 'women': 0, 'men': 0, 'region': 'Kanto'},
                {'name': 'JIS SHINJUKU', 'women': 0, 'men': 0, 'region': 'Kanto'},
                {'name': 'JIS NISHISHINJUKU', 'women': 0, 'men': 0, 'region': 'Kanto'},
                {'name': 'OLG ag新宿', 'women': 0, 'men': 0, 'region': 'Kanto'},
            ]
        try:
            # Open hours (Wednesday 21:30 JST) -> all Shinjuku stores are open `0 : 0`
            dt_open = datetime.datetime(2026, 10, 28, 21, 30, tzinfo=jst)
            with patch('store_hours.get_business_hours_status', side_effect=lambda s, now_jst=None, total_guests=0: orig_fn(s, now_jst=dt_open, total_guests=total_guests)):
                pc_open = self.client.get('/price-comparison?area=shinjuku').data.decode('utf-8')
                self.assertIn('0 : 0', pc_open)
                self.assertIn('は営業中です（全店 女性10名未満）', pc_open)

                sd_open = self.client.get('/store/oriental-lounge-shinjuku').data.decode('utf-8')
                self.assertIn('営業中（0 : 0）', sd_open)
                self.assertNotIn('女性0名', sd_open)
                self.assertNotIn('男性0名', sd_open)

            # Daytime closed hours (Wednesday 14:00 JST) -> all Shinjuku stores are off-hours
            dt_closed = datetime.datetime(2026, 10, 28, 14, 0, tzinfo=jst)
            with patch('store_hours.get_business_hours_status', side_effect=lambda s, now_jst=None, total_guests=0: orig_fn(s, now_jst=dt_closed, total_guests=total_guests)):
                pc_closed = self.client.get('/price-comparison?area=shinjuku').data.decode('utf-8')
                self.assertIn('は営業時間外です', pc_closed)

                sd_closed = self.client.get('/store/oriental-lounge-shinjuku').data.decode('utf-8')
                self.assertIn('営業時間外（本日 19:00〜翌5:00）', sd_closed)
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = old_full

    def test_hokkaido_fair_17_stores_and_same_city_event_isolation(self):
        import events
        import price_comparison

        # 1. 【全国17店舗】北海道フェア🌽 must resolve strictly to OLG_17_STORES (17 stores) and NEVER target_stores=[]
        ts_hokkaido, lbl_hokkaido = events.extract_olg_target_stores('【全国17店舗】北海道フェア🌽')
        self.assertEqual(len(ts_hokkaido), 17)
        self.assertEqual(set(ts_hokkaido), set(events.OLG_17_STORES))
        self.assertEqual(lbl_hokkaido, '対象17店舗限定')

        # 2. Legacy/cached event with empty target_stores for 【全国17店舗】北海道フェア🌽 must only match participating 17 stores
        mock_pool = [
            {
                'id': 'olg-hokkaido-fair',
                'brand': 'OLG',
                'title': '【全国17店舗】北海道フェア🌽',
                'url': 'https://oriental-lounge.com/announcements/494',
                'tag': 'フェア・企画',
                'target_stores': [],
                'target_label': '全店共通',
            },
            {
                'id': 'olg-shinjuku-only',
                'brand': 'OLG',
                'title': '【新宿】定額割開催中！',
                'url': 'https://oriental-lounge.com/announcements/470',
                'tag': '割引・特典',
                'target_stores': events.extract_olg_target_stores('【新宿】定額割開催中！')[0],
                'target_label': '新宿店限定',
            },
            {
                'id': 'olg-sakae-only',
                'brand': 'OLG',
                'title': '【名古屋栄】ハッピーアワー半額',
                'url': 'https://oriental-lounge.com/announcements/471',
                'tag': '割引・特典',
                'target_stores': events.extract_olg_target_stores('【名古屋栄】ハッピーアワー半額')[0],
                'target_label': '名古屋栄店限定',
            },
            {
                'id': 'olg-shibuya-multi',
                'brand': 'OLG',
                'title': '【恵比寿・渋谷本店・ag渋谷】200ギフトプレゼント🎁',
                'url': 'https://oriental-lounge.com/announcements/475',
                'tag': '割引・特典',
                'target_stores': events.extract_olg_target_stores('【恵比寿・渋谷本店・ag渋谷】200ギフトプレゼント🎁')[0],
                'target_label': '恵比寿・渋谷本店・ag渋谷店限定',
            },
        ]

        # Participating stores in 17-store Hokkaido fair (e.g. OLG 新宿, OLG 難波, OLG ag仙台, OLG 福岡) MUST receive it
        for participating in ('OLG 新宿', 'OLG 渋谷本店', 'OLG ag渋谷', 'OLG 難波', 'OLG ag仙台', 'OLG 福岡', 'OLG 小倉'):
            evs = events.get_events_for_store(participating, store_only=True, allow_blocking=False, preloaded_events=mock_pool)
            titles = [e['title'] for e in evs]
            self.assertIn('【全国17店舗】北海道フェア🌽', titles, f"{participating} should have Hokkaido Fair")

        # Non-participating stores (e.g. OLG 梅田, OLG 心斎橋, OLG 名古屋 栄, OLG 名古屋 錦, OLG 広島, OLG 仙台, OLG 札幌, OLG ag新宿, OLG 渋谷駅前, OLG ag福岡) MUST NOT receive it
        for non_participating in (
            'OLG 梅田', 'OLG 茶屋町', 'OLG 大阪駅前', 'OLG 心斎橋', 'OLG 京都', 'OLG 神戸',
            'OLG 名古屋 栄', 'OLG 名古屋 錦', 'OLG ag名古屋', 'OLG 広島', 'OLG ag広島',
            'OLG 岡山', 'OLG 札幌', 'OLG ag札幌', 'OLG 仙台', 'OLG 金沢', 'OLG ag金沢',
            'OLG 静岡', 'OLG 浜松', 'OLG 沖縄', 'OLG ag沖縄', 'OLG ag新宿', 'OLG 渋谷駅前', 'OLG ag福岡'
        ):
            evs = events.get_events_for_store(non_participating, store_only=True, allow_blocking=False, preloaded_events=mock_pool)
            titles = [e['title'] for e in evs]
            self.assertNotIn('【全国17店舗】北海道フェア🌽', titles, f"{non_participating} MUST NOT have Hokkaido Fair")

        # 3. Same-city isolation checks:
        # - 【新宿】定額割 matches OLG 新宿, NOT OLG ag新宿 or JIS SHINJUKU
        shinjuku_evs = [e['title'] for e in events.get_events_for_store('OLG 新宿', store_only=True, allow_blocking=False, preloaded_events=mock_pool)]
        ag_shinjuku_evs = [e['title'] for e in events.get_events_for_store('OLG ag新宿', store_only=True, allow_blocking=False, preloaded_events=mock_pool)]
        self.assertIn('【新宿】定額割開催中！', shinjuku_evs)
        self.assertNotIn('【新宿】定額割開催中！', ag_shinjuku_evs)

        # - 【名古屋栄】 matches OLG 名古屋 栄, NOT OLG 名古屋 錦 or OLG ag名古屋
        sakae_evs = [e['title'] for e in events.get_events_for_store('OLG 名古屋 栄', store_only=True, allow_blocking=False, preloaded_events=mock_pool)]
        nishiki_evs = [e['title'] for e in events.get_events_for_store('OLG 名古屋 錦', store_only=True, allow_blocking=False, preloaded_events=mock_pool)]
        ag_nagoya_evs = [e['title'] for e in events.get_events_for_store('OLG ag名古屋', store_only=True, allow_blocking=False, preloaded_events=mock_pool)]
        self.assertIn('【名古屋栄】ハッピーアワー半額', sakae_evs)
        self.assertNotIn('【名古屋栄】ハッピーアワー半額', nishiki_evs)
        self.assertNotIn('【名古屋栄】ハッピーアワー半額', ag_nagoya_evs)

        # - 【恵比寿・渋谷本店・ag渋谷】 matches OLG 恵比寿, OLG 渋谷本店, OLG ag渋谷, NOT OLG 渋谷駅前
        shibuya_honten_evs = [e['title'] for e in events.get_events_for_store('OLG 渋谷本店', store_only=True, allow_blocking=False, preloaded_events=mock_pool)]
        ag_shibuya_evs = [e['title'] for e in events.get_events_for_store('OLG ag渋谷', store_only=True, allow_blocking=False, preloaded_events=mock_pool)]
        shibuya_ekimae_evs = [e['title'] for e in events.get_events_for_store('OLG 渋谷駅前', store_only=True, allow_blocking=False, preloaded_events=mock_pool)]
        self.assertIn('【恵比寿・渋谷本店・ag渋谷】200ギフトプレゼント🎁', shibuya_honten_evs)
        self.assertIn('【恵比寿・渋谷本店・ag渋谷】200ギフトプレゼント🎁', ag_shibuya_evs)
        self.assertNotIn('【恵比寿・渋谷本店・ag渋谷】200ギフトプレゼント🎁', shibuya_ekimae_evs)

        # 4. Price comparison profile check: non-participating store (OLG 梅田) must not display Hokkaido Fair
        prof_umeda = price_comparison.get_store_price_profile('OLG 梅田', all_events_Pool=mock_pool)
        all_umeda_ev_titles = [x['full_title'] for x in prof_umeda['men_perks'] + prof_umeda['women_events']]
        self.assertNotIn('【全国17店舗】北海道フェア🌽', all_umeda_ev_titles)

        # 5. Guide page & SNS announcement API check
        guide_resp = self.client.get('/guide/price-event-comparison')
        self.assertEqual(guide_resp.status_code, 200)
        self.assertIn('比較ボードの活用ガイド', guide_resp.data.decode('utf-8'))

        sns_resp = self.client.get('/api/sns-post?type=price')
        self.assertEqual(sns_resp.status_code, 200)
        sns_json = sns_resp.get_json()
        self.assertEqual(sns_json['status'], 'success')
        self.assertIn('/price-comparison', sns_json['text'])
        self.assertIn('/guide/price-event-comparison', sns_json['text'])

        # 6. Top page & portal nav order + cleanup check
        idx_html = self.client.get('/').data.decode('utf-8')
        self.assertNotIn('公式URL変更', idx_html)
        self.assertNotIn('3大ブランドの違い・平日の穴場時間をデータでまとめました', idx_html)
        self.assertNotIn('毎晩の「入りやすい穴場店舗」をX・Threadsで更新中', idx_html)
        self.assertIn('公式SNSも更新中', idx_html)

        nav_start = idx_html.find('<nav class="portal-global-nav"')
        nav_end = idx_html.find('</nav>', nav_start)
        nav_block = idx_html[nav_start:nav_end]
        p_rank = nav_block.find('リアルタイム順位')
        p_price = nav_block.find('料金・イベント比較')
        p_col = nav_block.find('コラム・記事一覧')
        self.assertTrue(0 <= p_rank < p_price < p_col, "Top nav order must be リアルタイム順位 -> 料金・イベント比較 -> コラム・記事一覧")

        # Verify ranking-list appears BEFORE momentum-legend-bar and official-sns-compact (First-view optimization)
        pos_ranking = idx_html.find('id="ranking-list"')
        pos_legend = idx_html.find('class="momentum-legend-bar"')
        pos_sns = idx_html.find('official-sns-compact')
        self.assertTrue(0 < pos_ranking < pos_legend < pos_sns, "ranking-list must appear before legend and SNS bar")

        # 7. Step 2 Affiliate /link/ internal redirect & column 1-to-1 matching verification
        repo_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(repo_dir, 'static/affiliate_config.js'), 'r', encoding='utf-8') as af:
            aff_src = af.read()
        self.assertIn("url: '/link/mens_hair_removal'", aff_src)
        self.assertIn("url: '/link/mens_skincare_clinic'", aff_src)
        self.assertIn("url: '/link/dating_instant'", aff_src)
        self.assertIn("url: '/link/spu_styleup'", aff_src)
        for item_key in ('mens_hair_removal', 'mens_skincare_clinic', 'dating_instant', 'spu_styleup'):
            redir_resp = self.client.get(f'/link/{item_key}', follow_redirects=False)
            self.assertEqual(redir_resp.status_code, 302)



if __name__ == '__main__':
    unittest.main()
