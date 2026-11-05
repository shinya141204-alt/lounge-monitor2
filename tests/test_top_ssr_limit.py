"""トップページの SSR 縮小（上位20店舗のみサーバー描画・残りはクライアント描画）の回帰テスト。"""
import datetime
import json
import os
import re
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

_TMP = tempfile.mkdtemp(prefix='aiseki_ssr_test_')
os.environ.setdefault('DISABLE_SCHEDULER', '1')
os.environ.setdefault('DISABLE_SHEETS', '1')
os.environ.setdefault('LOCAL_DB_PATH', os.path.join(_TMP, 'test.db'))
os.environ.setdefault('JOB_STATE_DIR', os.path.join(_TMP, 'state'))
os.environ.setdefault('APP_LOG_PATH', os.path.join(_TMP, 'app.log'))
os.environ.setdefault('LATEST_STATUS_SNAPSHOT_FILE', os.path.join(_TMP, 'latest_status_cache.json'))
os.environ.setdefault('SECRET_KEY', 'test-secret-key-12345')

import app as app_module  # noqa: E402
import store_master  # noqa: E402


class TopPageSsrLimitTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        names = [n for n in store_master.load().keys() if 'ソウル' not in n][:40]
        stores = [{'name': n, 'women': 60 - i, 'men': 20, 'source': 'test',
                   'region': app_module.detect_region(n)} for i, n in enumerate(names)]
        with app_module.data_lock:
            app_module.latest_data['full_data'] = stores
            app_module.latest_data['last_updated'] = datetime.datetime.now(
                datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d %H:%M:%S')
        app_module.clear_fast_http_caches()
        with app_module._ssr_payload_cache_lock:
            app_module._ssr_payload_cache['key'] = None
        cls.client = app_module.app.test_client()
        cls.html = cls.client.get('/', headers={'User-Agent': 'Mozilla/5.0 test'}).data.decode('utf-8')
        m = re.search(r'window\.INITIAL_STATUS_DATA = (.*?);\n', cls.html)
        cls.payload = json.loads(m.group(1))

    def _ranking_list_html(self):
        start = self.html.index('id="ranking-list"')
        end = self.html.index('id="pc-store-inspector"')
        return self.html[start:end]

    def test_only_top_20_cards_are_server_rendered(self):
        self.assertEqual(len(self.payload['ranking']), 40)
        self.assertEqual(self._ranking_list_html().count('class="ranking-item'), 20)

    def test_infeed_slots_after_rank_4_and_10_still_rendered(self):
        block = self._ranking_list_html()
        self.assertIn('data-context-slot="rank4_context"', block)
        self.assertIn('data-context-slot="rank10_context"', block)
        self.assertLess(block.index('rank4_context'), block.index('rank10_context'))

    def test_all_store_links_remain_crawlable(self):
        for st in self.payload['ranking']:
            self.assertIn(f'/store/{st.get("slug")}', self.html)

    def test_deferred_names_and_client_appender_are_wired(self):
        tail_names = [s['name'] for s in self.payload['ranking'][20:]]
        self.assertTrue(tail_names)
        self.assertIn(json.dumps(tail_names[0]), self.html)
        with open(os.path.join(ROOT, 'static', 'app.js'), encoding='utf-8') as f:
            js = f.read()
        self.assertIn('function appendDeferredSsrStores', js)
        self.assertGreaterEqual(js.count('appendDeferredSsrStores(window.INITIAL_STATUS_DATA)'), 2)

    def test_client_payload_drops_server_only_fields(self):
        st = self.payload['ranking'][0]
        for k in app_module._CLIENT_STATUS_DROP_KEYS:
            self.assertNotIn(k, st)
        for k in ('name', 'women', 'men', 'ssr_area', 'ssr_event_badge', 'ssr_rank'):
            self.assertIn(k, st)


if __name__ == '__main__':
    unittest.main()
