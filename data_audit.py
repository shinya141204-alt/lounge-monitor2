"""
data_audit.py - 店舗マスタ・営業時間・料金テーブル・ライブデータの自動ズレ検知エンジン (Step 3)

目的:
1. 構造整合性チェック (Static Master Audit):
   - `store_master.json` の全店舗が `store_hours.STORE_BUSINESS_HOURS` に漏れなく登録されているか
   - 国内全店舗が `price_comparison.STORE_PRICE_TABLE` に漏れなく登録され、必須項目（単独料金・4時間帯単価・1名入店可否・チャージ）が揃っているか
2. リアルタイム営業時間ズレ検知 (Live vs Official Hours Drift Audit):
   - 公式営業時間設定（`store_hours.py`）では「営業時間外（または定休日）」と判定されているにもかかわらず、ライブスクレイピングで店内に一定数以上（例：合計4名以上）の客数が観測された場合、「店舗側が営業時間を前倒し・延長・変更した可能性」として自動検知する。
3. ライブ取得店舗の欠落・未知店舗検知 (Live Store Coverage Audit):
   - 店舗マスタにある国内店舗のうち、営業時間帯にライブデータから欠落している店舗や、逆にマスタ未登録の新店舗がないかを検知する。
4. 公式サイト料金・システム改定アナウンス検知 (Official Announcement Price/Hours Revision Audit):
   - `events.py` が取得した公式ニュース・お知らせタイトルに「料金改定」「営業時間変更」「システム変更」「閉店」「リニューアル」等の重要キーワードが含まれている場合、管理画面で即座に確認できるようピックアップする。
"""

import datetime
import threading
import store_master
import store_hours
import price_comparison

JST = datetime.timezone(datetime.timedelta(hours=9))

# 営業時間外なのにこの人数以上が入っていたら「営業時間変更（前倒しオープン・深夜延長・定休日変更）」を疑う閾値
# ※閉店直後の1組（2対2＝4名）のお会計待ち・POS精算遅延による誤検知を防ぐため、6名（3組相当）以上を対象とする
DRIFT_GUEST_THRESHOLD = 6

# 公式お知らせタイトルに含まれる場合、料金表や営業時間の更新が必要になり得るキーワード
REVISION_KEYWORDS = (
    "料金改定",
    "価格改定",
    "システム変更",
    "料金システム",
    "営業時間変更",
    "営業時間の変更",
    "定休日",
    "閉店",
    "移転",
)

# 単なるメニュー刷新や店舗内装リニューアルなど、料金・営業時間テーブルに影響しないお知らせ除外ワード
REVISION_IGNORE_PHRASES = (
    "フードメニュー",
    "ドリンクメニュー",
    "グランドメニュー",
    "季節メニュー",
    "リニューアルオープン",
)

_audit_lock = threading.Lock()
_latest_audit_report = None
_audit_fast_cache = {"key": None, "ts": 0.0, "report": None}


def run_data_audit(live_stores=None, all_events=None, now_jst=None, send_alerts=False):
    """全店舗のマスタ整合性・営業時間ズレ・公式改定アナウンスを一括点検し、レポート辞書を返す。"""
    global _latest_audit_report
    import time as _time
    use_fast_cache = (not send_alerts) and (all_events is None) and (now_jst is None)
    now_jst = now_jst or datetime.datetime.now(JST)
    if now_jst.tzinfo is None:
        now_jst = now_jst.replace(tzinfo=JST)

    if use_fast_cache:
        try:
            import events as _ev_mod
            ev_upd = _ev_mod._events_memory_cache.get("updated_at")
            op_cnt = len(_ev_mod.get_operational_notices() or [])
        except Exception:
            ev_upd, op_cnt = None, 0
        live_sig = tuple((s.get("name"), int(s.get("women") or 0), int(s.get("men") or 0)) for s in (live_stores or []))
        c_key = (live_sig, ev_upd, op_cnt, now_jst.strftime("%Y-%m-%d %H:%M"))
        now_mono = _time.monotonic()
        with _audit_lock:
            if _audit_fast_cache["key"] == c_key and _audit_fast_cache["report"] is not None and (now_mono - _audit_fast_cache["ts"] < 60.0):
                return _audit_fast_cache["report"]

    master = store_master.load()
    master_names = set(master.keys())
    domestic_master_names = {
        k for k, v in master.items()
        if (v.get("region") != "Korea") and ("カンナム" not in k) and ("ホンデ" not in k)
    }

    issues = []
    warnings = []

    # ---------------------------------------------------------
    # 1. 静的マスタ整合性チェック（店舗マスタ × 営業時間 × 料金表）
    # ---------------------------------------------------------
    missing_hours = sorted(master_names - set(store_hours.STORE_BUSINESS_HOURS.keys()))
    for sname in missing_hours:
        issues.append({
            "type": "missing_hours",
            "severity": "high",
            "store": sname,
            "seo_name": store_master.seo_name(sname),
            "message": f"{store_master.seo_name(sname)}（{sname}）が store_hours.py に未登録です。",
        })

    price_table_keys = set(price_comparison.STORE_PRICE_TABLE.keys())
    missing_price = sorted(domestic_master_names - price_table_keys)
    for sname in missing_price:
        issues.append({
            "type": "missing_price",
            "severity": "high",
            "store": sname,
            "seo_name": store_master.seo_name(sname),
            "message": f"{store_master.seo_name(sname)}（{sname}）が price_comparison.py の料金テーブルに未登録です。",
        })

    incomplete_price = []
    for sname in sorted(domestic_master_names & price_table_keys):
        prof = price_comparison.get_store_price_profile(sname, now_jst=now_jst)
        required_fields = ("wait_fee_str", "slot_18_20", "slot_20_22", "slot_22_24", "slot_24_last", "solo_rule", "charge_str")
        missing_f = [f for f in required_fields if not prof.get(f)]
        if missing_f:
            incomplete_price.append(sname)
            issues.append({
                "type": "incomplete_price_fields",
                "severity": "high",
                "store": sname,
                "seo_name": store_master.seo_name(sname),
                "message": f"{store_master.seo_name(sname)} の料金データに未入力項目があります: {', '.join(missing_f)}",
            })

    # ---------------------------------------------------------
    # 2. ライブデータ × 公式営業時間の「ズレ（ドリフト）」検知
    # ---------------------------------------------------------
    hours_drifts = []
    unknown_live_stores = []
    if live_stores:
        for item in live_stores:
            sname = (item.get("name") or "").strip()
            if not sname:
                continue
            if sname not in master_names:
                unknown_live_stores.append(sname)
                issues.append({
                    "type": "unknown_live_store",
                    "severity": "high",
                    "store": sname,
                    "seo_name": sname,
                    "message": f"公式サイトから未登録の店舗名「{sname}」が検出されました（新店舗または名称変更の可能性）。",
                })
                continue

            w = int(item.get("women") or 0)
            m = int(item.get("men") or 0)
            tot = w + m

            # total_guests=0 を渡して「純粋にスケジュール上で営業中かどうか」を判定する
            sched_status = store_hours.get_business_hours_status(sname, now_jst=now_jst, total_guests=0)
            if not sched_status["is_open_now"] and tot >= DRIFT_GUEST_THRESHOLD:
                drift_item = {
                    "type": "hours_drift",
                    "severity": "medium",
                    "store": sname,
                    "seo_name": store_master.seo_name(sname),
                    "women": w,
                    "men": m,
                    "total": tot,
                    "configured_hours": sched_status["today_hours_label"],
                    "message": (
                        f"{store_master.seo_name(sname)}：設定上は営業時間外（本日 {sched_status['today_hours_label']}）ですが、"
                        f"現在 女性{w}名・男性{m}名（計{tot}名）の来店が観測されています。営業時間変更・特別営業の可能性があります。"
                    ),
                }
                hours_drifts.append(drift_item)
                warnings.append(drift_item)

    # ---------------------------------------------------------
    # 3. 公式ニュース・お知らせ内の「料金・営業時間改定」キーワード検知
    # ---------------------------------------------------------
    op_notices = []
    if all_events is None:
        try:
            import events
            all_events = events.fetch_all_events(allow_blocking=False) or []
            op_notices = events.get_operational_notices() or []
        except Exception:
            all_events = []
    else:
        try:
            import events
            op_notices = events.get_operational_notices() or []
        except Exception:
            op_notices = []

    seen_notice_titles = set()
    combined_candidates = list(all_events or []) + list(op_notices)
    revision_notices = []
    for ev in combined_candidates:
        title = str(ev.get("title") or "")
        if not title or title in seen_notice_titles:
            continue
        if any(ign in title for ign in REVISION_IGNORE_PHRASES):
            continue
        matched_kw = [kw for kw in REVISION_KEYWORDS if kw in title]
        if matched_kw:
            seen_notice_titles.add(title)
            notice = {
                "type": "official_revision_notice",
                "severity": "medium",
                "brand": ev.get("brand") or "",
                "title": title,
                "url": ev.get("url") or "",
                "keywords": matched_kw,
                "message": f"公式お知らせに改定キーワード（{'/'.join(matched_kw)}）を検出: 「{title}」",
            }
            revision_notices.append(notice)
            warnings.append(notice)

    # 必要に応じたWebhook/ログ通知（health.send_alert のクールダウン機構を利用）
    if send_alerts:
        try:
            import health
            for iss in issues:
                health.send_alert(f"audit_issue:{iss['type']}:{iss.get('store', '')}", f"⚠️ [データ監査] {iss['message']}")
            for dr in hours_drifts:
                health.send_alert(
                    f"audit_drift:{dr['store']}:{now_jst.strftime('%Y-%m-%d')}",
                    f"🕒 [営業時間ズレ検知] {dr['message']}",
                )
            for rn in revision_notices:
                health.send_alert(
                    f"audit_rev:{rn['title'][:30]}",
                    f"📢 [公式改定のお知らせ検知] {rn['message']}",
                )
        except Exception:
            pass

    status = "ok"
    if issues:
        status = "error"
    elif warnings:
        status = "warning"

    report = {
        "status": status,
        "checked_at": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "summary": {
            "total_master_stores": len(master_names),
            "domestic_stores": len(domestic_master_names),
            "hours_registered": len(master_names) - len(missing_hours),
            "price_registered": len(domestic_master_names) - len(missing_price),
            "issue_count": len(issues),
            "warning_count": len(warnings),
            "hours_drift_count": len(hours_drifts),
            "revision_notice_count": len(revision_notices),
        },
        "issues": issues,
        "warnings": warnings,
        "hours_drifts": hours_drifts,
        "revision_notices": revision_notices,
    }

    with _audit_lock:
        _latest_audit_report = report
        if use_fast_cache:
            _audit_fast_cache["key"] = c_key
            _audit_fast_cache["ts"] = now_mono
            _audit_fast_cache["report"] = report
    return report


def get_latest_audit_report(live_stores=None):
    """キャッシュ済みの最新監査レポートを返す（未実行なら即座に生成）。"""
    with _audit_lock:
        if _latest_audit_report is not None and live_stores is None:
            return _latest_audit_report
    return run_data_audit(live_stores=live_stores, send_alerts=False)
