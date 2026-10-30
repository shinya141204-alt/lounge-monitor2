"""
tiktok_poster.py - TikTok Short Video Studio & Content Posting API v2 Inbox Client for Aiseki Navi.

Supports a 2-phase workflow:
- Phase 1 (Zero-config immediate operation): 1-tap 9:16 MP4 download (with TikTok UI safe-zones)
  + 1-tap TikTok SEO caption & cut-by-cut script copy so staff can attach trending TikTok BGM natively.
- Phase 2 (TikTok Content Posting API v2): Direct video push to the official TikTok mobile app Inbox
  (`/v2/post/publish/inbox/video/init/`) via `PULL_FROM_URL` with automatic `FILE_UPLOAD` fallback,
  backed by obfuscated `job_state` token persistence (`token:tiktok`).
"""

import datetime
import logging
import os
import threading

import requests

import job_state

TIKTOK_API_BASE = "https://open.tiktokapis.com/v2"
SITE_URL = "https://aiseki-navi.com"
TIKTOK_AUTO_ENABLED_KEY = "tiktok_auto_schedule_enabled"
_tiktok_post_lock = threading.Lock()

JST = datetime.timezone(datetime.timedelta(hours=9))


def _get_active_tiktok_token() -> str:
    """Return stored TikTok OAuth2 access token from `job_state`, falling back to `TIKTOK_ACCESS_TOKEN`."""
    try:
        stored = (job_state.get_value("token:tiktok") or "").strip()
        if stored:
            return stored
    except Exception:
        pass
    return (os.environ.get("TIKTOK_ACCESS_TOKEN") or "").strip()


def is_tiktok_token_configured() -> bool:
    return bool(_get_active_tiktok_token())


def is_tiktok_auto_schedule_enabled(allow_blocking: bool = False) -> bool:
    try:
        raw = (job_state.get_value(TIKTOK_AUTO_ENABLED_KEY, allow_blocking=allow_blocking) or "").strip().lower()
        return raw in ("1", "true", "on", "yes")
    except Exception:
        return False


def set_tiktok_auto_schedule_enabled(enabled: bool) -> bool:
    flag = bool(enabled)
    job_state.set_value(TIKTOK_AUTO_ENABLED_KEY, "1" if flag else "0")
    return flag


def verify_tiktok_token(access_token: str = None) -> dict:
    """Verify a TikTok OAuth2 user access token via `/v2/user/info/` without posting."""
    tok = (access_token if access_token is not None else _get_active_tiktok_token()).strip()
    if not tok:
        return {
            "ok": False,
            "status": "unconfigured",
            "phase": "phase1_manual",
            "error": "TIKTOK_ACCESS_TOKEN 未設定（現在は Phase 1：MP4 1タップ保存＋台本コピー運用で即稼働中）",
        }
    try:
        r = requests.get(
            f"{TIKTOK_API_BASE}/user/info/",
            params={"fields": "open_id,display_name,username"},
            headers={"Authorization": f"Bearer {tok}"},
            timeout=12,
        )
        if r.status_code == 200:
            payload = r.json() or {}
            err_obj = payload.get("error") or {}
            if err_obj.get("code") in (None, "", "ok"):
                u = (payload.get("data") or {}).get("user") or {}
                uname = str(u.get("username") or u.get("display_name") or u.get("open_id") or "tiktok_user").strip()
                return {
                    "ok": True,
                    "status": "active",
                    "phase": "phase2_api",
                    "id": str(u.get("open_id") or "").strip(),
                    "username": uname,
                }
            return {
                "ok": False,
                "status": "error",
                "error": f"TikTok API error ({err_obj.get('code')}): {err_obj.get('message', '')[:180]}",
            }
        return {"ok": False, "status": "error", "error": f"HTTP {r.status_code}: {r.text[:220]}"}
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e)}


def refresh_tiktok_token_if_needed(force: bool = False) -> dict:
    """Refresh TikTok OAuth2 token when `TIKTOK_CLIENT_KEY`, `TIKTOK_CLIENT_SECRET`, and refresh token exist."""
    client_key = (os.environ.get("TIKTOK_CLIENT_KEY") or "").strip()
    client_secret = (os.environ.get("TIKTOK_CLIENT_SECRET") or "").strip()
    refresh_tok = (
        (job_state.get_value("token:tiktok_refresh") or "").strip()
        or (os.environ.get("TIKTOK_REFRESH_TOKEN") or "").strip()
    )
    if not (client_key and client_secret and refresh_tok):
        return {"ok": False, "skipped": True, "reason": "no_refresh_credentials"}

    now_jst = datetime.datetime.now(JST)
    day_key = f"token_refresh:tiktok:{now_jst.strftime('%Y-%m-%d')}"
    if not force and job_state.is_done(day_key):
        return {"ok": True, "skipped": True, "reason": "already_refreshed_today"}

    try:
        r = requests.post(
            f"{TIKTOK_API_BASE}/oauth/token/",
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            data={
                "client_key": client_key,
                "client_secret": client_secret,
                "grant_type": "refresh_token",
                "refresh_token": refresh_tok,
            },
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json() or {}
            new_acc = (data.get("access_token") or "").strip()
            new_ref = (data.get("refresh_token") or "").strip()
            if new_acc:
                job_state.set_value("token:tiktok", new_acc)
                os.environ["TIKTOK_ACCESS_TOKEN"] = new_acc
                if new_ref:
                    job_state.set_value("token:tiktok_refresh", new_ref)
                job_state.mark_done(day_key)
                return {"ok": True, "refreshed": True}
        return {"ok": False, "error": f"HTTP {r.status_code}: {r.text[:180]}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}


def save_and_prepare_tiktok_token(raw_token: str, app_secret: str = "", refresh_token: str = "") -> dict:
    """Validate and save (or clear) TikTok Content Posting API v2 token in `job_state`."""
    cleaned = (raw_token or "").strip()
    if not cleaned:
        job_state.set_value("token:tiktok", "")
        job_state.set_value("token:tiktok_refresh", "")
        return {
            "ok": True,
            "cleared": True,
            "message": "管理画面のTikTokトークンを削除しました（Phase 1：MP4 1タップ保存＋台本コピー運用に戻りました）。",
        }

    ver = verify_tiktok_token(cleaned)
    if not ver.get("ok"):
        return {"ok": False, "error": f"TikTokトークンの疎通確認に失敗しました：{ver.get('error')}"}

    job_state.set_value("token:tiktok", cleaned)
    os.environ["TIKTOK_ACCESS_TOKEN"] = cleaned
    if refresh_token and refresh_token.strip():
        job_state.set_value("token:tiktok_refresh", refresh_token.strip())
    if app_secret and app_secret.strip():
        os.environ["TIKTOK_CLIENT_SECRET"] = app_secret.strip()

    try:
        import sns_editorial
        sns_editorial.record_sns_delivery_status("tiktok", "token_refreshed", detail="verified")
    except Exception:
        pass

    uname = ver.get("username") or "TikTok"
    return {
        "ok": True,
        "username": uname,
        "message": f"TikTok API v2 トークン（@{uname}）を暗号化保存しました！動画スタジオからアプリInbox下書き送信が利用できます。",
    }


def _upload_mp4_to_tiktok_inbox(access_token: str, video_url: str, mp4_bytes: bytes) -> dict:
    """
    Send 9:16 MP4 to TikTok mobile app Inbox (`/v2/post/publish/inbox/video/init/`).
    Tries `PULL_FROM_URL` first, then automatically falls back to single-chunk `FILE_UPLOAD`
    when URL domain verification is not configured on TikTok for Developers.
    """
    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json; charset=UTF-8",
    }

    # 1. Try PULL_FROM_URL (fastest, zero upload bandwidth from worker)
    try:
        r_pull = requests.post(
            f"{TIKTOK_API_BASE}/post/publish/inbox/video/init/",
            headers=headers,
            json={
                "source_info": {
                    "source": "PULL_FROM_URL",
                    "video_url": video_url,
                }
            },
            timeout=20,
        )
        if r_pull.status_code == 200:
            body = r_pull.json() or {}
            err_obj = body.get("error") or {}
            if err_obj.get("code") in (None, "", "ok"):
                pub_id = str((body.get("data") or {}).get("publish_id") or "").strip()
                return {
                    "status": "success",
                    "ok": True,
                    "method": "PULL_FROM_URL",
                    "publish_id": pub_id,
                    "id": pub_id,
                }
    except Exception as pull_err:
        logging.debug(f"TikTok PULL_FROM_URL attempt skipped, trying FILE_UPLOAD: {pull_err}")

    # 2. Fallback: FILE_UPLOAD (works even without URL prefix ownership verification)
    size_bytes = len(mp4_bytes or b"")
    if size_bytes <= 0:
        return {"status": "error", "ok": False, "detail": "Generated MP4 bytes are empty"}

    try:
        r_init = requests.post(
            f"{TIKTOK_API_BASE}/post/publish/inbox/video/init/",
            headers=headers,
            json={
                "source_info": {
                    "source": "FILE_UPLOAD",
                    "video_size": size_bytes,
                    "chunk_size": size_bytes,
                    "total_chunk_count": 1,
                }
            },
            timeout=20,
        )
        if r_init.status_code >= 400:
            return {
                "status": "error",
                "ok": False,
                "detail": f"TikTok Inbox init HTTP {r_init.status_code}: {r_init.text[:240]}",
            }
        init_body = r_init.json() or {}
        err_obj = init_body.get("error") or {}
        if err_obj.get("code") not in (None, "", "ok"):
            return {
                "status": "error",
                "ok": False,
                "detail": f"TikTok Inbox init ({err_obj.get('code')}): {err_obj.get('message', '')[:200]}",
            }

        data_obj = init_body.get("data") or {}
        upload_url = (data_obj.get("upload_url") or "").strip()
        pub_id = str(data_obj.get("publish_id") or "").strip()
        if not upload_url:
            return {"status": "error", "ok": False, "detail": "TikTok Inbox init returned empty upload_url"}

        r_put = requests.put(
            upload_url,
            headers={
                "Content-Type": "video/mp4",
                "Content-Length": str(size_bytes),
                "Content-Range": f"bytes 0-{size_bytes - 1}/{size_bytes}",
            },
            data=mp4_bytes,
            timeout=45,
        )
        if r_put.status_code >= 400:
            return {
                "status": "error",
                "ok": False,
                "detail": f"TikTok chunk upload HTTP {r_put.status_code}: {r_put.text[:200]}",
            }
        return {
            "status": "success",
            "ok": True,
            "method": "FILE_UPLOAD",
            "publish_id": pub_id,
            "id": pub_id,
        }
    except Exception as e:
        logging.error(f"TikTok Inbox upload exception: {e}")
        return {"status": "error", "ok": False, "detail": str(e)}


def publish_live_reel_to_tiktok_inbox(
    stores: list,
    reel_type: str = "tiktok_gap_shock",
    custom_caption: str = "",
    slot_label: str = "manual_tiktok",
    force: bool = True,
) -> dict:
    """
    Render the TikTok safe-zone 9:16 MP4 and send it to the TikTok mobile app Inbox (`video.upload` scope)
    when `TIKTOK_ACCESS_TOKEN` is configured, or return a helpful Phase 1 download-ready response when unconfigured.
    """
    if not _tiktok_post_lock.acquire(blocking=False):
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "already_running"}

    try:
        import reel_generator

        rt = reel_generator.normalize_reel_type(reel_type or "tiktok_gap_shock")
        now_jst = datetime.datetime.now(JST)
        cleaned_custom = reel_generator.enforce_brand_rule((custom_caption or "").strip())
        caption = cleaned_custom or reel_generator.build_reel_caption(stores, reel_type=rt, now_jst=now_jst)
        ts_code = now_jst.strftime("%Y%m%d_%H%M")
        video_url = f"{SITE_URL}/ogp/reel/{rt}.mp4?s={ts_code}"
        download_url = f"/ogp/reel/{rt}.mp4?download=1&s={ts_code}"

        refresh_tiktok_token_if_needed(force=False)
        tok = _get_active_tiktok_token()
        if not tok:
            return {
                "status": "phase1_manual",
                "ok": False,
                "phase": "phase1_manual",
                "reel_type": rt,
                "caption": caption,
                "video_url": video_url,
                "download_url": download_url,
                "detail": (
                    "現在は Phase 1 運用モード（APIトークン未登録）です。"
                    "「⬇ MP4動画を保存」から9:16動画を保存し、コピーした台本・キャプションとTikTokアプリのトレンド音源を合わせて投稿してください！"
                    "（TikTok for Developersのトークンを登録するとアプリInboxへの直接下書き送信が有効になります）"
                ),
            }

        try:
            mp4_bytes = reel_generator.render_reel_mp4_bytes(stores, reel_type=rt)
        except Exception as warm_err:
            err_detail = f"TikTok MP4 generation failed: {warm_err}"
            logging.error(err_detail)
            return {"status": "error", "ok": False, "detail": err_detail, "reel_type": rt}

        res = _upload_mp4_to_tiktok_inbox(tok, video_url=video_url, mp4_bytes=mp4_bytes)
        res["caption"] = caption
        res["video_url"] = video_url
        res["download_url"] = download_url
        res["reel_type"] = rt

        try:
            import sns_editorial
            if res.get("status") == "success":
                sns_editorial.record_sns_delivery_status(
                    "tiktok",
                    "success",
                    slot_label=slot_label,
                    post_id=res.get("publish_id") or "",
                    now_jst=now_jst,
                )
            else:
                sns_editorial.record_sns_delivery_status(
                    "tiktok",
                    "error",
                    slot_label=slot_label,
                    detail=res.get("detail") or "",
                    now_jst=now_jst,
                )
        except Exception:
            pass

        return res
    finally:
        _tiktok_post_lock.release()
