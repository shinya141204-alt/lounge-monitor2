"""
events.py - Event, Campaign & Promotion Crawler & Aggregator for Aiseki Navi
Collects real-time events, promotions, and notices from major lounge brands (OLG, ag, JIS).
Now supports deep per-store crawler for JIS individual store pages and Oriental Lounge announcements.
"""

import os
import re
import json
import time
import logging
import requests
from bs4 import BeautifulSoup
from datetime import datetime
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

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

AREA_ALIASES = {
    '新宿': ['新宿', 'shinjuku', '西新宿', 'nishishinjuku'],
    '西新宿': ['西新宿', 'nishishinjuku', '新宿', 'shinjuku'],
    '渋谷': ['渋谷', 'shibuya'],
    '恵比寿': ['恵比寿', 'ebisu'],
    '上野': ['上野', 'ueno'],
    '六本木': ['六本木', 'roppongi'],
    '銀座': ['銀座', 'ginza'],
    '池袋': ['池袋', 'ikebukuro'],
    '町田': ['町田', 'machida'],
    '横浜': ['横浜', 'yokohama'],
    '大宮': ['大宮', 'omiya'],
    '千葉': ['千葉', 'chiba'],
    '柏': ['柏', 'kashiwa'],
    '錦糸町': ['錦糸町', 'kinshicho'],
    '梅田': ['梅田', 'umeda', '茶屋町', 'chayamachi'],
    '茶屋町': ['茶屋町', 'chayamachi', '梅田', 'umeda'],
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
    '名古屋': ['名古屋', '栄', '錦', 'nagoya', 'sakae', 'nishiki'],
    '栄': ['栄', '名古屋', 'sakae', 'nagoya'],
    '錦': ['錦', '名古屋', 'nishiki', 'nagoya'],
    '広島': ['広島', 'hiroshima'],
    '岡山': ['岡山', 'okayama'],
    '宇都宮': ['宇都宮', 'utsunomiya'],
    '静岡': ['静岡', 'shizuoka'],
    '浜松': ['浜松', 'hamamatsu']
}

JIS_SHOPS = [
    ('JIS SHINJUKU', '新宿', 'https://jis.bar/shops/shinjuku/index.html'),
    ('JIS NISHISHINJUKU', '西新宿', 'https://jis.bar/shops/nishishinjuku/index.html'),
    ('JIS UMEDA', '梅田', 'https://jis.bar/shops/umeda/index.html'),
    ('JIS NAMBA', '難波', 'https://jis.bar/shops/namba/index.html'),
    ('JIS CHAYAMACHI', '茶屋町', 'https://jis.bar/shops/chayamachi/index.html'),
    ('JIS FUKUOKA', '福岡', 'https://jis.bar/shops/fukuoka/index.html'),
    ('JIS SAPPORO', '札幌', 'https://jis.bar/shops/sapporo/index.html'),
    ('JIS OMIYA', '大宮', 'https://jis.bar/shops/omiya/index.html'),
    ('JIS KUMAMOTO', '熊本', 'https://jis.bar/shops/kumamoto/index.html'),
    ('JIS MATSUYAMA', '松山', 'https://jis.bar/shops/matsuyama/index.html'),
]


def _clean_text(text: str) -> str:
    if not text:
        return ''
    return re.sub(r'\s+', ' ', text).strip()


def _classify_tag(title: str):
    """Classify event into a friendly category tag with badge styling."""
    t = title.lower()
    if any(k in t for k in ['割', '無料', '0円', '半額', 'キャッシュバック', '定額', 'off', '割引', 'プラン', 'jcb', 'スタンプ', 'パス']):
        return '割引・特典', 'tag-discount'
    elif any(k in t for k in ['フェア', 'イベント', '限定', '企画', 'フェス', '祭', 'ガチャ', 'ナイト', 'ラリー', 'ハロウィン', 'フード', 'シーシャ', '飲み放題']):
        return 'フェア・企画', 'tag-event'
    elif any(k in t for k in ['オープン', 'リニューアル', '営業時間', '料金', '休業', 'お知らせ']):
        return 'お知らせ', 'tag-notice'
    return 'イベント', 'tag-event'


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


def crawl_oriental_events():
    """Crawl events and announcements from oriental-lounge.com (/announcements and homepage)."""
    events = []
    seen_links = set()

    sources = [
        'https://oriental-lounge.com/announcements',
        'https://oriental-lounge.com'
    ]

    for url in sources:
        try:
            res = requests.get(url, headers=HEADERS, timeout=8)
            if res.status_code != 200:
                continue
            soup = BeautifulSoup(res.text, 'html.parser')

            # 1. /announcements items (rich list)
            for li in soup.find_all('li', class_='info-list-text-inner'):
                a = li.find('a', class_='info-list-link')
                if not a or not a.get('href'):
                    continue
                link = urljoin(url, a['href'])
                if link in seen_links:
                    continue
                seen_links.add(link)

                title = _clean_text(a.get_text())
                if not title:
                    continue

                date_div = li.find('div', class_='info-list-date')
                date_str = _clean_text(date_div.get_text()) if date_div else 'NEW'

                target_stores = []
                target_label = '全店共通'
                sm = re.search(r'【([^】]+)】', title)
                if sm:
                    extracted = sm.group(1).strip()
                    for area in AREA_ALIASES.keys():
                        if area in extracted:
                            target_stores.append(area)
                    if target_stores:
                        target_label = '・'.join(target_stores) + '店限定'

                tag_label, tag_cls = _classify_tag(title)
                event_id = f"olg-{re.sub(r'[^0-9a-zA-Z]', '', link.split('/')[-1])}"

                events.append({
                    'id': event_id,
                    'brand': 'OLG',
                    'brand_label': 'オリエンタル / ag',
                    'title': title,
                    'url': link,
                    'banner_url': '',
                    'gender': 'all',
                    'gender_label': '全店共通',
                    'date_str': date_str,
                    'tag': tag_label,
                    'tag_cls': tag_cls,
                    'target_stores': target_stores,
                    'target_label': target_label,
                    'created_at': datetime.now().strftime('%Y-%m-%d')
                })

            # 2. Homepage branch-news-inner (fallback or featured)
            for item in soup.find_all('div', class_='branch-news-inner'):
                a = item.find('a')
                if not a or not a.get('href'):
                    continue
                link = urljoin(url, a['href'])
                if link in seen_links:
                    continue
                seen_links.add(link)

                text_p = item.find('p', class_='branch-news-text')
                title = _clean_text(text_p.get_text() if text_p else item.get_text())
                if not title:
                    continue

                mark_p = item.find('p', class_='branch-news-mark')
                mark = _clean_text(mark_p.get_text() if mark_p else 'NEW')

                target_stores = []
                target_label = '全店共通'
                sm = re.search(r'【([^】]+)】', title)
                if sm:
                    extracted = sm.group(1).strip()
                    for area in AREA_ALIASES.keys():
                        if area in extracted:
                            target_stores.append(area)
                    if target_stores:
                        target_label = '・'.join(target_stores) + '店限定'

                tag_label, tag_cls = _classify_tag(title)
                event_id = f"olg-{re.sub(r'[^0-9a-zA-Z]', '', link.split('/')[-1])}"

                events.append({
                    'id': event_id,
                    'brand': 'OLG',
                    'brand_label': 'オリエンタル / ag',
                    'title': title,
                    'url': link,
                    'banner_url': '',
                    'gender': 'all',
                    'gender_label': '全店共通',
                    'date_str': mark,
                    'tag': tag_label,
                    'tag_cls': tag_cls,
                    'target_stores': target_stores,
                    'target_label': target_label,
                    'created_at': datetime.now().strftime('%Y-%m-%d')
                })
        except Exception as e:
            logger.error(f"Error crawling Oriental Lounge events from {url}: {e}")

    return events


def _crawl_single_jis_shop(shop_tuple):
    """Worker to crawl individual JIS store page for banners and campaigns."""
    store_name, area, base_url = shop_tuple
    events = []
    try:
        r = requests.get(base_url, headers=HEADERS, timeout=6)
        if r.status_code != 200:
            return events

        soup = BeautifulSoup(r.text, 'html.parser')

        raw_items = soup.find_all(class_='event-item')
        if not raw_items:
            slider = soup.find(class_='bp-hero-slider')
            if slider:
                raw_items = slider.find_all(class_='swiper-slide')

        seen_imgs = set()
        for idx, item in enumerate(raw_items):
            img = item.find('img')
            if not img:
                continue
            src = img.get('src') or ''
            if not src or src in seen_imgs:
                continue
            seen_imgs.add(src)

            # Skip utility icons or layout elements
            if any(k in src.lower() for k in ['arrow', 'recruit', 'logo', 'icon', 'fee', 'emblem', 'bar_gold', 'ph_gallery', 'payment']):
                continue

            banner_url = urljoin(base_url, src)
            gender_val = item.get('data-gender') or ('women' if 'women' in str(item.get('class', [])) else 'all')
            if gender_val not in ['men', 'women']:
                gender_val = 'all'

            a = item.find('a') if item.name != 'a' else item
            href = urljoin(base_url, a.get('href', '')) if (a and a.get('href')) else banner_url

            # Extract or infer readable title
            title = ''
            if href.endswith('.html') and not href.endswith('index.html'):
                try:
                    d_res = requests.get(href, headers=HEADERS, timeout=2.5)
                    if d_res.status_code == 200:
                        d_soup = BeautifulSoup(d_res.text, 'html.parser')
                        if d_soup.title and d_soup.title.string:
                            title = _clean_text(d_soup.title.string)
                except Exception:
                    pass

            if not title:
                fn = src.lower()
                if 'u25' in fn or 'u29' in fn or 'u30' in fn:
                    title = f'【{area}限定】U25/U30 特別フリープラン'
                elif 'free' in fn:
                    title = f'【{area}限定】お得なフリープラン・相席特典'
                elif 'drink' in fn or 'chami' in fn:
                    title = f'【{area}限定】人気ドリンク・チャミスル飲み放題フェア'
                elif 'jcb' in fn:
                    title = f'【{area}限定】JCBギフト・カードご利用キャンペーン'
                elif 'stamp' in fn:
                    title = f'【{area}限定】来店ポイント＆スタンプラリー'
                elif 'shisha' in fn:
                    title = f'【{area}限定】シーシャ特別体験フェア'
                elif 'hallo' in fn:
                    title = f'【{area}限定】ハロウィン限定フェア開催中🎃'
                elif 'food' in fn:
                    title = f'【{area}限定】特製フードフェア・限定メニュー🍽️'
                elif 'plan' in fn:
                    title = f'【{area}限定】特別限定プラン'
                else:
                    title = f'【{area}限定】特別キャンペーン実施中'

            gender_label = '男性限定' if gender_val == 'men' else ('女性限定' if gender_val == 'women' else '全店共通')
            tag_label, tag_cls = _classify_tag(title)

            # Generate stable id
            clean_token = re.sub(r'[^0-9a-zA-Z]', '', os.path.basename(banner_url).split('.')[0])
            event_id = f"jis-store-{area.lower()}-{clean_token or idx}"

            events.append({
                'id': event_id,
                'brand': 'JIS',
                'brand_label': 'JIS',
                'store_name': store_name,
                'title': title,
                'url': href,
                'banner_url': banner_url,
                'gender': gender_val,
                'gender_label': gender_label,
                'date_str': '開催中',
                'tag': tag_label,
                'tag_cls': tag_cls,
                'target_stores': [area],
                'target_label': f'{store_name} 限定',
                'created_at': datetime.now().strftime('%Y-%m-%d')
            })
    except Exception as e:
        logger.error(f"Error crawling JIS store {store_name}: {e}")

    return events


def crawl_jis_store_events():
    """Crawl individual JIS shop pages in parallel."""
    all_store_events = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        results = list(executor.map(_crawl_single_jis_shop, JIS_SHOPS))
    for res in results:
        all_store_events.extend(res)
    return all_store_events


def crawl_jis_global_events():
    """Crawl global news and announcements from jis.bar homepage."""
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
            h2 = a.find('h2')
            title = _clean_text(h2.get_text() if h2 else a.get_text())
            raw_text = a.get_text()

            date_match = re.search(r'(\d{4}\.\d{1,2}\.\d{1,2})', raw_text)
            date_str = date_match.group(1) if date_match else ''

            if not title:
                continue

            target_stores = []
            target_label = 'JIS全店'
            jis_areas = ['新宿', '難波', 'なんば', '梅田', '福岡', '天神', '熊本', '松山', '札幌']
            for area in jis_areas:
                if area in title:
                    target_stores.append(area)
                    target_label = f'JIS {area}限定'

            tag_label, tag_cls = _classify_tag(title)
            event_id = f"jis-global-{idx}-{re.sub(r'[^0-9a-zA-Z]', '', date_str)}"

            events.append({
                'id': event_id,
                'brand': 'JIS',
                'brand_label': 'JIS',
                'store_name': 'JIS',
                'title': title,
                'url': full_url,
                'banner_url': '',
                'gender': 'all',
                'gender_label': '全店共通',
                'date_str': date_str or '最新',
                'tag': tag_label,
                'tag_cls': tag_cls,
                'target_stores': target_stores,
                'target_label': target_label,
                'created_at': datetime.now().strftime('%Y-%m-%d')
            })
    except Exception as e:
        logger.error(f"Error crawling JIS global events: {e}")
    return events


def fetch_all_events(force_refresh: bool = False):
    """Fetch all events from all sources and update memory cache and disk cache."""
    global _events_memory_cache

    now = datetime.now()
    # Cache TTL: 2 hours
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

    # Crawl live: OLG announcements + JIS store pages + JIS global news
    olg_events = crawl_oriental_events()
    jis_store_events = crawl_jis_store_events()
    jis_global_events = crawl_jis_global_events()
    
    all_events = olg_events + jis_store_events + jis_global_events

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


def get_events_for_store(store_name: str):
    """Filter events relevant to a specific store (prioritizes store-specific banners and campaigns)."""
    all_events = fetch_all_events()
    matched = []
    global_matched = []

    clean_name = store_name.upper()
    is_jis = 'JIS' in clean_name
    is_olg = 'OLG' in clean_name or 'ORIENTAL' in clean_name or 'AG' in clean_name

    for ev in all_events:
        ev_brand = ev.get('brand', '')
        if is_jis and ev_brand == 'JIS':
            target_stores = ev.get('target_stores', [])
            if not target_stores:
                global_matched.append(ev)
            elif any(_store_matches_area(store_name, area) for area in target_stores):
                matched.append(ev)
        elif is_olg and ev_brand == 'OLG':
            target_stores = ev.get('target_stores', [])
            if not target_stores:
                global_matched.append(ev)
            elif any(_store_matches_area(store_name, area) for area in target_stores):
                matched.append(ev)

    # Return store-specific events first, then brand-wide events
    return matched + global_matched
