"""
threads_poster.py - Meta Threads automated posting & editorial copy generator for Aiseki Navi.

Features:
1. Build 400-500 char editorial-style Threads posts (East vs West live comparison + major city breakdown + actionable strategy).
2. Official Meta Threads Graph API integration (`POST /{user_id}/threads` -> `POST /{user_id}/threads_publish`).
3. Graceful skip when `THREADS_USER_ID` or `THREADS_ACCESS_TOKEN` is not configured, while keeping 1-tap copy generation always available on `/staff` and `/api/sns-post?type=threads`.
"""

import datetime
import logging
import os
import re
import sys
import threading
import time
from urllib.parse import urlparse, parse_qs

import requests

import job_state
import x_poster

THREADS_API_BASE = "https://graph.threads.net/v1.0"
DEFAULT_THREADS_USER_ID = "28848531981469633"  # @aiseki_navi1
SITE_URL = "https://aiseki-navi.com"
_cached_user_id = None
_threads_post_lock = threading.Lock()


def _warm_threads_image_inprocess(image_url: str, stores: list = None) -> None:
    """
    Pre-warm dynamic OGP/slide images in-process without making a loopback HTTP `requests.get`
    to our own single-worker server (preventing thread-pool self-deadlock).
    """
    if not image_url:
        return
    try:
        app_mod = sys.modules.get("app")
        if "/ogp/ig-slide/" in image_url:
            import ig_carousel_generator
            m = re.search(r"/ogp/ig-slide/(\d+)\.(jpg|jpeg|png)", image_url, re.IGNORECASE)
            if m:
                slide_num = max(1, min(5, int(m.group(1))))
                is_jpg = m.group(2).lower() in ("jpg", "jpeg")
                render_cached_fn = getattr(app_mod, "_render_cached_ig_slide_bytes", None) if app_mod else None
                warm_fn = getattr(app_mod, "warm_ig_slide_cache", None) if app_mod else None
                ctx = None
                if app_mod and hasattr(app_mod, "_get_sns_kit_context"):
                    try:
                        ctx = app_mod._get_sns_kit_context(fast=False)
                    except Exception:
                        ctx = None
                if ctx is None:
                    ctx = ig_carousel_generator.build_forecast_context(live_stores=stores)
                if callable(warm_fn):
                    warm_fn(slide_num, is_jpg=is_jpg, ctx=ctx)
                elif callable(render_cached_fn):
                    render_cached_fn(slide_num, is_jpg, ctx)
                elif is_jpg:
                    ig_carousel_generator.render_slide_jpeg(slide_num, ctx=ctx, quality=90)
                else:
                    ig_carousel_generator.render_slide_png(slide_num, ctx=ctx)
        elif "/ogp/live.png" in image_url:
            import ogp_generator
            parsed = urlparse(image_url)
            s_param = ((parse_qs(parsed.query).get("s") or [""])[0] or "").strip()[:40]
            live_stores = stores
            if live_stores is None and app_mod and hasattr(app_mod, "latest_data"):
                live_stores = list((app_mod.latest_data or {}).get("full_data") or [])
            ogp_generator.generate_live_ogp_png(live_stores or [], s_param=s_param)
    except Exception as e:
        logging.debug(f"In-process Threads image warm skipped: {e}")


def _filter_domestic_stores(stores: list) -> list:
    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    return [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]


def _pick_city_top(stores: list, keywords: tuple):
    matched = [
        s for s in stores
        if any(k in (s.get("name") or "").upper() or k in x_poster.format_ja_store_name(s.get("name", "")) for k in keywords)
    ]
    if not matched:
        return None
    matched.sort(key=lambda s: (int(s.get("women") or 0), int(s.get("men") or 0)), reverse=True)
    return matched[0] if int(matched[0].get("women") or 0) > 0 else None


def build_threads_live_post(stores: list, slot_label: str = "20:00", now_jst: datetime.datetime = None) -> str:
    """
    Build a 350-480 character conversational/editorial Threads post comparing
    Nationwide TOP 3 + Major City leaders (Shinjuku, Shibuya, Umeda, Namba, Nagoya, Fukuoka).
    Meta Threads limit is 500 characters per post.
    """
    now_jst = now_jst or datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekdays_ja[now_jst.weekday()]
    dom = _filter_domestic_stores(stores)

    if not dom:
        return (
            f"【{now_jst.month}/{now_jst.day}({wd}) {slot_label}】相席ラウンジ リアルタイム速報🥂\n\n"
            "現在、全国51店舗（オリエンタルラウンジ・ag・JIS等）の男女人数と時間帯別料金を比較できます。\n\n"
            f"▼ 全店舗の現在人数＆料金比較はこちら\n{SITE_URL}/price-comparison\n"
            "#相席ナビ #相席ラウンジ"
        )

    total_w = sum(int(s.get("women") or 0) for s in dom)
    total_m = sum(int(s.get("men") or 0) for s in dom)
    ranked = sorted(dom, key=lambda s: (int(s.get("women") or 0), int(s.get("men") or 0)), reverse=True)
    top3 = ranked[:3]

    slot_intros = {
        "20:00": [
            f"【{now_jst.month}/{now_jst.day}({wd}) 20時すぎ】1軒目ピーク前の立ち上がりチェック🥂",
            f"{now_jst.month}/{now_jst.day}({wd}) 20時台、早めの時間から人が集まってる店舗メモ📝",
        ],
        "22:00": [
            f"【{now_jst.month}/{now_jst.day}({wd}) 22時台】ゴールデンタイムの全国＆主要エリア状況🔥",
            f"{now_jst.month}/{now_jst.day}({wd}) 22時すぎ、いま盛り上がってるラウンジまとめ🥂",
            f"今夜（{now_jst.month}/{now_jst.day} {wd}曜）22時台の相席ラウンジ稼働トップ速報📊",
        ],
        "00:00": [
            f"【{now_jst.month}/{now_jst.day}({wd}) 24時すぎ】終電後の第2波・深夜穴場チェック🌙",
        ],
        "02:00": [
            f"【{now_jst.month}/{now_jst.day}({wd}) 深夜2時台】コアタイム稼働トップ速報🍸",
        ],
    }
    intro_candidates = slot_intros.get(slot_label) or [f"【{now_jst.month}/{now_jst.day}({wd}) {slot_label}】全国ラウンジ現在人数メモ🥂"]
    slot_intro = intro_candidates[now_jst.day % len(intro_candidates)]

    lines = [
        slot_intro,
        f"現在 全国{len(dom)}店舗合計：女性{total_w:,}名 ／ 男性{total_m:,}名",
        "",
        "🏆 全国リアルタイムTOP3",
    ]
    medals = ["🥇", "🥈", "🥉"]
    for i, s in enumerate(top3):
        w = int(s.get("women") or 0)
        m = int(s.get("men") or 0)
        tot = w + m
        pct = round((w / tot) * 100) if tot > 0 else 0
        ja = x_poster.format_ja_store_name(s.get("name", ""))
        lines.append(f"{medals[i]} {ja}（女性{w}名 / 男性{m}名・女性比{pct}%）")

    # Major city snapshot (East / West / Regional)
    city_defs = [
        ("新宿", ("新宿", "西新宿", "SHINJUKU")),
        ("渋谷・恵比寿", ("渋谷", "恵比寿", "SHIBUYA", "EBISU")),
        ("梅田・茶屋町", ("梅田", "茶屋町", "天満", "UMEDA", "CHAYAMACHI")),
        ("難波・心斎橋", ("難波", "なんば", "心斎橋", "NAMBA", "SHINSAIBASHI")),
        ("名古屋・福岡", ("名古屋", "栄", "錦", "福岡", "天神", "NAGOYA", "FUKUOKA")),
    ]
    city_lines = []
    for label, keys in city_defs:
        leader = _pick_city_top(dom, keys)
        if leader:
            ja = x_poster.format_ja_store_name(leader.get("name", ""))
            w = int(leader.get("women") or 0)
            m = int(leader.get("men") or 0)
            city_lines.append(f"・{label}：{ja}（女性{w}・男性{m}）")

    if city_lines:
        lines.append("")
        lines.append("📍 主要街の現在トップ")
        lines.extend(city_lines[:4])

    tips_rot = [
        "💡 席替えが回る「女性10名以上（約5組）」かつ女性比率55%以上の店舗を狙うのが鉄則です。",
        "💡 1店舗目で男性待ちが出ていたら、徒歩3〜4分の別ブランドへ回るとスムーズに座れます。",
        "💡 女性グループは逆に「男性が先行して盛り上がっている大型店」を選ぶと待ち0分で即乾杯しやすいです🥂",
    ]
    chosen_tip = tips_rot[now_jst.day % len(tips_rot)]

    # Alternate URL vs Bio-link guidance so Meta's algorithm doesn't flag every post as external link spam
    slot_hh = (slot_label or "22:00")[:2]
    if now_jst.day % 2 == 0:
        cta_line = "🔍 全51店舗の現在人数＆料金・イベント比較はプロフィールのリンク（相席ナビ）から見れます"
        tag_line = "#相席ナビ #相席ラウンジ"
    else:
        cta_line = f"▼ 全51店舗の現在人数＆料金・イベント比較\n{SITE_URL}/price-comparison?s=th{slot_hh}_live"
        tag_line = "#相席ナビ"

    lines.extend([
        "",
        chosen_tip,
        cta_line,
        tag_line,
    ])

    text = "\n".join(lines)
    if len(text) > 495:
        text = text[:492] + "..."
    return text


def _get_active_threads_token() -> str:
    """Return the latest refreshed Threads token from job_state, falling back to THREADS_ACCESS_TOKEN env var."""
    stored = (job_state.get_value("token:threads") or "").strip()
    if stored:
        return stored
    return (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()


def refresh_threads_token_if_needed(force: bool = False) -> dict:
    """
    Meta Threads long-lived tokens expire after 60 days.
    Automatically refresh once per ISO week via:
      GET https://graph.threads.net/refresh_access_token?grant_type=th_refresh_token&access_token=<current_token>
    and persist the renewed token in job_state ('token:threads') so manual env var updates are never needed.
    """
    token = _get_active_threads_token()
    if not token:
        return {"status": "skipped", "reason": "No THREADS_ACCESS_TOKEN to refresh"}

    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    iso_year, iso_week, _ = now_jst.isocalendar()
    week_key = f"token_refresh:threads:{iso_year}-W{iso_week:02d}"
    if not force and job_state.is_done(week_key):
        return {"status": "skipped", "reason": f"Already refreshed in {iso_year}-W{iso_week:02d}"}

    try:
        r = requests.get(
            "https://graph.threads.net/refresh_access_token",
            params={
                "grant_type": "th_refresh_token",
                "access_token": token,
            },
            timeout=15,
        )
        if r.status_code == 200:
            data = r.json() or {}
            new_tok = (data.get("access_token") or "").strip()
            if new_tok:
                job_state.set_value("token:threads", new_tok)
                job_state.mark_done(week_key)
                os.environ["THREADS_ACCESS_TOKEN"] = new_tok
                logging.info("Successfully refreshed Threads long-lived access token.")
                return {"status": "refreshed", "expires_in": data.get("expires_in")}
        return {"status": "error", "detail": f"HTTP {r.status_code}: {r.text[:200]}"}
    except Exception as e:
        logging.warning(f"Threads token refresh exception: {e}")
        return {"status": "error", "detail": str(e)}


def _resolve_threads_user_id(access_token: str) -> str:
    """Resolve Threads user_id from environment variable, cached lookup, or @aiseki_navi1 default."""
    global _cached_user_id
    env_uid = (os.environ.get("THREADS_USER_ID") or "").strip()
    if env_uid:
        return env_uid
    if _cached_user_id:
        return _cached_user_id
    try:
        resp = requests.get(
            f"{THREADS_API_BASE}/me",
            params={"fields": "id,username", "access_token": access_token},
            timeout=10,
        )
        if resp.status_code == 200:
            uid = str((resp.json() or {}).get("id") or "").strip()
            if uid:
                _cached_user_id = uid
                return uid
    except Exception:
        pass
    return DEFAULT_THREADS_USER_ID


def _wait_for_threads_container_ready(creation_id: str, access_token: str, max_checks: int = 5, interval_sec: float = 2.0) -> tuple:
    """
    Poll `GET /{creation_id}?fields=status,error_message` until `FINISHED` (especially needed for IMAGE containers).
    Exits immediately on mocks or responses without `status` field so unit tests never stall.
    """
    if not creation_id:
        return False, "Missing creation_id"
    wait_s = 0.05 if os.environ.get("DISABLE_SCHEDULER") == "1" else interval_sec

    for attempt in range(max_checks):
        try:
            r = requests.get(
                f"{THREADS_API_BASE}/{creation_id}",
                params={"fields": "status,error_message", "access_token": access_token},
                timeout=10,
            )
            sc = getattr(r, "status_code", None)
            if not isinstance(sc, int) or sc >= 400:
                return True, "status_check_unsupported"
            data = r.json() if hasattr(r, "json") else {}
            if not isinstance(data, dict) or "status" not in data:
                return True, "no_status_field"
            st = str(data.get("status") or "").upper()
            if st in ("FINISHED", "PUBLISHED"):
                return True, st
            if st in ("ERROR", "EXPIRED"):
                return False, f"Threads container status={st}: {data.get('error_message') or data}"
        except Exception:
            return True, "status_check_exception"

        if attempt < max_checks - 1:
            time.sleep(wait_s)

    return True, "timeout_proceed"


def post_to_threads(text: str, image_url: str = None) -> dict:
    """
    Publish a text or image post to Meta Threads using the official 2-step Graph API:
      1. POST /{threads_user_id}/threads (media_type=TEXT or IMAGE, text=...)
      2. Poll container status when needed, then POST /{threads_user_id}/threads_publish (creation_id=...)
    """
    access_token = _get_active_threads_token()

    if not access_token:
        return {
            "status": "skipped",
            "reason": "THREADS_ACCESS_TOKEN not configured",
        }

    user_id = _resolve_threads_user_id(access_token)

    try:
        if image_url:
            _warm_threads_image_inprocess(image_url)

        create_url = f"{THREADS_API_BASE}/{user_id}/threads"
        create_data = {
            "media_type": "IMAGE" if image_url else "TEXT",
            "text": text[:500],
            "access_token": access_token,
        }
        if image_url:
            create_data["image_url"] = image_url

        r1 = requests.post(
            create_url,
            data=create_data,
            timeout=20,
        )
        if r1.status_code >= 400:
            logging.error(f"Threads container creation failed ({r1.status_code}): {r1.text}")
            return {"status": "error", "detail": f"Step1 HTTP {r1.status_code}: {r1.text[:200]}"}

        creation_id = (r1.json() or {}).get("id")
        if not creation_id:
            return {"status": "error", "detail": f"Missing creation_id in response: {r1.text[:200]}"}

        if image_url:
            ok_ready, ready_detail = _wait_for_threads_container_ready(creation_id, access_token)
            if not ok_ready:
                logging.error(f"Threads image container failed processing: {ready_detail}")
                return {"status": "error", "detail": ready_detail}

        publish_url = f"{THREADS_API_BASE}/{user_id}/threads_publish"
        r2 = requests.post(
            publish_url,
            data={
                "creation_id": creation_id,
                "access_token": access_token,
            },
            timeout=20,
        )
        if r2.status_code >= 400:
            logging.error(f"Threads publish failed ({r2.status_code}): {r2.text}")
            return {"status": "error", "detail": f"Step2 HTTP {r2.status_code}: {r2.text[:200]}"}

        post_id = (r2.json() or {}).get("id")
        logging.info(f"Successfully published to Threads: id={post_id}")
        return {"status": "success", "id": post_id}
    except Exception as e:
        logging.error(f"Threads API exception: {e}")
        return {"status": "error", "detail": str(e)}


def execute_scheduled_threads_post(stores: list, slot_label: str = "20:00", force: bool = False, image_url: str = None) -> dict:
    """Execute scheduled Threads post with persistent job_state deduplication, headcount guard, and weekly token auto-refresh."""
    if not _threads_post_lock.acquire(blocking=False):
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "already_running"}

    try:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        biz_date = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        dedup_key = f"threads_post:{biz_date}_{slot_label}"

        if not force and job_state.is_done(dedup_key):
            return {"status": "skipped", "reason": "Already posted to Threads for this slot"}

        dom = _filter_domestic_stores(stores)
        if not force:
            total_dom_women = sum(int(s.get("women") or 0) for s in dom)
            if total_dom_women < 15:
                logging.info(f"Skipping Threads auto-post ({slot_label}): insufficient live headcount (women={total_dom_women}).")
                return {"status": "skipped", "reason": "Insufficient live headcount"}

        refresh_threads_token_if_needed(force=False)
        editorial_winner = None
        try:
            import sns_editorial
            ed_res = sns_editorial.run_editorial_pipeline(
                stores,
                slot_label=slot_label,
                channel="threads",
                now_jst=now_jst,
                record_history=not force,
                use_ai=True,
                force_ai_refresh=True,
            )
            editorial_winner = ed_res.get("winner")
        except Exception as e:
            logging.warning(f"Threads editorial pipeline fallback ({slot_label}): {e}")

        if editorial_winner and editorial_winner.get("main_text"):
            text = editorial_winner["main_text"]
        else:
            text = build_threads_live_post(stores, slot_label=slot_label, now_jst=now_jst)

        if image_url:
            _warm_threads_image_inprocess(image_url, stores=stores)

        res = post_to_threads(text, image_url=image_url)
        if res.get("status") == "success" and not force:
            job_state.mark_done(dedup_key)
        res["threads_text"] = text
        if editorial_winner:
            res["editorial_style"] = editorial_winner.get("style")
            res["editorial_score"] = editorial_winner.get("total_score")
        return res
    finally:
        _threads_post_lock.release()


def execute_scheduled_ladies_threads_post(
    stores: list,
    slot_label: str = "19:30",
    force: bool = False,
) -> dict:
    """
    Execute scheduled women-perspective Threads alert ('🎁 今夜の女子得＆待ちゼロ速報', Plan E).
    Guarded by persistent job_state deduplication and domestic live male headcount (`men >= 12`).
    """
    if not _threads_post_lock.acquire(blocking=False):
        return {"status": "skipped", "ok": False, "skipped": True, "reason": "already_running"}

    try:
        import ladies_guide

        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        biz_date = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        dedup_key = f"threads_ladies:{biz_date}_{slot_label}"

        if not force and job_state.is_done(dedup_key):
            return {"status": "skipped", "reason": "Already posted ladies alert to Threads for this slot"}

        dom = _filter_domestic_stores(stores)
        total_dom_men = sum(int(s.get("men") or 0) for s in dom)
        if not force and total_dom_men < 12:
            logging.info(
                f"Skipping Threads ladies auto-post ({slot_label}): insufficient live male headcount (men={total_dom_men})."
            )
            return {"status": "skipped", "reason": "Insufficient live male headcount for ladies alert"}

        refresh_threads_token_if_needed(force=False)
        text = ladies_guide.build_threads_ladies_post(stores, slot_label=slot_label, now_jst=now_jst)
        res = post_to_threads(text)
        if res.get("status") == "success" and not force:
            job_state.mark_done(dedup_key)
        res["threads_text"] = text
        res["editorial_style"] = "threads_ladies_zero_wait"
        return res
    finally:
        _threads_post_lock.release()
