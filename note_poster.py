"""
note_poster.py - Automatic note.com New Article Monitor & X (Twitter) Auto-Sharer

Monitors the official note RSS feed (https://note.com/aiseki_navi/rss)
and automatically tweets high-CTR promotional posts to X whenever a new article is published.
"""

import os
import re
import json
import logging
import datetime
import urllib.request
import xml.etree.ElementTree as ET
import x_poster

logger = logging.getLogger(__name__)


def _resolve_note_rss_url() -> str:
    """Return active note RSS URL, auto-migrating legacy creator ID if still set in env."""
    raw = os.environ.get("NOTE_RSS_URL", "https://note.com/aiseki_navi/rss").strip()
    if not raw:
        return "https://note.com/aiseki_navi/rss"
    return raw.replace("note.com/huge_finch7078", "note.com/aiseki_navi")


def _extract_note_key(url: str) -> str:
    """Extract immutable note article key (e.g. 'n12345abcdef' from '.../n/n12345abcdef') so creator ID changes never re-post old articles."""
    m = re.search(r"/n/([A-Za-z0-9_-]+)", url or "")
    return m.group(1) if m else ""


def _is_note_already_posted(url: str, posted_urls: set) -> bool:
    if url in posted_urls:
        return True
    key = _extract_note_key(url)
    if key:
        posted_keys = {_extract_note_key(u) for u in posted_urls}
        posted_keys.discard("")
        if key in posted_keys:
            return True
    return False


NOTE_RSS_URL = _resolve_note_rss_url()
STATE_DIR = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE = os.path.join(STATE_DIR, "posted_notes.json")


def _load_posted_urls_with_health() -> tuple:
    """Return (posted_urls: set, state_healthy: bool)."""
    posted = set()
    has_local_state = False
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                posted.update(data.get("posted_urls", []))
            has_local_state = True
    except Exception as e:
        logger.error(f"Failed to load posted note URLs: {e}")

    # data/ はデプロイで消えるため、永続化された job_state（Google Sheets）を正とする
    job_state_ok = False
    try:
        import job_state
        import sheets_store
        posted.update(k[len("note:"):] for k in job_state.all_keys() if k.startswith("note:"))
        job_state_ok = job_state.is_storage_healthy()
        if not sheets_store.is_disabled():
            # When Sheets is enabled, never trust local STATE_FILE alone before Sheets sync completes
            return posted, job_state_ok
    except Exception as e:
        logger.error(f"Failed to load note state from job_state: {e}")
        job_state_ok = False

    return posted, (has_local_state or job_state_ok)


def _load_posted_urls() -> set:
    posted, _ = _load_posted_urls_with_health()
    return posted


def _save_posted_url(url: str):
    try:
        import job_state
        job_state.mark_done(f"note:{url}")
    except Exception as e:
        logger.error(f"Failed to persist note URL to job_state: {e}")
    try:
        os.makedirs(STATE_DIR, exist_ok=True)
        posted = _load_posted_urls()
        posted.add(url)
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump({"posted_urls": list(posted), "updated_at": datetime.datetime.now().isoformat()}, f, ensure_ascii=False, indent=2)
    except Exception as e:
        logger.error(f"Failed to save posted note URL: {e}")


def fetch_latest_note_articles() -> list:
    """Fetch latest articles from note RSS feed."""
    rss_url = _resolve_note_rss_url()
    req = urllib.request.Request(rss_url, headers={"User-Agent": "AisekiNavi-Bot/1.0 (Mozilla/5.0)"})
    articles = []
    try:
        with urllib.request.urlopen(req, timeout=12) as resp:
            xml_data = resp.read()
            root = ET.fromstring(xml_data)
            for item in root.findall(".//item"):
                title = item.find("title").text if item.find("title") is not None else ""
                link = item.find("link").text if item.find("link") is not None else ""
                pub_date = item.find("pubDate").text if item.find("pubDate") is not None else ""
                guid = item.find("guid").text if item.find("guid") is not None else link
                if title and link:
                    articles.append({
                        "title": title.strip(),
                        "link": link.strip(),
                        "pub_date": pub_date.strip(),
                        "guid": guid.strip(),
                    })
    except Exception as e:
        logger.error(f"Failed to fetch note RSS from {rss_url}: {e}")
    return articles


def build_note_tweet_text(title: str, link: str, force: bool = False, use_ai: bool = False) -> str:
    """
    Build a natural, human-toned promotional post strictly within X's 280 weighted characters limit.
    When `use_ai=True`, crafts an authentic behind-the-scenes author monologue tailored to `title`.
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))

    if use_ai and title:
        try:
            import sns_editorial
            if sns_editorial.has_any_ai_key(allow_blocking=True):
                prompt = (
                    "あなたは夜の街の人間模様とリアルな空気感を綴る執筆者（『相席ナビ』編集長）です。\n"
                    f"いま note に新しいエッセイ『{title}』を公開したところです。\n"
                    "定型文の『新しいnoteを書きました✍️』や『モニター見てて〜』のような裏方アピールではなく、なぜこのテーマを書きたくなったのかを落ち着いた大人の素のトーンでこぼす"
                    "Xの告知1ポスト目（78〜118文字以内、X換算250ウェイト以下、URLは本文に絶対に入れない、絵文字は0〜1個）を書いてください。\n\n"
                    f"{sns_editorial.HUMAN_INSIDER_VOICE_GUIDE}\n\n"
                    "・最後は宣伝臭くせずさらっとリプ欄に触れるか余韻を残して締めること。架空の人数は書かないこと。\n"
                    '出力はJSON形式のみ：{"main_text": "投稿本文"}'
                )
                parsed, _, _ = sns_editorial.call_editorial_ai_json(
                    prompt=prompt,
                    max_output_tokens=1800,
                    thinking_budget=896,
                    total_deadline_sec=18.0,
                    now_jst=now_jst,
                    prefer_fast=True,
                )
                if parsed and parsed.get("main_text"):
                    ai_txt = sns_editorial._postprocess_ai_main_text(parsed["main_text"], channel="x")
                    if force:
                        ai_txt += f"\n(⏱️{now_jst.strftime('%H:%M:%S')})"
                    wt = x_poster.compute_twitter_weight(ai_txt)
                    if "http" not in ai_txt and 84 <= wt <= 274:
                        return ai_txt
        except Exception as e:
            logger.warning(f"Note X AI intro fallback: {e}")

    intros = [
        "前からずっと言葉にしておきたかった夜の話をnoteにまとめました。\n『",
        "夜の街でよく見るけれど誰もちゃんと言語化していなかった話をnoteに書きました。\n『",
        "久しぶりに少し長めのエッセイをnoteに置きました。\n『",
    ]
    header = intros[now_jst.day % len(intros)]
    footer = "』\n\n本編はリプ欄に置いてあります。\n#相席ラウンジ"
    if force:
        footer += f"\n(⏱️{now_jst.strftime('%H:%M:%S')})"

    def calc_weight(t: str) -> int:
        return sum(1 if ord(c) < 128 else 2 for c in t)

    base_weight = calc_weight(header + footer)
    max_title_weight = 276 - base_weight

    cur_weight = 0
    trimmed_title = ""
    for ch in title:
        w = 1 if ord(ch) < 128 else 2
        if cur_weight + w > max_title_weight - 4:
            trimmed_title += "..."
            break
        trimmed_title += ch
        cur_weight += w
    else:
        trimmed_title = title

    return f"{header}{trimmed_title}{footer}"


def build_note_reply_text(link: str) -> str:
    """Build the 2nd post (self-reply) containing ONLY the note article URL so X always renders note's eyecatch card."""
    return f"📖 note記事本編はこちら👇\n{link}"


def _normalize_title_signature(title: str) -> str:
    """Extract core title signature (ignoring bracketed prefix/suffix tags) to prevent duplicate X posts for revised versions of the same note topic."""
    t = re.sub(r"【[^】]*】", "", title or "")
    t = re.sub(r"[\s　・｜|〜~\-—―！？!?「」『』（）()]+", "", t)
    return t[:24].lower()


def check_and_post_new_note(force: bool = False, article_index: int = 0) -> dict:
    """
    Check for newly published note articles and post to X if not yet shared.
    - When `not force`, skips safely if state storage (local STATE_FILE + Sheets) is unavailable after redeploy.
    - Regardless of `posted_urls`, any article published >= 12 hours ago (or with an invalid/missing `pub_date`)
      is automatically marked as seen and never tweeted as "新しいnoteを書きました✍️".
    - Caps automatic note promotions at 1 per calendar day (JST) and skips revised duplicates with the same core title.
    - `force=True` ignores previously posted record for manual testing.
    """
    articles = fetch_latest_note_articles()
    if not articles:
        return {"status": "error", "reason": "No articles retrieved from note RSS"}

    if article_index is None or article_index < 0 or article_index >= len(articles):
        article_index = 0

    if force:
        target = articles[article_index]
    else:
        posted_urls, is_healthy = _load_posted_urls_with_health()
        if not is_healthy:
            logger.warning("Skipping note auto-post: local STATE_FILE absent or Sheets state sync incomplete.")
            return {
                "status": "skipped",
                "reason": "State storage unavailable after redeploy",
                "article": articles[article_index],
            }

        import job_state
        from email.utils import parsedate_to_datetime
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        today_jst_str = now_jst.strftime("%Y-%m-%d")
        daily_cap_key = f"note_daily:{today_jst_str}"

        # Always mark articles older than 12 hours (or with unparseable pub_date) as already seen
        # so old/legacy note articles are NEVER unearthed and tweeted as "新しいnoteを書きました✍️".
        newly_marked = False
        for a in articles:
            if _is_note_already_posted(a["link"], posted_urls):
                continue
            try:
                dt = parsedate_to_datetime(a.get("pub_date", ""))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=datetime.timezone.utc)
                age_h = (now_utc - dt).total_seconds() / 3600.0
            except Exception:
                age_h = 999.0
            if age_h >= 12.0:
                logger.info(f"Marking older note article as already seen (age={age_h:.1f}h): {a['link']}")
                _save_posted_url(a["link"])
                sig = _normalize_title_signature(a.get("title", ""))
                if sig:
                    job_state.mark_done(f"note_title:{sig}")
                newly_marked = True

        if newly_marked:
            posted_urls = _load_posted_urls()

        # Also prevent re-posting a revised/re-uploaded note with the same core title
        for a in articles:
            if _is_note_already_posted(a["link"], posted_urls):
                sig = _normalize_title_signature(a.get("title", ""))
                if sig:
                    job_state.mark_done(f"note_title:{sig}")

        for a in articles:
            if _is_note_already_posted(a["link"], posted_urls):
                continue
            sig = _normalize_title_signature(a.get("title", ""))
            if sig and job_state.is_done(f"note_title:{sig}"):
                logger.info(f"Skipping note article with duplicate title signature ({a['title']}): {a['link']}")
                _save_posted_url(a["link"])
                posted_urls.add(a["link"])

        if article_index > 0:
            target = articles[article_index]
            if _is_note_already_posted(target["link"], posted_urls):
                logger.info(f"Note article already posted ({target['link']}), skipping.")
                return {"status": "skipped", "reason": "Latest article already posted", "article": target}
        else:
            unposted = [
                (idx, a) for idx, a in enumerate(articles)
                if not _is_note_already_posted(a["link"], posted_urls)
            ]
            if not unposted:
                logger.info(f"Latest note article already posted ({articles[0]['link']}), skipping.")
                return {"status": "skipped", "reason": "Latest article already posted", "article": articles[0]}

            def _oldest_first_key(pair):
                idx, art = pair
                try:
                    dt = parsedate_to_datetime(art.get("pub_date", ""))
                    if dt.tzinfo is None:
                        dt = dt.replace(tzinfo=datetime.timezone.utc)
                    return (0, dt.timestamp(), -idx)
                except Exception:
                    return (1, -idx, 0)

            unposted.sort(key=_oldest_first_key)
            target = unposted[0][1]

        # Enforce max 1 automatic note promotion per day on X
        if job_state.is_done(daily_cap_key):
            logger.info(f"Daily note auto-post cap reached ({daily_cap_key}); deferring {target['link']}.")
            return {
                "status": "skipped",
                "reason": "Already posted a note promotion today",
                "article": target,
            }

        in_cd, cd_reason = x_poster.check_x_auto_post_cooldown(min_interval_minutes=48, now_jst=now_jst)
        if in_cd:
            logger.info(f"Deferring note X share due to cooldown ({cd_reason}): {target['link']}")
            return {
                "status": "skipped",
                "reason": cd_reason,
                "article": target,
            }

    link = target["link"]
    title = target["title"]

    tweet_text = build_note_tweet_text(title, link, force=force, use_ai=True)
    reply_text = build_note_reply_text(link)
    result = x_poster.post_to_x(tweet_text, reply_text=reply_text)

    if result.get("status") == "success":
        if not force:
            _save_posted_url(link)
            try:
                import job_state
                now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
                job_state.mark_done(f"note_daily:{now_jst.strftime('%Y-%m-%d')}")
                x_poster.record_x_auto_post_timestamp(now_jst)
                try:
                    import sns_editorial
                    sns_editorial.record_recent_x_text(tweet_text, source="note_share", now_jst=now_jst)
                except Exception:
                    pass
                sig = _normalize_title_signature(title)
                if sig:
                    job_state.mark_done(f"note_title:{sig}")
            except Exception:
                pass
        logger.info(f"Successfully posted note promotion to X: {link}")

    result["article"] = target
    result["tweet_text"] = tweet_text
    result["reply_text"] = reply_text
    return result
