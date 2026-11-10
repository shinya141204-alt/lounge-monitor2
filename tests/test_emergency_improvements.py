"""
tests/test_emergency_improvements.py - Phase 2 緊急改善項目の網羅的単体テストスイート

以下の改善項目を検証：
1. app.py: price_comparison_page のキャッシュキーがエリア別動的（price_comp_{selected_area}）になっていること
2. app.py: robots.txt に Disallow: /link/ が含まれ、アフィリエイト転送URLのクロールが遮断されていること
3. app.py: /link/<item_id> のリダイレクトレスポンスヘッダーに X-Robots-Tag: noindex, nofollow が付与されていること
4. affiliate_manager.py & app.py: 女性向けスロット（women_dating_bachelor, women_beauty_clinic）が男性用URLではなく正規の女性向け/公式URLにリダイレクトされること
5. templates/grooming.html: 景表法ステマ規制PR注記、PRバッジ、A8.net計測ピクセル（0.gif）が正しく配置されていること
6. static/app.js: renderGuideModal() のカードにPRバッジが含まれていること
7. templates/store_detail.html: LocalBusiness / BarOrPub の JSON-LD 構造化データが正しく出力されること
8. キャッシュバスティング同期: app.js?v=20261104_2, sw.js?v=86, aiseki-navi-v86 が完全同期していること
"""

import json
import os
import unittest
from unittest import mock
import urllib.parse
from bs4 import BeautifulSoup

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

import app as app_module
import affiliate_manager
import price_comparison
import store_master


class TestEmergencyImprovements(unittest.TestCase):
    def setUp(self):
        self.app = app_module.app
        self.app.config['TESTING'] = True
        self.client = self.app.test_client()
        app_module.clear_fast_http_caches()

    def tearDown(self):
        app_module.clear_fast_http_caches()

    # ------------------------------------------------------------------
    # 1. 料金比較ページのキャッシュキー動的化テスト
    # ------------------------------------------------------------------
    def test_price_comparison_cache_key_area_isolation(self):
        """price-comparison が ?area=shinjuku と ?area=shibuya で独立してレンダリング・キャッシュされること"""
        app_module.clear_fast_http_caches()
        # Shinjuku
        res_shinjuku = self.client.get('/price-comparison?area=shinjuku')
        self.assertEqual(res_shinjuku.status_code, 200)
        html_shinjuku = res_shinjuku.data.decode('utf-8')
        self.assertIn('shinjuku', html_shinjuku)

        # Shibuya
        res_shibuya = self.client.get('/price-comparison?area=shibuya')
        self.assertEqual(res_shibuya.status_code, 200)
        html_shibuya = res_shibuya.data.decode('utf-8')
        self.assertIn('shibuya', html_shibuya)

        # Confirm default fallback to shinjuku for invalid area
        res_invalid = self.client.get('/price-comparison?area=unknown_xyz')
        self.assertEqual(res_invalid.status_code, 200)
        html_invalid = res_invalid.data.decode('utf-8')
        self.assertIn('shinjuku', html_invalid)

    # ------------------------------------------------------------------
    # 2. robots.txt の Disallow: /link/ 遮断テスト
    # ------------------------------------------------------------------
    def test_robots_txt_disallows_affiliate_link_redirects(self):
        """robots.txt に Disallow: /link/ が含まれていること"""
        resp = self.client.get('/robots.txt')
        self.assertEqual(resp.status_code, 200)
        txt = resp.data.decode('utf-8')
        self.assertIn('Disallow: /link/\n', txt)
        self.assertIn('Disallow: /staff\n', txt)

    def test_robots_txt_ai_bot_groups_also_block_link_and_private_api(self):
        """AIクローラー専用グループにも /link/ と管理APIの除外が入っていること（グループは個別指定が優先されるため）"""
        txt = self.client.get('/robots.txt').data.decode('utf-8')
        groups = [g for g in txt.split('\n\n') if g.startswith('User-agent: ') and not g.startswith('User-agent: *')]
        self.assertGreaterEqual(len(groups), 10)
        for g in groups:
            self.assertIn('Disallow: /link/', g, g.splitlines()[0])
            self.assertIn('Disallow: /api/trigger-', g, g.splitlines()[0])
            self.assertIn('Disallow: /staff', g, g.splitlines()[0])
        self.assertNotIn('Disallow: /api/llms-facts.json', txt)

    def test_ai_user_agents_are_not_counted_as_human_clicks(self):
        """ChatGPT-User 等の 'bot' を含まないAIフェッチャーもボット判定されること"""
        uas = [
            'Mozilla/5.0 AppleWebKit/537.36 (KHTML, like Gecko); compatible; ChatGPT-User/1.0; +https://openai.com/bot',
            'Mozilla/5.0 (compatible; Perplexity-User/1.0)',
            'Mozilla/5.0 (compatible; Claude-User/1.0)',
            'meta-externalfetcher/1.1',
        ]
        for ua in uas:
            with self.app.test_request_context('/link/orbis', headers={'User-Agent': ua}):
                self.assertTrue(app_module._is_bot_request(), ua)
        human = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
        with self.app.test_request_context('/link/orbis', headers={'User-Agent': human}):
            self.assertFalse(app_module._is_bot_request())

    def test_query_passcode_login_is_disabled(self):
        """?passcode= のURLログインは廃止され、セッションが付与されないこと"""
        resp = self.client.get(f'/staff?passcode={app_module.MASTER_PASSCODE}')
        self.assertIn(resp.status_code, (301, 302))
        with self.client.session_transaction() as s:
            self.assertFalse(s.get('is_staff'))
            self.assertFalse(s.get('is_master'))

    def test_admin_secret_accepted_via_header(self):
        """管理トークンは X-Admin-Secret ヘッダーでも認証できること"""
        with self.app.test_request_context('/api/x', headers={'X-Admin-Secret': app_module.ADMIN_API_SECRET}):
            self.assertTrue(app_module.is_master_authorized())
        with self.app.test_request_context('/api/x', headers={'X-Admin-Secret': 'wrong-value'}):
            self.assertFalse(app_module.is_master_authorized())

    def test_baseline_security_headers(self):
        """公開ページに nosniff / X-Frame-Options / Referrer-Policy が付き、HTTPS時のみHSTSが付くこと"""
        resp = self.client.get('/privacy')
        self.assertEqual(resp.headers.get('X-Content-Type-Options'), 'nosniff')
        self.assertEqual(resp.headers.get('X-Frame-Options'), 'SAMEORIGIN')
        self.assertEqual(resp.headers.get('Referrer-Policy'), 'strict-origin-when-cross-origin')
        self.assertIsNone(resp.headers.get('Strict-Transport-Security'))
        resp_https = self.client.get('/privacy', base_url='https://aiseki-navi.com')
        self.assertIn('max-age=', resp_https.headers.get('Strict-Transport-Security', ''))

    def test_production_never_uses_public_default_credentials(self):
        """本番（FLY_APP_NAMEあり）で未設定の機密値は公開済みの既定値ではなくランダム値になること"""
        from unittest.mock import patch
        with patch.dict(os.environ, {'FLY_APP_NAME': 'aiseki-navi-tokyo'}, clear=False):
            os.environ.pop('MASTER_PASSCODE_TEST_UNSET', None)
            v1 = app_module._load_secure_config('MASTER_PASSCODE_TEST_UNSET', '2918')
            v2 = app_module._load_secure_config('MASTER_PASSCODE_TEST_UNSET', '2918')
        self.assertNotEqual(v1, '2918')
        self.assertNotEqual(v1, v2)
        self.assertGreaterEqual(len(v1), 40)

    # ------------------------------------------------------------------
    # 3. /link/<item_id> の X-Robots-Tag ヘッダー付与テスト
    # ------------------------------------------------------------------
    def test_affiliate_redirect_sets_x_robots_tag(self):
        """/link/ リダイレクトレスポンスに X-Robots-Tag: noindex, nofollow が付与されること"""
        for item_id in ('hair_removal', 'orbis', 'dating_instant', 'women_dating_bachelor', 'night_care_supplement'):
            resp = self.client.get(f'/link/{item_id}')
            self.assertEqual(resp.status_code, 302, f"Failed for {item_id}")
            self.assertEqual(resp.headers.get('X-Robots-Tag'), 'noindex, nofollow', f"Missing X-Robots-Tag for {item_id}")

    # ------------------------------------------------------------------
    # 4. 女性向けアフィリエイトURLの正常化テスト
    # ------------------------------------------------------------------
    def test_women_affiliate_slots_redirect_to_proper_female_destinations(self):
        """women_dating_bachelor と women_beauty_clinic が男性用ヒゲ脱毛/男性アプリではなく正規URLへ遷移すること"""
        # women_dating_bachelor
        resp_dating = self.client.get('/link/women_dating_bachelor')
        self.assertEqual(resp_dating.status_code, 302)
        loc_dating = resp_dating.headers.get('Location', '')
        self.assertIn('bachelorapp.net', loc_dating)
        # Verify it is NOT the male instant dating A8 link
        self.assertNotIn('4BEK9C+21TRSI+5SFC+61RI9', loc_dating)

        # women_beauty_clinic
        resp_clinic = self.client.get('/link/women_beauty_clinic')
        self.assertEqual(resp_clinic.status_code, 302)
        loc_clinic = resp_clinic.headers.get('Location', '')
        self.assertIn('s-b-c.net', loc_clinic)
        # Verify it is NOT the men's beard removal A8 link
        self.assertNotIn('4BEL1F+61B8KY+328C+2HCB1D', loc_clinic)

    # ------------------------------------------------------------------
    # 5. grooming.html の景表法PR表記およびA8計測ピクセルテスト
    # ------------------------------------------------------------------
    def test_grooming_pr_compliance_and_tracking_pixels(self):
        """grooming.html にプロモーション表記、PRバッジ、A8.net 0.gif ピクセルが設置されていること"""
        app_module.clear_fast_http_caches()
        resp = self.client.get('/grooming')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        # 1. 景表法プロモーション注記
        self.assertIn('本ページにはプロモーション（広告）が含まれます', html)

        # 2. PRバッジ
        soup = BeautifulSoup(html, 'html.parser')
        pr_badges = soup.find_all('span', class_='pr-badge')
        self.assertGreaterEqual(len(pr_badges), 6, "Expected at least 6 PR badges for recommended cards")
        for badge in pr_badges:
            self.assertEqual(badge.get_text(strip=True), 'PR')

        # 3. A8.net 計測ピクセル (0.gif)
        self.assertIn('https://www17.a8.net/0.gif?a8mat=4BEL1F+61B8KY+328C+2HCB1D', html)  # hair_removal
        self.assertIn('https://www18.a8.net/0.gif?a8mat=4BEL1F+67V08I+1USQ+2Z90WX', html)  # orbis
        self.assertIn('https://www15.a8.net/0.gif?a8mat=4BEK9C+21TRSI+5SFC+61RI9', html)  # dating_instant
        self.assertIn('https://www19.a8.net/0.gif?a8mat=4BELTJ+7RTR3M+3H28+5ZU29', html)  # night_care_supplement
        self.assertIn('https://www15.a8.net/0.gif?a8mat=4BGDZ4+9R9ROY+1OK+NVP2P', html)  # second_venue_reservation (ikyu)

    # ------------------------------------------------------------------
    # 6. static/app.js の renderGuideModal() PRバッジ表記テスト
    # ------------------------------------------------------------------
    def test_app_js_render_guide_modal_contains_pr_badge(self):
        """static/app.js の renderGuideModal() に PR バッジ定義が含まれていること"""
        js_path = os.path.join(PROJECT_ROOT, 'static', 'app.js')
        with open(js_path, 'r', encoding='utf-8') as f:
            js_content = f.read()
        self.assertIn('<span class="guide-app-badge pr-badge"', js_content)
        self.assertIn('>PR</span>', js_content)

    # ------------------------------------------------------------------
    # 7. templates/store_detail.html の LocalBusiness 構造化データテスト
    # ------------------------------------------------------------------
    def test_store_detail_local_business_schema(self):
        """store_detail_page のレスポンスに LocalBusiness / BarOrPub の JSON-LD が含まれ有効なJSONであること"""
        app_module.clear_fast_http_caches()
        # Use an existing slug e.g. oriental-lounge-shibuya-honten
        resp = self.client.get('/store/oriental-lounge-shibuya-honten')
        self.assertEqual(resp.status_code, 200)
        html = resp.data.decode('utf-8')

        soup = BeautifulSoup(html, 'html.parser')
        scripts = soup.find_all('script', type='application/ld+json')
        self.assertTrue(len(scripts) > 0, "No JSON-LD script found")

        found_local_biz = False
        for s in scripts:
            try:
                data = json.loads(s.string)
                if isinstance(data, list):
                    for item in data:
                        t = item.get('@type')
                        if t == ['LocalBusiness', 'BarOrPub'] or t in ('LocalBusiness', 'BarOrPub'):
                            found_local_biz = True
                            self.assertIn('オリエンタルラウンジ', item.get('name', ''))
                            self.assertEqual(item.get('priceRange'), '男性 2,640円〜 / 女性 無料')
                            self.assertIn('address', item)
                            self.assertIn('openingHoursSpecification', item)
                            self.assertIsInstance(item['openingHoursSpecification'], list)
                            self.assertTrue(len(item['openingHoursSpecification']) > 0)
                            for oh_spec in item['openingHoursSpecification']:
                                self.assertEqual(oh_spec.get('@type'), 'OpeningHoursSpecification')
                                self.assertIn('dayOfWeek', oh_spec)
                                self.assertIn('opens', oh_spec)
                                self.assertIn('closes', oh_spec)
                            if 'geo' in item:
                                self.assertIn('latitude', item['geo'])
                                self.assertIn('longitude', item['geo'])
            except json.JSONDecodeError as e:
                self.fail(f"Invalid JSON in JSON-LD script: {e}\n{s.string}")

        self.assertTrue(found_local_biz, "LocalBusiness / BarOrPub schema not found in store_detail.html")

    # ------------------------------------------------------------------
    # 8. キャッシュバスティング同期テスト
    # ------------------------------------------------------------------
    def test_cache_busting_sync_app_js_v2_and_sw_v86(self):
        """app.js?v=20261110_1 と sw.js?v=109, CACHE_NAME aiseki-navi-v109 が同期していること"""
        home_resp = self.client.get('/')
        self.assertEqual(home_resp.status_code, 200)
        home_html = home_resp.data.decode('utf-8')
        home_resp.close()
        self.assertIn('/static/app.js?v=20261110_1', home_html)
        self.assertIn('/sw.js?v=109', home_html)

        sw_resp = self.client.get('/sw.js')
        self.assertEqual(sw_resp.status_code, 200)
        sw_txt = sw_resp.data.decode('utf-8')
        sw_resp.close()
        self.assertIn("CACHE_NAME = 'aiseki-navi-v109'", sw_txt)
        self.assertIn("'/static/app.js?v=20261110_1'", sw_txt)

    def test_store_hours_opening_hours_specification(self):
        """store_hours.get_store_opening_hours_specification の生成仕様テスト"""
        import store_hours
        spec_shinjuku = store_hours.get_store_opening_hours_specification("OLG 新宿")
        self.assertIsInstance(spec_shinjuku, list)
        self.assertTrue(len(spec_shinjuku) > 0)
        self.assertEqual(spec_shinjuku[0]['@type'], 'OpeningHoursSpecification')
        self.assertEqual(spec_shinjuku[0]['opens'], '19:00')
        self.assertEqual(spec_shinjuku[0]['closes'], '05:00')
        self.assertIn('Monday', spec_shinjuku[0]['dayOfWeek'])

        # 定休日がある店舗（OLG 宇都宮: 水曜定休）
        spec_utsunomiya = store_hours.get_store_opening_hours_specification("OLG 宇都宮")
        all_days = []
        for s in spec_utsunomiya:
            all_days.extend(s['dayOfWeek'])
        self.assertNotIn('Wednesday', all_days)
        self.assertIn('Monday', all_days)
        self.assertIn('Friday', all_days)

    # ------------------------------------------------------------------
    # 9. Phase C 技術的負債解消（Gunicorn 23 / google-auth / Blueprint分割）
    # ------------------------------------------------------------------
    def test_phase_c_dependencies_and_oauth2client_removal(self):
        """requirements.txt が gunicorn>=23・google-auth へ更新され、本番コードから oauth2client が撤廃されていること"""
        req_path = os.path.join(PROJECT_ROOT, 'requirements.txt')
        with open(req_path, 'r', encoding='utf-8') as f:
            req_txt = f.read()
        self.assertIn('gunicorn>=23', req_txt)
        self.assertIn('google-auth', req_txt)
        self.assertNotIn('oauth2client', req_txt)

        offending = []
        for root_dir, dirs, files in os.walk(PROJECT_ROOT):
            dirs[:] = [d for d in dirs if d not in ('.git', '__pycache__', 'venv', '.venv', 'tests', '.agents')]
            for fn in files:
                if not fn.endswith('.py'):
                    continue
                fp = os.path.join(root_dir, fn)
                with open(fp, 'r', encoding='utf-8') as pf:
                    src = pf.read()
                if 'oauth2client' in src:
                    offending.append(os.path.relpath(fp, PROJECT_ROOT))
        self.assertEqual(offending, [], f"oauth2client still referenced in: {offending}")

    def test_phase_c_app_py_slimmed_and_blueprints_registered(self):
        """app.py が 2,800行以下に分割され、4つのドメイン Blueprint が登録されていること"""
        app_path = os.path.join(PROJECT_ROOT, 'app.py')
        with open(app_path, 'r', encoding='utf-8') as f:
            line_count = sum(1 for _ in f)
        self.assertLessEqual(line_count, 2800, f"Expected app.py <= 2800 lines, got {line_count}")

        for bp_name in ('portal_bp', 'seo_feeds_bp', 'staff_bp', 'sns_jobs_bp'):
            self.assertIn(bp_name, self.app.blueprints, f"Missing blueprint: {bp_name}")

    # ------------------------------------------------------------------
    # 10. Claude Opus 5.5 厳格監査の指摘修正に対する回帰テスト群
    # ------------------------------------------------------------------
    def test_claude_audit_query_secret_rejected_header_only(self):
        """?secret= クエリでの管理認証が完全に廃止され、X-Admin-Secret ヘッダーのみ有効であること"""
        with self.app.test_request_context(f'/api/debug_ps?secret={app_module.ADMIN_API_SECRET}'):
            self.assertFalse(app_module.is_master_authorized())
            self.assertFalse(app_module.is_admin_authorized())
        with self.app.test_request_context('/api/debug_ps', headers={'X-Admin-Secret': app_module.ADMIN_API_SECRET}):
            self.assertTrue(app_module.is_master_authorized())

    def test_claude_audit_trigger_csrf_blocks_get_and_cross_origin_post(self):
        """マスターセッションでも GET /api/trigger-* は 405、Cross-Origin POST は 403 で遮断されること"""
        with self.client.session_transaction() as sess:
            sess['is_staff'] = True
            sess['is_master'] = True
            sess['auth_gen'] = app_module._compute_session_auth_gen(True)
        # GET link navigation CSRF blocked with 405
        self.assertEqual(self.client.get('/api/trigger-x-post?force=1').status_code, 405)
        # Cross-origin POST form CSRF blocked with 403
        self.assertEqual(
            self.client.post('/api/trigger-x-post?force=1', headers={'Origin': 'https://evil.example.com'}).status_code,
            403,
        )
        self.assertEqual(
            self.client.post('/api/trigger-x-post?force=1', headers={'Sec-Fetch-Site': 'cross-site'}).status_code,
            403,
        )

    def test_claude_audit_ogp_live_random_s_param_hits_slot_cache(self):
        """/ogp/live.png?s=<乱数> を連打しても Pillow 再描画が走らず正規化スロットキャッシュにヒットすること"""
        from unittest.mock import patch
        import ogp_generator
        with app_module._ogp_bytes_cache_lock:
            app_module._ogp_bytes_cache.clear()
        calls = []

        def _fake_png(stores, s_param='', now_jst=None):
            calls.append(s_param)
            return b'\x89PNG\r\n\x1a\nFAKE_OGP_PNG'

        with patch.object(ogp_generator, 'generate_live_ogp_png', side_effect=_fake_png):
            r1 = self.client.get('/ogp/live.png?s=random_attack_001')
            r2 = self.client.get('/ogp/live.png?s=random_attack_002')
            r3 = self.client.get('/ogp/live.png?s=random_attack_003')
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r2.status_code, 200)
        self.assertEqual(r3.status_code, 200)
        self.assertEqual(len(calls), 1, f"Expected 1 Pillow render across random ?s= params, got {len(calls)}")

    def test_claude_audit_session_7d_and_passcode_rotation_revokes_cookie(self):
        """セッション寿命が7日間であり、パスコード変更時に既存ログインセッションが即時失効すること"""
        import datetime
        from unittest.mock import patch
        self.assertEqual(app_module.app.permanent_session_lifetime, datetime.timedelta(days=7))

        r_login = self.client.post('/staff', data={'passcode': app_module.MASTER_PASSCODE})
        self.assertEqual(r_login.status_code, 302)
        self.assertEqual(self.client.get('/api/debug_ps').status_code, 200)

        # Rotate MASTER_PASSCODE -> existing cookie is immediately revoked on next request
        with patch.object(app_module, 'MASTER_PASSCODE', '9999_rotated_new'):
            self.assertEqual(self.client.get('/api/debug_ps').status_code, 401)

    def test_claude_audit_shared_bus_rate_limit_and_distributed_lock(self):
        """turbo_store の共有レート制限と分散 cron ロックが全ワーカー共通で機能すること"""
        import turbo_store
        turbo_store.enable_memory_bus_for_tests(True)
        try:
            for _ in range(3):
                self.assertTrue(turbo_store.check_sliding_rate_limit('test_ns', '198.51.100.7', limit=3, window_sec=60))
            self.assertFalse(turbo_store.check_sliding_rate_limit('test_ns', '198.51.100.7', limit=3, window_sec=60))

            self.assertTrue(turbo_store.acquire_distributed_lock('cron:test_slot', ttl_sec=30))
            self.assertFalse(turbo_store.acquire_distributed_lock('cron:test_slot', ttl_sec=30))
        finally:
            turbo_store.reset_for_tests()

    def test_claude_audit_dockerfile_non_root_and_sw_503_and_stale_cap(self):
        """Dockerfile が非root USER で実行され、sw.js がオフライン 503 と 10分キャッシュ上限を持つこと"""
        df_path = os.path.join(PROJECT_ROOT, 'Dockerfile')
        with open(df_path, 'r', encoding='utf-8') as f:
            df_txt = f.read()
        self.assertIn('USER appuser', df_txt)
        self.assertNotIn('|| true', df_txt)

        sw_path = os.path.join(PROJECT_ROOT, 'static', 'sw.js')
        with open(sw_path, 'r', encoding='utf-8') as f:
            sw_txt = f.read()
        self.assertIn('status: 503', sw_txt)
        self.assertIn('LIVE_MAX_STALE_MS = 600000', sw_txt)

    def test_claude_audit_seo_eeat_store_detail_and_security_headers(self):
        """CSP / Permissions-Policy / HSTS includeSubDomains、about の sameAs、store_detail の固定title・FAQPage撤去・main要素を検証"""
        r_about = self.client.get('/about', base_url='https://aiseki-navi.com')
        self.assertEqual(r_about.status_code, 200)
        self.assertIn('includeSubDomains', r_about.headers.get('Strict-Transport-Security', ''))
        self.assertIn("frame-ancestors 'self'", r_about.headers.get('Content-Security-Policy', ''))
        self.assertIn('camera=()', r_about.headers.get('Permissions-Policy', ''))
        about_html = r_about.data.decode('utf-8')
        self.assertIn('<main>', about_html)
        self.assertIn('"sameAs"', about_html)
        self.assertIn('編集長：くわもん', about_html)
        self.assertNotIn('Kuwano', about_html)
        self.assertIn('JIS（ジス）', about_html)
        self.assertIn('ASOBIBAR（アソビバー）', about_html)

        app_module.clear_fast_http_caches()
        r_store = self.client.get('/store/oriental-lounge-shinjuku')
        self.assertEqual(r_store.status_code, 200)
        st_html = r_store.data.decode('utf-8')
        self.assertIn('<main>', st_html)
        self.assertNotIn('"@type": "FAQPage"', st_html)
        soup = BeautifulSoup(st_html, 'html.parser')
        self.assertNotIn('現在：女性', soup.title.string or '')

    def test_owner_ops_governance_suite(self):
        """オーナー運用4項目（24h対応方針、店舗/ブランド即時非表示スイッチ、スタッフ強制ログアウト＆パスコード変更、受信箱＆SSRF防止Webhook）の総合検証"""
        import tempfile
        from unittest.mock import patch
        import contact_form
        import job_state
        import ops_governance

        with tempfile.TemporaryDirectory() as tmp_js_dir, \
             patch.dict(os.environ, {'JOB_STATE_DIR': tmp_js_dir, 'DISABLE_SHEETS': '1'}, clear=False):
            job_state.reset_cache_for_tests()
            try:
                adm_hdr = {'X-Admin-Secret': app_module.ADMIN_API_SECRET}

                # 1. お問い合わせ（削除依頼）受信箱の記録・未読カウント・既読化
                contact_form.store_inquiry(
                    {
                        'category': 'removal',
                        'name': 'テスト店舗担当者',
                        'email': 'rep@example.com',
                        'message': '自店舗の掲載一時見直しをお願いいたします。',
                    },
                    ip_hash='abcdef0123456789',
                )
                unread = ops_governance.get_unread_counts(force=True)
                self.assertEqual(unread['unread_total'], 1)
                self.assertEqual(unread['unread_priority'], 1)
                inqs = ops_governance.list_inquiries()
                self.assertTrue(inqs[0]['is_priority'])
                inq_id = inqs[0]['id']

                r_res = self.client.post(
                    '/api/ops/governance',
                    json={'action': 'resolve_inquiry', 'inquiry_id': inq_id, 'resolved': True},
                    headers=adm_hdr,
                )
                self.assertEqual(r_res.status_code, 200)
                self.assertEqual(ops_governance.get_unread_counts(force=True)['unread_total'], 0)

                # 2. 店舗・ブランド緊急非表示スイッチ（410 Gone + noindex + sitemap.xml + /api/status 除外）
                r_hide_s = self.client.post(
                    '/api/ops/governance',
                    json={'action': 'toggle_store', 'store_name': 'JIS SHINJUKU', 'hidden': True},
                    headers=adm_hdr,
                )
                self.assertEqual(r_hide_s.status_code, 200)
                r_st_410 = self.client.get('/store/jis-shinjuku')
                self.assertEqual(r_st_410.status_code, 410)
                self.assertEqual(r_st_410.headers.get('X-Robots-Tag'), 'noindex, nofollow')
                self.assertNotIn('/store/jis-shinjuku', self.client.get('/sitemap.xml').data.decode('utf-8'))

                r_hide_b = self.client.post(
                    '/api/ops/governance',
                    json={'action': 'toggle_brand', 'brand_key': 'asobibar', 'hidden': True},
                    headers=adm_hdr,
                )
                self.assertEqual(r_hide_b.status_code, 200)
                self.assertEqual(self.client.get('/asobibar').status_code, 410)
                self.assertNotIn('https://aiseki-navi.com/asobibar</loc>', self.client.get('/sitemap.xml').data.decode('utf-8'))

                # 公開再開で 200 OK に復帰すること
                self.client.post('/api/ops/governance', json={'action': 'toggle_store', 'store_name': 'JIS SHINJUKU', 'hidden': False}, headers=adm_hdr)
                self.client.post('/api/ops/governance', json={'action': 'toggle_brand', 'brand_key': 'asobibar', 'hidden': False}, headers=adm_hdr)
                self.assertEqual(self.client.get('/store/jis-shinjuku').status_code, 200)
                self.assertEqual(self.client.get('/asobibar').status_code, 200)

                # 3. 一般スタッフ端末の一括強制ログアウト ＆ パスコード変更（マスター端末は維持）
                staff_client = self.app.test_client()
                master_client = self.app.test_client()
                self.assertEqual(staff_client.post('/staff', data={'passcode': app_module.STAFF_PASSCODE}).status_code, 302)
                self.assertEqual(master_client.post('/staff', data={'passcode': app_module.MASTER_PASSCODE}).status_code, 302)
                self.assertNotIn('認証してモニターを開く', staff_client.get('/staff').data.decode('utf-8'))

                # 強制ログアウト実行 -> 一般スタッフは未認証ログイン画面へ戻り、マスターは維持
                r_fl = master_client.post(
                    '/api/ops/governance',
                    json={'action': 'force_logout_staff'},
                    headers={'Origin': 'http://localhost'},
                )
                self.assertEqual(r_fl.status_code, 200)
                self.assertIn('認証してモニターを開く', staff_client.get('/staff').data.decode('utf-8'))
                self.assertEqual(master_client.get('/staff/analytics').status_code, 200)

                # スマホから一般スタッフ用パスコードを変更 -> 旧コード拒否・新コード有効
                r_pw = master_client.post(
                    '/api/ops/governance',
                    json={'action': 'update_staff_passcode', 'passcode': '8899new'},
                    headers={'Origin': 'http://localhost'},
                )
                self.assertEqual(r_pw.status_code, 200)
                self.assertEqual(staff_client.post('/staff', data={'passcode': app_module.STAFF_PASSCODE}).status_code, 200)  # login error page
                self.assertEqual(staff_client.post('/staff', data={'passcode': '8899new'}).status_code, 302)
                master_client.post('/api/ops/governance', json={'action': 'update_staff_passcode', 'reset_to_env': True}, headers=adm_hdr)

                # 4. Webhook URL の SSRF 防御と登録・マスク表示
                for bad_url in ('http://example.com/hook', 'https://localhost/hook', 'https://127.0.0.1/hook', 'https://169.254.169.254/latest'):
                    r_bad = self.client.post('/api/ops/governance', json={'action': 'set_webhook', 'url': bad_url}, headers=adm_hdr)
                    self.assertEqual(r_bad.status_code, 400, f"Expected SSRF rejection for {bad_url}")

                r_ok_wh = self.client.post(
                    '/api/ops/governance',
                    json={'action': 'set_webhook', 'url': 'https://discord.com/api/webhooks/123456/abcdef999999'},
                    headers=adm_hdr,
                )
                self.assertEqual(r_ok_wh.status_code, 200)
                self.assertIn('discord.com', r_ok_wh.get_json()['masked_url'])
                self.assertNotIn('abcdef', r_ok_wh.get_json()['masked_url'])
            finally:
                job_state.reset_cache_for_tests()

    # ------------------------------------------------------------------
    # 9. テスト汚染ハードロック ＆ クローラー機械スキャン遮断の回帰テスト
    # ------------------------------------------------------------------
    def test_sheets_store_hard_locks_live_sheets_during_unittest_even_without_env(self):
        """DISABLE_SHEETS 未設定でも unittest 実行中は実 Google Sheets への接続が物理遮断されること"""
        import logger as gs_logger
        import sheets_store

        old_dis = os.environ.pop('DISABLE_SHEETS', None)
        old_allow = os.environ.pop('ALLOW_LIVE_SHEETS_IN_TESTS', None)
        try:
            self.assertFalse(sheets_store.is_disabled())
            self.assertTrue(sheets_store._is_test_runtime())
            with mock.patch.object(gs_logger, 'get_client') as mock_get_client:
                ws = sheets_store.get_worksheet('Analytics Daily', ['date', 'event', 'label', 'count'])
                self.assertIsNone(ws)
                mock_get_client.assert_not_called()
        finally:
            if old_dis is not None:
                os.environ['DISABLE_SHEETS'] = old_dis
            else:
                os.environ['DISABLE_SHEETS'] = '1'
            if old_allow is not None:
                os.environ['ALLOW_LIVE_SHEETS_IN_TESTS'] = old_allow

    def test_ad_seo_and_headless_crawlers_blocked_from_link_and_analytics(self):
        """Mediapartners-Google / AdsBot / GoogleOther / Lighthouse / HTTPクライアントがボット判定＆robots遮断されること"""
        txt = self.client.get('/robots.txt').data.decode('utf-8')
        for bot_name in ('Mediapartners-Google', 'AdsBot-Google', 'GoogleOther', 'Google-InspectionTool', 'SemrushBot', 'AhrefsBot'):
            self.assertIn(f'User-agent: {bot_name}\n', txt)

        crawler_uas = [
            'Mediapartners-Google',
            'AdsBot-Google (+http://www.google.com/adsbot.html)',
            'Mozilla/5.0 (Linux; Android 6.0.1; Nexus 5X Build/MMB29P) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Mobile Safari/537.36 (compatible; GoogleOther)',
            'Mozilla/5.0 (compatible; Google-InspectionTool/1.0;)',
            'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) Chrome-Lighthouse',
            'axios/1.7.7',
            'undici',
        ]
        for ua in crawler_uas:
            with self.app.test_request_context('/link/orbis', headers={'User-Agent': ua}):
                self.assertTrue(app_module._is_bot_request(), ua)

    def test_affiliate_redirect_blocks_prefetch_and_ip_burst_scanning_without_breaking_redirect(self):
        """プリフェッチや同一IPからの機械的バースト巡回でも302遷移は維持しつつ affiliate_click 水増しだけを遮断すること"""
        import analytics
        import sheets_store

        human_ua = (
            'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) '
            'AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1'
        )
        analytics.reset_for_tests()
        try:
            with mock.patch.object(sheets_store, 'read_rows', return_value=[]):
                # 1) プリフェッチや既知ボットは ASP へ転送せず内部クッションへ逃がし、カウントもしない（staff_test=1 は確認用にASP URLを返す）
                r_pre = self.client.get(
                    '/link/orbis?from=/grooming',
                    headers={'User-Agent': human_ua, 'Sec-Purpose': 'prefetch'},
                )
                self.assertEqual(r_pre.status_code, 302)
                self.assertEqual(r_pre.headers.get('Location'), '/grooming')
                r_bot = self.client.get(
                    '/link/women_cosme_salon?from=/for-women',
                    headers={'User-Agent': 'Bytespider/1.0'},
                )
                self.assertEqual(r_bot.status_code, 302)
                self.assertEqual(r_bot.headers.get('Location'), '/for-women')
                r_probe = self.client.get(
                    '/link/orbis?staff_test=1',
                    headers={'User-Agent': 'curl/8.7.1'},
                )
                self.assertEqual(r_probe.status_code, 302)
                self.assertIn('px.a8.net', r_probe.headers.get('Location', ''))
                self.assertEqual(analytics.summary(days=1)['by_event'].get('affiliate_click', 0), 0)

                # 2) /api/track へのサーバー専用イベント（affiliate_click / page_view / sns_inflow）偽装POSTは拒否される
                for spoof_ev in ('affiliate_click', 'page_view', 'sns_inflow'):
                    r_sp = self.client.post(
                        '/api/track',
                        json={'event': spoof_ev, 'label': 'orbis @ /grooming'},
                        headers={'User-Agent': human_ua},
                    )
                    self.assertEqual(r_sp.status_code, 204)
                self.assertEqual(analytics.summary(days=1)['by_event'].get('affiliate_click', 0), 0)
                self.assertEqual(analytics.summary(days=1)['by_event'].get('page_view', 0), 0)

                # 3) 同一IP+UAから連続14枠スキャンしても 302 は全て成功し、計測は上限10件で打ち止めになる
                for _ in range(14):
                    r_clk = self.client.get('/link/orbis?from=/grooming', headers={'User-Agent': human_ua})
                    self.assertEqual(r_clk.status_code, 302)
                self.assertEqual(analytics.summary(days=1)['by_event'].get('affiliate_click', 0), 10)
        finally:
            analytics.reset_for_tests()


if __name__ == '__main__':
    unittest.main()

