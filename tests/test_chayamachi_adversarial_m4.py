"""
Empirical Adversarial Stress Test Suite for Milestone 4
(梅田・茶屋町 ASOBIBAR vs ヤタコイ 回遊シフト).

Authored by: Challenger 1 (teamwork_challenger)
Purpose: Stress-test boundary conditions, malformed Firebase inputs, zero seats,
time transitions, and ensure zero unhandled exceptions can escape under any input.
"""

import datetime
import math
import os
import unittest
from unittest.mock import MagicMock, patch

import jinja2
import monitor
import moat_intelligence


class TestYatakoiMalformedFirebaseAdversarial(unittest.TestCase):
    """Adversarial stress testing of monitor.get_yatakoi_data with corrupt Firebase payloads."""

    def _run_with_payload(self, payload, now_jst=None):
        with patch("monitor._get_http_session") as mock_get_session:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = payload
            mock_session = MagicMock()
            mock_session.get.return_value = mock_resp
            mock_get_session.return_value = mock_session

            dt = now_jst or datetime.datetime(2026, 11, 10, 21, 0)
            return monitor.get_yatakoi_data(now_jst=dt)

    def test_firebase_returns_none(self):
        res = self._run_with_payload(None)
        self.assertEqual(res, [], "None response should safely return empty list")

    def test_firebase_returns_empty_string(self):
        res = self._run_with_payload("")
        self.assertEqual(res, [], "Empty string response should safely return empty list")

    def test_firebase_returns_html_error_string(self):
        res = self._run_with_payload("<html><body>502 Bad Gateway</body></html>")
        self.assertEqual(res, [], "HTML error string should safely return empty list")

    def test_firebase_returns_list_instead_of_dict(self):
        res = self._run_with_payload([1, 2, 3, {"males": 5}])
        self.assertEqual(res, [], "List response should safely return empty list")

    def test_firebase_returns_integer(self):
        res = self._run_with_payload(12345)
        self.assertEqual(res, [], "Integer response should safely return empty list")

    def test_firebase_all_keys_none(self):
        payload = {
            "males": None,
            "females": None,
            "ykMales": None,
            "ykFemales": None,
            "seats": None,
            "ykSeats": None,
            "isDouble": None,
        }
        res = self._run_with_payload(payload)
        self.assertEqual(len(res), 1)
        store = res[0]
        self.assertEqual(store["men"], 0)
        self.assertEqual(store["women"], 0)
        pos = store["pos_telemetry"]
        self.assertEqual(pos["yatakoi_men"], 0)
        self.assertEqual(pos["yatakoi_women"], 0)
        self.assertEqual(pos["asobibar_men"], 0)
        self.assertEqual(pos["asobibar_women"], 0)
        self.assertEqual(pos["yatakoi_seats"], 0)
        self.assertEqual(pos["asobibar_seats"], 0)
        self.assertFalse(pos["is_double"])

    def test_firebase_invalid_strings_in_counts(self):
        # When string cannot be parsed as int, get_yatakoi_data should catch exception and return []
        payload = {
            "males": "invalid_not_an_int",
            "females": "corrupted",
            "ykMales": "bad",
            "ykFemales": "error",
        }
        res = self._run_with_payload(payload)
        self.assertEqual(res, [], "Unparseable int values should safely catch ValueError and return []")

    def test_firebase_nan_and_inf(self):
        payload = {
            "males": float("nan"),
            "females": float("inf"),
            "ykMales": 10,
            "ykFemales": 12,
        }
        res = self._run_with_payload(payload)
        self.assertEqual(res, [], "NaN/Inf in int() conversion should catch exception and return []")

    def test_firebase_huge_integers(self):
        payload = {
            "males": 10**30,
            "females": 10**30,
            "ykMales": 10**30,
            "ykFemales": 10**30,
            "seats": 100,
            "ykSeats": 100,
        }
        res = self._run_with_payload(payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["men"], 2 * 10**30)

    def test_firebase_negative_counts(self):
        payload = {
            "males": -10,
            "females": -20,
            "ykMales": -5,
            "ykFemales": -15,
            "seats": 10,
            "ykSeats": 70,
        }
        res = self._run_with_payload(payload)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["men"], -15)
        self.assertEqual(res[0]["women"], -35)

    def test_firebase_zero_seats(self):
        payload = {
            "males": 5,
            "females": 5,
            "ykMales": 10,
            "ykFemales": 10,
            "seats": 0,
            "ykSeats": 0,
        }
        res = self._run_with_payload(payload)
        self.assertEqual(len(res), 1)
        pos = res[0]["pos_telemetry"]
        self.assertEqual(pos["asobibar_seats"], 0)
        self.assertEqual(pos["yatakoi_seats"], 0)

    @patch("monitor._get_http_session")
    def test_firebase_network_exceptions(self, mock_get_session):
        import requests
        for exc in (
            requests.exceptions.ReadTimeout("Timeout"),
            requests.exceptions.ConnectionError("Refused"),
            requests.exceptions.HTTPError("500 Server Error"),
            requests.exceptions.ChunkedEncodingError("Chunk error"),
            Exception("Arbitrary unknown network failure"),
        ):
            mock_session = MagicMock()
            mock_session.get.side_effect = exc
            mock_get_session.return_value = mock_session
            res = monitor.get_yatakoi_data()
            self.assertEqual(res, [], f"Exception {exc} must safely return []")


class TestChayamachiShiftAnalysisAdversarial(unittest.TestCase):
    """Adversarial stress testing of get_chayamachi_shift_analysis against zero division and types."""

    def test_zero_seats_no_zero_division(self):
        """Verify ZeroDivisionError is strictly impossible with seats=0, negative, or None."""
        adversarial_seats = [
            (0, 0),
            (-1, -1),
            (-1000, -500),
            (None, None),
            ("0", "0"),
            ("-5", "-10"),
            ("invalid", "corrupt"),
            (float("nan"), float("nan")),
            (float("inf"), float("inf")),
            (float("-inf"), float("-inf")),
        ]

        dt = datetime.datetime(2026, 11, 10, 20, 0)
        for y_s, a_s in adversarial_seats:
            with self.subTest(y_seats=y_s, a_seats=a_s):
                res = moat_intelligence.get_chayamachi_shift_analysis(
                    now_jst=dt,
                    yatakoi_cust=20,
                    asobibar_cust=10,
                    yatakoi_seats=y_s,
                    asobibar_seats=a_s,
                )
                self.assertIsInstance(res, dict)
                self.assertGreaterEqual(res["yatakoi_seats"], 1)
                self.assertGreaterEqual(res["asobibar_seats"], 1)
                self.assertIsInstance(res["yatakoi_occ_pct"], float)
                self.assertIsInstance(res["asobibar_occ_pct"], float)
                self.assertFalse(math.isnan(res["yatakoi_occ_pct"]))
                self.assertFalse(math.isinf(res["yatakoi_occ_pct"]))

    def test_zero_customers_no_zero_division(self):
        """Verify total_cust == 0 produces valid ratios without ZeroDivisionError."""
        dt = datetime.datetime(2026, 11, 10, 20, 0)
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust=0,
            asobibar_cust=0,
            yatakoi_seats=70,
            asobibar_seats=10,
        )
        self.assertEqual(res["total_cust"], 0)
        self.assertEqual(res["yatakoi_ratio_pct"], 80.0)
        self.assertEqual(res["asobibar_ratio_pct"], 20.0)
        self.assertEqual(res["yatakoi_occ_pct"], 0.0)
        self.assertEqual(res["asobibar_occ_pct"], 0.0)

    def test_extreme_customer_values(self):
        """Verify negative, huge, and non-numeric customer values are clamped safely."""
        dt = datetime.datetime(2026, 11, 10, 20, 0)
        # 1. Negative customers clamped to 0
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust=-100,
            asobibar_cust=-50,
        )
        self.assertEqual(res["yatakoi_cust"], 0)
        self.assertEqual(res["asobibar_cust"], 0)
        self.assertEqual(res["total_cust"], 0)

        # 2. Huge customers clamped to 10000
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust=10**50,
            asobibar_cust=10**50,
        )
        self.assertEqual(res["yatakoi_cust"], 10000)
        self.assertEqual(res["asobibar_cust"], 10000)
        self.assertEqual(res["total_cust"], 20000)

        # 3. String & NaN customers safely default to 0
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust="not_a_number",
            asobibar_cust=float("nan"),
        )
        self.assertEqual(res["yatakoi_cust"], 0)
        self.assertEqual(res["asobibar_cust"], 0)

    def test_pos_telemetry_pathological_inputs(self):
        """Test pos_telemetry with corrupt or unusual structures."""
        dt = datetime.datetime(2026, 11, 10, 21, 0)

        # Non-dict pos_telemetry
        for bad_pos in ["corrupt_str", 12345, [1, 2, 3], False]:
            res = moat_intelligence.get_chayamachi_shift_analysis(
                now_jst=dt,
                pos_telemetry=bad_pos,
            )
            self.assertIsInstance(res, dict)

        # Dict with pathological values
        bad_dict = {
            "telemetry_type": "yatakoi_pos",
            "yatakoi_men": float("nan"),
            "yatakoi_women": float("inf"),
            "asobibar_men": -99,
            "asobibar_women": "bad_str",
            "yatakoi_seats": 0,
            "asobibar_seats": -10,
        }
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            pos_telemetry=bad_dict,
        )
        self.assertEqual(res["yatakoi_cust"], 0)
        self.assertEqual(res["asobibar_cust"], 0)
        self.assertGreaterEqual(res["yatakoi_seats"], 1)
        self.assertGreaterEqual(res["asobibar_seats"], 1)
        self.assertFalse(math.isnan(res["yatakoi_occ_pct"]))

    def test_pos_telemetry_dict_without_seats_and_string_seats_parameter(self):
        """
        Verify that passing pos_telemetry without seat keys and non-int seats parameter
        does not crash with TypeError: '<=' not supported between instances of 'str' and 'int'.
        """
        res = moat_intelligence.get_chayamachi_shift_analysis(
            pos_telemetry={"telemetry_type": "yatakoi_pos"},
            yatakoi_seats="invalid_seats",
            asobibar_seats="invalid_seats",
        )
        self.assertIsInstance(res, dict)
        self.assertGreaterEqual(res["yatakoi_seats"], 1)
        self.assertGreaterEqual(res["asobibar_seats"], 1)


class TestTimeTransitions24hAdversarial(unittest.TestCase):
    """Test full 24-hour time transitions and exact boundary seconds."""

    def test_time_boundary_transitions(self):
        """
        Verify time transitions at:
        - 17:59:59 -> closed
        - 18:00:00 -> yatakoi_dominant
        - 21:59:59 -> yatakoi_dominant
        - 22:00:00 -> transition
        - 23:59:59 -> transition
        - 00:00:00 -> asobibar_shift
        - 04:59:59 -> asobibar_shift
        - 05:00:00 -> closed
        """
        test_cases = [
            # (hour, minute, second, expected_phase, expected_open, expected_flow)
            (17, 59, 59, "closed", False, "idle"),
            (18, 0, 0, "yatakoi_dominant", True, "yatakoi_inflow"),
            (18, 0, 1, "yatakoi_dominant", True, "yatakoi_inflow"),
            (20, 30, 0, "yatakoi_dominant", True, "yatakoi_inflow"),
            (21, 59, 59, "yatakoi_dominant", True, "yatakoi_inflow"),
            (22, 0, 0, "transition", True, "yatakoi_to_asobibar"),
            (22, 0, 1, "transition", True, "yatakoi_to_asobibar"),
            (23, 59, 59, "transition", True, "yatakoi_to_asobibar"),
            (0, 0, 0, "asobibar_shift", True, "asobibar_inflow"),
            (0, 0, 1, "asobibar_shift", True, "asobibar_inflow"),
            (3, 15, 0, "asobibar_shift", True, "asobibar_inflow"),
            (4, 59, 59, "asobibar_shift", True, "asobibar_inflow"),
            (5, 0, 0, "closed", False, "idle"),
            (5, 0, 1, "closed", False, "idle"),
            (12, 0, 0, "closed", False, "idle"),
        ]

        for h, m, s, exp_phase, exp_open, exp_flow in test_cases:
            dt = datetime.datetime(2026, 11, 10, h, m, s)
            with self.subTest(time=f"{h:02d}:{m:02d}:{s:02d}"):
                res = moat_intelligence.get_chayamachi_shift_analysis(
                    now_jst=dt,
                    yatakoi_cust=20,
                    asobibar_cust=10,
                )
                self.assertEqual(res["phase"], exp_phase)
                self.assertEqual(res["is_open_now"], exp_open)
                self.assertEqual(res["flow_direction"], exp_flow)
                self.assertIn("phase_tone", res)
                self.assertIn("phase_label_ja", res)
                self.assertIn("recommendation", res)
                self.assertIn("advice", res)

    def test_timezone_normalization_resilience(self):
        """Test naive datetime, UTC datetime, and timezone offsets."""
        jst = datetime.timezone(datetime.timedelta(hours=9))
        utc = datetime.timezone.utc

        # 21:00 JST is 12:00 UTC
        dt_utc = datetime.datetime(2026, 11, 10, 12, 0, 0, tzinfo=utc)
        res_utc = moat_intelligence.get_chayamachi_shift_analysis(now_jst=dt_utc)
        self.assertEqual(res_utc["phase"], "yatakoi_dominant")

        # 21:00 naive is treated as 21:00 JST
        dt_naive = datetime.datetime(2026, 11, 10, 21, 0, 0)
        res_naive = moat_intelligence.get_chayamachi_shift_analysis(now_jst=dt_naive)
        self.assertEqual(res_naive["phase"], "yatakoi_dominant")

        # None now_jst uses current datetime without crash
        res_none = moat_intelligence.get_chayamachi_shift_analysis(now_jst=None)
        self.assertIsInstance(res_none, dict)
        self.assertIn("phase", res_none)


class TestCrossStoreEnrichmentAndContractCompleteness(unittest.TestCase):
    """Test enrich_stores_with_moat_signals and contract completeness."""

    def test_enrichment_with_malformed_context(self):
        """Ensure enrich_stores_with_moat_signals survives corrupted context_stores."""
        yatakoi_store = {
            "name": "YATAKOI UMEDA",
            "slug": "yatakoi-umeda",
            "women": 10,
            "men": 10,
            "total": 20,
        }
        corrupted_context = [
            None,
            "string_in_list",
            12345,
            {},
            {"pos_telemetry": None},
            {"pos_telemetry": "not_a_dict"},
            {"pos_telemetry": {"telemetry_type": "unknown_type"}},
            yatakoi_store,
        ]

        now_jst = datetime.datetime(2026, 11, 10, 21, 0)
        enriched = moat_intelligence.enrich_stores_with_moat_signals(
            [yatakoi_store],
            now_jst=now_jst,
            context_stores=corrupted_context,
        )
        self.assertEqual(len(enriched), 1)
        self.assertIn("chayamachi_shift", enriched[0])
        self.assertIsInstance(enriched[0]["chayamachi_shift"], dict)

    def test_all_contract_keys_strictly_present(self):
        """Verify all 23 expected keys in get_chayamachi_shift_analysis response."""
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=datetime.datetime(2026, 11, 10, 21, 0),
            yatakoi_cust=10,
            asobibar_cust=5,
        )
        expected_keys = [
            "phase",
            "phase_label",
            "phase_label_ja",
            "phase_tone",
            "flow_direction",
            "flow_label_ja",
            "yatakoi_cust",
            "asobibar_cust",
            "total_cust",
            "yatakoi_seats",
            "asobibar_seats",
            "yatakoi_occ_pct",
            "asobibar_occ_pct",
            "yatakoi_ratio",
            "asobibar_ratio",
            "yatakoi_ratio_pct",
            "asobibar_ratio_pct",
            "shift_score",
            "advice",
            "recommended_action",
            "recommendation",
            "description",
            "is_open_now",
        ]
        for key in expected_keys:
            self.assertIn(key, res, f"Contract key '{key}' must be present in shift analysis")


class TestJinja2StoreDetailRenderingAdversarial(unittest.TestCase):
    """Stress test Jinja2 template rendering under missing/pathological store data."""

    def setUp(self):
        tmpl_dir = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "templates",
        )
        self.env = jinja2.Environment(
            loader=jinja2.FileSystemLoader(tmpl_dir),
            autoescape=True,
        )

    def test_render_store_detail_chayamachi_minimal_data(self):
        from app import app
        from routes._bridge import core

        test_cases = [
            # Case 1: Standard Yatakoi
            {
                "name": "YATAKOI UMEDA",
                "slug": "yatakoi-umeda",
                "women": 12,
                "men": 10,
                "pos_telemetry": {
                    "telemetry_type": "yatakoi_pos",
                    "yatakoi_men": 10,
                    "yatakoi_women": 12,
                    "asobibar_men": 4,
                    "asobibar_women": 3,
                    "yatakoi_seats": 70,
                    "asobibar_seats": 10,
                },
            },
            # Case 2: Asobibar Chayamachi without telemetry (relies on shift analysis)
            {
                "name": "ASOBIBAR 茶屋町",
                "slug": "asobibar-chayamachi",
                "women": 3,
                "men": 4,
            },
            # Case 3: Empty telemetry dict
            {
                "name": "YATAKOI UMEDA",
                "slug": "yatakoi-umeda",
                "women": 0,
                "men": 0,
                "pos_telemetry": {},
            },
            # Case 4: None pos_telemetry
            {
                "name": "YATAKOI UMEDA",
                "slug": "yatakoi-umeda",
                "women": 0,
                "men": 0,
                "pos_telemetry": None,
            },
        ]

        client = app.test_client()
        for idx, store_data in enumerate(test_cases):
            with self.subTest(case_idx=idx, name=store_data["name"]):
                with core.data_lock:
                    core.latest_data["full_data"] = [store_data]
                resp = client.get(f"/store/{store_data['slug']}")
                self.assertEqual(
                    resp.status_code,
                    200,
                    f"Store page for {store_data['slug']} returned {resp.status_code}",
                )
                html = resp.get_data(as_text=True)
                self.assertIn("chayamachi-shift-panel", html)
                self.assertIn("梅田茶屋町ビル 2フロア回遊シフト", html)
                self.assertIn("3F ヤタコイ", html)
                self.assertIn("2F ASOBIBAR", html)


if __name__ == "__main__":
    unittest.main()
