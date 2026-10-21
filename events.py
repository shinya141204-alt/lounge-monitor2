"""
events.py - Event and Campaign Crawler & Aggregator for Aiseki Navi
Collects real-time events, promotions, and notices from major lounge brands (OLG, ag, JIS).
"""

import os
import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime

logger = logging.getLogger(__name__)

CACHE_FILE = os.path.join(os.path.dirname(__file__), 'data', 'events_cache.json')
_events_memory_cache = {
    'updated_at': None,
    'events': []
}

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    'Accept-Language': 'ja,ja-JP;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
}


def _clean_text(text: str) -> str:
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def _classify_tag(title: str):
    """Classify event into a friendly category tag with badge styling."""
    t = title.lower()
    if any(k in t for k in ['割', '無料', '0円', '半額', 'キャッシュバック', '定額', 'off', '割引']):
        return '割引・特典', 'tag-discount'
    elif any(k in t for k in ['フェア', 'イベント', '限定', '企画', 'フェス', '祭', 'ガチャ', 'ナイト']):
        return 'フェア・企画', 'tag-event'
    elif any(k in t for k in ['オープン', 'リニューアル', '営業時間', '料金', '休業', 'お知らせ']):
        return 'お知らせ', 'tag-notice'
    return 'イベント', 'tag-event'


def crawl_oriental_events():
    """Crawl events and announcements from oriental-lounge.com."""
    events = []
    try:
        url = 'https://oriental-lounge.com'
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return events
        
        soup = BeautifulSoup(res.text, 'html.parser')
        news_section = soup.find('section', class_=lambda c: c and 'branch-news' in c)
        if not news_section:
            return events

        items = news_section.find_all('div', class_=lambda c: c and 'branch-news-inner' in c)
        for idx, item in enumerate(items[:8]):
            a = item.find('a')
            if not a or not a.get('href'):
                continue
            
            link = a['href']
            if link.startswith('/'):
                link = 'https://oriental-lounge.com' + link

            text_p = item.find('p', class_='branch-news-text')
            title = _clean_text(text_p.get_text() if text_p else item.get_text())
            if not title:
                continue

            mark_p = item.find('p', class_='branch-news-mark')
            mark = _clean_text(mark_p.get_text() if mark_p else '')

            # Detect store matching
            target_stores = []
            target_label = '全店共通'
            # Look for store name patterns like 【新宿】, 【渋谷】, 【梅田】
            store_match = re.search(r'【([^】]+)】', title)
            if store_match:
                extracted = store_match.group(1).strip()
                # Check known keywords
                known_areas = ['新宿', '渋谷', '恵比寿', '上野', '六本木', '銀座', '池袋', '町田', '横浜', '大宮', '千葉', '名古屋', '栄', '梅田', '心斎橋', '難波', '京都', '神戸', '広島', '福岡', '天神', '小倉', '熊本', '札幌', '仙台', '宇都宮', '静岡', '浜松', '岡山']
                for area in known_areas:
                    if area in extracted:
                        target_stores.append(area)
                        target_label = f'{area}店限定'

            tag_label, tag_cls = _classify_tag(title)

            # Extract numeric id from link if possible
            event_id = f"olg-{re.sub(r'[^0-9a-zA-Z]', '', link.split('/')[-1]) or idx}"

            events.append({
                'id': event_id,
                'brand': 'OLG',
                'brand_label': 'オリエンタル / ag',
                'title': title,
                'url': link,
                'date_str': mark or 'NEW',
                'tag': tag_label,
                'tag_cls': tag_cls,
                'target_stores': target_stores,
                'target_label': target_label,
                'created_at': datetime.now().strftime('%Y-%m-%d')
            })
    except Exception as e:
        logger.error(f"Error crawling Oriental Lounge events: {e}")
    return events


def crawl_jis_events():
    """Crawl events and announcements from jis.bar."""
    events = []
    try:
        url = 'https://jis.bar'
        res = requests.get(url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return events

        soup = BeautifulSoup(res.text, 'html.parser')
        news_container = soup.find(class_='news-container')
        if not news_container:
            return events

        links = news_container.find_all('a')
        for idx, a in enumerate(links[:8]):
            href = a.get('href', '')
            if not href:
                continue
            
            full_url = href if href.startswith('http') else f"https://jis.bar/{href.lstrip('/')}"
            
            # Text extraction: often contains date like '2026.9.27' followed by h2
            h2 = a.find('h2')
            title = _clean_text(h2.get_text() if h2 else a.get_text())
            raw_text = a.get_text()
            
            # Extract date pattern YYYY.MM.DD
            date_match = re.search(r'(\d{4}\.\d{1,2}\.\d{1,2})', raw_text)
            date_str = date_match.group(1) if date_match else ''

            if not title:
                continue

            # Detect store matching
            target_stores = []
            target_label = 'JIS全店'
            jis_areas = ['新宿', '難波', 'なんば', '梅田', '福岡', '天神', '熊本', '松山', '札幌']
            for area in jis_areas:
                if area in title:
                    target_stores.append(area)
                    target_label = f'JIS {area}限定'

            tag_label, tag_cls = _classify_tag(title)
            event_id = f"jis-{idx}-{re.sub(r'[^0-9a-zA-Z]', '', date_str)}"

            events.append({
                'id': event_id,
                'brand': 'JIS',
                'brand_label': 'JIS',
                'title': title,
                'url': full_url,
                'date_str': date_str or '最新',
                'tag': tag_label,
                'tag_cls': tag_cls,
                'target_stores': target_stores,
                'target_label': target_label,
                'created_at': datetime.now().strftime('%Y-%m-%d')
            })
    except Exception as e:
        logger.error(f"Error crawling JIS events: {e}")
    return events


def fetch_all_events(force_refresh: bool = False):
    """Fetch all events from all sources and update memory cache and disk cache."""
    global _events_memory_cache

    now = datetime.now()
    # Cache TTL: 2 hours (no need to poll every minute)
    if not force_refresh and _events_memory_cache.get('updated_at'):
        cached_time = datetime.strptime(_events_memory_cache['updated_at'], '%Y-%m-%d %H:%M:%S')
        if (now - cached_time).total_seconds() < 7200:
            return _events_memory_cache['events']

    # Load from disk cache first if memory is empty
    if not _events_memory_cache.get('events') and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                disk_data = json.load(f)
                if disk_data.get('events'):
                    _events_memory_cache = disk_data
                    if not force_refresh:
                        return disk_data['events']
        except Exception as e:
            logger.warning(f"Failed to read events cache from disk: {e}")

    # Crawl live
    olg_events = crawl_oriental_events()
    jis_events = crawl_jis_events()
    all_events = olg_events + jis_events

    if all_events:
        _events_memory_cache = {
            'updated_at': now.strftime('%Y-%m-%d %H:%M:%S'),
            'events': all_events
        }
        # Save to disk
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(_events_memory_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save events cache to disk: {e}")
        return all_events

    return _events_memory_cache.get('events', [])


AREA_ALIASES = {
    '新宿': ['新宿', 'shinjuku'],
    '渋谷': ['渋谷', 'shibuya'],
    '恵比寿': ['恵比寿', 'ebisu'],
    '上野': ['上野', 'ueno'],
    '六本木': ['六本木', 'roppongi'],
    '銀座': ['銀座', 'ginza'],
    '池袋': ['池袋', 'ikebukuro'],
    '梅田': ['梅田', 'umeda'],
    '難波': ['難波', 'なんば', 'namba'],
    '心斎橋': ['心斎橋', 'shinsaibashi'],
    '京都': ['京都', 'kyoto'],
    '神戸': ['神戸', 'kobe'],
    '福岡': ['福岡', '天神', 'fukuoka', 'tenjin'],
    '小倉': ['小倉', 'kokura'],
    '熊本': ['熊本', 'kumamoto'],
    '松山': ['松山', 'matsuyama'],
    '札幌': ['札幌', 'sapporo'],
    '仙台': ['仙台', 'sendai'],
    '名古屋': ['名古屋', '栄', 'nagoya', 'sakae'],
    '広島': ['広島', 'hiroshima'],
    '岡山': ['岡山', 'okayama']
}


def _store_matches_area(store_name: str, area_keyword: str) -> bool:
    sn = store_name.lower()
    ak = area_keyword.lower()
    if ak in sn:
        return True
    for key, aliases in AREA_ALIASES.items():
        if key == area_keyword or area_keyword in aliases:
            if any(alias in sn for alias in aliases):
                return True
    return False


def get_events_for_store(store_name: str):
    """Filter events relevant to a specific store."""
    all_events = fetch_all_events()
    matched = []
    
    clean_name = store_name.upper()
    is_jis = 'JIS' in clean_name
    is_olg = 'OLG' in clean_name or 'ORIENTAL' in clean_name or 'AG' in clean_name

    for ev in all_events:
        ev_brand = ev.get('brand', '')
        if is_jis and ev_brand == 'JIS':
            target_stores = ev.get('target_stores', [])
            if not target_stores:
                matched.append(ev)
            elif any(_store_matches_area(store_name, area) for area in target_stores):
                matched.append(ev)
        elif is_olg and ev_brand == 'OLG':
            target_stores = ev.get('target_stores', [])
            if not target_stores:
                matched.append(ev)
            elif any(_store_matches_area(store_name, area) for area in target_stores):
                matched.append(ev)

    return matched
