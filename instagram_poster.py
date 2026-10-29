"""
instagram_poster.py - Meta Instagram Graph API Automated Carousel & Feed Poster for Aiseki Navi.

Features:
1. Publicly serves signed/time-busted 1080x1350 PNG slides (`/ogp/ig-slide/<1..5>.png`) so Meta's Graph API servers can fetch them directly without requiring external S3/Cloudinary storage.
2. Publishes 5-slide Weekend Forecast Carousels (`media_type=CAROUSEL`) or Daily Live Ranking Single Feed posts (`image_url` + caption) to Instagram.
3. Auto-resolves `INSTAGRAM_USER_ID` from `INSTAGRAM_ACCESS_TOKEN` via `/me?fields=id,username` (or Facebook Page linked `instagram_business_account`).
4. Graceful skip when `INSTAGRAM_ACCESS_TOKEN` is not configured.
"""

import datetime
import logging
import os
import re
import sys
import threading
import time

import requests

import ig_carousel_generator
import job_state
import threads_poster

IG_GRAPH_BASE = "https://graph.instagram.com/v21.0"
FB_GRAPH_BASE = "https://graph.facebook.com/v21.0"
DEFAULT_IG_USER_ID = "17841410169754469"  # @aiseki_navi1 professional user_id
SITE_URL = "https://aiseki-navi.com"
REEL_AUTO_ENABLED_KEY = "reel_auto_schedule_enabled"
_cached_ig_user_id = None
_ig_post_lock = threading.Lock()


def is_reel_auto_schedule_enabled(allow_blocking: bool = False) -> bool:
    """Return whether automated Instagram Reels scheduled posting is enabled (default False)."""
    try:
        raw = (job_state.get_value(REEL_AUTO_ENABLED_KEY, allow_blocking=allow_blocking) or "").strip().lower()
        return raw in ("1", "true", "on", "yes")
    except Exception:
        return False


def set_reel_auto_schedule_enabled(enabled: bool) -> bool:
    """Persist automated Instagram Reels scheduled posting toggle in `job_state`."""
    flag = bool(enabled)
    job_state.set_value(REEL_AUTO_ENABLED_KEY, "1" if flag else "0")
    return flag


def _warm_slide_urls(urls: list, ctx: dict = None) -> None:
    """
    Pre-render Instagram slide images in-process (never via self-HTTP `requests.get`,
    which risks thread-pool self-deadlock on single-worker Gunicorn) and populate
    `app._sns_slide_bytes_cache` via `app._render_cached_ig_slide_bytes` when available.
    """
    if not urls:
        return
    app_mod = sys.modules.get("app")
    render_cached_fn = getattr(app_mod, "_render_cached_ig_slide_bytes", None) if app_mod else None
    warm_fn = getattr(app_mod, "warm_ig_slide_cache", None) if app_mod else None

    resolved_ctx = ctx
    if app_mod and hasattr(app_mod, "_get_sns_kit_context"):
        try:
            # Align context signature with `/ogp/ig-slide/<n>.jpg` route so Meta's fetch hits RAM cache
            resolved_ctx = app_mod._get_sns_kit_context(fast=False) or ctx
        except Exception:
            resolved_ctx = ctx

    for url in urls[:10]:
        if not url or "/ogp/ig-slide/" not in url:
            continue
        m = re.search(r"/ogp/ig-slide/(\d+)\.(jpg|jpeg|png)", url, re.IGNORECASE)
        if not m:
            continue
        slide_num = max(1, min(5, int(m.group(1))))
        is_jpg = m.group(2).lower() in ("jpg", "jpeg")
        try:
            if callable(warm_fn):
                warm_fn(slide_num, is_jpg=is_jpg, ctx=resolved_ctx)
            elif callable(render_cached_fn):
                if resolved_ctx is None:
                    resolved_ctx = ig_carousel_generator.build_forecast_context()
                render_cached_fn(slide_num, is_jpg, resolved_ctx)
            else:
                if resolved_ctx is None:
                    resolved_ctx = ig_carousel_generator.build_forecast_context()
                if is_jpg:
                    ig_carousel_generator.render_slide_jpeg(slide_num, ctx=resolved_ctx, quality=90)
                else:
                    ig_carousel_generator.render_slide_png(slide_num, ctx=resolved_ctx)
        except Exception as e:
            logging.debug(f"In-process IG slide warm skipped (slide={slide_num}): {e}")


def _resolve_ig_user_id(access_token: str) -> str:
    """Resolve Instagram professional account user_id from env var or Graph API."""
    global _cached_ig_user_id
    env_uid = (os.environ.get("INSTAGRAM_USER_ID") or "").strip()
    if env_uid:
        return env_uid
    if _cached_ig_user_id:
        return _cached_ig_user_id

    # 1. Try Instagram Graph API (/me) for Instagram Login tokens (IGAA... / IGQ...)
    for base in (IG_GRAPH_BASE, FB_GRAPH_BASE):
        try:
            r = requests.get(
                f"{base}/me",
                params={"fields": "id,username,user_id", "access_token": access_token},
                timeout=10,
            )
            if r.status_code == 200:
                data = r.json() or {}
                uid = str(data.get("user_id") or data.get("id") or "").strip()
                if uid and data.get("username"):
                    _cached_ig_user_id = uid
                    return uid
        except Exception:
            pass

    # 2. Try Facebook Page linked instagram_business_account for EAA... tokens
    try:
        r = requests.get(
            f"{FB_GRAPH_BASE}/me/accounts",
            params={"fields": "instagram_business_account{id,username}", "access_token": access_token},
            timeout=10,
        )
        if r.status_code == 200:
            pages = (r.json() or {}).get("data") or []
            for p in pages:
                ig_acct = p.get("instagram_business_account") or {}
                uid = str(ig_acct.get("id") or "").strip()
                if uid:
                    _cached_ig_user_id = uid
                    return uid
    except Exception:
        pass

    return DEFAULT_IG_USER_ID


def _get_active_ig_token() -> str:
    """Return the latest refreshed Instagram token from job_state, falling back to INSTAGRAM_ACCESS_TOKEN env var."""
    stored = (job_state.get_value("token:instagram") or "").strip()
    if stored:
        return stored
    return (os.environ.get("INSTAGRAM_ACCESS_TOKEN") or "").strip()


def verify_ig_token(access_token: str = None) -> dict:
    """Verify an Instagram Graph API token without creating any post."""
    tok = (access_token if access_token is not None else _get_active_ig_token()).strip()
    if not tok:
        return {"ok": False, "status": "unconfigured", "error": "INSTAGRAM_ACCESS_TOKEN が未設定です"}
    base = _graph_base_for_token(tok)
    try:
        if tok.startswith("EAA"):
            uid = _resolve_ig_user_id(tok)
            r = requests.get(
                f"{base}/{uid}",
                params={"fields": "id,username", "access_token": tok},
                timeout=12,
            )
        else:
            r = requests.get(
                f"{base}/me",
                params={"fields": "id,username", "access_token": tok},
                timeout=12,
            )
        if r.status_code == 200:
            data = r.json() or {}
            return {
                "ok": True,
                "status": "active",
                "id": str(data.get("id") or "").strip(),
                "username": str(data.get("username") or "").strip(),
            }
        return {"ok": False, "status": "error", "error": f"HTTP {r.status_code}: {r.text[:220]}"}
    except Exception as e:
        return {"ok": False, "status": "error", "error": str(e)}


def save_and_prepare_ig_token(raw_token: str, app_secret: str = "") -> dict:
    """Validate, optionally exchange/refresh to 60-day long-lived IG token, and persist in obfuscated `job_state`."""
    global _cached_ig_user_id
    cleaned = (raw_token or "").strip()
    if not cleaned:
        job_state.set_value("token:instagram", "")
        _cached_ig_user_id = None
        return {"ok": True, "cleared": True, "message": "管理画面のInstagramトークンを削除しました。"}

    eff_secret = (app_secret or os.environ.get("INSTAGRAM_APP_SECRET") or "").strip()
    best_token = cleaned
    exchanged = False

    if eff_secret and not best_token.startswith("EAA"):
        try:
            rx = requests.get(
                "https://graph.instagram.com/access_token",
                params={
                    "grant_type": "ig_exchange_token",
                    "client_secret": eff_secret,
                    "access_token": best_token,
                },
                timeout=15,
            )
            if rx.status_code == 200:
                xdata = rx.json() or {}
                cand_tok = (xdata.get("access_token") or "").strip()
                if cand_tok:
                    best_token = cand_tok
                    exchanged = True
        except Exception:
            pass

    if not exchanged and not best_token.startswith("EAA"):
        try:
            rr = requests.get(
                "https://graph.instagram.com/refresh_access_token",
                params={"grant_type": "ig_refresh_token", "access_token": best_token},
                timeout=15,
            )
            if rr.status_code == 200:
                rdata = rr.json() or {}
                cand_tok = (rdata.get("access_token") or "").strip()
                if cand_tok:
                    best_token = cand_tok
                    exchanged = True
        except Exception:
            pass

    ver = verify_ig_token(best_token)
    if not ver.get("ok"):
        return {"ok": False, "error": f"Instagramトークンの疎通確認に失敗しました：{ver.get('error')}"}

    job_state.set_value("token:instagram", best_token)
    os.environ["INSTAGRAM_ACCESS_TOKEN"] = best_token
    if exchanged:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        iso_year, iso_week, _ = now_jst.isocalendar()
        job_state.mark_done(f"token_refresh:instagram:{iso_year}-W{iso_week:02d}")
    try:
        import sns_editorial
        sns_editorial.record_sns_delivery_status("instagram", "token_refreshed", detail="verified")
    except Exception:
        pass
    uname = ver.get("username") or ver.get("id") or "Instagram"
    return {
        "ok": True,
        "username": uname,
        "id": ver.get("id"),
        "exchanged_long_lived": exchanged,
        "message": f"@{uname} のInstagramトークンを暗号化保存しました！",
    }


def refresh_ig_token_if_needed(force: bool = False) -> dict:
    """
    Meta Instagram long-lived tokens (IGAA.../IGQ...) expire after 60 days.
    Automatically refresh once per ISO week via:
      GET https://graph.instagram.com/refresh_access_token?grant_type=ig_refresh_token&access_token=<current_token>
    and persist the renewed token in job_state ('token:instagram') so manual env var updates are never needed.
    """
    token = _get_active_ig_token()
    if not token:
        return {"status": "skipped", "reason": "No INSTAGRAM_ACCESS_TOKEN to refresh"}
    if token.startswith("EAA"):
        return {"status": "skipped", "reason": "EAA page token managed via Meta Business"}

    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    iso_year, iso_week, _ = now_jst.isocalendar()
    week_key = f"token_refresh:instagram:{iso_year}-W{iso_week:02d}"
    if not force and job_state.is_done(week_key):
        return {"status": "skipped", "reason": f"Already refreshed in {iso_year}-W{iso_week:02d}"}

    try:
        r = requests.get(
            "https://graph.instagram.com/refresh_access_token",
            params={
                "grant_type": "ig_refresh_token",
                "access_token": token,
            },
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json() or {}
            new_tok = (data.get("access_token") or "").strip()
            if new_tok:
                job_state.set_value("token:instagram", new_tok)
                job_state.mark_done(week_key)
                os.environ["INSTAGRAM_ACCESS_TOKEN"] = new_tok
                logging.info("Successfully refreshed Instagram long-lived access token.")
                try:
                    import sns_editorial
                    sns_editorial.record_sns_delivery_status("instagram", "token_refreshed", now_jst=now_jst)
                except Exception:
                    pass
                return {"status": "refreshed", "expires_in": data.get("expires_in")}
        err_detail = f"HTTP {r.status_code}: {r.text[:200]}"
        ver = verify_ig_token(token)
        if not ver.get("ok"):
            try:
                import sns_editorial
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    "token_error",
                    detail=ver.get("error") or err_detail,
                    now_jst=now_jst,
                )
            except Exception:
                pass
        return {"status": "error", "detail": err_detail}
    except Exception as e:
        logging.warning(f"Instagram token refresh exception: {e}")
        return {"status": "error", "detail": str(e)}


def _graph_base_for_token(access_token: str) -> str:
    """Choose graph.instagram.com for IG tokens, graph.facebook.com for EAA tokens."""
    if access_token.startswith("EAA"):
        return FB_GRAPH_BASE
    return IG_GRAPH_BASE


def _is_ig_publish_not_ready(resp) -> bool:
    """Detect transient IG error 9007 / 2207027 ('Media ID is not available' / 'not ready') during publish."""
    if getattr(resp, "status_code", None) != 400:
        return False
    body_low = (getattr(resp, "text", "") or "").lower()
    return (
        "9007" in body_low
        or "2207027" in body_low
        or "not ready" in body_low
        or "media id is not available" in body_low
    )


def _wait_for_ig_container_ready(
    base: str,
    creation_id: str,
    access_token: str,
    max_checks: int = 12,
    interval_sec: float = 3.0,
) -> tuple:
    """
    Poll `GET /{creation_id}?fields=status_code` up to `max_checks` times until `FINISHED`.
    Exits immediately on mocks or 4xx responses lacking `status_code` so unit tests never stall,
    while continuing across transient 5xx or network exceptions.
    """
    if not creation_id:
        return False, "Missing creation_id"
    wait_s = 0.05 if os.environ.get("DISABLE_SCHEDULER") == "1" else interval_sec

    for attempt in range(max_checks):
        try:
            r = requests.get(
                f"{base}/{creation_id}",
                params={"fields": "status_code", "access_token": access_token},
                timeout=10,
            )
            sc = getattr(r, "status_code", None)
            if not isinstance(sc, int) or (400 <= sc < 500):
                return True, "status_check_unsupported"
            if sc >= 500:
                logging.warning(
                    f"IG container status check HTTP {sc} for {creation_id} (attempt {attempt + 1}/{max_checks})"
                )
            else:
                data = r.json() if hasattr(r, "json") else {}
                if not isinstance(data, dict) or "status_code" not in data:
                    return True, "no_status_code_field"
                code = str(data.get("status_code") or "").upper()
                if code in ("FINISHED", "PUBLISHED"):
                    return True, code
                if code in ("ERROR", "EXPIRED"):
                    return False, f"IG container status={code}: {data}"
        except Exception as e:
            logging.warning(
                f"IG container status check transient exception (attempt {attempt + 1}/{max_checks}): {e}"
            )

        if attempt < max_checks - 1:
            time.sleep(wait_s)

    return True, "timeout_proceed"


def post_single_image_to_instagram(image_url: str, caption: str) -> dict:
    """
    Publish a single image feed post to Instagram:
      1. POST /{ig_user_id}/media (image_url, caption)
      2. Poll `status_code` until FINISHED
      3. POST /{ig_user_id}/media_publish (creation_id)
    """
    access_token = _get_active_ig_token()
    if not access_token:
        return {"status": "skipped", "reason": "INSTAGRAM_ACCESS_TOKEN not configured"}

    ig_user_id = _resolve_ig_user_id(access_token)
    if not ig_user_id:
        return {"status": "error", "detail": "Could not resolve INSTAGRAM_USER_ID from token"}

    base = _graph_base_for_token(access_token)
    pub_retry_wait = 0.05 if os.environ.get("DISABLE_SCHEDULER") == "1" else 3.0
    try:
        _warm_slide_urls([image_url])

        r1 = requests.post(
            f"{base}/{ig_user_id}/media",
            data={
                "image_url": image_url,
                "caption": caption[:2100],
                "access_token": access_token,
            },
            timeout=25,
        )
        if r1.status_code >= 400:
            return {"status": "error", "detail": f"IG media create HTTP {r1.status_code}: {r1.text[:200]}"}
        creation_id = (r1.json() or {}).get("id")
        if not creation_id:
            return {"status": "error", "detail": f"Missing creation_id: {r1.text[:200]}"}

        ok_ready, ready_detail = _wait_for_ig_container_ready(base, creation_id, access_token)
        if not ok_ready:
            return {"status": "error", "detail": ready_detail}

        r2 = None
        for pub_attempt in range(3):
            r2 = requests.post(
                f"{base}/{ig_user_id}/media_publish",
                data={
                    "creation_id": creation_id,
                    "access_token": access_token,
                },
                timeout=25,
            )
            if r2.status_code < 400:
                break
            if pub_attempt < 2 and _is_ig_publish_not_ready(r2):
                logging.warning(
                    f"IG single media_publish not ready yet (attempt {pub_attempt + 1}/3); retrying in {pub_retry_wait}s: {r2.text[:160]}"
                )
                time.sleep(pub_retry_wait)
                continue
            break
        if r2.status_code >= 400:
            return {"status": "error", "detail": f"IG media publish HTTP {r2.status_code}: {r2.text[:200]}"}

        post_id = (r2.json() or {}).get("id")
        logging.info(f"Successfully published single image to Instagram: id={post_id}")
        return {"status": "success", "id": post_id, "image_url": image_url}
    except Exception as e:
        logging.error(f"Instagram single post exception: {e}")
        return {"status": "error", "detail": str(e)}


def post_carousel_to_instagram(image_urls: list, caption: str) -> dict:
    """
    Publish a multi-slide 4:5 portrait carousel (up to 10 images) to Instagram:
      1. For each image_url: POST /{ig_user_id}/media (is_carousel_item=true, image_url=...)
      2. POST /{ig_user_id}/media (media_type=CAROUSEL, children=id1,id2,..., caption=...)
      3. Poll `status_code` until FINISHED, then POST /{ig_user_id}/media_publish (creation_id=carousel_container_id)
    """
    access_token = _get_active_ig_token()
    if not access_token:
        return {"status": "skipped", "reason": "INSTAGRAM_ACCESS_TOKEN not configured"}

    ig_user_id = _resolve_ig_user_id(access_token)
    if not ig_user_id:
        return {"status": "error", "detail": "Could not resolve INSTAGRAM_USER_ID from token"}

    base = _graph_base_for_token(access_token)
    child_ids = []
    retry_wait = 0.05 if os.environ.get("DISABLE_SCHEDULER") == "1" else 2.0
    pub_retry_wait = 0.05 if os.environ.get("DISABLE_SCHEDULER") == "1" else 3.0
    try:
        # Warm up all slide images in-process (never via self-HTTP loopback) before Meta's crawler fetches them
        _warm_slide_urls(image_urls[:10])

        for idx, url in enumerate(image_urls[:10]):
            cid = None
            last_err = ""
            for attempt in range(3):
                r_item = requests.post(
                    f"{base}/{ig_user_id}/media",
                    data={
                        "image_url": url,
                        "is_carousel_item": "true",
                        "access_token": access_token,
                    },
                    timeout=25,
                )
                if r_item.status_code < 400:
                    cid = (r_item.json() or {}).get("id")
                    if cid:
                        break
                last_err = f"Slide {idx+1} HTTP {r_item.status_code}: {r_item.text[:200]}"
                time.sleep(retry_wait)
            if not cid:
                return {"status": "error", "detail": last_err or f"Missing child id on slide {idx+1}"}
            ok_child, child_detail = _wait_for_ig_container_ready(base, cid, access_token)
            if not ok_child:
                return {"status": "error", "detail": f"Slide {idx+1} container failed: {child_detail}"}
            child_ids.append(cid)

        r_car = requests.post(
            f"{base}/{ig_user_id}/media",
            data={
                "media_type": "CAROUSEL",
                "children": ",".join(child_ids),
                "caption": caption[:2100],
                "access_token": access_token,
            },
            timeout=25,
        )
        if r_car.status_code >= 400:
            return {"status": "error", "detail": f"Carousel container HTTP {r_car.status_code}: {r_car.text[:200]}"}
        carousel_id = (r_car.json() or {}).get("id")
        if not carousel_id:
            return {"status": "error", "detail": f"Missing carousel_id: {r_car.text[:200]}"}

        ok_car, car_detail = _wait_for_ig_container_ready(base, carousel_id, access_token)
        if not ok_car:
            return {"status": "error", "detail": car_detail}

        r_pub = None
        for pub_attempt in range(3):
            r_pub = requests.post(
                f"{base}/{ig_user_id}/media_publish",
                data={
                    "creation_id": carousel_id,
                    "access_token": access_token,
                },
                timeout=25,
            )
            if r_pub.status_code < 400:
                break
            if pub_attempt < 2 and _is_ig_publish_not_ready(r_pub):
                logging.warning(
                    f"IG carousel media_publish not ready yet (attempt {pub_attempt + 1}/3); retrying in {pub_retry_wait}s: {r_pub.text[:160]}"
                )
                time.sleep(pub_retry_wait)
                continue
            break
        if r_pub.status_code >= 400:
            return {"status": "error", "detail": f"Carousel publish HTTP {r_pub.status_code}: {r_pub.text[:200]}"}

        post_id = (r_pub.json() or {}).get("id")
        logging.info(f"Successfully published 5-slide carousel to Instagram: id={post_id}")
        return {"status": "success", "id": post_id, "children": child_ids}
    except Exception as e:
        logging.error(f"Instagram carousel exception: {e}")
        return {"status": "error", "detail": str(e)}


def execute_scheduled_instagram_post(stores: list, mode: str = "carousel", slot_label: str = "22:00", force: bool = False) -> dict:
    """
    Execute scheduled Instagram post with persistent job_state deduplication and weekly token auto-refresh.
    - mode="carousel": 5-slide 1080x1350 portrait forecast & ranking carousel (connected to real weekly_report stats)
    - mode="single_live" / "live": single live ranking card + detailed caption (guarded by domestic live headcount)
    """
    if not _ig_post_lock.acquire(blocking=False):
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "already_running"}

    try:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        biz_date = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        dedup_key = f"ig_post:{biz_date}_{mode}_{slot_label}"

        if not force and job_state.is_done(dedup_key):
            return {"status": "skipped", "reason": "Already posted to Instagram for this slot"}

        if mode != "carousel" and not force:
            dom = threads_poster._filter_domestic_stores(stores)
            total_dom_women = sum(int(s.get("women") or 0) for s in dom)
            if total_dom_women < 15:
                logging.info(f"Skipping Instagram live post ({slot_label}): insufficient live headcount (women={total_dom_women}).")
                return {"status": "skipped", "reason": "Insufficient live headcount"}

        refresh_ig_token_if_needed(force=False)
        ts_code = now_jst.strftime("%Y%m%d_%H%M")
        if mode == "carousel":
            rep = None
            try:
                import weekly_report
                rep_cand = weekly_report.generate_weekly_report()
                if isinstance(rep_cand, dict) and "error" not in rep_cand:
                    rep = rep_cand
            except Exception as e:
                logging.warning(f"Failed to load weekly report for Instagram carousel: {e}")
                rep = None

            ctx = ig_carousel_generator.build_forecast_context(report=rep, live_stores=stores, now_jst=now_jst)
            editorial_winner = None
            ed_session_record = None
            try:
                import sns_editorial
                ed_res = sns_editorial.run_editorial_pipeline(
                    stores,
                    slot_label=slot_label,
                    channel="instagram",
                    ig_ctx=ctx,
                    now_jst=now_jst,
                    record_history=False,
                    use_ai=True,
                    force_ai_refresh=True,
                )
                editorial_winner = ed_res.get("winner")
                ed_session_record = ed_res.get("session_record")
            except Exception as e:
                logging.warning(f"Instagram editorial pipeline fallback ({slot_label}): {e}")

            if editorial_winner and editorial_winner.get("main_text"):
                caption = editorial_winner["main_text"]
            else:
                captions = ig_carousel_generator.build_captions(ctx)
                caption = captions.get("instagram_caption") or ""
            slide_urls = [
                f"{SITE_URL}/ogp/ig-slide/{i}.jpg?s={ts_code}"
                for i in range(1, 6)
            ]
            _warm_slide_urls(slide_urls, ctx=ctx)
            res = post_carousel_to_instagram(slide_urls, caption)
            res["caption"] = caption
            res["slide_urls"] = slide_urls
            if editorial_winner:
                res["editorial_style"] = editorial_winner.get("style")
                res["editorial_score"] = editorial_winner.get("total_score")
        else:
            ed_session_record = None
            live_ctx = ig_carousel_generator.build_forecast_context(live_stores=stores, now_jst=now_jst)
            editorial_winner = None
            try:
                import sns_editorial
                ed_res = sns_editorial.run_editorial_pipeline(
                    stores,
                    slot_label=slot_label,
                    channel="instagram",
                    ig_ctx=live_ctx,
                    now_jst=now_jst,
                    record_history=False,
                )
                editorial_winner = ed_res.get("winner")
            except Exception:
                editorial_winner = None
            caption = (
                editorial_winner["main_text"]
                if (editorial_winner and editorial_winner.get("main_text"))
                else ig_carousel_generator.build_captions(live_ctx).get("instagram_caption")
                or threads_poster.build_threads_live_post(stores, slot_label=slot_label, now_jst=now_jst)
            )
            image_url = f"{SITE_URL}/ogp/ig-slide/2.jpg?s={ts_code}"
            _warm_slide_urls([image_url], ctx=live_ctx)
            res = post_single_image_to_instagram(image_url, caption)
            res["caption"] = caption
            if editorial_winner:
                res["editorial_style"] = editorial_winner.get("style")
                res["editorial_score"] = editorial_winner.get("total_score")

        try:
            import sns_editorial
            if res.get("status") == "success":
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    "success",
                    slot_label=slot_label,
                    post_id=res.get("id") or "",
                    now_jst=now_jst,
                )
                if not force:
                    job_state.mark_done(dedup_key)
                    if ed_session_record:
                        sns_editorial.persist_editorial_session_record("instagram", ed_session_record)
            else:
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    res.get("status") or "error",
                    slot_label=slot_label,
                    detail=res.get("detail") or res.get("reason") or "",
                    now_jst=now_jst,
                )
        except Exception:
            if res.get("status") == "success" and not force:
                job_state.mark_done(dedup_key)
        return res
    finally:
        _ig_post_lock.release()


def execute_scheduled_ladies_instagram_post(
    stores: list,
    slot_label: str = "19:45",
    force: bool = False,
) -> dict:
    """
    Execute scheduled women-perspective Instagram Feed alert ('🎁 今夜の女子得＆待ちゼロ速報', Plan E).
    Guarded by persistent job_state deduplication and domestic live male headcount (`men >= 12`).
    """
    if not _ig_post_lock.acquire(blocking=False):
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "already_running"}

    try:
        import ladies_guide

        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        biz_date = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        dedup_key = f"ig_ladies:{biz_date}_{slot_label}"

        if not force and job_state.is_done(dedup_key):
            return {"status": "skipped", "reason": "Already posted ladies alert to Instagram for this slot"}

        dom = threads_poster._filter_domestic_stores(stores)
        total_dom_men = sum(int(s.get("men") or 0) for s in dom)
        if not force and total_dom_men < 12:
            logging.info(
                f"Skipping Instagram ladies post ({slot_label}): insufficient live male headcount (men={total_dom_men})."
            )
            return {"status": "skipped", "reason": "Insufficient live male headcount for ladies alert"}

        refresh_ig_token_if_needed(force=False)
        ts_code = now_jst.strftime("%Y%m%d_%H%M")
        live_ctx = ig_carousel_generator.build_forecast_context(live_stores=stores, now_jst=now_jst)
        caption = ladies_guide.build_instagram_ladies_caption(stores, slot_label=slot_label, now_jst=now_jst, use_ai=True)
        image_url = f"{SITE_URL}/ogp/ig-slide/2.jpg?s={ts_code}"
        _warm_slide_urls([image_url], ctx=live_ctx)
        res = post_single_image_to_instagram(image_url, caption)
        res["caption"] = caption
        res["editorial_style"] = "ig_ladies_night_guide"
        try:
            import sns_editorial
            if res.get("status") == "success":
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    "success",
                    slot_label=slot_label,
                    post_id=res.get("id") or "",
                    now_jst=now_jst,
                )
            else:
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    res.get("status") or "error",
                    slot_label=slot_label,
                    detail=res.get("detail") or res.get("reason") or "",
                    now_jst=now_jst,
                )
        except Exception:
            pass
        if res.get("status") == "success" and not force:
            job_state.mark_done(dedup_key)
        return res
    finally:
        _ig_post_lock.release()


def _publish_ig_container(
    base: str,
    ig_user_id: str,
    creation_id: str,
    access_token: str,
    max_attempts: int = 3,
    retry_wait: float = None,
):
    """Publish a prepared IG media/reel container with transient 9007/2207027 not-ready retry."""
    wait_s = retry_wait if retry_wait is not None else (0.05 if os.environ.get("DISABLE_SCHEDULER") == "1" else 3.0)
    resp = None
    for pub_attempt in range(max_attempts):
        resp = requests.post(
            f"{base}/{ig_user_id}/media_publish",
            data={
                "creation_id": creation_id,
                "access_token": access_token,
            },
            timeout=25,
        )
        if resp.status_code < 400:
            break
        if pub_attempt < max_attempts - 1 and _is_ig_publish_not_ready(resp):
            logging.warning(
                f"IG media_publish not ready yet for {creation_id} (attempt {pub_attempt + 1}/{max_attempts}); "
                f"retrying in {wait_s}s: {resp.text[:160]}"
            )
            time.sleep(wait_s)
            continue
        break
    return resp


def post_reel_to_instagram(video_url: str, caption: str, share_to_feed: bool = True) -> dict:
    """
    Publish a 9:16 MP4 Reel to Instagram via Meta Graph API:
      1. POST /{ig_user_id}/media (media_type=REELS, video_url=..., caption=..., share_to_feed=...)
      2. Poll GET /{creation_id}?fields=status_code,status up to 20 times (3s interval, 60s max) until FINISHED
      3. Publish via `_publish_ig_container` (`media_publish`)
    """
    access_token = _get_active_ig_token()
    if not access_token:
        return {"status": "skipped", "ok": False, "reason": "INSTAGRAM_ACCESS_TOKEN not configured"}

    ig_user_id = _resolve_ig_user_id(access_token)
    if not ig_user_id:
        return {"status": "error", "ok": False, "detail": "Could not resolve INSTAGRAM_USER_ID from token"}

    base = _graph_base_for_token(access_token)
    poll_wait = 0.05 if os.environ.get("DISABLE_SCHEDULER") == "1" else 3.0

    try:
        r1 = requests.post(
            f"{base}/{ig_user_id}/media",
            data={
                "media_type": "REELS",
                "video_url": video_url,
                "caption": (caption or "")[:2100],
                "share_to_feed": "true" if share_to_feed else "false",
                "access_token": access_token,
            },
            timeout=30,
        )
        if r1.status_code >= 400:
            return {
                "status": "error",
                "ok": False,
                "detail": f"IG Reel container HTTP {r1.status_code}: {r1.text[:240]}",
            }
        creation_id = (r1.json() or {}).get("id")
        if not creation_id:
            return {
                "status": "error",
                "ok": False,
                "detail": f"Missing Reel creation_id: {r1.text[:200]}",
            }

        max_checks = 20
        last_code = ""
        for attempt in range(max_checks):
            try:
                r_poll = requests.get(
                    f"{base}/{creation_id}",
                    params={"fields": "status_code,status", "access_token": access_token},
                    timeout=12,
                )
                sc = getattr(r_poll, "status_code", None)
                if not isinstance(sc, int) or (400 <= sc < 500):
                    break
                if sc >= 500:
                    logging.warning(
                        f"IG Reel status check HTTP {sc} for {creation_id} (attempt {attempt + 1}/{max_checks})"
                    )
                else:
                    pdata = r_poll.json() if hasattr(r_poll, "json") else {}
                    if not isinstance(pdata, dict) or "status_code" not in pdata:
                        break
                    code = str(pdata.get("status_code") or "").upper()
                    status_msg = str(pdata.get("status") or "").strip()
                    last_code = code
                    if code in ("FINISHED", "PUBLISHED"):
                        break
                    if code in ("ERROR", "EXPIRED"):
                        err_info = f"{status_msg} | {pdata}" if status_msg else str(pdata)
                        return {
                            "status": "error",
                            "ok": False,
                            "detail": f"IG Reel container status={code}: {err_info[:260]}",
                        }
            except Exception as poll_err:
                logging.warning(
                    f"IG Reel container poll transient exception (attempt {attempt + 1}/{max_checks}): {poll_err}"
                )

            if attempt < max_checks - 1:
                time.sleep(poll_wait)

        r2 = _publish_ig_container(base, ig_user_id, creation_id, access_token)
        if r2.status_code >= 400:
            extra_state = f" (last_poll={last_code})" if last_code else ""
            return {
                "status": "error",
                "ok": False,
                "detail": f"IG Reel publish HTTP {r2.status_code}{extra_state}: {r2.text[:240]}",
            }

        post_id = (r2.json() or {}).get("id")
        logging.info(f"Successfully published Reel to Instagram: id={post_id} url={video_url}")
        return {
            "status": "success",
            "ok": True,
            "id": post_id,
            "creation_id": creation_id,
            "video_url": video_url,
        }
    except Exception as e:
        logging.error(f"Instagram Reel post exception: {e}")
        return {"status": "error", "ok": False, "detail": str(e)}


def publish_live_reel_to_instagram(
    stores: list,
    reel_type: str = "live_top5",
    custom_caption: str = "",
    share_to_feed: bool = True,
    slot_label: str = "manual_reel",
    force: bool = True,
) -> dict:
    """
    Warm-generate the live 9:16 MP4 Reel in-process, publish via Meta Instagram Graph API
    using `{SITE_URL}/ogp/reel/{reel_type}.mp4?s={ts_code}`, and record delivery status.
    """
    if not _ig_post_lock.acquire(blocking=False):
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "already_running"}

    try:
        import reel_generator

        rt = reel_generator.normalize_reel_type(reel_type)
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        biz_date = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        dedup_key = f"ig_post:{biz_date}_reel_{rt}_{slot_label}"

        if not force:
            if not is_reel_auto_schedule_enabled(allow_blocking=False):
                return {"status": "skipped", "ok": False, "reason": "Reel auto-schedule is disabled"}
            if job_state.is_done(dedup_key):
                return {"status": "skipped", "ok": False, "reason": "Already posted Reel to Instagram for this slot"}

            dom = threads_poster._filter_domestic_stores(stores)
            if rt == "ladies_zero_wait":
                total_dom_men = sum(int(s.get("men") or 0) for s in dom)
                if total_dom_men < 12:
                    logging.info(
                        f"Skipping scheduled IG ladies Reel ({slot_label}): insufficient male headcount (men={total_dom_men})."
                    )
                    return {"status": "skipped", "ok": False, "reason": "Insufficient live male headcount"}
            else:
                total_dom_women = sum(int(s.get("women") or 0) for s in dom)
                if total_dom_women < 15:
                    logging.info(
                        f"Skipping scheduled IG Reel ({slot_label}): insufficient female headcount (women={total_dom_women})."
                    )
                    return {"status": "skipped", "ok": False, "reason": "Insufficient live headcount"}

        refresh_ig_token_if_needed(force=False)
        cleaned_custom = reel_generator.enforce_brand_rule((custom_caption or "").strip())
        caption = cleaned_custom or reel_generator.build_reel_caption(stores, reel_type=rt, now_jst=now_jst)

        try:
            reel_generator.render_reel_mp4_bytes(stores, reel_type=rt)
        except Exception as warm_err:
            err_detail = f"Reel MP4 warm generation failed: {warm_err}"
            logging.error(err_detail)
            try:
                import sns_editorial
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    "error",
                    slot_label=slot_label,
                    detail=err_detail,
                    now_jst=now_jst,
                )
            except Exception:
                pass
            return {"status": "error", "ok": False, "detail": err_detail, "reel_type": rt}

        ts_code = now_jst.strftime("%Y%m%d_%H%M")
        video_url = f"{SITE_URL}/ogp/reel/{rt}.mp4?s={ts_code}"
        res = post_reel_to_instagram(video_url, caption, share_to_feed=share_to_feed)
        res["caption"] = caption
        res["video_url"] = video_url
        res["reel_type"] = rt
        res.setdefault("ok", res.get("status") == "success")

        try:
            import sns_editorial
            if res.get("status") == "success":
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    "success",
                    slot_label=slot_label,
                    post_id=res.get("id") or "",
                    now_jst=now_jst,
                )
            else:
                sns_editorial.record_sns_delivery_status(
                    "instagram",
                    res.get("status") or "error",
                    slot_label=slot_label,
                    detail=res.get("detail") or res.get("reason") or "",
                    now_jst=now_jst,
                )
        except Exception:
            pass

        if res.get("status") == "success" and not force:
            job_state.mark_done(dedup_key)
        return res
    finally:
        _ig_post_lock.release()
