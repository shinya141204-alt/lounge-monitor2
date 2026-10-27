"""
sns_workflows.py - SNS copy generation and scheduled X auto-post workflows.
Extracted from app.py (Step 5 modularization) to separate SNS orchestration from Flask HTTP routes.
"""

import datetime
import logging

import job_state
import monitor
import x_poster


def build_sns_post_payload(post_type, base_url, latest_data):
    """Build ready-to-post viral X/SNS text payload for `/api/sns-post`."""
    post_type = (post_type or 'all').lower()
    base_url = (base_url or 'https://aiseki-navi.com').rstrip('/')
    stores = [s for s in (latest_data.get('full_data') or []) if s.get('region') != 'Korea']
    ts = latest_data.get('last_updated') or ''
    time_part = ts.split(' ')[1] if ' ' in ts else '現在'

    if not stores:
        return {'status': 'loading', 'text': f'全国の相席ラウンジ人数ランキング「相席ナビ」\n{base_url}/'}

    if post_type == 'kanto':
        filtered = [s for s in stores if s.get('region') == 'Kanto']
        header = f"{time_part}時点、関東でいま女性が多く入っている上位5店舗🗼"
        tags = "#相席ナビ #相席ラウンジ #新宿 #渋谷 #オリエンタルラウンジ"
    elif post_type == 'kansai':
        filtered = [s for s in stores if s.get('region') == 'Kinki']
        header = f"{time_part}時点、関西でいま女性が多く入っている上位5店舗🐙"
        tags = "#相席ナビ #相席ラウンジ #梅田 #難波 #心斎橋"
    elif post_type == 'anaba':
        candidates = [s for s in stores if s.get('women', 0) >= 10]
        candidates.sort(key=lambda x: (x.get('women', 0) / max(1, x.get('women', 0) + x.get('men', 0)), x.get('women', 0)), reverse=True)
        filtered = candidates
        header = f"{time_part}時点、いま女性比率が高めで入りやすい穴場店舗✨"
        tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS"
    elif post_type == 'threads':
        import threads_poster
        threads_text = threads_poster.build_threads_live_post(stores, slot_label=time_part)
        return {
            'status': 'success',
            'type': post_type,
            'timestamp': ts,
            'text': threads_text,
        }
    elif post_type == 'price':
        announce_lines = [
            "【新機能】相席ラウンジ全51店舗「料金・イベント・現在人数」横並び比較ボードを公開しました📊",
            "",
            "✅ 単独（待機）料金（220〜330円/10分 or 定額パス）",
            "✅ 4つの時間帯別（〜20時/20〜22時/22〜24時/24時〜）の相席10分単価",
            "✅ 男性1名（ソロ）入店可否＆店舗別の開催中イベント",
            "✅ 席替え余地を考慮した「女性10名以上（約5組）」基準のエリア推奨判定",
            "✅ 公式営業時間に基づくオープン中の「0 : 0」表記",
            "",
            "👇全51店舗の比較ボードはこちら",
            f"{base_url}/price-comparison",
            "",
            "📖 比較ボードの詳しい見方・活用ガイド",
            f"{base_url}/guide/price-event-comparison",
            "",
            "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS #ag",
        ]
        return {
            'status': 'success',
            'type': post_type,
            'timestamp': ts,
            'text': "\n".join(announce_lines),
        }
    else:
        filtered = list(stores)
        header = f"{time_part}時点、全国でいま人が集まっている相席ラウンジTOP5🍻"
        tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #JIS #ag"

    limit = 5
    medals = ['🥇', '🥈', '🥉', '4️⃣', '5️⃣']
    lines = [header, ""]
    for i, s in enumerate(filtered[:limit]):
        w = s.get('women', 0)
        m = s.get('men', 0)
        tot = w + m
        pct = round((w / tot) * 100) if tot > 0 else 0
        ja_name = x_poster.format_ja_store_name(s.get('name') or '')
        if post_type == 'anaba':
            lines.append(f"{medals[i]} {ja_name}（女性比{pct}% ｜ 女性{w}名 / 男性{m}名）")
        else:
            lines.append(f"{medals[i]} {ja_name}（女性{w}名 / 男性{m}名・女性比{pct}%）")

    lines.extend([
        "",
        "💡 ひとことメモ",
        "1店舗目で男性待ちが長そうなときは、近くの女性比率が高めの店舗に回る方がスムーズに座れます。",
        "",
        "📖 オリエンタルラウンジ・ag・JISの違いまとめ：",
        f"{base_url}/guide/brand-comparison",
        "",
        "👇全店舗の現在人数・男女比グラフ",
        f"{base_url}/",
        "",
        tags,
    ])
    return {
        'status': 'success',
        'type': post_type,
        'timestamp': ts,
        'text': "\n".join(lines),
    }


def execute_scheduled_x_post(latest_data, slot_label="20:00", force=False, include_meta=False):
    """Execute scheduled X auto-post with duplicate prevention and live headcount safety guards."""
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    biz_date = (now_jst - datetime.timedelta(hours=7)).strftime('%Y-%m-%d')
    dedup_key = f"x_post:{biz_date}_{slot_label}"

    if not force and job_state.is_done(dedup_key):
        logging.info(f"X auto-post already sent for {dedup_key}, skipping.")
        return {"status": "skipped", "reason": "Already posted for this slot"}

    latest_data = latest_data or {}
    is_seed = bool(latest_data.get('is_seed'))
    stores = [] if is_seed else (latest_data.get('full_data') or [])
    if not stores:
        try:
            stores = monitor.get_all_data() or []
        except Exception as e:
            logging.error(f"Failed to fetch stores for X post: {e}")
            return {"status": "error", "detail": str(e)}

    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    dom_stores = [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]
    if not force:
        total_dom_women = sum(int(s.get("women") or 0) for s in dom_stores)
        top1_dom_women = max((int(s.get("women") or 0) for s in dom_stores), default=0)
        if total_dom_women < 15 or top1_dom_women < 5:
            logging.info(
                f"Skipping X auto-post ({slot_label}): insufficient live headcount "
                f"(total_women={total_dom_women}, top1_women={top1_dom_women})."
            )
            return {"status": "skipped", "reason": "Insufficient live headcount"}

    # Run 4-role SNS Editorial Room pipeline (Researcher -> Planner -> Writer -> Evaluator)
    editorial_winner = None
    try:
        import sns_editorial
        editorial_res = sns_editorial.run_editorial_pipeline(
            stores,
            slot_label=slot_label,
            now_jst=now_jst,
            record_history=not force,
            use_ai=True,
            force_ai_refresh=True,
        )
        editorial_winner = editorial_res.get("winner")
    except Exception as e:
        logging.warning(f"SNS editorial pipeline fallback to legacy builder ({slot_label}): {e}")

    if editorial_winner and editorial_winner.get("main_text"):
        text = editorial_winner["main_text"]
        reply_text = editorial_winner.get("reply_text") or x_poster.build_scheduled_reply_text(slot_label=slot_label)
    else:
        text = x_poster.build_scheduled_tweet_text(stores, slot_label=slot_label)
        if not text:
            return {"status": "error", "detail": "Empty tweet text"}
        reply_text = x_poster.build_scheduled_reply_text(slot_label=slot_label)

    png_bytes = None
    try:
        import ogp_generator
        slot_code = (slot_label or "20:00").replace(":", "")
        png_bytes = ogp_generator.generate_live_ogp_png(
            stores,
            s_param=f"{now_jst.strftime('%Y%m%d')}_{slot_code}",
            now_jst=now_jst,
        )
    except Exception as e:
        logging.warning(f"Failed to generate live OGP PNG for X post ({slot_label}): {e}")

    result = x_poster.post_to_x(
        text,
        reply_text=reply_text,
        media_bytes=png_bytes,
        media_mime_type="image/png",
    )
    if result.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
    elif result.get("status") == "error":
        try:
            import health
            health.send_alert(f"xpost:{dedup_key}", f"⚠️ X自動投稿（{slot_label}）に失敗しました: {str(result.get('detail') or result)[:200]}")
        except Exception:
            pass
    result["tweet_text"] = text
    result["reply_text"] = reply_text
    if editorial_winner:
        result["editorial_style"] = editorial_winner.get("style")
        result["editorial_score"] = editorial_winner.get("total_score")

    # Optional fallback Meta hooks (never fire on `force=True` X manual tests; normally scheduled independently in app.py)
    if include_meta and slot_label == "22:00" and not force:
        try:
            import threads_poster
            threads_res = threads_poster.execute_scheduled_threads_post(stores, slot_label=slot_label, force=False)
            result["threads_status"] = threads_res.get("status")
        except Exception as e:
            logging.warning(f"Threads auto-post skipped/failed during {slot_label}: {e}")

        if now_jst.weekday() in (1, 4, 6):
            try:
                import instagram_poster
                ig_res = instagram_poster.execute_scheduled_instagram_post(stores, mode="carousel", slot_label=slot_label, force=False)
                result["instagram_status"] = ig_res.get("status")
            except Exception as e:
                logging.warning(f"Instagram auto-post skipped/failed during {slot_label}: {e}")

    return result


def execute_scheduled_weekly_x_post(force=False):
    """Execute weekly summary X auto-post (with dynamic /ogp/weekly.png card attached to 1st post)."""
    import weekly_report
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    iso_year, iso_week, _ = now_jst.isocalendar()
    dedup_key = f"x_weekly:{iso_year}-W{iso_week:02d}"

    if not force and job_state.is_done(dedup_key):
        logging.info(f"Weekly X auto-post already sent for {dedup_key}, skipping.")
        return {"status": "skipped", "reason": "Already posted for this week"}

    try:
        report = weekly_report.generate_weekly_report()
    except Exception as e:
        logging.error(f"Failed to generate weekly report for X post: {e}")
        return {"status": "error", "detail": str(e)}

    if not report or "error" in report:
        return {"status": "error", "detail": str(report.get("error") if report else "Empty report")}

    main_text, reply_text = x_poster.build_weekly_report_tweet_thread(report)
    if not main_text:
        return {"status": "error", "detail": "Empty weekly tweet text"}

    png_bytes = None
    try:
        import ogp_generator
        png_bytes = ogp_generator.generate_weekly_ogp_png(report)
    except Exception as e:
        logging.warning(f"Failed to generate weekly OGP PNG for X post: {e}")

    result = x_poster.post_to_x(
        main_text,
        reply_text=reply_text,
        media_bytes=png_bytes,
        media_mime_type="image/png",
    )
    if result.get("status") == "success" and not force:
        job_state.mark_done(dedup_key)
    result["tweet_text"] = main_text
    result["reply_text"] = reply_text
    return result
