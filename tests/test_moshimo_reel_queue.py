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
        os.environ["JOB_STATE_DIR"] = self._tmpdir.name
        os.environ["MOSHIMO_EVERGREEN_RECYCLE_DEFAULT"] = "0"
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
        self._tmpdir.cleanup()

    def test_all_ten_moshimo_assets_captions_and_youtube_pack_valid(self):
        self.assertEqual(len(moshimo_reel_queue.MOSHIMO_EPISODES), 10)
        st = moshimo_reel_queue.get_moshimo_queue_status(allow_blocking=False)
        self.assertEqual(st["total_episodes"], 10)
        self.assertEqual(st["remaining_count"], 10)
        self.assertEqual(st["posted_count"], 0)
        self.assertTrue(st["youtube_bio_url"].endswith("/?s=short_bio"))
        for ep in moshimo_reel_queue.MOSHIMO_EPISODES:
            local_p = moshimo_reel_queue.resolve_episode_local_path(ep)
            self.assertTrue(os.path.isfile(local_p), f"Missing MP4: {local_p}")
            self.assertGreater(os.path.getsize(local_p), 1_000_000)
            self.assertNotIn("/link/", ep["caption"])
            self.assertIn("相席ナビ", ep["caption"])
            self.assertTrue(ep.get("youtube_title"))
            self.assertLessEqual(len(ep["youtube_title"]), 100)
            yt_desc = moshimo_reel_queue.build_youtube_shorts_description(ep)
            self.assertIn("@aiseki_navi", yt_desc)
            self.assertNotIn("/link/", yt_desc)

    def test_ten_episode_schedule_saturday_live_and_evergreen_recycle(self):
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

            # 4) Mon..Fri 2026-11-16..20 -> ep2..ep6 publish on weekdays
            week1_expected = [
                (16, "ep2_matching_app"),
                (17, "ep3_women_free"),
                (18, "ep4_last_train"),
                (19, "ep5_crowd_paradox"),
                (20, "ep6_rainy_payday"),
            ]
            for day_num, exp_ep in week1_expected:
                dt = datetime.datetime(2026, 11, day_num, 20, 10, tzinfo=JST)
                r = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=dt)
                self.assertEqual(r["status"], "success")
                self.assertEqual(r["ep_id"], exp_ep)

            # 5) Sat 2026-11-21 20:10 JST -> Peak Saturday serves live-data Reel while ep7..ep10 wait for Mon..Thu
            sat2_20 = datetime.datetime(2026, 11, 21, 20, 10, tzinfo=JST)
            res_sat2 = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=sat2_20)
            self.assertEqual(res_sat2["status"], "success")
            self.assertEqual(res_sat2["reel_type"], "live_top5")
            self.assertEqual(mock_live_fallback.call_count, 1)

            # 6) Sun 2026-11-22 20:10 JST -> Sunday rest day skipped
            sun2_20 = datetime.datetime(2026, 11, 22, 20, 10, tzinfo=JST)
            res_sun2 = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=sun2_20)
            self.assertEqual(res_sun2["status"], "skipped")

            # 7) Mon..Thu 2026-11-23..26 -> ep7..ep10 publish in order
            week2_expected = [
                (23, "ep7_dome_live"),
                (24, "ep8_no_change"),
                (25, "ep9_sunday_night"),
                (26, "ep10_solo_night"),
            ]
            for day_num, exp_ep in week2_expected:
                dt = datetime.datetime(2026, 11, day_num, 20, 10, tzinfo=JST)
                r = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=dt)
                self.assertEqual(r["status"], "success")
                self.assertEqual(r["ep_id"], exp_ep)

            self.assertEqual(mock_post_ig.call_count, 10)
            self.assertFalse(moshimo_reel_queue.any_unposted_moshimo_episodes())

            # 8) Fri 2026-11-27 20:10 JST -> All 10 done; seamlessly falls back to live Reel
            fri_20 = datetime.datetime(2026, 11, 27, 20, 10, tzinfo=JST)
            res_fri = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=fri_20)
            self.assertEqual(res_fri["status"], "success")
            self.assertEqual(res_fri["reel_type"], "live_top5")

            # 9) Evergreen recycle ON: <21 days (Tue 2026-12-01) -> live fallback; >=21 days (Tue 2026-12-08) -> recycles ep1
            moshimo_reel_queue.set_evergreen_recycle_enabled(True)
            tue_early = datetime.datetime(2026, 12, 1, 20, 10, tzinfo=JST)
            res_early = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=tue_early)
            self.assertEqual(res_early["reel_type"], "live_top5")

            tue_recycle = datetime.datetime(2026, 12, 8, 20, 10, tzinfo=JST)
            res_rec = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=tue_recycle)
            self.assertEqual(res_rec["status"], "success")
            self.assertEqual(res_rec["ep_id"], "ep1_vanished")

            # Wednesday 2026-12-09 still serves live-data Reel (Evergreen only runs Tue/Thu)
            wed_live = datetime.datetime(2026, 12, 9, 20, 10, tzinfo=JST)
            res_wed = moshimo_reel_queue.dispatch_scheduled_ig_reel(sample_stores, slot_label="20:00", now_jst=wed_live)
            self.assertEqual(res_wed["reel_type"], "live_top5")


if __name__ == "__main__":
    unittest.main()
