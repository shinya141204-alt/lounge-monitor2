"""SSR payload slimming, ground-truth card UX metadata, provenance & deferred card pool builders."""

import datetime
import logging
import math
import os
import re
import threading
import time
import urllib.parse

logger = logging.getLogger(__name__)
JST = datetime.timezone(datetime.timedelta(hours=9))

# Fields only used by the server-side card template (dropped from `window.INITIAL_STATUS_DATA` to keep HTML compact).
# Note: `fetched_at` is intentionally KEPT so the client can render accurate per-card freshness ("2分前").
CLIENT_STATUS_DROP_KEYS = (
    'ssr_title', 'ssr_brand', 'ssr_brand_cls', 'ssr_disp', 'ssr_is_dup', 'ssr_ladies_pct', 'ssr_men_pct',
    'seo_name', 'pos_telemetry',
    'plan2_signal_text', 'plan2_signal_cls', 'plan2_sub_text', 'plan2_staff_sub_text', 'plan2_forecast_text', 'plan2_needs_brand_pfx',
)
CLIENT_STATUS_DROP_NESTED = {
    'forecast_60m': ('model_version',),
    'wait_estimate': ('instant_match_prob_pct',),
}

_BADGE_EMOJI_PREFIX_RE = re.compile(r'^[\U0001F300-\U0001FAFF\u2600-\u27BF\u2197\u2198\uFE0E\uFE0F\s]+')
_YEN_NUM_RE = re.compile(r'(?:[¥￥]\s*(\d[\d,]*)|(\d[\d,]*)\s*円)')
_OPEN_TIME_RE = re.compile(r'(\d{1,2}:\d{2})')
_KOREA_KEYWORDS = ('カンナム', 'ホンデ', 'ソウル', 'GANGNAM', 'HONGDAE', 'SEOUL')
_MIN_VERIFIED_FORECAST_SAMPLES = 30

_SPEC_CACHE = None
_SPEC_CACHE_TS = 0.0
_SPEC_CACHE_TTL_SEC = 300.0
_SPEC_CACHE_LOCK = threading.Lock()
_FC_WARM_LOCK = threading.Lock()
_LAST_FC_WARM_MONO = None


def is_korea_store(st_or_name) -> bool:
    if isinstance(st_or_name, dict):
        if st_or_name.get('region') == 'Korea':
            return True
        raw_n = str(st_or_name.get('name') or '')
    else:
        raw_n = str(st_or_name or '')
    up = raw_n.upper()
    return any(k in raw_n or k in up for k in _KOREA_KEYWORDS)


def _strip_badge_prefix(raw: str) -> str:
    if not raw:
        return ''
    s = _BADGE_EMOJI_PREFIX_RE.sub('', str(raw)).strip()
    return re.sub(r'^(?:推定|実測|予測|目安)\s*[:：｜|]\s*', '', s).strip()


def format_midnight_retention_plain(pct) -> str:
    """Translate raw midnight retention % into plain Japanese while preserving truthful meaning."""
    if pct is None:
        return ''
    try:
        val = float(pct)
        if not math.isfinite(val) or val < 0:
            return ''
        iv = max(0, min(250, int(val + 0.5)))
    except (TypeError, ValueError):
        return ''
    if iv >= 105:
        return f'終電後も女性が増える店（{iv}%）'
    if iv >= 95:
        return f'終電後もほぼ同数が残る（{iv}%）'
    if iv >= 65:
        wari = max(6, min(9, int((iv + 5) // 10)))
        return f'終電後も女性の約{wari}割が残る'
    return f'終電前に帰る人が多め（終電後{iv}%）'


def format_usual_diff_plain(diff_val, gender_label: str = '') -> str:
    """Translate `平比+7` / `平比-2` into plain Japanese `いつもより+7人` / `いつもより2人少なめ`."""
    if diff_val is None:
        return ''
    try:
        fval = float(diff_val)
        if not math.isfinite(fval):
            return ''
        d = int(math.copysign(int(abs(fval) + 0.5), fval)) if abs(fval) >= 0.5 else 0
    except (TypeError, ValueError):
        return ''
    prefix = f'{gender_label}' if gender_label else ''
    if d > 0:
        return f'いつもより{prefix}+{d}人'
    if d < 0:
        return f'いつもより{prefix}{abs(d)}人少なめ'
    return 'いつも並み'


def _extract_yen_int(text: str):
    if not text:
        return None
    m = _YEN_NUM_RE.search(str(text))
    if not m:
        return None
    raw = m.group(1) or m.group(2)
    return int(raw.replace(',', '')) if raw else None


def _parse_30m_price_summary(sname: str, brand_code: str, pt: dict, bspec: dict, is_korea: bool) -> tuple:
    """Build (spec_price_short, spec_age_short, is_flat_rate, is_standing) strictly from verified tables."""
    if is_korea:
        return ('女性 無料｜韓国店舗（現地料金は公式確認）', '客層データ収集中', False, False)

    bu = (brand_code or '').strip()
    is_flat = bool(pt.get('is_flat_rate') or bspec.get('is_flat_rate'))
    is_standing = bool(bu == 'ASOBIBAR' or str(sname or '').upper().startswith('ASOBIBAR'))

    # Grounded age label: ONLY show age bracket when backed by official published brand entry/tier criteria;
    # never substring-match 'ag' against city names like NAGOYA / NAGASAKI / KAGOSHIMA.
    if bu == 'ag' or re.match(r'^ag(?:[\s　（(]|$)', str(sname or ''), flags=re.I) or str(sname or '').startswith('OLG ag'):
        age_short = '20代前半中心（公式年齢基準）'
    elif bu in ('ヤタコイ', 'YATAKOI') or str(sname or '').upper().startswith('YATAKOI') or str(sname or '').startswith('ヤタコイ'):
        age_short = '20代中心（U24・35歳以下基準）'
    else:
        age_short = '客層データ収集中'

    if bu in ('ヤタコイ', 'YATAKOI') or str(sname or '').upper().startswith('YATAKOI') or str(sname or '').startswith('ヤタコイ'):
        return ('男性 定額¥2,980〜（週末+¥500）｜女性 無料', age_short, True, False)
    if is_standing:
        return ('男性 定額¥2,500〜（金土¥3,500〜）｜女性 無料', age_short, True, True)

    slot_str = str(pt.get('slot_18_20') or pt.get('slot_20_22') or bspec.get('slot_18_20') or '')
    parts = slot_str.split('/')
    wd_10m = None
    wk_10m = None
    if len(parts) >= 2:
        wd_10m = _extract_yen_int(parts[0])
        wk_10m = _extract_yen_int(parts[1])
    if wd_10m is None and bspec.get('weekday_10m_yen'):
        wd_10m = int(bspec['weekday_10m_yen'])
    if wk_10m is None and bspec.get('weekend_10m_yen'):
        wk_10m = int(bspec['weekend_10m_yen'])

    if wd_10m is None or wk_10m is None:
        if is_flat:
            return ('定額制（料金詳細は店舗ページへ）', age_short, True, False)
        return ('料金詳細は店舗ページへ', age_short, False, False)

    wd_30m = wd_10m * 3
    wk_30m = wk_10m * 3
    return (f'男性30分 ¥{wd_30m:,}〜（金土¥{wk_30m:,}〜）｜女性 無料', age_short, False, False)


def invalidate_store_spec_cache() -> None:
    global _SPEC_CACHE, _SPEC_CACHE_TS
    with _SPEC_CACHE_LOCK:
        _SPEC_CACHE = None
        _SPEC_CACHE_TS = 0.0


def get_store_spec_map() -> dict:
    """Memoized map (300s TTL, never caches empty) of canonical store_name -> verified price, age tag, hours, map URL."""
    global _SPEC_CACHE, _SPEC_CACHE_TS
    now_m = time.monotonic()
    if _SPEC_CACHE and (now_m - _SPEC_CACHE_TS) < _SPEC_CACHE_TTL_SEC:
        return _SPEC_CACHE
    with _SPEC_CACHE_LOCK:
        now_m = time.monotonic()
        if _SPEC_CACHE and (now_m - _SPEC_CACHE_TS) < _SPEC_CACHE_TTL_SEC:
            return _SPEC_CACHE
        try:
            import price_comparison
            import store_hours
            import store_master
            master = store_master.load() or {}
            if not master:
                return _SPEC_CACHE or {}
            built = {}
            for sname, minfo in master.items():
                minfo = minfo or {}
                brand_code = minfo.get('brand') or ''
                is_kor = is_korea_store({'name': sname, 'region': minfo.get('region')})
                pt = price_comparison.STORE_PRICE_TABLE.get(sname) or {}
                bspec = price_comparison.BRAND_PRICE_SPECS.get(brand_code) or {}
                price_short, age_short, is_flat, is_standing = _parse_30m_price_summary(
                    sname, brand_code, pt, bspec, is_kor
                )
                bh = (store_hours.STORE_BUSINESS_HOURS.get(sname) or {}).get('label') or ''
                anchor = (minfo.get('map_anchor') or store_master.seo_name(sname) or sname).strip()
                map_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(anchor)}" if anchor else ''
                entry = {
                    'spec_price_short': price_short,
                    'spec_age_short': age_short,
                    'default_hours_label': bh,
                    'map_url': map_url,
                    'is_flat_rate': is_flat,
                    'is_standing': is_standing,
                    'is_korea': is_kor,
                }
                built[sname] = entry
                seo_n = store_master.seo_name(sname)
                if seo_n:
                    built[seo_n] = entry
                disp_n = (minfo.get('display_name') or '').strip()
                if brand_code and disp_n:
                    brand_ja = {'OLG': 'オリエンタルラウンジ', 'AG': 'ag', 'YATAKOI': 'ヤタコイ'}.get(brand_code, brand_code)
                    built.setdefault(f"{brand_ja} {disp_n}", entry)
            if built:
                _SPEC_CACHE = built
                _SPEC_CACHE_TS = now_m
            return built
        except Exception as exc:
            logger.warning('[ssr_payload] get_store_spec_map fallback on error: %s', exc)
            return _SPEC_CACHE or {}


def _extract_open_time(hours_label: str) -> str:
    if not hours_label:
        return ''
    m = _OPEN_TIME_RE.search(str(hours_label))
    return m.group(1) if m else ''


def parse_jst_epoch(ts_str: str):
    """Parse 'YYYY-MM-DD HH:MM:SS' in JST into Unix epoch seconds, or None."""
    if not ts_str or not isinstance(ts_str, str):
        return None
    try:
        dt = datetime.datetime.strptime(ts_str.strip()[:19], '%Y-%m-%d %H:%M:%S').replace(tzinfo=JST)
        return dt.timestamp()
    except Exception:
        return None


def _coerce_epoch(val):
    if val is None:
        return 0.0
    if isinstance(val, (int, float)):
        return float(val) if math.isfinite(val) and val > 0 else 0.0
    if isinstance(val, str):
        s = val.strip()
        if not s:
            return 0.0
        try:
            f = float(s)
            return f if math.isfinite(f) and f > 0 else 0.0
        except ValueError:
            ep = parse_jst_epoch(s)
            return float(ep) if ep else 0.0
    return 0.0


def stale_thresholds_sec(fetch_interval_sec: int = 60) -> tuple:
    """Return (delayed_sec, halted_sec) scaled to the active scrape interval (60s night vs 600s daytime)."""
    try:
        iv = max(60, min(900, int(fetch_interval_sec or 60)))
    except (TypeError, ValueError):
        iv = 60
    return (max(300, int(iv * 1.6) + 60), max(600, int(iv * 2.6) + 60))


def infer_effective_fetch_interval_sec(
    snap_epoch=None,
    now_jst=None,
    raw_interval=None,
    dom_total: int = 0,
    open_dom_cnt: int = 0,
) -> int:
    """Single source of truth for effective scrape interval (60s vs 600s), including the 17:00 JST transition window."""
    if raw_interval is not None:
        try:
            return max(60, min(900, int(raw_interval)))
        except (TypeError, ValueError):
            pass
    now_dt = now_jst or datetime.datetime.now(JST)
    if 7 <= now_dt.hour < 17 and dom_total > 0 and open_dom_cnt == 0:
        return 600
    if snap_epoch is not None and now_dt.hour == 17 and now_dt.minute < 15:
        try:
            snap_dt = datetime.datetime.fromtimestamp(float(snap_epoch), JST)
            if snap_dt.date() == now_dt.date() and 7 <= snap_dt.hour < 17:
                return 600
        except (TypeError, ValueError, OSError):
            pass
    return 60


def is_area_redundant_with_branch(area_label: str, branch_title: str) -> bool:
    """Return True when the store branch name already includes the area/city name (e.g. area='長崎', branch='長崎')."""
    ar = re.sub(r'エリア$', '', str(area_label or '')).strip()
    br = re.sub(r'店$', '', str(branch_title or '')).strip()
    if not ar or not br:
        return False
    if ar == br:
        return True
    parts = [p.strip() for p in re.split(r'[・/／\-\s　]+', ar) if len(p.strip()) >= 2]
    for p in parts:
        if p == '京都' and '東京' in br and '京都' not in br.replace('東京', ''):
            continue
        if p in br:
            return True
    return False


def compute_freshness_fields(item: dict, now_epoch: float = None, fallback_epoch: float = None) -> tuple:
    """Return (source_kind, source_label, age_min, freshness_label, freshness_short) with strict JIS POS verification."""
    now_ep = float(now_epoch if now_epoch is not None else time.time())
    pos_tel = item.get('pos_telemetry')
    has_jis_pos = bool(
        (item.get('has_jis_pos') is True and item.get('jis_wait_m_grp') is not None)
        or (
            isinstance(pos_tel, dict)
            and pos_tel.get('telemetry_type') == 'jis_pos'
            and pos_tel.get('men_waiting_groups') is not None
        )
    )
    if has_jis_pos:
        source_kind = 'jis_pos'
        source_label = 'JIS公式POS実測'
        short_prefix = 'POS実測・'
    else:
        source_kind = 'official_web'
        source_label = '公式サイト表示値'
        short_prefix = ''

    f_ep = _coerce_epoch(item.get('fetched_at'))
    if f_ep <= 0.0 and fallback_epoch:
        f_ep = _coerce_epoch(fallback_epoch)

    if f_ep <= 0.0:
        return (source_kind, source_label, None, f'{source_label}・計測待機中', f'{short_prefix}待機中')

    raw_diff = now_ep - f_ep
    age_sec = max(0.0, raw_diff)
    age_min = int(age_sec // 60)
    if age_sec < 60:
        rel = '1分以内'
    elif age_min < 60:
        rel = f'{age_min}分前'
    else:
        jst_dt = datetime.datetime.fromtimestamp(f_ep, tz=JST)
        now_dt = datetime.datetime.fromtimestamp(now_ep, tz=JST)
        if jst_dt.date() == now_dt.date():
            rel = f'{jst_dt.strftime("%H:%M")}取得'
        else:
            rel = f'{jst_dt.strftime("%m/%d %H:%M")}取得'
    return (source_kind, source_label, age_min, f'{source_label}・{rel}', f'{short_prefix}{rel}')


def build_dynamic_human_line(item: dict, is_women_view: bool = False, all_closed_mode: bool = False) -> str:
    """Generate 100% numeric conditional store summary (zero canned '男女バランスが良く、待ち時間なし' copy)."""
    w = max(0, int(item.get('women') or 0))
    m = max(0, int(item.get('men') or 0))
    tot = w + m
    is_open = bool(item.get('is_open_now')) or tot > 0
    hours_lbl = str(item.get('today_hours_label') or item.get('spec_hours_short') or '')
    open_t = _extract_open_time(hours_lbl)
    wd_lbl = str(item.get('wd_label') or '').strip()
    wd_pk_h = item.get('wd_peak_hour')
    wd_pk_w = item.get('wd_peak_women')
    core_short = item.get('core_time_short') if item.get('has_measured_core') else None

    open_phrase = f'本日{open_t}オープン' if open_t else '現在は営業時間外'
    if tot == 0 and not is_open:
        if wd_pk_h is not None and wd_pk_w is not None and float(wd_pk_w) >= 3.0:
            day_prefix = f'{wd_lbl}曜' if wd_lbl else '同曜日'
            if all_closed_mode:
                tail = f'｜本日{open_t}オープン' if open_t else ''
                return f'{day_prefix}ピーク実績は{int(wd_pk_h)}時台（女性平均{int(round(float(wd_pk_w)))}名）{tail}'
            return f'{open_phrase}。{day_prefix}のピーク実績は{int(wd_pk_h)}時台（女性平均{int(round(float(wd_pk_w)))}名）'
        if core_short:
            return f'{open_phrase}。普段のピーク時間帯は{core_short}'
        return f'{open_phrase}（営業開始前・営業時間外）' if open_t else '営業時間外です'

    if tot == 0 and is_open:
        if wd_pk_h is not None and wd_pk_w is not None and float(wd_pk_w) >= 3.0:
            day_prefix = f'{wd_lbl}曜' if wd_lbl else '同曜日'
            return f'現在オープン中（0名:0名）｜{day_prefix}のピーク実績は{int(wd_pk_h)}時台'
        return '現在オープン中（女性0名・男性0名）'

    wpct = int(round((w / float(tot)) * 100.0))
    we = item.get('wait_estimate') if isinstance(item.get('wait_estimate'), dict) else {}
    wait_m_min = max(0, int(we.get('est_wait_men_min') or 0))
    jis_m_grp = item.get('jis_wait_m_grp')
    jis_w_grp = item.get('jis_wait_w_grp')
    is_jis_pos = bool(item.get('source_kind') == 'jis_pos' and jis_m_grp is not None)
    wait_m_grp = max(0, int(jis_m_grp)) if is_jis_pos else 0
    women_has_wait = bool((jis_w_grp is not None and int(jis_w_grp) > 0) or int(we.get('est_women_wait_min') or 0) > 0)

    uwd = item.get('usual_w_diff')
    usl_raw = str(item.get('usual_slot_label') or '').strip()
    slot_phrase = f'いつもの{usl_raw}' if (usl_raw and usl_raw != '普段同時間帯') else '普段の同時間帯'
    diff_suffix = ''
    if uwd is not None:
        try:
            uwd_i = int(round(float(uwd)))
            if uwd_i > 0:
                diff_suffix = f'｜{slot_phrase}より女性+{uwd_i}人'
            elif uwd_i < 0:
                diff_suffix = f'｜{slot_phrase}より女性{uwd_i}人'
        except (TypeError, ValueError):
            diff_suffix = ''

    if is_women_view:
        if m == 0 and w > 0:
            return f'現在女性{w}名のみ（男性0名）。相席開始まで少し待つ可能性あり'
        if tot < 10:
            return f'男性{m}名・女性{w}名｜少人数のため参考値（落ち着いた時間帯）'
        if 45 <= wpct <= 55:
            return f'男女ほぼ同数（女性{w}・男性{m}）。席替えも自然に回りやすい状況'
        if m >= 4 and m > w:
            return f'男性{m}名が先行中（女性{w}名）。女性は待ちほぼ0で即着席しやすい'
        return f'女性{w}名・男性{m}名で女性多め。近隣の男性先行店も比較がおすすめ'

    # Men / Default View strict numeric branching
    if w == 0 and m > 0:
        return f'いま女性0名（男性{m}名）。男性は待ちの可能性大'
    if m == 0 and w > 0:
        if tot < 10:
            return f'女性{w}名が待機中（男性0名）。男性は入店即相席｜少人数のため参考値'
        return f'女性{w}名が待機中・男性0名。男性は入店即相席{diff_suffix}'

    if tot < 10:
        if is_jis_pos:
            wait_txt = f'男性待ち{wait_m_grp}組（実測）'
        elif wait_m_min > 0:
            wait_txt = f'男性待ち目安{wait_m_min}分（推定）'
        else:
            wait_txt = '男性待ち0組目安（推定）'
        return f'女性{w}・男性{m}｜{wait_txt}｜少人数のため比率は参考値'

    wait_seg = (
        f'男性待ち{wait_m_grp}組（実測）'
        if is_jis_pos
        else (f'男性待ち目安{wait_m_min}分（推定）' if wait_m_min > 0 else '男性待ち0組目安（推定）')
    )
    if wait_m_min > 0 or wait_m_grp > 0:
        w_note = '（女性もやや混雑）' if women_has_wait else '（女性はスムーズ案内）'
        return f'女性{w}・男性{m}｜{wait_seg}｜男性はやや混雑{w_note}{diff_suffix}'
    if 45 <= wpct <= 55:
        return f'男女ほぼ同数（女性{w}・男性{m}）｜すぐ相席になりやすい{diff_suffix}'
    if wpct > 55:
        return f'女性が多め（女性{w}・男性{m}）｜{wait_seg}{diff_suffix}'
    if 40 <= wpct < 45:
        return f'女性{w}・男性{m}｜{wait_seg}{diff_suffix}'
    return f'男性が先行中（女性{w}・男性{m}）｜男性は待機注意{diff_suffix}'


def format_wait_pill_with_source(raw_badge: str, item: dict) -> str:
    """Prefix wait badge explicitly with `実測｜` (JIS POS), `回遊制｜` (standing), `定額制｜`, or `推定｜` (modeled)."""
    clean = _strip_badge_prefix(raw_badge)
    if not clean:
        return ''
    if item.get('is_standing') or '回遊' in clean:
        return f'回遊制｜{clean}'
    if item.get('is_flat_rate') or '定額' in clean:
        return f'定額制｜{clean}'
    if item.get('source_kind') == 'jis_pos' and item.get('jis_wait_m_grp') is not None:
        clean_no_jis = re.sub(r'^JIS実測[:：\s]*', '', clean).strip()
        return f'実測｜{clean_no_jis}'
    return f'推定｜{clean}'


def format_forecast_pill_with_source(raw_badge: str, forecast_verified: bool = False) -> str:
    """Prefix 60-min forecast badge with `推定｜` (verified model) or `目安｜` (cold-start tendency)."""
    clean = _strip_badge_prefix(raw_badge)
    if not clean:
        return ''
    prefix = '推定' if forecast_verified else '目安'
    if not forecast_verified:
        clean = clean.replace('予測', '傾向')
    return f'{prefix}｜{clean}'


def attach_card_ux_metadata(
    item: dict,
    raw_store_name: str = None,
    benchmarks_entry: dict = None,
    now_epoch: float = None,
    fallback_epoch: float = None,
    forecast_verified: bool = False,
    all_closed_mode: bool = False,
) -> dict:
    """Attach provenance, freshness, spec row, plain-Japanese micro copy, and dynamic human copy in-place."""
    if not isinstance(item, dict):
        return item
    sname = (raw_store_name or item.get('name') or '').strip()
    spec_map = get_store_spec_map()
    spec = spec_map.get(sname) or {}

    pos_tel = item.get('pos_telemetry')
    if (
        isinstance(pos_tel, dict)
        and pos_tel.get('telemetry_type') == 'jis_pos'
        and pos_tel.get('men_waiting_groups') is not None
    ):
        item['has_jis_pos'] = True
        item['jis_wait_m_grp'] = max(0, int(pos_tel.get('men_waiting_groups') or 0))
        item['jis_wait_w_grp'] = (
            max(0, int(pos_tel.get('women_waiting_groups') or 0))
            if pos_tel.get('women_waiting_groups') is not None
            else None
        )
    else:
        item['has_jis_pos'] = False
        item['jis_wait_m_grp'] = None
        item['jis_wait_w_grp'] = None

    if benchmarks_entry and isinstance(benchmarks_entry, dict):
        item['wd_peak_hour'] = benchmarks_entry.get('wd_peak_hour')
        item['wd_peak_women'] = benchmarks_entry.get('wd_peak_women')
        item['wd_label'] = benchmarks_entry.get('wd_label')
        item['peak_band_avg_w'] = benchmarks_entry.get('peak_band_avg_w')

    source_kind, source_label, age_min, freshness_label, freshness_short = compute_freshness_fields(
        item, now_epoch=now_epoch, fallback_epoch=fallback_epoch
    )
    item['source_kind'] = source_kind
    item['source_label'] = source_label
    item['age_min'] = age_min
    item['freshness_label'] = freshness_label
    item['freshness_short'] = freshness_short
    branch_disp = str(item.get('ssr_disp') or item.get('ssr_title') or item.get('name') or '')
    item['area_redundant_sp'] = is_area_redundant_with_branch(item.get('ssr_area') or '', branch_disp)

    hours_lbl = item.get('today_hours_label') or spec.get('default_hours_label') or ''
    item['spec_price_short'] = spec.get('spec_price_short') or '料金詳細は店舗ページへ'
    item['spec_age_short'] = spec.get('spec_age_short') or '客層データ収集中'
    item['spec_hours_short'] = hours_lbl
    item['open_time_short'] = _extract_open_time(hours_lbl)
    item['map_url'] = spec.get('map_url') or ''
    item['is_flat_rate'] = bool(spec.get('is_flat_rate'))
    item['is_standing'] = bool(spec.get('is_standing'))

    w = max(0, int(item.get('women') or 0))
    m = max(0, int(item.get('men') or 0))
    tot = w + m
    item['is_small_sample'] = bool(0 < tot < 10)

    mid_pct = item.get('midnight_retention_pct') if item.get('has_measured_midnight') else None
    item['midnight_plain'] = format_midnight_retention_plain(mid_pct)
    item['usual_w_plain'] = format_usual_diff_plain(item.get('usual_w_diff'), '女性') if tot > 0 else ''
    item['usual_m_plain'] = format_usual_diff_plain(item.get('usual_m_diff'), '男性') if tot > 0 else ''

    we = item.get('wait_estimate') if isinstance(item.get('wait_estimate'), dict) else {}
    fc = item.get('forecast_60m') if isinstance(item.get('forecast_60m'), dict) else {}
    has_guest = tot > 0 or int(we.get('est_men_wait_groups') or 0) > 0 or (item.get('jis_wait_m_grp') or 0) > 0
    item['wait_pill_men'] = format_wait_pill_with_source(we.get('wait_badge_men') or '', item) if has_guest else ''
    item['wait_pill_women'] = format_wait_pill_with_source(we.get('wait_badge_women') or '', item) if has_guest else ''
    item['forecast_pill_men'] = format_forecast_pill_with_source(fc.get('badge_men') or '', forecast_verified=forecast_verified) if fc else ''
    item['forecast_pill_women'] = format_forecast_pill_with_source(fc.get('badge_women') or '', forecast_verified=forecast_verified) if fc else ''

    item['human_line'] = build_dynamic_human_line(item, is_women_view=False, all_closed_mode=all_closed_mode)
    item['human_line_women'] = build_dynamic_human_line(item, is_women_view=True, all_closed_mode=all_closed_mode)

    # Plan 2 (TableCheck / VACAN Signal Card) concise SSR fields
    est_wait_m = max(0, int(we.get('est_wait_men_min') or 0))
    jis_wait_grp = max(0, int(item.get('jis_wait_m_grp') or we.get('est_men_wait_groups') or 0))
    if tot == 0:
        p2_sig_txt = '営業時間外'
        p2_sig_cls = 'is-wait is-closed'
    elif source_kind == 'jis_pos':
        if jis_wait_grp == 0:
            p2_sig_txt = 'いま待ち0組（公式POS実測）'
            p2_sig_cls = 'is-ok'
        else:
            p2_sig_txt = f'男性待ち{jis_wait_grp}組（公式POS実測）'
            p2_sig_cls = 'is-wait'
    elif est_wait_m > 0:
        p2_sig_txt = f'やや混雑（男性待ち約{est_wait_m}分）'
        p2_sig_cls = 'is-wait'
    elif w == 0 and m > 0:
        p2_sig_txt = '男性先行中（女性来店待ち）'
        p2_sig_cls = 'is-wait'
    else:
        p2_sig_txt = '即相席圏（推定待ち0分）'
        p2_sig_cls = 'is-ok'
    item['plan2_signal_text'] = p2_sig_txt
    item['plan2_signal_cls'] = p2_sig_cls

    concise_price = re.split(r'[（(｜|]', item['spec_price_short'])[0].strip() or item['spec_price_short']
    ssr_area_lbl = (item.get('ssr_area') or '').strip()
    sub_parts = []
    if ssr_area_lbl and not item['area_redundant_sp']:
        sub_parts.append(ssr_area_lbl)
    elif hours_lbl:
        sub_parts.append(hours_lbl)
    if concise_price:
        sub_parts.append(concise_price)
    elif item['spec_age_short'] and item['spec_age_short'] != '客層データ収集中':
        sub_parts.append(item['spec_age_short'])
    item['plan2_sub_text'] = ' ・ '.join(sub_parts)

    staff_sub_parts = []
    if freshness_short or freshness_label:
        staff_sub_parts.append(freshness_short or freshness_label)
    if tot > 0 and item['usual_w_plain']:
        staff_sub_parts.append(item['usual_w_plain'])
    elif ssr_area_lbl and not item['area_redundant_sp']:
        staff_sub_parts.append(ssr_area_lbl)
    elif hours_lbl:
        staff_sub_parts.append(f'営業 {hours_lbl}')
    item['plan2_staff_sub_text'] = ' ・ '.join(staff_sub_parts)

    if tot == 0:
        p2_fc_txt = f"本日 {item['open_time_short']} オープン予定" if item['open_time_short'] else '営業時間外（開店後に自動更新）'
    elif fc and fc.get('pred_60m_wpct') is not None:
        wp_val = int(fc['pred_60m_wpct'])
        d_pt = int(fc.get('pred_delta_pt') or 0)
        fc_sfx = '（好調持続）' if (wp_val >= 65 and d_pt >= -4) else ('（女性上昇傾向）' if d_pt >= 4 else ('（終電・低下注意）' if d_pt <= -8 else '（安定）'))
        fc_pfx = '60分後予測' if forecast_verified else '60分後目安'
        p2_fc_txt = f'{fc_pfx}：女性{wp_val}%{fc_sfx}'
    elif item.get('core_time_short'):
        p2_fc_txt = f"狙い目時間帯：{item['core_time_short']}"
    else:
        p2_fc_txt = freshness_label or '1分ごとに自動更新中'
    item['plan2_forecast_text'] = p2_fc_txt

    ssr_brand_str = str(item.get('ssr_brand') or '').strip()
    ssr_title_str = str(item.get('ssr_title') or item.get('ssr_disp') or '').strip()
    item['plan2_needs_brand_pfx'] = bool(
        not item.get('ssr_is_dup')
        and ssr_brand_str
        and ssr_brand_str.upper() not in ssr_title_str.upper()
    )
    return item


def finalize_enriched_ranking(
    enriched: list,
    last_updated: str = None,
    benchmarks: dict = None,
    local_db_mod=None,
    is_testing: bool = False,
) -> list:
    """Return a freshly sorted shallow-copied list with UX metadata attached (never mutating shared dicts in-place)."""
    if not isinstance(enriched, list) or not enriched:
        return enriched
    items = [dict(st) if isinstance(st, dict) else st for st in enriched]
    now_ep = time.time()
    now_jst = datetime.datetime.now(JST)
    fb_ep = parse_jst_epoch(last_updated or '')

    dom_items = [st for st in items if isinstance(st, dict) and not is_korea_store(st)]
    open_dom_cnt = sum(
        1 for st in dom_items
        if bool(st.get('is_open_now')) or (int(st.get('women') or 0) + int(st.get('men') or 0)) > 0
    )
    eff_interval = infer_effective_fetch_interval_sec(
        snap_epoch=fb_ep,
        now_jst=now_jst,
        dom_total=len(dom_items),
        open_dom_cnt=open_dom_cnt,
    )
    _, halted_sec = stale_thresholds_sec(eff_interval)
    is_halted = bool(fb_ep is not None and (now_ep - fb_ep) >= halted_sec)

    fc_verified = False
    if local_db_mod is not None:
        try:
            fc_rep = local_db_mod.get_forecast_accuracy_report(
                days=30, limit_recent=5, allow_blocking=False, auto_backfill=False
            )
            fcs = (fc_rep.get('summary') or {}) if isinstance(fc_rep, dict) else {}
            fc_verified = bool(
                int(fcs.get('resolved_count') or 0) >= _MIN_VERIFIED_FORECAST_SAMPLES
                and fcs.get('mae_wpct') is not None
            )
            if not fc_verified and not is_testing:
                _maybe_warm_forecast_30d_async(local_db_mod)
        except Exception:
            fc_verified = False

    all_closed = bool(len(dom_items) > 0 and open_dom_cnt == 0 and not is_halted)

    for st in items:
        if not isinstance(st, dict):
            continue
        raw_n = st.get('name') or ''
        bm_entry = (benchmarks or {}).get(raw_n) if isinstance(benchmarks, dict) else None
        attach_card_ux_metadata(
            st,
            raw_store_name=raw_n,
            benchmarks_entry=bm_entry,
            now_epoch=now_ep,
            fallback_epoch=fb_ep,
            forecast_verified=fc_verified,
            all_closed_mode=all_closed,
        )
        w = int(st.get('women') or 0)
        m = int(st.get('men') or 0)
        st['is_preopen_zero'] = bool(not st.get('is_open_now') and (w + m) == 0)

    def _peak_metric(x: dict) -> float:
        v1 = x.get('wd_peak_women')
        if v1 is not None:
            try:
                return float(v1)
            except (TypeError, ValueError):
                pass
        v2 = x.get('peak_band_avg_w')
        if v2 is not None:
            try:
                return float(v2)
            except (TypeError, ValueError):
                pass
        return -1.0

    if all_closed:
        items.sort(
            key=lambda x: (
                isinstance(x, dict) and not is_korea_store(x),
                _peak_metric(x) if isinstance(x, dict) else -1.0,
                int(x.get('women') or 0) if isinstance(x, dict) else 0,
                int(x.get('men') or 0) if isinstance(x, dict) else 0,
            ),
            reverse=True,
        )
    else:
        items.sort(
            key=lambda x: (
                isinstance(x, dict) and not is_korea_store(x),
                not bool(x.get('is_preopen_zero')) if isinstance(x, dict) else False,
                int(x.get('women') or 0) if isinstance(x, dict) else 0,
                int(x.get('men') or 0) if isinstance(x, dict) else 0,
            ),
            reverse=True,
        )

    open_rank = 0
    for st in items:
        if not isinstance(st, dict):
            continue
        if is_korea_store(st):
            st['ssr_rank'] = None
        elif all_closed:
            if _peak_metric(st) >= 0.0 or len(dom_items) <= 5:
                open_rank += 1
                st['ssr_rank'] = open_rank
            else:
                st['ssr_rank'] = None
        elif not st.get('is_preopen_zero'):
            open_rank += 1
            st['ssr_rank'] = open_rank
        else:
            st['ssr_rank'] = None
    enriched[:] = items
    return items


def _maybe_warm_forecast_30d_async(local_db_mod) -> None:
    global _LAST_FC_WARM_MONO
    if os.environ.get('DISABLE_SCHEDULER') == '1':
        return
    now_m = time.monotonic()
    with _FC_WARM_LOCK:
        if _LAST_FC_WARM_MONO is not None and (now_m - _LAST_FC_WARM_MONO) < 600.0:
            return
        _LAST_FC_WARM_MONO = now_m

    def _runner():
        try:
            local_db_mod.get_forecast_accuracy_report(
                days=30, limit_recent=5, allow_blocking=True, auto_backfill=False
            )
        except Exception as exc:
            logger.warning('[ssr_payload] async 30d forecast warm warning: %s', exc)

    threading.Thread(target=_runner, daemon=True, name='fc30d-warm').start()


def _get_master_store_counts() -> tuple:
    try:
        import store_master
        master = store_master.load() or {}
        if not master:
            return (0, 0)
        try:
            import ops_governance
            vis = ops_governance.filter_visible_stores([{'name': k, 'region': (v or {}).get('region')} for k, v in master.items()])
        except Exception:
            vis = [{'name': k, 'region': (v or {}).get('region')} for k, v in master.items()]
        tot = len(vis)
        dom = sum(1 for x in vis if not is_korea_store(x))
        return (tot, dom)
    except Exception:
        return (0, 0)


def build_top_provenance_meta(initial_status: dict, local_db_mod=None, is_testing: bool = False) -> dict:
    """Build verified FV trust-bar, stale-banner, and open/pre-open summary metadata for `/`."""
    now_jst = datetime.datetime.now(JST)
    weekdays_ja = ['月', '火', '水', '木', '金', '土', '日']
    biz_dt = (now_jst - datetime.timedelta(days=1)) if now_jst.hour < 5 else now_jst
    today_wd_ja = weekdays_ja[biz_dt.weekday()]

    ts_str = (initial_status or {}).get('timestamp') or ''
    is_seed = bool((initial_status or {}).get('is_seed'))
    last_hm = '--:--'
    data_age_min = None
    if ts_str and ' ' in str(ts_str):
        last_hm = str(ts_str).split(' ')[1][:5]
    ts_ep = parse_jst_epoch(ts_str)

    ranking = (initial_status or {}).get('ranking') or []
    dom_stores = [s for s in ranking if isinstance(s, dict) and not is_korea_store(s)]
    open_dom = [
        s for s in dom_stores
        if bool(s.get('is_open_now')) or (int(s.get('women') or 0) + int(s.get('men') or 0)) > 0
    ]
    preopen_dom = [
        s for s in dom_stores
        if not bool(s.get('is_open_now')) and (int(s.get('women') or 0) + int(s.get('men') or 0)) == 0
    ]

    raw_iv = (initial_status or {}).get('fetch_interval_sec')
    eff_interval = infer_effective_fetch_interval_sec(
        snap_epoch=ts_ep,
        now_jst=now_jst,
        raw_interval=raw_iv,
        dom_total=len(dom_stores),
        open_dom_cnt=len(open_dom),
    )
    delayed_sec, halted_sec = stale_thresholds_sec(eff_interval)
    is_daytime_10m = bool(eff_interval >= 300 and len(dom_stores) > 0 and len(open_dom) == 0)

    if is_seed:
        stale_state = 'seed'
    elif ts_ep is None:
        stale_state = 'delayed'
    else:
        raw_diff = time.time() - ts_ep
        if raw_diff < -60.0:
            logger.warning('[ssr_payload] future timestamp detected in initial_status: %s', ts_str)
            stale_state = 'delayed'
        else:
            age_sec = max(0.0, raw_diff)
            data_age_min = int(age_sec // 60)
            if age_sec >= halted_sec:
                stale_state = 'halted'
            elif age_sec >= delayed_sec:
                stale_state = 'delayed'
            else:
                stale_state = 'live'

    cov = {}
    fc_summary = {}
    if local_db_mod is not None:
        try:
            cov = local_db_mod.get_moat_coverage_summary() or {}
        except Exception:
            cov = {}
        try:
            fc_rep = local_db_mod.get_forecast_accuracy_report(
                days=30, limit_recent=5, allow_blocking=False, auto_backfill=False
            )
            fc_summary = (fc_rep.get('summary') or {}) if isinstance(fc_rep, dict) else {}
            if int(fc_summary.get('resolved_count') or 0) < _MIN_VERIFIED_FORECAST_SAMPLES and not is_testing:
                _maybe_warm_forecast_30d_async(local_db_mod)
        except Exception:
            fc_summary = {}

    raw_snapshots = int(cov.get('raw_snapshots') or 0)
    since_src = str(cov.get('rollup_min_date') or cov.get('min_timestamp') or '')
    collection_since = ''
    if len(since_src) >= 10 and since_src[:4].isdigit():
        collection_since = f"{since_src[:4]}/{since_src[5:7]}/{since_src[8:10]}〜現在"

    resolved_cnt = int(fc_summary.get('resolved_count') or 0)
    mae_wpct = fc_summary.get('mae_wpct')
    within_10pt = fc_summary.get('within_10pt_pct')
    dir_hit = fc_summary.get('direction_hit_pct')
    forecast_verified = bool(resolved_cnt >= _MIN_VERIFIED_FORECAST_SAMPLES and mae_wpct is not None)

    all_closed_mode = bool(
        len(dom_stores) > 0
        and len(open_dom) == 0
        and stale_state not in ('halted', 'seed')
    )

    master_tot, master_dom = _get_master_store_counts()
    tot_monitored = len(ranking) if len(ranking) >= 50 else (master_tot or len(ranking))
    dom_monitored = len(dom_stores) if len(dom_stores) >= 50 else (master_dom or len(dom_stores))

    return {
        'stale_state': stale_state,
        'fetch_interval_sec': eff_interval,
        'is_daytime_10m': is_daytime_10m,
        'delayed_threshold_sec': delayed_sec,
        'halted_threshold_sec': halted_sec,
        'data_age_min': data_age_min,
        'last_hm': last_hm,
        'raw_snapshots': raw_snapshots,
        'collection_since': collection_since,
        'rolled_up_hours': int(cov.get('rolled_up_hours') or 0),
        'rollup_observations': int(cov.get('rollup_observations') or 0),
        'pos_telemetry_rows': int(cov.get('pos_telemetry_rows') or 0),
        'forecast_verified': forecast_verified,
        'forecast_mae_pt': round(float(mae_wpct), 1) if forecast_verified else None,
        'forecast_within_10pt_pct': round(float(within_10pt), 1) if (forecast_verified and within_10pt is not None) else None,
        'forecast_direction_hit_pct': round(float(dir_hit), 1) if (forecast_verified and dir_hit is not None) else None,
        'forecast_resolved_count': resolved_cnt,
        'total_monitored_stores': tot_monitored,
        'domestic_monitored_stores': dom_monitored,
        'open_domestic_count': len(open_dom),
        'preopen_domestic_count': len(preopen_dom),
        'all_closed_mode': all_closed_mode,
        'today_wd_ja': today_wd_ja,
    }


def client_status_payload(status):
    if not isinstance(status, dict) or not isinstance(status.get('ranking'), list):
        return status
    slim = dict(status)
    ranking = []
    for st in status['ranking']:
        if not isinstance(st, dict):
            ranking.append(st)
            continue
        item = {k: v for k, v in st.items() if k not in CLIENT_STATUS_DROP_KEYS}
        for parent, keys in CLIENT_STATUS_DROP_NESTED.items():
            sub = item.get(parent)
            if isinstance(sub, dict):
                item[parent] = {k: v for k, v in sub.items() if k not in keys}
        ranking.append(item)
    slim['ranking'] = ranking
    return slim


def build_deferred_card_pool(status, is_staff=False, start_idx=20):
    """Build compact card descriptors for stores ranked beyond SSR_RANK_LIMIT so pinned stores render real cards on first paint."""
    if not isinstance(status, dict) or not isinstance(status.get('ranking'), list):
        return []
    out = []
    for s in status['ranking'][start_idx:]:
        if not isinstance(s, dict):
            continue
        name = s.get('seo_name') or s.get('name') or ''
        if not name:
            continue
        w = int(s.get('women') or 0)
        m = int(s.get('men') or 0)
        we = s.get('wait_estimate') if isinstance(s.get('wait_estimate'), dict) else {}
        fc = s.get('forecast_60m') if isinstance(s.get('forecast_60m'), dict) else {}
        votes = s.get('votes') if isinstance(s.get('votes'), dict) else {}
        hl = s.get('human_line') or build_dynamic_human_line(s, is_women_view=False)

        ssr_rank = s.get('ssr_rank')
        if not is_staff and ssr_rank:
            rk = f"{int(ssr_rank):02d}"
        elif ssr_rank:
            rk = str(ssr_rank)
        else:
            rk = '-'

        w_diff = int(s.get('women_diff') or 0)
        m_diff = int(s.get('men_diff') or 0)
        v_smooth = int(votes.get('smooth') or 0)
        v_wait = int(votes.get('waiting') or 0)
        mt = ''
        mtt = ''
        if w_diff >= 1:
            mt = f'+{w_diff}名'
            mtt = f'直近約15分で女性が+{w_diff}名増えています'
        elif v_smooth > 0 and v_smooth >= v_wait:
            mt = '即入店'
            mtt = '直近の現場レポートで「待ちなしで入れた」が報告されています'

        w_badge = s.get('wait_pill_men') or ''
        f_badge = s.get('forecast_pill_men') or ''

        entry = {
            'n': name,
            'rk': rk,
            'bc': s.get('ssr_brand_cls') or '',
            'br': s.get('ssr_brand') or '',
            'ar': s.get('ssr_area') or '',
            'ti': s.get('ssr_title') or name,
            'hl': hl,
            'hlw': s.get('human_line_women') or '',
            'fl': s.get('freshness_label') or '',
            'fls': s.get('freshness_short') or '',
            'ars': 1 if s.get('area_redundant_sp') else 0,
            'sp': s.get('spec_price_short') or '',
            'sa': s.get('spec_age_short') or '',
            'sh': s.get('spec_hours_short') or '',
            'mu': s.get('map_url') or '',
            'io': 1 if (s.get('is_open_now') or (w + m) > 0) else 0,
            'ss': 1 if s.get('is_small_sample') else 0,
            'w': w,
            'm': m,
            'wp': int(s.get('ssr_ladies_pct') or 0),
            'mp': int(s.get('ssr_men_pct') or 0),
            'psg': s.get('plan2_signal_text') or '',
            'psc': s.get('plan2_signal_cls') or 'is-ok',
            'psb': (s.get('plan2_staff_sub_text') if is_staff else s.get('plan2_sub_text')) or '',
            'pfc': s.get('plan2_forecast_text') or '',
            'ndp': 1 if s.get('plan2_needs_brand_pfx') else 0,
        }
        if s.get('ssr_is_dup'):
            entry['dup'] = 1
            entry['dp'] = s.get('ssr_disp') or ''
        if not is_staff and s.get('ssr_event_badge'):
            entry['eb'] = s.get('ssr_event_badge')
            entry['et'] = s.get('ssr_event_title') or ''
        if mt:
            entry['mt'] = mt
            entry['mtt'] = mtt
        if s.get('has_measured_core') and s.get('core_time_short'):
            entry['ct'] = s.get('core_time_short')
        if s.get('midnight_plain'):
            entry['mp_txt'] = s.get('midnight_plain')
        if w_badge:
            entry['wb'] = w_badge
            entry['wt'] = we.get('wait_tone') or 'neutral'
            entry['wti'] = we.get('pos_summary_short') or ''
        if f_badge:
            entry['fb'] = f_badge
            entry['ft'] = fc.get('badge_tone') or 'neutral'
            entry['fti'] = fc.get('reason_short') or ''
        if w_diff > 0:
            entry['wd'] = w_diff
        if m_diff > 0:
            entry['md'] = m_diff
        if s.get('usual_w_diff') is not None:
            entry['uwd'] = int(round(float(s.get('usual_w_diff'))))
            entry['uw'] = s.get('usual_women')
            entry['usl'] = s.get('usual_slot_label') or ''
        if s.get('usual_m_diff') is not None:
            entry['umd'] = int(round(float(s.get('usual_m_diff'))))
            entry['um'] = s.get('usual_men')
            entry['usl'] = s.get('usual_slot_label') or ''
        out.append(entry)
    return out
