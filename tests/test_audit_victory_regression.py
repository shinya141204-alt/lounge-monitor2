import json
import re
import unittest
from app import app
import store_master

class VictoryAuditRegressionTests(unittest.TestCase):
    def setUp(self):
        self.client = app.test_client()

    def test_api_analysis_brand_naming_compliance(self):
        """1. /api/analysis/<store_name> returns canonical store_name with zero bare 'OLG'."""
        resp = self.client.get('/api/analysis/oriental-lounge-shinjuku')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json()
        self.assertEqual(data.get('store_name'), 'オリエンタルラウンジ 新宿')
        self.assertNotIn('OLG', data.get('store_name', ''))

    def test_api_weekly_report_brand_naming_compliance(self):
        """2. /api/weekly-report exposes zero bare 'OLG' in stores, anaba_stores, or by_brand."""
        resp = self.client.get('/api/weekly-report')
        self.assertEqual(resp.status_code, 200)
        data = resp.get_json().get('data', {})
        stores = data.get('stores', [])
        anaba = data.get('anaba_stores', [])
        by_brand = data.get('by_brand', {})

        for s in stores:
            self.assertNotEqual(s.get('brand'), 'OLG')
            self.assertFalse(s.get('name', '').startswith('OLG '))
        for s in anaba:
            self.assertNotEqual(s.get('brand'), 'OLG')
            self.assertFalse(s.get('name', '').startswith('OLG '))
        self.assertNotIn('OLG', by_brand)

    def test_api_forecast_accuracy_brand_naming_compliance(self):
        """3. /api/forecast-accuracy exposes zero bare 'OLG' across all stores, summaries, and filters."""
        resp = self.client.get('/api/forecast-accuracy')
        self.assertEqual(resp.status_code, 200)
        raw_json_str = resp.data.decode('utf-8')
        # Check no standalone word 'OLG'
        self.assertIsNone(re.search(r'\bOLG\b', raw_json_str))
        data = resp.get_json()
        for it in data.get('by_store', []):
            self.assertNotIn('OLG', it.get('store_name', ''))
        summary = data.get('summary', {})
        for k in ('top_underperforming', 'top_overperforming', 'best_store', 'worst_store'):
            v = summary.get(k)
            if v:
                self.assertNotIn('OLG', str(v))

    def test_api_concierge_brand_naming_compliance(self):
        """4. /api/concierge returns brand-safe store names in candidates and backups."""
        for area in ('tokyo', 'shinjuku', 'shibuya', 'all'):
            resp = self.client.get(f'/api/concierge?area={area}')
            self.assertEqual(resp.status_code, 200)
            raw_json_str = resp.data.decode('utf-8')
            self.assertIsNone(re.search(r'\bOLG\b', raw_json_str))

    def test_index_html_modal_attributes_compliance(self):
        """5. templates/index.html event modal tab buttons have zero bare 'ORIENTAL'."""
        with open('templates/index.html', 'r', encoding='utf-8') as f:
            html = f.read()
        self.assertNotIn("filterEventsModal('ORIENTAL'", html)
        self.assertNotIn('data-filter="ORIENTAL"', html)
        self.assertIn("filterEventsModal('ORIENTAL_LOUNGE'", html)
        self.assertIn('data-filter="ORIENTAL_LOUNGE"', html)

if __name__ == '__main__':
    unittest.main()
