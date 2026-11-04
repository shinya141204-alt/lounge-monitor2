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
        """app.js?v=20261105_1 と sw.js?v=90, CACHE_NAME aiseki-navi-v90 が同期していること"""
        home_resp = self.client.get('/')
        self.assertEqual(home_resp.status_code, 200)
        home_html = home_resp.data.decode('utf-8')
        home_resp.close()
        self.assertIn('/static/app.js?v=20261105_1', home_html)
        self.assertIn('/sw.js?v=90', home_html)

        sw_resp = self.client.get('/sw.js')
        self.assertEqual(sw_resp.status_code, 200)
        sw_txt = sw_resp.data.decode('utf-8')
        sw_resp.close()
        self.assertIn("CACHE_NAME = 'aiseki-navi-v90'", sw_txt)
        self.assertIn("'/static/app.js?v=20261105_1'", sw_txt)

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


if __name__ == '__main__':
    unittest.main()

