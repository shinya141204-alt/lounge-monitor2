# -*- coding: utf-8 -*-
"""
tests/test_adversarial_stress_m4.py - Empirical Challenger Stress & Adversarial Test Suite

Tests extreme boundary conditions, adversarial inputs, fault injection, and fail-open guarantees for:
- visual_qa_gate.py (API key missing, timeouts, corrupted images, broken JSON, malformed types)
- second_venues.py (None, empty, symbols, script injection, path traversal, Unicode, huge strings)
"""

import unittest
from unittest.mock import patch, MagicMock
import requests
import json

import visual_qa_gate
import second_venues


class TestVisualQAGateAdversarial(unittest.TestCase):
    """Adversarial stress testing on visual_qa_gate.py."""

    def test_inspect_reel_no_api_key(self):
        """API key missing or empty must safely fail-open without blocking or hanging."""
        with patch.dict("os.environ", {}, clear=True), \
             patch("sns_editorial.get_gemini_api_key", return_value=""):
            # 1. api_key=None
            res1 = visual_qa_gate.inspect_reel_visual_quality([], reel_type="live_top5", api_key=None)
            self.assertTrue(res1["pass"], "Must fail-open (pass=True) when API key is None")
            self.assertEqual(res1["model_used"], "fail_open")
            self.assertEqual(res1["reason"], "api_key_not_set")

            # 2. api_key=""
            res2 = visual_qa_gate.inspect_reel_visual_quality([], reel_type="area_battle", api_key="")
            self.assertTrue(res2["pass"], "Must fail-open (pass=True) when API key is empty string")
            self.assertEqual(res2["model_used"], "fail_open")

            # 3. api_key whitespace only
            res3 = visual_qa_gate.inspect_reel_visual_quality([], reel_type="live_top5", api_key="   \n\t  ")
            self.assertTrue(res3["pass"], "Must fail-open (pass=True) when API key is whitespace")
            self.assertEqual(res3["model_used"], "fail_open")

    def test_inspect_image_no_api_key_or_empty_bytes(self):
        """inspect_image_visual_quality with missing key or empty bytes must fail-open."""
        with patch("sns_editorial.get_gemini_api_key", return_value=""):
            # Empty bytes and no key
            res = visual_qa_gate.inspect_image_visual_quality(b"", api_key="")
            self.assertTrue(res["pass"])
            self.assertEqual(res["model_used"], "fail_open")

            # Valid-looking bytes but no key
            res2 = visual_qa_gate.inspect_image_visual_quality(b"\x89PNG\r\n\x1a\nfake", api_key="")
            self.assertTrue(res2["pass"])
            self.assertEqual(res2["model_used"], "fail_open")

            # Key present but image bytes empty
            res3 = visual_qa_gate.inspect_image_visual_quality(b"", api_key="fake_key_123")
            self.assertTrue(res3["pass"])
            self.assertEqual(res3["model_used"], "fail_open")

    def test_inspect_reel_corrupted_frame_rendering(self):
        """When frame rendering raises an unexpected exception, fail-open must activate."""
        with patch("reel_generator.render_reel_preview_frame_jpeg", side_effect=RuntimeError("Pillow font crash")):
            res = visual_qa_gate.inspect_reel_visual_quality([], reel_type="live_top5", api_key="valid_key")
            self.assertTrue(res["pass"], "Must fail-open when frame generation crashes")
            self.assertEqual(res["model_used"], "fail_open")
            self.assertIn("frame_extract_error", res["reason"])

    def test_inspect_reel_empty_rendered_frames(self):
        """When frame rendering returns None or empty bytes, fail-open must activate."""
        with patch("reel_generator.render_reel_preview_frame_jpeg", return_value=b""):
            res = visual_qa_gate.inspect_reel_visual_quality([], reel_type="live_top5", api_key="valid_key")
            self.assertTrue(res["pass"])
            self.assertEqual(res["model_used"], "fail_open")
            self.assertEqual(res["reason"], "empty_frames")

    def test_inspect_reel_network_timeouts_all_models(self):
        """When requests to all models time out, execution must not hang and must fail-open."""
        mock_frame = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"
        with patch("reel_generator.render_reel_preview_frame_jpeg", return_value=mock_frame), \
             patch("requests.post", side_effect=requests.exceptions.Timeout("Connection timed out after 7.0s")):
            res = visual_qa_gate.inspect_reel_visual_quality([], reel_type="live_top5", timeout_sec=10.0, api_key="fake_key")
            self.assertTrue(res["pass"], "Must fail-open on timeouts")
            self.assertEqual(res["model_used"], "fail_open")
            self.assertIn("timeout", res["reason"])

    def test_inspect_image_network_exceptions_all_models(self):
        """When inspect_image encounters HTTP errors or connection resets, it must fail-open."""
        mock_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        with patch("requests.post", side_effect=ConnectionResetError("Peer reset connection")):
            res = visual_qa_gate.inspect_image_visual_quality(mock_png, api_key="fake_key")
            self.assertTrue(res["pass"], "Must fail-open on connection errors")
            self.assertEqual(res["model_used"], "fail_open")

    def test_broken_and_adversarial_json_responses(self):
        """Test API returning garbage, truncated JSON, HTML error pages, and type mismatch values."""
        mock_frame = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"

        # These payloads must trigger fail-open (pass=True) via syntax error, type error, or empty candidates
        fail_open_payloads = [
            # HTML 502 / Cloudflare block page
            "<html><head><title>502 Bad Gateway</title></head><body><h1>Bad Gateway</h1></body></html>",
            # Truncated JSON
            '{"pass": true, "overall_score": 90, "hook_st',
            # Empty text
            "",
            # String instead of int for scores (triggers ValueError inside int(), caught by except Exception)
            '{"pass": true, "overall_score": "VERY_HIGH", "hook_strength": "awesome"}',
            # Array instead of int (triggers TypeError inside int(), caught by except Exception)
            '{"pass": true, "overall_score": [100, 90]}',
            # Completely empty JSON object (defaults to pass=True)
            '{}',
            # Nested markdown corruption
            '```json\n```json\n{"pass": true}\n```\n```',
        ]

        for payload_text in fail_open_payloads:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "candidates": [
                    {
                        "content": {
                            "parts": [{"text": payload_text}]
                        }
                    }
                ]
            }

            with patch("reel_generator.render_reel_preview_frame_jpeg", return_value=mock_frame), \
                 patch("requests.post", return_value=mock_resp):
                # Must never raise an unhandled exception or hang, and must safely fail-open
                res = visual_qa_gate.inspect_reel_visual_quality([], reel_type="live_top5", api_key="fake_key")
                self.assertIsInstance(res, dict)
                self.assertIn("pass", res)
                self.assertIn("overall_score", res)
                self.assertTrue(res["pass"], f"Failed to safely fail-open on payload: {payload_text!r}")

    def test_inspect_image_broken_json_payloads(self):
        """inspect_image_visual_quality must fail-open when Gemini returns broken JSON or errors."""
        mock_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
        broken_payloads = [
            "<html>Error</html>",
            '{"incomplete":',
            "",
            '{"overall_score": "BROKEN"}',
        ]
        for payload in broken_payloads:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": payload}]}}]
            }
            with patch("requests.post", return_value=mock_resp):
                res = visual_qa_gate.inspect_image_visual_quality(mock_png, api_key="fake_key")
                self.assertIsInstance(res, dict)
                self.assertTrue(res["pass"], f"inspect_image must fail-open on {payload!r}")
                self.assertEqual(res["model_used"], "fail_open")

    def test_null_fields_json_handling(self):
        """Test how visual_qa handles explicit null fields in parsed JSON without crashing."""
        mock_frame = b"\xff\xd8\xff\xe0\x00\x10JFIF\x00\x01\x01\x00\x00\x01\x00\x01\x00\x00\xff\xdb"
        null_payload = '{"pass": null, "overall_score": null, "recommended_template": null}'

        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [{"text": null_payload}]
                    }
                }
            ]
        }

        with patch("reel_generator.render_reel_preview_frame_jpeg", return_value=mock_frame), \
             patch("requests.post", return_value=mock_resp):
            res = visual_qa_gate.inspect_reel_visual_quality([], reel_type="live_top5", api_key="fake_key")
            # Should safely return a dictionary without crashing or throwing unhandled exception
            self.assertIsInstance(res, dict)
            self.assertIn("overall_score", res)
            self.assertEqual(res["overall_score"], 85)
            # When recommended_template is null, it safely defaults to ALTERNATIVE_REEL_TEMPLATES[rt]
            self.assertEqual(res["recommended_template"], "area_battle")

    def test_enforce_reel_visual_qa_rejection_and_fallback_resilience(self):
        """enforce_reel_visual_qa must auto-switch templates on rejection, and never crash if 2nd pass fails."""
        # 1st call rejects (pass: False, overall_score: 40)
        rejection_res = {
            "pass": False,
            "overall_score": 40,
            "hook_strength": 30,
            "contrast_readability": 40,
            "text_overflow": True,
            "text_overlap": False,
            "brand_compliance": True,
            "issues": ["Severe text overflow"],
            "recommended_template": "area_battle",
            "model_used": "gemini-4-carbon",
            "reason": "inspected",
            "latency_sec": 0.5,
        }

        # 2nd call throws an unexpected error
        with patch("visual_qa_gate.inspect_reel_visual_quality", side_effect=[rejection_res, RuntimeError("2nd pass crash")]):
            final_type, qa_res = visual_qa_gate.enforce_reel_visual_qa([], reel_type="live_top5", api_key="fake_key")
            self.assertEqual(final_type, "area_battle", "Must switch to recommended template")
            self.assertEqual(qa_res["overall_score"], 40)


class TestSecondVenuesAdversarial(unittest.TestCase):
    """Adversarial stress testing on second_venues.py."""

    def test_get_second_venues_boundary_and_none_inputs(self):
        """Test store_name=None, empty, whitespace only."""
        test_inputs = [None, "", "   ", "\t\n  \r\n"]
        for inp in test_inputs:
            res = second_venues.get_second_venues_for_store(inp)
            self.assertIsInstance(res, dict)
            self.assertTrue(res["is_fallback"], f"Should fallback for input: {inp!r}")
            self.assertFalse(res["is_store_curated"])
            self.assertEqual(len(res["spots"]), 4, "Should generate 4 smart fallback spots")
            for sp in res["spots"]:
                self.assertTrue(sp["is_dynamic"])
                self.assertTrue(sp["map_url"].startswith("https://www.google.com/maps/search/?api=1&query="))
                self.assertTrue(sp["line_share_url"].startswith("https://line.me/R/msg/text/?"))
                self.assertTrue(sp["reservation_url"].startswith("/link/second_venue_reservation?from="))

    def test_get_second_venues_nonexistent_and_huge_inputs(self):
        """Test nonexistent stores and gigantic strings (DoS / memory stress)."""
        huge_name = "架空店舗ラウンジ_" + ("あ" * 10000)
        res = second_venues.get_second_venues_for_store(huge_name)
        self.assertIsInstance(res, dict)
        self.assertTrue(res["is_fallback"])
        self.assertEqual(len(res["spots"]), 4)
        # Verify URL is safely generated and bounded
        for sp in res["spots"]:
            self.assertTrue(len(sp["map_url"]) > 0)

    def test_injection_and_symbol_inputs(self):
        """Test path traversal, XSS scripts, SQL injections, and pure symbols."""
        pure_hostile_inputs = [
            "../../../../../etc/passwd",
            "<script>alert('xss')</script>",
            "'; DROP TABLE stores; --",
            "!@#$%^&*()_+-=[]{}|;':\",./<>?`~",
            "https://evil.com/?phishing=true",
        ]
        for hostile in pure_hostile_inputs:
            res = second_venues.get_second_venues_for_store(hostile)
            self.assertIsInstance(res, dict)
            self.assertTrue(res["is_fallback"], f"Pure symbol/attack input must fallback: {hostile}")
            self.assertEqual(len(res["spots"]), 4)
            for sp in res["spots"]:
                self.assertIn("https://www.google.com/maps/search/?api=1&query=", sp["map_url"])
                self.assertIn("https://line.me/R/msg/text/?", sp["line_share_url"])

    def test_unicode_and_mixed_symbol_inputs(self):
        """Test Unicode emojis, null characters, zero-width spaces, and foreign alphabets."""
        unicode_inputs = [
            "🍸🍷BAR🍾🎉",
            "渋谷\u200b本店\u0000",
            "カンナム 롸운지 🇰🇷",
        ]
        for inp in unicode_inputs:
            res = second_venues.get_second_venues_for_store(inp)
            self.assertIsInstance(res, dict)
            self.assertGreaterEqual(len(res["spots"]), 3)
            for sp in res["spots"]:
                self.assertIn("https://www.google.com/maps/search/?api=1&query=", sp["map_url"])
                self.assertIn("https://line.me/R/msg/text/?", sp["line_share_url"])

    def test_all_63_stores_resolvability(self):
        """Verify all 63 stores are resolvable and return curated spots (not fallback)."""
        import store_master
        master_stores = store_master.load()
        self.assertEqual(len(master_stores), 63, "Must have exactly 63 stores in master")

        for s_name in master_stores.keys():
            res = second_venues.get_second_venues_for_store(s_name)
            self.assertFalse(res["is_fallback"], f"Store {s_name} should have curated spots, got fallback")
            self.assertTrue(res["is_store_curated"], f"Store {s_name} should be store_curated")
            self.assertGreaterEqual(len(res["spots"]), 2, f"Store {s_name} should have >= 2 spots")

    def test_internal_helpers_robustness(self):
        """Directly stress helper functions with hostile inputs."""
        hostile_args = [None, "", 12345, [], {}, "!?#$", "   "]
        for arg in hostile_args:
            # resolve_store_slug
            slug = second_venues.resolve_store_slug(arg if isinstance(arg, str) or arg is None else str(arg))
            self.assertTrue(slug is None or isinstance(slug, str))

            # get_area_key_for_store
            area = second_venues.get_area_key_for_store(arg if isinstance(arg, str) else "")
            self.assertTrue(area is None or isinstance(area, str))

            # get_store_anchor
            anchor = second_venues.get_store_anchor(arg if isinstance(arg, str) or arg is None else str(arg))
            self.assertIsInstance(anchor, str)
            self.assertTrue(len(anchor) > 0)

            # URLs
            url = second_venues.get_google_maps_intent_url(str(arg or ""))
            self.assertTrue(url.startswith("https://www.google.com/maps/search/?api=1&query="))
            line_url = second_venues.get_line_share_url(str(arg or ""))
            self.assertTrue(line_url.startswith("https://line.me/R/msg/text/?"))


if __name__ == "__main__":
    unittest.main()
