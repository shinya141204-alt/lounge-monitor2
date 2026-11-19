import datetime
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

import instagram_poster
import job_state
import moshimo_reel_queue


JST = datetime.timezone(datetime.timedelta(hours=9))


class MoshimoReelQueueTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_dir = os.environ.get("JOB_STATE_DIR")
        self._prev_ev = os.environ.get("MOSHIMO_EVERGREEN_RECYCLE_DEFAULT")
        self._prev_lf = os.environ.get("MOSHIMO_LIVE_REEL_FALLBACK_DEFAULT")
        os.environ["JOB_STATE_DIR"] = self._tmpdir.name
        os.environ["MOSHIMO_EVERGREEN_RECYCLE_DEFAULT"] = "0"
        os.environ["MOSHIMO_LIVE_REEL_FALLBACK_DEFAULT"] = "0"
        with job_state._lock:
            job_state._keys = None
            job_state._kv = None
            job_state._kv_ts = None
            job_state._local_loaded = False
            job_state._sheets_loaded = True
            job_state._turbo_loaded = False
            job_state._neg_cache.clear()

    def tearDown(self):
        if self._prev_dir is None:
            os.environ.pop("JOB_STATE_DIR", None)
        else:
            os.environ["JOB_STATE_DIR"] = self._prev_dir
        if self._prev_ev is None:
            os.environ.pop("MOSHIMO_EVERGREEN_RECYCLE_DEFAULT", None)
        else:
            os.environ["MOSHIMO_EVERGREEN_RECYCLE_DEFAULT"] = self._prev_ev
        if self._prev_lf is None:
            os.environ.pop("MOSHIMO_LIVE_REEL_FALLBACK_DEFAULT", None)
        else:
            os.environ["MOSHIMO_LIVE_REEL_FALLBACK_DEFAULT"] = self._prev_lf
        self._tmpdir.cleanup()

    def test_all_fifteen_moshimo_assets_captions_and_youtube_pack_valid(self):
        import pathlib
        import re
        import reel_generator

        self.assertEqual(len(moshimo_reel_queue.MOSHIMO_EPISODES), 15)
        st = moshimo_reel_queue.get_moshimo_queue_status(allow_blocking=False)
        self.assertEqual(st["total_episodes"], 15)
        self.assertEqual(st["remaining_count"], 15)
        self.assertEqual(st["posted_count"], 0)
        self.assertFalse(st["live_reel_fallback"])
        self.assertEqual(st["schedule_cadence"], "mon_sat_6x")
        self.assertTrue(st["youtube_bio_url"].endswith("/?s=short_bio"))
        self.assertTrue(st["instagram_bio_url"].endswith("/?s=ig_bio"))

        bad_brand_re = re.compile(r"(?<![ァ-ヶー])オリエンタル(?!ラウンジ)|(?<![A-Za-z])OLG(?![A-Za-z])|オリラジ|YATAKOI")
        routes_src = pathlib.Path(moshimo_reel_queue._REPO_DIR, "routes", "portal_bp.py").read_text(encoding="utf-8")

        for ep in moshimo_reel_queue.MOSHIMO_EPISODES:
            local_p = moshimo_reel_queue.resolve_episode_local_path(ep)
            self.assertTrue(os.path.isfile(local_p), f"Missing MP4: {local_p}")
            self.assertGreater(os.path.getsize(local_p), 1_000_000)
            self.assertNotIn("/link/", ep["caption"])
            self.assertIn("相席ナビ", ep["caption"])
            self.assertTrue(ep.get("youtube_title"))
            self.assertLessEqual(len(ep["youtube_title"]), 100)
            col_path = ep.get("related_column_path", "")
            self.assertTrue(
                f"'{col_path}'" in routes_src or f'"{col_path}"' in routes_src,
                f"Missing route for {ep['ep_id']}: {col_path}",
            )
            for field_name in ("title", "youtube_title", "caption", "pinned_comment"):
                val = str(ep.get(field_name) or "")
                self.assertIsNone(bad_brand_re.search(val), f"Brand naming issue in {ep['ep_id']} {field_name}: {val}")
            # Ensure enforce_brand_rule is idempotent (no double kana parentheses)
            enforced = reel_generator.enforce_brand_rule(ep["caption"])
            self.assertNotIn("（ジス）（ジス）", enforced)
            self.assertNotIn("（アグ）（アグ）", enforced)
            yt_desc = moshimo_reel_queue.build_youtube_shorts_description(ep)
            yt_pin = moshimo_reel_queue.build_youtube_shorts_pinned_comment(ep)
            self.assertIn("@aiseki_navi", yt_desc)
            self.assertIn("@aiseki_navi", yt_pin)
            self.assertNotIn("プロフィールの『相席ナビ』", yt_desc)
            self.assertNotIn("/link/", yt_desc)
            self.assertNotIn("/link/", yt_pin)

    def test_fifteen_episode_6x_week_moshimo_only_and_evergreen_recycle(self):
        sample_stores = [{"id": "jis_umeda", "name": "JIS UMEDA", "women": 40, "men": 25}]

        with (
            patch.object(instagram_poster, "is_reel_auto_schedule_enabled", return_value=True),
            patch.object(instagram_poster, "refresh_ig_token_if_needed", return_value=None),
            patch.object(
                instagram_poster,
                "post_reel_to_instagram",
                side_effect=lambda video_url, caption, **kw: {
                    "status": "success",
                    "ok": True,
                    "id": f"ig_media_{video_url.split('=')[-1]}",
                },
            ) as mock_post_ig,
            patch.object(
                instagram_poster,
                "publish_live_reel_to_instagram",
                return_value={"status": "success", "ok": True, "reel_type": "live_top5"},
            ) as mock_live_fallback,
        ):
            # 1) Launch night Sat 2026-11-14 20:10 JST -> ep1_vanished publishes
            sat_20 = datetime.datetime(2026, 11, 14, 20, 10, tzinfo=JST)
            res1 = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=sat_20)
            self.assertEqual(res1["status"], "success")
            self.assertEqual(res1["ep_id"], "ep1_vanished")
            self.assertEqual(mock_post_ig.call_count, 1)

            # 2) Same night Sat 2026-11-14 22:55 JST -> 22:50 slot suppressed
            sat_2250 = datetime.datetime(2026, 11, 14, 22, 55, tzinfo=JST)
            res_late = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="22:50", now_jst=sat_2250)
            self.assertEqual(res_late["status"], "skipped")
            self.assertIn("Suppressed", res_late["reason"])

            # 3) Sun 2026-11-15 20:10 JST -> Sunday rest day skipped
            sun_20 = datetime.datetime(2026, 11, 15, 20, 10, tzinfo=JST)
            res_sun = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=sun_20)
            self.assertEqual(res_sun["status"], "skipped")
            self.assertIn("Sunday rest day", res_sun["reason"])

            # 4) Mon..Sat 2026-11-16..21 -> ep2..ep7 publish 6x/week (including Saturday 11/21!)
            week1_expected = [
                (16, "ep2_matching_app"),
                (17, "ep3_women_free"),
                (18, "ep4_last_train"),
                (19, "ep5_crowd_paradox"),
                (20, "ep6_rainy_payday"),
                (21, "ep7_dome_live"),
            ]
            for day_num, exp_ep in week1_expected:
                dt = datetime.datetime(2026, 11, day_num, 20, 10, tzinfo=JST)
                r = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=dt)
                self.assertEqual(r["status"], "success")
                self.assertEqual(r["ep_id"], exp_ep)

            # 5) Sun 2026-11-22 20:10 JST -> Sunday rest day skipped
            sun2_20 = datetime.datetime(2026, 11, 22, 20, 10, tzinfo=JST)
            res_sun2 = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=sun2_20)
            self.assertEqual(res_sun2["status"], "skipped")
            self.assertIn("Sunday rest day", res_sun2["reason"])

            # 6) Mon..Sat 2026-11-23..28 -> ep8..ep13 publish 6x/week (Season 2 starts Thu 11/26!)
            week2_expected = [
                (23, "ep8_no_change"),
                (24, "ep9_sunday_night"),
                (25, "ep10_solo_night"),
                (26, "ep11_no_read_receipt"),
                (27, "ep12_no_mirror"),
                (28, "ep13_no_smartphone"),
            ]
            for day_num, exp_ep in week2_expected:
                dt = datetime.datetime(2026, 11, day_num, 20, 10, tzinfo=JST)
                r = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=dt)
                self.assertEqual(r["status"], "success")
                self.assertEqual(r["ep_id"], exp_ep)

            # 7) Sun 2026-11-29 -> Sunday rest day skipped (20:00 & 22:50)
            sun3_20 = datetime.datetime(2026, 11, 29, 20, 10, tzinfo=JST)
            res_sun3 = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=sun3_20)
            self.assertEqual(res_sun3["status"], "skipped")
            self.assertIn("Sunday rest day", res_sun3["reason"])

            # 8) Mon..Tue 2026-11-30..12-01 -> ep14..ep15 publish in order
            week3_expected = [
                (11, 30, "ep14_no_tokyo_friday"),
                (12, 1, "ep15_no_live_count"),
            ]
            for mon_num, day_num, exp_ep in week3_expected:
                dt = datetime.datetime(2026, mon_num, day_num, 20, 10, tzinfo=JST)
                r = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=dt)
                self.assertEqual(r["status"], "success")
                self.assertEqual(r["ep_id"], exp_ep)

            self.assertEqual(mock_post_ig.call_count, 15)
            self.assertFalse(moshimo_reel_queue.any_unposted_moshimo_episodes())

            # 9) Wed 2026-12-02 20:10 & 22:55 JST -> Moshimo-only mode skips live-headcount auto-reels!
            wed_20 = datetime.datetime(2026, 12, 2, 20, 10, tzinfo=JST)
            res_wed_off = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=wed_20)
            self.assertEqual(res_wed_off["status"], "skipped")
            self.assertIn("Moshimo-only", res_wed_off["reason"])
            wed_2250 = datetime.datetime(2026, 12, 2, 22, 55, tzinfo=JST)
            res_wed_late = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="22:50", now_jst=wed_2250)
            self.assertEqual(res_wed_late["status"], "skipped")
            self.assertIn("Moshimo-only", res_wed_late["reason"])
            self.assertEqual(mock_live_fallback.call_count, 0)

            # 10) Evergreen recycle ON in Moshimo-only mode:
            #     - Mon 2026-12-07 (wrong weekday) -> skipped (no live Reel pollution)
            #     - Tue 2026-12-08 (>=21d after 11/14) -> recycles ep1_vanished
            #     - Wed 2026-12-09 (wrong weekday) -> skipped (no live Reel pollution)
            moshimo_reel_queue.set_evergreen_recycle_enabled(True)
            mon_skip = datetime.datetime(2026, 12, 7, 20, 10, tzinfo=JST)
            res_mon = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=mon_skip)
            self.assertEqual(res_mon["status"], "skipped")

            tue_recycle = datetime.datetime(2026, 12, 8, 20, 10, tzinfo=JST)
            res_rec = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=tue_recycle)
            self.assertEqual(res_rec["status"], "success")
            self.assertEqual(res_rec["ep_id"], "ep1_vanished")

            wed_skip = datetime.datetime(2026, 12, 9, 20, 10, tzinfo=JST)
            res_wed = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=wed_skip)
            self.assertEqual(res_wed["status"], "skipped")
            self.assertEqual(mock_live_fallback.call_count, 0)

            # 11) Explicit opt-in (`set_live_reel_fallback_enabled(True)`) restores live fallback when desired
            moshimo_reel_queue.set_live_reel_fallback_enabled(True)
            res_optin = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=wed_skip)
            self.assertEqual(res_optin["status"], "success")
            self.assertEqual(res_optin["reel_type"], "live_top5")
            self.assertEqual(mock_live_fallback.call_count, 1)

    def test_evergreen_21_day_cooldown_and_2250_suppression(self):
        sample_stores = [{"id": "jis_umeda", "name": "JIS UMEDA", "women": 40, "men": 25}]
        # Mark all 15 episodes as posted on 2026-11-25 so Tue 2026-12-08 is ONLY 13 days (<21d)
        for ep in moshimo_reel_queue.MOSHIMO_EPISODES:
            job_state.mark_done(f"{moshimo_reel_queue.MOSHIMO_EPISODE_DONE_PREFIX}{ep['ep_id']}")
            job_state.set_value(f"{moshimo_reel_queue.MOSHIMO_EPISODE_LAST_TS_PREFIX}{ep['ep_id']}", "2026-11-25 20:10:00")
        moshimo_reel_queue.set_evergreen_recycle_enabled(True)

        with (
            patch.object(instagram_poster, "is_reel_auto_schedule_enabled", return_value=True),
            patch.object(instagram_poster, "refresh_ig_token_if_needed", return_value=None),
            patch.object(
                instagram_poster,
                "post_reel_to_instagram",
                side_effect=lambda video_url, caption, **kw: {"status": "success", "ok": True, "id": "ig_rec"},
            ),
            patch.object(
                instagram_poster,
                "publish_live_reel_to_instagram",
                return_value={"status": "success", "ok": True, "reel_type": "live_top5"},
            ) as mock_live,
        ):
            # Tue 2026-12-08 (13d elapsed < 21d cooldown) -> skipped (Moshimo-only mode, zero live fallback)
            tue_too_soon = datetime.datetime(2026, 12, 8, 20, 10, tzinfo=JST)
            r_early = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=tue_too_soon)
            self.assertEqual(r_early["status"], "skipped")
            self.assertEqual(mock_live.call_count, 0)

            # Thu 2026-12-17 (22d elapsed >= 21d cooldown) -> recycles ep1_vanished, then suppresses 22:50 same night
            thu_ready = datetime.datetime(2026, 12, 17, 20, 10, tzinfo=JST)
            r_rec = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=thu_ready)
            self.assertEqual(r_rec["status"], "success")
            self.assertEqual(r_rec["ep_id"], "ep1_vanished")
            thu_2250 = datetime.datetime(2026, 12, 17, 22, 55, tzinfo=JST)
            r_sup = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="22:50", now_jst=thu_2250)
            self.assertEqual(r_sup["status"], "skipped")
            self.assertIn("Suppressed", r_sup["reason"])


if __name__ == "__main__":
    unittest.main()
