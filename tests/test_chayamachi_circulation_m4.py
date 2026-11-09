"""
Unit and Integration Tests for Milestone 4 (梅田・茶屋町 ASOBIBAR vs ヤタコイ 回遊シフト).

Tests cover:
1. `monitor.get_yatakoi_data` floor split and `pos_telemetry` dict structure.
2. `moat_intelligence.get_chayamachi_shift_analysis` mathematical modeling, phases, and zero division protection.
3. `moat_intelligence.enrich_stores_with_moat_signals` Chayamachi shift injection and cross-store telemetry sharing.
4. `templates/store_detail.html` `#chayamachi-shift-panel` Jinja2 rendering and CSS tokens.
"""

import datetime
import os
import unittest
from unittest.mock import MagicMock, patch
import jinja2

import monitor
import moat_intelligence


class TestYatakoiDataFloorSplit(unittest.TestCase):
    """Test monitor.get_yatakoi_data floor splitting and pos_telemetry integration."""

    @patch("monitor._get_http_session")
    def test_yatakoi_data_normal_split(self, mock_get_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "isDouble": True,
            "males": 6,
            "females": 8,
            "seats": 12,
            "ykMales": 14,
            "ykFemales": 16,
            "ykSeats": 70,
            "vip": 0,
        }
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_get_session.return_value = mock_session

        dt_21 = datetime.datetime(2026, 11, 10, 21, 0)
        res = monitor.get_yatakoi_data(now_jst=dt_21)

        self.assertEqual(len(res), 1)
        store = res[0]
        self.assertEqual(store["name"], "YATAKOI UMEDA")
        # Top-level backward compatibility: sum of both floors
        self.assertEqual(store["men"], 6 + 14)
        self.assertEqual(store["women"], 8 + 16)

        pos = store.get("pos_telemetry")
        self.assertIsInstance(pos, dict)
        self.assertEqual(pos["telemetry_type"], "yatakoi_pos")
        self.assertEqual(pos["asobibar_men"], 6)
        self.assertEqual(pos["asobibar_women"], 8)
        self.assertEqual(pos["yatakoi_men"], 14)
        self.assertEqual(pos["yatakoi_women"], 16)
        self.assertEqual(pos["asobibar_seats"], 12)
        self.assertEqual(pos["yatakoi_seats"], 70)
        self.assertTrue(pos["is_double"])

    @patch("monitor._get_http_session")
    def test_yatakoi_data_empty_dict(self, mock_get_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {}
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_get_session.return_value = mock_session

        res = monitor.get_yatakoi_data(now_jst=datetime.datetime(2026, 11, 10, 21, 0))
        self.assertIsInstance(res, list)
        self.assertEqual(len(res), 1)
        self.assertEqual(res[0]["men"], 0)
        self.assertEqual(res[0]["women"], 0)
        pos = res[0].get("pos_telemetry")
        self.assertIsInstance(pos, dict)
        self.assertEqual(pos["yatakoi_men"], 0)
        self.assertEqual(pos["asobibar_men"], 0)

    @patch("monitor._get_http_session")
    def test_yatakoi_data_outside_business_hours(self, mock_get_session):
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "males": 10,
            "females": 10,
            "ykMales": 20,
            "ykFemales": 20,
            "seats": 10,
            "ykSeats": 70,
        }
        mock_session = MagicMock()
        mock_session.get.return_value = mock_resp
        mock_get_session.return_value = mock_session

        # 14:00 JST is outside business hours (opens at 18:00)
        dt_daytime = datetime.datetime(2026, 11, 10, 14, 0)
        res = monitor.get_yatakoi_data(now_jst=dt_daytime)

        self.assertEqual(len(res), 1)
        # Counts must be zeroed out
        self.assertEqual(res[0]["men"], 0)
        self.assertEqual(res[0]["women"], 0)
        pos = res[0]["pos_telemetry"]
        self.assertEqual(pos["yatakoi_men"], 0)
        self.assertEqual(pos["asobibar_men"], 0)
        # Seat count is preserved
        self.assertEqual(pos["yatakoi_seats"], 70)
        self.assertEqual(pos["asobibar_seats"], 10)

    @patch("monitor._get_http_session")
    def test_yatakoi_data_http_failure(self, mock_get_session):
        mock_session = MagicMock()
        mock_session.get.side_effect = Exception("Firebase timeout")
        mock_get_session.return_value = mock_session

        res = monitor.get_yatakoi_data()
        self.assertEqual(res, [])


class TestChayamachiShiftAnalysis(unittest.TestCase):
    """Test moat_intelligence.get_chayamachi_shift_analysis phase calculation and robustness."""

    def test_early_evening_yatakoi_dominant(self):
        # 20:00 JST (Early evening相席フェーズ)
        dt = datetime.datetime(2026, 11, 10, 20, 0)
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust=35,
            asobibar_cust=5,
            yatakoi_seats=70,
            asobibar_seats=10,
        )
        self.assertIsInstance(res, dict)
        self.assertIn("phase", res)
        self.assertIn("yatakoi", res["phase"])
        self.assertEqual(res["phase"], "yatakoi_dominant")
        self.assertEqual(res["phase_tone"], "emerald")
        self.assertEqual(res["flow_direction"], "yatakoi_inflow")
        self.assertEqual(res["yatakoi_cust"], 35)
        self.assertEqual(res["asobibar_cust"], 5)
        self.assertEqual(res["total_cust"], 40)
        self.assertEqual(res["yatakoi_occ_pct"], 50.0)
        self.assertEqual(res["asobibar_occ_pct"], 50.0)
        self.assertGreater(res["yatakoi_ratio_pct"], res["asobibar_ratio_pct"])
        self.assertTrue(res["is_open_now"])

    def test_late_night_transition(self):
        # 22:30 JST (回遊移行期)
        dt = datetime.datetime(2026, 11, 10, 22, 30)
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust=25,
            asobibar_cust=15,
        )
        self.assertEqual(res["phase"], "transition")
        self.assertEqual(res["phase_tone"], "amber")
        self.assertEqual(res["flow_direction"], "yatakoi_to_asobibar")
        self.assertTrue(res["is_open_now"])

    def test_midnight_asobibar_shift(self):
        # 01:30 JST (深夜ASOBIBARバーシフト)
        dt = datetime.datetime(2026, 11, 10, 1, 30)
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust=10,
            asobibar_cust=25,
        )
        self.assertIn("asobibar", res["phase"])
        self.assertEqual(res["phase_tone"], "purple")
        self.assertTrue(res["is_open_now"])

    def test_daytime_closed(self):
        # 12:00 JST (営業時間外)
        dt = datetime.datetime(2026, 11, 10, 12, 0)
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust=0,
            asobibar_cust=0,
        )
        self.assertEqual(res["phase"], "closed")
        self.assertEqual(res["phase_tone"], "zinc")
        self.assertFalse(res["is_open_now"])

    def test_zero_seats_and_none_protection(self):
        # Zero seats, None cust, None seats must not raise ZeroDivisionError or TypeError
        dt = datetime.datetime(2026, 11, 10, 20, 0)
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=dt,
            yatakoi_cust=None,
            asobibar_cust=None,
            yatakoi_seats=0,
            asobibar_seats=0,
        )
        self.assertIsInstance(res, dict)
        self.assertEqual(res["yatakoi_seats"], 1)
        self.assertEqual(res["asobibar_seats"], 1)
        self.assertEqual(res["yatakoi_occ_pct"], 0.0)
        self.assertEqual(res["asobibar_occ_pct"], 0.0)

    def test_pos_telemetry_extraction(self):
        # Passing pos_telemetry extracts values automatically
        pos = {
            "telemetry_type": "yatakoi_pos",
            "yatakoi_men": 12,
            "yatakoi_women": 14,
            "asobibar_men": 5,
            "asobibar_women": 3,
            "yatakoi_seats": 70,
            "asobibar_seats": 15,
        }
        res = moat_intelligence.get_chayamachi_shift_analysis(
            now_jst=datetime.datetime(2026, 11, 10, 20, 0),
            pos_telemetry=pos,
        )
        self.assertEqual(res["yatakoi_cust"], 26)
        self.assertEqual(res["asobibar_cust"], 8)
        self.assertEqual(res["total_cust"], 34)
        self.assertEqual(res["yatakoi_seats"], 70)
        self.assertEqual(res["asobibar_seats"], 15)


class TestChayamachiEnrichment(unittest.TestCase):
    """Test moat_intelligence.enrich_stores_with_moat_signals injecting chayamachi_shift."""

    def test_enrich_chayamachi_stores_with_telemetry(self):
        yatakoi_store = {
            "name": "YATAKOI UMEDA",
            "slug": "yatakoi-umeda",
            "women": 15,
            "men": 12,
            "total": 27,
            "is_open": True,
            "pos_telemetry": {
                "telemetry_type": "yatakoi_pos",
                "yatakoi_men": 12,
                "yatakoi_women": 15,
                "asobibar_men": 4,
                "asobibar_women": 3,
                "yatakoi_seats": 70,
                "asobibar_seats": 10,
            },
        }
        asobibar_store = {
            "name": "ASOBIBAR 茶屋町",
            "slug": "asobibar-chayamachi",
            "women": 3,
            "men": 4,
            "total": 7,
            "is_open": True,
        }
        stores = [yatakoi_store, asobibar_store]

        now_jst = datetime.datetime(2026, 11, 10, 20, 0)
        enriched = moat_intelligence.enrich_stores_with_moat_signals(
            stores,
            now_jst=now_jst,
            context_stores=stores,
        )

        # Both stores should receive chayamachi_shift
        self.assertIn("chayamachi_shift", enriched[0])
        self.assertIn("chayamachi_shift", enriched[1])

        yk_shift = enriched[0]["chayamachi_shift"]
        asb_shift = enriched[1]["chayamachi_shift"]

        self.assertEqual(yk_shift["phase"], "yatakoi_dominant")
        self.assertEqual(asb_shift["phase"], "yatakoi_dominant")
        self.assertEqual(asb_shift["yatakoi_cust"], 27)
        self.assertEqual(asb_shift["asobibar_cust"], 7)

        # ASOBIBAR should have inherited pos_telemetry from YATAKOI in context
        self.assertIn("pos_telemetry", enriched[1])
        self.assertEqual(enriched[1]["pos_telemetry"]["telemetry_type"], "yatakoi_pos")


class TestChayamachiDetailUIRendering(unittest.TestCase):
    """Test store_detail.html chayamachi shift panel elements and Jinja2 rendering."""

    def test_template_contains_chayamachi_panel_markup(self):
        tmpl_path = os.path.join(
            os.path.dirname(os.path.dirname(__file__)),
            "templates",
            "store_detail.html",
        )
        with open(tmpl_path, "r", encoding="utf-8") as f:
            html = f.read()

        self.assertIn("id=\"chayamachi-shift-panel\"", html)
        self.assertIn(".chayamachi-shift-panel", html)
        self.assertIn("梅田茶屋町ビル 2フロア回遊シフト", html)
        self.assertIn("3F ヤタコイ", html)
        self.assertIn("2F ASOBIBAR", html)
        self.assertIn("chayamachi-ratio-track", html)

    def test_flask_renders_chayamachi_shift_panel(self):
        from app import app
        from routes._bridge import core

        with core.data_lock:
            core.latest_data["full_data"] = [
                {
                    "name": "YATAKOI UMEDA",
                    "women": 14,
                    "men": 11,
                    "pos_telemetry": {
                        "telemetry_type": "yatakoi_pos",
                        "yatakoi_men": 11,
                        "yatakoi_women": 14,
                        "asobibar_men": 3,
                        "asobibar_women": 2,
                        "yatakoi_seats": 70,
                        "asobibar_seats": 10,
                    },
                },
                {
                    "name": "ASOBIBAR 茶屋町",
                    "women": 2,
                    "men": 3,
                },
            ]

        client = app.test_client()
        resp = client.get("/store/yatakoi-umeda")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("chayamachi-shift-panel", html)
        self.assertIn("梅田茶屋町ビル 2フロア回遊シフト", html)
        self.assertIn("3F ヤタコイ", html)
        self.assertIn("2F ASOBIBAR", html)

        # Test ASOBIBAR 茶屋町 store detail page
        resp_asb = client.get("/store/asobibar-chayamachi")
        self.assertEqual(resp_asb.status_code, 200)
        html_asb = resp_asb.get_data(as_text=True)
        self.assertIn("chayamachi-shift-panel", html_asb)
        self.assertIn("梅田茶屋町ビル 2フロア回遊シフト", html_asb)


if __name__ == "__main__":
    unittest.main()
