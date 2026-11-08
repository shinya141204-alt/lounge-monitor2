"""
analytics.py - 自前の軽量アクセス集計（個人情報なし）

「どの記事が読まれ、どのボタン・アフィリエイトがクリックされているか」を
日別・イベント別・ラベル別の件数だけで集計します（IP・端末IDなどは保存しません）。

・メモリ上で加算 → 10分ごとに Google Sheets 'Analytics Daily' へまとめて追記
・集計画面（/staff/analytics）は Sheets の値＋未送信分を合算して表示
"""
import os
import datetime
import time
import threading
import logging
from collections import defaultdict

import sheets_store
import turbo_store

SHEET_TITLE = 'Analytics Daily'
HEADER = ['date_jst', 'event', 'label', 'count']

HOURLY_SHEET_TITLE = 'Analytics Hourly'
HOURLY_HEADER = ['date_jst', 'hour_jst', 'bucket', 'count']
_JST = datetime.timezone(datetime.timedelta(hours=9))

ALLOWED_EVENTS = {
    'page_view',             # 記事・ポータルページの閲覧（サーバー側で記録）
    'affiliate_click',       # /link/<id> 経由のアフィリエイトクリック（サーバー側で記録）
    'affiliate_impression',  # 画面内（IntersectionObserver）に表示されたPR枠インプレッション
    'cushion_click',         # コラム・診断等から2ステップ内部クッション（/grooming・/for-women）への遷移
    'store_open',            # 店舗詳細モーダルを開いた
    'map_cafe',              # 2軒目: 夜カフェのマップ検索
    'map_bar',               # 2軒目: バー・バルのマップ検索
    'official_click',        # 公式サイトへのリンク
    'sns_click',             # X/Threads/note への導線
    'share',                 # LINE/X 共有・コピー
    'vote',                  # 現場投票
    'vote_wait',             # 現場投票に付随する待ち時間申告（store:15m 形式）
    'watch_intent',          # 条件ウォッチ登録（store:condition 形式）
    'sns_inflow',            # ?s= 識別子付きのSNS投稿からのサイト流入・CV追跡
}

# distinct (date, event, label) キー上限。超過時は新規ラベルを '__overflow__' に丸めてメモリ膨張を防ぐ
_PENDING_MAX_KEYS = 5000
_OVERFLOW_LABEL = '__overflow__'

_lock = threading.Lock()
_pending = defaultdict(int)   # (date, event, label) -> count（未フラッシュ分）
_sheets_retry = defaultdict(int)  # Postgres保存済み・Sheetsのみ再送待ち分
_pg_retry = defaultdict(int)      # Sheets保存済み・Postgresのみ再送待ち分
_flushed = defaultdict(int)   # 起動後に送信済みの分（Sheets読込失敗時の表示用）
_SHEET_ROWS_CACHE_TTL = 900.0
_sheet_rows_cache = {'ts': 0.0, 'rows': None}
_sheet_refresh_in_flight = False
_pg_seeded_from_sheets = False
_pg_seed_in_flight = False

_hourly_pending = defaultdict(int)       # (date_jst, hour_jst, bucket) -> count
_hourly_sheets_retry = defaultdict(int)  # Postgres保存済み・Sheetsのみ再送待ち分
_hourly_pg_retry = defaultdict(int)      # Sheets保存済み・Postgresのみ再送待ち分
_hourly_flushed = defaultdict(int)       # 起動後に送信済みの分（Sheets読込失敗時の表示用）
_hourly_sheet_rows_cache = {'ts': 0.0, 'rows': None}
_hourly_sheet_refresh_in_flight = False
_pg_hourly_seeded_from_sheets = False
_pg_hourly_seed_in_flight = False


def _refresh_hourly_sheet_rows_async():
    """Refresh Analytics Hourly rows from Google Sheets in a background daemon thread (Stale-While-Revalidate)."""
    global _hourly_sheet_refresh_in_flight
    if sheets_store.is_disabled():
        return
    with _lock:
        if _hourly_sheet_refresh_in_flight:
            return
        _hourly_sheet_refresh_in_flight = True

    def _worker():
        global _hourly_sheet_refresh_in_flight
        try:
            rows = sheets_store.read_hourly_rows_or_none(HOURLY_SHEET_TITLE, HOURLY_HEADER)
            if rows is not None:
                with _lock:
                    _hourly_sheet_rows_cache['ts'] = time.monotonic()
                    _hourly_sheet_rows_cache['rows'] = rows
        except Exception as err:
            logging.warning(f"[analytics] async hourly sheet refresh failed: {err}")
        finally:
            with _lock:
                _hourly_sheet_refresh_in_flight = False

    threading.Thread(target=_worker, daemon=True).start()


def _refresh_sheet_rows_async():
    """Refresh Analytics Daily rows from Google Sheets in a background daemon thread (Stale-While-Revalidate)."""
    global _sheet_refresh_in_flight
    if sheets_store.is_disabled():
        return
    with _lock:
        if _sheet_refresh_in_flight:
            return
        _sheet_refresh_in_flight = True

    def _worker():
        global _sheet_refresh_in_flight
        try:
            rows = sheets_store.read_rows_or_none(SHEET_TITLE, HEADER)
            if rows is not None:
                with _lock:
                    _sheet_rows_cache['ts'] = time.monotonic()
                    _sheet_rows_cache['rows'] = rows
        except Exception as err:
            logging.warning(f"[analytics] async sheet refresh failed: {err}")
        finally:
            with _lock:
                _sheet_refresh_in_flight = False

    threading.Thread(target=_worker, daemon=True).start()


def _today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d')


_CTA_SURFACE_LABELS = {
    '/': 'トップページ（ランキング・おすすめモーダル）',
    '/store/*': '店舗詳細ページ（リアルタイム状況連動CTA・2軒目枠）',
    '/concierge': '3秒穴場＆徒歩ハシゴ診断（スマートアクション枠）',
    '/grooming': '入店前の身だしなみ・清潔感ガイド',
    '/for-women': '女性向けリアルタイム待ちゼロ店＆無料デート特設',
    '/price-comparison': '全店料金・開催中イベント比較ボード',
    '/area/*': 'エリア別ポータルページ',
    '/guide/*': 'データ考察コラム・攻略ガイド',
    '/brand/*': 'ブランド別全店リアルタイム一覧',
    '/other': 'その他ページ',
}


def normalize_cta_surface(raw_path: str) -> str:
    """Group page paths into bounded surface buckets so impression rows stay compact in Google Sheets."""
    p = str(raw_path or '').strip().split('?', 1)[0].split('#', 1)[0].strip()
    if not p or p == '-':
        return '/other'
    if not p.startswith('/'):
        p = '/' + p
    if p == '/':
        return '/'
    if p == '/store/*' or p.startswith('/store/'):
        return '/store/*'
    if p in ('/concierge', '/grooming', '/for-women', '/price-comparison'):
        return p
    if p == '/stats' or p.startswith('/stats/'):
        return '/stats'
    if p in ('/areas', '/area/*') or p.startswith('/area/'):
        return '/area/*'
    if p in ('/guide', '/columns', '/guide/*') or p.startswith('/guide/'):
        return '/guide/*'
    if p == '/brand/*' or p in ('/oriental-lounge', '/ag', '/jis', '/asobibar'):
        return '/brand/*'
    return '/other'


def normalize_affiliate_impression_label(raw_label: str) -> str:
    """Validate slot against affiliate_manager and normalize '<canonical_slot> @ <surface_bucket>'."""
    raw = str(raw_label or '').strip()
    if not raw:
        return ''
    if '@' in raw:
        slot_part, path_part = [x.strip() for x in raw.split('@', 1)]
    else:
        slot_part, path_part = raw, '/'
    try:
        import affiliate_manager
        cid = affiliate_manager.canonical_slot_id(slot_part)
    except Exception:
        cid = ''
    if not cid:
        return ''
    surface = normalize_cta_surface(path_part)
    # AGENTS.md §9: primary statistics pages (/stats) never host affiliate slots
    if surface == '/stats':
        return ''
    return f"{cid} @ {surface}"


def normalize_cushion_click_label(raw_label: str) -> str:
    """
    Validate and normalize 2-step internal cushion transitions (`<target_hub[#anchor]> @ <source_path>`).
    Accepts `/grooming`, `/for-women`, `/concierge`, `/price-comparison`, and `/store/<slug>` as targets,
    and rejects self-page anchor jumps or external paths.
    """
    import re
    raw = str(raw_label or '').strip()
    if not raw or '@' not in raw:
        return ''
    target_part, src_part = [x.strip() for x in raw.split('@', 1)]
    if not target_part or not src_part:
        return ''
    t_no_q = target_part.split('?', 1)[0].strip()
    if '#' in t_no_q:
        t_path, t_frag = t_no_q.split('#', 1)
    else:
        t_path, t_frag = t_no_q, ''
    t_path = t_path.rstrip('/') if t_path != '/' else '/'
    is_valid_hub = t_path in ('/grooming', '/for-women', '/concierge', '/price-comparison')
    is_valid_store = bool(re.fullmatch(r'/store/[A-Za-z0-9_-]{1,64}', t_path))
    if not (is_valid_hub or is_valid_store):
        return ''
    clean_frag = re.sub(r'[^a-zA-Z0-9_-]', '', t_frag)[:28]

    # Parse and sanitize internal source path
    s_clean = src_part.split('?', 1)[0].split('#', 1)[0].strip()
    if not s_clean.startswith('/') or s_clean.startswith('//') or '..' in s_clean or '\\' in s_clean:
        return ''
    if not re.match(r'^/[A-Za-z0-9_/\-]*$', s_clean):
        return ''
    if len(s_clean) > 1:
        s_clean = s_clean.rstrip('/')
    s_clean = s_clean[:48]
    # Exclude same-page anchor jumps
    if s_clean == t_path or s_clean.startswith('/staff'):
        return ''

    norm_target = f"{t_path}#{clean_frag}" if clean_frag else t_path
    return f"{norm_target} @ {s_clean}"


_STORE_TRACK_EVENTS = ('store_open', 'map_cafe', 'map_bar', 'official_click', 'vote', 'vote_wait', 'watch_intent')
_STORE_SUFFIX_EVENTS = ('vote', 'vote_wait', 'watch_intent')


def sanitize_generic_track_label(raw_label: str) -> str:
    """Strip control characters and neutralize spreadsheet formula injection prefixes (=, +, -, @)."""
    s = str(raw_label or '').replace('\r', ' ').replace('\n', ' ').replace('\t', ' ').strip()[:80]
    while s and s[0] in ('=', '+', '-', '@'):
        if s == '-':
            break
        s = s[1:].lstrip()
    return s


def normalize_store_event_label(event: str, raw_label: str) -> str:
    """Unify SEO display names / slugs (e.g. 'オリエンタルラウンジ 長崎') into canonical internal keys ('OLG 長崎')."""
    s = sanitize_generic_track_label(raw_label)
    if not s or event not in _STORE_TRACK_EVENTS:
        return s
    if event in _STORE_SUFFIX_EVENTS and ':' in s:
        sname_raw, suffix = [p.strip() for p in s.split(':', 1)]
    else:
        sname_raw, suffix = s, ''
    canon = sname_raw
    try:
        import store_master
        master = store_master.load() or {}
        if sname_raw not in master:
            cand = store_master.to_internal_store_name(sname_raw)
            if cand and cand in master:
                canon = cand
            else:
                by_slug = store_master.store_by_slug(sname_raw)
                if by_slug and by_slug in master:
                    canon = by_slug
    except Exception:
        pass
    return f"{canon}:{suffix}" if (event in _STORE_SUFFIX_EVENTS and suffix) else canon


_FALLBACK_AFF_ALIAS_MAP = {
    'dating_instant': 'dating_instant',
    'dating': 'dating_instant',
    'dating_casual': 'dating_instant',
    'dating_bachelor': 'dating_bachelor',
    'mens_hair_removal': 'mens_hair_removal',
    'hair_removal': 'mens_hair_removal',
    'hair_removal_trial': 'mens_hair_removal',
    'mens_skincare_clinic': 'mens_skincare_clinic',
    'orbis': 'mens_skincare_clinic',
    'spu_styleup': 'spu_styleup',
    'spu': 'spu_styleup',
    'women_dating_bachelor': 'women_dating_bachelor',
    'women_dating': 'women_dating_bachelor',
    'women_beauty_clinic': 'women_beauty_clinic',
    'women_clinic': 'women_beauty_clinic',
    'women_hair_removal': 'women_beauty_clinic',
    'women_cosme_salon': 'women_cosme_salon',
    'women_cosme': 'women_cosme_salon',
    'women_skincare': 'women_cosme_salon',
    'night_care_supplement': 'night_care_supplement',
    'hangover_supplement': 'night_care_supplement',
    'supplement': 'night_care_supplement',
    'night_supplement': 'night_care_supplement',
    'women_night_supplement': 'night_care_supplement',
    'second_venue_reservation': 'second_venue_reservation',
    'ikyu_restaurant': 'second_venue_reservation',
    'ikyu': 'second_venue_reservation',
    'second_venue': 'second_venue_reservation',
    'women_second_venue': 'second_venue_reservation',
}


def classify_hourly_bucket(event: str, norm_label: str) -> str:
    """
    Map `(event, normalized_label)` into a strictly bounded bucket key for `Analytics Hourly`.
    `page_view` is partitioned into 9 mutually exclusive buckets (`pv_*`) so their sum equals exact `pv_total`.
    """
    ev = str(event or '').strip()
    lbl = str(norm_label or '').strip()
    if ev == 'page_view':
        p = lbl.split('?', 1)[0].split('#', 1)[0].strip() or '/'
        if not p.startswith('/'):
            p = '/' + p
        if len(p) > 1:
            p = p.rstrip('/')
        if p == '/':
            return 'pv_top'
        if p.startswith('/store/'):
            return 'pv_store'
        if p in ('/columns', '/guide') or p.startswith('/guide/'):
            return 'pv_guide'
        if p == '/concierge':
            return 'pv_concierge'
        if p == '/for-women':
            return 'pv_women'
        if p == '/grooming':
            return 'pv_grooming'
        if p == '/price-comparison':
            return 'pv_price'
        if p == '/stats' or p.startswith('/stats/'):
            return 'pv_stats'
        return 'pv_other'
    if ev == 'affiliate_click':
        slot_part = lbl.split('@', 1)[0].strip() if '@' in lbl else lbl
        cid = ''
        try:
            import affiliate_manager
            cid = affiliate_manager.canonical_slot_id(slot_part)
        except Exception:
            cid = ''
        if not cid:
            cid = _FALLBACK_AFF_ALIAS_MAP.get(slot_part.lower(), '')
        return f"aff_clk:{cid}" if cid else 'aff_clk:other'
    if ev == 'affiliate_impression':
        return 'aff_imp'
    if ev == 'store_open':
        return 'store_open'
    if ev == 'cushion_click':
        return 'cushion_click'
    if ev == 'sns_inflow':
        return 'sns_inflow'
    if ev in ('map_cafe', 'map_bar'):
        return 'map_search'
    if ev in ('official_click', 'sns_click', 'share'):
        return 'external_engage'
    if ev == 'vote':
        return 'vote'
    # 待ち時間申告は vote と同時送信されるため 'vote' に合算すると二重計上になる → 独立バケット
    if ev == 'vote_wait':
        return 'vote_wait'
    if ev == 'watch_intent':
        return 'watch_intent'
    return ''


def track(event, label='', _now_jst=None):
    if event not in ALLOWED_EVENTS:
        return False
    if event == 'affiliate_impression':
        norm_lbl = normalize_affiliate_impression_label(label)
        if not norm_lbl:
            return False
        label = norm_lbl
    elif event == 'cushion_click':
        norm_lbl = normalize_cushion_click_label(label)
        if not norm_lbl:
            return False
        label = norm_lbl
    elif event in _STORE_TRACK_EVENTS:
        label = normalize_store_event_label(event, label)
    else:
        label = sanitize_generic_track_label(label)

    if _now_jst is not None:
        dt_jst = _now_jst.replace(tzinfo=_JST) if _now_jst.tzinfo is None else _now_jst.astimezone(_JST)
    else:
        dt_jst = datetime.datetime.now(_JST)
    date_str = dt_jst.strftime('%Y-%m-%d')
    hour_int = int(dt_jst.hour)

    h_bucket = classify_hourly_bucket(event, label)
    with _lock:
        p_key = (date_str, event, label)
        if p_key not in _pending and len(_pending) >= _PENDING_MAX_KEYS:
            p_key = (date_str, event, _OVERFLOW_LABEL)
        _pending[p_key] += 1
        if h_bucket:
            _hourly_pending[(date_str, hour_int, h_bucket)] += 1
    return True


def _flush_hourly(force_sheets: bool, cutoff_3d: str) -> None:
    """Flush hourly bucket counters alongside daily counters without altering `flush()` return contract."""
    if not force_sheets:
        if not turbo_store.is_postgres_active():
            return
        with _lock:
            for k in [k for k in _hourly_flushed.keys() if k[0] <= cutoff_3d]:
                del _hourly_flushed[k]
            for k in [k for k in _hourly_sheets_retry.keys() if k[0] <= cutoff_3d]:
                del _hourly_sheets_retry[k]
            h_new_items = list(_hourly_pending.items())
            _hourly_pending.clear()
            h_pg_retry_items = list(_hourly_pg_retry.items())
            _hourly_pg_retry.clear()
        if not h_new_items and not h_pg_retry_items:
            return
        h_combined_pg = defaultdict(int)
        for k, c in h_pg_retry_items:
            h_combined_pg[k] += c
        for k, c in h_new_items:
            h_combined_pg[k] += c
        h_pg_saved = turbo_store.analytics_hourly_add_batch(list(h_combined_pg.items()))
        with _lock:
            if h_pg_saved:
                for key, c in h_new_items:
                    _hourly_sheets_retry[key] += c
            else:
                for key, c in h_pg_retry_items:
                    _hourly_pg_retry[key] += c
                for key, c in h_new_items:
                    _hourly_pending[key] += c
        return

    with _lock:
        for k in [k for k in _hourly_flushed.keys() if k[0] <= cutoff_3d]:
            del _hourly_flushed[k]
        for k in [k for k in _hourly_sheets_retry.keys() if k[0] <= cutoff_3d]:
            del _hourly_sheets_retry[k]
        h_new_items = list(_hourly_pending.items())
        _hourly_pending.clear()
        h_retry_items = list(_hourly_sheets_retry.items())
        _hourly_sheets_retry.clear()
        h_pg_retry_items = list(_hourly_pg_retry.items())
        _hourly_pg_retry.clear()
    if not h_new_items and not h_retry_items and not h_pg_retry_items:
        return

    h_pg_saved = False
    if h_new_items or h_pg_retry_items:
        h_combined_pg = defaultdict(int)
        for k, c in h_pg_retry_items:
            h_combined_pg[k] += c
        for k, c in h_new_items:
            h_combined_pg[k] += c
        h_pg_saved = turbo_store.analytics_hourly_add_batch(list(h_combined_pg.items()))
    elif h_retry_items and turbo_store.is_postgres_active():
        h_pg_saved = True

    h_merged_sheets = defaultdict(int)
    for k, c in h_retry_items:
        h_merged_sheets[k] += c
    for k, c in h_new_items:
        h_merged_sheets[k] += c

    h_rows = [[d, int(h), b, c] for (d, h, b), c in h_merged_sheets.items()]
    if h_rows and turbo_store.is_named_mutex_held('sheets_analytics_rewrite'):
        h_ok = False
    else:
        h_ok = sheets_store.append_hourly_rows(HOURLY_SHEET_TITLE, HOURLY_HEADER, h_rows) if h_rows else True
    with _lock:
        if h_ok:
            for key, c in h_merged_sheets.items():
                if key[0] > cutoff_3d:
                    _hourly_flushed[key] += c
            if _hourly_sheet_rows_cache['rows'] is not None and h_rows:
                _hourly_sheet_rows_cache['rows'] = (
                    list(_hourly_sheet_rows_cache['rows'])
                    + [[d, str(h), b, str(c)] for d, h, b, c in h_rows]
                )
            _hourly_sheet_rows_cache['ts'] = 0.0
            if not h_pg_saved and turbo_store.is_postgres_active():
                for key, c in h_pg_retry_items:
                    if key[0] > cutoff_3d:
                        _hourly_pg_retry[key] += c
                for key, c in h_new_items:
                    if key[0] > cutoff_3d:
                        _hourly_pg_retry[key] += c
        else:
            for key, c in h_retry_items:
                if key[0] > cutoff_3d:
                    _hourly_sheets_retry[key] += c
            if h_pg_saved:
                for key, c in h_new_items:
                    if key[0] > cutoff_3d:
                        _hourly_sheets_retry[key] += c
            else:
                for key, c in h_pg_retry_items:
                    if key[0] > cutoff_3d:
                        _hourly_pg_retry[key] += c
                for key, c in h_new_items:
                    _hourly_pending[key] += c


def flush(force_sheets=True):
    """未送信分を PostgreSQL / Sheets に追記。
    force_sheets=False の場合（Webワーカーの60秒周期フラッシュ）は PostgreSQL へ即時合算し、
    Google Sheets へは次回 force_sheets=True（10分周期・手動フラッシュ・プロセス終了時）にまとめて追記する。
    """
    cutoff_3d = (
        datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        - datetime.timedelta(days=3)
    ).strftime('%Y-%m-%d')

    _flush_hourly(force_sheets=bool(force_sheets), cutoff_3d=cutoff_3d)

    if not force_sheets:
        if not turbo_store.is_postgres_active():
            return 0
        with _lock:
            for k in [k for k in _flushed.keys() if k[0] <= cutoff_3d]:
                del _flushed[k]
            for k in [k for k in _sheets_retry.keys() if k[0] <= cutoff_3d]:
                del _sheets_retry[k]
            new_items = list(_pending.items())
            _pending.clear()
            pg_retry_items = list(_pg_retry.items())
            _pg_retry.clear()
        if not new_items and not pg_retry_items:
            return 0
        combined_pg = defaultdict(int)
        for k, c in pg_retry_items:
            combined_pg[k] += c
        for k, c in new_items:
            combined_pg[k] += c
        pg_saved = turbo_store.analytics_add_batch(list(combined_pg.items()))
        with _lock:
            if pg_saved:
                for key, c in new_items:
                    _sheets_retry[key] += c
            else:
                for key, c in pg_retry_items:
                    _pg_retry[key] += c
                for key, c in new_items:
                    _pending[key] += c
        return len(combined_pg) if pg_saved else 0

    with _lock:
        for k in [k for k in _flushed.keys() if k[0] <= cutoff_3d]:
            del _flushed[k]
        for k in [k for k in _sheets_retry.keys() if k[0] <= cutoff_3d]:
            del _sheets_retry[k]
        new_items = list(_pending.items())
        _pending.clear()
        retry_items = list(_sheets_retry.items())
        _sheets_retry.clear()
        pg_retry_items = list(_pg_retry.items())
        _pg_retry.clear()
    if not new_items and not retry_items and not pg_retry_items:
        return 0

    pg_saved = False
    if new_items or pg_retry_items:
        combined_pg = defaultdict(int)
        for k, c in pg_retry_items:
            combined_pg[k] += c
        for k, c in new_items:
            combined_pg[k] += c
        pg_saved = turbo_store.analytics_add_batch(list(combined_pg.items()))
    elif retry_items and turbo_store.is_postgres_active():
        pg_saved = True

    merged_for_sheets = defaultdict(int)
    for k, c in retry_items:
        merged_for_sheets[k] += c
    for k, c in new_items:
        merged_for_sheets[k] += c

    rows = [[d, e, l, c] for (d, e, l), c in merged_for_sheets.items()]
    if rows and turbo_store.is_named_mutex_held('sheets_analytics_rewrite'):
        ok = False
    else:
        ok = sheets_store.append_rows(SHEET_TITLE, HEADER, rows) if rows else True
    with _lock:
        if ok:
            for key, c in merged_for_sheets.items():
                if key[0] > cutoff_3d:
                    _flushed[key] += c
            if _sheet_rows_cache['rows'] is not None and rows:
                _sheet_rows_cache['rows'] = list(_sheet_rows_cache['rows']) + [[d, e, l, str(c)] for d, e, l, c in rows]
            _sheet_rows_cache['ts'] = 0.0
            if not pg_saved and turbo_store.is_postgres_active():
                for key, c in pg_retry_items:
                    if key[0] > cutoff_3d:
                        _pg_retry[key] += c
                for key, c in new_items:
                    if key[0] > cutoff_3d:
                        _pg_retry[key] += c
        else:
            for key, c in retry_items:
                if key[0] > cutoff_3d:
                    _sheets_retry[key] += c
            if pg_saved:
                # Already persisted safely in PostgreSQL/turbo_store; only retry Google Sheets mirror next tick
                for key, c in new_items:
                    if key[0] > cutoff_3d:
                        _sheets_retry[key] += c
            else:
                for key, c in pg_retry_items:
                    if key[0] > cutoff_3d:
                        _pg_retry[key] += c
                for key, c in new_items:
                    _pending[key] += c
    if not ok and not pg_saved:
        logging.warning('[analytics] flush failed; will retry')
    return len(rows) if (ok or pg_saved) else 0


_PAGE_TITLES = {
    '/': '🔥 トップページ（全国リアルタイム人数ランキング）',
    '/concierge': '🧭 今夜どこ行く？3秒穴場＆徒歩ハシゴ診断',
    '/columns': '📚 コラム・攻略記事一覧ページ',
    '/areas': '📍 全国エリア一覧ディレクトリ',
    '/grooming': '✨ 相席ラウンジ前の身だしなみ・清潔感特集',
    '/stats': '📊 全国ラウンジ統計・混雑データレポート',
    '/price-comparison': '💴 店舗別料金・開催中イベント比較',
    '/oriental-lounge': '🥂 オリエンタルラウンジ全店リアルタイム一覧',
    '/ag': '⚡ ag（アグ）全店リアルタイム一覧',
    '/jis': '💎 JIS（ジス）全店リアルタイム一覧',
    '/asobibar': '🍸 ASOBIBAR（アソビバー）全店リアルタイム一覧',
    '/guide': '📖 相席ラウンジ完全攻略ガイド（基本編）',
    '/guide/strategy': '📖 相席ラウンジ完全攻略ガイド（基本編）',
    '/guide/brand-comparison': '📖 3大ブランド徹底比較（オリエンタルラウンジ・ag・JIS）',
    '/guide/weekday-strategy': '📖 水曜・木曜の平日穴場攻略コラム',
    '/guide/weekend-strategy': '📖 金曜・土曜ピークの待ち時間回避＆ハシゴ攻略',
    '/guide/shinjuku': '📖 新宿エリア3店舗の使い分け・穴場ガイド',
    '/guide/solo-vs-pair': '📖 1人参戦（ソロ）vs 2人組のメリット・勝率比較',
    '/guide/second-venue': '📖 2軒目の誘い方・断られない声かけタイミング',
    '/guide/first-10-minutes': '📖 最初の10分で差がつく会話術・第一印象の作り方',
    '/guide/club-area-midnight': '📖 ダンスクラブ密集エリアの深夜0時「第2波」実測データ検証',
    '/guide/beauty-area-stage': '📖 美容サロン激戦区のラウンジが「自分磨きの舞台」になる理由',
    '/guide/male-lead-mechanics': '📖 男性先行・男性比率が高いお店に女性が集まるデータ法則',
    '/guide/price-event-comparison': '📖 国内61店舗「料金・イベント・現在人数」比較ボード活用ガイド',
    '/guide/last-train-cliff': '📖 23時20分の終電崖（魔の40分間）と23時で逆転する街別ハシゴ法則',
    '/guide/local-vs-urban': '📖 地方店vs都心大型店｜深夜0時に女性が121%へ逆転増加する一極集中の法則',
    '/guide/cost-break-even': '📖 90分粘るvs40分で見切る｜男性の滞在時間と料金の損益分岐点検証',
    '/guide/rain-payday': '📖 雨の日・給料日前・連休中日の男女比変動と隠れ穴場日データ',
    '/guide/osaka-kita-vs-minami': '📖 大阪キタvsミナミ全9店舗比較｜時間帯で逆転する南北ハシゴ法則',
    '/guide/table-change-protocol': '📖 相席ラウンジの席替え（チェンジ）完全攻略｜言えない男と察する女の心理学と成功店舗の条件',
    '/guide/thirties-lounge-guide': '📖 相席ラウンジは30代・アラサーでも浮かない？｜店舗選びと客層データの境界線',
    '/guide/crowd-rush-paradox': '📖 金土22時の群衆殺到パラドックスと女性比70%安全バッファ検証',
    '/guide/brand-waveform-asymmetry': '📖 5大ブランドの時間波形と男女で真逆になる店選びデータ検証',
    '/guide/lounge-vs-screened-date': '📖 相席ラウンジ vs 審査制1対1デート徹底比較（実測ログ・使い分け境界線）',
    '/guide/dress-code-cleanliness': '📖 相席ラウンジの服装・清潔感チェックガイド（ブランド別NG・70cm照明対策）',
    '/guide/shibuya-ebisu-comparison': '📖 渋谷・恵比寿5店舗比較（山手線1駅で変わる年齢層・料金・時間波形）',
    '/guide/ueno-kinshicho-machida': '📖 上野・錦糸町・町田・柏 沿線5店舗比較（隣接2店と沿線ターミナル検証）',
    '/guide/yokohama-omiya-kanto': '📖 横浜・大宮・宇都宮・高崎 関東5店舗比較（時間波形・料金・ソロ対応）',
    '/guide/nagoya-sakae-nishiki': '📖 名古屋（栄・錦・住吉）5店舗比較（南北700mの年齢層・料金・ソロ境界）',
    '/guide/fukuoka-tenjin-kyushu': '📖 福岡天神・九州沖縄11店舗比較（大名3ブランド使い分け・全店ソロ対応）',
    '/guide/hiroshima-okayama-matsuyama': '📖 広島・岡山・松山6店舗比較（新天地半径185m競合・料金システム検証）',
    '/guide/sapporo-sendai-north': '📖 札幌すすきの・仙台国分町比較（JIS札幌・ag仙台の時間波形と曜日ルール）',
    '/guide/sunday-lounge-strategy': '📖 日曜日の相席ラウンジは行くべきか？｜21時半の撤退急落と、月曜休みの隠れ狙い目',
    '/for-women': '💄 女子のための相席ラウンジ完全ガイド＆待ちゼロ店舗ボード',
    '/privacy': '⚖️ 運営者情報・プライバシーポリシー',
}

_AFFILIATE_NAMES = {
    'spu': '👔 SPU スタイルアップ便（メンズ服コーデ）',
    'spu_styleup': '👔 SPU スタイルアップ便（メンズ服コーデ）',
    'orbis': '🧴 ORBIS Mr.（オルビス ミスター スキンケア）',
    'mens_skincare_clinic': '🧴 ORBIS Mr.（オルビス ミスター スキンケア）',
    'hair_removal': '🪒 メンズ医療脱毛・ヒゲ脱毛クリニック',
    'mens_hair_removal': '🪒 メンズ医療脱毛・ヒゲ脱毛クリニック',
    'hair_removal_trial': '🪒 メンズヒゲ脱毛 初回体験トライアル枠',
    'dating': '💝 バチェラーデート（審査制マッチングアプリ）',
    'dating_instant': '💝 近くで今日会える恋活・マッチングアプリ',
    'dating_bachelor': '🥂 審査制・ハイクラスデートアプリ（バチェラーデート等）',
    'dating_casual': '🍻 今日すぐ近くで飲める恋活アプリ（即時合流枠）',
    'women_dating_bachelor': '💄🥂 女性向け｜審査制・完全無料ハイクラスデート枠',
    'women_dating': '💄🥂 女性向け｜審査制・完全無料ハイクラスデート枠',
    'women_beauty_clinic': '💄💎 女性向け｜美容医療・美肌クリニック＆医療脱毛枠',
    'women_clinic': '💄💎 女性向け｜美容医療・美肌クリニック＆医療脱毛枠',
    'women_hair_removal': '💄💎 女性向け｜美容医療・美肌クリニック＆医療脱毛枠',
    'women_cosme_salon': '💄🧴 女性向け｜デパコス・ご褒美スキンケア＆サロン枠',
    'women_cosme': '💄🧴 女性向け｜デパコス・ご褒美スキンケア＆サロン枠',
    'women_skincare': '💄🧴 女性向け｜デパコス・ご褒美スキンケア＆サロン枠',
    'night_care_supplement': '🥂 乾杯サポート・ナイトケアサプリ',
    'hangover_supplement': '🥂 乾杯サポート・ナイトケアサプリ',
    'supplement': '🥂 乾杯サポート・ナイトケアサプリ',
    'night_supplement': '🥂 乾杯サポート・ナイトケアサプリ',
    'women_night_supplement': '💄🥂 女性向け｜乾杯サポート・ナイトケアサプリ',
    'second_venue_reservation': '🍽️ 一休.comレストラン（2軒目・ディナー予約枠）',
    'ikyu_restaurant': '🍽️ 一休.comレストラン（2軒目・ディナー予約枠）',
    'ikyu': '🍽️ 一休.comレストラン（2軒目・ディナー予約枠）',
    'second_venue': '🍽️ 一休.comレストラン（2軒目・ディナー予約枠）',
    'women_second_venue': '💄🍽️ 女性向け｜一休.comレストラン（女子会・アフタヌーンティー予約枠）',
}

_AREA_NAMES = {
    'shinjuku': '新宿・西新宿エリア',
    'shibuya': '渋谷・恵比寿エリア',
    'shibuya-ebisu': '渋谷・恵比寿エリア',
    'ginza': '銀座・有楽町エリア',
    'ikebukuro': '池袋エリア',
    'ueno': '上野・町田エリア',
    'tokyo-east': '上野・銀座・錦糸町・町田',
    'kanto': '横浜・大宮・千葉・北関東エリア',
    'yokohama-kanto': '横浜・大宮・柏・北関東',
    'osaka': '大阪（梅田・茶屋町・心斎橋・難波）エリア',
    'umeda': '大阪・梅田・茶屋町・天満',
    'namba': '大阪・難波・心斎橋エリア',
    'namba-shinsaibashi': '難波・心斎橋エリア',
    'yokohama': '横浜エリア',
    'omiya': '大宮エリア',
    'sapporo': '札幌・すすきのエリア',
    'sendai': '仙台・国分町エリア',
    'hiroshima': '広島・流川・紙屋町エリア',
    'kyoto-kobe': '京都・神戸エリア',
    'nagoya': '名古屋（栄・錦）エリア',
    'fukuoka': '福岡（天神・中洲・小倉）エリア',
    'fukuoka-kyushu': '福岡・九州・沖縄エリア',
    'hokkaido-tohoku': '札幌・仙台エリア',
    'chugoku-shikoku': '広島・岡山・松山・高松エリア',
    'kyushu-okinawa': '熊本・宮崎・鹿児島・沖縄エリア',
}

_SNS_NAMES = {
    'x': '𝕏 公式X（旧Twitter）',
    'threads': '🧵 公式Threads（スレッド）',
    'note': '📝 公式noteコラム',
    'instagram': '📸 公式Instagram',
}

_INFLOW_STYLE_JA = {
    'ai_live_adlib': 'AI長考アドリブ実況型',
    'hub_gap': '街別ギャップ・ハシゴ型',
    'surge_focus': '1店舗急増・穴場フォーカス型',
    'usual_anomaly': '普段比（平比）ギャップ型',
    'east_west_talk': '東西トップ対比トーク型',
    'compact_top3': '定型TOP3ランキング型',
    'threads_city_story': '街の温度差エッセイ型',
    'threads_anomaly_insight': '普段比異変＆立ち回り型',
    'threads_hopping_guide': '1軒目→2軒目比較型',
    'threads_classic_top3': '定型TOP3掲示板型',
    'threads_ladies_zero_wait': '女子得・待ちゼロ＆特典速報型',
    'ig_ladies_night_guide': '女子組・待ちゼロ＆特典保存ガイド型',
    'ladies_night': '女子得・待ちゼロ速報枠',
    'live': 'リアルタイム速報枠',
    'tip': '解説コラム紹介枠',
    'rel': '新着コラム解禁告知',
    'wk': '日曜・週間統計白書',
}


_VALID_INFLOW_RE_STR = (
    r'^(?:(?:x|th|ig|tt|tk)\d{2}_[a-z0-9_\-]{1,32}'
    r'|(?:x|th|ig|reel|tiktok|short|note|line|push)_[a-z0-9_\-]{1,32}'
    r'|x|th|ig|note|line|push_watch|line_bot)$'
)


def normalize_inflow_source(raw_s: str) -> str:
    """
    Normalize `?s=` query parameter into a clean, date-independent cohort tag so PDCA can aggregate
    clicks across days by channel + slot + editorial style:
      - 'x22_hub_gap_20261028' -> 'x22_hub_gap'
      - '20261028_2200'        -> 'x22_live'
      - 'th22_threads_city_story' -> 'th22_threads_city_story'
      - 'x18_tip'              -> 'x18_tip'
    Rejects random non-cohort query strings ('') so bot fuzzing cannot explode Analytics Daily cardinality.
    """
    import re
    s = re.sub(r'[^a-zA-Z0-9_\-]', '', (raw_s or '').strip())[:48]
    if not s:
        return ''
    # Legacy OGP cache-buster format: YYYYMMDD_HHMM
    m_leg = re.match(r'^\d{8}_(\d{2})\d{2}$', s)
    if m_leg:
        return f"x{m_leg.group(1)}_live"
    # Strip trailing _YYYYMMDD or _MMDD cache-buster suffix
    s = re.sub(r'_(\d{8}|\d{4})$', '', s)
    s_low = s.lower()[:40]
    if not re.match(_VALID_INFLOW_RE_STR, s_low):
        return ''
    return s_low


def describe_inflow_tag(tag: str) -> str:
    """Human-readable label for a normalized inflow tag like 'x22_hub_gap' or 'th22_threads_city_story'."""
    import re
    t = (tag or '').strip()
    if t.startswith('cv:'):
        inner = describe_inflow_tag(t[3:])
        return f"🎯 アフィリエイトCV（流入元: {inner}）"
    m_x = re.match(r'^x(\d{2})_(.+)$', t)
    if m_x:
        hr, st = m_x.group(1), m_x.group(2)
        return f"𝕏 X {hr}:00枠（{_INFLOW_STYLE_JA.get(st, st)}）"
    m_th = re.match(r'^th(\d{2})_(.+)$', t)
    if m_th:
        hr, st = m_th.group(1), m_th.group(2)
        return f"🧵 Threads {hr}:00枠（{_INFLOW_STYLE_JA.get(st, st)}）"
    if t == 'x_rel':
        return "𝕏 X 新着コラム解禁スレッド"
    if t == 'x_wk':
        return "𝕏 X 日曜・週間データ白書スレッド"
    if t == 'push_watch':
        return "🔔 条件ウォッチWebプッシュ通知（リピート再訪）"
    if t == 'line_bot':
        return "💬 公式LINE自動応答Bot（リピート再訪）"
    if t.startswith('ig'):
        return f"📸 Instagram（{t}）"
    return f"📥 外部SNS流入（{t}）"


def describe_label(event, raw_label):
    """プログラム用のパスやIDを直感的な日本語名称に変換する。"""
    label = (raw_label or '').strip()
    if event == 'page_view':
        if label in _PAGE_TITLES:
            return {'title': _PAGE_TITLES[label], 'sub': label, 'href': label}
        if label.startswith('/guide/'):
            gslug = label.split('/guide/', 1)[1].strip('/')
            try:
                import scheduled_articles
                sitem = scheduled_articles.get_article_by_slug(gslug, allow_blocking=False)
                if sitem:
                    return {'title': f"📖 {sitem.get('short_title') or sitem.get('title')}", 'sub': label, 'href': label}
            except Exception:
                pass
        if label.startswith('/area/'):
            slug = label.split('/area/', 1)[1].strip('/')
            aname = _AREA_NAMES.get(slug, slug)
            return {'title': f'📍 エリア別：{aname}', 'sub': label, 'href': label}
        if label.startswith('/store/'):
            slug = label.split('/store/', 1)[1].strip('/')
            try:
                import store_master
                sname = store_master.store_by_slug(slug)
                if sname:
                    return {'title': f"🏪 店舗ページ：{store_master.seo_name(sname)}", 'sub': label, 'href': label}
            except Exception:
                pass
            return {'title': f'🏪 店舗ページ：{slug}', 'sub': label, 'href': label}
        return {'title': label or '不明なページ', 'sub': '', 'href': label if label.startswith('/') else ''}

    if event in ('affiliate_click', 'affiliate_impression'):
        if '@' in label:
            item_id, ref_path = [p.strip() for p in label.split('@', 1)]
        else:
            item_id, ref_path = label, '-'
        item_title = _AFFILIATE_NAMES.get(item_id.lower(), f'💰 広告案件 ({item_id})')
        try:
            import affiliate_manager
            cid = affiliate_manager.canonical_slot_id(item_id)
            if cid:
                ov = affiliate_manager._load_overrides(allow_blocking=False).get(cid)
                if isinstance(ov, dict) and (ov.get('memo') or '').strip():
                    item_title = f"{item_title} ［{ov.get('memo').strip()}］"
        except Exception:
            pass
        ref_title = _CTA_SURFACE_LABELS.get(ref_path) or _PAGE_TITLES.get(ref_path, ref_path)
        prefix = '表示面' if event == 'affiliate_impression' else 'クリック元'
        return {'title': item_title, 'sub': f'{prefix}: {ref_title}', 'href': ''}

    if event in _STORE_TRACK_EVENTS:
        sname = label.split(':', 1)[0].strip() if ':' in label else label
        vtype = label.split(':', 1)[1].strip() if (event in _STORE_SUFFIX_EVENTS and ':' in label) else ''
        vtype_ja = {'smooth': 'すぐ入れた', 'waiting': '少し待ち', 'lively': '大盛況'}.get(vtype, vtype)
        try:
            import store_master
            info = store_master.get(sname)
            if info:
                seo = store_master.seo_name(sname)
                area = info.get('area_label', '')
                slug = store_master.slug_of(sname)
                title_suffix = f'（{vtype_ja}）' if vtype_ja else ''
                return {
                    'title': f'🏪 {seo}{title_suffix}',
                    'sub': f'{area}（{sname}）' if area else sname,
                    'href': f'/store/{slug}' if slug else ''
                }
        except Exception:
            pass
        return {'title': label or '-', 'sub': '', 'href': ''}

    if event == 'sns_inflow':
        if '@' in label:
            src_tag, dest_path = [p.strip() for p in label.split('@', 1)]
        else:
            src_tag, dest_path = label.strip(), ''
        return {
            'title': describe_inflow_tag(src_tag),
            'sub': f'着地ページ: {dest_path}' if dest_path else 'SNS流入コンバージョン',
            'href': dest_path if dest_path.startswith('/') else '',
        }

    if event == 'cushion_click':
        if '@' in label:
            tgt_part, src_path = [p.strip() for p in label.split('@', 1)]
        else:
            tgt_part, src_path = label.strip(), ''
        tgt_base = tgt_part.split('#', 1)[0]
        _cushion_hub_titles = {
            '/grooming': '入店前準備ガイド (/grooming)',
            '/for-women': '女性向け特設ガイド (/for-women)',
            '/concierge': '相席コンシェルジュ診断 (/concierge)',
            '/price-comparison': '全店料金・イベント比較 (/price-comparison)',
        }
        if tgt_base in _cushion_hub_titles:
            tgt_ja = _cushion_hub_titles[tgt_base]
        elif tgt_base.startswith('/store/'):
            slug = tgt_base[len('/store/'):].strip('/')
            try:
                import store_master
                st_key = store_master.store_by_slug(slug)
                tgt_ja = f"{store_master.seo_name(st_key)} ({tgt_base})" if st_key else (_PAGE_TITLES.get(tgt_base) or tgt_base)
            except Exception:
                tgt_ja = _PAGE_TITLES.get(tgt_base) or tgt_base
        else:
            tgt_ja = _PAGE_TITLES.get(tgt_base) or tgt_base or '-'
        if '#' in tgt_part:
            tgt_ja = f"{tgt_ja} #{tgt_part.split('#', 1)[1]}"
        src_title = _PAGE_TITLES.get(src_path) or src_path or '-'
        return {
            'title': f'🧭 クッション送客：{tgt_ja}',
            'sub': f'遷移元: {src_title}',
            'href': src_path if src_path.startswith('/') else '',
        }

    if event == 'sns_click':
        return {'title': _SNS_NAMES.get(label.lower(), f'📣 {label}'), 'sub': 'SNS外部リンク', 'href': ''}

    if event == 'share':
        return {'title': f'🔗 シェア・コピー（{label or "汎用"}）', 'sub': '', 'href': ''}

    return {'title': label or '-', 'sub': '', 'href': ''}


_WEEKDAY_JA = ('月', '火', '水', '木', '金', '土', '日')


_HOURLY_PV_BUCKETS = (
    'pv_top',
    'pv_store',
    'pv_guide',
    'pv_concierge',
    'pv_women',
    'pv_grooming',
    'pv_price',
    'pv_stats',
    'pv_other',
)

_HOURLY_PV_SURFACE_LABELS = {
    'pv_top': 'トップランキング',
    'pv_store': '店舗詳細ページ',
    'pv_guide': '攻略・データコラム',
    'pv_concierge': '3秒穴場診断',
    'pv_women': '女性向け特設',
    'pv_grooming': '身だしなみガイド',
    'pv_price': '料金・イベント比較',
    'pv_stats': '一次統計レポート',
    'pv_other': 'エリア・ブランド・その他',
}

_CLEAN_AFF_SHORT_NAMES = {
    'dating_instant': '即時合流・恋活アプリ',
    'dating_bachelor': '審査制デート（バチェラー）',
    'mens_hair_removal': 'メンズ医療脱毛',
    'mens_skincare_clinic': 'ORBIS Mr.（スキンケア）',
    'spu_styleup': 'SPU スタイルアップ便',
    'women_dating_bachelor': '女性｜審査制デート',
    'women_beauty_clinic': '女性｜美容医療・脱毛',
    'women_cosme_salon': '女性｜コスメ・サロン',
    'night_care_supplement': 'ナイトケアサプリ',
    'second_venue_reservation': '一休.com（2軒目予約）',
    'other': 'その他広告枠',
}

_HOURLY_ZONE_SPECS = (
    ('day_prep', '昼・事前リサーチ帯', '12:00〜17:59', (12, 13, 14, 15, 16, 17)),
    ('prime_first', '1軒目ゴールデン帯', '18:00〜21:59', (18, 19, 20, 21)),
    ('late_hop', '終電前後・2軒目帯', '22:00〜01:59', (22, 23, 0, 1)),
    ('deep_morning', '深夜第2波〜朝帯', '02:00〜11:59', (2, 3, 4, 5, 6, 7, 8, 9, 10, 11)),
)


def _short_slot_name(slot_id: str) -> str:
    cid = str(slot_id or '').strip()
    if not cid:
        return '-'
    try:
        import affiliate_manager
        canon = affiliate_manager.canonical_slot_id(cid)
        if canon:
            cid = canon
    except Exception:
        pass
    base = _CLEAN_AFF_SHORT_NAMES.get(cid, cid)
    try:
        import affiliate_manager
        ov = affiliate_manager._load_overrides(allow_blocking=False).get(cid)
        if isinstance(ov, dict) and (ov.get('memo') or '').strip():
            memo = ov.get('memo').strip()[:18]
            return f"{base}（{memo}）"
    except Exception:
        pass
    return base


def _hour_zone_key(hour_int: int) -> str:
    for z_key, _z_lbl, _z_desc, z_hours in _HOURLY_ZONE_SPECS:
        if hour_int in z_hours:
            return z_key
    return 'deep_morning'


def _business_night_date_str(date_str: str, hour_int: int) -> str:
    """Return business session date string (`YYYY-MM-DD`), shifting 00:00-04:59 to the previous evening."""
    try:
        dt = datetime.date.fromisoformat(date_str)
        if int(hour_int) < 5:
            dt = dt - datetime.timedelta(days=1)
        return dt.strftime('%Y-%m-%d')
    except ValueError:
        return date_str


def _business_night_is_fri_sat(date_str: str, hour_int: int) -> bool:
    """Treat 00:00-04:59 as the previous calendar evening's nightlife session (Fri/Sat nights include Sat/Sun 00-04h)."""
    try:
        dt = datetime.date.fromisoformat(_business_night_date_str(date_str, hour_int))
        return dt.weekday() in (4, 5)
    except ValueError:
        return False


def _load_hourly_totals_since(start_date_str: str, allow_blocking: bool = True) -> dict:
    """Return `{(date_jst, hour_jst, bucket): count}` for `date_jst >= start_date_str`."""
    global _pg_hourly_seeded_from_sheets, _pg_hourly_seed_in_flight
    totals = defaultdict(int)
    is_mocked_hourly_sheets = getattr(sheets_store.read_hourly_rows, '__module__', '') != 'sheets_store'
    fast_rows = None if is_mocked_hourly_sheets else turbo_store.analytics_hourly_query_since(start_date_str)

    if fast_rows is not None:
        if turbo_store.is_postgres_active() and not sheets_store.is_disabled():
            should_seed = False
            with _lock:
                if not _pg_hourly_seeded_from_sheets and not _pg_hourly_seed_in_flight and not _hourly_pg_retry:
                    _pg_hourly_seed_in_flight = True
                    should_seed = True
            if should_seed:
                def _seed_pg_hourly():
                    global _pg_hourly_seeded_from_sheets, _pg_hourly_seed_in_flight
                    try:
                        hist_rows = sheets_store.read_hourly_rows_or_none(HOURLY_SHEET_TITLE, HOURLY_HEADER)
                        if hist_rows is not None:
                            if hist_rows:
                                turbo_store.analytics_hourly_replace_from_sheets(hist_rows)
                            with _lock:
                                _pg_hourly_seeded_from_sheets = True
                    except Exception:
                        pass
                    finally:
                        with _lock:
                            _pg_hourly_seed_in_flight = False

                threading.Thread(target=_seed_pg_hourly, daemon=True).start()
        for r in fast_rows:
            if len(r) >= 4 and str(r[0]) >= start_date_str:
                try:
                    hi = int(r[1])
                    ci = int(r[3])
                except (TypeError, ValueError):
                    continue
                if 0 <= hi <= 23 and ci > 0 and r[2]:
                    totals[(str(r[0]), hi, str(r[2]))] += ci
        with _lock:
            for k, c in _hourly_pg_retry.items():
                if k[0] >= start_date_str:
                    totals[k] += c
    else:
        if is_mocked_hourly_sheets:
            sheet_rows = sheets_store.read_hourly_rows(HOURLY_SHEET_TITLE, HOURLY_HEADER)
        elif sheets_store.is_disabled():
            sheet_rows = []
        else:
            now_mono = time.monotonic()
            with _lock:
                cached_ts = _hourly_sheet_rows_cache['ts']
                cached_rows = _hourly_sheet_rows_cache['rows']
            if cached_rows is not None and cached_ts > 0.0 and (now_mono - cached_ts < _SHEET_ROWS_CACHE_TTL):
                sheet_rows = cached_rows
            elif cached_rows is not None and not allow_blocking:
                _refresh_hourly_sheet_rows_async()
                sheet_rows = cached_rows
            elif not allow_blocking:
                _refresh_hourly_sheet_rows_async()
                sheet_rows = []
            else:
                fetched = sheets_store.read_hourly_rows_or_none(HOURLY_SHEET_TITLE, HOURLY_HEADER)
                if fetched is not None:
                    with _lock:
                        _hourly_sheet_rows_cache['ts'] = time.monotonic()
                        _hourly_sheet_rows_cache['rows'] = fetched
                    sheet_rows = fetched
                else:
                    sheet_rows = []
        if sheet_rows:
            for r in sheet_rows:
                if len(r) >= 4 and str(r[0]).strip() >= start_date_str:
                    try:
                        hi = int(str(r[1]).strip())
                        ci = int(float(str(r[3] or '0').replace(',', '').strip()))
                    except (TypeError, ValueError):
                        continue
                    b_str = str(r[2] or '').strip()
                    if 0 <= hi <= 23 and ci > 0 and b_str:
                        totals[(str(r[0]).strip(), hi, b_str)] += ci
        else:
            with _lock:
                for k, c in _hourly_flushed.items():
                    if k[0] >= start_date_str:
                        totals[k] += c
        with _lock:
            for k, c in _hourly_sheets_retry.items():
                if k[0] >= start_date_str:
                    totals[k] += c

    with _lock:
        for k, c in _hourly_pending.items():
            if k[0] >= start_date_str:
                totals[k] += c
    return totals


def _build_hourly_segment(seg_key: str, seg_label: str, entries: list, imp_days_set: set) -> dict:
    """Aggregate `[(date_jst, hour_jst, bucket, count), ...]` into a 24-hour waveform + 4 zones + golden hours."""
    by_h = {h: defaultdict(int) for h in range(24)}
    measurable_clk_by_h = defaultdict(int)
    slot_clk_by_h = {h: defaultdict(int) for h in range(24)}
    dates_seen = set()
    biz_dates_seen = set()

    for d_str, h_int, bucket, cnt in entries:
        dates_seen.add(d_str)
        biz_dates_seen.add(_business_night_date_str(d_str, h_int))
        if bucket.startswith('aff_clk:'):
            slot_id = bucket.split(':', 1)[1] or 'other'
            by_h[h_int]['aff_click'] += cnt
            slot_clk_by_h[h_int][slot_id] += cnt
            if d_str in imp_days_set:
                measurable_clk_by_h[h_int] += cnt
        else:
            by_h[h_int][bucket] += cnt

    pv_by_h = {}
    sub_pv_by_h = {}
    for h in range(24):
        hb = by_h[h]
        pv_h = sum(hb.get(bk, 0) for bk in _HOURLY_PV_BUCKETS)
        pv_by_h[h] = pv_h
        sub_pv_by_h[h] = max(0, pv_h - hb.get('pv_top', 0))

    pv_total = sum(pv_by_h.values())
    sub_pv_total = sum(sub_pv_by_h.values())
    store_open_total = sum(by_h[h].get('store_open', 0) for h in range(24))
    cushion_total = sum(by_h[h].get('cushion_click', 0) for h in range(24))
    aff_imp_total = sum(by_h[h].get('aff_imp', 0) for h in range(24))
    aff_clk_total = sum(by_h[h].get('aff_click', 0) for h in range(24))
    measurable_clk_total = sum(measurable_clk_by_h.values())
    map_total = sum(by_h[h].get('map_search', 0) for h in range(24))
    ext_total = sum(by_h[h].get('external_engage', 0) for h in range(24))
    sns_inflow_total = sum(by_h[h].get('sns_inflow', 0) for h in range(24))
    vote_total = sum(by_h[h].get('vote', 0) for h in range(24))
    action_total = aff_clk_total + map_total + ext_total

    max_h_pv = max(pv_by_h.values(), default=1) or 1
    peak_pv_val = max(pv_by_h.values(), default=0)
    peak_clk_val = max((by_h[h].get('aff_click', 0) for h in range(24)), default=0)

    hours_list = []
    for h in range(24):
        hb = by_h[h]
        pv_h = pv_by_h[h]
        sub_h = sub_pv_by_h[h]
        imp_h = hb.get('aff_imp', 0)
        clk_h = hb.get('aff_click', 0)
        m_clk_h = measurable_clk_by_h[h]
        slots_h = slot_clk_by_h[h]
        if slots_h:
            top_sid, top_scnt = sorted(slots_h.items(), key=lambda kv: (-kv[1], kv[0]))[0]
            top_stitle = _short_slot_name(top_sid)
        else:
            top_sid, top_scnt, top_stitle = '', 0, '-'

        # Most viewed surface category in this hour
        pv_cat_pairs = [(bk, hb.get(bk, 0)) for bk in _HOURLY_PV_BUCKETS if hb.get(bk, 0) > 0]
        if pv_cat_pairs:
            lead_bk = sorted(pv_cat_pairs, key=lambda kv: (-kv[1], kv[0]))[0][0]
            lead_surface = _HOURLY_PV_SURFACE_LABELS.get(lead_bk, lead_bk)
        else:
            lead_surface = '-'

        slot_ctr_h = min(100.0, round((m_clk_h / imp_h) * 100, 2)) if imp_h > 0 else None
        pv_ctr_h = min(100.0, round((clk_h / pv_h) * 100, 2)) if pv_h > 0 else None

        hours_list.append({
            'hour': h,
            'hour_str': f"{h:02d}",
            'hour_label': f"{h:02d}時",
            'time_range': f"{h:02d}:00-{h:02d}:59",
            'zone_key': _hour_zone_key(h),
            'pv': pv_h,
            'pv_top': hb.get('pv_top', 0),
            'sub_pv': sub_h,
            'pv_guide': hb.get('pv_guide', 0),
            'pv_store': hb.get('pv_store', 0),
            'pv_concierge': hb.get('pv_concierge', 0),
            'pv_women': hb.get('pv_women', 0),
            'pv_grooming': hb.get('pv_grooming', 0),
            'store_open': hb.get('store_open', 0),
            'cushion_click': hb.get('cushion_click', 0),
            'aff_imp': imp_h,
            'aff_click': clk_h,
            'map_search': hb.get('map_search', 0),
            'external_engage': hb.get('external_engage', 0),
            'sns_inflow': hb.get('sns_inflow', 0),
            'vote': hb.get('vote', 0),
            'pv_bar_pct': max(4.0, round((pv_h / max_h_pv) * 100, 1)) if pv_h > 0 else 0.0,
            'pv_share_pct': round((pv_h / pv_total) * 100, 1) if pv_total > 0 else 0.0,
            'slot_ctr_pct': slot_ctr_h,
            'pv_ctr_pct': pv_ctr_h,
            'top_slot_id': top_sid,
            'top_slot_title': top_stitle,
            'top_slot_clicks': top_scnt,
            'lead_surface': lead_surface,
            'is_peak_pv': bool(peak_pv_val > 0 and pv_h == peak_pv_val),
            'is_peak_click': bool(peak_clk_val > 0 and clk_h == peak_clk_val),
        })

    zones_list = []
    for z_key, z_label, z_desc, z_hours in _HOURLY_ZONE_SPECS:
        z_pv = sum(pv_by_h[h] for h in z_hours)
        z_sub = sum(sub_pv_by_h[h] for h in z_hours)
        z_open = sum(by_h[h].get('store_open', 0) for h in z_hours)
        z_cush = sum(by_h[h].get('cushion_click', 0) for h in z_hours)
        z_imp = sum(by_h[h].get('aff_imp', 0) for h in z_hours)
        z_clk = sum(by_h[h].get('aff_click', 0) for h in z_hours)
        z_mclk = sum(measurable_clk_by_h[h] for h in z_hours)

        z_slots = defaultdict(int)
        z_pv_cats = defaultdict(int)
        for h in z_hours:
            for sid, scnt in slot_clk_by_h[h].items():
                z_slots[sid] += scnt
            for bk in _HOURLY_PV_BUCKETS:
                if by_h[h].get(bk, 0) > 0:
                    z_pv_cats[bk] += by_h[h][bk]

        z_top_slot = _short_slot_name(sorted(z_slots.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]) if z_slots else '-'
        # Highlight highest subpage category if available, else overall lead category
        non_top_cats = [(bk, c) for bk, c in z_pv_cats.items() if bk != 'pv_top' and c > 0]
        if non_top_cats:
            lead_bk = sorted(non_top_cats, key=lambda kv: (-kv[1], kv[0]))[0][0]
            z_lead_surface = _HOURLY_PV_SURFACE_LABELS.get(lead_bk, lead_bk)
        elif z_pv_cats:
            lead_bk = sorted(z_pv_cats.items(), key=lambda kv: (-kv[1], kv[0]))[0][0]
            z_lead_surface = _HOURLY_PV_SURFACE_LABELS.get(lead_bk, lead_bk)
        else:
            z_lead_surface = '-'

        zones_list.append({
            'key': z_key,
            'label': z_label,
            'hours_desc': z_desc,
            'pv': z_pv,
            'pv_share_pct': round((z_pv / pv_total) * 100, 1) if pv_total > 0 else 0.0,
            'sub_pv': z_sub,
            'store_open': z_open,
            'cushion_click': z_cush,
            'aff_imp': z_imp,
            'aff_click': z_clk,
            'click_share_pct': round((z_clk / aff_clk_total) * 100, 1) if aff_clk_total > 0 else 0.0,
            'slot_ctr_pct': min(100.0, round((z_mclk / z_imp) * 100, 2)) if z_imp > 0 else None,
            'pv_ctr_pct': min(100.0, round((z_clk / z_pv) * 100, 2)) if z_pv > 0 else None,
            'top_slot_title': z_top_slot,
            'lead_surface': z_lead_surface,
        })

    peak_pv_hours = sorted(
        [h_obj for h_obj in hours_list if h_obj['pv'] > 0],
        key=lambda x: (-x['pv'], -x['aff_click'], x['hour']),
    )[:3]
    peak_click_hours = sorted(
        [h_obj for h_obj in hours_list if h_obj['aff_click'] > 0],
        key=lambda x: (-x['aff_click'], -(x['pv_ctr_pct'] if x['pv_ctr_pct'] is not None else -1.0), -x['pv'], x['hour']),
    )[:3]

    # Minimum sample threshold (imp >= 3 or pv >= 5) prevents noisy 1-PV / 1-click 100% outliers
    ctr_candidates = [
        h_obj for h_obj in hours_list
        if h_obj['aff_click'] >= 1 and (h_obj['aff_imp'] >= 3 or h_obj['pv'] >= 5)
    ]
    best_ctr_hour = None
    if ctr_candidates:
        best_ctr_hour = sorted(
            ctr_candidates,
            key=lambda x: (
                -(x['slot_ctr_pct'] if (x['slot_ctr_pct'] is not None and x['aff_imp'] >= 3) else (x['pv_ctr_pct'] or 0.0)),
                -x['aff_click'],
                -x['pv'],
                x['hour'],
            ),
        )[0]

    active_count = len(biz_dates_seen) if seg_key in ('fri_sat', 'sun_thu') else len(dates_seen)
    return {
        'key': seg_key,
        'label': seg_label,
        'active_days_count': active_count,
        'pv_total': pv_total,
        'sub_pv_total': sub_pv_total,
        'store_open_total': store_open_total,
        'cushion_click_total': cushion_total,
        'aff_imp_total': aff_imp_total,
        'aff_click_total': aff_clk_total,
        'map_total': map_total,
        'external_engage_total': ext_total,
        'sns_inflow_total': sns_inflow_total,
        'vote_total': vote_total,
        'action_total': action_total,
        'slot_ctr_pct': min(100.0, round((measurable_clk_total / aff_imp_total) * 100, 2)) if aff_imp_total > 0 else None,
        'pv_ctr_pct': min(100.0, round((aff_clk_total / pv_total) * 100, 2)) if pv_total > 0 else None,
        'hours': hours_list,
        'zones': zones_list,
        'peak_pv_hours': peak_pv_hours,
        'peak_click_hours': peak_click_hours,
        'best_ctr_hour': best_ctr_hour,
    }


def hourly_summary(days=7, allow_blocking=True, _now_jst=None):
    """直近 days 日の時間帯別（00〜23時）アクセス＆収益アクション波形集計（全日 / 日〜木 / 金・土）。"""
    days = max(1, int(days or 7))
    now_jst = _now_jst if _now_jst is not None else datetime.datetime.now(_JST)
    today_date = now_jst.date()
    start_date = today_date - datetime.timedelta(days=days - 1)
    start_str = start_date.strftime('%Y-%m-%d')
    today_str = today_date.strftime('%Y-%m-%d')

    totals = _load_hourly_totals_since(start_str, allow_blocking=allow_blocking)
    imp_days_set = {
        d_str for (d_str, _h, b_str), cnt in totals.items()
        if b_str == 'aff_imp' and cnt > 0 and d_str >= start_str
    }

    all_entries = []
    fri_sat_entries = []
    sun_thu_entries = []
    recorded_dates = set()

    for (d_str, h_int, b_str), cnt in totals.items():
        if d_str < start_str or cnt <= 0 or not (0 <= int(h_int) <= 23):
            continue
        recorded_dates.add(d_str)
        item = (d_str, int(h_int), b_str, int(cnt))
        all_entries.append(item)
        if _business_night_is_fri_sat(d_str, int(h_int)):
            fri_sat_entries.append(item)
        else:
            sun_thu_entries.append(item)

    seg_all = _build_hourly_segment('all', '全日（月〜日）', all_entries, imp_days_set)
    seg_sun_thu = _build_hourly_segment('sun_thu', '日〜木（平日・日曜夜）', sun_thu_entries, imp_days_set)
    seg_fri_sat = _build_hourly_segment('fri_sat', '金・土（週末ピーク夜）', fri_sat_entries, imp_days_set)

    has_hourly_data = bool(
        seg_all['pv_total'] > 0
        or seg_all['action_total'] > 0
        or seg_all['store_open_total'] > 0
        or seg_all['cushion_click_total'] > 0
    )
    earliest_date = min(recorded_dates) if recorded_dates else ''
    latest_date = max(recorded_dates) if recorded_dates else ''

    return {
        'days': days,
        'since': start_str,
        'today': today_str,
        'current_hour_jst': int(now_jst.hour),
        'has_hourly_data': has_hourly_data,
        'recorded_days': len(recorded_dates),
        'earliest_date': earliest_date,
        'latest_date': latest_date,
        'segments': {
            'all': seg_all,
            'sun_thu': seg_sun_thu,
            'fri_sat': seg_fri_sat,
        },
    }


def summary(days=7, allow_blocking=True):
    """直近 days 日の集計: {'by_event': {...}, 'by_label': {...}, 'by_label_pretty': {...}, 'funnel': {...}, 'revenue': {...}, 'daily_series': [...]}"""
    global _pg_seeded_from_sheets, _pg_seed_in_flight
    days = max(1, int(days or 7))
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    today_date = now_jst.date()
    start_date = today_date - datetime.timedelta(days=days - 1)
    start = start_date.strftime('%Y-%m-%d')
    today_str = today_date.strftime('%Y-%m-%d')
    yesterday_str = (today_date - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    totals = defaultdict(int)
    is_mocked_sheets = getattr(sheets_store.read_rows, '__module__', '') != 'sheets_store'
    fast_rows = None if is_mocked_sheets else turbo_store.analytics_query_since(start)

    if fast_rows is not None:
        if turbo_store.is_postgres_active() and not sheets_store.is_disabled():
            should_seed = False
            with _lock:
                if not _pg_seeded_from_sheets and not _pg_seed_in_flight and not _pg_retry:
                    _pg_seed_in_flight = True
                    should_seed = True
            if should_seed:
                def _seed_pg_analytics():
                    global _pg_seeded_from_sheets, _pg_seed_in_flight
                    try:
                        hist_rows = sheets_store.read_rows_or_none(SHEET_TITLE, HEADER)
                        # Only mark seeded after a successful read (None = transient Sheets failure → retry later)
                        if hist_rows is not None:
                            if hist_rows:
                                turbo_store.analytics_replace_from_sheets(hist_rows)
                            with _lock:
                                _pg_seeded_from_sheets = True
                    except Exception as err:
                        logging.warning(f"[analytics] PG seed from Sheets failed (will retry): {err}")
                    finally:
                        with _lock:
                            _pg_seed_in_flight = False
                threading.Thread(target=_seed_pg_analytics, daemon=True).start()
        for r in fast_rows:
            if len(r) >= 4 and r[0] >= start:
                try:
                    totals[(r[0], r[1], r[2])] += int(r[3])
                except ValueError:
                    continue
    else:
        if is_mocked_sheets:
            sheet_rows = sheets_store.read_rows(SHEET_TITLE, HEADER)
        elif sheets_store.is_disabled():
            sheet_rows = []
        else:
            now_mono = time.monotonic()
            with _lock:
                cached_ts = _sheet_rows_cache['ts']
                cached_rows = _sheet_rows_cache['rows']
            if cached_rows is not None and cached_ts > 0.0 and (now_mono - cached_ts < _SHEET_ROWS_CACHE_TTL):
                sheet_rows = cached_rows
            elif cached_rows is not None and not allow_blocking:
                _refresh_sheet_rows_async()
                sheet_rows = cached_rows
            elif not allow_blocking:
                _refresh_sheet_rows_async()
                sheet_rows = []
            else:
                sheet_rows = sheets_store.read_rows(SHEET_TITLE, HEADER)
                with _lock:
                    _sheet_rows_cache['ts'] = time.monotonic()
                    _sheet_rows_cache['rows'] = sheet_rows
        if sheet_rows:
            for r in sheet_rows:
                if len(r) >= 4 and r[0] >= start:
                    try:
                        totals[(r[0], r[1], r[2])] += int(r[3])
                    except ValueError:
                        continue
        else:
            with _lock:
                for k, c in _flushed.items():
                    totals[k] += c
        with _lock:
            for k, c in _sheets_retry.items():
                totals[k] += c
    with _lock:
        for k, c in _pending.items():
            totals[k] += c

    by_event = defaultdict(int)
    by_label = defaultdict(lambda: defaultdict(int))
    by_day = defaultdict(lambda: defaultdict(int))
    article_views = 0
    for (d, e, l), c in totals.items():
        if d < start:
            continue
        norm_l = normalize_store_event_label(e, l) if e in _STORE_TRACK_EVENTS else l
        by_event[e] += c
        by_label[e][norm_l] += c
        by_day[d][e] += c
        if e == 'page_view' and norm_l and norm_l != '/':
            article_views += c

    by_label_sorted = {e: sorted(v.items(), key=lambda x: -x[1])[:15] for e, v in by_label.items()}
    by_label_pretty = {}
    for e, items in by_label_sorted.items():
        max_c = max(1, items[0][1]) if items else 1
        ev_tot = max(1, by_event.get(e, 1))
        pretty_list = []
        for raw_lbl, cnt in items:
            desc = describe_label(e, raw_lbl)
            pretty_list.append({
                'raw': raw_lbl,
                'title': desc['title'],
                'sub': desc['sub'],
                'href': desc['href'],
                'count': cnt,
                'bar_pct': max(4, round((cnt / max_c) * 100)),
                'share_pct': round((cnt / ev_tot) * 100, 1),
            })
        by_label_pretty[e] = pretty_list

    pv_total = by_event.get('page_view', 0)
    store_open_total = by_event.get('store_open', 0)
    aff_total = by_event.get('affiliate_click', 0)
    aff_impressions_total = by_event.get('affiliate_impression', 0)
    cushion_clicks_total = by_event.get('cushion_click', 0)
    map_total = by_event.get('map_cafe', 0) + by_event.get('map_bar', 0)
    official_total = by_event.get('official_click', 0)
    sns_total = by_event.get('sns_click', 0)
    share_total = by_event.get('share', 0)
    vote_total = by_event.get('vote', 0)

    engagement_total = store_open_total + article_views + vote_total
    action_total = aff_total + map_total + official_total + sns_total + share_total

    cushion_by_source = defaultdict(int)
    for raw_lbl, cnt in by_label.get('cushion_click', {}).items():
        if '@' in raw_lbl:
            _tgt_p, src_p = [x.strip() for x in raw_lbl.split('@', 1)]
            if src_p:
                cushion_by_source[src_p] += int(cnt or 0)

    imp_days = {d for (d, e, _l), c in totals.items() if e == 'affiliate_impression' and c > 0 and d >= start}
    aff_clicks_measurable = sum(
        c for (d, e, _l), c in totals.items()
        if e == 'affiliate_click' and d >= start and d in imp_days
    )

    funnel = {
        'pv_total': pv_total,
        'article_views': article_views,
        'store_open_total': store_open_total,
        'engagement_total': engagement_total,
        'engagement_rate_pct': min(100.0, round((engagement_total / pv_total) * 100, 1)) if pv_total > 0 else 0.0,
        'action_total': action_total,
        'action_rate_pct': round((action_total / pv_total) * 100, 1) if pv_total > 0 else 0.0,
        'aff_total': aff_total,
        'aff_impressions_total': aff_impressions_total,
        'aff_clicks_measurable': aff_clicks_measurable,
        'cushion_clicks_total': cushion_clicks_total,
        'aff_ctr_pct': round((aff_total / pv_total) * 100, 2) if pv_total > 0 else 0.0,
        'aff_slot_ctr_pct': min(100.0, round((aff_clicks_measurable / aff_impressions_total) * 100, 2)) if aff_impressions_total > 0 else 0.0,
        'map_total': map_total,
        'official_total': official_total,
        'sns_total': sns_total,
        'share_total': share_total,
        'vote_total': vote_total,
    }

    # Build Slot x Surface CTR breakdown joining `affiliate_impression` and `affiliate_click`
    surface_imp = defaultdict(int)
    surface_clk = defaultdict(int)
    try:
        import affiliate_manager as _am_ctr
    except Exception:
        _am_ctr = None

    for raw_lbl, cnt in by_label.get('affiliate_impression', {}).items():
        if '@' in raw_lbl:
            s_part, p_part = [x.strip() for x in raw_lbl.split('@', 1)]
        else:
            s_part, p_part = raw_lbl.strip(), '/other'
        cid = _am_ctr.canonical_slot_id(s_part) if _am_ctr else s_part.lower()
        surf = normalize_cta_surface(p_part)
        if cid and surf != '/stats':
            surface_imp[(cid, surf)] += int(cnt or 0)

    for (d, e, raw_lbl), cnt in totals.items():
        if e != 'affiliate_click' or d < start:
            continue
        if imp_days and d not in imp_days:
            continue
        if '@' in raw_lbl:
            s_part, p_part = [x.strip() for x in raw_lbl.split('@', 1)]
        else:
            s_part, p_part = raw_lbl.strip(), '/other'
        cid = _am_ctr.canonical_slot_id(s_part) if _am_ctr else s_part.lower()
        surf = normalize_cta_surface(p_part)
        if cid and surf != '/stats':
            surface_clk[(cid, surf)] += int(cnt or 0)

    cta_surface_ctr = []
    all_pairs = {
        pair for pair in (set(surface_imp.keys()) | set(surface_clk.keys()))
        if not (surface_imp[pair] == 0 and pair[1] == '/other')
    }
    for cid, surf in sorted(
        all_pairs,
        key=lambda k: (0 if surface_imp[k] > 0 else 1, -surface_clk[k], -surface_imp[k], k[0], k[1]),
    )[:15]:
        imp_c = surface_imp[(cid, surf)]
        clk_c = surface_clk[(cid, surf)]
        desc = describe_label('affiliate_impression', f"{cid} @ {surf}")
        cta_surface_ctr.append({
            'slot_id': cid,
            'surface': surf,
            'surface_label': _CTA_SURFACE_LABELS.get(surf, surf),
            'title': desc['title'],
            'impressions': imp_c,
            'clicks': clk_c,
            'ctr_pct': min(100.0, round((clk_c / imp_c) * 100, 2)) if imp_c > 0 else 0.0,
        })

    # Estimated revenue simulation:
    #   - AdSense (`est_adsense_yen`): counted ONLY when AdSense client/publisher ID is configured (zero phantom estimate per AGENTS.md §1)
    #   - Gross expected value (`est_aff_yen`): ~90 yen/click (occurrence basis across high-ticket trials & apps)
    #   - Net confirmed value (`est_confirmed_aff_yen`): ~30 yen/click conservative industry median after approval rate
    adsense_enabled = bool((os.environ.get('ADSENSE_CLIENT_ID') or os.environ.get('ADSENSE_PUB_ID') or '').strip())
    est_adsense_yen = round(pv_total * 0.35) if adsense_enabled else 0
    est_aff_yen = round(aff_total * 90)
    est_confirmed_epc_yen = 30
    est_confirmed_aff_yen = round(aff_total * est_confirmed_epc_yen)
    est_total_yen = est_adsense_yen + est_aff_yen
    est_confirmed_total_yen = est_adsense_yen + est_confirmed_aff_yen
    if days <= 1 or not by_day:
        pace_denom_days = max(1, days) if not by_day else 1
    else:
        try:
            earliest_dt = datetime.date.fromisoformat(min(by_day.keys()))
            span_since_earliest = max(1, (today_date - earliest_dt).days + 1)
            pace_denom_days = max(1, min(days, span_since_earliest))
        except ValueError:
            pace_denom_days = max(1, len(by_day))
    est_monthly_pace_yen = round((est_total_yen / pace_denom_days) * 30)
    est_confirmed_monthly_pace_yen = round((est_confirmed_total_yen / pace_denom_days) * 30)

    revenue = {
        'adsense_enabled': adsense_enabled,
        'est_adsense_yen': est_adsense_yen,
        'est_aff_yen': est_aff_yen,
        'est_total_yen': est_total_yen,
        'est_monthly_pace_yen': est_monthly_pace_yen,
        'est_confirmed_epc_yen': est_confirmed_epc_yen,
        'est_confirmed_aff_yen': est_confirmed_aff_yen,
        'est_confirmed_total_yen': est_confirmed_total_yen,
        'est_confirmed_monthly_pace_yen': est_confirmed_monthly_pace_yen,
        'epc_sample_status': 'sample_sufficient' if aff_total >= 500 else 'accumulating',
        'epc_basis': 'assumed_fixed',
        'epc_note': '発生期待EPC 90円/click・承認考慮後の実勢確定EPC目安 25〜35円/click（基準30円）',
        'pace_denom_days': pace_denom_days,
    }

    # Build contiguous calendar daily series (chronological for chart, reversed in template for table)
    calendar_day_set = {
        (start_date + datetime.timedelta(days=offset)).strftime('%Y-%m-%d')
        for offset in range(days)
    }
    for recorded_d in by_day.keys():
        if start <= recorded_d <= today_str:
            calendar_day_set.add(recorded_d)
    sorted_days = sorted(calendar_day_set)
    max_day_pv = max([by_day[d].get('page_view', 0) for d in sorted_days], default=1) or 1
    daily_series = []
    for d in sorted_days:
        evs = by_day.get(d, {})
        dpv = evs.get('page_view', 0)
        dopen = evs.get('store_open', 0)
        daff = evs.get('affiliate_click', 0)
        dmap = evs.get('map_cafe', 0) + evs.get('map_bar', 0)
        dsns = evs.get('sns_click', 0) + evs.get('official_click', 0) + evs.get('share', 0)
        try:
            wd_idx = datetime.date.fromisoformat(d).weekday()
            weekday_ja = _WEEKDAY_JA[wd_idx]
            is_fri_sat = wd_idx in (4, 5)
            is_weekend = wd_idx in (5, 6)
        except ValueError:
            weekday_ja = ''
            is_fri_sat = False
            is_weekend = False
        daily_series.append({
            'date': d,
            'short_date': d[5:].replace('-', '/'),
            'weekday_ja': weekday_ja,
            'is_today': d == today_str,
            'is_yesterday': d == yesterday_str,
            'is_fri_sat': is_fri_sat,
            'is_weekend': is_weekend,
            'pv': dpv,
            'store_open': dopen,
            'affiliate': daff,
            'map': dmap,
            'sns_official': dsns,
            'pv_bar_pct': max(3.0, round((dpv / max_day_pv) * 100, 1)) if dpv > 0 else 0,
        })

    return {
        'days': days,
        'since': start,
        'today': today_str,
        'by_event': dict(sorted(by_event.items(), key=lambda x: -x[1])),
        'by_label': by_label_sorted,
        'all_by_label': {e: dict(v) for e, v in by_label.items()},
        'by_label_pretty': by_label_pretty,
        'by_day': {d: dict(v) for d, v in sorted(by_day.items(), reverse=True)},
        'daily_series': daily_series,
        'funnel': funnel,
        'revenue': revenue,
        'cta_surface_ctr': cta_surface_ctr,
        'cushion_by_source': dict(cushion_by_source),
        'hourly': hourly_summary(days=days, allow_blocking=allow_blocking, _now_jst=now_jst),
    }


def reset_for_tests():
    global _pg_seeded_from_sheets, _pg_hourly_seeded_from_sheets, _pg_hourly_seed_in_flight, _pg_seed_in_flight
    with _lock:
        _pg_seed_in_flight = False
        _pending.clear()
        _sheets_retry.clear()
        _pg_retry.clear()
        _flushed.clear()
        _sheet_rows_cache['ts'] = 0.0
        _sheet_rows_cache['rows'] = None
        _pg_seeded_from_sheets = False
        _hourly_pending.clear()
        _hourly_sheets_retry.clear()
        _hourly_pg_retry.clear()
        _hourly_flushed.clear()
        _hourly_sheet_rows_cache['ts'] = 0.0
        _hourly_sheet_rows_cache['rows'] = None
        _pg_hourly_seeded_from_sheets = False
        _pg_hourly_seed_in_flight = False
    try:
        turbo_store.clear_rate_limit('aff_clk_human')
        turbo_store.clear_rate_limit('aff_clk_ip')
        turbo_store.clear_rate_limit('api_track')
        turbo_store.clear_rate_limit('api_track_ip')
        turbo_store.clear_rate_limit('push_sub_ip')
        turbo_store.clear_rate_limit('push_unsub_ip')
    except Exception:
        pass
