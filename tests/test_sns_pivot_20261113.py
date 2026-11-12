import datetime
import os
import re
import tempfile
import unittest
from unittest import mock

os.environ["DISABLE_SCHEDULER"] = "1"

import app as app_module
import job_state
import sns_editorial
import sns_workflows
import threads_poster
import worker as worker_module
import x_poster

JST = datetime.timezone(datetime.timedelta(hours=9))
BANNED_BRAND_RE = re.compile(
    r"オリエンタル(?!ラウンジ)|(?<![A-Za-z])OLG(?![A-Za-z])|(?<![A-Za-z])YATAKOI(?![A-Za-z])|オリラジ"
)


class SnsOption1PivotTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._env_patch = mock.patch.dict(
            os.environ,
            {
                "JOB_STATE_DIR": self._tmpdir.name,
                "DISABLE_SCHEDULER": "1",
                "MASTER_PASSWORD": "test_master_pw_pivot",
            },
        )
        self._env_patch.start()
        job_state.reset_cache_for_tests()
        self.client = app_module.app.test_client()

    def tearDown(self):
        self._env_patch.stop()
        self._tmpdir.cleanup()

    def test_worker_scheduler_reflects_option1_pivot_and_retires_bot_jobs(self):
        sched = worker_module.build_worker_scheduler()
        jobs_by_id = {j.id: j for j in sched.get_jobs()}

        for active_id in (
            "x_data_law_1915",
            "x_search_2200",
            "threads_conv_2115",
            "x_weekly_sun",
            "ig_reel_2000",
            "ig_reel_2250",
            "tiktok_inbox_2035",
        ):
            self.assertIn(active_id, jobs_by_id)

        for retired_id in (
            "x_post_2000",
            "x_post_2200",
            "x_post_0000",
            "x_tip_1800",
            "bonus_alert_10m",
            "threads_daily_2200",
            "threads_ladies_1930",
        ):
            self.assertNotIn(retired_id, jobs_by_id)

        self.assertIn("day_of_week='mon,tue,wed,thu,fri'", str(jobs_by_id["x_data_law_1915"].trigger))
        self.assertIn("day_of_week='fri,sat'", str(jobs_by_id["x_search_2200"].trigger))
        self.assertIn("day_of_week='tue,thu,fri,sun'", str(jobs_by_id["threads_conv_2115"].trigger))

        pacing = sns_editorial.get_sns_human_pacing_summary()
        self.assertIn("データ法則", pacing["x_cadence_ja"])
        self.assertIn("問いかけ", pacing["threads_cadence_ja"])

    def test_threads_conversational_seeds_and_sanitizer_invariants(self):
        self.assertGreaterEqual(len(threads_poster.THREADS_CONVERSATION_SEEDS), 14)
        for seed in threads_poster.THREADS_CONVERSATION_SEEDS:
            txt = threads_poster._sanitize_threads_conversational_text(seed["text"])
            self.assertTrue(txt.endswith("？"), f"Seed {seed['key']} must end with '？': {txt}")
            self.assertNotIn("http", txt)
            self.assertNotIn("aiseki-navi.com", txt)
            self.assertNotIn("#", txt)
            self.assertNotIn("プロフ", txt)
            self.assertNotIn("リプ欄", txt)
            self.assertNotIn("👇", txt)
            self.assertIsNone(BANNED_BRAND_RE.search(txt), f"Banned brand shorthand in {seed['key']}: {txt}")
            self.assertLessEqual(len(txt), 420)

        dirty = (
            "OLGの席で盛り上がりました！\n"
            "詳細はプロフのリンクから https://aiseki-navi.com/?s=1 👇\n"
            "みんなはどっち派 #相席ナビ #相席ラウンジ"
        )
        cleaned = threads_poster._sanitize_threads_conversational_text(dirty)
        self.assertIn("オリエンタルラウンジ", cleaned)
        self.assertNotIn("OLG", cleaned)
        self.assertNotIn("http", cleaned)
        self.assertNotIn("プロフ", cleaned)
        self.assertNotIn("#", cleaned)
        self.assertTrue(cleaned.endswith("？"))

    def test_execute_scheduled_conversational_threads_post_rotates_and_posts_text_only(self):
        seen_keys = []
        base_dt = datetime.datetime(2026, 11, 13, 21, 25, tzinfo=JST)
        with mock.patch("threads_poster.refresh_threads_token_if_needed"), \
             mock.patch("threads_poster.post_to_threads", return_value={"status": "success", "id": "th_conv_1"}) as m_post:
            for day_off in range(5):
                dt = base_dt + datetime.timedelta(days=day_off)
                res = threads_poster.execute_scheduled_conversational_threads_post(
                    stores=[],
                    slot_label="21:15",
                    force=False,
                    now_jst=dt,
                    use_ai=False,
                )
                self.assertEqual(res["status"], "success")
                self.assertTrue(res["threads_text"].endswith("？"))
                seen_keys.append(res["seed_key"])
            # Pure TEXT post: image_url must always be None
            for call_args in m_post.call_args_list:
                self.assertIsNone(call_args.kwargs.get("image_url"))

            # Duplicate within same slot is skipped
            dup_res = threads_poster.execute_scheduled_conversational_threads_post(
                stores=[],
                slot_label="21:15",
                force=False,
                now_jst=base_dt + datetime.timedelta(days=4, minutes=10),
                use_ai=False,
            )
            self.assertEqual(dup_res["status"], "skipped")

        self.assertEqual(len(set(seen_keys)), 5, f"Expected 5 distinct Threads seeds across 5 days, got {seen_keys}")

    def test_x_data_law_posts_are_standalone_url_free_and_within_weight_budget(self):
        self.assertGreaterEqual(len(x_poster.X_DATA_LAW_POSTS), 14)
        for item in x_poster.X_DATA_LAW_POSTS:
            txt = x_poster._sanitize_x_standalone_text(item["text"])
            wt = x_poster.compute_twitter_weight(txt)
            self.assertLessEqual(wt, 272, f"{item['key']} exceeded 272 weight ({wt}): {txt}")
            self.assertNotIn("http", txt)
            self.assertNotIn("aiseki-navi.com", txt)
            self.assertNotIn("リプ欄", txt)
            self.assertNotIn("👇", txt)
            self.assertNotIn("#", txt)
            self.assertIsNone(BANNED_BRAND_RE.search(txt), f"Banned brand in {item['key']}: {txt}")

        base_dt = datetime.datetime(2026, 11, 16, 19, 25, tzinfo=JST)
        seen_laws = []
        with mock.patch("x_poster.post_to_x", return_value={"status": "success", "data": {"data": {"id": "tw_law_1"}}}) as m_x:
            for d in range(5):
                dt = base_dt + datetime.timedelta(days=d)
                res = sns_workflows.execute_scheduled_x_data_law_post(slot_label="19:15", force=False, now_jst=dt)
                self.assertEqual(res["status"], "success")
                self.assertTrue(res["standalone_no_reply"])
                self.assertEqual(res["reply_text"], "")
                seen_laws.append(res["law_key"])
            for call_args in m_x.call_args_list:
                self.assertIsNone(call_args.kwargs.get("reply_text"))
        self.assertEqual(len(set(seen_laws)), 5)

    def test_x_search_catcher_official_brand_names_and_clean_format(self):
        self.assertEqual(x_poster.format_official_searchable_store_name("OLG 新宿"), "オリエンタルラウンジ 新宿")
        self.assertEqual(x_poster.format_official_searchable_store_name("OLG ag渋谷"), "ag（アグ）渋谷")
        self.assertEqual(x_poster.format_official_searchable_store_name("JIS SHINJUKU"), "JIS（ジス）新宿")
        self.assertEqual(x_poster.format_official_searchable_store_name("ASOBIBAR 心斎橋"), "ASOBIBAR（アソビバー）心斎橋")
        self.assertEqual(x_poster.format_official_searchable_store_name("XIX OKAYAMA"), "XIX（ジックス）岡山")

        sample_stores = [
            {"name": "JIS SHINJUKU", "women": 28, "men": 14, "region": "Kanto"},
            {"name": "OLG ag渋谷", "women": 22, "men": 11, "region": "Kanto"},
            {"name": "OLG 梅田", "women": 25, "men": 16, "region": "Kinki"},
            {"name": "ASOBIBAR 心斎橋", "women": 18, "men": 15, "region": "Kinki"},
        ]
        fri_dt = datetime.datetime(2026, 11, 13, 22, 12, tzinfo=JST)
        main_txt, rep_txt = x_poster.build_x_search_catcher_post(sample_stores, now_jst=fri_dt)
        self.assertLessEqual(x_poster.compute_twitter_weight(main_txt), 272)
        self.assertIn("JIS（ジス）新宿", main_txt)
        self.assertIn("ag（アグ）渋谷", main_txt)
        self.assertIn("オリエンタルラウンジ 梅田", main_txt)
        self.assertNotIn("👇", main_txt)
        self.assertNotIn("リプ欄", main_txt)
        self.assertNotIn("#", main_txt)
        self.assertNotIn("http", main_txt)
        self.assertIsNone(BANNED_BRAND_RE.search(main_txt))
        self.assertIn("https://aiseki-navi.com/?s=x22_search_20261113", rep_txt)

        with mock.patch("x_poster.post_to_x", return_value={"status": "success", "data": {"data": {"id": "tw_s_1"}}}) as m_x:
            res = sns_workflows.execute_scheduled_x_search_catcher_post(
                {"full_data": sample_stores, "is_seed": False},
                slot_label="22:00",
                force=False,
                now_jst=fri_dt,
            )
            self.assertEqual(res["status"], "success")
            self.assertEqual(res["editorial_style"], "x_search_catcher")
            self.assertTrue(m_x.called)

    def test_sns_preview_and_master_trigger_modes(self):
        with app_module.data_lock:
            app_module.latest_data["full_data"] = [
                {"name": "JIS SHINJUKU", "women": 24, "men": 12, "region": "Kanto"},
                {"name": "OLG 梅田", "women": 20, "men": 14, "region": "Kinki"},
            ]
            app_module.latest_data["is_seed"] = False

        for ptype in ("threads_conv", "x_data_law", "x_search"):
            resp = self.client.get(f"/api/sns-post?type={ptype}")
            self.assertEqual(resp.status_code, 200)
            body = resp.get_json()
            self.assertEqual(body["status"], "success")
            self.assertTrue(body["text"])


if __name__ == "__main__":
    unittest.main()
