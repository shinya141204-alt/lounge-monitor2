import base64
import datetime
import json
import os
import tempfile
import unittest
from unittest.mock import patch

os.environ["DISABLE_SHEETS"] = "1"
os.environ["DISABLE_SCHEDULER"] = "1"

import app as app_mod
import gcp_vertex_bridge
import job_state
import moshimo_reel_queue
import sns_editorial
import sns_insights_loop
import voice_narration
import youtube_analytics_sync
import youtube_poster

JST = datetime.timezone(datetime.timedelta(hours=9))


class _FakeHttpResp:
    def __init__(self, status_code: int, payload=None, headers=None, raw_bytes: bytes = b""):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.headers = dict(headers or {})
        if raw_bytes:
            self.content = raw_bytes
            self.text = ""
        else:
            self.text = json.dumps(self._payload, ensure_ascii=False)
            self.content = self.text.encode("utf-8")

    def json(self):
        return self._payload


class TestGcpCreditsAndYouTubeAutoPost(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._prev_job_dir = os.environ.get("JOB_STATE_DIR")
        self._prev_gemini_key = os.environ.get("GEMINI_API_KEY")
        self._prev_claude_key = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["JOB_STATE_DIR"] = self._tmpdir.name
        os.environ.pop("GEMINI_API_KEY", None)
        os.environ.pop("ANTHROPIC_API_KEY", None)

        job_state.reset_cache_for_tests()
        sns_insights_loop.reset_insights_loop_cache_for_tests()
        gcp_vertex_bridge.reset_gcp_bridge_state_for_tests()
        sns_editorial.clear_editorial_ai_cache()

        app_mod.app.config["TESTING"] = True
        self.client = app_mod.app.test_client()

    def tearDown(self):
        gcp_vertex_bridge.reset_gcp_bridge_state_for_tests()
        if self._prev_job_dir is None:
            os.environ.pop("JOB_STATE_DIR", None)
        else:
            os.environ["JOB_STATE_DIR"] = self._prev_job_dir
        if self._prev_gemini_key is None:
            os.environ.pop("GEMINI_API_KEY", None)
        else:
            os.environ["GEMINI_API_KEY"] = self._prev_gemini_key
        if self._prev_claude_key is None:
            os.environ.pop("ANTHROPIC_API_KEY", None)
        else:
            os.environ["ANTHROPIC_API_KEY"] = self._prev_claude_key
        job_state.reset_cache_for_tests()
        sns_insights_loop.reset_insights_loop_cache_for_tests()
        self._tmpdir.cleanup()

    def _login_master(self):
        with self.client.session_transaction() as sess:
            sess["is_master"] = True
            sess["staff_authed"] = True

    def _enable_mock_gcp_sa(self):
        gcp_vertex_bridge._test_credentials_override = {
            "project_id": "loungemonitor",
            "client_email": "lounge-monitor-bot@loungemonitor.iam.gserviceaccount.com",
        }
        gcp_vertex_bridge._test_token_override = "ya29.mock_gcp_sa_token"

    def test_unit_test_isolation_is_active_by_default(self):
        self.assertTrue(gcp_vertex_bridge._is_unit_test_isolated())
        self.assertFalse(gcp_vertex_bridge.is_gcp_service_account_configured())
        self.assertFalse(gcp_vertex_bridge.is_vertex_ai_enabled())
        self.assertEqual(gcp_vertex_bridge.get_gcp_tts_mode(), "off")

    def test_vertex_ai_priority_routing_and_circuit_breaker_failover_to_ai_studio(self):
        self._enable_mock_gcp_sa()
        gcp_vertex_bridge.update_gcp_credit_settings(vertex_enabled=True)
        sns_editorial.set_ai_engine_pref("gemini")

        calls = []

        def fake_vertex_ok(url, headers=None, json=None, timeout=12, **kw):
            calls.append(url)
            self.assertIn("aiplatform.googleapis.com", url)
            self.assertIn("projects/loungemonitor/locations/global", url)
            self.assertEqual((headers or {}).get("Authorization"), "Bearer ya29.mock_gcp_sa_token")
            return _FakeHttpResp(
                200,
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [
                                    {"text": '{"main_text": "金曜の梅田、女性先行で動いてますね。", "editorial_angle": "test"}'}
                                ]
                            }
                        }
                    ]
                },
            )

        # 1) Works on GCP Vertex AI credits even with ZERO `GEMINI_API_KEY`
        with patch("gcp_vertex_bridge.requests.post", side_effect=fake_vertex_ok):
            parsed, model_used, err = sns_editorial.call_editorial_ai_json(
                prompt="テスト投稿を生成して",
                prefer_fast=True,
            )
        self.assertEqual(err, "")
        self.assertTrue(model_used.startswith("vertex:"))
        self.assertIn("梅田", parsed["main_text"])
        self.assertEqual(len(calls), 1)

        # 2) If Vertex AI returns 403 (credits exhausted or API disabled), immediately fails over to AI Studio key
        sns_editorial.set_gemini_api_key("AIzaSyMockFallbackKey123456789")

        def fake_vertex_403_then_aistudio_ok(url, **kw):
            if "aiplatform.googleapis.com" in url:
                return _FakeHttpResp(403, {"error": {"message": "Billing account disabled"}})
            self.assertIn("generativelanguage.googleapis.com", url)
            return _FakeHttpResp(
                200,
                {
                    "candidates": [
                        {
                            "content": {
                                "parts": [{"text": '{"main_text": "AI Studio予備ルート成功"}'}]
                            }
                        }
                    ]
                },
            )

        with (
            patch("requests.post", side_effect=fake_vertex_403_then_aistudio_ok),
            patch("sns_editorial._discover_ai_studio_models", return_value=["gemini-2.5-flash"]),
        ):
            parsed2, model2, err2 = sns_editorial.call_editorial_ai_json(
                prompt="フォールバック確認",
                prefer_fast=True,
            )
        self.assertEqual(err2, "")
        self.assertEqual(parsed2["main_text"], "AI Studio予備ルート成功")
        self.assertEqual(model2, "gemini-2.5-flash")
        st_after = gcp_vertex_bridge.get_gcp_credit_bridge_status()
        self.assertGreater(st_after["vertex_cooldown_sec"], 0)

    def test_gcp_cloud_tts_primary_and_elevenlabs_429_fallback(self):
        self._enable_mock_gcp_sa()
        fake_mp3 = b"ID3" + (b"\x00\xff\xfb\x90" * 400)  # > 1600 bytes valid-sized payload
        b64_mp3 = base64.b64encode(fake_mp3).decode("ascii")

        def fake_cloud_tts(url, headers=None, json=None, timeout=14, **kw):
            self.assertEqual(url, gcp_vertex_bridge.CLOUD_TTS_ENDPOINT)
            self.assertEqual((headers or {}).get("Authorization"), "Bearer ya29.mock_gcp_sa_token")
            return _FakeHttpResp(200, {"audioContent": b64_mp3})

        # 1) `gcp_primary` mode uses Cloud TTS directly with zero ElevenLabs key
        gcp_vertex_bridge.update_gcp_credit_settings(tts_mode="gcp_primary", tts_voice="ja-JP-Chirp3-HD-Puck")
        with (
            patch("requests.post", side_effect=fake_cloud_tts) as mock_gcp_tts,
            patch.object(voice_narration, "TTS_TMP_CACHE_DIR", self._tmpdir.name),
        ):
            res_primary = voice_narration.synthesize_narration_audio(
                "今夜の1位はJIS梅田で女性42名。",
                force_refresh=True,
            )
            self.assertTrue(res_primary["ok"])
            self.assertEqual(res_primary["provider"], "gcp_cloud_tts")
            self.assertEqual(res_primary["voice_id"], "ja-JP-Chirp3-HD-Puck")
            self.assertEqual(mock_gcp_tts.call_count, 1)

        # 2) `auto_fallback` mode falls back to Cloud TTS when ElevenLabs returns HTTP 429
        gcp_vertex_bridge.update_gcp_credit_settings(tts_mode="auto_fallback", tts_voice="ja-JP-Neural2-C")
        voice_narration.set_voice_narration_config(
            api_key="sk_mock_elevenlabs_key_12345678",
            voice_id="VoiceIdMock12345",
            enabled=True,
        )
        tts_calls = []

        def fake_el_429_then_gcp_tts(url, headers=None, **kw):
            tts_calls.append(url)
            if "api.elevenlabs.io" in url:
                return _FakeHttpResp(429, {"detail": "quota_exceeded"})
            self.assertEqual(url, gcp_vertex_bridge.CLOUD_TTS_ENDPOINT)
            return _FakeHttpResp(200, {"audioContent": b64_mp3})

        with (
            patch("requests.post", side_effect=fake_el_429_then_gcp_tts),
            patch.object(voice_narration, "TTS_TMP_CACHE_DIR", self._tmpdir.name),
        ):
            res_fb = voice_narration.synthesize_narration_audio(
                "2軒目速報、オリエンタルラウンジ新宿が活況です。",
                force_refresh=True,
            )
            self.assertTrue(res_fb["ok"])
            self.assertEqual(res_fb["provider"], "gcp_cloud_tts")
            self.assertEqual(res_fb["voice_id"], "ja-JP-Neural2-C")
            self.assertTrue(any("api.elevenlabs.io" in u for u in tts_calls))
            self.assertTrue(any("texttospeech.googleapis.com" in u for u in tts_calls))

    def test_youtube_title_description_sanitization_and_resumable_upload(self):
        # Verify `<`, `>`, brand rule (`OLG` -> `オリエンタルラウンジ`), `/link/` stripping, and `#Shorts`
        dirty_title = "OLG新宿 <検証> もし女性無料が消えたら？\n改行テスト"
        clean_t = youtube_poster.sanitize_youtube_title(dirty_title)
        self.assertNotIn("<", clean_t)
        self.assertNotIn(">", clean_t)
        self.assertIn("オリエンタルラウンジ新宿", clean_t)
        self.assertLessEqual(len(clean_t), 100)

        dirty_desc = "詳細 <こちら> https://aiseki-navi.com/link/pcmax?src=yt"
        clean_d = youtube_poster.sanitize_youtube_description(dirty_desc, title_for_check=clean_t)
        self.assertNotIn("<", clean_d)
        self.assertNotIn(">", clean_d)
        self.assertNotIn("/link/", clean_d)
        self.assertIn("#Shorts", clean_d)

        # Configure OAuth with `youtube.upload` scope
        youtube_analytics_sync.save_oauth_client_credentials(
            "500762809036-test.apps.googleusercontent.com",
            "GOCSPX-TestSecretUpload123",
        )
        auth_url, err = youtube_analytics_sync.build_oauth_authorize_url(
            "https://aiseki-navi.com/staff/youtube/oauth/callback",
            "state_xyz",
            include_upload=True,
        )
        self.assertEqual(err, "")
        self.assertIn("youtube.upload", auth_url)

        def fake_oauth_post(url, data=None, **kw):
            if "oauth2.googleapis.com/token" in url:
                return _FakeHttpResp(
                    200,
                    {
                        "access_token": "ya29.upload_tok",
                        "refresh_token": "1//0refresh_upload",
                        "scope": " ".join(youtube_analytics_sync.YOUTUBE_FULL_SCOPES),
                    },
                )
            raise AssertionError(f"Unexpected oauth POST: {url}")

        def fake_ch_get(url, **kw):
            return _FakeHttpResp(
                200,
                {
                    "items": [
                        {
                            "id": "UCAWNbuYB19Hk32J_YM57iNg",
                            "snippet": {"title": "相席ナビ", "customUrl": "@aiseki_navi"},
                            "contentDetails": {"relatedPlaylists": {"uploads": "UUAWNbuYB19Hk32J_YM57iNg"}},
                        }
                    ]
                },
            )

        with (
            patch("youtube_analytics_sync.requests.post", side_effect=fake_oauth_post),
            patch("youtube_analytics_sync.requests.get", side_effect=fake_ch_get),
        ):
            ex = youtube_analytics_sync.exchange_code_and_save(
                code="code_upload_ok",
                redirect_uri="https://aiseki-navi.com/staff/youtube/oauth/callback",
                requested_upload=True,
            )
        self.assertTrue(ex["ok"])
        self.assertTrue(ex["has_upload_scope"])

        # Seed #1 and #2 as already live on `@aiseki_navi` in `sns_insights_loop`
        sns_insights_loop.ingest_manual_insight_records(
            {
                "posts": [
                    {
                        "channel": "youtube_shorts",
                        "post_id": "yt_Ep1VidId123",
                        "opener": "もし日本から相席ラウンジが消えたら？金曜23時の実測データで検証 #もしもシリーズ",
                        "views": 1128,
                    },
                    {
                        "channel": "youtube_shorts",
                        "post_id": "yt_Ep2VidId456",
                        "opener": "もし日本からマッチングアプリが消えたら？夜の街に起きること #もしもシリーズ",
                        "views": 253,
                    },
                ]
            }
        )

        # Verify #1 and #2 are recognized as already posted and skipped by scheduler!
        ep1 = moshimo_reel_queue.get_episode_by_id("ep1_vanished")
        ep2 = moshimo_reel_queue.get_episode_by_id("ep2_matching_app")
        self.assertTrue(youtube_poster.find_synced_youtube_video_for_episode(ep1)[0])
        self.assertTrue(youtube_poster.find_synced_youtube_video_for_episode(ep2)[0])

        # On 2026-11-18 20:05 JST, exact match `ep4_last_train` is selected first!
        wed_1118 = datetime.datetime(2026, 11, 18, 20, 5, tzinfo=JST)
        due_ep = youtube_poster.select_due_youtube_moshimo_episode(now_jst=wed_1118)
        self.assertIsNotNone(due_ep)
        self.assertEqual(due_ep["ep_id"], "ep4_last_train")

        # Execute full 2-step Resumable Upload and verify `locked_as_private` detection
        put_bytes_len = []

        def fake_combined_yt_post(url, params=None, headers=None, data=None, **kw):
            if "oauth2.googleapis.com/token" in url:
                return fake_oauth_post(url, data=data, **kw)
            self.assertIn("/upload/youtube/v3/videos", url)
            self.assertEqual((params or {}).get("uploadType"), "resumable")
            body_json = json.loads(data.decode("utf-8"))
            self.assertEqual(body_json["status"]["privacyStatus"], "public")
            self.assertFalse(body_json["status"]["selfDeclaredMadeForKids"])
            return _FakeHttpResp(
                200,
                {},
                headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=session_abc123"},
            )

        def fake_yt_upload_put(url, data=None, headers=None, **kw):
            self.assertIn("upload_id=session_abc123", url)
            chunk = data.read() if hasattr(data, "read") else bytes(data)
            put_bytes_len.append(len(chunk))
            return _FakeHttpResp(
                200,
                {
                    "id": "YtShortEp4X",
                    "status": {
                        "uploadStatus": "uploaded",
                        "privacyStatus": "private",
                        "rejectionReason": "lockedAsPrivate",
                    },
                },
            )

        with (
            patch("requests.post", side_effect=fake_combined_yt_post),
            patch("requests.put", side_effect=fake_yt_upload_put),
        ):
            pub_res = youtube_poster.dispatch_scheduled_youtube_short(
                stores=[],
                slot_label="20:00",
                now_jst=wed_1118,
            )

        self.assertTrue(pub_res["ok"])
        self.assertEqual(pub_res["status"], "locked_private")
        self.assertEqual(pub_res["ep_id"], "ep4_last_train")
        self.assertEqual(pub_res["video_id"], "YtShortEp4X")
        self.assertTrue(pub_res["privacy_locked"])
        # H-3: Auto-post must automatically pause when Google locks an upload as private!
        self.assertFalse(youtube_poster.is_youtube_auto_post_enabled(allow_blocking=True))
        self.assertEqual(len(put_bytes_len), 1)
        self.assertGreater(put_bytes_len[0], 1_000_000)

        # Re-enable and verify second dispatch on same slot is deduplicated
        youtube_poster.set_youtube_auto_post_enabled(True)
        dup_res = youtube_poster.dispatch_scheduled_youtube_short(
            stores=[],
            slot_label="20:00",
            now_jst=wed_1118,
        )
        self.assertEqual(dup_res["status"], "skipped")

    def test_fable_hardening_guards_recovery_isolation_and_path_traversal(self):
        # 1) H-5: Unmocked upload in test environment is strictly blocked before any network I/O
        ep3 = moshimo_reel_queue.get_episode_by_id("ep3_women_free")
        mp4_p = moshimo_reel_queue.resolve_episode_local_path(ep3)
        blocked = youtube_poster.upload_short_video_to_youtube(mp4_p, "title", "desc")
        self.assertFalse(blocked["ok"])
        self.assertEqual(blocked["status"], "blocked_in_test")

        # 2) M-10: Path traversal in custom episode `static_rel_path` is neutralized
        trav_path = moshimo_reel_queue.resolve_episode_local_path({"static_rel_path": "/static/reels/moshimo/../../../app.py"})
        self.assertFalse(os.path.isfile(trav_path))
        self.assertIn("__invalid_path__.mp4", trav_path)

        # 3) H-2: Resumable upload recovery rescues `video_id` when initial PUT raises TimeoutError
        youtube_analytics_sync.save_oauth_client_credentials(
            "500762809036-test.apps.googleusercontent.com",
            "GOCSPX-TestSecretUpload123",
        )
        job_state.set_value(youtube_analytics_sync.YOUTUBE_REFRESH_TOKEN_STATE_KEY, "1//0rt_rec")
        youtube_analytics_sync._merge_and_save_meta({
            "client_id": "500762809036-test.apps.googleusercontent.com",
            "channel_id": "UCAWNbuYB19Hk32J_YM57iNg",
            "granted_scopes": list(youtube_analytics_sync.YOUTUBE_FULL_SCOPES),
            "has_upload_scope": True,
        })

        put_attempt = {"n": 0}

        def fake_post_rec(url, **kw):
            if "oauth2.googleapis.com/token" in url:
                return _FakeHttpResp(200, {"access_token": "ya29.rec_tok"})
            return _FakeHttpResp(
                200,
                {},
                headers={"Location": "https://www.googleapis.com/upload/youtube/v3/videos?upload_id=recover_99"},
            )

        def fake_put_timeout_then_recover(url, headers=None, **kw):
            put_attempt["n"] += 1
            if put_attempt["n"] == 1:
                raise TimeoutError("Simulated stream read timeout after final byte")
            self.assertIn("bytes */", (headers or {}).get("Content-Range", ""))
            return _FakeHttpResp(
                200,
                {"id": "RecVid12345", "status": {"uploadStatus": "uploaded", "privacyStatus": "public"}},
            )

        with (
            patch("requests.post", side_effect=fake_post_rec),
            patch("requests.put", side_effect=fake_put_timeout_then_recover),
        ):
            rec_res = youtube_poster.upload_short_video_to_youtube(
                mp4_p,
                "リカバリー検証 #Shorts",
                "概要欄テスト",
                session_key_id="ep3_test",
            )
        self.assertTrue(rec_res["ok"])
        self.assertEqual(rec_res["video_id"], "RecVid12345")
        self.assertFalse(rec_res["privacy_locked"])

        # 4) M-8: Channel pinning blocks accidental authorization of a different Google account's channel
        with (
            patch("requests.post", return_value=_FakeHttpResp(200, {"access_token": "ya29.other", "refresh_token": "1//0other"})),
            patch(
                "requests.get",
                return_value=_FakeHttpResp(
                    200,
                    {"items": [{"id": "UC_WRONG_PERSONAL_CH", "snippet": {"title": "Personal"}, "contentDetails": {"relatedPlaylists": {"uploads": "UU_X"}}}]},
                ),
            ),
        ):
            wrong_ch = youtube_analytics_sync.exchange_code_and_save(
                code="code_wrong",
                redirect_uri="https://aiseki-navi.com/staff/youtube/oauth/callback",
                requested_upload=True,
            )
        self.assertFalse(wrong_ch["ok"])
        self.assertIn("異なるアカウント", wrong_ch["error"])

    def test_master_routes_csrf_and_dashboard_rendering(self):
        # Unauthenticated requests must return 401
        self.assertEqual(self.client.get("/api/pdca/gcp-credits").status_code, 401)
        self.assertEqual(self.client.post("/api/pdca/youtube/auto-post", json={"enabled": True}).status_code, 401)

        self._login_master()
        self._enable_mock_gcp_sa()

        # Cross-site POST blocked with 403
        r_csrf = self.client.post(
            "/api/pdca/gcp-credits",
            json={"vertex_enabled": False},
            headers={"Sec-Fetch-Site": "cross-site", "Origin": "https://evil.example"},
        )
        self.assertEqual(r_csrf.status_code, 403)

        # Same-origin Master POST updates settings cleanly
        r_gcp = self.client.post(
            "/api/pdca/gcp-credits",
            json={"vertex_enabled": True, "tts_mode": "gcp_primary", "tts_voice": "ja-JP-Chirp3-HD-Aoede"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r_gcp.status_code, 200)
        j_gcp = r_gcp.get_json()
        self.assertTrue(j_gcp["ok"])
        self.assertEqual(j_gcp["gcp_status"]["tts_mode"], "gcp_primary")
        self.assertEqual(j_gcp["gcp_status"]["tts_voice"], "ja-JP-Chirp3-HD-Aoede")

        r_yt_set = self.client.post(
            "/api/pdca/youtube/auto-post",
            json={"enabled": True, "privacy_status": "unlisted"},
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(r_yt_set.status_code, 200)
        self.assertEqual(r_yt_set.get_json()["youtube_auto_post"]["privacy_status"], "unlisted")

        # Both Master dashboards render 200 OK with new panels
        r_pdca = self.client.get("/staff/pdca")
        self.assertEqual(r_pdca.status_code, 200)
        html_pdca = r_pdca.get_data(as_text=True)
        self.assertIn("gcp-credits-setup", html_pdca)
        self.assertIn("GCPクレジット優先消化モード", html_pdca)

        r_sns = self.client.get("/staff/sns-editorial?channel=instagram")
        self.assertEqual(r_sns.status_code, 200)
        html_sns = r_sns.get_data(as_text=True)
        self.assertIn("moshimo-publish-yt-btn", html_sns)
        self.assertIn("平日20:00 YouTube自動公開", html_sns)
        self.assertIn("GCPクレジット優先消化ON", html_sns)

    def test_ep3_hard_pinned_and_gsc_auto_sync_cohort_decomposition(self):
        # 1) #1, #2, and #3 (`ep3_women_free` -> `D-UDwCqzxm8`) are all hard-pinned in production runtime
        with patch.dict(os.environ, {"DISABLE_SHEETS": "0"}):
            for ep_id, expected_vid in (
                ("ep1_vanished", "6g-Rc0tTy_Y"),
                ("ep2_matching_app", "FQrXvYkdfno"),
                ("ep3_women_free", "D-UDwCqzxm8"),
            ):
                ep = moshimo_reel_queue.get_episode_by_id(ep_id)
                found, vid, _ = youtube_poster.find_synced_youtube_video_for_episode(ep)
                self.assertTrue(found)
                self.assertEqual(vid, expected_vid)

            # Even on Tuesday 2026-11-17 (scheduled date of ep3), catch-up will NOT re-select ep3;
            # on Wednesday 2026-11-18, next due episode is ep4_last_train
            tue_1117 = datetime.datetime(2026, 11, 17, 20, 15, tzinfo=JST)
            self.assertIsNone(youtube_poster.select_due_youtube_moshimo_episode(now_jst=tue_1117))
            wed_1118 = datetime.datetime(2026, 11, 18, 20, 15, tzinfo=JST)
            nxt = youtube_poster.select_due_youtube_moshimo_episode(now_jst=wed_1118)
            self.assertIsNotNone(nxt)
            self.assertEqual(nxt["ep_id"], "ep4_last_train")

        # Also verify `youtube_title` matching ("もし相席ラウンジの「女性無料」が有料になったらどうなる？") works even in DISABLE_SHEETS=1
        sns_insights_loop.ingest_manual_insight_records({
            "posts": [{
                "channel": "youtube_shorts",
                "post_id": "yt_D-UDwCqzxm8",
                "opener": "もし相席ラウンジの「女性無料」が有料になったらどうなる？ #もしもシリーズ #相席ラウンジ",
                "views": 359,
            }]
        })
        ep3 = moshimo_reel_queue.get_episode_by_id("ep3_women_free")
        found_yt_title, vid_yt_title, _ = youtube_poster.find_synced_youtube_video_for_episode(ep3)
        self.assertTrue(found_yt_title)
        self.assertEqual(vid_yt_title, "D-UDwCqzxm8")
        wed_1118 = datetime.datetime(2026, 11, 18, 20, 15, tzinfo=JST)

        # 2) Unmocked `collect_gsc_insights` is strictly isolated in unit tests (`DISABLE_SHEETS=1`)
        iso_res = sns_insights_loop.collect_gsc_insights(days=28, now_jst=wed_1118)
        self.assertFalse(iso_res["ok"])
        self.assertEqual(iso_res["status"], "test_isolated")

        # 3) Mocked official Search Console API sync computes un-thresholded `date` totals + W1/W2 cohort split
        self._enable_mock_gcp_sa()

        def fake_gsc_get(url, **kw):
            self.assertIn("/webmasters/v3/sites", url)
            return _FakeHttpResp(200, {"siteEntry": [{"siteUrl": "https://aiseki-navi.com/", "permissionLevel": "siteRestrictedUser"}]})

        def fake_gsc_post(url, json=None, **kw):
            self.assertIn("/searchAnalytics/query", url)
            self.assertEqual(json.get("dataState"), "final")
            dims = json.get("dimensions") or []
            s_d, e_d = json.get("startDate"), json.get("endDate")
            if dims == ["date"]:
                return _FakeHttpResp(200, {"rows": [
                    {"keys": ["2026-11-05"], "clicks": 35, "impressions": 444, "ctr": 0.0788, "position": 11.6},
                    {"keys": ["2026-11-14"], "clicks": 33, "impressions": 672, "ctr": 0.0491, "position": 14.5},
                ]})
            if dims == ["query"]:
                return _FakeHttpResp(200, {"rows": [
                    {"keys": ["jis 新宿 人数"], "clicks": 0, "impressions": 24, "ctr": 0.0, "position": 9.5},
                    {"keys": ["ag 人数"], "clicks": 4, "impressions": 15, "ctr": 0.2667, "position": 4.3},
                ]})
            if dims == ["page"]:
                if s_d == "2026-11-03" and e_d == "2026-11-09":
                    return _FakeHttpResp(200, {"rows": [
                        {"keys": ["https://aiseki-navi.com/store/jis-shinjuku"], "clicks": 20, "impressions": 200, "ctr": 0.10, "position": 9.0},
                    ]})
                if s_d == "2026-11-10" and e_d == "2026-11-16":
                    return _FakeHttpResp(200, {"rows": [
                        {"keys": ["https://aiseki-navi.com/store/jis-shinjuku"], "clicks": 12, "impressions": 180, "ctr": 0.0667, "position": 11.0},
                        {"keys": ["https://aiseki-navi.com/jis"], "clicks": 6, "impressions": 140, "ctr": 0.0429, "position": 5.6},
                    ]})
                return _FakeHttpResp(200, {"rows": [
                    {"keys": ["https://aiseki-navi.com/jis"], "clicks": 0, "impressions": 21, "ctr": 0.0, "position": 5.57},
                ]})
            return _FakeHttpResp(200, {"rows": []})

        with (
            patch("gcp_vertex_bridge.get_gcp_access_token", return_value=("ya29.gsc_tok", "")),
            patch("sns_insights_loop.requests.get", side_effect=fake_gsc_get),
            patch("sns_insights_loop.requests.post", side_effect=fake_gsc_post),
        ):
            gsc_res = sns_insights_loop.collect_gsc_insights(days=28, now_jst=wed_1118)

        self.assertTrue(gsc_res["ok"])
        self.assertEqual(gsc_res["finalized_through_date"], "2026-11-16")
        fb = sns_insights_loop.compute_loop_feedback(days=28, allow_blocking=True)
        gd = fb["gsc_diagnostics"]
        self.assertEqual(gd["source"], "gsc_api_auto")
        self.assertEqual(gd["total_clicks"], 68)
        self.assertEqual(gd["total_impressions"], 1116)
        self.assertEqual(gd["w1_clicks"], 35)
        self.assertEqual(gd["w2_clicks"], 33)
        self.assertEqual(gd["continuing_pages_count"], 1)
        self.assertEqual(gd["continuing_w1_clicks"], 20)
        self.assertEqual(gd["continuing_w2_clicks"], 12)
        self.assertEqual(gd["new_pages_count"], 1)
        self.assertEqual(gd["new_w2_clicks"], 6)


if __name__ == "__main__":
    unittest.main()
