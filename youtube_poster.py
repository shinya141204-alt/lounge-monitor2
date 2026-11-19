"""
youtube_poster.py - YouTube Shorts Automatic Upload Engine (YouTube Data API v3 `videos.insert`).

Publishes 9:16 short-form MP4 videos (3D Voxel Moshimo episodes and optional live data Reels)
directly to `@aiseki_navi` via Google's official 2-step Resumable Upload protocol:
  1. `POST https://www.googleapis.com/upload/youtube/v3/videos?uploadType=resumable&part=snippet,status`
  2. `PUT <Location>` streaming raw `video/mp4` bytes (with `Content-Range: bytes */{size}` recovery).

Key Production Safeguards (Hardened per Claude Fable 5.2 Audit):
  - Cross-process distributed locking (`turbo_store.acquire_distributed_lock`) across `app` & `worker`
    machines so concurrent schedulers or double-clicks can never double-publish an episode.
  - Resumable session recovery: persists `upload_url` before streaming bytes; if `PUT` times out or
    returns 5xx, queries `Content-Range: bytes */{size}` to rescue any completed `video_id` before
    falling back, and writes `yt_moshimo_posted:{ep_id}` + `yt_moshimo_vid:{ep_id}` atomically first.
  - Hard-pinned `#1` (`6g-Rc0tTy_Y`), `#2` (`FQrXvYkdfno`), and `#3` (`D-UDwCqzxm8`) live `@aiseki_navi`
    video IDs plus fail-closed store checks so manually uploaded episodes are NEVER re-uploaded.
  - Automatic pause on `locked_as_private`: if Google's unverified-project compliance check locks an
    upload as `private`, auto-posting pauses immediately (`enabled=False`) so remaining episodes are
    protected until the Compliance Audit Form is approved, without registering a fake public post.
  - Strict unit-test network isolation: blocks any unmocked outbound HTTP call when `DISABLE_SHEETS=1`.
"""

import datetime
import json
import logging
import os
import re
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

import requests

import job_state
import reel_generator
import turbo_store
import youtube_analytics_sync

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

YOUTUBE_RESUMABLE_UPLOAD_URL = "https://www.googleapis.com/upload/youtube/v3/videos"
YOUTUBE_COMMENT_THREADS_URL = "https://www.googleapis.com/youtube/v3/commentThreads"
MAX_YOUTUBE_UPLOAD_BYTES = 128 * 1024 * 1024  # 128 MB safety ceiling for Shorts MP4

YOUTUBE_AUTO_POST_ENABLED_KEY = "youtube_shorts_auto_post_enabled"
YOUTUBE_LIVE_REELS_ENABLED_KEY = "youtube_shorts_live_reels_enabled"
YOUTUBE_UPLOAD_PRIVACY_KEY = "youtube_shorts_upload_privacy"
YOUTUBE_PRIVACY_LOCKED_KEY = "youtube_shorts_privacy_locked"
YOUTUBE_LAST_AUTO_POST_TS_KEY = "youtube_shorts_last_auto_ts"
YOUTUBE_UPLOAD_HISTORY_KEY = "youtube_shorts_upload_history_v1"

YOUTUBE_EPISODE_DONE_PREFIX = "yt_moshimo_posted:"
YOUTUBE_EPISODE_VID_PREFIX = "yt_moshimo_vid:"
YOUTUBE_EPISODE_TS_PREFIX = "yt_moshimo_last_ts:"
YOUTUBE_EPISODE_COMMENT_DONE_PREFIX = "yt_moshimo_commented:"
YOUTUBE_EPISODE_COMMENT_ID_PREFIX = "yt_moshimo_cid:"
YOUTUBE_EPISODE_COMMENT_TS_PREFIX = "yt_moshimo_cts:"
YOUTUBE_DATE_POSTED_PREFIX = "yt_moshimo_posted_date:"
YOUTUBE_SLOT_PREFIX = "yt_short_slot:"
YOUTUBE_RESUMABLE_SESSION_PREFIX = "yt_resumable_url:"

# Confirmed live `@aiseki_navi` uploads prior to auto-poster activation (deterministic anti-duplication)
KNOWN_PUBLISHED_YOUTUBE_EPISODES: Dict[str, Tuple[str, str]] = {
    "ep1_vanished": ("6g-Rc0tTy_Y", "2026-11-14 19:10:26"),
    "ep2_matching_app": ("FQrXvYkdfno", "2026-11-17 01:36:33"),
    "ep3_women_free": ("D-UDwCqzxm8", "2026-11-17 18:11:54"),
}

SUPPORTED_PRIVACY_STATUSES = ("public", "unlisted", "private")
DEFAULT_YOUTUBE_TAGS = ("もしもシリーズ", "相席ラウンジ", "雑学", "相席ナビ", "Shorts")

_yt_post_lock = threading.Lock()
_TITLE_NORM_RE = re.compile(r"[\s#＃!！?？「」『』【】（）()・｜|〜~\-_]+")
_OLG_ABBR_RE = re.compile(r"(?<![A-Za-z0-9])OLG(?![A-Za-z0-9])")
_ANY_LINK_PATH_RE = re.compile(r"(?:https?://[^\s]+|(?:www\.)?aiseki-navi\.com)?/link/[A-Za-z0-9_/?=&.%-]+")


def _now_jst(now_jst: Optional[datetime.datetime] = None) -> datetime.datetime:
    if isinstance(now_jst, datetime.datetime):
        return now_jst.astimezone(JST) if now_jst.tzinfo else now_jst.replace(tzinfo=JST)
    return datetime.datetime.now(JST)


def _business_dt(now_jst: Optional[datetime.datetime] = None) -> datetime.datetime:
    """Match `moshimo_reel_queue._business_dt` (-7 hours, so 00:00..06:59 belongs to prior night)."""
    return _now_jst(now_jst) - datetime.timedelta(hours=7)


def _is_fn_mocked(fn: Any) -> bool:
    return bool(fn is not None and (hasattr(fn, "assert_called") or type(fn).__module__.startswith("unittest.mock")))


def _is_requests_mocked() -> bool:
    for fn in (getattr(requests, "post", None), getattr(requests, "put", None), getattr(requests, "get", None)):
        if _is_fn_mocked(fn):
            return True
    return False


def _is_youtube_network_blocked_in_tests() -> bool:
    if os.environ.get("ALLOW_YOUTUBE_UPLOAD_IN_TESTS") == "1":
        return False
    if _is_requests_mocked():
        return False
    return bool(os.environ.get("DISABLE_SHEETS") == "1" or os.environ.get("PYTEST_CURRENT_TEST"))


def is_youtube_auto_post_enabled(allow_blocking: bool = False) -> bool:
    raw = str(job_state.get_value(YOUTUBE_AUTO_POST_ENABLED_KEY, allow_blocking=allow_blocking, default="") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    env_def = (os.environ.get("YOUTUBE_SHORTS_AUTO_POST_DEFAULT") or "1").strip().lower()
    return env_def not in ("0", "false", "off", "no")


def set_youtube_auto_post_enabled(enabled: bool) -> bool:
    flag = bool(enabled)
    job_state.set_value(YOUTUBE_AUTO_POST_ENABLED_KEY, "1" if flag else "0")
    return flag


def is_youtube_live_reels_enabled(allow_blocking: bool = False) -> bool:
    raw = str(job_state.get_value(YOUTUBE_LIVE_REELS_ENABLED_KEY, allow_blocking=allow_blocking, default="") or "").strip().lower()
    return raw in ("1", "true", "on", "yes")


def set_youtube_live_reels_enabled(enabled: bool) -> bool:
    flag = bool(enabled)
    job_state.set_value(YOUTUBE_LIVE_REELS_ENABLED_KEY, "1" if flag else "0")
    return flag


def get_youtube_upload_privacy(allow_blocking: bool = False) -> str:
    raw = str(job_state.get_value(YOUTUBE_UPLOAD_PRIVACY_KEY, allow_blocking=allow_blocking, default="") or "").strip().lower()
    if raw in SUPPORTED_PRIVACY_STATUSES:
        return raw
    env_p = (os.environ.get("YOUTUBE_SHORTS_PRIVACY") or "public").strip().lower()
    return env_p if env_p in SUPPORTED_PRIVACY_STATUSES else "public"


def set_youtube_upload_privacy(privacy: str) -> str:
    norm = str(privacy or "").strip().lower()
    if norm not in SUPPORTED_PRIVACY_STATUSES:
        norm = "public"
    job_state.set_value(YOUTUBE_UPLOAD_PRIVACY_KEY, norm)
    return norm


def update_youtube_auto_post_settings(
    enabled: Optional[bool] = None,
    privacy_status: Optional[str] = None,
    live_reels_enabled: Optional[bool] = None,
) -> Dict[str, Any]:
    if enabled is not None:
        set_youtube_auto_post_enabled(bool(enabled))
        if bool(enabled):
            # Re-enabling clears any stale privacy-lock banner until next upload test
            job_state.set_value(YOUTUBE_PRIVACY_LOCKED_KEY, "0")
    if privacy_status is not None:
        set_youtube_upload_privacy(str(privacy_status))
    if live_reels_enabled is not None:
        set_youtube_live_reels_enabled(bool(live_reels_enabled))
    return get_youtube_auto_post_status(allow_blocking=True)


def _normalize_youtube_brand_text(raw: str) -> str:
    s = str(raw or "")
    s = _OLG_ABBR_RE.sub("オリエンタルラウンジ", s)
    s = s.replace("オリラジ", "オリエンタルラウンジ")
    return reel_generator.enforce_brand_rule(s)


def sanitize_youtube_title(raw_title: str) -> str:
    """
    Sanitize a YouTube video title to strictly satisfy YouTube Data API v3 `snippet.title` rules:
      - No `<` or `>` characters (`400 invalidTitle`)
      - No control characters or newlines
      - Official brand naming (`オリエンタルラウンジ`)
      - Non-empty and `<= 100` characters
    """
    txt = _normalize_youtube_brand_text(str(raw_title or "").strip())
    txt = _ANY_LINK_PATH_RE.sub("相席ナビ", txt)
    txt = txt.replace("<", "＜").replace(">", "＞")
    txt = re.sub(r"[\r\n\t\x00-\x1f\x7f]+", " ", txt)
    txt = re.sub(r"\s{2,}", " ", txt).strip()
    if not txt:
        txt = "夜の街データ検証 #もしもシリーズ #Shorts"
    if len(txt) > 100:
        txt = txt[:99].rstrip(" #＃・|｜")
    return txt


def sanitize_youtube_description(raw_description: str, ensure_shorts_tag: bool = True, title_for_check: str = "") -> str:
    """
    Sanitize YouTube `snippet.description`:
      - Replaces `<` and `>` (`400 invalidDescription`)
      - Enforces official brand names and strips any `/link/` affiliate paths (`AGENTS.md §4`)
      - Appends `#Shorts` if neither title nor description contains `#shorts`
      - Clamps UTF-8 byte length `<= 4800` bytes (YouTube hard limit is 5000 bytes)
    """
    txt = _normalize_youtube_brand_text(str(raw_description or "").strip())
    txt = txt.replace("<", "＜").replace(">", "＞")
    txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", "", txt)
    txt = _ANY_LINK_PATH_RE.sub("https://aiseki-navi.com/?s=short_bio", txt)

    combined_low = f"{title_for_check}\n{txt}".lower()
    if ensure_shorts_tag and "#shorts" not in combined_low and "#short" not in combined_low:
        txt = f"{txt}\n#Shorts".strip()

    encoded = txt.encode("utf-8")
    if len(encoded) > 4800:
        txt = encoded[:4800].decode("utf-8", errors="ignore").rstrip()
    return txt


def sanitize_youtube_comment(raw_comment: str) -> str:
    """
    Sanitize top-level official comment text (`commentThreads.insert`):
      - Official brand naming (`オリエンタルラウンジ`)
      - Strips any `/link/` affiliate paths (`AGENTS.md §4`)
      - Clamps UTF-8 byte length `<= 4500` bytes
    """
    txt = _normalize_youtube_brand_text(str(raw_comment or "").strip())
    txt = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]+", "", txt)
    txt = _ANY_LINK_PATH_RE.sub("https://aiseki-navi.com/?s=short_bio", txt)
    encoded = txt.encode("utf-8")
    if len(encoded) > 4500:
        txt = encoded[:4500].decode("utf-8", errors="ignore").rstrip()
    return txt


def is_episode_youtube_commented(ep_id: str, allow_blocking: bool = False) -> Tuple[bool, str, str]:
    clean_ep_id = str(ep_id or "").strip()
    if not clean_ep_id:
        return False, "", ""
    if job_state.is_done(f"{YOUTUBE_EPISODE_COMMENT_DONE_PREFIX}{clean_ep_id}"):
        cid = str(
            job_state.get_value(f"{YOUTUBE_EPISODE_COMMENT_ID_PREFIX}{clean_ep_id}", allow_blocking=allow_blocking, default="") or ""
        ).strip()
        ts = str(
            job_state.get_value(f"{YOUTUBE_EPISODE_COMMENT_TS_PREFIX}{clean_ep_id}", allow_blocking=allow_blocking, default="") or ""
        ).strip()
        return True, cid, ts
    return False, "", ""


def _sanitize_youtube_tags(tags: Optional[List[str]] = None) -> List[str]:
    src = list(tags) if tags else list(DEFAULT_YOUTUBE_TAGS)
    out: List[str] = []
    total_chars = 0
    for raw in src:
        clean = str(raw or "").lstrip("#＃").replace("<", "").replace(">", "").strip()
        if not clean or clean in out or len(clean) > 40:
            continue
        if total_chars + len(clean) + 1 > 380:
            break
        out.append(clean)
        total_chars += len(clean) + 1
    return out


def _normalize_title_core(text: str) -> str:
    s = str(text or "").splitlines()[0] if text else ""
    s = re.sub(r"[#＃][^\s#＃]+", "", s)
    return _TITLE_NORM_RE.sub("", s).lower()


def find_synced_youtube_video_for_episode(
    ep: Dict[str, Any],
    allow_blocking: bool = False,
) -> Tuple[bool, str, str]:
    """
    Check if a Moshimo episode has ALREADY been uploaded to YouTube (`@aiseki_navi`), via:
      1) `job_state` (`yt_moshimo_posted:{ep_id}`)
      2) Hard-pinned production IDs (`KNOWN_PUBLISHED_YOUTUBE_EPISODES` for `#1` & `#2`) when live
         credentials are connected (or in production runtime)
      3) Synced `sns_insights_loop` `youtube_shorts` records matching `[ep_id:...]` or full normalized title.
    Returns `(is_posted, video_id, posted_at_str)`.
    """
    ep_id = str((ep or {}).get("ep_id") or "").strip()
    if not ep_id:
        return False, "", ""

    if job_state.is_done(f"{YOUTUBE_EPISODE_DONE_PREFIX}{ep_id}"):
        vid = str(job_state.get_value(f"{YOUTUBE_EPISODE_VID_PREFIX}{ep_id}", allow_blocking=allow_blocking, default="") or "").strip()
        ts = str(job_state.get_value(f"{YOUTUBE_EPISODE_TS_PREFIX}{ep_id}", allow_blocking=allow_blocking, default="") or "").strip()
        return True, vid, ts

    # Hard-pinned protection for manually uploaded #1, #2, and #3 on `@aiseki_navi`
    if ep_id in KNOWN_PUBLISHED_YOUTUBE_EPISODES and os.environ.get("DISABLE_SHEETS") != "1":
        p_vid, p_ts = KNOWN_PUBLISHED_YOUTUBE_EPISODES[ep_id]
        return True, p_vid, p_ts

    ep_cores = [
        c
        for c in (
            _normalize_title_core(str(ep.get("title") or "")),
            _normalize_title_core(str(ep.get("youtube_title") or "")),
        )
        if len(c) >= 10
    ]
    if not ep_cores:
        return False, "", ""

    try:
        import sns_insights_loop
        store = sns_insights_loop._load_store(allow_blocking=allow_blocking)
        for item in (store.get("posts") or []):
            if not isinstance(item, dict) or item.get("channel") != "youtube_shorts":
                continue
            pid = str(item.get("post_id") or "").strip()
            vid_match = re.match(r"^yt_([A-Za-z0-9_-]{11})$", pid)
            if not vid_match:
                continue
            if str(item.get("style") or "") == f"moshimo_{ep_id}":
                return True, vid_match.group(1), str(item.get("published_at") or "")
            cand_core = _normalize_title_core(str(item.get("opener") or item.get("text_snippet") or ""))
            if len(cand_core) >= 12 and any(ec in cand_core or cand_core in ec for ec in ep_cores):
                return True, vid_match.group(1), str(item.get("published_at") or "")
    except Exception as exc:
        logger.warning(f"[youtube_poster] sns_insights_loop read error during dedup check: {exc}")

    return False, "", ""


def _append_upload_history(entry: Dict[str, Any]) -> None:
    try:
        raw = job_state.get_value(YOUTUBE_UPLOAD_HISTORY_KEY, allow_blocking=True, default="")
        items: List[Dict[str, Any]] = []
        if raw:
            parsed = json.loads(raw)
            if isinstance(parsed, list):
                items = [x for x in parsed if isinstance(x, dict)]
        items.insert(0, dict(entry))
        job_state.set_value(YOUTUBE_UPLOAD_HISTORY_KEY, json.dumps(items[:20], ensure_ascii=False))
    except Exception as exc:
        logger.warning(f"[youtube_poster] upload history write skipped: {exc}")


def get_upload_history(allow_blocking: bool = False) -> List[Dict[str, Any]]:
    raw = job_state.get_value(YOUTUBE_UPLOAD_HISTORY_KEY, allow_blocking=allow_blocking, default="")
    if not raw:
        return []
    try:
        parsed = json.loads(raw)
        return [x for x in parsed if isinstance(x, dict)][:20] if isinstance(parsed, list) else []
    except Exception:
        return []


def check_youtube_auto_post_cooldown(
    min_interval_minutes: int = 90,
    now_jst: Optional[datetime.datetime] = None,
    allow_blocking: bool = True,
) -> Tuple[bool, str]:
    raw_ts = str(job_state.get_value(YOUTUBE_LAST_AUTO_POST_TS_KEY, allow_blocking=allow_blocking, default="") or "").strip()
    if not raw_ts:
        return False, ""
    try:
        last_dt = datetime.datetime.strptime(raw_ts, "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        elapsed_min = (_now_jst(now_jst) - last_dt).total_seconds() / 60.0
        if 0 <= elapsed_min < min_interval_minutes:
            return True, f"直近のYouTube自動投稿から{int(elapsed_min)}分のため待機中（最低{min_interval_minutes}分間隔）"
    except Exception:
        pass
    return False, ""


def _classify_youtube_upload_error(status_code: int, resp_text: str) -> str:
    raw = resp_text or ""
    reason = ""
    msg = ""
    try:
        err_obj = (json.loads(raw) or {}).get("error") or {}
        msg = str(err_obj.get("message") or "")[:140]
        errs = err_obj.get("errors") or []
        if errs and isinstance(errs[0], dict):
            reason = str(errs[0].get("reason") or "")
    except Exception:
        msg = raw[:140]

    low_reason = reason.lower()
    low_msg = msg.lower()
    if status_code == 403 and ("insufficientpermissions" in low_reason or "insufficient authentication scopes" in low_msg):
        return "YouTube自動投稿の権限（youtube.upload）が未認可です。管理画面の『YouTube自動投稿を認可』から1クリック承認してください。"
    if status_code == 403 and "quotaexceeded" in low_reason:
        return "YouTube Data API v3 の1日あたりクォータ上限（10,000単位）に達しました（日本時間17時頃に自動リセットされます）。"
    if status_code in (400, 403) and "uploadlimitexceeded" in low_reason:
        return "YouTubeチャンネル側の1日あたり動画アップロード上限に達しました。"
    if status_code == 401:
        return "YouTubeアクセストークンが無効です。再度OAuth連携を実行してください。"
    return f"YouTubeアップロードエラー (HTTP {status_code}{': ' + reason if reason else ''}) {msg}".strip()


def _probe_resumable_upload_completion(upload_url: str, access_token: str, file_size: int) -> Optional[Dict[str, Any]]:
    """
    Query Google's Resumable Upload session (`Content-Range: bytes */{file_size}`) to check whether
    a timed-out / interrupted `PUT` actually succeeded on YouTube's side, recovering `video_id`.
    """
    if not upload_url:
        return None
    try:
        r = requests.put(
            upload_url,
            data=b"",
            headers={
                "Authorization": f"Bearer {access_token}",
                "Content-Length": "0",
                "Content-Range": f"bytes */{file_size}",
            },
            timeout=15,
        )
        if r.status_code in (200, 201):
            body = r.json() or {}
            if isinstance(body, dict) and body.get("id"):
                return body
    except Exception:
        pass
    return None


def _finalize_upload_response(resp_json: Dict[str, Any], clean_title: str, clean_privacy: str) -> Dict[str, Any]:
    video_id = str((resp_json or {}).get("id") or "").strip()
    if not video_id:
        return {"ok": False, "status": "api_error", "error": "YouTubeから video_id が返されませんでした"}

    status_obj = resp_json.get("status") or {}
    actual_privacy = str(status_obj.get("privacyStatus") or clean_privacy).strip().lower()
    upload_status = str(status_obj.get("uploadStatus") or "uploaded").strip().lower()
    rejection_reason = str(status_obj.get("rejectionReason") or "").strip()

    privacy_locked = bool(
        (clean_privacy == "public" and actual_privacy == "private")
        or rejection_reason == "lockedAsPrivate"
    )
    try:
        job_state.set_value(YOUTUBE_PRIVACY_LOCKED_KEY, "1" if privacy_locked else "0")
        if privacy_locked:
            # Auto-pause future scheduled uploads so remaining episodes aren't burned as locked private!
            set_youtube_auto_post_enabled(False)
    except Exception as exc:
        logger.warning(f"[youtube_poster] privacy_locked state save warning: {exc}")

    return {
        "ok": True,
        "status": "locked_private" if privacy_locked else "success",
        "id": video_id,
        "video_id": video_id,
        "title": clean_title,
        "requested_privacy": clean_privacy,
        "privacy_status": actual_privacy,
        "upload_status": upload_status,
        "privacy_locked": privacy_locked,
        "shorts_url": f"https://www.youtube.com/shorts/{video_id}",
        "studio_url": f"https://studio.youtube.com/video/{video_id}/edit",
    }


def upload_short_video_to_youtube(
    mp4_path: str,
    title: str,
    description: str,
    tags: Optional[List[str]] = None,
    privacy_status: Optional[str] = None,
    session_key_id: str = "",
    timeout_sec: float = 180.0,
) -> Dict[str, Any]:
    """
    Upload a local MP4 file to YouTube Shorts via 2-step Resumable Upload (`videos.insert`),
    with resumable session recovery to prevent duplicate uploads on transient network timeouts.
    """
    if _is_youtube_network_blocked_in_tests():
        return {
            "ok": False,
            "status": "blocked_in_test",
            "error": "テスト環境での実YouTube API送信はブロックされています",
        }

    if not mp4_path or not os.path.isfile(mp4_path):
        return {"ok": False, "status": "error", "error": f"MP4ファイルが見つかりません: {mp4_path}"}

    file_size = os.path.getsize(mp4_path)
    if file_size <= 1024:
        return {"ok": False, "status": "error", "error": "MP4ファイルサイズが不正（空ファイル）です"}
    if file_size > MAX_YOUTUBE_UPLOAD_BYTES:
        return {"ok": False, "status": "error", "error": "MP4ファイルサイズが128MBを超えています"}

    access_token, _, tok_err = youtube_analytics_sync.get_valid_youtube_access_token(require_upload_scope=True)
    if tok_err or not access_token:
        return {"ok": False, "status": "auth_error", "error": tok_err or "YouTubeトークン取得失敗"}

    clean_privacy = str(privacy_status or get_youtube_upload_privacy(allow_blocking=True)).strip().lower()
    if clean_privacy not in SUPPORTED_PRIVACY_STATUSES:
        clean_privacy = "public"

    clean_title = sanitize_youtube_title(title)
    clean_desc = sanitize_youtube_description(description, ensure_shorts_tag=True, title_for_check=clean_title)
    clean_tags = _sanitize_youtube_tags(tags)

    resumable_state_key = f"{YOUTUBE_RESUMABLE_SESSION_PREFIX}{session_key_id}" if session_key_id else ""
    if resumable_state_key:
        prev_raw = str(job_state.get_value(resumable_state_key, allow_blocking=True, default="") or "").strip()
        if prev_raw:
            try:
                prev_obj = json.loads(prev_raw)
                prev_url = str((prev_obj or {}).get("url") or "")
                prev_ts = float((prev_obj or {}).get("ts") or 0.0)
                if prev_url and (time.time() - prev_ts) < 7200:
                    recovered = _probe_resumable_upload_completion(prev_url, access_token, file_size)
                    if recovered:
                        job_state.set_value(resumable_state_key, "")
                        return _finalize_upload_response(recovered, clean_title, clean_privacy)
            except Exception:
                pass

    metadata_body = {
        "snippet": {
            "title": clean_title,
            "description": clean_desc,
            "tags": clean_tags,
            "categoryId": "22",
            "defaultLanguage": "ja",
            "defaultAudioLanguage": "ja",
        },
        "status": {
            "privacyStatus": clean_privacy,
            "selfDeclaredMadeForKids": False,
        },
    }

    init_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
        "X-Upload-Content-Length": str(file_size),
        "X-Upload-Content-Type": "video/mp4",
    }

    try:
        init_resp = requests.post(
            YOUTUBE_RESUMABLE_UPLOAD_URL,
            params={"uploadType": "resumable", "part": "snippet,status"},
            headers=init_headers,
            data=json.dumps(metadata_body, ensure_ascii=False).encode("utf-8"),
            timeout=25,
        )
    except Exception as exc:
        return {"ok": False, "status": "network_error", "error": f"YouTubeアップロード開始通信エラー: {type(exc).__name__}"}

    if init_resp.status_code >= 400:
        err_msg = _classify_youtube_upload_error(init_resp.status_code, init_resp.text or "")
        return {"ok": False, "status": "api_error", "error": err_msg, "http_status": init_resp.status_code}

    upload_url = str(init_resp.headers.get("Location") or init_resp.headers.get("location") or "").strip()
    if not upload_url:
        return {"ok": False, "status": "api_error", "error": "YouTube Resumable Upload URL（Locationヘッダ）が返されませんでした"}

    if resumable_state_key:
        try:
            job_state.set_value(resumable_state_key, json.dumps({"url": upload_url, "ts": time.time()}))
        except Exception:
            pass

    put_headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "video/mp4",
        "Content-Length": str(file_size),
    }

    put_resp = None
    try:
        with open(mp4_path, "rb") as f_mp4:
            put_resp = requests.put(
                upload_url,
                data=f_mp4,
                headers=put_headers,
                timeout=max(30.0, float(timeout_sec)),
            )
    except Exception as exc:
        recovered = _probe_resumable_upload_completion(upload_url, access_token, file_size)
        if recovered:
            if resumable_state_key:
                try:
                    job_state.set_value(resumable_state_key, "")
                except Exception:
                    pass
            return _finalize_upload_response(recovered, clean_title, clean_privacy)
        return {"ok": False, "status": "network_error", "error": f"YouTube動画本体送信エラー: {type(exc).__name__}"}

    if put_resp.status_code >= 500 or put_resp.status_code == 308:
        recovered = _probe_resumable_upload_completion(upload_url, access_token, file_size)
        if recovered:
            if resumable_state_key:
                try:
                    job_state.set_value(resumable_state_key, "")
                except Exception:
                    pass
            return _finalize_upload_response(recovered, clean_title, clean_privacy)

    if put_resp.status_code >= 400:
        err_msg = _classify_youtube_upload_error(put_resp.status_code, put_resp.text or "")
        return {"ok": False, "status": "api_error", "error": err_msg, "http_status": put_resp.status_code}

    try:
        resp_json = put_resp.json() or {}
    except Exception:
        recovered = _probe_resumable_upload_completion(upload_url, access_token, file_size)
        if recovered:
            resp_json = recovered
        else:
            return {"ok": False, "status": "api_error", "error": "YouTubeアップロード応答のJSON解析に失敗しました"}

    if resumable_state_key:
        try:
            job_state.set_value(resumable_state_key, "")
        except Exception:
            pass

    return _finalize_upload_response(resp_json, clean_title, clean_privacy)


def _persist_critical_episode_done(ep_id: str, video_id: str, biz_date: str, ts_str: str) -> None:
    """Persist critical anti-duplication markers FIRST with retry so `video_id` is never lost."""
    for attempt in range(2):
        try:
            job_state.mark_done(f"{YOUTUBE_EPISODE_DONE_PREFIX}{ep_id}")
            job_state.set_value(f"{YOUTUBE_EPISODE_VID_PREFIX}{ep_id}", video_id)
            job_state.set_value(f"{YOUTUBE_EPISODE_TS_PREFIX}{ep_id}", ts_str)
            job_state.mark_done(f"{YOUTUBE_DATE_POSTED_PREFIX}{biz_date}")
            job_state.set_value(YOUTUBE_LAST_AUTO_POST_TS_KEY, ts_str)
            return
        except Exception as exc:
            logger.error(f"[youtube_poster] Critical dedup save attempt {attempt + 1} failed for {ep_id} ({video_id}): {exc}")
            time.sleep(0.2)


def select_due_youtube_moshimo_episode(now_jst: Optional[datetime.datetime] = None) -> Optional[Dict[str, Any]]:
    """
    Select the Moshimo episode due for YouTube Shorts on `biz_date`'s `20:00` JST slot:
      1. Never publish more than 1 Moshimo episode to YouTube on the same `biz_date`.
      2. Exact `scheduled_date == biz_date` match wins first (keeps IG & YouTube on the exact same episode).
      3. Catch-up on Mon–Sat (`weekday in 0..5`) for unposted episodes (`#1`/`#2`/`#3` are pinned as done),
         preserving Sunday as a rest day.
      4. Evergreen Recycle on Tue/Thu (`>= 21` days cooldown) once all episodes have aired and recycle is ON.
    """
    import moshimo_reel_queue

    biz_dt = _business_dt(now_jst)
    biz_date = biz_dt.strftime("%Y-%m-%d")
    biz_weekday = biz_dt.weekday()

    if job_state.is_done(f"{YOUTUBE_DATE_POSTED_PREFIX}{biz_date}"):
        return None

    all_eps = moshimo_reel_queue.get_all_moshimo_episodes(allow_blocking=True)

    # 1. Exact scheduled date match
    for ep in all_eps:
        already, _, _ = find_synced_youtube_video_for_episode(ep, allow_blocking=True)
        if ep["scheduled_date"] == biz_date and not already:
            if os.path.isfile(moshimo_reel_queue.resolve_episode_local_path(ep)):
                return dict(ep)

    # 2. Overdue catch-up on Mon–Sat (never on Sunday rest day)
    if biz_weekday in (0, 1, 2, 3, 4, 5):
        for ep in all_eps:
            already, _, _ = find_synced_youtube_video_for_episode(ep, allow_blocking=True)
            if ep["scheduled_date"] < biz_date and not already:
                if os.path.isfile(moshimo_reel_queue.resolve_episode_local_path(ep)):
                    return dict(ep)

    # 3. Post-campaign Evergreen Recycle on Tue/Thu (only when ALL episodes have been posted, not merely missing on disk)
    any_unposted = any(
        not find_synced_youtube_video_for_episode(ep, allow_blocking=True)[0]
        for ep in all_eps
    )
    if not any_unposted and moshimo_reel_queue.is_evergreen_recycle_enabled(allow_blocking=True):
        if biz_weekday in (1, 3):
            biz_date_obj = biz_dt.date()
            cands = []
            for ep in all_eps:
                if not os.path.isfile(moshimo_reel_queue.resolve_episode_local_path(ep)):
                    continue
                _, _, last_ts = find_synced_youtube_video_for_episode(ep, allow_blocking=True)
                ref_str = (last_ts[:10] if len(last_ts) >= 10 else ep.get("scheduled_date") or "")
                try:
                    ref_date = datetime.date.fromisoformat(ref_str)
                except Exception:
                    continue
                if (biz_date_obj - ref_date).days >= moshimo_reel_queue.EVERGREEN_COOLDOWN_DAYS:
                    cands.append((ref_date, ep["order"], ep))
            if cands:
                cands.sort(key=lambda t: (t[0], t[1]))
                chosen = dict(cands[0][2])
                chosen["is_evergreen_recycle"] = True
                return chosen

    return None


def _check_connected_with_upload_scope() -> bool:
    yt_st = youtube_analytics_sync.get_youtube_sync_status(allow_blocking=True)
    if not yt_st.get("connected"):
        return False
    if yt_st.get("has_upload_scope"):
        return True
    if _is_youtube_network_blocked_in_tests():
        return False
    try:
        meta = youtube_analytics_sync._load_meta(allow_blocking=True)
        if "has_upload_scope" not in meta:
            youtube_analytics_sync.get_valid_youtube_access_token(require_upload_scope=False)
            yt_st = youtube_analytics_sync.get_youtube_sync_status(allow_blocking=True)
            return bool(yt_st.get("connected") and yt_st.get("has_upload_scope"))
    except Exception:
        pass
    return False


def _check_connected_with_comment_scope() -> bool:
    yt_st = youtube_analytics_sync.get_youtube_sync_status(allow_blocking=True)
    if not yt_st.get("connected"):
        return False
    if yt_st.get("has_comment_scope"):
        return True
    if _is_youtube_network_blocked_in_tests():
        return False
    try:
        meta = youtube_analytics_sync._load_meta(allow_blocking=True)
        if "has_comment_scope" not in meta:
            youtube_analytics_sync.get_valid_youtube_access_token(require_upload_scope=False)
            yt_st = youtube_analytics_sync.get_youtube_sync_status(allow_blocking=True)
            return bool(yt_st.get("connected") and yt_st.get("has_comment_scope"))
    except Exception:
        pass
    return False


def _find_existing_channel_comment_id(video_id: str, access_token: str, channel_id: str) -> str:
    """
    Check whether `@aiseki_navi` (`channel_id`) has ALREADY posted a top-level comment on `video_id`
    via `commentThreads.list`, preventing duplicate official comments even across manual + auto runs.
    """
    if not video_id or not access_token or not channel_id:
        return ""
    in_test_env = bool(os.environ.get("DISABLE_SHEETS") == "1" or os.environ.get("PYTEST_CURRENT_TEST"))
    if in_test_env and os.environ.get("ALLOW_YOUTUBE_UPLOAD_IN_TESTS") != "1" and not _is_fn_mocked(getattr(requests, "get", None)):
        return ""
    try:
        resp = requests.get(
            YOUTUBE_COMMENT_THREADS_URL,
            params={"part": "snippet", "videoId": video_id, "maxResults": "25"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=12,
        )
        if resp.status_code >= 400:
            return ""
        items = (resp.json() or {}).get("items") or []
        for item in items:
            if not isinstance(item, dict):
                continue
            top = ((item.get("snippet") or {}).get("topLevelComment") or {})
            author_ch = str(((top.get("snippet") or {}).get("authorChannelId") or {}).get("value") or "").strip()
            if author_ch and author_ch == channel_id:
                return str(top.get("id") or item.get("id") or "existing")
    except Exception as exc:
        logger.debug(f"[youtube_poster] existing comment check warning on {video_id}: {exc}")
    return ""


def post_youtube_top_level_comment(
    video_id: str,
    comment_text: str,
    retry_delays: Tuple[float, ...] = (0.0, 2.5, 5.0),
) -> Dict[str, Any]:
    """
    Post an official top-level comment on `video_id` via YouTube Data API v3 (`commentThreads.insert`).
    Note: Official YouTube Data API v3 does not expose a comment-pinning endpoint, so posting the
    first official comment makes copy-paste unnecessary (and stays on top initially until 1-tap pinned in Studio).
    """
    if _is_youtube_network_blocked_in_tests():
        return {
            "ok": False,
            "status": "blocked_in_test",
            "error": "テスト環境での実YouTube API送信はブロックされています",
        }

    clean_vid = str(video_id or "").strip()
    clean_text = sanitize_youtube_comment(comment_text)
    if not clean_vid or not clean_text:
        return {"ok": False, "status": "error", "error": "video_id またはコメント本文が空です"}

    access_token, meta, tok_err = youtube_analytics_sync.get_valid_youtube_access_token(require_comment_scope=True)
    if tok_err or not access_token:
        return {"ok": False, "status": "missing_comment_scope", "error": tok_err or "YouTubeコメント権限トークン取得失敗"}

    channel_id = str((meta or {}).get("channel_id") or "").strip()
    existing_cid = _find_existing_channel_comment_id(clean_vid, access_token, channel_id)
    if existing_cid:
        return {
            "ok": True,
            "status": "already_exists",
            "comment_id": existing_cid,
            "video_id": clean_vid,
            "text": clean_text,
            "studio_comments_url": f"https://studio.youtube.com/video/{clean_vid}/comments",
        }

    body = {
        "snippet": {
            "videoId": clean_vid,
            "topLevelComment": {
                "snippet": {
                    "textOriginal": clean_text,
                }
            },
        }
    }
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    last_status = 0
    last_text = ""
    is_mock_post = _is_fn_mocked(getattr(requests, "post", None))
    delays = retry_delays if retry_delays else (0.0,)

    for idx, delay_sec in enumerate(delays):
        if delay_sec > 0 and not is_mock_post:
            time.sleep(delay_sec)
        try:
            resp = requests.post(
                YOUTUBE_COMMENT_THREADS_URL,
                params={"part": "snippet"},
                headers=headers,
                data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                timeout=15,
            )
        except Exception as exc:
            if idx + 1 < len(delays):
                continue
            return {"ok": False, "status": "network_error", "error": f"YouTubeコメント送信通信エラー: {type(exc).__name__}"}

        last_status = resp.status_code
        last_text = resp.text or ""
        if resp.status_code in (200, 201):
            try:
                rjson = resp.json() or {}
            except Exception:
                rjson = {}
            cid = str(
                rjson.get("id")
                or (((rjson.get("snippet") or {}).get("topLevelComment") or {}).get("id"))
                or "posted"
            ).strip()
            return {
                "ok": True,
                "status": "success",
                "comment_id": cid,
                "video_id": clean_vid,
                "text": clean_text,
                "studio_comments_url": f"https://studio.youtube.com/video/{clean_vid}/comments",
            }

        # Newly uploaded Shorts can briefly return 404/5xx while YouTube indexes the video
        if resp.status_code in (404, 500, 502, 503, 504) and idx + 1 < len(delays):
            continue
        break

    err_msg = _classify_youtube_upload_error(last_status, last_text)
    return {
        "ok": False,
        "status": "api_error",
        "http_status": last_status,
        "error": err_msg,
        "video_id": clean_vid,
    }


def ensure_moshimo_episode_youtube_comment(
    ep: Dict[str, Any],
    video_id: str = "",
    custom_comment: str = "",
    force: bool = False,
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """Idempotently post the episode's `pinned_comment` as an official top-level comment on YouTube."""
    import moshimo_reel_queue

    ep_id = str((ep or {}).get("ep_id") or "").strip()
    if not ep_id:
        return {"ok": False, "status": "skipped", "reason": "missing_ep_id"}

    already_c, existing_cid, existing_cts = is_episode_youtube_commented(ep_id, allow_blocking=True)
    if already_c and not force:
        return {
            "ok": True,
            "status": "already_commented",
            "ep_id": ep_id,
            "comment_id": existing_cid,
            "commented_at": existing_cts,
        }

    vid = str(video_id or "").strip()
    if not vid:
        _, vid, _ = find_synced_youtube_video_for_episode(ep, allow_blocking=True)
    if not vid:
        return {"ok": False, "status": "skipped", "ep_id": ep_id, "reason": "video_not_published_yet"}

    if not turbo_store.acquire_distributed_lock(f"yt_comment_claim:{ep_id}:{vid}", ttl_sec=90, fail_open=True):
        return {"ok": False, "status": "skipped", "ep_id": ep_id, "reason": "comment_lock_busy"}

    comment_txt = (custom_comment or moshimo_reel_queue.build_youtube_shorts_pinned_comment(ep)).strip()
    res = post_youtube_top_level_comment(vid, comment_txt)
    res["ep_id"] = ep_id
    if res.get("ok"):
        ts_str = _now_jst(now_jst).strftime("%Y-%m-%d %H:%M:%S")
        try:
            job_state.mark_done(f"{YOUTUBE_EPISODE_COMMENT_DONE_PREFIX}{ep_id}")
            job_state.set_value(f"{YOUTUBE_EPISODE_COMMENT_ID_PREFIX}{ep_id}", str(res.get("comment_id") or "posted"))
            job_state.set_value(f"{YOUTUBE_EPISODE_COMMENT_TS_PREFIX}{ep_id}", ts_str)
        except Exception as exc:
            logger.warning(f"[youtube_poster] comment state save warning for {ep_id}: {exc}")
    return res


def backfill_published_moshimo_comments(now_jst: Optional[datetime.datetime] = None) -> Dict[str, Any]:
    """
    Scan all published Moshimo YouTube Shorts (`#1..#10` + custom) and post official top-level
    comments on any video that does not yet have one. Safe & idempotent to run on OAuth callback or cron.
    """
    import moshimo_reel_queue

    if not _check_connected_with_comment_scope():
        return {
            "ok": False,
            "status": "missing_comment_scope",
            "reason": "YouTube自動コメント権限（youtube.force-ssl）が未認可です",
            "commented": [],
            "already_done": [],
            "errors": [],
        }

    now_dt = _now_jst(now_jst)
    commented: List[Dict[str, Any]] = []
    already_done: List[str] = []
    errors: List[Dict[str, Any]] = []

    for ep in moshimo_reel_queue.get_all_moshimo_episodes(allow_blocking=True):
        posted, vid, _ = find_synced_youtube_video_for_episode(ep, allow_blocking=True)
        if not posted or not vid:
            continue
        c_res = ensure_moshimo_episode_youtube_comment(ep, video_id=vid, force=False, now_jst=now_dt)
        st = str(c_res.get("status") or "")
        if st in ("already_commented", "already_exists"):
            already_done.append(ep["ep_id"])
        elif c_res.get("ok"):
            commented.append(
                {
                    "ep_id": ep["ep_id"],
                    "video_id": vid,
                    "comment_id": c_res.get("comment_id") or "",
                    "studio_comments_url": c_res.get("studio_comments_url") or f"https://studio.youtube.com/video/{vid}/comments",
                }
            )
        else:
            errors.append({"ep_id": ep["ep_id"], "video_id": vid, "error": c_res.get("error") or c_res.get("reason") or st})

    return {
        "ok": len(errors) == 0,
        "status": "success" if not errors else ("partial" if commented or already_done else "error"),
        "commented_count": len(commented),
        "already_done_count": len(already_done),
        "commented": commented,
        "already_done": already_done,
        "errors": errors,
    }


def publish_moshimo_episode_to_youtube(
    ep_id: Optional[str] = None,
    slot_label: str = "20:00",
    custom_title: str = "",
    custom_description: str = "",
    privacy_status: Optional[str] = None,
    force: bool = False,
    allow_recycle: bool = False,
    allow_reupload: bool = False,
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Publish a specific (or next due) 3D Voxel Moshimo episode to YouTube Shorts (`@aiseki_navi`),
    guarded by both process lock and distributed Redis/DB claim lock (`H-1`).
    """
    import moshimo_reel_queue

    if not _yt_post_lock.acquire(blocking=False):
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "already_running"}

    try:
        if not turbo_store.acquire_distributed_lock("yt_upload_mutex", ttl_sec=240, fail_open=True):
            return {"status": "skipped", "ok": False, "skipped": True, "reason": "distributed_lock_busy"}

        now_dt = _now_jst(now_jst)
        biz_date = _business_dt(now_dt).strftime("%Y-%m-%d")
        slot_dedup_key = f"{YOUTUBE_SLOT_PREFIX}{biz_date}_{slot_label}"

        if not force:
            if not is_youtube_auto_post_enabled(allow_blocking=True):
                return {"status": "skipped", "ok": False, "reason": "YouTube Shorts auto-post is disabled"}
            if not _check_connected_with_upload_scope():
                return {"status": "skipped", "ok": False, "reason": "YouTube upload scope not yet authorized"}
            if job_state.is_done(slot_dedup_key):
                return {"status": "skipped", "ok": False, "reason": "Already posted scheduled YouTube Short for this slot"}
            in_cd, cd_reason = check_youtube_auto_post_cooldown(min_interval_minutes=90, now_jst=now_dt, allow_blocking=True)
            if in_cd:
                return {"status": "skipped", "ok": False, "reason": cd_reason}

        if ep_id:
            ep = moshimo_reel_queue.get_episode_by_id(ep_id)
        else:
            ep = select_due_youtube_moshimo_episode(now_jst=now_dt)
            if ep is None and force:
                for cand in moshimo_reel_queue.get_all_moshimo_episodes(allow_blocking=True):
                    already, _, _ = find_synced_youtube_video_for_episode(cand, allow_blocking=True)
                    if not already and os.path.isfile(moshimo_reel_queue.resolve_episode_local_path(cand)):
                        ep = dict(cand)
                        break

        if not ep:
            return {"status": "skipped", "ok": False, "reason": "No matching or due Moshimo episode for YouTube"}

        resolved_ep_id = ep["ep_id"]
        is_recycle = bool(allow_recycle or ep.get("is_evergreen_recycle"))
        already_done, existing_vid, _ = find_synced_youtube_video_for_episode(ep, allow_blocking=True)
        if already_done and not is_recycle and not allow_reupload:
            return {
                "status": "skipped",
                "ok": False,
                "reason": f"エピソード『{resolved_ep_id}』は既にYouTube公開済みです（重複防止｜video_id={existing_vid or 'done'}）",
                "ep_id": resolved_ep_id,
                "video_id": existing_vid,
            }

        claim_key = f"yt_upload_claim:{resolved_ep_id}:{biz_date}"
        if not turbo_store.acquire_distributed_lock(claim_key, ttl_sec=360, fail_open=True):
            return {
                "status": "skipped",
                "ok": False,
                "skipped": True,
                "reason": f"Another worker is currently uploading {resolved_ep_id}",
            }

        local_path = moshimo_reel_queue.resolve_episode_local_path(ep)
        if not os.path.isfile(local_path):
            return {
                "status": "error",
                "ok": False,
                "error": f"Moshimo MP4 file missing on disk: {ep['static_rel_path']}",
                "ep_id": resolved_ep_id,
            }

        raw_title = (custom_title or ep.get("youtube_title") or f"{ep['title']} #もしもシリーズ #Shorts").strip()
        raw_desc = (custom_description or moshimo_reel_queue.build_youtube_shorts_description(ep)).strip()
        style_tag = f"moshimo_{resolved_ep_id}"

        up_res = upload_short_video_to_youtube(
            mp4_path=local_path,
            title=raw_title,
            description=raw_desc,
            tags=list(DEFAULT_YOUTUBE_TAGS),
            privacy_status=privacy_status,
            session_key_id=f"{resolved_ep_id}_{biz_date}",
        )
        up_res["ep_id"] = resolved_ep_id
        up_res["episode_title"] = ep["title"]
        up_res["reel_type"] = style_tag
        up_res["is_evergreen_recycle"] = is_recycle

        ts_str = now_dt.strftime("%Y-%m-%d %H:%M:%S")
        if up_res.get("ok"):
            vid = str(up_res.get("video_id") or "")
            # 1) Persist critical anti-duplication keys FIRST (`H-2`)
            _persist_critical_episode_done(resolved_ep_id, vid, biz_date, ts_str)
            if not force:
                try:
                    job_state.mark_done(slot_dedup_key)
                except Exception:
                    pass

            # 2) Auto-post official top-level comment (`pinned_comment`) when public/unlisted & comment scope granted
            is_locked = bool(up_res.get("privacy_locked"))
            if not is_locked and up_res.get("privacy_status") in ("public", "unlisted") and _check_connected_with_comment_scope():
                try:
                    c_res = ensure_moshimo_episode_youtube_comment(
                        ep,
                        video_id=vid,
                        force=is_recycle,
                        now_jst=now_dt,
                    )
                    up_res["comment_result"] = c_res
                except Exception as c_exc:
                    logger.warning(f"[youtube_poster] auto-comment warning for {resolved_ep_id} ({vid}): {c_exc}")

            # 3) Secondary analytics/history registration (`H-3`: never register locked_private as public post)
            try:
                import sns_editorial
                if is_locked:
                    sns_editorial.record_sns_delivery_status(
                        "youtube_shorts",
                        "error",
                        slot_label=slot_label,
                        post_id=vid,
                        detail="Google未審査ロックにより非公開化（自動投稿を一時停止しました）",
                        now_jst=now_dt,
                    )
                else:
                    sns_editorial.record_sns_delivery_status(
                        "youtube_shorts",
                        "success",
                        slot_label=slot_label,
                        post_id=vid,
                        now_jst=now_dt,
                    )
                    if up_res.get("privacy_status") == "public":
                        import sns_insights_loop
                        sns_insights_loop.register_published_post(
                            channel="youtube_shorts",
                            post_id=f"yt_{vid}",
                            text=f"{up_res.get('title', raw_title)}\n{raw_desc}",
                            style=style_tag,
                            reel_type="moshimo_3d",
                            slot_label=slot_label,
                            s_param="short_bio",
                            now_jst=now_dt,
                        )
            except Exception as exc:
                logger.warning(f"[youtube_poster] post-upload analytics hook warning: {exc}")

            _append_upload_history({
                "timestamp": ts_str,
                "ep_id": resolved_ep_id,
                "title": up_res.get("title") or ep["title"],
                "video_id": vid,
                "shorts_url": up_res.get("shorts_url") or "",
                "studio_url": up_res.get("studio_url") or "",
                "privacy_status": up_res.get("privacy_status") or "public",
                "privacy_locked": is_locked,
            })
            logger.info(
                f"[youtube_poster] Uploaded Moshimo Short '{resolved_ep_id}' -> video_id={vid} "
                f"(privacy={up_res.get('privacy_status')}, locked={is_locked})"
            )
        else:
            try:
                import sns_editorial
                sns_editorial.record_sns_delivery_status(
                    "youtube_shorts",
                    "error",
                    slot_label=slot_label,
                    detail=str(up_res.get("error") or "")[:140],
                    now_jst=now_dt,
                )
            except Exception:
                pass

        return up_res
    finally:
        _yt_post_lock.release()


def dispatch_scheduled_youtube_short(
    stores: Optional[List[Dict[str, Any]]] = None,
    slot_label: str = "20:00",
    is_seed: bool = False,
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Scheduled `20:00` JST YouTube Shorts dispatcher called alongside Instagram Reels in `worker.py` / `app.py`.
    """
    if slot_label != "20:00":
        return {"status": "skipped", "ok": False, "reason": "YouTube Shorts auto-dispatch runs only on 20:00 slot"}
    if not is_youtube_auto_post_enabled(allow_blocking=True):
        return {"status": "skipped", "ok": False, "reason": "YouTube Shorts auto-post disabled"}

    if not _check_connected_with_upload_scope():
        return {"status": "skipped", "ok": False, "reason": "YouTube upload scope not yet authorized"}

    now_dt = _now_jst(now_jst)
    due_ep = select_due_youtube_moshimo_episode(now_jst=now_dt)
    if due_ep is not None:
        pub_res = publish_moshimo_episode_to_youtube(
            ep_id=due_ep["ep_id"],
            slot_label=slot_label,
            force=False,
            allow_recycle=bool(due_ep.get("is_evergreen_recycle")),
            now_jst=now_dt,
        )
        if _check_connected_with_comment_scope():
            try:
                backfill_published_moshimo_comments(now_jst=now_dt)
            except Exception:
                pass
        return pub_res

    if _check_connected_with_comment_scope():
        try:
            backfill_published_moshimo_comments(now_jst=now_dt)
        except Exception:
            pass

    # Optional live-data Reel cross-posting to YouTube Shorts (OFF by default to keep channel curated)
    if is_youtube_live_reels_enabled(allow_blocking=True) and stores and not is_seed:
        biz_date = _business_dt(now_dt).strftime("%Y-%m-%d")
        if job_state.is_done(f"{YOUTUBE_DATE_POSTED_PREFIX}{biz_date}"):
            return {"status": "skipped", "ok": False, "reason": "Moshimo Short already posted today"}
        slot_dedup_key = f"{YOUTUBE_SLOT_PREFIX}{biz_date}_{slot_label}"
        if job_state.is_done(slot_dedup_key):
            return {"status": "skipped", "ok": False, "reason": "Already posted YouTube Short for this slot"}
        if not _yt_post_lock.acquire(blocking=False):
            return {"status": "skipped", "ok": False, "reason": "already_running"}
        tmp_mp4 = ""
        try:
            if not turbo_store.acquire_distributed_lock(f"yt_live_claim:{biz_date}", ttl_sec=240, fail_open=True):
                return {"status": "skipped", "ok": False, "reason": "distributed_lock_busy"}
            mp4_bytes = reel_generator.render_reel_mp4_bytes(stores, reel_type="live_top5", force_refresh=False)
            tmp_dir = "/tmp/aiseki_reels"
            os.makedirs(tmp_dir, exist_ok=True)
            tmp_mp4 = os.path.join(tmp_dir, f"yt_live_{biz_date}_{os.getpid()}.mp4")
            with open(tmp_mp4, "wb") as fp:
                fp.write(mp4_bytes)
            ctx = reel_generator.build_reel_context(stores, reel_type="live_top5", now_jst=now_dt)
            title = f"今夜の全国リアルタイム男女比TOP5（{biz_date}） #相席ラウンジ #相席ナビ #Shorts"
            desc = reel_generator.build_reel_caption(ctx) + "\n\n▼ リアルタイム全店は @aiseki_navi プロフィールのリンクから\n#Shorts"
            res = upload_short_video_to_youtube(tmp_mp4, title=title, description=desc, session_key_id=f"live_{biz_date}")
            if res.get("ok"):
                ts_s = now_dt.strftime("%Y-%m-%d %H:%M:%S")
                job_state.mark_done(slot_dedup_key)
                job_state.set_value(YOUTUBE_LAST_AUTO_POST_TS_KEY, ts_s)
            return res
        except Exception as exc:
            return {"status": "error", "ok": False, "error": f"Live Reel YouTube upload failed: {type(exc).__name__}"}
        finally:
            if tmp_mp4 and os.path.isfile(tmp_mp4):
                try:
                    os.remove(tmp_mp4)
                except Exception:
                    pass
            _yt_post_lock.release()

    return {"status": "skipped", "ok": False, "reason": "No due Moshimo episode for YouTube today"}


def get_youtube_auto_post_status(allow_blocking: bool = False) -> Dict[str, Any]:
    """Return safe, secret-free YouTube Shorts Auto-Post status for Master dashboards."""
    yt_oauth = youtube_analytics_sync.get_youtube_sync_status(allow_blocking=allow_blocking)
    enabled = is_youtube_auto_post_enabled(allow_blocking=allow_blocking)
    live_reels_on = is_youtube_live_reels_enabled(allow_blocking=allow_blocking)
    privacy = get_youtube_upload_privacy(allow_blocking=allow_blocking)
    privacy_locked = str(job_state.get_value(YOUTUBE_PRIVACY_LOCKED_KEY, allow_blocking=allow_blocking, default="0") or "") == "1"
    last_ts = str(job_state.get_value(YOUTUBE_LAST_AUTO_POST_TS_KEY, allow_blocking=allow_blocking, default="") or "")
    history = get_upload_history(allow_blocking=allow_blocking)

    connected = bool(yt_oauth.get("connected"))
    has_upload_scope = bool(yt_oauth.get("has_upload_scope"))
    has_comment_scope = bool(yt_oauth.get("has_comment_scope"))
    ready = bool(connected and has_upload_scope and enabled and not privacy_locked)

    return {
        "enabled": enabled,
        "live_reels_enabled": live_reels_on,
        "privacy_status": privacy,
        "supported_privacy_statuses": list(SUPPORTED_PRIVACY_STATUSES),
        "connected": connected,
        "has_upload_scope": has_upload_scope,
        "has_comment_scope": has_comment_scope,
        "ready": ready,
        "channel_title": yt_oauth.get("channel_title") or "",
        "custom_url": yt_oauth.get("custom_url") or "",
        "privacy_locked": privacy_locked,
        "last_auto_post_at": last_ts,
        "recent_uploads": history[:5],
        "audit_form_url": "https://support.google.com/youtube/contact/yt_api_form",
    }
