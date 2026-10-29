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
os.environ['LATEST_STATUS_SNAPSHOT_FILE'] = os.path.join(_TMP, 'latest_status_cache.json')
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
        # Regular staff (is_staff=True, is_master=False) cannot access owner analytics
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = False
        r_staff_only = self.client.get('/staff/analytics')
        self.assertEqual(r_staff_only.status_code, 302)

        # Master (is_master=True) can access owner analytics
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = True
        r = self.client.get('/staff/analytics')
        self.assertEqual(r.status_code, 200)
        self.assertIn('アクセス集計'.encode('utf-8'), r.data)
        with self.client.session_transaction() as s:
            s.pop('is_staff', None)
            s.pop('is_master', None)

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

    def test_note_subsequent_run_skips_old_articles_and_enforces_daily_and_title_dedup(self):
        import note_poster
        from email.utils import format_datetime
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        fresh_pub = format_datetime(now_utc - datetime.timedelta(minutes=30))
        old_pub = format_datetime(now_utc - datetime.timedelta(days=5))

        # Even when posted_urls already has 1 entry, a 5-day-old unrecorded article must NOT be posted
        job_state.mark_done('note:https://note.com/aiseki_navi/n/n_already_known')
        old_unrecorded = {
            'title': '【平日が一番勝てる】なぜ相席ラウンジは週末より「水曜・木曜20時〜22時」が最強の穴場なのか？',
            'link': 'https://note.com/aiseki_navi/n/n_old_unrecorded',
            'pub_date': old_pub,
            'guid': 'n_old_unrecorded',
        }
        revised_same_topic = {
            'title': '【実は一番勝てる】なぜ相席ラウンジは週末より「木曜20時〜22時」が最強の穴場なのか？',
            'link': 'https://note.com/aiseki_navi/n/n_revised_same_topic',
            'pub_date': fresh_pub,
            'guid': 'n_revised_same_topic',
        }
        brand_new_topic_1 = {
            'title': '【新検証】23時20分の終電崖と深夜0時半の第2波データ',
            'link': 'https://note.com/aiseki_navi/n/n_brand_new_1',
            'pub_date': fresh_pub,
            'guid': 'n_brand_new_1',
        }
        brand_new_topic_2 = {
            'title': '【別テーマ】雨の日と給料日前の男女比データ検証',
            'link': 'https://note.com/aiseki_navi/n/n_brand_new_2',
            'pub_date': fresh_pub,
            'guid': 'n_brand_new_2',
        }

        state_file = os.path.join(_TMP, 'notes_dedup.json')
        # 1) Old unrecorded article is silently marked seen without posting; revised article with similar core title is also skipped
        job_state.mark_done(f"note_title:{note_poster._normalize_title_signature('なぜ相席ラウンジは週末より「木曜20時〜22時」が最強の穴場なのか？')}")
        with mock.patch.object(note_poster, 'STATE_FILE', state_file), \
             mock.patch('note_poster.fetch_latest_note_articles', return_value=[revised_same_topic, old_unrecorded]), \
             mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as post_mock:
            r_skip = note_poster.check_and_post_new_note()
        self.assertEqual(r_skip['status'], 'skipped')
        self.assertEqual(post_mock.call_count, 0)
        self.assertTrue(job_state.is_done('note:https://note.com/aiseki_navi/n/n_old_unrecorded'))
        self.assertTrue(job_state.is_done('note:https://note.com/aiseki_navi/n/n_revised_same_topic'))

        # 2) Two brand-new topics published on the same day -> 1st posts, 2nd is deferred by daily cap (max 1/day)
        with mock.patch.object(note_poster, 'STATE_FILE', state_file), \
             mock.patch('note_poster.fetch_latest_note_articles', return_value=[brand_new_topic_2, brand_new_topic_1]), \
             mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as post_mock2:
            r_first = note_poster.check_and_post_new_note()
            r_second = note_poster.check_and_post_new_note()
        self.assertEqual(r_first['status'], 'success')
        self.assertEqual(r_second['status'], 'skipped')
        self.assertIn('today', r_second.get('reason', ''))
        self.assertEqual(post_mock2.call_count, 1)


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
            '/guide/last-train-cliff',
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
        self.assertIn('狙い目 ', html)
        self.assertIn('終電後 ', html)
        self.assertIn('平比', html)

        r_status = client.get('/api/status')
        self.assertEqual(r_status.status_code, 200)
        st_json = r_status.get_json()
        ebisu_item = next((x for x in st_json['ranking'] if x['name'] == 'OLG 恵比寿'), None)
        self.assertIsNotNone(ebisu_item)
        self.assertIn('core_time_short', ebisu_item)
        self.assertIn('midnight_retention_pct', ebisu_item)
        self.assertIn('usual_w_diff', ebisu_item)
        self.assertIn('usual_m_diff', ebisu_item)

        r_detail = client.get('/store/oriental-lounge-ebisu')
        self.assertEqual(r_detail.status_code, 200)
        detail_html = r_detail.data.decode('utf-8')
        self.assertIn('終電後の残りやすさ指標', detail_html)
        self.assertIn('終電後維持率', detail_html)


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

    def test_sw_js_syntax(self):
        import subprocess
        js_path = os.path.join(ROOT, 'static', 'sw.js')
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

            # Logged-in staff/master can preview all 3 upcoming articles with noindex protection
            with self.client.session_transaction() as s:
                s['is_staff'] = True
                s['is_master'] = True
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
                s.pop('is_master', None)

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
            self.assertIn('/guide/local-vs-urban', tips_poster.get_tip_reply_text(tip_index=58, now_jst=all_dt))
            self.assertIn('/guide/cost-break-even', tips_poster.get_tip_reply_text(tip_index=59, now_jst=all_dt))
            self.assertIn('/guide/rain-payday', tips_poster.get_tip_reply_text(tip_index=60, now_jst=all_dt))

    def test_scheduled_tweets_and_all_tips_within_280_weight(self):
        import scheduled_articles
        import x_poster
        import tips_poster
        for item in scheduled_articles.SCHEDULED_ARTICLES:
            w = x_poster.compute_twitter_weight(item['x_main_text'])
            self.assertLessEqual(w, 280, f"{item['slug']} weight={w}")
        for item in x_poster.COLUMN_PROMO_LIST:
            w = x_poster.compute_twitter_weight(item['main_text'])
            self.assertLessEqual(w, 280, f"promo[{item['slug']}] weight={w}")
        for i in range(len(tips_poster.TIPS_LIST)):
            tip_text = tips_poster.get_tip_for_slot(tip_index=i)
            w = x_poster.compute_twitter_weight(tip_text)
            self.assertLessEqual(w, 280, f"tip[{i}] weight={w}")

    def test_all_columns_have_human_dialogue_and_proper_brand_naming(self):
        import re
        import scheduled_articles
        all_dt = datetime.datetime(2026, 11, 10, 12, 0, 0)
        column_paths = [
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
            '/guide/last-train-cliff',
            '/guide/local-vs-urban',
            '/guide/cost-break-even',
            '/guide/rain-payday',
            '/grooming',
        ]
        with mock.patch.object(scheduled_articles, 'now_jst_naive', return_value=all_dt):
            for p in column_paths:
                r = self.client.get(p)
                self.assertEqual(r.status_code, 200, p)
                html = r.data.decode('utf-8')
                self.assertIn('dialogue-box', html, f"{p} missing human dialogue-box")
                bare_oriental = re.findall(r'オリエンタル(?!ラウンジ)', html)
                self.assertEqual(bare_oriental, [], f"{p} still contains bare オリエンタル: {bare_oriental}")


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
            self.assertIn('no-cache', resp.headers.get('Cache-Control', ''))
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

            # Verify store detail page also embeds the nearby price & event comparison table + live sync JS + no-cache ETag header
            s_resp = self.client.get('/store/oriental-lounge-shinjuku')
            self.assertEqual(s_resp.status_code, 200)
            self.assertIn('no-cache', s_resp.headers.get('Cache-Control', ''))
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
                self.assertIn("aiseki-navi-v47", sw_txt)
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
            self.assertIn('QUOカード', clovers_html)

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
            s['is_master'] = True
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
                s.pop('is_master', None)

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
            '/for-women',
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
            '/guide/last-train-cliff',
        ]
        for p in public_paths:
            r = self.client.get(p)
            self.assertEqual(r.status_code, 200, f"Public path {p} returned {r.status_code}")
            body = r.data.decode('utf-8')
            self.assertIn('/static/calm.css?v=19', body, f"{p} missing /static/calm.css?v=19")
            if '/static/style.css' in body:
                self.assertIn('/static/style.css?v=20261029_2', body, f"{p} missing /static/style.css?v=20261029_2")
            self.assertIn('公式ホームページ', body, f"{p} missing official website verification notice")

        # Also check scheduled preview templates via staff session
        with self.client.session_transaction() as s:
            s['is_staff'] = True
        try:
            for sp in ('/guide/local-vs-urban', '/guide/cost-break-even', '/guide/rain-payday'):
                sr = self.client.get(sp)
                self.assertEqual(sr.status_code, 200, f"Scheduled path {sp} returned {sr.status_code}")
                sbody = sr.data.decode('utf-8')
                self.assertIn('/static/calm.css?v=19', sbody, f"{sp} missing /static/calm.css?v=19")
                if '/static/style.css' in sbody:
                    self.assertIn('/static/style.css?v=20261029_2', sbody, f"{sp} missing /static/style.css?v=20261029_2")
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

        # 8. Step 3 Automated Price & Business Hours Drift Audit verification
        import data_audit
        jst = datetime.timezone(datetime.timedelta(hours=9))
        # Baseline clean audit
        clean_rep = data_audit.run_data_audit(live_stores=[], all_events=[], now_jst=datetime.datetime(2026, 10, 28, 21, 0, tzinfo=jst))
        self.assertEqual(clean_rep['status'], 'ok')
        self.assertEqual(clean_rep['summary']['issue_count'], 0)
        self.assertEqual(clean_rep['summary']['hours_registered'], 53)
        self.assertEqual(clean_rep['summary']['price_registered'], 51)

        # Simulate off-hours guest drift (14:00 Wednesday when OLG 新宿 is closed, but 6 guests detected)
        drift_rep = data_audit.run_data_audit(
            live_stores=[{'name': 'OLG 新宿', 'women': 4, 'men': 2}, {'name': 'OLG 錦糸町', 'women': 2, 'men': 2}],
            all_events=[
                {'brand': 'OLG', 'title': '【重要】新宿店 営業時間変更のお知らせ', 'url': 'https://oriental-lounge.com/'},
                {'brand': 'OLG', 'title': 'フードメニューをリニューアル！🍽️', 'url': 'https://oriental-lounge.com/'},
                {'brand': 'JIS', 'title': '福岡店リニューアルオープンのお知らせ', 'url': 'https://jis.bar/'},
            ],
            now_jst=datetime.datetime(2026, 10, 28, 14, 0, tzinfo=jst),
        )
        self.assertEqual(drift_rep['status'], 'warning')
        self.assertEqual(drift_rep['summary']['hours_drift_count'], 1)
        self.assertEqual(drift_rep['summary']['revision_notice_count'], 1)

        # Verify YATAKOI UMEDA zeroes out stale Firebase ghost counts after official closing time (03:30 AM Tue)
        import monitor
        with mock.patch('monitor.requests.get') as m_yk_get:
            m_yk_get.return_value = mock.Mock(status_code=200, json=lambda: {'males': 3, 'females': 3, 'ykMales': 6, 'ykFemales': 6})
            m_yk_get.return_value.raise_for_status = lambda: None
            closed_dt = datetime.datetime(2026, 10, 27, 3, 30, tzinfo=jst)
            open_dt = datetime.datetime(2026, 10, 26, 22, 30, tzinfo=jst)
            res_closed = monitor.get_yatakoi_data(now_jst=closed_dt)
            res_open = monitor.get_yatakoi_data(now_jst=open_dt)
            self.assertEqual(res_closed[0]['women'], 0)
            self.assertEqual(res_closed[0]['men'], 0)
            self.assertEqual(res_open[0]['women'], 9)
            self.assertEqual(res_open[0]['men'], 9)

        # Staff (5555) sees field data only; Master (2918) sees full management & audit panel
        self.assertEqual(self.client.get('/api/data-audit').status_code, 401)

        # 1) Regular Staff login with passcode 5555
        r_staff_login = self.client.post('/staff', data={'passcode': '5555'}, follow_redirects=True)
        self.assertEqual(r_staff_login.status_code, 200)
        staff_only_html = r_staff_login.data.decode('utf-8')
        self.assertIn('現場データ専用 モニタリングパネル', staff_only_html)
        self.assertIn('週間レポート（全店舗の集計・ピークランキング）', staff_only_html)
        self.assertNotIn('料金・営業時間データ自動監査', staff_only_html)
        self.assertNotIn('/staff/analytics', staff_only_html)
        self.assertNotIn('/staff/sns-kit', staff_only_html)
        self.assertEqual(self.client.get('/staff/analytics').status_code, 302)
        self.assertEqual(self.client.get('/staff/sns-kit').status_code, 302)

        # 2) Master login with passcode 2918
        r_master_login = self.client.post('/staff', data={'passcode': '2918'}, follow_redirects=True)
        self.assertEqual(r_master_login.status_code, 200)
        master_html = r_master_login.data.decode('utf-8')
        try:
            self.assertIn('MASTER MONITOR', master_html)
            self.assertIn('料金・営業時間データ自動監査', master_html)
            self.assertIn('/staff/analytics', master_html)
            self.assertIn('/staff/sns-kit', master_html)
            api_rep = self.client.get('/api/data-audit')
            self.assertEqual(api_rep.status_code, 200)
            self.assertIn('summary', api_rep.get_json())
        finally:
            self.client.get('/staff/logout')

        # 9. Step 4 CSS Consolidation & Syntax Balance verification
        with open(os.path.join(repo_dir, 'static/style.css'), 'r', encoding='utf-8') as sf:
            style_src = sf.read()
        self.assertEqual(style_src.count('{'), style_src.count('}'), "static/style.css must have balanced braces")
        self.assertIn('--bg-color: #0f1014;', style_src)
        self.assertNotIn('.domain-notice-banner', style_src)
        self.assertNotIn('.column-banner-wrapper', style_src)
        self.assertNotIn('.top-sns-quickbar', style_src)
        with open(os.path.join(repo_dir, 'static/calm.css'), 'r', encoding='utf-8') as cf:
            calm_src = cf.read()
        self.assertEqual(calm_src.count('{'), calm_src.count('}'), "static/calm.css must have balanced braces")

        # 10. Step 5 Backend Modularization verification (portal_guides, store_analysis, sns_workflows)
        import portal_guides
        import store_analysis
        import sns_workflows
        import threads_poster
        import x_poster
        self.assertEqual(portal_guides.AREA_GUIDES, app_module.AREA_GUIDES)
        self.assertIn('oriental-lounge', portal_guides.BRAND_GUIDES)
        sns_sample = sns_workflows.build_sns_post_payload('price', 'https://aiseki-navi.com', {'full_data': [{'name': 'OLG 新宿', 'women': 20, 'men': 10, 'region': 'Kanto'}], 'last_updated': '2026/10/26 21:00'})
        self.assertEqual(sns_sample['status'], 'success')
        self.assertIn('/price-comparison', sns_sample['text'])
        self.assertTrue(callable(store_analysis.compute_store_analysis_payload))

        # 11. Step A/B/C verification: East-West X summary, City quick filters, Multi-ASP dating links, and Threads long-form poster
        sample_stores = [
            {'name': 'OLG 新宿', 'women': 42, 'men': 28, 'region': 'Kanto', 'source': 'ORIENTAL'},
            {'name': 'JIS UMEDA', 'women': 38, 'men': 25, 'region': 'Kinki', 'source': 'JIS'},
            {'name': 'OLG 名古屋 栄', 'women': 24, 'men': 16, 'region': 'Chubu', 'source': 'ORIENTAL'},
            {'name': 'JIS FUKUOKA', 'women': 30, 'men': 20, 'region': 'Kyushu', 'source': 'JIS'},
        ]
        ew_line = x_poster._build_east_west_summary_line(sample_stores)
        self.assertIn('関東1位', ew_line)
        self.assertIn('関西1位', ew_line)
        self.assertIn('price-event-comparison', [c.get('slug') for c in x_poster.COLUMN_PROMO_LIST])

        # City quick filter buttons & dating_bachelor redirect
        self.assertIn('data-region="City:shinjuku"', idx_html)
        self.assertIn('data-region="City:umeda"', idx_html)
        self.assertIn("url: '/link/dating_bachelor'", aff_src)
        self.assertEqual(self.client.get('/link/dating_bachelor', follow_redirects=False).status_code, 302)

        # Threads live post builder & API endpoint
        th_text = threads_poster.build_threads_live_post(sample_stores, slot_label='22:00')
        self.assertIn('全国リアルタイムTOP3', th_text)
        self.assertIn('主要街の現在トップ', th_text)
        self.assertLessEqual(len(th_text), 500)
        th_payload = sns_workflows.build_sns_post_payload('threads', 'https://aiseki-navi.com', {'full_data': sample_stores, 'last_updated': '2026/10/26 22:00'})
        self.assertEqual(th_payload['status'], 'success')
        self.assertIn('全国リアルタイムTOP3', th_payload['text'])
        self.assertEqual(self.client.get('/api/trigger-threads-post').status_code, 401)

        # Instagram Graph API poster & public slide endpoint verification
        import instagram_poster
        import ig_carousel_generator
        self.assertEqual(self.client.get('/api/trigger-ig-post').status_code, 401)
        ig_slide_resp = self.client.get('/ogp/ig-slide/1.jpg')
        try:
            self.assertEqual(ig_slide_resp.status_code, 200)
            self.assertEqual(ig_slide_resp.mimetype, 'image/jpeg')
        finally:
            ig_slide_resp.close()
        self.assertEqual(instagram_poster.post_single_image_to_instagram('https://aiseki-navi.com/ogp/ig-slide/1.jpg', 'test')['status'], 'skipped')

        # Token persistence in job_state + auto-refresh & weekday/weekend/sunday IG themes
        job_state.set_value('token:test_dummy', 'tok_abc_123')
        self.assertEqual(job_state.get_value('token:test_dummy'), 'tok_abc_123')
        mon_dt = datetime.datetime(2026, 10, 26, 21, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))  # Monday
        fri_dt = datetime.datetime(2026, 10, 30, 21, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))  # Friday
        sun_dt = datetime.datetime(2026, 11, 1, 21, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))   # Sunday
        ctx_mon = ig_carousel_generator.build_forecast_context(live_stores=sample_stores, now_jst=mon_dt)
        ctx_fri = ig_carousel_generator.build_forecast_context(live_stores=sample_stores, now_jst=fri_dt)
        ctx_sun = ig_carousel_generator.build_forecast_context(live_stores=sample_stores, now_jst=sun_dt)
        self.assertEqual(ctx_mon['theme']['theme_key'], 'weekday')
        self.assertEqual(ctx_fri['theme']['theme_key'], 'weekend')
        self.assertEqual(ctx_sun['theme']['theme_key'], 'sunday')
        self.assertIn('平日夜', ig_carousel_generator.build_captions(ctx_mon)['instagram_caption'])
        self.assertIn('週間データ', ig_carousel_generator.build_captions(ctx_sun)['instagram_caption'])
        self.assertGreater(len(ig_carousel_generator.render_slide_png(1, ctx=ctx_mon)), 10000)
        self.assertGreater(len(ig_carousel_generator.render_slide_png(1, ctx=ctx_sun)), 10000)

        # Verify Threads URL-free vs URL rotation across odd/even days (anti-spam)
        th_even = threads_poster.build_threads_live_post(sample_stores, slot_label='22:00', now_jst=mon_dt)  # day=26 (even)
        th_odd = threads_poster.build_threads_live_post(sample_stores, slot_label='22:00', now_jst=sun_dt)   # day=1 (odd)
        self.assertNotIn('https://', th_even)
        self.assertIn('プロフィールのリンク', th_even)
        self.assertIn('https://aiseki-navi.com/price-comparison', th_odd)


class FullAuditRegressionTests(unittest.TestCase):
    """Comprehensive regression tests for the full site & SNS audit fixes."""

    def setUp(self):
        self.client = app_module.app.test_client()
        with app_module._login_failures_lock:
            app_module._login_failures.clear()

    def test_legacy_store_301_redirects_strict_404s_and_chugoku_shikoku_area(self):
        # 1. Retired/merged legacy store slugs must 301 redirect to active successors (never ghost 200 or soft-404)
        for legacy_slug in (
            'ag-shinjuku',
            'oriental-lounge-umeda',
            'oriental-lounge-chayamachi',
            'oriental-lounge-shibuya-ekimae',
            'ag-fukuoka',
            'oriental-lounge-sapporo',
            'ag-sapporo',
            'oriental-lounge-sendai',
            'oriental-lounge-kanazawa',
            'oriental-lounge-hiroshima',
            'oriental-lounge-okinawa',
        ):
            r = self.client.get(f'/store/{legacy_slug}', follow_redirects=False)
            self.assertEqual(r.status_code, 301, f"/store/{legacy_slug} should return 301")

        # 2. Canonical duplicate guide routes must 301 redirect
        r_strat = self.client.get('/guide/strategy', follow_redirects=False)
        self.assertEqual(r_strat.status_code, 301)
        self.assertTrue(r_strat.headers['Location'].endswith('/guide'))

        r_comp = self.client.get('/guide/compare', follow_redirects=False)
        self.assertEqual(r_comp.status_code, 301)
        self.assertTrue(r_comp.headers['Location'].endswith('/guide/brand-comparison'))

        # 3. Unknown store / area slugs must return true HTTP 404
        self.assertEqual(self.client.get('/store/non-existent-store-xyz').status_code, 404)
        self.assertEqual(self.client.get('/area/non-existent-area-xyz').status_code, 404)

        # 4. Dedicated Chugoku/Shikoku area separation from Hokkaido/Tohoku
        r_cs = self.client.get('/area/chugoku-shikoku')
        self.assertEqual(r_cs.status_code, 200)
        cs_html = r_cs.data.decode('utf-8')
        self.assertIn('OLG 岡山', cs_html)
        self.assertIn('OLG ag広島', cs_html)
        self.assertIn('JIS MATSUYAMA', cs_html)

        r_ht = self.client.get('/area/hokkaido-tohoku')
        self.assertEqual(r_ht.status_code, 200)
        ht_html = r_ht.data.decode('utf-8')
        self.assertNotIn('OLG 岡山', ht_html)
        self.assertNotIn('JIS MATSUYAMA', ht_html)

        # 5. Independent brands must not have brand_slug='oriental-lounge' in price_comparison
        import price_comparison
        for indep in ('XIX OKAYAMA', 'YATAKOI UMEDA', 'CLOVERS HIROSHIMA', 'ALFA HIROSHIMA'):
            self.assertEqual(price_comparison.get_store_price_profile(indep)['brand_slug'], '')

    def test_master_vs_staff_authorization_and_login_rate_limit(self):
        protected_master_endpoints = [
            '/api/trigger-x-post',
            '/api/trigger-x-tip',
            '/api/trigger-note-share',
            '/api/trigger-x-pinned',
            '/api/trigger-x-column',
            '/api/trigger-x-weekly',
            '/api/trigger-x-bonus',
            '/api/trigger-threads-post',
            '/api/trigger-ig-post',
            '/api/data-audit',
            '/api/sns-editorial',
            '/api/debug_ps',
            '/api/logs',
            '/api/thread_status',
        ]
        # Regular staff (5555) is blocked from all master-only API endpoints
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = False
        for ep in protected_master_endpoints:
            r = self.client.get(ep)
            self.assertEqual(r.status_code, 401, f"Regular staff must get 401 on {ep}")
        self.assertEqual(self.client.get('/staff/sns-editorial', follow_redirects=False).status_code, 302)

        # Master (2918) can access diagnostic and data-audit endpoints
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = True
        self.assertEqual(self.client.get('/api/data-audit').status_code, 200)
        self.assertEqual(self.client.get('/api/sns-editorial').status_code, 200)
        self.assertEqual(self.client.get('/staff/sns-editorial').status_code, 200)
        self.assertEqual(self.client.get('/api/debug_ps').status_code, 200)
        self.assertEqual(self.client.get('/api/thread_status').status_code, 200)
        with self.client.session_transaction() as s:
            s.pop('is_staff', None)
            s.pop('is_master', None)

        # Brute-force rate limiter blocks after LOGIN_MAX_ATTEMPTS wrong attempts
        for _ in range(app_module.LOGIN_MAX_ATTEMPTS):
            r_bad = self.client.post('/staff', data={'passcode': '0000'})
            self.assertEqual(r_bad.status_code, 200)
        r_locked = self.client.post('/staff', data={'passcode': '2918'})
        self.assertEqual(r_locked.status_code, 429)
        with app_module._login_failures_lock:
            app_module._login_failures.clear()

    def test_restored_city_events_and_operational_notice_audit(self):
        import events
        import data_audit

        self.assertEqual(events.extract_olg_target_stores('【梅田】週末イベント')[0], ['OLG 大阪駅前', 'OLG ag梅田'])
        self.assertEqual(events.extract_olg_target_stores('【仙台】週末イベント')[0], ['OLG ag仙台'])
        self.assertEqual(events.extract_olg_target_stores('【金沢】週末イベント')[0], ['OLG ag金沢'])
        self.assertEqual(events.extract_olg_target_stores('【広島】週末イベント')[0], ['OLG ag広島'])
        self.assertEqual(events.extract_olg_target_stores('【沖縄】週末イベント')[0], ['OLG ag沖縄'])

        events._recent_operational_notices.clear()
        events._events_memory_cache['operational_notices'] = []
        try:
            events._record_operational_notice('OLG', '【重要】恵比寿店 料金改定のお知らせ', 'https://oriental-lounge.com/announcements/999')
            jst = datetime.timezone(datetime.timedelta(hours=9))
            rep = data_audit.run_data_audit(
                live_stores=[],
                all_events=[],
                now_jst=datetime.datetime(2026, 10, 28, 21, 0, tzinfo=jst),
            )
            self.assertEqual(rep['summary']['revision_notice_count'], 1)
            self.assertTrue(any('料金改定' in str(w) for w in rep['warnings']))
        finally:
            events._recent_operational_notices.clear()

    def test_adsense_coverage_privacy_exclusion_and_staff_suppression(self):
        # 1. Public visitor gets AdSense on newly completed guide pages, NOT on /privacy
        for pub_path in ('/guide/price-event-comparison', '/guide/male-lead-mechanics', '/guide/beauty-area-stage', '/store/oriental-lounge-shinjuku'):
            r = self.client.get(pub_path)
            self.assertEqual(r.status_code, 200, pub_path)
            self.assertIn(b'ca-pub-3043764334238627', r.data, f"{pub_path} missing AdSense for public user")

        r_priv = self.client.get('/privacy')
        self.assertEqual(r_priv.status_code, 200)
        self.assertNotIn(b'ca-pub-3043764334238627', r_priv.data)

        # 2. Staff session hides AdSense and /link/ PR across all pages including /
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = False
        try:
            for check_path in ('/', '/for-women', '/grooming', '/store/oriental-lounge-shinjuku', '/guide/price-event-comparison', '/columns', '/areas'):
                rs = self.client.get(check_path)
                self.assertEqual(rs.status_code, 200, check_path)
                body = rs.data.decode('utf-8')
                self.assertNotIn('ca-pub-3043764334238627', body, f"Staff leaked AdSense on {check_path}")
                self.assertNotIn('/link/', body, f"Staff leaked /link/ affiliate on {check_path}")
        finally:
            with self.client.session_transaction() as s:
                s.pop('is_staff', None)
                s.pop('is_master', None)

    def test_sns_zero_guest_guards_job_state_retention_and_sw_v48(self):
        import sns_workflows
        import threads_poster

        zero_stores = [{'name': 'OLG 新宿', 'women': 0, 'men': 0, 'region': 'Kanto'}]
        with mock.patch('monitor.get_all_data', return_value=zero_stores), \
             mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as m_xpost:
            res_x = sns_workflows.execute_scheduled_x_post({'full_data': zero_stores, 'is_seed': True}, slot_label='20:00', force=False)
            self.assertEqual(res_x['status'], 'skipped')
            self.assertEqual(m_xpost.call_count, 0)

        res_th = threads_poster.execute_scheduled_threads_post(zero_stores, slot_label='22:00', force=False)
        self.assertEqual(res_th['status'], 'skipped')

        # Verify job_state preserves token:* and note:* across >2500 x_post:* insertions
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        job_state.reset_cache_for_tests()
        job_state.set_value('token:ig_access_token', 'secret_tok_999')
        job_state.mark_done('note:https://note.com/aiseki_navi/n/n12345')
        for i in range(2500):
            job_state.mark_done(f'x_post:2026-01-01_{i:04d}')
        job_state.reset_cache_for_tests()
        self.assertEqual(job_state.get_value('token:ig_access_token'), 'secret_tok_999')
        self.assertTrue(job_state.is_done('note:https://note.com/aiseki_navi/n/n12345'))

        sw_resp = self.client.get('/sw.js')
        try:
            sw_txt = sw_resp.data.decode('utf-8')
            self.assertIn("CACHE_NAME = 'aiseki-navi-v64'", sw_txt)
            self.assertIn("event.waitUntil", sw_txt)
        finally:
            sw_resp.close()

    def test_layout_regression_and_fast_cache_invalidation(self):
        """Ensure mobile staff header centering, low-key editorial tone, and instant live-cache invalidation work."""
        # 1. Staff page (5555) should not have duplicate emoji inside SVG-equipped pills and should use light-safe classes
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = False
        r_staff = self.client.get('/staff', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r_staff.status_code, 200)
        import gzip as _gz
        raw_staff = _gz.decompress(r_staff.data).decode('utf-8') if r_staff.headers.get('Content-Encoding') == 'gzip' else r_staff.data.decode('utf-8')
        self.assertIn('staff-master-switch-btn', raw_staff)
        self.assertIn('<span>週間レポート（全店舗の集計・ピークランキング）</span>', raw_staff)
        self.assertNotIn('<span>📅 週間レポート', raw_staff)
        self.assertNotIn('<span>📊 系列・競合比較', raw_staff)
        self.assertNotIn('<span>🎉 イベント速報', raw_staff)

        # Verify calm.css strictly centers body.staff-mode header title & action buttons
        with open(os.path.join(ROOT, 'static', 'calm.css'), 'r', encoding='utf-8') as cf:
            calm_txt = cf.read()
        self.assertIn('body.staff-mode .header-brand-block', calm_txt)
        self.assertIn('text-align: center !important;', calm_txt)
        self.assertIn('justify-content: center !important;', calm_txt)

        # 2. Master page (2918) should render theme-aware master-subpanel classes instead of dark-only inline colors
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = True
        r_master = self.client.get('/staff')
        self.assertEqual(r_master.status_code, 200)
        raw_master = r_master.data.decode('utf-8')
        self.assertIn('master-subpanel', raw_master)

        # 3. Clear staff session: verify overt affiliate push cards are removed from '/' & store_detail, and live cache invalidates instantly
        with self.client.session_transaction() as s:
            s.clear()
        pub_home = self.client.get('/').data.decode('utf-8')
        self.assertNotIn('ヒゲ脱毛・スキンケアを見る', pub_home)
        self.assertNotIn('近くで会える恋活アプリを見る', pub_home)
        self.assertNotIn('スタイリスト監修コーデを見る', pub_home)
        self.assertNotIn('特集アプリ', pub_home)
        self.assertIn('データで見るお店選び・当日のチェックポイント', pub_home)

        with open(os.path.join(ROOT, 'static', 'affiliate_config.js'), 'r', encoding='utf-8') as af:
            aff_txt = af.read()
        self.assertIn('infeedSlots: []', aff_txt)
        with app_module.data_lock:
            saved_full = list(app_module.latest_data.get('full_data') or [])
            saved_upd = app_module.latest_data.get('last_updated')
            saved_seed = app_module.latest_data.get('is_seed')
        try:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = [{
                    'name': 'OLG 新宿', 'women': 41, 'men': 19, 'source': 'oriental',
                    'region': 'Kanto', 'women_diff': 3, 'men_diff': 1,
                    'votes': {'smooth': 1, 'waiting': 0, 'lively': 0},
                }]
                app_module.latest_data['last_updated'] = '2026-10-27 21:00:00'
                app_module.latest_data['is_seed'] = False
            r1 = self.client.get('/')
            self.assertIn('>41<', r1.data.decode('utf-8'))
            s1 = self.client.get('/api/status').get_json()
            self.assertEqual(s1['ranking'][0]['women'], 41)

            # Mutate women count -> both '/' SSR HTML and '/api/status' JSON must reflect 58 immediately
            with app_module.data_lock:
                app_module.latest_data['full_data'][0]['women'] = 58
                app_module.latest_data['last_updated'] = '2026-10-27 21:01:00'
            r2 = self.client.get('/')
            self.assertIn('>58<', r2.data.decode('utf-8'))
            s2 = self.client.get('/api/status').get_json()
            self.assertEqual(s2['ranking'][0]['women'], 58)

            # 4. Even when is_seed=True (cold container boot), '/' must render store cards immediately (never an empty #ranking-list)
            with app_module.data_lock:
                app_module.latest_data['is_seed'] = True
                app_module.latest_data['full_data'][0]['women'] = 0
                app_module.latest_data['full_data'][0]['men'] = 0
            app_module.clear_fast_http_caches()
            r_seed = self.client.get('/')
            seed_html = r_seed.data.decode('utf-8')
            self.assertIn('class="ranking-item', seed_html)
            self.assertNotIn('id="content" class="content hidden"', seed_html)

            # 5. refresh_momentum_from_db() promotes is_seed=True to live counts when SQLite snapshot has >=10 stores
            now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
            recent_ts = (now_jst - datetime.timedelta(minutes=2)).strftime('%Y-%m-%d %H:%M:%S')
            local_db.insert_logs_with_ts([
                {'ts': recent_ts, 'name': sname, 'men': 14, 'women': 29, 'source': 'oriental'}
                for sname in LIVE_STORE_NAMES[:15]
            ])
            app_module.refresh_momentum_from_db()
            with app_module.data_lock:
                self.assertFalse(app_module.latest_data['is_seed'])
                self.assertTrue(any(s.get('women') == 29 for s in app_module.latest_data['full_data']))

            # 6. Public '/' must show '<h1>相席ナビ</h1>' even when master session is active, and place #ranking-list BEFORE hidden modals
            with self.client.session_transaction() as s:
                s['is_staff'] = True
                s['is_master'] = True
            r_pub_with_master = self.client.get('/')
            pub_master_html = r_pub_with_master.data.decode('utf-8')
            self.assertIn('<h1>相席ナビ</h1>', pub_master_html)
            self.assertNotIn('<h1>相席ナビ マスター管理</h1>', pub_master_html)
            self.assertIn('data-store="OLG 新宿"', pub_master_html)
            self.assertIn('window.__SSR_PINNED_HYDRATED', pub_master_html)
            self.assertLess(pub_master_html.index('id="ranking-list"'), pub_master_html.index('id="analysis-modal"'))
            with self.client.session_transaction() as s:
                s.clear()
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = saved_full
                app_module.latest_data['last_updated'] = saved_upd
                app_module.latest_data['is_seed'] = saved_seed
            app_module.clear_fast_http_caches()

    def test_sns_editorial_4_role_pipeline_and_master_only_visibility(self):
        import sns_editorial
        sample_stores = [
            {'name': 'OLG 梅田茶屋町', 'women': 44, 'men': 22, 'women_diff': 6, 'usual_w_diff': 9, 'region': 'Kinki'},
            {'name': 'JIS UMEDA', 'women': 12, 'men': 28, 'women_diff': -2, 'usual_w_diff': -4, 'region': 'Kinki'},
            {'name': 'OLG 新宿', 'women': 38, 'men': 24, 'women_diff': 4, 'usual_w_diff': 5, 'region': 'Kanto'},
            {'name': 'JIS SHINJUKU', 'women': 18, 'men': 25, 'women_diff': 0, 'usual_w_diff': 0, 'region': 'Kanto'},
        ]
        res = sns_editorial.run_editorial_pipeline(sample_stores, slot_label='22:00', record_history=True)
        winner = res['winner']
        self.assertIsNotNone(winner)
        # Winner must NOT be the mechanical 'compact_top3' medal format
        self.assertNotEqual(winner['style'], 'compact_top3')
        self.assertNotIn('🥇', winner['main_text'])
        self.assertGreaterEqual(winner['total_score'], 80)
        self.assertLessEqual(winner['weight'], 280)
        self.assertGreaterEqual(len(res['evaluation']['candidates']), 4)

        # Verify Regular Staff (5555) NEVER sees the SNS Editorial subpanel on /staff
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = False
        app_module.clear_fast_http_caches()
        staff_html = self.client.get('/staff').data.decode('utf-8')
        self.assertNotIn('SNS編集部（4役合議）', staff_html)
        self.assertNotIn('/staff/sns-editorial', staff_html)

        # Verify X, Threads, and Instagram 4-role pipelines all avoid mechanical medal lists and enforce platform limits
        res_th = sns_editorial.run_editorial_pipeline(sample_stores, slot_label='22:00', channel='threads', record_history=True)
        self.assertNotEqual(res_th['winner']['style'], 'threads_classic_top3')
        self.assertNotIn('🥇', res_th['winner']['main_text'])
        self.assertNotIn('OLG ', res_th['winner']['main_text'])
        self.assertLessEqual(len(res_th['winner']['main_text']), 495)

        res_ig = sns_editorial.run_editorial_pipeline(sample_stores, slot_label='21:45', channel='instagram', record_history=True)
        self.assertNotEqual(res_ig['winner']['style'], 'ig_classic_template')
        self.assertIn('保存', res_ig['winner']['main_text'])
        self.assertGreaterEqual(res_ig['winner']['total_score'], 80)

        # Verify Master (2918) sees both the subpanel on /staff and full details across all 3 channels on /staff/sns-editorial
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = True
        app_module.clear_fast_http_caches()
        with app_module.data_lock:
            old_full = list(app_module.latest_data.get('full_data') or [])
            app_module.latest_data['full_data'] = sample_stores
        try:
            master_html = self.client.get('/staff').data.decode('utf-8')
            self.assertIn('SNS編集部（4役合議）', master_html)
            self.assertIn('/staff/sns-editorial?channel=threads', master_html)
            self.assertIn('/staff/sns-editorial?channel=instagram', master_html)
            for ch_name in ('x', 'threads', 'instagram'):
                ed_page = self.client.get(f'/staff/sns-editorial?channel={ch_name}&slot=22:00').data.decode('utf-8')
                self.assertIn('① リサーチャー（現場データ発掘係）', ed_page)
                self.assertIn('② プランナー（編成・頻度調整係）', ed_page)
                self.assertIn('③ ライター（執筆・AIアドリブ係）', ed_page)
                self.assertIn('④ デスク（数値改ざん検閲・採点係）', ed_page)
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = old_full
            with self.client.session_transaction() as s:
                s.clear()
            app_module.clear_fast_http_caches()

    def test_sns_editorial_gemini_adlib_weather_and_fact_guard(self):
        import sns_editorial
        from unittest.mock import patch, MagicMock

        sample_stores = [
            {'name': 'OLG 梅田茶屋町', 'women': 44, 'men': 22, 'women_diff': 6, 'usual_w_diff': 9, 'region': 'Kinki'},
            {'name': 'JIS UMEDA', 'women': 12, 'men': 28, 'women_diff': -2, 'usual_w_diff': -4, 'region': 'Kinki'},
            {'name': 'OLG 新宿', 'women': 38, 'men': 24, 'women_diff': 4, 'usual_w_diff': 5, 'region': 'Kanto'},
            {'name': 'JIS SHINJUKU', 'women': 18, 'men': 25, 'women_diff': 0, 'usual_w_diff': 0, 'region': 'Kanto'},
        ]

        # 1. Unauthorized & regular staff (5555) cannot set Gemini API key
        self.assertEqual(self.client.post('/api/sns-editorial/gemini-key', json={'api_key': 'AIzaSyDummyKey1234567890'}).status_code, 401)
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = False
        self.assertEqual(self.client.post('/api/sns-editorial/gemini-key', json={'api_key': 'AIzaSyDummyKey1234567890'}).status_code, 401)

        # 2. Master (2918) can save obfuscated Gemini API key; raw key is never exposed in status
        with self.client.session_transaction() as s:
            s['is_staff'] = True
            s['is_master'] = True
        r_save = self.client.post('/api/sns-editorial/gemini-key', json={'action': 'save', 'api_key': 'AIzaSyDummyKey1234567890'})
        self.assertEqual(r_save.status_code, 200)
        st = r_save.get_json()['gemini_status']
        self.assertTrue(st['configured'])
        self.assertEqual(st['masked_key'], 'AIza...7890')
        self.assertNotIn('DummyKey', json.dumps(st))

        try:
            # 3. Valid Gemini deep-thinking ad-lib draft ('オリエンタルラウンジ' + colloquial 'オリラジ' + weather vibe + thinking_memo) wins #1 with 100/100
            valid_ai_json = json.dumps({
                'main_text': (
                    '小雨の金曜夜☔ 駅チカの梅田まわりで今かなり座りやすいのがオリエンタルラウンジ梅田茶屋町✨\n\n'
                    '・オリラジ梅田茶屋町：女性44・男性22（女性比67%）\n'
                    '・オリラジ新宿：女性38・男性24\n\n'
                    '向かう前の比較はリプ欄から👇\n#相席ナビ #相席ラウンジ'
                ),
                'writer_note': '雨天の駅チカ集中と梅田の実測値を掛け合わせたアドリブ案',
                'thinking_memo': '①独り言風・②常連LINE風・③温度差発見風の3案を比較し、雨の22時ピークなので②をベースに宣伝臭を削って採用。',
            }, ensure_ascii=False)
            mock_resp_ok = MagicMock()
            mock_resp_ok.status_code = 200
            mock_resp_ok.json.return_value = {'candidates': [{'content': {'parts': [{'text': valid_ai_json}]}}]}

            with patch('sns_editorial.requests.post', return_value=mock_resp_ok):
                res_ok = sns_editorial.run_editorial_pipeline(
                    sample_stores, slot_label='22:00', channel='x', use_ai=True, force_ai_refresh=True
                )
            self.assertEqual(res_ok['winner']['style'], 'ai_live_adlib')
            self.assertEqual(res_ok['winner']['total_score'], 100)
            self.assertIn('小雨の金曜夜', res_ok['winner']['main_text'])
            self.assertIn('オリエンタルラウンジ梅田茶屋町', res_ok['winner']['main_text'])
            self.assertIn('オリラジ新宿', res_ok['winner']['main_text'])
            self.assertIn('3案を比較', res_ok['winner']['thinking_memo'])
            self.assertIn('22:00', res_ok['research']['slot_persona']['label'])

            # 3a. Bare 'オリエンタル〇〇' ('ラウンジ' omitted) in AI output is automatically normalized to 'オリエンタルラウンジ〇〇' / 'オリラジ〇〇'
            bare_oriental_json = json.dumps({
                'main_text': (
                    '雨上がりで動きが早い夜🌙 いま狙い目なのはオリエンタル梅田茶屋町！\n\n'
                    '・オリエンタル梅田茶屋町：女性44・男性22（女性比67%）\n'
                    '・オリエンタル新宿：女性38・男性24\n\n'
                    '比較はリプ欄へ👇\n#相席ナビ #相席ラウンジ'
                ),
                'writer_note': '裸のオリエンタル自動補正テスト',
            }, ensure_ascii=False)
            mock_resp_bare = MagicMock()
            mock_resp_bare.status_code = 200
            mock_resp_bare.json.return_value = {'candidates': [{'content': {'parts': [{'text': bare_oriental_json}]}}]}
            with patch('sns_editorial.requests.post', return_value=mock_resp_bare):
                res_bare = sns_editorial.run_editorial_pipeline(
                    sample_stores, slot_label='22:00', channel='x', use_ai=True, force_ai_refresh=True
                )
            self.assertEqual(res_bare['winner']['style'], 'ai_live_adlib')
            self.assertEqual(res_bare['winner']['total_score'], 100)
            self.assertNotIn('オリエンタル梅田茶屋町', res_bare['winner']['main_text'])
            self.assertTrue(
                'オリエンタルラウンジ梅田茶屋町' in res_bare['winner']['main_text']
                or 'オリラジ梅田茶屋町' in res_bare['winner']['main_text']
            )

            # 3b. AI draft containing robotic copywriting cliches ('必見', 'まさに') is penalized on human_tone by Desk
            cliche_ai_json = json.dumps({
                'main_text': (
                    '今夜はまさに必見の盛り上がり！オリエンタルラウンジ梅田茶屋町が女性44・男性22（女性比67%）✨\n'
                    '向かう前の比較はリプ欄から👇\n#相席ナビ #相席ラウンジ'
                ),
                'writer_note': 'AI臭ワードテスト',
            }, ensure_ascii=False)
            mock_resp_cliche = MagicMock()
            mock_resp_cliche.status_code = 200
            mock_resp_cliche.json.return_value = {'candidates': [{'content': {'parts': [{'text': cliche_ai_json}]}}]}
            with patch('sns_editorial.requests.post', return_value=mock_resp_cliche):
                res_cliche = sns_editorial.run_editorial_pipeline(
                    sample_stores, slot_label='00:00', channel='x', use_ai=True, force_ai_refresh=True
                )
            cliche_eval = next(c for c in res_cliche['evaluation']['candidates'] if c['style'] == 'ai_live_adlib')
            self.assertEqual(cliche_eval['scores']['human_tone'], 22)
            self.assertIn('00:00', res_cliche['research']['slot_persona']['label'])

            # 4. Hallucinated AI draft (invents 女性99名 instead of 44名) is strictly blocked by Desk (-50 data score)
            bad_ai_json = json.dumps({
                'main_text': (
                    '今夜のオリラジ梅田茶屋町、なんと女性99名・男性22名で大爆発中🔥\n'
                    'いますぐリプ欄からチェック👇\n#相席ナビ #相席ラウンジ'
                ),
                'writer_note': '誇張テスト',
            }, ensure_ascii=False)
            mock_resp_bad = MagicMock()
            mock_resp_bad.status_code = 200
            mock_resp_bad.json.return_value = {'candidates': [{'content': {'parts': [{'text': bad_ai_json}]}}]}

            with patch('sns_editorial.requests.post', return_value=mock_resp_bad):
                res_bad = sns_editorial.run_editorial_pipeline(
                    sample_stores, slot_label='22:00', channel='x', use_ai=True, force_ai_refresh=True
                )
            # Winner MUST fall back safely to verified deterministic draft, NOT the hallucinated AI draft!
            self.assertNotEqual(res_bad['winner']['style'], 'ai_live_adlib')
            ai_cand_eval = next(c for c in res_bad['evaluation']['candidates'] if c['style'] == 'ai_live_adlib')
            self.assertEqual(ai_cand_eval['scores']['data_specificity'], -50)

            # 5. Modern AI Studio `AQ....` auth key targets `gemini-2.5-pro` first with deep thinking (`thinkingBudget: 1536`)
            sns_editorial.set_gemini_api_key('AQ.Ab8RN6SampleExpressKey1234dgKQ')
            with patch('sns_editorial.requests.post', return_value=mock_resp_ok) as mock_aq_post:
                res_aq = sns_editorial.run_editorial_pipeline(
                    sample_stores, slot_label='22:00', channel='x', use_ai=True, force_ai_refresh=True
                )
            self.assertEqual(res_aq['winner']['style'], 'ai_live_adlib')
            called_url = mock_aq_post.call_args[0][0]
            called_json = mock_aq_post.call_args[1]['json']
            self.assertIn('generativelanguage.googleapis.com/v1beta/models/gemini-3.1-pro-preview:generateContent', called_url)
            self.assertEqual(called_json['generationConfig'].get('thinkingConfig'), {'thinkingLevel': 'MEDIUM'})
            self.assertEqual(called_json['generationConfig'].get('maxOutputTokens'), 4648)
            self.assertEqual(res_aq['gemini_status']['state'], 'active')

            # 6. HTML page `GET /staff/sns-editorial` NEVER blocks on Gemini HTTP calls (0 network posts, instant render)
            sns_editorial.clear_editorial_ai_cache()
            with app_module.data_lock:
                old_full = app_module.latest_data.get('full_data')
                app_module.latest_data['full_data'] = sample_stores
            try:
                with patch('sns_editorial.requests.post', side_effect=AssertionError('HTML render must not block on Gemini API!')):
                    html_resp = self.client.get('/staff/sns-editorial?channel=x&slot=22:00')
                self.assertEqual(html_resp.status_code, 200)
                html_body = html_resp.get_data(as_text=True)
                self.assertIn('🧠 上位AI編集長が長考モードで3案を脳内コンペ・本音推敲中', html_body)

                # And async JSON endpoint `/api/sns-editorial` resolves the Gemini ad-lib draft + thinking_memo cleanly
                with patch('sns_editorial.requests.post', return_value=mock_resp_ok) as mock_async_post:
                    api_resp = self.client.get('/api/sns-editorial?channel=x&slot=22:00')
                self.assertEqual(api_resp.status_code, 200)
                api_json = api_resp.get_json()
                self.assertEqual(api_json['winner']['style'], 'ai_live_adlib')
                self.assertIn('3案を比較', api_json['winner']['thinking_memo'])
                self.assertEqual(mock_async_post.call_count, 1)

                # Subsequent HTML load renders the cached deep-thinking draft + brain memo immediately in 0ms
                with patch('sns_editorial.requests.post', side_effect=AssertionError('Cached HTML must not hit network!')):
                    cached_html = self.client.get('/staff/sns-editorial?channel=x&slot=22:00').get_data(as_text=True)
                self.assertIn('🧠 AI編集長の脳内メモ（3案コンペ・推敲プロセス）', cached_html)
                self.assertIn('3案を比較', cached_html)
            finally:
                with app_module.data_lock:
                    app_module.latest_data['full_data'] = old_full
        finally:
            sns_editorial.set_gemini_api_key('')
            with self.client.session_transaction() as s:
                s.clear()
            app_module.clear_fast_http_caches()

    def test_performance_caching_and_audit_bugfixes(self):
        import sns_editorial
        import sheets_store
        from unittest.mock import patch

        # 1. job_state kv_ts persistence: older Google Sheets row must NEVER overwrite a newer local token after restart
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        job_state.reset_cache_for_tests()
        job_state.set_value('token:gemini_api_key', 'NEW_FRESH_KEY_2026')
        job_state.reset_cache_for_tests()
        stale_sheet_entry = f"__kvt__:20200101000000:token:gemini_api_key={job_state._obfuscate_secret('OLD_STALE_KEY_2020')}"
        with patch.object(sheets_store, 'is_disabled', return_value=False), \
             patch.object(sheets_store, 'read_rows_or_none', return_value=[[stale_sheet_entry]]):
            self.assertEqual(job_state.get_value('token:gemini_api_key'), 'NEW_FRESH_KEY_2026')
        job_state.set_value('token:gemini_api_key', '')

        # 2. sns_editorial: usual_women benchmark triggers usual_anomaly, 00:05 Saturday is Friday night, and non-headcount numbers pass Desk
        jst = datetime.timezone(datetime.timedelta(hours=9))
        sat_midnight = datetime.datetime(2026, 10, 31, 0, 5, tzinfo=jst)  # Saturday 00:05 AM = Friday business night
        mock_bm = {'OLG 新宿': {'usual_women': 20.0, 'usual_men': 15.0}}
        live_sample = [
            {'name': 'OLG 新宿', 'women': 34, 'men': 18, 'region': 'Kanto'},
            {'name': 'OLG 渋谷本店', 'women': 22, 'men': 16, 'region': 'Kanto'},
        ]
        with patch('local_db.get_all_stores_live_benchmarks', return_value=mock_bm):
            research = sns_editorial.run_researcher(live_sample, slot_label='00:00', now_jst=sat_midnight)
        self.assertIsNotNone(research.get('anomaly_store'))
        self.assertEqual(research['anomaly_store']['usual_w_diff'], 14)
        self.assertEqual(research['calendar']['weekday_ja'], '金')
        self.assertIsNone(research.get('west_top'))  # No Kansai store open -> must NOT duplicate east_top
        x_cands = sns_editorial._write_x_candidates(research, '00:00', sat_midnight, 'https://aiseki-navi.com')
        self.assertTrue(any(c.get('style') == 'usual_anomaly' for c in x_cands))

        ok_natural, _ = sns_editorial.verify_ai_factual_numbers(
            '男性1名でも入りやすい夜！オリエンタルラウンジ新宿は女性34・男性18（女性2人組中心・男性30分待ちなし）',
            research,
        )
        self.assertTrue(ok_natural)
        ok_hallucinated, _ = sns_editorial.verify_ai_factual_numbers(
            'オリエンタルラウンジ新宿は女性99・男性18です',
            research,
        )
        self.assertFalse(ok_hallucinated)

        # 3. Unknown store on /api/analysis/<store> returns fast 404 without hitting compute_store_analysis_payload
        with patch('store_analysis.compute_store_analysis_payload', side_effect=AssertionError('Must not run for unknown store!')):
            r_404 = self.client.get('/api/analysis/NON_EXISTENT_SCAN_STORE_999')
        self.assertEqual(r_404.status_code, 404)

        # 4. /api/status ETag -> 304 Not Modified, plus instant invalidation on data change
        r_st1 = self.client.get('/api/status', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r_st1.status_code, 200)
        etag = r_st1.headers.get('ETag')
        self.assertTrue(etag)
        r_304 = self.client.get('/api/status', headers={'If-None-Match': etag})
        self.assertEqual(r_304.status_code, 304)
        self.assertEqual(len(r_304.data), 0)

        # 5. Gzipped /sitemap.xml and cached /ogp/live.png + /ogp/ig-slide/1.jpg
        r_smap = self.client.get('/sitemap.xml', headers={'Accept-Encoding': 'gzip'})
        self.assertEqual(r_smap.status_code, 200)
        self.assertEqual(r_smap.headers.get('Content-Encoding'), 'gzip')
        r_ogp = self.client.get('/ogp/live.png')
        self.assertEqual(r_ogp.status_code, 200)
        self.assertTrue(r_ogp.data.startswith(b'\x89PNG'))
        r_ig_jpg = self.client.get('/ogp/ig-slide/1.jpg')
        self.assertEqual(r_ig_jpg.status_code, 200)
        self.assertEqual(r_ig_jpg.mimetype, 'image/jpeg')
        self.assertTrue(r_ig_jpg.data.startswith(b'\xff\xd8'))

        # 6. Staff session sets X-Staff-View: 1 & private, no-store; vote tracking includes store_name
        with self.client.session_transaction() as s:
            s['is_staff'] = True
        r_staff_col = self.client.get('/columns')
        self.assertEqual(r_staff_col.headers.get('X-Staff-View'), '1')
        self.assertIn('no-store', r_staff_col.headers.get('Cache-Control', ''))
        with self.client.session_transaction() as s:
            s.clear()

        desc_vote = analytics.describe_label('vote', 'OLG 新宿:smooth')
        self.assertIn('オリエンタルラウンジ 新宿', desc_vote['title'])
        self.assertIn('すぐ入れた', desc_vote['title'])


class ColumnEditorialRoomTests(unittest.TestCase):
    """End-to-end tests for the Master-only 4-Role Column & note.com Editorial Room."""

    def setUp(self):
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        import column_editorial
        job_state.reset_cache_for_tests()
        column_editorial.clear_column_ai_cache()
        with app_module.data_lock:
            app_module.latest_data['full_data'] = _fake_stores()
            app_module.latest_data['is_seed_fallback'] = False
            app_module.latest_data['last_updated'] = datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=9))
            ).strftime('%Y-%m-%d %H:%M:%S')
        app_module._last_fetch_epoch = 9999999999.0
        app_module.clear_fast_http_caches()
        self.client = app_module.app.test_client()

    def tearDown(self):
        import column_editorial
        column_editorial.clear_column_ai_cache()
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        job_state.reset_cache_for_tests()
        app_module.clear_fast_http_caches()

    def test_column_editorial_permissions_ai_sanitization_and_scheduled_publish_flow(self):
        import re
        import json
        import column_editorial
        import scheduled_articles
        import x_poster
        import analytics

        # 1. Access control: anonymous & regular staff blocked; master allowed
        self.assertEqual(self.client.get('/staff/column-editorial').status_code, 302)
        self.assertEqual(self.client.get('/api/column-editorial').status_code, 401)
        self.assertEqual(self.client.post('/api/column-editorial/publish', json={}).status_code, 401)

        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = False
        self.assertEqual(self.client.get('/staff/column-editorial').status_code, 302)
        self.assertEqual(self.client.get('/api/column-editorial').status_code, 401)

        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True

        r_board = self.client.get('/staff/column-editorial?theme=midnight_wave')
        self.assertEqual(r_board.status_code, 200)
        board_html = r_board.data.decode('utf-8')
        self.assertIn('コラム・note編集部', board_html)
        self.assertIn('全コラム「鬼デスク品質・人間味＆ブランド呼称」監査一覧', board_html)

        # 2. Pipeline with mocked Gemini thinking output containing bare 'オリエンタル大阪駅前' -> auto-normalized!
        mock_ai_json = {
            "thinking_memo": "受付待ちの失敗談・相方LINE・雨夜の3案を脳内で比較し、23時半のエレベーター前の温度差から入る導入を採用。",
            "writer_note": "オリエンタル大阪駅前とag梅田の深夜維持差を会話3箇所で描写",
            "slug": "umeda-midnight-test",
            "title": "23時半のオリエンタル大阪駅前とag梅田で見た「第2波」のリアルな温度差",
            "short_title": "梅田23時半の第2波とハシゴ術",
            "desc": "終電前に一度落ち着いたフロアが0時過ぎに再び華やぐ瞬間を、オリエンタル大阪駅前の実測データと現場の会話で紐解きます。",
            "sections": [
                {
                    "heading": "1. 23時20分のエレベーター前で迷った夜",
                    "paragraphs": ["オリエンタル大阪駅前のビル下で相方と画面を見比べました。"],
                    "dialogue": ["「もう終電近いし今日は帰る？」", "「いや、0時過ぎから第2波来るらしいから1杯だけ寄ろう」"],
                    "after_paragraphs": ["結果的にこの判断が大当たりでした。"],
                    "highlight_title": "終電際の盲点",
                    "highlight_body": "23時台の減少だけで判断しないこと。"
                },
                {
                    "heading": "2. 数字が裏付ける深夜0時半の再流入",
                    "paragraphs": ["2軒目帰りの女性グループが合流します。"],
                    "dialogue": ["「さっきまで近くで飲んでて2軒目で来ました〜」", "「ちょうど落ち着いた席でよかったね！」"],
                    "after_paragraphs": ["会話のテンポも終電前よりゆったりしています。"],
                    "highlight_title": "",
                    "highlight_body": ""
                },
                {
                    "heading": "3. 深夜ラウンドは45分スマート勝負",
                    "paragraphs": ["ダラダラ居座らずスッキリ切り上げるのが鉄則です。"],
                    "dialogue": ["「近くでシメのパフェかバー1杯だけ行かない？」", "「いいですね、行きましょう！」"],
                    "after_paragraphs": ["無理のない声かけが一番自然に通ります。"],
                    "highlight_title": "深夜の3原則",
                    "highlight_body": "①比率確認 ②雑談スタート ③45分判断"
                }
            ],
            "summary_points": ["23時半の谷を越えると第2波が来る", "徒歩圏の比率差を見る", "45分でスマートに動く"],
            "faqs": [
                {"q": "0時過ぎからでも女性はいますか？", "a": "金土や祝前日は2軒目利用の女性が多く来店します。"},
                {"q": "1人でも入れますか？", "a": "店舗状況により案内可能です。"}
            ],
            "x_title": "梅田23時半の第2波とリアルなハシゴ温度差",
            "x_main_text": "23時半のオリエンタル大阪駅前で「もう遅いかな…」と迷った夜、0時過ぎから2軒目帰りの女の子が続々入ってきた話🌙\n\n終電後の第2波と会話のコツを書きました👇\n#相席ラウンジ #相席ナビ",
            "note_title": "23時半に帰りかけた夜、梅田のラウンジで見た「第2波」の話",
            "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ"
        }

        class _DummyResp:
            status_code = 200
            text = ""
            def json(self_inner):
                return {"candidates": [{"content": {"parts": [{"text": json.dumps(mock_ai_json, ensure_ascii=False)}]}}]}

        sample_stores = [
            {"name": "OLG 大阪駅前", "women": 50, "men": 28, "region": "Kansai"},
            {"name": "OLG ag梅田", "women": 42, "men": 24, "region": "Kansai"},
            {"name": "JIS UMEDA", "women": 31, "men": 33, "region": "Kansai"},
        ]
        with mock.patch('sns_editorial.get_gemini_api_key', return_value='AIzaDummyKeyForColumnEditorialTest12345'), \
             mock.patch('requests.post', return_value=_DummyResp()):
            ed = column_editorial.run_column_editorial_pipeline(
                sample_stores,
                theme_key='midnight_wave',
                custom_topic='日曜23時半の梅田第2波テスト',
                use_ai=True,
                force_ai_refresh=True,
                ai_cache_only=False,
            )

        winner = ed['winner']
        self.assertTrue(winner['is_ai_generated'])
        self.assertIn('脳内で比較', winner['thinking_memo'])
        # Bare 'オリエンタル(?!ラウンジ)' must be 100% eliminated across title, preview, note, and X text!
        for field in ('title', 'desc', 'preview_text', 'note_markdown', 'x_main_text'):
            self.assertIsNone(
                re.search(r'オリエンタル(?!ラウンジ)', winner[field]),
                f"Bare オリエンタル leaked in {field}: {winner[field]}",
            )
        self.assertLessEqual(x_poster.compute_twitter_weight(winner['x_main_text']), 280)

        # 3. Schedule publication via POST /api/column-editorial/publish
        r_pub = self.client.post('/api/column-editorial/publish', json={
            'candidate': winner,
            'publish_mode': 'schedule',
            'publish_at': '2026-12-10T19:00',
            'slug': 'umeda-midnight-test',
            'eyecatch_key': 'last-train',
        })
        self.assertEqual(r_pub.status_code, 200)
        pub_json = r_pub.get_json()
        self.assertTrue(pub_json['ok'])
        self.assertEqual(pub_json['path'], '/guide/umeda-midnight-test')

        # Before scheduled time: staff gets 200 noindex preview; public visitor gets 404; unknown slug always 404
        t_before = datetime.datetime(2026, 12, 10, 18, 0, 0)
        with mock.patch('scheduled_articles.now_jst_naive', return_value=t_before):
            app_module.clear_fast_http_caches()
            r_staff_prev = self.client.get('/guide/umeda-midnight-test')
            self.assertEqual(r_staff_prev.status_code, 200)
            prev_html = r_staff_prev.data.decode('utf-8')
            self.assertIn('社内限定プレビュー', prev_html)
            self.assertIn('dialogue-box', prev_html)
            self.assertIn('data-table', prev_html)
            self.assertEqual(r_staff_prev.headers.get('X-Robots-Tag'), 'noindex, nofollow')

            with self.client.session_transaction() as sess:
                sess.clear()
            self.assertEqual(self.client.get('/guide/umeda-midnight-test').status_code, 404)
            self.assertEqual(self.client.get('/guide/non-existent-random-column-999').status_code, 404)
            col_before = self.client.get('/columns').data.decode('utf-8')
            self.assertNotIn('/guide/umeda-midnight-test', col_before)

        # After scheduled time: public gets 200, listed on /columns & /sitemap.xml, analytics resolves title, X thread fires once
        t_after = datetime.datetime(2026, 12, 10, 19, 10, 0)
        with mock.patch('scheduled_articles.now_jst_naive', return_value=t_after):
            app_module.clear_fast_http_caches()
            r_live = self.client.get('/guide/umeda-midnight-test')
            self.assertEqual(r_live.status_code, 200)
            live_html = r_live.data.decode('utf-8')
            self.assertNotIn('社内限定プレビュー', live_html)
            self.assertIn('オリエンタルラウンジ大阪駅前', live_html)

            col_after = self.client.get('/columns').data.decode('utf-8')
            self.assertIn('/guide/umeda-midnight-test', col_after)

            smap_xml = self.client.get('/sitemap.xml').data.decode('utf-8')
            self.assertIn('https://aiseki-navi.com/guide/umeda-midnight-test', smap_xml)

            lbl = analytics.describe_label('page_view', '/guide/umeda-midnight-test')
            self.assertIn('梅田23時半の第2波とハシゴ術', lbl['title'])

            with mock.patch('x_poster.post_to_x', return_value={'status': 'success', 'tweet_id': '999111'}) as mock_post:
                ann1 = scheduled_articles.check_and_announce_newly_published(now_jst=t_after)
                slugs_announced = [a['slug'] for a in ann1]
                self.assertIn('umeda-midnight-test', slugs_announced)
                ann2 = scheduled_articles.check_and_announce_newly_published(now_jst=t_after)
                self.assertNotIn('umeda-midnight-test', [a['slug'] for a in ann2])
                self.assertGreaterEqual(mock_post.call_count, 1)

        # 4. Delete dynamic column as Master -> returns to 404
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
        r_del = self.client.post('/api/column-editorial/delete', json={'slug': 'umeda-midnight-test'})
        self.assertEqual(r_del.status_code, 200)
        self.assertTrue(r_del.get_json()['ok'])
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.client.get('/guide/umeda-midnight-test').status_code, 404)


class AutonomousPdcaEngineTests(unittest.TestCase):
    """End-to-end closed-loop tests for the Autonomous PDCA Strategist (`/staff/pdca` & `pdca_engine.py`)."""

    def setUp(self):
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        import analytics
        import column_editorial
        import pdca_engine
        job_state.reset_cache_for_tests()
        column_editorial.clear_column_ai_cache()
        pdca_engine.reset_pdca_cache_for_tests()
        with analytics._lock:
            analytics._pending.clear()
            analytics._flushed.clear()
            analytics._sheet_rows_cache['ts'] = 0.0
            analytics._sheet_rows_cache['rows'] = []
        with app_module.data_lock:
            app_module.latest_data['full_data'] = [
                {'name': 'OLG 大阪駅前', 'women': 54, 'men': 26, 'region': 'Kinki'},
                {'name': 'JIS UMEDA', 'women': 22, 'men': 38, 'region': 'Kinki'},
                {'name': 'OLG 新宿', 'women': 36, 'men': 28, 'region': 'Kanto'},
            ]
            app_module.latest_data['is_seed_fallback'] = False
            app_module.latest_data['last_updated'] = datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=9))
            ).strftime('%Y-%m-%d %H:%M:%S')
        app_module._last_fetch_epoch = 9999999999.0
        app_module.clear_fast_http_caches()
        self.client = app_module.app.test_client()

    def tearDown(self):
        import analytics
        import column_editorial
        import pdca_engine
        with analytics._lock:
            analytics._pending.clear()
            analytics._flushed.clear()
            analytics._sheet_rows_cache['ts'] = 0.0
            analytics._sheet_rows_cache['rows'] = None
        column_editorial.clear_column_ai_cache()
        pdca_engine.reset_pdca_cache_for_tests()
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        job_state.reset_cache_for_tests()
        app_module.clear_fast_http_caches()

    def test_closed_loop_pdca_inflow_attribution_sns_boost_and_auto_replenish(self):
        import pdca_engine
        import sns_editorial
        import tips_poster
        import scheduled_articles

        # 1. Access control: anonymous & regular staff blocked; master allowed
        self.assertEqual(self.client.get('/staff/pdca').status_code, 302)
        self.assertEqual(self.client.get('/api/pdca').status_code, 401)
        self.assertEqual(self.client.post('/api/pdca/settings', json={}).status_code, 401)
        self.assertEqual(self.client.post('/api/pdca/run', json={}).status_code, 401)

        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = False
        self.assertEqual(self.client.get('/staff/pdca').status_code, 302)
        self.assertEqual(self.client.get('/api/pdca').status_code, 401)
        with self.client.session_transaction() as sess:
            sess.clear()

        # 2. Simulate real visitor arriving from X 22:00 `hub_gap` post (`/?s=x22_hub_gap_20261028`),
        #    browsing `/guide/first-10-minutes`, and clicking `/link/orbis` (24h cookie attribution!)
        human_headers = {'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15'}
        with mock.patch('sheets_store.read_rows', return_value=[]):
            r_land = self.client.get('/?s=x22_hub_gap_20261028', headers=human_headers)
            self.assertEqual(r_land.status_code, 200)
            for _ in range(10):
                self.client.get('/guide/first-10-minutes', headers=human_headers)
            r_aff = self.client.get(
                '/link/orbis',
                headers={**human_headers, 'Referer': 'https://aiseki-navi.com/guide/first-10-minutes'},
            )
            self.assertEqual(r_aff.status_code, 302)
            # Also simulate a high-PV zero-click bottleneck article (`/guide/second-venue`)
            for _ in range(16):
                self.client.get('/guide/second-venue', headers=human_headers)

            # 3. Login as Master, open `/staff/pdca`, and trigger `POST /api/pdca/run`
            with self.client.session_transaction() as sess:
                sess['is_staff'] = True
                sess['is_master'] = True

            r_board = self.client.get('/staff/pdca?days=14')
            self.assertEqual(r_board.status_code, 200)
            board_html = r_board.data.decode('utf-8')
            self.assertIn('自律PDCA作戦参謀・グロース統括ボード', board_html)

            with mock.patch('sns_editorial.get_gemini_api_key', return_value=''):
                r_run = self.client.post('/api/pdca/run', json={'days': 14})
            self.assertEqual(r_run.status_code, 200)
            run_json = r_run.get_json()
            self.assertTrue(run_json['ok'])
            diag = run_json['pdca']['diagnostics']

            # Verify `?s=` cohort normalization (`x22_hub_gap_20261028` -> `x22_hub_gap`) + attributed CV
            cohorts = {c['tag']: c for c in diag['inflow_cohorts']}
            self.assertIn('x22_hub_gap', cohorts)
            self.assertEqual(cohorts['x22_hub_gap']['visits'], 1)
            self.assertEqual(cohorts['x22_hub_gap']['conversions'], 1)

            # Verify article winner vs bottleneck detection
            art_map = {a['path']: a for a in diag['article_perf']}
            self.assertIn('高CV牽引', art_map['/guide/first-10-minutes']['status_badge'])
            self.assertTrue(any('導線ボトルネック記事' in iss['title'] for iss in diag['issues']))

            # Verify active directives persisted & wired into SNS Desk + Tips Poster + Column Editorial UI
            directives = pdca_engine.get_active_directives(allow_blocking=True)
            self.assertEqual(directives['sns_style_boosts'].get('hub_gap'), 4)
            self.assertEqual(directives['priority_column_paths'][0], '/guide/first-10-minutes')

            ed_x = sns_editorial.run_editorial_pipeline(
                app_module.latest_data['full_data'],
                slot_label='22:00',
                channel='x',
                record_history=False,
                use_ai=False,
            )
            hub_cand = next(c for c in ed_x['evaluation']['candidates'] if c['style'] == 'hub_gap')
            self.assertEqual(hub_cand['pdca_boost'], 4)
            self.assertTrue(any('PDCA参謀補正' in cm for cm in hub_cand['desk_comments']))
            self.assertIn('?s=x22_hub_gap', hub_cand['reply_text'])

            # Evening 18:00 tip slot automatically picks a PDCA priority column and appends `?s=x18_tip`
            _, chosen_tip_col = tips_poster.resolve_tip_column(slot_label='18:00')
            self.assertIn(chosen_tip_col, directives['priority_column_paths'])
            self.assertIn('?s=x18_tip', tips_poster.get_tip_reply_text(slot_label='18:00'))

            # Column Editorial Room displays the PDCA Special Order banner
            r_col_ed = self.client.get('/staff/column-editorial')
            self.assertEqual(r_col_ed.status_code, 200)
            self.assertIn('PDCA参謀の次回特命お題', r_col_ed.data.decode('utf-8'))

            # 4. Toggle `auto_replenish_columns=True` and simulate empty upcoming queue -> auto-schedules a new column!
            r_set = self.client.post('/api/pdca/settings', json={
                'auto_optimize_sns': True,
                'auto_replenish_columns': True,
            })
            self.assertEqual(r_set.status_code, 200)
            self.assertTrue(r_set.get_json()['settings']['auto_replenish_columns'])

            future_naive = datetime.datetime(2026, 11, 20, 12, 0, 0)
            with mock.patch.object(scheduled_articles, 'now_jst_naive', return_value=future_naive), \
                 mock.patch('sns_editorial.get_gemini_api_key', return_value=''):
                auto_cycle = pdca_engine.run_pdca_cycle(
                    live_stores=app_module.latest_data['full_data'],
                    days=14,
                    apply_actions=True,
                    use_ai=False,
                    record_log=True,
                )
            self.assertIsNotNone(auto_cycle['auto_published_result'])
            self.assertTrue(auto_cycle['auto_published_result']['ok'])
            auto_slug = auto_cycle['auto_published_result']['slug']
            self.assertIsNotNone(scheduled_articles.get_article_by_slug(auto_slug))


class OrientalColdStartResilienceTests(unittest.TestCase):
    """Ensure Oriental Lounge / ag stores NEVER vanish from ranking when cold-start scrape times out."""

    def test_cold_start_partial_scrape_preserves_all_53_stores_and_fast_retries(self):
        import monitor

        # 1. Resilient HTML parser handles both classic `wave-anime-wrap` and stripped `<a class="card" href="/stores/4">`
        alt_html = '''
        <html><body>
          <a class="card" href="/stores/4">
            <div class="card-title"><h4>渋谷本店</h4><span>営業中</span></div>
            <div class="counts">
              <span class="counts-num num-male">18名</span>
              <span class="counts-num num-female">32名</span>
            </div>
          </a>
        </body></html>
        '''.encode('utf-8')
        parsed = monitor._extract_oriental_stores_from_html(alt_html)
        self.assertEqual(len(parsed), 1)
        self.assertEqual(parsed[0]['name'], 'OLG 渋谷本店')
        self.assertEqual(parsed[0]['women'], 32)
        self.assertEqual(parsed[0]['men'], 18)

        # 2. Simulate cold-start (`is_seed=True`) where Oriental Lounge times out and ONLY JIS succeeds:
        #    All 53 canonical stores MUST remain in `latest_data['full_data']`, and daytime interval must drop to 60s!
        with app_module.data_lock:
            app_module.latest_data['full_data'] = [
                {'name': n, 'men': 0, 'women': 0, 'source': 'oriental', 'region': 'Kanto'}
                for n in store_master.load().keys()
            ]
            app_module.latest_data['is_seed'] = True

        jis_only = [{'name': 'JIS SHINJUKU', 'men': 8, 'women': 14, 'source': 'jis'}]
        def _fake_get_all():
            monitor.fetch_errors.clear()
            monitor.fetch_errors['get_oriental_data'] = 'ReadTimeout'
            return list(jis_only)

        jst = datetime.timezone(datetime.timedelta(hours=9))
        morning_9am = datetime.datetime(2026, 10, 28, 9, 0, tzinfo=jst)
        with mock.patch('monitor.get_all_data', side_effect=_fake_get_all):
            app_module.update_job(force=True)
            with app_module.data_lock:
                names_after = {s['name'] for s in app_module.latest_data['full_data']}
            self.assertIn('OLG 新宿', names_after)
            self.assertIn('OLG 渋谷本店', names_after)
            self.assertIn('OLG 大阪駅前', names_after)
            self.assertEqual(len(names_after), len(store_master.load()))
            # Daytime interval drops from 600s -> 60s while `get_oriental_data` is in `fetch_errors`
            self.assertEqual(app_module.fetch_interval_sec(now_jst=morning_9am), 60)

        monitor.fetch_errors.clear()
        self.assertEqual(app_module.fetch_interval_sec(now_jst=morning_9am), 600)
        with app_module.data_lock:
            app_module.latest_data['full_data'] = _fake_stores()
            app_module.latest_data['is_seed'] = False
        app_module.clear_fast_http_caches()


class SpicyAndFemaleRealToneTests(unittest.TestCase):
    """Verify the 4-mode Editorial Tone & Female Perspective system across SNS, Columns, Tips, and Templates."""

    def tearDown(self):
        import sns_editorial
        sns_editorial.set_default_editorial_tone_mode('spicy_female')
        app_module.clear_fast_http_caches()

    def test_all_four_tone_modes_across_sns_and_column_pipelines(self):
        import re
        import sns_editorial
        import column_editorial
        import x_poster

        stores = _fake_stores()
        bare_oriental_re = re.compile(r'オリエンタル(?!ラウンジ)')
        bare_olg_re = re.compile(r'\bOLG\b')

        with mock.patch('sns_editorial.get_gemini_api_key', return_value=''):
            for mode in ('spicy_female', 'spicy', 'female_real', 'standard'):
                for ch in ('x', 'threads', 'instagram'):
                    res = sns_editorial.run_editorial_pipeline(
                        stores,
                        slot_label='22:00',
                        channel=ch,
                        record_history=False,
                        use_ai=False,
                        tone_mode=mode,
                    )
                    self.assertEqual(res['tone_mode'], mode)
                    winner = res['winner']
                    self.assertIsNotNone(winner)
                    text = winner['main_text']
                    self.assertIsNone(bare_oriental_re.search(text), f"Bare オリエンタル in SNS {ch} ({mode}): {text}")
                    self.assertIsNone(bare_olg_re.search(text), f"Raw OLG in SNS {ch} ({mode}): {text}")
                    if ch == 'x':
                        self.assertLessEqual(x_poster.compute_twitter_weight(text), 280)

                for theme_key in ('area_hopping', 'female_real_voice', 'bitter_loser_habits'):
                    col_res = column_editorial.run_column_editorial_pipeline(
                        stores,
                        theme_key=theme_key,
                        use_ai=False,
                        tone_mode=mode,
                    )
                    self.assertEqual(col_res['tone_mode'], mode)
                    col_winner = col_res['winner']
                    self.assertIsNotNone(col_winner)
                    combined = "\n".join([
                        col_winner.get('title', ''),
                        col_winner.get('desc', ''),
                        col_winner.get('preview_text', ''),
                        col_winner.get('note_markdown', ''),
                        col_winner.get('x_main_text', ''),
                    ])
                    self.assertIsNone(bare_oriental_re.search(combined), f"Bare オリエンタル in column {theme_key} ({mode})")
                    self.assertIsNone(bare_olg_re.search(combined), f"Raw OLG in column {theme_key} ({mode})")
                    for sweet in column_editorial.SWEET_FANTASY_CLICHES:
                        self.assertNotIn(sweet, combined)
                    self.assertLessEqual(x_poster.compute_twitter_weight(col_winner['x_main_text']), 280)

    def test_tone_mode_api_persistence_and_editorial_ui_rendering(self):
        import sns_editorial

        client = app_module.app.test_client()
        # Unauthorized request must return 401
        r_unauth = client.post('/api/editorial/tone-mode', json={'tone_mode': 'spicy'})
        self.assertEqual(r_unauth.status_code, 401)

        with client.session_transaction() as sess:
            sess['is_master'] = True

        # Invalid mode must return 400
        r_bad = client.post('/api/editorial/tone-mode', json={'tone_mode': 'unknown_mode'})
        self.assertEqual(r_bad.status_code, 400)

        # Save 'spicy' as default and verify both editorial UIs reflect it
        r_ok = client.post('/api/editorial/tone-mode', json={'tone_mode': 'spicy'})
        self.assertEqual(r_ok.status_code, 200)
        data = r_ok.get_json()
        self.assertTrue(data['ok'])
        self.assertEqual(data['tone_mode'], 'spicy')
        self.assertEqual(sns_editorial.get_default_editorial_tone_mode(allow_blocking=True), 'spicy')

        r_sns_ui = client.get('/staff/sns-editorial?tone=female_real')
        self.assertEqual(r_sns_ui.status_code, 200)
        html_sns = r_sns_ui.get_data(as_text=True)
        self.assertIn('執筆トーン＆視点', html_sns)
        self.assertIn('female_real', html_sns)

        r_col_ui = client.get('/staff/column-editorial?theme=female_real_voice&tone=spicy_female')
        self.assertEqual(r_col_ui.status_code, 200)
        html_col = r_col_ui.get_data(as_text=True)
        self.assertIn('女性目線の本音・女子トイレ反省会', html_col)
        self.assertIn('辛口解剖・「いい人止まり」の敗因', html_col)

    def test_all_62_tips_and_rewritten_columns_hygiene(self):
        import re
        import tips_poster
        import x_poster
        import column_editorial

        bare_oriental_re = re.compile(r'オリエンタル(?!ラウンジ)')
        bare_olg_re = re.compile(r'\bOLG\b')

        self.assertGreaterEqual(len(tips_poster.TIPS_LIST), 62)
        for idx in range(len(tips_poster.TIPS_LIST)):
            for slot in ('12:00', '18:00'):
                tweet = tips_poster.get_tip_for_slot(slot_label=slot, tip_index=idx)
                w = x_poster.compute_twitter_weight(tweet)
                self.assertLessEqual(w, 280, f"Tip #{idx} ({slot}) exceeded 280 weight ({w}): {tweet}")
                self.assertIsNone(bare_oriental_re.search(tweet), f"Tip #{idx} has bare オリエンタル: {tweet}")
                self.assertIsNone(bare_olg_re.search(tweet), f"Tip #{idx} has raw OLG: {tweet}")

        audit = column_editorial.audit_existing_columns(force=True)
        self.assertEqual(audit['total_bare_oriental'], 0)

        rewritten_files = (
            'guide_local_vs_urban.html',
            'guide_cost_break_even.html',
            'guide_rain_payday.html',
            'guide_first_10min.html',
            'guide_second_venue.html',
        )
        templates_dir = os.path.join(os.path.dirname(os.path.dirname(__file__)), 'templates')
        for fname in rewritten_files:
            fpath = os.path.join(templates_dir, fname)
            with open(fpath, 'r', encoding='utf-8') as f:
                raw_html = f.read()
            self.assertIsNone(bare_oriental_re.search(raw_html), f"Bare オリエンタル in {fname}")
            for sweet in column_editorial.SWEET_FANTASY_CLICHES:
                self.assertNotIn(sweet, raw_html, f"Sweet fantasy cliché '{sweet}' still in {fname}")


class PerformanceNonBlockingRegressionTests(unittest.TestCase):
    """Regression suite guaranteeing zero synchronous Sheets/SQLite stalls on cold start and top-of-hour transitions."""

    def test_hourly_benchmark_transition_reuses_base_cache_without_sqlite_rescan(self):
        from unittest.mock import patch
        import tempfile
        import local_db

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, 'perf_bench.db')
            old_db = local_db.DB_PATH
            try:
                local_db.DB_PATH = test_db
                local_db.init_db()
                local_db.clear_store_7d_cache(clear_benchmarks=True)
                local_db.insert_logs_with_ts([
                    {'ts': '2026-10-20 21:10:00', 'name': 'OLG 新宿', 'men': 18, 'women': 30, 'source': 'oriental'},
                    {'ts': '2026-10-27 21:20:00', 'name': 'OLG 新宿', 'men': 22, 'women': 34, 'source': 'oriental'},
                    {'ts': '2026-10-20 22:10:00', 'name': 'OLG 新宿', 'men': 25, 'women': 46, 'source': 'oriental'},
                    {'ts': '2026-10-27 22:20:00', 'name': 'OLG 新宿', 'men': 27, 'women': 50, 'source': 'oriental'},
                ])
                dt_21 = datetime.datetime(2026, 10, 27, 21, 59, 0)
                dt_22 = datetime.datetime(2026, 10, 27, 22, 0, 5)

                bm_21 = local_db.get_all_stores_live_benchmarks(now_jst=dt_21, allow_blocking=False)
                self.assertEqual(bm_21['OLG 新宿']['usual_women'], 32.0)

                # Transitioning to 22:00 MUST NOT rescan SQLite; it must derive from _benchmark_base_cache in O(1)
                with patch.object(local_db, '_compute_benchmark_base_from_sqlite', side_effect=AssertionError('Must not rescan SQLite on hour rollover!')):
                    bm_22 = local_db.get_all_stores_live_benchmarks(now_jst=dt_22, allow_blocking=False)
                self.assertEqual(bm_22['OLG 新宿']['usual_women'], 48.0)
            finally:
                local_db.clear_store_7d_cache(clear_benchmarks=True)
                local_db.DB_PATH = old_db

    def test_store_analysis_avoids_sync_sheets_during_backfill_and_bounds_gviz_window(self):
        from unittest.mock import patch
        import tempfile
        import urllib.parse
        import local_db
        import store_analysis
        import logger

        with tempfile.TemporaryDirectory() as tmpdir:
            test_db = os.path.join(tmpdir, 'perf_analysis.db')
            old_db = local_db.DB_PATH
            old_sync = local_db._sync_in_progress
            old_partial = local_db._partial_recent_only
            try:
                local_db.DB_PATH = test_db
                local_db.init_db()
                now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
                local_db.insert_logs([{'name': 'OLG 新宿', 'men': 15, 'women': 28, 'source': 'oriental'}], now_str)
                local_db._partial_recent_only = True
                local_db._sync_in_progress = True

                # 1. While background sync is running and SQLite has recent rows, never hit Google Sheets synchronously
                cache_box = {}
                with patch.object(local_db, '_trigger_store_backfill_async') as mock_async_bf, \
                     patch.object(logger.http_session, 'get', side_effect=AssertionError('Must not call gviz synchronously while syncing!')):
                    payload, code = store_analysis.compute_store_analysis_payload('OLG 新宿', cache_box, cache_duration=900)
                self.assertEqual(code, 200)
                self.assertEqual(payload['store_name'], 'OLG 新宿')
                self.assertTrue(mock_async_bf.called)

                # 2. When SQLite is completely empty, fallback gviz query MUST include a 30-day date lower bound (A >= '...')
                empty_db = os.path.join(tmpdir, 'empty.db')
                local_db.DB_PATH = empty_db
                local_db.init_db()
                local_db._sync_in_progress = False
                captured_urls = []

                class _FakeResp:
                    status_code = 200
                    text = 'google.visualization.Query.setResponse({"table":{"rows":[{"c":[{"v":"2026-10-27 21:00:00"},{"v":"OLG 渋谷本店"},{"v":12},{"v":24}]}]}})'

                def _fake_get(url, headers=None, timeout=None):
                    captured_urls.append(url)
                    return _FakeResp()

                with patch.object(logger, 'get_access_token', return_value='tok'), \
                     patch.object(logger, 'get_spreadsheet_id', return_value='sid'), \
                     patch.object(logger.http_session, 'get', side_effect=_fake_get):
                    p2, c2 = store_analysis.compute_store_analysis_payload('OLG 渋谷本店', {}, cache_duration=900)
                self.assertEqual(c2, 200)
                self.assertEqual(len(captured_urls), 1)
                decoded_tq = urllib.parse.unquote(captured_urls[0])
                self.assertIn("WHERE B='OLG 渋谷本店' AND A >=", decoded_tq)
            finally:
                local_db.DB_PATH = old_db
                local_db._sync_in_progress = old_sync
                local_db._partial_recent_only = old_partial

    def test_analytics_summary_allow_blocking_false_is_instant(self):
        from unittest.mock import patch
        import analytics
        import sheets_store

        old_disabled = os.environ.get('DISABLE_SHEETS')
        try:
            os.environ.pop('DISABLE_SHEETS', None)
            with analytics._lock:
                old_cache = dict(analytics._sheet_rows_cache)
                analytics._sheet_rows_cache['ts'] = 0.0
                analytics._sheet_rows_cache['rows'] = None
            try:
                with patch.object(analytics, '_refresh_sheet_rows_async') as mock_bg:
                    res = analytics.summary(days=7, allow_blocking=False)
                self.assertIn('by_event', res)
                self.assertTrue(mock_bg.called)
            finally:
                with analytics._lock:
                    analytics._sheet_rows_cache.update(old_cache)
        finally:
            if old_disabled is not None:
                os.environ['DISABLE_SHEETS'] = old_disabled


class AffiliateAndSheetsRetentionTests(unittest.TestCase):
    """Verify (1) 5-slot affiliate URL separation & live runtime override, and (2) Google Sheets 10M-cell rotation."""

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        import affiliate_manager
        import job_state
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        os.makedirs(os.environ['JOB_STATE_DIR'], exist_ok=True)
        job_state.reset_cache_for_tests()
        affiliate_manager.reset_cache_for_tests()

    def tearDown(self):
        import affiliate_manager
        affiliate_manager.reset_cache_for_tests()

    def test_affiliate_slot_separation_and_live_override_api(self):
        import affiliate_manager

        # 1. Default state detects URL overlap between dating_instant and dating_bachelor
        init_status = affiliate_manager.get_affiliate_slots_status(allow_blocking=True)
        self.assertTrue(init_status['has_duplicates'])
        self.assertEqual(init_status['duplicate_slot_count'], 2)

        # 2. Public column pages (and scheduled template post-publish) use normalized /link/dating_instant
        for route_path in ('/guide/price-event-comparison', '/guide/male-lead-mechanics'):
            html = self.client.get(route_path).data.decode('utf-8')
            self.assertIn('/link/dating_instant', html)
        with mock.patch('scheduled_articles.is_published', return_value=True):
            html_sched = self.client.get('/guide/cost-break-even').data.decode('utf-8')
            self.assertIn('/link/dating_instant', html_sched)

        # 3. API rejects unauthenticated or non-master staff requests
        r_unauth = self.client.post('/api/affiliate/config', json={'slot_id': 'dating_instant', 'url': 'https://example.com/a'})
        self.assertEqual(r_unauth.status_code, 401)
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = False
        r_staff = self.client.post('/api/affiliate/config', json={'slot_id': 'dating_instant', 'url': 'https://example.com/a'})
        self.assertEqual(r_staff.status_code, 401)

        # 4. Master session: blocks unsafe scheme, accepts valid HTTPS ASP URL, separates slots immediately
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True

        r_bad = self.client.post('/api/affiliate/config', json={
            'slot_id': 'dating_instant',
            'url': 'javascript:alert(1)',
        })
        self.assertEqual(r_bad.status_code, 400)

        r_ok = self.client.post('/api/affiliate/config', json={
            'slot_id': 'dating_instant',
            'url': 'https://example.com/tapple-instant',
            'memo': 'タップル A8',
        })
        self.assertEqual(r_ok.status_code, 200)
        body = r_ok.get_json()
        self.assertTrue(body['ok'])
        self.assertFalse(body['aff_status']['has_duplicates'])
        self.assertEqual(body['aff_status']['duplicate_slot_count'], 0)

        # Clear staff session to test public /link/* redirects & tracking compatibility
        with self.client.session_transaction() as sess:
            sess.clear()

        r_inst = self.client.get('/link/dating_instant', headers={'User-Agent': 'Mozilla/5.0 TestUser'})
        self.assertEqual(r_inst.status_code, 302)
        self.assertEqual(r_inst.headers['Location'], 'https://example.com/tapple-instant')

        # Legacy alias /link/dating also routes to the new instant slot URL
        r_alias = self.client.get('/link/dating', headers={'User-Agent': 'Mozilla/5.0 TestUser'})
        self.assertEqual(r_alias.status_code, 302)
        self.assertEqual(r_alias.headers['Location'], 'https://example.com/tapple-instant')

        # /link/dating_bachelor remains completely independent
        r_bach = self.client.get('/link/dating_bachelor', headers={'User-Agent': 'Mozilla/5.0 TestUser'})
        self.assertEqual(r_bach.status_code, 302)
        self.assertNotEqual(r_bach.headers['Location'], 'https://example.com/tapple-instant')

        # Persists across memory cache resets via job_state
        affiliate_manager.reset_cache_for_tests()
        self.assertEqual(
            affiliate_manager.resolve_affiliate_url('dating_instant', allow_blocking=True),
            'https://example.com/tapple-instant',
        )

        # Master /staff/analytics renders both management panels cleanly
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
        r_ana = self.client.get('/staff/analytics')
        self.assertEqual(r_ana.status_code, 200)
        ana_html = r_ana.data.decode('utf-8')
        self.assertIn('id="affiliate-slot-manager"', ana_html)
        self.assertIn('id="sheets-retention-manager"', ana_html)
        self.assertIn('タップル A8', ana_html)

    def test_sheets_retention_compresses_daily_summary_and_rotates_multi_sheets(self):
        from unittest.mock import patch
        import archive_data
        import analytics
        import local_db
        import job_state

        class _FakeWorksheet:
            def __init__(self, title, rows, cols=5):
                self.title = title
                self._rows = [list(r) for r in rows]
                self.cols = cols

            @property
            def row_count(self):
                return max(len(self._rows), 1)

            @property
            def col_count(self):
                return self.cols

            def get_all_values(self):
                return [list(r) for r in self._rows]

            def append_row(self, row, value_input_option='RAW'):
                self._rows.append(list(row))

            def append_rows(self, rows, value_input_option='RAW'):
                for r in rows:
                    self._rows.append(list(r))

            def delete_rows(self, start_idx, end_idx):
                # 1-indexed inclusive deletion matching gspread Worksheet.delete_rows
                del self._rows[start_idx - 1:end_idx]

            def clear(self):
                self._rows = []

        class _FakeSpreadsheet:
            def __init__(self, ws_list):
                self._map = {w.title: w for w in ws_list}

            @property
            def sheet1(self):
                return self._map['Sheet1']

            def worksheets(self):
                return list(self._map.values())

            def worksheet(self, title):
                if title not in self._map:
                    raise KeyError(title)
                return self._map[title]

            def add_worksheet(self, title, rows=1000, cols=7):
                w = _FakeWorksheet(title, [], cols=cols)
                self._map[title] = w
                return w

        now_jst = datetime.datetime.now(archive_data.JST).replace(tzinfo=None)
        old_dt_1 = (now_jst - datetime.timedelta(days=45)).replace(hour=21, minute=10, second=0)
        old_dt_2 = (now_jst - datetime.timedelta(days=45)).replace(hour=22, minute=20, second=0)
        old_dt_late = (now_jst - datetime.timedelta(days=44)).replace(hour=1, minute=30, second=0)  # same business day!
        recent_dt = (now_jst - datetime.timedelta(days=3)).replace(hour=21, minute=0, second=0)

        ws_main = _FakeWorksheet('Sheet1', [
            ['timestamp', 'store', 'men', 'women', 'source'],
            [old_dt_1.strftime('%Y-%m-%d %H:%M:%S'), 'OLG 新宿', '18', '30', 'oriental'],
            [old_dt_2.strftime('%Y-%m-%d %H:%M:%S'), 'OLG 新宿', '22', '44', 'oriental'],
            [old_dt_late.strftime('%Y-%m-%d %H:%M:%S'), 'OLG 新宿', '20', '34', 'oriental'],
            [recent_dt.strftime('%Y-%m-%d %H:%M:%S'), 'OLG 新宿', '15', '28', 'oriental'],
        ], cols=5)

        expired_day = (now_jst - datetime.timedelta(days=140)).strftime('%Y-%m-%d')
        past_day = (now_jst - datetime.timedelta(days=5)).strftime('%Y-%m-%d')
        today_day = now_jst.strftime('%Y-%m-%d')
        ws_ana = _FakeWorksheet(analytics.SHEET_TITLE, [
            analytics.HEADER,
            [expired_day, 'page_view', '/', '12'],
            [past_day, 'page_view', '/', '10'],
            [past_day, 'page_view', '/', '25'],
            [today_day, 'page_view', '/', '7'],
        ], cols=4)

        old_vote_ts = (now_jst - datetime.timedelta(days=20)).strftime('%Y-%m-%d %H:%M:%S')
        new_vote_ts = (now_jst - datetime.timedelta(days=1)).strftime('%Y-%m-%d %H:%M:%S')
        ws_votes = _FakeWorksheet(local_db.VOTE_SHEET_TITLE, [
            ['ts', 'store_name', 'vote_type', 'voter_hash'],
            [old_vote_ts, 'OLG 新宿', 'smooth', 'h1'],
            [new_vote_ts, 'OLG 新宿', 'lively', 'h2'],
        ], cols=4)

        ws_js = _FakeWorksheet(job_state.SHEET_TITLE, [job_state.HEADER, ['k1', '2026-10-01']], cols=2)
        fake_ss = _FakeSpreadsheet([ws_main, ws_ana, ws_votes, ws_js])

        old_disabled = os.environ.get('DISABLE_SHEETS')
        try:
            os.environ.pop('DISABLE_SHEETS', None)
            with patch('archive_data.logger.get_client', return_value=object()), \
                 patch('archive_data.logger.open_spreadsheet', return_value=fake_ss, create=True):
                # Dry-run first: must NOT mutate sheets
                dry_rep = archive_data.run_full_sheets_retention(dry_run=True)
                self.assertTrue(dry_rep['ok'])
                self.assertTrue(dry_rep['dry_run'])
                self.assertEqual(len(ws_main.get_all_values()), 5)

                # Real execution: compresses 3 old rows -> 1 Daily Store Summary row & deletes old rows from Sheet1
                exec_rep = archive_data.run_full_sheets_retention(dry_run=False)
                self.assertTrue(exec_rep['ok'])
                self.assertEqual(exec_rep['details']['main_logs']['deleted_rows'], 3)
                self.assertEqual(exec_rep['details']['main_logs']['summary_rows_added'], 1)
                # Remaining Sheet1 rows = header + 1 recent row
                self.assertEqual(len(ws_main.get_all_values()), 2)
                self.assertEqual(ws_main.get_all_values()[1][3], '28')

                # Verify Daily Store Summary compressed stats: peak_women=44, peak_men=22, avg_women=36.0, samples=3
                sum_ws = fake_ss.worksheet(archive_data.SUMMARY_SHEET_NAME)
                sum_vals = sum_ws.get_all_values()
                self.assertEqual(len(sum_vals), 2)
                self.assertEqual(sum_vals[1][1], 'OLG 新宿')
                self.assertEqual(sum_vals[1][2], 44)
                self.assertEqual(sum_vals[1][3], 22)
                self.assertEqual(sum_vals[1][4], 36.0)
                self.assertEqual(sum_vals[1][6], 3)

                # Verify Analytics Daily merged past_day (10 + 25 = 35), dropped expired_day, kept today_day
                ana_vals = ws_ana.get_all_values()
                self.assertEqual(len(ana_vals), 3)  # header + past_day merged + today_day
                self.assertEqual(ana_vals[1], [past_day, 'page_view', '/', '35'])
                self.assertEqual(ana_vals[2], [today_day, 'page_view', '/', '7'])

                # Verify Crowd Votes trimmed 20d-old row and kept 1d-old row
                vote_vals = ws_votes.get_all_values()
                self.assertEqual(len(vote_vals), 2)
                self.assertEqual(vote_vals[1][2], 'lively')

                # Verify API endpoint auth & response
                self.assertEqual(self.client.post('/api/sheets/retention', json={'dry_run': True}).status_code, 401)
                with self.client.session_transaction() as sess:
                    sess['is_staff'] = True
                    sess['is_master'] = True
                api_resp = self.client.post('/api/sheets/retention', json={'dry_run': True})
                self.assertEqual(api_resp.status_code, 200)
                self.assertTrue(api_resp.get_json()['ok'])
        finally:
            if old_disabled is not None:
                os.environ['DISABLE_SHEETS'] = old_disabled


class WomenFeaturesACDETests(unittest.TestCase):
    """Verify Plans A, C, D, and E for female users (toggle/sort, /for-women SEO page, 3 women PR slots, Threads/IG alerts)."""

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()
        import affiliate_manager
        import job_state
        shutil.rmtree(os.environ['JOB_STATE_DIR'], ignore_errors=True)
        os.makedirs(os.environ['JOB_STATE_DIR'], exist_ok=True)
        job_state.reset_cache_for_tests()
        affiliate_manager.reset_cache_for_tests()
        app_module.clear_fast_http_caches()

    def tearDown(self):
        import affiliate_manager
        affiliate_manager.reset_cache_for_tests()
        app_module.clear_fast_http_caches()

    def test_plan_a_audience_toggle_and_ladies_comfort_sort_ui(self):
        pub_html = self.client.get('/').data.decode('utf-8')
        self.assertIn('id="audience-mode-bar"', pub_html)
        self.assertIn("setAudienceMode('women', true)", pub_html)
        self.assertIn('data-sort="ladies_comfort"', pub_html)
        self.assertIn('href="/for-women"', pub_html)

        # Staff mode must NOT show public audience mode bar
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = False
        try:
            staff_html = self.client.get('/staff').data.decode('utf-8')
            self.assertNotIn('id="audience-mode-bar"', staff_html)
        finally:
            with self.client.session_transaction() as sess:
                sess.clear()

        with open(os.path.join(ROOT, 'static', 'app.js'), 'r', encoding='utf-8') as f_js:
            app_js = f_js.read()
        for sym in (
            'evaluateLadiesStoreJs',
            'setAudienceMode',
            'currentAudience',
            'womenCategories',
            'womenItems',
            "setViewTheme('light', false)",
            'aiseki_men_prev_theme',
            'aiseki_women_manual_theme',
        ):
            self.assertIn(sym, app_js)

        with open(os.path.join(ROOT, 'static', 'calm.css'), 'r', encoding='utf-8') as f_css:
            calm_css = f_css.read()
        self.assertIn('.audience-mode-bar', calm_css)
        self.assertIn('body.theme-light .audience-mode-bar', calm_css)
        self.assertIn('body.theme-light.audience-women', calm_css)
        self.assertIn('aiseki_women_manual_theme', pub_html)

    def test_plan_c_for_women_live_seo_page_and_portal_links(self):
        import re
        bare_oriental_re = re.compile(r'オリエンタル(?!ラウンジ)')

        with app_module.data_lock:
            old_full = list(app_module.latest_data.get('full_data') or [])
            old_upd = app_module.latest_data.get('last_updated')
            app_module.latest_data['full_data'] = [
                {'name': 'OLG 新宿', 'women': 10, 'men': 28, 'region': 'Kanto'},
                {'name': 'JIS UMEDA', 'women': 14, 'men': 20, 'region': 'Kinki'},
                {'name': 'OLG 渋谷本店', 'women': 34, 'men': 6, 'region': 'Kanto'},
            ]
            app_module.latest_data['last_updated'] = '21:15'
        app_module.clear_fast_http_caches()

        try:
            r = self.client.get('/for-women')
            self.assertEqual(r.status_code, 200)
            self.assertIn('no-cache', r.headers.get('Cache-Control', ''))
            html = r.data.decode('utf-8')
            self.assertIn('class="theme-light audience-women"', html)
            self.assertIn('toggleForWomenTheme()', html)
            self.assertIn('オリエンタルラウンジ新宿', html)
            self.assertIn('🟢 女子待ち0・即案内圏', html)
            self.assertIn('"@type": "FAQPage"', html)
            self.assertIn('/link/women_dating_bachelor', html)
            self.assertIn('/link/women_beauty_clinic', html)
            self.assertIn('/link/women_cosme_salon', html)
            self.assertIsNone(bare_oriental_re.search(html))

            # /ladies 301 redirect
            r_leg = self.client.get('/ladies')
            self.assertEqual(r_leg.status_code, 301)
            self.assertTrue(r_leg.headers['Location'].endswith('/for-women'))

            # Sitemap & Columns index integration
            smap = self.client.get('/sitemap.xml').data.decode('utf-8')
            self.assertIn('https://aiseki-navi.com/for-women', smap)
            cols = self.client.get('/columns').data.decode('utf-8')
            self.assertIn('href="/for-women"', cols)
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = old_full
                app_module.latest_data['last_updated'] = old_upd
            app_module.clear_fast_http_caches()

    def test_plan_d_women_affiliate_slots_and_live_override(self):
        import affiliate_manager

        st = affiliate_manager.get_affiliate_slots_status(allow_blocking=True)
        self.assertEqual(st['total_slots'], 8)
        slot_ids = {s['slot_id'] for s in st['slots']}
        self.assertTrue({'women_dating_bachelor', 'women_beauty_clinic', 'women_cosme_salon'}.issubset(slot_ids))

        # Public redirect check for all 3 women canonical slots + alias
        for sid in ('women_dating_bachelor', 'women_beauty_clinic', 'women_cosme_salon', 'women_dating'):
            resp = self.client.get(f'/link/{sid}', headers={'User-Agent': 'Mozilla/5.0 Test'})
            self.assertEqual(resp.status_code, 302, sid)
        cosme_resp = self.client.get('/link/women_cosme_salon', headers={'User-Agent': 'Mozilla/5.0 Test'})
        self.assertIn('a8mat=4BERA3+6J68QA+5GZ4+5YZ75', cosme_resp.headers.get('Location', ''))

        # Master live override for women_beauty_clinic
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
        r_upd = self.client.post('/api/affiliate/config', json={
            'slot_id': 'women_beauty_clinic',
            'url': 'https://example.com/ladies-clinic-a8',
            'memo': '女性美容皮膚科 A8',
        })
        self.assertEqual(r_upd.status_code, 200)
        self.assertTrue(r_upd.get_json()['ok'])

        with self.client.session_transaction() as sess:
            sess.clear()
        r_redir = self.client.get('/link/women_beauty_clinic', headers={'User-Agent': 'Mozilla/5.0 Test'})
        self.assertEqual(r_redir.status_code, 302)
        self.assertEqual(r_redir.headers['Location'], 'https://example.com/ladies-clinic-a8')
        # Alias women_clinic follows the same canonical slot
        r_alias = self.client.get('/link/women_clinic', headers={'User-Agent': 'Mozilla/5.0 Test'})
        self.assertEqual(r_alias.headers['Location'], 'https://example.com/ladies-clinic-a8')

    def test_plan_e_ladies_threads_and_instagram_alerts(self):
        import re
        import ladies_guide
        import threads_poster
        import instagram_poster

        bare_oriental_re = re.compile(r'オリエンタル(?!ラウンジ)')
        sample_stores = [
            {'name': 'OLG 新宿', 'women': 8, 'men': 22, 'region': 'Kanto'},
            {'name': 'JIS UMEDA', 'women': 10, 'men': 18, 'region': 'Kinki'},
            {'name': 'OLG ag渋谷', 'women': 24, 'men': 6, 'region': 'Kanto'},
            {'name': 'OLG ソウル カンナム', 'women': 30, 'men': 40, 'region': 'Korea'},
        ]

        th_txt = ladies_guide.build_threads_ladies_post(sample_stores, slot_label='19:30')
        self.assertLessEqual(len(th_txt), 495)
        self.assertNotIn('OLG ', th_txt)
        self.assertIsNone(bare_oriental_re.search(th_txt))
        self.assertNotIn('カンナム', th_txt)

        ig_txt = ladies_guide.build_instagram_ladies_caption(sample_stores, slot_label='19:45')
        self.assertLessEqual(len(ig_txt), 2100)
        self.assertIn('保存', ig_txt)
        self.assertNotIn('OLG ', ig_txt)
        self.assertIsNone(bare_oriental_re.search(ig_txt))

        # Guard: low domestic male headcount (< 12) must skip both Threads and IG ladies alerts
        low_men_stores = [{'name': 'OLG 新宿', 'women': 15, 'men': 4, 'region': 'Kanto'}]
        self.assertEqual(
            threads_poster.execute_scheduled_ladies_threads_post(low_men_stores, slot_label='19:30', force=False)['status'],
            'skipped',
        )
        self.assertEqual(
            instagram_poster.execute_scheduled_ladies_instagram_post(low_men_stores, slot_label='19:45', force=False)['status'],
            'skipped',
        )

        # Success + job_state deduplication check
        with mock.patch('threads_poster.refresh_threads_token_if_needed'), \
             mock.patch('threads_poster.post_to_threads', return_value={'status': 'success', 'id': 'th_ladies_1'}) as m_th:
            res1 = threads_poster.execute_scheduled_ladies_threads_post(sample_stores, slot_label='19:30', force=False)
            self.assertEqual(res1['status'], 'success')
            self.assertEqual(res1['editorial_style'], 'threads_ladies_zero_wait')
            self.assertEqual(m_th.call_count, 1)
            # Duplicate call on same business date & slot is skipped
            res2 = threads_poster.execute_scheduled_ladies_threads_post(sample_stores, slot_label='19:30', force=False)
            self.assertEqual(res2['status'], 'skipped')

        with mock.patch('instagram_poster.refresh_ig_token_if_needed'), \
             mock.patch('instagram_poster._warm_slide_urls'), \
             mock.patch('instagram_poster.post_single_image_to_instagram', return_value={'status': 'success', 'id': 'ig_ladies_1'}) as m_ig:
            ig1 = instagram_poster.execute_scheduled_ladies_instagram_post(sample_stores, slot_label='19:45', force=False)
            self.assertEqual(ig1['status'], 'success')
            self.assertEqual(ig1['editorial_style'], 'ig_ladies_night_guide')
            self.assertEqual(m_ig.call_count, 1)
            ig2 = instagram_poster.execute_scheduled_ladies_instagram_post(sample_stores, slot_label='19:45', force=False)
            self.assertEqual(ig2['status'], 'skipped')

        # Endpoint auth check
        self.assertEqual(self.client.get('/api/trigger-ladies-sns').status_code, 401)
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
        with mock.patch('threads_poster.execute_scheduled_ladies_threads_post', return_value={'status': 'success', 'id': 'ok1'}):
            ep_resp = self.client.get('/api/trigger-ladies-sns?channel=threads&force=1')
            self.assertEqual(ep_resp.status_code, 200)
            self.assertEqual(ep_resp.get_json()['status'], 'success')

    def test_mobile_column_heading_wrapping_and_note_exporter(self):
        import re
        import column_editorial
        import scheduled_articles

        # 1. Verify mobile column heading anti-clipping rules in style.css and calm.css
        with open(os.path.join(ROOT, 'static', 'style.css'), 'r', encoding='utf-8') as f_style:
            style_css = f_style.read()
        with open(os.path.join(ROOT, 'static', 'calm.css'), 'r', encoding='utf-8') as f_calm:
            calm_css = f_calm.read()

        self.assertEqual(style_css.count('{'), style_css.count('}'))
        self.assertEqual(calm_css.count('{'), calm_css.count('}'))
        h1_block = re.search(r'(?m)^h1\s*\{([^}]*)\}', style_css)
        self.assertIsNotNone(h1_block)
        self.assertNotIn('keep-all', h1_block.group(1))
        self.assertIn('overflow-wrap: anywhere', h1_block.group(1))
        card_title_block = re.search(r'\.article-card-title\s*\{([^}]*)\}', style_css)
        self.assertIsNotNone(card_title_block)
        self.assertNotIn('-webkit-line-clamp', card_title_block.group(1))
        self.assertIn('overflow-wrap: anywhere !important;', calm_css)
        self.assertIn('.content-box h2', calm_css)

        # 2. Verify note.com export quality across ALL 18 static & scheduled columns
        bare_oriental_re = re.compile(r'オリエンタル(?!ラウンジ)')
        html_tag_re = re.compile(r'</?[a-zA-Z][^>]*>')
        all_slugs = [c['slug'] for c in column_editorial.EXISTING_STATIC_COLUMNS] + [
            c['slug'] for c in scheduled_articles.SCHEDULED_ARTICLES
        ]
        self.assertEqual(len(all_slugs), 18)
        for sl in all_slugs:
            res = column_editorial.export_column_for_note(sl)
            self.assertTrue(res.get('ok'), f"Failed exporting {sl}: {res}")
            self.assertTrue(res['title'])
            self.assertGreaterEqual(res['char_count'], 1500, f"{sl} body too short: {res['char_count']}")
            self.assertIn('\n## ', '\n' + res['body_markdown'], f"{sl} missing ## headings")
            self.assertIn('\n> ', '\n' + res['body_markdown'], f"{sl} missing > dialogue quotes")
            self.assertFalse(res['body_markdown'].startswith('# '), f"{sl} body must not duplicate # H1")
            self.assertIn('#相席ラウンジ', res['hashtags'])
            self.assertTrue(res['eyecatch_url'].startswith('https://aiseki-navi.com/static/'))
            self.assertIsNone(bare_oriental_re.search(res['full_markdown']), f"Bare オリエンタル in {sl}")
            self.assertIsNone(html_tag_re.search(res['full_markdown']), f"Raw HTML tag left in {sl}")
            self.assertNotIn('/link/', res['full_markdown'], f"Affiliate /link/ left in {sl}")
            self.assertNotIn('{%', res['full_markdown'])
            self.assertNotIn('{{', res['full_markdown'])

        # 3. Verify dynamic column export & cleanup
        ed = column_editorial.run_column_editorial_pipeline([], theme_key='female_real_voice', use_ai=False)
        saved = column_editorial.save_editorial_column(
            candidate=ed['winner'],
            publish_mode='schedule',
            publish_at_str='2026-12-01 19:00',
            custom_slug='note-export-dyn-test',
        )
        self.assertTrue(saved['ok'])
        try:
            dyn_exp = column_editorial.export_column_for_note('note-export-dyn-test')
            self.assertTrue(dyn_exp['ok'])
            self.assertFalse(dyn_exp['body_markdown'].startswith('# '))
            self.assertGreater(dyn_exp['char_count'], 500)
        finally:
            column_editorial.delete_editorial_column('note-export-dyn-test')

        # 4. Verify master-only authorization on /api/column-editorial/export-note & /columns cache isolation
        with self.client.session_transaction() as sess:
            sess.clear()
        app_module.clear_fast_http_caches()
        self.assertEqual(self.client.get('/api/column-editorial/export-note?slug=last-train-cliff').status_code, 401)
        pub_cols = self.client.get('/columns').data.decode('utf-8')
        self.assertNotIn('note貼り付け出力キットを開く', pub_cols)

        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = False
        self.assertEqual(self.client.get('/api/column-editorial/export-note?slug=last-train-cliff').status_code, 401)
        staff_cols = self.client.get('/columns').data.decode('utf-8')
        self.assertNotIn('note貼り付け出力キットを開く', staff_cols)

        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
        r_api = self.client.get('/api/column-editorial/export-note?slug=last-train-cliff')
        self.assertEqual(r_api.status_code, 200)
        self.assertTrue(r_api.get_json()['ok'])
        master_cols = self.client.get('/columns').data.decode('utf-8')
        self.assertIn('note貼り付け出力キットを開く', master_cols)
        r_board = self.client.get('/staff/column-editorial?export=for-women')
        self.assertEqual(r_board.status_code, 200)
        board_html = r_board.data.decode('utf-8')
        self.assertIn('id="note-export-panel"', board_html)
        self.assertIn('"for-women"', board_html)

    def test_sns_delivery_health_token_exchange_and_x_antispam_guards(self):
        import sns_editorial
        import threads_poster
        import bonus_alert
        import tips_poster
        import scheduled_articles
        import x_poster

        jst = datetime.timezone(datetime.timedelta(hours=9))
        sample_stores = [
            {'name': 'OLG 渋谷本店', 'women': 40, 'men': 14, 'women_diff': 5, 'region': 'Kanto'},
            {'name': 'JIS UMEDA', 'women': 28, 'men': 22, 'region': 'Kinki'},
        ]

        # 1. Failed Threads post must NOT pollute editorial history and MUST record persistent error health
        job_state.reset_cache_for_tests()
        with mock.patch('threads_poster.refresh_threads_token_if_needed'), \
             mock.patch('threads_poster.post_to_threads', return_value={'status': 'error', 'detail': 'Step1 HTTP 400: Session has expired'}):
            fail_res = threads_poster.execute_scheduled_threads_post(sample_stores, slot_label='22:00', force=False)
        self.assertEqual(fail_res['status'], 'error')
        self.assertEqual(len(sns_editorial.get_recent_editorial_history(channel='threads')), 0)
        health_after_err = sns_editorial.get_sns_delivery_health(allow_blocking=True)
        self.assertEqual(health_after_err['threads']['state'], 'error')
        self.assertIn('Session has expired', health_after_err['threads']['last_error'])

        # 2. Master-only platform token endpoint exchanges short-lived -> 60-day token, verifies /me, and clears error state
        with self.client.session_transaction() as sess:
            sess.clear()
        self.assertEqual(self.client.post('/api/sns-editorial/platform-token', json={'channel': 'threads', 'action': 'verify'}).status_code, 401)

        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True

        def _fake_requests_get(url, params=None, timeout=15):
            resp = mock.MagicMock()
            resp.status_code = 200
            if 'access_token' in url and params and params.get('grant_type') == 'th_exchange_token':
                resp.json.return_value = {'access_token': 'TH_LONG_LIVED_60D_TOKEN_1234567890', 'expires_in': 5184000}
            elif url.endswith('/me'):
                resp.json.return_value = {'id': '28848531981469633', 'username': 'aiseki_navi1'}
            else:
                resp.status_code = 400
                resp.text = 'not needed'
            return resp

        with mock.patch('threads_poster.requests.get', side_effect=_fake_requests_get):
            tok_resp = self.client.post(
                '/api/sns-editorial/platform-token',
                json={
                    'channel': 'threads',
                    'action': 'save',
                    'access_token': 'TH_SHORT_LIVED_TOKEN_ABCDEFGHIJ12345',
                    'app_secret': 'test_threads_app_secret',
                },
            )
        self.assertEqual(tok_resp.status_code, 200)
        tok_json = tok_resp.get_json()
        self.assertTrue(tok_json['ok'])
        self.assertTrue(tok_json['exchanged_long_lived'])
        self.assertEqual(job_state.get_value('token:threads'), 'TH_LONG_LIVED_60D_TOKEN_1234567890')
        self.assertEqual(tok_json['delivery_health']['threads']['state'], 'active')

        # Successful Threads post now records editorial history properly
        with mock.patch('threads_poster.refresh_threads_token_if_needed'), \
             mock.patch('threads_poster.post_to_threads', return_value={'status': 'success', 'id': 'th_ok_999'}):
            ok_res = threads_poster.execute_scheduled_threads_post(sample_stores, slot_label='22:00', force=False)
        self.assertEqual(ok_res['status'], 'success')
        self.assertEqual(len(sns_editorial.get_recent_editorial_history(channel='threads')), 1)

        # 3. Bonus alert anti-spam: 19:25 evening rush is blocked, moderate stores are ignored, rare outliers pass
        ok_1925, _ = bonus_alert.is_within_alert_window(datetime.datetime(2026, 10, 28, 19, 25, tzinfo=jst))
        self.assertFalse(ok_1925)
        moderate_stores = [{'name': 'OLG 新宿', 'women': 27, 'men': 15, 'region': 'Kanto'}]
        self.assertIsNone(bonus_alert.find_bonus_time_candidate(moderate_stores))
        self.assertIsNotNone(bonus_alert.find_bonus_time_candidate(sample_stores))

        # 4. X 48-min minimum cooldown defers column release bursts without losing the announcement
        job_state.reset_cache_for_tests()
        dt_1810 = datetime.datetime(2026, 10, 28, 18, 10, tzinfo=jst)
        x_poster.record_x_auto_post_timestamp(dt_1810)
        with mock.patch('x_poster.post_to_x', return_value={'status': 'success'}) as m_x:
            # At 19:01 (51m? Wait: test 18:45 = 35m -> deferred; then 19:05 = 55m -> posted!)
            deferred = scheduled_articles.check_and_announce_newly_published(
                now_jst=datetime.datetime(2026, 10, 28, 19, 1, 0) - datetime.timedelta(minutes=16)  # 18:45 (<48m)
            )
            # Re-test at 19:01 right after an 18:30 post (31m elapsed -> deferred)
            x_poster.record_x_auto_post_timestamp(datetime.datetime(2026, 10, 28, 18, 30, tzinfo=jst))
            deferred_1901 = scheduled_articles.check_and_announce_newly_published(
                now_jst=datetime.datetime(2026, 10, 28, 19, 1, 0)
            )
            self.assertEqual(m_x.call_count, 0)
            self.assertFalse(job_state.is_done('col_release:local-vs-urban'))

            # At 19:20 (50m elapsed >= 48m), announcement fires cleanly
            released_1920 = scheduled_articles.check_and_announce_newly_published(
                now_jst=datetime.datetime(2026, 10, 28, 19, 20, 0)
            )
            self.assertEqual(m_x.call_count, 1)
            self.assertTrue(job_state.is_done('col_release:local-vs-urban'))
            self.assertEqual(released_1920[0]['result']['status'], 'success')

        # 5. Alternating 18:00 Tips: 1 day pure standalone (no reply URL), next day 2-post thread with column reply
        job_state.reset_cache_for_tests()
        day_a = datetime.datetime(2026, 10, 28, 18, 5, tzinfo=jst)
        day_b = datetime.datetime(2026, 10, 29, 18, 5, tzinfo=jst)
        calls = []
        with mock.patch('x_poster.post_to_x', side_effect=lambda txt, reply_text=None, **kw: (calls.append((txt, reply_text)) or {'status': 'success'})):
            ra = tips_poster.run_scheduled_tip_post('18:00', now_jst=day_a)
            rb = tips_poster.run_scheduled_tip_post('18:00', now_jst=day_b)
        self.assertEqual(ra['status'], 'success')
        self.assertEqual(rb['status'], 'success')
        self.assertNotEqual(ra['standalone_no_reply'], rb['standalone_no_reply'])
        standalone_call = calls[0] if ra['standalone_no_reply'] else calls[1]
        thread_call = calls[1] if ra['standalone_no_reply'] else calls[0]
        self.assertIsNone(standalone_call[1])
        self.assertNotIn('リプ欄へ', standalone_call[0])
        self.assertIsNotNone(thread_call[1])
        self.assertIn('https://aiseki-navi.com', thread_call[1])

    def test_llmo_endpoints_robots_and_structured_data(self):
        sample_stores = [
            {'name': 'OLG 新宿', 'women': 48, 'men': 22, 'total': 70, 'region': 'Kanto'},
            {'name': 'JIS 梅田', 'women': 18, 'men': 32, 'total': 50, 'region': 'Kansai'},
        ]
        with app_module.data_lock:
            old_full = app_module.latest_data.get('full_data')
            old_upd = app_module.latest_data.get('last_updated')
            app_module.latest_data['full_data'] = sample_stores
            app_module.latest_data['last_updated'] = '2026-10-29 21:30:00'
        app_module.clear_fast_http_caches()
        try:
            # 1. /robots.txt welcomes AI search crawlers and advertises /llms.txt
            r_rob = self.client.get('/robots.txt')
            self.assertEqual(r_rob.status_code, 200)
            rob_txt = r_rob.get_data(as_text=True)
            for bot in ('OAI-SearchBot', 'ChatGPT-User', 'PerplexityBot', 'ClaudeBot', 'Google-Extended'):
                self.assertIn(f'User-agent: {bot}', rob_txt)
            self.assertIn('https://aiseki-navi.com/llms.txt', rob_txt)
            self.assertIn('https://aiseki-navi.com/llms-full.txt', rob_txt)

            # 2. /llms.txt returns live national summary + brand matrix + guide directory
            r_llms = self.client.get('/llms.txt')
            self.assertEqual(r_llms.status_code, 200)
            llms_txt = r_llms.get_data(as_text=True)
            self.assertIn('# 相席ナビ (AISEKI NAVI)', llms_txt)
            self.assertIn('オリエンタルラウンジ 新宿', llms_txt)
            self.assertIn('女性 48名 / 男性 22名', llms_txt)
            self.assertIn('https://aiseki-navi.com/llms-full.txt', llms_txt)

            # 3. /llms-full.txt includes complete 53-store directory + FAQ
            r_full = self.client.get('/llms-full.txt')
            self.assertEqual(r_full.status_code, 200)
            full_txt = r_full.get_data(as_text=True)
            self.assertIn('全国53店舗 リアルタイム人数＆個別データページ完全一覧', full_txt)
            self.assertIn('よくある質問と回答 (FAQ for AI Assistants)', full_txt)

            # 4. Homepage <head> exposes alternate markdown links & Dataset/FAQPage JSON-LD
            r_idx = self.client.get('/')
            self.assertEqual(r_idx.status_code, 200)
            idx_html = r_idx.get_data(as_text=True)
            self.assertIn('href="https://aiseki-navi.com/llms.txt"', idx_html)
            self.assertIn('"@type": "Dataset"', idx_html)
            self.assertIn('"@type": "FAQPage"', idx_html)
        finally:
            with app_module.data_lock:
                app_module.latest_data['full_data'] = old_full
                app_module.latest_data['last_updated'] = old_upd
            app_module.clear_fast_http_caches()


class SystemStabilityAndSpeedRegressionTests(unittest.TestCase):
    """Regression suite covering OLG DoH/ag-ree.jp recovery, cache_only=1 non-blocking APIs, lively vote invalidation, and staff tracking guards."""

    def setUp(self):
        app_module.app.config['TESTING'] = True
        self.client = app_module.app.test_client()

    def test_oriental_doh_and_ag_official_fallback_parsing(self):
        import monitor
        from unittest.mock import patch, MagicMock

        ag_sample_html = b"""
        <html><body>
          <a class="top_restaurant" href="/shibuya/">
            <p class="top_restaurant_ttl2">\xe6\xb8\x8b\xe8\xb0\xb7\xe5\xba\x97</p>
            <div class="customers_num"><span>18</span><span>34</span></div>
          </a>
          <a class="top_restaurant" href="/umeda/">
            <p class="top_restaurant_ttl2">\xe6\xa2\x85\xe7\x94\xb0</p>
            <div class="customers_num"><span>21</span><span>29</span></div>
          </a>
        </body></html>
        """
        mock_ag_resp = MagicMock()
        mock_ag_resp.status_code = 200
        mock_ag_resp.content = ag_sample_html
        mock_ag_resp.raise_for_status = lambda: None
        with patch('monitor.requests.get', return_value=mock_ag_resp):
            ag_stores = monitor._fetch_ag_official_fallback()
        self.assertEqual(len(ag_stores), 2)
        self.assertEqual(ag_stores[0], {'name': 'OLG ag渋谷', 'men': 18, 'women': 34, 'source': 'oriental'})
        self.assertEqual(ag_stores[1], {'name': 'OLG ag梅田', 'men': 21, 'women': 29, 'source': 'oriental'})

        # Verify DoH recovery promotes fresh stores with served_stale=False when local DNS serves 678-byte stub
        stub_resp = MagicMock()
        stub_resp.status_code = 200
        stub_resp.content = b"<html><body>403 Decommissioned Server Stub</body></html>"
        stub_resp.raise_for_status = lambda: None
        fake_sess = MagicMock()
        fake_sess.get.return_value = stub_resp
        recovered_stores = [{'name': 'OLG 新宿', 'men': 24, 'women': 46, 'source': 'oriental'}]
        with patch('monitor._get_http_session', return_value=fake_sess), \
             patch('monitor._reset_http_session', return_value=fake_sess), \
             patch('monitor.time.sleep'), \
             patch('monitor._fetch_oriental_via_doh', return_value=(recovered_stores, None)):
            out = monitor.get_oriental_data()
        self.assertEqual(out, recovered_stores)
        self.assertFalse(monitor._olg_cache.get('served_stale'))

    def test_editorial_and_pdca_cache_only_never_blocks_on_ai(self):
        import sns_editorial
        import column_editorial
        import pdca_engine
        from unittest.mock import patch

        sns_editorial.clear_editorial_ai_cache()
        column_editorial.clear_column_ai_cache()
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True

        with patch('sns_editorial.requests.post', side_effect=AssertionError('cache_only=1 must NEVER call outbound AI!')):
            r1 = self.client.get('/api/sns-editorial?channel=x&slot=20:00&cache_only=1')
            self.assertEqual(r1.status_code, 200)
            self.assertIn('winner', r1.get_json())

            r2 = self.client.get('/api/column-editorial?theme=area_hopping&cache_only=1')
            self.assertEqual(r2.status_code, 200)
            self.assertIn('winner', r2.get_json())

            r3 = self.client.get('/api/pdca?days=14&cache_only=1')
            self.assertEqual(r3.status_code, 200)
            self.assertIn('diagnostics', r3.get_json())

    def test_lively_vote_cache_invalidation_and_staff_analytics_suppression(self):
        import analytics
        from unittest.mock import patch

        sample_store = {
            'name': 'OLG 新宿',
            'men': 20,
            'women': 35,
            'region': 'Kanto',
            'source': 'oriental',
            'votes': {'smooth': 1, 'waiting': 0, 'lively': 0},
        }
        sample_store_2 = {
            'name': 'OLG 新宿',
            'men': 20,
            'women': 35,
            'region': 'Kanto',
            'source': 'oriental',
            'votes': {'smooth': 1, 'waiting': 0, 'lively': 2},
        }
        sig1 = app_module._live_data_signature([sample_store], '2026-10-29 21:00:00', False)
        sig2 = app_module._live_data_signature([sample_store_2], '2026-10-29 21:00:00', False)
        self.assertNotEqual(sig1, sig2)

        # Staff sessions must not pollute /api/track or /link/<id> conversion metrics
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = False
        with patch.object(analytics, 'track') as mock_track:
            tr_resp = self.client.post('/api/track', json={'event': 'pr_click', 'label': 'test'})
            self.assertEqual(tr_resp.status_code, 204)
            lk_resp = self.client.get('/link/orbis')
            self.assertEqual(lk_resp.status_code, 302)
            mock_track.assert_not_called()

        # IG slide cache signature ignores volatile generated_at and uses live rankings
        ctx_a = {
            'theme_key': 'fri_forecast',
            'weekend_label': '10/30(金)〜11/1(日)',
            'generated_at': '2026-10-29 21:00:01',
            'top_volume': [{'raw_name': 'OLG 新宿', 'women': 45, 'men': 25, 'avg_wpct': 64}],
            'top_anaba': [{'raw_name': 'OLG 恵比寿', 'women': 28, 'avg_wpct': 68}],
        }
        ctx_b = dict(ctx_a, generated_at='2026-10-29 21:00:59')
        ctx_c = dict(ctx_a, top_volume=[{'raw_name': 'OLG 新宿', 'women': 52, 'men': 25, 'avg_wpct': 68}])
        self.assertEqual(app_module._sns_context_sig(ctx_a), app_module._sns_context_sig(ctx_b))
        self.assertNotEqual(app_module._sns_context_sig(ctx_a), app_module._sns_context_sig(ctx_c))


if __name__ == '__main__':
    unittest.main()
