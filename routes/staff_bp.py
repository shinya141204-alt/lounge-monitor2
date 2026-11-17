"""Staff / Master management dashboards and administrative JSON APIs Blueprint."""

from collections import OrderedDict
import datetime
import hashlib
import hmac
import logging
import secrets
import threading
import time
from urllib.parse import quote

from flask import Blueprint, Response, jsonify, redirect, render_template, request, session, url_for

from routes._bridge import core

staff_bp = Blueprint('staff_bp', __name__)

_sns_kit_ctx_cache = {'ctx': None, 'key': None, 'ts': 0.0}
_sns_slide_bytes_cache = OrderedDict()  # (slide_num, is_jpg, ctx_sig) -> (ts, bytes)
_sns_zip_bytes_cache = {'key': None, 'ts': 0.0, 'bytes': None}


def _is_same_origin_get_action_allowed() -> bool:
    """Allow expensive/side-effecting GET flags (`refresh_ai=1`, `flush=1`) only on same-origin or secret-authenticated requests."""
    if core._secret_header_ok():
        return True
    sec_fetch = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
    if sec_fetch and sec_fetch not in ('same-origin', 'none'):
        return False
    return True


def _get_visible_live_stores() -> list:
    import ops_governance
    with core.data_lock:
        raw_s = list(core.latest_data.get('full_data') or [])
    return ops_governance.filter_visible_stores(raw_s)


def _safe_clamped_days(raw_val, default: int = 14) -> int:
    try:
        parsed = int(raw_val) if raw_val is not None and str(raw_val).strip() != '' else default
    except (TypeError, ValueError):
        parsed = default
    return max(1, min(90, parsed))


def _sns_context_sig(ctx: dict) -> tuple:
    if not isinstance(ctx, dict):
        return ()
    vol_sig = tuple(
        (
            p.get('raw_name') or p.get('short_name'),
            p.get('women') if p.get('women') is not None else p.get('peak_women'),
            p.get('men'),
            p.get('avg_wpct'),
        )
        for p in (ctx.get('top_volume') or ctx.get('top_picks') or [])[:5]
    )
    ana_sig = tuple(
        (
            p.get('raw_name') or p.get('short_name'),
            p.get('women') if p.get('women') is not None else p.get('peak_women'),
            p.get('avg_wpct'),
        )
        for p in (ctx.get('top_anaba') or [])[:3]
    )
    return (ctx.get('theme_key'), ctx.get('weekend_label'), vol_sig, ana_sig)


def _get_sns_kit_context(fast=False):
    import ig_carousel_generator
    import weekly_report
    now_t = time.time()
    live_s = _get_visible_live_stores()
    live_top_sig = tuple((s.get('name'), int(s.get('women') or 0), int(s.get('men') or 0)) for s in live_s[:8])
    cache_key = (
        core.local_db.DB_PATH,
        live_top_sig,
        datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d%H'),
    )
    ctx_cache = core._sns_kit_ctx_cache
    with core._sns_cache_lock:
        if ctx_cache['ctx'] is not None and ctx_cache['key'] == cache_key and (now_t - ctx_cache['ts'] < 300):
            return ctx_cache['ctx']

    shared_ctx_wrap = core.turbo_store.get_ai_cache("ig_kit_ctx", "latest")
    if isinstance(shared_ctx_wrap, dict) and isinstance(shared_ctx_wrap.get("ctx"), dict):
        saved_top_sig = tuple(tuple(x) for x in (shared_ctx_wrap.get("live_top_sig") or []))
        if saved_top_sig == live_top_sig or fast or core.is_web_only_role():
            ctx = dict(shared_ctx_wrap["ctx"])
            if isinstance(ctx.get("theme"), dict):
                restored_theme = dict(ctx["theme"])
                for tk, tv in list(restored_theme.items()):
                    if isinstance(tv, list):
                        restored_theme[tk] = tuple(tv)
                ctx["theme"] = restored_theme
            with core._sns_cache_lock:
                ctx_cache['ctx'] = ctx
                ctx_cache['key'] = cache_key
                ctx_cache['ts'] = now_t
            return ctx

    rep = None
    try:
        cached_rep = (getattr(weekly_report, '_report_cache', None) or {}).get('data')
        if fast and isinstance(cached_rep, dict) and 'error' not in cached_rep:
            rep = cached_rep
        else:
            res = weekly_report.generate_weekly_report()
            if isinstance(res, dict) and 'error' not in res:
                rep = res
    except Exception:
        rep = None

    ctx = ig_carousel_generator.build_forecast_context(report=rep, live_stores=live_s)
    with core._sns_cache_lock:
        ctx_cache['ctx'] = ctx
        ctx_cache['key'] = cache_key
        ctx_cache['ts'] = now_t
    core.turbo_store.set_ai_cache(
        "ig_kit_ctx",
        "latest",
        {"ctx": ctx, "live_top_sig": [list(x) for x in live_top_sig]},
        ttl_sec=900,
    )
    return ctx


def _render_cached_ig_slide_bytes(slide_num: int, is_jpg: bool, ctx: dict) -> bytes:
    import ig_carousel_generator
    clamped = max(1, min(5, int(slide_num)))
    ctx_sig = core._sns_context_sig(ctx)
    cache_key = (clamped, bool(is_jpg), ctx_sig)
    media_blob_key = f"ig_slide:{clamped}:{'jpg' if is_jpg else 'png'}:{core.turbo_store.make_cache_hash(ctx_sig)}"
    now_t = time.time()
    slide_cache = core._sns_slide_bytes_cache
    with core._sns_cache_lock:
        cached = slide_cache.get(cache_key)
        if cached is not None and (now_t - cached[0] < 300):
            slide_cache.move_to_end(cache_key)
            return cached[1]

    shared_blob = core.turbo_store.get_media_blob(media_blob_key)
    if shared_blob and shared_blob[0]:
        img_bytes = shared_blob[0]
        with core._sns_cache_lock:
            slide_cache[cache_key] = (now_t, img_bytes)
            slide_cache.move_to_end(cache_key)
            while len(slide_cache) > 20:
                slide_cache.popitem(last=False)
        return img_bytes

    if is_jpg and hasattr(ig_carousel_generator, 'render_slide_jpeg'):
        img_bytes = ig_carousel_generator.render_slide_jpeg(clamped, ctx=ctx)
    elif is_jpg:
        import io
        from PIL import Image
        png_bytes = ig_carousel_generator.render_slide_png(clamped, ctx=ctx)
        img = Image.open(io.BytesIO(png_bytes)).convert('RGB')
        out = io.BytesIO()
        img.save(out, format='JPEG', quality=92, optimize=True)
        img_bytes = out.getvalue()
    else:
        img_bytes = ig_carousel_generator.render_slide_png(clamped, ctx=ctx)

    with core._sns_cache_lock:
        slide_cache[cache_key] = (now_t, img_bytes)
        slide_cache.move_to_end(cache_key)
        while len(slide_cache) > 20:
            slide_cache.popitem(last=False)
    core.turbo_store.set_media_blob(
        media_blob_key,
        img_bytes,
        mime_type='image/jpeg' if is_jpg else 'image/png',
        ttl_sec=3600,
    )
    return img_bytes


@staff_bp.route('/staff/analytics')
def staff_analytics():
    """Master-only analytics summary (page views, affiliate clicks, button clicks, affiliate slot manager & Sheets retention)."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import analytics
    import affiliate_manager
    import archive_data
    days = max(1, min(90, request.args.get('days', 7, type=int)))
    is_json_req = request.args.get('format') == 'json'
    force_flush = (request.args.get('flush') == '1') and _is_same_origin_get_action_allowed()
    flushed_rows = 0
    if force_flush:
        try:
            flushed_rows = analytics.flush(force_sheets=True)
        except Exception:
            pass
    data = analytics.summary(days=days, allow_blocking=True)
    if is_json_req:
        return jsonify(data)
    aff_status = affiliate_manager.get_affiliate_slots_status(analytics_data=data, allow_blocking=False)
    retention_status = archive_data.get_retention_status(allow_blocking=False)
    try:
        forecast_accuracy = core.local_db.get_forecast_accuracy_report(
            days=max(7, min(30, days)),
            limit_recent=25,
            allow_blocking=True,
            auto_backfill=False,
        )
    except Exception:
        forecast_accuracy = None
    import ops_governance
    ops_gov = ops_governance.get_governance_dashboard_state()
    return render_template(
        'staff_analytics.html',
        data=data,
        days=days,
        flushed=force_flush,
        flushed_rows=flushed_rows,
        aff_status=aff_status,
        retention_status=retention_status,
        forecast_accuracy=forecast_accuracy,
        ops_gov=ops_gov,
    )


def _check_master_api_csrf():
    if not core.is_master_authorized():
        return jsonify({'ok': False, 'error': 'マスター権限が必要です'}), 401
    if request.method == 'POST' and not core._secret_header_ok():
        sec_fetch = (request.headers.get('Sec-Fetch-Site') or '').strip().lower()
        if sec_fetch and sec_fetch not in ('same-origin', 'none'):
            return jsonify({'ok': False, 'error': '不正なリクエスト元です'}), 403
        import contact_form
        if not contact_form.is_same_origin(request.headers.get('Origin'), request.headers.get('Referer'), request.host):
            return jsonify({'ok': False, 'error': 'オリジンが一致しません'}), 403
    return None


@staff_bp.route('/api/ops/governance', methods=['GET', 'POST'])
def api_ops_governance():
    """Master-only API for emergency store/brand opt-out, inquiry inbox, staff force-logout/passcode, and webhook."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import ops_governance
    if request.method == 'GET':
        return jsonify({'ok': True, 'ops_gov': ops_governance.get_governance_dashboard_state()})

    payload = request.get_json(silent=True, force=True) or {}
    action = str(payload.get('action') or '').strip()

    if action == 'toggle_brand':
        res = ops_governance.set_brand_hidden(str(payload.get('brand_key') or ''), bool(payload.get('hidden')))
        if not res.get('ok'):
            return jsonify(res), 400
        core.clear_fast_http_caches()
        return jsonify({**res, 'ops_gov': ops_governance.get_governance_dashboard_state()})

    if action == 'toggle_store':
        res = ops_governance.set_store_hidden(str(payload.get('store_name') or ''), bool(payload.get('hidden')))
        if not res.get('ok'):
            return jsonify(res), 400
        core.clear_fast_http_caches()
        return jsonify({**res, 'ops_gov': ops_governance.get_governance_dashboard_state()})

    if action == 'resolve_inquiry':
        res = ops_governance.set_inquiry_resolved(str(payload.get('inquiry_id') or ''), bool(payload.get('resolved', True)))
        if not res.get('ok'):
            return jsonify(res), 400
        return jsonify({**res, 'ops_gov': ops_governance.get_governance_dashboard_state()})

    if action == 'force_logout_staff':
        res = ops_governance.force_logout_all_staff()
        return jsonify({
            **res,
            'message': '全一般スタッフ端末のログインセッションを即時失効しました（マスターログインは維持されます）。',
            'ops_gov': ops_governance.get_governance_dashboard_state(),
        })

    if action == 'update_staff_passcode':
        res = ops_governance.update_staff_passcode(
            new_passcode=str(payload.get('passcode') or ''),
            reset_to_env=bool(payload.get('reset_to_env', False)),
        )
        if not res.get('ok'):
            return jsonify(res), 400
        return jsonify({**res, 'ops_gov': ops_governance.get_governance_dashboard_state()})

    if action == 'set_webhook':
        res = ops_governance.set_webhook_url(str(payload.get('url') or ''))
        if not res.get('ok'):
            return jsonify(res), 400
        return jsonify({**res, 'ops_gov': ops_governance.get_governance_dashboard_state()})

    if action == 'test_webhook':
        import health
        res = health.send_test_webhook()
        status_code = 200 if res.get('ok') else 400
        return jsonify({**res, 'ops_gov': ops_governance.get_governance_dashboard_state()}), status_code

    return jsonify({'ok': False, 'error': f'未対応の操作です: {action}'}), 400


@staff_bp.route('/api/affiliate/config', methods=['GET', 'POST'])
def api_affiliate_config():
    """Master-only API to inspect or live-update affiliate destination URLs without redeploying."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import analytics
    import affiliate_manager

    if request.method == 'GET':
        days = max(1, min(90, request.args.get('days', 7, type=int)))
        ana_data = analytics.summary(days=days, allow_blocking=False)
        return jsonify({
            'ok': True,
            **affiliate_manager.get_affiliate_slots_status(analytics_data=ana_data, allow_blocking=True),
        })

    payload = request.get_json(silent=True, force=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': '不正なJSONペイロードです'}), 400
    slot_id = str(payload.get('slot_id') or '').strip()
    target_url = str(payload.get('url') or '').strip()
    memo = str(payload.get('memo') or '').strip()
    days = _safe_clamped_days(payload.get('days'), default=7)

    res = affiliate_manager.set_affiliate_slot_override(slot_id=slot_id, target_url=target_url, memo=memo)
    if not res.get('ok'):
        return jsonify(res), 400
    core.clear_fast_http_caches()

    ana_data = analytics.summary(days=days, allow_blocking=False)
    status_now = affiliate_manager.get_affiliate_slots_status(analytics_data=ana_data, allow_blocking=False)
    return jsonify({
        **res,
        'aff_status': status_now,
    })


@staff_bp.route('/api/sheets/retention', methods=['GET', 'POST'])
def api_sheets_retention():
    """Master-only API to inspect Google Sheets 10M-cell capacity or run compression/rotation immediately."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import archive_data

    if request.method == 'GET':
        return jsonify(archive_data.get_retention_status(allow_blocking=True))

    payload = request.get_json(silent=True, force=True) or {}
    dry_run = bool(payload.get('dry_run', False))
    res = archive_data.run_full_sheets_retention(dry_run=dry_run)
    status_code = 200 if res.get('ok') else (409 if res.get('busy') else 500)
    return jsonify(res), status_code


@staff_bp.route('/api/data-audit')
def api_data_audit():
    """Master-only endpoint returning automated price & business hours drift audit report."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import data_audit
    with core.data_lock:
        cur_live = list(core.latest_data.get('full_data') or [])
    report = data_audit.run_data_audit(live_stores=cur_live, send_alerts=False)
    return jsonify(report)


@staff_bp.route('/staff/logout')
def staff_logout():
    """Clear staff and master authentication and return to staff login."""
    session.pop('is_staff', None)
    session.pop('is_master', None)
    return redirect(url_for('staff_monitor'))


@staff_bp.route('/staff/sns-editorial')
def staff_sns_editorial():
    """Master-only 4-role SNS Editorial Room board across X, Threads, and Instagram."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import sns_editorial
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    channel = (request.args.get('channel') or 'x').strip().lower()
    if channel not in ('x', 'threads', 'instagram', 'tiktok'):
        channel = 'x'
    slot_label = (request.args.get('slot') or '22:00').strip()
    if slot_label not in ('20:00', '22:00', '00:00', '02:00'):
        slot_label = '22:00'
    tone_mode = (request.args.get('tone') or '').strip().lower() or None
    live_s = _get_visible_live_stores()
    ig_ctx = core._get_sns_kit_context(fast=True) if channel == 'instagram' else None
    force_ai = (request.args.get('refresh_ai') == '1') and _is_same_origin_get_action_allowed()
    is_json_req = request.args.get('format') == 'json'
    editorial = sns_editorial.run_editorial_pipeline(
        live_s,
        slot_label=slot_label,
        channel=channel,
        ig_ctx=ig_ctx,
        record_history=False,
        use_ai=True,
        force_ai_refresh=force_ai,
        ai_cache_only=(not is_json_req),
        tone_mode=tone_mode,
    )
    if is_json_req:
        return jsonify(editorial)
    reel_auto_enabled = False
    tiktok_auto_enabled = False
    reel_studio = None
    moshimo_queue = None
    try:
        import instagram_poster
        import moshimo_reel_queue
        import reel_generator
        import tiktok_poster
        reel_auto_enabled = instagram_poster.is_reel_auto_schedule_enabled(allow_blocking=False)
        tiktok_auto_enabled = tiktok_poster.is_tiktok_auto_schedule_enabled(allow_blocking=False)
        init_rt = 'tiktok_gap_shock' if channel == 'tiktok' else 'live_top5'
        reel_studio = reel_generator.get_reel_studio_info(
            live_s,
            reel_type=init_rt,
            auto_schedule_enabled=reel_auto_enabled,
            tiktok_auto_schedule_enabled=tiktok_auto_enabled,
        )
        moshimo_queue = moshimo_reel_queue.get_moshimo_queue_status(allow_blocking=False)
    except Exception as _re_err:
        logging.debug(f"Reel studio init fallback: {_re_err}")
    return render_template(
        'staff_sns_editorial.html',
        editorial=editorial,
        slot_label=slot_label,
        channel=channel,
        tone_mode=editorial.get('tone_mode'),
        reel_auto_enabled=reel_auto_enabled,
        tiktok_auto_enabled=tiktok_auto_enabled,
        reel_studio=reel_studio,
        moshimo_queue=moshimo_queue,
    )


@staff_bp.route('/api/staff/sns-reel/info')
def api_staff_sns_reel_info():
    """Master-only JSON metadata for the Google AI Ultra 9:16 Short Video Studio (Reels / Shorts / TikTok)."""
    if not core.is_master_authorized():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    import instagram_poster
    import reel_generator
    import tiktok_poster
    core.restore_latest_data_if_empty()
    reel_type = reel_generator.normalize_reel_type(request.args.get('reel_type', 'live_top5'))
    live_s = _get_visible_live_stores()
    auto_on = instagram_poster.is_reel_auto_schedule_enabled(allow_blocking=False)
    tk_auto_on = tiktok_poster.is_tiktok_auto_schedule_enabled(allow_blocking=False)
    return jsonify(reel_generator.get_reel_studio_info(
        live_s,
        reel_type=reel_type,
        auto_schedule_enabled=auto_on,
        tiktok_auto_schedule_enabled=tk_auto_on,
    ))


@staff_bp.route('/api/staff/sns-reel/settings', methods=['POST'])
def api_staff_sns_reel_settings():
    """Master-only endpoint to toggle scheduled Instagram Reels auto-posting."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import instagram_poster
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get('auto_schedule_enabled'))
    saved = instagram_poster.set_reel_auto_schedule_enabled(enabled)
    return jsonify({
        'ok': True,
        'auto_schedule_enabled': saved,
        'message': 'Instagramリールの定期自動投稿をONにしました！' if saved else 'Instagramリールの定期自動投稿をOFF（手動1クリック運用）に設定しました。',
    })


@staff_bp.route('/api/staff/tiktok/settings', methods=['POST'])
def api_staff_tiktok_settings():
    """Master-only endpoint to toggle scheduled Wed/Fri/Sat TikTok Inbox auto-delivery."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import tiktok_poster
    payload = request.get_json(silent=True) or {}
    enabled = bool(payload.get('auto_schedule_enabled'))
    saved = tiktok_poster.set_tiktok_auto_schedule_enabled(enabled)
    return jsonify({
        'ok': True,
        'auto_schedule_enabled': saved,
        'message': 'TikTokアプリの定期Inbox下書き送信（水・金・土）をONにしました！' if saved else 'TikTokアプリの定期Inbox下書き送信をOFF（手動1クリック運用）に設定しました。',
    })


@staff_bp.route('/api/staff/sns-reel/publish', methods=['POST'])
def api_staff_sns_reel_publish():
    """Master-only 1-click trigger to render and publish a 9:16 vertical Reel to Instagram."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import instagram_poster
    import reel_generator
    core.restore_latest_data_if_empty()
    payload = request.get_json(silent=True) or {}
    reel_type = reel_generator.normalize_reel_type(payload.get('reel_type', 'live_top5'))
    custom_caption = (payload.get('caption') or '').strip()
    custom_narration = (payload.get('narration_text') or '').strip()
    share_to_feed = bool(payload.get('share_to_feed', True))
    if request.args.get('async') == '1' or payload.get('async') is True:
        worker_only = bool(payload.get('worker_only')) or (request.args.get('worker_only') == '1')
        job_rec = core._enqueue_sns_job_and_maybe_dispatch('ig_reel', {
            'reel_type': reel_type,
            'caption': custom_caption,
            'narration_text': custom_narration,
            'share_to_feed': share_to_feed,
            'force': True,
        }, worker_only=worker_only)
        return jsonify({
            'ok': True,
            'queued': True,
            'job': job_rec,
            'job_id': job_rec['job_id'],
            'poll_url': f"/api/sns-jobs/{job_rec['job_id']}",
        }), 202
    live_s = _get_visible_live_stores()
    res = instagram_poster.publish_live_reel_to_instagram(
        live_s,
        reel_type=reel_type,
        custom_caption=custom_caption,
        share_to_feed=share_to_feed,
        slot_label='manual_reel',
        force=True,
        custom_narration_text=custom_narration,
    )
    ok_flag = res.get('status') == 'success'
    return jsonify({'ok': ok_flag, **res}), (200 if ok_flag else 400)


@staff_bp.route('/api/staff/tiktok/publish-inbox', methods=['POST'])
def api_staff_tiktok_publish_inbox():
    """Master-only 1-click trigger to render and push a 9:16 short video to the TikTok mobile app Inbox (or return Phase 1 ready info)."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import reel_generator
    import tiktok_poster
    core.restore_latest_data_if_empty()
    payload = request.get_json(silent=True) or {}
    reel_type = reel_generator.normalize_reel_type(payload.get('reel_type', 'tiktok_gap_shock'))
    custom_caption = (payload.get('caption') or '').strip()
    custom_narration = (payload.get('narration_text') or '').strip()
    if request.args.get('async') == '1' or payload.get('async') is True:
        worker_only = bool(payload.get('worker_only')) or (request.args.get('worker_only') == '1')
        job_rec = core._enqueue_sns_job_and_maybe_dispatch('tiktok_inbox', {
            'reel_type': reel_type,
            'caption': custom_caption,
            'narration_text': custom_narration,
            'force': True,
        }, worker_only=worker_only)
        return jsonify({
            'ok': True,
            'queued': True,
            'job': job_rec,
            'job_id': job_rec['job_id'],
            'poll_url': f"/api/sns-jobs/{job_rec['job_id']}",
        }), 202
    live_s = _get_visible_live_stores()
    res = tiktok_poster.publish_live_reel_to_tiktok_inbox(
        live_s,
        reel_type=reel_type,
        custom_caption=custom_caption,
        slot_label='manual_tiktok',
        force=True,
        custom_narration_text=custom_narration,
    )
    st = res.get('status')
    ok_flag = (st == 'success')
    http_code = 200 if st in ('success', 'phase1_manual', 'skipped') else 400
    return jsonify({'ok': ok_flag, **res}), http_code


@staff_bp.route('/api/staff/voice-narration/status')
def api_staff_voice_narration_status():
    """Master-only JSON status & current factual narration script for ElevenLabs custom voice cloning."""
    if not core.is_master_authorized():
        return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
    import reel_generator
    import voice_narration
    core.restore_latest_data_if_empty()
    rt = reel_generator.normalize_reel_type(request.args.get('reel_type', 'live_top5'))
    live_s = _get_visible_live_stores()
    ctx = reel_generator.build_reel_context(live_s, reel_type=rt)
    return jsonify({
        'ok': True,
        'reel_type': rt,
        'narration_script': ctx.get('narration_script') or '',
        'narration_phonetic': ctx.get('narration_phonetic') or '',
        'voice_narration': voice_narration.get_voice_narration_status(allow_blocking=True),
    })


@staff_bp.route('/api/staff/voice-narration/config', methods=['POST'])
def api_staff_voice_narration_config():
    """Master-only endpoint to save/clear ElevenLabs API key, Voice ID, model, and ON/OFF toggle."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import reel_generator
    import voice_narration
    payload = request.get_json(silent=True) or {}
    action = (payload.get('action') or 'save').strip().lower()

    try:
        if action == 'clear_key':
            st = voice_narration.set_voice_narration_config(clear_api_key=True)
            reel_generator.clear_reel_mp4_caches()
            return jsonify({
                'ok': True,
                'message': '登録済みのElevenLabs APIキーを削除しました。',
                'voice_narration': st,
            })

        api_key = payload.get('api_key')
        if api_key is not None:
            api_key = str(api_key).strip()
            if api_key and len(api_key) < 12:
                return jsonify({'ok': False, 'error': '有効なElevenLabs APIキーを入力してください（12文字以上）。'}), 400
            if not api_key:
                api_key = None

        enabled_val = payload.get('enabled')
        if enabled_val is not None:
            enabled_val = bool(enabled_val)

        st = voice_narration.set_voice_narration_config(
            api_key=api_key,
            voice_id=payload.get('voice_id'),
            voice_name=payload.get('voice_name'),
            enabled=enabled_val,
            model_id=payload.get('model_id'),
        )
        reel_generator.clear_reel_mp4_caches()
        if st.get('active'):
            msg = '自分の声AIナレーション設定を保存し、自動合成を「稼働中」に切り替えました。'
        elif not st.get('enabled'):
            msg = '自分の声AIナレーションを「OFF（BGMのみ）」に保存しました。'
        else:
            msg = '設定を保存しました（APIキーとVoice IDの両方が揃うと自動合成が有効になります）。'
        return jsonify({
            'ok': True,
            'message': msg,
            'voice_narration': st,
        })
    except ValueError as ve:
        return jsonify({'ok': False, 'error': str(ve)}), 400


@staff_bp.route('/api/staff/voice-narration/voices', methods=['GET', 'POST'])
def api_staff_voice_narration_voices():
    """Master-only helper to list cloned/available voices from the user's ElevenLabs account."""
    if request.method == 'POST':
        guard = _check_master_api_csrf()
        if guard is not None:
            return guard
        payload = request.get_json(silent=True) or {}
        temp_key = (payload.get('api_key') or '').strip() or None
    else:
        if not core.is_master_authorized():
            return jsonify({'ok': False, 'error': 'Unauthorized'}), 401
        temp_key = None

    import voice_narration
    res = voice_narration.list_elevenlabs_voices(api_key=temp_key)
    return jsonify(res), (200 if res.get('ok') else 400)


@staff_bp.route('/api/staff/voice-narration/test', methods=['POST'])
def api_staff_voice_narration_test():
    """Master-only 1-click voice audition returning base64 MP3 data URL + phonetic preview."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import base64
    import reel_generator
    import voice_narration
    core.restore_latest_data_if_empty()
    payload = request.get_json(silent=True) or {}
    rt = reel_generator.normalize_reel_type(payload.get('reel_type', 'live_top5'))
    raw_text = (payload.get('text') or '').strip()
    if not raw_text:
        live_s = _get_visible_live_stores()
        ctx = reel_generator.build_reel_context(live_s, reel_type=rt)
        raw_text = ctx.get('narration_script') or ''
    temp_key = (payload.get('api_key') or '').strip() or None
    temp_vid = (payload.get('voice_id') or '').strip() or None
    temp_model = (payload.get('model_id') or '').strip() or None

    res = voice_narration.synthesize_narration_audio(
        raw_text,
        voice_id=temp_vid,
        api_key=temp_key,
        model_id=temp_model,
    )
    if not res.get('ok') or not res.get('audio_bytes'):
        return jsonify({
            'ok': False,
            'error': res.get('error') or '音声合成に失敗しました。',
            'script_text': res.get('script_text') or raw_text,
            'phonetic_text': res.get('phonetic_text') or voice_narration.to_tts_phonetic_japanese(raw_text),
        }), 400

    b64_mp3 = base64.b64encode(res['audio_bytes']).decode('ascii')
    return jsonify({
        'ok': True,
        'cached': bool(res.get('cached')),
        'duration_sec': float(res.get('duration_sec') or 0.0),
        'script_text': res.get('script_text') or raw_text,
        'phonetic_text': res.get('phonetic_text') or '',
        'audio_data_url': f"data:audio/mpeg;base64,{b64_mp3}",
    })


@staff_bp.route('/api/staff/voice-narration/phonetic', methods=['POST'])
def api_staff_voice_narration_phonetic():
    """Master-only instant phonetic preview endpoint (zero ElevenLabs credit usage)."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import voice_narration
    payload = request.get_json(silent=True) or {}
    raw_text = (payload.get('text') or '').strip()
    clean_script = voice_narration.normalize_narration_script_input(raw_text)
    return jsonify({
        'ok': True,
        'script_text': clean_script,
        'phonetic_text': voice_narration.to_tts_phonetic_japanese(clean_script),
    })


@staff_bp.route('/api/staff/voice-narration/pin-preview', methods=['POST'])
def api_staff_voice_narration_pin_preview():
    """Master-only endpoint to render & pin a snapshot-locked narrated 9:16 MP4 slot for `<video>` preview & download."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import time as _time
    import reel_generator
    import voice_narration
    core.restore_latest_data_if_empty()
    payload = request.get_json(silent=True) or {}
    rt = reel_generator.normalize_reel_type(payload.get('reel_type', 'live_top5'))
    narration_text = (payload.get('narration_text') or '').strip()
    voice_on = payload.get('voice_on')
    if voice_on is not None:
        voice_on = bool(voice_on)

    live_s = _get_visible_live_stores()
    try:
        ctx = reel_generator.build_reel_context(live_s, reel_type=rt)
        eff_script = (
            voice_narration.normalize_narration_script_input(narration_text)
            or ctx.get('narration_script')
            or ''
        )
        mp4_bytes = reel_generator.render_reel_mp4_bytes(
            live_s,
            reel_type=rt,
            force_refresh=True,
            narration_override=voice_on,
            custom_narration_text=eff_script or None,
        )
        tok = str(int(_time.time() * 1000))
        slot_id = f"prev_{tok}"
        reel_generator.pin_shared_reel_mp4_slot(rt, slot_id, mp4_bytes, ttl_sec=600)
        return jsonify({
            'ok': True,
            'reel_type': rt,
            'preview_token': tok,
            'mp4_url': f"/ogp/reel/{rt}.mp4?s={slot_id}&t={tok}",
            'jpg_url': f"/ogp/reel/{rt}.jpg?refresh=1&t={tok}",
            'download_url': f"/ogp/reel/{rt}.mp4?s={slot_id}&t={tok}&download=1",
            'narration_script': eff_script,
            'phonetic_text': voice_narration.to_tts_phonetic_japanese(eff_script) if eff_script else '',
            'caption': ctx.get('caption') or '',
            'voice_active': voice_narration.is_voice_narration_active(allow_blocking=False) if voice_on is None else bool(voice_on),
        })
    except Exception as exc:
        return jsonify({'ok': False, 'error': f"動画レンダリングエラー: {exc}"}), 503


@staff_bp.route('/api/sns-editorial')
def api_sns_editorial():
    """Master-only JSON API returning the 4-role SNS editorial proposals and scores for X, Threads, or Instagram."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import sns_editorial
    core.restore_latest_data_if_empty()
    channel = (request.args.get('channel') or 'x').strip().lower()
    if channel not in ('x', 'threads', 'instagram', 'tiktok'):
        channel = 'x'
    slot_label = (request.args.get('slot') or '22:00').strip()
    tone_mode = (request.args.get('tone') or '').strip().lower() or None
    force_ai = (request.args.get('refresh_ai') == '1') and _is_same_origin_get_action_allowed()
    cache_only = (request.args.get('cache_only') == '1') and not force_ai
    live_s = _get_visible_live_stores()
    ig_ctx = core._get_sns_kit_context(fast=True) if channel == 'instagram' else None
    return jsonify(sns_editorial.run_editorial_pipeline(
        live_s,
        slot_label=slot_label,
        channel=channel,
        ig_ctx=ig_ctx,
        record_history=False,
        use_ai=True,
        force_ai_refresh=force_ai,
        ai_cache_only=cache_only,
        tone_mode=tone_mode,
    ))


@staff_bp.route('/api/editorial/tone-mode', methods=['POST'])
def api_editorial_tone_mode():
    """Master-only endpoint to persist the default writing tone & perspective across SNS and Column editorial rooms."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import sns_editorial
    import column_editorial
    payload = request.get_json(silent=True) or {}
    raw_tm = (payload.get('tone_mode') or '').strip().lower()
    norm_tm = sns_editorial.normalize_tone_mode(raw_tm)
    if not norm_tm:
        return jsonify({'ok': False, 'error': '有効な執筆トーン（spicy_female / spicy / female_real / standard）を指定してください。'}), 400
    saved_tm = sns_editorial.set_default_editorial_tone_mode(norm_tm)
    column_editorial.clear_column_ai_cache()
    core.clear_fast_http_caches()
    meta = sns_editorial.EDITORIAL_TONE_MODES[saved_tm]
    return jsonify({
        'ok': True,
        'tone_mode': saved_tm,
        'label': meta['label'],
        'short_label': meta['short_label'],
        'message': f"自動投稿＆編集部の標準トーンを『{meta['label']}』に保存しました！",
    })


@staff_bp.route('/api/sns-editorial/gemini-key', methods=['POST'])
@staff_bp.route('/api/sns-editorial/ai-config', methods=['POST'])
def api_sns_editorial_gemini_key():
    """Master-only endpoint to securely store/clear Gemini & Claude API keys and switch AI engine preference."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import sns_editorial
    payload = request.get_json(silent=True) or {}
    action = (payload.get('action') or 'save').strip().lower()
    provider = (payload.get('provider') or 'auto').strip().lower()

    def _flush_editorial_caches():
        try:
            import column_editorial
            column_editorial.clear_column_ai_cache()
        except Exception:
            pass
        core.clear_fast_http_caches()

    if action == 'set_pref':
        saved_pref = sns_editorial.set_ai_engine_pref(payload.get('engine_pref') or 'auto')
        _flush_editorial_caches()
        pref_labels = {
            'auto': '自動ハイブリッド（Claude登録時はClaude Opus×2段階推敲優先・未登録時は上位Gemini Pro/Flash）',
            'claude': 'Claude Opus × 2段階推敲 最優先（障害時はGeminiへ自動退避）',
            'gemini': '上位 Gemini Pro / Flash 最優先（障害時はClaudeへ自動退避）',
        }
        return jsonify({
            'ok': True,
            'engine_pref': saved_pref,
            'message': f"優先AIエンジンを『{pref_labels.get(saved_pref, saved_pref)}』に切り替えました！",
            'gemini_status': sns_editorial.get_gemini_status(),
        })

    if action == 'clear':
        if provider == 'claude':
            sns_editorial.set_claude_api_key('')
            msg = '登録済みのClaude APIキーを削除しました。'
        else:
            sns_editorial.set_gemini_api_key('')
            msg = '登録済みのGemini APIキーを削除しました（Render環境変数がある場合はそちらが使用されます）。'
        _flush_editorial_caches()
        return jsonify({
            'ok': True,
            'message': msg,
            'gemini_status': sns_editorial.get_gemini_status(),
        })

    api_key = (payload.get('api_key') or '').strip()
    if not api_key or len(api_key) < 16:
        return jsonify({'ok': False, 'error': '有効なAPIキーを入力してください（AIza... / AQ.... または sk-ant-...）'}), 400

    is_claude_key = api_key.startswith('sk-ant-') or (provider == 'claude' and not api_key.startswith(('AIza', 'AQ.')))
    if is_claude_key:
        sns_editorial.set_claude_api_key(api_key)
        saved_msg = 'Claude Opus（2段階推敲）APIキーを暗号化保存しました！人間味重視のメイン執筆エンジンとして即座に稼働します。'
    else:
        sns_editorial.set_gemini_api_key(api_key)
        saved_msg = 'Gemini APIキーを暗号化保存しました！上位モデル（Gemini Pro / Flash）での本音執筆が有効になります。'

    _flush_editorial_caches()
    return jsonify({
        'ok': True,
        'provider': 'claude' if is_claude_key else 'gemini',
        'message': saved_msg,
        'gemini_status': sns_editorial.get_gemini_status(),
    })


@staff_bp.route('/api/sns-editorial/platform-token', methods=['POST'])
def api_sns_editorial_platform_token():
    """Master-only endpoint to verify, exchange (short->60d long-lived), save, or clear Threads / Instagram / TikTok tokens without redeploying."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import sns_editorial
    import threads_poster
    import instagram_poster
    import tiktok_poster

    payload = request.get_json(silent=True) or {}
    ch = (payload.get('channel') or 'threads').strip().lower()
    if ch not in ('threads', 'instagram', 'tiktok'):
        return jsonify({'ok': False, 'error': 'channel は threads / instagram / tiktok を指定してください。'}), 400

    action = (payload.get('action') or 'save').strip().lower()
    if action == 'clear':
        if ch == 'threads':
            res = threads_poster.save_and_prepare_threads_token('')
        elif ch == 'tiktok':
            res = tiktok_poster.save_and_prepare_tiktok_token('')
        else:
            res = instagram_poster.save_and_prepare_ig_token('')
        core.clear_fast_http_caches()
        return jsonify({
            **res,
            'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
        })

    if action == 'verify':
        if ch == 'threads':
            ver = threads_poster.verify_threads_token()
        elif ch == 'tiktok':
            ver = tiktok_poster.verify_tiktok_token()
        else:
            ver = instagram_poster.verify_ig_token()
        if ver.get('ok'):
            sns_editorial.record_sns_delivery_status(ch, 'token_refreshed', detail='verified')
            uname = ver.get('username') or ver.get('id') or ch
            return jsonify({
                'ok': True,
                'verify': ver,
                'message': f"疎通確認OK！ @{uname} と正常に接続されています（投稿は行っていません）。",
                'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
            })
        if ch == 'tiktok' and ver.get('status') == 'unconfigured':
            return jsonify({
                'ok': True,
                'verify': ver,
                'message': "🔵 TikTok は現在 Phase 1 稼働中（MP4 1タップ保存＋台本コピー運用）です。APIトークンを登録するとアプリInbox下書き送信（Phase 2）へ即移行できます。",
                'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
            })
        err_msg = ver.get('error') or 'トークンが無効または未設定です'
        if ver.get('status') == 'error':
            sns_editorial.record_sns_delivery_status(ch, 'token_error', detail=err_msg)
        return jsonify({
            'ok': False,
            'verify': ver,
            'error': f"疎通エラー：{err_msg}",
            'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
        }), 400

    raw_token = (payload.get('access_token') or '').strip()
    app_secret = (payload.get('app_secret') or '').strip()
    if not raw_token or len(raw_token) < 20:
        return jsonify({'ok': False, 'error': '有効なアクセストークンを貼り付けてください。'}), 400

    if ch == 'threads':
        res = threads_poster.save_and_prepare_threads_token(raw_token, app_secret=app_secret)
    elif ch == 'tiktok':
        res = tiktok_poster.save_and_prepare_tiktok_token(raw_token, app_secret=app_secret)
    else:
        res = instagram_poster.save_and_prepare_ig_token(raw_token, app_secret=app_secret)
    core.clear_fast_http_caches()
    status_code = 200 if res.get('ok') else 400
    return jsonify({
        **res,
        'delivery_health': sns_editorial.get_sns_delivery_health(allow_blocking=True),
    }), status_code


@staff_bp.route('/staff/column-editorial')
def staff_column_editorial():
    """Master-only 4-role Column & note.com Editorial Room board."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import column_editorial
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    theme_key = (request.args.get('theme') or 'area_hopping').strip().lower()
    custom_topic = (request.args.get('topic') or '').strip()
    tone_mode = (request.args.get('tone') or '').strip().lower() or None
    export_slug = (request.args.get('export') or '').strip()
    force_ai = (request.args.get('refresh_ai') == '1') and _is_same_origin_get_action_allowed()
    is_json_req = request.args.get('format') == 'json'
    live_s = _get_visible_live_stores()
    editorial = column_editorial.run_column_editorial_pipeline(
        live_s,
        theme_key=theme_key,
        custom_topic=custom_topic,
        use_ai=True,
        force_ai_refresh=force_ai,
        ai_cache_only=(not is_json_req),
        tone_mode=tone_mode,
    )
    if is_json_req:
        return jsonify(editorial)
    return render_template(
        'staff_column_editorial.html',
        editorial=editorial,
        custom_topic=custom_topic,
        tone_mode=editorial.get('tone_mode'),
        export_slug=export_slug,
    )


@staff_bp.route('/api/column-editorial')
def api_column_editorial():
    """Master-only JSON API returning 4-role column & note proposals, scores, and audit table."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import column_editorial
    core.restore_latest_data_if_empty()
    theme_key = (request.args.get('theme') or 'area_hopping').strip().lower()
    custom_topic = (request.args.get('topic') or '').strip()
    tone_mode = (request.args.get('tone') or '').strip().lower() or None
    force_ai = (request.args.get('refresh_ai') == '1') and _is_same_origin_get_action_allowed()
    cache_only = (request.args.get('cache_only') == '1') and not force_ai
    live_s = _get_visible_live_stores()
    return jsonify(column_editorial.run_column_editorial_pipeline(
        live_stores=live_s,
        theme_key=theme_key,
        custom_topic=custom_topic,
        use_ai=True,
        force_ai_refresh=force_ai,
        ai_cache_only=cache_only,
        tone_mode=tone_mode,
    ))


@staff_bp.route('/api/column-editorial/publish', methods=['POST'])
def api_column_editorial_publish():
    """Master-only endpoint to schedule or immediately publish an editorial column to the live site."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import column_editorial
    payload = request.get_json(silent=True) or {}
    candidate = payload.get('candidate') or {}
    if not isinstance(candidate, dict):
        return jsonify({'ok': False, 'error': '有効な記事データがありません。'}), 400
    publish_mode = (payload.get('publish_mode') or 'schedule').strip().lower()
    publish_at_str = (payload.get('publish_at') or '').strip()
    custom_slug = (payload.get('slug') or '').strip()
    eyecatch_key = (payload.get('eyecatch_key') or '').strip()
    res = column_editorial.save_editorial_column(
        candidate=candidate,
        publish_mode=publish_mode,
        publish_at_str=publish_at_str,
        custom_slug=custom_slug,
        eyecatch_key=eyecatch_key,
    )
    if not res.get('ok'):
        return jsonify(res), 400
    core.clear_fast_http_caches()
    return jsonify(res)


@staff_bp.route('/api/column-editorial/delete', methods=['POST'])
def api_column_editorial_delete():
    """Master-only endpoint to delete a dynamically registered editorial column."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import column_editorial
    payload = request.get_json(silent=True) or {}
    slug = (payload.get('slug') or '').strip()
    res = column_editorial.delete_editorial_column(slug)
    if not res.get('ok'):
        return jsonify(res), 400
    core.clear_fast_http_caches()
    return jsonify(res)


@staff_bp.route('/api/column-editorial/export-note')
def api_column_editorial_export_note():
    """Master-only endpoint converting any existing/scheduled/dynamic column into ready-to-paste note.com format."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import column_editorial
    slug = (request.args.get('slug') or '').strip()
    mode = (request.args.get('mode') or 'essay').strip().lower()
    res = column_editorial.export_column_for_note(slug, site_url='https://aiseki-navi.com', mode=mode)
    if not res.get('ok'):
        return jsonify(res), 404
    return jsonify(res)


@staff_bp.route('/staff/pdca')
def staff_pdca():
    """Master-only Autonomous Closed-Loop PDCA Strategist board."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import pdca_engine
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    days = max(1, min(90, request.args.get('days', 14, type=int)))
    is_json_req = request.args.get('format') == 'json'
    allow_get_side_effect = _is_same_origin_get_action_allowed()
    live_s = _get_visible_live_stores()
    has_existing = bool(pdca_engine.get_active_directives(allow_blocking=True))
    if not has_existing and not is_json_req and allow_get_side_effect:
        def _seed_pdca_async(stores_snap):
            try:
                if not pdca_engine.get_active_directives(allow_blocking=True):
                    pdca_engine.run_pdca_cycle(
                        live_stores=stores_snap,
                        days=days,
                        apply_actions=True,
                        use_ai=False,
                        record_log=True,
                    )
            except Exception:
                pass
        threading.Thread(target=_seed_pdca_async, args=(live_s,), daemon=True).start()
    pdca_data = pdca_engine.run_pdca_cycle(
        live_stores=live_s,
        days=days,
        apply_actions=(not has_existing and is_json_req and allow_get_side_effect),
        use_ai=True,
        ai_cache_only=(not (is_json_req and allow_get_side_effect)),
        record_log=(not has_existing and is_json_req and allow_get_side_effect),
    )
    if is_json_req:
        return jsonify(pdca_data)
    import youtube_analytics_sync
    yt_oauth = youtube_analytics_sync.get_youtube_sync_status(base_url=request.url_root, allow_blocking=True)
    return render_template('staff_pdca.html', pdca=pdca_data, days=days, yt_oauth=yt_oauth)


@staff_bp.route('/api/pdca')
def api_pdca():
    """Master-only JSON API returning PDCA diagnostics, active directives, and AI Chief Strategist briefing."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import pdca_engine
    core.restore_latest_data_if_empty()
    days = max(1, min(90, request.args.get('days', 14, type=int)))
    force_ai = (request.args.get('refresh_ai') == '1') and _is_same_origin_get_action_allowed()
    cache_only = (request.args.get('cache_only') == '1') and not force_ai
    live_s = _get_visible_live_stores()
    return jsonify(pdca_engine.run_pdca_cycle(
        live_stores=live_s,
        days=days,
        apply_actions=False,
        use_ai=True,
        ai_cache_only=cache_only,
        force_ai_refresh=force_ai,
        record_log=False,
    ))


@staff_bp.route('/api/pdca/settings', methods=['POST'])
def api_pdca_settings():
    """Master-only endpoint to toggle autonomous SNS optimization and low-stock column auto-replenishment."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import pdca_engine
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': '不正なJSONペイロードです'}), 400
    auto_sns = payload.get('auto_optimize_sns') if 'auto_optimize_sns' in payload else None
    auto_col = payload.get('auto_replenish_columns') if 'auto_replenish_columns' in payload else None
    updated = pdca_engine.update_pdca_settings(
        auto_optimize_sns=auto_sns,
        auto_replenish_columns=auto_col,
    )
    return jsonify({
        'ok': True,
        'settings': updated,
        'active_directives': pdca_engine.get_active_directives(allow_blocking=True),
    })


@staff_bp.route('/api/pdca/run', methods=['POST'])
def api_pdca_run():
    """Master-only manual trigger to execute the full PDCA cycle immediately and update editorial directives."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import pdca_engine
    core.restore_latest_data_if_empty()
    payload = request.get_json(silent=True) or {}
    if not isinstance(payload, dict):
        return jsonify({'ok': False, 'error': '不正なJSONペイロードです'}), 400
    days = _safe_clamped_days(payload.get('days'), default=14)
    live_s = _get_visible_live_stores()
    res = pdca_engine.run_pdca_cycle(
        live_stores=live_s,
        days=days,
        apply_actions=True,
        use_ai=True,
        ai_cache_only=False,
        force_ai_refresh=bool(payload.get('refresh_ai')),
        record_log=True,
    )
    core.clear_fast_http_caches()
    return jsonify({'ok': True, 'pdca': res})


@staff_bp.route('/api/pdca/sns-insights')
def api_pdca_sns_insights():
    """Master-only JSON endpoint returning 3-layer closed-loop SNS insights, Reel bandit, and note CTR feedback."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    import sns_insights_loop
    days = _safe_clamped_days(request.args.get('days'), default=28)
    return jsonify(sns_insights_loop.get_insights_dashboard_payload(days=days, allow_blocking=True))


@staff_bp.route('/api/pdca/sns-insights/sync', methods=['POST'])
def api_pdca_sns_insights_sync():
    """Master-only endpoint to trigger immediate Meta Graph API (Threads + IG Reels) & YouTube Analytics API insight harvest & PDCA directive refresh."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import pdca_engine
    import sns_insights_loop
    core.restore_latest_data_if_empty()
    sync_res = sns_insights_loop.sync_all_sns_insights(limit=15)
    live_s = _get_visible_live_stores()
    pdca_res = pdca_engine.run_pdca_cycle(
        live_stores=live_s,
        days=14,
        apply_actions=True,
        use_ai=False,
        record_log=False,
    )
    core.clear_fast_http_caches()
    return jsonify({
        'ok': True,
        'sync': sync_res,
        'active_directives': pdca_res.get('active_directives') or {},
        'closed_loop_feedback': pdca_res.get('closed_loop_feedback') or sync_res.get('feedback') or {},
    })


_MAX_OAUTH_JSON_BYTES = 256 * 1024
_OAUTH_STATE_TTL_SEC = 900
_recent_oauth_codes = {}  # code_sha24 -> (unix_ts, synced_cnt)
_oauth_cb_lock = threading.Lock()


@staff_bp.route('/api/pdca/youtube/config', methods=['GET', 'POST'])
def api_pdca_youtube_config():
    """Master-only endpoint to inspect or configure Read-Only Google OAuth2 client credentials for YouTube Shorts Auto-Sync."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import youtube_analytics_sync

    if request.method == 'GET':
        return jsonify({
            'ok': True,
            'youtube_oauth': youtube_analytics_sync.get_youtube_sync_status(
                base_url=request.url_root, allow_blocking=True
            ),
        })

    wants_json = 'application/json' in (request.headers.get('Accept') or '').lower()
    client_json_raw = ''
    client_id = ''
    client_secret = ''

    if request.files and ('client_json_file' in request.files or 'file' in request.files):
        f_obj = request.files.get('client_json_file') or request.files.get('file')
        if f_obj and f_obj.filename:
            raw_bytes = f_obj.read(_MAX_OAUTH_JSON_BYTES + 1024)
            if len(raw_bytes) > _MAX_OAUTH_JSON_BYTES:
                err_msg = 'JSONファイルサイズは256KB以内にしてください'
                if wants_json:
                    return jsonify({'ok': False, 'error': err_msg}), 400
                return redirect(f"/staff/pdca?yt_error={quote(err_msg)}#yt-oauth-setup")
            client_json_raw = raw_bytes.decode('utf-8-sig', errors='replace')

    if request.form:
        client_id = (request.form.get('client_id') or '').strip()
        client_secret = (request.form.get('client_secret') or '').strip()
    else:
        payload = request.get_json(silent=True) or {}
        if isinstance(payload, dict):
            client_json_raw = client_json_raw or str(payload.get('client_json') or '').strip()
            client_id = client_id or str(payload.get('client_id') or '').strip()
            client_secret = client_secret or str(payload.get('client_secret') or '').strip()

    cid, csec, parse_err = youtube_analytics_sync.parse_oauth_client_credentials(
        client_json_raw=client_json_raw,
        client_id=client_id,
        client_secret=client_secret,
        allow_stored_secret_fallback=True,
    )
    if parse_err:
        if wants_json:
            return jsonify({'ok': False, 'error': parse_err}), 400
        return redirect(f"/staff/pdca?yt_error={quote(parse_err)}#yt-oauth-setup")

    save_res = youtube_analytics_sync.save_oauth_client_credentials(cid, csec)
    if not save_res.get('ok'):
        err_msg = str(save_res.get('error') or '設定の保存に失敗しました')
        if wants_json:
            return jsonify({'ok': False, 'error': err_msg}), 400
        return redirect(f"/staff/pdca?yt_error={quote(err_msg)}#yt-oauth-setup")

    state_tok = secrets.token_urlsafe(24)
    session['yt_oauth_state'] = state_tok
    session['yt_oauth_state_ts'] = int(time.time())
    redirect_uri = youtube_analytics_sync.resolve_oauth_redirect_uri(base_url=request.url_root)
    auth_url, url_err = youtube_analytics_sync.build_oauth_authorize_url(redirect_uri, state_tok)
    if url_err or not auth_url:
        err_msg = url_err or 'Google認可URLの生成に失敗しました'
        if wants_json:
            return jsonify({'ok': False, 'error': err_msg}), 400
        return redirect(f"/staff/pdca?yt_error={quote(err_msg)}#yt-oauth-setup")

    if wants_json:
        return jsonify({
            'ok': True,
            'authorize_url': auth_url,
            'youtube_oauth': youtube_analytics_sync.get_youtube_sync_status(
                base_url=request.url_root, allow_blocking=True
            ),
        })
    return redirect(auth_url)


@staff_bp.route('/staff/youtube/oauth/start')
def staff_youtube_oauth_start():
    """Master-only redirect to Google OAuth2 consent screen (prompts account/Brand Account selection)."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    if not _is_same_origin_get_action_allowed():
        return redirect('/staff/pdca?yt_error=' + quote('同一オリジンから操作してください') + '#yt-oauth-setup')
    import youtube_analytics_sync

    state_tok = secrets.token_urlsafe(24)
    session['yt_oauth_state'] = state_tok
    session['yt_oauth_state_ts'] = int(time.time())
    redirect_uri = youtube_analytics_sync.resolve_oauth_redirect_uri(base_url=request.url_root)
    auth_url, err_msg = youtube_analytics_sync.build_oauth_authorize_url(redirect_uri, state_tok)
    if err_msg or not auth_url:
        return redirect('/staff/pdca?yt_error=' + quote(err_msg or 'クライアントID・シークレットを設定してください') + '#yt-oauth-setup')
    return redirect(auth_url)


@staff_bp.route('/staff/youtube/oauth/callback')
def staff_youtube_oauth_callback():
    """Master-only Google OAuth2 callback verifying cryptographic state and executing initial YouTube Shorts sync."""
    # Never consume one-time OAuth authorization `code` or session `state` on Chromium Service-Worker Navigation Preload
    if (
        request.headers.get('Service-Worker-Navigation-Preload')
        or 'prefetch' in (request.headers.get('Sec-Purpose') or '').lower()
        or (request.headers.get('Purpose') or '').lower() == 'prefetch'
    ):
        return Response('', status=204, headers={'Cache-Control': 'private, no-store'})

    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import pdca_engine
    import sns_insights_loop
    import youtube_analytics_sync

    oauth_err = (request.args.get('error') or '').strip()
    if oauth_err:
        session.pop('yt_oauth_state', None)
        session.pop('yt_oauth_state_ts', None)
        msg = 'Google認証がキャンセルまたは拒否されました（' + oauth_err[:40] + '）'
        return redirect('/staff/pdca?yt_error=' + quote(msg) + '#yt-oauth-setup')

    code = (request.args.get('code') or '').strip()
    code_sig = hashlib.sha256(code.encode('utf-8')).hexdigest()[:24] if code else ''

    with _oauth_cb_lock:
        now_ts = time.time()
        for k_old, (t_old, _) in list(_recent_oauth_codes.items()):
            if now_ts - t_old > 120:
                _recent_oauth_codes.pop(k_old, None)
        if code_sig and code_sig in _recent_oauth_codes:
            prev_cnt = _recent_oauth_codes[code_sig][1]
            return redirect(f"/staff/pdca?yt_connected=1&yt_synced={prev_cnt}#pdca-closed-loop")

        expected_state = str(session.pop('yt_oauth_state', '') or '')
        state_ts = int(session.pop('yt_oauth_state_ts', 0) or 0)
        got_state = str(request.args.get('state') or '')
        if (
            not expected_state
            or not got_state
            or (now_ts - state_ts > _OAUTH_STATE_TTL_SEC)
            or not hmac.compare_digest(expected_state, got_state)
        ):
            return redirect(
                '/staff/pdca?yt_error='
                + quote('OAuthセッション検証（state）に失敗しました。もう一度連携ボタンを押してください。')
                + '#yt-oauth-setup'
            )

        redirect_uri = youtube_analytics_sync.resolve_oauth_redirect_uri(base_url=request.url_root)
        ex_res = youtube_analytics_sync.exchange_code_and_save(code=code, redirect_uri=redirect_uri)
        if not ex_res.get('ok'):
            err_msg = str(ex_res.get('error') or 'YouTube連携に失敗しました')
            # Multi-machine duplicate callback fallback: if the sibling Fly machine already redeemed this code moments ago
            if 'invalid_grant' in err_msg:
                cur_st = youtube_analytics_sync.get_youtube_sync_status(allow_blocking=True)
                if cur_st.get('connected') and cur_st.get('connected_at'):
                    try:
                        conn_dt = datetime.datetime.strptime(
                            cur_st['connected_at'], '%Y-%m-%d %H:%M:%S'
                        ).replace(tzinfo=datetime.timezone(datetime.timedelta(hours=9)))
                        if abs(now_ts - conn_dt.timestamp()) <= 45:
                            return redirect(
                                f"/staff/pdca?yt_connected=1&yt_synced={int(cur_st.get('last_synced_count') or 0)}#pdca-closed-loop"
                            )
                    except Exception:
                        pass
            return redirect('/staff/pdca?yt_error=' + quote(err_msg) + '#yt-oauth-setup')

        synced_cnt = 0
        try:
            core.restore_latest_data_if_empty()
            yt_sync = sns_insights_loop.collect_youtube_shorts_insights(limit=25)
            synced_cnt = int(yt_sync.get('synced') or 0)
            live_s = _get_visible_live_stores()
            pdca_engine.run_pdca_cycle(
                live_stores=live_s,
                days=14,
                apply_actions=True,
                use_ai=False,
                record_log=False,
            )
            core.clear_fast_http_caches()
        except Exception as exc:
            logging.warning(f"[staff_bp] Initial YouTube sync after OAuth warning: {exc}")

        if code_sig:
            _recent_oauth_codes[code_sig] = (time.time(), synced_cnt)

    return redirect(f"/staff/pdca?yt_connected=1&yt_synced={synced_cnt}#pdca-closed-loop")


@staff_bp.route('/api/pdca/youtube/disconnect', methods=['POST'])
def api_pdca_youtube_disconnect():
    """Master-only endpoint to revoke stored YouTube OAuth refresh token."""
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import youtube_analytics_sync

    payload = request.get_json(silent=True) or {}
    clear_client = bool(payload.get('clear_client_credentials')) if isinstance(payload, dict) else False
    youtube_analytics_sync.disconnect_youtube_oauth(clear_client_credentials=clear_client)
    return jsonify({
        'ok': True,
        'youtube_oauth': youtube_analytics_sync.get_youtube_sync_status(
            base_url=request.url_root, allow_blocking=True
        ),
    })


_MAX_INSIGHT_CSV_BYTES = 4 * 1024 * 1024


def _decode_uploaded_csv_file(f_obj) -> tuple:
    """Safely read and decode an uploaded UTF-8/UTF-8-SIG/CP932 CSV file up to 4MB."""
    raw_bytes = f_obj.read(_MAX_INSIGHT_CSV_BYTES + 1024)
    if not raw_bytes:
        return '', 'CSVファイルが空です'
    if len(raw_bytes) > _MAX_INSIGHT_CSV_BYTES:
        return '', 'ファイルサイズは4MB以内にしてください'
    for enc in ('utf-8-sig', 'cp932', 'utf-8'):
        try:
            return raw_bytes.decode(enc), ''
        except UnicodeDecodeError:
            continue
    return raw_bytes.decode('utf-8', errors='replace'), ''


def _ingest_uploaded_csv_or_zip(f_obj, platform_hint: str = 'auto') -> tuple:
    """Handle either an exported `.zip` archive (Search Console / YouTube Studio) or a single `.csv` file."""
    import sns_insights_loop
    filename = f_obj.filename or ''
    fname_low = filename.lower()
    raw_bytes = f_obj.read(_MAX_INSIGHT_CSV_BYTES + 1024)
    if not raw_bytes:
        return None, 'ファイルが空です'
    if len(raw_bytes) > _MAX_INSIGHT_CSV_BYTES:
        return None, 'ファイルサイズは4MB以内にしてください'
    if fname_low.endswith('.zip') or raw_bytes[:4] == b'PK\x03\x04':
        return sns_insights_loop.ingest_zip_insights(raw_bytes, platform_hint=platform_hint, filename=filename), ''
    csv_text = sns_insights_loop._decode_csv_bytes_best_effort(raw_bytes)
    return sns_insights_loop.ingest_csv_insights(csv_text, platform_hint=platform_hint, filename=filename), ''


def _finalize_insights_ingest_response(ingest_res: dict):
    import pdca_engine
    if not ingest_res or not ingest_res.get('ok'):
        return jsonify(ingest_res or {'ok': False, 'error': 'インサイト取込に失敗しました'}), 400
    live_s = _get_visible_live_stores()
    pdca_res = pdca_engine.run_pdca_cycle(
        live_stores=live_s,
        days=14,
        apply_actions=True,
        use_ai=False,
        record_log=False,
    )
    core.clear_fast_http_caches()
    return jsonify({
        'ok': True,
        'ingest': ingest_res,
        'active_directives': pdca_res.get('active_directives') or {},
        'closed_loop_feedback': pdca_res.get('closed_loop_feedback') or ingest_res.get('feedback') or {},
        'flywheel_status': ((pdca_res.get('diagnostics') or {}).get('flywheel_status')) or {},
    })


@staff_bp.route('/api/pdca/sns-insights/ingest-csv', methods=['POST'])
def api_pdca_sns_insights_ingest_csv():
    """
    Master-only endpoint to ingest exported CSVs from Google Search Console or YouTube Studio
    with zero API token consumption and zero hallucination.
    """
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import sns_insights_loop
    core.restore_latest_data_if_empty()

    f_obj = None
    if request.files:
        f_obj = request.files.get('csv_file') or request.files.get('file')

    if f_obj is not None:
        platform_hint = request.form.get('platform') or 'auto'
        ingest_res, err_msg = _ingest_uploaded_csv_or_zip(f_obj, platform_hint=platform_hint)
        if err_msg:
            return jsonify({'ok': False, 'error': err_msg}), 400
        return _finalize_insights_ingest_response(ingest_res)
    else:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict) or not payload.get('csv_text'):
            return jsonify({'ok': False, 'error': 'CSV/ZIPファイルまたはcsv_textを指定してください'}), 400
        csv_text = str(payload.get('csv_text') or '')
        if len(csv_text.encode('utf-8', errors='ignore')) > _MAX_INSIGHT_CSV_BYTES:
            return jsonify({'ok': False, 'error': 'CSVデータサイズは4MB以内にしてください'}), 400
        platform_hint = str(payload.get('platform') or 'auto')
        filename = str(payload.get('filename') or '')

    ingest_res = sns_insights_loop.ingest_csv_insights(
        csv_text,
        platform_hint=platform_hint,
        filename=filename,
    )
    return _finalize_insights_ingest_response(ingest_res)


@staff_bp.route('/api/pdca/sns-insights/ingest', methods=['POST'])
def api_pdca_sns_insights_ingest():
    """
    Master-only endpoint to ingest SNS/YouTube/GSC/note insight screenshots (via Gemini 4 Carbon Vision),
    exported CSV/ZIP files, or structured JSON records and immediately apply updated directives.
    """
    guard = _check_master_api_csrf()
    if guard is not None:
        return guard
    import sns_insights_loop
    core.restore_latest_data_if_empty()

    ingest_res = None
    if request.files and ('csv_file' in request.files or 'file' in request.files):
        f_obj = request.files.get('csv_file') or request.files.get('file')
        fname = (f_obj.filename or '').lower()
        mime_type = (f_obj.mimetype or '').lower()
        platform_hint = request.form.get('platform') or 'auto'
        if (
            'csv_file' in request.files
            or fname.endswith(('.csv', '.zip'))
            or mime_type in ('text/csv', 'application/csv', 'application/vnd.ms-excel', 'application/zip', 'application/x-zip-compressed')
        ):
            ingest_res, err_msg = _ingest_uploaded_csv_or_zip(f_obj, platform_hint=platform_hint)
            if err_msg:
                return jsonify({'ok': False, 'error': err_msg}), 400
        else:
            raw_bytes = f_obj.read(sns_insights_loop.MAX_SCREENSHOT_BYTES + 1024)
            ingest_res = sns_insights_loop.ingest_insight_screenshot_via_carbon(
                raw_bytes,
                mime_type=f_obj.mimetype or 'image/png',
                platform_hint=platform_hint,
            )
    else:
        payload = request.get_json(silent=True) or {}
        if not isinstance(payload, dict):
            return jsonify({'ok': False, 'error': '不正なリクエスト形式です（画像・CSVファイルまたはJSONを指定してください）'}), 400
        if payload.get('csv_text'):
            csv_raw = str(payload.get('csv_text') or '')
            if len(csv_raw.encode('utf-8', errors='ignore')) > _MAX_INSIGHT_CSV_BYTES:
                return jsonify({'ok': False, 'error': 'CSVデータサイズは2MB以内にしてください'}), 400
            ingest_res = sns_insights_loop.ingest_csv_insights(
                csv_raw,
                platform_hint=str(payload.get('platform') or 'auto'),
                filename=str(payload.get('filename') or ''),
            )
        elif payload.get('image_base64'):
            import base64
            b64_raw = str(payload.get('image_base64') or '')
            if ',' in b64_raw and b64_raw.lstrip().startswith('data:'):
                b64_raw = b64_raw.split(',', 1)[1]
            try:
                raw_bytes = base64.b64decode(b64_raw, validate=False)
            except Exception:
                return jsonify({'ok': False, 'error': 'base64画像のデコードに失敗しました'}), 400
            ingest_res = sns_insights_loop.ingest_insight_screenshot_via_carbon(
                raw_bytes,
                mime_type=str(payload.get('mime_type') or 'image/png'),
                platform_hint=str(payload.get('platform') or 'auto'),
            )
        else:
            ingest_res = sns_insights_loop.ingest_manual_insight_records(
                payload,
                source='master_json_api',
            )

    return _finalize_insights_ingest_response(ingest_res)


@staff_bp.route('/staff/sns-kit')
def staff_sns_kit():
    """Master-only one-click Instagram / Threads weekend forecast carousel & caption generator."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import ig_carousel_generator
    ctx = core._get_sns_kit_context()
    captions = ig_carousel_generator.build_captions(ctx)
    cache_bust = int(time.time()) // 300
    return render_template('staff_sns_kit.html', ctx=ctx, captions=captions, cache_bust=cache_bust)


@staff_bp.route('/staff/sns-kit/slide/<int:slide_num>.png')
def staff_sns_kit_slide(slide_num):
    """Render individual 1080x1350 PNG slide for master mobile save or preview (memoized in RAM)."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    ctx = core._get_sns_kit_context()
    png_bytes = core._render_cached_ig_slide_bytes(slide_num, is_jpg=False, ctx=ctx)
    headers = {'Cache-Control': 'private, max-age=300'}
    if request.args.get('download') == '1':
        headers['Content-Disposition'] = f'attachment; filename="aiseki_navi_slide_0{max(1, min(5, slide_num))}.png"'
    return Response(png_bytes, mimetype='image/png', headers=headers)


@staff_bp.route('/staff/sns-kit/download.zip')
def staff_sns_kit_zip():
    """Download all 5 portrait PNG slides + Instagram/Threads captions in one ZIP (memoized in RAM)."""
    if not session.get('is_master'):
        return redirect(url_for('staff_monitor', switch='1'))
    import ig_carousel_generator
    ctx = core._get_sns_kit_context()
    ctx_sig = core._sns_context_sig(ctx)
    now_t = time.time()
    zip_cache = core._sns_zip_bytes_cache
    with core._sns_cache_lock:
        if zip_cache.get('bytes') is not None and zip_cache.get('key') == ctx_sig and (now_t - zip_cache.get('ts', 0.0) < 300):
            zip_bytes = zip_cache['bytes']
        else:
            zip_bytes = None
    if zip_bytes is None:
        zip_blob_key = f"ig_zip:{core.turbo_store.make_cache_hash(ctx_sig)}"
        shared_zip = core.turbo_store.get_media_blob(zip_blob_key)
        if shared_zip and shared_zip[0]:
            zip_bytes = shared_zip[0]
        else:
            zip_bytes = ig_carousel_generator.render_all_slides_zip(ctx=ctx)
            core.turbo_store.set_media_blob(zip_blob_key, zip_bytes, mime_type='application/zip', ttl_sec=3600)
        with core._sns_cache_lock:
            zip_cache['key'] = ctx_sig
            zip_cache['ts'] = now_t
            zip_cache['bytes'] = zip_bytes
    today_s = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d')
    return Response(
        zip_bytes,
        mimetype='application/zip',
        headers={'Content-Disposition': f'attachment; filename="aiseki_navi_weekend_kit_{today_s}.zip"'},
    )
