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
    "梅田", "茶屋町", "難波", "心斎橋", "名古屋", "栄", "名古屋 錦", "名古屋錦",
    "福岡", "天神", "札幌", "仙台",
    "SHINJUKU", "SHIBUYA", "EBISU", "UMEDA", "CHAYAMACHI",
    "NAMBA", "SHINSAIBASHI", "NAGOYA", "FUKUOKA", "SAPPORO",
)


def is_within_alert_window(now_jst: datetime.datetime = None) -> tuple:
    """
    Check if `now_jst` falls inside the off-rush prime window (20:35 - 23:35 JST daily, plus up to 01:25 on Fri/Sat nights)
    and is at least 28 minutes away from regular scheduled posts so X never fires bursts between 18:00 and 20:15 JST.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    h, m = now_jst.hour, now_jst.minute
    biz_dt = (now_jst - datetime.timedelta(hours=7)).date()
    is_weekend_biz = biz_dt.weekday() in (4, 5)  # Friday or Saturday business night

    in_hours = (
        (h == 20 and m >= 35)
        or (h in (21, 22))
        or (h == 23 and m <= 35)
        or (is_weekend_biz and (h == 0 or (h == 1 and m <= 25)))
    )
    if not in_hours:
        return False, "Outside prime bonus alert window (20:35-23:35 JST; 18:00-20:34 rush excluded)"

    # Avoid ±28 min around scheduled slots (20:00, 22:00, 00:00)
    minutes_of_day = h * 60 + m
    scheduled_slots = [18 * 60, 20 * 60, 22 * 60, 0, 24 * 60]
    if now_jst.weekday() == 6:
        scheduled_slots.append(19 * 60)
    for slot_min in scheduled_slots:
        if abs(minutes_of_day - slot_min) <= 28:
            return False, "Too close to scheduled X post slot"

    return True, "ok"


def _recent_alerted_store_names(now_jst: datetime.datetime) -> set:
    """Return store names alerted in the previous 3 business nights to diversify featured stores."""
    names = set()
    try:
        all_k = job_state.all_keys()
        biz_dt = (now_jst - datetime.timedelta(hours=7)).date()
        recent_prefixes = [
            f"bonus_store:{(biz_dt - datetime.timedelta(days=d)).strftime('%Y-%m-%d')}:"
            for d in (1, 2, 3)
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
    Evaluate live domestic stores and return ONLY truly rare outlier bonus-time candidates (prevents daily bot firing):
      - Pattern A ('female_lead'): >=35 women, >=68% female ratio, and women - men >= 16
      - Pattern B ('male_lead', 21:00-23:35): >=42 men, >=14 women, and men - women >= 18
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
        if w >= 35 and (w - m) >= 16 and ratio >= 0.68:
            is_hub = any(k in sname.upper() or k in sname for k in MAJOR_HUB_KEYWORDS)
            w_diff = int(s.get("women_diff") or 0)
            score = (
                (w - m) * 2.0
                + (ratio * 80.0)
                + (15.0 if is_hub else 0.0)
                + (6.0 if w_diff > 0 else 0.0)
                - (60.0 if sname in recently_alerted_names else 0.0)
            )
            female_candidates.append((score, s, round(ratio * 100)))

    if female_candidates:
        female_candidates.sort(key=lambda x: x[0], reverse=True)
        _, best_s, wpct = female_candidates[0]
        return {"store": best_s, "mode": "female_lead", "wpct": wpct}

    if now_jst.hour in (21, 22, 23):
        male_candidates = []
        for s in dom_stores:
            sname = s.get("name") or ""
            w = int(s.get("women") or 0)
            m = int(s.get("men") or 0)
            tot = w + m
            if m >= 42 and w >= 14 and (m - w) >= 18:
                is_hub = any(k in sname.upper() or k in sname for k in MAJOR_HUB_KEYWORDS)
                score = (
                    (m - w) * 1.5
                    + (m * 0.5)
                    + (15.0 if is_hub else 0.0)
                    - (60.0 if sname in recently_alerted_names else 0.0)
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
    use_ai: bool = False,
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

    store_url = f"{site_url}/store/{slug}" if slug else f"{site_url}/"
    reply_text = (
        f"📍 {seo_label}の現在人数推移＆徒歩圏の他店舗とのバランス比較はこちら👇\n"
        f"{store_url}"
    )

    if use_ai:
        try:
            import sns_editorial
            if sns_editorial.has_any_ai_key(allow_blocking=True):
                casual_n = sns_editorial.casual_store_name(sname)
                weather_ctx = sns_editorial.fetch_live_weather_context(allow_blocking=True) or {}
                cal_ctx = sns_editorial.build_calendar_context(now_jst)
                facts = {
                    "time": f"{cal_ctx.get('weekday_ja', '')}曜 {t_str}",
                    "weather": weather_ctx.get("adlib_hint") or "",
                    "mode": "女子待ちゼロ（男性大幅先行）" if mode == "male_lead" else "男性ボーナスタイム（女性大幅先行）",
                    "store_name": ja_name,
                    "casual_alias": casual_n,
                    "women": w,
                    "men": m,
                    "women_pct": wpct,
                }
                prompt = (
                    "あなたは全国53店舗の相席ラウンジ人数モニターを見ている『相席ナビ』の運営中の人です。\n"
                    "いまモニターをふと見たら1店舗だけ極端な男女比の偏り（レアな狙い目状態）が出ているのを発見し、"
                    "思わずXで呟いたような『人間味100%のゲリラ独り言ポスト（90〜122文字以内、X換算258ウェイト以下）』を1本書いてください。\n\n"
                    f"{sns_editorial.HUMAN_INSIDER_VOICE_GUIDE}\n\n"
                    "【必須ルール】\n"
                    "1. 『🚨【速報】』のような業者見出しやURL（http...）は絶対に入れないこと。\n"
                    f"2. 店舗名（『{ja_name}』または『{casual_n}』）と実測値（女性{w}名・男性{m}名、必要なら女性比{wpct}%）を1人の狂いもなく正確に入れ、最後はリプ欄へ自然に流して『#相席ナビ #相席ラウンジ』で締めること。\n\n"
                    f"【検証済みファクト】\n{sns_editorial.json.dumps(facts, ensure_ascii=False)}\n\n"
                    '出力はJSON形式のみ：{"main_text": "投稿本文"}'
                )
                parsed, _, _ = sns_editorial.call_editorial_ai_json(
                    prompt=prompt,
                    max_output_tokens=1800,
                    thinking_budget=896,
                    total_deadline_sec=28.0,
                    per_call_timeout_sec=19.0,
                    now_jst=now_jst,
                    prefer_fast=True,
                )
                if parsed and parsed.get("main_text"):
                    ai_txt = sns_editorial._postprocess_ai_main_text(parsed["main_text"], channel="x")
                    wt = x_poster.compute_twitter_weight(ai_txt)
                    w_vals = sns_editorial._extract_headcount_values(ai_txt, "women")
                    m_vals = sns_editorial._extract_headcount_values(ai_txt, "men")
                    has_url = ("http://" in ai_txt or "https://" in ai_txt)
                    if (
                        not has_url
                        and 110 <= wt <= 274
                        and all(v == w for v in w_vals)
                        and all(v == m for v in m_vals)
                        and (w in w_vals or m in m_vals)
                    ):
                        return ai_txt, reply_text
        except Exception as e:
            logger.warning(f"Bonus alert AI generation fallback: {e}")

    if mode == "male_lead":
        main_text = (
            f"いま{t_str}のモニター見てたんだけど、「{ja_name}」が男性{m}名・女性{w}名でだいぶ男性先行になってる…🥂\n\n"
            f"近くにいる女子組なら受付待ちゼロでそのままソファ席通されそうな空気。男性先行の30分後に女性が増えやすい傾向もあるので推移はリプ欄置いときます👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
    else:
        female_lead_openers = (
            f"いま{t_str}のモニター見てたんだけど、「{ja_name}」が女性{w}名・男性{m}名（女性比{wpct}%）までかなり偏ってる…",
            f"{t_str}時点、「{ja_name}」のバランスが女性{w}名・男性{m}名（女性比{wpct}%）で今夜いちばん狙い目な空気になってる👀",
            f"ふと数字を見比べてたら、「{ja_name}」がいま女性{w}名・男性{m}名（女性比{wpct}%）まで伸びてた（{t_str}時点）",
        )
        lead_line = female_lead_openers[(now_jst.day + now_jst.hour) % len(female_lead_openers)]
        main_text = (
            f"{lead_line}\n\n"
            f"人気店でこのバランスはなかなか出ないやつ。男性待ちに切り替わる前の細かい推移はリプ欄置いときます👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
    if x_poster.compute_twitter_weight(main_text) > 274:
        if mode == "male_lead":
            main_text = (
                f"{t_str}速報：「{ja_name}」が男性{m}名・女性{w}名で男性先行に🥂\n\n"
                f"女子組は待ちゼロで案内されやすい状況。推移はリプ欄へ👇\n"
                f"#相席ナビ #相席ラウンジ"
            )
        else:
            main_text = (
                f"{t_str}速報：「{ja_name}」が女性{w}名・男性{m}名（女性比{wpct}%）に到達📊\n\n"
                f"混雑前の推移はリプ欄へ👇\n"
                f"#相席ナビ #相席ラウンジ"
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
        in_cd, cd_reason = x_poster.check_x_auto_post_cooldown(min_interval_minutes=45, now_jst=now_jst)
        if in_cd:
            return {"status": "skipped", "reason": cd_reason}

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

    main_text, reply_text = build_bonus_alert_tweet_thread(candidate, now_jst=now_jst, app_url=app_url, use_ai=True)
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
        x_poster.record_x_auto_post_timestamp(now_jst)
        sname = (candidate["store"].get("name") or "").strip()
        if sname:
            job_state.mark_done(f"bonus_store:{biz_date}:{sname}")

    res["mode"] = candidate["mode"]
    res["store_name"] = candidate["store"].get("name")
    res["tweet_text"] = main_text
    res["reply_text"] = reply_text
    return res
