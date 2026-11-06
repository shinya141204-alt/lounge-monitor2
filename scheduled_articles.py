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
        "tag_primary": ("tag-cyan", "全国51店舗ログ比較"),
        "tag_secondary": ("tag-pink", "深夜0時の逆転現象"),
        "title": "地方店と都心の大型店、客層とピークはどう違うのか｜深夜0時に女性が121%へ増える地方の一極集中",
        "short_title": "地方店と都心大型店の客層・深夜ピーク比較",
        "desc": "「出張先で20時に行ったら空いていたのに、居酒屋帰りの23時半には満席近かった」。都心33店とは逆の動きを見せる地方18店の深夜121%の伸びと、現地で自然に会話が弾むコツ。",
        "x_title": "地方店と都心大型店｜深夜0時に女性が121%へ増える理由",
        "x_main_text": (
            "東京の感覚で出張先の相席ラウンジに20時に行ったら空いていて、居酒屋終わりの23時半に覗いたら女性グループで満席近かった。\n\n"
            "地方18店は終電の壁が薄く、深夜0〜2時に女性が121.1%まで増える。\n\n"
            "地元の人の過ごし方と狙い目はリプ欄で。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "osaka-kita-vs-minami",
        "path": "/guide/osaka-kita-vs-minami",
        "template": "guide_osaka_kita_vs_minami.html",
        "publish_at": "2026-10-31 19:00:00",
        "publish_at_ja": "2026年10月31日(土) 19:00",
        "lastmod": "2026-10-31",
        "eyecatch_jpg": "/static/eyecatch-osaka-kita-vs-minami.jpg",
        "eyecatch_webp": "/static/eyecatch-osaka-kita-vs-minami.webp",
        "badge": "大阪9店舗データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-amber", "大阪全9店舗・27万件ログ"),
        "tag_secondary": ("tag-cyan", "キタvsミナミ南北ハシゴ"),
        "title": "大阪キタ（梅田・茶屋町・天満）とミナミ（難波・心斎橋）全9店舗を27万件ログで比べる｜時間帯で入れ替わる南北のハシゴ",
        "short_title": "梅田キタと難波ミナミ｜大阪全9店舗の南北ハシゴ",
        "desc": "「梅田の東通りで45分待ちと言われた夜、そのまま粘るか御堂筋線で難波へ降りるか」。20時の茶屋町（女子率86%）、22時の裏天満、24時以降のJIS梅田と難波へと主役が入れ替わる全9店舗のデータ。",
        "x_title": "梅田キタと難波ミナミ｜大阪全9店舗の南北ハシゴ",
        "x_main_text": (
            "梅田の1軒目で「45分待ち」と言われた夜。東通りで粘るか、御堂筋線で9分南へ降りるかで、その後の流れがまるで変わる。\n\n"
            "20時は茶屋町（女子率86%）、深夜0時以降はJIS梅田と難波に主役が移る。\n\n"
            "全9店の南北の回り方はリプ欄にまとめました。\n"
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
        "tag_primary": ("tag-amber", "滞在時間×コスパ検証"),
        "tag_secondary": ("tag-cyan", "40分見切りの法則"),
        "title": "微妙な席で90分粘るか、40分で切り上げるか｜滞在時間とトータル料金の損益分岐点",
        "short_title": "滞在時間とトータル料金の損益分岐点",
        "desc": "「気を遣って90分延長したのに、女性側も化粧室のLINEで『いつ帰る？』と時計を見ていた」。お財布も空気もすり減らす惰性の滞在を、40分で区切るための損益分岐点。",
        "x_title": "90分粘るか、40分で切り上げるか｜相席ラウンジ料金の損益分岐点",
        "x_main_text": (
            "少し言いにくい話をすると、噛み合わない席で「悪い気もするし」と90分延長しても、向かいの女性も内心いつ出ようか時計を見ていることが多い。\n\n"
            "1万近いレシートを見て改札で後悔する前に、40分で区切る目安をリプ欄に。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "table-change-protocol",
        "path": "/guide/table-change-protocol",
        "template": "guide_table_change_protocol.html",
        "publish_at": "2026-11-03 19:00:00",
        "publish_at_ja": "2026年11月3日(火・祝) 19:00",
        "lastmod": "2026-11-03",
        "eyecatch_jpg": "/static/eyecatch-table-change-protocol.jpg",
        "eyecatch_webp": "/static/eyecatch-table-change-protocol.webp",
        "badge": "席替え・チェンジ検証",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-pink", "27.6万件ログ実測"),
        "tag_secondary": ("tag-cyan", "スマート離脱プロトコル"),
        "title": "相席ラウンジの席替え（チェンジ）｜言い出せない男性と察している女性、27万件ログで見る成功店舗の条件",
        "short_title": "気まずい沈黙を終わらせる席替え（チェンジ）の頼み方",
        "desc": "「会話が盛り上がらないのに席替えを頼めず、10分750円のメーターだけが回る沈黙」。女性が化粧室で相談を始めるサイン、スタッフへのさりげない3つの伝え方、そして店内に女性が10名以上いないと席替えがうまくいかないデータ上の理由。",
        "x_title": "相席ラウンジの席替え｜言い出せない男性と察している女性の心理",
        "x_main_text": (
            "会話が止まったテーブルで「席替えを頼んだら失礼かな」と迷う間にも、10分750円のメーターは回っている。\n\n"
            "女性10名未満の店で頼むと、男性1人で待つ時間に料金だけがかかることも。\n\n"
            "お互いの気持ちを損なわずに席を離れる方法はリプ欄へ。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "thirties-lounge-guide",
        "path": "/guide/thirties-lounge-guide",
        "template": "guide_thirties_lounge_guide.html",
        "publish_at": "2026-11-04 19:00:00",
        "publish_at_ja": "2026年11月4日(水) 19:00",
        "lastmod": "2026-11-04",
        "eyecatch_jpg": "/static/eyecatch-thirties-lounge-guide.jpg",
        "eyecatch_webp": "/static/eyecatch-thirties-lounge-guide.webp",
        "badge": "30代・年齢層データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-amber", "27.6万件客層ログ"),
        "tag_secondary": ("tag-cyan", "アラサーの店舗選び"),
        "title": "相席ラウンジは30代でも浮かない？｜学生ノリの店と、落ち着いた大人が集まる店の境界線",
        "short_title": "30代・アラサーのための相席ラウンジの選び方",
        "desc": "「20代前半ばかりで場違いでは」とためらうアラサー男女へ。全国27.6万件のログから、ag・オリエンタルラウンジ・JISの年齢層の違いと、平日20時に社会人が集まる時間帯を読み解く。",
        "x_title": "相席ラウンジは30代でも浮かない？｜27.6万件ログで見る店選びの境界線",
        "x_main_text": (
            "「20代前半ばかりで浮くんじゃないか」とためらっている30代の人へ。\n\n"
            "agは20代前半が7割ほどだけれど、オリエンタルラウンジやJISは社会人が6割を超える。\n\n"
            "水木20時の社会人が集まる時間帯と店選びはリプ欄に続きます。\n"
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
        "tag_primary": ("tag-pink", "天候・カレンダー検証"),
        "tag_secondary": ("tag-cyan", "給料日前の穴場法則"),
        "title": "雨の日・給料日前後・連休中日で男女比はどう動く？9.6万件ログで見えたライバルの少ない日",
        "short_title": "雨の日・給料日前後・連休中日の客足データ",
        "desc": "25日の給料日直後はスーツ姿の男性待ちが並ぶ一方、15〜24日の給料日前や、「前髪を守りたい」女性が集まる雨の日の地下直結店は意外と落ち着いて座れる。その理由を9.6万件ログから読み解く。",
        "x_title": "雨の日・給料日前・連休中日｜9.6万件ログで見る穴場日の傾向",
        "x_main_text": (
            "25日の給料日直後は男性の待ち列ができているのに、15〜24日の給料日前はライバルが減り、女性比は62.1%（+7.4pt）まで上がる。\n\n"
            "雨の日に前髪を濡らしたくない人が集まる地下直結の店など、穴場の日の話はリプ欄で。\n"
            "#相席ラウンジ #相席ナビ"
        ),
    },
    {
        "slug": "sunday-lounge-strategy",
        "path": "/guide/sunday-lounge-strategy",
        "template": "guide_sunday_lounge_strategy.html",
        "publish_at": "2026-11-07 19:00:00",
        "publish_at_ja": "2026年11月7日(土) 19:00",
        "lastmod": "2026-11-07",
        "eyecatch_jpg": "/static/eyecatch-sunday-lounge-strategy.jpg",
        "eyecatch_webp": "/static/eyecatch-sunday-lounge-strategy.webp",
        "badge": "日曜日・客層データ",
        "read_time": "約6分で読める",
        "tag_primary": ("tag-pink", "28.6万件ログ実測"),
        "tag_secondary": ("tag-cyan", "サンデーサク飲み検証"),
        "title": "日曜日の相席ラウンジは行くべきか？｜21時半を境に入れ替わる会社員と月曜休み組の実測データ",
        "short_title": "日曜日の相席ラウンジの狙い目と客層データ",
        "desc": "日曜日の相席ラウンジは土曜の約半分の人数ながら女性比率は61.2%と高水準。18:30〜21:00の女性先行サク飲み帯と21時半の客層入れ替わり、そして深夜に合流する月曜休み組（美容・アパレル・看護・シフト制）のリアルを28.6万件ログで解明。",
        "x_title": "日曜日の相席ラウンジは行くべきか？｜21時半の撤退急落と、月曜休みの隠れ狙い目",
        "x_main_text": (
            "日曜夜の相席ラウンジは土曜の3割の人数だけど、女性比率は61.2%と高い。\n\n"
            "ただ「21時半」に翌朝仕事のOLが一斉に帰るため、金土の感覚で遅く入ると男性余りに。\n\n"
            "18時台サク飲みか、22時以降の月曜休み組（美容・アパレル）狙いかの二択。\n\n"
            "データはリプ欄へ。\n"
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


def get_all_scheduled_articles(allow_blocking: bool = False) -> list:
    """Combine built-in scheduled articles with dynamically published/scheduled editorial columns."""
    combined = list(SCHEDULED_ARTICLES)
    seen = {item["slug"] for item in combined}
    try:
        import column_editorial
        for dyn in column_editorial.get_dynamic_scheduled_articles(allow_blocking=allow_blocking):
            sl = dyn.get("slug")
            if sl and sl not in seen:
                seen.add(sl)
                combined.append(dyn)
    except Exception as e:
        logger.warning(f"[scheduled_articles] Dynamic column load skipped: {e}")
    return combined


def get_article_by_slug(slug: str, allow_blocking: bool = True) -> dict:
    slug_clean = (slug or "").strip("/")
    if not slug_clean:
        return None
    for item in get_all_scheduled_articles(allow_blocking=False):
        if item["slug"] == slug_clean:
            return item
    if not allow_blocking:
        return None
    import job_state
    if getattr(job_state, "_sheets_loaded", False) or job_state.sheets_store.is_disabled():
        return None
    for item in get_all_scheduled_articles(allow_blocking=True):
        if item["slug"] == slug_clean:
            return item
    return None


def is_published(slug_or_item, now_jst: datetime.datetime = None) -> bool:
    now_jst = _coerce_naive_jst(now_jst)
    item = slug_or_item if isinstance(slug_or_item, dict) else get_article_by_slug(str(slug_or_item))
    if not item:
        return True  # Non-scheduled legacy articles are already public
    return now_jst >= parse_publish_at(item["publish_at"])


def get_published_articles(now_jst: datetime.datetime = None, allow_blocking: bool = False) -> list:
    """Return scheduled articles whose publish_at has arrived, sorted newest first."""
    now_jst = _coerce_naive_jst(now_jst)
    published = [item for item in get_all_scheduled_articles(allow_blocking=allow_blocking) if is_published(item, now_jst=now_jst)]
    published.sort(key=lambda x: x["publish_at"], reverse=True)
    return published


def get_upcoming_articles(now_jst: datetime.datetime = None, allow_blocking: bool = False) -> list:
    """Return scheduled articles waiting for release, sorted soonest first."""
    now_jst = _coerce_naive_jst(now_jst)
    upcoming = [item for item in get_all_scheduled_articles(allow_blocking=allow_blocking) if not is_published(item, now_jst=now_jst)]
    upcoming.sort(key=lambda x: x["publish_at"])
    return upcoming


def get_all_with_status(now_jst: datetime.datetime = None, allow_blocking: bool = False) -> list:
    """Return all scheduled articles with live `published` boolean flag for `/staff` dashboard."""
    now_jst = _coerce_naive_jst(now_jst)
    out = []
    for item in sorted(get_all_scheduled_articles(allow_blocking=allow_blocking), key=lambda x: x["publish_at"]):
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

    for item in get_all_scheduled_articles(allow_blocking=True):
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

        biz_date_str = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
        daily_col_key = f"col_release_daily:{biz_date_str}"
        if job_state.is_done(daily_col_key):
            reason = f"Daily column X release cap reached ({daily_col_key})"
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": reason}})
            break

        mins_of_day = now_jst.hour * 60 + now_jst.minute
        if mins_of_day < (11 * 60 + 30) or mins_of_day > (23 * 60 + 25):
            reason = f"Holding column X announcement during quiet off-hours ({now_jst.strftime('%H:%M')} JST)"
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": reason}})
            break

        if (19 * 60 + 42 <= mins_of_day <= 20 * 60 + 30) or (21 * 60 + 42 <= mins_of_day <= 22 * 60 + 30):
            reason = f"Holding column X announcement outside prime live slot rush ({now_jst.strftime('%H:%M')} JST)"
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": reason}})
            break

        in_cd, cd_reason = x_poster.check_x_auto_post_cooldown(min_interval_minutes=48, now_jst=now_jst)
        if in_cd:
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({cd_reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": cd_reason}})
            break

        over_b, b_reason = x_poster.check_x_daily_post_budget(now_jst=now_jst, is_primary_peak=False)
        if over_b:
            logger.info(f"[scheduled_articles] Deferring X announcement for {item['slug']} ({b_reason})")
            results.append({"slug": item["slug"], "result": {"status": "skipped", "reason": b_reason}})
            break

        reply_text = (
            f"新しいコラムを書きました\n"
            f"『{item['x_title']}』\n"
            f"{site_url}{item['path']}?s=x_rel"
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
            job_state.mark_done(daily_col_key)
            job_state.mark_done(f"x_col_used:{now_jst.strftime('%Y-%m-%d')}:{item['path']}")
            x_poster.record_x_auto_post_timestamp(now_jst)
            try:
                import sns_editorial
                sns_editorial.record_recent_x_text(item["x_main_text"])
            except Exception:
                pass
            logger.info(f"[scheduled_articles] Announced newly released column on X: {item['slug']}")
        results.append({"slug": item["slug"], "result": res})
        if res.get("status") == "success":
            # At most 1 newly published column X announcement per 15-min check cycle to prevent burst firing
            break

    return results
