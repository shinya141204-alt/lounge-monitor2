"""
embed_widgets.py - 曜日×時間帯別 実測男女比＆来店人数ヒートマップ（CC BY 4.0 埋め込みウィジェット）集計・スニペット生成

- 集計母数は「時間枠バケット」= (カレンダー日付 JST, 時, 正規化店舗名) 単位。
- 主ソース `store_hourly_rollups`（永久1時間ロールアップ）＋ 補完ソース `monitor_logs`（90日生ログを SQL GROUP BY で
  1時間×店舗に集約）。同一バケットはロールアップを優先し、生ログ側は未登録バケットのみ加算（二重計上ゼロ）。
- Fly Postgres 512MB VM の OOM を防ぐため、取得は必ず最大7日チャンク単位で逐次集計し、`SELECT *` は使わない。
- サンプル不足セルには推測値を一切入れず `status: "insufficient"`・数値 `None` を返す。
"""

import datetime
import html as _html
import logging
import re
import threading
import time
import urllib.parse

_log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# 軸定義・閾値
# ---------------------------------------------------------------------------
HEATMAP_HOURS = [18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4]
HEATMAP_DOWS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']
DOW_LABELS_JA = {'mon': '月', 'tue': '火', 'wed': '水', 'thu': '木', 'fri': '金', 'sat': '土', 'sun': '日'}
HOUR_LABELS_JA = {
    18: '18時', 19: '19時', 20: '20時', 21: '21時', 22: '22時', 23: '23時',
    0: '24時', 1: '翌1時', 2: '翌2時', 3: '翌3時', 4: '翌4時',
}
MIN_RATIO_HEADCOUNT = 4
MIN_CELL_BUCKETS = {'all': 5, 'area': 3, 'store': 2}
DEFAULT_DAYS = 90
MIN_DAYS = 7
MAX_DAYS = 90
ALLOWED_DAYS = (7, 30, 90)
CACHE_TTL_SEC = 900.0
STALE_MAX_SEC = 3600.0
DB_ERR_COOLDOWN_SEC = 10.0

SITE_ORIGIN = 'https://aiseki-navi.com'
EMBED_PAGE_URL = f'{SITE_ORIGIN}/embed/heatmap'
EMBED_JSON_URL = f'{SITE_ORIGIN}/api/embed/heatmap.json'
STATS_URL = f'{SITE_ORIGIN}/stats'
LICENSE_URL = 'https://creativecommons.org/licenses/by/4.0/'
ATTRIBUTION = '出典: 相席ナビ編集部（https://aiseki-navi.com/stats） / CC BY 4.0'

_HOUR_SET = frozenset(HEATMAP_HOURS)
_SLUG_RE = re.compile(r'^[a-z0-9][a-z0-9-]{0,79}$')
_JST = datetime.timezone(datetime.timedelta(hours=9))

# 主要エリアプリセット（順序付き）：(key, 表示ラベル, store_master.area_slug 候補, 店舗名キーワード or None=エリア全店)
_AREA_PRESETS = (
    ('shinjuku', '新宿・西新宿', ('shinjuku',), None),
    ('shibuya', '渋谷', ('shibuya',), ('渋谷',)),
    ('ebisu', '恵比寿', ('shibuya',), ('恵比寿',)),
    ('ueno-kinshicho', '上野・錦糸町・町田', ('tokyo-east', 'kanto'), ('上野', '錦糸町', '町田', '柏')),
    ('yokohama-omiya', '横浜・大宮・北関東', ('kanto',), ('横浜', '大宮', '宇都宮', '高崎')),
    ('umeda', '大阪キタ（梅田・茶屋町・天満）', ('osaka',), ('大阪駅前', '梅田', '茶屋町', '天満')),
    ('namba', '大阪ミナミ（難波・心斎橋）', ('osaka',), ('難波', '心斎橋')),
    ('kyoto-kobe', '京都・神戸三宮', ('osaka',), ('京都', '神戸', '三宮')),
    ('nagoya-sakae', '名古屋（栄・錦・住吉）', ('nagoya',), ('名古屋', '栄', '錦', '住吉')),
    ('fukuoka-tenjin', '福岡・天神', ('fukuoka',), ('福岡', '天神')),
    ('sapporo-susukino', '札幌・すすきの', ('hokkaido-tohoku',), ('札幌',)),
    ('sendai', '仙台・国分町', ('hokkaido-tohoku',), ('仙台',)),
    ('hiroshima-okayama', '広島・岡山・松山', ('chugoku-shikoku',), None),
)
_PRESET_BY_KEY = {p[0]: p for p in _AREA_PRESETS}
# portal_guides.AREA_GUIDES の広域スラッグ → 同等プリセットへの正規化
_AREA_GUIDE_ALIASES = {'tokyo-east': 'ueno-kinshicho', 'kanto': 'yokohama-omiya'}

_CACHE = {}
_CACHE_LOCK = threading.Lock()
# 同時アクセス時に重い90日集計が並列多重実行され 512MB VM を圧迫しないよう、計算自体も直列化する
_COMPUTE_LOCK = threading.Lock()
_CACHE_MAX_ENTRIES = 8
_DB_ERR_UNTIL = 0.0


# ---------------------------------------------------------------------------
# 正規化ヘルパー
# ---------------------------------------------------------------------------
def normalize_metric(metric) -> str:
    return 'headcount' if str(metric or '').strip().lower() == 'headcount' else 'ratio'


def normalize_theme(theme) -> str:
    return 'light' if str(theme or '').strip().lower() == 'light' else 'dark'


def clamp_days(days) -> int:
    """許容ウィンドウを ALLOWED_DAYS (7 / 30 / 90日) に丸め、任意 days 連打によるキャッシュ枯渇を防ぐ。"""
    try:
        d = int(days)
    except (TypeError, ValueError):
        return DEFAULT_DAYS
    clamped = max(MIN_DAYS, min(MAX_DAYS, d))
    return min(ALLOWED_DAYS, key=lambda cand: abs(cand - clamped))


def clear_heatmap_cache() -> None:
    global _DB_ERR_UNTIL
    with _CACHE_LOCK:
        _CACHE.clear()
        _DB_ERR_UNTIL = 0.0


def _hidden_sets() -> tuple:
    try:
        import ops_governance
        return set(ops_governance.get_hidden_brands() or ()), set(ops_governance.get_hidden_stores() or ())
    except Exception:
        return set(), set()


def _is_hidden(raw_name: str, hb: set, hs: set) -> bool:
    if not hb and not hs:
        return False
    try:
        import ops_governance
        return bool(ops_governance.is_store_hidden(raw_name, hb, hs))
    except Exception:
        return False


def _domestic_visible_master(hb: set, hs: set) -> dict:
    """国内（韓国除外）かつ非表示指定でない店舗マスタ {raw_name: info}（マスタ順を維持）。"""
    import store_master
    out = {}
    for raw_name, info in (store_master.load() or {}).items():
        info = info or {}
        if not raw_name or info.get('region') == 'Korea':
            continue
        if _is_hidden(raw_name, hb, hs):
            continue
        out[raw_name] = info
    return out


def _match_stores(master: dict, area_slugs, keywords) -> list:
    hits = []
    for raw_name, info in master.items():
        if info.get('area_slug') not in area_slugs:
            continue
        if keywords:
            disp = str(info.get('display_name') or '')
            if not any(k in disp or k in raw_name for k in keywords):
                continue
        hits.append(raw_name)
    return hits


def _public_store_list(raw_names) -> list:
    import store_master
    return [{'slug': store_master.slug_of(n), 'name': store_master.seo_name(n)} for n in raw_names]


# ---------------------------------------------------------------------------
# スコープ解決
# ---------------------------------------------------------------------------
def resolve_heatmap_scope(scope='all', store=None) -> dict:
    """
    スコープを解決し `{key, type, label, store_slug, store_name, raw_stores}` を返す。
    `raw_stores` は内部集計用の店舗キーで、公開ペイロードには含めない。
    未知のスコープ・店舗は LookupError。
    """
    import store_master
    hb, hs = _hidden_sets()
    master = _domestic_visible_master(hb, hs)

    store_slug = str(store or '').strip().lower()
    if store_slug:
        if not _SLUG_RE.match(store_slug):
            raise LookupError('unknown_store')
        for raw_name in master.keys():
            if store_master.slug_of(raw_name) == store_slug:
                name = store_master.seo_name(raw_name)
                return {
                    'key': store_slug,
                    'type': 'store',
                    'label': name,
                    'store_slug': store_slug,
                    'store_name': name,
                    'raw_stores': [raw_name],
                }
        raise LookupError('unknown_store')

    key = str(scope or 'all').strip().lower() or 'all'
    if key == 'all':
        return {
            'key': 'all', 'type': 'all', 'label': '全国',
            'store_slug': None, 'store_name': None, 'raw_stores': list(master.keys()),
        }
    if not _SLUG_RE.match(key):
        raise LookupError('unknown_scope')
    key = _AREA_GUIDE_ALIASES.get(key, key)
    preset = _PRESET_BY_KEY.get(key)
    if preset:
        _, label, area_slugs, keywords = preset
        return {
            'key': key, 'type': 'area', 'label': label,
            'store_slug': None, 'store_name': None,
            'raw_stores': _match_stores(master, area_slugs, keywords),
        }
    try:
        from portal_guides import AREA_GUIDES
    except Exception:
        AREA_GUIDES = {}
    ag = AREA_GUIDES.get(key)
    if not ag:
        raise LookupError('unknown_scope')
    parent = ag.get('parent_area')
    if parent:
        raw = _match_stores(master, (key, parent), tuple(ag.get('keywords') or ()))
    else:
        raw = _match_stores(master, (key,), None)
    return {
        'key': key, 'type': 'area', 'label': ag.get('short_name') or ag.get('name') or key,
        'store_slug': None, 'store_name': None, 'raw_stores': raw,
    }


def list_heatmap_scopes() -> list:
    """`/stats` のスコープ切替UI用：全国＋主要エリアプリセット（対象店舗0件のエリアは除外）。"""
    out = []
    for key in ('all',) + tuple(p[0] for p in _AREA_PRESETS):
        try:
            sc = resolve_heatmap_scope(scope=key)
        except LookupError:
            continue
        if not sc['raw_stores']:
            continue
        q = urllib.parse.urlencode({'scope': sc['key']})
        out.append({
            'key': sc['key'],
            'type': sc['type'],
            'label': sc['label'],
            'store_count': len(sc['raw_stores']),
            'embed_url': f'{EMBED_PAGE_URL}?{q}',
            'json_url': f'{EMBED_JSON_URL}?{q}',
        })
    return out


# ---------------------------------------------------------------------------
# 実測DB集計（7日チャンク逐次・OOM防止）
# ---------------------------------------------------------------------------
def _empty_aggregates(days: int, window_from: str, window_to: str) -> dict:
    return {
        'stores': {},
        'days': days,
        'window_from': window_from,
        'window_to': window_to,
        'total_buckets': 0,
        'total_snapshots': 0,
        'rollup_buckets': 0,
        'raw_buckets': 0,
        'generated_at_jst': datetime.datetime.now(_JST).strftime('%Y-%m-%d %H:%M:%S'),
    }


def _merge_bucket(target: dict, key: tuple, n: float, w: float, m: float, wp_sum: float, wp_n: float) -> None:
    """同一 (d_jst, hr, canon) の複数表記エイリアスを重み付きで1バケットへマージ。"""
    cur = target.get(key)
    if cur is None:
        target[key] = [n, w * n, m * n, wp_sum, wp_n]
    else:
        cur[0] += n
        cur[1] += w * n
        cur[2] += m * n
        cur[3] += wp_sum
        cur[4] += wp_n


def _load_store_slot_aggregates(days: int) -> dict:
    """店舗別 77 スロット（曜日×時間）集計 `{canon: {(dow_idx, hour): [n_buckets, n_snap, sum_w, sum_m, n_ratio, sum_ratio]}}`。"""
    global _DB_ERR_UNTIL
    import local_db

    days = clamp_days(days)
    today = datetime.datetime.now(_JST).date()
    w_from = today - datetime.timedelta(days=days - 1)
    w_to = today
    w_from_s = w_from.isoformat()
    w_to_s = w_to.isoformat()
    agg = _empty_aggregates(days, w_from_s, w_to_s)

    if time.monotonic() < _DB_ERR_UNTIL:
        return agg

    try:
        if not local_db._db_available():
            _DB_ERR_UNTIL = time.monotonic() + DB_ERR_COOLDOWN_SEC
            return agg
    except Exception as err:
        _DB_ERR_UNTIL = time.monotonic() + DB_ERR_COOLDOWN_SEC
        _log.warning('heatmap: db availability probe failed: %s', err)
        return agg

    hb, hs = _hidden_sets()
    try:
        master = _domestic_visible_master(hb, hs)
    except Exception as err:
        _log.warning('heatmap: store master load failed: %s', err)
        return agg
    canon_memo = {}

    def _canon(raw) -> str:
        s = str(raw or '').strip()
        if s in canon_memo:
            return canon_memo[s]
        c = ''
        try:
            if s and not local_db._is_korea_store_name(s) and not local_db._is_synthetic_seed_alias(s):
                c = local_db.resolve_canonical_store_name(s) or ''
                if c not in master:
                    c = ''
        except Exception:
            c = ''
        canon_memo[s] = c
        return c

    try:
        rollups_ok = bool(local_db._ensure_moat_rollup_schema())
    except Exception as err:
        _log.warning('heatmap: rollup schema ensure failed: %s', err)
        rollups_ok = False

    # 深夜 0..4 時（翌朝カレンダー日付）を取りこぼさないよう window_to + 1日まで取得する
    query_dates = [
        (w_from + datetime.timedelta(days=i)).isoformat()
        for i in range((w_to - w_from).days + 2)
    ]
    min_hc_int = int(MIN_RATIO_HEADCOUNT)
    sql_rollup = (
        "SELECT date_jst, hour_jst, business_date, store_name, sample_count, avg_women, avg_men, avg_wpct "
        "FROM store_hourly_rollups "
        "WHERE date_jst >= ? AND date_jst <= ? "
        "AND (hour_jst >= 18 OR hour_jst <= 4)"
    )
    sql_raw = (
        "SELECT SUBSTR(timestamp, 1, 10) AS d_jst, "
        "SUBSTR(timestamp, 12, 2) AS h_str, "
        "store_name, "
        "COUNT(*) AS snap_n, "
        "AVG(women) AS avg_w, "
        "AVG(men) AS avg_m, "
        f"SUM(CASE WHEN (women + men) >= {min_hc_int} THEN (women * 100.0) / (women + men) ELSE 0.0 END) AS wp_sum, "
        f"SUM(CASE WHEN (women + men) >= {min_hc_int} THEN 1 ELSE 0 END) AS wp_n "
        "FROM monitor_logs "
        "WHERE timestamp >= ? AND timestamp <= ? "
        "AND women >= 0 AND men >= 0 "
        "AND (SUBSTR(timestamp, 12, 2) >= '18' OR SUBSTR(timestamp, 12, 2) <= '04') "
        "GROUP BY SUBSTR(timestamp, 1, 10), SUBSTR(timestamp, 12, 2), store_name"
    )

    # インデックス境界 (MIN/MAX) で実データ存在区間を先に特定し、空DBや未蓄積期間の無駄な13チャンク走査を省く
    roll_min_d = roll_max_d = None
    if rollups_ok:
        try:
            rb = local_db._fetch_all_rows(
                "SELECT MIN(date_jst), MAX(date_jst) FROM store_hourly_rollups "
                "WHERE date_jst >= ? AND date_jst <= ?",
                (query_dates[0], query_dates[-1]),
            )
            if rb and rb[0] and rb[0][0] and rb[0][1]:
                roll_min_d = str(rb[0][0])[:10]
                roll_max_d = str(rb[0][1])[:10]
        except Exception as err:
            _log.warning('heatmap: rollup bounds probe failed: %s', err)
            roll_min_d, roll_max_d = query_dates[0], query_dates[-1]

    raw_min_d = raw_max_d = None
    try:
        lb = local_db._fetch_all_rows(
            "SELECT SUBSTR(MIN(timestamp), 1, 10), SUBSTR(MAX(timestamp), 1, 10) "
            "FROM monitor_logs WHERE timestamp >= ? AND timestamp <= ?",
            (f'{query_dates[0]} 00:00:00', f'{query_dates[-1]} 23:59:59'),
        )
        if lb and lb[0] and lb[0][0] and lb[0][1]:
            raw_min_d = str(lb[0][0])[:10]
            raw_max_d = str(lb[0][1])[:10]
    except Exception as err:
        _DB_ERR_UNTIL = time.monotonic() + DB_ERR_COOLDOWN_SEC
        _log.warning('heatmap: monitor_logs bounds probe failed: %s', err)
        return agg

    if not roll_min_d and not raw_min_d:
        return agg

    stores = agg['stores']
    min_hc = float(MIN_RATIO_HEADCOUNT)
    weekday_memo = {}

    def _slot_of(d_jst: str, hr: int):
        if hr not in _HOUR_SET:
            return None
        bdate = local_db._business_date_for_ymd_hour(d_jst, hr)
        if not (w_from_s <= bdate <= w_to_s):
            return None
        wd = weekday_memo.get(bdate)
        if wd is None:
            try:
                wd = datetime.date.fromisoformat(bdate).weekday()
            except ValueError:
                return None
            weekday_memo[bdate] = wd
        return (wd, hr)

    def _fold(buckets: dict, source: str) -> None:
        for (d_jst, hr, canon), (n, sw, sm, wp_sum, wp_n) in buckets.items():
            if n <= 0:
                continue
            slot = _slot_of(d_jst, hr)
            if slot is None:
                continue
            avg_w = sw / n
            avg_m = sm / n
            st = stores.setdefault(canon, {})
            acc = st.get(slot)
            if acc is None:
                acc = st[slot] = [0, 0, 0.0, 0.0, 0, 0.0]
            acc[0] += 1
            acc[1] += int(n)
            acc[2] += avg_w
            acc[3] += avg_m
            if (avg_w + avg_m) >= min_hc:
                if wp_n > 0:
                    ratio = wp_sum / wp_n
                elif source == 'rollup' and (avg_w + avg_m) > 0:
                    ratio = (avg_w / (avg_w + avg_m)) * 100.0
                else:
                    ratio = None
                if ratio is not None:
                    acc[4] += 1
                    acc[5] += ratio
            agg['total_buckets'] += 1
            agg['total_snapshots'] += int(n)
            agg['rollup_buckets' if source == 'rollup' else 'raw_buckets'] += 1

    for d0, d1 in local_db._contiguous_date_ranges(query_dates, max_span_days=7):
        has_roll = bool(rollups_ok and roll_min_d and roll_max_d and d1 >= roll_min_d and d0 <= roll_max_d)
        has_raw = bool(raw_min_d and raw_max_d and d1 >= raw_min_d and d0 <= raw_max_d)
        if not has_roll and not has_raw:
            continue
        seen_buckets = set()
        rollup_buckets = {}
        if has_roll:
            try:
                rows = local_db._fetch_all_rows(sql_rollup, (d0, d1))
            except Exception as err:
                _log.warning('heatmap: rollup chunk %s..%s failed: %s', d0, d1, err)
                rows = []
            for r in rows:
                try:
                    d_jst = str(r[0] or '')[:10]
                    hr = int(r[1])
                except (TypeError, ValueError):
                    continue
                canon = _canon(r[3])
                if not canon or not d_jst:
                    continue
                key = (d_jst, hr, canon)
                seen_buckets.add(key)
                try:
                    sc = float(r[4] or 0)
                    aw = float(r[5] or 0.0)
                    am = float(r[6] or 0.0)
                    awp = float(r[7] or 0.0)
                except (TypeError, ValueError):
                    continue
                if sc <= 0 or aw < 0 or am < 0:
                    continue
                if awp > 0 and (aw + am) >= min_hc:
                    _merge_bucket(rollup_buckets, key, sc, aw, am, awp * sc, sc)
                else:
                    _merge_bucket(rollup_buckets, key, sc, aw, am, 0.0, 0.0)
            del rows
        _fold(rollup_buckets, 'rollup')
        del rollup_buckets

        if has_raw:
            raw_buckets = {}
            try:
                for chunk in local_db._iter_ts_window_in_7d_chunks(sql_raw, f'{d0} 00:00:00', f'{d1} 23:59:59'):
                    for r in chunk:
                        try:
                            d_jst = str(r[0] or '')[:10]
                            hr = int(str(r[1] or '').strip())
                        except (TypeError, ValueError):
                            continue
                        if hr not in _HOUR_SET or not d_jst:
                            continue
                        canon = _canon(r[2])
                        if not canon:
                            continue
                        key = (d_jst, hr, canon)
                        if key in seen_buckets:
                            continue
                        try:
                            sn = float(r[3] or 0)
                            aw = float(r[4] or 0.0)
                            am = float(r[5] or 0.0)
                            wps = float(r[6] or 0.0)
                            wpn = float(r[7] or 0)
                        except (TypeError, ValueError):
                            continue
                        if sn <= 0:
                            continue
                        _merge_bucket(raw_buckets, key, sn, aw, am, wps, wpn)
            except Exception as err:
                _log.warning('heatmap: monitor_logs chunk %s..%s failed: %s', d0, d1, err)
            _fold(raw_buckets, 'raw')
            del raw_buckets
        del seen_buckets

    agg['generated_at_jst'] = datetime.datetime.now(_JST).strftime('%Y-%m-%d %H:%M:%S')
    return agg


def _get_aggregates(days: int) -> dict:
    import local_db

    days = clamp_days(days)
    hb, hs = _hidden_sets()
    try:
        db_key = local_db._active_db_key()
    except Exception:
        db_key = None
    cache_key = (db_key, days, (tuple(sorted(hb)), tuple(sorted(hs))))

    def _inspect():
        with _CACHE_LOCK:
            ent = _CACHE.get(cache_key)
            if ent:
                age = time.monotonic() - ent[0]
                if age < CACHE_TTL_SEC:
                    return ent[1], None
                if age < STALE_MAX_SEC:
                    return None, ent[1]
        return None, None

    fresh, stale = _inspect()
    if fresh is not None:
        return fresh

    # Stale-While-Revalidate: 期限切れ直後に別スレッドが再計算中なら、ロック待ちせず既存実データを即返す
    if stale is not None:
        if not _COMPUTE_LOCK.acquire(blocking=False):
            return stale
        try:
            fresh2, _ = _inspect()
            if fresh2 is not None:
                return fresh2
            data = _load_store_slot_aggregates(days)
            if data.get('total_buckets', 0) > 0:
                with _CACHE_LOCK:
                    if len(_CACHE) >= _CACHE_MAX_ENTRIES and cache_key not in _CACHE:
                        _CACHE.clear()
                    _CACHE[cache_key] = (time.monotonic(), data)
                return data
            return stale
        finally:
            _COMPUTE_LOCK.release()

    with _COMPUTE_LOCK:
        fresh2, stale2 = _inspect()
        if fresh2 is not None:
            return fresh2
        data = _load_store_slot_aggregates(days)
        # 空データ（DB一時障害・蓄積前）は絶対にキャッシュしない
        if data.get('total_buckets', 0) > 0:
            with _CACHE_LOCK:
                if len(_CACHE) >= _CACHE_MAX_ENTRIES and cache_key not in _CACHE:
                    _CACHE.clear()
                _CACHE[cache_key] = (time.monotonic(), data)
            return data
        if stale2 is not None:
            return stale2
    return data


def peek_cached_total_buckets(days: int = DEFAULT_DAYS):
    """DBに触れずメモリキャッシュ上の総バケット数のみ返す（`/stats` HTMLキャッシュ署名用・未計算時は None）。"""
    days = clamp_days(days)
    now_m = time.monotonic()
    with _CACHE_LOCK:
        for (_, d, _), (ts, data) in _CACHE.items():
            if d == days and (now_m - ts) < CACHE_TTL_SEC:
                return int(data.get('total_buckets') or 0)
    return None


# ---------------------------------------------------------------------------
# セル・バンド・ピーク
# ---------------------------------------------------------------------------
def _ratio_band(v) -> str:
    if v is None:
        return 'na'
    if v < 42.0:
        return 'r1'
    if v < 48.0:
        return 'r2'
    if v < 52.0:
        return 'r3'
    if v < 58.0:
        return 'r4'
    return 'r5'


def _quantile(sorted_vals: list, q: float) -> float:
    if not sorted_vals:
        return 0.0
    pos = (len(sorted_vals) - 1) * q
    lo = int(pos)
    hi = min(lo + 1, len(sorted_vals) - 1)
    frac = pos - lo
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * frac


def _headcount_thresholds(values: list) -> tuple:
    """有効セルが5件以上なら五分位点、少数時は自然な固定閾値（10/20/35/50名）。"""
    vals = sorted(v for v in values if v is not None)
    if len(vals) >= 5 and vals[0] != vals[-1]:
        return tuple(round(_quantile(vals, q), 1) for q in (0.2, 0.4, 0.6, 0.8)), 'quintile'
    return (10.0, 20.0, 35.0, 50.0), 'fixed'


def _headcount_band(v, thresholds: tuple) -> str:
    if v is None:
        return 'na'
    return f'h{1 + sum(1 for t in thresholds if v > t)}'


def _peak_view(c: dict) -> dict:
    return {
        'dow': c['dow'],
        'dow_label': c['dow_label'],
        'hour': c['hour'],
        'hour_label': c['hour_label'],
        'women_ratio_pct': c['women_ratio_pct'],
        'avg_women': c['avg_women'],
        'avg_men': c['avg_men'],
        'avg_total': c['avg_total'],
        'n_buckets': c['n_buckets'],
        'n_ratio': c['n_ratio'],
        'n_snapshots': c['n_snapshots'],
    }


def _build_cells(agg: dict, raw_stores: list, min_b: int) -> list:
    store_slots = agg.get('stores') or {}
    selected = [store_slots[s] for s in raw_stores if s in store_slots]
    cells = []
    for dow_i, dow in enumerate(HEATMAP_DOWS):
        for hr in HEATMAP_HOURS:
            nb = ns = nr = 0
            sw = sm = sr = 0.0
            for slots in selected:
                a = slots.get((dow_i, hr))
                if a:
                    nb += a[0]
                    ns += a[1]
                    sw += a[2]
                    sm += a[3]
                    nr += a[4]
                    sr += a[5]
            cell = {
                'dow': dow,
                'dow_label': DOW_LABELS_JA[dow],
                'hour': hr,
                'hour_label': HOUR_LABELS_JA[hr],
                'n_buckets': int(nb),
                'n_snapshots': int(ns),
                'n_ratio': int(nr),
                'status': 'insufficient',
                'avg_women': None,
                'avg_men': None,
                'avg_total': None,
                'women_ratio_pct': None,
            }
            if nb >= min_b:
                w_r = round(sw / nb, 1)
                m_r = round(sm / nb, 1)
                cell['avg_women'] = w_r
                cell['avg_men'] = m_r
                cell['avg_total'] = round(w_r + m_r, 1)
                cell['women_ratio_pct'] = round(sr / nr, 1) if nr >= min_b else None
                cell['status'] = 'ok'
            cells.append(cell)
    return cells


# ---------------------------------------------------------------------------
# 公開ペイロード
# ---------------------------------------------------------------------------
def get_heatmap_payload(scope='all', store=None, days=DEFAULT_DAYS, metric='ratio', theme='dark') -> dict:
    sc = resolve_heatmap_scope(scope=scope, store=store)
    days = clamp_days(days)
    metric = normalize_metric(metric)
    theme = normalize_theme(theme)
    min_b = MIN_CELL_BUCKETS.get(sc['type'], MIN_CELL_BUCKETS['all'])

    agg = _get_aggregates(days)
    cells = _build_cells(agg, sc['raw_stores'], min_b)

    hc_thresholds, hc_method = _headcount_thresholds([c['avg_total'] for c in cells])
    for c in cells:
        c['ratio_band'] = _ratio_band(c['women_ratio_pct'])
        c['headcount_band'] = _headcount_band(c['avg_total'], hc_thresholds)
        c['women_majority'] = bool(c['women_ratio_pct'] is not None and c['women_ratio_pct'] >= 50.0)

    order = {(c['dow'], c['hour']): i for i, c in enumerate(cells)}
    # 18時オープン直後の平均3名のような極小人数枠より実戦稼働帯（1店舗平均10名以上）を優先して引用ハイライトする。
    # 該当枠が1件も無い場合のみ、比率が算出済みの全セルへフォールバックする（推測値は使わない）。
    ratio_pool = [c for c in cells if c['women_ratio_pct'] is not None]
    ratio_core = [c for c in ratio_pool if c['avg_total'] is not None and c['avg_total'] >= 10.0]
    ratio_top = sorted(
        ratio_core or ratio_pool,
        key=lambda c: (-c['women_ratio_pct'], -c['n_ratio'], order[(c['dow'], c['hour'])]),
    )[:3]
    head_top = sorted(
        (c for c in cells if c['avg_total'] is not None),
        key=lambda c: (-c['avg_total'], -c['n_buckets'], order[(c['dow'], c['hour'])]),
    )[:3]

    total_buckets = sum(c['n_buckets'] for c in cells)
    total_snapshots = sum(c['n_snapshots'] for c in cells)
    ok_cells = sum(1 for c in cells if c['status'] == 'ok')
    scope_q = {'store': sc['store_slug']} if sc['type'] == 'store' else {'scope': sc['key']}
    if days != DEFAULT_DAYS:
        scope_q['days'] = days

    payload = {
        'schema': 'aiseki-navi.embed-heatmap/1',
        'scope': {
            'key': sc['key'],
            'type': sc['type'],
            'label': sc['label'],
            'store_slug': sc['store_slug'],
            'store_name': sc['store_name'],
            'store_count': len(sc['raw_stores']),
            'stores': _public_store_list(sc['raw_stores']),
        },
        'meta': {
            'days': days,
            'window_from': agg.get('window_from'),
            'window_to': agg.get('window_to'),
            'generated_at_jst': agg.get('generated_at_jst'),
            'total_buckets': int(total_buckets),
            'total_snapshots': int(total_snapshots),
            'ok_cells': ok_cells,
            'cell_count': len(cells),
            'min_cell_buckets': min_b,
            'min_ratio_headcount': MIN_RATIO_HEADCOUNT,
            'data_status': 'ok' if ok_cells > 0 else 'insufficient',
            'bucket_definition': '営業日×1時間×店舗の実測時間枠（store_hourly_rollups 優先・monitor_logs 補完、二重計上なし）',
            'business_day_rule': '0〜4時は前日の営業日として集計（例: 日曜深夜1時は日曜の翌1時セル）',
            'license': LICENSE_URL,
            'attribution': ATTRIBUTION,
            'source_url': STATS_URL,
        },
        'axes': {
            'dows': [{'key': d, 'label': DOW_LABELS_JA[d]} for d in HEATMAP_DOWS],
            'hours': [{'hour': h, 'label': HOUR_LABELS_JA[h]} for h in HEATMAP_HOURS],
        },
        'bands': {
            'ratio': {'thresholds': [42.0, 48.0, 52.0, 58.0], 'classes': ['r1', 'r2', 'r3', 'r4', 'r5']},
            'headcount': {'thresholds': list(hc_thresholds), 'method': hc_method, 'classes': ['h1', 'h2', 'h3', 'h4', 'h5']},
        },
        'cells': cells,
        'peaks': {
            'ratio_top': [_peak_view(c) for c in ratio_top],
            'headcount_top': [_peak_view(c) for c in head_top],
        },
        'metric': metric,
        'theme': theme,
        'urls': {
            'embed': f'{EMBED_PAGE_URL}?{urllib.parse.urlencode(scope_q)}',
            'json': f'{EMBED_JSON_URL}?{urllib.parse.urlencode(scope_q)}',
            'stats': f'{STATS_URL}#heatmap',
            'store': f"{SITE_ORIGIN}/store/{sc['store_slug']}" if sc['type'] == 'store' else None,
        },
    }
    payload['embed'] = build_embed_snippets(payload, theme=theme, metric=metric)
    return payload


def build_embed_snippets(payload: dict, theme='dark', metric='ratio') -> dict:
    theme = normalize_theme(theme)
    metric = normalize_metric(metric)
    sc = payload.get('scope') or {}
    meta = payload.get('meta') or {}
    days = clamp_days(meta.get('days', DEFAULT_DAYS))
    is_store = sc.get('type') == 'store'
    scope_label = str(sc.get('label') or '全国')

    if is_store:
        q = {'store': sc.get('store_slug') or '', 'metric': metric, 'theme': theme}
    else:
        q = {'scope': sc.get('key') or 'all', 'metric': metric, 'theme': theme}
    if days != DEFAULT_DAYS:
        q['days'] = days
    iframe_src = f'{EMBED_PAGE_URL}?{urllib.parse.urlencode(q)}'
    esc = lambda s: _html.escape(str(s), quote=True)  # noqa: E731

    title = f'相席ナビ 曜日×時間帯別 実測男女比ヒートマップ（{scope_label}・過去{days}日実測）'
    credit = f'データ出典：<a href="{STATS_URL}">相席ナビ 曜日×時間帯別リアルタイム統計（CC BY 4.0）</a>'
    if is_store:
        slug = sc.get('store_slug') or ''
        credit += (
            f' ／ <a href="{SITE_ORIGIN}/store/{esc(urllib.parse.quote(slug, safe="-"))}">'
            f'{esc(sc.get("store_name") or scope_label)}の実測データ</a>'
        )
    html_snippet = (
        f'<iframe src="{esc(iframe_src)}" title="{esc(title)}" width="100%" height="460" loading="lazy" '
        f'style="border:0;max-width:780px;display:block;border-radius:12px;"></iframe>\n'
        f'<p style="font-size:12px;margin:6px 0 0;line-height:1.5;">{credit}</p>'
    )

    peaks = (payload.get('peaks') or {}).get('headcount_top' if metric == 'headcount' else 'ratio_top') or []
    md_lines = []
    if peaks:
        head = '平均来店人数' if metric == 'headcount' else '平均女性比率'
        md_lines.append(f'> 相席ナビ実測（{scope_label}・過去{days}日）曜日×時間帯別 {head}の上位時間帯')
        for i, p in enumerate(peaks[:3], 1):
            if metric == 'headcount':
                main = f"平均{p['avg_total']}名（女性{p['avg_women']}名 / 男性{p['avg_men']}名）"
                n_txt = f"n={p['n_buckets']}時間枠"
            else:
                main = f"女性比率 {p['women_ratio_pct']}%"
                n_txt = f"n={p['n_ratio']}時間枠"
            md_lines.append(f"> {i}. {p['dow_label']}曜 {p['hour_label']}：{main}（{n_txt}）")
    else:
        md_lines.append(f'> 相席ナビ（{scope_label}）曜日×時間帯別の実測データは検証ログ蓄積中です。')
    md_lines.append('>')
    md_lines.append('> 出典: [相席ナビ編集部（https://aiseki-navi.com/stats） / CC BY 4.0](https://aiseki-navi.com/stats)')

    return {
        'iframe_src': iframe_src,
        'html': html_snippet,
        'markdown': '\n'.join(md_lines),
        'theme': theme,
        'metric': metric,
    }
