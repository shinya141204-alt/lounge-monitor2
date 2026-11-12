"""Unit and regression tests for Forecast Model v3 and Stats Simulator UI.

Verifies:
1. Hierarchical Empirical-Bayes calibration across brand, hour, and rainy conditions.
2. Graceful hierarchical fallback (Store x Hour x Rain -> Store -> Brand prior -> 0.0).
3. Zero direct /link/ citations inside templates/stats_report.html.
4. Simulator UI DOM structure, selectors, inline SVG rendering, and WCAG AA contrast compliance.
"""

import os
import re
import datetime
import shutil
import tempfile
import unittest

import app as app_module
import local_db
import moat_intelligence
import store_master

_TMP_DB_DIR = None
_ORIG_DB_PATH = None
_ORIG_DATABASE_URL = None


def setUpModule():
    global _TMP_DB_DIR, _ORIG_DB_PATH, _ORIG_DATABASE_URL
    _TMP_DB_DIR = tempfile.mkdtemp(prefix="aiseki_forecast_v3_")
    _ORIG_DB_PATH = local_db.DB_PATH
    _ORIG_DATABASE_URL = os.environ.pop("DATABASE_URL", None)
    local_db.DB_PATH = os.path.join(_TMP_DB_DIR, "test_lounge.db")
    local_db.reset_caches_for_tests()
    local_db.init_db()


def tearDownModule():
    local_db.reset_caches_for_tests()
    if _ORIG_DB_PATH is not None:
        local_db.DB_PATH = _ORIG_DB_PATH
    if _ORIG_DATABASE_URL is not None:
        os.environ["DATABASE_URL"] = _ORIG_DATABASE_URL
    local_db.reset_caches_for_tests()
    if _TMP_DB_DIR:
        shutil.rmtree(_TMP_DB_DIR, ignore_errors=True)


class TestForecastV3SelfCalibrating(unittest.TestCase):
    """Test Forecast v3 hierarchical Empirical-Bayes online self-calibration in local_db."""

    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.app = app_module.app
        self.client = app_module.app.test_client()
        local_db.init_db()
        local_db._invalidate_forecast_caches()
        self._clean_test_records()

    def tearDown(self):
        self._clean_test_records()
        local_db._invalidate_forecast_caches()

    def _clean_test_records(self):
        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM forecast_accuracy_logs WHERE store_name LIKE '%テスト%';")
            cur.execute("DELETE FROM store_hourly_rollups WHERE store_name LIKE '%テスト%';")
            conn.commit()
        finally:
            conn.close()

    def test_rainy_slot_direct_calibration_and_separation(self):
        """Rainy slot bias is distinguished from clear-weather slot bias."""
        now_jst = datetime.datetime.now(local_db._JST_TZ).replace(tzinfo=None)
        store = "OLG テスト渋谷"
        hr = 21

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            # Insert 4 days of rainy records at 21h (each date gets k_burst=0 with weight 1.0)
            for d in range(1, 5):
                d_str = (now_jst - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
                cur.execute(
                    """
                    INSERT OR REPLACE INTO store_hourly_rollups (
                        date_jst, hour_jst, business_date, store_name, sample_count,
                        is_rainy
                    ) VALUES (?, ?, ?, ?, 10, 1)
                    """,
                    (d_str, hr, d_str, store),
                )
                cur.execute(
                    """
                    INSERT OR REPLACE INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, raw_pred_wpct,
                        resolved_at, actual_women, actual_men, actual_wpct,
                        error_wpct_pt, abs_error_wpct_pt, online_bias_adj
                    ) VALUES (?, ?, ?, ?, 20, 20, 50, 25, 20, 56, 56, ?, 18, 23, 44, 12, 12, 0.0)
                    """,
                    (
                        f"{d_str} 21:15:00",
                        f"{d_str} 22:15:00",
                        store,
                        moat_intelligence.FORECAST_MODEL_VERSION,
                        f"{d_str} 22:15:00",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        local_db._invalidate_forecast_caches()
        local_db._refresh_forecast_caches_from_db(days=7)

        # When checking with is_rainy=True, adjustment should be negative (damping positive error)
        adj_rainy = local_db.get_cached_forecast_residual_adjustment(store, hr, is_rainy=True)
        self.assertLess(adj_rainy, -1.0, f"Rainy adjustment should dampen overshooting (+12pt error), got {adj_rainy}")

        # When checking with is_rainy=False, there were no clear samples for this store-hour, so it falls back to store-wide
        adj_clear = local_db.get_cached_forecast_residual_adjustment(store, hr, is_rainy=False)
        self.assertIsNotNone(adj_clear)

    def test_hierarchical_fallback_levels(self):
        """Hierarchical shrinkage falls back gracefully: Cell -> Store -> Brand -> 0.0."""
        now_jst = datetime.datetime.now(local_db._JST_TZ).replace(tzinfo=None)
        brand_store = "OLG agテスト心斎橋"  # Brand: ag
        new_store = "OLG agテスト新店舗"       # Brand: ag, 0 samples
        hr = 22

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            # Insert 5 days of brand_store records at 22h rainy (k_burst=0, sum eff_n > 4.0)
            for d in range(1, 6):
                d_str = (now_jst - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
                cur.execute(
                    """
                    INSERT OR REPLACE INTO store_hourly_rollups (
                        date_jst, hour_jst, business_date, store_name, sample_count,
                        is_rainy
                    ) VALUES (?, ?, ?, ?, 10, 1)
                    """,
                    (d_str, hr, d_str, brand_store),
                )
                cur.execute(
                    """
                    INSERT OR REPLACE INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, raw_pred_wpct,
                        resolved_at, actual_women, actual_men, actual_wpct,
                        error_wpct_pt, abs_error_wpct_pt, online_bias_adj
                    ) VALUES (?, ?, ?, ?, 20, 20, 50, 25, 20, 56, 56, ?, 18, 22, 45, 11, 11, 0.0)
                    """,
                    (
                        f"{d_str} 22:20:00",
                        f"{d_str} 23:20:00",
                        brand_store,
                        moat_intelligence.FORECAST_MODEL_VERSION,
                        f"{d_str} 23:20:00",
                    ),
                )
            conn.commit()
        finally:
            conn.close()

        local_db._invalidate_forecast_caches()
        local_db._refresh_forecast_caches_from_db(days=7)

        # 1. brand_store (has 5 samples) gets direct cell adjustment
        adj_brand_store = local_db.get_cached_forecast_residual_adjustment(brand_store, hr, is_rainy=True)
        self.assertLess(adj_brand_store, -1.0)

        # 2. new_store has 0 samples, but shares 'ag' brand key with brand_store.
        # Under is_rainy=True, it inherits brand prior dampening
        adj_new_store = local_db.get_cached_forecast_residual_adjustment(new_store, hr, is_rainy=True)
        self.assertLess(adj_new_store, 0.0, "New store should inherit cautious brand prior under matching conditions")
        self.assertGreaterEqual(adj_new_store, adj_brand_store, "Brand prior adjustment must be equal or more conservative than direct cell")

        # 3. Completely unknown brand / non-existent store with no brand prior returns 0.0
        adj_unknown = local_db.get_cached_forecast_residual_adjustment("NONEXISTENT_BRAND_XYZ", hr, is_rainy=True)
        self.assertEqual(adj_unknown, 0.0, "Zero-evidence store must strictly return 0.0 without hallucination")

        # 4. Empty store name strictly returns 0.0
        adj_empty = local_db.get_cached_forecast_residual_adjustment("", hr, is_rainy=True)
        self.assertEqual(adj_empty, 0.0, "Empty store string must strictly return 0.0")

    def test_backward_compatibility_when_is_rainy_omitted(self):
        """Omitting is_rainy preserves strict store-specific lookup without leaking brand priors."""
        # For an unobserved store, omitting is_rainy MUST return 0.0
        self.assertEqual(local_db.get_cached_forecast_residual_adjustment("TEST_NEW_UNOBSERVED_STORE", 20), 0.0)
        self.assertEqual(local_db.get_cached_forecast_residual_adjustment("", 20), 0.0)


class TestStatsSimulatorUI(unittest.TestCase):
    """Test /stats interactive simulator UI, inline SVG rendering, and zero direct /link/ violations."""

    def setUp(self):
        app_module.app.config["TESTING"] = True
        self.app = app_module.app
        self.client = app_module.app.test_client()
        self.stats_template_path = os.path.join(
            os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
            "templates",
            "stats_report.html",
        )

    def test_simulator_template_has_zero_direct_links(self):
        """AGENTS.md / Ops-Sentinel compliance: templates/stats_report.html must contain 0 direct /link/ citations."""
        self.assertTrue(os.path.isfile(self.stats_template_path), f"File not found: {self.stats_template_path}")
        with open(self.stats_template_path, "r", encoding="utf-8") as f:
            content = f.read()

        direct_link_pattern = re.compile(r'href=["\'](?:/link/|https?://[^"\']*/link/)', re.IGNORECASE)
        matches = direct_link_pattern.findall(content)
        self.assertEqual(
            len(matches),
            0,
            f"Found forbidden direct /link/ in templates/stats_report.html: {matches}",
        )

    def test_simulator_ui_elements_and_aria_structure(self):
        """Simulator UI contains interactive controls, inline SVG, and accessibility attributes."""
        with open(self.stats_template_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Section & heading
        self.assertIn('id="stats-simulator"', content)
        self.assertIn("全国相席ラウンジ 時間波形シミュレーター", content)

        # Selectors
        self.assertIn('id="sim-area"', content)
        self.assertIn('id="sim-brand"', content)
        self.assertIn('id="sim-dow"', content)
        self.assertIn('id="sim-weather"', content)

        # Options verification
        self.assertIn('value="oriental-lounge"', content)
        self.assertIn('value="ag"', content)
        self.assertIn('value="jis"', content)
        self.assertIn('value="asobibar"', content)
        self.assertIn('value="rainy"', content)

        # Inline SVG Chart
        self.assertIn('id="sim-svg"', content)
        self.assertIn('viewBox="0 0 660 250"', content)

        # Result KPI cards
        self.assertIn('id="sim-res-peak"', content)
        self.assertIn('id="sim-res-slot"', content)
        self.assertIn('id="sim-res-crowd"', content)
        self.assertIn('id="sim-res-duration"', content)

    def test_simulator_ui_theme_contrast_rules(self):
        """Styles define high-contrast color variables conforming to WCAG AA for both dark and light modes."""
        with open(self.stats_template_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Dark mode wave stroke: #f472b6 (Rose-400 on dark background > 6:1)
        self.assertIn("#f472b6", content)
        # Light mode wave stroke: #be185d (Rose-700 on white background > 5:1)
        self.assertIn("#be185d", content)
        # Light mode container overrides
        self.assertIn("body.theme-light .sim-container", content)
        self.assertIn("body.theme-light .sim-select", content)
        self.assertIn("body.theme-light .sim-chart-box", content)

    def test_stats_route_integration(self):
        """GET /stats renders simulator UI with HTTP 200 without runtime Jinja exceptions, with ZERO direct /link/ citations."""
        app_module.clear_fast_http_caches()
        resp = self.client.get("/stats")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn("全国相席ラウンジ 時間波形シミュレーター", html)
        self.assertIn('id="stats-simulator"', html)
        self.assertIn('id="sim-svg"', html)
        self.assertIn('id="sim-area"', html)

        # Strict zero direct /link/ in SSR rendered HTML
        direct_link_pattern = re.compile(r'href=["\'](?:/link/|https?://[^"\']*/link/)', re.IGNORECASE)
        matches = direct_link_pattern.findall(html)
        self.assertEqual(len(matches), 0, f"Found forbidden direct /link/ in SSR rendered /stats HTML: {matches}")


if __name__ == "__main__":
    unittest.main()
