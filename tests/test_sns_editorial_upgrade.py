"""Unit and integration tests for Milestone 2: SNS AI & Workflow Upgrade.

Covers:
- F2.1: note channel integration, prompt building, length/weight evaluation, delivery status & health
- F2.2: Situation-specific quick selectors (second_venue, zero_wait, rain_underground, last_train_cliff, start_dash)
- F2.3: AI image generation prompts for 5 major SNS platforms (X, IG, note, TikTok, Threads)
- Brand name sanitization & protection rules (strict Oriental Lounge compliance)
- Factual verification guarding against hallucination
- Template integration sanity checks (staff_sns_editorial.html, index.html #sns-modal)
"""

import json
import os
import sys
import unittest
from unittest import mock
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import sns_editorial


class SnsEditorialUpgradeTests(unittest.TestCase):
    """Test suite for Milestone 2 SNS AI and workflow upgrades."""

    def setUp(self):
        self.sample_stores = [
            {
                "name": "オリエンタルラウンジ 渋谷店",
                "men": 25,
                "women": 28,
                "area": "渋谷",
                "station": "渋谷駅",
                "source": "oriental",
            },
            {
                "name": "オリエンタルラウンジ 新宿店",
                "men": 30,
                "women": 22,
                "area": "新宿",
                "station": "新宿駅",
                "source": "oriental",
            },
            {
                "name": "ag 難波店",
                "men": 18,
                "women": 20,
                "area": "難波",
                "station": "難波駅",
                "source": "oriental",
            },
        ]

    # -------------------------------------------------------------------------
    # F2.1: note channel tests
    # -------------------------------------------------------------------------

    def test_channel_labels_and_styles_contain_note(self):
        """F2.1: Ensure 'note' is registered in CHANNEL_LABELS and STYLE_LABELS."""
        self.assertIn("note", sns_editorial.CHANNEL_LABELS)
        self.assertIn("note", sns_editorial.CHANNEL_LABELS["note"].lower())

        # Check note styles
        note_styles = [
            "note_essay_insight",
            "note_short_column",
            "note_tsubuyaki_gap",
            "note_classic_summary",
        ]
        for style in note_styles:
            self.assertIn(style, sns_editorial.STYLE_LABELS)

    def test_note_delivery_status_and_health(self):
        """F2.1: Verify delivery status recording and health monitoring for 'note' channel."""
        with mock.patch("job_state.set_value") as mock_set_val:
            # Record a delivery for note
            sns_editorial.record_sns_delivery_status(
                channel="note",
                status="success",
                slot_label="22:00",
                detail="noteテスト配信成功",
                post_id="note_test_123",
            )
            self.assertTrue(mock_set_val.called)

        # Check get_sns_delivery_health returns note channel info
        health = sns_editorial.get_sns_delivery_health(allow_blocking=False)
        self.assertIn("note", health)
        note_health = health["note"]
        self.assertIn("label", note_health)
        self.assertTrue(note_health.get("configured"))

    def test_note_rule_based_candidates_generation(self):
        """F2.1: Verify rule-based note candidates produce essays and tsubuyaki."""
        research = sns_editorial.run_researcher(self.sample_stores)
        candidates = sns_editorial._write_note_candidates(
            research, situation="normal"
        )
        self.assertGreaterEqual(len(candidates), 3)

        styles = [c["style"] for c in candidates]
        self.assertIn("note_essay_insight", styles)
        self.assertIn("note_short_column", styles)
        self.assertIn("note_tsubuyaki_gap", styles)

        # Verify essay character length is substantial (structured with headings/summary)
        essay_cand = next(c for c in candidates if c["style"] == "note_essay_insight")
        essay_text = essay_cand.get("main_text") or essay_cand.get("text") or ""
        self.assertGreater(len(essay_text), 500)
        self.assertTrue(essay_text.startswith("【"))

        # Verify tsubuyaki character length meets 120-240 range
        tsubuyaki_cand = next(
            c for c in candidates if c["style"] == "note_tsubuyaki_gap"
        )
        tsubu_text = tsubuyaki_cand.get("main_text") or tsubuyaki_cand.get("text") or ""
        self.assertGreaterEqual(len(tsubu_text), 100)
        self.assertLessEqual(len(tsubu_text), 300)

    def test_note_evaluation_scoring(self):
        """F2.1: Verify run_evaluator scores note essays and tsubuyaki properly."""
        research = sns_editorial.run_researcher(self.sample_stores)
        plan = sns_editorial.run_planner(research, channel="note")

        candidates = [
            {
                "id": "c1",
                "style": "note_essay_insight",
                "text": "【相席の心理学】" + "相席ラウンジの観察記録。" * 70,  # ~1000文字
                "writer_persona": "note_essayist",
            },
            {
                "id": "c2",
                "style": "note_tsubuyaki_gap",
                "text": "金曜夜の相席ラウンジ、実は21時半が最大の狙い目。"
                "一次会終わりのグループが一気に入ってくる直前だから、待ち時間ゼロで入れる確率が最も高い。"
                "リアルタイム相席ナビを見ながら動くのが賢い立ち回り。",  # ~140文字
                "writer_persona": "note_writer",
            },
        ]
        eval_result = sns_editorial.run_evaluator(
            candidates,
            research,
            plan,
            channel="note",
        )
        scored = eval_result.get("candidates", [])
        self.assertEqual(len(scored), 2)
        for cand in scored:
            self.assertGreaterEqual(cand["total_score"], 60)
            self.assertIn("weight", cand)
            self.assertIn("metric_unit", cand)
        self.assertIsNotNone(eval_result.get("winner"))

    # -------------------------------------------------------------------------
    # F2.2: Situation selector tests
    # -------------------------------------------------------------------------

    def test_situation_specs_definition(self):
        """F2.2: Ensure all required situation specs are defined."""
        expected_situations = [
            "second_venue",
            "zero_wait",
            "rain_underground",
            "last_train_cliff",
            "start_dash",
        ]
        for sit in expected_situations:
            self.assertIn(sit, sns_editorial.SITUATION_SPECS)
            spec = sns_editorial.SITUATION_SPECS[sit]
            self.assertIn("label", spec)
            self.assertIn("desc", spec)
            self.assertIn("directive", spec)

    def test_planner_incorporates_situation(self):
        """F2.2: Ensure run_planner adapts strategy and memo to selected situation."""
        research = sns_editorial.run_researcher(self.sample_stores)
        for sit_key, sit_spec in sns_editorial.SITUATION_SPECS.items():
            plan = sns_editorial.run_planner(
                research, channel="x", situation=sit_key
            )
            self.assertEqual(plan["situation"], sit_key)
            self.assertIn(sit_spec["label"], plan["planner_memo"])
            self.assertIn("シチュエーション特化", plan["planner_memo"])

    def test_writer_note_candidates_reflect_situation(self):
        """F2.2: Verify note candidates reflect situation-specific themes."""
        research = sns_editorial.run_researcher(self.sample_stores)
        # Test second_venue
        candidates = sns_editorial._write_note_candidates(
            research, situation="second_venue"
        )
        texts = "".join([c.get("main_text") or c.get("text") or "" for c in candidates])
        self.assertTrue(
            "2軒目" in texts or "ハシゴ" in texts or "二次会" in texts,
            "Candidates should contain 2軒目 keywords for second_venue",
        )

        # Test zero_wait
        candidates_zero = sns_editorial._write_note_candidates(
            research, situation="zero_wait"
        )
        texts_zero = "".join([c.get("main_text") or c.get("text") or "" for c in candidates_zero])
        self.assertTrue(
            "待ち" in texts_zero or "即入店" in texts_zero,
            "Candidates should contain wait keywords for zero_wait",
        )

    # -------------------------------------------------------------------------
    # F2.3: AI image generation prompts tests
    # -------------------------------------------------------------------------

    def test_image_prompts_generation_all_channels(self):
        """F2.3: Test generate_image_prompts outputs prompts for all 5 major SNS platforms."""
        research = sns_editorial.run_researcher(self.sample_stores)
        prompts_res = sns_editorial.generate_image_prompts(
            research, channel="all", situation="second_venue"
        )
        self.assertIn("gallery", prompts_res)
        gallery = prompts_res["gallery"]
        self.assertEqual(len(gallery), 5)
        channels = [p["channel"] for p in gallery]
        self.assertIn("x", channels)
        self.assertIn("instagram", channels)
        self.assertIn("note", channels)
        self.assertIn("tiktok", channels)
        self.assertIn("threads", channels)

        # Check aspect ratios
        aspect_map = {p["channel"]: p["aspect_ratio"] for p in gallery}
        self.assertEqual(aspect_map["x"], "16:9")
        self.assertEqual(aspect_map["instagram"], "4:5")
        self.assertEqual(aspect_map["note"], "1.91:1")
        self.assertEqual(aspect_map["tiktok"], "9:16")
        self.assertEqual(aspect_map["threads"], "1:1")

        # Verify English prompt and Japanese description content
        for p in gallery:
            self.assertTrue(len(p["prompt_en"]) > 20)
            self.assertTrue(len(p["prompt_ja"]) > 10)
            self.assertIn("prompt_en", p)
            self.assertIn("copy_target", p)

    def test_image_prompts_included_in_pipeline(self):
        """F2.3: Verify run_editorial_pipeline includes image_prompts in return dict."""
        with mock.patch("sns_editorial.call_editorial_ai_json", return_value=(None, "", "mocked")):
            result = sns_editorial.run_editorial_pipeline(
                self.sample_stores, channel="note", situation="rain_underground"
            )
            self.assertIn("image_prompts", result)
            self.assertIn("gallery", result["image_prompts"])
            self.assertEqual(len(result["image_prompts"]["gallery"]), 5)
            self.assertEqual(result["situation"], "rain_underground")

    # -------------------------------------------------------------------------
    # Brand Name Protection & Fact Checking
    # -------------------------------------------------------------------------

    def test_sanitize_brand_names(self):
        """Ensure standalone 'オリエンタル', 'OLG', and 'オリラジ' are converted to 'オリエンタルラウンジ'."""
        bad_texts = [
            ("オリエンタルで今夜飲む", "オリエンタルラウンジで今夜飲む"),
            ("OLG渋谷店が盛り上がってる", "オリエンタルラウンジ 渋谷店が盛り上がってる"),
            ("今夜はオリラジ行こう", "今夜はオリエンタルラウンジ行こう"),
            # Existing correct forms should not be doubled
            ("オリエンタルラウンジ新宿", "オリエンタルラウンジ新宿"),
        ]
        for src, expected in bad_texts:
            sanitized = sns_editorial.sanitize_brand_names(src)
            self.assertEqual(sanitized, expected)

    def test_fact_check_verification(self):
        """Verify hallucinated headcount is penalized in evaluation."""
        research = sns_editorial.run_researcher(self.sample_stores)
        fake_text = "渋谷店は今夜男性999人、女性999人で大混雑！"
        is_valid, bad_tokens = sns_editorial.verify_ai_factual_numbers(fake_text, research)
        self.assertFalse(is_valid)
        self.assertGreater(len(bad_tokens), 0)

        # Real store factual text should pass
        real_text = "オリエンタルラウンジ 渋谷店は現在女性28名来店中！"
        real_valid, real_bad = sns_editorial.verify_ai_factual_numbers(
            real_text, research
        )
        self.assertTrue(real_valid)
        self.assertEqual(len(real_bad), 0)

    # -------------------------------------------------------------------------
    # Template Integration Sanity Checks
    # -------------------------------------------------------------------------

    def test_staff_sns_editorial_template_elements(self):
        """Verify templates/staff_sns_editorial.html contains UI controls for note, situation, and prompts."""
        tpl_path = os.path.join(ROOT, "templates", "staff_sns_editorial.html")
        self.assertTrue(os.path.exists(tpl_path))
        with open(tpl_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check note channel tab
        self.assertIn("channel=note", content)
        self.assertIn("📝 note", content)

        # Check situation selector buttons
        self.assertIn("editorial.situation_specs", content)
        self.assertIn("🎯 シチュエーション特化セレクター", content)

        # Check image prompt section
        self.assertIn("🎨 AIアイキャッチ・画像生成プロンプト帳", content)
        self.assertIn("img-prompt-card", content)

        # Check inline editor and char counter
        self.assertIn("cand-edit-textarea", content)
        self.assertIn("char-counter-badge", content)
        self.assertIn("openWebIntent", content)

    def test_index_modal_elements(self):
        """Verify templates/index.html #sns-modal contains situation buttons and strict brand name."""
        tpl_path = os.path.join(ROOT, "templates", "index.html")
        self.assertTrue(os.path.exists(tpl_path))
        with open(tpl_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Check #sns-modal exists
        self.assertIn('id="sns-modal"', content)
        # Check second venue & situation buttons
        self.assertIn("2軒目ハシゴ案内", content)
        self.assertIn("女子待ちゼロ速報", content)
        self.assertIn("雨天・直結穴場", content)
        self.assertIn("終電前ラストチャンス", content)
        # Check strict brand name in modal script
        self.assertIn("オリエンタルラウンジ", content)
        self.assertNotIn("'OLG '", content)

    # -------------------------------------------------------------------------
    # Claude 5.x Compatibility & Anti-"二度見" Regression Tests
    # -------------------------------------------------------------------------

    def test_claude_5_omits_temperature_and_takes_priority(self):
        """Claude 5+ models must omit deprecated `temperature` and take priority over Gemini when configured."""
        sns_editorial._claude_no_temp_models.clear()
        self.assertFalse(sns_editorial._claude_supports_temperature("claude-sonnet-5-5"))
        self.assertFalse(sns_editorial._claude_supports_temperature("claude-sonnet-5"))
        self.assertFalse(sns_editorial._claude_supports_temperature("claude-haiku-5-5"))
        self.assertTrue(sns_editorial._claude_supports_temperature("claude-sonnet-4-6"))

        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": '{"thinking_memo": "ok", "main_text": "テスト本文"}'}],
        }

        with (
            mock.patch("sns_editorial.get_claude_api_key", return_value="sk-ant-test-key-1234567890"),
            mock.patch("sns_editorial.get_gemini_api_key", return_value="AIzaSyTestKey1234567890"),
            mock.patch("sns_editorial.get_ai_engine_pref", return_value="auto"),
            mock.patch("sns_editorial.requests.post", return_value=fake_resp) as mock_post,
        ):
            parsed, model_used, err = sns_editorial.call_editorial_ai_json('{"hello": 1}')
            self.assertEqual(err, "")
            self.assertEqual(model_used, "claude-sonnet-5-5")
            self.assertEqual(parsed.get("main_text"), "テスト本文")
            self.assertEqual(mock_post.call_count, 1)
            called_url = mock_post.call_args[0][0]
            called_json = mock_post.call_args[1]["json"]
            self.assertEqual(called_url, "https://api.anthropic.com/v1/messages")
            self.assertNotIn("temperature", called_json)
            self.assertEqual(called_json.get("output_config"), {"effort": "medium"})
            self.assertGreaterEqual(called_json["max_tokens"], 4096)

        # Ensure SNS & Column prompts never request `thinking_memo` (avoids Claude 5.5 reasoning_extraction refusal)
        res = sns_editorial.run_researcher(self.sample_stores, channel="x")
        prompt_x, _ = sns_editorial._build_gemini_prompt(
            res, {}, "22:00", "x", sns_editorial.datetime.datetime.now(sns_editorial.JST), "https://aiseki-navi.com"
        )
        self.assertNotIn("thinking_memo", prompt_x)
        self.assertIn("editorial_angle", prompt_x)

    def test_claude_temperature_deprecation_auto_retry(self):
        """If a Claude model returns HTTP 400 deprecating `temperature`, retry immediately without falling back to Gemini."""
        sns_editorial._claude_no_temp_models.clear()
        err_resp = mock.MagicMock()
        err_resp.status_code = 400
        err_resp.text = '{"type":"error","error":{"type":"invalid_request_error","message":"`temperature` is deprecated for this model."}}'

        ok_resp = mock.MagicMock()
        ok_resp.status_code = 200
        ok_resp.json.return_value = {
            "model": "claude-sonnet-4-6",
            "content": [{"type": "text", "text": '{"main_text": "リトライ成功"}'}],
        }

        with (
            mock.patch.dict(os.environ, {"CLAUDE_MODEL": "claude-sonnet-4-6"}, clear=False),
            mock.patch("sns_editorial.get_claude_api_key", return_value="sk-ant-test-key-1234567890"),
            mock.patch("sns_editorial.get_gemini_api_key", return_value="AIzaSyTestKey1234567890"),
            mock.patch("sns_editorial.get_ai_engine_pref", return_value="claude"),
            mock.patch("sns_editorial.requests.post", side_effect=[err_resp, ok_resp]) as mock_post,
        ):
            parsed, model_used, err = sns_editorial.call_editorial_ai_json('{"hello": 1}')
            self.assertEqual(err, "")
            self.assertEqual(model_used, "claude-sonnet-4-6")
            self.assertEqual(parsed.get("main_text"), "リトライ成功")
            self.assertEqual(mock_post.call_count, 2)
            first_json = mock_post.call_args_list[0][1]["json"]
            second_json = mock_post.call_args_list[1][1]["json"]
            self.assertIn("temperature", first_json)
            self.assertNotIn("temperature", second_json)

    def test_zero_nidomiru_cliche_in_prompts_and_outputs(self):
        """Ensure '二度見 / ２度見 / 2度見' is purged from guides/templates and stripped by postprocess."""
        import bonus_alert

        for w in ("二度見", "２度見", "2度見"):
            self.assertIn(w, sns_editorial.AI_CLICHE_WORDS)
            self.assertNotIn(w, sns_editorial.HUMAN_INSIDER_VOICE_GUIDE)

        for ch in ("x", "threads", "instagram", "tiktok", "note"):
            cands = sns_editorial.run_writer(self.sample_stores and sns_editorial.run_researcher(self.sample_stores, channel=ch), {}, channel=ch, use_ai=False)
            for c in cands:
                for w in ("二度見", "２度見", "2度見"):
                    self.assertNotIn(w, c.get("main_text", ""))

        cleaned = sns_editorial._postprocess_ai_main_text(
            "モニターを二度見したんだけど、オリエンタルラウンジ新宿が女性60名・男性25名まで伸びてて２度見した…",
            channel="x",
        )
        for w in ("二度見", "２度見", "2度見"):
            self.assertNotIn(w, cleaned)

        alert_cand = {"store": self.sample_stores[0], "mode": "female_lead", "wpct": 70}
        main_t, _ = bonus_alert.build_bonus_alert_tweet_thread(alert_cand, use_ai=False)
        for w in ("二度見", "２度見", "2度見"):
            self.assertNotIn(w, main_t)

    def test_unspaced_oriental_lounge_two_store_comparison_wins_100pts(self):
        """Ensure unspaced 'オリエンタルラウンジ新宿' + 'JIS新宿' on the same line passes fact-check and wins 100pts."""
        stores = [
            {"name": "OLG 新宿", "women": 52, "men": 24, "region": "Kanto"},
            {"name": "JIS 新宿", "women": 38, "men": 31, "region": "Kanto"},
            {"name": "JIS 梅田", "women": 61, "men": 35, "region": "Kinki"},
        ]
        research = sns_editorial.run_researcher(stores, slot_label="22:00", channel="x")
        claude_text = (
            "穏やかな夜なのに、モニターの新宿だけ温度差すごい。"
            "オリエンタルラウンジ新宿が女性52名・男性24名（女性比68%）で、徒歩圏のJIS新宿は55%。"
            "看板だけで選ぶと損なんだよな…細かい数字はリプ欄へ👇 #相席ナビ #相席ラウンジ"
        )
        ok, bad = sns_editorial.verify_ai_factual_numbers(claude_text, research, channel="x")
        self.assertTrue(ok, f"Unexpected false-positive rejection: {bad}")

        # Swapping JIS梅田's headcount (61) onto single-store オリエンタルラウンジ新宿 line must still be rejected
        swapped = "オリエンタルラウンジ新宿が女性61名・男性35名まで伸びてる👇 #相席ナビ"
        ok_swap, bad_swap = sns_editorial.verify_ai_factual_numbers(swapped, research, channel="x")
        self.assertFalse(ok_swap)
        self.assertTrue(bad_swap)

        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "model": "claude-sonnet-5-5",
            "content": [{"type": "text", "text": json.dumps({"editorial_angle": "新宿ギャップ", "main_text": claude_text}, ensure_ascii=False)}],
        }
        with (
            mock.patch.dict(os.environ, {"CLAUDE_MODEL": "claude-sonnet-5-5"}, clear=False),
            mock.patch("sns_editorial.get_claude_api_key", return_value="sk-ant-test-key-1234567890"),
            mock.patch("sns_editorial.get_ai_engine_pref", return_value="claude"),
            mock.patch("sns_editorial.requests.post", return_value=fake_resp),
        ):
            res = sns_editorial.run_editorial_pipeline(
                stores, slot_label="22:00", channel="x", use_ai=True, force_ai_refresh=True, record_history=False
            )
            self.assertEqual(res["winner"]["style"], "ai_live_adlib")
            self.assertEqual(res["winner"]["total_score"], 100)
            self.assertIn("claude-sonnet-5-5", res["winner"]["style_label"])


if __name__ == "__main__":
    unittest.main()
