"""
youtube_analytics_sync.py - Read-Only YouTube Data API v3 + YouTube Analytics API v2 Auto-Sync.

Automates daily (`17:25` & `03:25` JST) and on-demand (`/staff/pdca`) harvesting of YouTube Shorts
performance metrics for `@aiseki_navi` without manual CSV exports or screenshots:
  - Public & real-time video stats via YouTube Data API v3 (`channels.list`, `playlistItems.list`, `videos.list`)
  - Retention & swipe-cliff metrics via YouTube Analytics API v2 (`reports.query`):
      * `views` (total Shorts plays/starts in feed, matching YouTube Studio's `視聴回数`)
      * `engagedViews` (viewers who stayed past the initial hook threshold -> `stayed_to_watch_pct = engagedViews / views * 100`)
      * `averageViewDuration` (`avg_watch_sec`)
      * `averageViewPercentage` (`avg_pct_viewed`)
      * `estimatedMinutesWatched` (`total_watch_sec`)
      * `likes`, `comments`, `shares`, `subscribersGained`

Security & Compliance Guarantees:
  1. Strictly READ-ONLY Google OAuth 2.0 scopes (`youtube.readonly`, `yt-analytics.readonly`).
     Never requests `youtube.upload` or write scopes — zero risk of unverified-app private-lock or channel mutation.
  2. `client_secret` and `refresh_token` are persisted exclusively under `token:*` keys in `job_state`
     (automatic `obf1:` stream obfuscation across PostgreSQL, Redis, local cache, and Sheets backup)
     and are NEVER returned in status payloads or logs.
  3. Strict Fact Guarantee (`AGENTS.md §1`): Zero fabricated values. When YouTube Analytics API has a
     24–48h lag on newly published Shorts (`analytics_views == 0`), real-time `views`/`likes` from Data API
     are synced while retention fields remain omitted so any previously ingested Studio screenshot metrics
     are preserved intact until official Analytics rows arrive.
"""

import datetime
import json
import logging
import os
import re
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, urlparse

import requests

import job_state

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

YOUTUBE_OAUTH_META_KEY = "youtube_oauth_meta_v1"
YOUTUBE_CLIENT_SECRET_STATE_KEY = "token:youtube_client_secret"
YOUTUBE_REFRESH_TOKEN_STATE_KEY = "token:youtube_refresh_token"

GOOGLE_AUTH_ENDPOINT = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN_ENDPOINT = "https://oauth2.googleapis.com/token"
YOUTUBE_DATA_API_BASE = "https://www.googleapis.com/youtube/v3"
YOUTUBE_ANALYTICS_API_URL = "https://youtubeanalytics.googleapis.com/v2/reports"

PROD_SITE_ORIGIN = "https://aiseki-navi.com"
OAUTH_CALLBACK_PATH = "/staff/youtube/oauth/callback"

YOUTUBE_READONLY_SCOPES = (
    "https://www.googleapis.com/auth/youtube.readonly",
    "https://www.googleapis.com/auth/yt-analytics.readonly",
)

MAX_SHORTS_DURATION_SEC = 185  # YouTube Shorts max duration is 180s (+5s container rounding tolerance)

_ISO_DURATION_RE = re.compile(
    r"^P(?:(?P<days>\d+)D)?(?:T(?:(?P<hours>\d+)H)?(?:(?P<minutes>\d+)M)?(?:(?P<seconds>\d+)S)?)?$",
    re.IGNORECASE,
)


def _now_jst(now_jst: Optional[datetime.datetime] = None) -> datetime.datetime:
    if isinstance(now_jst, datetime.datetime):
        return now_jst.astimezone(JST) if now_jst.tzinfo else now_jst.replace(tzinfo=JST)
    return datetime.datetime.now(JST)


def _now_jst_str(now_jst: Optional[datetime.datetime] = None) -> str:
    return _now_jst(now_jst).strftime("%Y-%m-%d %H:%M:%S")


def parse_iso8601_duration_seconds(duration_str: str) -> int:
    """Convert ISO-8601 YouTube video duration (e.g. `PT26S`, `PT1M12S`, `PT3M`) into integer seconds."""
    raw = str(duration_str or "").strip()
    if not raw:
        return 0
    m = _ISO_DURATION_RE.match(raw)
    if not m:
        return 0
    days = int(m.group("days") or 0)
    hours = int(m.group("hours") or 0)
    minutes = int(m.group("minutes") or 0)
    seconds = int(m.group("seconds") or 0)
    return days * 86400 + hours * 3600 + minutes * 60 + seconds


def _load_meta(allow_blocking: bool = False) -> Dict[str, Any]:
    raw = job_state.get_value(YOUTUBE_OAUTH_META_KEY, allow_blocking=allow_blocking, default="")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except Exception:
        return {}


def _save_meta(meta: Dict[str, Any]) -> None:
    clean = dict(meta or {})
    # Never allow secrets inside non-token metadata JSON
    clean.pop("client_secret", None)
    clean.pop("refresh_token", None)
    clean.pop("access_token", None)
    job_state.set_value(YOUTUBE_OAUTH_META_KEY, json.dumps(clean, ensure_ascii=False))


def _get_effective_credentials(allow_blocking: bool = True) -> Dict[str, str]:
    meta = _load_meta(allow_blocking=allow_blocking)
    client_id = (
        str(meta.get("client_id") or "").strip()
        or str(os.environ.get("YOUTUBE_CLIENT_ID") or "").strip()
    )
    client_secret = (
        str(job_state.get_value(YOUTUBE_CLIENT_SECRET_STATE_KEY, allow_blocking=allow_blocking, default="") or "").strip()
        or str(os.environ.get("YOUTUBE_CLIENT_SECRET") or "").strip()
    )
    refresh_token = (
        str(job_state.get_value(YOUTUBE_REFRESH_TOKEN_STATE_KEY, allow_blocking=allow_blocking, default="") or "").strip()
        or str(os.environ.get("YOUTUBE_REFRESH_TOKEN") or "").strip()
    )
    channel_id = (
        str(meta.get("channel_id") or "").strip()
        or str(os.environ.get("YOUTUBE_CHANNEL_ID") or "").strip()
    )
    return {
        "client_id": client_id,
        "client_secret": client_secret,
        "refresh_token": refresh_token,
        "channel_id": channel_id,
    }


def _mask_client_id(client_id: str) -> str:
    cid = str(client_id or "").strip()
    if not cid:
        return ""
    if len(cid) <= 16:
        return cid[:4] + "..."
    if ".apps.googleusercontent.com" in cid:
        prefix = cid.split(".apps.googleusercontent.com", 1)[0]
        head = prefix[:8]
        tail = prefix[-4:] if len(prefix) > 12 else ""
        return f"{head}...{tail}.apps.googleusercontent.com"
    return f"{cid[:8]}...{cid[-4:]}"


def resolve_oauth_redirect_uri(base_url: Optional[str] = None) -> str:
    """
    Return the canonical OAuth2 callback URI.
    Preserves `http://127.0.0.1:<port>` / `http://localhost:<port>` for local testing,
    and normalizes production traffic behind Fly proxy to `https://aiseki-navi.com/staff/youtube/oauth/callback`.
    """
    env_override = str(os.environ.get("YOUTUBE_REDIRECT_URI") or "").strip()
    if env_override:
        return env_override
    if base_url:
        try:
            parsed = urlparse(str(base_url).strip())
            host = (parsed.hostname or "").lower()
            if host in ("127.0.0.1", "localhost"):
                scheme = parsed.scheme or "http"
                netloc = parsed.netloc
                return f"{scheme}://{netloc}{OAUTH_CALLBACK_PATH}"
        except Exception:
            pass
    return f"{PROD_SITE_ORIGIN}{OAUTH_CALLBACK_PATH}"


def get_youtube_sync_status(base_url: Optional[str] = None, allow_blocking: bool = False) -> Dict[str, Any]:
    """Return safe, secret-free YouTube OAuth & sync status for `/staff/pdca`."""
    meta = _load_meta(allow_blocking=allow_blocking)
    creds = _get_effective_credentials(allow_blocking=allow_blocking)
    has_client = bool(creds["client_id"] and creds["client_secret"])
    connected = bool(has_client and creds["refresh_token"])
    return {
        "has_client_credentials": has_client,
        "connected": connected,
        "client_id_masked": _mask_client_id(creds["client_id"]),
        "client_id": creds["client_id"],
        "channel_id": creds["channel_id"],
        "channel_title": str(meta.get("channel_title") or ""),
        "custom_url": str(meta.get("custom_url") or ""),
        "connected_at": str(meta.get("connected_at") or ""),
        "last_sync_at": str(meta.get("last_sync_at") or ""),
        "last_synced_count": int(meta.get("last_synced_count") or 0),
        "last_error": str(meta.get("last_error") or ""),
        "redirect_uri": resolve_oauth_redirect_uri(base_url=base_url),
        "scopes": list(YOUTUBE_READONLY_SCOPES),
    }


def parse_oauth_client_credentials(
    client_json_raw: Optional[str] = None,
    client_id: Optional[str] = None,
    client_secret: Optional[str] = None,
    allow_stored_secret_fallback: bool = True,
) -> Tuple[str, str, str]:
    """
    Extract and validate `(client_id, client_secret, error_msg)` from either:
      1) Google Cloud Console's downloaded `client_secret_*.json` (`{"web": {...}}` or `{"installed": {...}}`)
      2) Direct `client_id` + `client_secret` form fields.
    """
    cid = str(client_id or "").strip()
    csec = str(client_secret or "").strip()

    if client_json_raw and str(client_json_raw).strip():
        try:
            data = json.loads(str(client_json_raw))
        except Exception:
            return "", "", "JSONファイルの形式が正しくありません（Google Cloudからダウンロードした client_secret_*.json を選択してください）"
        if not isinstance(data, dict):
            return "", "", "JSONデータの構造が不正です"
        block = data.get("web") or data.get("installed") or data
        if not isinstance(block, dict):
            return "", "", "OAuthクライアント情報（web / installed）がJSON内に見つかりません"
        cid = str(block.get("client_id") or cid).strip()
        csec = str(block.get("client_secret") or csec).strip()

    if allow_stored_secret_fallback and cid and not csec:
        existing = _get_effective_credentials(allow_blocking=True)
        if existing["client_id"] == cid and existing["client_secret"]:
            csec = existing["client_secret"]

    if not cid:
        return "", "", "OAuth クライアントID（または client_secret.json ファイル）を入力してください"
    if len(cid) < 10 or len(cid) > 256 or any(ch.isspace() for ch in cid):
        return "", "", "クライアントIDの形式が正しくありません（空白を含まない文字列を入力してください）"
    if not csec:
        return "", "", "クライアントシークレットを入力してください（または client_secret.json を選択してください）"
    if len(csec) < 8 or len(csec) > 256 or any(ch.isspace() for ch in csec):
        return "", "", "クライアントシークレットの形式が正しくありません"

    return cid, csec, ""


def save_oauth_client_credentials(client_id: str, client_secret: str) -> Dict[str, Any]:
    """Persist validated OAuth client credentials (obfuscating `client_secret` via `token:*`)."""
    cid, csec, err = parse_oauth_client_credentials(
        client_id=client_id,
        client_secret=client_secret,
        allow_stored_secret_fallback=True,
    )
    if err:
        return {"ok": False, "error": err}

    meta = _load_meta(allow_blocking=True)
    meta["client_id"] = cid
    meta["updated_at"] = _now_jst_str()
    _save_meta(meta)
    job_state.set_value(YOUTUBE_CLIENT_SECRET_STATE_KEY, csec)
    return {"ok": True, "client_id_masked": _mask_client_id(cid)}


def build_oauth_authorize_url(redirect_uri: str, state_token: str) -> Tuple[str, str]:
    """
    Build Google OAuth 2.0 authorization URL with `prompt="consent select_account"` so the user
    can explicitly choose the `@aiseki_navi` YouTube Brand Account and receive an offline `refresh_token`.
    """
    creds = _get_effective_credentials(allow_blocking=True)
    if not creds["client_id"] or not creds["client_secret"]:
        return "", "先にGoogle OAuthクライアントID・シークレット（またはJSONファイル）を保存してください"
    if not state_token:
        return "", "OAuth stateトークンの生成に失敗しました"

    params = {
        "client_id": creds["client_id"],
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": " ".join(YOUTUBE_READONLY_SCOPES),
        "access_type": "offline",
        "prompt": "consent select_account",
        "include_granted_scopes": "true",
        "state": state_token,
    }
    return f"{GOOGLE_AUTH_ENDPOINT}?{urlencode(params)}", ""


def _fetch_my_youtube_channel(access_token: str) -> Tuple[Optional[Dict[str, Any]], str]:
    try:
        resp = requests.get(
            f"{YOUTUBE_DATA_API_BASE}/channels",
            params={"part": "snippet,contentDetails,statistics", "mine": "true"},
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=14,
        )
        if resp.status_code >= 400:
            err_detail = ""
            try:
                err_json = resp.json() or {}
                err_detail = str(((err_json.get("error") or {}).get("message")) or "")[:120]
            except Exception:
                err_detail = ""
            if resp.status_code == 403 and ("has not been used" in err_detail or "disabled" in err_detail.lower()):
                return None, "Google Cloudプロジェクトで『YouTube Data API v3』がまだ有効化されていません。有効化後に再度お試しください。"
            return None, f"YouTubeチャンネル情報の取得に失敗しました (HTTP {resp.status_code})"
        items = (resp.json() or {}).get("items") or []
        if not items:
            return (
                None,
                "選択したGoogleアカウントにYouTubeチャンネルが見つかりませんでした。画面でブランドアカウント『相席ナビ (@aiseki_navi)』を選択してください。",
            )
        ch = items[0] if isinstance(items[0], dict) else {}
        snippet = ch.get("snippet") or {}
        cdetails = (ch.get("contentDetails") or {}).get("relatedPlaylists") or {}
        return {
            "channel_id": str(ch.get("id") or "").strip(),
            "channel_title": str(snippet.get("title") or "").strip(),
            "custom_url": str(snippet.get("customUrl") or "").strip(),
            "uploads_playlist_id": str(cdetails.get("uploads") or "").strip(),
        }, ""
    except Exception as exc:
        return None, f"YouTube API通信エラー: {type(exc).__name__}"


def exchange_code_and_save(code: str, redirect_uri: str) -> Dict[str, Any]:
    """Exchange OAuth2 authorization `code` for tokens, verify channel identity, and persist obfuscated refresh token."""
    clean_code = str(code or "").strip()
    if not clean_code:
        return {"ok": False, "error": "Googleから認証コードが返されませんでした"}

    creds = _get_effective_credentials(allow_blocking=True)
    if not creds["client_id"] or not creds["client_secret"]:
        return {"ok": False, "error": "OAuthクライアント認証情報が未設定です"}

    try:
        tok_resp = requests.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "code": clean_code,
                "client_id": creds["client_id"],
                "client_secret": creds["client_secret"],
                "redirect_uri": redirect_uri,
                "grant_type": "authorization_code",
            },
            timeout=15,
        )
    except Exception as exc:
        return {"ok": False, "error": f"Googleトークン交換の通信エラー: {type(exc).__name__}"}

    if tok_resp.status_code >= 400:
        err_code = ""
        try:
            err_code = str((tok_resp.json() or {}).get("error") or "")[:60]
        except Exception:
            err_code = ""
        return {
            "ok": False,
            "error": f"Googleトークン交換に失敗しました (HTTP {tok_resp.status_code}{': ' + err_code if err_code else ''})",
        }

    tok_json = tok_resp.json() or {}
    access_token = str(tok_json.get("access_token") or "").strip()
    refresh_token = str(tok_json.get("refresh_token") or "").strip() or creds["refresh_token"]
    if not access_token:
        return {"ok": False, "error": "アクセストークンを取得できませんでした"}
    if not refresh_token:
        return {
            "ok": False,
            "error": "リフレッシュトークンが発行されませんでした。Googleアカウント権限画面から一度アクセス権を削除し、再度連携してください。",
        }

    ch_info, ch_err = _fetch_my_youtube_channel(access_token)
    if ch_err or not ch_info:
        return {"ok": False, "error": ch_err or "YouTubeチャンネルの確認に失敗しました"}

    job_state.set_value(YOUTUBE_REFRESH_TOKEN_STATE_KEY, refresh_token)
    meta = _load_meta(allow_blocking=True)
    meta.update(
        {
            "client_id": creds["client_id"],
            "channel_id": ch_info["channel_id"],
            "channel_title": ch_info["channel_title"],
            "custom_url": ch_info["custom_url"],
            "uploads_playlist_id": ch_info["uploads_playlist_id"],
            "connected_at": _now_jst_str(),
            "last_error": "",
        }
    )
    _save_meta(meta)
    return {
        "ok": True,
        "channel_id": ch_info["channel_id"],
        "channel_title": ch_info["channel_title"],
        "custom_url": ch_info["custom_url"],
    }


def disconnect_youtube_oauth(clear_client_credentials: bool = False) -> Dict[str, Any]:
    """Clear stored YouTube OAuth refresh token (and optionally client credentials)."""
    job_state.set_value(YOUTUBE_REFRESH_TOKEN_STATE_KEY, "")
    meta = _load_meta(allow_blocking=True)
    meta["last_error"] = ""
    meta["connected_at"] = ""
    if clear_client_credentials:
        job_state.set_value(YOUTUBE_CLIENT_SECRET_STATE_KEY, "")
        meta["client_id"] = ""
    _save_meta(meta)
    return {"ok": True}


def _refresh_access_token(client_id: str, client_secret: str, refresh_token: str) -> Tuple[str, str]:
    try:
        resp = requests.post(
            GOOGLE_TOKEN_ENDPOINT,
            data={
                "client_id": client_id,
                "client_secret": client_secret,
                "refresh_token": refresh_token,
                "grant_type": "refresh_token",
            },
            timeout=14,
        )
    except Exception as exc:
        return "", f"トークン更新通信エラー: {type(exc).__name__}"

    if resp.status_code >= 400:
        err_tag = ""
        try:
            err_tag = str((resp.json() or {}).get("error") or "")[:50]
        except Exception:
            err_tag = ""
        if err_tag == "invalid_grant" or resp.status_code in (400, 401):
            return "", f"YouTube認証トークンの再認可が必要です ({err_tag or resp.status_code})"
        return "", f"YouTubeトークン更新 HTTP {resp.status_code}"

    access_token = str((resp.json() or {}).get("access_token") or "").strip()
    if not access_token:
        return "", "YouTubeアクセストークンが空でした"
    return access_token, ""


def _parse_analytics_report_rows(report_json: Dict[str, Any]) -> Dict[str, Dict[str, float]]:
    """Parse `youtubeanalytics.googleapis.com/v2/reports` tabular payload by header name."""
    out: Dict[str, Dict[str, float]] = {}
    if not isinstance(report_json, dict):
        return out
    headers = report_json.get("columnHeaders") or []
    rows = report_json.get("rows") or []
    if not isinstance(headers, list) or not isinstance(rows, list):
        return out
    col_names = [str((h or {}).get("name") or "") for h in headers]
    if "video" not in col_names:
        return out
    vid_idx = col_names.index("video")
    for row in rows:
        if not isinstance(row, list) or len(row) <= vid_idx:
            continue
        vid = str(row[vid_idx] or "").strip()
        if not vid:
            continue
        row_map: Dict[str, float] = {}
        for idx, cname in enumerate(col_names):
            if idx == vid_idx or not cname or idx >= len(row):
                continue
            val = row[idx]
            if isinstance(val, (int, float)) and not isinstance(val, bool):
                row_map[cname] = float(val)
        out[vid] = row_map
    return out


def _query_youtube_analytics_by_video(
    access_token: str,
    channel_id: str,
    video_ids: List[str],
    start_date: str,
    end_date: str,
) -> Tuple[Dict[str, Dict[str, float]], str]:
    if not video_ids:
        return {}, ""
    ids_expr = f"channel=={channel_id}" if channel_id else "channel==MINE"
    metric_sets = (
        "views,engagedViews,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,comments,shares,subscribersGained",
        "views,estimatedMinutesWatched,averageViewDuration,averageViewPercentage,likes,subscribersGained",
    )
    id_targets = [ids_expr] if ids_expr == "channel==MINE" else [ids_expr, "channel==MINE"]
    last_warning = ""

    for target_ids in id_targets:
        for metric_csv in metric_sets:
            try:
                resp = requests.get(
                    YOUTUBE_ANALYTICS_API_URL,
                    params={
                        "ids": target_ids,
                        "startDate": start_date,
                        "endDate": end_date,
                        "dimensions": "video",
                        "filters": f"video=={','.join(video_ids[:40])}",
                        "metrics": metric_csv,
                        "maxResults": max(25, len(video_ids)),
                    },
                    headers={"Authorization": f"Bearer {access_token}"},
                    timeout=15,
                )
            except Exception as exc:
                return {}, f"YouTube Analytics通信エラー: {type(exc).__name__}"

            if resp.status_code == 200:
                return _parse_analytics_report_rows(resp.json() or {}), ""

            if resp.status_code == 403:
                err_msg = ""
                try:
                    err_msg = str((((resp.json() or {}).get("error") or {}).get("message")) or "")
                except Exception:
                    err_msg = ""
                if "has not been used" in err_msg or "disabled" in err_msg.lower():
                    last_warning = "Google Cloudで『YouTube Analytics API』を有効化すると視聴継続率・維持率も自動取得されます"
                    return {}, last_warning
                last_warning = f"YouTube Analytics権限エラー (HTTP 403)"
            elif resp.status_code == 400:
                last_warning = "YouTube Analytics一部指標未対応 (HTTP 400)"
                continue
            else:
                last_warning = f"YouTube Analytics HTTP {resp.status_code}"
    return {}, last_warning


def _is_short_video(duration_sec: int, title: str, description: str) -> bool:
    combined = f"{title or ''}\n{description or ''}".lower()
    if "#shorts" in combined or "#short" in combined:
        return True
    return 0 < duration_sec <= MAX_SHORTS_DURATION_SEC


def _safe_nonneg_int(val: Any) -> Optional[int]:
    if val is None or isinstance(val, bool):
        return None
    try:
        n = int(round(float(val)))
        return n if n >= 0 else None
    except (TypeError, ValueError):
        return None


def fetch_youtube_shorts_metrics(
    limit: int = 20,
    days: int = 90,
    now_jst: Optional[datetime.datetime] = None,
) -> Dict[str, Any]:
    """
    Fetch recent YouTube Shorts + per-video Analytics metrics via Official Google APIs.
    Returns structured `posts` list ready for `sns_insights_loop._upsert_post_record`.
    """
    creds = _get_effective_credentials(allow_blocking=True)
    if not (creds["client_id"] and creds["client_secret"] and creds["refresh_token"]):
        return {
            "ok": False,
            "channel": "youtube_shorts",
            "status": "unconfigured",
            "synced": 0,
            "posts": [],
            "detail": "YouTube OAuth未連携（必要時は /staff/pdca から1クリック連携可能）",
        }

    meta = _load_meta(allow_blocking=True)
    access_token, tok_err = _refresh_access_token(
        creds["client_id"], creds["client_secret"], creds["refresh_token"]
    )
    if tok_err or not access_token:
        meta["last_error"] = tok_err or "トークン更新失敗"
        _save_meta(meta)
        return {
            "ok": False,
            "channel": "youtube_shorts",
            "status": "auth_error",
            "synced": 0,
            "posts": [],
            "detail": tok_err or "YouTubeトークン更新失敗",
        }

    uploads_pid = str(meta.get("uploads_playlist_id") or "").strip()
    channel_id = creds["channel_id"] or str(meta.get("channel_id") or "").strip()
    if not uploads_pid or not channel_id:
        ch_info, ch_err = _fetch_my_youtube_channel(access_token)
        if ch_err or not ch_info:
            meta["last_error"] = ch_err or "チャンネル取得失敗"
            _save_meta(meta)
            return {
                "ok": False,
                "channel": "youtube_shorts",
                "status": "channel_error",
                "synced": 0,
                "posts": [],
                "detail": ch_err or "YouTubeチャンネル取得失敗",
            }
        channel_id = ch_info["channel_id"]
        uploads_pid = ch_info["uploads_playlist_id"]
        meta.update(
            {
                "channel_id": channel_id,
                "channel_title": ch_info["channel_title"],
                "custom_url": ch_info["custom_url"],
                "uploads_playlist_id": uploads_pid,
            }
        )

    safe_limit = max(1, min(35, int(limit or 20)))
    try:
        pl_resp = requests.get(
            f"{YOUTUBE_DATA_API_BASE}/playlistItems",
            params={
                "part": "snippet,contentDetails",
                "playlistId": uploads_pid,
                "maxResults": safe_limit,
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=14,
        )
        if pl_resp.status_code == 404:
            # Empty uploads playlist on brand-new channels returns 404 in YouTube Data API v3
            pl_items = []
        elif pl_resp.status_code >= 400:
            err_txt = f"YouTube投稿一覧 HTTP {pl_resp.status_code}"
            meta["last_error"] = err_txt
            _save_meta(meta)
            return {
                "ok": False,
                "channel": "youtube_shorts",
                "status": "api_error",
                "synced": 0,
                "posts": [],
                "detail": err_txt,
            }
        else:
            pl_items = (pl_resp.json() or {}).get("items") or []
    except Exception as exc:
        err_txt = f"YouTube投稿一覧通信エラー: {type(exc).__name__}"
        meta["last_error"] = err_txt
        _save_meta(meta)
        return {
            "ok": False,
            "channel": "youtube_shorts",
            "status": "network_error",
            "synced": 0,
            "posts": [],
            "detail": err_txt,
        }

    video_ids: List[str] = []
    for item in pl_items:
        if not isinstance(item, dict):
            continue
        vid = str(((item.get("contentDetails") or {}).get("videoId")) or "").strip()
        if vid and vid not in video_ids:
            video_ids.append(vid)

    now_dt = _now_jst(now_jst)
    ts_str = _now_jst_str(now_dt)
    if not video_ids:
        meta["last_sync_at"] = ts_str
        meta["last_synced_count"] = 0
        meta["last_error"] = ""
        _save_meta(meta)
        return {
            "ok": True,
            "channel": "youtube_shorts",
            "status": "ok",
            "synced": 0,
            "updated": 0,
            "analytics_fetched": 0,
            "posts": [],
            "detail": "YouTubeチャンネル連携済み（公開ショート動画 0件）",
        }

    try:
        v_resp = requests.get(
            f"{YOUTUBE_DATA_API_BASE}/videos",
            params={
                "part": "snippet,contentDetails,statistics,status",
                "id": ",".join(video_ids[:35]),
            },
            headers={"Authorization": f"Bearer {access_token}"},
            timeout=14,
        )
        if v_resp.status_code >= 400:
            err_txt = f"YouTube動画詳細 HTTP {v_resp.status_code}"
            meta["last_error"] = err_txt
            _save_meta(meta)
            return {
                "ok": False,
                "channel": "youtube_shorts",
                "status": "api_error",
                "synced": 0,
                "posts": [],
                "detail": err_txt,
            }
        v_items = (v_resp.json() or {}).get("items") or []
    except Exception as exc:
        err_txt = f"YouTube動画詳細通信エラー: {type(exc).__name__}"
        meta["last_error"] = err_txt
        _save_meta(meta)
        return {
            "ok": False,
            "channel": "youtube_shorts",
            "status": "network_error",
            "synced": 0,
            "posts": [],
            "detail": err_txt,
        }

    short_videos: List[Dict[str, Any]] = []
    for v in v_items:
        if not isinstance(v, dict):
            continue
        vid = str(v.get("id") or "").strip()
        if not vid:
            continue
        v_status = v.get("status") or {}
        privacy = str(v_status.get("privacyStatus") or "public").lower()
        # Skip private/unscheduled drafts so pre-scheduled uploads aren't counted before going public
        if privacy == "private":
            continue
        snip = v.get("snippet") or {}
        cdet = v.get("contentDetails") or {}
        stats = v.get("statistics") or {}
        dur_sec = parse_iso8601_duration_seconds(str(cdet.get("duration") or ""))
        title = str(snip.get("title") or "").strip()
        desc = str(snip.get("description") or "").strip()
        if not _is_short_video(dur_sec, title, desc):
            continue
        short_videos.append(
            {
                "video_id": vid,
                "title": title,
                "description": desc,
                "published_at": str(snip.get("publishedAt") or ""),
                "duration_sec": dur_sec,
                "view_count": _safe_nonneg_int(stats.get("viewCount")),
                "like_count": _safe_nonneg_int(stats.get("likeCount")),
                "comment_count": _safe_nonneg_int(stats.get("commentCount")),
            }
        )

    safe_days = max(7, min(365, int(days or 90)))
    start_date = (now_dt.date() - datetime.timedelta(days=safe_days)).strftime("%Y-%m-%d")
    end_date = now_dt.date().strftime("%Y-%m-%d")
    short_ids = [sv["video_id"] for sv in short_videos]

    analytics_map, analytics_warn = _query_youtube_analytics_by_video(
        access_token=access_token,
        channel_id=channel_id,
        video_ids=short_ids,
        start_date=start_date,
        end_date=end_date,
    )

    out_posts: List[Dict[str, Any]] = []
    analytics_hit_count = 0

    for sv in short_videos:
        vid = sv["video_id"]
        arow = analytics_map.get(vid) or {}
        metrics_out: Dict[str, Any] = {}

        data_views = sv.get("view_count")
        ana_views = _safe_nonneg_int(arow.get("views"))
        eff_views = max(v for v in (data_views, ana_views) if v is not None) if (data_views is not None or ana_views is not None) else None
        if eff_views is not None:
            metrics_out["views"] = eff_views

        data_likes = sv.get("like_count")
        ana_likes = _safe_nonneg_int(arow.get("likes"))
        eff_likes = max(v for v in (data_likes, ana_likes) if v is not None) if (data_likes is not None or ana_likes is not None) else None
        if eff_likes is not None:
            metrics_out["likes"] = eff_likes

        data_comments = sv.get("comment_count")
        ana_comments = _safe_nonneg_int(arow.get("comments"))
        eff_comments = max(v for v in (data_comments, ana_comments) if v is not None) if (data_comments is not None or ana_comments is not None) else None
        if eff_comments is not None:
            metrics_out["comments"] = eff_comments

        if ana_views is not None and ana_views > 0:
            analytics_hit_count += 1
            metrics_out["impressions"] = ana_views

            engaged_v = _safe_nonneg_int(arow.get("engagedViews"))
            if engaged_v is not None and 0 <= engaged_v <= ana_views:
                stayed_pct = round((float(engaged_v) / float(ana_views)) * 100.0, 1)
                metrics_out["stayed_to_watch_pct"] = stayed_pct
                metrics_out["skip_rate_pct"] = round(max(0.0, 100.0 - stayed_pct), 1)

            avg_dur = arow.get("averageViewDuration")
            if isinstance(avg_dur, (int, float)) and 0.0 < float(avg_dur) <= 600.0:
                metrics_out["avg_watch_sec"] = round(float(avg_dur), 2)

            avg_pct = arow.get("averageViewPercentage")
            if isinstance(avg_pct, (int, float)) and 0.0 < float(avg_pct) <= 300.0:
                metrics_out["avg_pct_viewed"] = round(float(avg_pct), 1)

            est_mins = arow.get("estimatedMinutesWatched")
            if isinstance(est_mins, (int, float)) and float(est_mins) >= 0.0:
                metrics_out["total_watch_sec"] = round(float(est_mins) * 60.0, 1)

            shares_v = _safe_nonneg_int(arow.get("shares"))
            if shares_v is not None:
                metrics_out["shares"] = shares_v

            subs_v = _safe_nonneg_int(arow.get("subscribersGained"))
            if subs_v is not None:
                metrics_out["subscribers_gained"] = subs_v

        out_posts.append(
            {
                "post_id": f"yt_{vid}",
                "video_id": vid,
                "title": sv["title"],
                "description": sv["description"],
                "published_at": sv["published_at"],
                "permalink": f"https://www.youtube.com/shorts/{vid}",
                "duration_sec": sv["duration_sec"],
                "metrics": metrics_out,
            }
        )

    meta["last_sync_at"] = ts_str
    meta["last_synced_count"] = len(out_posts)
    meta["last_error"] = analytics_warn
    _save_meta(meta)

    detail_msg = (
        f"YouTubeショート {len(out_posts)}本（Analytics維持率 {analytics_hit_count}本）を自動同期"
        if not analytics_warn
        else f"YouTubeショート {len(out_posts)}本を同期（{analytics_warn}）"
    )
    return {
        "ok": True,
        "channel": "youtube_shorts",
        "status": "ok" if not analytics_warn else "partial_analytics",
        "synced": len(out_posts),
        "updated": len(out_posts),
        "analytics_fetched": analytics_hit_count,
        "posts": out_posts,
        "detail": detail_msg,
    }
