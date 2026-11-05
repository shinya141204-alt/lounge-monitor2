"""Dynamic OGP/Reel media endpoints, async SNS job queue engine, and master SNS trigger endpoints Blueprint."""

import datetime
import logging
import re
import threading
import time

from flask import Blueprint, Response, abort, jsonify, request, send_from_directory
from werkzeug.exceptions import HTTPException

from routes._bridge import core

sns_jobs_bp = Blueprint('sns_jobs_bp', __name__)


@sns_jobs_bp.route('/ogp/live.png')
def dynamic_live_ogp_image():
    """Dynamically render a 1200x630 live TOP3 ranking OGP card image (memoized in RAM)."""
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    s_param = (request.args.get('s') or '').strip()[:40]
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    bucket_10m = f"{now_jst.strftime('%Y%m%d%H')}_{(now_jst.minute // 10) * 10:02d}"
    with core.data_lock:
        stores = list(core.latest_data.get('full_data') or [])
        last_u = core.latest_data.get('last_updated')
        is_s = bool(core.latest_data.get('is_seed'))
    ogp_key = ('live', s_param, bucket_10m, core._live_data_signature(stores, last_u, is_s))
    now_mono = time.monotonic()
    ogp_cache = core._ogp_bytes_cache
    with core._ogp_bytes_cache_lock:
        hit = ogp_cache.get(ogp_key)
        if hit is not None and (now_mono - hit[0] < 120.0):
            ogp_cache.move_to_end(ogp_key)
            resp = Response(hit[1], mimetype='image/png')
            resp.headers['Cache-Control'] = 'public, max-age=300'
            return resp
    try:
        import ogp_generator
        png_bytes = ogp_generator.generate_live_ogp_png(stores, s_param=s_param)
        with core._ogp_bytes_cache_lock:
            ogp_cache[ogp_key] = (now_mono, png_bytes)
            ogp_cache.move_to_end(ogp_key)
            while len(ogp_cache) > core._OGP_BYTES_CACHE_MAX:
                ogp_cache.popitem(last=False)
        resp = Response(png_bytes, mimetype='image/png')
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    except Exception as e:
        logging.error(f"Dynamic OGP generation failed, falling back to static image: {e}")
        return send_from_directory(core.app.static_folder, 'note-eyecatch.jpg', mimetype='image/jpeg')


@sns_jobs_bp.route('/ogp/weekly.png')
def dynamic_weekly_ogp_image():
    """Dynamically render a 1200x630 weekly ranking & open-data infographic OGP card image (memoized in RAM)."""
    try:
        import ogp_generator
        import weekly_report
        report = weekly_report.generate_weekly_report()
        valid_rep = report if isinstance(report, dict) and 'error' not in report else {}
        ogp_key = (
            'weekly',
            valid_rep.get('generated_at'),
            len(valid_rep.get('overall_ranking') or []),
            core.local_db.DB_PATH,
        )
        now_mono = time.monotonic()
        ogp_cache = core._ogp_bytes_cache
        with core._ogp_bytes_cache_lock:
            hit = ogp_cache.get(ogp_key)
            if hit is not None and (now_mono - hit[0] < 600.0):
                ogp_cache.move_to_end(ogp_key)
                resp = Response(hit[1], mimetype='image/png')
                resp.headers['Cache-Control'] = 'public, max-age=1800'
                return resp
        png_bytes = ogp_generator.generate_weekly_ogp_png(valid_rep)
        with core._ogp_bytes_cache_lock:
            ogp_cache[ogp_key] = (now_mono, png_bytes)
            ogp_cache.move_to_end(ogp_key)
            while len(ogp_cache) > core._OGP_BYTES_CACHE_MAX:
                ogp_cache.popitem(last=False)
        resp = Response(png_bytes, mimetype='image/png')
        resp.headers['Cache-Control'] = 'public, max-age=1800'
        return resp
    except Exception as e:
        logging.error(f"Weekly OGP generation failed, falling back to static image: {e}")
        return send_from_directory(core.app.static_folder, 'note-eyecatch.jpg', mimetype='image/jpeg')


@sns_jobs_bp.route('/ogp/ig-slide/<int:slide_num>.png')
@sns_jobs_bp.route('/ogp/ig-slide/<int:slide_num>.jpg')
def public_ig_slide_png(slide_num):
    """Publicly accessible 1080x1350 JPEG/PNG slide endpoint so Meta Instagram Graph API servers can fetch carousel images."""
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    clamped = max(1, min(5, slide_num))
    is_jpg = request.path.endswith('.jpg')
    try:
        ctx = core._get_sns_kit_context(fast=False)
        img_bytes = core._render_cached_ig_slide_bytes(clamped, is_jpg=is_jpg, ctx=ctx)
        resp = Response(img_bytes, mimetype='image/jpeg' if is_jpg else 'image/png')
        resp.headers['Cache-Control'] = 'public, max-age=300'
        return resp
    except Exception as e:
        logging.error(f"Public IG slide generation failed: {e}")
        abort(503)


@sns_jobs_bp.route('/ogp/reel/<reel_type>.mp4')
def public_reel_mp4(reel_type):
    """Publicly accessible 720x1280 9:16 H.264+AAC MP4 endpoint for Meta Instagram Reels API & TikTok/Shorts download."""
    import reel_generator
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    rt = reel_generator.normalize_reel_type(reel_type)
    has_range = bool(request.range and request.range.ranges)
    first_start = request.range.ranges[0][0] if has_range else None
    is_cont_range = bool(
        has_range
        and (
            (first_start is not None and first_start != 0)
            or bool(request.headers.get('If-Range'))
        )
    )
    wants_refresh = (request.args.get('refresh') == '1')
    preview_tok = re.sub(r'[^A-Za-z0-9_.-]', '', (request.args.get('t') or '').strip())[:32]
    force_refresh = wants_refresh and not is_cont_range
    slot_token = (request.args.get('s') or '').strip()
    with core.data_lock:
        live_s = list(core.latest_data.get('full_data') or [])
    try:
        mp4_bytes = None
        if wants_refresh and preview_tok and preview_tok != 'init':
            mp4_bytes = reel_generator.get_pinned_reel_mp4_slot(rt, f"prev_{preview_tok}")
            if mp4_bytes:
                force_refresh = False
        if not mp4_bytes and slot_token and not force_refresh:
            mp4_bytes = reel_generator.get_pinned_reel_mp4_slot(rt, slot_token)
        if not mp4_bytes:
            mp4_bytes = reel_generator.render_reel_mp4_bytes(live_s, reel_type=rt, force_refresh=force_refresh)
            if wants_refresh and preview_tok and preview_tok != 'init':
                reel_generator.pin_shared_reel_mp4_slot(rt, f"prev_{preview_tok}", mp4_bytes, ttl_sec=300)
            if slot_token:
                reel_generator.pin_shared_reel_mp4_slot(rt, slot_token, mp4_bytes)
        headers = {
            'Cache-Control': 'public, max-age=300',
            'Accept-Ranges': 'bytes',
        }
        if request.args.get('download') == '1':
            today_s = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y%m%d_%H%M')
            headers['Content-Disposition'] = f'attachment; filename="aiseki_navi_reel_{rt}_{today_s}.mp4"'
            return Response(mp4_bytes, mimetype='video/mp4', headers=headers)
        total_len = len(mp4_bytes)
        resp = Response(mp4_bytes, mimetype='video/mp4', headers=headers)
        resp.add_etag()
        # RFC 9110 §14.2: Werkzeug does not pack multipart/byteranges and raises 416 on any multi-range header.
        # Serve 200 OK when at least one requested range is satisfiable; return 416 only when all ranges are out-of-bounds.
        if has_range and len(request.range.ranges) > 1:
            any_satisfiable = any(
                (total_len > 0) if r_start < 0 else (r_start < total_len and (r_end is None or r_end > r_start))
                for r_start, r_end in request.range.ranges
            )
            if any_satisfiable:
                return resp.make_conditional(request, accept_ranges=False)
        return resp.make_conditional(request, accept_ranges=True, complete_length=total_len)
    except HTTPException:
        raise
    except RuntimeError as re_err:
        logging.warning(f"Reel MP4 unavailable ({rt}): {re_err}")
        return jsonify({'ok': False, 'error': str(re_err)}), 503
    except Exception as e:
        logging.error(f"Public Reel MP4 generation failed ({rt}): {e}")
        abort(503)


@sns_jobs_bp.route('/ogp/reel/<reel_type>.jpg')
def public_reel_preview_jpg(reel_type):
    """Pure-Pillow 720x1280 9:16 JPEG poster frame for `<video poster=...>` and instant preview."""
    import reel_generator
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    rt = reel_generator.normalize_reel_type(reel_type)
    with core.data_lock:
        live_s = list(core.latest_data.get('full_data') or [])
    try:
        jpg_bytes = reel_generator.render_reel_preview_frame_jpeg(live_s, reel_type=rt, sec=6.0)
        return Response(jpg_bytes, mimetype='image/jpeg', headers={'Cache-Control': 'public, max-age=120'})
    except Exception as e:
        logging.error(f"Public Reel preview JPEG failed ({rt}): {e}")
        abort(503)


@sns_jobs_bp.route('/api/sns-post')
def generate_sns_post_api():
    """Returns ready-to-post viral X/SNS text from current live data."""
    import sns_workflows
    post_type = request.args.get('type', 'all')
    base_url = request.url_root.rstrip('/')
    return jsonify(sns_workflows.build_sns_post_payload(post_type, base_url, core.latest_data))


def run_scheduled_x_post(slot_label="20:00", force=False):
    """Execute scheduled X auto-post with duplicate prevention that survives restarts/redeploys."""
    import sns_workflows
    return sns_workflows.execute_scheduled_x_post(core.latest_data, slot_label=slot_label, force=force)


def run_scheduled_weekly_x_post(force=False):
    """Execute weekly summary X auto-post (with dynamic /ogp/weekly.png card attached to 1st post)."""
    import sns_workflows
    return sns_workflows.execute_scheduled_weekly_x_post(force=force)


def precompute_editorial_and_media_bundle(
    channels=('x', 'threads'),
    slot_labels=None,
    include_column: bool = False,
    include_pdca: bool = False,
    include_ig_slides: bool = False,
) -> dict:
    """Pre-generate AI editorial drafts and 5-slide IG carousel images on `worker` so `web` & SNS APIs respond in 0ms."""
    import sns_editorial
    started_m = time.monotonic()
    core.restore_latest_data_if_empty()
    with core.data_lock:
        live_s = list(core.latest_data.get('full_data') or [])
        is_seed = bool(core.latest_data.get('is_seed'))

    summary = {
        'ok': True,
        'store_count': len(live_s),
        'is_seed': is_seed,
        'ig_slides_rendered': 0,
        'editorial_warmed': [],
        'column_warmed': False,
        'pdca_warmed': False,
        'errors': [],
    }
    if not live_s:
        summary['ok'] = False
        summary['errors'].append('no_live_stores')
        return summary

    ig_ctx = None
    if include_ig_slides or ('instagram' in (channels or ())):
        try:
            ig_ctx = core._get_sns_kit_context(fast=False)
            if include_ig_slides and isinstance(ig_ctx, dict):
                for s_num in range(1, 6):
                    core._render_cached_ig_slide_bytes(s_num, is_jpg=True, ctx=ig_ctx)
                    core._render_cached_ig_slide_bytes(s_num, is_jpg=False, ctx=ig_ctx)
                    summary['ig_slides_rendered'] += 1
                try:
                    import ig_carousel_generator
                    ctx_sig = core._sns_context_sig(ig_ctx)
                    zip_blob_key = f"ig_zip:{core.turbo_store.make_cache_hash(ctx_sig)}"
                    if not core.turbo_store.get_media_blob(zip_blob_key):
                        z_bytes = ig_carousel_generator.render_all_slides_zip(ctx=ig_ctx)
                        core.turbo_store.set_media_blob(zip_blob_key, z_bytes, mime_type='application/zip', ttl_sec=3600)
                except Exception as z_err:
                    summary['errors'].append(f"ig_zip:{z_err}")
        except Exception as ig_err:
            summary['errors'].append(f"ig_ctx:{ig_err}")

    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    if not slot_labels:
        hr = now_jst.hour
        if hr < 20:
            default_slots = ('20:00',)
        elif hr < 22:
            default_slots = ('22:00',)
        else:
            default_slots = ('00:00', '22:00')
    else:
        default_slots = tuple(slot_labels)

    try:
        sns_editorial.fetch_live_weather_context(allow_blocking=True)
    except Exception:
        pass

    for ch in (channels or ()):
        ch_clean = str(ch or '').strip().lower()
        if ch_clean not in ('x', 'threads', 'instagram'):
            continue
        ch_slots = ('21:45',) if ch_clean == 'instagram' and not slot_labels else default_slots
        for sl in ch_slots:
            try:
                ed = sns_editorial.run_editorial_pipeline(
                    live_s,
                    slot_label=sl,
                    channel=ch_clean,
                    ig_ctx=ig_ctx if ch_clean == 'instagram' else None,
                    record_history=False,
                    use_ai=True,
                    force_ai_refresh=False,
                    ai_cache_only=False,
                )
                winner_style = ((ed or {}).get('winner') or {}).get('style', '')
                summary['editorial_warmed'].append(f"{ch_clean}:{sl}:{winner_style}")
            except Exception as ed_err:
                summary['errors'].append(f"editorial:{ch_clean}:{sl}:{ed_err}")

    if include_column:
        try:
            import column_editorial
            column_editorial.run_column_editorial_pipeline(
                live_s,
                theme_key='area_hopping',
                use_ai=True,
                force_ai_refresh=False,
                ai_cache_only=False,
            )
            summary['column_warmed'] = True
        except Exception as col_err:
            summary['errors'].append(f"column:{col_err}")

    if include_pdca:
        try:
            import pdca_engine
            pdca_engine.run_pdca_cycle(
                live_stores=live_s,
                days=14,
                apply_actions=False,
                use_ai=True,
                ai_cache_only=False,
                force_ai_refresh=False,
                record_log=False,
            )
            summary['pdca_warmed'] = True
        except Exception as pdca_err:
            summary['errors'].append(f"pdca:{pdca_err}")

    summary['elapsed_ms'] = int((time.monotonic() - started_m) * 1000)
    return summary


SUPPORTED_SNS_JOB_TYPES = (
    'x_post',
    'x_tip',
    'note_share',
    'x_pinned',
    'x_column',
    'x_weekly',
    'x_bonus',
    'threads_post',
    'ig_post',
    'ig_reel',
    'tiktok_inbox',
    'ladies_sns',
    'prewarm_bundle',
)


def execute_sns_job_payload(job_type: str, params: dict = None) -> dict:
    """Execute a single SNS / prewarm job synchronously (invoked by `worker.py` queue consumer or inline fallback)."""
    p = dict(params or {})
    jtype = str(job_type or '').strip().lower()
    force = bool(p.get('force')) if isinstance(p.get('force'), bool) else (str(p.get('force', '0')) == '1')
    core.restore_latest_data_if_empty()

    if jtype == 'x_post':
        slot = str(p.get('slot') or '20:00').strip()
        return core.run_scheduled_x_post(slot_label=slot, force=force)

    if jtype == 'x_tip':
        import tips_poster
        slot = str(p.get('slot') or '12:00').strip()
        idx_raw = p.get('idx')
        idx = int(idx_raw) if idx_raw is not None and str(idx_raw).strip() != '' else None
        return tips_poster.run_scheduled_tip_post(slot_label=slot, force=force, tip_index=idx)

    if jtype == 'note_share':
        import note_poster
        idx = int(p.get('idx') or 0)
        return note_poster.check_and_post_new_note(force=force, article_index=idx)

    if jtype == 'x_pinned':
        import x_poster
        main_text, reply_text = x_poster.build_pinned_tweet_thread()
        res = x_poster.post_to_x(main_text, reply_text=reply_text)
        res['tweet_text'] = main_text
        res['reply_text'] = reply_text
        return res

    if jtype == 'x_column':
        import x_poster
        idx = int(p.get('idx') or 0)
        main_text, reply_text, item = x_poster.build_column_tweet_thread(col_index=idx)
        media_bytes, media_mime = x_poster.load_column_eyecatch_bytes(item)
        res = x_poster.post_to_x(
            main_text,
            reply_text=reply_text,
            media_bytes=media_bytes,
            media_mime_type=media_mime,
        )
        res['column'] = item
        res['tweet_text'] = main_text
        res['reply_text'] = reply_text
        return res

    if jtype == 'x_weekly':
        return core.run_scheduled_weekly_x_post(force=force)

    if jtype == 'x_bonus':
        import bonus_alert
        with core.data_lock:
            stores = list(core.latest_data.get('full_data') or [])
        return bonus_alert.check_and_post_bonus_alert(stores, force=force)

    if jtype == 'threads_post':
        import threads_poster
        slot = str(p.get('slot') or '20:00').strip()
        with core.data_lock:
            stores = list(core.latest_data.get('full_data') or [])
        return threads_poster.execute_scheduled_threads_post(stores, slot_label=slot, force=force)

    if jtype == 'ig_post':
        import instagram_poster
        mode = str(p.get('mode') or 'carousel').strip()
        slot = str(p.get('slot') or '22:00').strip()
        with core.data_lock:
            stores = list(core.latest_data.get('full_data') or [])
        return instagram_poster.execute_scheduled_instagram_post(stores, mode=mode, slot_label=slot, force=force)

    if jtype == 'ig_reel':
        import instagram_poster
        import reel_generator
        rt = reel_generator.normalize_reel_type(p.get('reel_type', 'live_top5'))
        cap = str(p.get('caption') or '').strip()
        share_feed = bool(p.get('share_to_feed', True))
        slot = str(p.get('slot') or 'manual_reel').strip()
        with core.data_lock:
            stores = list(core.latest_data.get('full_data') or [])
        return instagram_poster.publish_live_reel_to_instagram(
            stores,
            reel_type=rt,
            custom_caption=cap,
            share_to_feed=share_feed,
            slot_label=slot,
            force=force,
        )

    if jtype == 'tiktok_inbox':
        import reel_generator
        import tiktok_poster
        rt = reel_generator.normalize_reel_type(p.get('reel_type', 'tiktok_gap_shock'))
        cap = str(p.get('caption') or '').strip()
        slot = str(p.get('slot') or 'manual_tiktok').strip()
        with core.data_lock:
            stores = list(core.latest_data.get('full_data') or [])
        return tiktok_poster.publish_live_reel_to_tiktok_inbox(
            stores,
            reel_type=rt,
            custom_caption=cap,
            slot_label=slot,
            force=force,
        )

    if jtype == 'ladies_sns':
        channel = str(p.get('channel') or 'threads').strip().lower()
        with core.data_lock:
            stores = list(core.latest_data.get('full_data') or [])
        if channel in ('ig', 'instagram'):
            import instagram_poster
            slot = str(p.get('slot') or '19:45').strip()
            return instagram_poster.execute_scheduled_ladies_instagram_post(stores, slot_label=slot, force=force)
        import threads_poster
        slot = str(p.get('slot') or '19:30').strip()
        return threads_poster.execute_scheduled_ladies_threads_post(stores, slot_label=slot, force=force)

    if jtype == 'prewarm_bundle':
        channels = p.get('channels') or ('x', 'threads')
        slot_labels = p.get('slot_labels')
        return core.precompute_editorial_and_media_bundle(
            channels=channels,
            slot_labels=slot_labels,
            include_column=bool(p.get('include_column')),
            include_pdca=bool(p.get('include_pdca')),
            include_ig_slides=bool(p.get('include_ig_slides', False)),
        )

    raise ValueError(f"Unsupported SNS job_type: {jtype}")


_inline_sns_consumer_lock = threading.Lock()
_inline_sns_consumer_running = False


def drain_sns_job_queue_once(timeout_sec: float = 0.05) -> dict:
    """Claim and execute a single queued SNS job, updating its final record in `turbo_store`."""
    claimed = core.turbo_store.claim_next_sns_job(timeout_sec=timeout_sec)
    if not claimed:
        return None
    jid = claimed.get('job_id')
    jtype = claimed.get('job_type')
    jparams = claimed.get('params') or {}
    try:
        res = core.execute_sns_job_payload(jtype, jparams)
        err_msg = ''
        if isinstance(res, dict):
            st = str(res.get('status') or '').lower()
            if st in ('error', 'failed') or res.get('ok') is False:
                err_msg = str(res.get('error') or res.get('reason') or res.get('detail') or 'delivery_error')
        return core.turbo_store.complete_sns_job(jid, result=res if isinstance(res, dict) else {'result': res}, error=err_msg)
    except Exception as exc:
        logging.error(f"Async SNS job {jid} ({jtype}) failed: {exc}")
        return core.turbo_store.complete_sns_job(jid, result={'status': 'error', 'error': str(exc)}, error=str(exc))


def _ensure_inline_sns_consumer_started():
    """Fallback background consumer when no external Redis-connected `worker.py` process is active."""
    global _inline_sns_consumer_running
    with _inline_sns_consumer_lock:
        if _inline_sns_consumer_running:
            return
        _inline_sns_consumer_running = True

    def _runner():
        global _inline_sns_consumer_running
        try:
            for _ in range(32):
                done = core.drain_sns_job_queue_once(timeout_sec=0.05)
                if done is None:
                    break
        finally:
            with _inline_sns_consumer_lock:
                _inline_sns_consumer_running = False

    threading.Thread(target=_runner, daemon=True).start()


def _enqueue_sns_job_and_maybe_dispatch(job_type: str, params: dict = None, worker_only: bool = False) -> dict:
    job = core.turbo_store.enqueue_sns_job(job_type, params=params or {}, requested_by='master')
    dedicated_redis_worker = core.is_web_only_role() and (core.turbo_store.get_redis_client() is not None)
    if not worker_only and not dedicated_redis_worker:
        core._ensure_inline_sns_consumer_started()
    return job


def _maybe_enqueue_async_sns_trigger(job_type: str, params: dict = None):
    if request.args.get('async') != '1':
        return None
    worker_only = request.args.get('worker_only') == '1'
    job = core._enqueue_sns_job_and_maybe_dispatch(job_type, params=params, worker_only=worker_only)
    return jsonify({
        'ok': True,
        'queued': True,
        'job_id': job['job_id'],
        'job_type': job['job_type'],
        'status': job['status'],
        'poll_url': f"/api/sns-jobs/{job['job_id']}",
    }), 202


@sns_jobs_bp.route('/api/sns-jobs/enqueue', methods=['POST'])
def api_sns_jobs_enqueue():
    """Master-only endpoint to enqueue an SNS post or prewarm bundle onto the background worker in <1ms."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    payload = request.get_json(silent=True, force=True) or {}
    job_type = str(payload.get('job_type') or '').strip().lower()
    if job_type not in core.SUPPORTED_SNS_JOB_TYPES:
        return jsonify({'ok': False, 'error': f'Unsupported job_type: {job_type}'}), 400
    params = payload.get('params') if isinstance(payload.get('params'), dict) else {}
    worker_only = bool(payload.get('worker_only')) or (request.args.get('worker_only') == '1')
    job = core._enqueue_sns_job_and_maybe_dispatch(job_type, params=params, worker_only=worker_only)
    return jsonify({
        'ok': True,
        'queued': True,
        'job': job,
        'job_id': job['job_id'],
        'poll_url': f"/api/sns-jobs/{job['job_id']}",
    }), 202


@sns_jobs_bp.route('/api/sns-jobs/<job_id>')
def api_sns_job_detail(job_id):
    """Master-only status polling endpoint for async SNS jobs."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    job = core.turbo_store.get_sns_job(str(job_id or '').strip()[:64])
    if not job:
        return jsonify({'ok': False, 'error': 'Job not found'}), 404
    return jsonify({'ok': True, 'job': job})


@sns_jobs_bp.route('/api/sns-jobs')
def api_sns_jobs_list():
    """Master-only list of recent async SNS jobs."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    limit = request.args.get('limit', 12, type=int)
    jobs = core.turbo_store.list_recent_sns_jobs(limit=limit)
    return jsonify({'ok': True, 'jobs': jobs, 'turbo': core.turbo_store.get_backend_status()})


@sns_jobs_bp.route('/api/trigger-prewarm', methods=['GET', 'POST'])
def trigger_prewarm_endpoint():
    """Master-only endpoint to precompute AI editorial drafts on demand."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    include_col = request.args.get('column', '0') == '1'
    include_pdca = request.args.get('pdca', '0') == '1'
    include_ig = request.args.get('ig_slides', '0') == '1'
    queued_resp = _maybe_enqueue_async_sns_trigger(
        'prewarm_bundle',
        {'include_column': include_col, 'include_pdca': include_pdca, 'include_ig_slides': include_ig},
    )
    if queued_resp is not None:
        return queued_resp
    res = core.precompute_editorial_and_media_bundle(include_column=include_col, include_pdca=include_pdca, include_ig_slides=include_ig)
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-x-post')
def trigger_x_post_endpoint():
    """Endpoint to test or externally trigger X auto-posting (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    slot = request.args.get('slot', '20:00')
    force = request.args.get('force', '0') == '1'
    queued_resp = _maybe_enqueue_async_sns_trigger('x_post', {'slot': slot, 'force': force})
    if queued_resp is not None:
        return queued_resp
    res = core.run_scheduled_x_post(slot_label=slot, force=force)
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-x-tip')
def trigger_x_tip_endpoint():
    """Endpoint to test or externally trigger X human-like tips posting (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    slot = request.args.get('slot', '12:00')
    force = request.args.get('force', '0') == '1'
    idx = request.args.get('idx', type=int)
    queued_resp = _maybe_enqueue_async_sns_trigger('x_tip', {'slot': slot, 'force': force, 'idx': idx})
    if queued_resp is not None:
        return queued_resp
    import tips_poster
    res = tips_poster.run_scheduled_tip_post(slot_label=slot, force=force, tip_index=idx)
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-note-share')
def trigger_note_share_endpoint():
    """Endpoint to test or externally trigger note promotion posting (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    idx = request.args.get('idx', 0, type=int)
    queued_resp = _maybe_enqueue_async_sns_trigger('note_share', {'force': force, 'idx': idx})
    if queued_resp is not None:
        return queued_resp
    import note_poster
    res = note_poster.check_and_post_new_note(force=force, article_index=idx)
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-x-pinned')
def trigger_x_pinned_endpoint():
    """Endpoint to post the 2-post Pinned Tweet thread (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    queued_resp = _maybe_enqueue_async_sns_trigger('x_pinned', {})
    if queued_resp is not None:
        return queued_resp
    import x_poster
    main_text, reply_text = x_poster.build_pinned_tweet_thread()
    res = x_poster.post_to_x(main_text, reply_text=reply_text)
    res["tweet_text"] = main_text
    res["reply_text"] = reply_text
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-x-column')
def trigger_x_column_endpoint():
    """Endpoint to post a 2-post Column Promotion thread (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    idx = request.args.get('idx', 0, type=int)
    queued_resp = _maybe_enqueue_async_sns_trigger('x_column', {'idx': idx})
    if queued_resp is not None:
        return queued_resp
    import x_poster
    main_text, reply_text, item = x_poster.build_column_tweet_thread(col_index=idx)
    media_bytes, media_mime = x_poster.load_column_eyecatch_bytes(item)
    res = x_poster.post_to_x(
        main_text,
        reply_text=reply_text,
        media_bytes=media_bytes,
        media_mime_type=media_mime,
    )
    res["column"] = item
    res["tweet_text"] = main_text
    res["reply_text"] = reply_text
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-x-weekly')
def trigger_x_weekly_endpoint():
    """Endpoint to test or externally trigger the Weekly Summary X post (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    queued_resp = _maybe_enqueue_async_sns_trigger('x_weekly', {'force': force})
    if queued_resp is not None:
        return queued_resp
    res = core.run_scheduled_weekly_x_post(force=force)
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-x-bonus')
def trigger_x_bonus_endpoint():
    """Endpoint to test or externally trigger the Bonus-Time Guerrilla Alert X post (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    force = request.args.get('force', '0') == '1'
    queued_resp = _maybe_enqueue_async_sns_trigger('x_bonus', {'force': force})
    if queued_resp is not None:
        return queued_resp
    import bonus_alert
    with core.data_lock:
        stores = list(core.latest_data.get('full_data') or [])
    res = bonus_alert.check_and_post_bonus_alert(stores, force=force)
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-threads-post')
def trigger_threads_post_endpoint():
    """Endpoint to test or externally trigger Meta Threads auto-post (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    slot = request.args.get('slot', '20:00')
    force = request.args.get('force', '0') == '1'
    queued_resp = _maybe_enqueue_async_sns_trigger('threads_post', {'slot': slot, 'force': force})
    if queued_resp is not None:
        return queued_resp
    import threads_poster
    with core.data_lock:
        stores = list(core.latest_data.get('full_data') or [])
    res = threads_poster.execute_scheduled_threads_post(stores, slot_label=slot, force=force)
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-ig-post')
def trigger_ig_post_endpoint():
    """Endpoint to test or externally trigger Meta Instagram Graph API auto-post (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    mode = request.args.get('mode', 'carousel')
    slot = request.args.get('slot', '22:00')
    force = request.args.get('force', '0') == '1'
    queued_resp = _maybe_enqueue_async_sns_trigger('ig_post', {'mode': mode, 'slot': slot, 'force': force})
    if queued_resp is not None:
        return queued_resp
    import instagram_poster
    with core.data_lock:
        stores = list(core.latest_data.get('full_data') or [])
    res = instagram_poster.execute_scheduled_instagram_post(stores, mode=mode, slot_label=slot, force=force)
    return jsonify(res)


@sns_jobs_bp.route('/api/trigger-ladies-sns')
def trigger_ladies_sns_endpoint():
    """Endpoint to test or externally trigger Plan E Women-Perspective Zero-Wait & Perks SNS alerts (master only)."""
    if not core.is_master_authorized():
        return jsonify({'error': 'Unauthorized'}), 401
    channel = (request.args.get('channel') or 'threads').strip().lower()
    force = request.args.get('force', '0') == '1'
    slot_arg = request.args.get('slot') or ('19:45' if channel in ('ig', 'instagram') else '19:30')
    queued_resp = _maybe_enqueue_async_sns_trigger('ladies_sns', {'channel': channel, 'slot': slot_arg, 'force': force})
    if queued_resp is not None:
        return queued_resp
    with core.data_lock:
        stores = list(core.latest_data.get('full_data') or [])
    if channel in ('ig', 'instagram'):
        import instagram_poster
        res = instagram_poster.execute_scheduled_ladies_instagram_post(stores, slot_label=slot_arg, force=force)
    else:
        import threads_poster
        res = threads_poster.execute_scheduled_ladies_threads_post(stores, slot_label=slot_arg, force=force)
    return jsonify(res)
