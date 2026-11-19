"""routes/live_api_bp.py - Public live analytics, weekly report, events & votes API Blueprint (S4)."""

import logging
import threading
import time

from flask import Blueprint, jsonify, request

from routes._bridge import core

live_api_bp = Blueprint('live_api_bp', __name__)

_COLD_ANALYSIS_LOCKS = {}
_COLD_ANALYSIS_LOCK_GUARD = threading.Lock()
_WEEKLY_COLD_LOCK = threading.Lock()
_WEEKLY_ERR_MEMO = {'mono': 0.0, 'db_key': None, 'payload': None}


def _get_analysis_lock(store_key: str) -> threading.Lock:
    with _COLD_ANALYSIS_LOCK_GUARD:
        lk = _COLD_ANALYSIS_LOCKS.get(store_key)
        if lk is None:
            if len(_COLD_ANALYSIS_LOCKS) > 128:
                _COLD_ANALYSIS_LOCKS.clear()
            lk = threading.Lock()
            _COLD_ANALYSIS_LOCKS[store_key] = lk
        return lk


@live_api_bp.route('/api/analysis/<path:store_name>')
def get_store_analysis(store_name):
    """Returns aggregated hourly and weekly data for a specific store."""
    clean_name = (store_name or '').strip()
    if not clean_name or len(clean_name) > 80 or any(ord(ch) < 32 for ch in clean_name):
        return jsonify({'error': 'Store not found'}), 404

    import ops_governance
    import store_master
    internal_name = store_master.to_internal_store_name(clean_name)
    if ops_governance.is_store_hidden(internal_name):
        return jsonify({'error': 'Store not found'}), 404
    master_map = store_master.load()
    if internal_name not in master_map:
        with core.data_lock:
            live_names = {s.get('name') for s in (core.latest_data.get('full_data') or [])}
        cached_proc = (core.analysis_cache.get('processed_per_store') or {}).get(internal_name)
        if internal_name not in live_names and cached_proc is None:
            return jsonify({'error': 'Store not found'}), 404
    clean_name = internal_name

    import store_analysis
    raw_entry = (core.analysis_cache.get('raw_data_by_store') or {}).get(clean_name)
    proc_entry = (core.analysis_cache.get('processed_per_store') or {}).get(clean_name)
    raw_ts = raw_entry.get('last_fetched') if isinstance(raw_entry, dict) else None

    def _safe_analysis_call():
        p, code = store_analysis.compute_store_analysis_payload(
            clean_name,
            analysis_cache=core.analysis_cache,
            cache_duration=core.ANALYSIS_CACHE_DURATION,
        )
        if isinstance(p, dict) and 'store_name' in p:
            p = dict(p)
            p['store_name'] = store_master.seo_name(internal_name)
        return p, code

    if proc_entry is not None and raw_ts is not None:
        return core._cached_json_response(
            ('analysis', core.local_db.DB_PATH, clean_name, raw_ts),
            _safe_analysis_call,
            ttl_sec=60.0,
        )
    with _get_analysis_lock(clean_name):
        proc_entry2 = (core.analysis_cache.get('processed_per_store') or {}).get(clean_name)
        raw_entry2 = (core.analysis_cache.get('raw_data_by_store') or {}).get(clean_name)
        raw_ts2 = raw_entry2.get('last_fetched') if isinstance(raw_entry2, dict) else None
        if proc_entry2 is not None and raw_ts2 is not None:
            return core._cached_json_response(
                ('analysis', core.local_db.DB_PATH, clean_name, raw_ts2),
                _safe_analysis_call,
                ttl_sec=60.0,
            )
        payload, status_code = _safe_analysis_call()
    if status_code == 200:
        new_proc = (core.analysis_cache.get('processed_per_store') or {}).get(clean_name)
        new_raw = (core.analysis_cache.get('raw_data_by_store') or {}).get(clean_name)
        new_ts = new_raw.get('last_fetched') if isinstance(new_raw, dict) else None
        if new_proc is not None and new_ts is not None:
            return core._cached_json_response(
                ('analysis', core.local_db.DB_PATH, clean_name, new_ts),
                lambda: (payload, 200),
                ttl_sec=60.0,
            )
    return jsonify(payload), status_code


@live_api_bp.route('/api/weekly-report')
def get_weekly_report():
    try:
        import ops_governance
        import store_master
        import weekly_report

        hb = set(ops_governance.get_hidden_brands() or ())
        hs = set(ops_governance.get_hidden_stores() or ())
        gov_sig = (tuple(sorted(hb)), tuple(sorted(hs)))

        def _is_hidden_name(raw_n: str) -> bool:
            if not raw_n or (not hb and not hs):
                return False
            return ops_governance.is_store_hidden(raw_n, hb=hb, hs=hs)

        def _clean_report(rep):
            if not isinstance(rep, dict):
                return rep
            rep_clean = dict(rep)
            for k in ('stores', 'anaba_stores'):
                new_list = []
                for s in rep_clean.get(k, []):
                    item = dict(s)
                    raw_n = item.get('name') or ''
                    if _is_hidden_name(raw_n):
                        continue
                    if item.get('brand') == 'OLG':
                        item['brand'] = 'オリエンタルラウンジ'
                    if 'name' in item:
                        item['name'] = store_master.seo_name(item['name'])
                    new_list.append(item)
                rep_clean[k] = new_list
            if 'by_brand' in rep_clean and isinstance(rep_clean['by_brand'], dict):
                cleaned_bb = {}
                brand_key_map = {
                    'OLG': 'oriental-lounge',
                    'オリエンタルラウンジ': 'oriental-lounge',
                    'AG': 'ag',
                    'ag': 'ag',
                    'JIS': 'jis',
                    'ASOBIBAR': 'asobibar',
                    'ヤタコイ': 'yatakoi',
                    'YATAKOI': 'yatakoi',
                    'XIX': 'xix',
                    'CLOVERS': 'clovers',
                    'ALFA': 'alfa',
                }
                for b_name, b_data in rep_clean['by_brand'].items():
                    b_key = brand_key_map.get(str(b_name or '').strip(), str(b_name or '').strip().lower())
                    if b_key in hb:
                        continue
                    safe_b = 'オリエンタルラウンジ' if b_name == 'OLG' else b_name
                    cleaned_bb[safe_b] = b_data
                rep_clean['by_brand'] = cleaned_bb
            return rep_clean

        def _build_weekly():
            report = weekly_report.generate_weekly_report()
            if not isinstance(report, dict) or 'error' in report:
                if isinstance(report, dict) and report.get('error'):
                    logging.warning('[weekly_report] generation returned error: %s', report.get('error'))
                return {'status': 'error', 'error': 'weekly_report_unavailable'}, 500
            return {'status': 'success', 'data': _clean_report(report)}, 200

        lf = weekly_report._report_cache.get('last_fetched')
        gen_sig = id(getattr(weekly_report, 'generate_weekly_report', None))
        if lf is None:
            now_m = time.monotonic()
            with _WEEKLY_COLD_LOCK:
                if (
                    _WEEKLY_ERR_MEMO['payload'] is not None
                    and _WEEKLY_ERR_MEMO['db_key'] == core.local_db.DB_PATH
                    and (now_m - _WEEKLY_ERR_MEMO['mono']) < 10.0
                ):
                    return jsonify(_WEEKLY_ERR_MEMO['payload']), 500
                payload, code = _build_weekly()
                if code != 200:
                    _WEEKLY_ERR_MEMO['mono'] = time.monotonic()
                    _WEEKLY_ERR_MEMO['db_key'] = core.local_db.DB_PATH
                    _WEEKLY_ERR_MEMO['payload'] = payload
                    return jsonify(payload), code
                _WEEKLY_ERR_MEMO['payload'] = None
                return jsonify(payload), code
        return core._cached_json_response(('weekly_report', core.local_db.DB_PATH, lf, gen_sig, gov_sig), _build_weekly, ttl_sec=60.0)
    except Exception:
        logging.exception('[api/weekly-report] unexpected failure')
        return jsonify({'status': 'error', 'error': 'weekly_report_unavailable'}), 500


@live_api_bp.route('/api/events')
def get_events():
    """Returns active events and promotions for lounge stores."""
    try:
        import events
        import ops_governance
        import store_master

        hb = set(ops_governance.get_hidden_brands() or ())
        hs = set(ops_governance.get_hidden_stores() or ())
        gov_sig = (tuple(sorted(hb)), tuple(sorted(hs)))
        brand_key_map = {
            'OLG': 'oriental-lounge',
            'オリエンタルラウンジ': 'oriental-lounge',
            'AG': 'ag',
            'ag': 'ag',
            'JIS': 'jis',
            'ASOBIBAR': 'asobibar',
            'ヤタコイ': 'yatakoi',
            'YATAKOI': 'yatakoi',
            'XIX': 'xix',
            'CLOVERS': 'clovers',
            'ALFA': 'alfa',
        }

        def _clean_evs(ev_list):
            out = []
            for ev in (ev_list or []):
                if not isinstance(ev, dict):
                    continue
                raw_st = ev.get('store_name') or ''
                if raw_st and ops_governance.is_store_hidden(raw_st, hb=hb, hs=hs):
                    continue
                raw_br = str(ev.get('brand') or '').strip()
                if raw_br and brand_key_map.get(raw_br, raw_br.lower()) in hb:
                    continue
                item = dict(ev)
                if item.get('brand') == 'OLG':
                    item['brand'] = 'オリエンタルラウンジ'
                if 'target_stores' in item and isinstance(item['target_stores'], list):
                    orig_targets = item['target_stores']
                    visible_targets = [
                        store_master.seo_name(s)
                        for s in orig_targets
                        if not ops_governance.is_store_hidden(s, hb=hb, hs=hs)
                    ]
                    if orig_targets and not visible_targets:
                        continue
                    item['target_stores'] = visible_targets
                out.append(item)
            return out

        store = (request.args.get('store') or '').strip()
        internal_store = ''
        if store:
            if len(store) > 80 or any(ord(ch) < 32 for ch in store):
                return jsonify({'status': 'error', 'error': 'Invalid store name'}), 400
            internal_store = store_master.to_internal_store_name(store)
            if not store_master.get(internal_store) or ops_governance.is_store_hidden(internal_store, hb=hb, hs=hs):
                return jsonify({'status': 'error', 'error': 'Store not found'}), 404

        force = (
            (request.args.get('refresh', '').lower() == 'true')
            and core.is_master_authorized()
            and (request.headers.get('Sec-Fetch-Site') or '').lower() != 'cross-site'
        )
        if not force:
            ev_upd = events._events_memory_cache.get('updated_at')
            ev_cnt = len(events._events_memory_cache.get('events') or [])
            if ev_cnt > 0:
                def _build_cached_events():
                    evs = (
                        events.get_events_for_store(internal_store, allow_blocking=False)
                        if internal_store
                        else events.fetch_all_events(force_refresh=False, allow_blocking=False)
                    )
                    cleaned = _clean_evs(evs)
                    return {'status': 'success', 'count': len(cleaned), 'events': cleaned}, 200

                return core._cached_json_response(
                    ('events', internal_store, ev_upd, ev_cnt, gov_sig),
                    _build_cached_events,
                    ttl_sec=60.0,
                )
        if internal_store:
            evs = events.get_events_for_store(internal_store, allow_blocking=force)
        else:
            evs = events.fetch_all_events(force_refresh=force, allow_blocking=force)
        cleaned = _clean_evs(evs)
        return jsonify({
            'status': 'success',
            'count': len(cleaned),
            'events': cleaned,
        })
    except Exception:
        logging.exception('[api/events] unexpected failure')
        return jsonify({'status': 'error', 'error': 'events_unavailable'}), 500


@live_api_bp.route('/api/votes/<path:store_name>')
def get_store_votes(store_name):
    """Returns user crowd votes in the last 4 hours for a specific store."""
    import store_master
    import ops_governance
    if not store_name or len(store_name) > 80 or any(ord(ch) < 32 for ch in store_name):
        return jsonify({'status': 'error', 'error': 'Invalid store name'}), 400
    internal_name = store_master.to_internal_store_name(store_name)
    if not store_master.get(internal_name):
        return jsonify({'status': 'error', 'error': 'Store not found'}), 404
    if ops_governance.is_store_hidden(internal_name or store_name):
        return jsonify({'status': 'error', 'error': 'Store not found'}), 404
    counts = core.local_db.get_recent_votes(store_name=internal_name, hours=4)
    disp_name = store_master.seo_name(internal_name) if internal_name else store_name
    return jsonify({'status': 'success', 'store_name': disp_name, 'votes': counts})
