"""Dedicated Background Engine (`APP_ROLE=worker`) for Aiseki Navi (Plan Take).

Responsibilities isolated from HTTP Gunicorn `web` workers:
  1. Every-minute scraping of all 53 stores + PostgreSQL persistence + `<0.5ms` Redis live snapshot broadcast
  2. Continuous async SNS job queue consumer (`claim_next_sns_job` -> `execute_sns_job_payload`)
  3. Pre-scheduled AI editorial & 5-slide IG carousel precomputation (8 minutes before every SNS slot + every 30m)
  4. All scheduled X / Threads / Instagram / Note / Bonus / Autonomous PDCA / Meta 60-day token maintenance jobs
"""

import logging
import os
import signal
import sys
import threading
import time

# Force worker role and prevent `app.py` from auto-starting its legacy embedded scheduler on import
os.environ.setdefault("APP_ROLE", "worker")
_was_disable_scheduler = os.environ.get("DISABLE_SCHEDULER")
# Ensure app.py module import does not trigger its embedded legacy BackgroundScheduler
os.environ["DISABLE_SCHEDULER"] = "1"

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler

import app as app_module
# Mark app legacy scheduler as started to prevent any subsequent trigger
app_module._scheduler_started = True
# Restore original DISABLE_SCHEDULER environment variable (removes it if it wasn't originally set)
if _was_disable_scheduler is not None:
    os.environ["DISABLE_SCHEDULER"] = _was_disable_scheduler
else:
    os.environ.pop("DISABLE_SCHEDULER", None)

import local_db
import scheduled_articles
import turbo_store

logger = logging.getLogger("aiseki.worker")


def _flush_analytics_safe():
    try:
        import analytics
        analytics.flush()
    except Exception as err:
        logger.error(f"[worker] analytics flush failed: {err}")


def _x_scheduled_job(slot_lbl="20:00"):
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("x", slot_lbl):
            return
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
        skip_pace, pace_reason = sns_editorial.should_skip_slot_for_human_pacing("x", slot_lbl, stores=cur_stores)
        if skip_pace:
            logger.info(f"[worker] Skipping X {slot_lbl} for human pacing: {pace_reason}")
            return
        app_module.run_scheduled_x_post(slot_lbl)
    except Exception as err:
        logger.error(f"[worker] Scheduled X post ({slot_lbl}) failed: {err}")


def _x_weekly_sun_job():
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("x_weekly", "19:00"):
            return
        app_module.run_scheduled_weekly_x_post()
    except Exception as err:
        logger.error(f"[worker] Scheduled weekly X post failed: {err}")


def _tip_18_job():
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("x_tip", "18:00"):
            return
        skip_pace, pace_reason = sns_editorial.should_skip_slot_for_human_pacing("x_tip", "18:00")
        if skip_pace:
            logger.info(f"[worker] Skipping X tip 18:00 for human pacing: {pace_reason}")
            return
        import tips_poster
        tips_poster.run_scheduled_tip_post(slot_label="18:00")
    except Exception as err:
        logger.error(f"[worker] Scheduled tip 18:00 failed: {err}")


def _note_check_job():
    try:
        import note_poster
        note_poster.check_and_post_new_note()
    except Exception as err:
        logger.error(f"[worker] Scheduled note promotion check failed: {err}")


def _threads_daily_job():
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("threads", "22:00"):
            return
        import threads_poster
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            skip_pace, pace_reason = sns_editorial.should_skip_slot_for_human_pacing("threads", "22:00", stores=cur_stores)
            if skip_pace:
                logger.info(f"[worker] Skipping Threads 22:00 for human pacing: {pace_reason}")
                return
            img_url = threads_poster.select_scheduled_threads_image_url()
            threads_poster.execute_scheduled_threads_post(cur_stores, slot_label="22:00", image_url=img_url)
    except Exception as err:
        logger.error(f"[worker] Scheduled Threads post failed: {err}")


def _ig_carousel_job(slot_lbl="21:45"):
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("instagram", slot_lbl):
            return
        import instagram_poster
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if not is_seed:
            instagram_poster.execute_scheduled_instagram_post(cur_stores, mode="carousel", slot_label=slot_lbl)
    except Exception as err:
        logger.error(f"[worker] Scheduled Instagram carousel post failed: {err}")


def _threads_ladies_job():
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("threads_ladies", "19:30"):
            return
        import threads_poster
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            threads_poster.execute_scheduled_ladies_threads_post(cur_stores, slot_label="19:30")
    except Exception as err:
        logger.error(f"[worker] Scheduled Threads ladies post failed: {err}")


def _ig_ladies_job():
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("ig_ladies", "19:45"):
            return
        import instagram_poster
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            instagram_poster.execute_scheduled_ladies_instagram_post(cur_stores, slot_label="19:45")
    except Exception as err:
        logger.error(f"[worker] Scheduled Instagram ladies post failed: {err}")


def _ig_reel_scheduled_job(slot_lbl="21:15"):
    """Scheduled Wed/Thu/Fri/Sat Reel post (4-template auto rotation + human jitter + Feed grid protection)."""
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("ig_reel", slot_lbl):
            return
        import instagram_poster
        if not instagram_poster.is_reel_auto_schedule_enabled(allow_blocking=False):
            return
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            instagram_poster.publish_live_reel_to_instagram(
                cur_stores,
                reel_type="auto",
                slot_label=slot_lbl,
                force=False,
            )
    except Exception as err:
        logger.error(f"[worker] Scheduled Instagram Reel post failed: {err}")


def _tiktok_inbox_scheduled_job(slot_lbl="20:35"):
    """Scheduled Wed/Fri/Sat Phase 2 TikTok Inbox push (zero overhead when token or toggle is off)."""
    try:
        import tiktok_poster
        if not tiktok_poster.is_tiktok_auto_schedule_enabled(allow_blocking=False):
            return
        if not tiktok_poster.is_tiktok_token_configured():
            return
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("tiktok", slot_lbl):
            return
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            tiktok_poster.execute_scheduled_tiktok_inbox_post(cur_stores, slot_label=slot_lbl, force=False)
    except Exception as err:
        logger.error(f"[worker] Scheduled TikTok Inbox post failed: {err}")


def _crawl_events_job():
    try:
        import data_audit
        import events
        evs = events.fetch_all_events(force_refresh=True)
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
        data_audit.run_data_audit(live_stores=cur_stores, all_events=evs, send_alerts=True)
    except Exception as err:
        logger.error(f"[worker] Scheduled events crawl failed: {err}")


def _scheduled_articles_release_job():
    try:
        scheduled_articles.check_and_announce_newly_published()
    except Exception as err:
        logger.error(f"[worker] Scheduled articles release check failed: {err}")


def _bonus_alert_check_job():
    try:
        import bonus_alert
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            bonus_alert.check_and_post_bonus_alert(cur_stores)
    except Exception as err:
        logger.error(f"[worker] Bonus alert check failed: {err}")


def _pdca_autonomous_job():
    try:
        import pdca_engine
        with app_module.data_lock:
            cur_stores = list(app_module.latest_data.get("full_data") or [])
        pdca_engine.run_pdca_cycle(
            live_stores=cur_stores,
            days=14,
            apply_actions=True,
            use_ai=True,
            record_log=True,
        )
    except Exception as err:
        logger.error(f"[worker] Autonomous PDCA cycle failed: {err}")


def _sheets_retention_job():
    try:
        import archive_data
        archive_data.run_scheduled_retention_job()
    except Exception as err:
        logger.error(f"[worker] Scheduled Google Sheets retention job failed: {err}")


def _daily_token_and_db_maintenance_job():
    try:
        import instagram_poster
        import threads_poster
        if hasattr(threads_poster, "maybe_refresh_threads_token"):
            threads_poster.maybe_refresh_threads_token()
        if hasattr(instagram_poster, "maybe_refresh_ig_token"):
            instagram_poster.maybe_refresh_ig_token()
    except Exception as err:
        logger.warning(f"[worker] Daily Meta token auto-refresh check warning: {err}")
    try:
        local_db.prune_old_logs(monitor_days=60, vote_days=14)
    except Exception as err:
        logger.warning(f"[worker] Daily DB prune warning: {err}")


def _prewarm_slot_job(slot_label: str = None, include_column: bool = False):
    try:
        slots = (slot_label,) if slot_label else None
        res = app_module.precompute_editorial_and_media_bundle(
            channels=("x", "threads", "instagram"),
            slot_labels=slots,
            include_column=include_column,
            include_pdca=False,
            include_ig_slides=True,
        )
        logger.info(
            f"[worker] Precomputed editorial & IG media bundle (slot={slot_label or 'auto'}): "
            f"slides={res.get('ig_slides_rendered')}, warmed={res.get('editorial_warmed')}, ms={res.get('elapsed_ms')}"
        )
    except Exception as err:
        logger.warning(f"[worker] Precompute bundle warning: {err}")


def build_worker_scheduler() -> BackgroundScheduler:
    """Construct the dedicated worker APScheduler with scraping, precomputation, and all SNS cron jobs."""
    scheduler = BackgroundScheduler(
        timezone="UTC",
        executors={
            "default": APSThreadPoolExecutor(8),
            "sns_ai": APSThreadPoolExecutor(6),
        },
        job_defaults={
            "misfire_grace_time": 600,
            "coalesce": True,
            "max_instances": 1,
        },
    )

    # 1. Core live 53-store scraping + Redis snapshot broadcast
    scheduler.add_job(id="live_scrape_1m", func=app_module.update_job, trigger="interval", minutes=1)
    scheduler.add_job(id="analytics_flush_10m", func=_flush_analytics_safe, trigger="interval", minutes=10)
    scheduler.add_job(
        id="sheets_backfill_2h",
        func=lambda: local_db.sync_from_google_sheets(days=2),
        trigger="interval",
        hours=2,
    )

    # 2. Precomputation jobs (8 minutes before each SNS slot + rolling 30-min refresh for instant staff board)
    scheduler.add_job(
        id="prewarm_rolling_30m",
        func=_prewarm_slot_job,
        trigger="interval",
        minutes=30,
        executor="sns_ai",
    )
    # 18:37 JST (09:37 UTC) -> before Fri IG 18:45
    scheduler.add_job(id="prewarm_1837", func=_prewarm_slot_job, trigger="cron", hour=9, minute=37, args=["20:00"], executor="sns_ai")
    # 19:22 JST (10:22 UTC) -> before Ladies 19:30 / 19:45 & X 20:00
    scheduler.add_job(id="prewarm_1922", func=_prewarm_slot_job, trigger="cron", hour=10, minute=22, args=["20:00"], executor="sns_ai")
    # 19:52 JST (10:52 UTC) -> fresh live headcount draft right before X 20:00
    scheduler.add_job(id="prewarm_1952", func=_prewarm_slot_job, trigger="cron", hour=10, minute=52, args=["20:00"], executor="sns_ai")
    # 21:37 JST (12:37 UTC) -> right before IG 21:45 & X/Threads 22:00 peak
    scheduler.add_job(id="prewarm_2137", func=_prewarm_slot_job, trigger="cron", hour=12, minute=37, args=["22:00"], executor="sns_ai")
    # 23:52 JST (14:52 UTC) -> right before Fri/Sat X 00:00 midnight wave
    scheduler.add_job(id="prewarm_2352", func=_prewarm_slot_job, trigger="cron", day_of_week="fri,sat", hour=14, minute=52, args=["00:00"], executor="sns_ai")

    # 3. Scheduled X / Threads / Instagram / Note / Bonus / PDCA / Maintenance jobs (with daily human timing jitter)
    scheduler.add_job(id="x_post_2000", func=_x_scheduled_job, trigger="cron", hour=11, minute="2,6,10,14,18,22,27", jitter=75, args=["20:00"], executor="sns_ai")
    scheduler.add_job(id="x_post_2200", func=_x_scheduled_job, trigger="cron", hour=13, minute="2,6,10,14,18,22,27", jitter=75, args=["22:00"], executor="sns_ai")
    scheduler.add_job(id="x_post_0000", func=_x_scheduled_job, trigger="cron", day_of_week="fri,sat", hour=15, minute="2,6,10,14,18,22,27", jitter=75, args=["00:00"], executor="sns_ai")
    scheduler.add_job(id="x_tip_1800", func=_tip_18_job, trigger="cron", hour=9, minute="2,6,10,14,18,23,28", jitter=75, executor="sns_ai")
    scheduler.add_job(id="note_rss_check", func=_note_check_job, trigger="cron", hour="3,8", minute=15, jitter=180, executor="sns_ai")
    scheduler.add_job(id="threads_daily_2200", func=_threads_daily_job, trigger="cron", hour=13, minute="5,9,13,17,21,26,32", jitter=75, executor="sns_ai")
    scheduler.add_job(id="ig_fri_1845", func=_ig_carousel_job, trigger="cron", day_of_week="fri", hour=9, minute="46,50,54,58", jitter=45, args=["18:45"], executor="sns_ai")
    scheduler.add_job(id="ig_tuesun_2145", func=_ig_carousel_job, trigger="cron", day_of_week="tue,sun", hour=12, minute="46,50,54,58", jitter=45, args=["21:45"], executor="sns_ai")
    scheduler.add_job(id="threads_ladies_1930", func=_threads_ladies_job, trigger="cron", day_of_week="wed,fri,sat", hour=10, minute="31,35,39,44,49", jitter=60, executor="sns_ai")
    scheduler.add_job(id="ig_ladies_1945", func=_ig_ladies_job, trigger="cron", day_of_week="thu", hour=10, minute="46,50,54,58", jitter=45, executor="sns_ai")
    scheduler.add_job(id="ig_reel_2115", func=_ig_reel_scheduled_job, trigger="cron", day_of_week="wed,thu,fri,sat", hour=12, minute="18,24,30,36,42,48", jitter=50, args=["21:15"], executor="sns_ai")
    scheduler.add_job(id="tiktok_inbox_2035", func=_tiktok_inbox_scheduled_job, trigger="cron", day_of_week="wed,fri,sat", hour=11, minute="39,45,51,57", jitter=50, args=["20:35"], executor="sns_ai")
    scheduler.add_job(id="events_crawl", func=_crawl_events_job, trigger="cron", hour="1,8,20", minute=0)
    scheduler.add_job(id="scheduled_articles_15m", func=_scheduled_articles_release_job, trigger="interval", minutes=15, executor="sns_ai")
    scheduler.add_job(id="bonus_alert_10m", func=_bonus_alert_check_job, trigger="interval", minutes=10, executor="sns_ai")
    scheduler.add_job(id="x_weekly_sun", func=_x_weekly_sun_job, trigger="cron", day_of_week="sun", hour=10, minute="2,7,12,18,24", jitter=75, executor="sns_ai")
    scheduler.add_job(id="pdca_cycle", func=_pdca_autonomous_job, trigger="cron", hour="8,18", minute=35, jitter=120, executor="sns_ai")
    scheduler.add_job(id="sheets_retention", func=_sheets_retention_job, trigger="cron", hour=21, minute=20, jitter=120)
    scheduler.add_job(id="daily_token_db_maint", func=_daily_token_and_db_maintenance_job, trigger="cron", hour=21, minute=35, jitter=120, executor="sns_ai")

    return scheduler


def _sns_queue_consumer_loop(stop_event: threading.Event):
    """Continuously drain `/api/sns-jobs` tasks enqueued from the master dashboard or `?async=1` triggers."""
    logger.info("[worker] Async SNS job queue consumer started.")
    while not stop_event.is_set():
        try:
            finished_job = app_module.drain_sns_job_queue_once(timeout_sec=15.0)
            if finished_job:
                logger.info(
                    f"[worker] Completed async SNS job {finished_job.get('job_id')} "
                    f"({finished_job.get('job_type')}) -> {finished_job.get('status')}"
                )
        except Exception as err:
            logger.error(f"[worker] Queue consumer loop error: {err}")
            stop_event.wait(2.0)
    logger.info("[worker] Async SNS job queue consumer stopped.")


def _worker_staggered_warmup(stop_event: threading.Event):
    """Warm DB history, encrypted tokens, weather, events, and initial AI/IG media bundle after boot."""
    try:
        import events as _ev_boot
        has_disk_events = os.path.exists(_ev_boot.CACHE_FILE)
        if has_disk_events:
            _ev_boot.fetch_all_events(force_refresh=False, allow_blocking=False)

        if stop_event.wait(3.0):
            return
        local_db.sync_from_google_sheets(days=10, on_recent_synced=app_module.refresh_momentum_from_db)

        if stop_event.wait(2.0):
            return
        local_db.restore_votes_from_sheets(hours=24)
        try:
            import affiliate_manager as _afm
            import analytics as _an
            import archive_data as _ard
            import job_state as _js
            import sns_editorial as _se
            _js.all_keys()
            _afm._load_overrides(allow_blocking=True)
            _ard.get_retention_status(allow_blocking=True)
            _an._refresh_sheet_rows_async()
            _se.fetch_live_weather_context(allow_blocking=True)
        except Exception as warm_err:
            logger.warning(f"[worker] State/token warmup warning: {warm_err}")

        # Precompute IG 5-slide images + AI editorial drafts immediately so `web` workers hit 100% warm cache
        if stop_event.wait(2.0):
            return
        _prewarm_slot_job(slot_label=None, include_column=True)

        if stop_event.wait(15.0 if has_disk_events else 8.0):
            return
        _crawl_events_job()
        import pdca_engine as _pdca_boot
        if not _pdca_boot.get_active_directives(allow_blocking=True):
            with app_module.data_lock:
                boot_stores = list(app_module.latest_data.get("full_data") or [])
            _pdca_boot.run_pdca_cycle(live_stores=boot_stores, days=14, apply_actions=True, use_ai=False, record_log=True)
    except Exception as err:
        logger.error(f"[worker] Staggered warmup error: {err}")


def run_worker():
    stop_event = threading.Event()

    def _handle_sig(signum, _frame):
        logger.info(f"[worker] Received signal {signum}; initiating graceful shutdown...")
        stop_event.set()

    signal.signal(signal.SIGTERM, _handle_sig)
    signal.signal(signal.SIGINT, _handle_sig)

    logger.info(f"[worker] Booting Aiseki Navi dedicated worker: {turbo_store.get_backend_status()}")
    local_db.init_db()
    turbo_store.init_postgres_aux_schema()
    app_module.restore_latest_data_if_empty()

    # Priority #1: fetch live 53-store headcounts immediately and broadcast to Redis
    try:
        app_module.update_job(force=True)
    except Exception as err:
        logger.error(f"[worker] Initial live fetch error: {err}")

    consumer_thread = threading.Thread(
        target=_sns_queue_consumer_loop,
        args=(stop_event,),
        name="sns-job-consumer",
        daemon=True,
    )
    consumer_thread.start()

    scheduler = build_worker_scheduler()
    scheduler.start()
    logger.info(f"[worker] APScheduler started with {len(scheduler.get_jobs())} jobs.")

    warmup_thread = threading.Thread(
        target=_worker_staggered_warmup,
        args=(stop_event,),
        name="worker-warmup",
        daemon=True,
    )
    warmup_thread.start()

    try:
        while not stop_event.is_set():
            stop_event.wait(5.0)
    finally:
        logger.info("[worker] Shutting down scheduler and flushing analytics...")
        try:
            scheduler.shutdown(wait=False)
        except Exception:
            pass
        _flush_analytics_safe()
        logger.info("[worker] Shutdown complete.")


if __name__ == "__main__":
    run_worker()
