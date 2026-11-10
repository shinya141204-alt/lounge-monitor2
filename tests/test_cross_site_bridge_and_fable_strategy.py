# -*- coding: utf-8 -*-
"""Unit tests for Fable 5.2 Strategy Top 5 (Track A) and Cross-Site Architecture P0+P1 (Track B)."""

import os
import unittest

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

import affiliate_manager
import analytics
import app as app_mod
import cross_site_bridge
import pdca_engine


class TestCrossSiteBridgeAndFableStrategy(unittest.TestCase):
    def setUp(self):
        self.client = app_mod.app.test_client()
        cross_site_bridge.clear_caches()

    def test_analytics_co_view_and_sister_bridge_normalization(self):
        self.assertIn("co_view", analytics.ALLOWED_EVENTS)
        self.assertIn("sister_bridge_impression", analytics.ALLOWED_EVENTS)
        self.assertIn("sister_bridge_click", analytics.ALLOWED_EVENTS)

        valid_co = analytics.normalize_co_view_label(
            "oriental-lounge-shinjuku|jis-shinjuku"
        )
        self.assertEqual(valid_co, "OLG 新宿|JIS SHINJUKU")
        self.assertEqual(
            analytics.normalize_co_view_label(
                "oriental-lounge-shinjuku|oriental-lounge-shinjuku"
            ),
            "",
        )
        self.assertEqual(analytics.normalize_co_view_label("unknown-a|unknown-b"), "")

        valid_sb = analytics.normalize_sister_bridge_label("shinjuku:men_glut:men")
        self.assertEqual(valid_sb, "shinjuku:men_glut:men")
        self.assertEqual(
            analytics.normalize_sister_bridge_label("shinjuku:bad:men"),
            "shinjuku:other:men",
        )

    def test_women_offer_category_and_smart_cta_internal_href(self):
        women_cat = next(
            c for c in pdca_engine.OFFER_CATEGORIES if c.get("category_key") == "women"
        )
        self.assertEqual(
            women_cat["primary_link"],
            "/link/women_second_venue",
        )
        cta = affiliate_manager.build_store_smart_cta(
            {"slug": "oriental-lounge-shinjuku", "name": "オリエンタルラウンジ 新宿"},
            {"men": 25, "women": 18, "total": 43},
        )
        self.assertIn("primary_internal_href", cta)
        self.assertTrue(str(cta["primary_internal_href"]).startswith("/"))
        self.assertTrue(bool(cta.get("primary_internal_label")))

    def test_exchange_area_night_feed_and_weekend_index_routes(self):
        r_ex = self.client.get("/api/exchange/area-night.v1.json?staff_test=1")
        self.assertEqual(r_ex.status_code, 200)
        ex_data = r_ex.get_json()
        self.assertEqual(ex_data["schema_version"], "1.0")
        self.assertEqual(ex_data["feed"], "aiseki_navi.area_night")
        self.assertEqual(len(ex_data["areas"]), 10)
        self.assertNotIn("/link/", r_ex.get_data(as_text=True))

        r_di_html = self.client.get("/stats/weekend-index?staff_test=1")
        self.assertEqual(r_di_html.status_code, 200)
        di_text = r_di_html.get_data(as_text=True)
        self.assertIn("application/ld+json", di_text)
        self.assertIn("https://machinavi-data.com/data/exchange/area-evening.v1.json", di_text)
        self.assertNotIn("/link/", di_text)

        r_di_json = self.client.get("/stats/weekend-index.json?staff_test=1")
        self.assertEqual(r_di_json.status_code, 200)
        di_json = r_di_json.get_json()
        self.assertEqual(di_json["schema_version"], "1.0")
        self.assertNotIn("/link/", r_di_json.get_data(as_text=True))

        r_di_csv = self.client.get("/stats/weekend-index.csv?staff_test=1")
        self.assertEqual(r_di_csv.status_code, 200)
        csv_text = r_di_csv.get_data(as_text=True)
        self.assertIn("area_id,area_label", csv_text)
        self.assertNotIn("/link/", csv_text)

    def test_monthly_snapshot_and_cushion_adsense_isolation(self):
        r_m = self.client.get("/stats/monthly/2026-11?staff_test=1")
        self.assertEqual(r_m.status_code, 200)
        m_text = r_m.get_data(as_text=True)
        self.assertIn("2026年11月", m_text)
        self.assertNotIn("/link/", m_text)

        r_bad = self.client.get("/stats/monthly/2020-13?staff_test=1")
        self.assertEqual(r_bad.status_code, 404)

        for path in ("/grooming", "/for-women"):
            resp = self.client.get(f"{path}?staff_test=1")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_data(as_text=True)
            self.assertNotIn("adsbygoogle.js", body, path)

        r_pc = self.client.get("/price-comparison?staff_test=1")
        self.assertEqual(r_pc.status_code, 200)
        pc_body = r_pc.get_data(as_text=True)
        self.assertIn("/concierge", pc_body)
        self.assertIn("https://machinavi-data.com/?from=aiseki#diagnose", pc_body)

    def test_reel_bgm_synthesis_hook_spotlight_and_grid_protection(self):
        import array
        import datetime
        import tempfile
        import wave
        from unittest import mock
        import instagram_poster
        import job_state
        import reel_generator

        # 1. Non-silent stereo Lounge Chill + UI SFX WAV synthesis for all templates
        for rt in reel_generator.REEL_TEMPLATES:
            wav_bytes = reel_generator.synthesize_lounge_bgm_wav_bytes(rt)
            self.assertTrue(wav_bytes.startswith(b"RIFF"))
            with wave.open(reel_generator.io.BytesIO(wav_bytes), "rb") as wf:
                self.assertEqual(wf.getnchannels(), 2)
                self.assertEqual(wf.getsampwidth(), 2)
                self.assertEqual(wf.getframerate(), 22050)
                samples = array.array("h", wf.readframes(wf.getnframes()))
                self.assertGreater(max(abs(x) for x in samples), 5000)

        # 2. Scene 1 concrete spotlight & Save CTA rendering
        stores = [
            {"name": "OLG 新宿", "women": 46, "men": 22, "region": "Kanto", "area": "新宿"},
            {"name": "JIS SHINJUKU", "women": 12, "men": 28, "region": "Kanto", "area": "新宿"},
            {"name": "OLG 梅田", "women": 38, "men": 20, "region": "Kinki", "area": "梅田"},
        ]
        for rt in ("live_top5", "area_battle", "ladies_zero_wait", "tiktok_gap_shock"):
            f1 = reel_generator.render_reel_preview_frame_jpeg(stores, reel_type=rt, sec=1.2)
            self.assertGreater(len(f1), 5000)
            cap = reel_generator.build_reel_caption(stores, reel_type=rt)
            self.assertIn("保存", cap)

        # 3. 36h Profile Grid Protection & consecutive-template rotation
        jst = datetime.timezone(datetime.timedelta(hours=9))
        d1 = datetime.datetime(2026, 11, 10, 20, 10, tzinfo=jst)
        d2_next_day = datetime.datetime(2026, 11, 11, 20, 10, tzinfo=jst)  # +24h (<36h -> share_to_feed=False)
        d3_two_days = datetime.datetime(2026, 11, 12, 20, 15, tzinfo=jst)  # +48h (>=36h -> share_to_feed=True)
        with tempfile.TemporaryDirectory() as tmpdir:
            with mock.patch.dict(os.environ, {"JOB_STATE_DIR": tmpdir}):
                job_state.reset_cache_for_tests()
                instagram_poster.record_ig_auto_post_timestamp(d1, is_feed=True, reel_type="live_top5")
                self.assertTrue(instagram_poster.has_ig_grid_posted_recently(d2_next_day, min_hours=36.0))
                self.assertFalse(instagram_poster.has_ig_grid_posted_recently(d3_two_days, min_hours=36.0))

    def test_cross_sns_4_pillar_improvements(self):
        import datetime
        import re
        import column_editorial
        import ogp_generator
        import reel_generator
        import sns_editorial

        # 1. Reels 8.5s timeline + Scene 1 animated bar + Scene 2 fly-in before 2.0s
        self.assertEqual(reel_generator.REEL_DURATION_SEC, 8.5)
        self.assertEqual(reel_generator.REEL_TOTAL_FRAMES, 204)
        stores = [
            {"name": "OLG 名古屋 栄", "women": 55, "men": 20, "region": "Chubu", "area": "栄"},
            {"name": "OLG 新宿", "women": 46, "men": 22, "region": "Kanto", "area": "新宿"},
            {"name": "JIS UMEDA", "women": 41, "men": 14, "region": "Kinki", "area": "梅田"},
            {"name": "OLG 大阪駅前", "women": 9, "men": 9, "region": "Kinki", "area": "梅田"},
            {"name": "XIX 岡山", "women": 10, "men": 9, "region": "Chugoku", "area": "岡山"},
        ]
        ctx = reel_generator.build_reel_context(stores, reel_type="tiktok_gap_shock")
        comp = reel_generator._ReelSceneRenderer(ctx)
        self.assertIsNotNone(comp.scene1_bar_spec)
        for sec in (0.6, 1.2, 1.8, 6.0, 7.5):
            jpeg = reel_generator.render_reel_preview_frame_jpeg(stores, reel_type="tiktok_gap_shock", sec=sec)
            self.assertGreater(len(jpeg), 5000)

        # 2. X OGP Card pins stores mentioned in tweet text (`focus_text`) into TOP 3 in mention order
        tweet_okayama = "岡山、日曜でも1次会を早めに締めた男性陣がこの時間に動いてる。XIX岡山は1時間前まで女性比100%でしたが、いま女性10名・男性9名（53%）になってます。"
        top3_okayama = ogp_generator._select_top3_for_slot(stores, "22:00", focus_text=tweet_okayama)
        self.assertEqual(top3_okayama[0]["name"], "XIX 岡山")

        tweet_umeda = "梅田の20時前、同じ駅まわりでも店のノリで初速がだいぶ違う。「JIS梅田」は女性41名・男性14名（75%）、徒歩4分の「オリエンタルラウンジ 大阪駅前」は女性9名・男性9名（50%）。"
        top3_umeda = ogp_generator._select_top3_for_slot(stores, "20:00", focus_text=tweet_umeda)
        self.assertEqual(top3_umeda[0]["name"], "JIS UMEDA")
        self.assertEqual(top3_umeda[1]["name"], "OLG 大阪駅前")

        self.assertEqual(ogp_generator.resolve_slot_from_param("x22_ai_live_adlib_20261110"), "22:00")
        self.assertEqual(ogp_generator.resolve_slot_from_param("x20_ai_live_adlib_20261110"), "20:00")

        # 3. Stale Reply OGP Card fix: `/` with `?s=x22_ai_live_adlib` and `/store/<slug>` always include YYYYMMDD cache-buster
        r_home = self.client.get("/?s=x22_ai_live_adlib&staff_test=1")
        self.assertEqual(r_home.status_code, 200)
        home_html = r_home.get_data(as_text=True)
        self.assertRegex(home_html, r'property="og:image"\s+content="https://aiseki-navi\.com/ogp/live\.png\?s=x22_ai_live_adlib_\d{8}_\d{4}"')

        r_store = self.client.get("/store/oriental-lounge-shinjuku?s=x20_ai_live_adlib&staff_test=1")
        self.assertEqual(r_store.status_code, 200)
        store_html = r_store.get_data(as_text=True)
        self.assertRegex(store_html, r'property="og:image"\s+content="https://aiseki-navi\.com/ogp/live\.png\?s=\d{8}_\d{4}"')
        self.assertRegex(store_html, r'name="twitter:image"\s+content="https://aiseki-navi\.com/ogp/live\.png\?s=\d{8}_\d{4}"')

        # 4. Threads poetic weather opener penalty & note high-CTR utility bracket title formatting
        jst = datetime.timezone(datetime.timedelta(hours=9))
        now_jst = datetime.datetime(2026, 11, 10, 22, 0, tzinfo=jst)
        res = sns_editorial.run_researcher(stores, slot_label="22:00", now_jst=now_jst)
        plan = sns_editorial.run_planner(res, slot_label="22:00", channel="threads", now_jst=now_jst)
        eval_res = sns_editorial.run_evaluator(
            [
                {
                    "style": "poetic_test",
                    "main_text": "「このあとどうする？」が出るにはまだ少し早い、水曜の19時台。東京は曇りで16℃。\nオリエンタルラウンジ新宿は女性46名・男性22名。",
                },
                {
                    "style": "concrete_test",
                    "main_text": "岡山の話。XIX岡山、1時間前は女性比100%。いま女性10名・男性9名（女性比53%）でちょうど拮抗してます。",
                },
            ],
            res,
            plan,
            channel="threads",
            now_jst=now_jst,
        )
        scores_by_style = {c["style"]: c["scores"]["human_tone"] for c in eval_res["candidates"]}
        self.assertLess(scores_by_style["poetic_test"], scores_by_style["concrete_test"])

        note_t = column_editorial.format_high_ctr_note_title(
            "23時20分、店内の空気が一斉に変わる｜60店舗超のログから見えた夜の行動経済学",
            theme_key="last_train_cliff",
        )
        self.assertTrue(note_t.startswith("【終電前の逆転法則】"))
        self.assertIn("相席ラウンジ", note_t[:30])
        self.assertNotIn("夜の行動経済学", note_t)

        # 5. Fable 5.2 audit regression checks:
        # (a) Unverified `9.6万件` stripped when historical_log_count is absent; preserved when verified
        note_unverified = column_editorial.format_high_ctr_note_title(
            "【9.6万件データ検証】金曜21時に並ぶ人がハマる罠｜数字の裏側にある夜の意思決定ノート",
            theme_key="friday_rush",
            research={"historical_log_count": 0},
        )
        self.assertNotIn("9.6万件", note_unverified)
        self.assertNotIn("意思決定ノート", note_unverified)
        note_verified = column_editorial.format_high_ctr_note_title(
            "金曜21時に並ぶ人がハマる罠｜数字の裏側にある夜の意思決定ノート",
            theme_key="friday_rush",
            research={"historical_log_count": 96000},
        )
        self.assertIn("96,000件", note_verified)

        # (b) Store-name bracket preserved & English `Instagram` not false-matched as `ag`
        note_store_b = column_editorial.format_high_ctr_note_title(
            "【オリエンタルラウンジ新宿】22時台の女性比とInstagram口コミの差",
            theme_key="area_hopping",
        )
        self.assertIn("【オリエンタルラウンジ新宿】", note_store_b)

        # (c) Empty stores do not fabricate 58%/60% Scene 1 bar
        empty_ctx = reel_generator.build_reel_context([], reel_type="live_top5")
        empty_comp = reel_generator._ReelSceneRenderer(empty_ctx)
        self.assertIsNone(empty_comp.scene1_bar_spec)

        # (d) Stale campaign prefix (`x22_...`) + daytime 15m bucket (`_20261111_1500`) resolves to daytime/20:00
        self.assertEqual(
            ogp_generator.resolve_slot_from_param("x22_ai_live_adlib_20261111_1500"),
            "20:00",
        )
        self.assertEqual(
            ogp_generator.resolve_slot_from_param("x22_ai_live_adlib_20261110_2215"),
            "22:00",
        )

        # (e) `2度目の波` in first line is NOT penalized as a temperature opener
        eval_deg = sns_editorial.run_evaluator(
            [
                {
                    "style": "wave_test",
                    "main_text": "新宿の23時半、今夜2度目の波が来てる。オリエンタルラウンジ新宿は女性46名・男性22名（女性比68%）。",
                }
            ],
            res,
            plan,
            channel="threads",
            now_jst=now_jst,
        )
        self.assertGreaterEqual(eval_deg["candidates"][0]["scores"]["human_tone"], 28)


if __name__ == "__main__":
    unittest.main()
