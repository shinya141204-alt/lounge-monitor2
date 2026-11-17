import io
import json
import os
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

os.environ["DISABLE_SHEETS"] = "1"
os.environ["DISABLE_SCHEDULER"] = "1"

import app as app_mod
import job_state
import sns_insights_loop
import youtube_analytics_sync


class _FakeResp:
    def __init__(self, status_code: int, payload: dict):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload, ensure_ascii=False)

    def json(self):
        return self._payload


class TestYouTubeAnalyticsAutoSync(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_job_dir = os.environ.get("JOB_STATE_DIR")
        os.environ["JOB_STATE_DIR"] = self._tmpdir.name
        job_state.reset_cache_for_tests()
        sns_insights_loop.reset_insights_loop_cache_for_tests()
        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()

    def tearDown(self):
        if self._prev_job_dir is None:
            os.environ.pop("JOB_STATE_DIR", None)
        else:
            os.environ["JOB_STATE_DIR"] = self._prev_job_dir
        job_state.reset_cache_for_tests()
        sns_insights_loop.reset_insights_loop_cache_for_tests()
        self._tmpdir.cleanup()

    def _login_master(self):
        with self.client.session_transaction() as sess:
            sess["is_master"] = True
            sess["staff_authed"] = True

    def test_iso8601_duration_parser(self):
        self.assertEqual(youtube_analytics_sync.parse_iso8601_duration_seconds("PT26S"), 26)
        self.assertEqual(youtube_analytics_sync.parse_iso8601_duration_seconds("PT1M15S"), 75)
        self.assertEqual(youtube_analytics_sync.parse_iso8601_duration_seconds("PT3M"), 180)
        self.assertEqual(youtube_analytics_sync.parse_iso8601_duration_seconds("PT10M5S"), 605)
        self.assertEqual(youtube_analytics_sync.parse_iso8601_duration_seconds(""), 0)

    def test_parse_google_client_secret_json_and_obfuscated_storage(self):
        raw_json = json.dumps(
            {
                "web": {
                    "client_id": "998877665544-abcdefg.apps.googleusercontent.com",
                    "project_id": "loungemonitor",
                    "client_secret": "GOCSPX-SuperSecretValue123",
                }
            }
        )
        cid, csec, err = youtube_analytics_sync.parse_oauth_client_credentials(client_json_raw=raw_json)
        self.assertEqual(err, "")
        self.assertEqual(cid, "998877665544-abcdefg.apps.googleusercontent.com")
        self.assertEqual(csec, "GOCSPX-SuperSecretValue123")

        save_res = youtube_analytics_sync.save_oauth_client_credentials(cid, csec)
        self.assertTrue(save_res["ok"])

        status = youtube_analytics_sync.get_youtube_sync_status()
        self.assertTrue(status["has_client_credentials"])
        self.assertFalse(status["connected"])
        status_dump = json.dumps(status)
        self.assertNotIn("GOCSPX-SuperSecretValue123", status_dump)

        # Verify local job_state.json obfuscates the client secret via `obf1:`
        js_file = os.path.join(self._tmpdir.name, "job_state.json")
        with open(js_file, "r", encoding="utf-8") as fp:
            disk_raw = fp.read()
        self.assertNotIn("GOCSPX-SuperSecretValue123", disk_raw)
        self.assertIn("obf1:", disk_raw)

    def test_oauth_flow_csrf_state_and_shorts_deduplication(self):
        # 1) Pre-seed manual Studio screenshot entry (`yt_moshimo_ep1_vanished`)
        sns_insights_loop.ingest_manual_insight_records(
            {
                "posts": [
                    {
                        "channel": "youtube_shorts",
                        "post_id": "yt_moshimo_ep1_vanished",
                        "opener": "【もしも検証#1】もし相席ラウンジ入店3秒で相手が全員消えたら？",
                        "reel_type": "moshimo_3d",
                        "views": 1122,
                        "stayed_to_watch_pct": 20.5,
                        "avg_watch_sec": 14.1,
                        "likes": 1,
                    }
                ]
            }
        )

        # 2) Unauthenticated request is blocked
        r_unauth = self.client.post(
            "/api/pdca/youtube/config",
            json={"client_id": "123456789012.apps.googleusercontent.com", "client_secret": "GOCSPX-TestSecret99"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r_unauth.status_code, 401)

        # 3) Master uploads Google Cloud `client_secret.json`
        self._login_master()
        client_json_bytes = json.dumps(
            {
                "web": {
                    "client_id": "123456789012-xyz987.apps.googleusercontent.com",
                    "client_secret": "GOCSPX-TestSecret99",
                }
            }
        ).encode("utf-8")
        r_cfg = self.client.post(
            "/api/pdca/youtube/config",
            data={"client_json_file": (io.BytesIO(client_json_bytes), "client_secret_test.json")},
            content_type="multipart/form-data",
            headers={"Accept": "application/json", "Origin": "http://localhost"},
        )
        self.assertEqual(r_cfg.status_code, 200)
        j_cfg = r_cfg.get_json()
        self.assertTrue(j_cfg["ok"])
        auth_url = j_cfg["authorize_url"]
        self.assertIn("youtube.readonly", auth_url)
        self.assertIn("yt-analytics.readonly", auth_url)
        self.assertNotIn("youtube.upload", auth_url)
        self.assertIn("select_account", auth_url)

        with self.client.session_transaction() as sess:
            saved_state = sess.get("yt_oauth_state")
        self.assertTrue(saved_state)

        # 4) Forged OAuth `state` is rejected
        r_bad_state = self.client.get("/staff/youtube/oauth/callback?code=auth_code_1&state=wrong_state")
        self.assertEqual(r_bad_state.status_code, 302)
        self.assertIn("yt_error=", r_bad_state.headers["Location"])

        # Re-initiate OAuth start to get fresh state
        r_start = self.client.get("/staff/youtube/oauth/start", headers={"Sec-Fetch-Site": "same-origin"})
        self.assertEqual(r_start.status_code, 302)
        with self.client.session_transaction() as sess:
            valid_state = sess.get("yt_oauth_state")

        # 5) Mock Google OAuth token exchange + YouTube Data API v3 + YouTube Analytics API v2
        def fake_post(url, data=None, timeout=15, **kwargs):
            if "oauth2.googleapis.com/token" in url:
                gtype = (data or {}).get("grant_type")
                if gtype == "authorization_code":
                    return _FakeResp(
                        200,
                        {
                            "access_token": "ya29.mock_access_1",
                            "refresh_token": "1//0mock_refresh_token_abc",
                            "expires_in": 3599,
                        },
                    )
                if gtype == "refresh_token":
                    return _FakeResp(200, {"access_token": "ya29.mock_access_2", "expires_in": 3599})
            raise AssertionError(f"Unexpected POST: {url}")

        def fake_get(url, params=None, headers=None, timeout=15, **kwargs):
            p = params or {}
            if url.endswith("/youtube/v3/channels"):
                return _FakeResp(
                    200,
                    {
                        "items": [
                            {
                                "id": "UC_aiseki_navi_brand",
                                "snippet": {
                                    "title": "相席ナビ | 夜の街データ＆もしも検証",
                                    "customUrl": "@aiseki_navi",
                                },
                                "contentDetails": {
                                    "relatedPlaylists": {"uploads": "UU_aiseki_navi_brand"}
                                },
                            }
                        ]
                    },
                )
            if url.endswith("/youtube/v3/playlistItems"):
                return _FakeResp(
                    200,
                    {
                        "items": [
                            {"contentDetails": {"videoId": "dQw4w9WgXcQ"}},
                            {"contentDetails": {"videoId": "AbCdEfGhIjK"}},
                            {"contentDetails": {"videoId": "LoNgViDeO99"}},
                        ]
                    },
                )
            if url.endswith("/youtube/v3/videos"):
                return _FakeResp(
                    200,
                    {
                        "items": [
                            {
                                "id": "dQw4w9WgXcQ",
                                "status": {"privacyStatus": "public"},
                                "snippet": {
                                    "title": "もし相席ラウンジ入店3秒で相手が全員消えたら？ #Shorts",
                                    "description": "25万件データ検証 #shorts",
                                    "publishedAt": "2026-11-14T10:10:00Z",
                                },
                                "contentDetails": {"duration": "PT26S"},
                                "statistics": {"viewCount": "1500", "likeCount": "9", "commentCount": "2"},
                            },
                            {
                                "id": "AbCdEfGhIjK",
                                "status": {"privacyStatus": "public"},
                                "snippet": {
                                    "title": "もし金曜20時に大雨が降ったら男女比はどう変わる？ #Shorts",
                                    "description": "雨天パラドックス検証",
                                    "publishedAt": "2026-11-15T00:05:00Z",
                                },
                                "contentDetails": {"duration": "PT28S"},
                                "statistics": {"viewCount": "85", "likeCount": "4", "commentCount": "0"},
                            },
                            {
                                "id": "LoNgViDeO99",
                                "status": {"privacyStatus": "public"},
                                "snippet": {
                                    "title": "相席ラウンジ完全攻略10分解説",
                                    "description": "長尺動画",
                                    "publishedAt": "2026-11-10T12:00:00Z",
                                },
                                "contentDetails": {"duration": "PT10M12S"},
                                "statistics": {"viewCount": "500", "likeCount": "10", "commentCount": "1"},
                            },
                        ]
                    },
                )
            if "youtubeanalytics.googleapis.com/v2/reports" in url:
                return _FakeResp(
                    200,
                    {
                        "columnHeaders": [
                            {"name": "video"},
                            {"name": "views"},
                            {"name": "engagedViews"},
                            {"name": "estimatedMinutesWatched"},
                            {"name": "averageViewDuration"},
                            {"name": "averageViewPercentage"},
                            {"name": "likes"},
                            {"name": "comments"},
                            {"name": "shares"},
                            {"name": "subscribersGained"},
                        ],
                        "rows": [
                            # Video 1 has full Analytics rows (`330 / 1500 = 22.0%` stayed-to-watch)
                            ["dQw4w9WgXcQ", 1500, 330, 385.0, 15.4, 59.2, 9, 2, 5, 3],
                            # Video 2 has 0 Analytics rows yet (24h reporting lag)
                        ],
                    },
                )
            raise AssertionError(f"Unexpected GET: {url} ({p})")

        with patch("youtube_analytics_sync.requests.post", side_effect=fake_post), patch(
            "youtube_analytics_sync.requests.get", side_effect=fake_get
        ):
            # Chromium Service Worker Navigation Preload must NOT consume one-time `code` or `yt_oauth_state`
            r_preload = self.client.get(
                f"/staff/youtube/oauth/callback?code=valid_code_xyz&state={valid_state}",
                headers={"Service-Worker-Navigation-Preload": "true"},
            )
            self.assertEqual(r_preload.status_code, 204)

            r_cb = self.client.get(f"/staff/youtube/oauth/callback?code=valid_code_xyz&state={valid_state}")
            self.assertEqual(r_cb.status_code, 302)
            self.assertIn("yt_connected=1", r_cb.headers["Location"])
            self.assertIn("yt_synced=2", r_cb.headers["Location"])

            # Duplicate callback with same `code` is idempotent and redirects cleanly instead of `invalid_grant`
            r_dup = self.client.get(f"/staff/youtube/oauth/callback?code=valid_code_xyz&state={valid_state}")
            self.assertEqual(r_dup.status_code, 302)
            self.assertIn("yt_connected=1", r_dup.headers["Location"])

            # Verify `sync_all_sns_insights()` includes `youtube_updated: 2`
            with patch("sns_insights_loop.collect_threads_insights", return_value={"ok": False, "synced": 0}), patch(
                "sns_insights_loop.collect_instagram_insights", return_value={"ok": False, "synced": 0}
            ):
                all_sync = sns_insights_loop.sync_all_sns_insights(limit=15)
            self.assertTrue(all_sync["ok"])
            self.assertEqual(all_sync["summary"]["youtube_updated"], 2)

        fb = sns_insights_loop.compute_loop_feedback(days=28, allow_blocking=True)
        yt_diag = fb["youtube_shorts_diagnostics"]
        # Exactly 2 Shorts (Video 1 upgraded in-place from `yt_moshimo_ep1_vanished` + Video 2; 10-min long video excluded)
        self.assertEqual(yt_diag["sample_count"], 2)
        self.assertEqual(yt_diag["total_views"], 1585)
        self.assertEqual(yt_diag["avg_stayed_to_watch_pct"], 22.0)
        self.assertEqual(yt_diag["avg_pct_viewed"], 59.2)
        self.assertEqual(yt_diag["subscribers_gained"], 3)

        by_views = {s["views"]: s for s in yt_diag["top_shorts"]}
        self.assertEqual(by_views[1500]["stayed_to_watch_pct"], 22.0)
        # Newly uploaded Video 2 has real-time Data API views=85, while unpopulated Analytics metrics stay None (never fabricated)
        self.assertIsNone(by_views[85]["stayed_to_watch_pct"])

        # 6) Verify `/staff/pdca` renders connected status, accessible `<details>`, and modern form attributes
        r_page = self.client.get("/staff/pdca?days=14")
        self.assertEqual(r_page.status_code, 200)
        html = r_page.get_data(as_text=True)
        self.assertIn('id="yt-oauth-setup"', html)
        self.assertIn("API自動同期ON", html)
        self.assertIn("@aiseki_navi", html)
        self.assertIn('command="--copy"', html)
        self.assertIn('commandfor="yt-redirect-uri-code"', html)
        self.assertIn('autocorrect="off"', html)
        self.assertNotIn("1//0mock_refresh_token_abc", html)
        self.assertNotIn("GOCSPX-TestSecret99", html)

        # Ensure no `<h1>`-`<h6>` heading is placed inside `<summary>` (`accordion` guidance)
        summaries = re.findall(r"<summary\b[^>]*>(.*?)</summary>", html, flags=re.I | re.S)
        for s_inner in summaries:
            self.assertIsNone(re.search(r"<h[1-6]\b", s_inner, flags=re.I))


if __name__ == "__main__":
    unittest.main()
