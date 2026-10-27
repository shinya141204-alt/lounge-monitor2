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
import time

import requests

import ig_carousel_generator
import job_state
import threads_poster

IG_GRAPH_BASE = "https://graph.instagram.com/v21.0"
FB_GRAPH_BASE = "https://graph.facebook.com/v21.0"
DEFAULT_IG_USER_ID = "17841410169754469"  # @aiseki_navi1 professional user_id
SITE_URL = "https://aiseki-navi.com"
_cached_ig_user_id = None


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
                return {"status": "refreshed", "expires_in": data.get("expires_in")}
        return {"status": "error", "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        logging.warning(f"Instagram token refresh exception: {e}")
        return {"status": "error", "detail": str(e)}


def _graph_base_for_token(access_token: str) -> str:
    """Choose graph.instagram.com for IG tokens, graph.facebook.com for EAA tokens."""
    if access_token.startswith("EAA"):
        return FB_GRAPH_BASE
    return IG_GRAPH_BASE


def _wait_for_ig_container_ready(
    base: str,
    creation_id: str,
    access_token: str,
    max_checks: int = 5,
    interval_sec: float = 2.5,
) -> tuple:
    """
    Poll `GET /{creation_id}?fields=status_code` up to `max_checks` times until `FINISHED`.
    Exits immediately on mocks or responses lacking `status_code` so unit tests never stall.
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
            if not isinstance(sc, int) or sc >= 400:
                return True, "status_check_unsupported"
            data = r.json() if hasattr(r, "json") else {}
            if not isinstance(data, dict) or "status_code" not in data:
                return True, "no_status_code_field"
            code = str(data.get("status_code") or "").upper()
            if code in ("FINISHED", "PUBLISHED"):
                return True, code
            if code in ("ERROR", "EXPIRED"):
                return False, f"IG container status={code}: {data}"
        except Exception:
            return True, "status_check_exception"

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
    try:
        try:
            requests.get(image_url, timeout=15)
        except Exception:
            pass

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

        r2 = requests.post(
            f"{base}/{ig_user_id}/media_publish",
            data={
                "creation_id": creation_id,
                "access_token": access_token,
            },
            timeout=25,
        )
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
    try:
        # Warm up all slide URLs first so Render caches them in memory before Meta's crawler fetches them
        for url in image_urls[:10]:
            try:
                requests.get(url, timeout=15)
            except Exception:
                pass

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

        r_pub = requests.post(
            f"{base}/{ig_user_id}/media_publish",
            data={
                "creation_id": carousel_id,
                "access_token": access_token,
            },
            timeout=25,
        )
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
        try:
            import sns_editorial
            ed_res = sns_editorial.run_editorial_pipeline(
                stores,
                slot_label=slot_label,
                channel="instagram",
                ig_ctx=ctx,
                now_jst=now_jst,
                record_history=not force,
            )
            editorial_winner = ed_res.get("winner")
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
        res = post_carousel_to_instagram(slide_urls, caption)
        res["caption"] = caption
        res["slide_urls"] = slide_urls
        if editorial_winner:
            res["editorial_style"] = editorial_winner.get("style")
            res["editorial_score"] = editorial_winner.get("total_score")
    else:
        editorial_winner = None
        try:
            import sns_editorial
            ed_res = sns_editorial.run_editorial_pipeline(
                stores,
                slot_label=slot_label,
                channel="threads",
                now_jst=now_jst,
                record_history=False,
            )
            editorial_winner = ed_res.get("winner")
        except Exception:
            editorial_winner = None
        caption = (
            editorial_winner["main_text"]
            if (editorial_winner and editorial_winner.get("main_text"))
            else threads_poster.build_threads_live_post(stores, slot_label=slot_label, now_jst=now_jst)
        )
        image_url = f"{SITE_URL}/ogp/ig-slide/2.jpg?s={ts_code}"
        res = post_single_image_to_instagram(image_url, caption)
        res["caption"] = caption
        if editorial_winner:
            res["editorial_style"] = editorial_winner.get("style")
            res["editorial_score"] = editorial_winner.get("total_score")

    if res.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
    return res
