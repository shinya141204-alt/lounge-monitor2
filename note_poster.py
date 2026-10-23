"""
note_poster.py - Automatic note.com New Article Monitor & X (Twitter) Auto-Sharer

Monitors the official note RSS feed (https://note.com/huge_finch7078/rss)
and automatically tweets high-CTR promotional posts to X whenever a new article is published.
"""

import os
import json
import logging
import datetime
import urllib.request
import xml.etree.ElementTree as ET
import x_poster

logger = logging.getLogger(__name__)

NOTE_RSS_URL = os.environ.get("NOTE_RSS_URL", "https://note.com/huge_finch7078/rss")
STATE_DIR = os.path.join(os.path.dirname(__file__), "data")
STATE_FILE = os.path.join(STATE_DIR, "posted_notes.json")


def _load_posted_urls() -> set:
    posted = set()
    try:
        if os.path.exists(STATE_FILE):
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                posted.update(data.get("posted_urls", []))
    except Exception as e:
        logger.error(f"Failed to load posted note URLs: {e}")
    # data/ はデプロイで消えるため、永続化された job_state（Google Sheets）も参照する
    try:
        import job_state
        posted.update(k[len("note:"):] for k in job_state.all_keys() if k.startswith("note:"))
    except Exception as e:
        logger.error(f"Failed to load note state from job_state: {e}")
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
    req = urllib.request.Request(NOTE_RSS_URL, headers={"User-Agent": "AisekiNavi-Bot/1.0 (Mozilla/5.0)"})
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
        logger.error(f"Failed to fetch note RSS from {NOTE_RSS_URL}: {e}")
    return articles


def build_note_tweet_text(title: str, link: str, force: bool = False) -> str:
    """
    Build a natural, human-toned promotional post strictly within X's 280 weighted characters limit.
    """
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    
    footer = "』\n\n実際の混雑データを見ながら、外さないお店や時間帯の選び方をまとめました。\n記事リンクはリプ欄へ👇\n#相席ナビ #相席ラウンジ"
    if force:
        footer += f"\n(⏱️{now_jst.strftime('%H:%M:%S')})"
    
    header = "新しいnoteを書きました✍️\n『"
    
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
    """Build the 2nd post (self-reply) containing the note article URL."""
    site_url = os.environ.get("PUBLIC_SITE_URL", "https://aiseki-navi.com").rstrip("/")
    return f"📖 note記事はこちら👇\n{link}\n\n🔍 全国53店舗の現在人数ランキング\n{site_url}/"


def check_and_post_new_note(force: bool = False, article_index: int = 0) -> dict:
    """
    Check for newly published note articles and post to X if not yet shared.
    force=True ignores previously posted record for testing.
    """
    articles = fetch_latest_note_articles()
    if not articles:
        return {"status": "error", "reason": "No articles retrieved from note RSS"}

    if article_index >= len(articles):
        article_index = 0

    target = articles[article_index]
    link = target["link"]
    title = target["title"]

    posted_urls = _load_posted_urls()
    if not force and not posted_urls:
        # 投稿履歴が一切ない（初回 or 永続化導入直後）場合、公開から12時間以上経った記事は
        # 「告知済み」とみなして記録だけ行い、古い記事の再告知を防ぐ
        from email.utils import parsedate_to_datetime
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        for a in articles:
            try:
                age_h = (now_utc - parsedate_to_datetime(a.get("pub_date", ""))).total_seconds() / 3600
            except Exception:
                age_h = 999
            if age_h >= 12:
                _save_posted_url(a["link"])
        posted_urls = _load_posted_urls()
    if not force and link in posted_urls:
        logger.info(f"Latest note article already posted ({link}), skipping.")
        return {"status": "skipped", "reason": "Latest article already posted", "article": target}

    tweet_text = build_note_tweet_text(title, link, force=force)
    reply_text = build_note_reply_text(link)
    result = x_poster.post_to_x(tweet_text, reply_text=reply_text)
    
    if result.get("status") == "success":
        if not force:
            _save_posted_url(link)
        logger.info(f"Successfully posted note promotion to X: {link}")

    result["article"] = target
    result["tweet_text"] = tweet_text
    result["reply_text"] = reply_text
    return result
