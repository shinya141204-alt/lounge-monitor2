"""
tests/test_voice_narration_reels.py - Unit & Integration tests for ElevenLabs Custom Voice Narration.

Verifies:
1. Japanese TTS phonetic conversion (`to_tts_phonetic_japanese`) & official brand naming (`オリエンタルラウンジ`, `ag`->`アグ`, `JIS`->`ジス`, `XIX`->`ナインティーン`, `茶屋町`->`ちゃやまち`, `%`->`パーセント`, `pt`->`ポイント`).
2. Factual 8.5s narration script generation across all 6 Reel/TikTok templates + zero-metric off-hours honesty.
3. Encrypted/obfuscated `job_state` storage (`token:elevenlabs_api_key`), ElevenLabs API synthesis (`xi-api-key` header), credit-saving disk/shared cache hit on repeat synthesis, and strict fail-open on HTTP 429/5xx.
4. Real FFmpeg 9:16 MP4 muxing with synthetic voice track + sidechain BGM auto-ducking + overlong `atempo` fit (< 8.5s).
5. Master auth & CSRF guards + full workflow across `/api/staff/voice-narration/*` and `/staff/sns-editorial?channel=instagram`.
"""

import array
import datetime
import io
import json
import math
import os
import subprocess
import sys
import tempfile
import unittest
import wave
from unittest import mock

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

os.environ.setdefault("DISABLE_SCHEDULER", "1")
os.environ.setdefault("DISABLE_SHEETS", "1")

import app as app_module
import job_state
import reel_generator
import turbo_store
import voice_narration

JST = datetime.timezone(datetime.timedelta(hours=9))


def _make_synthetic_speech_wav_bytes(duration_sec: float = 3.2, sample_rate: int = 22050) -> bytes:
    """Create a realistic speech-band stereo 16-bit WAV payload for deterministic offline FFmpeg tests."""
    n_frames = int(round(duration_sec * sample_rate))
    pcm = array.array("h", [0] * (n_frames * 2))
    two_pi = 2.0 * math.pi
    for i in range(n_frames):
        t = i / float(sample_rate)
        # Modulated speech-like envelope (syllables at ~5Hz) so sidechaincompress actively ducks BGM
        syllable_env = max(0.0, math.sin(two_pi * 4.5 * t)) ** 0.6
        carrier = 0.55 * math.sin(two_pi * 210.0 * t) + 0.30 * math.sin(two_pi * 420.0 * t)
        val = int(max(-0.9, min(0.9, syllable_env * carrier)) * 22000)
        pcm[i * 2] = val
        pcm[i * 2 + 1] = val
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(2)
        wf.setsampwidth(2)
        wf.setframerate(sample_rate)
        wf.writeframes(pcm.tobytes())
    return buf.getvalue()


class VoiceNarrationReelsTests(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.TemporaryDirectory()
        self._orig_job_state_dir = os.environ.get("JOB_STATE_DIR")
        self._orig_local_file = job_state._LOCAL_FILE
        self._orig_tts_dir = voice_narration.TTS_TMP_CACHE_DIR
        self._orig_env_key = os.environ.pop("ELEVENLABS_API_KEY", None)
        self._orig_env_vid = os.environ.pop("ELEVENLABS_VOICE_ID", None)

        os.environ["JOB_STATE_DIR"] = self._tmpdir.name
        job_state._LOCAL_FILE = os.path.join(self._tmpdir.name, "job_state.json")
        with job_state._lock:
            job_state._keys = None
            job_state._kv = None
            job_state._kv_ts = None

        voice_narration.TTS_TMP_CACHE_DIR = os.path.join(self._tmpdir.name, "tts_cache")
        os.makedirs(voice_narration.TTS_TMP_CACHE_DIR, exist_ok=True)
        reel_generator.clear_reel_mp4_caches()

        self.sample_stores = [
            {"name": "ORIENTAL LOUNGE SHINJUKU", "women": 28, "men": 16, "midnight_retention_pct": 84},
            {"name": "ag SHINJUKU", "women": 8, "men": 20, "midnight_retention_pct": 72},
            {"name": "ORIENTAL LOUNGE UMEDA", "women": 24, "men": 14, "midnight_retention_pct": 81},
            {"name": "JIS UMEDA", "women": 10, "men": 19, "midnight_retention_pct": 79},
            {"name": "ag SHIBUYA", "women": 19, "men": 12, "midnight_retention_pct": 76},
            {"name": "ORIENTAL LOUNGE SHIBUYA", "women": 9, "men": 17, "midnight_retention_pct": 70},
        ]

    def tearDown(self):
        voice_narration.TTS_TMP_CACHE_DIR = self._orig_tts_dir
        if self._orig_job_state_dir is None:
            os.environ.pop("JOB_STATE_DIR", None)
        else:
            os.environ["JOB_STATE_DIR"] = self._orig_job_state_dir
        if self._orig_env_key is not None:
            os.environ["ELEVENLABS_API_KEY"] = self._orig_env_key
        if self._orig_env_vid is not None:
            os.environ["ELEVENLABS_VOICE_ID"] = self._orig_env_vid
        job_state._LOCAL_FILE = self._orig_local_file
        with job_state._lock:
            job_state._keys = None
            job_state._kv = None
            job_state._kv_ts = None
        self._tmpdir.cleanup()

    def test_phonetic_japanese_and_brand_enforcement(self):
        raw = "🔥 オリエンタル新宿とag渋谷、JIS茶屋町・心斎橋・XIX・ASOBIBAR・YATAKOIで2軒目！女性比64%（22pt差）女子2人組も待ち0"
        enforced = voice_narration.enforce_spoken_brand_rule(raw)
        self.assertIn("オリエンタルラウンジ新宿", enforced)
        self.assertIn("ヤタコイ", enforced)
        self.assertNotRegex(enforced, r"オリエンタル(?!ラウンジ)")

        phonetic = voice_narration.to_tts_phonetic_japanese(raw)
        self.assertNotIn("🔥", phonetic)
        self.assertIn("オリエンタルラウンジ新宿", phonetic)
        self.assertIn("アグ渋谷", phonetic)
        self.assertIn("ジスちゃやまち", phonetic)
        self.assertIn("しんさいばし", phonetic)
        self.assertIn("ナインティーン", phonetic)
        self.assertIn("アソビバー", phonetic)
        self.assertIn("ヤタコイ", phonetic)
        self.assertIn("にけんめ", phonetic)
        self.assertIn("64パーセント", phonetic)
        self.assertIn("22ポイント差", phonetic)
        self.assertIn("ふたりぐみ", phonetic)
        self.assertIn("まちゼロ", phonetic)

    def test_all_six_templates_and_offhours_narration_scripts(self):
        ref_dt = datetime.datetime(2026, 11, 13, 21, 15, tzinfo=JST)
        for rt in reel_generator.REEL_TEMPLATES:
            ctx = reel_generator.build_reel_context(self.sample_stores, reel_type=rt, now_jst=ref_dt)
            script = ctx.get("narration_script") or ""
            phonetic = ctx.get("narration_phonetic") or ""
            self.assertTrue(len(script) >= 30, f"{rt} script too short: {script}")
            self.assertTrue(len(script) <= 85, f"{rt} script too long ({len(script)} chars): {script}")
            self.assertIn("相席ナビ", script)
            self.assertNotRegex(script, r"オリエンタル(?!ラウンジ)")
            self.assertTrue(len(phonetic) >= 30)

        # Insufficient / off-hours context must contain zero fabricated headcounts
        empty_ctx = reel_generator.build_reel_context([], reel_type="live_top5", now_jst=ref_dt)
        self.assertEqual(empty_ctx["data_status"], "insufficient")
        off_script = empty_ctx["narration_script"]
        self.assertIn("営業時間外", off_script)
        self.assertNotIn("名", off_script)

    def test_encrypted_key_storage_tts_caching_and_fail_open(self):
        raw_secret = "sk_elevenlabs_unit_test_secret_987654"
        vid = "JBFqnCBsd6RMkjVDRZzb"
        st = voice_narration.set_voice_narration_config(
            api_key=raw_secret,
            voice_id=vid,
            voice_name="My Own Voice",
            enabled=True,
            model_id="eleven_multilingual_v2",
        )
        self.assertTrue(st["configured"])
        self.assertTrue(st["active"])
        self.assertEqual(st["voice_id"], vid)
        self.assertNotIn(raw_secret, json.dumps(st, ensure_ascii=False))

        # Verify raw secret is obfuscated (`obf1:`) on disk, never stored in plaintext
        with open(job_state._LOCAL_FILE, "r", encoding="utf-8") as f:
            disk_raw = f.read()
        self.assertNotIn(raw_secret, disk_raw)
        self.assertIn("obf1:", disk_raw)

        # Reject path-traversal Voice IDs
        with self.assertRaises(ValueError):
            voice_narration.set_voice_narration_config(voice_id="../../etc/passwd")

        fake_mp3_payload = _make_synthetic_speech_wav_bytes(duration_sec=2.6)
        mock_resp_ok = mock.Mock()
        mock_resp_ok.status_code = 200
        mock_resp_ok.content = fake_mp3_payload

        with mock.patch("voice_narration.requests.post", return_value=mock_resp_ok) as m_post:
            res1 = voice_narration.synthesize_narration_audio("今夜の相席速報。1位はag渋谷で女性19名。プロフの相席ナビへ。")
            self.assertTrue(res1["ok"])
            self.assertFalse(res1["cached"])
            self.assertEqual(m_post.call_count, 1)
            called_headers = m_post.call_args.kwargs.get("headers") or {}
            self.assertEqual(called_headers.get("xi-api-key"), raw_secret)
            sent_body = m_post.call_args.kwargs.get("json") or {}
            self.assertIn("アグ渋谷", sent_body.get("text", ""))

            # Second identical synthesis hits disk cache with ZERO API credit usage
            res2 = voice_narration.synthesize_narration_audio("今夜の相席速報。1位はag渋谷で女性19名。プロフの相席ナビへ。")
            self.assertTrue(res2["ok"])
            self.assertTrue(res2["cached"])
            self.assertEqual(m_post.call_count, 1)

        # Simulate HTTP 429 quota exceeded -> fail-open dict without leaking API key
        mock_resp_429 = mock.Mock()
        mock_resp_429.status_code = 429
        mock_resp_429.content = b""
        mock_resp_429.text = f"Quota exceeded for key {raw_secret}"
        with mock.patch("voice_narration.requests.post", return_value=mock_resp_429):
            res_err = voice_narration.synthesize_narration_audio("別パターンの新規テスト文面です。")
            self.assertFalse(res_err["ok"])
            self.assertNotIn(raw_secret, res_err.get("error", ""))

    def test_audio_filter_graph_cache_key_and_pipeline_orchestration(self):
        # 1. Verify normal-length voice (`6.8s <= 8.12s`) builds sidechain auto-ducking without `atempo`
        flt_normal = reel_generator._build_ffmpeg_narration_audio_filter(1, 2, 8.5, 6.8)
        self.assertIn("adelay=120|120", flt_normal)
        self.assertIn("sidechaincompress=", flt_normal)
        self.assertIn("amix=inputs=2", flt_normal)
        self.assertNotIn("atempo=", flt_normal)

        # 2. Verify overlong voice (`9.4s > 8.12s`) automatically injects `atempo=` so final word never clips at 8.5s
        flt_long = reel_generator._build_ffmpeg_narration_audio_filter(1, 2, 8.5, 9.4)
        self.assertIn("atempo=", flt_long)
        self.assertIn("sidechaincompress=", flt_long)

        # 3. Verify MP4 cache keys separate BGM-only vs Voice-enabled vs Custom-script renders
        ref_dt = datetime.datetime(2026, 11, 13, 21, 30, tzinfo=JST)
        bg_info = reel_generator.list_available_backgrounds(reel_type="live_top5", now_jst=ref_dt)
        sig_bgm = voice_narration.get_reel_cache_voice_signature(narration_override=False)
        voice_narration.set_voice_narration_config(
            api_key="sk_cache_sig_test_key_1234",
            voice_id="VoiceCloneAbc999",
            enabled=True,
        )
        sig_auto = voice_narration.get_reel_cache_voice_signature()
        sig_custom = voice_narration.get_reel_cache_voice_signature(custom_narration_text="今夜のカスタム台本です。")
        self.assertEqual(sig_bgm, "bgm_only")
        self.assertNotEqual(sig_auto, sig_bgm)
        self.assertNotEqual(sig_auto, sig_custom)
        k_bgm = reel_generator._compute_reel_cache_key(self.sample_stores, "live_top5", bg_info, now_jst=ref_dt, voice_sig=sig_bgm)
        k_auto = reel_generator._compute_reel_cache_key(self.sample_stores, "live_top5", bg_info, now_jst=ref_dt, voice_sig=sig_auto)
        k_cust = reel_generator._compute_reel_cache_key(self.sample_stores, "live_top5", bg_info, now_jst=ref_dt, voice_sig=sig_custom)
        self.assertEqual(len({k_bgm, k_auto, k_cust}), 3)

        # 4. Verify `render_reel_mp4_bytes` fail-open fallback when ElevenLabs fails -> still renders BGM MP4
        def _fake_encode(ffmpeg_bin, renderer, bg_i, out_path, use_video_bg=False, narration_audio_path=None, narration_duration_sec=0.0):
            tag = b"VOICE_MIX" if narration_audio_path else b"BGM_ONLY"
            with open(out_path, "wb") as f_mp4:
                f_mp4.write(b"\x00\x00\x00\x18ftypmp42" + tag + (b"\x00" * 4096))

        with mock.patch("reel_generator.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             mock.patch("reel_generator._encode_with_ffmpeg", side_effect=_fake_encode), \
             mock.patch("voice_narration.synthesize_narration_audio", return_value={"ok": False, "error": "HTTP 429"}):
            mp4_failopen = reel_generator.render_reel_mp4_bytes(
                self.sample_stores,
                reel_type="live_top5",
                now_jst=ref_dt,
                force_refresh=True,
            )
            self.assertIn(b"BGM_ONLY", mp4_failopen)

        # And when TTS succeeds, `narration_audio_path` is passed into `_encode_with_ffmpeg`
        wav_path = os.path.join(self._tmpdir.name, "synth_ok.wav")
        with open(wav_path, "wb") as f_w:
            f_w.write(_make_synthetic_speech_wav_bytes(duration_sec=4.2))
        with mock.patch("reel_generator.resolve_ffmpeg_bin", return_value="/usr/bin/ffmpeg"), \
             mock.patch("reel_generator._encode_with_ffmpeg", side_effect=_fake_encode), \
             mock.patch("voice_narration.synthesize_narration_audio", return_value={"ok": True, "audio_path": wav_path, "duration_sec": 4.2}):
            mp4_voiced = reel_generator.render_reel_mp4_bytes(
                self.sample_stores,
                reel_type="area_battle",
                now_jst=ref_dt,
                force_refresh=True,
            )
            self.assertIn(b"VOICE_MIX", mp4_voiced)

    def test_ffmpeg_muxing_with_voice_ducking_and_overlong_atempo_fit(self):
        ffmpeg_bin = reel_generator.resolve_ffmpeg_bin()
        if not ffmpeg_bin:
            self.skipTest("ffmpeg binary not installed in test environment")

        # Create an intentionally overlong 9.4s voice WAV to verify `atempo` fits cleanly inside 8.5s MP4
        overlong_wav_path = os.path.join(self._tmpdir.name, "overlong_9_4s.wav")
        with open(overlong_wav_path, "wb") as f:
            f.write(_make_synthetic_speech_wav_bytes(duration_sec=9.4))

        ref_dt = datetime.datetime(2026, 11, 13, 21, 30, tzinfo=JST)
        bgm_only_mp4 = reel_generator.render_reel_mp4_bytes(
            self.sample_stores,
            reel_type="live_top5",
            now_jst=ref_dt,
            force_refresh=True,
            narration_override=False,
        )
        narrated_mp4 = reel_generator.render_reel_mp4_bytes(
            self.sample_stores,
            reel_type="live_top5",
            now_jst=ref_dt,
            force_refresh=True,
            narration_audio_path_override=overlong_wav_path,
        )
        self.assertGreater(len(bgm_only_mp4), 15000)
        self.assertGreater(len(narrated_mp4), 15000)
        self.assertIn(b"ftyp", narrated_mp4[:64])
        self.assertNotEqual(bgm_only_mp4, narrated_mp4)

        out_mp4_file = os.path.join(self._tmpdir.name, "narrated_check.mp4")
        with open(out_mp4_file, "wb") as f:
            f.write(narrated_mp4)

        dur = voice_narration.probe_audio_duration_sec(out_mp4_file, ffmpeg_bin=ffmpeg_bin)
        self.assertGreaterEqual(dur, 8.2)
        self.assertLessEqual(dur, 8.7)

        ffprobe_bin = os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe")
        if os.path.isfile(ffprobe_bin):
            probe = subprocess.run(
                [ffprobe_bin, "-v", "error", "-show_entries", "stream=codec_name,codec_type", "-of", "json", out_mp4_file],
                capture_output=True,
                text=True,
                timeout=6,
            )
            streams = (json.loads(probe.stdout or "{}").get("streams") or [])
            codecs = {s.get("codec_type"): s.get("codec_name") for s in streams}
            self.assertEqual(codecs.get("video"), "h264")
            self.assertEqual(codecs.get("audio"), "aac")

    def test_master_endpoints_csrf_and_studio_ui_rendering(self):
        with app_module.data_lock:
            app_module.latest_data["full_data"] = list(self.sample_stores)
            app_module.latest_data["is_seed"] = False

        client = app_module.app.test_client()

        # Unauthenticated calls must be rejected
        self.assertEqual(client.get("/api/staff/voice-narration/status").status_code, 401)
        self.assertIn(client.post("/api/staff/voice-narration/config", json={"enabled": True}).status_code, (401, 403))
        self.assertEqual(client.get("/api/staff/voice-narration/voices").status_code, 401)
        self.assertIn(client.post("/api/staff/voice-narration/test", json={"text": "テスト"}).status_code, (401, 403))

        with client.session_transaction() as sess:
            sess["is_master"] = True
            sess["staff_authed"] = True

        # Cross-origin POST blocked by CSRF guard
        csrf_res = client.post(
            "/api/staff/voice-narration/config",
            json={"enabled": True},
            headers={"Origin": "https://evil.example"},
        )
        self.assertEqual(csrf_res.status_code, 403)

        # Save config as Master
        cfg_res = client.post(
            "/api/staff/voice-narration/config",
            json={
                "api_key": "sk_master_test_elevenlabs_key_999",
                "voice_id": "VoiceCloneId12345",
                "voice_name": "桑野クローン音声",
                "model_id": "eleven_multilingual_v2",
                "enabled": True,
            },
            headers={"Origin": "http://localhost"},
        )
        self.assertEqual(cfg_res.status_code, 200)
        cfg_body = cfg_res.get_json()
        self.assertTrue(cfg_body["ok"])
        self.assertTrue(cfg_body["voice_narration"]["active"])
        self.assertEqual(cfg_body["voice_narration"]["voice_name"], "桑野クローン音声")

        # Voices endpoint prioritizes cloned voices
        fake_voices_resp = mock.Mock()
        fake_voices_resp.status_code = 200
        fake_voices_resp.json.return_value = {
            "voices": [
                {"voice_id": "PremadeVoice999", "name": "Rachel", "category": "premade"},
                {"voice_id": "VoiceCloneId12345", "name": "桑野クローン音声", "category": "cloned"},
            ]
        }
        with mock.patch("voice_narration.requests.get", return_value=fake_voices_resp):
            v_res = client.post(
                "/api/staff/voice-narration/voices",
                json={},
                headers={"Origin": "http://localhost"},
            )
            self.assertEqual(v_res.status_code, 200)
            v_body = v_res.get_json()
            self.assertTrue(v_body["ok"])
            self.assertEqual(v_body["voices"][0]["voice_id"], "VoiceCloneId12345")
            self.assertTrue(v_body["voices"][0]["is_cloned"])

        # Test audio endpoint returns base64 data URL + phonetic preview
        fake_tts_resp = mock.Mock()
        fake_tts_resp.status_code = 200
        fake_tts_resp.content = _make_synthetic_speech_wav_bytes(duration_sec=2.0)
        with mock.patch("voice_narration.requests.post", return_value=fake_tts_resp):
            t_res = client.post(
                "/api/staff/voice-narration/test",
                json={"reel_type": "tiktok_gap_shock", "text": "同じ駅でもag新宿とオリエンタル新宿で28%差。プロフの相席ナビへ。"},
                headers={"Origin": "http://localhost"},
            )
            self.assertEqual(t_res.status_code, 200)
            t_body = t_res.get_json()
            self.assertTrue(t_body["ok"])
            self.assertTrue(t_body["audio_data_url"].startswith("data:audio/mpeg;base64,"))
            self.assertIn("オリエンタルラウンジ新宿", t_body["phonetic_text"])
            self.assertIn("アグ新宿", t_body["phonetic_text"])

        # Render `/staff/sns-editorial?channel=instagram` and verify Studio UI elements
        page = client.get(
            "/staff/sns-editorial?channel=instagram",
            headers={"Referer": "http://localhost/staff/sns-editorial"},
        )
        self.assertEqual(page.status_code, 200)
        html = page.get_data(as_text=True)
        self.assertIn('id="voice-narration-config-form"', html)
        self.assertIn('id="reel-narration-input"', html)
        self.assertIn('id="vn-sample-script-box"', html)
        self.assertIn("桑野クローン音声", html)


if __name__ == "__main__":
    unittest.main()
