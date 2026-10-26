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

import requests

import job_state
import x_poster

THREADS_API_BASE = "https://graph.threads.net/v1.0"
DEFAULT_THREADS_USER_ID = "28848531981469633"  # @aiseki_navi1
SITE_URL = "https://aiseki-navi.com"
_cached_user_id = None


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
            "#相席ラウンジ #相席ナビ #オリエンタルラウンジ #JIS"
        )

    total_w = sum(int(s.get("women") or 0) for s in dom)
    total_m = sum(int(s.get("men") or 0) for s in dom)
    ranked = sorted(dom, key=lambda s: (int(s.get("women") or 0), int(s.get("men") or 0)), reverse=True)
    top3 = ranked[:3]

    slot_intro = {
        "20:00": f"【{now_jst.month}/{now_jst.day}({wd}) 20:00】1軒目ピーク前の立ち上がり速報🥂",
        "22:00": f"【{now_jst.month}/{now_jst.day}({wd}) 22:00】ゴールデンタイム突入！全国＆主要エリア速報🔥",
        "00:00": f"【{now_jst.month}/{now_jst.day}({wd}) 24:00】終電後の第2波・深夜穴場チェック🌙",
        "02:00": f"【{now_jst.month}/{now_jst.day}({wd}) 26:00】深夜2時のコアタイム稼働トップ速報🍸",
    }.get(slot_label, f"【{now_jst.month}/{now_jst.day}({wd}) {slot_label}】全国ラウンジ現在人数速報🥂")

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
            city_lines.append(f"・{label}：{ja}（女{w}:男{m}）")

    if city_lines:
        lines.append("")
        lines.append("📍 主要街の現在トップ")
        lines.extend(city_lines[:4])

    lines.extend([
        "",
        "💡 席替えが回る「女性10名以上（約5組）」かつ女性比率55%以上の店舗を狙うのが鉄則です。",
        f"▼ 全51店舗の現在人数＆料金・イベント比較\n{SITE_URL}/price-comparison",
        "#相席ラウンジ #相席ナビ #オリエンタルラウンジ #JIS",
    ])

    text = "\n".join(lines)
    if len(text) > 495:
        text = text[:492] + "..."
    return text


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


def post_to_threads(text: str) -> dict:
    """
    Publish a text post to Meta Threads using the official 2-step Graph API:
      1. POST /{threads_user_id}/threads (media_type=TEXT, text=...)
      2. POST /{threads_user_id}/threads_publish (creation_id=...)
    """
    access_token = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()

    if not access_token:
        return {
            "status": "skipped",
            "reason": "THREADS_ACCESS_TOKEN not configured",
        }

    user_id = _resolve_threads_user_id(access_token)

    try:
        create_url = f"{THREADS_API_BASE}/{user_id}/threads"
        r1 = requests.post(
            create_url,
            data={
                "media_type": "TEXT",
                "text": text[:500],
                "access_token": access_token,
            },
            timeout=15,
        )
        if r1.status_code >= 400:
            logging.error(f"Threads container creation failed ({r1.status_code}): {r1.text}")
            return {"status": "error", "detail": f"Step1 HTTP {r1.status_code}: {r1.text[:200]}"}

        creation_id = (r1.json() or {}).get("id")
        if not creation_id:
            return {"status": "error", "detail": f"Missing creation_id in response: {r1.text[:200]}"}

        publish_url = f"{THREADS_API_BASE}/{user_id}/threads_publish"
        r2 = requests.post(
            publish_url,
            data={
                "creation_id": creation_id,
                "access_token": access_token,
            },
            timeout=15,
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


def execute_scheduled_threads_post(stores: list, slot_label: str = "20:00", force: bool = False) -> dict:
    """Execute scheduled Threads post with persistent job_state deduplication."""
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    biz_date = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
    dedup_key = f"threads_post:{biz_date}_{slot_label}"

    if not force and job_state.is_done(dedup_key):
        return {"status": "skipped", "reason": "Already posted to Threads for this slot"}

    text = build_threads_live_post(stores, slot_label=slot_label, now_jst=now_jst)
    res = post_to_threads(text)
    if res.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
    res["threads_text"] = text
    return res
