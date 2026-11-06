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

    def test_no_black_placeholder_card_in_template_or_calm_css(self):
        """リロード時に黒塗り空枠になる .pin-placeholder 生成や .ranking-item への content-visibility: auto が存在しないこと"""
        self.assertNotIn('pin-placeholder', self.html)
        tpl_pos = self.html.index('id="ranking-item-template"')
        pinned_pos = self.html.index('id="pinned-list-container"')
        self.assertLess(tpl_pos, pinned_pos)
        with open(os.path.join(ROOT, 'static', 'calm.css'), encoding='utf-8') as f:
            calm_css = re.sub(r'/\*.*?\*/', '', f.read(), flags=re.S)
        self.assertNotRegex(
            calm_css,
            r'\.ranking-item[^{]*\{[^}]*content-visibility\s*:\s*auto',
        )

    def test_deferred_pinned_store_renders_real_card_on_first_paint_and_reload(self):
        """21位以下のお気に入り店舗をピン留めして / および /staff をリロードした際、app.js実行前の初回ペイント段階から黒箱ゼロで実カードが表示されること"""
        from urllib.parse import urlparse
        from playwright.sync_api import sync_playwright

        deferred_target = self.payload['ranking'][25]
        target_name = deferred_target['name']
        target_women = str(deferred_target['women'])
        target_men = str(deferred_target['men'])
        base = 'http://aiseki.local'

        def attach_router(ctx, client, block_app_js=False):
            def handle(route):
                req = route.request
                u = urlparse(req.url)
                if u.netloc != 'aiseki.local':
                    route.abort()
                    return
                if block_app_js and u.path == '/static/app.js':
                    route.abort()
                    return
                path_qs = u.path + (('?' + u.query) if u.query else '')
                headers = {k: v for k, v in req.headers.items() if k.lower() not in ('host', 'content-length', 'cookie')}
                resp = client.open(path_qs, method=req.method.upper(), headers=headers, data=req.post_data_buffer, follow_redirects=True)
                resp_headers = {k: v for k, v in resp.headers.items() if k.lower() not in ('content-encoding', 'transfer-encoding', 'content-length')}
                route.fulfill(status=resp.status_code, headers=resp_headers, body=resp.get_data())
                resp.close()
            ctx.route('**/*', handle)

        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                # 1) First-paint check BEFORE app.js runs (block_app_js=True) on both public `/` and `/staff`
                with self.client.session_transaction() as sess:
                    sess['is_staff'] = True
                    sess['is_master'] = True
                    sess['auth_gen'] = app_module._compute_session_auth_gen(True)
                app_module.clear_fast_http_caches()

                ctx_fp = browser.new_context(viewport={'width': 1280, 'height': 900})
                ctx_fp.add_init_script(
                    f"localStorage.setItem('olg_pinned_stores', {json.dumps(json.dumps([target_name]))});"
                )
                attach_router(ctx_fp, self.client, block_app_js=True)
                fp_page = ctx_fp.new_page()
                for route_path in ('/', '/staff'):
                    fp_page.goto(base + route_path, wait_until='domcontentloaded')
                    fp_page.reload(wait_until='domcontentloaded')
                    state = fp_page.evaluate("""() => {
                        const cont = document.getElementById('pinned-list-container');
                        const cards = Array.from(document.querySelectorAll('#pinned-list .ranking-item'));
                        return {
                            hidden: cont ? cont.classList.contains('hidden') : true,
                            placeholderCount: document.querySelectorAll('.pin-placeholder').length,
                            hydratedFlag: Boolean(window.__SSR_PINNED_HYDRATED),
                            cardCount: cards.length,
                            dataStore: cards[0]?.getAttribute('data-store') || '',
                            storeTitle: cards[0]?.querySelector('.store-name')?.innerText?.trim() || '',
                            ladies: cards[0]?.querySelector('.stat.ladies .count')?.innerText?.trim() || '',
                            men: cards[0]?.querySelector('.stat.men .count')?.innerText?.trim() || '',
                        };
                    }""")
                    self.assertFalse(state['hidden'], f"Pinned section hidden on {route_path}")
                    self.assertEqual(state['placeholderCount'], 0, f"Black placeholder found on {route_path}")
                    self.assertTrue(state['hydratedFlag'], f"SSR pinned hydration flag missing on {route_path}")
                    self.assertEqual(state['cardCount'], 1)
                    self.assertEqual(state['dataStore'], target_name)
                    self.assertTrue(len(state['storeTitle']) > 0, f"Empty store title on {route_path}")
                    self.assertEqual(state['ladies'], target_women)
                    self.assertEqual(state['men'], target_men)
                ctx_fp.close()

                # 2) Full load + reload check WITH app.js active (ensures zero TDZ/page errors, zero duplicate cards & zero empty cards)
                ctx_full = browser.new_context(viewport={'width': 1280, 'height': 900})
                ctx_full.add_init_script(
                    f"localStorage.setItem('olg_pinned_stores', {json.dumps(json.dumps([target_name]))});"
                )
                attach_router(ctx_full, self.client, block_app_js=False)
                full_page = ctx_full.new_page()
                js_errors = []
                full_page.on('pageerror', lambda err: js_errors.append(str(err)))
                full_page.on(
                    'console',
                    lambda msg: js_errors.append(msg.text) if msg.type == 'error' and 'restoreCachedStatus' in msg.text else None,
                )
                for route_path in ('/', '/staff'):
                    full_page.goto(base + route_path, wait_until='domcontentloaded')
                    full_page.reload(wait_until='domcontentloaded')
                    full_page.wait_for_timeout(120)
                    full_state = full_page.evaluate("""() => {
                        const pinnedCards = Array.from(document.querySelectorAll('#pinned-list .ranking-item'));
                        const rankCards = Array.from(document.querySelectorAll('#ranking-list .ranking-item'));
                        const emptyCards = [...pinnedCards, ...rankCards].filter(
                            c => !(c.querySelector('.store-name')?.innerText?.trim())
                        ).length;
                        return {
                            pinnedCount: pinnedCards.length,
                            rankCount: rankCards.length,
                            emptyCards,
                            pinnedStore: pinnedCards[0]?.getAttribute('data-store') || '',
                            inRankList: rankCards.some(c => c.getAttribute('data-store') === (pinnedCards[0]?.getAttribute('data-store') || '')),
                        };
                    }""")
                    self.assertEqual(js_errors, [], f"JS errors on {route_path}: {js_errors}")
                    self.assertEqual(full_state['emptyCards'], 0, f"Empty card detected on {route_path}")
                    self.assertEqual(full_state['pinnedCount'], 1)
                    self.assertEqual(full_state['rankCount'], 39)
                    self.assertEqual(full_state['pinnedStore'], target_name)
                    self.assertFalse(full_state['inRankList'])
                ctx_full.close()
            finally:
                with self.client.session_transaction() as sess:
                    sess.clear()
                app_module.clear_fast_http_caches()
                browser.close()


if __name__ == '__main__':
    unittest.main()
