"""
bonus_alert.py - Automated X (Twitter) Guerrilla Alert for Rare "Bonus Time" Store Conditions.

Monitors live domestic store counts during evening/night hours (19:20 - 01:40 JST) and posts at most
ONE high-signal guerrilla alert per business night (via `job_state`), avoiding ±18 min collisions
with scheduled 20:00 / 22:00 / 00:00 / 02:00 posts and respecting X API v2 daily rate budgets.
"""

import os
import datetime
import logging

import job_state
import store_master
import x_poster

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

MAJOR_HUB_KEYWORDS = (
    "新宿", "渋谷", "恵比寿", "銀座", "上野", "大宮", "横浜",
    "梅田", "茶屋町", "難波", "心斎橋", "名古屋", "栄", "錦",
    "福岡", "天神", "札幌", "仙台",
    "SHINJUKU", "SHIBUYA", "EBISU", "UMEDA", "CHAYAMACHI",
    "NAMBA", "SHINSAIBASHI", "NAGOYA", "FUKUOKA", "SAPPORO",
)


def is_within_alert_window(now_jst: datetime.datetime = None) -> tuple:
    """
    Check if `now_jst` falls inside 19:20 - 01:40 JST and is at least 22 minutes away
    from regular scheduled posts (18:00 Tips, Sunday 19:00 Weekly Report, 20:00, 22:00, 00:00, 02:00 JST).
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    h, m = now_jst.hour, now_jst.minute

    in_hours = (
        (h == 19 and m >= 20)
        or (h in (20, 21, 22, 23, 0))
        or (h == 1 and m <= 40)
    )
    if not in_hours:
        return False, "Outside evening bonus alert window (19:20-01:40 JST)"

    # Avoid ±22 min around 18:00, (Sunday 19:00), 20:00, 22:00, 00:00, 02:00
    minutes_of_day = h * 60 + m
    scheduled_slots = [18 * 60, 20 * 60, 22 * 60, 0, 24 * 60, 2 * 60]
    if now_jst.weekday() == 6:
        scheduled_slots.append(19 * 60)
    for slot_min in scheduled_slots:
        if abs(minutes_of_day - slot_min) <= 22:
            return False, "Too close to scheduled X post slot"

    return True, "ok"


def _recent_alerted_store_names(now_jst: datetime.datetime) -> set:
    """Return store names alerted in the previous 2 business nights to diversify featured stores."""
    names = set()
    try:
        all_k = job_state.all_keys()
        biz_dt = (now_jst - datetime.timedelta(hours=7)).date()
        recent_prefixes = [
            f"bonus_store:{(biz_dt - datetime.timedelta(days=d)).strftime('%Y-%m-%d')}:"
            for d in (1, 2)
        ]
        for k in all_k:
            for pfx in recent_prefixes:
                if k.startswith(pfx):
                    names.add(k[len(pfx):])
    except Exception:
        pass
    return names


def find_bonus_time_candidate(
    stores: list,
    now_jst: datetime.datetime = None,
    recently_alerted_names: set = None,
) -> dict:
    """
    Evaluate live domestic stores and return the strongest rare bonus-time candidate, or None.
      - Pattern A ('female_lead'): >=26 women, >=64% female ratio, and women - men >= 10
      - Pattern B ('male_lead', 21:00-00:59): >=32 men, >=12 women, and men - women >= 12
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    recently_alerted_names = recently_alerted_names or set()
    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")

    dom_stores = [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]
    if not dom_stores:
        return None

    female_candidates = []
    for s in dom_stores:
        sname = s.get("name") or ""
        w = int(s.get("women") or 0)
        m = int(s.get("men") or 0)
        tot = w + m
        if tot <= 0:
            continue
        ratio = w / tot
        if w >= 26 and (w - m) >= 10 and ratio >= 0.64:
            is_hub = any(k in sname.upper() or k in sname for k in MAJOR_HUB_KEYWORDS)
            w_diff = int(s.get("women_diff") or 0)
            score = (
                (w - m) * 2.0
                + (ratio * 80.0)
                + (15.0 if is_hub else 0.0)
                + (6.0 if w_diff > 0 else 0.0)
                - (25.0 if sname in recently_alerted_names else 0.0)
            )
            female_candidates.append((score, s, round(ratio * 100)))

    if female_candidates:
        female_candidates.sort(key=lambda x: x[0], reverse=True)
        _, best_s, wpct = female_candidates[0]
        return {"store": best_s, "mode": "female_lead", "wpct": wpct}

    if now_jst.hour in (21, 22, 23, 0):
        male_candidates = []
        for s in dom_stores:
            sname = s.get("name") or ""
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
            tot = w + m
            if m >= 32 and w >= 12 and (m - w) >= 12:
                is_hub = any(k in sname.upper() or k in sname for k in MAJOR_HUB_KEYWORDS)
                score = (
                    (m - w) * 1.5
                    + (m * 0.5)
                    + (15.0 if is_hub else 0.0)
                    - (25.0 if sname in recently_alerted_names else 0.0)
                )
                wpct = round((w / tot) * 100) if tot > 0 else 0
                male_candidates.append((score, s, wpct))
        if male_candidates:
            male_candidates.sort(key=lambda x: x[0], reverse=True)
            _, best_s, wpct = male_candidates[0]
            return {"store": best_s, "mode": "male_lead", "wpct": wpct}

    return None


def build_bonus_alert_tweet_thread(
    candidate: dict,
    now_jst: datetime.datetime = None,
    app_url: str = "",
) -> tuple:
    """Build URL-free 1st guerrilla alert post (<=274 weight) + store-detail reply URL."""
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    store = candidate["store"]
    mode = candidate.get("mode", "female_lead")

    sname = store.get("name") or ""
    ja_name = x_poster.format_ja_store_name(sname)
    seo_label = store_master.seo_name(sname) or ja_name
    slug = store_master.slug_of(sname)

    w = int(store.get("women") or 0)
    m = int(store.get("men") or 0)
    tot = w + m
    wpct = round((w / tot) * 100) if tot > 0 else 50
    t_str = now_jst.strftime("%H:%M")

    if mode == "male_lead":
        main_text = (
            f"🚨【盛り上がり速報】{t_str}時点\n"
            f"いま「{ja_name}」が男性{m}名・女性{w}名で大盛況中🥂\n\n"
            f"・女性は待ち時間ゼロで即相席◎\n"
            f"・過去ログでは男性先行の30分後に女性も急増しやすい傾向📊\n\n"
            f"近隣店舗との比較はリプ欄へ👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
    else:
        main_text = (
            f"🚨【ボーナスタイム速報】{t_str}時点\n"
            f"いま「{ja_name}」が女性大幅リードの狙い目状態🔥\n\n"
            f"👩 女性：{w}名 / 👨 男性：{m}名（女性比{wpct}%）\n\n"
            f"人気店でこの男女比はかなりレア！男性待ちになる前にどうぞ👇\n"
            f"#相席ナビ #相席ラウンジ"
        )

    store_url = f"{site_url}/store/{slug}" if slug else f"{site_url}/"
    reply_text = (
        f"📍 {seo_label}の最新人数＆徒歩圏の周辺店舗とのハシゴ比較はこちら👇\n"
        f"{store_url}"
    )
    return main_text, reply_text


def check_and_post_bonus_alert(
    stores: list,
    force: bool = False,
    now_jst: datetime.datetime = None,
    app_url: str = "",
) -> dict:
    """
    Evaluate current stores and post at most 1 guerrilla bonus-time alert per business night.
    When `force=True` (manual staff trigger), bypasses time window and daily cap checks,
    and falls back to the top female-count store if no strict threshold is currently met.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    biz_date = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
    dedup_key = f"bonus_alert:{biz_date}"

    if not force:
        ok_win, reason = is_within_alert_window(now_jst)
        if not ok_win:
            return {"status": "skipped", "reason": reason}
        if job_state.is_done(dedup_key):
            return {"status": "skipped", "reason": "Already posted bonus alert tonight"}

    recent_names = _recent_alerted_store_names(now_jst)
    candidate = find_bonus_time_candidate(stores, now_jst=now_jst, recently_alerted_names=recent_names)

    if not candidate and force and stores:
        dom = [s for s in stores if s.get("region") != "Korea"]
        if dom:
            dom.sort(key=lambda x: (int(x.get("women") or 0), int(x.get("men") or 0)), reverse=True)
            top_s = dom[0]
            tot = max(1, int(top_s.get("women") or 0) + int(top_s.get("men") or 0))
            candidate = {
                "store": top_s,
                "mode": "female_lead",
                "wpct": round((int(top_s.get("women") or 0) / tot) * 100),
            }

    if not candidate:
        return {"status": "skipped", "reason": "No store currently meets bonus-time threshold"}

    main_text, reply_text = build_bonus_alert_tweet_thread(candidate, now_jst=now_jst, app_url=app_url)
    png_bytes = None
    try:
        import ogp_generator
        png_bytes = ogp_generator.generate_bonus_alert_png(
            candidate["store"],
            mode=candidate["mode"],
            now_jst=now_jst,
        )
    except Exception as e:
        logger.warning(f"Failed to render bonus alert PNG: {e}")

    res = x_poster.post_to_x(
        main_text,
        reply_text=reply_text,
        media_bytes=png_bytes,
        media_mime_type="image/png",
    )
    if res.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
        sname = (candidate["store"].get("name") or "").strip()
        if sname:
            job_state.mark_done(f"bonus_store:{biz_date}:{sname}")

    res["mode"] = candidate["mode"]
    res["store_name"] = candidate["store"].get("name")
    res["tweet_text"] = main_text
    res["reply_text"] = reply_text
    return res
