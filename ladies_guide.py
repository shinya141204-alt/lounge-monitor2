"""
ladies_guide.py - Women's Zero-Wait Store Ranking, Perks Aggregator & Ladies-Night SNS Copy Engine.

Powers:
  - Plan A & C: Real-time 'Zero-Wait & Perks' live store board on `/for-women`
  - Plan E: Automated women-perspective Threads & Instagram 'Tonight's Zero-Wait & Ladies Perks' alerts
"""

import datetime
import re

import events
import sns_editorial
import store_master

JST = datetime.timezone(datetime.timedelta(hours=9))
SITE_URL = "https://aiseki-navi.com"

KOREA_KEYWORDS = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")


def is_domestic_store(store: dict) -> bool:
    if not isinstance(store, dict):
        return False
    if store.get("region") == "Korea":
        return False
    uname = (store.get("name") or "").upper()
    return not any(k in uname for k in KOREA_KEYWORDS)


def evaluate_ladies_store_status(
    women: int,
    men: int,
    has_event: bool = False,
    smooth_votes: int = 0,
    waiting_votes: int = 0,
) -> dict:
    """
    Score a store from a 2-woman pair's perspective:
      - Highest score: Men are already seated/waiting (`men >= 4` and `men >= women`) -> 0-min reception wait & VIP treatment!
      - Good score: Healthy rotation (`men >= 4` and female share <= 62%)
      - Bonus: Store-specific campaign / ladies gift / dessert fair active today
      - Penalty: Heavy female surplus (`women >> men`) where women risk reception wait or solo table placement
    """
    w = max(0, int(women or 0))
    m = max(0, int(men or 0))
    tot = w + m
    if tot == 0:
        return {
            "score": -1000.0,
            "tier": "closed_or_empty",
            "badge": "営業時間外・準備中",
            "badge_cls": "quiet",
            "note": "現在まだ来店カウントがありません",
        }

    wpct = round((w / tot) * 100)
    mpct = 100 - wpct
    ev_bonus = 45.0 if has_event else 0.0
    vote_bonus = 15.0 if (smooth_votes > 0 and smooth_votes >= waiting_votes) else 0.0

    if m >= 4 and m >= w:
        lead = m - w
        score = 300.0 + min(45.0, lead * 2.5) + min(55.0, m * 1.5) + ev_bonus + vote_bonus
        return {
            "score": round(score, 1),
            "tier": "zero_wait_vip",
            "badge": "🟢 女子待ち0・即案内圏",
            "badge_cls": "vip",
            "note": f"男性{m}名が先行中（男性比{mpct}%）・受付待ちほぼゼロで即乾杯しやすい状態",
        }

    if m >= 4 and wpct <= 62:
        score = 215.0 + min(45.0, m * 1.4) - max(0, (w - m) * 1.2) + ev_bonus + vote_bonus
        return {
            "score": round(score, 1),
            "tier": "balanced_smooth",
            "badge": "✨ スムーズ相席圏",
            "badge_cls": "good",
            "note": f"男女バランス良好（男性{m}名・女性{w}名）・席替えも自然に回りやすい状態",
        }

    if 1 <= m < 4 and w <= 6:
        score = 150.0 + m * 8.0 + ev_bonus + vote_bonus
        return {
            "score": round(score, 1),
            "tier": "calm_cafe",
            "badge": "☕ ゆったり2軒目向き",
            "badge_cls": "calm",
            "note": "まだ落ち着いた人数帯・友達とカフェ感覚でゆっくり話したい夜向き",
        }

    if m == 0 and w > 0:
        score = 15.0 + ev_bonus * 0.5
        return {
            "score": round(score, 1),
            "tier": "female_only_waiting",
            "badge": "⏳ 男性来店待ち",
            "badge_cls": "wait",
            "note": f"現在女性{w}名のみ・相席スタートまで少し待つ可能性あり",
        }

    # Female-heavy (`wpct > 62` or `w > 6` with few men)
    surplus = max(0, w - m)
    score = max(25.0, 110.0 - surplus * 1.6) + ev_bonus * 0.6
    return {
        "score": round(score, 1),
        "tier": "female_crowded",
        "badge": "⚠️ 女子多め・待ち注意",
        "badge_cls": "warn",
        "note": f"女性比率{wpct}%で女子組が集中中・近隣の男性先行店へ回る方がスムーズ",
    }


def _clean_event_title(raw_title: str) -> str:
    t = re.sub(r"\s+", " ", str(raw_title or "")).strip()
    if len(t) > 46:
        return t[:45] + "…"
    return t


def build_ladies_live_board(live_stores: list, all_events: list = None) -> dict:
    """Build real-time store recommendations tailored for female pairs (`/for-women` & Ladies SNS)."""
    if all_events is None:
        try:
            all_events = events.fetch_all_events(force_refresh=False, allow_blocking=False) or []
        except Exception:
            all_events = []

    dom = [s for s in (live_stores or []) if is_domestic_store(s)]
    enriched = []
    total_w = 0
    total_m = 0
    zero_wait_count = 0

    for s in dom:
        sname = s.get("name") or ""
        w = max(0, int(s.get("women") or 0))
        m = max(0, int(s.get("men") or 0))
        total_w += w
        total_m += m

        matched_evs = []
        if sname and all_events:
            try:
                matched_evs = events.get_events_for_store(
                    sname,
                    store_only=True,
                    allow_blocking=False,
                    preloaded_events=all_events,
                ) or []
            except Exception:
                matched_evs = []

        ev_title = _clean_event_title(matched_evs[0].get("title")) if matched_evs else ""
        has_disc = any(e.get("tag") == "割引・特典" for e in matched_evs)
        votes = s.get("votes") or {}
        eval_info = evaluate_ladies_store_status(
            women=w,
            men=m,
            has_event=bool(matched_evs),
            smooth_votes=int(votes.get("smooth") or 0),
            waiting_votes=int(votes.get("waiting") or 0),
        )
        if eval_info["tier"] in ("zero_wait_vip", "balanced_smooth") and m >= 4:
            zero_wait_count += 1

        ja_name = sns_editorial.natural_store_name(sname)
        casual_name = sns_editorial.casual_store_name(sname)
        slug = store_master.slug_of(sname)
        area_label = store_master.area_label_of(sname) if hasattr(store_master, "area_label_of") else ""
        if not area_label:
            m_info = (store_master.load() or {}).get(sname) or {}
            area_label = m_info.get("area_label") or ""

        tot = w + m
        enriched.append({
            "name": sname,
            "ja_name": ja_name,
            "casual_name": casual_name,
            "slug": slug,
            "href": f"/store/{slug}" if slug else "/",
            "area_label": area_label,
            "women": w,
            "men": m,
            "total": tot,
            "men_pct": round((m / tot) * 100) if tot > 0 else 0,
            "women_pct": round((w / tot) * 100) if tot > 0 else 0,
            "has_event": bool(matched_evs),
            "event_badge": ("🎁 特典開催中" if has_disc else "✨ 限定フェア中") if matched_evs else "",
            "event_title": ev_title,
            **eval_info,
        })

    enriched.sort(key=lambda x: (x["score"], x["men"], -x["women"]), reverse=True)
    active_picks = [x for x in enriched if x["total"] > 0]
    event_picks = [x for x in enriched if x["has_event"]]

    return {
        "zero_wait_picks": (active_picks or enriched)[:10],
        "event_picks": event_picks[:8],
        "all_ranked": enriched,
        "summary": {
            "domestic_count": len(dom),
            "total_women": total_w,
            "total_men": total_m,
            "zero_wait_count": zero_wait_count,
            "event_store_count": len(event_picks),
        },
    }


def build_threads_ladies_post(
    stores: list,
    slot_label: str = "19:30",
    now_jst: datetime.datetime = None,
) -> str:
    """
    Build a women-perspective Threads alert ('今夜の女子得＆待ちゼロ速報', <= 490 chars).
    Guarantees zero bare 'オリエンタル' (`オリエンタル(?!ラウンジ)`) and natural human conversational tone.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekdays_ja[now_jst.weekday()]
    board = build_ladies_live_board(stores)
    picks = [p for p in board["zero_wait_picks"] if p["men"] > 0][:3]
    ev_picks = [p for p in board["event_picks"] if p["total"] > 0][:2]

    slot_hh = (slot_label or "19:30")[:2]
    if now_jst.day % 2 == 0:
        cta_footer = (
            "💄 いま女子待ち0で入れる全国一覧＆席替え裏マニュアルはプロフィールのリンク（相席ナビ）から見れます\n"
            "#相席ラウンジ"
        )
    else:
        cta_footer = (
            f"💄 いま女子待ち0で入れる全国店舗＆裏ガイド\n"
            f"{SITE_URL}/for-women?s=th{slot_hh}_ladies_night\n"
            "#相席ラウンジ"
        )

    if not picks:
        return (
            f"{now_jst.month}/{now_jst.day}({wd})の夜、これから友達と2軒目や夜カフェ代わりにラウンジ寄ろうかな〜と迷っている女子組へ💄\n\n"
            "実は女子が『受付で30分待たされる店』と『待ち0分で即ソファ席へ通される店』の差は、入る前の男女比だけでほぼ決まります。\n\n"
            "男性が先行して盛り上がっている店舗を選ぶだけで、待ち時間ゼロ＆ドリンク提供まで圧倒的にスムーズ🥂\n\n"
            f"{cta_footer}"
        )

    openers = [
        f"せっかく髪もメイクも仕上げたのに、ラウンジの受付で『女子待ち30分です』って言われる夜が一番テンション下がりますよね…💄（{now_jst.month}/{now_jst.day} {wd}曜夜メモ）",
        f"{now_jst.month}/{now_jst.day}({wd})これから友達と「2軒目どこ行く？」って相談中の女子組へ🥂 いま受付待ちゼロで即座れそうな穴場だけ抜粋しました📝",
        f"ぶっちゃけ女子組が相席ラウンジで当たり夜を引くコツ、『女余りの有名店を避けて、男性が先行してる店にスッと入る』これだけです💄",
    ]
    opener = openers[now_jst.day % len(openers)]

    pick_lines = []
    for p in picks:
        ev_tag = "・🎁イベント中" if p["has_event"] else ""
        pick_lines.append(f"・{p['ja_name']}（男性{p['men']}名 / 女性{p['women']}名{ev_tag}）")

    ev_line = ""
    if ev_picks and ev_picks[0]["event_title"]:
        ev_line = f"\n🎁 本日『{ev_picks[0]['ja_name']}』などで「{ev_picks[0]['event_title']}」も開催中✨\n"

    text = (
        f"{opener}\n\n"
        f"🟢 いま男性先行・好バランスで即案内されやすい店舗\n"
        + "\n".join(pick_lines)
        + f"\n{ev_line}\n"
        "微妙な席に当たったとき角を立てずに席替え・退店するコツもまとめてるので、入る前の作戦会議にどうぞ👇\n\n"
        f"{cta_footer}"
    )

    if len(text) > 475:
        text = text.replace("オリエンタルラウンジ", "オリラジ")
    if len(text) > 495:
        text = text[:492] + "..."
    text = re.sub(r"オリエンタル(?!ラウンジ)", "オリラジ", text)
    return text


def build_instagram_ladies_caption(
    stores: list,
    slot_label: str = "19:45",
    now_jst: datetime.datetime = None,
) -> str:
    """Build an Instagram Feed/Carousel caption tailored for female followers ('今夜の女子得＆待ちゼロ速報')."""
    now_jst = now_jst or datetime.datetime.now(JST)
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekdays_ja[now_jst.weekday()]
    board = build_ladies_live_board(stores)
    picks = [p for p in board["zero_wait_picks"] if p["men"] > 0][:4]
    ev_picks = board["event_picks"][:3]

    pick_lines = (
        "\n".join(f"・{p['ja_name']}（現在 男性{p['men']}名 / 女性{p['women']}名）" for p in picks)
        if picks
        else "・新宿 / 渋谷 / 梅田 / 栄 / 福岡の大型店は20時台から男性先行でスムーズに入店しやすい傾向です"
    )
    ev_section = ""
    if ev_picks:
        ev_bullets = "\n".join(
            f"・{e['ja_name']}：{e['event_title']}"
            for e in ev_picks
            if e.get("event_title")
        )
        if ev_bullets:
            ev_section = f"\n🎁 本日チェックしたい店舗特典・限定フェア\n{ev_bullets}\n"

    caption = (
        f"「せっかくメイクも服も整えたのに、受付で30分待たされた…」そんな失敗をゼロにする{wd}曜夜の女子向けメモです💄\n"
        "友達と合流する直前に見返せるよう、右上の「保存📌」をどうぞ！\n\n"
        "実は相席ラウンジって、女子人気が高すぎる時間帯の1店舗に集中すると『入場制限』や『女子だけの待機席』になりやすいんです。\n"
        "逆に、いま男性が先行して入っている店舗を選べば【受付待ち0分・即乾杯】でフードやスイーツもスムーズに楽しめます🥂\n\n"
        "🟢 いま男性先行・好バランスで入りやすい注目店\n"
        f"{pick_lines}\n"
        f"{ev_section}\n"
        "💡 女子2人組が損しない3つのマイルール\n"
        "① 入る3分前に『相席ナビ』を【💄女性目線モード】にして男性先行店を選ぶ\n"
        "② 顔写真付きの公的身分証（免許証・マイナカード等）を忘れずに持参する\n"
        "③ もし会話が噛み合わない席でも無理して粘らず、お手洗いタイミングでスマートに席替え相談する\n\n"
        "全国53店舗の『いま女子待ちゼロで入れる店舗一覧』と『角が立たない席替え・退店裏ガイド』はプロフィールのリンク（aiseki-navi.com/for-women）で無料公開中です🔍\n\n"
        "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #女子会ディナー #夜カフェ"
    )
    caption = re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", caption)
    return caption[:2100]
