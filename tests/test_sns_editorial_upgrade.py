"""Unit and integration tests for Milestone 2: SNS AI & Workflow Upgrade.

Covers:
- F2.1: note channel integration, prompt building, length/weight evaluation, delivery status & health
- F2.2: Situation-specific quick selectors (second_venue, zero_wait, rain_underground, last_train_cliff, start_dash)
- F2.3: AI image generation prompts for 5 major SNS platforms (X, IG, note, TikTok, Threads)
- Brand name sanitization & protection rules (strict Oriental Lounge compliance)
- Factual verification guarding against hallucination
- Template integration sanity checks (staff_sns_editorial.html, index.html #sns-modal)
"""

import datetime
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
        """Claude 5+ Opus/Sonnet models must omit deprecated `temperature` and route Opus primary / Sonnet fast."""
        sns_editorial._claude_no_temp_models.clear()
        sns_editorial._last_gemini_meta["working_route"] = None
        self.assertFalse(sns_editorial._claude_supports_temperature("claude-opus-5-5"))
        self.assertFalse(sns_editorial._claude_supports_temperature("claude-sonnet-5-5"))
        self.assertFalse(sns_editorial._claude_supports_temperature("claude-sonnet-5"))
        self.assertFalse(sns_editorial._claude_supports_temperature("claude-haiku-5-5"))
        self.assertTrue(sns_editorial._claude_supports_temperature("claude-sonnet-4-6"))

        fake_resp = mock.MagicMock()
        fake_resp.status_code = 200
        fake_resp.json.return_value = {
            "model": "claude-opus-5-5",
            "content": [{"type": "text", "text": '{"thinking_memo": "ok", "main_text": "テスト本文"}'}],
        }

        with (
            mock.patch.dict(os.environ, {"CLAUDE_MODEL": ""}, clear=False),
            mock.patch("sns_editorial.get_claude_api_key", return_value="sk-ant-test-key-1234567890"),
            mock.patch("sns_editorial.get_gemini_api_key", return_value="AIzaSyTestKey1234567890"),
            mock.patch("sns_editorial.get_ai_engine_pref", return_value="auto"),
            mock.patch("sns_editorial.requests.post", return_value=fake_resp) as mock_post,
        ):
            parsed, model_used, err = sns_editorial.call_editorial_ai_json('{"hello": 1}')
            self.assertEqual(err, "")
            self.assertEqual(model_used, "claude-opus-5-5")
            self.assertEqual(parsed.get("main_text"), "テスト本文")
            self.assertEqual(mock_post.call_count, 1)
            called_url = mock_post.call_args[0][0]
            called_json = mock_post.call_args[1]["json"]
            self.assertEqual(called_url, "https://api.anthropic.com/v1/messages")
            self.assertNotIn("temperature", called_json)
            self.assertEqual(called_json.get("output_config"), {"effort": "medium"})
            self.assertGreaterEqual(called_json["max_tokens"], 4096)

            # Fast/polisher pass (`prefer_fast=True`) routes to Sonnet (`claude-sonnet-5-5`) with low effort
            _, fast_model, err2 = sns_editorial.call_editorial_ai_json('{"hello": 2}', prefer_fast=True)
            self.assertEqual(err2, "")
            self.assertEqual(fast_model, "claude-sonnet-5-5")

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

    def test_x_archetype_rotation_silent_tree_and_short_snap_100pts(self):
        """Ensure 8 X skeletons rotate (including silent-tree tagless tweets) and crisp short tweets score 100pts."""
        seen_keys = set()
        silent_count = 0
        base_dt = datetime.datetime(2026, 11, 1, 20, 0, tzinfo=sns_editorial.JST)
        for day_off in range(8):
            for slot, hh in (("20:00", 20), ("22:00", 22), ("00:00", 0)):
                dt = (base_dt + datetime.timedelta(days=day_off)).replace(hour=hh)
                arch = sns_editorial.select_x_post_archetype(slot, dt)
                seen_keys.add(arch["key"])
                if arch["silent_tree"]:
                    silent_count += 1
        self.assertGreaterEqual(len(seen_keys), 7)
        self.assertGreater(silent_count, 0)

        # Crisp 52-char silent-tree snap (zero 👇, zero #) must score 100/100 in run_evaluator
        stores = [
            {"name": "ag 恵比寿", "women": 34, "men": 18, "region": "Kanto"},
            {"name": "OLG 新宿", "women": 40, "men": 32, "region": "Kanto"},
        ]
        research = sns_editorial.run_researcher(stores, slot_label="22:00", channel="x", now_jst=base_dt)
        short_human_cand = {
            "style": "ai_live_adlib",
            "style_label": "✨ AI編集長（claude-sonnet-5-5）",
            "main_text": "22時すぎのag恵比寿、女性34名・男性18名（女性比65%）。今夜はだいぶ落ち着いた空気ですね。",
            "reply_text": "推移メモ\nhttps://aiseki-navi.com/store/ag-ebisu",
            "is_ai_generated": True,
        }
        ev = sns_editorial.run_evaluator([short_human_cand], research, {}, channel="x", now_jst=base_dt)
        self.assertEqual(ev["winner"]["total_score"], 100)

    def test_store_spotlight_4_4_2_tier_and_recent_cooldown(self):
        """Ensure stores classify into tier1/tier2/tier3 and recent featured stores get cooled down."""
        self.assertEqual(sns_editorial.classify_store_spotlight_tier("OLG 新宿"), "tier1")
        self.assertEqual(sns_editorial.classify_store_spotlight_tier("ag 恵比寿"), "tier2")
        self.assertEqual(sns_editorial.classify_store_spotlight_tier("JIS 仙台"), "tier3")

        stores = [
            {"name": "OLG 新宿", "women": 46, "men": 26, "region": "Kanto"},
            {"name": "ag 恵比寿", "women": 42, "men": 22, "region": "Kanto"},
            {"name": "JIS 仙台", "women": 32, "men": 16, "region": "Tohoku"},
        ]
        recent_fake = ["金曜夜のオリエンタルラウンジ新宿、女性46名・男性26名で盛り上がってる。"]
        # Pick a date/slot whose 4:4:2 rotation targets 'submajor' or 'regional' and cools down 新宿
        dt_sub = datetime.datetime(2026, 11, 2, 22, 0, tzinfo=sns_editorial.JST)
        with mock.patch("sns_editorial.get_recent_cross_channel_texts", return_value=recent_fake):
            res = sns_editorial.run_researcher(stores, slot_label="22:00", channel="x", now_jst=dt_sub)
            self.assertIn("オリエンタルラウンジ 新宿", res["recently_featured_names"])
            self.assertNotEqual(res["focus_store"]["name"], "OLG 新宿")

    def test_backstage_cliche_ban_and_cross_module_clean_tone(self):
        """Ensure backstage words ('モニター見てたら', 'バックヤード') are stripped and absent from fallbacks."""
        import bonus_alert
        import ladies_guide
        import note_poster

        raw = "バックヤードでモニター見てたら、オリエンタルラウンジ新宿が女性50名・男性22名だった。"
        cleaned = sns_editorial._postprocess_ai_main_text(raw, channel="x")
        self.assertNotIn("バックヤード", cleaned)
        self.assertNotIn("モニター見て", cleaned)
        self.assertIn("オリエンタルラウンジ新宿", cleaned)

        dt = datetime.datetime(2026, 11, 1, 21, 15, tzinfo=sns_editorial.JST)
        for day_i in range(3):
            d_cur = dt + datetime.timedelta(days=day_i)
            th_post = ladies_guide.build_threads_ladies_post(self.sample_stores, now_jst=d_cur, use_ai=False)
            self.assertNotIn("モニター", th_post)
            self.assertNotIn("バックヤード", th_post)
            b_main, _ = bonus_alert.build_bonus_alert_tweet_thread(
                {"store": self.sample_stores[0], "mode": "female_lead", "wpct": 70}, now_jst=d_cur, use_ai=False
            )
            self.assertNotIn("モニター", b_main)
        note_tw = note_poster.build_note_tweet_text("終電後の街の空気について", "https://note.com/aiseki_navi/n/n123", use_ai=False)
        self.assertNotIn("モニター見て", note_tw)

    def test_tips_zero_base_seed_rotation_and_dedup(self):
        """Ensure 24 human observation seeds never repeat any seed recorded in recent 18 Tips."""
        import tips_poster

        self.assertGreaterEqual(len(tips_poster.HUMAN_OBSERVATION_SEEDS), 24)
        used_18 = [{"seed_key": s["key"], "text": f"past {i}"} for i, s in enumerate(tips_poster.HUMAN_OBSERVATION_SEEDS[:18])]
        picked = tips_poster.select_fresh_observation_seed(
            now_jst=datetime.datetime(2026, 11, 3, 18, 0, tzinfo=sns_editorial.JST),
            recent_history=used_18,
        )
        self.assertNotIn(picked["key"], {u["seed_key"] for u in used_18})

    def test_column_editorial_three_literary_styles_without_academic_jargon(self):
        """Ensure column_editorial rotates 3 literary styles and avoids mandatory textbook buzzwords."""
        import column_editorial

        s1 = column_editorial.select_column_writing_style("area_hopping")
        s2 = column_editorial.select_column_writing_style("female_real_voice")
        s3 = column_editorial.select_column_writing_style("second_venue_move")
        self.assertEqual({s1["key"], s2["key"], s3["key"]}, {"street_reportage", "table_psychology_dialogue", "live_data_gap_anatomy"})

        res = column_editorial.run_column_editorial_pipeline(self.sample_stores, theme_key="second_venue_move", use_ai=False)
        prompt = column_editorial._build_column_gemini_prompt(res["research"], res["plan"], "https://aiseki-navi.com")
        self.assertIn("実測ギャップ・時間感覚検証スタイル", prompt)
        note_exp = column_editorial.export_column_for_note("osaka-kita-vs-minami", mode="essay")
        self.assertTrue(note_exp["ok"])
        self.assertNotIn("Two-Sided Market", note_exp["body_markdown"])

    def test_two_pass_humanizer_polish_and_digit_lock(self):
        """Ensure 2nd-pass humanizer softens stiff AI phrasing while rejecting any altered numbers."""
        stores = [
            {"name": "OLG 新宿", "women": 52, "men": 24, "region": "Kanto"},
            {"name": "JIS 新宿", "women": 38, "men": 31, "region": "Kanto"},
        ]
        research = sns_editorial.run_researcher(stores, slot_label="22:00", channel="x")
        draft_stiff = "新宿エリアにおいてはオリエンタルラウンジ 新宿が女性52名・男性24名（女性比68%）という傾向にあります。今から動くのが正解。"
        polished_natural = "いまの新宿、オリエンタルラウンジ 新宿は女性52名・男性24名（68%）になってます。1軒目終わりに置いときます。"

        with mock.patch(
            "sns_editorial.call_editorial_ai_json",
            return_value=({"polished_text": polished_natural}, "claude-sonnet-5-5", ""),
        ):
            out_ok, p_mod = sns_editorial.polish_draft_to_human_voice(
                draft_text=draft_stiff,
                channel="x",
                research=research,
                slot_label="22:00",
                force_polish=True,
            )
            self.assertEqual(p_mod, "claude-sonnet-5-5")
            self.assertEqual(out_ok, polished_natural)

        tampered_digits = "いまの新宿、オリエンタルラウンジ 新宿は女性60名・男性24名（68%）。この時間でも崩れてないですね。"
        with mock.patch(
            "sns_editorial.call_editorial_ai_json",
            return_value=({"polished_text": tampered_digits}, "claude-sonnet-5-5", ""),
        ):
            out_bad, p_mod_bad = sns_editorial.polish_draft_to_human_voice(
                draft_text=draft_stiff,
                channel="x",
                research=research,
                slot_label="22:00",
                force_polish=True,
            )
            self.assertEqual(p_mod_bad, "")
            self.assertEqual(out_bad, draft_stiff)

    def test_tonight_street_pulse_web_search_and_prompt_injection(self):
        """Ensure Claude web_search_20250305 fetches tonight street pulse once and caches across calls."""
        with sns_editorial._street_pulse_cache["lock"]:
            sns_editorial._street_pulse_cache["entries"].clear()

        fake_search_resp = mock.MagicMock()
        fake_search_resp.status_code = 200
        fake_search_resp.json.return_value = {
            "model": "claude-haiku-5-5",
            "content": [
                {
                    "type": "text",
                    "text": '{"national_vibe": "月末の週末で繁華街は早めから賑わい", "kanto": "東京ドームでライブ終演後の人の流れあり", "kansai": "京セラドーム帰りで難波・心斎橋が早め", "other": ""}',
                }
            ],
        }
        dt = datetime.datetime(2026, 10, 31, 20, 0, tzinfo=sns_editorial.JST)
        with (
            mock.patch("sns_editorial.get_claude_api_key", return_value="sk-ant-test-key-1234567890"),
            mock.patch("sns_editorial._discover_claude_models", return_value=["claude-opus-5-5", "claude-sonnet-5-5", "claude-haiku-5-5"]),
            mock.patch("sns_editorial.requests.post", return_value=fake_search_resp) as mock_post,
        ):
            p1 = sns_editorial.fetch_tonight_street_pulse(now_jst=dt, force_network=True, allow_blocking=True)
            self.assertTrue(p1["available"])
            self.assertIn("東京ドーム", p1["kanto"])
            self.assertEqual(mock_post.call_count, 1)
            sent_tools = mock_post.call_args[1]["json"]["tools"]
            self.assertEqual(sent_tools[0]["type"], "web_search_20250305")

            # Subsequent call within 4h hits cache with 0 network calls
            p2 = sns_editorial.fetch_tonight_street_pulse(now_jst=dt, force_network=False, allow_blocking=False)
            self.assertTrue(p2["available"])
            self.assertEqual(mock_post.call_count, 1)

        res = sns_editorial.run_researcher(self.sample_stores, slot_label="20:00", channel="x", now_jst=dt)
        res["street_pulse"] = p1
        prompt_str, _ = sns_editorial._build_gemini_prompt(res, {}, "20:00", "x", dt, "https://aiseki-navi.com")
        self.assertIn("tonight_street_pulse", prompt_str)
        self.assertIn("東京ドーム", prompt_str)

    def test_human_schedule_jitter_spread_and_fail_open(self):
        """Ensure daily human timing offsets vary across days/channels and never block late retries."""
        offsets_x = {
            sns_editorial.compute_human_schedule_offset_min(
                "x", "20:00", now_jst=datetime.datetime(2026, 11, d, 20, 0, tzinfo=sns_editorial.JST)
            )
            for d in range(1, 15)
        }
        self.assertGreaterEqual(len(offsets_x), 6)
        for off in offsets_x:
            self.assertTrue(2 <= off <= 21)

        # Early minute (20:01) holds back; past +25m always fail-opens
        dt_early = datetime.datetime(2026, 11, 1, 20, 1, tzinfo=sns_editorial.JST)
        dt_late = datetime.datetime(2026, 11, 1, 20, 26, tzinfo=sns_editorial.JST)
        self.assertFalse(sns_editorial.should_fire_scheduled_slot_now("x", "20:00", now_jst=dt_early))
        self.assertTrue(sns_editorial.should_fire_scheduled_slot_now("x", "20:00", now_jst=dt_late))

    def test_reel_4x_schedule_rotation_captions_and_cliche_purge(self):
        """Verify 4x/week IG Reel cron schedule, default-ON toggle, non-repeating 4-template rotation, and cliché-free captions."""
        import tempfile
        import instagram_poster
        import job_state
        import reel_generator
        import worker as worker_module

        sched = worker_module.build_worker_scheduler()
        jobs_by_id = {j.id: j for j in sched.get_jobs()}
        self.assertIn("ig_reel_2115", jobs_by_id)
        self.assertIn("tiktok_inbox_2035", jobs_by_id)
        reel_trig_str = str(jobs_by_id["ig_reel_2115"].trigger)
        for day_tok in ("wed", "thu", "fri", "sat"):
            self.assertIn(day_tok, reel_trig_str)

        bare_oriental_re = re.compile(r"オリエンタル(?!ラウンジ)")
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"JOB_STATE_DIR": tmpdir}):
                job_state.reset_cache_for_tests()
                # Default is ON when unset, respects explicit OFF ("0") and ON ("1")
                self.assertTrue(instagram_poster.is_reel_auto_schedule_enabled())
                instagram_poster.set_reel_auto_schedule_enabled(False)
                self.assertFalse(instagram_poster.is_reel_auto_schedule_enabled())
                instagram_poster.set_reel_auto_schedule_enabled(True)
                self.assertTrue(instagram_poster.is_reel_auto_schedule_enabled())

                rich_stores = [
                    {"name": "OLG 新宿", "women": 34, "men": 12, "region": "Kanto", "area": "新宿"},
                    {"name": "JIS SHINJUKU", "women": 8, "men": 24, "region": "Kanto", "area": "新宿"},
                    {"name": "OLG 梅田", "women": 28, "men": 14, "region": "Kinki", "area": "梅田"},
                    {"name": "JIS UMEDA", "women": 10, "men": 22, "region": "Kinki", "area": "梅田"},
                    {"name": "OLG 渋谷本店", "women": 26, "men": 15, "region": "Kanto", "area": "渋谷"},
                ]
                chosen_seq = []
                base_dt = datetime.datetime(2026, 11, 4, 21, 25, tzinfo=sns_editorial.JST)  # Wednesday
                for step in range(8):
                    cur_dt = base_dt + datetime.timedelta(days=step)
                    picked = reel_generator.select_scheduled_reel_type(rich_stores, now_jst=cur_dt, platform="instagram")
                    if chosen_seq:
                        self.assertNotEqual(picked, chosen_seq[-1], "Back-to-back IG Reel template repeat detected")
                    reel_generator.record_recent_reel_type(picked, platform="instagram")
                    chosen_seq.append(picked)
                self.assertGreaterEqual(len(set(chosen_seq)), 3)

                # Edge case: empty stores list never crashes rotation or caption builder
                empty_pick = reel_generator.select_scheduled_reel_type([], now_jst=base_dt, platform="instagram")
                self.assertIn(empty_pick, reel_generator.REEL_TEMPLATES)

                for rt in reel_generator.REEL_TEMPLATES:
                    for stores_arg in (rich_stores, []):
                        cap = reel_generator.build_reel_caption(stores_arg, reel_type=rt, now_jst=base_dt)
                        self.assertNotIn("モニター見て", cap)
                        self.assertNotIn("バックヤード", cap)
                        self.assertNotIn("全51店舗", cap)
                        self.assertNotIn("全国53店舗", cap)
                        self.assertIsNone(bare_oriental_re.search(cap))
                        self.assertIn("#相席ナビ", cap)
                        tags = re.findall(r"#[^\s#]+", cap)
                        self.assertTrue(3 <= len(tags) <= 6, f"Unexpected hashtag count ({len(tags)}) for {rt}: {tags}")

    def test_ig_reel_grid_protection_cooldown_and_slot_dedup(self):
        """Verify scheduled IG Reel slot deduplication, 95m IG cooldown, error recovery, and automatic share_to_feed=False grid protection."""
        import tempfile
        import instagram_poster
        import job_state

        stores = [
            {"name": "OLG 新宿", "women": 32, "men": 14, "region": "Kanto"},
            {"name": "JIS UMEDA", "women": 24, "men": 16, "region": "Kinki"},
        ]
        wed_dt = datetime.datetime(2026, 11, 4, 21, 30, tzinfo=sns_editorial.JST)
        fri_feed_dt = datetime.datetime(2026, 11, 6, 18, 52, tzinfo=sns_editorial.JST)
        fri_early_reel_dt = datetime.datetime(2026, 11, 6, 19, 55, tzinfo=sns_editorial.JST)  # +63m (<95m)
        fri_ok_reel_dt = datetime.datetime(2026, 11, 6, 21, 35, tzinfo=sns_editorial.JST)     # +163m (>=95m)

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"JOB_STATE_DIR": tmpdir}):
                job_state.reset_cache_for_tests()
                reel_calls = []

                def _fake_post_reel(video_url, caption, share_to_feed=True, thumb_offset_ms=3500):
                    reel_calls.append({"video_url": video_url, "caption": caption, "share_to_feed": share_to_feed})
                    return {"status": "success", "ok": True, "id": f"reel_{len(reel_calls)}"}

                with (
                    mock.patch("instagram_poster.refresh_ig_token_if_needed"),
                    mock.patch("reel_generator.render_reel_mp4_bytes", return_value=b"FAKE_MP4"),
                    mock.patch("instagram_poster.post_reel_to_instagram", side_effect=_fake_post_reel),
                ):
                    # 1) Wednesday (no Feed post today) -> share_to_feed=True, marks slot done
                    r_wed = instagram_poster.publish_live_reel_to_instagram(
                        stores, reel_type="auto", slot_label="21:15", force=False, now_jst=wed_dt
                    )
                    self.assertEqual(r_wed["status"], "success")
                    self.assertTrue(reel_calls[-1]["share_to_feed"])

                    # Retry tick on same Wednesday slot -> fast-skips
                    r_wed_dup = instagram_poster.publish_live_reel_to_instagram(
                        stores, reel_type="auto", slot_label="21:15", force=False, now_jst=wed_dt + datetime.timedelta(minutes=6)
                    )
                    self.assertEqual(r_wed_dup["status"], "skipped")
                    self.assertEqual(len(reel_calls), 1)

                    # 2) Friday: 18:52 Carousel posted -> 19:55 Reel blocked by 95m cooldown -> 21:35 Reel fires with share_to_feed=False!
                    instagram_poster.record_ig_auto_post_timestamp(fri_feed_dt, is_feed=True)
                    r_fri_cd = instagram_poster.publish_live_reel_to_instagram(
                        stores, reel_type="auto", slot_label="21:15", force=False, now_jst=fri_early_reel_dt
                    )
                    self.assertEqual(r_fri_cd["status"], "skipped")
                    self.assertIn("cooldown", r_fri_cd["reason"].lower())

                    r_fri_ok = instagram_poster.publish_live_reel_to_instagram(
                        stores, reel_type="auto", slot_label="21:15", force=False, now_jst=fri_ok_reel_dt
                    )
                    self.assertEqual(r_fri_ok["status"], "success")
                    self.assertFalse(r_fri_ok["share_to_feed"])
                    self.assertFalse(reel_calls[-1]["share_to_feed"])

                # 3) Error path: failed Meta API call must NOT mark slot done so retry tick can recover
                sat_dt = datetime.datetime(2026, 11, 7, 21, 30, tzinfo=sns_editorial.JST)
                with (
                    mock.patch("instagram_poster.refresh_ig_token_if_needed"),
                    mock.patch("reel_generator.render_reel_mp4_bytes", return_value=b"FAKE_MP4"),
                    mock.patch("instagram_poster.post_reel_to_instagram", return_value={"status": "error", "detail": "HTTP 500"}),
                ):
                    r_err = instagram_poster.publish_live_reel_to_instagram(
                        stores, reel_type="auto", slot_label="21:15", force=False, now_jst=sat_dt
                    )
                    self.assertEqual(r_err["status"], "error")
                    self.assertFalse(job_state.is_done("ig_reel_slot:2026-11-07_21:15"))

    def test_all_sns_antibot_pacing_rest_slots_and_live_surge_override(self):
        """Verify natural weekday rest slots on X/Threads and live surge override when total women >= 140/120."""
        quiet_stores = [{"name": "OLG 新宿", "women": 28, "men": 18, "region": "Kanto"}]
        surge_stores = [
            {"name": "OLG 新宿", "women": 85, "men": 40, "region": "Kanto"},
            {"name": "OLG 渋谷本店", "women": 70, "men": 35, "region": "Kanto"},
        ]  # total_women = 155

        mon_20 = datetime.datetime(2026, 11, 2, 20, 10, tzinfo=sns_editorial.JST)  # Monday
        fri_20 = datetime.datetime(2026, 11, 6, 20, 10, tzinfo=sns_editorial.JST)  # Friday
        sun_18 = datetime.datetime(2026, 11, 8, 18, 10, tzinfo=sns_editorial.JST)  # Sunday

        # X 20:00 skips quiet Monday, overrides on Monday surge, never skips Friday 20:00 or any 22:00 peak
        self.assertTrue(sns_editorial.should_skip_slot_for_human_pacing("x", "20:00", now_jst=mon_20, stores=quiet_stores)[0])
        self.assertFalse(sns_editorial.should_skip_slot_for_human_pacing("x", "20:00", now_jst=mon_20, stores=surge_stores)[0])
        self.assertFalse(sns_editorial.should_skip_slot_for_human_pacing("x", "20:00", now_jst=fri_20, stores=quiet_stores)[0])
        self.assertFalse(sns_editorial.should_skip_slot_for_human_pacing("x", "22:00", now_jst=mon_20, stores=quiet_stores)[0])

        # X 18:00 Tip rests on Sunday + 1 rotating weekday (5x/week active)
        self.assertTrue(sns_editorial.should_skip_slot_for_human_pacing("x_tip", "18:00", now_jst=sun_18)[0])
        active_tip_days = sum(
            0 if sns_editorial.should_skip_slot_for_human_pacing(
                "x_tip", "18:00", now_jst=datetime.datetime(2026, 11, 2 + d, 18, 10, tzinfo=sns_editorial.JST)
            )[0] else 1
            for d in range(7)
        )
        self.assertEqual(active_tip_days, 5)

        # Threads 22:00 runs 6x/week on quiet weeks, 7x/week during surge
        active_th_days = sum(
            0 if sns_editorial.should_skip_slot_for_human_pacing(
                "threads", "22:00",
                now_jst=datetime.datetime(2026, 11, 2 + d, 22, 10, tzinfo=sns_editorial.JST),
                stores=quiet_stores,
            )[0] else 1
            for d in range(7)
        )
        self.assertEqual(active_th_days, 6)

        # IG Reel human jitter spread (3..28m) and fail-open after +32m
        off_r = sns_editorial.compute_human_schedule_offset_min("ig_reel", "21:15", now_jst=fri_20)
        self.assertTrue(3 <= off_r <= 28)
        self.assertFalse(sns_editorial.should_fire_scheduled_slot_now(
            "ig_reel", "21:15", now_jst=datetime.datetime(2026, 11, 6, 21, 16, tzinfo=sns_editorial.JST)
        ))
        self.assertTrue(sns_editorial.should_fire_scheduled_slot_now(
            "ig_reel", "21:15", now_jst=datetime.datetime(2026, 11, 6, 21, 48, tzinfo=sns_editorial.JST)
        ))

    def test_x_daily_budget_standalone_mix_and_column_rush_avoidance(self):
        """Verify X daily post budget ceiling, standalone 1-tweet anti-bot mix, and column release rush/daily guards."""
        import tempfile
        import job_state
        import scheduled_articles
        import sns_workflows
        import x_poster

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"JOB_STATE_DIR": tmpdir}):
                job_state.reset_cache_for_tests()
                wed_dt = datetime.datetime(2026, 11, 4, 21, 0, tzinfo=sns_editorial.JST)  # Wednesday (cap=4, peak=5)
                for i in range(4):
                    x_poster.record_x_auto_post_timestamp(wed_dt - datetime.timedelta(hours=4 - i))
                self.assertEqual(x_poster.get_x_daily_auto_post_count(now_jst=wed_dt), 4)
                over_reg, _ = x_poster.check_x_daily_post_budget(now_jst=wed_dt, is_primary_peak=False)
                over_peak, _ = x_poster.check_x_daily_post_budget(now_jst=wed_dt, is_primary_peak=True)
                self.assertTrue(over_reg)
                self.assertFalse(over_peak)

                # Sub-step 1b: 5th non-peak scheduled X post on Wednesday is blocked by daily budget!
                res_over = sns_workflows.execute_scheduled_x_post(
                    {"full_data": self.sample_stores, "is_seed": False},
                    slot_label="20:00",
                    force=False,
                    now_jst=wed_dt,
                )
                self.assertEqual(res_over["status"], "skipped")
                self.assertIn("cap reached", res_over["reason"])

                # Standalone 1-tweet X mix on Friday: minimal_snap omits self-reply URL and OGP card
                fri_dt = datetime.datetime(2026, 11, 6, 20, 12, tzinfo=sns_editorial.JST)
                sent_x = []
                fake_ed = {
                    "winner": {
                        "main_text": "金曜の渋谷、雨が上がってからオリエンタルラウンジ渋谷本店の女性28名・男性25名で急に華やいできた。",
                        "reply_text": "https://aiseki-navi.com/?s=x20",
                        "archetype_key": "minimal_snap",
                        "style": "ai_live_adlib",
                        "total_score": 93,
                    },
                    "session_record": {"slot_label": "20:00", "selected_text": "金曜の渋谷"},
                }
                with (
                    mock.patch("sns_editorial.run_editorial_pipeline", return_value=fake_ed),
                    mock.patch("x_poster.post_to_x", side_effect=lambda txt, reply_text=None, media_bytes=None, **kw: (sent_x.append((txt, reply_text, media_bytes)) or {"status": "success"})),
                ):
                    res_x = sns_workflows.execute_scheduled_x_post(
                        {"full_data": self.sample_stores, "is_seed": False},
                        slot_label="20:00",
                        force=False,
                        now_jst=fri_dt,
                    )
                self.assertEqual(res_x["status"], "success")
                self.assertTrue(res_x.get("standalone_no_reply"))
                self.assertIsNone(sent_x[0][1])
                self.assertIsNone(sent_x[0][2])

                # Column X release: holds during 19:50 rush, posts at 19:20, caps at max 1 column release per business day
                for fn in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, fn))
                job_state.reset_cache_for_tests()
                fake_articles = [
                    {
                        "slug": "col-a",
                        "path": "/guide/col-a",
                        "publish_at": "2026-11-04 19:00:00",
                        "x_title": "テスト記事A",
                        "x_main_text": "テスト記事Aの本音ツイート",
                    },
                    {
                        "slug": "col-b",
                        "path": "/guide/col-b",
                        "publish_at": "2026-11-04 19:00:00",
                        "x_title": "テスト記事B",
                        "x_main_text": "テスト記事Bの本音ツイート",
                    },
                ]
                with (
                    mock.patch("scheduled_articles.get_all_scheduled_articles", return_value=fake_articles),
                    mock.patch("x_poster.load_column_eyecatch_bytes", return_value=(None, "image/jpeg")),
                    mock.patch("x_poster.post_to_x", return_value={"status": "success"}) as mock_col_x,
                ):
                    rush_res = scheduled_articles.check_and_announce_newly_published(
                        now_jst=datetime.datetime(2026, 11, 4, 19, 50, 0)
                    )
                    self.assertEqual(mock_col_x.call_count, 0)
                    self.assertEqual(rush_res[0]["result"]["status"], "skipped")

                    ok_res = scheduled_articles.check_and_announce_newly_published(
                        now_jst=datetime.datetime(2026, 11, 4, 19, 20, 0)
                    )
                    self.assertEqual(mock_col_x.call_count, 1)
                    self.assertEqual(ok_res[0]["slug"], "col-a")

                    # Even 2 hours later on same business night, 2nd article is held until tomorrow
                    same_night_2nd = scheduled_articles.check_and_announce_newly_published(
                        now_jst=datetime.datetime(2026, 11, 4, 21, 10, 0)
                    )
                    self.assertEqual(mock_col_x.call_count, 1)
                    self.assertEqual(same_night_2nd[0]["result"]["status"], "skipped")

    def test_threads_1_in_3_url_non_collision_cooldown_and_visual_card(self):
        """Ensure Threads 19:30 Ladies and 22:00 Daily never both include URLs on the same night, and enforce 90m cooldown."""
        import tempfile
        import job_state
        import ladies_guide
        import threads_poster

        stores = [
            {"name": "OLG 新宿", "women": 12, "men": 28, "region": "Kanto"},
            {"name": "JIS UMEDA", "women": 24, "men": 18, "region": "Kinki"},
        ]
        url_ladies_days = 0
        url_daily_days = 0
        img_days = 0
        for d in range(1, 10):
            dt_1930 = datetime.datetime(2026, 11, d, 19, 30, tzinfo=sns_editorial.JST)
            dt_2200 = datetime.datetime(2026, 11, d, 22, 0, tzinfo=sns_editorial.JST)
            l_txt = ladies_guide.build_threads_ladies_post(stores, slot_label="19:30", now_jst=dt_1930, use_ai=False)
            d_footer = sns_editorial.build_threads_cta_footer(
                site_url="https://aiseki-navi.com",
                slot_label="22:00",
                style_key="live",
                now_jst=dt_2200,
            )
            img_u = threads_poster.select_scheduled_threads_image_url(now_jst=dt_2200)
            has_l_url = "https://" in l_txt
            has_d_url = "https://" in d_footer
            self.assertFalse(has_l_url and has_d_url, f"Both 19:30 and 22:00 Threads posts had URLs on 2026-11-{d:02d}")
            if img_u:
                self.assertFalse(has_d_url, "Visual card night should be URL-free")
                img_days += 1
            if has_l_url:
                url_ladies_days += 1
            if has_d_url:
                url_daily_days += 1
        self.assertEqual(url_ladies_days, 3)
        self.assertEqual(url_daily_days, 3)
        self.assertEqual(img_days, 3)

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"JOB_STATE_DIR": tmpdir}):
                job_state.reset_cache_for_tests()
                t0 = datetime.datetime(2026, 11, 6, 19, 35, tzinfo=sns_editorial.JST)
                threads_poster.record_threads_auto_post_timestamp(t0)
                self.assertTrue(threads_poster.check_threads_auto_post_cooldown(90, now_jst=t0 + datetime.timedelta(minutes=60))[0])
                self.assertFalse(threads_poster.check_threads_auto_post_cooldown(90, now_jst=t0 + datetime.timedelta(minutes=95))[0])


if __name__ == "__main__":
    unittest.main()
