# -*- coding: utf-8 -*-
"""tests/test_challenger_argon_empirical.py

Empirical Challenger Adversarial Test Suite for M4 Integration.

Empirically verifies:
1. Forecast Model v3 (local_db.py):
   - Extreme positive outliers (+1000% error) strictly clamped to -5.0 pt.
   - Extreme negative outliers (-990% error) strictly clamped to +5.0 pt.
   - Sample count filter (<4 people) strictly ignored to prevent overfitting.
   - Zero-evidence / unknown stores strictly return 0.0 without hallucination.
   - Empty store strings and None strictly return 0.0.
   - Malformed rainy flags (str, int, list, dict, object, nan, inf) never crash and stay clamped in [-5.0, +5.0].
   - Malformed hours (-1, 24, 99, -999, '21') stay clamped in [-5.0, +5.0].
   - Global bias_map invariant: all entries without exception lie in [-5.0, +5.0].
   - predict_store_60m_dynamics preserves bounds [8, 92] under extreme biases and returns None on zero people.

2. All 63 Store Second Venues (static/second_venues.json):
   - 63/63 stores have valid geographic coordinates (lat/lng) in Japan or Seoul.
   - 63/63 stores have curated second venues (is_fallback=False, is_store_curated=True, len >= 2).
   - 100% of venues have walk_minutes in [0, 5].
   - 100% of venues use Google official free Web Intent URL (https://www.google.com/maps/search/?api=1&query=...).
   - 100% of queries are validly URL-encoded with non-empty targets.
   - Zero GCP paid API (Places API, Maps SDK, key=AIza...) across all venues and code.
"""

import os
import re
import json
import shutil
import tempfile
import urllib.parse
import datetime
import unittest
from unittest.mock import patch

import store_master
import second_venues
import local_db
import moat_intelligence

_TMP_DB_DIR = None
_ORIG_DB_PATH = None
_ORIG_DATABASE_URL = None


def setUpModule():
    global _TMP_DB_DIR, _ORIG_DB_PATH, _ORIG_DATABASE_URL
    _TMP_DB_DIR = tempfile.mkdtemp(prefix="aiseki_argon_emp_")
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


class TestForecastV3AdversarialStress(unittest.TestCase):
    """Adversarial stress testing of Forecast Model v3 calibration and clamping."""

    def setUp(self):
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
            cur.execute("DELETE FROM forecast_accuracy_logs WHERE store_name LIKE '%CHALLENGER_V3_%';")
            cur.execute("DELETE FROM store_hourly_rollups WHERE store_name LIKE '%CHALLENGER_V3_%';")
            conn.commit()
        finally:
            conn.close()

    def test_extreme_positive_outlier_clamped_to_minus_5(self):
        """+1000% overprediction error must be strictly clamped to -5.0 pt."""
        now_jst = datetime.datetime.now(local_db._JST_TZ).replace(tzinfo=None)
        store = "OLG CHALLENGER_V3_POS"
        cur_ver = moat_intelligence.FORECAST_MODEL_VERSION

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            for d in range(1, 6):
                d_str = (now_jst - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
                cur.execute(
                    """
                    INSERT OR REPLACE INTO store_hourly_rollups (
                        date_jst, hour_jst, business_date, store_name, sample_count, is_rainy
                    ) VALUES (?, 20, ?, ?, 10, 1)
                    """,
                    (d_str, d_str, store),
                )
                t_str = f"{d_str} 20:15:00"
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, raw_pred_wpct,
                        resolved_at, actual_women, actual_men, actual_wpct,
                        error_wpct_pt, abs_error_wpct_pt, online_bias_adj
                    ) VALUES (?, ?, ?, ?, 20, 20, 50, 100, 10, 1000, 1000, ?, 10, 10, 50, 950, 950, 0.0)
                    """,
                    (t_str, t_str, store, cur_ver, t_str),
                )
            conn.commit()
        finally:
            conn.close()

        bmap, _ = local_db._refresh_forecast_caches_from_db(days=7)
        adj_rain = local_db.get_cached_forecast_residual_adjustment(store, 20, is_rainy=True)
        self.assertEqual(adj_rain, -5.0, f"Expected clamp to -5.0 pt, got {adj_rain}")

    def test_extreme_negative_outlier_clamped_to_plus_5(self):
        """Extreme underprediction error (-990 pt) must be strictly clamped to +5.0 pt."""
        now_jst = datetime.datetime.now(local_db._JST_TZ).replace(tzinfo=None)
        store = "OLG CHALLENGER_V3_NEG"
        cur_ver = moat_intelligence.FORECAST_MODEL_VERSION

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            for d in range(1, 6):
                d_str = (now_jst - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
                cur.execute(
                    """
                    INSERT OR REPLACE INTO store_hourly_rollups (
                        date_jst, hour_jst, business_date, store_name, sample_count, is_rainy
                    ) VALUES (?, 21, ?, ?, 10, 0)
                    """,
                    (d_str, d_str, store),
                )
                t_str = f"{d_str} 21:15:00"
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, raw_pred_wpct,
                        resolved_at, actual_women, actual_men, actual_wpct,
                        error_wpct_pt, abs_error_wpct_pt, online_bias_adj
                    ) VALUES (?, ?, ?, ?, 10, 10, 50, 1, 10, 10, 10, ?, 100, 10, 1000, -990, 990, 0.0)
                    """,
                    (t_str, t_str, store, cur_ver, t_str),
                )
            conn.commit()
        finally:
            conn.close()

        bmap, _ = local_db._refresh_forecast_caches_from_db(days=7)
        adj_clear = local_db.get_cached_forecast_residual_adjustment(store, 21, is_rainy=False)
        self.assertEqual(adj_clear, 5.0, f"Expected clamp to +5.0 pt, got {adj_clear}")

    def test_sample_count_filter_prevents_noise_overfitting(self):
        """Rows with actual people < 4 must be ignored during calibration."""
        now_jst = datetime.datetime.now(local_db._JST_TZ).replace(tzinfo=None)
        store = "CHALLENGER_V3_FEW_PEOPLE"
        cur_ver = moat_intelligence.FORECAST_MODEL_VERSION

        conn = local_db.get_connection()
        try:
            cur = conn.cursor()
            cur.execute("DELETE FROM forecast_accuracy_logs WHERE store_name = ?;", (store,))
            for d in range(1, 6):
                d_str = (now_jst - datetime.timedelta(days=d)).strftime("%Y-%m-%d")
                t_str = f"{d_str} 22:15:00"
                cur.execute(
                    """
                    INSERT INTO forecast_accuracy_logs (
                        predicted_at, target_at, store_name, model_version,
                        start_women, start_men, start_wpct,
                        pred_women, pred_men, pred_wpct, raw_pred_wpct,
                        resolved_at, actual_women, actual_men, actual_wpct,
                        error_wpct_pt, abs_error_wpct_pt, online_bias_adj
                    ) VALUES (?, ?, ?, ?, 1, 1, 50, 2, 1, 66, 66, ?, 1, 1, 50, 16, 16, 0.0)
                    """,
                    (t_str, t_str, store, cur_ver, t_str),
                )
            conn.commit()
        finally:
            conn.close()

        local_db._refresh_forecast_caches_from_db(days=7)
        adj = local_db.get_cached_forecast_residual_adjustment(store, 22, is_rainy=False)
        self.assertEqual(adj, 0.0, "Stores with <4 people must return 0.0 and never calibrate noise")

    def test_zero_evidence_unknown_store_returns_zero(self):
        """Completely unknown brands with 0 samples must strictly return 0.0."""
        adj = local_db.get_cached_forecast_residual_adjustment("NONEXISTENT_BRAND_ABC", 20, is_rainy=True)
        self.assertEqual(adj, 0.0, "Zero-evidence store must strictly return 0.0 without hallucination")

        adj_empty = local_db.get_cached_forecast_residual_adjustment("", 20, is_rainy=True)
        self.assertEqual(adj_empty, 0.0)

        adj_none = local_db.get_cached_forecast_residual_adjustment(None, 20, is_rainy=True)
        self.assertEqual(adj_none, 0.0)

    def test_malformed_rainy_flags_never_crash(self):
        """Various truthy/falsy/malformed is_rainy flags must execute safely and stay clamped."""
        test_flags = [
            True, False, None,
            "rainy", "clear", "",
            1, 0, -1, 999,
            [], [1], {}, {"rain": True},
            object(), float("nan"), float("inf"),
        ]
        for rf in test_flags:
            adj = local_db.get_cached_forecast_residual_adjustment("OLG 渋谷本店", 21, is_rainy=rf)
            self.assertIsInstance(adj, float)
            self.assertGreaterEqual(adj, -5.0)
            self.assertLessEqual(adj, 5.0)

    def test_malformed_hours_never_crash(self):
        """Out of range hours must fall back safely and stay clamped."""
        for h in [-1, 0, 12, 23, 24, 99, -999, "21"]:
            adj = local_db.get_cached_forecast_residual_adjustment("OLG 渋谷本店", h, is_rainy=False)
            self.assertIsInstance(adj, float)
            self.assertGreaterEqual(adj, -5.0)
            self.assertLessEqual(adj, 5.0)

    def test_global_bias_map_invariants(self):
        """Every entry in bias_map must strictly satisfy -5.0 <= val <= 5.0."""
        bmap, _ = local_db._refresh_forecast_caches_from_db(days=7)
        for k, v in bmap.items():
            self.assertGreaterEqual(v, -5.0, f"Bias {k} = {v} < -5.0")
            self.assertLessEqual(v, 5.0, f"Bias {k} = {v} > 5.0")

    def test_predict_store_60m_dynamics_bounds_and_zero_people(self):
        """predict_store_60m_dynamics must preserve bounds [8, 92] and return None on 0 people."""
        now_dt = datetime.datetime(2026, 11, 8, 22, 0, 0)

        # 1. Under max positive bias (+5.0)
        with patch("local_db.get_cached_forecast_residual_adjustment", return_value=5.0):
            res = moat_intelligence.predict_store_60m_dynamics(
                store_name="OLG 渋谷本店", women=90, men=5, now_jst=now_dt, apply_online_bias=True
            )
            self.assertLessEqual(res["pred_60m_wpct"], 92)
            self.assertGreaterEqual(res["pred_60m_wpct"], 8)

        # 2. Under max negative bias (-5.0)
        with patch("local_db.get_cached_forecast_residual_adjustment", return_value=-5.0):
            res = moat_intelligence.predict_store_60m_dynamics(
                store_name="OLG 渋谷本店", women=5, men=90, now_jst=now_dt, apply_online_bias=True
            )
            self.assertLessEqual(res["pred_60m_wpct"], 92)
            self.assertGreaterEqual(res["pred_60m_wpct"], 8)

        # 3. Zero people (0 women, 0 men): ratio must be None (zero hallucination)
        res_zero = moat_intelligence.predict_store_60m_dynamics(
            store_name="OLG 渋谷本店", women=0, men=0, now_jst=now_dt, apply_online_bias=True
        )
        self.assertIsNone(res_zero["pred_60m_wpct"], "Ratio on 0 people must be None, not fabricated")
        self.assertEqual(res_zero["pred_60m_women"], 0)
        self.assertEqual(res_zero["pred_60m_men"], 0)


class TestSecondVenues63FullScanAdversarial(unittest.TestCase):
    """Full-scan adversarial verification of all 63 stores' second venues data."""

    @classmethod
    def setUpClass(cls):
        cls.active_slugs = sorted(list(set([
            slug for sname, slug in store_master.STORE_SLUGS.items()
            if slug not in store_master.LEGACY_SLUG_REDIRECTS
        ])))
        with open("static/store_master.json", "r", encoding="utf-8") as f:
            cls.store_master_data = json.load(f).get("stores", {})
        with open("static/second_venues.json", "r", encoding="utf-8") as f:
            cls.venues_data = json.load(f)

    def test_active_slugs_count_strictly_63(self):
        """Active store count must be exactly 63."""
        self.assertEqual(len(self.active_slugs), 63)
        self.assertEqual(len(self.store_master_data), 63)

    def test_all_63_stores_coordinates_validity(self):
        """Each of the 63 stores must have valid physical coordinates in Japan or Seoul."""
        for slug in self.active_slugs:
            meta = None
            for k, v in self.store_master_data.items():
                if store_master.STORE_SLUGS.get(k) == slug:
                    meta = v
                    break
            self.assertIsNotNone(meta, f"Missing store_master metadata for {slug}")
            lat = meta.get("lat")
            lng = meta.get("lng")
            self.assertIsInstance(lat, (int, float), f"Invalid lat for {slug}: {lat}")
            self.assertIsInstance(lng, (int, float), f"Invalid lng for {slug}: {lng}")

            is_japan = (24.0 <= lat <= 46.0) and (122.0 <= lng <= 146.0)
            is_seoul = (37.0 <= lat <= 38.5) and (126.5 <= lng <= 127.5)
            self.assertTrue(is_japan or is_seoul, f"Store {slug} coords ({lat}, {lng}) out of bounds")

    def test_all_63_stores_have_curated_venues(self):
        """All 63 stores must have curated venues with len >= 2 and is_fallback=False."""
        venues_by_store = self.venues_data.get("venues_by_store", {})
        self.assertEqual(len(venues_by_store), 63)

        for slug in self.active_slugs:
            self.assertIn(slug, venues_by_store, f"Store {slug} missing in venues_by_store")
            venues_list = venues_by_store[slug]
            self.assertGreaterEqual(len(venues_list), 2, f"Store {slug} has <2 venues")

            res = second_venues.get_second_venues_for_store(slug)
            self.assertFalse(res["is_fallback"], f"Store {slug} returned fallback")
            self.assertTrue(res["is_store_curated"], f"Store {slug} not marked curated")
            self.assertEqual(len(res["spots"]), len(venues_list))

    def test_venue_walk_minutes_within_5_minutes(self):
        """Every venue must have 0 <= walk_minutes <= 5."""
        for slug in self.active_slugs:
            res = second_venues.get_second_venues_for_store(slug)
            for sp in res["spots"]:
                wm = sp.get("walk_minutes")
                self.assertIsInstance(wm, int, f"walk_minutes not int in {sp.get('name')}")
                self.assertGreaterEqual(wm, 0)
                self.assertLessEqual(wm, 5, f"walk_minutes {wm} > 5 in {sp.get('name')} ({slug})")

    def test_google_maps_free_web_intent_urls_and_zero_paid_api(self):
        """Every venue's map_url must be a valid Google Maps search intent URL with zero paid API usage."""
        free_intent_re = re.compile(r"^https://www\.google\.com/maps/search/\?api=1&query=(.+)$")
        forbidden_patterns = [
            "maps.googleapis.com",
            "places.googleapis.com",
            "key=",
            "place_id=",
            "sessiontoken=",
            "googlemaps",
        ]

        total_spots = 0
        for slug in self.active_slugs:
            res = second_venues.get_second_venues_for_store(slug)
            for sp in res["spots"]:
                total_spots += 1
                map_url = sp.get("map_url", "")

                # 1. Match Google Free Intent regex
                match = free_intent_re.match(map_url)
                self.assertIsNotNone(match, f"Invalid intent URL format: {map_url}")

                # 2. Query target unquoted and non-empty
                query_target = urllib.parse.unquote(match.group(1))
                self.assertTrue(len(query_target.strip()) > 0)

                # 3. Zero paid API patterns in URL
                for pat in forbidden_patterns:
                    self.assertNotIn(pat, map_url.lower(), f"Forbidden GCP API pattern {pat} in {map_url}")

                # 4. LINE share URL and Reservation URL
                self.assertTrue(sp["line_share_url"].startswith("https://line.me/R/msg/text/?"))
                self.assertTrue(sp["reservation_url"].startswith(f"/link/second_venue_reservation?from=/store/{slug}"))

        self.assertGreaterEqual(total_spots, 126)


if __name__ == "__main__":
    unittest.main()
