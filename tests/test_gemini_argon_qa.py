"""
tests/test_gemini_argon_qa.py - Unit and Integration Tests for Gemini 4 Argon & Visual QA Gate (M1)

Verifies:
1. R1: gemini-4-argon pipeline support, thinkingConfig (LOW/MEDIUM/HIGH), token budgeting,
   model discovery priority, and graceful failover to Pro/Flash.
2. R2: visual_qa_gate module, Pillow frame extraction, multimodal inspection parsing,
   alternative template rerendering, and strict Fail-Open resiliency.
3. Instagram Reels & OGP integration with pre-delivery visual inspection.
"""

import os
import sys
import json
import base64
import datetime
import unittest
from unittest import mock
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DISABLE_SHEETS", "1")

import sns_editorial
import visual_qa_gate
import instagram_poster
import ogp_generator
import reel_generator


class TestGemini4ArgonPipeline(unittest.TestCase):
    """Test suite for R1: gemini-4-argon support and failover behavior."""

    def setUp(self):
        self.sample_prompt = '相席ナビのリアルタイム集計を分析し、JSON {"headline": "..."} で出力せよ。'

    def test_default_gemini_models_starts_with_carbon(self):
        """Verify DEFAULT_GEMINI_MODELS prioritizes gemini-4-carbon with gemini-4-argon fallback."""
        self.assertIn("gemini-4-carbon", sns_editorial.DEFAULT_GEMINI_MODELS)
        self.assertIn("gemini-4-argon", sns_editorial.DEFAULT_GEMINI_MODELS)
        self.assertEqual(sns_editorial.DEFAULT_GEMINI_MODELS[0], "gemini-4-carbon")
        self.assertEqual(sns_editorial.DEFAULT_GEMINI_MODELS[1], "gemini-4-argon")

    def test_build_endpoint_request_carbon_and_argon_thinking_levels(self):
        """Verify thinkingLevel (LOW, MEDIUM, HIGH) and output tokens expansion for Carbon and Argon."""
        for m_name in ("gemini-4-carbon", "gemini-4-argon"):
            # High thinking level (>=2048 budget)
            url, headers, params, payload = sns_editorial._build_endpoint_request(
                "ai_studio",
                m_name,
                "fake_key",
                self.sample_prompt,
                max_output_tokens=3000,
                thinking_budget=2048,
            )
            self.assertIn(f"{m_name}:generateContent", url)
            self.assertEqual(headers["x-goog-api-key"], "fake_key")
            gen_cfg = payload["generationConfig"]
            self.assertEqual(gen_cfg.get("thinkingConfig"), {"thinkingLevel": "HIGH"})
            self.assertGreaterEqual(gen_cfg.get("maxOutputTokens", 0), 4096)

            # Medium thinking level (1024 <= budget < 2048)
            _, _, _, payload_med = sns_editorial._build_endpoint_request(
                "ai_studio",
                m_name,
                "fake_key",
                self.sample_prompt,
                max_output_tokens=2000,
                thinking_budget=1024,
            )
            self.assertEqual(payload_med["generationConfig"].get("thinkingConfig"), {"thinkingLevel": "MEDIUM"})

            # Low thinking level (< 1024 budget)
            _, _, _, payload_low = sns_editorial._build_endpoint_request(
                "ai_studio",
                m_name,
                "fake_key",
                self.sample_prompt,
                max_output_tokens=2000,
                thinking_budget=512,
            )
            self.assertEqual(payload_low["generationConfig"].get("thinkingConfig"), {"thinkingLevel": "LOW"})

    @mock.patch("requests.get")
    def test_discover_ai_studio_models_carbon_priority(self, mock_get):
        """Verify _discover_ai_studio_models discovers and ranks gemini-4-carbon first, gemini-4-argon second."""
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "models": [
                {
                    "name": "models/gemini-2.5-flash",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-3.1-pro-preview",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-4-argon",
                    "supportedGenerationMethods": ["generateContent"],
                },
                {
                    "name": "models/gemini-4-carbon",
                    "supportedGenerationMethods": ["generateContent"],
                },
            ]
        }
        mock_get.return_value = mock_resp

        discovered = sns_editorial._discover_ai_studio_models("test_key_discovery_123")
        self.assertGreater(len(discovered), 0)
        self.assertEqual(discovered[0], "gemini-4-carbon")
        self.assertEqual(discovered[1], "gemini-4-argon")
        self.assertIn("gemini-3.1-pro-preview", discovered)
        self.assertIn("gemini-2.5-flash", discovered)

    @mock.patch("sns_editorial.get_gemini_api_key", return_value="fake_gemini_key")
    @mock.patch("sns_editorial.get_claude_api_key", return_value="")
    @mock.patch("requests.post")
    def test_call_editorial_ai_json_carbon_failover_to_argon_and_pro(self, mock_post, mock_c_key, mock_g_key):
        """Verify failover: when gemini-4-carbon encounters 429/404, fallback to Argon/Pro succeeds."""
        # 1st call (gemini-4-carbon) returns 429 Rate Limit
        resp_429 = mock.MagicMock()
        resp_429.status_code = 429
        resp_429.text = "Quota exceeded for gemini-4-carbon"

        # 2nd call (gemini-4-argon) returns 404
        resp_404 = mock.MagicMock()
        resp_404.status_code = 404
        resp_404.text = "gemini-4-argon not found"

        # 3rd call (gemini-3.1-pro-preview) succeeds
        resp_ok = mock.MagicMock()
        resp_ok.status_code = 200
        resp_ok.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": json.dumps({"headline": "週末ピーク直前！梅田ラウンジ速報"})}
                        ]
                    }
                }
            ]
        }
        mock_post.side_effect = [resp_429, resp_404, resp_ok]

        parsed, model_used, err = sns_editorial.call_editorial_ai_json(
            prompt="テストプロンプト",
            allow_blocking=True,
            total_deadline_sec=10.0,
            per_call_timeout_sec=5.0,
        )

        self.assertIsNotNone(parsed)
        self.assertEqual(parsed.get("headline"), "週末ピーク直前！梅田ラウンジ速報")
        # Ensure gemini-4-carbon was attempted first
        self.assertIn("gemini-4-carbon", mock_post.call_args_list[0][0][0])
        # Second call was gemini-4-argon fallback
        self.assertIn("gemini-4-argon", mock_post.call_args_list[1][0][0])
        # Third call was Pro fallback
        self.assertIn("gemini-3.1-pro-preview", mock_post.call_args_list[2][0][0])


class TestVisualQAGate(unittest.TestCase):
    """Test suite for R2: visual_qa_gate module and Fail-Open behaviors."""

    def setUp(self):
        self.sample_stores = [
            {"name": "オリエンタルラウンジ 渋谷駅前", "men": 30, "women": 32, "area": "渋谷", "status": "open"},
            {"name": "ag 渋谷", "men": 20, "women": 24, "area": "渋谷", "status": "open"},
            {"name": "JIS 新宿", "men": 15, "women": 18, "area": "新宿", "status": "open"},
        ]

    def test_qa_models_fallback_chain_prioritizes_carbon(self):
        """Verify QA_MODELS_FALLBACK_CHAIN has gemini-4-carbon at rank 0 and gemini-4-argon at rank 1."""
        self.assertIn("gemini-4-carbon", visual_qa_gate.QA_MODELS_FALLBACK_CHAIN)
        self.assertIn("gemini-4-argon", visual_qa_gate.QA_MODELS_FALLBACK_CHAIN)
        self.assertEqual(visual_qa_gate.QA_MODELS_FALLBACK_CHAIN[0], "gemini-4-carbon")
        self.assertEqual(visual_qa_gate.QA_MODELS_FALLBACK_CHAIN[1], "gemini-4-argon")

    def test_inspect_reel_fail_open_when_no_api_key(self):
        """Fail-Open test: When API key is missing, visual QA must pass immediately."""
        with mock.patch("sns_editorial.get_gemini_api_key", return_value=""):
            res = visual_qa_gate.inspect_reel_visual_quality(
                self.sample_stores, reel_type="live_top5", api_key=""
            )
            self.assertTrue(res.get("pass"))
            self.assertEqual(res.get("reason"), "api_key_not_set")
            self.assertEqual(res.get("overall_score"), 100)

    @mock.patch("requests.post", side_effect=requests.exceptions.Timeout("API request timed out"))
    def test_inspect_reel_fail_open_on_timeout(self, mock_post):
        """Fail-Open test: When Gemini API times out, QA passes without throwing exceptions."""
        res = visual_qa_gate.inspect_reel_visual_quality(
            self.sample_stores,
            reel_type="live_top5",
            timeout_sec=2.0,
            api_key="dummy_key",
        )
        self.assertTrue(res.get("pass"))
        self.assertIn("timeout", res.get("reason", "").lower())
        self.assertEqual(res.get("overall_score"), 100)

    @mock.patch("requests.post")
    def test_inspect_reel_pass_verdict(self, mock_post):
        """Verify successful QA parsing when Gemini approves the reel frames."""
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "pass": True,
                                    "overall_score": 92,
                                    "hook_strength": 90,
                                    "contrast_readability": 95,
                                    "text_overflow": False,
                                    "text_overlap": False,
                                    "brand_compliance": True,
                                    "issues": [],
                                    "recommended_template": "live_top5",
                                })
                            }
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        res = visual_qa_gate.inspect_reel_visual_quality(
            self.sample_stores,
            reel_type="live_top5",
            api_key="dummy_key",
        )
        self.assertTrue(res.get("pass"))
        self.assertEqual(res.get("overall_score"), 92)
        self.assertEqual(res.get("hook_strength"), 90)
        self.assertFalse(res.get("text_overflow"))
        self.assertFalse(res.get("text_overlap"))
        self.assertEqual(res.get("model_used"), "gemini-4-carbon")

    @mock.patch("requests.post")
    def test_inspect_reel_rejection_and_alternative_template(self, mock_post):
        """Verify QA rejection triggers pass=False and returns recommended alternative template."""
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {
                                "text": json.dumps({
                                    "pass": False,
                                    "overall_score": 52,
                                    "hook_strength": 40,
                                    "contrast_readability": 60,
                                    "text_overflow": True,
                                    "text_overlap": False,
                                    "brand_compliance": True,
                                    "issues": ["Header safe-zone clipping on top 15%"],
                                    "recommended_template": "area_battle",
                                })
                            }
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        res = visual_qa_gate.inspect_reel_visual_quality(
            self.sample_stores,
            reel_type="live_top5",
            api_key="dummy_key",
        )
        self.assertFalse(res.get("pass"))
        self.assertEqual(res.get("overall_score"), 52)
        self.assertTrue(res.get("text_overflow"))
        self.assertEqual(res.get("recommended_template"), "area_battle")

    @mock.patch("visual_qa_gate.inspect_reel_visual_quality")
    def test_enforce_reel_visual_qa_switches_template_on_rejection(self, mock_inspect):
        """Verify enforce_reel_visual_qa automatically swaps template on rejection."""
        mock_inspect.return_value = {
            "pass": False,
            "overall_score": 50,
            "recommended_template": "area_battle",
            "issues": ["Low contrast"],
        }
        final_rt, qa = visual_qa_gate.enforce_reel_visual_qa(
            self.sample_stores, reel_type="live_top5"
        )
        self.assertEqual(final_rt, "area_battle")
        self.assertFalse(qa.get("pass"))

    def test_inspect_image_visual_quality_fail_open_on_empty(self):
        """Fail-Open test: inspect_image_visual_quality returns pass=True when given empty bytes."""
        res = visual_qa_gate.inspect_image_visual_quality(b"", image_type="ogp")
        self.assertTrue(res.get("pass"))
        self.assertEqual(res.get("overall_score"), 100)

    @mock.patch("requests.post")
    def test_inspect_image_visual_quality_carbon_thinking_config(self, mock_post):
        """Verify inspect_image_visual_quality applies thinkingConfig LOW and maxOutputTokens 2048 to Carbon."""
        mock_resp = mock.MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [
                {
                    "content": {
                        "parts": [
                            {"text": json.dumps({"pass": True, "overall_score": 95, "issues": []})}
                        ]
                    }
                }
            ]
        }
        mock_post.return_value = mock_resp

        dummy_png = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\x00\x00\x00\x01\x08\x06\x00\x00\x00\x1f\x15c4\x00\x00\x00\nIDATx\x9cc\x00\x01\x00\x00\x05\x00\x01\r\n-\xb4\x00\x00\x00\x00IEND\xaeB`\x82"
        res = visual_qa_gate.inspect_image_visual_quality(
            dummy_png, image_type="ogp", api_key="dummy_key"
        )
        self.assertTrue(res.get("pass"))
        self.assertEqual(res.get("model_used"), "gemini-4-carbon")
        called_json = mock_post.call_args[1].get("json") or {}
        gen_cfg = called_json.get("generationConfig") or {}
        self.assertEqual(gen_cfg.get("thinkingConfig"), {"thinkingLevel": "LOW"})
        self.assertEqual(gen_cfg.get("maxOutputTokens"), 2048)


class TestInstagramAndOgpIntegration(unittest.TestCase):
    """Test suite for Instagram Reels and OGP pre-delivery visual QA integration."""

    def setUp(self):
        self.sample_stores = [
            {"name": "オリエンタルラウンジ 難波", "men": 28, "women": 30, "area": "大阪", "status": "open"},
            {"name": "JIS 難波", "men": 18, "women": 22, "area": "大阪", "status": "open"},
        ]

    @mock.patch("reel_generator.pin_shared_reel_mp4_slot")
    @mock.patch("reel_generator.render_reel_mp4_bytes", return_value=b"fake_mp4_data_over_1024_bytes_" + b"0" * 1500)
    @mock.patch("instagram_poster.post_reel_to_instagram")
    @mock.patch("visual_qa_gate.inspect_reel_visual_quality")
    def test_instagram_auto_post_rerenders_on_qa_reject(self, mock_qa, mock_post_reel, mock_render_mp4, mock_pin):
        """Verify auto_post_scheduled_reel rerenders alternative template if QA rejects the initial template."""
        # Visual QA rejects live_top5 and suggests ladies_zero_wait
        mock_qa.return_value = {
            "pass": False,
            "overall_score": 55,
            "recommended_template": "ladies_zero_wait",
            "issues": ["Text overflow in scene 1"],
        }
        mock_post_reel.return_value = {"status": "success", "id": "reel_post_999", "ok": True}

        res = instagram_poster.auto_post_scheduled_reel(
            self.sample_stores,
            reel_type="live_top5",
            force=True,
            now_jst=datetime.datetime(2026, 11, 8, 22, 0),
        )

        self.assertEqual(res.get("status"), "success")
        # Template should have switched to ladies_zero_wait
        self.assertEqual(res.get("reel_type"), "ladies_zero_wait")
        self.assertIn("visual_qa", res)
        self.assertFalse(res["visual_qa"].get("pass"))

    def test_ogp_visual_qa_fail_open_guarantee(self):
        """Verify inspect_ogp_visual_quality and generate_verified_live_ogp_png never fail."""
        png_bytes, qa_res = ogp_generator.generate_verified_live_ogp_png(
            self.sample_stores,
            s_param="22:00",
            now_jst=datetime.datetime(2026, 11, 8, 22, 0),
        )
        self.assertIsInstance(png_bytes, bytes)
        self.assertGreater(len(png_bytes), 1000)
        self.assertIsInstance(qa_res, dict)
        self.assertTrue(qa_res.get("pass"))


if __name__ == "__main__":
    unittest.main()
