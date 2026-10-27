"""
scheduled_articles.py - Time-gated (scheduled) editorial column publishing & X auto-release manager.

Articles registered here remain invisible to public visitors, `/columns`, `/`, `/sitemap.xml`,
and Google crawlers (returning HTTP 404 to non-staff) until `publish_at` (JST) arrives.
Logged-in staff (`/staff`) can preview upcoming scheduled articles at any time.
Once `publish_at` arrives:
  1. `/guide/<slug>` automatically goes live (HTTP 200).
  2. `/columns`, `/`, and `/sitemap.xml` automatically list the article.
  3. APScheduler automatically posts a 2-tweet release announcement thread to X (once per slug via `job_state`).
"""

import datetime
import logging

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

# Ordered by publish_at ascending (get_published_articles returns newest-published first)
SCHEDULED_ARTICLES = [
    {
        "slug": "local-vs-urban",
        "path": "/guide/local-vs-urban",
        "template": "guide_local_vs_urban.html",
        "publish_at": "2026-10-28 19:00:00",
        "publish_at_ja": "2026年10月28日(水) 19:00",
        "lastmod": "2026-10-28",
        "eyecatch_jpg": "/static/eyecatch-local-vs-urban.jpg",
        "eyecatch_webp": "/static/eyecatch-local-vs-urban.webp",
        "badge": "地方vs都心データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-cyan", "📊 全国51店舗ログ比較"),
        "tag_secondary": ("tag-pink", "深夜0時の逆転現象"),
        "title": "地方店と都心大型店で客層・ピークはどう違う？深夜0時に女性が121%へ逆転増加する「一極集中」の法則",
        "short_title": "地方店vs都心大型店の客層・深夜ピーク比較",
        "desc": "「出張先で20時に行ったら空いていたのに、居酒屋帰りの23時半には満席近かった」——都心33店と真逆に動く地方18店の深夜121%逆転現象と、現地で自然に盛り上がる会話のコツ。",
        "x_title": "地方店 vs 都心大型店｜深夜0時に女性が121%へ逆転増加する理由",
        "x_main_text": (
            "東京の感覚で出張先の相席ラウンジに20時に行ったらまだ静かだったのに、居酒屋帰りの23時半に覗いたら女の子だらけだった夜の話✈️\n\n"
            "実は地方18店は終電の壁が薄く、深夜0〜2時に女性が【121.1%】へ逆転増します📊\n\n"
            "熊本・沖縄・金沢・仙台などの狙い目はリプ欄へ👇\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "cost-break-even",
        "path": "/guide/cost-break-even",
        "template": "guide_cost_break_even.html",
        "publish_at": "2026-11-01 12:30:00",
        "publish_at_ja": "2026年11月1日(日) 12:30",
        "lastmod": "2026-11-01",
        "eyecatch_jpg": "/static/eyecatch-cost-break-even.jpg",
        "eyecatch_webp": "/static/eyecatch-cost-break-even.webp",
        "badge": "料金×損益分岐点",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-amber", "💰 滞在時間×コスパ検証"),
        "tag_secondary": ("tag-cyan", "40分見切りの法則"),
        "title": "微妙な席で90分粘る vs 40分で見切る｜男性の滞在時間とトータル料金・コスパの「損益分岐点」シミュレーション",
        "short_title": "滞在時間とトータル料金の損益分岐点",
        "desc": "「悪い子じゃないけど話が広がらない…」と気を使って100分居座り、改札で1万円近いレシートを見て相方と苦笑いした夜から学ぶ、徒歩5分圏の男女比ギャップと「40分見切り」の損益分岐点。",
        "x_title": "90分粘る vs 40分で見切る｜相席ラウンジ料金と勝率の損益分岐点",
        "x_main_text": (
            "「悪い子じゃないけど話が広がらないな…」と気を使って100分いて、1人1万近い会計を見て駅の改札で相方と苦笑いした夜、たぶん誰にでもあると思う💸\n\n"
            "徒歩4分の別店舗と女性比が15%違う街で一番賢い「40分見切り」の損益分岐点を書きました👇\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "rain-payday",
        "path": "/guide/rain-payday",
        "template": "guide_rain_payday.html",
        "publish_at": "2026-11-05 19:00:00",
        "publish_at_ja": "2026年11月5日(木) 19:00",
        "lastmod": "2026-11-05",
        "eyecatch_jpg": "/static/eyecatch-rain-payday.jpg",
        "eyecatch_webp": "/static/eyecatch-rain-payday.webp",
        "badge": "天候・給料日データ",
        "read_time": "約5分で読める",
        "tag_primary": ("tag-pink", "☔ 天候・カレンダー検証"),
        "tag_secondary": ("tag-cyan", "給料日前の穴場法則"),
        "title": "雨の日・給料日前後・連休中日で男女比はどう崩れる？9.6万件ログで見つけた「ライバルが減る隠れ穴場日」",
        "short_title": "雨の日・給料日前後・連休中日の客足データ",
        "desc": "25日の給料日直後に張り切って行くと男性待ちだらけなのに、15〜24日の給料日前や雨の地下直結店にふらっと寄ると驚くほどすぐ座れる理由を、9.6万件のログと現場の肌感覚で紐解きます。",
        "x_title": "雨の日・給料日前・連休中日｜データが証明する「隠れ穴場日」の法則",
        "x_main_text": (
            "25日の給料日直後に張り切って行くと男性待ちだらけなのに、「15〜24日の給料日前」にふらっと寄るとライバルが減って女性比62.1%（+7.4pt）まで上がる不思議📊\n\n"
            "雨の日の地下直結店や3連休中日など、実は一番勝てる隠れ穴場日の話👇\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
]


def now_jst_naive() -> datetime.datetime:
    return datetime.datetime.now(JST).replace(tzinfo=None)


def _coerce_naive_jst(now_jst: datetime.datetime = None) -> datetime.datetime:
    if now_jst is None:
        now_jst = now_jst_naive()
    if now_jst is not None and now_jst.tzinfo is not None:
        now_jst = now_jst.astimezone(JST).replace(tzinfo=None)
    return now_jst


def parse_publish_at(publish_at_str: str) -> datetime.datetime:
    return datetime.datetime.strptime(publish_at_str, "%Y-%m-%d %H:%M:%S")


def get_article_by_slug(slug: str) -> dict:
    slug_clean = (slug or "").strip("/")
    for item in SCHEDULED_ARTICLES:
        if item["slug"] == slug_clean:
            return item
    return None


def is_published(slug_or_item, now_jst: datetime.datetime = None) -> bool:
    now_jst = _coerce_naive_jst(now_jst)
    item = slug_or_item if isinstance(slug_or_item, dict) else get_article_by_slug(str(slug_or_item))
    if not item:
        return True  # Non-scheduled legacy articles are already public
    return now_jst >= parse_publish_at(item["publish_at"])


def get_published_articles(now_jst: datetime.datetime = None) -> list:
    """Return scheduled articles whose publish_at has arrived, sorted newest first."""
    now_jst = _coerce_naive_jst(now_jst)
    published = [item for item in SCHEDULED_ARTICLES if is_published(item, now_jst=now_jst)]
    published.sort(key=lambda x: x["publish_at"], reverse=True)
    return published


def get_upcoming_articles(now_jst: datetime.datetime = None) -> list:
    """Return scheduled articles waiting for release, sorted soonest first."""
    now_jst = _coerce_naive_jst(now_jst)
    upcoming = [item for item in SCHEDULED_ARTICLES if not is_published(item, now_jst=now_jst)]
    upcoming.sort(key=lambda x: x["publish_at"])
    return upcoming


def get_all_with_status(now_jst: datetime.datetime = None) -> list:
    """Return all scheduled articles with live `published` boolean flag for `/staff` dashboard."""
    now_jst = _coerce_naive_jst(now_jst)
    out = []
    for item in sorted(SCHEDULED_ARTICLES, key=lambda x: x["publish_at"]):
        copy_item = dict(item)
        copy_item["published"] = is_published(item, now_jst=now_jst)
        out.append(copy_item)
    return out


def check_and_announce_newly_published(app_url: str = "", now_jst: datetime.datetime = None) -> list:
    """
    Called periodically by APScheduler.
    If an article's `publish_at` has arrived (within the last 72h) and hasn't been announced on X yet,
    posts its 2-tweet release thread and records `col_release:<slug>` in `job_state`.
    """
    import os
    import job_state
    import x_poster

    now_jst = _coerce_naive_jst(now_jst)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    results = []

    for item in SCHEDULED_ARTICLES:
        pub_dt = parse_publish_at(item["publish_at"])
        if now_jst < pub_dt:
            continue
        # Safety window: only auto-announce within 72 hours of scheduled release
        age_hours = (now_jst - pub_dt).total_seconds() / 3600.0
        if age_hours > 72.0:
            continue

        dedup_key = f"col_release:{item['slug']}"
        if job_state.is_done(dedup_key):
            continue

        reply_text = (
            f"📖 新しいコラムを書きました👇\n"
            f"『{item['x_title']}』\n"
            f"{site_url}{item['path']}"
        )
        media_bytes, media_mime = x_poster.load_column_eyecatch_bytes(item)
        res = x_poster.post_to_x(
            item["x_main_text"],
            reply_text=reply_text,
            media_bytes=media_bytes,
            media_mime_type=media_mime,
        )
        if res.get("status") == "success":
            job_state.mark_done(dedup_key)
            job_state.mark_done(f"x_col_used:{now_jst.strftime('%Y-%m-%d')}:{item['path']}")
            logger.info(f"[scheduled_articles] Announced newly released column on X: {item['slug']}")
        results.append({"slug": item["slug"], "result": res})

    return results
