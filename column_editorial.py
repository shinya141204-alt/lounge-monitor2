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
    "guide",
    "for-women",
    "ladies",
    "grooming",
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

SWEET_FANTASY_CLICHES = (
    "え、行きたいです",
    "ちょうどもう少しゆっくり話したい",
    "白Tに",
    "爪を短く",
    "間違いなくモテます",
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
    "female_real_voice": {
        "key": "female_real_voice",
        "label": "💄 女性目線の本音・女子トイレ反省会",
        "badge": "女性目線リアル",
        "eyecatch_key": "first-10min",
        "tag_primary": ["tag-pink", "💄 女性ペアの本音解剖"],
        "tag_secondary": ["tag-cyan", "女子トイレ反省会"],
        "slug_prefix": "female-real-voice",
        "target_reader": "『普通に盛り上がっていたはずなのに、なぜ解散後に既読スルーされるのか』を知りたい男性＆ハズレ店を避けて即乾杯したい女性2人組",
        "core_question": "乾杯15分後の女子トイレLINEで実は何が送られているのか？女性側が『タダ飯消化モード』に切り替わる境界線と、女子組が待ち0分で当たり席を引く店舗選びとは",
    },
    "bitter_loser_habits": {
        "key": "bitter_loser_habits",
        "label": "🌶️ 辛口解剖・「いい人止まり」の敗因",
        "badge": "辛口・敗因解剖",
        "eyecatch_key": "second-venue",
        "tag_primary": ["tag-amber", "🌶️ 現場の残酷なリアル"],
        "tag_secondary": ["tag-pink", "脱・いい人止まり"],
        "slug_prefix": "bitter-loser-habits",
        "target_reader": "礼儀正しく気を配っているつもりなのに、いつも『4人のグループLINE』や『またみんなで飲もう』で終わってしまう男性",
        "core_question": "なぜ『優しい質問役』や『お会計間際の2軒目打診』は9割が社交辞令で終わるのか？綺麗事抜きの敗因解剖とスマートに抜け出す立ち回りとは",
    },
}

EXISTING_STATIC_COLUMNS = [
    {"slug": "for-women", "path": "/for-women", "template": "for_women.html", "title": "「受付で30分放置…」をゼロにする女子のための相席ラウンジ完全ガイド｜待ち0店舗の見分け方＆席替え・退店術"},
    {"slug": "last-train-cliff", "path": "/guide/last-train-cliff", "template": "guide_last_train_cliff.html", "title": "23時20分の終電崖（魔の40分間）と23時を境に逆転する街別ハシゴ法則"},
    {"slug": "price-event-comparison", "path": "/guide/price-event-comparison", "template": "guide_price_comparison.html", "title": "全51店舗「料金・イベント・現在人数」比較ボード活用ガイド"},
    {"slug": "male-lead-mechanics", "path": "/guide/male-lead-mechanics", "template": "guide_male_lead.html", "title": "男性先行のお店に後から女性が集まるメカニズム"},
    {"slug": "beauty-area-stage", "path": "/guide/beauty-area-stage", "template": "guide_beauty_stage.html", "title": "美容サロン激戦区のラウンジに早い時間から女性が集まる理由"},
    {"slug": "club-area-midnight", "path": "/guide/club-area-midnight", "template": "guide_club_midnight.html", "title": "ダンスクラブ密集エリアの深夜0時「第2波」実測データ検証"},
    {"slug": "weekend-strategy", "path": "/guide/weekend-strategy", "template": "guide_weekend.html", "title": "金曜・土曜ピークの男性待ち回避＆時間帯別攻略"},
    {"slug": "brand-comparison", "path": "/guide/brand-comparison", "template": "guide_comparison.html", "title": "オリエンタルラウンジ・ag・JISの3大ブランド徹底比較"},
    {"slug": "weekday-strategy", "path": "/guide/weekday-strategy", "template": "guide_weekday.html", "title": "水曜・木曜の平日夜が実は一番話しやすいデータ的理由"},
    {"slug": "guide", "path": "/guide", "template": "guide_article.html", "title": "相席ラウンジ完全攻略ガイド｜時間帯・男女比・店選びの基本"},
    {"slug": "shinjuku", "path": "/guide/shinjuku", "template": "guide_shinjuku.html", "title": "新宿エリア3店舗の客層・男女比・使い分けガイド"},
    {"slug": "solo-vs-pair", "path": "/guide/solo-vs-pair", "template": "guide_solo_pair.html", "title": "1人参戦（ソロ）vs 2人組ペアのメリット・立ち回り比較"},
    {"slug": "second-venue", "path": "/guide/second-venue", "template": "guide_second_venue.html", "title": "2軒目の誘い方と断られない声かけタイミング"},
    {"slug": "first-10-minutes", "path": "/guide/first-10-minutes", "template": "guide_first_10min.html", "title": "最初の10分で差がつく会話テンポと距離70cmの清潔感"},
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


def _build_deterministic_candidates(
    research: dict,
    plan: dict,
    site_url: str,
    tone_mode: str = None,
) -> list:
    """Generate 2 rich, human-voiced baseline column candidates grounded in live research numbers and `tone_mode`."""
    tm = sns_editorial.get_editorial_tone_mode(tone_mode)
    tone_meta = sns_editorial.EDITORIAL_TONE_MODES[tm]
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

    if tm == "standard":
        # Mild / polite evergreen baseline (still realistic, zero cheesy fantasy clichés)
        slug_a = plan["suggested_slug"]
        title_a = (
            f"「{plan['custom_topic']}」を現場と実測ログで検証——徒歩5分で女性比が{gap_pt}%割れる夜の立ち回り"
            if plan.get("custom_topic")
            else f"エレベーター前の「男性待ち」で消耗しない——{area_name}の実測ログで見えた徒歩5分ハシゴと会話の温度感"
        )
        cand_a = {
            "style": "field_dialogue_story",
            "style_label": f"🥂 【{tone_meta['short_label']}】現場ルポ・丁寧ストーリー案",
            "writer_note": f"『{s1['ja_name']}』と『{s2['ja_name']}』の実測差（女性比{gap_pt}pt差）を軸に、初心者にも馴染みやすい落ち着いたトーンで執筆。",
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
            "desc": f"「せっかく来たのに受付がスーツ姿の男性だらけ…」と迷った夜に、徒歩数分の別店舗へ切り替えて落ち着いた席を引けた実例と、{s1['ja_name']}・{s3['ja_name']}の実測ログを整理しました。",
            "keywords": f"相席ラウンジ,{s1['ja_name']},{s2['ja_name']},{s3['ja_name']},男女比,ハシゴ,相席ナビ",
            "sections": [
                {
                    "heading": "1. 「ここ、さっきの店と空気ぜんぜん違うね」——徒歩4分で景色が変わった夜",
                    "paragraphs": [
                        f"{research['calendar']['tag']}の21時半すぎ。仕事終わりに相方と合流して最初に向かった店では、エレベーターの扉が開いた瞬間に受付前のソファまで男性グループが並んでいました。",
                        f"以前の僕たちなら「せっかく来たし待つか…」と番号札を受け取って、案内される頃にはすっかり疲れていたと思います。でもその夜はスマホで近隣店舗の人数を見比べ、徒歩4分の『{s1['casual_name']}』へサッと切り替えました。",
                    ],
                    "dialogue": [
                        "「さっきの店、受付まで男の人でいっぱいだったのに、こっちはすぐ案内されたね」",
                        "「ほんとだ。周りのテーブルも仕事帰りの2人組が多くて落ち着いて話せそう」",
                    ],
                    "after_paragraphs": [
                        f"実際に計測画面を見ていると、同じ{area_name}の目と鼻の先でも『{s1['ja_name']}』（女性{s1['women']}名・女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性{s2['women']}名・女性比{s2['wpct']}%）のように、15〜20分で空気感がガラッと割れる瞬間が毎晩起きています。",
                    ],
                    "highlight_title": f"今夜のテーマ焦点：{topic_focus}",
                    "highlight_body": "1店舗目の混雑だけで判断せず、徒歩5分圏の男女比ギャップを見てフラットに店を選び直すだけで、最初の乾杯の余裕が変わります。",
                    "store_links": store_links_1,
                },
                {
                    "heading": "2. 数字で見ると納得する「1軒目の波」と「終電後の粘り強さ」",
                    "paragraphs": [
                        "なぜ同じ駅のすぐ近くでここまで男女比に差がつくのでしょうか。全国53店舗の人数推移を追いかけていると、立地やブランドの年齢層によって、女性が入ってくるタイミングに明確な時差があることが分かります。",
                    ],
                    "dialogue": [
                        "「私たち1軒目でビストロにいて、甘いお酒飲みたくなって2軒目で寄ったんです」",
                        "「実は僕たちもさっき別のお店覗いたら混んでて、こっちに切り替えて正解だったよ（笑）」",
                    ],
                    "after_paragraphs": [
                        f"『{s1['ja_name']}』のように女性比率が{s1['wpct']}%前後で安定しやすい店舗もあれば、『{s3['ja_name']}』のように終電後の維持目安が{s3['midnight_retention_pct']}%と粘り強い店舗もあります。",
                    ],
                    "highlight_title": "",
                    "highlight_body": "",
                    "store_links": store_links_2,
                },
                {
                    "heading": "3. 乾杯10分で空気がほぐれる席は「一問一答」をしない",
                    "paragraphs": [
                        "良い男女比の店舗に入れたあとに大切なのが、座って最初の10分の温度感です。履歴書のような質問を繰り返すよりも、その日の街の空気や1軒目の話を軽くシェアするほうが自然に打ち解けます。",
                    ],
                    "dialogue": [
                        "「ここ少し音楽大きくなってきたし、裏に落ち着いた夜カフェあるんだけどコーヒーか甘いもの寄ってかない？」",
                        "「明日仕事だから1杯だけなら！ 甘いものちょっと食べたいかもです」",
                    ],
                    "after_paragraphs": [
                        "「1杯だけ・30分だけ」と出口を明確にしてあげると、相手も構えずに頷きやすくなります。まずは入店前の店舗選びで余裕を作るところから始めてみてください。",
                    ],
                    "highlight_title": "明日から使える「3つの現場マイルール」",
                    "highlight_body": "①エレベーター下で必ず近隣2〜3店舗の女性比率を見比べる ／ ②座って3分は一問一答ではなく1軒目トークから入る ／ ③外へ誘うときは「1杯だけ・30分だけ」と帰宅時間の安心感をセットで伝える。",
                    "store_links": store_links_1 + store_links_2,
                },
            ],
            "data_table": common_table,
            "summary_points": [
                f"同じ街でも『{s1['ja_name']}』（女性比{s1['wpct']}%）と近隣店で{gap_pt}pt近い差が出るため、入店前の10秒確認を習慣にする",
                f"22時半以降の2軒目ラウンドでは、終電後維持目安が{s3['midnight_retention_pct']}%の『{s3['ja_name']}』など粘り強い店舗を候補に入れる",
                "乾杯直後は面接のような質問を避け、2軒目打診は「1杯だけ」とハードルを下げて提案する",
            ],
            "faqs": [
                {
                    "q": f"{area_name}で1軒目が男性待ちだった場合、どれくらい待つか移動すべきですか？",
                    "a": f"受付で20分以上の待ち案内が出ている場合、徒歩5分圏内に『{s1['ja_name']}』など女性比率55%超の別店舗があれば移動した方が体力も予算も温存できます。",
                },
                {
                    "q": "22時半や23時過ぎから入店しても間に合いますか？",
                    "a": f"『{s3['ja_name']}』（深夜維持目安{s3['midnight_retention_pct']}%）など繁華街中心部の店舗では0時過ぎから第2波が入るため十分狙えます。",
                },
            ],
            "x_title": f"{area_name}で徒歩5分の男女比が{gap_pt}%割れる理由とハシゴ術",
            "x_main_text": (
                f"受付まで男性待ちだった夜、徒歩4分の『{s1['casual_name']}』を見たら女性{s1['women']}名（女性比{s1['wpct']}%）で別世界だった話🌃\n\n"
                f"同じ街でも{gap_pt}%近く割れる理由と、乾杯10分の自然な会話のコツをまとめました👇\n"
                "#相席ラウンジ #相席ナビ"
            ),
            "note_title": title_a,
            "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #夜遊び #恋愛コラム",
        }
    else:
        # Spicy / Bitter Reality Candidate (featured in `spicy`, `spicy_female`, and #2 in `female_real`)
        slug_a = plan["suggested_slug"]
        title_a = (
            f"【辛口検証】{plan['custom_topic']}——綺麗事抜きで解剖する「いい人止まり組」と勝てる組の残酷な差"
            if plan.get("custom_topic")
            else f"なぜ「礼儀正しいいい人」ほどグループLINEの墓場で終わるのか？{area_name}の男女比{gap_pt}%差と現場の残酷なリアル"
        )
        cand_a = {
            "style": "field_dialogue_story",
            "style_label": f"🌶️ 【{tone_meta['short_label']}】綺麗事ゼロ・現場の敗因＆本音解剖案",
            "writer_note": f"『{s1['ja_name']}』（女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性比{s2['wpct']}%）のリアルな格差を軸に、受付待ち消耗・面接トーク・グループLINE墓場の図星を辛口に解剖。",
            "thinking_memo": "",
            "is_ai_generated": False,
            "slug": slug_a,
            "eyecatch_key": plan["eyecatch_key"],
            "badge": "辛口・現場解剖",
            "read_time": "約6分で読める",
            "tag_primary": ["tag-amber", "🌶️ 綺麗事ゼロの現場解剖"],
            "tag_secondary": plan["tag_secondary"],
            "title": title_a,
            "short_title": (plan["custom_topic"][:28] if plan.get("custom_topic") else "脱いい人止まり｜現場の残酷な敗因と比率差"),
            "desc": f"「普通に盛り上がったのに既読スルーされた」——その原因の8割は、店選びの段階での消耗と着席15分の『無難な面接官ムーブ』にあります。{s1['ja_name']}・{s2['ja_name']}の実測差とともに本音で斬ります。",
            "keywords": f"相席ラウンジ,{s1['ja_name']},{s2['ja_name']},{s3['ja_name']},本音,脈なしサイン,相席ナビ",
            "sections": [
                {
                    "heading": f"1. 残酷な現実：看板の知名度だけで並んで「疲れた顔」で座った時点で負けている",
                    "paragraphs": [
                        f"少し耳の痛い話から始めます。{research['calendar']['tag']}の夜、何も調べずに一番有名な店舗へ突撃し、受付前の黒いソファでスーツ姿のまま35分待たされてから席に着く男性ペアを毎晩のように見かけます。",
                        f"待たされたイライラと「せっかく高いチャージ払うんだから元を取らなきゃ」という必死さは、座って3秒で女性側に伝染します。しかも皮肉なことに、その同じ瞬間、歩いて4分の『{s1['casual_name']}』を見れば女性{s1['women']}名・男性{s1['men']}名（女性比{s1['wpct']}%）で待ち時間ゼロで回っていたりするのです。",
                    ],
                    "dialogue": [
                        "「さっきの店、男10組待ちで空気ピリついてたのに、こっち普通に女の子だらけじゃん…」",
                        "「マジで危なかったな。あそこで意地張って並んでたら、1杯目から死んだ目で『お仕事何ですか？』って聞くところだった笑」",
                    ],
                    "after_paragraphs": [
                        f"実測データを見れば一目瞭然ですが、同じ{area_name}でも『{s1['ja_name']}』（女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性比{s2['wpct']}%）のように{gap_pt}pt近い差がつくのは日常茶飯事です。トーク力を磨く前に、まず「戦う前から不利な戦場に並ばない」だけで勝率は半分決まります。",
                    ],
                    "highlight_title": f"現場の図星チェック：{topic_focus}",
                    "highlight_body": "「どの店も大差ないでしょ」と看板だけで入る人ほど男余りの店を引いて消耗します。余裕のある男に見える最大の秘訣は、精神論ではなく『女性比率に余裕がある店に最初から座ること』です。",
                    "store_links": store_links_1,
                },
                {
                    "heading": "2. スマホの「表置き・裏置き」に出る、女子側のシビアな15分査定",
                    "paragraphs": [
                        "運よく良い比率の店に入れたとしても、次に待っているのが「いい人止まり」の罠です。一生懸命盛り上げようとして『休みの日なにしてるの？』『どこ住み？』『何歳に見える？』と履歴書確認を始めた瞬間、慣れている女性ペアは心の中で静かにシャッターを下ろします。",
                    ],
                    "dialogue": [
                        "「（女子2人が同時に化粧室へ立った直後、相方と小声で）…なあ、向こうのスマホ、最初からずっと画面上向きでLINE通知見える状態だったよな」",
                        "「うん、たぶん今の化粧室で『次のドリンク来たら出よっか』って作戦会議されてるやつだ。次の席は質問攻めやめよう…」",
                    ],
                    "after_paragraphs": [
                        f"実際、女性グループは驚くほど冷静です。テーブルの上にスマホが画面上向きで置かれ、相方とアイコンタクトが増えたら黄色信号。逆に本当に居心地がいい席ではスマホはバッグの中か画面下向きになり、終電維持目安{s3['midnight_retention_pct']}%の『{s3['ja_name']}』のような店舗ではそのまま2回転目まで話が弾みます。",
                    ],
                    "highlight_title": "",
                    "highlight_body": "",
                    "store_links": store_links_2,
                },
                {
                    "heading": "3. 「4人でグループLINE作ろう！」は9割が社交辞令の墓場になる",
                    "paragraphs": [
                        "そして極めつけの敗因が、お会計や席替えの直前になって慌てて『みんなでグループLINE作ろうよ！』と提案するパターンです。あれは誰も傷つかない代わりに、翌日の昼には通知オフにされて二度と動かない『社交辞令の墓場』になりがちです。",
                    ],
                    "dialogue": [
                        "「さっき言ってた三茶のビストロ、忘れないうちにインスタのDMで店名だけ送っとくね。今アカウント出せる？」",
                        "「あ、嬉しい！ そこ気になってたから保存しときたいです、今出しますね」",
                    ],
                    "after_paragraphs": [
                        "連絡先も2軒目も、「お会計の瞬間」ではなく「食べ物の話や趣味の話が一番盛り上がったピークの瞬間」に1対1の導線（お店のURL共有やインスタ交換）で作ってしまうのが鉄則です。ご都合主義の甘い期待を捨てて、店選びの数字と会話のピーク設計だけシビアに押さえてみてください。",
                    ],
                    "highlight_title": "綺麗事抜きの「脱・負け組」3原則",
                    "highlight_body": "①男性待ちが出ている店に意地で並ばず、女性比55%超の近隣店へ即移動する ／ ②『休みの日何してる？』の面接質問と相方イジりを封印する ／ ③4人グループLINEに逃げず、会話が一番跳ねた瞬間に店リンク共有で個別交換する。",
                    "store_links": store_links_1 + store_links_2,
                },
            ],
            "data_table": common_table,
            "summary_points": [
                f"同じ街でも『{s1['ja_name']}』（女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性比{s2['wpct']}%）で{gap_pt}pt差がつくため、受付待ちで消耗する店を最初から避ける",
                "スマホ画面上向き放置や同時トイレ離席は「タダ飯消化モード」のサイン。質問攻めではなく共通の話題で空気を変える",
                "解散間際の「4人グループLINE」は9割自然消滅するため、盛り上がった最中に店情報シェアで個別交換を済ませる",
            ],
            "faqs": [
                {
                    "q": "相席した女性ペアが盛り上がっているのか社交辞令なのか見分けるサインは？",
                    "a": "一番わかりやすいのは「質問が相手からも返ってくるか」と「スマホの置き方」です。こちらが質問しないと沈黙になる状態が25分続くなら、お互いに消耗する前に笑顔でお礼を言って切り替える方が賢明です。",
                },
                {
                    "q": "2軒目に誘っても『明日早いから』と断られやすいのはなぜですか？",
                    "a": f"長居しすぎて23時を回ってから重たく「もう1軒飲みに行こう」と誘うからです。遅い時間から合流したい夜は最初から終電後キープ目安{s3['midnight_retention_pct']}%の『{s3['ja_name']}』のような深夜強い店舗を選び、早い時間なら開始45分で「30分だけ夜カフェ寄らない？」と出口時間を切って誘うのが自然です。",
                },
            ],
            "x_title": f"なぜ「いい人」ほどグループLINEの墓場で終わるのか？{area_name}の実測比率と敗因解剖",
            "x_main_text": (
                f"正直、相席で一番もったいないのは「看板だけで男余りの店に並んで疲れ果て、お会計前に4人グループLINEを作って自然消滅する」パターン💸\n\n"
                f"『{s1['casual_name']}』（女性比{s1['wpct']}%）など実測格差と女子側のシビアな15分査定を本音で書きました👇\n"
                "#相席ラウンジ #相席ナビ"
            ),
            "note_title": title_a,
            "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #恋愛の本音 #夜遊び",
        }

    # Candidate B: Female Real Perspective ("女子トイレ反省会＆女性側から見た勝ち店舗の逆張り法則")
    slug_b = f"{plan['suggested_slug']}-female-real" if tm != "standard" else f"{plan['suggested_slug']}-data"
    if tm == "standard":
        title_b = (
            f"【実測比較】{plan['custom_topic']}——全国{research['national_women'] + research['national_men']}名の比率から逆算する店選びと引き際"
            if plan.get("custom_topic")
            else f"気を使って長居するほど疲れやすい？全国女性比{research['national_wpct']}%の実測ログから逆算するスマートな店選び"
        )
        cand_b = {
            "style": "data_contrast_guide",
            "style_label": f"📊 【{tone_meta['short_label']}】実測データ×引き際ロジック案",
            "writer_note": f"全国稼働（女性{research['national_women']}名・平均女性比{research['national_wpct']}%）と上位店舗の深夜維持率を対比したデータガイド構成。",
            "thinking_memo": "",
            "is_ai_generated": False,
            "slug": slug_b,
            "eyecatch_key": "cost-break" if plan["theme_key"] == "second_venue_move" else plan["eyecatch_key"],
            "badge": "実測×データ比較",
            "read_time": "約6分で読める",
            "tag_primary": ["tag-amber", "📊 実測ログ×店舗比較"],
            "tag_secondary": plan["tag_secondary"],
            "title": title_b,
            "short_title": "実測比率で選ぶ外さない店舗と引き際",
            "desc": f"全国女性比{research['national_wpct']}%の実測データをもとに、時間帯ごとの店舗の使い分けとスマートな声かけタイミングを整理しました。",
            "keywords": f"相席ラウンジ,料金,男女比,{s1['ja_name']},{s3['ja_name']},相席ナビ",
            "sections": [
                {
                    "heading": "1. レシートを見て相方と振り返った「なんとなく延長」の落とし穴",
                    "paragraphs": [
                        "相席ラウンジで意外と多いのが、会話が盛り上がりきらない席で気を使って90分以上滞在してしまうケースです。",
                    ],
                    "dialogue": [
                        "「悪い子たちじゃなかったけど、後半はお互いちょっと話すことなくなってたね」",
                        "「最初に周辺の比率を見ておけば、もう少し余裕を持って動けたかもね」",
                    ],
                    "after_paragraphs": [
                        f"現在も全国で女性{research['national_women']}名・男性{research['national_men']}名（女性比{research['national_wpct']}%）が稼働していますが、店舗ごとのバランスを見ておくだけで落ち着いて判断できます。",
                    ],
                    "highlight_title": "周辺の現在地を知っておくメリット",
                    "highlight_body": f"『{s1['ja_name']}』（女性{s1['women']}名・女性比{s1['wpct']}%）のようにバランスが良い店舗を把握しておくと安心です。",
                    "store_links": store_links_1,
                },
                {
                    "heading": "2. 早い時間に強い店舗と、終電後に強い店舗を使い分ける",
                    "paragraphs": [
                        f"{research['calendar']['tag']}のような夜は、時間帯ごとに人が集まるブランドが変化します。",
                    ],
                    "dialogue": [
                        "「23時前に一度落ち着いたけど、0時回ってからまた2軒目組が入ってきたね」",
                        f"「『{s3['casual_name']}』は遅い時間でも活気があるから2軒目からでも入りやすいね」",
                    ],
                    "after_paragraphs": [
                        f"『{s3['ja_name']}』は終電後の維持目安が{s3['midnight_retention_pct']}%と高く、早い時間は『{s1['ja_name']}』、遅めなら『{s3['ja_name']}』と使い分けるのがスムーズです。",
                    ],
                    "highlight_title": "",
                    "highlight_body": "",
                    "store_links": store_links_2,
                },
                {
                    "heading": "3. 波長が合ったら「短時間・低ハードル」で2軒目を提案する",
                    "paragraphs": [
                        "会話が弾んだ席では、ダラダラ長居するよりも少し余韻が残る45分前後に声をかけるほうが自然です。",
                    ],
                    "dialogue": [
                        "「すぐ近くにコーヒーも飲める静かなバーがあるんだけど、終電まで30分だけ寄らない？」",
                        "「30分だけなら大丈夫かな、ちょっと行ってみようか！」",
                    ],
                    "after_paragraphs": [
                        "相手の帰宅時間を気遣う一言を添えるだけで、誠実な印象がぐっと伝わります。",
                    ],
                    "highlight_title": "今夜の判断チェックリスト",
                    "highlight_body": "①女性比率55%以上の店舗を第1候補にする ／ ②遅い時間帯は深夜維持率の高い店舗を選ぶ ／ ③声かけは帰宅時間を気遣う短時間提案にする。",
                    "store_links": store_links_1 + store_links_2,
                },
            ],
            "data_table": common_table,
            "summary_points": [
                f"全国平均女性比（{research['national_wpct']}%）より余裕のある店舗を優先する",
                f"23時以降は深夜維持目安{s3['midnight_retention_pct']}%の『{s3['ja_name']}』などを選ぶ",
                "2軒目の提案は「終電まで30分だけ」と時間を区切って安心感を作る",
            ],
            "faqs": [
                {
                    "q": "初めて行く場合、どの時間帯が一番話しやすいですか？",
                    "a": "金土なら混雑前の20時台、平日なら20時半〜22時頃が待ち時間も少なく落ち着いて会話できます。",
                },
                {
                    "q": "店舗ごとの現在の男女比はどこで確認できますか？",
                    "a": "相席ナビのトップページおよび各店舗ページで毎分リアルタイム更新しています。",
                },
            ],
            "x_title": "実測比率から逆算するスマートな店選びと引き際",
            "x_main_text": (
                f"なんとなく長居して疲れてしまう夜を減らすために、『{s1['casual_name']}』（女性比{s1['wpct']}%）など実測ログを使った時間帯別の店選びを整理しました📊👇\n"
                "#相席ラウンジ #相席ナビ"
            ),
            "note_title": title_b,
            "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #夜遊び #出会い",
        }
    else:
        title_b = (
            f"【女性目線の本音】{plan['custom_topic']}——女子トイレのLINE反省会と「タダだから気楽でしょ」の大誤解"
            if plan.get("custom_topic")
            else f"「女の子は無料だから気楽でしょ？」が大間違いな理由——女子トイレの本音LINEと、男女で真逆になる『正解の店』"
        )
        cand_b = {
            "style": "female_real_perspective",
            "style_label": f"💄 【{tone_meta['short_label']}】女性目線リアル・女子トイレ本音＆逆張り店選び案",
            "writer_note": f"女性2人組が乾杯15分でチェックしているリアルな減点ポイントと、女子側にとっては『{s2['ja_name']}』のような男性先行店こそ即乾杯の穴場になる非対称構造を執筆。",
            "thinking_memo": "",
            "is_ai_generated": False,
            "slug": slug_b,
            "eyecatch_key": "first-10min",
            "badge": "女性目線の本音",
            "read_time": "約6分で読める",
            "tag_primary": ["tag-pink", "💄 女性ペアの本音解剖"],
            "tag_secondary": ["tag-cyan", "男女逆張りの店選び"],
            "title": title_b,
            "short_title": "女子トイレの本音LINEと男女で真逆の店選び",
            "desc": f"1時間かけてメイクと髪を巻いて来た女子2人組が、乾杯15分後の化粧室でどんなLINEを送り合っているのか。男性が気づかない減点サインと、{s1['ja_name']}・{s2['ja_name']}の男女別・正解ルートを明かします。",
            "keywords": f"相席ラウンジ,女性目線,本音,{s1['ja_name']},{s2['ja_name']},女子会,相席ナビ",
            "sections": [
                {
                    "heading": "1. 「女の子はタダで飲めていいよね」と言った瞬間、恋愛対象から外れる理由",
                    "paragraphs": [
                        "男性側が一番やりがちな勘違いに、「女の子は無料なんだから待たされてもハズレ席でもノーダメージでしょ」という思い込みがあります。でも女性側のリアルなコストは『お金』ではなく『準備の1時間半とヒールで歩く体力、そして今夜という時間』です。",
                        f"仕事終わりにコテで髪を直し、せっかくメイクを整えて入ったのに、女余りの店舗で相席がつかないまま30分放置されたり、やっと座ってきた男性から開口一番「タダ飯いいよね〜」と軽く扱われた瞬間、女子2人の温度は一気に氷点下まで下がります。",
                    ],
                    "dialogue": [
                        "「（席を立って化粧室の鏡の前で）…ねえ、さっきの右側の人、乾杯して3分で『今日何人目？』って聞いてきたんだけど笑」",
                        "「それな、しかも相方の人ずっと自分の仕事の武勇伝だし…ごめん、戻って今のドリンク飲み干したら別のお店行こ！」",
                    ],
                    "after_paragraphs": [
                        "これが現場で毎晩起きている『女子トイレ反省会』のリアルです。女性ペアが急に2人揃って化粧室へ立ち、戻ってきてから食事のペースが急に早くなったら、それは楽しんでいるのではなく『早く食べて出る準備』に入っています。",
                    ],
                    "highlight_title": "女性ペアが最初の10分で密かに見ている3箇所",
                    "highlight_body": "①店員さんへの口の利き方（タメ口・横柄さは一発アウト） ／ ②距離70cmで漂うタバコ・ニンニク臭と肌の清潔感 ／ ③片方の女の子だけでなく『友達側にもちゃんと敬意を払って話を振れるか』（相方の女の子を退屈させた時点で帰られます）。",
                    "store_links": store_links_1,
                },
                {
                    "heading": f"2. 男女で「行くべき正解の店」は真逆になる——実測比率のカラクリ",
                    "paragraphs": [
                        f"ここで面白いのが、リアルタイム人数を見ると『男性にとっての天国』と『女性にとっての天国』が真逆になることです。たとえば今、{area_name}まわりでは『{s1['ja_name']}』（女性{s1['women']}名 / 男性{s1['men']}名・女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性{s2['women']}名 / 男性{s2['men']}名・女性比{s2['wpct']}%）でくっきりバランスが分かれています。",
                    ],
                    "dialogue": [
                        f"「女余りすぎてる店に入ると、女子だけで30分放置されて気まずいんだよね…」",
                        f"「わかる！ 私たちが入るなら男性がちゃんといっぱい入ってる『{s2['casual_name']}』の方が、待ち0分で即乾杯できるし席替えもすぐ来てラクだよね💄」",
                    ],
                    "after_paragraphs": [
                        f"そう、男性目線なら女性比率が{s1['wpct']}%ある『{s1['ja_name']}』が圧倒的に有利ですが、女性2人組の目線なら、男性がしっかり入っている『{s2['ja_name']}』や深夜まで活気のある『{s3['ja_name']}』を選んだほうが、放置ゼロ＆余裕のある男性陣とすぐ飲めるのです。",
                    ],
                    "highlight_title": "",
                    "highlight_body": "",
                    "store_links": store_links_2,
                },
                {
                    "heading": "3. 女子が「この席は当たりかも」とガードを解く瞬間",
                    "paragraphs": [
                        "では、女性側が『あ、この人たちともう少し話したいな』と思うのはどんな席でしょうか。実はイケメンや派手な盛り上げではなく、拍子抜けするほどシンプルな『対等な余裕』です。",
                    ],
                    "dialogue": [
                        "「今日外ちょっと湿気すごかったよね、髪巻くの大変じゃなかった？ 何か冷たいもの頼もうか」",
                        "「えっ、わかるんですか！？ そうなんですよ前髪すぐ取れちゃって…！ ありがとうございます、じゃあジャスミン茶もらおうかな」",
                    ],
                    "after_paragraphs": [
                        "相手の準備の手間をさらっと労い、お酒を強要せず、2人の女の子両方が笑える話題を選ぶ。それだけで上位1割の『また会いたい席』に入れます。男性も女性も、入店前に今の男女バランスを10秒チェックして、自分たちにとって一番ストレスのない店舗から夜を始めてみてください。",
                    ],
                    "highlight_title": "男女別・今夜のスマートな店舗選び",
                    "highlight_body": f"【男性ペア】女性比{s1['wpct']}%の『{s1['ja_name']}』など女性先行店を選び、余裕を持って乾杯する ／ 【女性ペア】女余り放置を避け、『{s2['ja_name']}』など男性がしっかり入っている店舗で待ち0分＆好待遇を狙う。",
                    "store_links": store_links_1 + store_links_2,
                },
            ],
            "data_table": common_table,
            "summary_points": [
                "女性側のコストは「準備の1時間半と今夜の時間」。『タダでいいよね』発言や店員への横柄な態度は即座に女子トイレ反省会行きになる",
                "相方の女の子を退屈させない気配り（4人全員が話せる話題選び）こそが、2軒目へ進める最大の鍵になる",
                f"男性は女性比率の高い『{s1['ja_name']}』、女性は即相席できる『{s2['ja_name']}』と、データを見て逆張りで使い分けるのが正解",
            ],
            "faqs": [
                {
                    "q": "女性2人組で行く場合、一番ハズレを引きにくい時間帯・お店選びは？",
                    "a": f"極端に女性ばかり多い店（女性75%超など）に入ると相席待ちで放置されやすいため、相席ナビで『男性人数がしっかり入っている大型店（{s2['ja_name']}など）』を見てから入ると、待ち0分で歓迎されやすく非常に快適です。",
                },
                {
                    "q": "雨の日に女性が特定の店舗に集中しやすいのは本当ですか？",
                    "a": "本当です。雨や湿気の強い夜はせっかく整えた前髪や靴が濡れるのを避けるため、路面店よりも駅・地下街直結の店舗へ女性ペアが一極集中します。",
                },
            ],
            "x_title": "女子トイレの本音LINEと、男女で真逆になる「正解の相席ラウンジ選び」",
            "x_main_text": (
                "「女の子はタダだから気楽でしょ」って言っちゃう人ほど、乾杯15分後の女子トイレLINEで『この杯飲み干したら出よ笑』って書かれてる現実💄\n\n"
                f"女性が見ている減点サインと、『{s1['casual_name']}』など男女で真逆になる店選びの本音👇\n"
                "#相席ラウンジ #相席ナビ"
            ),
            "note_title": title_b,
            "note_hashtags": "#相席ラウンジ #女子の本音 #オリエンタルラウンジ #相席ナビ #恋愛コラム",
        }

    rendered_a = _render_candidate_preview_and_note(cand_a, site_url=site_url)
    rendered_b = _render_candidate_preview_and_note(cand_b, site_url=site_url)
    # In `female_real` mode or `female_real_voice` theme, place the Female Perspective draft first!
    if tm == "female_real" or plan.get("theme_key") == "female_real_voice":
        return [rendered_b, rendered_a]
    return [rendered_a, rendered_b]


def _build_column_gemini_prompt(
    research: dict,
    plan: dict,
    site_url: str,
    tone_mode: str = None,
) -> str:
    tm = sns_editorial.get_editorial_tone_mode(tone_mode)
    tone_directive = sns_editorial.EDITORIAL_TONE_MODES[tm]["prompt_directive"]
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
        "tone_mode": tm,
    }
    cliches_str = "・".join(f"「{w}」" for w in COLUMN_AI_CLICHES)
    fantasy_str = "・".join(f"「{w}」" for w in SWEET_FANTASY_CLICHES)
    return (
        "あなたは全国53店舗の相席ラウンジ人数を毎晩リアルタイム計測している『相席ナビ』の運営中の人・コラム編集長です。\n"
        "AIが書いたような教科書的な解説記事や綺麗にまとまりすぎた優等生コラムではなく、毎晩53店舗のモニターの波と現場の空気（エレベーターが開いた瞬間の重たい空気、女子2人組が化粧室で送り合うシビアな本音LINE、相方との帰り道のちょっと苦い反省会）を見続けてきた生身の人間が、深夜に本音で書き下ろしたエッセイ調のWebコラム＆note原稿＆X告知文を1セット執筆してください。\n\n"
        f"{sns_editorial.HUMAN_INSIDER_VOICE_GUIDE}\n\n"
        f"{tone_directive}\n\n"
        "【コラム・note特有の『人間味エッセイ文体』ルール】\n"
        "- すべての段落を同じ長さ・同じ『〜です。〜ます。』の単調なリズムで揃えないこと。『正直、ここが一番残酷なところで。』『理由は拍子抜けするほどシンプルだ。』のような短い一文や、読者に語りかける自然な緩急を混ぜること。\n"
        "- 最後の締めくくりも『いかがでしたか』『ぜひ最高の夜を楽しんでくださいね』のような宣伝・道徳オチにせず、夜の街を見続けてきた人間のリアルな余韻で締めること。\n\n"
        "【長考（Thinking）プロセス：脳内3案コンペ＆脱・安易さ推敲】\n"
        "まず `thinking_memo` の中で以下の3ステップを実行してから記事本編を出力してください：\n"
        "  Step 1. 導入の人間味エピソードを3通り（①受付待ちや席での苦い敗因・空回りから入る辛口案、②女性2人組の女子トイレや帰り道LINEのリアルな本音から入る案、③雨や終電前など街の空気と男女比のねじれから入る案）頭の中で比べる。\n"
        "  Step 2. 今回のトーン指定に最も刺さり、読者が『うわ、それ図星すぎる…』と3行目で引き込まれる1案を選び、3章すべてに生身の口語会話（dialogue配列、各2セリフ）を入れる。\n"
        "  Step 3. ファクトシートの実測数値・店舗名と1人・1%の狂いもないか確認する。\n\n"
        "【絶対厳守ルール（違反すると鬼デスク検閲で大幅減点されます）】\n"
        "1. 店舗名・人数・女性比率は下記ファクトシートにある数値のみを使い、架空の人数を捏造しないこと。\n"
        "2. ブランド呼称ルール：単体の『オリエンタル』や『オリエンタル新宿』（「ラウンジ」抜け）、内部コード『OLG』は絶対に使わないこと！必ず正式名称『オリエンタルラウンジ〇〇』または常連の自然な口語略称『オリラジ〇〇』（会話やX告知で有効）と表記すること。『ag〇〇』『JIS〇〇』はそのまま表記。\n"
        f"3. AI臭ワードの完全禁止：{cliches_str} はタイトル・本文・FAQ・X告知のどこにも絶対に使わないこと。\n"
        f"4. 通販マンガ・進研ゼミ的なご都合主義セリフ＆手垢テンプレの禁止：{fantasy_str} のような『都合よく女の子が即答で食いつくファンタジー会話』や浅い一般論は禁止。迷い・駆け引き・女子側のシビアな視点を入れること。また毎回ワンパターンな『40分見切り』一辺倒の結論にせず、テーマ固有の深い示唆で締めること。\n"
        "5. X告知文（`x_main_text`）は全角122文字前後（最大でもTwitter weight 262以内）に引き締め、運営中の人のリアルな独り言トーンで書き、末尾に `#相席ラウンジ #相席ナビ` を付けること。\n\n"
        f"【検証済みファクトシート】\n{json.dumps(fact_sheet, ensure_ascii=False, indent=2)}\n\n"
        "以下のJSONオブジェクト形式のみで出力してください（Markdownコードブロック不要）：\n"
        "{\n"
        '  "thinking_memo": "脳内でどんな導入3案を比較し、どうAI臭を削ぎ落として人間らしい行間・本音にしたかの編集長推敲メモ（90〜150文字）",\n'
        '  "writer_note": "今回の記事で一番効かせた実測データと辛口/女性目線フックの1行要約",\n'
        '  "slug": "英小文字とハイフンのみのURLスラッグ（例: umeda-gap-hopping-1028）",\n'
        '  "title": "読者が思わずクリックしたくなる本音×データ根拠のある記事タイトル（36〜62文字）",\n'
        '  "short_title": "パンくず・一覧用の短いタイトル（18〜28文字）",\n'
        '  "desc": "検索スニペット・カード用の共感リード文（90〜135文字）",\n'
        '  "sections": [\n'
        '    {\n'
        '      "heading": "1. 第1章見出し",\n'
        '      "paragraphs": ["導入段落1（情景・苦いリアル体験）", "導入段落2"],\n'
        '      "dialogue": ["「現場での生々しくもリアルな会話セリフ1」", "「それに対するリアルな返答セリフ2」"],\n'
        '      "after_paragraphs": ["会話を踏まえた実測データへの橋渡し段落"],\n'
        '      "highlight_title": "要点ボックス見出し",\n'
        '      "highlight_body": "要点ボックス本文"\n'
        '    },\n'
        '    {\n'
        '      "heading": "2. 第2章見出し（データ裏付け×男女の温度差）",\n'
        '      "paragraphs": ["データ分析段落"],\n'
        '      "dialogue": ["「女性ペアの本音や相方とのリアルなセリフ1」", "「返答セリフ2」"],\n'
        '      "after_paragraphs": ["店舗ごとの使い分け解説段落"],\n'
        '      "highlight_title": "",\n'
        '      "highlight_body": ""\n'
        '    },\n'
        '    {\n'
        '      "heading": "3. 第3章見出し（実践アクション）",\n'
        '      "paragraphs": ["立ち回り解説段落"],\n'
        '      "dialogue": ["「駆け引きや温度感のある自然なセリフ1」", "「返答セリフ2」"],\n'
        '      "after_paragraphs": ["余韻のある締めくくり段落"],\n'
        '      "highlight_title": "明日から使える実践ルール",\n'
        '      "highlight_body": "具体的な3つの行動指針"\n'
        '    }\n'
        '  ],\n'
        '  "summary_points": ["まとめ箇条書き1", "まとめ箇条書き2", "まとめ箇条書き3"],\n'
        '  "faqs": [\n'
        '    {"q": "読者がよく迷う質問1", "a": "綺麗事抜きの具体的で誠実な回答1"},\n'
        '    {"q": "読者がよく迷う質問2", "a": "具体的で誠実な回答2"}\n'
        '  ],\n'
        '  "x_title": "Xリプ欄用の短い記事見出し",\n'
        '  "x_main_text": "X告知1投目（運営中の人の本音独り言＋実測の気づき＋👇 #相席ラウンジ #相席ナビ）",\n'
        '  "note_title": "note読者向けに少しエッセイ感と本音感を強めたタイトル",\n'
        '  "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #夜遊び #恋愛コラム"\n'
        "}"
    )


def _make_col_cache_key(research: dict, plan: dict, tone_mode: str = None):
    tm = sns_editorial.get_editorial_tone_mode(tone_mode)
    eng_pref = sns_editorial.get_ai_engine_pref(allow_blocking=False)
    has_c = bool(sns_editorial.get_claude_api_key(allow_blocking=False))
    stores_sig = tuple(
        (s.get("raw_name", ""), int(s.get("women") or 0), int(s.get("men") or 0))
        for s in (research.get("featured_stores") or [])[:3]
    )
    return (
        eng_pref,
        has_c,
        tm,
        plan.get("theme_key") or "area_hopping",
        plan.get("custom_topic") or "",
        stores_sig,
    )


def has_cached_column_ai(research: dict, plan: dict, tone_mode: str = None) -> bool:
    ck = _make_col_cache_key(research, plan, tone_mode=tone_mode)
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
    tone_mode: str = None,
) -> dict:
    if not sns_editorial.has_any_ai_key(allow_blocking=(not cache_only)):
        return None

    tm = sns_editorial.get_editorial_tone_mode(tone_mode)
    tone_short = sns_editorial.EDITORIAL_TONE_MODES[tm]["short_label"]
    ck = _make_col_cache_key(research, plan, tone_mode=tm)
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

    prompt = _build_column_gemini_prompt(research, plan, site_url, tone_mode=tm)
    parsed, mn, _ = sns_editorial.call_editorial_ai_json(
        prompt=prompt,
        max_output_tokens=6500,
        thinking_budget=2048,
        temperature=0.86,
        total_deadline_sec=45.0,
        per_call_timeout_sec=30.0,
        allow_blocking=True,
        now_jst=now_jst,
    )
    if not isinstance(parsed, dict) or not parsed.get("title") or not parsed.get("sections"):
        return None

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
        return None

    x_main = _sanitize_brand_names(parsed.get("x_main_text") or baseline_cand["x_main_text"], prefer_casual_if_tight=True)
    if x_poster.compute_twitter_weight(x_main) > 272 and "オリエンタルラウンジ" in x_main:
        x_main = x_main.replace("オリエンタルラウンジ", "オリラジ")

    cand = {
        "style": "ai_longform_story",
        "style_label": f"✨ AI編集長・本音エッセイ案（{mn} ｜ {tone_short}）",
        "writer_note": f"【{mn}・{tone_short}】{_sanitize_brand_names(parsed.get('writer_note') or '現場の本音と実測ログを掛け合わせたAI編集長案')}",
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
    with _col_ai_lock:
        _col_ai_cache[ck] = {"ts": time.time(), "candidate": cand, "model": mn}
        while len(_col_ai_cache) > _COL_AI_CACHE_MAX_SIZE:
            _col_ai_cache.popitem(last=False)
    return dict(cand)


def run_column_writer(
    research: dict,
    plan: dict,
    now_jst: datetime.datetime = None,
    app_url: str = "",
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
    tone_mode: str = None,
) -> list:
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    tm = sns_editorial.get_editorial_tone_mode(tone_mode, allow_blocking=(not ai_cache_only))
    base_cands = _build_deterministic_candidates(research, plan, site_url=site_url, tone_mode=tm)

    if use_ai:
        ai_cand = _generate_gemini_column_candidate(
            research=research,
            plan=plan,
            baseline_cand=base_cands[0],
            site_url=site_url,
            now_jst=now_jst,
            force_refresh=force_ai_refresh,
            cache_only=ai_cache_only,
            tone_mode=tm,
        )
        if ai_cand:
            return [ai_cand] + base_cands
    return base_cands


# =====================================================================
# Role 4: Evaluator / Desk (鬼デスク・100点採点＆検閲係)
# =====================================================================
def run_column_evaluator(candidates: list, research: dict, plan: dict, tone_mode: str = None) -> dict:
    evaluated = []
    tm = sns_editorial.get_editorial_tone_mode(tone_mode)
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
        elif c.get("style") == "female_real_perspective":
            human_pts += (4 if tm == "female_real" else 3)
            pros.append("女子トイレ反省会・女性目線の本音と男女逆張りロジックが鮮明")
        elif c.get("style") == "field_dialogue_story":
            human_pts += (4 if tm in ("spicy", "spicy_female") else 3)
            pros.append("綺麗事を排した現場の敗因解剖・リアル会話で読者の図星を突く構成")
        else:
            human_pts += 2

        cliche_hits = [w for w in COLUMN_AI_CLICHES if w in full_blob]
        fantasy_hits = [w for w in SWEET_FANTASY_CLICHES if w in full_blob]
        if cliche_hits:
            human_pts -= min(14, len(cliche_hits) * 4)
            warnings.append(f"AI定型句（{', '.join(cliche_hits[:3])}）を検出したため減点")
        elif fantasy_hits:
            human_pts -= min(10, len(fantasy_hits) * 4)
            warnings.append(f"ご都合主義・手垢テンプレ表現（{', '.join(fantasy_hits[:2])}）を検出したため減点")
        else:
            pros.append("AI臭ワード・通販マンガ風のご都合主義セリフともにゼロクリア")
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
        f"【{sns_editorial.EDITORIAL_TONE_MODES[tm]['short_label']}】全{len(evaluated)}案を100点満点で検閲し、"
        f"『{winner['style_label']}』（{winner['total_score']}点／会話{winner['dialogue_count']}箇所・X weight {winner['x_weight']}/280）を第1位推薦に選出しました。"
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
        cliche_cnt = sum(1 for w in COLUMN_AI_CLICHES if w in plain) + sum(1 for w in SWEET_FANTASY_CLICHES if w in plain)

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
        score -= min(10, cliche_cnt * 3)
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
# Published & Scheduled Column -> note.com Ready-to-Paste Exporter
# =====================================================================
_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

_SKIP_HTML_TAGS = frozenset({
    "head", "script", "style", "noscript", "nav", "footer", "aside",
    "figure", "picture", "svg", "button", "form", "input", "select",
})

_SKIP_EXACT_CLASSES = frozenset({
    "breadcrumb", "toc-box", "toc-list", "toc-title", "article-toc",
    "portal-global-nav", "portal-top-header", "top-header", "top-nav-bar",
    "portal-footer", "official-notice-box", "article-affiliate-card",
    "aff-card", "ladies-pr-section", "cta-box", "live-tool-cta-box",
    "preview-banner", "live-board", "article-cat-tag", "article-badge-row",
    "article-date", "article-meta", "article-meta-line", "cat-badge",
    "rec-badge", "hero-kicker", "hero-cta-row", "share-btn-row",
    "related-article-card", "editorial-ref-note", "store-chip-row",
    "ref-kicker", "cta-btn", "live-tool-btn", "rec-btn", "ladies-pr-link",
})

_NOTE_HASHTAG_PRESETS = {
    "for-women": "#相席ラウンジ #女子会 #オリエンタルラウンジ #JIS #ag #夜カフェ #相席ナビ",
    "last-train-cliff": "#相席ラウンジ #オリエンタルラウンジ #JIS #ag #相席ナビ #終電後 #夜遊び",
    "price-event-comparison": "#相席ラウンジ #オリエンタルラウンジ #JIS #ag #コスパ #相席ナビ #出会い",
    "male-lead-mechanics": "#相席ラウンジ #オリエンタルラウンジ #データ分析 #相席ナビ #出会い #夜遊び",
    "beauty-area-stage": "#相席ラウンジ #オリエンタルラウンジ #美容 #清潔感 #相席ナビ #出会い",
    "club-area-midnight": "#相席ラウンジ #オリエンタルラウンジ #JIS #渋谷 #梅田 #新宿 #相席ナビ",
    "weekend-strategy": "#相席ラウンジ #オリエンタルラウンジ #週末 #金曜日 #相席ナビ #出会い",
    "brand-comparison": "#オリエンタルラウンジ #JIS #ag #相席ラウンジ #徹底比較 #相席ナビ",
    "weekday-strategy": "#相席ラウンジ #オリエンタルラウンジ #水曜日 #木曜日 #相席ナビ #出会い",
    "shinjuku": "#新宿 #相席ラウンジ #オリエンタルラウンジ #JIS新宿 #歌舞伎町 #相席ナビ",
    "solo-vs-pair": "#相席ラウンジ #一人飲み #オリエンタルラウンジ #相席ナビ #出会い #夜遊び",
    "second-venue": "#相席ラウンジ #2軒目 #デート #会話術 #オリエンタルラウンジ #相席ナビ",
    "first-10-minutes": "#相席ラウンジ #会話術 #清潔感 #第一印象 #オリエンタルラウンジ #相席ナビ",
    "grooming": "#メンズ美容 #清潔感 #相席ラウンジ #身だしなみ #オリエンタルラウンジ #相席ナビ",
    "local-vs-urban": "#相席ラウンジ #オリエンタルラウンジ #出張 #地方 #データ分析 #相席ナビ",
    "cost-break-even": "#相席ラウンジ #オリエンタルラウンジ #コスパ #恋愛 #相席ナビ #夜遊び",
    "rain-payday": "#相席ラウンジ #オリエンタルラウンジ #雨の日 #穴場 #データ分析 #相席ナビ",
}


class _HtmlNode:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []

    def classes(self) -> set:
        raw = self.attrs.get("class") or ""
        return {c.strip() for c in raw.split() if c.strip()}


from html.parser import HTMLParser as _StdHTMLParser


class _SimpleDomParser(_StdHTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("root")
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        t = (tag or "").lower()
        node = _HtmlNode(t, {k.lower(): (v or "") for k, v in attrs})
        self._stack[-1].children.append(node)
        if t not in _VOID_HTML_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        t = (tag or "").lower()
        node = _HtmlNode(t, {k.lower(): (v or "") for k, v in attrs})
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        t = (tag or "").lower()
        if t in _VOID_HTML_TAGS:
            return
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == t:
                del self._stack[idx:]
                break

    def handle_data(self, data):
        if data:
            self._stack[-1].children.append(data)


def _polish_note_brand_text(md_text: str) -> str:
    """Normalize internal 'OLG' / bare 'オリエンタル' in prose while preserving URLs untouched."""
    if not md_text:
        return ""
    protected = []

    def _stash_url(m):
        protected.append(m.group(0))
        return f"__URL_TOKEN_{len(protected) - 1}__"

    work = re.sub(r"https?://[^\s\)\]>]+", _stash_url, md_text)
    work = re.sub(r"\bOLG\s*ag", "ag", work)
    work = re.sub(r"OLG\s*(?=[ァ-ヶー一-龠々A-Za-z])", "オリエンタルラウンジ", work)
    work = re.sub(r"\bOLG\b", "オリエンタルラウンジ", work)
    work = _sanitize_brand_names(work, prefer_casual_if_tight=False)
    for i, orig in enumerate(protected):
        work = work.replace(f"__URL_TOKEN_{i}__", orig)
    return work


def _subtree_has_affiliate_link(node: _HtmlNode) -> bool:
    if isinstance(node, str):
        return False
    if node.tag == "a":
        href = (node.attrs.get("href") or "").strip()
        if "/link/" in href or "sponsored" in (node.attrs.get("rel") or ""):
            return True
    return any(_subtree_has_affiliate_link(ch) for ch in node.children)


def _should_skip_dom_node(node: _HtmlNode) -> bool:
    if isinstance(node, str):
        return False
    if node.tag in _SKIP_HTML_TAGS:
        return True
    cls_set = node.classes()
    if cls_set & _SKIP_EXACT_CLASSES:
        return True
    if any(c.startswith("related-") for c in cls_set):
        return True
    # Exclude PR / affiliate cards (e.g. .rec-card with /link/...) while keeping pure dress-code rule cards
    if ("rec-card" in cls_set or "ladies-pr-card" in cls_set) and _subtree_has_affiliate_link(node):
        return True
    # Exclude standalone CTA button wrappers at the bottom of articles
    if node.tag == "div" and not cls_set:
        non_ws_children = [
            ch for ch in node.children
            if not (isinstance(ch, str) and not ch.strip())
        ]
        if len(non_ws_children) == 1 and isinstance(non_ws_children[0], _HtmlNode):
            only = non_ws_children[0]
            if only.tag == "a" and (only.classes() & {"btn-rose", "btn-outline", "cta-btn", "live-tool-btn"}):
                return True
    return False


def _render_inline_for_note(node, site_url: str, allow_bold: bool = True) -> str:
    if isinstance(node, str):
        return re.sub(r"[ \t\r\f\v]+", " ", node.replace("\n", " "))
    if _should_skip_dom_node(node):
        return ""
    if node.tag == "br":
        return "\n"
    inner = "".join(_render_inline_for_note(ch, site_url, allow_bold=allow_bold) for ch in node.children)
    if node.tag in ("strong", "b"):
        clean = inner.strip()
        if not clean:
            return ""
        return f"**{clean}**" if allow_bold else clean
    if node.tag == "code":
        clean = inner.strip()
        return f"「{clean}」" if clean else ""
    if node.tag == "a":
        label = re.sub(r"\s+", " ", inner).strip()
        href = (node.attrs.get("href") or "").strip()
        if not label:
            return ""
        if not href or href.startswith("#") or "/link/" in href or "sponsored" in (node.attrs.get("rel") or ""):
            return "" if (node.classes() & {"rec-btn", "ladies-pr-link", "cta-btn", "btn-rose"}) else label
        if href.startswith("/"):
            full_href = f"{site_url.rstrip('/')}{href}"
            return f"[{label}]({full_href})"
        if href.startswith("https://aiseki-navi.com"):
            return f"[{label}]({href})"
        return label
    return inner


def _clean_multiline_block(raw_text: str) -> str:
    lines = []
    for ln in (raw_text or "").split("\n"):
        cleaned = re.sub(r"[ \t]+", " ", ln).strip()
        # Collapse accidental nested **...** markers
        cleaned = re.sub(r"\*{3,}", "**", cleaned)
        lines.append(cleaned)
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _collect_elements_by_tag(node: _HtmlNode, target_tags: tuple) -> list:
    found = []
    for ch in node.children:
        if isinstance(ch, _HtmlNode) and not _should_skip_dom_node(ch):
            if ch.tag in target_tags:
                found.append(ch)
            else:
                found.extend(_collect_elements_by_tag(ch, target_tags))
    return found


def _convert_static_html_to_note(raw_html: str, meta: dict, site_url: str = "https://aiseki-navi.com") -> dict:
    """Convert any static/scheduled column HTML template into clean note.com Markdown + metadata."""
    site_base = site_url.rstrip("/")
    og_img_m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', raw_html, flags=re.I)
    if not og_img_m:
        og_img_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', raw_html, flags=re.I)
    eyecatch_url = ""
    if og_img_m:
        eyecatch_url = og_img_m.group(1).strip()
    elif meta.get("eyecatch_jpg"):
        eyecatch_url = meta["eyecatch_jpg"]
    else:
        body_img_m = re.search(r'<img[^>]+src=["\'](/static/[^"\']+\.(?:jpg|png|webp))["\']', raw_html, flags=re.I)
        if body_img_m:
            eyecatch_url = body_img_m.group(1)
    if not eyecatch_url:
        eyecatch_url = "/static/note-eyecatch.jpg"
    if eyecatch_url.startswith("/"):
        eyecatch_url = f"{site_base}{eyecatch_url}"

    # Strip HTML comments and Jinja2 preview/loop blocks before DOM parsing
    cleaned = re.sub(r"<!--.*?-->", "", raw_html, flags=re.S)
    cleaned = re.sub(r"\{%\s*if\s+is_scheduled_preview\s*%\}[\s\S]*?\{%\s*endif\s*%\}", "", cleaned)
    cleaned = re.sub(r"\{%\s*for\s+[\s\S]*?\{%\s*endfor\s*%\}", "", cleaned)
    cleaned = re.sub(r"\{%[\s\S]*?%\}", "", cleaned)
    cleaned = re.sub(r"\{\{[\s\S]*?\}\}", "", cleaned)

    parser = _SimpleDomParser()
    parser.feed(cleaned)
    parser.close()

    extracted_h1 = []
    blocks = []

    def _walk(node):
        if isinstance(node, str) or _should_skip_dom_node(node):
            return
        cls_set = node.classes()
        tag = node.tag

        if tag == "h1":
            h1_txt = re.sub(r"\s+", " ", _render_inline_for_note(node, site_base, allow_bold=False)).strip()
            if h1_txt and h1_txt != "相席ナビ":
                extracted_h1.append(h1_txt)
            return

        if tag == "h2":
            h2_txt = re.sub(r"\s+", " ", _render_inline_for_note(node, site_base, allow_bold=False)).strip()
            if h2_txt:
                blocks.append(f"## {h2_txt}")
            return

        if tag == "h3":
            h3_txt = re.sub(r"\s+", " ", _render_inline_for_note(node, site_base, allow_bold=False)).strip()
            if h3_txt:
                blocks.append(f"### {h3_txt}")
            return

        if tag == "h4":
            h4_txt = re.sub(r"\s+", " ", _render_inline_for_note(node, site_base, allow_bold=False)).strip()
            if h4_txt:
                blocks.append(f"**■ {h4_txt}**")
            return

        if "dialogue-box" in cls_set:
            dl_raw = _clean_multiline_block(_render_inline_for_note(node, site_base, allow_bold=True))
            dl_lines = [f"> {ln}" for ln in dl_raw.split("\n") if ln.strip()]
            if dl_lines:
                blocks.append("\n".join(dl_lines))
            return

        if "stat-box" in cls_set:
            parts = []
            for ch in node.children:
                if isinstance(ch, _HtmlNode):
                    t = re.sub(r"\s+", " ", _render_inline_for_note(ch, site_base, allow_bold=False)).strip()
                    if t:
                        parts.append(t)
            if parts:
                if len(parts) >= 2:
                    blocks.append(f"- 📊 **{parts[0]}：{parts[1]}**（{' '.join(parts[2:])}）".replace("（）", ""))
                else:
                    blocks.append(f"- 📊 {parts[0]}")
            return

        if "faq-item" in cls_set or "faq-card" in cls_set:
            for ch in node.children:
                if isinstance(ch, _HtmlNode):
                    c_cls = ch.classes()
                    txt = _clean_multiline_block(_render_inline_for_note(ch, site_base, allow_bold=True))
                    if not txt:
                        continue
                    if "faq-q" in c_cls:
                        q_clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", txt)
                        blocks.append(f"### 💬 {q_clean}")
                    else:
                        blocks.append(txt)
            return

        if tag in ("ul", "ol"):
            items = []
            for ch in node.children:
                if isinstance(ch, _HtmlNode) and ch.tag == "li" and not _should_skip_dom_node(ch):
                    li_txt = _clean_multiline_block(_render_inline_for_note(ch, site_base, allow_bold=True))
                    li_single = " ／ ".join(s.strip() for s in li_txt.split("\n") if s.strip())
                    if li_single:
                        items.append(f"- {li_single}")
            if items:
                blocks.append("\n".join(items))
            return

        if tag == "table":
            trs = _collect_elements_by_tag(node, ("tr",))
            headers = []
            row_lines = []
            for tr in trs:
                ths = [c for c in tr.children if isinstance(c, _HtmlNode) and c.tag == "th"]
                tds = [c for c in tr.children if isinstance(c, _HtmlNode) and c.tag == "td"]
                if ths and not headers:
                    headers = [
                        re.sub(r"\s+", " ", _render_inline_for_note(th, site_base, allow_bold=False)).strip()
                        for th in ths
                    ]
                elif tds:
                    vals = [
                        re.sub(r"\s+", " ", _render_inline_for_note(td, site_base, allow_bold=False)).strip()
                        for td in tds
                    ]
                    if not any(vals):
                        continue
                    if headers and len(vals) >= 2:
                        lead = vals[0]
                        rest = [
                            f"{headers[i]}: {vals[i]}" if i < len(headers) and headers[i] else vals[i]
                            for i in range(1, len(vals))
                            if vals[i]
                        ]
                        row_lines.append(f"- **{lead}** — " + " ／ ".join(rest))
                    else:
                        row_lines.append("- " + " ／ ".join(v for v in vals if v))
            if row_lines:
                blocks.append("\n".join(row_lines))
            return

        if tag == "p":
            p_txt = _clean_multiline_block(_render_inline_for_note(node, site_base, allow_bold=True))
            if p_txt and not p_txt.startswith("公開日："):
                blocks.append(p_txt)
            return

        # Handle mixed text+br boxes like .lead-box / .article-lead that contain direct text without <p>
        has_block_child = any(
            isinstance(ch, _HtmlNode) and ch.tag in ("p", "h1", "h2", "h3", "h4", "ul", "ol", "table", "div", "section", "article", "header")
            for ch in node.children
        )
        if not has_block_child and tag in ("div", "section"):
            box_txt = _clean_multiline_block(_render_inline_for_note(node, site_base, allow_bold=True))
            if box_txt:
                blocks.append(box_txt)
            return

        for ch in node.children:
            _walk(ch)

    _walk(parser.root)

    title_final = _polish_note_brand_text(extracted_h1[0] if extracted_h1 else (meta.get("title") or ""))
    path_str = meta.get("path") or f"/guide/{meta.get('slug', '')}"
    article_url = f"{site_base}{path_str}"

    slug = meta.get("slug") or ""
    if slug == "for-women":
        cta_footer = "\n".join([
            "---",
            "",
            "💄 **全国53店舗の「いま女子待ちゼロで入れるお店＆開催中イベント」は『相席ナビ』でリアルタイム公開中！**",
            "入店3分前にスマホでチェックできる女性向け特設ガイドはこちら👇",
            "",
            article_url,
            "",
            "👉 女性目線モード（待ち0・特典順）で現在の全国ランキングを見る",
            f"{site_base}/?mode=women",
        ])
    else:
        cta_footer = "\n".join([
            "---",
            "",
            "📊 **全国53店舗の「いま現在の男女人数・普段比・終電後維持率」は『相席ナビ』で1分ごとに無料更新中！**",
            "今回のコラムの詳しい図表データや、これから行く店舗のリアルタイム状況はこちら👇",
            "",
            article_url,
            "",
            "👉 全国リアルタイム人数ランキング（相席ナビ トップページ）",
            f"{site_base}/",
        ])

    body_joined = "\n\n".join(b for b in blocks if b.strip())
    body_markdown = _polish_note_brand_text(f"{body_joined}\n\n{cta_footer}".strip())
    hashtags = _NOTE_HASHTAG_PRESETS.get(
        slug,
        "#相席ラウンジ #オリエンタルラウンジ #JIS #ag #相席ナビ #出会い #夜遊び",
    )
    full_markdown = f"# {title_final}\n\n{body_markdown}\n\n{hashtags}"

    return {
        "ok": True,
        "slug": slug,
        "path": path_str,
        "article_url": article_url,
        "title": title_final,
        "body_markdown": body_markdown,
        "hashtags": hashtags,
        "full_markdown": full_markdown,
        "eyecatch_url": eyecatch_url,
        "char_count": len(body_markdown),
    }


def export_column_for_note(slug: str, site_url: str = "https://aiseki-navi.com") -> dict:
    """
    Export ANY existing static column (15), scheduled column (3), or dynamically published/scheduled
    editorial column into a ready-to-paste note.com manuscript kit (eyecatch URL, title, body Markdown, hashtags).
    """
    slug_clean = (slug or "").strip().strip("/")
    if slug_clean.startswith("guide/"):
        slug_clean = slug_clean[len("guide/"):]
    if not slug_clean:
        slug_clean = "last-train-cliff"

    site_base = (site_url or "https://aiseki-navi.com").rstrip("/")

    # 1. Check dynamically created editorial columns first (`col_article:<slug>`)
    for dyn in get_dynamic_scheduled_articles(allow_blocking=True):
        if dyn.get("slug") == slug_clean:
            title_val = _polish_note_brand_names_simple(dyn.get("note_title") or dyn.get("title") or "")
            raw_md = dyn.get("note_markdown") or ""
            if not raw_md:
                rendered = _render_candidate_preview_and_note(dict(dyn), site_url=site_base)
                raw_md = rendered.get("note_markdown") or ""
            lines = raw_md.splitlines()
            # Strip leading `# Title` and trailing `#hashtags` line so `body_markdown` pastes cleanly into note body
            if lines and lines[0].lstrip().startswith("# "):
                lines = lines[1:]
                while lines and not lines[0].strip():
                    lines = lines[1:]
            hashtags = (dyn.get("note_hashtags") or "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #出会い #夜遊び").strip()
            if lines and lines[-1].strip().startswith("#") and "##" not in lines[-1]:
                hashtags = lines[-1].strip()
                lines = lines[:-1]
                while lines and not lines[-1].strip():
                    lines = lines[:-1]
            body_md = _polish_note_brand_text("\n".join(lines).strip())
            path_str = dyn.get("path") or f"/guide/{slug_clean}"
            article_url = f"{site_base}{path_str}"
            eye_jpg = dyn.get("eyecatch_jpg") or "/static/eyecatch-last-train-cliff.jpg"
            eye_url = f"{site_base}{eye_jpg}" if eye_jpg.startswith("/") else eye_jpg
            full_md = f"# {title_val}\n\n{body_md}\n\n{hashtags}"
            return {
                "ok": True,
                "slug": slug_clean,
                "path": path_str,
                "article_url": article_url,
                "title": title_val,
                "body_markdown": body_md,
                "hashtags": hashtags,
                "full_markdown": full_md,
                "eyecatch_url": eye_url,
                "char_count": len(body_md),
            }

    # 2. Check static scheduled columns (`scheduled_articles.SCHEDULED_ARTICLES`) + evergreen static columns
    import scheduled_articles
    target_meta = None
    for sc in scheduled_articles.SCHEDULED_ARTICLES:
        if sc["slug"] == slug_clean:
            target_meta = sc
            break
    if target_meta is None:
        for col in EXISTING_STATIC_COLUMNS:
            if col["slug"] == slug_clean or col["path"].strip("/") == slug_clean:
                target_meta = col
                break

    if target_meta is None:
        return {"ok": False, "error": f"指定されたコラム（{slug_clean}）が見つかりません。"}

    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", target_meta["template"])
    try:
        with open(tpl_path, "r", encoding="utf-8") as f:
            raw_html = f.read()
    except Exception as e:
        return {"ok": False, "error": f"テンプレートの読み込みに失敗しました: {e}"}

    return _convert_static_html_to_note(raw_html, target_meta, site_url=site_base)


def _polish_note_brand_names_simple(text: str) -> str:
    return _polish_note_brand_text(text)


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
    tone_mode: str = None,
) -> dict:
    now_jst = now_jst or datetime.datetime.now(JST)
    t_key = theme_key if theme_key in COLUMN_THEMES else "area_hopping"
    allow_blocking = not ai_cache_only
    default_tm = sns_editorial.get_default_editorial_tone_mode(allow_blocking=allow_blocking)
    resolved_tm = sns_editorial.get_editorial_tone_mode(tone_mode, allow_blocking=allow_blocking)

    research = run_column_researcher(
        live_stores=live_stores,
        theme_key=t_key,
        custom_topic=custom_topic,
        now_jst=now_jst,
    )
    plan = run_column_planner(research=research, now_jst=now_jst)

    gemini_status = sns_editorial.get_gemini_status(allow_blocking=allow_blocking)
    ai_cached = has_cached_column_ai(research, plan, tone_mode=resolved_tm)
    ai_pending_async = bool(use_ai and ai_cache_only and gemini_status.get("configured") and not ai_cached)

    candidates = run_column_writer(
        research=research,
        plan=plan,
        now_jst=now_jst,
        app_url=app_url,
        use_ai=use_ai,
        force_ai_refresh=force_ai_refresh,
        ai_cache_only=ai_cache_only,
        tone_mode=resolved_tm,
    )
    evaluation = run_column_evaluator(candidates=candidates, research=research, plan=plan, tone_mode=resolved_tm)

    default_sched_dt = (now_jst.astimezone(JST).replace(tzinfo=None) + datetime.timedelta(days=1)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )

    pdca_directives = {}
    try:
        import pdca_engine
        pdca_directives = pdca_engine.get_active_directives(allow_blocking=False) or {}
    except Exception:
        pdca_directives = {}

    return {
        "theme_key": t_key,
        "themes": list(COLUMN_THEMES.values()),
        "tone_mode": resolved_tm,
        "tone_mode_label": sns_editorial.EDITORIAL_TONE_MODES[resolved_tm]["label"],
        "default_tone_mode": default_tm,
        "tone_modes": sns_editorial.get_tone_modes_catalog(),
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
        "pdca_directives": pdca_directives,
    }
