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

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DISABLE_SHEETS", "1")

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
                wed_dt = datetime.datetime(2026, 11, 4, 21, 0, tzinfo=sns_editorial.JST)  # Wednesday (non-peak=3, peak=4 total)
                for i in range(3):
                    x_poster.record_x_auto_post_timestamp(wed_dt - datetime.timedelta(hours=4 - i))
                self.assertEqual(x_poster.get_x_daily_auto_post_count(now_jst=wed_dt), 3)
                over_reg, _ = x_poster.check_x_daily_post_budget(now_jst=wed_dt, is_primary_peak=False)
                over_peak, _ = x_poster.check_x_daily_post_budget(now_jst=wed_dt, is_primary_peak=True)
                self.assertTrue(over_reg)
                self.assertFalse(over_peak)

                # Once 4th (peak) post is recorded on Wednesday, even primary peak is capped (hard ceiling = 4/day weekday)
                x_poster.record_x_auto_post_timestamp(wed_dt - datetime.timedelta(minutes=30))
                self.assertTrue(x_poster.check_x_daily_post_budget(now_jst=wed_dt, is_primary_peak=True)[0])

                # Reset to 3 posts: 4th non-peak scheduled X post on Wednesday is blocked, while 22:00 peak is allowed
                for fn in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, fn))
                job_state.reset_cache_for_tests()
                for i in range(3):
                    x_poster.record_x_auto_post_timestamp(wed_dt - datetime.timedelta(hours=4 - i))

                res_over = sns_workflows.execute_scheduled_x_post(
                    {"full_data": self.sample_stores, "is_seed": False},
                    slot_label="20:00",
                    force=False,
                    now_jst=wed_dt,
                )
                self.assertEqual(res_over["status"], "skipped")
                self.assertIn("cap reached", res_over["reason"])

                # Weekend Fri/Sat 00:00 late-night peak is also treated as is_primary_peak=True (hard ceiling = 5/day Fri-Sun)
                for fn in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, fn))
                job_state.reset_cache_for_tests()
                sat_midnight = datetime.datetime(2026, 11, 7, 0, 12, tzinfo=sns_editorial.JST)  # business Friday night
                for i in range(4):
                    x_poster.record_x_auto_post_timestamp(sat_midnight - datetime.timedelta(hours=5 - i))
                self.assertTrue(x_poster.check_x_daily_post_budget(now_jst=sat_midnight, is_primary_peak=False)[0])
                self.assertFalse(x_poster.check_x_daily_post_budget(now_jst=sat_midnight, is_primary_peak=True)[0])

                # Clear budget before testing standalone 1-tweet X mix on Friday
                for fn in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, fn))
                job_state.reset_cache_for_tests()

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

                # Column X release: holds during 07:05 AM sleep hours & 19:50 rush, posts at 19:20, caps at max 1/day
                for fn in os.listdir(tmpdir):
                    os.remove(os.path.join(tmpdir, fn))
                job_state.reset_cache_for_tests()
                fake_articles = [
                    {
                        "slug": "col-a",
                        "path": "/guide/col-a",
                        "publish_at": "2026-11-04 06:00:00",
                        "x_title": "テスト記事A",
                        "x_main_text": "テスト記事Aの本音ツイート",
                    },
                    {
                        "slug": "col-b",
                        "path": "/guide/col-b",
                        "publish_at": "2026-11-04 06:00:00",
                        "x_title": "テスト記事B",
                        "x_main_text": "テスト記事Bの本音ツイート",
                    },
                ]
                with (
                    mock.patch("scheduled_articles.get_all_scheduled_articles", return_value=fake_articles),
                    mock.patch("x_poster.load_column_eyecatch_bytes", return_value=(None, "image/jpeg")),
                    mock.patch("x_poster.post_to_x", return_value={"status": "success"}) as mock_col_x,
                ):
                    # 07:05 AM JST after biz_date rollover must NOT leak a morning bot post
                    dawn_res = scheduled_articles.check_and_announce_newly_published(
                        now_jst=datetime.datetime(2026, 11, 4, 7, 5, 0)
                    )
                    self.assertEqual(mock_col_x.call_count, 0)
                    self.assertEqual(dawn_res[0]["result"]["status"], "skipped")
                    self.assertIn("quiet off-hours", dawn_res[0]["result"]["reason"])

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

                    # Even 2 hours later on same business night, 2nd article is held until tomorrow's active hours
                    same_night_2nd = scheduled_articles.check_and_announce_newly_published(
                        now_jst=datetime.datetime(2026, 11, 4, 21, 10, 0)
                    )
                    self.assertEqual(mock_col_x.call_count, 1)
                    self.assertEqual(same_night_2nd[0]["result"]["status"], "skipped")

                    # Next morning 07:05 AM JST (biz_date rolled over to 2026-11-05) still holds until >= 11:30 JST!
                    next_dawn_2nd = scheduled_articles.check_and_announce_newly_published(
                        now_jst=datetime.datetime(2026, 11, 5, 7, 5, 0)
                    )
                    self.assertEqual(mock_col_x.call_count, 1)
                    self.assertEqual(next_dawn_2nd[0]["result"]["status"], "skipped")

                    # At 12:15 JST next day, col-b announces cleanly
                    next_noon_2nd = scheduled_articles.check_and_announce_newly_published(
                        now_jst=datetime.datetime(2026, 11, 5, 12, 15, 0)
                    )
                    self.assertEqual(mock_col_x.call_count, 2)
                    self.assertEqual(next_noon_2nd[0]["slug"], "col-b")

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

    def test_tiktok_auto_schedule_default_on_settings_api_and_cron_coverage(self):
        """Verify TikTok scheduled job defaults ON when token exists, toggles via API, filters Korea, and has 100% cron coverage."""
        import tempfile
        import app as flask_app
        import job_state
        import tiktok_poster

        # 1. Verify 100% cron coverage across 90 consecutive days for TikTok 20:35 (cron ticks at 20:39, 45, 51, 57 JST)
        cron_minutes = (39, 45, 51, 57)
        for day_offset in range(90):
            base_day = datetime.date(2026, 11, 1) + datetime.timedelta(days=day_offset)
            off = sns_editorial.compute_human_schedule_offset_min(
                "tiktok", "20:35",
                now_jst=datetime.datetime(base_day.year, base_day.month, base_day.day, 20, 39, tzinfo=sns_editorial.JST),
            )
            self.assertTrue(4 <= off <= 20, f"Unexpected TikTok offset {off} on {base_day}")
            fired = any(
                sns_editorial.should_fire_scheduled_slot_now(
                    "tiktok", "20:35",
                    now_jst=datetime.datetime(base_day.year, base_day.month, base_day.day, 20, m, tzinfo=sns_editorial.JST),
                )
                for m in cron_minutes
            )
            self.assertTrue(fired, f"TikTok cron missed scheduled slot on {base_day} (offset={off})")

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"JOB_STATE_DIR": tmpdir}):
                job_state.reset_cache_for_tests()
                # Default is True when unset in job_state
                self.assertTrue(tiktok_poster.is_tiktok_auto_schedule_enabled())

                # Staff API toggles OFF and back ON
                client = flask_app.app.test_client()
                with client.session_transaction() as sess:
                    sess["is_staff"] = True
                    sess["is_master"] = True
                r_off = client.post("/api/staff/tiktok/settings", json={"auto_schedule_enabled": False})
                self.assertEqual(r_off.status_code, 200)
                self.assertFalse(r_off.get_json()["auto_schedule_enabled"])
                self.assertFalse(tiktok_poster.is_tiktok_auto_schedule_enabled())

                r_on = client.post("/api/staff/tiktok/settings", json={"auto_schedule_enabled": True})
                self.assertEqual(r_on.status_code, 200)
                self.assertTrue(r_on.get_json()["auto_schedule_enabled"])
                self.assertTrue(tiktok_poster.is_tiktok_auto_schedule_enabled())

                # Korean stores excluded by keyword even if region missing; male-surge domestic night still runs
                mixed_stores = [
                    {"name": "オリエンタルラウンジ カンナム", "women": 40, "men": 40},
                    {"name": "オリエンタルラウンジ 新宿", "women": 10, "men": 22, "region": "Kanto"},
                ]
                tue_dt = datetime.datetime(2026, 11, 3, 20, 51, tzinfo=sns_editorial.JST)  # Tuesday
                with (
                    mock.patch.dict(os.environ, {"TIKTOK_ACCESS_TOKEN": "test_tt_tok"}),
                    mock.patch("tiktok_poster.publish_live_reel_to_tiktok_inbox", return_value={"status": "success", "publish_id": "pub_123"}) as mock_tt_pub,
                ):
                    res1 = tiktok_poster.execute_scheduled_tiktok_inbox_post(
                        {"full_data": mixed_stores, "is_seed": False},
                        slot_label="20:35",
                        force=False,
                        now_jst=tue_dt,
                    )
                    self.assertEqual(res1["status"], "success")
                    self.assertEqual(mock_tt_pub.call_count, 1)
                    passed_stores = mock_tt_pub.call_args.args[0]
                    self.assertEqual(len(passed_stores), 1)
                    self.assertIn("新宿", passed_stores[0]["name"])

                    # Deduplicates on same business day when force=False
                    res2 = tiktok_poster.execute_scheduled_tiktok_inbox_post(
                        {"full_data": mixed_stores, "is_seed": False},
                        slot_label="20:35",
                        force=False,
                        now_jst=tue_dt + datetime.timedelta(minutes=6),
                    )
                    self.assertEqual(res2["status"], "skipped")
                    self.assertIn("Already pushed", res2["reason"])

    def test_instagram_reel_thumb_offset_and_thursday_late_feed_cooldown_coverage(self):
        """Verify IG Reel sets thumb_offset=1200ms (non-blank Scene 1 cover) and minute-54 cron covers Thursday late Feed."""
        import instagram_poster
        import reel_generator

        # Frame 0 (t=0.0s) has Scene 1 alpha == 0.0, whereas t=1.2s (1200ms) has Scene 1 alpha == 1.0
        self.assertEqual(reel_generator._fade_window(0.0, 0.0, 3.0, fade_in=0.38, fade_out=0.28), 0.0)
        self.assertEqual(reel_generator._fade_window(1.2, 0.0, 3.0, fade_in=0.38, fade_out=0.28), 1.0)

        captured_init_payload = {}

        class _FakeResp:
            def __init__(self, status_code, data):
                self.status_code = status_code
                self._data = data
                self.text = str(data)

            def json(self):
                return self._data

        def _fake_post(url, data=None, timeout=None):
            if url.endswith("/media"):
                captured_init_payload.update(data or {})
                return _FakeResp(200, {"id": "creation_999"})
            if url.endswith("/media_publish"):
                return _FakeResp(200, {"id": "media_777"})
            raise AssertionError(f"Unexpected POST url: {url}")

        def _fake_get(url, params=None, timeout=None):
            return _FakeResp(200, {"status_code": "FINISHED"})

        with (
            mock.patch.dict(os.environ, {"INSTAGRAM_ACCESS_TOKEN": "ig_tok", "INSTAGRAM_BUSINESS_ACCOUNT_ID": "1784140000"}),
            mock.patch("instagram_poster.requests.post", side_effect=_fake_post),
            mock.patch("instagram_poster.requests.get", side_effect=_fake_get),
            mock.patch("instagram_poster.time.sleep", return_value=None),
        ):
            res = instagram_poster.post_reel_to_instagram(
                "https://aiseki-navi.com/api/sns-card/reel-video/tok/live_top5.mp4",
                "テストキャプション",
                share_to_feed=False,
            )
        self.assertEqual(res["status"], "success")
        self.assertEqual(captured_init_payload.get("thumb_offset"), "1200")
        self.assertEqual(captured_init_payload.get("share_to_feed"), "false")

        # Verify worker.py ig_reel_2115 cron includes minute 54 so a 19:57 Thursday Feed + 95m cooldown still fires at 21:54
        with open(os.path.join(os.path.dirname(os.path.dirname(__file__)), "worker.py"), "r", encoding="utf-8") as fh:
            worker_src = fh.read()
        self.assertIn('minute="18,24,30,36,42,48,54"', worker_src)

    def test_staff_reel_and_tiktok_async_publish_no_nameerror(self):
        """Verify /api/staff/sns-reel/publish?async=1 and /api/staff/tiktok/publish-inbox?async=1 enqueue without NameError."""
        import tempfile
        import app as flask_app
        import job_state

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"JOB_STATE_DIR": tmpdir}):
                job_state.reset_cache_for_tests()
                client = flask_app.app.test_client()
                with client.session_transaction() as sess:
                    sess["is_staff"] = True
                    sess["is_master"] = True

                with mock.patch.object(flask_app, "_ensure_inline_sns_consumer_started", return_value=None):
                    r_reel = client.post(
                        "/api/staff/sns-reel/publish?async=1",
                        json={"reel_type": "live_top5", "caption": "テスト", "share_to_feed": True},
                    )
                    self.assertEqual(r_reel.status_code, 202)
                    j_reel = r_reel.get_json()
                    self.assertTrue(j_reel["ok"])
                    self.assertTrue(j_reel["queued"])
                    self.assertTrue(j_reel["job_id"])

                    r_tt = client.post(
                        "/api/staff/tiktok/publish-inbox?async=1",
                        json={"reel_type": "tiktok_gap_shock", "caption": "テストTikTok"},
                    )
                    self.assertEqual(r_tt.status_code, 202)
                    j_tt = r_tt.get_json()
                    self.assertTrue(j_tt["ok"])
                    self.assertTrue(j_tt["queued"])
                    self.assertTrue(j_tt["job_id"])

                with flask_app.data_lock:
                    old_full = flask_app.latest_data.get("full_data")
                    flask_app.latest_data["full_data"] = self.sample_stores
                try:
                    with mock.patch("tiktok_poster.publish_live_reel_to_tiktok_inbox", return_value={"status": "success", "publish_id": "tt_async_1"}):
                        exec_res = flask_app.execute_sns_job_payload(
                            "tiktok_inbox",
                            {"reel_type": "tiktok_gap_shock", "caption": "テストTikTok"},
                        )
                finally:
                    with flask_app.data_lock:
                        flask_app.latest_data["full_data"] = old_full
                self.assertEqual(exec_res["status"], "success")
                self.assertEqual(exec_res["publish_id"], "tt_async_1")

    def test_threads_human_polish_preserves_all_5_footer_variants(self):
        """Verify polish_draft_to_human_voice detaches & reattaches all Threads URL and non-URL footers without Lock 1 rejection."""
        import ladies_guide

        base_body = (
            "金曜22時まわって渋谷の空気もかなり温まってきました。\n"
            "オリエンタルラウンジ渋谷本店が女性28名・男性25名でちょうどいい熱量なのに対して、"
            "徒歩数分の別ブランドは男性先行で女子組が受付待ちゼロで座りやすいバランス。\n"
            "2軒目で動くなら入店直前の比率だけ軽く見ておくとハズしにくいです。"
        )
        claude_polished_body = (
            "金曜22時をまわって、渋谷の夜もいい感じに温まってきた。\n"
            "オリエンタルラウンジ渋谷本店が女性28名・男性25名でちょうどいい熱量なのに対して、"
            "徒歩数分の別ブランドは男性先行で女子組がそのまま座りやすい空気感。\n"
            "2軒目で動くなら、入る前の比率だけ軽く覗いておくとラクかも。"
        )

        with mock.patch(
            "sns_editorial.call_editorial_ai_json",
            return_value=({"polished_text": claude_polished_body}, "claude-sonnet-test", None),
        ):
            for day in range(1, 7):
                dt = datetime.datetime(2026, 11, day, 22, 10, tzinfo=sns_editorial.JST)
                footer = sns_editorial.build_threads_cta_footer(
                    site_url="https://aiseki-navi.com",
                    slot_label="22:00",
                    style_key="ai_live_adlib",
                    now_jst=dt,
                )
                draft = f"{base_body}\n\n{footer}"
                polished, pol_model = sns_editorial.polish_draft_to_human_voice(
                    draft, channel="threads", now_jst=dt, force_polish=True
                )
                self.assertEqual(pol_model, "claude-sonnet-test", f"Threads polish failed on day={day}")
                self.assertIn(claude_polished_body, polished)
                self.assertTrue(polished.endswith(footer), f"Footer was mutated or lost on day={day}: {polished}")

        # Also verify ladies_guide URL and non-URL footers
        for day in (1, 3):  # day%3==0 carries URL, day%3==1 has bio-link text
            dt_l = datetime.datetime(2026, 11, day, 19, 35, tzinfo=sns_editorial.JST)
            l_draft = ladies_guide.build_threads_ladies_post(self.sample_stores, slot_label="19:30", now_jst=dt_l, use_ai=False)
            l_body_only = l_draft.rsplit("\n\n", 1)[0]
            with mock.patch(
                "sns_editorial.call_editorial_ai_json",
                return_value=({"polished_text": l_body_only}, "claude-sonnet-test", None),
            ):
                l_pol, l_model = sns_editorial.polish_draft_to_human_voice(
                    l_draft, channel="threads", now_jst=dt_l, force_polish=True
                )
                self.assertEqual(l_model, "claude-sonnet-test", f"Ladies Threads polish failed on day={day}")
                if "https://" in l_draft:
                    self.assertIn("https://aiseki-navi.com", l_pol)

    def test_x_archetype_selection_sync_and_non_ai_winner_preservation(self):
        """Ensure deterministic non-AI X winners keep their reply URL & OGP even on minimal_snap rotation slots."""
        import tempfile
        import job_state
        import sns_workflows

        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"JOB_STATE_DIR": tmpdir}):
                job_state.reset_cache_for_tests()
                fri_dt = datetime.datetime(2026, 11, 6, 20, 12, tzinfo=sns_editorial.JST)
                sent_x = []
                fake_det_ed = {
                    "winner": {
                        "main_text": "金曜20時、オリエンタルラウンジ渋谷本店が女性28名・男性25名で盛り上がっています。",
                        "reply_text": "▼リアルタイム空席確認\nhttps://aiseki-navi.com/?s=x20",
                        "style": "live",
                        "total_score": 84,
                    },
                    "session_record": {"slot_label": "20:00", "selected_text": "金曜20時"},
                }
                with (
                    mock.patch("sns_editorial.select_x_post_archetype", return_value={"key": "minimal_snap"}),
                    mock.patch("sns_editorial.run_editorial_pipeline", return_value=fake_det_ed),
                    mock.patch("ogp_generator.generate_live_ogp_png", return_value=b"FAKEPNG"),
                    mock.patch("x_poster.post_to_x", side_effect=lambda txt, reply_text=None, media_bytes=None, **kw: (sent_x.append((txt, reply_text, media_bytes)) or {"status": "success"})),
                ):
                    res = sns_workflows.execute_scheduled_x_post(
                        {"full_data": self.sample_stores, "is_seed": False},
                        slot_label="20:00",
                        force=False,
                        now_jst=fri_dt,
                    )
                self.assertEqual(res["status"], "success")
                self.assertFalse(res.get("standalone_no_reply"))
                self.assertIsNotNone(sent_x[0][1])
                self.assertEqual(sent_x[0][2], b"FAKEPNG")

    def test_reel_ai_caption_fact_lock_same_hub_guard_and_store_count_unification(self):
        """Verify Reel AI caption accepts weather/time digits, avoids cross-city 'same station' claims, and unifies 全国60店舗超."""
        import ig_carousel_generator
        import ogp_generator
        import reel_generator
        import tips_poster
        import x_poster

        # 1. Cross-city stores ONLY (no same-hub pair): select_scheduled_reel_type must NOT pick tiktok_gap_shock,
        # and build_reel_caption(reel_type="tiktok_gap_shock") must NOT claim "同じ駅で徒歩4分"
        cross_city_stores = [
            {"name": "オリエンタルラウンジ 新宿", "women": 32, "men": 26, "region": "Kanto"},
            {"name": "JIS UMEDA", "women": 4, "men": 19, "region": "Kinki"},
        ]
        for day in range(1, 15):
            dt = datetime.datetime(2026, 11, day, 21, 15, tzinfo=sns_editorial.JST)
            if dt.weekday() in (2, 3, 4, 5):
                picked = reel_generator.select_scheduled_reel_type(cross_city_stores, now_jst=dt)
                self.assertNotEqual(picked, "tiktok_gap_shock")
        gap_cap = reel_generator.build_reel_caption(cross_city_stores, reel_type="tiktok_gap_shock")
        self.assertNotIn("同じ駅で徒歩4分", gap_cap)

        # 2. build_ai_reel_caption accepts weather temperature (14℃) & current minute (15分) on ladies_zero_wait
        ladies_stores = [
            {"name": "オリエンタルラウンジ 新宿", "women": 8, "men": 24, "region": "Kanto"},
            {"name": "オリエンタルラウンジ 渋谷本店", "women": 28, "men": 12, "region": "Kanto"},
        ]
        dt_2115 = datetime.datetime(2026, 11, 5, 21, 15, tzinfo=sns_editorial.JST)
        ai_hook = "気温14℃の21時15分、オリエンタルラウンジ新宿は男性24名：女性8名で女性は待ち時間ゼロの好条件。"
        with (
            mock.patch.dict(os.environ, {"DISABLE_SCHEDULER": "0"}),
            mock.patch("sns_editorial.has_any_ai_key", return_value=True),
            mock.patch("sns_editorial.fetch_live_weather_context", return_value={"summary": "東京 14℃ 晴れ"}),
            mock.patch("sns_editorial.call_editorial_ai_json", return_value=({"hook_line": ai_hook}, "gemini-test", None)),
        ):
            cap = reel_generator.build_ai_reel_caption(ladies_stores, reel_type="ladies_zero_wait", now_jst=dt_2115)
        self.assertIn("気温14℃の21時15分", cap)
        self.assertNotIn("53店舗", cap)
        self.assertNotIn("51店舗", cap)

        # Verify live_top5 closers use 全国60店舗超 across days
        top5_caps = [
            reel_generator.build_reel_caption(
                self.sample_stores,
                reel_type="live_top5",
                now_jst=datetime.datetime(2026, 11, d, 21, 15, tzinfo=sns_editorial.JST),
            )
            for d in range(1, 5)
        ]
        self.assertTrue(any("全国60店舗超" in c for c in top5_caps))
        for c in top5_caps:
            self.assertNotIn("53店舗", c)
            self.assertNotIn("51店舗", c)

        # 3. Verify zero leftover 51店舗 / 53店舗 / 53店 in generated copy & metadata across modules
        carousel_ctx = ig_carousel_generator.build_forecast_context(self.sample_stores, now_jst=dt_2115)
        carousel_cap = ig_carousel_generator.build_captions(carousel_ctx)["instagram_caption"]
        self.assertIn("全国60店舗超", carousel_cap)
        self.assertNotIn("53店舗", carousel_cap)
        self.assertNotIn("51店舗", carousel_cap)

        with open(tips_poster.__file__, "r", encoding="utf-8") as fh:
            tips_src = fh.read()
        self.assertNotIn("51店舗", tips_src)
        self.assertNotIn("53店舗", tips_src)

        with open(x_poster.__file__, "r", encoding="utf-8") as fh:
            xp_src = fh.read()
        self.assertNotIn("51店舗", xp_src)
        self.assertNotIn("53店舗", xp_src)

        with open(ogp_generator.__file__, "r", encoding="utf-8") as fh:
            ogp_src = fh.read()
        self.assertNotIn("53店舗", ogp_src)
        self.assertNotIn("51店舗", ogp_src)

    def test_tiktok_schedule_label_consistency_and_pacing_summary_ui_wiring(self):
        """Verify Wed/Fri/Sat TikTok schedule labels, pacing_summary UI rendering, and zero stale Claude Sonnet labels."""
        import app as app_module
        import tiktok_poster

        self.assertIn("Wed/Fri/Sat", tiktok_poster.execute_scheduled_tiktok_inbox_post.__doc__ or "")
        self.assertNotIn("Tue/Thu/Fri/Sat", tiktok_poster.execute_scheduled_tiktok_inbox_post.__doc__ or "")

        client = app_module.app.test_client()
        with client.session_transaction() as sess:
            sess["is_master"] = True
            sess["staff_authenticated"] = True

        for ch in ("x", "tiktok"):
            resp = client.get(f"/staff/sns-editorial?channel={ch}")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            self.assertIn('id="sns-pacing-summary-box"', html)
            self.assertIn("水・木・金・土（週4回", html)
            self.assertIn("水・金・土（週3回", html)
            self.assertIn("最低48分間隔", html)
            self.assertIn("週6日稼働", html)
            self.assertIn("12:20/15:20", html)
            self.assertIn("水・金・土のTikTok自動下書き送信", html)
            self.assertNotIn("火・木・金・土", html)
            self.assertNotIn("Claude Sonnet 優先", html)
            self.assertIn("Claude Opus × 2段階推敲 優先", html)

        pref_resp = client.post(
            "/api/sns-editorial/ai-config",
            json={"action": "set_pref", "engine_pref": "claude"},
        )
        self.assertEqual(pref_resp.status_code, 200)
        pref_json = pref_resp.get_json()
        self.assertIn("Claude Opus × 2段階推敲", pref_json.get("message", ""))
        self.assertNotIn("Claude Sonnet", pref_json.get("message", ""))

    def test_public_reel_mp4_http_206_range_slot_pinning_and_900s_ttl(self):
        """Verify /ogp/reel/<rt>.mp4 supports HTTP 206 Range, 416 invalid Range, slot pinning, continuation guard, and 900s shared TTL."""
        import app as app_module
        import reel_generator
        import turbo_store

        self.assertEqual(reel_generator.REEL_SHARED_CACHE_TTL_SEC, 900)
        sample_mp4 = b"\x00\x00\x00\x18ftypmp42" + bytes(range(256)) * 12
        client = app_module.app.test_client()

        render_calls = []

        def _fake_render(_stores, reel_type="live_top5", force_refresh=False):
            render_calls.append((reel_type, bool(force_refresh)))
            return sample_mp4

        with mock.patch("reel_generator.render_reel_mp4_bytes", side_effect=_fake_render):
            # 1) Standard 200 full fetch
            r200 = client.get("/ogp/reel/live_top5.mp4")
            self.assertEqual(r200.status_code, 200)
            self.assertEqual(r200.headers.get("Accept-Ranges"), "bytes")
            self.assertTrue(r200.headers.get("ETag"))
            self.assertEqual(r200.data, sample_mp4)

            # 2) Safari / Meta / TikTok 2-byte probe Range: bytes=0-1
            r206_probe = client.get("/ogp/reel/live_top5.mp4", headers={"Range": "bytes=0-1"})
            self.assertEqual(r206_probe.status_code, 206)
            self.assertEqual(r206_probe.headers.get("Content-Range"), f"bytes 0-1/{len(sample_mp4)}")
            self.assertEqual(int(r206_probe.headers.get("Content-Length")), 2)
            self.assertEqual(r206_probe.data, sample_mp4[:2])

            # 3) Mid-stream chunk Range: bytes=64-127
            r206_mid = client.get("/ogp/reel/live_top5.mp4", headers={"Range": "bytes=64-127"})
            self.assertEqual(r206_mid.status_code, 206)
            self.assertEqual(r206_mid.headers.get("Content-Range"), f"bytes 64-127/{len(sample_mp4)}")
            self.assertEqual(r206_mid.data, sample_mp4[64:128])

            # 4) Out-of-bounds Range -> 416
            r416 = client.get("/ogp/reel/live_top5.mp4", headers={"Range": "bytes=999999-1000000"})
            self.assertEqual(r416.status_code, 416)

            # 5) Explicit ?download=1 -> 200 attachment
            r_dl = client.get("/ogp/reel/live_top5.mp4?download=1")
            self.assertEqual(r_dl.status_code, 200)
            self.assertIn("attachment;", r_dl.headers.get("Content-Disposition", ""))
            self.assertEqual(r_dl.data, sample_mp4)

            # 6) Safari continuation Range on ?refresh=1 must NOT force_refresh on byte offset > 0
            render_calls.clear()
            client.get("/ogp/reel/live_top5.mp4?refresh=1", headers={"Range": "bytes=0-1"})
            client.get("/ogp/reel/live_top5.mp4?refresh=1", headers={"Range": "bytes=64-127"})
            self.assertEqual(render_calls, [("live_top5", True), ("live_top5", False)])

        # 7) Pinned ?s=<ts_code> slot serves byte-identical MP4 without calling render_reel_mp4_bytes
        import tempfile
        pinned_mp4 = b"\x00\x00\x00\x18ftypisom" + b"PINNED_SLOT_MP4_" * 120
        with tempfile.TemporaryDirectory() as tmp_reel_dir, \
             mock.patch.object(reel_generator, "REEL_TMP_CACHE_DIR", tmp_reel_dir):
            with mock.patch.object(turbo_store, "set_media_blob", wraps=turbo_store.set_media_blob) as spy_set:
                reel_generator.pin_shared_reel_mp4_slot("area_battle", "20261104_2118", pinned_mp4)
                self.assertTrue(any(c.kwargs.get("ttl_sec") == 900 for c in spy_set.call_args_list))

            with mock.patch("reel_generator.render_reel_mp4_bytes", side_effect=AssertionError("Should hit pinned slot")):
                r_pin = client.get("/ogp/reel/area_battle.mp4?s=20261104_2118", headers={"Range": "bytes=0-15"})
                self.assertEqual(r_pin.status_code, 206)
                self.assertEqual(r_pin.data, pinned_mp4[:16])

    def test_reel_background_and_ken_burns_rotation_across_templates_and_days(self):
        """Verify Reel templates rotate across all portrait images, procedural bokeh variants, and 4 Ken Burns drift modes."""
        import reel_generator

        jst = datetime.timezone(datetime.timedelta(hours=9))
        base_night = datetime.datetime(2026, 11, 4, 21, 15, tzinfo=jst)

        # On the same night, live_top5, area_battle, and tiktok_gap_shock must pick 3 distinct backgrounds
        same_night_bgs = {
            rt: reel_generator.list_available_backgrounds(rt, now_jst=base_night)
            for rt in ("live_top5", "area_battle", "tiktok_gap_shock")
        }
        chosen_sources = {
            info["active_file"] or f"procedural:{info['bg_variant']}"
            for info in same_night_bgs.values()
        }
        self.assertEqual(len(chosen_sources), 3)

        # Across 4 consecutive nights, live_top5 cycles through multiple backgrounds and all 4 Ken Burns modes
        multi_day_sources = set()
        multi_day_kb = set()
        for d in range(4):
            dt = base_night + datetime.timedelta(days=d)
            info = reel_generator.list_available_backgrounds("live_top5", now_jst=dt)
            multi_day_sources.add(info["active_file"] or "procedural_luxury")
            multi_day_kb.add(info["ken_burns_mode"])
        self.assertGreaterEqual(len(multi_day_sources), 3)
        self.assertEqual(multi_day_kb, set(reel_generator._KEN_BURNS_MODES))

        # Procedural luxury variants 0, 1, 2 render distinct valid RGB artwork and preview frames differ across days
        c0 = reel_generator._build_procedural_luxury_bg(760, 1350, "live_top5", bg_variant=0)
        c1 = reel_generator._build_procedural_luxury_bg(760, 1350, "live_top5", bg_variant=1)
        self.assertNotEqual(c0.tobytes(), c1.tobytes())

        jpg_d0 = reel_generator.render_reel_preview_frame_jpeg(self.sample_stores, reel_type="live_top5", sec=4.5, now_jst=base_night)
        jpg_d1 = reel_generator.render_reel_preview_frame_jpeg(
            self.sample_stores,
            reel_type="live_top5",
            sec=4.5,
            now_jst=base_night + datetime.timedelta(days=1),
        )
        self.assertTrue(jpg_d0.startswith(b"\xff\xd8"))
        self.assertTrue(jpg_d1.startswith(b"\xff\xd8"))
        self.assertNotEqual(jpg_d0, jpg_d1)

    def test_rotated_hashtags_across_ig_tiktok_ladies_and_evaluator_100pts(self):
        """Verify IG/TikTok/Ladies hashtag blocks rotate across days, obey brand rules, and score full marks in run_evaluator."""
        import ig_carousel_generator
        import ladies_guide

        jst = datetime.timezone(datetime.timedelta(hours=9))
        base_dt = datetime.datetime(2026, 11, 4, 21, 0, tzinfo=jst)
        rich_stores = self.sample_stores + [
            {"name": "JIS 新宿店", "men": 14, "women": 36, "area": "新宿", "station": "新宿駅", "source": "jis"},
            {"name": "オリエンタルラウンジ 梅田店", "men": 32, "women": 24, "area": "梅田", "station": "梅田駅", "source": "oriental"},
        ]

        for cat in ("carousel", "ladies", "tiktok_gap", "tiktok_midnight"):
            seen_lines = set()
            for d in range(4):
                line = sns_editorial.pick_rotated_hashtag_line(
                    "tiktok" if cat.startswith("tiktok") else "instagram",
                    cat,
                    now_jst=base_dt + datetime.timedelta(days=d),
                    area_tag="新宿・歌舞伎町" if cat == "tiktok_gap" else "",
                )
                seen_lines.add(line)
                tags = re.findall(r"#\S+", line)
                self.assertGreaterEqual(len(tags), 5)
                self.assertLessEqual(len(tags), 7)
                self.assertIn("#相席ナビ", tags)
                self.assertIn("#相席ラウンジ", tags)
                self.assertIsNone(re.search(r"オリエンタル(?!ラウンジ)", line))
                self.assertIsNone(re.search(r"\d", line))
            self.assertGreaterEqual(len(seen_lines), 3)

        # Verify IG candidates, TikTok candidates, ig_carousel_generator, and ladies_guide rotate across days & pass evaluator
        research_d0 = sns_editorial.run_researcher(rich_stores, slot_label="21:45", now_jst=base_dt)
        research_d1 = sns_editorial.run_researcher(
            rich_stores,
            slot_label="21:45",
            now_jst=base_dt + datetime.timedelta(days=1),
        )
        ig_cands_d0 = sns_editorial._write_instagram_candidates(research_d0, now_jst=base_dt)
        ig_cands_d1 = sns_editorial._write_instagram_candidates(research_d1, now_jst=base_dt + datetime.timedelta(days=1))
        self.assertNotEqual(
            ig_cands_d0[0]["main_text"].splitlines()[-1],
            ig_cands_d1[0]["main_text"].splitlines()[-1],
        )
        ev_ig = sns_editorial.run_evaluator(ig_cands_d0[:3], research_d0, {}, channel="instagram")
        self.assertEqual(ev_ig["winner"]["scores"]["weight_fit"], 10)

        tt_cands_d0 = sns_editorial._write_tiktok_candidates(
            research_d0,
            slot_label="20:35",
            now_jst=base_dt,
            site_url="https://aiseki-navi.com",
        )
        tt_cands_d1 = sns_editorial._write_tiktok_candidates(
            research_d1,
            slot_label="20:35",
            now_jst=base_dt + datetime.timedelta(days=1),
            site_url="https://aiseki-navi.com",
        )
        self.assertNotEqual(
            tt_cands_d0[0]["main_text"].splitlines()[-1],
            tt_cands_d1[0]["main_text"].splitlines()[-1],
        )
        for c in tt_cands_d0:
            self.assertNotIn("モニター", c["main_text"])
        ev_tt = sns_editorial.run_evaluator(tt_cands_d0[:3], research_d0, {}, channel="tiktok")
        self.assertEqual(ev_tt["winner"]["scores"]["weight_fit"], 10)

        ctx0 = ig_carousel_generator.build_forecast_context(live_stores=rich_stores, now_jst=base_dt)
        ctx1 = ig_carousel_generator.build_forecast_context(live_stores=rich_stores, now_jst=base_dt + datetime.timedelta(days=1))
        self.assertNotEqual(
            ig_carousel_generator.build_captions(ctx0)["instagram_caption"].splitlines()[-1],
            ig_carousel_generator.build_captions(ctx1)["instagram_caption"].splitlines()[-1],
        )

        lad0 = ladies_guide.build_instagram_ladies_caption(rich_stores, now_jst=base_dt, use_ai=False)
        lad1 = ladies_guide.build_instagram_ladies_caption(
            rich_stores,
            now_jst=base_dt + datetime.timedelta(days=1),
            use_ai=False,
        )
        self.assertNotEqual(lad0.splitlines()[-1], lad1.splitlines()[-1])

    def test_note_rss_check_schedule_spacing_and_24h_freshness_window(self):
        """Verify note_rss_check runs at 03:20 & 06:20 UTC (12:20 & 15:20 JST) away from 18:00 Tips and retains 24h freshness."""
        from email.utils import format_datetime
        import tempfile
        import job_state
        import note_poster
        import worker

        sched = worker.build_worker_scheduler()
        note_job = sched.get_job("note_rss_check")
        self.assertIsNotNone(note_job)
        trig_str = str(note_job.trigger)
        self.assertIn("hour='3,6'", trig_str)
        self.assertNotIn("8", trig_str)

        now_utc = datetime.datetime.now(datetime.timezone.utc)
        evening_yesterday_pub = format_datetime(now_utc - datetime.timedelta(hours=18))
        stale_30h_pub = format_datetime(now_utc - datetime.timedelta(hours=30))

        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notes_window.json")
            job_state.mark_done("note:https://note.com/aiseki_navi/n/n_seed_init")
            articles = [
                {
                    "title": "昨夜公開した新鮮な検証記事",
                    "link": "https://note.com/aiseki_navi/n/n_18h_fresh",
                    "pub_date": evening_yesterday_pub,
                    "guid": "n_18h_fresh",
                },
                {
                    "title": "30時間前の過去記事",
                    "link": "https://note.com/aiseki_navi/n/n_30h_old",
                    "pub_date": stale_30h_pub,
                    "guid": "n_30h_old",
                },
            ]
            with mock.patch.object(note_poster, "STATE_FILE", state_file), \
                 mock.patch("note_poster.fetch_latest_note_articles", return_value=articles), \
                 mock.patch("x_poster.post_to_x", return_value={"status": "success"}) as post_mock:
                res = note_poster.check_and_post_new_note()
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["article"]["guid"], "n_18h_fresh")
            self.assertEqual(post_mock.call_count, 1)
            self.assertTrue(job_state.is_done("note:https://note.com/aiseki_navi/n/n_30h_old"))

    def test_chunked_redis_media_blob_and_cross_worker_disk_slot_cache(self):
        """Verify >1MB Reel MP4s chunk safely below Upstash 1MB Redis limit, detect tampering, share via /tmp slot disk cache, and pin-on-miss."""
        import app as app_mod
        import reel_generator
        import turbo_store

        class _UpstashStrictFakeRedis:
            MAX_CMD_BYTES = 1048576  # 1 MB Upstash command limit

            def __init__(self):
                self.kv = {}
                self.setex_sizes = []

            def setex(self, key, ttl, val):
                b_val = val if isinstance(val, (bytes, bytearray)) else str(val).encode("utf-8")
                if len(b_val) > self.MAX_CMD_BYTES:
                    raise RuntimeError(f"ERR max request size exceeded ({len(b_val)} > {self.MAX_CMD_BYTES})")
                self.setex_sizes.append(len(b_val))
                self.kv[key] = bytes(b_val)
                return True

            def get(self, key):
                return self.kv.get(key)

        fake_redis = _UpstashStrictFakeRedis()
        big_mp4 = b"\x00\x00\x00\x18ftypisom" + (b"AISEKI_REEL_CHUNK_PAYLOAD_9x16_" * 59193)  # ~1.83 MB
        self.assertGreater(len(big_mp4), 1750000)

        with mock.patch.object(turbo_store, "_mem_bus_enabled", False), \
             mock.patch.object(turbo_store, "get_redis_client", return_value=fake_redis):
            ok = turbo_store.set_media_blob("reel_mp4_slot:live_top5:20261106_2118", big_mp4, mime_type="video/mp4", ttl_sec=900)
            self.assertTrue(ok)
            self.assertGreaterEqual(len(fake_redis.setex_sizes), 4)
            self.assertTrue(all(sz < fake_redis.MAX_CMD_BYTES for sz in fake_redis.setex_sizes))

            loaded = turbo_store.get_media_blob("reel_mp4_slot:live_top5:20261106_2118")
            self.assertIsNotNone(loaded)
            self.assertEqual(loaded[1], "video/mp4")
            self.assertEqual(loaded[0], big_mp4)

            # Tamper with one part -> SHA-256 verification must reject torn/corrupt payload
            part_keys = [k for k in fake_redis.kv if ":part:" in k]
            self.assertGreaterEqual(len(part_keys), 3)
            fake_redis.kv[part_keys[1]] = b"CORRUPTED" + fake_redis.kv[part_keys[1]][9:]
            self.assertIsNone(turbo_store.get_media_blob("reel_mp4_slot:live_top5:20261106_2118"))

        # Cross-worker /tmp/aiseki_reels/slot_*.mp4 recovery when both Redis and process-local RAM miss
        import tempfile
        with tempfile.TemporaryDirectory() as tmp_reel_dir, \
             mock.patch.object(reel_generator, "REEL_TMP_CACHE_DIR", tmp_reel_dir):
            disk_mp4 = b"\x00\x00\x00\x18ftypisom" + (b"CROSS_WORKER_DISK_SLOT_" * 150)
            reel_generator.pin_shared_reel_mp4_slot("live_top5", "worker2_2118", disk_mp4, ttl_sec=900)
            with reel_generator._PINNED_SLOT_LOCK:
                reel_generator._PINNED_SLOT_CACHE.clear()
            with mock.patch.object(turbo_store, "get_media_blob", return_value=None):
                recovered = reel_generator.get_pinned_reel_mp4_slot("live_top5", "worker2_2118")
                self.assertEqual(recovered, disk_mp4)

            # Pin-on-miss in /ogp/reel/<rt>.mp4?s=<slot_token> prevents re-rendering on second Range chunk
            client = app_mod.app.test_client()
            miss_calls = []
            slot_sample = b"\x00\x00\x00\x18ftypisom" + (b"PIN_ON_MISS_SAMPLE_" * 140)

            def _fake_render_once(stores, reel_type="live_top5", force_refresh=False, now_jst=None):
                miss_calls.append((reel_type, bool(force_refresh)))
                return slot_sample

            with mock.patch("reel_generator.render_reel_mp4_bytes", side_effect=_fake_render_once):
                r_c1 = client.get("/ogp/reel/live_top5.mp4?s=miss_20261107_2118", headers={"Range": "bytes=0-31"})
                r_c2 = client.get("/ogp/reel/live_top5.mp4?s=miss_20261107_2118", headers={"Range": "bytes=32-95"})
                self.assertEqual(r_c1.status_code, 206)
                self.assertEqual(r_c2.status_code, 206)
                self.assertEqual(r_c1.data + r_c2.data, slot_sample[:96])
                self.assertEqual(len(miss_calls), 1)

    def test_public_reel_mp4_multipart_range_if_range_suffix_and_safari_preview_pin(self):
        """Verify RFC 9110 §14.2 multipart Range -> 200 OK, OOB multipart -> 416, If-Range, suffix Range, and single-encode Safari preview."""
        import tempfile
        import app as app_mod
        import reel_generator

        sample_mp4 = b"\x00\x00\x00\x18ftypisom" + bytes(range(256)) * 20
        render_calls = []

        def _fake_render(stores, reel_type="live_top5", force_refresh=False, now_jst=None):
            render_calls.append((reel_type, bool(force_refresh)))
            return sample_mp4

        client = app_mod.app.test_client()
        with tempfile.TemporaryDirectory() as tmp_reel_dir, \
             mock.patch.object(reel_generator, "REEL_TMP_CACHE_DIR", tmp_reel_dir), \
             mock.patch("reel_generator.render_reel_mp4_bytes", side_effect=_fake_render):
            with reel_generator._PINNED_SLOT_LOCK:
                reel_generator._PINNED_SLOT_CACHE.clear()
            # 1) Satisfiable multipart Range must fall back to 200 OK instead of crashing with 416
            r_multi_ok = client.get("/ogp/reel/live_top5.mp4", headers={"Range": "bytes=0-63, 128-191"})
            self.assertEqual(r_multi_ok.status_code, 200)
            self.assertEqual(r_multi_ok.data, sample_mp4)
            etag = r_multi_ok.headers.get("ETag")
            self.assertTrue(etag)

            # 2) Unsatisfiable multipart Range (all ranges past EOF) must still return 416
            r_multi_416 = client.get(
                "/ogp/reel/live_top5.mp4",
                headers={"Range": "bytes=999999-1000000, 888888-999999"},
            )
            self.assertEqual(r_multi_416.status_code, 416)

            # 3) If-Range matching ETag -> 206; mismatched ETag -> 200 full body
            r_if_match = client.get(
                "/ogp/reel/live_top5.mp4",
                headers={"Range": "bytes=32-95", "If-Range": etag},
            )
            self.assertEqual(r_if_match.status_code, 206)
            self.assertEqual(r_if_match.data, sample_mp4[32:96])

            r_if_mismatch = client.get(
                "/ogp/reel/live_top5.mp4",
                headers={"Range": "bytes=32-95", "If-Range": '"stale-mismatched-etag"'},
            )
            self.assertEqual(r_if_mismatch.status_code, 200)
            self.assertEqual(r_if_mismatch.data, sample_mp4)

            # 4) Safari ?refresh=1&t=<ts> sequence (0-1 probe -> -64 suffix EOF probe -> 0-127 chunk) encodes ONLY ONCE
            render_calls.clear()
            r_p1 = client.get("/ogp/reel/live_top5.mp4?refresh=1&t=1761888123", headers={"Range": "bytes=0-1"})
            r_p2 = client.get("/ogp/reel/live_top5.mp4?refresh=1&t=1761888123", headers={"Range": "bytes=-64"})
            r_p3 = client.get("/ogp/reel/live_top5.mp4?refresh=1&t=1761888123", headers={"Range": "bytes=0-127"})
            self.assertEqual(r_p1.status_code, 206)
            self.assertEqual(r_p2.status_code, 206)
            self.assertEqual(r_p2.data, sample_mp4[-64:])
            self.assertEqual(r_p3.status_code, 206)
            self.assertEqual(r_p3.data, sample_mp4[:128])
            self.assertEqual(render_calls, [("live_top5", True)])

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root_dir, "templates", "staff_sns_editorial.html"), encoding="utf-8") as f_tpl:
            tpl_src = f_tpl.read()
        self.assertIn("download=1", tpl_src)

    def test_twelve_week_scheduled_reel_zero_adjacent_bg_collision_and_subpixel_ken_burns(self):
        """Verify 12 weeks (48 nights) of scheduled Wed-Sat IG Reels never repeat background or Ken Burns back-to-back, and Ken Burns moves smoothly per frame."""
        import reel_generator

        jst = datetime.timezone(datetime.timedelta(hours=9))
        start_dt = datetime.datetime(2026, 11, 4, 21, 18, tzinfo=jst)
        rich_stores = self.sample_stores + [
            {"name": "OLG 新宿", "women": 46, "men": 22, "region": "Kanto"},
            {"name": "JIS SHINJUKU", "women": 19, "men": 35, "region": "Kanto"},
            {"name": "OLG 大阪駅前", "women": 42, "men": 29, "region": "Kansai"},
            {"name": "JIS UMEDA", "women": 24, "men": 36, "region": "Kansai"},
        ]

        recent_rts = []
        prev_visual = None
        prev_kb = None
        checked_nights = 0
        for d in range(84):
            dt = start_dt + datetime.timedelta(days=d)
            if dt.weekday() not in (2, 3, 4, 5):
                continue
            rt = reel_generator.select_scheduled_reel_type(
                rich_stores,
                platform="instagram",
                now_jst=dt,
                recent_types=recent_rts,
            )
            recent_rts.insert(0, rt)
            bg_info = reel_generator.list_available_backgrounds(rt, now_jst=dt)
            cur_visual = bg_info["active_file"] or f"procedural:{bg_info['bg_variant']}"
            cur_kb = bg_info["ken_burns_mode"]
            if prev_visual is not None:
                self.assertNotEqual(cur_visual, prev_visual, f"Back-to-back Reel visual collision on {dt.date()} ({rt}): {cur_visual}")
                self.assertNotEqual(cur_kb, prev_kb, f"Back-to-back Ken Burns collision on {dt.date()} ({rt}): {cur_kb}")
            prev_visual = cur_visual
            prev_kb = cur_kb
            checked_nights += 1
        self.assertEqual(checked_nights, 48)

        # Subpixel Ken Burns check: two adjacent 24fps frames (t=5.50s vs t=5.5417s, after card entrance settles) smoothly interpolate background
        ctx = reel_generator.build_reel_context(rich_stores, reel_type="live_top5", now_jst=start_dt)
        bg_canvas = reel_generator._build_procedural_luxury_bg(828, 1472, "live_top5", bg_variant=0)
        renderer = reel_generator._ReelSceneRenderer(ctx)
        try:
            f0 = renderer.render_full_rgb_frame(5.50, bg_canvas, ken_burns_mode="zoom_in_up")
            f1 = renderer.render_full_rgb_frame(5.50 + (1.0 / 24.0), bg_canvas, ken_burns_mode="zoom_in_up")
            self.assertNotEqual(f0.tobytes(), f1.tobytes())
        finally:
            renderer.close()

    def test_weekday_consistent_hashtags_28h_note_window_and_copy_unification(self):
        """Verify Mon-Thu posts never emit #華金/#週末 tags, 25h cooldown-deferred notes still post (<28h), and 53店舗/Claude Sonnet leftovers are gone."""
        from email.utils import format_datetime
        import tempfile
        import column_editorial
        import job_state
        import note_poster
        import reel_generator

        jst = datetime.timezone(datetime.timedelta(hours=9))
        monday_night = datetime.datetime(2026, 11, 2, 21, 0, tzinfo=jst)  # Monday
        for offset in range(7):
            dt = monday_night + datetime.timedelta(days=offset)
            wd = (dt - datetime.timedelta(hours=7)).weekday()
            for cat in ("carousel", "ladies", "tiktok_gap", "tiktok_midnight"):
                line = sns_editorial.pick_rotated_hashtag_line(
                    "tiktok" if cat.startswith("tiktok") else "instagram",
                    cat,
                    now_jst=dt,
                    area_tag="渋谷",
                )
                if wd != 4:
                    self.assertNotIn("#華金", line, f"Unexpected #華金 on weekday={wd}: {line}")
                if wd in (0, 1, 2, 3):
                    self.assertNotIn("#週末", line, f"Unexpected #週末 on weekday={wd}: {line}")

            for rt in ("ladies_zero_wait", "tiktok_ladies_real", "area_battle", "live_top5"):
                rcap = reel_generator.build_reel_caption(self.sample_stores, reel_type=rt, now_jst=dt)
                if wd in (0, 1, 2, 3):
                    self.assertNotIn("#週末", rcap, f"Unexpected #週末 in Reel caption on weekday={wd} ({rt})")

        # 25h deferred note (< 28h) must still be promoted, whereas 30h legacy note (>= 28h) is marked seen and skipped
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        deferred_25h_pub = format_datetime(now_utc - datetime.timedelta(hours=25))
        stale_30h_pub = format_datetime(now_utc - datetime.timedelta(hours=30))
        with tempfile.TemporaryDirectory() as tmpdir:
            state_file = os.path.join(tmpdir, "notes_28h.json")
            job_state.mark_done("note:https://note.com/aiseki_navi/n/n_init_28h")
            articles = [
                {
                    "title": "前日午後公開でクールダウン待機した25時間前の記事",
                    "link": "https://note.com/aiseki_navi/n/n_25h_deferred",
                    "pub_date": deferred_25h_pub,
                    "guid": "n_25h_deferred",
                },
                {
                    "title": "30時間前の旧記事",
                    "link": "https://note.com/aiseki_navi/n/n_30h_legacy",
                    "pub_date": stale_30h_pub,
                    "guid": "n_30h_legacy",
                },
            ]
            with mock.patch.object(note_poster, "STATE_FILE", state_file), \
                 mock.patch("note_poster.fetch_latest_note_articles", return_value=articles), \
                 mock.patch("x_poster.post_to_x", return_value={"status": "success"}) as post_mock:
                res = note_poster.check_and_post_new_note()
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["article"]["guid"], "n_25h_deferred")
            self.assertEqual(post_mock.call_count, 1)
            self.assertTrue(job_state.is_done("note:https://note.com/aiseki_navi/n/n_30h_legacy"))

        with open(column_editorial.__file__, encoding="utf-8") as f_col:
            col_src = f_col.read()
        self.assertNotIn("53店舗", col_src)
        self.assertNotIn("51店舗", col_src)

        root_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        with open(os.path.join(root_dir, "templates", "staff_pdca.html"), encoding="utf-8") as f_pdca:
            pdca_src = f_pdca.read()
        self.assertNotIn("Claude Sonnet", pdca_src)
        self.assertIn("Claude Opus", pdca_src)


if __name__ == "__main__":
    unittest.main()
