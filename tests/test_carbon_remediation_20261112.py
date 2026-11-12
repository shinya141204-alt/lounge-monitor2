# -*- coding: utf-8 -*-
"""
tests/test_carbon_remediation_20261112.py

Fable × Carbon 全領域監査（P0 / P1 / P2）＋ Carbon Code Review 指摘事項の包括回帰テストスイート:
- P0-1: /link/<item_id> 非JSボット隔離（an_hp=1 Cookie ＆ HMAC csrf_clk インタースティシャル、Referer ?s= 保持）、
        CTR 100% クランプ撤廃＋ctr_anomaly 検知、human_pv トラッキング、/api/track Cookie 発行
- P0-2: コールドブート時（latest_data 空）の /api/vote ＆ /api/votes/<store> 未登録店舗インジェクション遮断、
        DATABASE_URL・LOCAL_DB_PATH・turbo_store の完全テスト隔離
- P1-1: visual_qa_gate の 0点評価・小数文字列保持・確定的合否判定・qa_status 識別・8MBガード、
        sns_editorial の _unhealthy_models クールダウン遵守・prefer_fast 時の Lite 優先順序・gemini-3.5-flash-lite 保護、
        column_editorial の Lite/Haiku 長文コラム汚染ブロック、
        voice_narration の 30分モデルメモ化・初回/フォールバック中 HTTP 429 180秒サーキットブレーカー・二重ルビ防止、
        sns_insights_loop の tiktok_ladies_real 登録＆UCB1 >= 6本閾値
- P1-2: /xix・/yatakoi・/clovers・/alfa の 301 正規化リダイレクト（クエリ保持）、
        カナ別名解決（ジックス・クローバーズ・アルファ）・seo_name_with_kana、store_detail 実測レビュー意図対応
- P2-1: 全 guide_*.html のライトモード直書き白系文字色ゼロ＆color-scheme メタタグ正常出力、
        /stats/weekend-index・/stats/monthly/<ym> の共通フッター＆直 /link/ ゼロ、
        トップ店舗カードの .analysis-btn 可視ラベル＆WCAG 2.5.3 Label in Name 整合
"""

import copy
import datetime
import glob
import io
import json
import os
import re
import sqlite3
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

import affiliate_manager
import analytics
import app as app_module
import column_editorial
import local_db
import pdca_engine
import sns_editorial
import sns_insights_loop
import store_master
import turbo_store
import visual_qa_gate
import voice_narration

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
JST = datetime.timezone(datetime.timedelta(hours=9))


class CarbonRemediation20261112Tests(unittest.TestCase):
    """P0 / P1 / P2 全改修項目および Carbon Code Review 修正の完全隔離単体・統合テスト"""

    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_db_url = os.environ.pop("DATABASE_URL", None)
        self._orig_env_db = os.environ.get("LOCAL_DB_PATH")
        self._orig_db_path = local_db.DB_PATH
        self._test_db_path = os.path.join(self._tmpdir.name, "test_carbon_remediation.db")
        os.environ["LOCAL_DB_PATH"] = self._test_db_path
        local_db.DB_PATH = self._test_db_path
        turbo_store.reset_for_tests()
        local_db.reset_caches_for_tests()
        local_db.init_db()
        analytics.reset_for_tests()
        sns_insights_loop.reset_insights_loop_cache_for_tests()

        app_module.app.config["TESTING"] = True
        app_module.clear_fast_http_caches()
        with app_module.data_lock:
            self._saved_latest = copy.deepcopy(app_module.latest_data)

        self.client = app_module.app.test_client()
        voice_narration.reset_voice_narration_state_for_tests()

    def tearDown(self):
        voice_narration.reset_voice_narration_state_for_tests()
        sns_insights_loop.reset_insights_loop_cache_for_tests()
        analytics.reset_for_tests()
        turbo_store.reset_for_tests()
        app_module.app.config.pop("ENFORCE_AFFILIATE_HUMAN_PROOF", None)
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        app_module.clear_fast_http_caches()

        if self._orig_env_db is None:
            os.environ.pop("LOCAL_DB_PATH", None)
        else:
            os.environ["LOCAL_DB_PATH"] = self._orig_env_db
        if self._orig_db_url is None:
            os.environ.pop("DATABASE_URL", None)
        else:
            os.environ["DATABASE_URL"] = self._orig_db_url
        local_db.DB_PATH = self._orig_db_path
        local_db.reset_caches_for_tests()
        self._tmpdir.cleanup()

    # ------------------------------------------------------------------
    # P0-1: Non-JS Bot Isolation on /link/<id>, Referer ?s=, CTR & human_pv
    # ------------------------------------------------------------------
    def test_p0_1_affiliate_human_proof_interstitial_and_hmac_confirm(self):
        """ENFORCE_AFFILIATE_HUMAN_PROOF 有効時、CookieなしGETは200確認画面（0クリック・Referer ?s= 保持）となり、正規HMAC POSTでのみ303＆1クリック記録されること"""
        app_module.app.config["ENFORCE_AFFILIATE_HUMAN_PROOF"] = True
        human_ua = {
            "User-Agent": "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 Mobile/15E148 Safari/604.1",
            "Referer": "http://localhost/grooming?s=th20_live",
        }

        # 1) Cookieless GET -> 200 interstitial preserving Referer ?s=th20_live, 0 affiliate_clicks
        r1 = self.client.get("/link/orbis?from=/grooming", headers=human_ua)
        self.assertEqual(r1.status_code, 200)
        self.assertEqual(r1.headers.get("X-Robots-Tag"), "noindex, nofollow")
        html1 = r1.get_data(as_text=True)
        self.assertIn("csrf_clk", html1)
        self.assertIn("s=th20_live", html1)

        snap1 = analytics.summary(days=7, allow_blocking=False)
        self.assertEqual(snap1["funnel"]["aff_total"], 0)

        # 2) Forged POST -> 200 re-interstitial, still 0 affiliate_clicks
        r_bad = self.client.post(
            "/link/orbis?from=/grooming&s=th20_live",
            data={"csrf_clk": "0000000000.deadbeef"},
            headers={"User-Agent": human_ua["User-Agent"]},
        )
        self.assertEqual(r_bad.status_code, 200)
        snap_bad = analytics.summary(days=7, allow_blocking=False)
        self.assertEqual(snap_bad["funnel"]["aff_total"], 0)

        # 3) Valid HMAC POST -> 303 redirect + an_hp=1 cookie + 1 affiliate_click + cv:th20_live
        m_tok = re.search(r'name="csrf_clk"\s+value="([^"]+)"', html1)
        self.assertIsNotNone(m_tok)
        r_ok = self.client.post(
            "/link/orbis?from=/grooming&s=th20_live",
            data={"csrf_clk": m_tok.group(1)},
            headers={"User-Agent": human_ua["User-Agent"]},
        )
        self.assertEqual(r_ok.status_code, 303)
        set_cookies = "\n".join(r_ok.headers.getlist("Set-Cookie"))
        self.assertIn("an_hp=1", set_cookies)

        snap_ok = analytics.summary(days=7, allow_blocking=False)
        self.assertEqual(snap_ok["funnel"]["aff_total"], 1)
        inflow_labels = dict(snap_ok.get("all_by_label", {}).get("sns_inflow", {}))
        self.assertEqual(inflow_labels.get("cv:th20_live"), 1)

        # 4) Subsequent GET with an_hp=1 cookie -> immediate 302 + 2nd click
        r_direct = self.client.get("/link/orbis?from=/grooming", headers=human_ua)
        self.assertEqual(r_direct.status_code, 302)
        snap_direct = analytics.summary(days=7, allow_blocking=False)
        self.assertEqual(snap_direct["funnel"]["aff_total"], 2)

    def test_p0_1_ctr_not_clamped_to_100_and_human_pv_tracked(self):
        """インプレッションを上回るクリック発生時にCTRを100.0%へ隠蔽せず実測値と ctr_anomaly=True を返し、human_pv と an_hp Cookie が機能すること"""
        human_ua = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 14_5) AppleWebKit/537.36 Chrome/126.0.0.0 Safari/537.36"}
        tr = self.client.post("/api/track", json={"event": "human_pv", "label": "/grooming"}, headers=human_ua)
        self.assertEqual(tr.status_code, 204)
        self.assertIn("an_hp=1", "\n".join(tr.headers.getlist("Set-Cookie")))

        analytics.track("affiliate_impression", "orbis @ /grooming")
        for _ in range(3):
            analytics.track("affiliate_click", "orbis @ /grooming")

        snap = analytics.summary(days=7, allow_blocking=False)
        self.assertEqual(snap["funnel"]["human_pv_total"], 1)
        self.assertEqual(snap["funnel"]["aff_slot_ctr_pct"], 300.0)
        self.assertTrue(snap["funnel"]["aff_slot_ctr_anomaly"])

        slots = affiliate_manager.get_affiliate_slots_status(snap)
        orbis_slot = next(s for s in slots["slots"] if s["slot_id"] == "mens_skincare_clinic")
        self.assertEqual(orbis_slot["ctr_pct"], 300.0)
        self.assertTrue(orbis_slot["ctr_anomaly"])

    # ------------------------------------------------------------------
    # P0-2: Cold-Boot /api/vote Unknown Store Guard
    # ------------------------------------------------------------------
    def test_p0_2_cold_boot_vote_rejects_unregistered_store_names(self):
        """latest_data['full_data'] が空のコールドブート状態でも、未登録店舗名への /api/vote と /api/votes/<name> を遮断しDB汚染ゼロを保つこと"""
        with app_module.data_lock:
            app_module.latest_data["full_data"] = []

        bad_name = "FUZZ_SQLI_STORE'; DROP TABLE logs;--"
        r_post = self.client.post("/api/vote", json={"store_name": bad_name, "vote_type": "lively"})
        self.assertEqual(r_post.status_code, 400)

        r_get = self.client.get("/api/votes/UNKNOWN_GHOST_STORE_999")
        self.assertIn(r_get.status_code, (400, 404))

        with sqlite3.connect(self._test_db_path) as conn:
            cnt = conn.execute("SELECT COUNT(*) FROM crowd_votes").fetchone()[0]
        self.assertEqual(cnt, 0)

        # Registered store still succeeds on cold boot via store_master canonical resolution
        r_valid = self.client.post("/api/vote", json={"store_name": "オリエンタルラウンジ 新宿", "vote_type": "lively"})
        self.assertEqual(r_valid.status_code, 200)

    # ------------------------------------------------------------------
    # P1-1: Visual QA, SNS Editorial, Column Editorial, Voice & Insights
    # ------------------------------------------------------------------
    def test_p1_1_visual_qa_preserves_zero_score_and_enforces_deterministic_pass(self):
        """visual_qa_gate が 0点・小数文字列を正しく処理し、text_overflow=True 時は pass=False に強制し、qa_status と 8MB 上限を守ること"""
        self.assertEqual(visual_qa_gate._coerce_qa_score(0, 85), 0)
        self.assertEqual(visual_qa_gate._coerce_qa_score("0", 85), 0)
        self.assertEqual(visual_qa_gate._coerce_qa_score("85.6", 70), 86)
        self.assertEqual(visual_qa_gate._coerce_qa_score(None, 85), 85)

        oversize_res = visual_qa_gate.inspect_image_visual_quality(
            b"x" * (visual_qa_gate.MAX_QA_IMAGE_BYTES + 1),
            api_key="dummy-key",
        )
        self.assertTrue(oversize_res["pass"])
        self.assertEqual(oversize_res["qa_status"], "fail_open_unverified")

        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (64, 64), (17, 17, 19)).save(buf, format="JPEG")
        frame_bytes = buf.getvalue()

        fake_http_resp = MagicMock()
        fake_http_resp.status_code = 200
        fake_http_resp.json.return_value = {
            "candidates": [{
                "content": {
                    "parts": [{
                        "text": json.dumps({
                            "pass": True,
                            "overall_score": 0,
                            "hook_strength": "75.4",
                            "contrast_readability": 90,
                            "text_overflow": True,
                            "text_overlap": False,
                            "brand_compliance": True,
                            "issues": ["端切れ"],
                            "recommended_template": "tiktok_gap_shock",
                        })
                    }]
                }
            }]
        }

        with patch.object(visual_qa_gate, "_render_qa_frames", return_value=(frame_bytes, frame_bytes)), \
             patch.object(visual_qa_gate.requests, "post", return_value=fake_http_resp):
            res = visual_qa_gate.inspect_reel_visual_quality(
                [{"name": "オリエンタルラウンジ 新宿", "women": 24, "men": 12}],
                reel_type="tiktok_ladies_real",
                api_key="dummy-key",
            )
        self.assertEqual(res["overall_score"], 0)
        self.assertEqual(res["hook_strength"], 75)
        self.assertFalse(res["pass"])
        self.assertEqual(res["qa_status"], "inspected")

        res_open = visual_qa_gate.inspect_image_visual_quality(frame_bytes, api_key="")
        self.assertTrue(res_open["pass"])
        self.assertEqual(res_open["qa_status"], "fail_open_unverified")

    def test_p1_1_sns_and_column_editorial_cooldown_and_lite_guard(self):
        """sns_editorial が gemini-3.5-flash-lite を不健全登録から保護し、column_editorial が Lite/Haiku 出力を拒否すること"""
        orig_unhealthy = dict(sns_editorial._unhealthy_models)
        try:
            sns_editorial._unhealthy_models.clear()
            sns_editorial._mark_model_unhealthy("gemini-3.5-flash-lite")
            self.assertNotIn("gemini-3.5-flash-lite", sns_editorial._unhealthy_models)

            sns_editorial._mark_model_unhealthy("gemini-4-carbon")
            self.assertIn("gemini-4-carbon", sns_editorial._unhealthy_models)
        finally:
            sns_editorial._unhealthy_models.clear()
            sns_editorial._unhealthy_models.update(orig_unhealthy)

        # column_editorial must reject Lite / Haiku emergency fallback output
        fake_col_payload = {
            "title": "テスト特集タイトル",
            "slug": "test-lite-guard-slug",
            "sections": [{"h2": "見出し", "body": "本文テキスト"}],
        }
        fake_research = {"theme_key": "area_hopping", "store_count": 61, "stores_sample": []}
        fake_plan = {"theme_key": "area_hopping", "suggested_slug": "test-lite-guard-slug"}
        with patch.object(sns_editorial, "has_any_ai_key", return_value=True), \
             patch.object(sns_editorial, "call_editorial_ai_json", return_value=(fake_col_payload, "gemini-3.5-flash-lite", "ai_studio")):
            cand = column_editorial._generate_gemini_column_candidate(
                fake_research,
                fake_plan,
                {},
                "https://aiseki-navi.com",
                datetime.datetime.now(JST),
                force_refresh=True,
            )
        self.assertIsNone(cand)

    def test_p1_1_voice_narration_memo_429_breaker_and_ruby_collapse(self):
        """voice_narration が二重ルビ（英語＋カナ＆同一カナ重複）を防止し、初回・フォールバック中の HTTP 429 時に180秒クールダウンを開き、成功モデルをメモ化すること"""
        norm = voice_narration.to_tts_phonetic_japanese(
            "今夜の相席ラウンジXIX（ジックス）岡山と相席CLOVERS（クローバーズ）広島とYATAKOI（ヤタコイ）梅田が見られます"
        )
        self.assertEqual(norm.count("ジックス"), 1)
        self.assertEqual(norm.count("クローバーズ"), 1)
        self.assertEqual(norm.count("ヤタコイ"), 1)
        self.assertNotIn("XIX", norm)

        # 1) HTTP 429 on initial model opens circuit breaker immediately
        resp_429 = MagicMock(status_code=429, content=b"quota exceeded", text="quota exceeded")
        with patch.object(voice_narration.requests, "post", return_value=resp_429) as mock_post:
            r1 = voice_narration.synthesize_narration_audio(
                "テスト原稿です。",
                api_key="sk_test_123456",
                voice_id="voice_abc1234567",
                model_id="eleven_v4",
                force_refresh=True,
            )
            self.assertFalse(r1["ok"])
            self.assertEqual(mock_post.call_count, 1)

            r2 = voice_narration.synthesize_narration_audio(
                "別の原稿です。",
                api_key="sk_test_123456",
                voice_id="voice_abc1234567",
                model_id="eleven_v4",
                force_refresh=False,
            )
            self.assertFalse(r2["ok"])
            self.assertTrue(r2.get("cooldown_active"))
            self.assertEqual(mock_post.call_count, 1)

        # 2) HTTP 429 during Fallback 2 (after 422 on eleven_v4) ALSO stops immediately and opens breaker
        voice_narration.reset_voice_narration_state_for_tests()
        resp_422 = MagicMock(status_code=422, content=b"invalid model", text="invalid model")
        with patch.object(voice_narration.requests, "post", side_effect=[resp_422, resp_422, resp_429]) as mock_fb429:
            rf1 = voice_narration.synthesize_narration_audio(
                "フォールバック中429テストです。",
                api_key="sk_test_123456",
                voice_id="voice_fb429",
                model_id="eleven_v4",
                force_refresh=True,
            )
            self.assertFalse(rf1["ok"])
            self.assertEqual(mock_fb429.call_count, 3)

            rf2 = voice_narration.synthesize_narration_audio(
                "直後の呼び出しです。",
                api_key="sk_test_123456",
                voice_id="voice_fb429",
                model_id="eleven_v4",
                force_refresh=False,
            )
            self.assertFalse(rf2["ok"])
            self.assertTrue(rf2.get("cooldown_active"))
            self.assertEqual(mock_fb429.call_count, 3)

        # 3) Fallback model memoization on 422 -> eleven_v3 200 OK
        voice_narration.reset_voice_narration_state_for_tests()
        resp_200 = MagicMock(status_code=200, content=b"ID3" + (b"\x00" * 600), text="")
        with patch.object(voice_narration.requests, "post", side_effect=[resp_422, resp_422, resp_200, resp_200]) as mock_fb, \
             patch.object(voice_narration, "probe_audio_duration_sec", return_value=4.2):
            m1 = voice_narration.synthesize_narration_audio(
                "1本目の原稿です。",
                api_key="sk_test_123456",
                voice_id="voice_memo123456",
                model_id="eleven_v4",
                force_refresh=True,
            )
            self.assertTrue(m1["ok"])
            self.assertEqual(m1["model_id"], "eleven_v3")
            self.assertEqual(mock_fb.call_count, 3)

            m2 = voice_narration.synthesize_narration_audio(
                "2本目の別原稿です。",
                api_key="sk_test_123456",
                voice_id="voice_memo123456",
                model_id="eleven_v4",
                force_refresh=True,
            )
            self.assertTrue(m2["ok"])
            self.assertEqual(m2["model_id"], "eleven_v3")
            self.assertEqual(mock_fb.call_count, 4)

    def test_p1_1_sns_insights_loop_ladies_template_and_ucb1_threshold(self):
        """sns_insights_loop が tiktok_ladies_real を認識し、UCB1 バンディットが 6本未満では発動せず 6本以上で発動すること"""
        self.assertIn("tiktok_ladies_real", sns_insights_loop.REEL_TEMPLATE_KEYS)
        self.assertEqual(
            sns_insights_loop.infer_reel_type_from_caption("女子2人組がいま受付待ちゼロでスマートに乾杯しやすい #相席ナビ"),
            "tiktok_ladies_real",
        )
        store_4 = {
            "version": 1,
            "updated_at": "2026-11-12 22:00:00",
            "last_sync_at": "2026-11-12 22:00:00",
            "last_sync_summary": {},
            "posts": [
                {
                    "id": f"r{i}",
                    "channel": "reels",
                    "reel_type": "tiktok_ladies_real" if i % 2 == 0 else "live_top5",
                    "published_at": "2026-11-11 21:00:00",
                    "metrics": {"views": 500 + i * 50, "avg_watch_sec": 5.5, "skip_rate_pct": 42.0},
                }
                for i in range(4)
            ],
            "note_articles": [],
        }
        with patch.object(sns_insights_loop, "_load_store", return_value=store_4):
            fb4 = sns_insights_loop.compute_loop_feedback(days=28, allow_blocking=False)
        self.assertEqual(fb4.get("reel_template_boosts"), {})

        store_6 = copy.deepcopy(store_4)
        for i in range(4, 6):
            store_6["posts"].append({
                "id": f"r{i}",
                "channel": "reels",
                "reel_type": "tiktok_ladies_real",
                "published_at": "2026-11-11 22:00:00",
                "metrics": {"views": 900, "avg_watch_sec": 7.2, "skip_rate_pct": 35.0},
            })
        sns_insights_loop.reset_insights_loop_cache_for_tests()
        with patch.object(sns_insights_loop, "_load_store", return_value=store_6):
            fb6 = sns_insights_loop.compute_loop_feedback(days=28, allow_blocking=False)
        self.assertIn("tiktok_ladies_real", fb6.get("reel_template_boosts", {}))

    # ------------------------------------------------------------------
    # P1-2 & P2-1: Brand Shortcuts, Kana Aliases, Light-Mode & Stats Purity
    # ------------------------------------------------------------------
    def test_p1_2_brand_shortcut_301_redirects_and_kana_resolution(self):
        """/xix・/yatakoi・/clovers・/alfa がクエリを維持して正規 /store/<slug> へ 301 リダイレクトし、カナ別名が内部店舗名へ解決されること"""
        cases = [
            ("/xix?staff_test=1", "/store/xix-okayama?staff_test=1"),
            ("/yatakoi?from=top", "/store/yatakoi-umeda?from=top"),
            ("/clovers", "/store/clovers-hiroshima"),
            ("/alfa", "/store/alfa-hiroshima"),
        ]
        for src, expected_suffix in cases:
            resp = self.client.get(src)
            self.assertEqual(resp.status_code, 301, f"{src} returned {resp.status_code}")
            loc = resp.headers.get("Location", "")
            self.assertTrue(loc.endswith(expected_suffix), f"{src} redirected to {loc!r}")

        self.assertEqual(store_master.to_internal_store_name("ジックス 岡山"), "XIX OKAYAMA")
        self.assertEqual(store_master.to_internal_store_name("クローバーズ 広島"), "CLOVERS HIROSHIMA")
        self.assertEqual(store_master.to_internal_store_name("相席クローバー広島"), "CLOVERS HIROSHIMA")
        self.assertEqual(store_master.to_internal_store_name("アルファ 広島"), "ALFA HIROSHIMA")
        self.assertEqual(store_master.seo_name_with_kana("XIX OKAYAMA"), "相席ラウンジXIX（ジックス） 岡山")
        self.assertEqual(store_master.seo_name_with_kana("CLOVERS HIROSHIMA"), "相席CLOVERS（クローバーズ） 広島")
        self.assertEqual(store_master.seo_name_with_kana("ALFA HIROSHIMA"), "相席ラウンジALFA（アルファ） 広島")

        r_oita = self.client.get("/store/oriental-lounge-oita")
        self.assertEqual(r_oita.status_code, 200)
        html_oita = r_oita.get_data(as_text=True)
        self.assertIn("評判", html_oita)
        self.assertIn("現場ワンタップ状況レポート", html_oita)

    def test_p2_1_guide_light_mode_purity_stats_zero_link_and_analysis_btn_label(self):
        """全 guide_*.html にダーク専用インライン白文字がなく、/stats* に /link/ が混入せず、トップ店舗カードに可視『分析』ラベルと WCAG 2.5.3 aria-label があること"""
        style_attr_re = re.compile(r'style="([^"]*)"', re.I)
        forbidden_re = re.compile(r"(?:^|;)\s*color\s*:\s*(#fff(?:fff)?|#f8fafc|#e2e8f0|#cbd5e1|#ebe8e3)\b", re.I)
        for path in sorted(glob.glob(os.path.join(REPO_ROOT, "templates", "guide_*.html"))):
            with open(path, encoding="utf-8") as fh:
                txt = fh.read()
            self.assertIn('<meta name="color-scheme" content="dark light">', txt, f"Missing color-scheme meta in {path}")
            for sm in style_attr_re.finditer(txt):
                self.assertIsNone(
                    forbidden_re.search(sm.group(1)),
                    f"Hardcoded light inline color in {os.path.basename(path)}: {sm.group(0)}",
                )

        for stats_url in ("/stats", "/stats/weekend-index", "/stats/monthly/2026-11", "/stats/year-end-2026"):
            resp = self.client.get(stats_url)
            self.assertEqual(resp.status_code, 200, f"{stats_url} returned {resp.status_code}")
            body = resp.get_data(as_text=True)
            self.assertNotIn("/link/", body, f"Direct /link/ leaked into {stats_url}")
            if stats_url in ("/stats/weekend-index", "/stats/monthly/2026-11"):
                self.assertIn('class="portal-footer"', body)

        top_resp = self.client.get("/")
        self.assertEqual(top_resp.status_code, 200)
        top_html = top_resp.get_data(as_text=True)
        self.assertIn('<span class="analysis-btn-label">分析</span>', top_html)
        self.assertIn('aria-label="分析（', top_html)


if __name__ == "__main__":
    unittest.main()
