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


class ScrapeIntervalTests(unittest.TestCase):
    """営業時間外は取得間隔を落として公式サイトへの負荷を減らす"""

    def test_interval(self):
        jst = datetime.timezone(datetime.timedelta(hours=9))
        self.assertEqual(app_module.fetch_interval_sec(datetime.datetime(2026, 10, 23, 10, 0, tzinfo=jst)), 600)
        self.assertEqual(app_module.fetch_interval_sec(datetime.datetime(2026, 10, 23, 21, 0, tzinfo=jst)), 60)
        self.assertEqual(app_module.fetch_interval_sec(datetime.datetime(2026, 10, 24, 2, 0, tzinfo=jst)), 60)


class MomentumTests(unittest.TestCase):
    """30分前比の人数増減（women_diff / men_diff）計算とSSR表示の検証"""

    def test_snapshot_around_and_ssr_momentum(self):
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).replace(tzinfo=None)
        ts_30m_ago = (now_jst - datetime.timedelta(minutes=30)).strftime('%Y-%m-%d %H:%M:%S')
        local_db.insert_logs_with_ts([
            {'ts': ts_30m_ago, 'name': 'OLG 恵比寿', 'men': 20, 'women': 40, 'source': 'oriental'},
            {'ts': ts_30m_ago, 'name': 'OLG 新宿', 'men': 30, 'women': 50, 'source': 'oriental'},
        ])
        snap = local_db.get_snapshot_around(15, 50)
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


if __name__ == '__main__':
    unittest.main()
