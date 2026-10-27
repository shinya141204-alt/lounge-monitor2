"""
sns_editorial.py - Multi-Platform 4-Role SNS Editorial Room Pipeline (X / Threads / Instagram)

Replaces rigid, repetitive "🥇🥈🥉 TOP3" bot posts across ALL 3 SNS channels (X, Threads, Instagram)
with a 4-role virtual editorial team:
  1. Researcher (現場データ発掘係): Extracts live hub gender-ratio gaps, 15-min surges, usual same-weekday anomalies, and East/West leaders, while verifying strict factual thresholds.
  2. Planner (編成・マンネリ防止係): Tracks per-channel history (`x`, `threads`, `instagram`) so consecutive posts never repeat the same angle or hook, and suppresses mechanical TOP3 lists.
  3. Writer (執筆・下書き係): Generates 4 distinct human-voiced draft candidates tailored to each platform's algorithm and character rules.
  4. Evaluator / Desk (鬼デスク・評価係): Scores all 4 candidates out of 100 across 4 axes (Human Tone, Anti-Duplication, Data Accuracy/Specificity, Platform Format Hygiene), attaches red-pen critiques, and selects the #1 winner.

All proposals, scores, and critiques are exposed exclusively to Master (`2918`) on `/staff/sns-editorial`.
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

CHANNEL_LABELS = {
    "x": "𝕏 X（旧Twitter）",
    "threads": "🧵 Threads（スレッズ）",
    "instagram": "📸 Instagram（インスタグラム）",
}

STYLE_LABELS = {
    # X styles
    "hub_gap": "🚶 エリア内ギャップ・ハシゴ観察型",
    "surge_focus": "📈 1店舗フォーカス・急増/穴場つぶやき型",
    "usual_anomaly": "💡 普段の同曜日比（平比）ギャップ発見型",
    "east_west_talk": "🗼🐙 東西トップ現場空気感トーク型",
    "compact_top3": "🥇 従来型ランキング速報（🥇🥈🥉 TOP3）",
    # Threads styles
    "threads_city_story": "🗣️ 街の温度差＆リアル現場エッセイ型",
    "threads_anomaly_insight": "📊 普段比（平比）の異変＆男女別立ち回り型",
    "threads_hopping_guide": "👟 東西4大エリア「1軒目→2軒目」実践比較型",
    "threads_classic_top3": "🏆 従来型・全国TOP3＆主要街掲示板型",
    # Instagram styles
    "ig_empathy_story": "💬 共感1行目フック＆待ち回避ストーリー型",
    "ig_data_discovery": "📖 データ編集長コラム＆穴場法則マガジン型",
    "ig_concise_save": "📌 余白重視・男女別スマート保存ガイド型",
    "ig_classic_template": "📊 従来型・定型アナウンスキャプション",
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


def natural_store_name(raw_name: str) -> str:
    """
    Convert internal codes like 'OLG 新宿' or 'JIS SHINJUKU' into natural, human-searchable Japanese names:
      - 'OLG 新宿' -> 'オリエンタル新宿'
      - 'OLG 大阪駅前' -> 'オリエンタル大阪駅前'
      - 'OLG ag渋谷' -> 'ag渋谷'
      - 'JIS SHINJUKU' -> 'JIS新宿'
      - 'YATAKOI UMEDA' -> 'ヤタコイ梅田'
    """
    formatted = x_poster.format_ja_store_name(raw_name)
    if formatted.startswith("OLG "):
        return "オリエンタル" + formatted[4:].replace(" ", "")
    if formatted.startswith("JIS "):
        return "JIS" + formatted[4:].replace(" ", "")
    return formatted


def _domestic_stores(stores: list) -> list:
    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    return [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]


def _enrich_with_benchmarks(stores: list) -> list:
    """Attach live benchmarks (usual_w_diff, core_time_short, midnight_retention_pct) and natural Japanese names."""
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
        item["ja_name"] = natural_store_name(sname)
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


def _history_key_for_channel(channel: str) -> str:
    ch = (channel or "x").lower()
    if ch == "x":
        return "sns_editorial_log"
    return f"sns_editorial_log_{ch}"


def get_recent_editorial_history(channel: str = "x", limit: int = 8) -> list:
    """Load recent editorial meeting logs for the specified SNS channel."""
    raw = job_state.get_value(_history_key_for_channel(channel))
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items[:limit]
    except Exception:
        pass
    return []


# =====================================================================
# Role 1: Researcher (現場データ発掘係)
# =====================================================================
def run_researcher(
    stores: list,
    slot_label: str = "22:00",
    channel: str = "x",
    ig_ctx: dict = None,
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Analyze live domestic store counts (and weekly carousel stats for IG) to extract verified field findings:
      1. Best same-city gender-ratio gap (`best_cluster`) where one store is female-lead and another differs
      2. Genuine sweet-spot / momentum surge store (`focus_store` with women >= men or top surge)
      3. Positive anomaly vs usual same-weekday/hour average (`anomaly_store`)
      4. City leaders across Shinjuku, Shibuya, Umeda, Namba, Nagoya, Fukuoka
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
    city_leaders = []
    for hub_label, area_id, kws in URBAN_CLUSTERS:
        hub_s = [
            s for s in dom
            if any(k in (s.get("name") or "").upper() or k in (s.get("name") or "") for k in kws)
        ]
        active_hub = [s for s in hub_s if s["total"] >= 8]
        substantial_hub = [s for s in hub_s if s["total"] >= 24]
        candidate_hub = substantial_hub if len(substantial_hub) >= 2 else active_hub
        if hub_s and hub_s[0]["women"] > 0:
            city_leaders.append({"hub_label": hub_label, "area_id": area_id, "store": hub_s[0]})

        if len(candidate_hub) >= 2:
            by_ratio = sorted(candidate_hub, key=lambda x: (x["wpct"], x["women"]), reverse=True)
            high_s = by_ratio[0]
            low_s = by_ratio[-1]
            pct_gap = high_s["wpct"] - low_s["wpct"]
            score = pct_gap * 1.35 + (high_s["women"] * 0.25) + (min(high_s["total"], low_s["total"]) * 0.25)
            if high_s["women"] >= 10 and score > best_cluster_score:
                best_cluster_score = score
                best_cluster = {
                    "hub_label": hub_label,
                    "area_id": area_id,
                    "high_store": high_s,
                    "low_store": low_s,
                    "pct_gap": pct_gap,
                    "store_count": len(active_hub),
                }

    # 2. Genuine sweet-spot / surge store (strictly prefer stores where women >= men so we never call a male-heavy store "穴場")
    female_lead_pool = [s for s in dom if s["women"] >= 12 and s["women"] >= s["men"]]
    if not female_lead_pool:
        female_lead_pool = [s for s in dom if s["women"] >= 10]
    female_lead_pool.sort(
        key=lambda x: (
            int(x.get("women_diff") or 0) * 3.0
            + (x["women"] - x["men"]) * 1.6
            + x["women"] * 0.45
        ),
        reverse=True,
    )
    focus_store = female_lead_pool[0] if female_lead_pool else (dom[0] if dom else None)

    # Also detect strongest male-lead lively store (great for female followers: 0-min wait)
    male_lead_pool = [s for s in dom if s["men"] >= 18 and s["women"] >= 10 and (s["men"] - s["women"]) >= 6]
    male_lead_pool.sort(key=lambda x: (x["men"] - x["women"], x["men"]), reverse=True)
    male_lead_store = male_lead_pool[0] if male_lead_pool else None

    # 3. Usual same-weekday/hour positive anomaly
    usual_candidates = [
        s for s in dom
        if s.get("usual_w_diff") is not None and int(s.get("usual_w_diff") or 0) >= 4 and s["women"] >= 12
    ]
    usual_candidates.sort(key=lambda x: (int(x.get("usual_w_diff") or 0), x["women"]), reverse=True)
    anomaly_store = usual_candidates[0] if usual_candidates else None

    # 4. East vs West top stores
    kanto_s = [s for s in dom if s.get("region") == "Kanto"]
    kansai_s = [s for s in dom if s.get("region") == "Kinki"]
    east_top = kanto_s[0] if kanto_s else (dom[0] if dom else None)
    west_top = kansai_s[0] if kansai_s else (dom[1] if len(dom) > 1 else east_top)

    notes = [
        f"対象媒体：{CHANNEL_LABELS.get(channel, channel)} ｜ 全国稼働 {len(active_stores)}店舗（女性計 {total_w:,}名 / 男性計 {total_m:,}名）を解析。"
    ]
    if best_cluster:
        hs = best_cluster["high_store"]
        ls = best_cluster["low_store"]
        notes.append(
            f"【街別ギャップ】{best_cluster['hub_label']}で「{hs['ja_name']}（女性{hs['women']}・男性{hs['men']} / 女性比{hs['wpct']}%）」と"
            f"「{ls['ja_name']}（女性{ls['women']}・男性{ls['men']} / 女性比{ls['wpct']}%）」に {best_cluster['pct_gap']}pt の差を検出。"
        )
    if focus_store:
        w_diff = int(focus_store.get("women_diff") or 0)
        diff_txt = f"（15分前比 +{w_diff}名）" if w_diff > 0 else ""
        notes.append(
            f"【男性向け穴場】{focus_store['ja_name']}が女性{focus_store['women']}名・男性{focus_store['men']}名"
            f"（女性比{focus_store['wpct']}%）{diff_txt}で待ち回避に最適。"
        )
    if anomaly_store:
        notes.append(
            f"【普段比プラス】{anomaly_store['ja_name']}が普段の同曜日・同時間帯より女性 +{anomaly_store['usual_w_diff']}名で上振れ中。"
        )
    if ig_ctx and ig_ctx.get("top_anaba"):
        top_a = ig_ctx["top_anaba"][0]
        notes.append(
            f"【カルーセル連動】スライド3枚目の高女性比率1位は「{top_a['seo_name']}（{top_a['avg_wpct']}%）」。"
        )

    return {
        "channel": channel,
        "slot_label": slot_label,
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "total_women": total_w,
        "total_men": total_m,
        "domestic_count": len(dom),
        "active_store_count": len(active_stores),
        "top_stores": dom[:6],
        "city_leaders": city_leaders,
        "best_cluster": best_cluster,
        "focus_store": focus_store,
        "male_lead_store": male_lead_store,
        "anomaly_store": anomaly_store,
        "east_top": east_top,
        "west_top": west_top,
        "ig_ctx": ig_ctx,
        "research_notes": notes,
    }


# =====================================================================
# Role 2: Planner (編成・頻度調整係)
# =====================================================================
def run_planner(
    research: dict,
    slot_label: str = "22:00",
    channel: str = "x",
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Determine style rotation and anti-repetition rules per SNS channel (`x`, `threads`, `instagram`).
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    history = get_recent_editorial_history(channel=ch, limit=6)
    recent_styles = [h.get("selected_style") for h in history if h.get("selected_style")]
    last_style = recent_styles[0] if recent_styles else None

    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]

    recent_post_cooldown = False
    cooldown_reason = "間隔OK（同チャンネルの直近投稿から十分な時間が経過）"
    if history:
        try:
            last_ts = datetime.datetime.strptime(history[0].get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            mins_ago = abs((now_jst.replace(tzinfo=None) - last_ts).total_seconds()) / 60.0
            if mins_ago < 90:
                recent_post_cooldown = True
                cooldown_reason = f"直近の{CHANNEL_LABELS.get(ch, ch)}投稿から{int(mins_ago)}分のため連投注意"
        except Exception:
            pass

    if ch == "threads":
        base_conv = ["threads_city_story", "threads_anomaly_insight", "threads_hopping_guide"]
        classic_key = "threads_classic_top3"
    elif ch == "instagram":
        base_conv = ["ig_empathy_story", "ig_data_discovery", "ig_concise_save"]
        classic_key = "ig_classic_template"
    else:
        if slot_label == "20:00":
            base_conv = ["usual_anomaly", "surge_focus", "hub_gap", "east_west_talk"]
        elif slot_label in ("22:00", "22:30"):
            base_conv = ["hub_gap", "surge_focus", "east_west_talk", "usual_anomaly"]
        else:
            base_conv = ["surge_focus", "hub_gap", "east_west_talk", "usual_anomaly"]
        classic_key = "compact_top3"

    rot = (now_jst.day + now_jst.hour) % len(base_conv)
    rotated = base_conv[rot:] + base_conv[:rot]
    style_priority = [st for st in rotated if st != last_style] + ([last_style] if last_style in rotated else [])
    style_priority.append(classic_key)

    planner_memo = (
        f"【{CHANNEL_LABELS.get(ch, ch)}・{wd_str}曜編成】"
        f"前回採用『{STYLE_LABELS.get(last_style, '履歴なし')}』との被りをブロックし、"
        f"定型テンプレ（{STYLE_LABELS.get(classic_key, '')}）を抑えて『{STYLE_LABELS.get(style_priority[0], '')}』を最優先に設定。"
    )

    return {
        "channel": ch,
        "slot_label": slot_label,
        "weekday_ja": wd_str,
        "last_style": last_style,
        "recent_styles": recent_styles[:4],
        "classic_key": classic_key,
        "suppress_classic": True,
        "style_priority": style_priority,
        "recent_post_cooldown": recent_post_cooldown,
        "cooldown_reason": cooldown_reason,
        "planner_memo": planner_memo,
    }


# =====================================================================
# Role 3: Writer (執筆・下書き係) — X / Threads / Instagram 専用ライティング
# =====================================================================
def _pick_distinct_store(stores: list, exclude_names: set):
    for s in stores:
        if s.get("name") not in exclude_names and int(s.get("women") or 0) > 0:
            return s
    return stores[0] if stores else None


def _write_x_candidates(research: dict, slot_label: str, now_jst: datetime.datetime, site_url: str) -> list:
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    t_str = now_jst.strftime("%H:%M")
    slot_code = (slot_label or "20:00").replace(":", "")
    live_url = f"{site_url}/?s={now_jst.strftime('%Y%m%d')}_{slot_code}"

    top_stores = research.get("top_stores") or []
    if not top_stores:
        return []

    s1 = top_stores[0]
    cluster = research.get("best_cluster")
    focus = research.get("focus_store") or s1
    anomaly = research.get("anomaly_store")
    east = research.get("east_top") or s1
    west = research.get("west_top") or _pick_distinct_store(top_stores, {east["name"]})

    candidates = []

    # 1) hub_gap: Same-city store gap without repeating the same store twice
    if cluster and cluster["high_store"]["name"] != cluster["low_store"]["name"]:
        hs = cluster["high_store"]
        ls = cluster["low_store"]
        hub_name = cluster["hub_label"]
        area_id = cluster["area_id"]
        outside_top = _pick_distinct_store(top_stores, {hs["name"], ls["name"]})
        outside_line = (
            f"他エリアは{outside_top['ja_name']}（女性{outside_top['women']}名）も好調。"
            if outside_top else ""
        )

        if cluster["pct_gap"] >= 12:
            body_a = (
                f"いま（{t_str}）の{hub_name}、徒歩圏でも店舗で男女比が割れてる👀\n\n"
                f"・{hs['ja_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                f"・{ls['ja_name']}：女性{ls['women']}・男性{ls['men']}（女性比{ls['wpct']}%）\n\n"
                f"{outside_line}比較はリプ欄へ👇\n"
                f"#相席ナビ #相席ラウンジ"
            )
            if x_poster.compute_twitter_weight(body_a) > 272:
                body_a = (
                    f"いま（{t_str}）の{hub_name}、徒歩圏でも店舗で男女比が割れてる👀\n\n"
                    f"・{hs['ja_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                    f"・{ls['ja_name']}：女性{ls['women']}・男性{ls['men']}（女性比{ls['wpct']}%）\n\n"
                    f"向かう前の比較はリプ欄から👇\n"
                    f"#相席ナビ #相席ラウンジ"
                )
        else:
            body_a = (
                f"{wd_str}曜{t_str}時点、いま{hub_name}まわりがいいバランスで集まってる🍻\n\n"
                f"・{hs['ja_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                f"・{ls['ja_name']}：女性{ls['women']}・男性{ls['men']}\n\n"
                f"待ちが出そうな時は近くの別店舗も覗くのが確実👇\n"
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
            "writer_note": f"{hub_name}内の男女比差（{hs['ja_name']} {hs['wpct']}% vs {ls['ja_name']} {ls['wpct']}%）を切り取り、同一店舗の重複言及を排除して作成。",
            "main_text": body_a,
            "reply_text": reply_a,
        })

    # 2) surge_focus: Spotlight 1 sweet-spot store + 2 distinct runners-up
    w_diff = int(focus.get("women_diff") or 0)
    if slot_label == "20:00":
        opener_b = f"まだ20時台だけど、今夜は「{focus['ja_name']}」の立ち上がりがかなり早い👀"
        closing_b = "混み始める前に待ちなしで座るなら今のうちが狙い目👇"
    elif slot_label in ("00:00", "02:00"):
        opener_b = f"終電すぎのこの時間（{t_str}）、いま一番女性が残ってるのは「{focus['ja_name']}」🌙"
        closing_b = "深夜は人が集まる店と静かな店が分かれるので事前確認が確実👇"
    else:
        if w_diff >= 3:
            opener_b = f"{t_str}時点、「{focus['ja_name']}」に直近で女性が+{w_diff}名増えて狙い目🔥"
        else:
            opener_b = f"{t_str}時点、いま女性比率が高めで座りやすいのが「{focus['ja_name']}」✨"
        closing_b = "向かう前に近隣店舗のバランスも見比べてみてください👇"

    other_top = [s for s in top_stores if s["name"] != focus["name"]][:2]
    other_line = ""
    if len(other_top) >= 2:
        other_line = f"ほかにも{other_top[0]['ja_name']}（女性{other_top[0]['women']}名）、{other_top[1]['ja_name']}（女性{other_top[1]['women']}名）が好調。\n\n"

    body_b = (
        f"{opener_b}\n"
        f"現在：女性{focus['women']}名 / 男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
        f"{other_line}"
        f"{closing_b}\n"
        f"#相席ラウンジ #相席ナビ"
    )
    if x_poster.compute_twitter_weight(body_b) > 272 and other_top:
        body_b = (
            f"{opener_b}\n"
            f"現在：女性{focus['women']}名 / 男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
            f"ほかにも{other_top[0]['ja_name']}（女性{other_top[0]['women']}名）が盛り上がってます。\n"
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
        "writer_note": f"女性比・勢いが良好な「{focus['ja_name']}」を主役に据え、機械的なメダル記号を使わず自然な観察文で作成。",
        "main_text": body_b,
        "reply_text": reply_b,
    })

    # 3) usual_anomaly or east_west_talk (strictly distinct second store!)
    if anomaly and anomaly.get("usual_w_diff") and int(anomaly["usual_w_diff"]) >= 4:
        u_diff = int(anomaly["usual_w_diff"])
        partner = _pick_distinct_store(top_stores, {anomaly["name"]})
        partner_line = (
            f"・{partner['ja_name']}：女性{partner['women']}・男性{partner['men']}（女性比{partner['wpct']}%）\n"
            if partner else ""
        )
        body_c = (
            f"いつもの{wd_str}曜{slot_label[:2]}時台より、今夜は「{anomaly['ja_name']}」に女性が+{u_diff}名多く入ってる📊\n\n"
            f"・{anomaly['ja_name']}：女性{anomaly['women']}・男性{anomaly['men']}（女性比{anomaly['wpct']}%）\n"
            f"{partner_line}\n"
            f"普段比プラスの店は当たりやすいので今夜の候補にどうぞ👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
        c_style = "usual_anomaly"
        w_note_c = f"普段の{wd_str}曜同時間帯より女性が+{u_diff}名多い実測ギャップをフックに作成。"
    else:
        body_c = (
            f"{wd_str}曜{t_str}時点、関東と関西でいま一番人が集まってる2店舗🍻\n\n"
            f"🗼関東：{east['ja_name']}（女性{east['women']}・男性{east['men']} / {east['wpct']}%）\n"
            f"🐙関西：{west['ja_name']}（女性{west['women']}・男性{west['men']} / {west['wpct']}%）\n\n"
            f"どちらも好ペースで推移中。各街の穴場・待ち状況はリプ欄へ👇\n"
            f"#相席ナビ #相席ラウンジ"
        )
        c_style = "east_west_talk"
        w_note_c = f"関東トップ（{east['ja_name']}）と関西トップ（{west['ja_name']}）を対比し、東西どちらのフォロワーにも刺さる構成で作成。"

    reply_c = (
        f"🔍 全国53店舗の現在人数＆普段の同曜日比（平比）はこちら\n{live_url}\n\n"
        f"💴 全51店舗の時間帯料金・開催中イベント比較ボード\n{site_url}/price-comparison"
    )
    candidates.append({
        "style": c_style,
        "style_label": STYLE_LABELS[c_style],
        "writer_note": w_note_c,
        "main_text": body_c,
        "reply_text": reply_c,
    })

    # 4) compact_top3 (Classic baseline for Desk comparison)
    legacy_text = x_poster.build_scheduled_tweet_text(top_stores, slot_label=slot_label, app_url=site_url, compact=True)
    legacy_reply = x_poster.build_scheduled_reply_text(slot_label=slot_label, app_url=site_url)
    candidates.append({
        "style": "compact_top3",
        "style_label": STYLE_LABELS["compact_top3"],
        "writer_note": "従来の『🥇🥈🥉 TOP3』定型フォーマット（デスク比較採点用ベンチマーク）。",
        "main_text": legacy_text,
        "reply_text": legacy_reply,
    })

    return candidates


def _write_threads_candidates(research: dict, slot_label: str, now_jst: datetime.datetime, site_url: str) -> list:
    """
    Generate 4 Meta Threads candidates (strictly <= 495 chars; conversational essay style works best on Threads).
    Respects odd/even day URL vs Bio-link anti-spam rotation.
    """
    import threads_poster
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[now_jst.weekday()]
    t_str = now_jst.strftime("%H:%M")

    top_stores = research.get("top_stores") or []
    if not top_stores:
        return []

    cluster = research.get("best_cluster")
    focus = research.get("focus_store") or top_stores[0]
    male_lead = research.get("male_lead_store")
    anomaly = research.get("anomaly_store")
    city_leaders = research.get("city_leaders") or []

    # Meta anti-spam link rotation (even day = bio link, odd day = direct link)
    if now_jst.day % 2 == 0:
        cta_footer = "🔍 全51店舗の現在人数＆料金比較はプロフィールのリンク（相席ナビ）から見れます\n#相席ラウンジ"
    else:
        cta_footer = f"▼ 全51店舗の現在人数＆料金・イベント比較\n{site_url}/price-comparison\n#相席ラウンジ"

    candidates = []

    # 1) threads_city_story: Conversational street-level temperature check
    if cluster and cluster["high_store"]["name"] != cluster["low_store"]["name"]:
        hs = cluster["high_store"]
        ls = cluster["low_store"]
        hub_name = cluster["hub_label"]
        other_cities = [c for c in city_leaders if c["hub_label"] != hub_name][:3]
        city_sub = "\n".join(
            f"・{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}・男性{c['store']['men']}）"
            for c in other_cities
        )
        if cluster["pct_gap"] >= 12 and ls["wpct"] <= 48:
            lead_1 = f"{wd_str}曜{t_str}すぎ、今夜は{hub_name}まわりで店舗ごとの男女比がかなりはっきり分かれてます👀"
            contrast_1 = f"近くの「{ls['ja_name']}」は女性{ls['women']}名・男性{ls['men']}名（女性比{ls['wpct']}%）と男性先行で空気感が対照的。"
        elif cluster["pct_gap"] >= 10:
            lead_1 = f"{wd_str}曜{t_str}すぎ、同じ{hub_name}エリアでも店舗によって入りやすさに結構差が出てます👀"
            contrast_1 = f"近くの「{ls['ja_name']}」は女性{ls['women']}名・男性{ls['men']}名（女性比{ls['wpct']}%）で推移中。"
        else:
            lead_1 = f"{wd_str}曜{t_str}すぎ、今夜は{hub_name}エリアの大型店がどこも高い熱量で盛り上がってます🍻"
            contrast_1 = f"近くの「{ls['ja_name']}」も女性{ls['women']}名・男性{ls['men']}名（女性比{ls['wpct']}%）と好バランス。"
        body_1 = (
            f"{lead_1}\n\n"
            f"たとえば「{hs['ja_name']}」は女性{hs['women']}名・男性{hs['men']}名（女性比{hs['wpct']}%）で座りやすく、"
            f"{contrast_1}\n\n"
            f"ほかの主要エリアの現在トップはこんな感じ👇\n"
            f"{city_sub}\n\n"
            f"1軒目で「男性待ち長そうだな」と思ったら、無理に粘らず徒歩3〜4分の別ブランドを覗くのが一番コスパいいです🥂\n\n"
            f"{cta_footer}"
        )
    else:
        c_lines = "\n".join(
            f"・{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}名 / 男性{c['store']['men']}名）"
            for c in city_leaders[:4]
        )
        body_1 = (
            f"{wd_str}曜{t_str}すぎ、全国{research['domestic_count']}店舗でいま女性{research['total_women']:,}名・男性{research['total_men']:,}名が入ってます🥂\n\n"
            f"今夜特にバランスがいいのは「{focus['ja_name']}（女性{focus['women']}名 / 男性{focus['men']}名・女性比{focus['wpct']}%）」。\n\n"
            f"主要エリアの現在トップメモ📝\n{c_lines}\n\n"
            f"向かう前の30秒で人数バランスを見ておくだけで「行ったのに1時間待ち」を回避できます。\n\n"
            f"{cta_footer}"
        )

    candidates.append({
        "style": "threads_city_story",
        "style_label": STYLE_LABELS["threads_city_story"],
        "writer_note": "Threadsで最も伸びやすい「現場の温度差を語りかけるエッセイ調」で作成（メダル記号なし）。",
        "main_text": body_1[:495],
        "reply_text": "（Threadsは1投稿完結・偶数日はプロフ誘導／奇数日はURL付与でリンク制限を回避）",
    })

    # 2) threads_anomaly_insight: Both Male & Female perspective + usual diff
    if anomaly and anomaly.get("usual_w_diff"):
        hook_2 = (
            f"いつもの{wd_str}曜夜と比べても、今夜は「{anomaly['ja_name']}」に普段より女性が+{anomaly['usual_w_diff']}名多く集まってます📊"
        )
    else:
        hook_2 = (
            f"今夜（{wd_str}曜{t_str}）の相席ラウンジ、全国で女性{research['total_women']:,}名・男性{research['total_men']:,}名とかなり動いてます🥂"
        )

    if male_lead:
        female_tip = (
            f"逆に女性グループなら、いま男性{male_lead['men']}名・女性{male_lead['women']}名で盛り上がっている"
            f"「{male_lead['ja_name']}」あたりが待ち時間ゼロで即乾杯できて狙い目です✨"
        )
    else:
        female_tip = "逆に女性グループなら、男性が先行して入っている大型店を選ぶと待ち0分で即乾杯しやすいです✨"

    runner = _pick_distinct_store(top_stores, {focus["name"]})
    runner_txt = f"と「{runner['ja_name']}（女性{runner['women']}名 / 男性{runner['men']}名）」" if runner else ""

    body_2 = (
        f"{hook_2}\n\n"
        f"男性目線でいま待ち時間を避けて座りやすいのは「{focus['ja_name']}（女性{focus['women']}名 / 男性{focus['men']}名・女性比{focus['wpct']}%）」{runner_txt}。\n\n"
        f"{female_tip}\n\n"
        f"同じ街でも店によって男女比が15〜25%違うので、入る前にチラッと比較してみてください👇\n\n"
        f"{cta_footer}"
    )
    candidates.append({
        "style": "threads_anomaly_insight",
        "style_label": STYLE_LABELS["threads_anomaly_insight"],
        "writer_note": "男性向けの穴場店舗と、女性向けの即相席店舗（男性先行店）の両目線を入れて保存・共感を狙う構成。",
        "main_text": body_2[:495],
        "reply_text": "（Threadsは1投稿完結）",
    })

    # 3) threads_hopping_guide: 4-hub quick hopping guide
    hub_bullets = "\n".join(
        f"📍{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}・男性{c['store']['men']} / 女性比{c['store']['wpct']}%）"
        for c in city_leaders[:4]
    )
    body_3 = (
        f"いま（{t_str}時点）から1軒目・2軒目で相席ラウンジ動く人向けに、主要エリアで一番人が集まってる店舗だけ抜粋しました📝\n\n"
        f"{hub_bullets}\n\n"
        f"席替えがしっかり回るのは「女性10名以上（約5組）×女性比55%以上」のお店。微妙な席で惰性で90分粘るより、40分で見切って近くの好バランス店に回る方が満足度もコスパも段違いです🍸\n\n"
        f"{cta_footer}"
    )
    candidates.append({
        "style": "threads_hopping_guide",
        "style_label": STYLE_LABELS["threads_hopping_guide"],
        "writer_note": "東西主要4エリアの現在トップと「40分見切り・女性10名基準」の実践セオリーを組み合わせた実用投稿。",
        "main_text": body_3[:495],
        "reply_text": "（Threadsは1投稿完結）",
    })

    # 4) threads_classic_top3: Legacy baseline
    classic_th = threads_poster.build_threads_live_post(top_stores, slot_label=slot_label, now_jst=now_jst)
    candidates.append({
        "style": "threads_classic_top3",
        "style_label": STYLE_LABELS["threads_classic_top3"],
        "writer_note": "従来の『🏆全国リアルタイムTOP3 🥇🥈🥉』定型フォーマット（比較ベンチマーク用）。",
        "main_text": classic_th,
        "reply_text": "（Threadsは1投稿完結）",
    })

    return candidates


def _write_instagram_candidates(research: dict, now_jst: datetime.datetime) -> list:
    """
    Generate 4 Instagram Carousel/Feed caption candidates.
    Instagram prioritizes:
      - First 1-2 lines before '...続きを読む' (hook empathy/curiosity without stiff bracket banners)
      - Save-worthiness ('保存しておくと便利')
      - Clean alignment with the 5 portrait slides (`top_volume` & `top_anaba`)
    """
    import ig_carousel_generator
    ig_ctx = research.get("ig_ctx")
    if not ig_ctx:
        ig_ctx = ig_carousel_generator.build_forecast_context(live_stores=research.get("top_stores") or [], now_jst=now_jst)

    v_top = ig_ctx.get("top_volume", [])[:3]
    a_top = ig_ctx.get("top_anaba", [])[:3]
    wk = ig_ctx.get("weekend_label", "")
    theme = ig_ctx.get("theme") or {}
    t_key = theme.get("theme_key", "weekend")
    is_live_fb = bool(ig_ctx.get("is_live_fallback"))
    v_lbl = "現在女性" if is_live_fb else "最高女性"
    a_lbl = "女性比率" if is_live_fb else "平均女性比"

    v_lines = "\n".join(f"・{s['seo_name']}（{v_lbl}{s['max_women']}名）" for s in v_top)
    a_lines = "\n".join(f"・{s['seo_name']}（{a_lbl}{s['avg_wpct']}%）" for s in a_top)
    tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #夜遊びガイド"

    candidates = []

    # 1) ig_empathy_story: Strong 1st-line human hook before fold
    if t_key == "weekday":
        hook_1 = "「平日の仕事帰り、どの店なら待たずに楽しく飲める？」って迷う夜用のまとめです📝"
    elif t_key == "sunday":
        hook_1 = "今週も全国51店舗のデータを追っていて、「やっぱり店選びで9割決まるな…」と実感した結果がこちら📊"
    else:
        hook_1 = "金土の21時半に何も見ないで突撃して「男性待ち50分です」って言われるの、一番もったいないですよね…😂"

    body_ig_1 = (
        f"{hook_1}\n"
        f"実は同じ新宿や梅田・渋谷でも、歩いて3分の別ブランドを見るだけで待ち時間ゼロで座れることが本当に多いんです。\n\n"
        f"お店に向かう前にすぐ見返せるよう、右上の「保存📌」をどうぞ！\n\n"
        f"── スライド2枚目：圧倒的に人が集まる人気店\n"
        f"{v_lines}\n\n"
        f"── スライド3枚目：男性待ちを回避しやすい高女性比率の穴場\n"
        f"{a_lines}\n\n"
        f"💡 男女別の賢い選び方\n"
        f"・男性は「女性10名以上×女性比55%以上」の店舗を選ぶと席替えもスムーズ！\n"
        f"・女性は「男性が先行して盛り上がっている大型店」を選ぶと待ち0分で即乾杯できます🥂\n\n"
        f"いま現在のリアルタイム男女人数と、全51店舗の料金・イベント比較はプロフィールのリンク（aiseki-navi.com）から3秒で見れます🔍\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_empathy_story",
        "style_label": STYLE_LABELS["ig_empathy_story"],
        "writer_note": "インスタの『…続きを読む』で折りたたまれる前の1行目に、一番共感されやすい現場あるあるを配置。",
        "main_text": body_ig_1,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 2) ig_data_discovery: Editorial magazine column style
    top_a_name = a_top[0]["seo_name"] if a_top else "穴場店舗"
    top_a_pct = a_top[0]["avg_wpct"] if a_top else 60
    body_ig_2 = (
        f"9.6万件の来店ログを集計していて分かった、{wk}の「外さないお店選び」を5枚のスライドに凝縮しました📖\n\n"
        f"特に注目なのが、女性比率{top_a_pct}%と非常にバランスが良い「{top_a_name}」をはじめとする穴場店舗の推移です👀\n\n"
        f"✦ 女性来店数が多い盛り上がり上位\n"
        f"{v_lines}\n\n"
        f"✦ 男性がスムーズに入りやすい高女性比率上位\n"
        f"{a_lines}\n\n"
        f"23時20分前後は終電組の退店で一時的に人数が動くため、1軒目は20時台、2軒目は0時すぎの第2波を狙うのがデータ上の鉄板ルートです🌙\n"
        f"あとで友達との作戦会議に使えるよう「保存📌」しておくのがおすすめ！\n\n"
        f"現在の全店リアルタイム人数はプロフィールのリンク（相席ナビ）で無料公開中です🔍\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_data_discovery",
        "style_label": STYLE_LABELS["ig_data_discovery"],
        "writer_note": "9.6万件ログの裏付けと「20時台・0時すぎ第2波」の時間帯法則を盛り込んだマガジン編集長トーン。",
        "main_text": body_ig_2,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 3) ig_concise_save: Clean, scannable save-first layout
    body_ig_3 = (
        f"行くお店で迷ったらこの5枚だけチェックしてみてください🥂（{wk}版）\n"
        f"※入店前にパッと見返せる「保存📌」が便利です\n\n"
        f"【ワイワイ盛り上がり重視ならここ】\n"
        f"{v_lines}\n\n"
        f"【待ち時間回避・ゆっくり話すならここ】\n"
        f"{a_lines}\n\n"
        f"同じエリアでも日によって男女比が15〜20%変わるので、向かう3分前にプロフィールのリンク（aiseki-navi.com）で現在人数を見てから入るのが一番確実です✨\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_concise_save",
        "style_label": STYLE_LABELS["ig_concise_save"],
        "writer_note": "長文を読まない層向けに余白を広く取り、目的別（盛り上がり重視 vs 待ち回避）でスッキリ整理。",
        "main_text": body_ig_3,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 4) ig_classic_template: Legacy baseline
    classic_ig = ig_carousel_generator.build_captions(ig_ctx)["instagram_caption"]
    candidates.append({
        "style": "ig_classic_template",
        "style_label": STYLE_LABELS["ig_classic_template"],
        "writer_note": "従来の『【〇〇 相席ラウンジ狙い目予報📊】』定型キャプション（比較ベンチマーク用）。",
        "main_text": classic_ig,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    return candidates


def run_writer(
    research: dict,
    plan: dict,
    slot_label: str = "22:00",
    channel: str = "x",
    now_jst: datetime.datetime = None,
    app_url: str = "",
) -> list:
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    ch = (channel or "x").lower()
    if ch == "threads":
        return _write_threads_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url)
    if ch == "instagram":
        return _write_instagram_candidates(research, now_jst=now_jst)
    return _write_x_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url)


# =====================================================================
# Role 4: Evaluator / Desk (鬼デスク・評価係)
# =====================================================================
def run_evaluator(
    candidates: list,
    research: dict,
    plan: dict,
    channel: str = "x",
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Score each candidate out of 100 across 4 criteria tailored to `x`, `threads`, or `instagram`:
      1. human_tone (35 pts): Natural human voice vs mechanical medal/bracket templates
      2. freshness_diversity (30 pts): Rotation vs recent history on the same SNS channel
      3. data_specificity (25 pts): Verified numbers, zero redundant store mentions, actionable insight
      4. weight_fit (10 pts): Strict compliance with each platform's character & link-hygiene limits
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    last_style = plan.get("last_style")
    recent_styles = plan.get("recent_styles") or []
    style_priority = plan.get("style_priority") or []
    classic_key = plan.get("classic_key")

    evaluated = []
    for cand in candidates:
        text = cand.get("main_text") or ""
        style = cand.get("style") or ""
        first_line = text.split("\n", 1)[0] if text else ""

        if ch == "x":
            metric_val = x_poster.compute_twitter_weight(text)
            metric_unit = f"{metric_val}/280w"
        else:
            metric_val = len(text)
            metric_unit = f"{metric_val}文字"

        # 1. Human Tone (max 35)
        is_medal_bot = ("🥇" in text and "🥈" in text and "🥉" in text)
        is_bracket_banner = first_line.startswith("【") and "】" in first_line
        if style == classic_key or is_medal_bot:
            human_score = 18
            human_comment = "⚠️ 毎回同じ『🥇🥈🥉 TOP3』や【角括弧見出し】の定型フォーマットで業者・ボット感が出やすいため減点。"
        elif is_bracket_banner:
            human_score = 24
            human_comment = "△ 冒頭が【角括弧の見出し】から始まっており少し宣伝っぽいため、自然な語り出し案を優先。"
        else:
            human_score = 34
            if any(w in text for w in ("割れてる", "立ち上がりがかなり早い", "いつもの", "もったいない", "実は同じ", "温度差")):
                human_score = 35
            human_comment = "◎ 人間がスマホで現場を見ながら話しかけている自然な導入で、タイムラインに馴染みます。"

        # 2. Freshness & Anti-Duplication (max 30)
        if style == last_style:
            fresh_score = 12
            fresh_comment = f"⚠️ 前回の{CHANNEL_LABELS.get(ch, ch)}投稿と同じ型（{STYLE_LABELS.get(style, style)}）のため連続回避で大幅減点。"
        elif style == classic_key and plan.get("suppress_classic"):
            fresh_score = 15
            fresh_comment = "⚠️ 定型ランキングのマンネリ防止ルールにより優先度を引き下げ。"
        elif style in recent_styles[:2]:
            fresh_score = 22
            fresh_comment = "◯ 直近2回以内に近い切り口があるため、別角度の候補を優先。"
        else:
            if style in style_priority:
                p_idx = style_priority.index(style)
                fresh_score = 30 if p_idx == 0 else (26 if p_idx == 1 else 24)
            else:
                fresh_score = 24
            fresh_comment = "◎ 直近の投稿履歴と切り口・1行目のフックが被っておらず新鮮です。"

        # 3. Data Specificity & Precision Check (max 25)
        # Check for awkward internal store prefixes ("OLG ") leaking into conversational drafts
        has_raw_olg = "OLG " in text and style != classic_key
        if has_raw_olg:
            data_score = 18
            data_comment = "⚠️ 内部略称『OLG』がそのまま残っているため減点（『オリエンタル〇〇』表記が自然）。"
        elif style in ("hub_gap", "threads_city_story") and research.get("best_cluster"):
            gap = research["best_cluster"].get("pct_gap", 0)
            data_score = 25 if gap >= 12 else 22
            data_comment = f"◎ 同一エリア内の男女比ギャップ（{gap}pt差）が実測値で示されており、店選びの納得感が非常に高い。"
        elif style in ("usual_anomaly", "threads_anomaly_insight"):
            data_score = 25
            data_comment = "◎ 普段比（平比）データや男女双方の目線（穴場店＋即相席店）が具体的で精度が高い。"
        elif style in ("ig_empathy_story", "ig_data_discovery"):
            data_score = 25
            data_comment = "◎ カルーセル5枚の統計データと完全連動し、保存したくなる具体的な立ち回り根拠あり。"
        else:
            data_score = 23 if style != classic_key else 20
            data_comment = "◎ 主要店舗の実測人数・比率が過不足なく整理されています。"

        # 4. Platform Format Hygiene (max 10; hard penalty if over API limit)
        if ch == "x":
            has_url_in_main = "http://" in text or "https://" in text
            if metric_val > 280 or has_url_in_main:
                weight_score = -40
                weight_comment = f"❌ X制限超過({metric_unit})または1ポスト目URL混入のため不採用。"
            elif 140 <= metric_val <= 274:
                weight_score = 10
                weight_comment = f"◎ X最適ウェイト（{metric_unit}・1ポスト目URLなし・リプ欄誘導）を完璧にクリア。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数ウェイト（{metric_unit}）は許容範囲内。"
        elif ch == "threads":
            tag_cnt = len(re.findall(r"#[^\s#]+", text))
            if metric_val > 500:
                weight_score = -40
                weight_comment = f"❌ Threads上限500文字を超過（{metric_unit}）しているため不採用。"
            elif 260 <= metric_val <= 485 and tag_cnt <= 2:
                weight_score = 10
                weight_comment = f"◎ Threads最適文字数（{metric_unit}/500文字・タグ{tag_cnt}個でスパム判定回避）に合致。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数（{metric_unit}）・タグ数（{tag_cnt}個）は許容範囲内。"
        else:  # instagram
            tag_cnt = len(re.findall(r"#[^\s#]+", text))
            if 350 <= metric_val <= 950 and 4 <= tag_cnt <= 8 and "保存" in text:
                weight_score = 10
                weight_comment = f"◎ IG最適長（{metric_unit}）・保存導線あり・厳選ハッシュタグ{tag_cnt}個でアルゴリズム最適。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数（{metric_unit}）・タグ{tag_cnt}個は標準範囲内。"

        total_score = human_score + fresh_score + data_score + weight_score
        item_copy = dict(cand)
        item_copy.update({
            "channel": ch,
            "total_score": total_score,
            "weight": metric_val,
            "metric_unit": metric_unit,
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
    classic_score = next((c["total_score"] for c in evaluated if c["style"] == classic_key), "-")
    desk_summary = (
        f"【{CHANNEL_LABELS.get(ch, ch)}】全{len(evaluated)}案を審査し、最高スコア {winner['total_score']}点（100点満点）の"
        f"『{winner['style_label']}』を採用しました。"
        f"従来の定型フォーマット（{classic_score}点）を避け、媒体アルゴリズムと人間の読みやすさに最適化しています。"
        if winner else "候補案なし"
    )

    return {
        "channel": ch,
        "winner": winner,
        "candidates": evaluated,
        "desk_summary": desk_summary,
    }


def run_editorial_pipeline(
    stores: list,
    slot_label: str = "22:00",
    channel: str = "x",
    ig_ctx: dict = None,
    now_jst: datetime.datetime = None,
    app_url: str = "",
    record_history: bool = False,
) -> dict:
    """
    Run the full 4-role SNS Editorial Room pipeline for `channel` ('x', 'threads', or 'instagram').
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    if ch not in ("x", "threads", "instagram"):
        ch = "x"

    research = run_researcher(stores, slot_label=slot_label, channel=ch, ig_ctx=ig_ctx, now_jst=now_jst)
    plan = run_planner(research, slot_label=slot_label, channel=ch, now_jst=now_jst)
    candidates = run_writer(research, plan, slot_label=slot_label, channel=ch, now_jst=now_jst, app_url=app_url)
    evaluation = run_evaluator(candidates, research, plan, channel=ch, now_jst=now_jst)

    winner = evaluation.get("winner") or {}
    session_record = {
        "channel": ch,
        "channel_label": CHANNEL_LABELS.get(ch, ch),
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
                "metric_unit": c.get("metric_unit"),
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
            h_key = _history_key_for_channel(ch)
            history = get_recent_editorial_history(channel=ch, limit=11)
            history.insert(0, session_record)
            job_state.set_value(h_key, json.dumps(history[:12], ensure_ascii=False))
        except Exception as e:
            logger.warning(f"Failed to persist {ch} editorial log: {e}")

    return {
        "channel": ch,
        "channel_label": CHANNEL_LABELS.get(ch, ch),
        "research": research,
        "plan": plan,
        "evaluation": evaluation,
        "winner": winner,
        "session_record": session_record,
        "history": get_recent_editorial_history(channel=ch, limit=8),
    }
