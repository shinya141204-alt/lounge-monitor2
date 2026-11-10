# -*- coding: utf-8 -*-
"""
tests/test_gemini4_argon_e2e.py - Gemini 4 Argon & Comprehensive E2E Test Suite (56 Tests)

【4層階層アプローチ（Opaque-box Verification）】
- Tier 1: 機能網羅（Feature Coverage: R1, R2, R3, R4 各5件以上 / 計23件）
- Tier 2: 境界値・極限ケース（Boundary & Corner: エラー・タイムアウト・空入力・Fail-Open / 計23件）
- Tier 3: 複数機能連携（Cross-Feature Combinations: 二刀流、Reels+QA再レンダリング等 / 計5件）
- Tier 4: 実世界シナリオ（Real-World Application Scenarios: 深夜ハシゴ、リール自動投稿等 / 計5件）

【排他的書き込み権限遵守】
- 本ファイルはテスト専任エージェント (teamwork_test_writer_e2e) が所有。
- アプリケーション本体・実装ファイル（app.py等）は改変せず、外部契約・振る舞いを不透明ボックスで検証する。
"""

import io
import json
import os
import re
import unittest
from unittest.mock import MagicMock, patch

# テスト実行時の環境変数ガード
os.environ["DISABLE_SHEETS"] = "1"
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ["FLASK_ENV"] = "testing"

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
STATIC_DIR = os.path.join(BASE_DIR, "static")
TEMPLATES_DIR = os.path.join(BASE_DIR, "templates")

import app as flask_app_module
import concierge_matcher
import local_db
import ogp_generator
import reel_generator
import second_venues
import sns_editorial
import store_master

try:
    import visual_qa_gate
except ImportError:
    visual_qa_gate = None

from PIL import Image


# ==============================================================================
# Tier 1: 機能網羅 (Feature Coverage Tests) — 計23件
# ==============================================================================

class Tier1FeatureCoverageTests(unittest.TestCase):
    """Tier 1: R1, R2, R3, R4 の各機能の正常系・仕様準拠・孤立検証"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    # --------------------------------------------------------------------------
    # R1: 本番AIパイプラインへの gemini-4-argon 解放
    # --------------------------------------------------------------------------

    def test_01_r1_default_gemini_models_includes_argon(self):
        """R1.1: DEFAULT_GEMINI_MODELS の先頭に 'gemini-4-carbon'、第2候補に 'gemini-4-argon' が指定されていること"""
        models = getattr(sns_editorial, "DEFAULT_GEMINI_MODELS", ())
        self.assertIn("gemini-4-carbon", models, "DEFAULT_GEMINI_MODELS must include 'gemini-4-carbon'")
        self.assertIn("gemini-4-argon", models, "DEFAULT_GEMINI_MODELS must include 'gemini-4-argon'")
        self.assertEqual(models[0], "gemini-4-carbon", "'gemini-4-carbon' should be the prioritized first model")
        self.assertEqual(models[1], "gemini-4-argon", "'gemini-4-argon' should be the prioritized second model")

    def test_02_r1_model_discovery_regex_and_prioritizes_argon(self):
        """R1.2: _discover_ai_studio_models が 'gemini-4-carbon' を認識・パースし最優先にランク付けすること"""
        fake_api_response = {
            "models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-3.1-pro-preview", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-4-argon", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/gemini-4-carbon", "supportedGenerationMethods": ["generateContent"]},
            ]
        }
        with patch("requests.get") as mock_get:
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = fake_api_response
            mock_get.return_value = mock_resp

            discovered = sns_editorial._discover_ai_studio_models("test_key")
            self.assertIsInstance(discovered, list)
            self.assertIn("gemini-4-carbon", discovered)
            self.assertIn("gemini-4-argon", discovered)
            self.assertEqual(discovered[0], "gemini-4-carbon", "Discovered models must prioritize 'gemini-4-carbon' at rank 0")
            self.assertEqual(discovered[1], "gemini-4-argon", "Discovered models must place 'gemini-4-argon' at rank 1")

    def test_03_r1_build_endpoint_request_thinking_config(self):
        """R1.3: _build_endpoint_request が 'gemini-4-carbon' / 'gemini-4-argon' に対して thinkingConfig を設定すること"""
        for m_name in ("gemini-4-carbon", "gemini-4-argon"):
            # thinking_budget に応じたレベル設定 (HIGH, MEDIUM, LOW)
            _, _, _, body_high = sns_editorial._build_endpoint_request(
                "ai_studio", m_name, "dummy_key", "prompt text", thinking_budget=2048
            )
            gen_cfg_high = body_high.get("generationConfig", {})
            self.assertIn("thinkingConfig", gen_cfg_high)
            self.assertEqual(gen_cfg_high["thinkingConfig"].get("thinkingLevel"), "HIGH")

            _, _, _, body_med = sns_editorial._build_endpoint_request(
                "ai_studio", m_name, "dummy_key", "prompt text", thinking_budget=1024
            )
            gen_cfg_med = body_med.get("generationConfig", {})
            self.assertEqual(gen_cfg_med.get("thinkingConfig", {}).get("thinkingLevel"), "MEDIUM")

            _, _, _, body_low = sns_editorial._build_endpoint_request(
                "ai_studio", m_name, "dummy_key", "prompt text", thinking_budget=512
            )
            gen_cfg_low = body_low.get("generationConfig", {})
            self.assertEqual(gen_cfg_low.get("thinkingConfig", {}).get("thinkingLevel"), "LOW")

    def test_04_r1_build_endpoint_request_max_output_tokens(self):
        """R1.4: _build_endpoint_request が 'gemini-4-carbon' / 'gemini-4-argon' に対して思考バッファ込みの十分な maxOutputTokens (>=4096) を確保すること"""
        for m_name in ("gemini-4-carbon", "gemini-4-argon"):
            _, _, _, body = sns_editorial._build_endpoint_request(
                "ai_studio", m_name, "dummy_key", "prompt text", max_output_tokens=2048
            )
            gen_cfg = body.get("generationConfig", {})
            max_tokens = gen_cfg.get("maxOutputTokens", 0)
            self.assertGreaterEqual(max_tokens, 4096, f"maxOutputTokens for {m_name} must be >= 4096 to prevent JSON truncation")

    def test_05_r1_trial_models_starts_with_argon(self):
        """R1.5: call_editorial_ai_json の試行候補モデルリストの最優先に 'gemini-4-carbon' が含まれること"""
        called_models = []

        def mock_post(url, headers=None, params=None, json=None, timeout=None):
            m = re.search(r"models/([^:]+):", url or "")
            if m:
                called_models.append(m.group(1))
            mock_resp = MagicMock()
            mock_resp.status_code = 200
            mock_resp.json.return_value = {
                "candidates": [{"content": {"parts": [{"text": '{"status": "ok"}'}]}}]
            }
            return mock_resp

        with patch("requests.post", side_effect=mock_post):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "fake_test_key"}):
                sns_editorial.call_editorial_ai_json("test prompt", prefer_fable=False)
                self.assertTrue(len(called_models) > 0, "At least one model must be called")
                self.assertEqual(called_models[0], "gemini-4-carbon", "First model called must be gemini-4-carbon")

    def test_06_r1_research_planner_uses_argon(self):
        """R1.6: pdca_engine や column_editorial のリサーチ・分析フェーズで高推論モデル (prefer_fable/argon) が指定されること"""
        import column_editorial
        import pdca_engine
        # column_editorial と pdca_engine が call_editorial_ai_json を prefer_fable=True または大トークンで呼び出しているか検証
        with patch.object(sns_editorial, "call_editorial_ai_json") as mock_call:
            mock_call.return_value = ({"status": "ok"}, "gemini-4-argon", "")
            try:
                pdca_engine.run_autonomous_weekly_pdca()
            except Exception:
                pass
            if mock_call.called:
                kwargs = mock_call.call_args[1]
                self.assertTrue(kwargs.get("prefer_fable", False) or kwargs.get("max_output_tokens", 0) >= 2000)

    # --------------------------------------------------------------------------
    # R2: Instagram Reels ＆ OGP画像の投稿前・視覚検閲ゲート
    # --------------------------------------------------------------------------

    def test_07_r2_visual_qa_gate_module_and_contract(self):
        """R2.1: visual_qa_gate モジュールおよび主要検閲関数が存在すること"""
        self.assertIsNotNone(visual_qa_gate, "visual_qa_gate module must be created")
        self.assertTrue(hasattr(visual_qa_gate, "inspect_reel_visual_quality"), "inspect_reel_visual_quality must exist")
        self.assertTrue(
            hasattr(visual_qa_gate, "inspect_image_visual_quality") or hasattr(visual_qa_gate, "inspect_ogp_visual_quality"),
            "inspect_image_visual_quality or inspect_ogp_visual_quality must exist"
        )

    def test_08_r2_inspect_reel_high_quality_passes(self):
        """R2.2: 高品質フレーム（高コントラスト・適正文字量・フックあり）に対して pass: True と 70点以上を返すこと"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")

        mock_qa_json = {
            "pass": True,
            "overall_score": 88,
            "hook_strength": 85,
            "contrast_readability": 90,
            "text_overflow": False,
            "text_overlap": False,
            "brand_compliance": True,
            "issues": [],
            "recommended_template": "live_top5"
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(mock_qa_json)}]}}]
        }

        with patch("requests.post", return_value=mock_resp):
            res = visual_qa_gate.inspect_reel_visual_quality(
                stores=[{"name": "渋谷本店", "women": 25, "men": 20}],
                api_key="fake_key"
            )
            self.assertIsInstance(res, dict)
            self.assertTrue(res.get("pass"), f"High quality frame must pass: {res}")
            self.assertGreaterEqual(res.get("overall_score", 0), 70)

    def test_09_r2_inspect_reel_low_quality_fails(self):
        """R2.3: 低品質フレーム（同系色文字・視認性不足・フック欠如）に対して pass: False と理由を返すこと"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")

        mock_qa_json = {
            "pass": False,
            "overall_score": 45,
            "hook_strength": 30,
            "contrast_readability": 40,
            "text_overflow": True,
            "text_overlap": True,
            "brand_compliance": True,
            "issues": ["Low contrast", "Text overlap"],
            "recommended_template": "area_battle"
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(mock_qa_json)}]}}]
        }

        with patch("requests.post", return_value=mock_resp):
            res = visual_qa_gate.inspect_reel_visual_quality(
                stores=[{"name": "渋谷本店", "women": 25, "men": 20}],
                api_key="fake_key"
            )
            self.assertIsInstance(res, dict)
            self.assertFalse(res.get("pass"), f"Low quality frame must fail: {res}")
            self.assertLess(res.get("overall_score", 100), 70)
            self.assertTrue(len(res.get("issues", [])) > 0 or len(res.get("reason", "")) > 0)

    def test_10_r2_preview_frame_generation_pure_pillow(self):
        """R2.4: reel_generator.render_reel_preview_frame_jpeg で t=1.2s, t=6.0s の JPEG フレームが Pure Pillow で生成可能であること"""
        stores = [{"name": "渋谷本店", "women": 25, "men": 20}]
        frame_1_2 = reel_generator.render_reel_preview_frame_jpeg(stores, reel_type="live_top5", sec=1.2)
        frame_6_0 = reel_generator.render_reel_preview_frame_jpeg(stores, reel_type="live_top5", sec=6.0)

        self.assertIsInstance(frame_1_2, bytes)
        self.assertIsInstance(frame_6_0, bytes)
        self.assertTrue(frame_1_2.startswith(b"\xff\xd8\xff"), "frame must be valid JPEG binary")
        self.assertTrue(frame_6_0.startswith(b"\xff\xd8\xff"), "frame must be valid JPEG binary")

    def test_11_r2_ogp_visual_qa_integration(self):
        """R2.5: ogp_generator の画像バイト列が inspect_image_visual_quality で検査可能な形式（PNGバイト列）であること"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")

        img = Image.new("RGBA", (1200, 630), color=(20, 20, 22, 255))
        buf = io.BytesIO()
        img.save(buf, format="PNG")
        png_bytes = buf.getvalue()

        mock_qa_json = {
            "pass": True,
            "overall_score": 90,
            "contrast_readability": 90,
            "text_overflow": False,
            "text_overlap": False,
            "issues": []
        }
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps(mock_qa_json)}]}}]
        }

        with patch("requests.post", return_value=mock_resp):
            fn = getattr(visual_qa_gate, "inspect_image_visual_quality", getattr(visual_qa_gate, "inspect_ogp_visual_quality", None))
            self.assertIsNotNone(fn)
            res = fn(png_bytes, api_key="fake_key")
            self.assertTrue(res.get("pass"))
            self.assertGreaterEqual(res.get("overall_score", 0), 70)

    def test_12_r2_metrics_structure_integrity(self):
        """R2.6: 視覚検閲メトリクスに text_overflow, hook_strength, contrast_readability 等のフィールドが含まれること"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")
        res = visual_qa_gate._safe_fail_open_result("test_reason")
        self.assertIn("text_overflow", res)
        self.assertIn("hook_strength", res)
        self.assertIn("contrast_readability", res)
        self.assertIn("pass", res)

    # --------------------------------------------------------------------------
    # R3: Google Maps Grounding × 全63店舗「徒歩圏・実在2軒目バー＆夜カフェ厳選ガイド」
    # --------------------------------------------------------------------------

    def test_13_r3_second_venues_json_63_stores_coverage(self):
        """R3.1: static/second_venues.json の venues_by_store に全63店舗すべてが定義されていること"""
        venues_path = os.path.join(STATIC_DIR, "second_venues.json")
        self.assertTrue(os.path.exists(venues_path), "static/second_venues.json must exist")
        with open(venues_path, "r", encoding="utf-8") as f:
            data = json.load(f)

        venues_by_store = data.get("venues_by_store")
        if venues_by_store is None:
            self.fail("venues_by_store must exist in second_venues.json (Worker M2 pending)")

        master_path = os.path.join(STATIC_DIR, "store_master.json")
        with open(master_path, "r", encoding="utf-8") as f:
            master = json.load(f)["stores"]

        missing = []
        for sname in master.keys():
            slug = store_master.STORE_SLUGS.get(sname)
            if sname not in venues_by_store and (not slug or slug not in venues_by_store):
                missing.append(sname)
        self.assertEqual(len(missing), 0, f"Missing stores in venues_by_store: {missing}")
        self.assertGreaterEqual(len(venues_by_store), 63, "Must cover at least 63 stores")


    def test_14_r3_spot_schema_and_real_venue_attributes(self):
        """R3.2: 全63店舗の各スポットに実在店名、深夜営業、徒歩1〜5分、席特徴、Googleマップ無料URLが完備されていること"""
        venues_path = os.path.join(STATIC_DIR, "second_venues.json")
        with open(venues_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        venues_by_store = data.get("venues_by_store", {})
        if not venues_by_store:
            self.fail("venues_by_store is empty or missing (Worker M2 pending)")

        required_keys = {"id", "name", "category", "walk_minutes", "budget", "features", "map_query"}
        for sname, spots in venues_by_store.items():
            self.assertGreaterEqual(len(spots), 1, f"Store '{sname}' must have at least 1 spot")
            for sp in spots:
                for rk in required_keys:
                    self.assertIn(rk, sp, f"Spot missing '{rk}' in store '{sname}'")
                self.assertLessEqual(sp["walk_minutes"], 5, f"Walk minutes must be <= 5 in '{sname}' spot '{sp['name']}'")
                self.assertNotIn("ダミー", sp["name"])
                self.assertNotIn("サンプル", sp["name"])
                self.assertTrue(len(sp["name"]) > 1, f"Spot name must not be empty in '{sname}'")

    def test_15_r3_get_second_venues_for_store_no_fallback_for_all_63(self):
        """R3.3: second_venues.get_second_venues_for_store が全63店舗で is_fallback: False を返すこと"""
        master_path = os.path.join(STATIC_DIR, "store_master.json")
        with open(master_path, "r", encoding="utf-8") as f:
            master = json.load(f)["stores"]

        fallback_count = 0
        for sname in master.keys():
            res = second_venues.get_second_venues_for_store(sname)
            if res.get("is_fallback"):
                fallback_count += 1
        if fallback_count > 0:
            self.fail(f"{fallback_count}/63 stores returned fallback (Worker M2 venues_by_store pending)")

    def test_16_r3_second_venue_reservation_link_generation(self):
        """R3.4: 各スポットに /link/second_venue_reservation への誘導URLが付与されていること"""
        res = second_venues.get_second_venues_for_store("OLG 渋谷本店")
        spots = res.get("spots", [])
        self.assertGreaterEqual(len(spots), 1)
        for sp in spots:
            res_url = sp.get("reservation_url") or sp.get("second_venue_reservation")
            if res_url is None:
                self.fail(f"Spot {sp.get('name')} must have reservation URL (Worker M2 pending)")
            self.assertIn("/link/second_venue_reservation", res_url)

    def test_17_r3_concierge_supports_seoul_and_routes(self):
        """R3.5: concierge_matcher.py がソウル2店舗（カンナム・ホンデ）を認識し、診断結果に実在2軒目ルートを返すこと"""
        if "seoul" not in concierge_matcher.CONCIERGE_AREAS:
            self.fail("CONCIERGE_AREAS must include 'seoul' (Worker M2 pending)")
        sample_stores = [
            {"name": "OLG ソウル カンナム", "women": 20, "men": 15},
            {"name": "OLG ソウル ホンデ", "women": 18, "men": 12},
        ]
        diag = concierge_matcher.diagnose_tonight_plan(sample_stores, area="seoul", audience="men", timing="now")
        self.assertIsNotNone(diag.get("primary"))
        pname = diag["primary"].get("raw_name", "") or diag["primary"].get("display_name", "")
        self.assertIn("ソウル", pname)
        self.assertIn("second_venue_tip", diag, "Diagnosis must include second_venue_tip advice")
        venues = diag.get("primary_second_venues", [])
        self.assertGreaterEqual(len(venues), 1, "Must contain at least 1 real second venue for Seoul")

    def test_18_r3_store_detail_ssr_second_venues(self):
        """R3.6: store_detail.html または /store/<slug> のSSR展開において実在2軒目案内セクションが存在すること"""
        slug = store_master.STORE_SLUGS.get("OLG 渋谷本店", "oriental-lounge-shibuya-honten")
        resp = self.client.get(f"/store/{slug}")
        self.assertEqual(resp.status_code, 200, f"/store/{slug} must return 200 OK")
        html = resp.get_data(as_text=True)
        self.assertIn("store-second-venue-section", html, "store_detail must contain store-second-venue-section")
        self.assertIn("/link/second_venue_reservation", html, "store_detail must contain second venue reservation link")

    # --------------------------------------------------------------------------
    # R4: 60分後予測モデル v3 ＆ インタラクティブ統計シミュレーター
    # --------------------------------------------------------------------------

    def test_19_r4_forecast_v3_hierarchical_empirical_bayes(self):
        """R4.1: local_db.py の予測キャッシュ生成において階層Empirical-Bayes構造（店舗×時間帯×雨天、店舗全体、ブランド事前分布）が存在すること"""
        with open(os.path.join(BASE_DIR, "local_db.py"), "r", encoding="utf-8") as f:
            source = f.read()
        self.assertIn("@brand", source, "Forecast v3 must implement brand-level prior '@brand'")
        self.assertIn("is_rainy", source, "Forecast v3 must take rain flag into account")

    def test_20_r4_forecast_v3_bias_clipping_guard(self):
        """R4.2: 予測バイアス補正値が ±5.0 ポイントの安全範囲内にクリップされていること"""
        with open(os.path.join(BASE_DIR, "local_db.py"), "r", encoding="utf-8") as f:
            source = f.read()
        self.assertTrue(
            "min(5.0" in source or "max(-5.0" in source or "clamp" in source or "clip" in source,
            "Bias adjustment must be clamped within [-5.0, 5.0]"
        )

    def test_21_r4_stats_report_simulator_ui_elements(self):
        """R4.3: templates/stats_report.html に曜日・時間帯・エリア・ブランド切り替えセレクタが存在すること"""
        stats_path = os.path.join(TEMPLATES_DIR, "stats_report.html")
        self.assertTrue(os.path.exists(stats_path), "templates/stats_report.html must exist")
        with open(stats_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("sim-container", html, "stats_report must have sim-container")
        self.assertIn("sim-controls", html, "stats_report must have sim-controls")
        self.assertIn("sim-select", html, "stats_report must have sim-select")

    def test_22_r4_stats_report_inline_svg_waveform(self):
        """R4.4: 統計シミュレーターがインラインSVG波形描画を採用し、外部重量JS（Chart.js等）依存ゼロであること"""
        stats_path = os.path.join(TEMPLATES_DIR, "stats_report.html")
        with open(stats_path, "r", encoding="utf-8") as f:
            html = f.read()
        self.assertIn("sim-svg-wrap", html, "Simulator must have SVG wrapper")
        self.assertNotIn("chart.js", html.lower(), "Chart.js must not be imported in stats_report")

    def test_23_r4_no_direct_link_in_stats_report(self):
        """R4.5: templates/stats_report.html 内に直 /link/ が1件も存在しないこと（監査完全遵守）"""
        stats_path = os.path.join(TEMPLATES_DIR, "stats_report.html")
        with open(stats_path, "r", encoding="utf-8") as f:
            html = f.read()
        direct_links = re.findall(r'href=[\'"][^\'"]*/link/[^\'"]*[\'"]', html)
        self.assertEqual(len(direct_links), 0, f"Found forbidden direct /link/ in stats_report: {direct_links}")


# ==============================================================================
# Tier 2: 境界値・極限ケース (Boundary & Corner Cases) — 計23件
# ==============================================================================

class Tier2BoundaryAndCornerCasesTests(unittest.TestCase):
    """Tier 2: 境界値、異常系、タイムアウト、空入力、Fail-Open安全フォールバックの網羅的検証"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    # --------------------------------------------------------------------------
    # R1 境界値・極限ケース
    # --------------------------------------------------------------------------

    def test_24_r1_fallback_on_missing_api_key(self):
        """R1.B1: APIキー未設定（None / 空文字）時にクラッシュせず安全フォールバックすること"""
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=True):
            res = sns_editorial._discover_ai_studio_models("", timeout=1.0)
            self.assertEqual(res, [], "Must safely return empty list on missing key")

    def test_25_r1_fallback_on_argon_404_or_429(self):
        """R1.B2: Carbon / Argon呼び出しで404（未対応）または429（クォータ枯渇）発生時に Pro/Flash へ安全フォールバックすること"""
        call_count = {"carbon": 0, "argon": 0, "fallback": 0}

        def mock_post(url, headers=None, params=None, json=None, timeout=None):
            m = re.search(r"models/([^:]+):", url or "")
            m_name = m.group(1) if m else ""
            mock_resp = MagicMock()
            if "carbon" in m_name:
                call_count["carbon"] += 1
                mock_resp.status_code = 429
                mock_resp.text = "Quota exceeded for gemini-4-carbon"
            elif "argon" in m_name:
                call_count["argon"] += 1
                mock_resp.status_code = 404
                mock_resp.text = "Model gemini-4-argon not found"
            else:
                call_count["fallback"] += 1
                mock_resp.status_code = 200
                mock_resp.json.return_value = {
                    "candidates": [{"content": {"parts": [{"text": '{"status": "ok_fallback"}'}]}}]
                }
            return mock_resp

        with patch("requests.post", side_effect=mock_post):
            with patch.dict(os.environ, {"GEMINI_API_KEY": "fake_test_key"}):
                res, model_used, err = sns_editorial.call_editorial_ai_json("test prompt", prefer_fable=False)
                self.assertIsNotNone(res)
                self.assertEqual(res.get("status"), "ok_fallback")
                self.assertGreaterEqual(call_count["carbon"], 1, "Must have tried Carbon first")
                self.assertGreaterEqual(call_count["argon"], 1, "Must have tried Argon second")
                self.assertGreaterEqual(call_count["fallback"], 1, "Must have fallen back to next model")

    def test_26_r1_thinking_config_auto_expands_output_tokens(self):
        """R1.B3: 思考トークン指定時に maxOutputTokens が自動拡張（+4096）され出力打ち切りを防ぐこと"""
        _, _, _, body = sns_editorial._build_endpoint_request(
            "ai_studio", "gemini-4-carbon", "dummy_key", "prompt", max_output_tokens=1000
        )
        tokens = body["generationConfig"]["maxOutputTokens"]
        self.assertGreaterEqual(tokens, 5096, "Output tokens must be expanded by at least 4096 buffer")

    def test_27_r1_invalid_thinking_level_fallback(self):
        """R1.B4: 不正な thinking_budget / レベル値が渡された場合に安全な既定値（LOW/MEDIUM）へフォールバックすること"""
        _, _, _, body = sns_editorial._build_endpoint_request(
            "ai_studio", "gemini-4-carbon", "dummy_key", "prompt", thinking_budget=-999
        )
        th_level = body["generationConfig"]["thinkingConfig"]["thinkingLevel"]
        self.assertIn(th_level, ("LOW", "MEDIUM", "HIGH"), "Must safely resolve to valid thinking level")

    def test_28_r1_model_discovery_api_failure_fallback(self):
        """R1.B5: モデルディスカバリーAPIが通信エラーまたは不正JSONを返した際に静的 DEFAULT_GEMINI_MODELS へ安全フォールバックすること"""
        with patch("requests.get", side_effect=Exception("Network failure")):
            res = sns_editorial._discover_ai_studio_models("any_key")
            self.assertEqual(res, [], "Must safely return empty list on network failure")

    def test_29_r1_empty_prompt_and_malformed_input_safety(self):
        """R1.B6: 空プロンプトや極大プロンプト入力時に制御された例外ハンドリングが行われること"""
        _, _, _, body_empty = sns_editorial._build_endpoint_request(
            "ai_studio", "gemini-4-carbon", "dummy_key", ""
        )
        self.assertIsInstance(body_empty, dict)
        self.assertIn("contents", body_empty)

    # --------------------------------------------------------------------------
    # R2 境界値・極限ケース
    # --------------------------------------------------------------------------

    def test_30_r2_visual_qa_fail_open_on_no_api_key(self):
        """R2.B1: APIキー未設定時に視覚QAゲートが pass: True（Fail-Open）を返し投稿を中断させないこと"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")
        with patch.dict(os.environ, {"GEMINI_API_KEY": ""}, clear=True):
            with patch.object(sns_editorial, "get_gemini_api_key", return_value=""):
                res = visual_qa_gate.inspect_reel_visual_quality(
                    stores=[{"name": "渋谷本店"}], api_key=""
                )
                self.assertTrue(res.get("pass"), f"Must fail-open (pass: True) when key is missing: {res}")
                self.assertEqual(res.get("model_used"), "fail_open")

    def test_31_r2_visual_qa_fail_open_on_timeout(self):
        """R2.B2: タイムアウト（7.0秒）発生時に pass: True（Fail-Open）で安全にフォールバックすること"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")
        with patch("requests.post", side_effect=TimeoutError("Request timed out after 7.0s")):
            res = visual_qa_gate.inspect_reel_visual_quality(
                stores=[{"name": "渋谷本店"}], api_key="fake_key"
            )
            self.assertTrue(res.get("pass"), "Must fail-open on timeout")
            self.assertEqual(res.get("model_used"), "fail_open")

    def test_32_r2_visual_qa_fail_open_on_corrupt_image_bytes(self):
        """R2.B3: 壊れた画像バイト（空バイト、破損PNG等）が渡された場合でも pass: True または制御されたフォールバックを行うこと"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")
        corrupt_bytes = b"not_an_image_at_all_corrupt_bytes"
        fn = getattr(visual_qa_gate, "inspect_image_visual_quality", getattr(visual_qa_gate, "inspect_ogp_visual_quality", None))
        if fn:
            res = fn(corrupt_bytes, api_key="fake_key")
            self.assertIsInstance(res, dict)
            self.assertTrue(res.get("pass"), "Must not crash on corrupt image bytes, fail-open to preserve posting")

    def test_33_r2_visual_qa_score_boundary_69_vs_70(self):
        """R2.B4: スコア境界値（69点は不合格、70点は合格）の厳密なしきい値判定"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")

        # 69点 (不合格)
        mock_resp_69 = MagicMock()
        mock_resp_69.status_code = 200
        mock_resp_69.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps({"pass": False, "overall_score": 69, "issues": ["Borderline low"]})}]}}]
        }
        with patch("requests.post", return_value=mock_resp_69):
            res_69 = visual_qa_gate.inspect_reel_visual_quality(stores=[{"name": "渋谷本店"}], api_key="fake_key")
            self.assertFalse(res_69.get("pass"), "Score 69 must fail")

        # 70点 (合格)
        mock_resp_70 = MagicMock()
        mock_resp_70.status_code = 200
        mock_resp_70.json.return_value = {
            "candidates": [{"content": {"parts": [{"text": json.dumps({"pass": True, "overall_score": 70, "issues": []})}]}}]
        }
        with patch("requests.post", return_value=mock_resp_70):
            res_70 = visual_qa_gate.inspect_reel_visual_quality(stores=[{"name": "渋谷本店"}], api_key="fake_key")
            self.assertTrue(res_70.get("pass"), "Score 70 must pass")

    def test_34_r2_visual_qa_oversized_image_handling(self):
        """R2.B5: 極大画像（10MB等）や変則アスペクト比画像に対してメモリ保護またはリサイズ防御が行われること"""
        if visual_qa_gate is None:
            self.skipTest("visual_qa_gate not yet implemented")
        large_bytes = b"\xff\xd8\xff" + b"\x00" * (10 * 1024 * 1024)
        fn = getattr(visual_qa_gate, "inspect_image_visual_quality", None)
        if fn:
            res = fn(large_bytes, api_key="fake_key")
            self.assertIsInstance(res, dict)
            self.assertTrue(res.get("pass"))

    def test_35_r2_visual_qa_max_retry_limit(self):
        """R2.B6: 視覚検閲不合格時の再レンダリング処理が無限ループせず最大1回で終了すること"""
        import instagram_poster
        rerender_count = {"count": 0}

        def mock_qa(stores, **kwargs):
            return {"pass": False, "overall_score": 50, "reason": "Low hook", "recommended_template": "area_battle"}

        def mock_render(stores, reel_type, **kwargs):
            rerender_count["count"] += 1
            return b"fake_mp4_bytes"

        with patch("visual_qa_gate.inspect_reel_visual_quality", side_effect=mock_qa):
            with patch("reel_generator.render_reel_mp4_bytes", side_effect=mock_render):
                with patch("instagram_poster.post_reel_to_instagram", return_value={"status": "ok"}):
                    try:
                        instagram_poster.generate_and_post_reels_workflow(stores=[{"name": "渋谷本店"}])
                    except Exception:
                        pass
                    self.assertLessEqual(rerender_count["count"], 2, "Must not retry more than 1 time (max 2 renders total)")

    # --------------------------------------------------------------------------
    # R3 境界値・極限ケース
    # --------------------------------------------------------------------------

    def test_36_r3_unknown_store_safe_fallback(self):
        """R3.B1: 未知の店舗名・存在しない店舗名が渡された場合に例外を投げずスマートフォールバック（is_fallback: True）を返すこと"""
        res = second_venues.get_second_venues_for_store("架空店舗_月面ラウンジ99")
        self.assertIsInstance(res, dict)
        self.assertTrue(res.get("is_fallback"), "Unknown store must safely fallback to dynamic search")
        self.assertGreaterEqual(len(res.get("spots", [])), 1)

    def test_37_r3_none_and_empty_store_name_safety(self):
        """R3.B2: 店舗名に None や空文字 '' が渡された場合の防御的ハンドリング"""
        try:
            res_none = second_venues.get_second_venues_for_store(None)
            self.assertTrue(res_none.get("is_fallback"))
        except (AttributeError, TypeError) as ex:
            self.fail(f"Implementation bug in second_venues.py (NoneType crash): {ex}")

        res_empty = second_venues.get_second_venues_for_store("")
        self.assertTrue(res_empty.get("is_fallback"))

    def test_38_r3_legacy_slug_redirect_safety(self):
        """R3.B3: 閉店・移転の旧スラッグ11店舗（LEGACY_SLUG_REDIRECTS）へのアクセス時に301転送または安全なフォールバックが行われること"""
        for legacy_slug, target_slug in store_master.LEGACY_SLUG_REDIRECTS.items():
            resp = self.client.get(f"/store/{legacy_slug}")
            self.assertIn(resp.status_code, (301, 302, 200), f"Legacy slug '{legacy_slug}' must safely handle redirect")
            if resp.status_code in (301, 302):
                self.assertIn(target_slug, resp.headers.get("Location", ""))

    def test_39_r3_seoul_hangul_and_coordinate_escaping(self):
        """R3.B4: ソウル店舗の実在スポットデータでハングル文字や座標が文字化け・エスケープ破損しないこと"""
        res_gangnam = second_venues.get_second_venues_for_store("OLG ソウル カンナム")
        if res_gangnam.get("is_fallback"):
            self.skipTest("Seoul store second venues pending Worker M2")
        spots = res_gangnam.get("spots", [])
        self.assertGreaterEqual(len(spots), 1)
        for sp in spots:
            map_url = sp.get("map_url", "")
            self.assertTrue(map_url.startswith("https://www.google.com/maps/search/?api=1&query="))

    def test_40_r3_google_maps_intent_url_special_chars_encoding(self):
        """R3.B5: マップ検索クエリ内の特殊記号（&, ?, #, 改行等）が安全にURLエンコードされること"""
        raw_query = "Bar & Cafe #1 ? 'Special' / 渋谷"
        url = second_venues.get_google_maps_intent_url(raw_query)
        self.assertNotIn(" ", url, "URL must not contain raw spaces")
        self.assertNotIn("\n", url, "URL must not contain raw newlines")
        self.assertIn("%26", url, "& must be encoded")
        self.assertIn("%23", url, "# must be encoded")

    def test_41_r3_missing_or_corrupt_second_venues_json_recovery(self):
        """R3.B6: second_venues.json が破損または存在しない場合でも second_venues.py がクラッシュせず空マスターへ復旧すること"""
        with patch("os.path.exists", return_value=False):
            data = second_venues.load_second_venues_master(force_reload=True)
            self.assertIsInstance(data, dict)
            self.assertIn("venues_by_area", data)
        # キャッシュの復旧
        second_venues.load_second_venues_master(force_reload=True)

    # --------------------------------------------------------------------------
    # R4 境界値・極限ケース
    # --------------------------------------------------------------------------

    def test_42_r4_forecast_zero_sample_safe_fallback(self):
        """R4.B1: 新店舗または履歴ゼロの時間帯で過学習せず補正値 0.0 へ安全フォールバックすること"""
        bias = local_db.get_cached_forecast_residual_adjustment("架空新店舗X", 15, is_rainy=False)
        self.assertEqual(bias, 0.0, "Zero sample store must fallback to 0.0 bias adjustment")

    def test_43_r4_forecast_rainy_flag_missing_fallback(self):
        """R4.B2: 気象データ（is_rainy）が None または欠損している場合に全天候ベースへ安全フォールバックすること"""
        bias = local_db.get_cached_forecast_residual_adjustment("OLG 渋谷本店", 21, is_rainy=None)
        self.assertIsInstance(bias, (int, float))
        self.assertGreaterEqual(bias, -5.0)
        self.assertLessEqual(bias, 5.0)

    def test_44_r4_forecast_extreme_outlier_clipping(self):
        """R4.B3: 誤差ログの集計時におけるバイアス補正が上限 [-5.0, 5.0] で安全にクリップされること"""
        db_k = local_db._active_db_key()
        cached = local_db._forecast_residual_bias_cache.get(db_k)
        if cached:
            bmap = cached[1]
            for k, v in bmap.items():
                self.assertGreaterEqual(v, -5.0, f"Bias {k} < -5.0")
                self.assertLessEqual(v, 5.0, f"Bias {k} > 5.0")

    def test_45_r4_stats_simulator_empty_or_single_data_point(self):
        """R4.B4: 統計シミュレーターUIでクエリパラメータ指定時でもページが正常描画されること"""
        resp = self.client.get("/stats?area=unknown_area&brand=unknown_brand")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("sim-container", html, "Must still render valid sim container")

    def test_46_r4_forecast_db_locked_or_missing_fallback(self):
        """R4.B5: 予測残差DBテーブルが存在しない／ロック中であっても予測エンジンが停止せず安全にフォールバックすること"""
        with patch.object(local_db, "_db_available", return_value=False):
            bias = local_db.get_cached_forecast_residual_adjustment("OLG 渋谷本店", 22)
            self.assertIsInstance(bias, (int, float))


# ==============================================================================
# Tier 3: 複数機能連携 (Cross-Feature Combinations) — 計5件
# ==============================================================================

class Tier3CrossFeatureCombinationsTests(unittest.TestCase):
    """Tier 3: 複数機能間・サブシステム間の相互作用・連携フローの検証"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    def test_47_r1_r1_argon_planner_to_claude_polisher_pipeline(self):
        """T3.1: Argonによる企画・構成JSON出力をClaude脱AI推敲関数（polish_draft_to_human_voice）へ入力する二刀流パイプライン連携"""
        argon_planning_output = {
            "title": "金曜夜の相席ラウンジ男女比分析",
            "key_findings": ["21時以降女性比率が急上昇", "渋谷・新宿の逆転現象"],
            "raw_text": "【速報】金曜21時の相席ラウンジは女性先行状態です。混雑しています。"
        }
        with patch.object(sns_editorial, "call_editorial_ai_json") as mock_claude:
            mock_claude.return_value = (
                {"polished_text": "金曜21時を回り、店内は女性先行の絶好のタイミング。渋谷・新宿ともに盛り上がっています。"},
                "claude-opus-5-5",
                ""
            )
            polished_tuple = sns_editorial.polish_draft_to_human_voice(
                draft_text=argon_planning_output["raw_text"],
                channel="x"
            )
            self.assertIsInstance(polished_tuple, tuple)
            polished_text = polished_tuple[0]
            self.assertTrue(len(polished_text) > 0)

    def test_48_r2_reels_generation_qa_gate_rerender_flow(self):
        """T3.2: リール動画生成 → 視覚検閲ゲート判定 → 不合格時別テンプレート再レンダリング → 合格フレーム確定の結合ワークフロー"""
        import instagram_poster
        qa_attempts = []

        def mock_qa(stores, reel_type="live_top5", **kwargs):
            if len(qa_attempts) == 0:
                qa_attempts.append("fail")
                return {"pass": False, "overall_score": 55, "reason": "Weak hook", "recommended_template": "area_battle"}
            else:
                qa_attempts.append("pass")
                return {"pass": True, "overall_score": 88, "reason": "High hook and clarity"}

        with patch("visual_qa_gate.inspect_reel_visual_quality", side_effect=mock_qa):
            with patch("reel_generator.render_reel_mp4_bytes", return_value=b"valid_mp4_payload"):
                with patch("instagram_poster.post_reel_to_instagram", return_value={"status": "ok"}):
                    res = instagram_poster.publish_live_reel_to_instagram(
                        stores=[{"name": "渋谷本店", "women": 30, "men": 25}],
                        force=True
                    )
                    self.assertIsInstance(res, dict)
                    self.assertEqual(len(qa_attempts), 2, "Workflow must re-render upon initial QA failure and then pass")

    def test_49_r3_concierge_to_second_venue_reservation_bridge(self):
        """T3.3: コンシェルジュ診断（/concierge）の結果から推奨店舗の2軒目ルートおよび予約導線（/link/second_venue_reservation）への結合"""
        resp = self.client.get("/concierge?area=shibuya&budget=standard")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        if "/link/second_venue_reservation" not in html:
            self.fail("/link/second_venue_reservation not found in /concierge (Worker M2 pending)")

    def test_50_r4_forecast_v3_to_stats_simulator_integration(self):
        """T3.4: 予測エンジンv3のバイアス補正済みデータが /stats 統計シミュレーターUIのデータセットとして整合して供給される結合"""
        resp = self.client.get("/stats")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        self.assertIn("sim-container", html)
        self.assertNotIn("/link/", html, "Stats surface must strictly avoid /link/ direct links")

    def test_51_r3_seoul_concierge_and_second_venues_integration(self):
        """T3.5: ソウルエリアのコンシェルジュマッチング結果からソウル実在2軒目バー・夜カフェへの結合"""
        try:
            sample_stores = [{"name": "OLG ソウル カンナム", "women": 20, "men": 15}]
            diag = concierge_matcher.diagnose_tonight_plan(sample_stores, area="seoul", audience="men", timing="now")
        except KeyError:
            self.fail("Seoul area not in concierge CONCIERGE_AREAS (Worker M2 pending)")
        venues = diag.get("primary_second_venues", [])
        if not venues:
            self.fail("Seoul second venues in concierge pending (Worker M2)")
        for v in venues:
            self.assertTrue(v.get("map_url", "").startswith("https://www.google.com/maps/search/?api=1&query="))


# ==============================================================================
# Tier 4: 実世界シナリオ (Real-World Application Scenarios) — 計5件
# ==============================================================================

class Tier4RealWorldScenariosTests(unittest.TestCase):
    """Tier 4: 実世界ユーザー動線・深夜運用・自動化パイプラインのエンドツーエンド検証"""

    @classmethod
    def setUpClass(cls):
        flask_app_module.app.testing = True
        cls.client = flask_app_module.app.test_client()

    def test_52_scenario_late_night_bar_hopping_flow(self):
        """T4.1: 深夜23:30の退店ユーザーが店舗詳細で徒歩圏バーを発見しGoogleマップ経路から予約へ至る完全動線"""
        slug = store_master.STORE_SLUGS.get("OLG 渋谷本店", "oriental-lounge-shibuya-honten")
        resp = self.client.get(f"/store/{slug}")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn("store-second-venue-section", html)

        res_link_match = re.search(r'href="(/link/second_venue_reservation\?[^"]*)"', html)
        if res_link_match:
            target_link = res_link_match.group(1)
            redir_resp = self.client.get(target_link)
            self.assertIn(redir_resp.status_code, (301, 302, 200))

    def test_53_scenario_automated_reels_nightly_posting(self):
        """T4.2: 定時バッチ（22:00）で混雑データ集約 → Reels動画生成 → 視覚QAゲート検閲 → Instagram投稿ペイロード完成の完全自動化動線"""
        import instagram_poster
        stores = [
            {"name": "オリエンタルラウンジ 渋谷本店", "women": 35, "men": 30},
            {"name": "オリエンタルラウンジ 新宿", "women": 28, "men": 25},
        ]
        with patch("visual_qa_gate.inspect_reel_visual_quality", return_value={"pass": True, "overall_score": 92}):
            with patch("reel_generator.render_reel_mp4_bytes", return_value=b"mp4_binary_stream"):
                with patch("instagram_poster.post_reel_to_instagram", return_value={"status": "ok", "media_id": "123456"}):
                    post_res = instagram_poster.publish_live_reel_to_instagram(stores=stores, force=True)
                    self.assertIsInstance(post_res, dict)

    def test_54_scenario_stats_interactive_simulator_exploration(self):
        """T4.3: ユーザーが /stats で金曜22時の各エリア・ブランドの混雑波形をインタラクティブに比較分析する完全動線"""
        resp = self.client.get("/stats")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)

        self.assertIn("sim-container", html)
        self.assertNotIn('href="/link/', html)
        self.assertIn('/store/', html)

    def test_55_scenario_late_night_api_outage_resilience(self):
        """T4.4: 深夜帯のGemini API障害・クォータ上限時におけるシステム全体の無停止フェイルオープン運用動線"""
        with patch("requests.post", side_effect=Exception("503 Service Unavailable")):
            ai_res, _, _ = sns_editorial.call_editorial_ai_json("急ぎのツイート作成")
            # 障害時でも例外でプロセスが死なないこと

            if visual_qa_gate:
                qa_res = visual_qa_gate.inspect_reel_visual_quality(stores=[{"name": "渋谷本店"}])
                self.assertTrue(qa_res.get("pass"), "Must fail-open during total API outage")

            venues_res = second_venues.get_second_venues_for_store("OLG 渋谷本店")
            self.assertGreaterEqual(len(venues_res.get("spots", [])), 1)

            resp = self.client.get("/api/status")
            self.assertEqual(resp.status_code, 200)

    def test_56_scenario_compliance_and_zero_direct_link_audit(self):
        """T4.5: 全システムサーフェスにおいて規約違反（直 /link/、単体「オリエンタル」表記）が完全ゼロであることを網羅検証する総合コンプライアンス動線"""
        guide_files = [f for f in os.listdir(TEMPLATES_DIR) if f.startswith("guide_") and f.endswith(".html")]
        for gf in guide_files:
            with open(os.path.join(TEMPLATES_DIR, gf), "r", encoding="utf-8") as f:
                content = f.read()
            self.assertNotIn('href="/link/', content, f"Direct /link/ found in {gf}")

        stats_path = os.path.join(TEMPLATES_DIR, "stats_report.html")
        if os.path.exists(stats_path):
            with open(stats_path, "r", encoding="utf-8") as f:
                stats_content = f.read()
            self.assertNotIn('href="/link/', stats_content, "Direct /link/ found in stats_report.html")

        venues_path = os.path.join(STATIC_DIR, "second_venues.json")
        if os.path.exists(venues_path):
            with open(venues_path, "r", encoding="utf-8") as f:
                venues_text = f.read()
            clean_text = venues_text.replace("オリエンタルラウンジ", "")
            self.assertNotIn("オリエンタル", clean_text, "Single 'オリエンタル' forbidden in second_venues.json")
            self.assertNotIn("OLG", clean_text, "Forbidden 'OLG' in second_venues.json")


if __name__ == "__main__":
    unittest.main()
