"""scheduler_jobs.py - Legacy single-process APScheduler job registration extracted from app.py (S1)."""

import atexit
import logging
import os
import threading
import time


def boot_legacy_scheduler(core) -> None:
    """Boot APScheduler jobs and staggered background warmup in legacy/all role mode."""
    try:
        from apscheduler.executors.pool import ThreadPoolExecutor as APSThreadPoolExecutor
        local_db = core.local_db
        local_db.init_db()
        core.restore_latest_data_if_empty()
        local_db.refresh_forecast_caches_async(days=21)
        local_db.trigger_forecast_backfill_async(days=21, min_existing=80, max_rows=4500)

        scheduler = core.BackgroundScheduler(
            timezone="UTC",
            executors={
                'default': APSThreadPoolExecutor(8),
                'sns_ai': APSThreadPoolExecutor(4),
            },
            job_defaults={
                'misfire_grace_time': 600,
                'coalesce': True,
                'max_instances': 1,
            },
        )
        scheduler.add_job(func=core.update_job, trigger="interval", minutes=1)

        def _flush_analytics():
            try:
                import analytics
                analytics.flush()
            except Exception as err:
                logging.error(f"analytics flush failed: {err}")

        atexit.register(_flush_analytics)
        scheduler.add_job(func=_flush_analytics, trigger="interval", minutes=10)
        scheduler.add_job(func=lambda: local_db.sync_from_google_sheets(days=2), trigger="interval", hours=2)

        # Core scraping / analytics jobs are registered and started before optional SNS modules
        # so an import/registration error in any single SNS module cannot stop live monitoring.
        scheduler.start()

        def _visible_live_stores():
            with core.data_lock:
                cur_copy = list(core.latest_data.get('full_data') or [])
                cur_seed = bool(core.latest_data.get('is_seed'))
            try:
                import ops_governance
                return ops_governance.filter_visible_stores(cur_copy), cur_seed
            except Exception:
                return cur_copy, cur_seed

        def _crawl_events_job():
            try:
                import events
                import data_audit
                evs = events.fetch_all_events(force_refresh=True)
                with core.data_lock:
                    cur_stores = list(core.latest_data.get('full_data') or [])
                data_audit.run_data_audit(live_stores=cur_stores, all_events=evs, send_alerts=True)
            except Exception as err:
                logging.error(f"Scheduled events crawl failed: {err}")

        def _safe_register(label: str, fn) -> None:
            try:
                fn()
            except Exception as err:
                logging.error(f"[scheduler_jobs] failed to register {label}: {err}")

        def _reg_x_jobs():
            import sns_editorial
            def _x_data_law_wrap(slot_lbl="19:15"):
                if not sns_editorial.should_fire_scheduled_slot_now("x_data_law", slot_lbl):
                    return
                core.run_scheduled_x_data_law_post(slot_lbl)

            def _x_search_wrap(slot_lbl="22:00"):
                if not sns_editorial.should_fire_scheduled_slot_now("x_search", slot_lbl):
                    return
                core.run_scheduled_x_search_catcher_post(slot_lbl)

            def _x_weekly_sun_fallback_job():
                try:
                    if sns_editorial.should_fire_scheduled_slot_now("x_weekly", "19:00"):
                        core.run_scheduled_weekly_x_post(force=False)
                except Exception as err:
                    logging.error(f"Scheduled weekly X post failed: {err}")

            scheduler.add_job(func=_x_data_law_wrap, trigger="cron", day_of_week="mon,tue,wed,thu,fri", hour=10, minute="17,21,25,29,33,38,42", jitter=60, args=["19:15"], executor="sns_ai")
            scheduler.add_job(func=_x_search_wrap, trigger="cron", day_of_week="fri,sat", hour=13, minute="2,6,10,14,18,22,27", jitter=60, args=["22:00"], executor="sns_ai")
            scheduler.add_job(func=_x_weekly_sun_fallback_job, trigger="cron", day_of_week="sun", hour=10, minute="2,7,12,18,24", jitter=75, executor="sns_ai")

        def _reg_note_job():
            import note_poster
            def _note_check_job():
                try:
                    note_poster.check_and_post_new_note()
                except Exception as err:
                    logging.error(f"Scheduled note promotion check failed: {err}")
            scheduler.add_job(func=_note_check_job, trigger="cron", hour="3,6", minute=20, jitter=240, executor="sns_ai")

        def _reg_threads_job():
            import sns_editorial
            import threads_poster
            def _threads_conv_job():
                try:
                    if not sns_editorial.should_fire_scheduled_slot_now("threads_conv", "21:15"):
                        return
                    cur_stores, _ = _visible_live_stores()
                    threads_poster.execute_scheduled_conversational_threads_post(cur_stores, slot_label="21:15")
                except Exception as err:
                    logging.error(f"Scheduled conversational Threads post failed: {err}")
            scheduler.add_job(func=_threads_conv_job, trigger="cron", day_of_week="tue,thu,fri,sun", hour=12, minute="18,22,26,30,34,39,43", jitter=60, executor="sns_ai")

        def _reg_ig_reel_job():
            import sns_editorial
            import instagram_poster
            from apscheduler.triggers.combining import OrTrigger
            from apscheduler.triggers.cron import CronTrigger
            def _ig_reel_scheduled_job(slot_lbl="20:00"):
                try:
                    if not sns_editorial.should_fire_scheduled_slot_now("ig_reel", slot_lbl):
                        return
                    cur_stores, is_seed = _visible_live_stores()
                    if instagram_poster.is_reel_auto_schedule_enabled(allow_blocking=False):
                        import moshimo_reel_queue
                        moshimo_reel_queue.dispatch_scheduled_ig_reel(cur_stores, slot_label=slot_lbl, is_seed=is_seed)
                    if slot_lbl == "20:00":
                        import youtube_poster
                        youtube_poster.dispatch_scheduled_youtube_short(stores=cur_stores, slot_label=slot_lbl, is_seed=is_seed)
                except Exception as err:
                    logging.error(f"Scheduled short-video post failed: {err}")
            scheduler.add_job(func=_ig_reel_scheduled_job, trigger="cron", hour=11, minute="4,10,16,22,28,34,40", jitter=50, args=["20:00"], executor="sns_ai")
            scheduler.add_job(func=_ig_reel_scheduled_job, trigger=OrTrigger([CronTrigger(hour=13, minute="54", jitter=45, timezone="UTC"), CronTrigger(hour=14, minute="0,6,12,18,24,30", jitter=50, timezone="UTC")]), args=["22:50"], executor="sns_ai")

        def _reg_tiktok_job():
            import sns_editorial
            import tiktok_poster
            def _tiktok_inbox_job(slot_lbl="20:35"):
                try:
                    if not tiktok_poster.is_tiktok_auto_schedule_enabled(allow_blocking=False) or not tiktok_poster.is_tiktok_token_configured() or not sns_editorial.should_fire_scheduled_slot_now("tiktok", slot_lbl):
                        return
                    cur_stores, is_seed = _visible_live_stores()
                    if cur_stores and not is_seed:
                        tiktok_poster.execute_scheduled_tiktok_inbox_post(cur_stores, slot_label=slot_lbl, force=False)
                except Exception as err:
                    logging.error(f"Scheduled TikTok Inbox post failed: {err}")
            scheduler.add_job(func=_tiktok_inbox_job, trigger="cron", day_of_week="wed,fri,sat", hour=11, minute="39,45,51,57", jitter=50, args=["20:35"], executor="sns_ai")

        def _reg_events_and_articles_jobs():
            scheduler.add_job(func=_crawl_events_job, trigger="cron", hour="1,8,20", minute=0)
            def _scheduled_articles_release_job():
                try:
                    core.scheduled_articles.check_and_announce_newly_published()
                except Exception as err:
                    logging.error(f"Scheduled articles release check failed: {err}")
            scheduler.add_job(func=_scheduled_articles_release_job, trigger="interval", minutes=15, executor="sns_ai")

        def _reg_insights_and_pdca_jobs():
            import sns_insights_loop
            import pdca_engine
            scheduler.add_job(func=sns_insights_loop.run_scheduled_sync_safe, trigger="cron", hour="8,18", minute=25, jitter=90, executor="sns_ai")
            def _pdca_autonomous_job():
                try:
                    cur_stores, _ = _visible_live_stores()
                    pdca_engine.run_pdca_cycle(
                        live_stores=cur_stores,
                        days=14,
                        apply_actions=True,
                        use_ai=False,
                        use_ai_for_replenish=True,
                        record_log=True,
                    )
                except Exception as err:
                    logging.error(f"Autonomous PDCA cycle failed: {err}")
            scheduler.add_job(func=_pdca_autonomous_job, trigger="cron", hour="8,18", minute=35, jitter=120, executor="sns_ai")

        def _reg_maintenance_jobs():
            def _sheets_retention_job():
                try:
                    import archive_data
                    archive_data.run_scheduled_retention_job()
                except Exception as err:
                    logging.error(f"Scheduled Google Sheets retention job failed: {err}")
            scheduler.add_job(func=_sheets_retention_job, trigger="cron", hour=21, minute=20, jitter=120)

            def _daily_token_and_db_maintenance_job():
                try:
                    import threads_poster
                    import instagram_poster
                    import tiktok_poster
                    if hasattr(threads_poster, 'maybe_refresh_threads_token'):
                        threads_poster.maybe_refresh_threads_token()
                    if hasattr(instagram_poster, 'maybe_refresh_ig_token'):
                        instagram_poster.maybe_refresh_ig_token()
                    if hasattr(tiktok_poster, 'refresh_tiktok_token_if_needed'):
                        tiktok_poster.refresh_tiktok_token_if_needed(force=False)
                except Exception as err:
                    logging.warning(f"Daily Meta/TikTok token auto-refresh check warning: {err}")
                try:
                    import arena_calendar
                    arena_calendar.get_arena_schedule_events(allow_blocking=True, force_refresh=True)
                except Exception as err:
                    logging.warning(f"Daily arena schedule refresh warning: {err}")
                try:
                    try:
                        rollup_res = local_db.rollup_store_hourly_stats(days_back=4)
                    except Exception as rollup_err:
                        rollup_res = {"ok": False, "error": str(rollup_err)}
                    if not (isinstance(rollup_res, dict) and rollup_res.get("ok")):
                        logging.warning(f"Daily SQLite rollup returned non-ok (proceeding with prune_old_logs for 14d anonymization): {rollup_res}")
                    local_db.prune_old_logs(monitor_days=90, vote_days=14)
                except Exception as err:
                    logging.warning(f"Daily SQLite rollup/prune warning: {err}")
            scheduler.add_job(func=_daily_token_and_db_maintenance_job, trigger="cron", hour=21, minute=35, jitter=120, executor="sns_ai")

        _safe_register("x_jobs", _reg_x_jobs)
        _safe_register("note_job", _reg_note_job)
        _safe_register("threads_job", _reg_threads_job)
        _safe_register("ig_reel_job", _reg_ig_reel_job)
        _safe_register("tiktok_job", _reg_tiktok_job)
        _safe_register("events_and_articles", _reg_events_and_articles_jobs)
        _safe_register("insights_and_pdca", _reg_insights_and_pdca_jobs)
        _safe_register("maintenance", _reg_maintenance_jobs)

        # Priority #1 at t=0s: scrape live headcounts first without competing with SQLite backfill or Sheets reads
        core.trigger_background_fetch_if_needed()

        def _staggered_background_warmup():
            try:
                import events as _ev_boot
                has_disk_events = os.path.exists(_ev_boot.CACHE_FILE)
                if has_disk_events:
                    _ev_boot.fetch_all_events(force_refresh=False, allow_blocking=False)

                time.sleep(5)
                local_db.sync_from_google_sheets(days=10, on_recent_synced=core.refresh_momentum_from_db)

                time.sleep(2)
                local_db.restore_votes_from_sheets(hours=24)
                try:
                    import job_state as _js
                    import analytics as _an
                    import sns_editorial as _se
                    import affiliate_manager as _afm
                    import archive_data as _ard
                    _js.all_keys()
                    _afm._load_overrides(allow_blocking=True)
                    _ard.get_retention_status(allow_blocking=True)
                    _an._refresh_sheet_rows_async()
                    _se.fetch_live_weather_context(allow_blocking=True)
                except Exception:
                    pass

                time.sleep(30 if has_disk_events else 12)
                _crawl_events_job()
                import pdca_engine as _pdca_boot
                if not _pdca_boot.get_active_directives(allow_blocking=True):
                    with core.data_lock:
                        boot_stores = list(core.latest_data.get('full_data') or [])
                    _pdca_boot.run_pdca_cycle(live_stores=boot_stores, days=14, apply_actions=True, use_ai=False, record_log=True)
            except Exception as err:
                logging.error(f"Staggered background warmup error: {err}")

        threading.Thread(target=_staggered_background_warmup, daemon=True).start()
        logging.info("APScheduler & staggered background warmup started successfully")
    except Exception as e:
        logging.error(f"Scheduler boot error: {e}")
