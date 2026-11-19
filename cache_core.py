"""cache_core.py - Shared HTTP/SSR/JSON/HTML caching, SWR, Single-Flight & live_gen invalidation (S3)."""

import datetime
import gzip
import importlib
import json
import os
import re
import sys
import threading
import time
from collections import OrderedDict

from flask import Response, request

_ssr_payload_cache_lock = threading.Lock()
_ssr_payload_cache = {'key': None, 'payload': None}
_ssr_inflight = {}

_status_json_cache_lock = threading.Lock()
_status_json_cache = {'key': None, 'raw': None, 'gzip': None, 'etag': None}
_status_inflight = {}

_api_json_cache_lock = threading.Lock()
_api_json_cache = OrderedDict()
_api_inflight = {}

_page_html_cache_lock = threading.Lock()
_page_html_cache = OrderedDict()
_editorial_html_cache = OrderedDict()
_html_inflight = {}
_PAGE_HTML_CACHE_MAX = 128
_EDITORIAL_HTML_CACHE_MAX = 64

_ogp_bytes_cache_lock = threading.Lock()
_ogp_bytes_cache = OrderedDict()
_OGP_BYTES_CACHE_MAX = 24

_sns_cache_lock = threading.Lock()
_staff_editorial_preview_cache = {'ts': 0.0, 'sig': None, 'data': None}

_aux_doc_cache_lock = threading.Lock()
_sitemap_xml_cache = {'key': None, 'xml': None}
_llms_txt_cache = {'key': None, 'short': None, 'full': None, 'facts': None}

_CACHE_QUERY_ALLOWLIST = ('s', 'area', 'slot', 'channel', 'switch', 'days', 'format', 'share')

_live_gen_lock = threading.Lock()
_live_gen = 0
_editorial_gen = 0


def is_fast_cache_tokens_enabled() -> bool:
    return os.environ.get('FAST_CACHE_TOKENS') == '1'


def get_live_gen() -> int:
    with _live_gen_lock:
        return _live_gen


def get_editorial_gen() -> int:
    with _live_gen_lock:
        return _editorial_gen


def _bump_live_gen(bump_editorial: bool = False) -> int:
    global _live_gen, _editorial_gen
    with _live_gen_lock:
        _live_gen += 1
        if bump_editorial:
            _editorial_gen += 1
        return _live_gen


def _get_app_mod():
    return sys.modules.get('app') or sys.modules.get('__main__')


def _get_local_db():
    mod = _get_app_mod()
    if mod is not None and hasattr(mod, 'local_db'):
        return mod.local_db
    import local_db
    return local_db


def _get_scheduled_articles():
    mod = _get_app_mod()
    if mod is not None and hasattr(mod, 'scheduled_articles'):
        return mod.scheduled_articles
    import scheduled_articles
    return scheduled_articles


def _normalize_cache_query_args() -> tuple:
    if not request.args:
        return ()
    out = []
    for k in _CACHE_QUERY_ALLOWLIST:
        if k not in request.args:
            continue
        raw_v = (request.args.get(k) or '').strip()
        if k == 's':
            if re.fullmatch(r'\d{8}_\d{4}', raw_v):
                norm_v = raw_v
            else:
                import analytics
                norm_v = analytics.normalize_inflow_source(raw_v)
            if norm_v:
                out.append(('s', norm_v))
            continue
        out.append((k, raw_v[:64]))
    return tuple(out)


def invalidate_live_caches(clear_editorial: bool = False) -> int:
    """O(1) generation bump for live snapshots in FAST_CACHE_TOKENS=1 mode (or full clear fallback)."""
    if not is_fast_cache_tokens_enabled():
        clear_fast_http_caches(clear_editorial=clear_editorial)
        return get_live_gen()
    new_gen = _bump_live_gen(bump_editorial=clear_editorial)
    if clear_editorial:
        with _aux_doc_cache_lock:
            _llms_txt_cache['key'] = None
            _llms_txt_cache['short'] = None
            _llms_txt_cache['full'] = None
            _llms_txt_cache['facts'] = None
            _sitemap_xml_cache['key'] = None
            _sitemap_xml_cache['xml'] = None
            _sf_mod = sys.modules.get('routes.seo_feeds_bp')
            if _sf_mod is not None and isinstance(_sf_mod, type(sys)):
                _sf_mod._DOC_BUNDLE_STATE['bundle'] = None
                _sf_mod._VOLATILE_BUNDLE_CACHE['mono'] = 0.0
                _sf_mod._VOLATILE_BUNDLE_CACHE['key'] = None
                _sf_mod._VOLATILE_BUNDLE_CACHE['bundle'] = None
                _sf_mod._HIST_RECORDS_CACHE['mono'] = 0.0
                _sf_mod._HIST_RECORDS_CACHE['status'] = ''
        try:
            _pb_mod = importlib.import_module('routes.portal_bp')
            _pb_mod.clear_monthly_csv_cache()
        except Exception:
            pass
    return new_gen


def clear_fast_http_caches(clear_editorial: bool = True):
    """Clear short-lived SSR/HTML/status caches when underlying data mutates."""
    _bump_live_gen(bump_editorial=clear_editorial)
    with _ssr_payload_cache_lock:
        _ssr_payload_cache['key'] = None
        _ssr_payload_cache['payload'] = None
    with _status_json_cache_lock:
        _status_json_cache['key'] = None
        _status_json_cache['raw'] = None
        _status_json_cache['gzip'] = None
        _status_json_cache['etag'] = None
    with _api_json_cache_lock:
        _api_json_cache.clear()
    with _page_html_cache_lock:
        _page_html_cache.clear()
        if clear_editorial:
            _editorial_html_cache.clear()
    with _ogp_bytes_cache_lock:
        _ogp_bytes_cache.clear()
    with _aux_doc_cache_lock:
        _llms_txt_cache['key'] = None
        _llms_txt_cache['short'] = None
        _llms_txt_cache['full'] = None
        _llms_txt_cache['facts'] = None
        if clear_editorial:
            _sitemap_xml_cache['key'] = None
            _sitemap_xml_cache['xml'] = None
            _sf_mod = sys.modules.get('routes.seo_feeds_bp')
            if _sf_mod is not None and isinstance(_sf_mod, type(sys)):
                _sf_mod._DOC_BUNDLE_STATE['bundle'] = None
                _sf_mod._VOLATILE_BUNDLE_CACHE['mono'] = 0.0
                _sf_mod._VOLATILE_BUNDLE_CACHE['key'] = None
                _sf_mod._VOLATILE_BUNDLE_CACHE['bundle'] = None
                _sf_mod._HIST_RECORDS_CACHE['mono'] = 0.0
                _sf_mod._HIST_RECORDS_CACHE['status'] = ''
    if clear_editorial:
        try:
            _pb_mod = importlib.import_module('routes.portal_bp')
            _pb_mod.clear_monthly_csv_cache()
        except Exception:
            pass
        _st_mod = sys.modules.get('routes.staff_bp')
        with _sns_cache_lock:
            _staff_editorial_preview_cache['ts'] = 0.0
            _staff_editorial_preview_cache['sig'] = None
            _staff_editorial_preview_cache['data'] = None
            if _st_mod is not None:
                _st_mod._sns_kit_ctx_cache['ctx'] = None
                _st_mod._sns_kit_ctx_cache['key'] = None
                _st_mod._sns_kit_ctx_cache['ts'] = 0.0
                _st_mod._sns_slide_bytes_cache.clear()
                _st_mod._sns_zip_bytes_cache['key'] = None
                _st_mod._sns_zip_bytes_cache['ts'] = 0.0
                _st_mod._sns_zip_bytes_cache['bytes'] = None


def _build_precompressed_response(raw_bytes: bytes, gz_bytes: bytes, content_type: str, status_code: int = 200):
    wants_gzip = (gz_bytes is not None) and ('gzip' in request.headers.get('Accept-Encoding', '').lower())
    if wants_gzip:
        resp = Response(gz_bytes, status=status_code, content_type=content_type)
        resp.headers['Content-Encoding'] = 'gzip'
        resp.headers['Content-Length'] = len(gz_bytes)
        resp.vary.add('Accept-Encoding')
        return resp
    resp = Response(raw_bytes, status=status_code, content_type=content_type)
    resp.vary.add('Accept-Encoding')
    return resp


def _cached_json_response(cache_key, build_dict_fn, ttl_sec=30.0):
    now_mono = time.monotonic()
    ev = None
    is_leader = False
    with _api_json_cache_lock:
        hit = _api_json_cache.get(cache_key)
        if hit is not None and (now_mono - hit[0] < ttl_sec):
            _api_json_cache.move_to_end(cache_key)
            return _build_precompressed_response(hit[1], hit[2], 'application/json', hit[3])
        inflight = _api_inflight.get(cache_key)
        if inflight is not None and (now_mono - inflight[1] < 10.0):
            ev = inflight[0]
        else:
            ev = threading.Event()
            _api_inflight[cache_key] = (ev, now_mono)
            is_leader = True

    if not is_leader and ev is not None:
        ev.wait(timeout=0.5)
        now2 = time.monotonic()
        with _api_json_cache_lock:
            hit2 = _api_json_cache.get(cache_key)
            if hit2 is not None and (now2 - hit2[0] < ttl_sec):
                _api_json_cache.move_to_end(cache_key)
                return _build_precompressed_response(hit2[1], hit2[2], 'application/json', hit2[3])

    try:
        payload, status_code = build_dict_fn()
        raw_bytes = json.dumps(payload, ensure_ascii=False).encode('utf-8')
        gz_bytes = gzip.compress(raw_bytes, compresslevel=4) if (status_code == 200 and len(raw_bytes) > 500) else None
        if status_code == 200:
            store_mono = time.monotonic()
            with _api_json_cache_lock:
                _api_json_cache[cache_key] = (store_mono, raw_bytes, gz_bytes, status_code)
                _api_json_cache.move_to_end(cache_key)
                while len(_api_json_cache) > 96:
                    _api_json_cache.popitem(last=False)
        return _build_precompressed_response(raw_bytes, gz_bytes, 'application/json', status_code)
    finally:
        if is_leader:
            with _api_json_cache_lock:
                cur = _api_inflight.get(cache_key)
                if cur is not None and cur[0] is ev:
                    _api_inflight.pop(cache_key, None)
            ev.set()


def _live_data_signature(stores, last_updated=None, is_seed=False):
    return (
        last_updated,
        bool(is_seed),
        tuple(
            (
                s.get('name'),
                int(s.get('women') or 0),
                int(s.get('men') or 0),
                int(s.get('women_diff') or 0),
                int(s.get('men_diff') or 0),
                int((s.get('votes') or {}).get('smooth') or 0),
                int((s.get('votes') or {}).get('waiting') or 0),
                int((s.get('votes') or {}).get('lively') or 0),
                int((s.get('pos_telemetry') or {}).get('waiting_groups') or 0),
                int((s.get('pos_telemetry') or {}).get('matched_groups') or 0),
            )
            for s in (stores or [])
        ),
    )


def _get_cached_events_nonblocking():
    """Return events from memory or disk cache without spawning network threads."""
    try:
        import events as _ev
        mem_evs = _ev._events_memory_cache.get('events')
        if mem_evs:
            return mem_evs, _ev._events_memory_cache.get('updated_at')
        if os.path.exists(_ev.CACHE_FILE):
            with open(_ev.CACHE_FILE, 'r', encoding='utf-8') as f:
                disk_data = json.load(f)
            if disk_data.get('events'):
                disk_data['events'] = _ev._ensure_regional_events(disk_data['events'])
                _ev._events_memory_cache = disk_data
                return disk_data['events'], disk_data.get('updated_at')
    except Exception:
        pass
    return [], None


def _gov_sig():
    try:
        import ops_governance as _og
        return (tuple(sorted(_og.get_hidden_brands())), tuple(sorted(_og.get_hidden_stores())))
    except Exception:
        return None


def _runtime_env_probe():
    """Lightweight microsecond probe so business-hour transitions or test mocks immediately invalidate HTML/SSR caches."""
    local_db = _get_local_db()
    sh_sig = None
    ev_sig = None
    db_sig = id(getattr(local_db, 'get_store_7day_analytics', None))
    wr_mod = sys.modules.get('weekly_report')
    wr_sig = id(getattr(wr_mod, 'generate_weekly_report', None)) if wr_mod else None
    try:
        import store_hours as _sh
        hp = _sh.get_business_hours_status('OLG 新宿', total_guests=0)
        sh_sig = (id(_sh.get_business_hours_status), hp.get('is_open_now'), hp.get('today_hours_label'))
    except Exception:
        pass
    try:
        import events as _ev
        ev_sig = (id(_ev.fetch_all_events), id(_ev.get_events_for_store))
    except Exception:
        pass
    return (sh_sig, ev_sig, db_sig, wr_sig, _gov_sig())


def _is_static_editorial_route(path: str) -> bool:
    return path == '/guide' or path.startswith('/guide/') or path in ('/grooming', '/privacy', '/about', '/columns')


def _get_cached_html_or_render(cache_extra_key, render_fn, ttl_sec=660.0, cache_if=None):
    """Fast thread-safe memoizer with Single-Flight & SWR for rendered HTML strings."""
    from flask import session
    is_editorial = _is_static_editorial_route(request.path)
    scheduled_articles = _get_scheduled_articles()
    local_db = _get_local_db()
    pub_slugs = tuple(a.get('slug') for a in scheduled_articles.get_published_articles())
    norm_q = _normalize_cache_query_args()
    is_staff = bool(session.get('is_staff'))
    is_master = bool(session.get('is_master'))
    fast_tokens = is_fast_cache_tokens_enabled()

    slot_key = (request.path, norm_q, is_staff, is_master, cache_extra_key)

    if is_editorial:
        eff_ttl = max(ttl_sec, 300.0)
        if fast_tokens:
            fresh_token = ('ed', getattr(local_db, 'DB_PATH', None), pub_slugs, get_editorial_gen(), _gov_sig())
        else:
            fresh_token = ('ed', pub_slugs, _runtime_env_probe())
        target_cache = _editorial_html_cache
        max_entries = _EDITORIAL_HTML_CACHE_MAX
    else:
        eff_ttl = ttl_sec
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        bucket_15m = now_jst.strftime('%Y%m%d%H%M') if request.path == '/' else f"{now_jst.strftime('%Y%m%d%H')}_{(now_jst.minute // 15) * 15:02d}"
        _, ev_upd = _get_cached_events_nonblocking()
        if fast_tokens:
            fresh_token = (
                'live',
                getattr(local_db, 'DB_PATH', None),
                bucket_15m,
                pub_slugs,
                ev_upd,
                get_live_gen(),
                _gov_sig(),
            )
        else:
            mod = _get_app_mod()
            data_lock = getattr(mod, 'data_lock', None)
            latest_data = getattr(mod, 'latest_data', None) or {}
            if data_lock is not None:
                with data_lock:
                    cur_stores = latest_data.get('full_data') or []
                    last_upd = latest_data.get('last_updated')
                    is_seed = bool(latest_data.get('is_seed'))
                    live_sig = _live_data_signature(cur_stores, last_upd, is_seed)
            else:
                live_sig = None
            fresh_token = (
                'live',
                getattr(local_db, 'DB_PATH', None),
                bucket_15m,
                pub_slugs,
                ev_upd,
                _runtime_env_probe(),
                live_sig,
            )
        target_cache = _page_html_cache
        max_entries = _PAGE_HTML_CACHE_MAX

    inflight_key = (is_editorial, slot_key)
    now_mono = time.monotonic()
    ev = None
    is_leader = False
    swr_hit = None

    with _page_html_cache_lock:
        hit = target_cache.get(slot_key)
        if hit is not None and hit[0] == fresh_token and (now_mono - hit[1] < eff_ttl):
            target_cache.move_to_end(slot_key)
            return _build_precompressed_response(hit[2], hit[3], 'text/html; charset=utf-8')

        inflight = _html_inflight.get(inflight_key)
        if inflight is not None and (now_mono - inflight[1] < 10.0):
            if hit is not None and not is_staff and not is_master:
                swr_hit = (hit[2], hit[3])
            else:
                ev = inflight[0]
        else:
            ev = threading.Event()
            _html_inflight[inflight_key] = (ev, now_mono)
            is_leader = True

    if swr_hit is not None:
        return _build_precompressed_response(swr_hit[0], swr_hit[1], 'text/html; charset=utf-8')

    if not is_leader and ev is not None:
        ev.wait(timeout=0.5)
        now2 = time.monotonic()
        with _page_html_cache_lock:
            hit2 = target_cache.get(slot_key)
            if hit2 is not None and hit2[0] == fresh_token and (now2 - hit2[1] < eff_ttl):
                target_cache.move_to_end(slot_key)
                return _build_precompressed_response(hit2[2], hit2[3], 'text/html; charset=utf-8')

    try:
        rendered = render_fn()
        if isinstance(rendered, str):
            raw_bytes = rendered.encode('utf-8')
            gz_bytes = gzip.compress(raw_bytes, compresslevel=4) if len(raw_bytes) > 500 else None
            should_store = True if cache_if is None else bool(cache_if())
            if should_store:
                store_mono = time.monotonic()
                with _page_html_cache_lock:
                    target_cache[slot_key] = (fresh_token, store_mono, raw_bytes, gz_bytes)
                    target_cache.move_to_end(slot_key)
                    while len(target_cache) > max_entries:
                        target_cache.popitem(last=False)
            return _build_precompressed_response(raw_bytes, gz_bytes, 'text/html; charset=utf-8')
        return rendered
    finally:
        if is_leader:
            with _page_html_cache_lock:
                cur = _html_inflight.get(inflight_key)
                if cur is not None and cur[0] is ev:
                    _html_inflight.pop(inflight_key, None)
            ev.set()
