"""
sns_editorial.py - 4-Role SNS Editorial Room Pipeline (Researcher -> Planner -> Writer -> Evaluator)

Replaces rigid, repetitive "🥇🥈🥉 TOP3" bot posts with a multi-role editorial team:
  1. Researcher (現場データ発掘係): Extracts live anomalies, hub gaps, momentum surges, usual-diff comparisons, and East/West leaders.
  2. Planner (編成・頻度調整係): Decides natural posting cadence, slot variety, and style rotation so consecutive posts never look alike.
  3. Writer (執筆・下書き係): Generates 4 distinct human-voiced draft candidates across 5 styles (hub_gap, surge_focus, usual_anomaly, east_west_talk, compact_top3).
  4. Evaluator / Desk (鬼デスク・評価係): Scores each candidate out of 100 on 4 axes (Data Freshness, Human Tone, Anti-Duplication, Character Balance), writes red-pen commentary, and selects the winner.

All editorial meetings (proposals, scores, red-pen comments, and final selection) are logged for the Master-only dashboard (/staff/sns-editorial).
"""

import os
import re
import json
import datetime
import logging

import job_state
import store_master
import x_poster

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

STYLE_LABELS = {
    "hub_gap": "🚶 エリア内ギャップ・ハシゴ観察型（人間味トーク）",
    "surge_focus": "📈 1店舗フォーカス・急増/穴場つぶやき型",
    "usual_anomaly": "💡 普段の同曜日比（平比）ギャップ発見型",
    "east_west_talk": "🗼🐙 東西トップ現場空気感トーク型",
    "compact_top3": "🥇 定番ランキング速報型（TOP3）",
}

URBAN_CLUSTERS = [
    ("新宿", "shinjuku", ("新宿", "SHINJUKU")),
    ("渋谷・恵比寿", "shibuya", ("渋谷", "恵比寿", "SHIBUYA", "EBISU")),
    ("梅田・茶屋町", "umeda", ("梅田", "茶屋町", "大阪駅前", "UMEDA", "CHAYAMACHI")),
    ("難波・心斎橋", "namba", ("難波", "心斎橋", "NAMBA", "SHINSAIBASHI")),
    ("名古屋・栄", "nagoya", ("名古屋", "栄", "錦", "NAGOYA")),
    ("福岡・天神", "fukuoka", ("福岡", "天神", "FUKUOKA")),
    ("札幌・すすきの", "sapporo", ("札幌", "SAPPORO")),
]


def _domestic_stores(stores: list) -> list:
    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    return [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]


def _enrich_with_benchmarks(stores: list) -> list:
    """Attach live benchmarks (usual_w_diff, core_time_short, midnight_retention_pct) if available."""
    try:
        import local_db
        benchmarks = local_db.get_all_stores_live_benchmarks(allow_blocking=False) or {}
    except Exception:
        benchmarks = {}

    enriched = []
    for s in stores:
        item = dict(s)
        sname = item.get("name") or ""
        w = int(item.get("women") or 0)
        m = int(item.get("men") or 0)
        tot = w + m
        item["women"] = w
        item["men"] = m
        item["total"] = tot
        item["wpct"] = round((w / tot) * 100) if tot > 0 else 0
        item["ja_name"] = x_poster.format_ja_store_name(sname)
        bm = benchmarks.get(sname) or {}
        if "usual_w_diff" not in item:
            avg_w = bm.get("usual_avg_w")
            item["usual_w_diff"] = int(round(w - avg_w)) if avg_w is not None and tot > 0 else None
        if "midnight_retention_pct" not in item:
            item["midnight_retention_pct"] = bm.get("midnight_retention_pct")
        if "core_time_short" not in item:
            item["core_time_short"] = bm.get("core_time_short")
        enriched.append(item)
    return enriched


# =====================================================================
# Role 1: Researcher (現場データ発掘係)
# =====================================================================
def run_researcher(stores: list, slot_label: str = "20:00", now_jst: datetime.datetime = None) -> dict:
    """
    Analyze live domestic store counts and extract 5 concrete field findings:
      1. Best urban cluster gender-ratio / headcount gap (for store-hopping observation)
      2. Strongest momentum surge or high-female-ratio pocket store
      3. Strongest positive anomaly vs usual same-weekday/hour average
      4. East (#1 Kanto) vs West (#1 Kansai) live comparison
      5. Overall national atmosphere & top 3 stores
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    dom = _enrich_with_benchmarks(_domestic_stores(stores))
    dom.sort(key=lambda x: (x["women"], x["men"]), reverse=True)

    total_w = sum(s["women"] for s in dom)
    total_m = sum(s["men"] for s in dom)
    active_stores = [s for s in dom if s["total"] >= 6]

    # 1. Urban cluster gap analysis
    best_cluster = None
    best_cluster_score = -1.0
    for hub_label, area_id, kws in URBAN_CLUSTERS:
        hub_s = [
            s for s in dom
            if any(k in (s.get("name") or "").upper() or k in (s.get("name") or "") for k in kws)
            and s["total"] >= 8
        ]
        if len(hub_s) >= 2:
            by_ratio = sorted(hub_s, key=lambda x: (x["wpct"], x["women"]), reverse=True)
            high_s = by_ratio[0]
            low_s = by_ratio[-1]
            pct_gap = high_s["wpct"] - low_s["wpct"]
            score = pct_gap + (high_s["women"] * 0.4)
            if high_s["women"] >= 10 and score > best_cluster_score:
                best_cluster_score = score
                best_cluster = {
                    "hub_label": hub_label,
                    "area_id": area_id,
                    "high_store": high_s,
                    "low_store": low_s,
                    "pct_gap": pct_gap,
                    "store_count": len(hub_s),
                }

    # 2. Surge / Sweet-spot store
    surge_candidates = [s for s in dom if s["women"] >= 12]
    surge_candidates.sort(
        key=lambda x: (
            int(x.get("women_diff") or 0) * 3.0
            + (x["women"] - x["men"]) * 1.5
            + x["women"] * 0.5
        ),
        reverse=True,
    )
    focus_store = surge_candidates[0] if surge_candidates else (dom[0] if dom else None)

    # 3. Usual same-weekday/hour positive anomaly
    usual_candidates = [
        s for s in dom
        if s.get("usual_w_diff") is not None and int(s.get("usual_w_diff") or 0) >= 4 and s["women"] >= 12
    ]
    usual_candidates.sort(key=lambda x: (int(x.get("usual_w_diff") or 0), x["women"]), reverse=True)
    anomaly_store = usual_candidates[0] if usual_candidates else focus_store

    # 4. East vs West top stores
    kanto_s = [s for s in dom if s.get("region") == "Kanto"]
    kansai_s = [s for s in dom if s.get("region") == "Kinki"]
    east_top = kanto_s[0] if kanto_s else (dom[0] if dom else None)
    west_top = kansai_s[0] if kansai_s else (dom[1] if len(dom) > 1 else east_top)

    # Build human-readable researcher notes for Master dashboard
    notes = []
    notes.append(f"全国稼働 {len(active_stores)}店舗（女性計 {total_w}名 / 男性計 {total_m}名）を集計。")
    if best_cluster:
        hs = best_cluster["high_store"]
        ls = best_cluster["low_store"]
        notes.append(
            f"【街別ギャップ】{best_cluster['hub_label']}で「{hs['ja_name']}（女性比{hs['wpct']}%）」と"
            f"「{ls['ja_name']}（女性比{ls['wpct']}%）」に {best_cluster['pct_gap']}pt の偏りを発見。"
        )
    if focus_store:
        w_diff = int(focus_store.get("women_diff") or 0)
        diff_txt = f"（直近15分で+{w_diff}名急増）" if w_diff > 0 else ""
        notes.append(
            f"【注目店舗】{focus_store['ja_name']}が女性{focus_store['women']}名・男性{focus_store['men']}名"
            f"（女性比{focus_store['wpct']}%）{diff_txt}で狙い目状態。"
        )
    if anomaly_store and anomaly_store.get("usual_w_diff"):
        notes.append(
            f"【普段比の異変】{anomaly_store['ja_name']}が普段の同じ曜日・時間帯より女性が+{anomaly_store['usual_w_diff']}名多く推移中。"
        )

    return {
        "slot_label": slot_label,
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "total_women": total_w,
        "total_men": total_m,
        "active_store_count": len(active_stores),
        "top_stores": dom[:5],
        "best_cluster": best_cluster,
        "focus_store": focus_store,
        "anomaly_store": anomaly_store,
        "east_top": east_top,
        "west_top": west_top,
        "research_notes": notes,
    }


# =====================================================================
# Role 2: Planner (編成・頻度調整係)
# =====================================================================
def get_recent_editorial_history(limit: int = 8) -> list:
    """Load recent editorial meeting logs stored in job_state."""
    raw = job_state.get_value("sns_editorial_log")
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items[:limit]
    except Exception:
        pass
    return []


def run_planner(research: dict, slot_label: str = "20:00", now_jst: datetime.datetime = None) -> dict:
    """
    Determine the editorial angle priority, pacing status, and whether `compact_top3` should be suppressed.
    Goal: Keep `compact_top3` to <= 20% of posts, never repeat the same style back-to-back,
    and adjust tone to match the time of night (20:00 early dash vs 22:00 peak hopping vs 00:00 after-last-train).
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    history = get_recent_editorial_history(limit=6)
    recent_styles = [h.get("selected_style") for h in history if h.get("selected_style")]
    last_style = recent_styles[0] if recent_styles else None
    top3_count_in_last_4 = sum(1 for st in recent_styles[:4] if st == "compact_top3")

    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]

    # Check if a note article or scheduled column was already announced in the last 90 minutes
    recent_post_cooldown = False
    cooldown_reason = "間隔OK（直近の他投稿との被りなし）"
    if history:
        try:
            last_ts = datetime.datetime.strptime(history[0].get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            mins_ago = abs((now_jst.replace(tzinfo=None) - last_ts).total_seconds()) / 60.0
            if mins_ago < 75:
                recent_post_cooldown = True
                cooldown_reason = f"直近の投稿から{int(mins_ago)}分しか経っていないため間隔注意"
        except Exception:
            pass

    # Decide recommended primary style based on data richness and anti-repetition
    preferred_order = ["hub_gap", "surge_focus", "usual_anomaly", "east_west_talk", "compact_top3"]
    if slot_label == "20:00":
        preferred_order = ["usual_anomaly", "surge_focus", "hub_gap", "east_west_talk", "compact_top3"]
    elif slot_label in ("22:00", "22:30"):
        preferred_order = ["hub_gap", "surge_focus", "east_west_talk", "usual_anomaly", "compact_top3"]
    elif slot_label in ("00:00", "02:00"):
        preferred_order = ["surge_focus", "hub_gap", "east_west_talk", "usual_anomaly", "compact_top3"]

    # Rotate by day + hour so even the same slot on consecutive days picks a fresh angle
    rot = (now_jst.day + now_jst.hour) % 4
    conversational_styles = preferred_order[:4]
    rotated_conv = conversational_styles[rot:] + conversational_styles[:rot]
    style_priority = [st for st in rotated_conv if st != last_style] + ([last_style] if last_style in rotated_conv else [])
    style_priority.append("compact_top3")

    planner_memo = (
        f"{wd_str}曜 {slot_label}枠の編成会議："
        f"直近スタイル『{STYLE_LABELS.get(last_style, 'なし')}』との連続を回避し、"
        f"『🥇🥈🥉 TOP3形式』の連投を抑えて現場観察トーク（{STYLE_LABELS.get(style_priority[0], '')}）を第一候補に設定。"
    )

    return {
        "slot_label": slot_label,
        "weekday_ja": wd_str,
        "last_style": last_style,
        "recent_styles": recent_styles[:4],
        "top3_count_in_last_4": top3_count_in_last_4,
        "suppress_top3": True,  # Always deprioritize mechanical TOP3 unless conversational drafts fail
        "style_priority": style_priority,
        "recent_post_cooldown": recent_post_cooldown,
        "cooldown_reason": cooldown_reason,
        "planner_memo": planner_memo,
    }


# =====================================================================
# Role 3: Writer (執筆・下書き係)
# =====================================================================
def run_writer(
    research: dict,
    plan: dict,
    slot_label: str = "20:00",
    now_jst: datetime.datetime = None,
    app_url: str = "",
) -> list:
    """
    Generate 4 distinct candidate drafts with different human voices and angles:
      - Candidate 1: `hub_gap` (Same-city store comparison / hopping talk)
      - Candidate 2: `surge_focus` (Single sweet-spot store + Top runners-up natural observation)
      - Candidate 3: `usual_anomaly` or `east_west_talk` (Usual weekday diff or East-West comparison)
      - Candidate 4: `compact_top3` (Classic medal TOP3 for baseline comparison in Master view)
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    t_str = now_jst.strftime("%H:%M")
    slot_code = (slot_label or "20:00").replace(":", "")
    live_url = f"{site_url}/?s={now_jst.strftime('%Y%m%d')}_{slot_code}"

    top_stores = research.get("top_stores") or []
    if not top_stores:
        return []

    s1 = top_stores[0]
    s2 = top_stores[1] if len(top_stores) > 1 else s1
    s3 = top_stores[2] if len(top_stores) > 2 else s2
    cluster = research.get("best_cluster")
    focus = research.get("focus_store") or s1
    anomaly = research.get("anomaly_store") or focus
    east = research.get("east_top") or s1
    west = research.get("west_top") or s2

    candidates = []

    # --- Draft A: hub_gap (エリア内ギャップ・ハシゴ観察型) ---
    if cluster and cluster["high_store"]["name"] != cluster["low_store"]["name"]:
        hs = cluster["high_store"]
        ls = cluster["low_store"]
        hub_name = cluster["hub_label"]
        area_id = cluster["area_id"]
        if cluster["pct_gap"] >= 10:
            body_a = (
                f"いま（{t_str}）の{hub_name}、同じエリアなのに店舗でかなり男女比が割れてる👀\n\n"
                f"・{hs['ja_name']}：女性{hs['women']}名 / 男性{hs['men']}名（女性比{hs['wpct']}%）\n"
                f"・{ls['ja_name']}：女性{ls['women']}名 / 男性{ls['men']}名（女性比{ls['wpct']}%）\n\n"
                f"全国でも{s1['ja_name']}（女性{s1['women']}名）がかなり賑わってます。移動前の確認はリプ欄から👇\n"
                f"#相席ナビ #相席ラウンジ"
            )
        else:
            body_a = (
                f"{wd_str}曜{t_str}時点、いま{hub_name}まわりが全体的に人集まってきてる🍻\n\n"
                f"・{hs['ja_name']}（女性{hs['women']}名 / 男性{hs['men']}名・女性比{hs['wpct']}%）\n"
                f"・{ls['ja_name']}（女性{ls['women']}名 / 男性{ls['men']}名）\n\n"
                f"1軒目で待ちが出そうな時は、徒歩圏の別ブランドも覗いてみるのが確実です👇\n"
                f"#相席ナビ #相席ラウンジ"
            )
        reply_a = (
            f"📍 {hub_name}エリアの料金・現在人数横並び比較はこちら\n"
            f"{site_url}/price-comparison?area={area_id}\n\n"
            f"🔍 全国53店舗リアルタイム人数ランキング\n{live_url}"
        )
        candidates.append({
            "style": "hub_gap",
            "style_label": STYLE_LABELS["hub_gap"],
            "writer_note": f"{hub_name}内の男女比ギャップ（{hs['ja_name']} vs {ls['ja_name']}）に着目し、現場を見て呟くトーンで作成。",
            "main_text": body_a,
            "reply_text": reply_a,
        })

    # --- Draft B: surge_focus (1店舗フォーカス・急増/穴場つぶやき型) ---
    w_diff = int(focus.get("women_diff") or 0)
    if slot_label == "20:00":
        opener_b = f"まだ20時台だけど、今夜は「{focus['ja_name']}」の出足がかなり早い👀"
        closing_b = "21時半を過ぎると一気に混み始めるので、待ち時間なしで座るなら今のうちが狙い目です👇"
    elif slot_label in ("00:00", "02:00"):
        opener_b = f"終電を過ぎたこの時間（{t_str}）、いま一番人が残ってるのは「{focus['ja_name']}」🌙"
        closing_b = "深夜帯は人が集まる店と静かな店がはっきり分かれるので、2軒目・3次会前に人数見ておくと外しません👇"
    else:
        if w_diff >= 3:
            opener_b = f"{t_str}時点、いま「{focus['ja_name']}」に直近で女性が+{w_diff}名増えて一気にいいバランスになってる🔥"
        else:
            opener_b = f"{t_str}時点、いま女性比率が高めで座りやすそうなのが「{focus['ja_name']}」✨"
        closing_b = "今から1軒目・2軒目で動く人は、向かう前に近くの店舗と見比べてみてください👇"

    other_top = [s for s in top_stores if s["name"] != focus["name"]][:2]
    other_line = ""
    if len(other_top) >= 2:
        other_line = f"ほかにも{other_top[0]['ja_name']}（女性{other_top[0]['women']}名）、{other_top[1]['ja_name']}（女性{other_top[1]['women']}名）あたりが盛り上がってます。\n\n"

    body_b = (
        f"{opener_b}\n"
        f"現在：女性{focus['women']}名 / 男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
        f"{other_line}"
        f"{closing_b}\n"
        f"#相席ラウンジ #相席ナビ"
    )
    f_slug = store_master.slug_of(focus["name"])
    reply_b = (
        f"📊 {focus['ja_name']}の現在人数・終電後の残りやすさ指標はこちら\n"
        f"{site_url}/store/{f_slug}\n\n"
        f"🔍 全国53店舗リアルタイム人数ランキング\n{live_url}"
    )
    candidates.append({
        "style": "surge_focus",
        "style_label": STYLE_LABELS["surge_focus"],
        "writer_note": f"一番勢いのある「{focus['ja_name']}」を主役にし、機械的な1位・2位・3位表記を外して自然な語り口で作成。",
        "main_text": body_b,
        "reply_text": reply_b,
    })

    # --- Draft C: usual_anomaly or east_west_talk (平比ギャップ発見 or 東西現場トーク型) ---
    u_diff = anomaly.get("usual_w_diff")
    if u_diff is not None and int(u_diff) >= 4:
        body_c = (
            f"いつもの{wd_str}曜{slot_label[:2]}時台と比べても、今夜は「{anomaly['ja_name']}」に女性が+{u_diff}名多く入ってる📊\n\n"
            f"・{anomaly['ja_name']}：女性{anomaly['women']}名 / 男性{anomaly['men']}名（女性比{anomaly['wpct']}%）\n"
            f"・{s1['ja_name']}：女性{s1['women']}名 / 男性{s1['men']}名\n\n"
            f"普段の同曜日比（平比）でプラスが出てる店は当たりやすいので、今夜の候補にどうぞ👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
        w_note_c = f"普段の{wd_str}曜同時間帯より女性が+{u_diff}名多い「平比プラス」データをフックに作成。"
    else:
        body_c = (
            f"{wd_str}曜{t_str}時点、関東と関西でいま一番人が集まってる2店舗がこちら🍻\n\n"
            f"🗼 関東トップ：{east['ja_name']}（女性{east['women']}名 / 男性{east['men']}名・女性比{east['wpct']}%）\n"
            f"🐙 関西トップ：{west['ja_name']}（女性{west['women']}名 / 男性{west['men']}名・女性比{west['wpct']}%）\n\n"
            f"どっちも今かなりいいペースで推移中。各エリアの穴場や待ち状況はリプ欄へ👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
        w_note_c = f"関東トップ（{east['ja_name']}）と関西トップ（{west['ja_name']}）の東西ライブ比較で作成。"

    reply_c = (
        f"🔍 全国53店舗の現在人数＆普段の同曜日比（平比）はこちら\n{live_url}\n\n"
        f"💴 全51店舗の時間帯料金・開催中イベント比較ボード\n{site_url}/price-comparison"
    )
    candidates.append({
        "style": "usual_anomaly" if (u_diff is not None and int(u_diff) >= 4) else "east_west_talk",
        "style_label": STYLE_LABELS["usual_anomaly"] if (u_diff is not None and int(u_diff) >= 4) else STYLE_LABELS["east_west_talk"],
        "writer_note": w_note_c,
        "main_text": body_c,
        "reply_text": reply_c,
    })

    # --- Draft D: compact_top3 (従来の🥇🥈🥉ランキング型・比較ベンチマーク用) ---
    legacy_text = x_poster.build_scheduled_tweet_text(top_stores, slot_label=slot_label, app_url=site_url, compact=True)
    legacy_reply = x_poster.build_scheduled_reply_text(slot_label=slot_label, app_url=site_url)
    candidates.append({
        "style": "compact_top3",
        "style_label": STYLE_LABELS["compact_top3"],
        "writer_note": "従来の『🥇🥈🥉 TOP3』定型フォーマット（他案との比較・評価ベンチマーク用）。",
        "main_text": legacy_text,
        "reply_text": legacy_reply,
    })

    return candidates


# =====================================================================
# Role 4: Evaluator / Desk (鬼デスク・評価係)
# =====================================================================
def run_evaluator(candidates: list, research: dict, plan: dict, now_jst: datetime.datetime = None) -> dict:
    """
    Score each candidate out of 100 across 4 criteria:
      1. human_tone (35 pts): Natural conversational phrasing, absence of rigid '🥇🥈🥉' bot list formatting
      2. freshness_diversity (30 pts): Not repeating recent post styles or opening phrases
      3. data_specificity (25 pts): Concrete store names, live headcounts, hub gaps, or usual-diff numbers
      4. weight_fit (10 pts): Optimal Twitter character weight (150..274 weight, URL-free 1st post)
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    last_style = plan.get("last_style")
    recent_styles = plan.get("recent_styles") or []
    style_priority = plan.get("style_priority") or []

    evaluated = []
    for cand in candidates:
        text = cand.get("main_text") or ""
        style = cand.get("style") or ""
        weight = x_poster.compute_twitter_weight(text)

        # 1. Human Tone (max 35)
        if style == "compact_top3" or ("🥇" in text and "🥈" in text and "🥉" in text):
            human_score = 18
            human_comment = "⚠️ 毎回同じ『🥇🥈🥉 TOP3』の定型フォーマットでボット感が出やすいため減点。"
        else:
            human_score = 33
            if any(w in text for w in ("割れてる", "出足がかなり早い", "普段の", "いいバランス", "残ってる")):
                human_score = 35
            human_comment = "◎ 現場を見て呟いているような自然な語り口で、宣伝・ボット臭さが抑えられています。"

        # 2. Freshness & Anti-Duplication (max 30)
        if style == last_style:
            fresh_score = 12
            fresh_comment = f"⚠️ 直前の投稿と同じ構成（{STYLE_LABELS.get(style, style)}）のため大幅減点。"
        elif style == "compact_top3" and plan.get("suppress_top3"):
            fresh_score = 15
            fresh_comment = "⚠️ ランキング羅列型の連投防止ルールにより優先度を引き下げ。"
        elif style in recent_styles[:2]:
            fresh_score = 22
            fresh_comment = "◯ 直近2回以内に近い型があるため、別角度の案を優先。"
        else:
            rank_bonus = max(0, 4 - style_priority.index(style)) if style in style_priority else 1
            fresh_score = min(30, 26 + rank_bonus)
            fresh_comment = "◎ 直近のタイムラインと切り口が被っておらず、新鮮さが高い構成です。"

        # 3. Data Specificity (max 25)
        data_score = 20
        if style == "hub_gap" and research.get("best_cluster"):
            gap = research["best_cluster"].get("pct_gap", 0)
            data_score = 25 if gap >= 10 else 22
            data_comment = f"◎ 同一エリア内の男女比ギャップ（{gap}pt差）が具体的で、ハシゴ判断に直結。"
        elif style == "usual_anomaly":
            data_score = 25
            data_comment = "◎ 普段の同曜日・同時間帯比（平比）のプラス根拠が入っており説得力が高い。"
        elif style == "surge_focus":
            data_score = 24
            data_comment = "◎ 注目店舗の現在人数と他上位店のバランスが分かりやすい。"
        else:
            data_comment = "◯ 基本の人数データは網羅。"

        # 4. Character Weight & Format Hygiene (max 10)
        has_url_in_main = "http://" in text or "https://" in text
        if weight > 280 or has_url_in_main:
            weight_score = 2
            weight_comment = f"⚠️ 文字数ウェイト({weight}/280)または1ポスト目URL混入のため減点。"
        elif 140 <= weight <= 274:
            weight_score = 10
            weight_comment = f"◎ 文字数ウェイト（{weight}/280・1ポスト目URLなし）が最適レンジ。"
        else:
            weight_score = 7
            weight_comment = f"◯ 文字数ウェイト（{weight}/280）は許容範囲内。"

        total_score = human_score + fresh_score + data_score + weight_score
        item_copy = dict(cand)
        item_copy.update({
            "total_score": total_score,
            "weight": weight,
            "scores": {
                "human_tone": human_score,
                "freshness_diversity": fresh_score,
                "data_specificity": data_score,
                "weight_fit": weight_score,
            },
            "desk_comments": [
                human_comment,
                fresh_comment,
                data_comment,
                weight_comment,
            ],
        })
        evaluated.append(item_copy)

    evaluated.sort(key=lambda x: x["total_score"], reverse=True)
    for idx, item in enumerate(evaluated):
        item["rank"] = idx + 1
        item["is_selected"] = (idx == 0)

    winner = evaluated[0] if evaluated else None
    desk_summary = (
        f"全{len(evaluated)}案を採点し、最高スコア {winner['total_score']}点（100点満点）の"
        f"『{winner['style_label']}』を採用しました。"
        f"従来の🥇🥈🥉定型ランキング（{next((c['total_score'] for c in evaluated if c['style'] == 'compact_top3'), '-')}点）を避け、"
        f"人間が現場データを見て呟く自然な文面に仕上げています。"
        if winner else "候補案なし"
    )

    return {
        "winner": winner,
        "candidates": evaluated,
        "desk_summary": desk_summary,
    }


def run_editorial_pipeline(
    stores: list,
    slot_label: str = "20:00",
    now_jst: datetime.datetime = None,
    app_url: str = "",
    record_history: bool = False,
) -> dict:
    """
    Run the full 4-role SNS Editorial Room pipeline and optionally persist the meeting record
    to `job_state` so the Master dashboard (`/staff/sns-editorial`) can inspect past & live sessions.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    research = run_researcher(stores, slot_label=slot_label, now_jst=now_jst)
    plan = run_planner(research, slot_label=slot_label, now_jst=now_jst)
    candidates = run_writer(research, plan, slot_label=slot_label, now_jst=now_jst, app_url=app_url)
    evaluation = run_evaluator(candidates, research, plan, now_jst=now_jst)

    winner = evaluation.get("winner") or {}
    session_record = {
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "slot_label": slot_label,
        "selected_style": winner.get("style", ""),
        "selected_style_label": winner.get("style_label", ""),
        "selected_score": winner.get("total_score", 0),
        "selected_text": winner.get("main_text", ""),
        "selected_reply": winner.get("reply_text", ""),
        "desk_summary": evaluation.get("desk_summary", ""),
        "planner_memo": plan.get("planner_memo", ""),
        "research_notes": research.get("research_notes", []),
        "candidates": [
            {
                "rank": c.get("rank"),
                "is_selected": c.get("is_selected"),
                "style": c.get("style"),
                "style_label": c.get("style_label"),
                "total_score": c.get("total_score"),
                "weight": c.get("weight"),
                "scores": c.get("scores"),
                "writer_note": c.get("writer_note"),
                "desk_comments": c.get("desk_comments"),
                "main_text": c.get("main_text"),
                "reply_text": c.get("reply_text"),
            }
            for c in (evaluation.get("candidates") or [])
        ],
    }

    if record_history and winner:
        try:
            history = get_recent_editorial_history(limit=11)
            history.insert(0, session_record)
            job_state.set_value("sns_editorial_log", json.dumps(history[:12], ensure_ascii=False))
        except Exception as e:
            logger.warning(f"Failed to persist sns_editorial_log: {e}")

    return {
        "research": research,
        "plan": plan,
        "evaluation": evaluation,
        "winner": winner,
        "session_record": session_record,
        "history": get_recent_editorial_history(limit=8),
    }
