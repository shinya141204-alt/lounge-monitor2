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


def _verify_ladies_numbers(text: str, picks: list) -> bool:
    """Ensure every male/female count mentioned in AI-generated ladies text matches `picks`."""
    import sns_editorial
    allowed_m = {int(p["men"]) for p in picks} | {0}
    allowed_w = {int(p["women"]) for p in picks} | {0, 10}
    for vm in sns_editorial._extract_headcount_values(text, "men"):
        if vm not in allowed_m:
            return False
    for vw in sns_editorial._extract_headcount_values(text, "women"):
        if vw not in allowed_w:
            return False
    return True


def build_threads_ladies_post(
    stores: list,
    slot_label: str = "19:30",
    now_jst: datetime.datetime = None,
    use_ai: bool = False,
) -> str:
    """
    Build a women-perspective Threads alert ('今夜の女子得＆待ちゼロ速報', <= 490 chars).
    When `use_ai=True` (used by scheduled auto-posts), uses Dual-Engine Chief Writer for fresh human monologue.
    Guarantees zero bare 'オリエンタル' (`オリエンタル(?!ラウンジ)`) and natural human conversational tone.
    """
    import sns_editorial
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

    if use_ai and picks and sns_editorial.has_any_ai_key(allow_blocking=True):
        try:
            weather_ctx = sns_editorial.fetch_live_weather_context(allow_blocking=True) or {}
            cal_ctx = sns_editorial.build_calendar_context(now_jst)
            facts = {
                "time": f"{now_jst.month}/{now_jst.day}({wd}) {now_jst.strftime('%H:%M')}",
                "calendar": f"{cal_ctx.get('tag', '')} - {cal_ctx.get('note', '')}",
                "weather": weather_ctx.get("summary") or weather_ctx.get("adlib_hint") or "",
                "zero_wait_stores": [
                    {
                        "name": p["ja_name"],
                        "casual_alias": p["casual_name"],
                        "men": p["men"],
                        "women": p["women"],
                        "event": p["event_title"] if p.get("has_event") else "",
                    }
                    for p in picks
                ],
            }
            prompt = (
                "あなたは全国53店舗の相席ラウンジ人数モニターを見ている『相席ナビ』の運営中の人です。\n"
                "これから友達と2軒目やディナー帰りにラウンジへ寄ろうか迷っている20〜30代の女性ペアに向けて、"
                "Threads用のリアルで人間味あふれる独り言・裏メモ（全体で280〜450文字以内、絶対上限480文字）を1本書いてください。\n\n"
                f"{sns_editorial.HUMAN_INSIDER_VOICE_GUIDE}\n\n"
                "【必須ルール】\n"
                "1. 1行目は『せっかく髪巻いてヒール履いて行ったのに受付で女子待ち30分って言われる夜が一番だるいんだけど…』や外の天気・曜日の空気感に絡めた生身の共感・ボヤきから入ること（角括弧【】見出し禁止）。\n"
                "2. 下記の検証済みファクトシートにある店舗名と人数（男性XX名・女性YY名）のみを使い、1人も数値を変えないこと。単体の『オリエンタル』は禁止（『オリエンタルラウンジ〇〇』または『オリラジ〇〇』）。\n"
                f"3. 文章の末尾は必ず次のフッター文字列を一字一句そのまま置いて終えること（ハッシュタグはこの末尾1個のみ）：\n{cta_footer}\n\n"
                f"【検証済みファクトシート】\n{ sns_editorial.json.dumps(facts, ensure_ascii=False) }\n\n"
                '出力はJSON形式のみ：{"thinking_memo": "どうAIっぽさを消して女子組に刺さる生の本音にしたか", "main_text": "投稿本文"}'
            )
            parsed, _, _ = sns_editorial.call_editorial_ai_json(
                prompt=prompt,
                max_output_tokens=1800,
                thinking_budget=1280,
                total_deadline_sec=22.0,
                now_jst=now_jst,
            )
            if parsed and parsed.get("main_text"):
                ai_txt = sns_editorial._postprocess_ai_main_text(parsed["main_text"], channel="threads")
                if cta_footer not in ai_txt:
                    ai_txt = re.sub(r"#相席[^\s]*", "", ai_txt).strip() + "\n\n" + cta_footer
                if 200 <= len(ai_txt) <= 490 and _verify_ladies_numbers(ai_txt, picks):
                    return ai_txt
        except Exception:
            pass

    if not picks:
        return (
            f"{now_jst.month}/{now_jst.day}({wd})の夜、これから友達と2軒目どうしようかな〜って話してる女子組へ💄\n\n"
            "正直、受付で『いま女子待ち30分です』って言われる店と『待ちゼロで即ソファ席に通される店』の差って、入る前の男女比だけでほぼ決まるんですよね。\n\n"
            "男性が先に盛り上がってる店舗へスッと入る方が、ドリンク提供も席の空気も圧倒的にラクだったりします。\n\n"
            f"{cta_footer}"
        )

    openers = [
        f"せっかく髪もメイクも仕上げて行ったのに、受付で『女子待ち30分です』って言われる瞬間が一番テンション下がるじゃないですか…（{now_jst.month}/{now_jst.day} {wd}曜のモニター裏メモ）",
        f"いまバックヤードでモニター見てたんだけど、{wd}曜のこの時間、店によって女子の入りやすさがだいぶ割れてます。",
        f"正直、女子2人組がラウンジでハズレ夜を引かない一番シンプルな裏技って『女余りの有名店を避けて、男性が先行してる店にスッと入る』これだけだったりします💄",
    ]
    opener = openers[now_jst.day % len(openers)]

    pick_lines = []
    for p in picks:
        ev_tag = "・特典あり" if p["has_event"] else ""
        pick_lines.append(f"・{p['ja_name']}（男性{p['men']}名 / 女性{p['women']}名{ev_tag}）")

    ev_line = ""
    if ev_picks and ev_picks[0]["event_title"]:
        ev_line = f"\nちなみに今日『{ev_picks[0]['ja_name']}』あたりで「{ev_picks[0]['event_title']}」も出てるみたいです。\n"

    text = (
        f"{opener}\n\n"
        f"いま男性先行で待ちゼロ案内になりやすそうなのはこのあたり。\n"
        + "\n".join(pick_lines)
        + f"\n{ev_line}\n"
        "もし微妙な席に当たったとき角を立てずに抜ける口実もサイト側に置いてあるので、入る前にちらっと覗いてみてください。\n\n"
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
    use_ai: bool = False,
) -> str:
    """Build an Instagram Feed/Carousel caption tailored for female followers ('今夜の女子得＆待ちゼロ速報')."""
    import sns_editorial
    now_jst = now_jst or datetime.datetime.now(JST)
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekdays_ja[now_jst.weekday()]
    board = build_ladies_live_board(stores)
    picks = [p for p in board["zero_wait_picks"] if p["men"] > 0][:4]
    ev_picks = board["event_picks"][:3]

    if use_ai and picks and sns_editorial.has_any_ai_key(allow_blocking=True):
        try:
            weather_ctx = sns_editorial.fetch_live_weather_context(allow_blocking=True) or {}
            cal_ctx = sns_editorial.build_calendar_context(now_jst)
            facts = {
                "time": f"{now_jst.month}/{now_jst.day}({wd}) {now_jst.strftime('%H:%M')}",
                "calendar": f"{cal_ctx.get('tag', '')} - {cal_ctx.get('note', '')}",
                "weather": weather_ctx.get("summary") or weather_ctx.get("adlib_hint") or "",
                "zero_wait_stores": [
                    {
                        "name": p["ja_name"],
                        "men": p["men"],
                        "women": p["women"],
                        "event": p["event_title"] if p.get("has_event") else "",
                    }
                    for p in picks
                ],
            }
            prompt = (
                "あなたは全国53店舗の相席ラウンジ人数モニターを見ている『相席ナビ』の運営中の人です。\n"
                "Instagramを見ている20〜30代の女性フォロワーに向けて、今夜受付待ちゼロでスムーズに入れる店舗と裏ワザを教える生身のキャプション（420〜720文字）を1本書いてください。\n\n"
                f"{sns_editorial.HUMAN_INSIDER_VOICE_GUIDE}\n\n"
                "【必須ルール】\n"
                "1. 『…続きを読む』で折りたたまれる前の最初の1〜2行目に、天気・前髪・ヒールの疲れ・女子待ちの気まずさなど、思わずタップしたくなるリアルな本音フックを書く（【】角括弧見出し禁止）。\n"
                "2. 友達と合流する前に見返せるよう自然な口調で『保存📌』を促し、下記ファクトシートの店舗名と人数（男性XX名・女性YY名）を1人の狂いもなく正確に紹介すること。単体の『オリエンタル』表記は禁止。\n"
                "3. プロフィールのリンク（aiseki-navi.com/for-women）の【女性目線モード】への案内を入れ、末尾は『#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #女子会ディナー #夜カフェ』で締めること。\n\n"
                f"【検証済みファクトシート】\n{ sns_editorial.json.dumps(facts, ensure_ascii=False) }\n\n"
                '出力はJSON形式のみ：{"thinking_memo": "推敲メモ", "main_text": "キャプション本文"}'
            )
            parsed, _, _ = sns_editorial.call_editorial_ai_json(
                prompt=prompt,
                max_output_tokens=2000,
                thinking_budget=1280,
                total_deadline_sec=22.0,
                now_jst=now_jst,
            )
            if parsed and parsed.get("main_text"):
                ai_cap = sns_editorial._postprocess_ai_main_text(parsed["main_text"], channel="instagram")
                if 320 <= len(ai_cap) <= 2000 and _verify_ladies_numbers(ai_cap, picks):
                    return ai_cap
        except Exception:
            pass

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
            ev_section = f"\n🎁 今日チェックしておきたい店舗特典・フェア\n{ev_bullets}\n"

    caption = (
        f"せっかく髪巻いてメイク直して行ったのに、受付で『いま女子待ち30分です…』って言われる夜が一番しんどいんですよね（{wd}曜夜の裏メモ💄）\n"
        "友達と合流する直前にすぐ見返せるよう、右上の「保存📌」置いといてください。\n\n"
        "実は女子人気が偏りすぎてる1店舗に突撃すると『入場制限』や『女子だけの待機席』になりがちなんだけど、"
        "逆にいま男性が先行して入っている店舗を選ぶだけで、受付待ちゼロでそのままソファ席へ通されます。\n\n"
        "🟢 いま男性先行・好バランスで入りやすい店舗\n"
        f"{pick_lines}\n"
        f"{ev_section}\n"
        "女子2人組で入るときのちょっとしたコツはこの3つだけ。\n"
        "① 入る3分前に『相席ナビ』を女性目線モードにして男性先行の店を見る\n"
        "② 顔写真付きの身分証（免許証・マイナカード等）だけは忘れない\n"
        "③ もし噛み合わない席に当たっても無理して消耗せず、お手洗いタイミングでさらっと席替え相談する\n\n"
        "全国53店舗の『いま女子待ちゼロで入れる店舗一覧』と『角が立たない席替え・退店裏マニュアル』はプロフィールのリンク（aiseki-navi.com/for-women）に置いてます🔍\n\n"
        "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #女子会ディナー #夜カフェ"
    )
    caption = re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", caption)
    return caption[:2100]
