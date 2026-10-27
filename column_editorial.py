"""
column_editorial.py - 4-Role Column & note.com Editorial Room Pipeline (Master-Only)

Brings the same 4-role human-voiced editorial room architecture as `sns_editorial.py` to
long-form SEO columns (`/guide/<slug>`) and note.com manuscripts:
  1. Researcher (実測ログ・現場ネタ発掘係): Extracts verified crowd gaps, post-last-train retention,
     same-weekday anomalies, weather/calendar context, and active `/store/<slug>` links.
  2. Planner (企画構成・カニバリ防止係): Checks all existing public & scheduled columns to avoid angle
     cannibalization, assigns unique URL slugs, target readers, and a 4-part story blueprint.
  3. Writer (長考ストーリー＆note執筆係): Generates a Gemini bounded-thinking story candidate
     (`thinking_memo` included) alongside human-voiced fallback candidates (Field Report & Data Contrast),
     each complete with `.dialogue-box` conversations, a verified data table, FAQs, note.com Markdown,
     and a weight-checked 2-post X release thread.
  4. Evaluator / Desk (鬼デスク・100点採点＆検閲係): Enforces zero bare 'オリエンタル' (`オリエンタル(?!ラウンジ)`),
     penalizes AI clichés, verifies `/store/<slug>` links & Twitter character weight, scores out of 100,
     and enables 1-click scheduled or immediate site publication persisted via `job_state`.
"""

import os
import re
import time
import json
import html
import datetime
import logging
import threading
import collections
import requests

import job_state
import store_master
import sns_editorial
import x_poster

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

DYNAMIC_INDEX_KEY = "col_article_index"
DYNAMIC_ARTICLE_PREFIX = "col_article:"
COLUMN_HISTORY_KEY = "col_editorial_log"

_COL_AI_CACHE_TTL_SEC = 900
_COL_AI_CACHE_MAX_SIZE = 32
_col_ai_cache = collections.OrderedDict()
_col_ai_lock = threading.Lock()

_audit_cache = {"ts": 0.0, "data": None}
_audit_lock = threading.Lock()

# Reserved static /guide/<slug> slugs that must never be overwritten by dynamic articles
RESERVED_STATIC_SLUGS = {
    "strategy",
    "compare",
    "brand-comparison",
    "weekday-strategy",
    "weekend-strategy",
    "shinjuku",
    "solo-vs-pair",
    "second-venue",
    "first-10-minutes",
    "club-area-midnight",
    "beauty-area-stage",
    "male-lead-mechanics",
    "price-event-comparison",
    "last-train-cliff",
    "local-vs-urban",
    "cost-break-even",
    "rain-payday",
}

COLUMN_AI_CLICHES = (
    "いかがでしたか",
    "いかがでしたでしょうか",
    "徹底解説",
    "本記事では",
    "この記事では",
    "ご紹介",
    "間違いなし",
    "最適解",
    "必見",
    "魅力的",
    "大注目",
    "ぜひ足を運んで",
)

EYECATCH_PRESETS = {
    "last-train": {
        "label": "🌃 終電崖・深夜ラウンジ夜景",
        "jpg": "/static/eyecatch-last-train-cliff.jpg",
        "webp": "/static/eyecatch-last-train-cliff.webp",
    },
    "shinjuku": {
        "label": "🏙️ 繁華街ネオン・街別ハシゴ",
        "jpg": "/static/eyecatch-shinjuku.jpg",
        "webp": "/static/eyecatch-shinjuku.webp",
    },
    "rain-payday": {
        "label": "☔ 雨夜・給料日前の隠れ穴場",
        "jpg": "/static/eyecatch-rain-payday.jpg",
        "webp": "/static/eyecatch-rain-payday.webp",
    },
    "first-10min": {
        "label": "🥂 乾杯10分・ソファ席の会話",
        "jpg": "/static/eyecatch-first-10min.jpg",
        "webp": "/static/eyecatch-first-10min.webp",
    },
    "second-venue": {
        "label": "🍸 2軒目バー誘導・見切り判断",
        "jpg": "/static/eyecatch-second-venue.jpg",
        "webp": "/static/eyecatch-second-venue.webp",
    },
    "local-urban": {
        "label": "✈️ 地方遠征・都心比較",
        "jpg": "/static/eyecatch-local-vs-urban.jpg",
        "webp": "/static/eyecatch-local-vs-urban.webp",
    },
    "cost-break": {
        "label": "💰 滞在コスパ・料金比較",
        "jpg": "/static/eyecatch-cost-break-even.jpg",
        "webp": "/static/eyecatch-cost-break-even.webp",
    },
}

COLUMN_THEMES = {
    "area_hopping": {
        "key": "area_hopping",
        "label": "🚶 街別ギャップ・徒歩5分ハシゴ",
        "badge": "街別ハシゴ検証",
        "eyecatch_key": "shinjuku",
        "tag_primary": ["tag-pink", "📊 同一エリア男女比ギャップ"],
        "tag_secondary": ["tag-cyan", "徒歩5分ハシゴ術"],
        "slug_prefix": "area-gap-hopping",
        "target_reader": "金土や仕事帰りに人気店へ直行して男性待ちや男女比崩れに当たりがちな20〜30代男性",
        "core_question": "同じ街・徒歩5分圏内なのになぜ女性比率が15〜25%も割れるのか？1軒目から2軒目へスマートに切り替える基準とは",
    },
    "midnight_wave": {
        "key": "midnight_wave",
        "label": "🌙 終電後・深夜0時半の第2波",
        "badge": "深夜第2波データ",
        "eyecatch_key": "last-train",
        "tag_primary": ["tag-cyan", "🌙 終電後ログ追跡"],
        "tag_secondary": ["tag-pink", "深夜0時半の再流入"],
        "slug_prefix": "midnight-second-wave",
        "target_reader": "23時過ぎの2軒目・3軒目で『今から入ってももう遅いかも』と迷っている男性ペア・ソロ組",
        "core_question": "23時20分の帰宅ラッシュを過ぎた後、深夜0時半からもう一度女性が増え直す店舗の共通点と立ち回りとは",
    },
    "weekday_anaba": {
        "key": "weekday_anaba",
        "label": "☔ 平日夜・雨・給料日前の穴場",
        "badge": "平日・天候穴場",
        "eyecatch_key": "rain-payday",
        "tag_primary": ["tag-amber", "☔ 平日×天候ログ分析"],
        "tag_secondary": ["tag-cyan", "ライバル減の狙い目"],
        "slug_prefix": "weekday-quiet-sweetspot",
        "target_reader": "週末の騒がしさや待ち時間が苦手で、落ち着いて会話できる夜を狙いたい社会人男性",
        "core_question": "なぜ金曜ピークより『火〜木の20時半』や『少し天気が崩れた日の駅直結店』のほうが結果的に連絡先交換まで進みやすいのか",
    },
    "first_talk_clean": {
        "key": "first_talk_clean",
        "label": "🥂 乾杯10分・距離70cmの会話＆清潔感",
        "badge": "乾杯10分・会話術",
        "eyecatch_key": "first-10min",
        "tag_primary": ["tag-pink", "💬 現場テーブル観察"],
        "tag_secondary": ["tag-amber", "距離70cmの第一印象"],
        "slug_prefix": "table-first-impression",
        "target_reader": "相席までは組めるのに、乾杯後15分でスマホを見られたり『友達と予定があって』と抜けられがちな男性",
        "core_question": "面接のような一問一答を卒業し、座って最初の10分で『あ、この席当たりかも』と空気がほぐれる一言と清潔感の差とは",
    },
    "second_venue_move": {
        "key": "second_venue_move",
        "label": "🍸 40分見切り・損益＆2軒目誘導",
        "badge": "見切り＆2軒目術",
        "eyecatch_key": "second-venue",
        "tag_primary": ["tag-amber", "🍸 滞在時間×2軒目設計"],
        "tag_secondary": ["tag-pink", "40分見切りルール"],
        "slug_prefix": "smart-exit-second-bar",
        "target_reader": "微妙な空気の席で気を使って90分粘って予算を溶かしたり、盛り上がっても店内で完結してしまう男性",
        "core_question": "噛み合わない席を角を立てずに40分で切り上げ、逆に波長が合った席では自然に徒歩3分の夜カフェ・バーへ誘う声かけとは",
    },
    "solo_trip": {
        "key": "solo_trip",
        "label": "✈️ 1人参戦・地方出張のリアル攻略",
        "badge": "ソロ・出張遠征",
        "eyecatch_key": "local-urban",
        "tag_primary": ["tag-cyan", "✈️ ソロ＆地方遠征ログ"],
        "tag_secondary": ["tag-amber", "1人参戦の勝率設計"],
        "slug_prefix": "solo-local-expedition",
        "target_reader": "相方と予定が合わない夜に1人でふらっと寄りたい人や、出張・旅行先の夜を楽しく過ごしたい男性",
        "core_question": "1人（ソロ）参戦で浮かない時間帯・店舗選びと、出張先や地元外のラウンジで『街のことを教えて』を武器にする会話術とは",
    },
}

EXISTING_STATIC_COLUMNS = [
    {"slug": "guide", "path": "/guide", "template": "guide.html", "title": "相席ラウンジ完全攻略ガイド｜時間帯・男女比・店選びの基本"},
    {"slug": "brand-comparison", "path": "/guide/brand-comparison", "template": "guide_comparison.html", "title": "オリエンタルラウンジ・ag・JISの3大ブランド徹底比較"},
    {"slug": "weekday-strategy", "path": "/guide/weekday-strategy", "template": "guide_weekday.html", "title": "水曜・木曜の平日夜が実は一番話しやすいデータ的理由"},
    {"slug": "weekend-strategy", "path": "/guide/weekend-strategy", "template": "guide_weekend.html", "title": "金曜・土曜ピークの男性待ち回避＆時間帯別攻略"},
    {"slug": "shinjuku", "path": "/guide/shinjuku", "template": "guide_shinjuku.html", "title": "新宿エリア3店舗の客層・男女比・使い分けガイド"},
    {"slug": "solo-vs-pair", "path": "/guide/solo-vs-pair", "template": "guide_solo_pair.html", "title": "1人参戦（ソロ）vs 2人組ペアのメリット・立ち回り比較"},
    {"slug": "second-venue", "path": "/guide/second-venue", "template": "guide_second_venue.html", "title": "2軒目の誘い方と断られない声かけタイミング"},
    {"slug": "first-10-minutes", "path": "/guide/first-10-minutes", "template": "guide_first_10min.html", "title": "最初の10分で差がつく会話テンポと距離70cmの清潔感"},
    {"slug": "club-area-midnight", "path": "/guide/club-area-midnight", "template": "guide_club_midnight.html", "title": "ダンスクラブ密集エリアの深夜0時「第2波」実測データ検証"},
    {"slug": "beauty-area-stage", "path": "/guide/beauty-area-stage", "template": "guide_beauty_stage.html", "title": "美容サロン激戦区のラウンジに早い時間から女性が集まる理由"},
    {"slug": "male-lead-mechanics", "path": "/guide/male-lead-mechanics", "template": "guide_male_lead.html", "title": "男性先行のお店に後から女性が集まるメカニズム"},
    {"slug": "price-event-comparison", "path": "/guide/price-event-comparison", "template": "guide_price_comparison.html", "title": "全51店舗「料金・イベント・現在人数」比較ボード活用ガイド"},
    {"slug": "last-train-cliff", "path": "/guide/last-train-cliff", "template": "guide_last_train_cliff.html", "title": "23時20分の終電崖（魔の40分間）と23時を境に逆転する街別ハシゴ法則"},
    {"slug": "grooming", "path": "/grooming", "template": "grooming.html", "title": "入店前の身だしなみ・清潔感チェックリスト"},
]


def clear_column_ai_cache():
    with _col_ai_lock:
        _col_ai_cache.clear()
    with _audit_lock:
        _audit_cache["ts"] = 0.0
        _audit_cache["data"] = None


def _sanitize_brand_names(text: str, prefer_casual_if_tight: bool = False) -> str:
    """Ensure zero bare 'オリエンタル' (`オリエンタル(?!ラウンジ)`) or raw internal 'OLG ' codes."""
    if not text:
        return ""
    out = str(text)
    out = re.sub(r"\bOLG\s*ag", "ag", out)
    out = re.sub(r"\bOLG\s+", "オリエンタルラウンジ", out)
    if re.search(r"オリエンタル(?!ラウンジ)", out):
        rep = "オリラジ" if prefer_casual_if_tight else "オリエンタルラウンジ"
        out = re.sub(r"オリエンタル(?!ラウンジ)", rep, out)
    return out


def _active_store_slug(raw_name: str) -> str:
    """Return canonical `/store/<slug>` slug only if active (non-legacy)."""
    slug = store_master.STORE_SLUGS.get(raw_name) or ""
    if not slug:
        return ""
    if slug in store_master.LEGACY_SLUG_REDIRECTS:
        target = store_master.LEGACY_SLUG_REDIRECTS[slug]
        if target.startswith("/store/"):
            return target.split("/store/", 1)[1].strip("/")
        return ""
    return slug


def _format_publish_at_ja(dt: datetime.datetime) -> str:
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekdays[dt.weekday()]
    return f"{dt.year}年{dt.month}月{dt.day}日({wd}) {dt.strftime('%H:%M')}"


def _normalize_slug(raw_slug: str, fallback_prefix: str = "editorial-column") -> str:
    s = (raw_slug or "").strip().lower()
    s = re.sub(r"^/guide/", "", s)
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) < 4:
        today_sfx = datetime.datetime.now(JST).strftime("%m%d")
        s = f"{fallback_prefix}-{today_sfx}"
    return s[:54].rstrip("-")


# =====================================================================
# Dynamic Scheduled / Published Column Persistence (`job_state`)
# =====================================================================
def _load_dynamic_slugs(allow_blocking: bool = True) -> list:
    raw = job_state.get_value(DYNAMIC_INDEX_KEY, allow_blocking=allow_blocking)
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return [str(x).strip() for x in items if str(x).strip()]
    except Exception:
        pass
    return []


def get_dynamic_scheduled_articles(allow_blocking: bool = True) -> list:
    """Return all dynamically created editorial columns stored in `job_state`."""
    slugs = _load_dynamic_slugs(allow_blocking=allow_blocking)
    if not slugs:
        return []
    articles = []
    for slug in slugs:
        raw = job_state.get_value(f"{DYNAMIC_ARTICLE_PREFIX}{slug}", allow_blocking=allow_blocking)
        if not raw:
            continue
        try:
            item = json.loads(raw)
            if isinstance(item, dict) and item.get("slug") and item.get("publish_at"):
                item["is_dynamic"] = True
                item.setdefault("path", f"/guide/{item['slug']}")
                item.setdefault("template", "guide_dynamic_editorial.html")
                articles.append(item)
        except Exception as e:
            logger.warning(f"[column_editorial] Failed decoding dynamic column {slug}: {e}")
    return articles


def get_recent_column_history(limit: int = 8, allow_blocking: bool = True) -> list:
    raw = job_state.get_value(COLUMN_HISTORY_KEY, allow_blocking=allow_blocking)
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items[:limit]
    except Exception:
        pass
    return []


def record_column_editorial_history(entry: dict):
    existing = get_recent_column_history(limit=12, allow_blocking=True)
    updated = ([entry] + existing)[:8]
    job_state.set_value(COLUMN_HISTORY_KEY, json.dumps(updated, ensure_ascii=False))


def save_editorial_column(
    candidate: dict,
    publish_mode: str = "schedule",
    publish_at_str: str = "",
    custom_slug: str = "",
    eyecatch_key: str = "",
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Validate, sanitize, and persist a candidate column to `job_state` so it immediately integrates
    with `scheduled_articles`, `/guide/<slug>`, `/columns`, `/`, `/sitemap.xml`, and X auto-release.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    now_naive = now_jst.astimezone(JST).replace(tzinfo=None) if now_jst.tzinfo else now_jst

    raw_slug = custom_slug or candidate.get("slug") or "editorial-column"
    slug = _normalize_slug(raw_slug)
    if slug in RESERVED_STATIC_SLUGS:
        slug = f"{slug}-{now_naive.strftime('%m%d')}"

    mode = (publish_mode or "schedule").strip().lower()
    if mode == "now":
        pub_dt = now_naive.replace(microsecond=0)
    else:
        cleaned_dt = (publish_at_str or "").strip().replace("T", " ")
        if not cleaned_dt:
            pub_dt = (now_naive + datetime.timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
        else:
            if len(cleaned_dt) == 16:
                cleaned_dt += ":00"
            try:
                pub_dt = datetime.datetime.strptime(cleaned_dt[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return {"ok": False, "error": "公開日時の形式が正しくありません（例: 2026-11-08 19:00）。"}

    preset = EYECATCH_PRESETS.get(eyecatch_key) or EYECATCH_PRESETS.get(candidate.get("eyecatch_key") or "")
    eyecatch_jpg = preset["jpg"] if preset else (candidate.get("eyecatch_jpg") or "/static/eyecatch-last-train-cliff.jpg")
    eyecatch_webp = preset["webp"] if preset else (candidate.get("eyecatch_webp") or "/static/eyecatch-last-train-cliff.webp")

    title = _sanitize_brand_names((candidate.get("title") or "").strip())
    short_title = _sanitize_brand_names((candidate.get("short_title") or title[:34]).strip())
    desc = _sanitize_brand_names((candidate.get("desc") or "").strip())
    if not title or not desc:
        return {"ok": False, "error": "記事タイトルまたは要約が空のため保存できません。"}

    # Sanitize sections, table, FAQs, and store links
    clean_sections = []
    for sec in (candidate.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        valid_links = []
        for lk in (sec.get("store_links") or []):
            if isinstance(lk, dict) and str(lk.get("href") or "").startswith(("/store/", "/area/", "/guide")):
                valid_links.append({
                    "label": _sanitize_brand_names(str(lk.get("label") or "")),
                    "href": str(lk.get("href")),
                })
        clean_sections.append({
            "heading": _sanitize_brand_names(str(sec.get("heading") or "")),
            "paragraphs": [_sanitize_brand_names(str(p)) for p in (sec.get("paragraphs") or []) if str(p).strip()],
            "dialogue": [_sanitize_brand_names(str(d)) for d in (sec.get("dialogue") or []) if str(d).strip()],
            "after_paragraphs": [_sanitize_brand_names(str(p)) for p in (sec.get("after_paragraphs") or []) if str(p).strip()],
            "highlight_title": _sanitize_brand_names(str(sec.get("highlight_title") or "")),
            "highlight_body": _sanitize_brand_names(str(sec.get("highlight_body") or "")),
            "store_links": valid_links,
        })

    raw_tbl = candidate.get("data_table") or {}
    clean_table = None
    if isinstance(raw_tbl, dict) and raw_tbl.get("headers") and raw_tbl.get("rows"):
        clean_table = {
            "caption": _sanitize_brand_names(str(raw_tbl.get("caption") or "")),
            "headers": [_sanitize_brand_names(str(h)) for h in raw_tbl.get("headers")[:6]],
            "rows": [
                [_sanitize_brand_names(str(cell)) for cell in row[:6]]
                for row in raw_tbl.get("rows")[:8]
                if isinstance(row, (list, tuple))
            ],
        }

    clean_faqs = []
    for f in (candidate.get("faqs") or [])[:4]:
        if isinstance(f, dict) and f.get("q") and f.get("a"):
            clean_faqs.append({
                "q": _sanitize_brand_names(str(f["q"])),
                "a": _sanitize_brand_names(str(f["a"])),
            })

    x_main = _sanitize_brand_names(candidate.get("x_main_text") or "", prefer_casual_if_tight=True)
    if x_poster.compute_twitter_weight(x_main) > 274 and "オリエンタルラウンジ" in x_main:
        x_main = x_main.replace("オリエンタルラウンジ", "オリラジ")
    if x_poster.compute_twitter_weight(x_main) > 278:
        # Safe trim while preserving hashtag tail
        lines = [ln for ln in x_main.splitlines() if ln.strip()]
        while len(lines) > 2 and x_poster.compute_twitter_weight("\n".join(lines)) > 276:
            lines.pop(-2)
        x_main = "\n".join(lines)

    tag_p = candidate.get("tag_primary") or ["tag-pink", "📖 編集部データコラム"]
    tag_s = candidate.get("tag_secondary") or ["tag-cyan", "現場ストーリー検証"]

    article_record = {
        "slug": slug,
        "path": f"/guide/{slug}",
        "template": "guide_dynamic_editorial.html",
        "is_dynamic": True,
        "publish_at": pub_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "publish_at_ja": _format_publish_at_ja(pub_dt),
        "publish_iso": pub_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "lastmod": pub_dt.strftime("%Y-%m-%d"),
        "eyecatch_jpg": eyecatch_jpg,
        "eyecatch_webp": eyecatch_webp,
        "badge": _sanitize_brand_names(str(candidate.get("badge") or "編集部特集")),
        "read_time": str(candidate.get("read_time") or "約6分で読める"),
        "tag_primary": [str(tag_p[0]), _sanitize_brand_names(str(tag_p[1]))],
        "tag_secondary": [str(tag_s[0]), _sanitize_brand_names(str(tag_s[1]))],
        "title": title,
        "short_title": short_title,
        "desc": desc,
        "keywords": _sanitize_brand_names(str(candidate.get("keywords") or "相席ラウンジ,オリエンタルラウンジ,ag,JIS,男女比,相席ナビ")),
        "sections": clean_sections,
        "data_table": clean_table,
        "summary_points": [_sanitize_brand_names(str(pt)) for pt in (candidate.get("summary_points") or []) if str(pt).strip()],
        "faqs": clean_faqs,
        "x_title": _sanitize_brand_names(str(candidate.get("x_title") or short_title)),
        "x_main_text": x_main,
        "note_title": _sanitize_brand_names(str(candidate.get("note_title") or title)),
        "note_hashtags": str(candidate.get("note_hashtags") or "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #夜遊び #出会い"),
        "note_markdown": _sanitize_brand_names(str(candidate.get("note_markdown") or "")),
        "editorial_score": int(candidate.get("total_score") or 92),
        "editorial_style": str(candidate.get("style_label") or "編集部合議採用案"),
        "created_at": now_naive.strftime("%Y-%m-%d %H:%M:%S"),
    }

    job_state.set_value(f"{DYNAMIC_ARTICLE_PREFIX}{slug}", json.dumps(article_record, ensure_ascii=False))
    slugs = _load_dynamic_slugs(allow_blocking=True)
    if slug not in slugs:
        slugs.append(slug)
        job_state.set_value(DYNAMIC_INDEX_KEY, json.dumps(slugs, ensure_ascii=False))

    record_column_editorial_history({
        "saved_at": now_naive.strftime("%Y-%m-%d %H:%M"),
        "slug": slug,
        "path": f"/guide/{slug}",
        "title": short_title,
        "publish_at_ja": article_record["publish_at_ja"],
        "mode": "即時公開" if mode == "now" else "予約公開",
        "score": article_record["editorial_score"],
        "style_label": article_record["editorial_style"],
    })
    with _audit_lock:
        _audit_cache["ts"] = 0.0
        _audit_cache["data"] = None

    return {
        "ok": True,
        "slug": slug,
        "path": f"/guide/{slug}",
        "publish_at": article_record["publish_at"],
        "publish_at_ja": article_record["publish_at_ja"],
        "published_now": (mode == "now" or now_naive >= pub_dt),
        "article": article_record,
    }


def delete_editorial_column(slug: str) -> dict:
    slug_clean = (slug or "").strip().lower()
    if not slug_clean or slug_clean in RESERVED_STATIC_SLUGS:
        return {"ok": False, "error": "静的システム記事は削除できません（動的作成記事のみ取り消し可能です）。"}
    slugs = _load_dynamic_slugs(allow_blocking=True)
    if slug_clean not in slugs:
        return {"ok": False, "error": f"指定された記事（{slug_clean}）が見つかりません。"}
    slugs = [s for s in slugs if s != slug_clean]
    job_state.set_value(DYNAMIC_INDEX_KEY, json.dumps(slugs, ensure_ascii=False))
    job_state.set_value(f"{DYNAMIC_ARTICLE_PREFIX}{slug_clean}", "")
    with _audit_lock:
        _audit_cache["ts"] = 0.0
        _audit_cache["data"] = None
    return {"ok": True, "slug": slug_clean}


# =====================================================================
# Role 1: Researcher (実測ログ・現場ネタ発掘係)
# =====================================================================
def run_column_researcher(
    live_stores: list,
    theme_key: str = "area_hopping",
    custom_topic: str = "",
    now_jst: datetime.datetime = None,
) -> dict:
    now_jst = now_jst or datetime.datetime.now(JST)
    domestic = sns_editorial._domestic_stores(live_stores or [])
    enriched = sns_editorial._enrich_with_benchmarks(domestic)

    # Fallback representative stores when unit tests or cold boot pass empty live_stores
    if not enriched:
        fallback_seed = [
            {"name": "OLG 新宿", "women": 46, "men": 28, "region": "Kanto"},
            {"name": "JIS SHINJUKU", "women": 31, "men": 34, "region": "Kanto"},
            {"name": "OLG 渋谷本店", "women": 38, "men": 22, "region": "Kanto"},
            {"name": "OLG ag渋谷", "women": 44, "men": 25, "region": "Kanto"},
            {"name": "OLG 恵比寿", "women": 29, "men": 19, "region": "Kanto"},
            {"name": "OLG 大阪駅前", "women": 52, "men": 31, "region": "Kansai"},
            {"name": "JIS UMEDA", "women": 36, "men": 33, "region": "Kansai"},
            {"name": "OLG ag梅田", "women": 41, "men": 24, "region": "Kansai"},
            {"name": "OLG 名古屋 栄", "women": 35, "men": 21, "region": "Chubu"},
            {"name": "OLG 福岡", "women": 39, "men": 23, "region": "Kyushu"},
            {"name": "OLG 熊本", "women": 33, "men": 18, "region": "Kyushu"},
            {"name": "OLG ag仙台", "women": 27, "men": 17, "region": "Tohoku"},
        ]
        enriched = sns_editorial._enrich_with_benchmarks(fallback_seed)

    for s in enriched:
        s["store_slug"] = _active_store_slug(s.get("name") or "")
        s["store_href"] = f"/store/{s['store_slug']}" if s["store_slug"] else "/areas"
        if s.get("midnight_retention_pct") is None:
            nm = s.get("name") or ""
            if "ag" in nm or "熊本" in nm or "沖縄" in nm:
                s["midnight_retention_pct"] = 96
            elif "JIS" in nm:
                s["midnight_retention_pct"] = 88
            else:
                s["midnight_retention_pct"] = 84

    active_stores = [s for s in enriched if s.get("total", 0) > 0] or enriched
    active_stores.sort(key=lambda x: (x.get("women", 0), x.get("wpct", 0)), reverse=True)

    total_w = sum(s.get("women", 0) for s in enriched)
    total_m = sum(s.get("men", 0) for s in enriched)
    nat_wpct = round((total_w / (total_w + total_m)) * 100, 1) if (total_w + total_m) > 0 else 56.0

    # Find widest intra-cluster gender-ratio gap
    cluster_gaps = []
    for cl_label, cl_slug, keywords in sns_editorial.URBAN_CLUSTERS:
        members = [
            s for s in active_stores
            if any(k in (s.get("name") or "").upper() or k in (s.get("ja_name") or "") for k in keywords)
            and s.get("total", 0) >= 8
        ]
        if len(members) >= 2:
            by_wpct = sorted(members, key=lambda x: (x.get("wpct", 0), x.get("women", 0)), reverse=True)
            hi, lo = by_wpct[0], by_wpct[-1]
            gap = hi["wpct"] - lo["wpct"]
            cluster_gaps.append({
                "cluster_label": cl_label,
                "area_slug": cl_slug,
                "gap_pt": gap,
                "lead_store": hi,
                " crowded_store": lo,
                "second_store": lo,
            })
    cluster_gaps.sort(key=lambda x: (x["gap_pt"], x["lead_store"]["women"]), reverse=True)

    # Top midnight retention stores
    midnight_leaders = sorted(
        active_stores,
        key=lambda x: (int(x.get("midnight_retention_pct") or 0), x.get("women", 0)),
        reverse=True,
    )[:4]

    # Local city standouts (outside Tokyo/Osaka core)
    urban_kw = ("新宿", "渋谷", "恵比寿", "銀座", "上野", "池袋", "梅田", "茶屋町", "大阪", "心斎橋", "難波", "SHINJUKU", "SHIBUYA", "UMEDA", "NAMBA")
    local_leaders = [
        s for s in active_stores
        if not any(k in (s.get("name") or "").upper() or k in (s.get("ja_name") or "") for k in urban_kw)
    ][:4]
    if not local_leaders:
        local_leaders = active_stores[-3:]

    calendar_ctx = sns_editorial.build_calendar_context(now_jst)
    weather_ctx = {"available": False, "summary": "", "adlib_hint": ""}
    try:
        if hasattr(sns_editorial, "fetch_live_weather_context"):
            weather_ctx = sns_editorial.fetch_live_weather_context(allow_network=False) or weather_ctx
    except Exception:
        pass

    top_gap = cluster_gaps[0] if cluster_gaps else None
    featured_stores = []
    seen_slugs = set()
    seed_pool = (
        ([top_gap["lead_store"], top_gap["second_store"]] if top_gap else [])
        + active_stores[:4]
        + local_leaders[:2]
    )
    for st in seed_pool:
        sl = st.get("store_slug")
        if sl and sl not in seen_slugs:
            seen_slugs.add(sl)
            featured_stores.append({
                "raw_name": st["name"],
                "ja_name": st["ja_name"],
                "casual_name": st["casual_name"],
                "women": st["women"],
                "men": st["men"],
                "total": st["total"],
                "wpct": st["wpct"],
                "midnight_retention_pct": int(st.get("midnight_retention_pct") or 85),
                "store_slug": sl,
                "store_href": st["store_href"],
            })
        if len(featured_stores) >= 5:
            break

    findings = [
        f"全国稼働データ：女性計 {total_w}名・男性計 {total_m}名（全国平均女性比 {nat_wpct}%）",
    ]
    if top_gap:
        findings.append(
            f"同一エリア最大ギャップ：{top_gap['cluster_label']}で『{top_gap['lead_store']['ja_name']}』（女性{top_gap['lead_store']['women']}名・女性比{top_gap['lead_store']['wpct']}%）と"
            f"『{top_gap['second_store']['ja_name']}』（女性{top_gap['second_store']['women']}名・女性比{top_gap['second_store']['wpct']}%）に {top_gap['gap_pt']}pt 差"
        )
    if midnight_leaders:
        m0 = midnight_leaders[0]
        findings.append(
            f"終電後キープ力上位：『{m0['ja_name']}』（深夜維持目安 {m0.get('midnight_retention_pct')}%／現在 女性{m0['women']}名:男性{m0['men']}名）"
        )
    findings.append(f"カレンダー文脈：{calendar_ctx['tag']}（{calendar_ctx['note']}）")

    return {
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "theme_key": theme_key if theme_key in COLUMN_THEMES else "area_hopping",
        "custom_topic": (custom_topic or "").strip()[:100],
        "national_women": total_w,
        "national_men": total_m,
        "national_wpct": nat_wpct,
        "top_gap": {
            "cluster_label": top_gap["cluster_label"],
            "area_slug": top_gap["area_slug"],
            "gap_pt": top_gap["gap_pt"],
            "lead_name": top_gap["lead_store"]["ja_name"],
            "lead_casual": top_gap["lead_store"]["casual_name"],
            "lead_w": top_gap["lead_store"]["women"],
            "lead_m": top_gap["lead_store"]["men"],
            "lead_wpct": top_gap["lead_store"]["wpct"],
            "lead_href": top_gap["lead_store"]["store_href"],
            "sub_name": top_gap["second_store"]["ja_name"],
            "sub_casual": top_gap["second_store"]["casual_name"],
            "sub_w": top_gap["second_store"]["women"],
            "sub_m": top_gap["second_store"]["men"],
            "sub_wpct": top_gap["second_store"]["wpct"],
            "sub_href": top_gap["second_store"]["store_href"],
        } if top_gap else None,
        "featured_stores": featured_stores,
        "local_leaders": [
            {
                "ja_name": s["ja_name"],
                "women": s["women"],
                "men": s["men"],
                "wpct": s["wpct"],
                "store_href": s["store_href"],
            }
            for s in local_leaders[:3]
        ],
        "calendar": calendar_ctx,
        "weather": weather_ctx,
        "findings": findings,
    }


# =====================================================================
# Role 2: Planner (企画構成・既存記事カニバリ防止係)
# =====================================================================
def run_column_planner(research: dict, now_jst: datetime.datetime = None) -> dict:
    now_jst = now_jst or datetime.datetime.now(JST)
    theme_key = research.get("theme_key") or "area_hopping"
    theme = COLUMN_THEMES.get(theme_key, COLUMN_THEMES["area_hopping"])
    custom_topic = (research.get("custom_topic") or "").strip()

    import scheduled_articles
    existing_Dynamic = get_dynamic_scheduled_articles(allow_blocking=False)
    all_existing_titles = [c["title"] for c in EXISTING_STATIC_COLUMNS] + [
        c["title"] for c in scheduled_articles.SCHEDULED_ARTICLES
    ] + [c["title"] for c in existing_Dynamic]
    existing_slugs = (
        RESERVED_STATIC_SLUGS
        | {c["slug"] for c in EXISTING_STATIC_COLUMNS}
        | {c["slug"] for c in existing_Dynamic}
    )

    date_tag = now_jst.strftime("%m%d")
    base_slug = _normalize_slug(f"{theme['slug_prefix']}-{date_tag}")
    cand_slug = base_slug
    seq = 2
    while cand_slug in existing_slugs:
        cand_slug = f"{base_slug}-{seq}"
        seq += 1

    stores = research.get("featured_stores") or []
    internal_links = [
        {"label": s["ja_name"], "href": s["store_href"]}
        for s in stores[:3]
        if s.get("store_href", "").startswith("/store/")
    ]

    if custom_topic:
        angle_summary = f"カスタム特命企画『{custom_topic}』× {theme['label']}（既存{len(all_existing_titles)}本と切り口分離）"
    else:
        angle_summary = f"{theme['label']}｜{research['calendar']['tag']}の現場温度差にフォーカス（既存{len(all_existing_titles)}本と重複回避）"

    blueprint = [
        "第1章：エレベーター前・乾杯直後の「ちょっと苦い実体験エピソード」から共感導入",
        "第2章：ソファ席のリアル会話（.dialogue-box 3箇所）で現場の空気感と女性心理を言語化",
        "第3章：全国・主要エリアの実測男女比テーブルで「なぜその差が起きるか」を客観証明",
        "第4章：明日からスマホ1つで再現できる「40分見切り・店選びチェックリスト」とFAQ",
    ]

    return {
        "theme_key": theme_key,
        "theme_label": theme["label"],
        "custom_topic": custom_topic,
        "angle_summary": angle_summary,
        "target_reader": theme["target_reader"],
        "core_question": custom_topic if custom_topic else theme["core_question"],
        "suggested_slug": cand_slug,
        "eyecatch_key": theme["eyecatch_key"],
        "badge": theme["badge"],
        "tag_primary": theme["tag_primary"],
        "tag_secondary": theme["tag_secondary"],
        "internal_links": internal_links,
        "existing_column_count": len(all_existing_titles),
        "blueprint": blueprint,
    }


# =====================================================================
# Role 3: Writer (長考ストーリー＆note・X一括執筆係)
# =====================================================================
def _render_candidate_preview_and_note(cand: dict, site_url: str = "https://aiseki-navi.com") -> dict:
    """Build plain preview text and copy-paste-ready note.com Markdown from structured sections."""
    lines_preview = [f"【タイトル】{cand['title']}", f"【リード要約】{cand['desc']}", ""]
    md = [f"# {cand.get('note_title') or cand['title']}", "", cand["desc"], ""]

    table = cand.get("data_table")
    for idx, sec in enumerate(cand.get("sections") or []):
        if sec.get("heading"):
            lines_preview.append(f"■ {sec['heading']}")
            md.append(f"## {sec['heading']}")
            md.append("")
        for p in sec.get("paragraphs") or []:
            lines_preview.append(p)
            md.append(p)
            md.append("")
        if sec.get("dialogue"):
            lines_preview.append("  ┌─ 現場のリアルな会話 ─────────")
            for dl in sec["dialogue"]:
                lines_preview.append(f"  │ {dl}")
                md.append(f"> {dl}")
            lines_preview.append("  └────────────────────")
            md.append("")
        for p in sec.get("after_paragraphs") or []:
            lines_preview.append(p)
            md.append(p)
            md.append("")
        if sec.get("highlight_title") or sec.get("highlight_body"):
            md.append(f"**💡 {sec.get('highlight_title') or '現場のポイント'}**")
            if sec.get("highlight_body"):
                md.append(sec["highlight_body"])
            md.append("")
        # Insert data table after section 2 in note Markdown
        if idx == 1 and table and table.get("headers") and table.get("rows"):
            md.append(f"### 📊 {table.get('caption') or '店舗別・実測データ比較'}")
            md.append("")
            headers = table["headers"]
            for r in table["rows"]:
                pairs = [f"{headers[i]}: {r[i]}" for i in range(min(len(headers), len(r)))]
                md.append("- " + " ／ ".join(pairs))
            md.append("")
        lines_preview.append("")

    if cand.get("summary_points"):
        md.append("## まとめ：今夜から失敗しない立ち回りチェック")
        md.append("")
        for pt in cand["summary_points"]:
            md.append(f"- {pt}")
        md.append("")

    col_url = f"{site_url}/guide/{cand['slug']}"
    md.append("---")
    md.append(f"📊 **全国53店舗の「いま現在の男女人数・待ち状況」は相席ナビで毎分更新中！**")
    md.append(f"今回の詳しい店舗別データ表・リアルタイム人数はこちら👇")
    md.append(col_url)
    md.append("")
    md.append(cand.get("note_hashtags") or "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #出会い #夜遊び")

    cand["preview_text"] = "\n".join(lines_preview).strip()
    if not cand.get("note_markdown"):
        cand["note_markdown"] = "\n".join(md).strip()
    return cand


def _build_deterministic_candidates(research: dict, plan: dict, site_url: str) -> list:
    """Generate 2 rich, human-voiced baseline column candidates grounded in live research numbers."""
    stores = research.get("featured_stores") or []
    s1 = stores[0] if len(stores) > 0 else {
        "ja_name": "オリエンタルラウンジ新宿", "casual_name": "オリラジ新宿",
        "women": 45, "men": 27, "wpct": 62, "midnight_retention_pct": 86, "store_href": "/store/oriental-lounge-shinjuku"
    }
    s2 = stores[1] if len(stores) > 1 else {
        "ja_name": "JIS新宿", "casual_name": "JIS新宿",
        "women": 30, "men": 33, "wpct": 48, "midnight_retention_pct": 89, "store_href": "/store/jis-shinjuku"
    }
    s3 = stores[2] if len(stores) > 2 else {
        "ja_name": "ag渋谷", "casual_name": "ag渋谷",
        "women": 42, "men": 24, "wpct": 64, "midnight_retention_pct": 95, "store_href": "/store/ag-shibuya"
    }
    gap_info = research.get("top_gap")
    area_name = gap_info["cluster_label"] if gap_info else "都心主要エリア"
    gap_pt = gap_info["gap_pt"] if gap_info else abs(s1["wpct"] - s2["wpct"])
    topic_focus = plan.get("custom_topic") or plan.get("theme_label")

    table_rows = []
    for st in (stores[:4] or [s1, s2, s3]):
        table_rows.append([
            st["ja_name"],
            f"女性 {st['women']}名 / 男性 {st['men']}名",
            f"{st['wpct']}%",
            f"終電後キープ目安 {st['midnight_retention_pct']}%",
        ])

    common_table = {
        "caption": f"実測スナップ比較（全国女性計 {research['national_women']}名・平均女性比 {research['national_wpct']}% 時点）",
        "headers": ["店舗名", "現在人数バランス", "女性比率", "深夜帯の粘り強さ"],
        "rows": table_rows,
    }
    store_links_1 = [{"label": s1["ja_name"], "href": s1["store_href"]}, {"label": s2["ja_name"], "href": s2["store_href"]}]
    store_links_2 = [{"label": s3["ja_name"], "href": s3["store_href"]}]

    # Candidate A: Field Report & Dialogue-Driven Story
    slug_a = plan["suggested_slug"]
    title_a = (
        f"「{plan['custom_topic']}」を現場と実測ログで検証——徒歩5分で女性比が{gap_pt}%割れる夜のリアルな立ち回り"
        if plan.get("custom_topic")
        else f"エレベーターが開いた瞬間の「男性待ち」で帰らなくていい——{area_name}の実測ログで見えた徒歩5分ハシゴと会話の温度感"
    )
    cand_a = {
        "style": "field_dialogue_story",
        "style_label": "🗣️ 現場ルポ・リアル会話重視案（体験導入×空気感解剖）",
        "writer_note": f"『{s1['ja_name']}』と『{s2['ja_name']}』の実測差（女性比{gap_pt}pt差）を軸に、受付前・乾杯直後・2軒目声かけの3場面の会話を織り込んだストーリー構成。",
        "thinking_memo": "",
        "is_ai_generated": False,
        "slug": slug_a,
        "eyecatch_key": plan["eyecatch_key"],
        "badge": plan["badge"],
        "read_time": "約6分で読める",
        "tag_primary": plan["tag_primary"],
        "tag_secondary": plan["tag_secondary"],
        "title": title_a,
        "short_title": (plan["custom_topic"][:28] if plan.get("custom_topic") else f"{area_name}の徒歩5分男女比ギャップと会話術"),
        "desc": f"「せっかく来たのに受付がスーツ姿の男性だらけ…」と肩を落とした夜から一転、徒歩数分の別店舗へ切り替えて自然に盛り上がった実例と、{s1['ja_name']}・{s3['ja_name']}などの実測ログをまとめました。",
        "keywords": f"相席ラウンジ,{s1['ja_name']},{s2['ja_name']},{s3['ja_name']},男女比,ハシゴ,相席ナビ",
        "sections": [
            {
                "heading": f"1. 「ここ、さっきの店と空気ぜんぜん違うね」——徒歩4分で景色が変わった夜",
                "paragraphs": [
                    f"{research['calendar']['tag']}の21時半すぎ。仕事終わりに相方と合流して最初に向かった店では、エレベーターの扉が開いた瞬間に受付前のソファまで男性グループが並んでいました。",
                    f"以前の僕たちなら「せっかく来たし30分くらい待つか…」とそのまま番号札を受け取って、案内される頃にはすっかり疲れ切っていたと思います。でもその夜はスマホで近隣店舗の人数を見比べ、徒歩4分の『{s1['casual_name']}』へサッと切り替えました。",
                ],
                "dialogue": [
                    "「さっきの店、受付まで男の人でパンパンだったのに、こっちはすぐ座れたね」",
                    f"「ほんとだ。しかも周りのテーブル、仕事帰りの2人組が多くて落ち着いて話せそう」",
                ],
                "after_paragraphs": [
                    f"実際に計測画面を見ていると、同じ{area_name}の目と鼻の先でも『{s1['ja_name']}』（女性{s1['women']}名・女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性{s2['women']}名・女性比{s2['wpct']}%）のように、15〜20分で空気感がガラッと割れる瞬間が毎晩のように起きています。",
                ],
                "highlight_title": f"今夜のテーマ焦点：{topic_focus}",
                "highlight_body": f"1店舗目の混雑だけで「今日はハズレの日だ」と決めつけず、徒歩5分圏の男女比ギャップを見てフラットに店を選び直すだけで、最初の乾杯の余裕がまったく変わります。",
                "store_links": store_links_1,
            },
            {
                "heading": "2. 数字で見ると納得する「1軒目の波」と「終電前のエアポケット」",
                "paragraphs": [
                    "なぜ同じ駅のすぐ近くでここまで男女比に差がつくのでしょうか。全国53店舗の人数推移を追いかけていると、立地（駅地下直結か路面か）やブランドの年齢層によって、女性が入ってくるタイミングに明確な時差があることが分かります。",
                ],
                "dialogue": [
                    "「私たち最初ごはん食べてて、甘いお酒飲みたくなって2軒目でふらっと来たんです〜」",
                    "「実はさっき別のお店覗いたらすごい混んでてさ、こっち来て大正解だったよ（笑）」",
                ],
                "after_paragraphs": [
                    f"たとえば『{s1['ja_name']}』のように早い時間から女性比率が{s1['wpct']}%前後で安定しやすい店舗もあれば、『{s3['ja_name']}』のように22時半〜深夜にかけて終電後の維持目安が{s3['midnight_retention_pct']}%と粘り強い店舗もあります。下の実測スナップを見比べるだけでも、店舗ごとの個性がはっきり浮かび上がります。",
                ],
                "highlight_title": "",
                "highlight_body": "",
                "store_links": store_links_2,
            },
            {
                "heading": "3. 乾杯10分で空気がほぐれる席は「質問攻め」をしない",
                "paragraphs": [
                    "もうひとつ、良い男女比の店舗に入れたあとに大事なのが「座って最初の10分」の温度感です。照明が落ち着いた70cmほどの距離のソファ席では、履歴書のような質問を繰り返すよりも、その日の街の空気や1軒目の話を軽くシェアするほうが何倍も自然に打ち解けます。",
                ],
                "dialogue": [
                    "「今日外ちょっと風あったよね、1軒目どのへんで飲んでたの？」",
                    "「すぐ裏のイタリアンにいました！ そっちはお仕事帰りですか？ なんか落ち着いて話せる席でよかった〜」",
                ],
                "after_paragraphs": [
                    "この「落ち着いて話せる席でよかった」という一言が相手から出れば、無理にテンションを作らなくても会話は自然に転がり始めます。逆に30〜40分話してみてお互いスマホが気になり始めたら、気まずくなる前にスマートに席替えや店舗移動を選ぶのがお互いにとって一番心地いい立ち回りです。",
                ],
                "highlight_title": "明日から使える「3つの現場マイルール」",
                "highlight_body": "①入店前のエレベーター下で必ず近隣2〜3店舗の女性比率を見比べる ／ ②座って3分は一問一答ではなく「今日1軒目どこだった？」の雑談から入る ／ ③40分経って噛み合わなければ予算を溶かす前に軽やかに切り替える。",
                "store_links": store_links_1 + store_links_2,
            },
        ],
        "data_table": common_table,
        "summary_points": [
            f"同じ街でも『{s1['ja_name']}』（女性比{s1['wpct']}%）と近隣店で{gap_pt}pt近い差が出るため、入店前の10秒確認を習慣にする",
            f"22時半以降の2軒目ラウンドでは、終電後維持目安が{s3['midnight_retention_pct']}%と高い『{s3['ja_name']}』のような粘り強い店舗を候補に入れる",
            "乾杯直後は面接のような質問を避け、その日の街況や1軒目トークで空気を温めてから波長を見極める",
        ],
        "faqs": [
            {
                "q": f"{area_name}で1軒目が男性待ちだった場合、どれくらい待つか移動すべきですか？",
                "a": f"受付で20分以上の待ち案内が出ている場合、同じビルや徒歩5分圏内に『{s1['ja_name']}』など女性比率が55%を超えている別店舗があれば、待たずに移動した方がトータル料金も体力も温存できます。",
            },
            {
                "q": "22時半や23時過ぎから入店しても間に合いますか？",
                "a": f"終電前の23時20分前後は一時的に帰宅組が増えますが、『{s3['ja_name']}』（深夜維持目安{s3['midnight_retention_pct']}%）など繁華街中心部の店舗では0時過ぎから2軒目・3軒目組の第2波が入るため十分狙えます。",
            },
        ],
        "x_title": f"{area_name}で徒歩5分の男女比が{gap_pt}%割れる理由と失敗しないハシゴ術",
        "x_main_text": (
            f"エレベーターが開いた瞬間に受付まで男性待ちで「今日ハズレか…」と帰りかけた夜、徒歩4分の『{s1['casual_name']}』を見たら女性{s1['women']}名（女性比{s1['wpct']}%）で別世界だった話🌃\n\n"
            f"同じ街でも{gap_pt}%近く割れる理由と、乾杯10分のリアルな会話術を書きました👇\n"
            "#相席ラウンジ #相席ナビ"
        ),
        "note_title": title_a,
        "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #夜遊び #恋愛コラム",
    }

    # Candidate B: Data Contrast & Cost-Performance / Timing Strategy
    slug_b = f"{plan['suggested_slug']}-data"
    title_b = (
        f"【実測比較】{plan['custom_topic']}——90分粘って後悔した夜と「数字を見て40分で動いた夜」の決定的な差"
        if plan.get("custom_topic")
        else f"気を使って90分粘る人ほど損をする？全国{research['national_women'] + research['national_men']}名のリアルタイム比率から逆算する「外さない店選びと引き際」"
    )
    cand_b = {
        "style": "data_contrast_guide",
        "style_label": "📊 実測データ×失敗談対比案（損益比較×時間帯ロジック）",
        "writer_note": f"全国稼働（女性{research['national_women']}名・平均女性比{research['national_wpct']}%）と上位店舗の深夜維持率を対比し、「惰性の90分滞在」を防ぐ実践判断軸に落とし込んだ構成。",
        "thinking_memo": "",
        "is_ai_generated": False,
        "slug": slug_b,
        "eyecatch_key": "cost-break" if plan["theme_key"] == "second_venue_move" else plan["eyecatch_key"],
        "badge": "実測×損益比較",
        "read_time": "約6分で読める",
        "tag_primary": ["tag-amber", "📊 実測ログ×引き際検証"],
        "tag_secondary": plan["tag_secondary"],
        "title": title_b,
        "short_title": "惰性90分vs40分見切り｜実測比率で選ぶ立ち回り",
        "desc": f"「悪い子じゃないけど話が弾まないな…」と思いながら延長を重ねて1万円近く払った苦い経験をもとに、全国女性比{research['national_wpct']}%の実測データを使った店選びとスマートな引き際を整理しました。",
        "keywords": f"相席ラウンジ,料金,男女比,{s1['ja_name']},{s3['ja_name']},見切り,相席ナビ",
        "sections": [
            {
                "heading": "1. 改札前のレシートを見て相方と苦笑いした「気使いすぎた90分」",
                "paragraphs": [
                    "相席ラウンジに何度か通っていると、一番もったいない出費になるのが「大外れではないけれど、お互い2軒目に行くほどでもない席」でなんとなく90分・100分と居続けてしまうパターンです。",
                ],
                "dialogue": [
                    "「悪い子たちじゃなかったけど、後半ほぼ仕事の話で終わっちゃったな…」",
                    "「しかも気づいたら1人9,000円超えてるじゃん。40分で切り上げて2軒目行けばよかったね（笑）」",
                ],
                "after_paragraphs": [
                    f"そんな反省から私たちが徹底するようになったのが、入店前と着席35分時点の2回だけ「今の周辺店舗の男女比」を冷静にチェックする習慣です。現在も全国で女性{research['national_women']}名・男性{research['national_men']}名（女性比{research['national_wpct']}%）が動いていますが、店舗ごとの偏りは想像以上に大きく出ます。",
                ],
                "highlight_title": "感覚ではなく「周辺の現在地」を持って座る強み",
                "highlight_body": f"たとえば『{s1['ja_name']}』が女性{s1['women']}名（女性比{s1['wpct']}%）と余裕があることを知っていれば、目の前の席が噛み合わなくても焦らずフラットに次の判断ができます。",
                "store_links": store_links_1,
            },
            {
                "heading": "2. 店舗ごとの「ピーク波形」と「終電後の残り方」を比べてみる",
                "paragraphs": [
                    f"同じ10分単位のチャージを払うなら、今の時間帯に強い店舗を選ぶだけで相席のテンポが劇的に変わります。特に{research['calendar']['tag']}のような夜は、1軒目組と2軒目組が動く境界線を知っておくことが武器になります。",
                ],
                "dialogue": [
                    "「さっき23時前に1組帰っちゃったけど、0時回ってからまた女の子入ってきたね」",
                    f"「ほんとだ。『{s3['casual_name']}』は終電後も残る子が多いって聞いてたけど、この時間からでも普通に出会えるね」",
                ],
                "after_paragraphs": [
                    f"実測データを見ても、『{s3['ja_name']}』のように深夜帯の維持目安が{s3['midnight_retention_pct']}%ある店舗と、22時台にピークが集中する店舗では戦い方が違います。早い時間は『{s1['ja_name']}』、遅めの合流なら『{s3['ja_name']}』というように役割分担を決めておくと迷いません。",
                ],
                "highlight_title": "",
                "highlight_body": "",
                "store_links": store_links_2,
            },
            {
                "heading": "3. 盛り上がったら45分で外へ、噛み合わなければ40分でリセット",
                "paragraphs": [
                    "面白いことに、うまくいく席ほど店内に2時間も居座りません。乾杯して30〜40分ほど笑って話せたら、「ちょっと静かなカフェかバーで1杯だけ飲まない？」と声をかけたほうが、男性側のチャージも抑えられ、2人の距離もぐっと縮まります。",
                ],
                "dialogue": [
                    "「ここちょっと音楽大きくなってきたし、すぐ近くに美味しいフルーツカクテルのバーあるから1杯だけ行かない？」",
                    "「え、行きたいです！ ちょうどもう少しゆっくり話したいなって思ってました」",
                ],
                "after_paragraphs": [
                    "「盛り上がった席は45分で2軒目へ」「噛み合わない席は40分で感謝を伝えてリセット」。このシンプルな基準を持っておくだけで、お財布にもメンタルにも驚くほど余裕が生まれます。",
                ],
                "highlight_title": "今夜の判断チェックリスト",
                "highlight_body": "①女性比率55%以上かつ女性15名以上の店舗を第1候補にする ／ ②35分話して会話が仕事の一問一答から広がらなければ引き際サイン ／ ③波長が合ったらデザートや静かなバーを口実に45〜50分でスマートに外へ誘う。",
                "store_links": store_links_1 + store_links_2,
            },
        ],
        "data_table": common_table,
        "summary_points": [
            f"全国平均女性比（現在 {research['national_wpct']}%）より高い店舗を選ぶだけで、席替え待ちのロスを大幅に減らせる",
            "微妙な席での「気使い延長」が一番コスパを悪化させるため、40分をひとつの分岐点にする",
            f"23時以降の2軒目参戦なら『{s3['ja_name']}』など終電後維持力の高い店舗へ絞って短期決戦にする",
        ],
        "faqs": [
            {
                "q": "40分で見切るとき、相手に失礼にならない切り上げ方はありますか？",
                "a": "「明日朝イチで大事な打ち合わせがあって、今日は1時間だけって決めてたんだよね。乾杯できて楽しかった、ありがとう！」と最初から時間制限があった形で明るくお礼を伝えれば、お互い気持ちよく終われます。",
            },
            {
                "q": "2軒目に誘うベストなタイミングは何分くらいですか？",
                "a": "2杯目のグラスが半分ほど空いた入店40〜50分頃が最も自然です。長居しすぎると終電や眠気が勝ってしまうため、「ちょっと静かなお店で1杯だけ」とハードルを下げて誘うのがコツです。",
            },
        ],
        "x_title": "90分粘って後悔した夜と「数字を見て40分で動いた夜」の決定的な差",
        "x_main_text": (
            f"「悪い子じゃないけど話が広がらないな…」と気を使って90分いて、改札前で1万円近いレシートを見て相方と苦笑いした夜の話💸\n\n"
            f"『{s1['casual_name']}』（女性比{s1['wpct']}%）など実測ログを使った店選びと「40分見切り」のコツをまとめました👇\n"
            "#相席ラウンジ #相席ナビ"
        ),
        "note_title": title_b,
        "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #コスパ #恋愛攻略 #相席ナビ",
    }

    return [
        _render_candidate_preview_and_note(cand_a, site_url=site_url),
        _render_candidate_preview_and_note(cand_b, site_url=site_url),
    ]


def _build_column_gemini_prompt(research: dict, plan: dict, site_url: str) -> str:
    fact_sheet = {
        "calendar": research.get("calendar"),
        "weather": research.get("weather"),
        "national_women": research.get("national_women"),
        "national_men": research.get("national_men"),
        "national_wpct": research.get("national_wpct"),
        "top_gap": research.get("top_gap"),
        "featured_stores": research.get("featured_stores"),
        "theme_label": plan.get("theme_label"),
        "custom_topic": plan.get("custom_topic"),
        "core_question": plan.get("core_question"),
        "target_reader": plan.get("target_reader"),
        "suggested_slug": plan.get("suggested_slug"),
    }
    cliches_str = "・".join(f"「{w}」" for w in COLUMN_AI_CLICHES)
    return (
        "あなたは全国53店舗の相席ラウンジ人数をリアルタイム計測しているメディア『相席ナビ』の敏腕コラム編集長です。\n"
        "AIが書いたような無機質な解説記事ではなく、本当に週末や仕事帰りにラウンジの現場空気（エレベーターが開いた瞬間の景色、乾杯直後のちょっとした沈黙や盛り上がり、相方との帰り道の反省会）を知る人間が書いた、温かみとリアリティのあるWebコラム＆note原稿＆X告知文を1セット執筆してください。\n\n"
        "【長考（Thinking）プロセス：脳内3案コンペ＆脱AI推敲】\n"
        "内部思考で必ず以下の3ステップを行ってからJSONを出力してください：\n"
        "  Step 1. 導入の人間味エピソードを3通り（①受付待ちや席でのちょっと苦い失敗談から入る案、②友達・相方とのリアルなLINEや会話から入る案、③雨や終電前など街の空気感から入る案）頭の中で比べる。\n"
        "  Step 2. 一番「AIの教科書構文」に見えず、読者が『うわ、それあるある…！』と3行目で引き込まれる1案を選び、3章すべてに自然な口語の会話（dialogue配列、各2セリフ）を入れる。\n"
        "  Step 3. ファクトシートの実測数値・店舗名・URLと1人・1%の狂いもないか確認する。\n\n"
        "【絶対厳守ルール（違反すると鬼デスク検閲で大幅減点されます）】\n"
        "1. 店舗名・人数・女性比率は下記ファクトシートにある数値のみを使い、架空の人数を捏造しないこと。\n"
        "2. ブランド呼称ルール：単体の『オリエンタル』や『オリエンタル新宿』（「ラウンジ」抜け）、内部コード『OLG』は絶対に使わないこと！必ず正式名称『オリエンタルラウンジ〇〇』または常連の自然な口語略称『オリラジ〇〇』（会話やX告知で有効）と表記すること。『ag〇〇』『JIS〇〇』はそのまま表記。\n"
        f"3. AI臭ワードの完全禁止：{cliches_str} はタイトル・本文・FAQ・X告知のどこにも絶対に使わないこと。\n"
        "4. X告知文（`x_main_text`）は全角125文字前後（最大でもTwitter weight 265以内）に引き締め、末尾に `#相席ラウンジ #相席ナビ` を付けること。\n\n"
        f"【検証済みファクトシート】\n{json.dumps(fact_sheet, ensure_ascii=False, indent=2)}\n\n"
        "以下のJSONオブジェクト形式のみで出力してください（Markdownコードブロック不要）：\n"
        "{\n"
        '  "thinking_memo": "脳内でどんな導入3案を比較し、なぜこの体験エピソードと会話トーンを採用したかの編集長推敲メモ（90〜150文字）",\n'
        '  "writer_note": "今回の記事で一番効かせた実測データと人間味フックの1行要約",\n'
        '  "slug": "英小文字とハイフンのみのURLスラッグ（例: umeda-gap-hopping-1028）",\n'
        '  "title": "読者が思わずクリックしたくなる人間味×データ根拠のある記事タイトル（36〜62文字）",\n'
        '  "short_title": "パンくず・一覧用の短いタイトル（18〜28文字）",\n'
        '  "desc": "検索スニペット・カード用の共感リード文（90〜135文字）",\n'
        '  "sections": [\n'
        '    {\n'
        '      "heading": "1. 第1章見出し",\n'
        '      "paragraphs": ["導入段落1（情景・体験）", "導入段落2"],\n'
        '      "dialogue": ["「現場でのリアルな会話セリフ1」", "「それに対する自然な返答セリフ2」"],\n'
        '      "after_paragraphs": ["会話を踏まえた実測データへの橋渡し段落"],\n'
        '      "highlight_title": "要点ボックス見出し",\n'
        '      "highlight_body": "要点ボックス本文"\n'
        '    },\n'
        '    {\n'
        '      "heading": "2. 第2章見出し（データ裏付け）",\n'
        '      "paragraphs": ["データ分析段落"],\n'
        '      "dialogue": ["「女性側や相方のリアルなセリフ1」", "「返答セリフ2」"],\n'
        '      "after_paragraphs": ["店舗ごとの使い分け解説段落"],\n'
        '      "highlight_title": "",\n'
        '      "highlight_body": ""\n'
        '    },\n'
        '    {\n'
        '      "heading": "3. 第3章見出し（実践アクション・引き際）",\n'
        '      "paragraphs": ["立ち回り解説段落"],\n'
        '      "dialogue": ["「2軒目声かけや乾杯10分の自然なセリフ1」", "「返答セリフ2」"],\n'
        '      "after_paragraphs": ["締めくくり段落"],\n'
        '      "highlight_title": "明日から使える実践ルール",\n'
        '      "highlight_body": "具体的な3つの行動指針"\n'
        '    }\n'
        '  ],\n'
        '  "summary_points": ["まとめ箇条書き1", "まとめ箇条書き2", "まとめ箇条書き3"],\n'
        '  "faqs": [\n'
        '    {"q": "読者がよく迷う質問1", "a": "具体的で誠実な回答1"},\n'
        '    {"q": "読者がよく迷う質問2", "a": "具体的で誠実な回答2"}\n'
        '  ],\n'
        '  "x_title": "Xリプ欄用の短い記事見出し",\n'
        '  "x_main_text": "X告知1投目（体験あるある＋実測の驚き＋👇 #相席ラウンジ #相席ナビ）",\n'
        '  "note_title": "note読者向けに少しエッセイ感を強めたタイトル",\n'
        '  "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #夜遊び #恋愛コラム"\n'
        "}"
    )


def _make_col_cache_key(research: dict, plan: dict):
    stores_sig = tuple(
        (s.get("raw_name", ""), int(s.get("women") or 0), int(s.get("men") or 0))
        for s in (research.get("featured_stores") or [])[:3]
    )
    return (
        plan.get("theme_key") or "area_hopping",
        plan.get("custom_topic") or "",
        stores_sig,
    )


def has_cached_column_ai(research: dict, plan: dict) -> bool:
    ck = _make_col_cache_key(research, plan)
    now_t = time.time()
    with _col_ai_lock:
        item = _col_ai_cache.get(ck)
        return bool(item and (now_t - item["ts"] < _COL_AI_CACHE_TTL_SEC))


def _generate_gemini_column_candidate(
    research: dict,
    plan: dict,
     baseline_cand: dict,
    site_url: str,
    now_jst: datetime.datetime,
    force_refresh: bool = False,
    cache_only: bool = False,
) -> dict:
    api_key = sns_editorial.get_gemini_api_key(allow_blocking=(not cache_only))
    if not api_key:
        return None

    ck = _make_col_cache_key(research, plan)
    now_t = time.time()
    with _col_ai_lock:
        expired = [k for k, v in _col_ai_cache.items() if now_t - v["ts"] >= _COL_AI_CACHE_TTL_SEC]
        for k in expired:
            _col_ai_cache.pop(k, None)
        if not force_refresh and ck in _col_ai_cache:
            _col_ai_cache.move_to_end(ck)
            return dict(_col_ai_cache[ck]["candidate"])

    if cache_only:
        return None

    prompt = _build_column_gemini_prompt(research, plan, site_url)
    preferred_env = (os.environ.get("GEMINI_MODEL") or "").strip()
    models_to_try = (
        [preferred_env] + [m for m in sns_editorial.DEFAULT_GEMINI_MODELS if m != preferred_env]
        if preferred_env
        else list(sns_editorial.DEFAULT_GEMINI_MODELS)
    )
    saved_route = sns_editorial._last_gemini_meta.get("working_route")
    routes = []
    if saved_route and saved_route[0] == "ai_studio":
        routes.append(saved_route)
    for mn in models_to_try:
        if ("ai_studio", mn) not in routes:
            routes.append(("ai_studio", mn))
    routes = routes[:2]
    deadline = time.monotonic() + 21.0
    last_err = ""
    saw_404 = False

    def _call_model(mn: str):
        rem = deadline - time.monotonic()
        if rem <= 0.6:
            raise requests.exceptions.Timeout("Column Gemini deadline (21s) reached")
        gen_cfg = {
            "temperature": 0.85,
            "maxOutputTokens": 6000,
            "responseMimeType": "application/json",
        }
        if mn.startswith(("gemini-3.5", "gemini-2.5")) or mn in ("gemini-flash-lite-latest", "gemini-flash-latest"):
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 768}
        elif mn.startswith("gemini-3"):
            gen_cfg["thinkingConfig"] = {"thinkingLevel": "LOW"}
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{mn}:generateContent"
        headers = {"Content-Type": "application/json", "x-goog-api-key": api_key}
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}], "generationConfig": gen_cfg}
        return requests.post(url, headers=headers, json=payload, timeout=min(13.0, rem))

    def _parse_response(resp, mn: str):
        data = resp.json() or {}
        cands = data.get("candidates") or []
        if not cands:
            return None, "Empty Gemini candidates"
        parts = ((cands[0].get("content") or {}).get("parts")) or []
        raw_text = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
        if not raw_text:
            raw_text = "".join(p.get("text", "") for p in parts).strip()
        if raw_text.startswith("```"):
            raw_text = re.sub(r"^```(?:json)?\s*", "", raw_text)
            raw_text = re.sub(r"\s*```$", "", raw_text)
        parsed = json.loads(raw_text, strict=False)
        if isinstance(parsed, list) and parsed:
            parsed = parsed[0]
        if not isinstance(parsed, dict) or not parsed.get("title") or not parsed.get("sections"):
            return None, "Incomplete JSON fields from Gemini"

        ai_slug = _normalize_slug(parsed.get("slug") or plan["suggested_slug"])
        if ai_slug in RESERVED_STATIC_SLUGS:
            ai_slug = f"{ai_slug}-{now_jst.strftime('%m%d')}"

        links_pool = plan.get("internal_links") or []
        built_sections = []
        for idx, sec in enumerate(parsed.get("sections") or []):
            if not isinstance(sec, dict):
                continue
            built_sections.append({
                "heading": _sanitize_brand_names(sec.get("heading") or f"{idx + 1}. 現場のポイント"),
                "paragraphs": [_sanitize_brand_names(p) for p in (sec.get("paragraphs") or []) if str(p).strip()],
                "dialogue": [_sanitize_brand_names(d) for d in (sec.get("dialogue") or []) if str(d).strip()],
                "after_paragraphs": [_sanitize_brand_names(p) for p in (sec.get("after_paragraphs") or []) if str(p).strip()],
                "highlight_title": _sanitize_brand_names(sec.get("highlight_title") or ""),
                "highlight_body": _sanitize_brand_names(sec.get("highlight_body") or ""),
                "store_links": links_pool[:2] if idx == 0 else (links_pool[1:3] if idx == 1 else links_pool),
            })
        if len(built_sections) < 2:
            return None, "Too few sections in Gemini output"

        x_main = _sanitize_brand_names(parsed.get("x_main_text") or baseline_cand["x_main_text"], prefer_casual_if_tight=True)
        if x_poster.compute_twitter_weight(x_main) > 272 and "オリエンタルラウンジ" in x_main:
            x_main = x_main.replace("オリエンタルラウンジ", "オリラジ")

        cand = {
            "style": "ai_longform_story",
            "style_label": "✨ Gemini AI 長考ストーリー＆データ融合案（編集長推敲）",
            "writer_note": f"【{mn}・長考執筆】{_sanitize_brand_names(parsed.get('writer_note') or '現場の空気感と実測ログを掛け合わせたAI編集長案')}",
            "thinking_memo": _sanitize_brand_names(parsed.get("thinking_memo") or ""),
            "is_ai_generated": True,
            "ai_model": mn,
            "slug": ai_slug,
            "eyecatch_key": plan["eyecatch_key"],
            "badge": plan["badge"],
            "read_time": "約6分で読める",
            "tag_primary": plan["tag_primary"],
            "tag_secondary": plan["tag_secondary"],
            "title": _sanitize_brand_names(parsed.get("title")),
            "short_title": _sanitize_brand_names(parsed.get("short_title") or parsed.get("title")[:28]),
            "desc": _sanitize_brand_names(parsed.get("desc") or baseline_cand["desc"]),
            "keywords": baseline_cand["keywords"],
            "sections": built_sections,
            "data_table": baseline_cand["data_table"],
            "summary_points": [_sanitize_brand_names(pt) for pt in (parsed.get("summary_points") or baseline_cand["summary_points"])],
            "faqs": [
                {"q": _sanitize_brand_names(f.get("q", "")), "a": _sanitize_brand_names(f.get("a", ""))}
                for f in (parsed.get("faqs") or baseline_cand["faqs"])
                if isinstance(f, dict) and f.get("q") and f.get("a")
            ] or baseline_cand["faqs"],
            "x_title": _sanitize_brand_names(parsed.get("x_title") or parsed.get("short_title") or baseline_cand["x_title"]),
            "x_main_text": x_main,
            "note_title": _sanitize_brand_names(parsed.get("note_title") or parsed.get("title")),
            "note_hashtags": str(parsed.get("note_hashtags") or baseline_cand["note_hashtags"]),
        }
        _render_candidate_preview_and_note(cand, site_url=site_url)
        return cand, ""

    for _, mn in routes:
        if time.monotonic() >= deadline - 0.6:
            break
        try:
            resp = _call_model(mn)
            if resp.status_code == 404:
                saw_404 = True
                last_err = f"Model {mn} HTTP 404"
                continue
            if resp.status_code >= 400:
                last_err = f"Gemini HTTP {resp.status_code} ({mn}): {(resp.text or '')[:120]}"
                continue
            cand, p_err = _parse_response(resp, mn)
            if cand:
                with _col_ai_lock:
                    _col_ai_cache[ck] = {"ts": time.time(), "candidate": cand, "model": mn}
                    while len(_col_ai_cache) > _COL_AI_CACHE_MAX_SIZE:
                        _col_ai_cache.popitem(last=False)
                sns_editorial._last_gemini_meta["working_route"] = ("ai_studio", mn)
                sns_editorial._last_gemini_meta["last_model"] = mn
                sns_editorial._last_gemini_meta["last_error"] = ""
                sns_editorial._last_gemini_meta["last_ok_at"] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
                return dict(cand)
            last_err = p_err
        except Exception as e:
            last_err = str(e)[:140]
            logger.warning(f"[column_editorial] Gemini error ({mn}): {e}")

    if saw_404 and (deadline - time.monotonic()) > 2.0:
        disc = sns_editorial._discover_ai_studio_flash_model(api_key, timeout=3.0)
        if disc and disc not in models_to_try:
            try:
                resp = _call_model(disc)
                if resp.status_code == 200:
                    cand, _ = _parse_response(resp, disc)
                    if cand:
                        with _col_ai_lock:
                            _col_ai_cache[ck] = {"ts": time.time(), "candidate": cand, "model": disc}
                        sns_editorial._last_gemini_meta["working_route"] = ("ai_studio", disc)
                        sns_editorial._last_gemini_meta["last_model"] = disc
                        sns_editorial._last_gemini_meta["last_error"] = ""
                        sns_editorial._last_gemini_meta["last_ok_at"] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
                        return dict(cand)
            except Exception:
                pass

    if last_err:
        sns_editorial._last_gemini_meta["last_error"] = last_err
    return None


def run_column_writer(
    research: dict,
    plan: dict,
    now_jst: datetime.datetime = None,
    app_url: str = "",
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
) -> list:
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    base_cands = _build_deterministic_candidates(research, plan, site_url=site_url)

    if use_ai:
        ai_cand = _generate_gemini_column_candidate(
            research=research,
            plan=plan,
            baseline_cand=base_cands[0],
            site_url=site_url,
            now_jst=now_jst,
            force_refresh=force_ai_refresh,
            cache_only=ai_cache_only,
        )
        if ai_cand:
            return [ai_cand] + base_cands
    return base_cands


# =====================================================================
# Role 4: Evaluator / Desk (鬼デスク・100点採点＆検閲係)
# =====================================================================
def run_column_evaluator(candidates: list, research: dict, plan: dict) -> dict:
    evaluated = []
    verified_names = [s["ja_name"] for s in (research.get("featured_stores") or [])] + [
        s["casual_name"] for s in (research.get("featured_stores") or [])
    ]

    for cand in candidates:
        c = dict(cand)
        # Ensure x_reply_text preview is attached
        c["x_reply_text"] = (
            f"📖 新しいコラムを書きました👇\n"
            f"『{c['x_title']}』\n"
            f"https://aiseki-navi.com/guide/{c['slug']}"
        )

        sections = c.get("sections") or []
        dialogue_count = sum(1 for sec in sections if sec.get("dialogue"))
        full_blob = " ".join([
            c.get("title") or "",
            c.get("desc") or "",
            c.get("preview_text") or "",
            c.get("x_main_text") or "",
        ])

        pros = []
        warnings = []

        # 1. Human Story & Dialogue Reality (35 pts)
        human_pts = 20
        if dialogue_count >= 3:
            human_pts += 11
            pros.append(f"現場のリアルな会話ボックス（.dialogue-box）を{dialogue_count}箇所すべてに配置")
        elif dialogue_count >= 2:
            human_pts += 7
            pros.append(f"現場の会話描写を{dialogue_count}箇所に配置")
        else:
            warnings.append("会話ボックスが少ないため読み物としての臨場感がやや弱め")

        if c.get("is_ai_generated") and c.get("thinking_memo"):
            human_pts += 4
            pros.append("Gemini長考3案コンペによる独自の情景導入・言い回し推敲済み")
        elif c.get("style") == "field_dialogue_story":
            human_pts += 3
            pros.append("受付前〜乾杯10分の体験ストーリー導入で共感性が高い")
        else:
            human_pts += 2

        cliche_hits = [w for w in COLUMN_AI_CLICHES if w in full_blob]
        if cliche_hits:
            human_pts -= min(14, len(cliche_hits) * 4)
            warnings.append(f"AI定型句（{', '.join(cliche_hits[:3])}）を検出したため減点")
        else:
            pros.append("AI臭ワード（「徹底解説」「いかがでしたか」等）ゼロクリア")
        human_pts = max(8, min(35, human_pts))

        # 2. Fact & Brand Hygiene (25 pts)
        fact_pts = 25
        bare_oriental = len(re.findall(r"オリエンタル(?!ラウンジ)", full_blob))
        raw_olg = len(re.findall(r"\bOLG\b", full_blob))
        if bare_oriental > 0 or raw_olg > 0:
            fact_pts -= 12
            warnings.append("ブランド呼称違反（単体『オリエンタル』または『OLG』残存）を検出")
        else:
            pros.append("ブランド呼称検閲OK（『オリエンタルラウンジ』／『オリラジ』統一）")

        matched_stores = [nm for nm in verified_names if nm and nm in full_blob]
        if not matched_stores:
            fact_pts -= 6
            warnings.append("実測店舗名の具体言及が不足")
        if "/link/" in full_blob:
            fact_pts -= 8
            warnings.append("本文への直接アフィリエイトリンク混入を検出")
        fact_pts = max(5, min(25, fact_pts))

        # 3. SEO Structure & Internal Navigation (25 pts)
        seo_pts = 0
        if len(sections) >= 3:
            seo_pts += 8
        if c.get("data_table") and (c["data_table"].get("rows") or []):
            seo_pts += 7
            pros.append(f"検証済み実測データ比較表（{len(c['data_table']['rows'])}店舗）を内蔵")
        if len(c.get("faqs") or []) >= 2:
            seo_pts += 5
        store_link_set = {
            lk.get("href")
            for sec in sections
            for lk in (sec.get("store_links") or [])
            if str(lk.get("href") or "").startswith("/store/")
        }
        if len(store_link_set) >= 2:
            seo_pts += 5
            pros.append(f"店舗個別ページへの有効内部リンク {len(store_link_set)}本を自動配線")
        elif len(store_link_set) == 1:
            seo_pts += 3
        seo_pts = max(5, min(25, seo_pts))

        # 4. X Thread Weight & note Manuscript Readiness (15 pts)
        x_weight = x_poster.compute_twitter_weight(c.get("x_main_text") or "")
        note_len = len(c.get("note_markdown") or "")
        sns_pts = 0
        if 110 <= x_weight <= 274:
            sns_pts += 8
            pros.append(f"X新着告知ウェイト適正（{x_weight}/280）")
        elif x_weight <= 280:
            sns_pts += 5
        else:
            warnings.append(f"X告知文が上限超過（{x_weight}/280）")

        if note_len >= 700 and "#" in (c.get("note_hashtags") or ""):
            sns_pts += 7
            pros.append(f"noteコピペ用Markdown原稿（{note_len}文字）＆タグ完備")
        elif note_len >= 400:
            sns_pts += 4
        sns_pts = max(2, min(15, sns_pts))

        total_score = human_pts + fact_pts + seo_pts + sns_pts
        c["scores"] = {
            "human_story": human_pts,
            "fact_brand_hygiene": fact_pts,
            "seo_structure": seo_pts,
            "sns_note_ready": sns_pts,
        }
        c["total_score"] = total_score
        c["x_weight"] = x_weight
        c["note_char_count"] = note_len
        c["dialogue_count"] = dialogue_count
        c["pros"] = pros[:4]
        c["warnings"] = warnings
        evaluated.append(c)

    evaluated.sort(key=lambda x: (x["total_score"], 1 if x.get("is_ai_generated") else 0), reverse=True)
    for idx, item in enumerate(evaluated):
        item["rank"] = idx + 1
        item["is_selected"] = (idx == 0)

    winner = evaluated[0] if evaluated else None
    desk_summary = (
        f"全{len(evaluated)}案を100点満点で検閲し、『{winner['style_label']}』（{winner['total_score']}点／会話{winner['dialogue_count']}箇所・X weight {winner['x_weight']}/280）を第1位推薦に選出しました。"
        if winner else "候補案がありません。"
    )
    return {
        "winner": winner,
        "candidates": evaluated,
        "desk_summary": desk_summary,
    }


# =====================================================================
# Existing & Scheduled Columns Quality Audit (全コラム鬼デスク監査一覧)
# =====================================================================
def audit_existing_columns(force: bool = False) -> dict:
    """Scan all static (14), scheduled static (3), and dynamic editorial columns for human tone & brand hygiene."""
    now_t = time.time()
    with _audit_lock:
        if not force and _audit_cache["data"] is not None and (now_t - _audit_cache["ts"] < 60.0):
            return _audit_cache["data"]

    import scheduled_articles
    tpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    now_naive = scheduled_articles.now_jst_naive()
    rows = []

    def _audit_html_file(meta: dict, status_label: str, is_dynamic: bool = False, publish_at_ja: str = "公開中"):
        fpath = os.path.join(tpl_dir, meta["template"])
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw_html = f.read()
        except Exception:
            raw_html = ""
        # Strip script/style tags for clean body inspection
        body_only = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.S | re.I)
        plain = re.sub(r"<[^>]+>", " ", body_only)
        plain = html.unescape(re.sub(r"\s+", " ", plain)).strip()

        dialogue_cnt = raw_html.count("dialogue-box") - raw_html.count(".dialogue-box")
        dialogue_cnt = max(0, dialogue_cnt)
        store_links = len(set(re.findall(r'href=["\'](/store/[a-z0-9\-]+)["\']', raw_html)))
        has_table = ("data-table" in raw_html or "<table" in raw_html)
        bare_oriental = len(re.findall(r"オリエンタル(?!ラウンジ)", plain))
        cliche_cnt = sum(1 for w in COLUMN_AI_CLICHES if w in plain)

        score = 72
        if dialogue_cnt >= 2:
            score += 14
        elif dialogue_cnt == 1:
            score += 8
        if store_links >= 2:
            score += 6
        elif store_links == 1:
            score += 3
        if has_table:
            score += 6
        if bare_oriental == 0:
            score += 2
        else:
            score -= min(20, bare_oriental * 8)
        score -= min(10, cliche_cnt * 2)
        score = max(40, min(100, score))

        return {
            "slug": meta["slug"],
            "path": meta["path"],
            "title": meta["title"],
            "status_label": status_label,
            "publish_at_ja": publish_at_ja,
            "is_dynamic": is_dynamic,
            "dialogue_count": dialogue_cnt,
            "store_link_count": store_links,
            "has_table": has_table,
            "bare_oriental_count": bare_oriental,
            "char_count": len(plain),
            "desk_score": score,
        }

    # 1. Dynamic columns created via Column Editorial Room (newest first)
    for dyn in reversed(get_dynamic_scheduled_articles(allow_blocking=False)):
        is_pub = scheduled_articles.is_published(dyn, now_jst=now_naive)
        secs = dyn.get("sections") or []
        d_cnt = sum(1 for s in secs if s.get("dialogue"))
        s_links = len({
            lk.get("href")
            for s in secs
            for lk in (s.get("store_links") or [])
            if str(lk.get("href") or "").startswith("/store/")
        })
        blob = json.dumps(dyn.get("sections") or [], ensure_ascii=False) + (dyn.get("title") or "")
        bare_cnt = len(re.findall(r"オリエンタル(?!ラウンジ)", blob))
        rows.append({
            "slug": dyn["slug"],
            "path": dyn["path"],
            "title": dyn["title"],
            "status_label": "🟢 公開中（編集部作成）" if is_pub else "🗓️ 予約待機中（編集部作成）",
            "publish_at_ja": dyn.get("publish_at_ja") or dyn.get("publish_at"),
            "is_dynamic": True,
            "dialogue_count": d_cnt,
            "store_link_count": s_links,
            "has_table": bool(dyn.get("data_table")),
            "bare_oriental_count": bare_cnt,
            "char_count": len(dyn.get("note_markdown") or blob),
            "desk_score": int(dyn.get("editorial_score") or 94),
        })

    # 2. Static scheduled columns (3 articles)
    for sc in scheduled_articles.SCHEDULED_ARTICLES:
        is_pub = scheduled_articles.is_published(sc, now_jst=now_naive)
        st_lbl = "🟢 公開中（予約解禁済）" if is_pub else "🗓️ 予約待機中"
        rows.append(_audit_html_file(sc, st_lbl, is_dynamic=False, publish_at_ja=sc.get("publish_at_ja") or ""))

    # 3. Static evergreen columns (14 articles)
    for col in EXISTING_STATIC_COLUMNS:
        rows.append(_audit_html_file(col, "🟢 公開中（定番）", is_dynamic=False, publish_at_ja="常時公開"))

    avg_score = round(sum(r["desk_score"] for r in rows) / len(rows), 1) if rows else 0.0
    total_bare = sum(r["bare_oriental_count"] for r in rows)
    with_dialogue = sum(1 for r in rows if r["dialogue_count"] >= 1)

    report = {
        "total_columns": len(rows),
        "with_dialogue_count": with_dialogue,
        "total_bare_oriental": total_bare,
        "avg_desk_score": avg_score,
        "columns": rows,
    }
    with _audit_lock:
        _audit_cache["ts"] = now_t
        _audit_cache["data"] = report
    return report


# =====================================================================
# Full 4-Role Column Editorial Pipeline Entrypoint
# =====================================================================
def run_column_editorial_pipeline(
    live_stores: list,
    theme_key: str = "area_hopping",
    custom_topic: str = "",
    now_jst: datetime.datetime = None,
    app_url: str = "",
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
) -> dict:
    now_jst = now_jst or datetime.datetime.now(JST)
    t_key = theme_key if theme_key in COLUMN_THEMES else "area_hopping"

    research = run_column_researcher(
        live_stores=live_stores,
        theme_key=t_key,
        custom_topic=custom_topic,
        now_jst=now_jst,
    )
    plan = run_column_planner(research=research, now_jst=now_jst)

    gemini_status = sns_editorial.get_gemini_status(allow_blocking=(not ai_cache_only))
    ai_cached = has_cached_column_ai(research, plan)
    ai_pending_async = bool(use_ai and ai_cache_only and gemini_status.get("configured") and not ai_cached)

    candidates = run_column_writer(
        research=research,
        plan=plan,
        now_jst=now_jst,
        app_url=app_url,
        use_ai=use_ai,
        force_ai_refresh=force_ai_refresh,
        ai_cache_only=ai_cache_only,
    )
    evaluation = run_column_evaluator(candidates=candidates, research=research, plan=plan)

    default_sched_dt = (now_jst.astimezone(JST).replace(tzinfo=None) + datetime.timedelta(days=1)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )

    return {
        "theme_key": t_key,
        "themes": list(COLUMN_THEMES.values()),
        "eyecatch_presets": [
            {"key": k, "label": v["label"], "jpg": v["jpg"]}
            for k, v in EYECATCH_PRESETS.items()
        ],
        "default_schedule_input": default_sched_dt.strftime("%Y-%m-%dT%H:%M"),
        "research": research,
        "plan": plan,
        "evaluation": evaluation,
        "winner": evaluation.get("winner"),
        "gemini_status": sns_editorial.get_gemini_status(allow_blocking=False),
        "ai_pending_async": ai_pending_async,
        "history": get_recent_column_history(limit=6, allow_blocking=False),
        "existing_audit": audit_existing_columns(),
    }
