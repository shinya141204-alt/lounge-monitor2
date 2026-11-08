"""Dedicated Background Engine (`APP_ROLE=worker`) for Aiseki Navi (Plan Take).

Responsibilities isolated from HTTP Gunicorn `web` workers:
  1. Every-minute scraping of all 63 stores + PostgreSQL persistence + `<0.5ms` Redis live snapshot broadcast
  2. Continuous async SNS job queue consumer (`claim_next_sns_job` -> `execute_sns_job_payload`)
  3. Pre-scheduled AI editorial & 5-slide IG carousel precomputation (8 minutes before every SNS slot + every 30m)
  4. All scheduled X / Threads / Instagram / Note / Bonus / Autonomous PDCA / Meta 60-day token maintenance jobs
"""

import logging
import os
import signal
import threading

# Force worker role and prevent `app.py` from auto-starting its legacy embedded scheduler on import
os.environ.setdefault("APP_ROLE", "worker")
_was_disable_scheduler = os.environ.get("DISABLE_SCHEDULER")
# Ensure app.py module import does not trigger its embedded legacy BackgroundScheduler
os.environ["DISABLE_SCHEDULER"] = "1"

from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.combining import OrTrigger
from apscheduler.triggers.cron import CronTrigger

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
    if not turbo_store.acquire_distributed_lock(f"cron:x:{slot_lbl}", ttl_sec=55, fail_open=False):
        return
    try:
        import ops_governance
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("x", slot_lbl):
            return
        with app_module.data_lock:
            cur_stores = ops_governance.filter_visible_stores(list(app_module.latest_data.get("full_data") or []))
        skip_pace, pace_reason = sns_editorial.should_skip_slot_for_human_pacing("x", slot_lbl, stores=cur_stores)
        if skip_pace:
            logger.info(f"[worker] Skipping X {slot_lbl} for human pacing: {pace_reason}")
            return
        app_module.run_scheduled_x_post(slot_lbl)
    except Exception as err:
        logger.error(f"[worker] Scheduled X post ({slot_lbl}) failed: {err}")


def _x_weekly_sun_job():
    if not turbo_store.acquire_distributed_lock("cron:x_weekly", ttl_sec=90, fail_open=False):
        return
    try:
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("x_weekly", "19:00"):
            return
        app_module.run_scheduled_weekly_x_post()
    except Exception as err:
        logger.error(f"[worker] Scheduled weekly X post failed: {err}")


def _tip_18_job():
    if not turbo_store.acquire_distributed_lock("cron:x_tip:1800", ttl_sec=55, fail_open=False):
        return
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
    if not turbo_store.acquire_distributed_lock("cron:note_check", ttl_sec=120, fail_open=False):
        return
    try:
        import note_poster
        note_poster.check_and_post_new_note()
    except Exception as err:
        logger.error(f"[worker] Scheduled note promotion check failed: {err}")


def _threads_daily_job():
    if not turbo_store.acquire_distributed_lock("cron:threads:2200", ttl_sec=55, fail_open=False):
        return
    try:
        import ops_governance
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("threads", "22:00"):
            return
        import threads_poster
        with app_module.data_lock:
            cur_stores = ops_governance.filter_visible_stores(list(app_module.latest_data.get("full_data") or []))
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


def _threads_ladies_job():
    if not turbo_store.acquire_distributed_lock("cron:threads_ladies:1930", ttl_sec=55, fail_open=False):
        return
    try:
        import ops_governance
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("threads_ladies", "19:30"):
            return
        import threads_poster
        with app_module.data_lock:
            cur_stores = ops_governance.filter_visible_stores(list(app_module.latest_data.get("full_data") or []))
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            threads_poster.execute_scheduled_ladies_threads_post(cur_stores, slot_label="19:30")
    except Exception as err:
        logger.error(f"[worker] Scheduled Threads ladies post failed: {err}")


def _ig_reel_scheduled_job(slot_lbl="20:00"):
    """Scheduled Plan B 2x/day IG Reel post (daily 20:00 + active-night 22:50, 5-template rotation + grid protection)."""
    if not turbo_store.acquire_distributed_lock(f"cron:ig_reel:{slot_lbl}", ttl_sec=120, fail_open=False):
        return
    try:
        import ops_governance
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("ig_reel", slot_lbl):
            return
        import instagram_poster
        if not instagram_poster.is_reel_auto_schedule_enabled(allow_blocking=False):
            return
        with app_module.data_lock:
            cur_stores = ops_governance.filter_visible_stores(list(app_module.latest_data.get("full_data") or []))
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
    if not turbo_store.acquire_distributed_lock(f"cron:tiktok:{slot_lbl}", ttl_sec=120, fail_open=False):
        return
    try:
        import ops_governance
        import tiktok_poster
        if not tiktok_poster.is_tiktok_auto_schedule_enabled(allow_blocking=False):
            return
        if not tiktok_poster.is_tiktok_token_configured():
            return
        import sns_editorial
        if not sns_editorial.should_fire_scheduled_slot_now("tiktok", slot_lbl):
            return
        with app_module.data_lock:
            cur_stores = ops_governance.filter_visible_stores(list(app_module.latest_data.get("full_data") or []))
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            tiktok_poster.execute_scheduled_tiktok_inbox_post(cur_stores, slot_label=slot_lbl, force=False)
    except Exception as err:
        logger.error(f"[worker] Scheduled TikTok Inbox post failed: {err}")


def _crawl_events_job():
    # Idempotent but heavy (60+ external pages): avoid duplicate crawls during rolling deploys (fail-open on Redis outage)
    if not turbo_store.acquire_distributed_lock("cron:events_crawl", ttl_sec=900):
        return
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
    if not turbo_store.acquire_distributed_lock("cron:bonus_alert", ttl_sec=55, fail_open=False):
        return
    try:
        import bonus_alert
        import ops_governance
        with app_module.data_lock:
            cur_stores = ops_governance.filter_visible_stores(list(app_module.latest_data.get("full_data") or []))
            is_seed = bool(app_module.latest_data.get("is_seed"))
        if cur_stores and not is_seed:
            bonus_alert.check_and_post_bonus_alert(cur_stores)
    except Exception as err:
        logger.error(f"[worker] Bonus alert check failed: {err}")


def _web_push_watch_job():
    if not turbo_store.acquire_distributed_lock("cron:web_push_watch", ttl_sec=90):
        return
    try:
        import ops_governance
        import push_watch
        with app_module.data_lock:
            cur_stores = ops_governance.filter_visible_stores(list(app_module.latest_data.get("full_data") or []))
            is_seed = bool(app_module.latest_data.get("is_seed"))
            last_upd = app_module.latest_data.get("last_updated")
        if cur_stores and not is_seed:
            res = push_watch.dispatch_matching_watch_pushes(cur_stores, last_updated=last_upd)
            if res.get("sent"):
                logger.info(f"[worker] Web Push watch dispatched: {res}")
    except Exception as err:
        logger.error(f"[worker] Web Push watch dispatch failed: {err}")


def _pdca_autonomous_job():
    if not turbo_store.acquire_distributed_lock("cron:pdca_cycle", ttl_sec=180):
        return
    try:
        import ops_governance
        import pdca_engine
        with app_module.data_lock:
            cur_stores = ops_governance.filter_visible_stores(list(app_module.latest_data.get("full_data") or []))
        pdca_engine.run_pdca_cycle(
            live_stores=cur_stores,
            days=14,
            apply_actions=True,
            use_ai=False,
            use_ai_for_replenish=True,
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
        import tiktok_poster
        if hasattr(threads_poster, "maybe_refresh_threads_token"):
            threads_poster.maybe_refresh_threads_token()
        if hasattr(instagram_poster, "maybe_refresh_ig_token"):
            instagram_poster.maybe_refresh_ig_token()
        if hasattr(tiktok_poster, "refresh_tiktok_token_if_needed"):
            tiktok_poster.refresh_tiktok_token_if_needed(force=False)
    except Exception as err:
        logger.warning(f"[worker] Daily Meta/TikTok token auto-refresh check warning: {err}")
    try:
        import arena_calendar
        arena_calendar.get_arena_schedule_events(allow_blocking=True, force_refresh=True)
    except Exception as err:
        logger.warning(f"[worker] Daily arena calendar refresh warning: {err}")
    try:
        local_db.rollup_store_hourly_stats(days_back=4)
        local_db.prune_old_logs(monitor_days=90, vote_days=14)
        turbo_store.prune_old_analytics_hourly(keep_days=90)
    except Exception as err:
        logger.warning(f"[worker] Daily DB rollup/prune warning: {err}")


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

    # 2. Scheduled X / Threads / Instagram Reel (Plan B: daily 20:00 + active-night 22:50) / Note / Bonus / PDCA / Maintenance jobs
    #    Note: Unconditional background AI prewarm jobs (`prewarm_rolling_30m` etc.) and IG Carousel/Feed cron jobs
    #    are intentionally disabled so Claude Opus runs ONLY on actual live post dispatches.
    scheduler.add_job(id="x_post_2000", func=_x_scheduled_job, trigger="cron", hour=11, minute="2,6,10,14,18,22,27", jitter=75, args=["20:00"], executor="sns_ai")
    scheduler.add_job(id="x_post_2200", func=_x_scheduled_job, trigger="cron", hour=13, minute="2,6,10,14,18,22,27", jitter=75, args=["22:00"], executor="sns_ai")
    scheduler.add_job(id="x_post_0000", func=_x_scheduled_job, trigger="cron", day_of_week="fri,sat", hour=15, minute="2,6,10,14,18,22,27", jitter=75, args=["00:00"], executor="sns_ai")
    scheduler.add_job(id="x_tip_1800", func=_tip_18_job, trigger="cron", hour=9, minute="2,6,10,14,18,23,28", jitter=75, executor="sns_ai")
    # Check note RSS during calm midday (12:20 JST) and mid-afternoon (15:20 JST) windows, well clear of 18:00 Tips & 20:00-22:00 rush
    scheduler.add_job(id="note_rss_check", func=_note_check_job, trigger="cron", hour="3,6", minute=20, jitter=240, executor="sns_ai")
    scheduler.add_job(id="threads_daily_2200", func=_threads_daily_job, trigger="cron", hour=13, minute="5,9,13,17,21,26,32", jitter=75, executor="sns_ai")
    scheduler.add_job(id="threads_ladies_1930", func=_threads_ladies_job, trigger="cron", day_of_week="wed,fri,sat", hour=10, minute="31,35,39,44,49", jitter=60, executor="sns_ai")
    scheduler.add_job(id="ig_reel_2000", func=_ig_reel_scheduled_job, trigger="cron", hour=11, minute="4,10,16,22,28,34,40", jitter=50, args=["20:00"], executor="sns_ai")
    scheduler.add_job(
        id="ig_reel_2250",
        func=_ig_reel_scheduled_job,
        trigger=OrTrigger([
            CronTrigger(hour=13, minute="54", jitter=45, timezone="UTC"),
            CronTrigger(hour=14, minute="0,6,12,18,24,30", jitter=50, timezone="UTC"),
        ]),
        args=["22:50"],
        executor="sns_ai",
    )
    scheduler.add_job(id="tiktok_inbox_2035", func=_tiktok_inbox_scheduled_job, trigger="cron", day_of_week="wed,fri,sat", hour=11, minute="39,45,51,57", jitter=50, args=["20:35"], executor="sns_ai")
    scheduler.add_job(id="events_crawl", func=_crawl_events_job, trigger="cron", hour="1,8,20", minute=0)
    scheduler.add_job(id="scheduled_articles_15m", func=_scheduled_articles_release_job, trigger="interval", minutes=15, executor="sns_ai")
    scheduler.add_job(id="bonus_alert_10m", func=_bonus_alert_check_job, trigger="interval", minutes=10, executor="sns_ai")
    scheduler.add_job(id="web_push_watch_2m", func=_web_push_watch_job, trigger="interval", minutes=2)
    scheduler.add_job(id="x_weekly_sun", func=_x_weekly_sun_job, trigger="cron", day_of_week="sun", hour=10, minute="2,7,12,18,24", jitter=75, executor="sns_ai")
    scheduler.add_job(id="pdca_cycle", func=_pdca_autonomous_job, trigger="cron", hour="8,18", minute=35, jitter=120, executor="sns_ai")
    scheduler.add_job(id="sheets_retention", func=_sheets_retention_job, trigger="cron", hour=21, minute=20, jitter=120)
    scheduler.add_job(id="daily_token_db_maint", func=_daily_token_and_db_maintenance_job, trigger="cron", hour=21, minute=35, jitter=120, executor="sns_ai")

    return scheduler


SNS_ORPHAN_RUNNING_MINUTES = 15


def _rescue_orphaned_sns_jobs(max_age_minutes: int = SNS_ORPHAN_RUNNING_MINUTES) -> int:
    """
    On boot, mark SNS jobs stuck in `running` for >= 15 minutes (worker crashed / redeployed mid-run) as `failed`.
    They are NOT re-executed automatically: SNS posting is non-idempotent and may already have been published,
    so the operator re-triggers from the dashboard after checking the platform.
    """
    import datetime as _dt
    jst = _dt.timezone(_dt.timedelta(hours=9))
    now_naive = _dt.datetime.now(jst).replace(tzinfo=None)
    rescued = 0
    try:
        jobs = turbo_store.list_recent_sns_jobs(limit=50) or []
    except Exception as err:
        logger.warning(f"[worker] orphan SNS job scan skipped: {err}")
        return 0
    for job in jobs:
        if not isinstance(job, dict) or job.get("status") != "running":
            continue
        started = str(job.get("started_at") or job.get("created_at") or "")[:19]
        try:
            started_dt = _dt.datetime.strptime(started, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            continue
        if (now_naive - started_dt).total_seconds() < max_age_minutes * 60:
            continue
        try:
            turbo_store.complete_sns_job(
                job.get("job_id"),
                result=None,
                error=f"orphaned: still running after {max_age_minutes}m at worker boot (not auto-retried; verify platform before re-trigger)",
            )
            rescued += 1
        except Exception as err:
            logger.warning(f"[worker] failed to finalize orphan SNS job {job.get('job_id')}: {err}")
    if rescued:
        logger.warning(f"[worker] Finalized {rescued} orphaned SNS job(s) stuck in running state.")
    return rescued


def _sns_queue_consumer_loop(stop_event: threading.Event):
    """Continuously drain `/api/sns-jobs` tasks enqueued from the master dashboard or `?async=1` triggers."""
    logger.info("[worker] Async SNS job queue consumer started.")
    while not stop_event.is_set():
        try:
            finished_job = app_module.drain_sns_job_queue_once(timeout_sec=2.0)
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
        try:
            local_db.get_all_stores_live_benchmarks(allow_blocking=True)
            local_db.backfill_forecast_accuracy_from_history(days=21, min_existing=80, max_rows=4500)
            local_db._refresh_forecast_caches_from_db(days=21)
        except Exception as fc_warm_err:
            logger.warning(f"[worker] Forecast accuracy warmup warning: {fc_warm_err}")

        if stop_event.wait(2.0):
            return
        local_db.restore_votes_from_sheets(hours=24)
        try:
            import affiliate_manager as _afm
            import analytics as _an
            import archive_data as _ard
            import arena_calendar as _ac
            import job_state as _js
            import sns_editorial as _se
            _js.all_keys()
            _afm._load_overrides(allow_blocking=True)
            _ard.get_retention_status(allow_blocking=True)
            _an._refresh_sheet_rows_async()
            _se.fetch_live_weather_context(allow_blocking=True)
            _ac.get_arena_schedule_events(allow_blocking=True)
            local_db.rollup_store_hourly_stats(days_back=3)
        except Exception as warm_err:
            logger.warning(f"[worker] State/token warmup warning: {warm_err}")

        # Note: Do NOT precompute AI editorial / column drafts on worker boot to prevent unnecessary Claude Opus token burn.
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
    _rescue_orphaned_sns_jobs()

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
