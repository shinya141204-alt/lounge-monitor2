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
import threading
import requests
from bs4 import BeautifulSoup
from datetime import datetime, timezone, timedelta
from urllib.parse import urljoin
from concurrent.futures import ThreadPoolExecutor

logger = logging.getLogger(__name__)
_JST = timezone(timedelta(hours=9))

CACHE_FILE = os.path.join(os.path.dirname(__file__), 'data', 'events_cache.json')
_events_memory_cache = {
    'updated_at': None,
    'events': [],
    'operational_notices': [],
}
_refresh_lock = threading.Lock()
_is_refreshing = False

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1',
    'Accept-Language': 'ja,ja-JP;q=0.9',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8'
}

AREA_ALIASES = {
    '西新宿': ['西新宿', 'nishishinjuku'],
    '新宿': ['新宿', 'shinjuku'],
    '茶屋町': ['茶屋町', 'chayamachi'],
    '梅田': ['梅田', 'umeda'],
    '大宮': ['大宮', 'omiya'],
    '渋谷': ['渋谷', 'shibuya'],
    '恵比寿': ['恵比寿', 'ebisu'],
    '上野': ['上野', 'ueno'],
    '六本木': ['六本木', 'roppongi'],
    '銀座': ['銀座', 'ginza'],
    '池袋': ['池袋', 'ikebukuro'],
    '町田': ['町田', 'machida'],
    '横浜': ['横浜', 'yokohama'],
    '千葉': ['千葉', 'chiba'],
    '柏': ['柏', 'kashiwa'],
    '錦糸町': ['錦糸町', 'kinshicho'],
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
    '名古屋': ['名古屋', 'nagoya'],
    '栄': ['栄', '名古屋栄', 'sakae'],
    '錦': ['錦', '名古屋錦', 'nishiki'],
    '広島': ['広島', 'hiroshima'],
    '岡山': ['岡山', 'okayama'],
    '宇都宮': ['宇都宮', 'utsunomiya'],
    '高崎': ['高崎', 'takasaki'],
    '金沢': ['金沢', 'kanazawa'],
    '静岡': ['静岡', 'shizuoka'],
    '浜松': ['浜松', 'hamamatsu'],
    '天満': ['天満', 'tenma'],
    '大阪駅前': ['大阪駅前', 'osaka'],
    '長崎': ['長崎', 'nagasaki'],
    '大分': ['大分', 'oita'],
    '宮崎': ['宮崎', 'miyazaki'],
    '鹿児島': ['鹿児島', 'kagoshima'],
    '沖縄': ['沖縄', 'okinawa']
}

JIS_STORE_TO_AREAS = {
    'JIS SHINJUKU': {'新宿', 'shinjuku'},
    'JIS NISHISHINJUKU': {'西新宿', 'nishishinjuku'},
    'JIS UMEDA': {'梅田', 'umeda'},
    'JIS CHAYAMACHI': {'茶屋町', 'chayamachi'},
    'JIS NAMBA': {'難波', 'なんば', 'namba'},
    'JIS FUKUOKA': {'福岡', '天神', 'fukuoka', 'tenjin'},
    'JIS SAPPORO': {'札幌', 'sapporo'},
    'JIS OMIYA': {'大宮', 'omiya'},
    'JIS KUMAMOTO': {'熊本', 'kumamoto'},
    'JIS MATSUYAMA': {'松山', 'matsuyama'}
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
    if any(k in t for k in ['割', '無料', '0円', '半額', 'キャッシュバック', '定額', 'off', '割引', 'プラン', 'jcb', 'スタンプ', 'パス', '食べ放題', '飲み放題', 'ハッピーアワー', 'ギフト', 'チケット', 'マイル', 'プレゼント', 'アイス', 'クーポン']):
        return '割引・特典', 'tag-discount'
    elif any(k in t for k in ['フェア', 'イベント', '限定', '企画', 'フェス', '祭', 'ガチャ', 'ナイト', 'ラリー', 'ハロウィン', 'フード', 'シーシャ', 'ハシゴ', 'ビンゴ', '大会']):
        return 'フェア・企画', 'tag-event'
    elif any(k in t for k in ['オープン', 'リニューアル', '営業時間', '料金', '休業', 'お知らせ']):
        return 'お知らせ', 'tag-notice'
    return 'イベント', 'tag-event'


OLG_STORE_HEADER_TO_AREAS = {
    'ag仙台': ['OLG ag仙台'],
    '仙台': ['OLG ag仙台'],
    'ag札幌': ['OLG ag札幌'],
    '札幌': ['OLG 札幌'],
    'ag金沢': ['OLG ag金沢'],
    '金沢': ['OLG ag金沢'],
    '宇都宮': ['OLG 宇都宮'],
    '高崎': ['OLG 高崎'],
    '大宮': ['OLG 大宮'],
    '柏': ['OLG 柏'],
    '錦糸町': ['OLG 錦糸町'],
    'ag上野': ['OLG ag上野'],
    '上野': ['OLG 上野'],
    'ag新宿': ['OLG ag新宿'],
    '新宿': ['OLG 新宿'],
    '渋谷本店': ['OLG 渋谷本店'],
    '渋谷駅前': ['OLG 渋谷駅前'],
    'ag渋谷': ['OLG ag渋谷'],
    '恵比寿': ['OLG 恵比寿'],
    '町田': ['OLG 町田'],
    '横浜': ['OLG 横浜'],
    '静岡': ['OLG 静岡'],
    '浜松': ['OLG 浜松'],
    '名古屋栄': ['OLG 名古屋 栄'],
    '名古屋錦': ['OLG 名古屋 錦'],
    'ag名古屋': ['OLG ag名古屋'],
    '京都': ['OLG 京都'],
    '心斎橋': ['OLG 心斎橋'],
    '難波': ['OLG 難波'],
    'ag梅田': ['OLG ag梅田'],
    '梅田': ['OLG 大阪駅前', 'OLG ag梅田'],
    '東梅田': ['OLG 大阪駅前'],
    '茶屋町': ['OLG 大阪駅前', 'OLG ag梅田'],
    '大阪駅前': ['OLG 大阪駅前'],
    '天満': ['OLG 天満'],
    '神戸': ['OLG 神戸'],
    '岡山': ['OLG 岡山'],
    'ag広島': ['OLG ag広島'],
    '広島': ['OLG ag広島'],
    'ag福岡': ['OLG ag福岡'],
    '福岡': ['OLG 福岡'],
    '小倉': ['OLG 小倉'],
    '長崎': ['OLG 長崎'],
    '熊本': ['OLG 熊本'],
    '大分': ['OLG 大分'],
    '宮崎': ['OLG 宮崎'],
    '鹿児島': ['OLG 鹿児島'],
    'ag沖縄': ['OLG ag沖縄'],
    '沖縄': ['OLG ag沖縄'],
}

# Exact 17 franchise stores for Oriental Lounge "【全国17店舗】" campaigns (e.g. 北海道フェア🌽, ハロウィンフェア)
OLG_17_STORES = [
    'OLG ag仙台',
    'OLG 宇都宮',
    'OLG 高崎',
    'OLG 大宮',
    'OLG 柏',
    'OLG 錦糸町',
    'OLG ag上野',
    'OLG 上野',
    'OLG 新宿',
    'OLG 渋谷本店',
    'OLG ag渋谷',
    'OLG 恵比寿',
    'OLG 町田',
    'OLG 横浜',
    'OLG 難波',
    'OLG 福岡',
    'OLG 小倉',
]

# Longest-first token mapping for Oriental Lounge / ag store extraction without substring cross-contamination
OLG_TOKEN_RULES = [
    ('オリエンタルラウンジ渋谷本店', ['OLG 渋谷本店']),
    ('オリエンタルラウンジ渋谷駅前', ['OLG 渋谷駅前']),
    ('オリエンタルラウンジ名古屋栄', ['OLG 名古屋 栄']),
    ('オリエンタルラウンジ名古屋錦', ['OLG 名古屋 錦']),
    ('オリエンタルラウンジ大阪駅前', ['OLG 大阪駅前']),
    ('オリエンタルラウンジ茶屋町', ['OLG 大阪駅前', 'OLG ag梅田']),
    ('オリエンタルラウンジ心斎橋', ['OLG 心斎橋']),
    ('オリエンタルラウンジ東梅田', ['OLG 大阪駅前']),
    ('オリエンタルラウンジ梅田', ['OLG 大阪駅前', 'OLG ag梅田']),
    ('オリエンタルラウンジ難波', ['OLG 難波']),
    ('オリエンタルラウンジ新宿', ['OLG 新宿']),
    ('オリエンタルラウンジ上野', ['OLG 上野']),
    ('オリエンタルラウンジ恵比寿', ['OLG 恵比寿']),
    ('オリエンタルラウンジ錦糸町', ['OLG 錦糸町']),
    ('オリエンタルラウンジ町田', ['OLG 町田']),
    ('オリエンタルラウンジ横浜', ['OLG 横浜']),
    ('オリエンタルラウンジ大宮', ['OLG 大宮']),
    ('オリエンタルラウンジ柏', ['OLG 柏']),
    ('オリエンタルラウンジ宇都宮', ['OLG 宇都宮']),
    ('オリエンタルラウンジ高崎', ['OLG 高崎']),
    ('オリエンタルラウンジ京都', ['OLG 京都']),
    ('オリエンタルラウンジ神戸', ['OLG 神戸']),
    ('オリエンタルラウンジ天満', ['OLG 天満']),
    ('オリエンタルラウンジ岡山', ['OLG 岡山']),
    ('オリエンタルラウンジ広島', ['OLG ag広島']),
    ('オリエンタルラウンジ福岡', ['OLG 福岡']),
    ('オリエンタルラウンジ小倉', ['OLG 小倉']),
    ('オリエンタルラウンジ札幌', ['OLG 札幌']),
    ('オリエンタルラウンジ仙台', ['OLG ag仙台']),
    ('オリエンタルラウンジ金沢', ['OLG ag金沢']),
    ('オリエンタルラウンジ静岡', ['OLG 静岡']),
    ('オリエンタルラウンジ浜松', ['OLG 浜松']),
    ('オリエンタルラウンジ長崎', ['OLG 長崎']),
    ('オリエンタルラウンジ熊本', ['OLG 熊本']),
    ('オリエンタルラウンジ大分', ['OLG 大分']),
    ('オリエンタルラウンジ宮崎', ['OLG 宮崎']),
    ('オリエンタルラウンジ鹿児島', ['OLG 鹿児島']),
    ('オリエンタルラウンジ沖縄', ['OLG ag沖縄']),
    ('ag名古屋', ['OLG ag名古屋']),
    ('ag 名古屋', ['OLG ag名古屋']),
    ('ag渋谷', ['OLG ag渋谷']),
    ('ag 渋谷', ['OLG ag渋谷']),
    ('ag新宿', ['OLG ag新宿']),
    ('ag 新宿', ['OLG ag新宿']),
    ('ag上野', ['OLG ag上野']),
    ('ag 上野', ['OLG ag上野']),
    ('ag梅田', ['OLG ag梅田']),
    ('ag 梅田', ['OLG ag梅田']),
    ('ag広島', ['OLG ag広島']),
    ('ag 広島', ['OLG ag広島']),
    ('ag仙台', ['OLG ag仙台']),
    ('ag 仙台', ['OLG ag仙台']),
    ('ag福岡', ['OLG ag福岡']),
    ('ag 福岡', ['OLG ag福岡']),
    ('ag札幌', ['OLG ag札幌']),
    ('ag 札幌', ['OLG ag札幌']),
    ('ag金沢', ['OLG ag金沢']),
    ('ag 金沢', ['OLG ag金沢']),
    ('ag沖縄', ['OLG ag沖縄']),
    ('ag 沖縄', ['OLG ag沖縄']),
    ('名古屋栄', ['OLG 名古屋 栄']),
    ('名古屋 栄', ['OLG 名古屋 栄']),
    ('名古屋錦', ['OLG 名古屋 錦']),
    ('名古屋 錦', ['OLG 名古屋 錦']),
    ('渋谷本店', ['OLG 渋谷本店']),
    ('渋谷駅前', ['OLG 渋谷駅前']),
    ('大阪駅前', ['OLG 大阪駅前']),
    ('東梅田', ['OLG 大阪駅前']),
    ('茶屋町', ['OLG 大阪駅前', 'OLG ag梅田']),
    ('心斎橋', ['OLG 心斎橋']),
    ('錦糸町', ['OLG 錦糸町']),
    ('宇都宮', ['OLG 宇都宮']),
    ('恵比寿', ['OLG 恵比寿']),
    ('鹿児島', ['OLG 鹿児島']),
    ('高崎', ['OLG 高崎']),
    ('大宮', ['OLG 大宮']),
    ('町田', ['OLG 町田']),
    ('横浜', ['OLG 横浜']),
    ('静岡', ['OLG 静岡']),
    ('浜松', ['OLG 浜松']),
    ('京都', ['OLG 京都']),
    ('難波', ['OLG 難波']),
    ('なんば', ['OLG 難波']),
    ('天満', ['OLG 天満']),
    ('神戸', ['OLG 神戸']),
    ('岡山', ['OLG 岡山']),
    ('小倉', ['OLG 小倉']),
    ('長崎', ['OLG 長崎']),
    ('熊本', ['OLG 熊本']),
    ('大分', ['OLG 大分']),
    ('宮崎', ['OLG 宮崎']),
    ('新宿', ['OLG 新宿']),
    ('渋谷', ['OLG 渋谷本店']),
    ('上野', ['OLG 上野']),
    ('梅田', ['OLG 大阪駅前', 'OLG ag梅田']),
    ('広島', ['OLG ag広島']),
    ('仙台', ['OLG ag仙台']),
    ('札幌', ['OLG 札幌']),
    ('金沢', ['OLG ag金沢']),
    ('福岡', ['OLG 福岡']),
    ('沖縄', ['OLG ag沖縄']),
    ('栄', ['OLG 名古屋 栄']),
    ('錦', ['OLG 名古屋 錦']),
    ('柏', ['OLG 柏']),
]


_OPERATIONAL_AUDIT_KEYWORDS = (
    '料金改定', '価格改定', 'システム変更', '料金システム',
    '営業時間変更', '営業時間の変更', '定休日', '臨時休業', '閉店', '移転',
)
_recent_operational_notices = []


def _record_operational_notice(brand: str, title: str, url: str):
    """Preserve operational/hours/price notices for data_audit.py even when hidden from public campaign cards."""
    t = title or ''
    if any(k in t for k in _OPERATIONAL_AUDIT_KEYWORDS):
        if not any(x.get('title') == t and x.get('url') == url for x in _recent_operational_notices):
            _recent_operational_notices.append({
                'brand': brand,
                'title': t,
                'url': url,
            })
            if len(_recent_operational_notices) > 100:
                del _recent_operational_notices[:-100]


def get_operational_notices() -> list:
    """Return operational announcements (hours/price revisions, closures) captured during crawling."""
    cached = _events_memory_cache.get('operational_notices') or []
    combined = list(_recent_operational_notices)
    seen = {(x.get('title'), x.get('url')) for x in combined}
    for item in cached:
        key = (item.get('title'), item.get('url'))
        if key not in seen:
            seen.add(key)
            combined.append(item)
    return combined


def _is_non_campaign_notice(title: str) -> bool:
    """Return True if an announcement title is an administrative notice or roundup index rather than a store campaign."""
    t = title or ''
    if any(k in t for k in (
        'イベントを一挙ご紹介',
        '各店イベント',
        '今月のイベント',
        '募集停止',
        'メンテナンス',
        'システム障害',
        '規約改定',
        'プライバシーポリシー',
        '臨時休業',
        '営業時間変更のお知らせ',
    )):
        return True
    return False


def extract_olg_target_stores(title: str, body_text: str = "") -> tuple:
    """Strictly extract canonical Oriental Lounge / ag target store names from an announcement title and optional body.
    Prevents multi-store announcements (like 【全国17店舗】北海道フェア🌽) from falling back to target_stores=[] (all 38 stores),
    and prevents same-city cross-contamination (e.g. 新宿->ag新宿, 名古屋栄->名古屋錦, 渋谷本店->渋谷駅前).
    """
    t = _clean_text(title)
    b = _clean_text(body_text)

    # 1. Handle 【全国17店舗】 or 17店舗限定 announcements explicitly
    if '17店舗' in t or '17店舗' in b:
        if b:
            body_stores = []
            work_b = b
            for token, canonicals in OLG_TOKEN_RULES:
                if token in work_b:
                    for c in canonicals:
                        if c not in body_stores:
                            body_stores.append(c)
                    work_b = work_b.replace(token, ' ')
            if 10 <= len(body_stores) <= 22:
                return body_stores, '対象17店舗限定'
        return list(OLG_17_STORES), '対象17店舗限定'

    # 2. Extract from leading bracket 【...】 or full title using longest-token consumption
    bracket_match = re.search(r'【([^】]+)】', t)
    search_zone = bracket_match.group(1).strip() if bracket_match else t

    # Explicit all-store declaration only if explicitly written as 全店 / 全店舗
    if bracket_match and search_zone in ('全店', '全店舗', '全店共通', 'ALL'):
        return [], '全店共通'

    matched_stores = []
    work = search_zone
    for token, canonicals in OLG_TOKEN_RULES:
        if token in work:
            for c in canonicals:
                if c not in matched_stores:
                    matched_stores.append(c)
            work = work.replace(token, ' ')

    # If bracket didn't have store names (e.g. 【期間限定】渋谷本店...), scan the rest of the title
    if not matched_stores and bracket_match:
        rest_work = re.sub(r'【[^】]+】', ' ', t)
        for token, canonicals in OLG_TOKEN_RULES:
            if token in rest_work:
                for c in canonicals:
                    if c not in matched_stores:
                        matched_stores.append(c)
                rest_work = rest_work.replace(token, ' ')

    # 3. If still empty and body_text is provided, extract from body_text
    if not matched_stores and b:
        work_b = b
        for token, canonicals in OLG_TOKEN_RULES:
            if token in work_b:
                for c in canonicals:
                    if c not in matched_stores:
                        matched_stores.append(c)
                work_b = work_b.replace(token, ' ')

    if matched_stores:
        short_names = [s.replace('OLG ', '').replace('名古屋 ', '名古屋') for s in matched_stores]
        if len(short_names) > 4:
            label = f"対象{len(short_names)}店舗限定"
        else:
            label = '・'.join(short_names) + '店限定'
        return matched_stores, label

    return [], '対象店舗要確認'


def _parse_oriental_roundup_article(article_url: str, date_str: str):
    """Parse store-specific events from monthly roundup announcement articles (e.g. 10月イベントを一挙ご紹介)."""
    sub_events = []
    try:
        res = requests.get(article_url, headers=HEADERS, timeout=8)
        if res.status_code != 200:
            return sub_events
        soup = BeautifulSoup(res.text, 'html.parser')
        content = soup.find(class_='announcement-detail-content') or soup.find('article') or soup.find('main')
        if not content:
            return sub_events

        text = content.get_text()
        blocks = re.split(r'●([^\n]+)', text)
        for i in range(1, len(blocks), 2):
            store_header = blocks[i].strip()
            store_body = blocks[i + 1].strip() if i + 1 < len(blocks) else ''

            target_areas = OLG_STORE_HEADER_TO_AREAS.get(store_header, [])
            if not target_areas:
                for k, v in OLG_STORE_HEADER_TO_AREAS.items():
                    if k in store_header:
                        target_areas = v
                        break
            if not target_areas:
                clean_h = store_header.replace('ag', '').replace('店', '').strip()
                target_areas = [clean_h]

            raw_events = [line.strip().lstrip('・') for line in store_body.split('\n') if line.strip().startswith('・')]
            for idx, ev_name in enumerate(raw_events):
                if not ev_name:
                    continue

                # Exclude ubiquitous default perks like "女性限定フード食べ放題" so real unique campaigns stand out
                if 'フード食べ放題' in ev_name or '食べ放題' in ev_name:
                    continue

                # Gender detection
                ev_lower = ev_name.lower()
                if '女性限定' in ev_lower or '女性' in ev_lower or 'ハーゲンダッツ' in ev_lower or 'フェイスパック' in ev_lower:
                    gender_val = 'women'
                    gender_label = '女性限定'
                elif '男性限定' in ev_lower or '男性' in ev_lower or '🚹' in ev_name or 'ハッピーアワー' in ev_lower:
                    gender_val = 'men'
                    gender_label = '男性限定'
                else:
                    gender_val = 'all'
                    gender_label = '全店共通'

                full_title = f'【{store_header}】{ev_name}'
                tag_label, tag_cls = _classify_tag(full_title)
                clean_header = re.sub(r'\s+', '', store_header)
                ev_id = f"olg-roundup-{clean_header}-{idx}"

                sub_events.append({
                    'id': ev_id,
                    'brand': 'OLG',
                    'brand_label': 'オリエンタル / ag',
                    'title': full_title,
                    'url': article_url,
                    'banner_url': '',
                    'gender': gender_val,
                    'gender_label': gender_label,
                    'date_str': date_str or '開催中',
                    'tag': tag_label,
                    'tag_cls': tag_cls,
                    'target_stores': target_areas,
                    'target_label': f'{store_header}限定',
                    'created_at': datetime.now().strftime('%Y-%m-%d')
                })
    except Exception as e:
        logger.warning(f"Failed to parse Oriental roundup article {article_url}: {e}")

    return sub_events


def _store_matches_area(store_name: str, area_keyword: str) -> bool:
    """Accurately match store with event area keywords without cross-contamination."""
    clean_store = store_name.upper().replace(' ', '')
    clean_kw = area_keyword.upper().replace(' ', '')

    if not clean_store or not clean_kw:
        return False

    # 0. Canonical full store name exact match (e.g. 'OLG 新宿', 'OLG ag渋谷', 'OLG 名古屋 栄', 'JIS SHINJUKU')
    if clean_kw.startswith('OLG') or clean_kw.startswith('JIS') or clean_kw.startswith('ALFA') or clean_kw.startswith('CLOVERS') or clean_kw.startswith('XIX') or clean_kw.startswith('YATAKOI'):
        return clean_store == clean_kw

    # 1. Specialized exact matching for JIS shops
    if store_name.upper() in JIS_STORE_TO_AREAS:
        ak = area_keyword.lower().strip()
        return ak in JIS_STORE_TO_AREAS[store_name.upper()]

    # 2. Strict ag vs OLG brand separation across ALL cities (including 新宿, 福岡, 仙台, 札幌, 金沢, 渋谷, 上野, 梅田, 名古屋, 広島, 沖縄)
    is_ag_store = 'AG' in clean_store
    is_ag_kw = 'AG' in clean_kw
    if is_ag_store != is_ag_kw:
        return False

    # 3. Shibuya Honten vs Shibuya Ekimae separation
    if '渋谷駅前' in clean_store:
        if '渋谷本店' in clean_kw or clean_kw in ('渋谷', 'SHIBUYA'):
            return False
    if '渋谷本店' in clean_store:
        if '渋谷駅前' in clean_kw:
            return False

    # 4. Nagoya Sakae vs Nishiki separation
    if '栄' in clean_store:
        if '錦' in clean_kw or 'NISHIKI' in clean_kw or clean_kw in ('名古屋', 'NAGOYA'):
            return False
    if '錦' in clean_store:
        if '栄' in clean_kw or 'SAKAE' in clean_kw or clean_kw in ('名古屋', 'NAGOYA'):
            return False

    # 5. Shinjuku vs Nishishinjuku & Umeda vs Chayamachi vs Osaka Ekimae separation
    if '西新宿' in clean_store or 'NISHISHINJUKU' in clean_store:
        if clean_kw in ('SHINJUKU', '新宿'):
            return False
    if ('SHINJUKU' in clean_store or '新宿' in clean_store) and 'NISHI' not in clean_store and '西新宿' not in clean_store:
        if '西新宿' in clean_kw or 'NISHISHINJUKU' in clean_kw:
            return False

    if '茶屋町' in clean_store or 'CHAYAMACHI' in clean_store:
        if clean_kw in ('UMEDA', '梅田', '東梅田', '大阪駅前'):
            return False
    if '大阪駅前' in clean_store:
        if clean_kw in ('UMEDA', '梅田', '東梅田', '茶屋町', 'CHAYAMACHI'):
            return False
    if ('UMEDA' in clean_store or '梅田' in clean_store) and 'CHAYAMACHI' not in clean_store and '茶屋町' not in clean_store:
        if '茶屋町' in clean_kw or 'CHAYAMACHI' in clean_kw or '大阪駅前' in clean_kw:
            return False

    # 6. Direct substring match
    if clean_kw in clean_store or clean_store in clean_kw:
        return True

    # 7. Alias mapping match
    sn = store_name.lower().replace(' ', '')
    ak = area_keyword.lower().replace(' ', '')
    for key, aliases in AREA_ALIASES.items():
        if key.lower() == ak or ak in [a.lower() for a in aliases]:
            if any(alias.lower() in sn for alias in aliases):
                return True

    return False


def _fetch_olg_announcement_body(detail_url: str) -> str:
    """Fetch detail text of an Oriental Lounge announcement when needed to resolve multi-store target lists."""
    try:
        r = requests.get(detail_url, headers=HEADERS, timeout=5)
        if r.status_code != 200:
            return ''
        soup = BeautifulSoup(r.text, 'html.parser')
        content = soup.find(class_='announcement-detail-content') or soup.find('article') or soup.find('main')
        return _clean_text(content.get_text()) if content else ''
    except Exception:
        return ''


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

                # Check if this announcement is a monthly roundup article with all store campaigns
                if any(k in title for k in ['イベントを一挙ご紹介', 'イベント情報', '各店イベント', '今月のイベント']):
                    sub_events = _parse_oriental_roundup_article(link, date_str)
                    if sub_events:
                        events.extend(sub_events)
                    continue

                if _is_non_campaign_notice(title):
                    _record_operational_notice('OLG', title, link)
                    continue

                target_stores, target_label = extract_olg_target_stores(title)
                if not target_stores and ('店舗' in title or '限定' in title):
                    body_txt = _fetch_olg_announcement_body(link)
                    if body_txt:
                        target_stores, target_label = extract_olg_target_stores(title, body_txt)

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

                if any(k in title for k in ['イベントを一挙ご紹介', 'イベント情報', '各店イベント', '今月のイベント']):
                    sub_events = _parse_oriental_roundup_article(link, mark)
                    if sub_events:
                        events.extend(sub_events)
                    continue

                if _is_non_campaign_notice(title):
                    _record_operational_notice('OLG', title, link)
                    continue

                target_stores, target_label = extract_olg_target_stores(title)
                if not target_stores and ('店舗' in title or '限定' in title):
                    body_txt = _fetch_olg_announcement_body(link)
                    if body_txt:
                        target_stores, target_label = extract_olg_target_stores(title, body_txt)

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

    # Deduplicate by event id
    unique_events = []
    seen_ids = set()
    for ev in events:
        if ev['id'] not in seen_ids:
            seen_ids.add(ev['id'])
            unique_events.append(ev)

    return unique_events


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
            jis_areas = ['西新宿', '新宿', '茶屋町', '梅田', '大宮', '難波', 'なんば', '福岡', '天神', '熊本', '松山', '札幌']
            for area in jis_areas:
                if area in title:
                    target_stores.append(area)
                    target_label = f'JIS {area}限定'
                    break

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


def get_regional_curated_events() -> list:
    """ Baseline verified campaigns for Hiroshima (ALFA, CLOVERS, OLG/ag) & Okayama (XIX)
    so regional cross-brand comparison always has store-specific men's & women's events
    even before or between live crawls."""
    today_s = datetime.now().strftime('%Y-%m-%d')
    return [
        {
            'id': 'clovers-hiroshima-halfprice',
            'brand': 'CLOVERS',
            'brand_label': 'CLOVERS広島',
            'store_name': 'CLOVERS HIROSHIMA',
            'title': '【広島CLOVERS】日〜木22時までの入店で男性相席料金が半額（50%OFF）',
            'url': 'https://bar-clovers.com/',
            'banner_url': '',
            'gender': 'men',
            'gender_label': '男性特典',
            'date_str': '開催中',
            'tag': '割引・特典',
            'tag_cls': 'tag-discount',
            'target_stores': ['広島'],
            'target_label': '相席CLOVERS 広島本店 限定',
            'created_at': today_s,
        },
        {
            'id': 'clovers-hiroshima-quocard',
            'brand': 'CLOVERS',
            'brand_label': 'CLOVERS広島',
            'store_name': 'CLOVERS HIROSHIMA',
            'title': '【広島CLOVERS】公式アプリクーポン＆相席60分でQUOカード1,000円分プレゼント企画',
            'url': 'https://bar-clovers.com/',
            'banner_url': '',
            'gender': 'all',
            'gender_label': '男女共通特典',
            'date_str': '開催中',
            'tag': '割引・特典',
            'tag_cls': 'tag-discount',
            'target_stores': ['広島'],
            'target_label': '相席CLOVERS 広島本店 限定',
            'created_at': today_s,
        },
        {
            'id': 'clovers-hiroshima-women-sweets',
            'brand': 'CLOVERS',
            'brand_label': 'CLOVERS広島',
            'store_name': 'CLOVERS HIROSHIMA',
            'title': '【広島CLOVERS】女性限定パフェ・特製スイーツ＆VIPカラオケ個室無料フェア',
            'url': 'https://bar-clovers.com/',
            'banner_url': '',
            'gender': 'women',
            'gender_label': '女性限定',
            'date_str': '開催中',
            'tag': 'フェア・企画',
            'tag_cls': 'tag-event',
            'target_stores': ['広島'],
            'target_label': '相席CLOVERS 広島本店 限定',
            'created_at': today_s,
        },
        {
            'id': 'alfa-hiroshima-men-early',
            'brand': 'ALFA',
            'brand_label': 'ALFA広島',
            'store_name': 'ALFA HIROSHIMA',
            'title': '【広島ALFA】男性相席10分500円〜（税込・飲み放題込）＆早割・1人参戦歓迎プラン',
            'url': 'https://aiseki-hiroshima.com/',
            'banner_url': '',
            'gender': 'men',
            'gender_label': '男性特典',
            'date_str': '開催中',
            'tag': '割引・特典',
            'tag_cls': 'tag-discount',
            'target_stores': ['広島'],
            'target_label': '相席ラウンジALFA 広島 限定',
            'created_at': today_s,
        },
        {
            'id': 'alfa-hiroshima-karaoke',
            'brand': 'ALFA',
            'brand_label': 'ALFA広島',
            'store_name': 'ALFA HIROSHIMA',
            'title': '【広島ALFA】新天地グランポルトビル・カラオケ個室完備＆週末パーティーナイト',
            'url': 'https://aiseki-hiroshima.com/',
            'banner_url': '',
            'gender': 'all',
            'gender_label': '全店共通',
            'date_str': '開催中',
            'tag': 'フェア・企画',
            'tag_cls': 'tag-event',
            'target_stores': ['広島'],
            'target_label': '相席ラウンジALFA 広島 限定',
            'created_at': today_s,
        },
        {
            'id': 'alfa-hiroshima-women-free',
            'brand': 'ALFA',
            'brand_label': 'ALFA広島',
            'store_name': 'ALFA HIROSHIMA',
            'title': '【広島ALFA】女性限定・入場0円＆ビュッフェ・スイーツ食べ飲み放題キャンペーン',
            'url': 'https://aiseki-hiroshima.com/',
            'banner_url': '',
            'gender': 'women',
            'gender_label': '女性限定',
            'date_str': '開催中',
            'tag': '割引・特典',
            'tag_cls': 'tag-discount',
            'target_stores': ['広島'],
            'target_label': '相席ラウンジALFA 広島 限定',
            'created_at': today_s,
        },
        {
            'id': 'xix-okayama-u29-plan',
            'brand': 'XIX',
            'brand_label': 'XIX岡山',
            'store_name': 'XIX OKAYAMA',
            'title': '【岡山XIX】U25/U29男性限定・平日早割（〜20:30入店）60分980円〜1,980円プラン',
            'url': 'https://xix-thelounge.com/',
            'banner_url': '',
            'gender': 'men',
            'gender_label': '男性特典',
            'date_str': '開催中',
            'tag': '割引・特典',
            'tag_cls': 'tag-discount',
            'target_stores': ['岡山'],
            'target_label': 'XIX 岡山 限定',
            'created_at': today_s,
        },
        {
            'id': 'xix-okayama-friday-pass',
            'brand': 'XIX',
            'brand_label': 'XIX岡山',
            'store_name': 'XIX OKAYAMA',
            'title': '【岡山XIX】金曜20代男性限定「定額相席し放題」＆LINE会員チャージ0円特典',
            'url': 'https://xix-thelounge.com/',
            'banner_url': '',
            'gender': 'men',
            'gender_label': '男性特典',
            'date_str': '開催中',
            'tag': '割引・特典',
            'tag_cls': 'tag-discount',
            'target_stores': ['岡山'],
            'target_label': 'XIX 岡山 限定',
            'created_at': today_s,
        },
        {
            'id': 'xix-okayama-scratch',
            'brand': 'XIX',
            'brand_label': 'XIX岡山',
            'store_name': 'XIX OKAYAMA',
            'title': '【岡山XIX】豪華コスメ・テーマパーク券・ギフト券が当たる相席スクラッチ企画',
            'url': 'https://xix-thelounge.com/',
            'banner_url': '',
            'gender': 'women',
            'gender_label': '女性限定',
            'date_str': '開催中',
            'tag': 'フェア・企画',
            'tag_cls': 'tag-event',
            'target_stores': ['岡山'],
            'target_label': 'XIX 岡山 限定',
            'created_at': today_s,
        },
    ]


def crawl_regional_brand_events() -> list:
    """Crawl live news/campaign headlines from CLOVERS Hiroshima (bar-clovers.com),
    ALFA Hiroshima (aiseki-hiroshima.com), and XIX Okayama (xix-thelounge.com),
    merged with verified baseline regional campaigns."""
    scraped = []
    today_s = datetime.now().strftime('%Y-%m-%d')
    targets = [
        ('CLOVERS', 'CLOVERS広島', 'CLOVERS HIROSHIMA', '広島', 'https://bar-clovers.com/'),
        ('ALFA', 'ALFA広島', 'ALFA HIROSHIMA', '広島', 'https://aiseki-hiroshima.com/'),
        ('XIX', 'XIX岡山', 'XIX OKAYAMA', '岡山', 'https://xix-thelounge.com/'),
    ]
    for brand_code, brand_label, sname, area, url in targets:
        try:
            res = requests.get(url, headers=HEADERS, timeout=5)
            if res.status_code != 200:
                continue
            soup = BeautifulSoup(res.text, 'html.parser')
            candidates = []
            for sel in ('article h2', 'article h3', '.news-list li', '.news li', '.event-list li', 'h3', 'h4'):
                for node in soup.select(sel)[:6]:
                    txt = _clean_text(node.get_text())
                    if 8 <= len(txt) <= 68 and any(
                        kw in txt for kw in ('割', '半額', '無料', '円', 'プレゼント', 'フェア', 'イベント', '限定', 'プラン', 'キャンペーン', 'クーポン', '相席')
                    ):
                        candidates.append(txt)
            seen = set()
            for idx, title in enumerate(candidates[:3]):
                if title in seen:
                    continue
                seen.add(title)
                tag_label, tag_cls = _classify_tag(title)
                full_t = title if f'【{area}' in title else f'【{brand_label}】{title}'
                scraped.append({
                    'id': f"{brand_code.lower()}-live-{idx}",
                    'brand': brand_code,
                    'brand_label': brand_label,
                    'store_name': sname,
                    'title': full_t,
                    'url': url,
                    'banner_url': '',
                    'gender': 'men' if any(k in title for k in ('男性', '半額', '早割', 'プラン')) else 'all',
                    'gender_label': '店舗限定',
                    'date_str': '開催中',
                    'tag': tag_label,
                    'tag_cls': tag_cls,
                    'target_stores': [area],
                    'target_label': f'{brand_label} 限定',
                    'created_at': today_s,
                })
        except Exception as e:
            logger.warning(f"Regional event crawl skipped for {sname}: {e}")

    # Merge with curated baseline (avoiding duplicate titles)
    existing_titles = {e['title'] for e in scraped}
    for base_ev in get_regional_curated_events():
        if base_ev['title'] not in existing_titles:
            scraped.append(base_ev)
    return scraped


def _crawl_and_save_events():
    """Perform live event crawl across brands and update memory + disk cache, preserving previous brand cache on partial network errors."""
    global _events_memory_cache
    now_jst = datetime.now(_JST)

    # Load existing disk cache if memory cache is still empty so fallback protection works on first crawl
    existing_events = list(_events_memory_cache.get('events') or [])
    if not existing_events and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                existing_events = json.load(f).get('events') or []
        except Exception:
            existing_events = []

    olg_events = crawl_oriental_events()
    jis_store_events = crawl_jis_store_events()
    jis_global_events = crawl_jis_global_events()
    regional_events = crawl_regional_brand_events()

    # Protect against transient DNS/timeout wiping out 150+ cached OLG/JIS events with only static regional items
    if not olg_events:
        olg_events = [e for e in existing_events if e.get('brand') == 'OLG']
    jis_combined = jis_store_events + jis_global_events
    if not jis_combined:
        jis_combined = [e for e in existing_events if e.get('brand') == 'JIS']

    all_events = _ensure_regional_events(olg_events + jis_combined + regional_events)
    if all_events:
        _events_memory_cache = {
            'updated_at': now_jst.strftime('%Y-%m-%d %H:%M:%S'),
            'events': all_events,
            'operational_notices': get_operational_notices(),
        }
        try:
            os.makedirs(os.path.dirname(CACHE_FILE), exist_ok=True)
            with open(CACHE_FILE, 'w', encoding='utf-8') as f:
                json.dump(_events_memory_cache, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.warning(f"Failed to save events cache to disk: {e}")
        return all_events
    return existing_events


def _trigger_background_refresh():
    """Refresh events cache asynchronously in a daemon thread so HTTP requests never block."""
    if os.environ.get("DISABLE_SHEETS") == "1" or os.environ.get("DISABLE_SCHEDULER") == "1":
        return
    global _is_refreshing
    with _refresh_lock:
        if _is_refreshing:
            return
        _is_refreshing = True

    def _worker():
        global _is_refreshing
        try:
            _crawl_and_save_events()
        except Exception as e:
            logger.error(f"Background events crawl failed: {e}")
        finally:
            with _refresh_lock:
                _is_refreshing = False

    threading.Thread(target=_worker, daemon=True).start()


def _ensure_regional_events(events_list: list) -> list:
    """Ensure regional brand events (ALFA, CLOVERS, XIX) are included and normalize legacy cached events
    so multi-store campaigns (e.g. 【全国17店舗】北海道フェア🌽) or ambiguous tokens never leak into non-participating stores."""
    if not events_list:
        return list(get_regional_curated_events())

    sanitized = []
    for ev in events_list:
        title = ev.get('title', '')
        if ev.get('brand') == 'OLG':
            if _is_non_campaign_notice(title):
                continue
            ts = ev.get('target_stores') or []
            if not ts or '17店舗' in title or any(not str(x).startswith('OLG ') for x in ts):
                new_ts, new_lbl = extract_olg_target_stores(title)
                if new_ts:
                    ev = dict(ev)
                    ev['target_stores'] = new_ts
                    ev['target_label'] = new_lbl
                elif not ts:
                    ev = dict(ev)
                    ev['target_label'] = new_lbl
        sanitized.append(ev)

    if any(e.get('brand') in ('ALFA', 'CLOVERS', 'XIX') for e in sanitized):
        return sanitized
    existing_ids = {e.get('id') for e in sanitized}
    merged = list(sanitized)
    for rev in get_regional_curated_events():
        if rev.get('id') not in existing_ids:
            merged.append(rev)
    return merged


def fetch_all_events(force_refresh: bool = False, allow_blocking: bool = True):
    """Fetch all events from all sources and update memory cache and disk cache.
    When allow_blocking=False (e.g. from /api/events), returns cached data immediately
    even if expired and refreshes in a background thread.
    """
    global _events_memory_cache

    now_jst = datetime.now(_JST).replace(tzinfo=None)

    # Load from disk cache first if memory is empty
    if not _events_memory_cache.get('events') and os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, 'r', encoding='utf-8') as f:
                disk_data = json.load(f)
                if disk_data.get('events'):
                    disk_data['events'] = _ensure_regional_events(disk_data['events'])
                    _events_memory_cache = disk_data
        except Exception as e:
            logger.warning(f"Failed to read events cache from disk: {e}")

    # Check if cache is fresh (TTL: 2 hours)
    is_fresh = False
    if not force_refresh and _events_memory_cache.get('updated_at'):
        try:
            cached_time = datetime.strptime(_events_memory_cache['updated_at'], '%Y-%m-%d %H:%M:%S')
            if 0 <= (now_jst - cached_time).total_seconds() < 7200:
                is_fresh = True
        except Exception:
            pass

    if is_fresh and _events_memory_cache.get('events'):
        _events_memory_cache['events'] = _ensure_regional_events(_events_memory_cache['events'])
        return _events_memory_cache['events']

    # Non-blocking mode for web requests: return existing cache immediately & refresh in background
    if not allow_blocking and not force_refresh:
        _trigger_background_refresh()
        return _ensure_regional_events(_events_memory_cache.get('events', []))

    return _crawl_and_save_events()


def get_events_for_store(
    store_name: str,
    store_only: bool = False,
    allow_blocking: bool = True,
    preloaded_events: list = None,
):
    """Filter events relevant to a specific store (prioritizes store-specific banners and campaigns)."""
    all_events = preloaded_events if preloaded_events is not None else fetch_all_events(allow_blocking=allow_blocking)
    matched = []
    global_matched = []

    clean_name = store_name.upper()
    is_jis = 'JIS' in clean_name
    is_olg = 'OLG' in clean_name or 'ORIENTAL' in clean_name or 'AG' in clean_name

    for ev in all_events:
        t = ev.get('title', '')
        # Filter out ubiquitous default perks and administrative notices
        if 'フード食べ放題' in t or '女性限定フード' in t or _is_non_campaign_notice(t):
            continue

        ev_brand = ev.get('brand', '')
        if ev.get('store_name') and ev.get('store_name').upper() == clean_name:
            matched.append(ev)
            continue

        if is_jis and ev_brand == 'JIS':
            target_stores = ev.get('target_stores', [])
            if not target_stores:
                if not store_only and ev.get('target_label') in ('全店共通', 'JIS全店'):
                    global_matched.append(ev)
            elif any(_store_matches_area(store_name, area) for area in target_stores):
                matched.append(ev)
        elif is_olg and ev_brand == 'OLG':
            target_stores = ev.get('target_stores', [])
            if not target_stores and ('17店舗' in t or '【' in t):
                inferred_ts, inferred_lbl = extract_olg_target_stores(t)
                if inferred_ts:
                    target_stores = inferred_ts
            if not target_stores:
                if not store_only and ev.get('target_label') == '全店共通' and ('【全店】' in t or '【全店舗】' in t):
                    global_matched.append(ev)
            elif any(_store_matches_area(store_name, area) for area in target_stores):
                matched.append(ev)

    # Deduplicate store-specific events by cleaned title
    seen_titles = set()
    deduped_matched = []
    for ev in matched:
        t_key = re.sub(r'【[^】]+】', '', ev.get('title', '')).strip()
        if t_key and t_key in seen_titles:
            continue
        seen_titles.add(t_key)
        deduped_matched.append(ev)

    # Return store-specific events first, then brand-wide events
    return deduped_matched if store_only else (deduped_matched + global_matched)
