"""
cross_site_bridge.py - 相席ナビ × マチナビ 姉妹サイト間データ交換・2軸需給DI指数・月次凍結スナップショット

契約（AGENTS.md §1 / §3 / §4.2 / §9 準拠）:
- 公開レスポンスの店舗名はすべて `store_master.seo_name()` 変換済み（内部キー `OLG` 等を出さない）。
- `ops_governance` で非表示化された店舗・ブランドはすべての公開JSON/CSVから除外する。
- `/link/` アフィリエイトURLは交換フィードおよび統計エンドポイントに一切含めない。
- 0件（未ウォーム・DB空）のレスポンスはメモリキャッシュせず、閾値未満は `data_status="insufficient"` と明示する。
"""
from __future__ import annotations

import csv
import datetime
import io
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from typing import Any

import local_db
import ops_governance
import store_hours
import store_master
import turbo_store

_JST = datetime.timezone(datetime.timedelta(hours=9))
_CROSSWALK_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "area_crosswalk.v1.json")
_PARTNER_EXCHANGE_URL = "https://machinavi-data.com/data/exchange/area-evening.v1.json"
_ALLOWED_PARTNER_HOSTS = frozenset({"machinavi-data.com"})
_MAX_PARTNER_FEED_BYTES = 262_144
_PARTNER_PULL_TIMEOUT_SEC = 8.0
_MIN_WALKIN_STORE_HOURS = 12
_MIN_PREPAID_SAMPLES = 20

_crosswalk_lock = threading.Lock()
_crosswalk_cache: dict[str, Any] | None = None

_exchange_cache_lock = threading.Lock()
_exchange_cache: dict[str, Any] = {"key": None, "mono": 0.0, "payload": None}
_EXCHANGE_CACHE_TTL = 45.0

_di_cache_lock = threading.Lock()
_di_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_DI_CACHE_TTL = 300.0

_monthly_snap_lock = threading.Lock()
_monthly_snap_cache: dict[tuple, tuple[float, dict[str, Any]]] = {}
_MONTHLY_SNAP_TTL = 600.0

_partner_pull_lock = threading.Lock()
_last_partner_pull_mono = 0.0
_PARTNER_PULL_DEBOUNCE_SEC = 900.0


def clear_caches() -> None:
    """テストおよび手動更新時にメモリキャッシュをクリアする。"""
    global _last_partner_pull_mono
    with _exchange_cache_lock:
        _exchange_cache["key"] = None
        _exchange_cache["mono"] = 0.0
        _exchange_cache["payload"] = None
    with _di_cache_lock:
        _di_cache.clear()
    with _monthly_snap_lock:
        _monthly_snap_cache.clear()
    with _partner_pull_lock:
        _last_partner_pull_mono = 0.0


def load_crosswalk() -> dict[str, Any]:
    """`data/area_crosswalk.v1.json` を読み込む（スレッドセーフキャッシュ）。"""
    global _crosswalk_cache
    with _crosswalk_lock:
        if _crosswalk_cache is None:
            try:
                with open(_CROSSWALK_PATH, "r", encoding="utf-8") as f:
                    _crosswalk_cache = json.load(f)
            except Exception as err:
                logging.getLogger(__name__).warning("load_crosswalk failed: %s", err)
                _crosswalk_cache = {"schema_version": "1.0", "crosswalk_version": "2026-11-10.v1", "areas": []}
        return _crosswalk_cache


def _hidden_sets() -> tuple[frozenset[str], frozenset[str]]:
    try:
        hb = frozenset(ops_governance.get_hidden_brands() or ())
    except Exception:
        hb = frozenset()
    try:
        hs = frozenset(ops_governance.get_hidden_stores() or ())
    except Exception:
        hs = frozenset()
    return hb, hs


def _is_hidden(store_name: str, hb: frozenset[str], hs: frozenset[str]) -> bool:
    return local_db._is_hidden_with_sets(ops_governance, store_name, hb, hs)


def build_area_night_exchange_payload(
    raw_stores: list[dict[str, Any]] | None = None,
    last_updated: str | None = None,
    now_jst: datetime.datetime | None = None,
) -> dict[str, Any]:
    """`GET /api/exchange/area-night.v1.json` 用の公開エリア別夜間リアルタイム交換フィードを生成する。"""
    dt_now = now_jst.astimezone(_JST) if now_jst and now_jst.tzinfo else (now_jst.replace(tzinfo=_JST) if now_jst else datetime.datetime.now(_JST))
    hb, hs = _hidden_sets()
    visible_live = ops_governance.filter_visible_stores(list(raw_stores or []))
    live_by_canon: dict[str, dict[str, Any]] = {}
    for s in visible_live:
        raw_n = str((s or {}).get("name") or "").strip()
        if not raw_n:
            continue
        canon_n = store_master.to_internal_store_name(raw_n) or raw_n
        if not _is_hidden(canon_n, hb, hs):
            live_by_canon[canon_n] = s

    cw = load_crosswalk()
    areas_out: list[dict[str, Any]] = []
    total_active_stores = 0

    for area_spec in cw.get("areas") or []:
        area_id = str(area_spec.get("area_id") or "").strip()
        if not area_id:
            continue
        candidate_stores = [
            sn for sn in (area_spec.get("aiseki_stores") or [])
            if sn and not _is_hidden(sn, hb, hs)
        ]
        open_count = 0
        measured_count = 0
        w_sum = 0
        m_sum = 0
        best_cand: dict[str, Any] | None = None

        for sname in candidate_stores:
            s_live = live_by_canon.get(sname) or {}
            try:
                w = max(0, int(float(s_live.get("women", 0) or 0)))
            except Exception:
                w = 0
            try:
                m = max(0, int(float(s_live.get("men", 0) or 0)))
            except Exception:
                m = 0
            tot = w + m
            h_info = store_hours.get_business_hours_status(sname, total_guests=tot)
            is_open = bool(h_info.get("is_open_now"))
            if is_open:
                open_count += 1
            if sname in live_by_canon:
                measured_count += 1
                total_active_stores += 1
            w_sum += w
            m_sum += m
            if tot >= 6 and w >= 6:
                wpct_s = round((w / tot) * 100, 1)
                slug_s = store_master.slug_of(sname)
                cand_obj = {
                    "seo_name": store_master.seo_name(sname),
                    "slug": slug_s,
                    "url": f"https://aiseki-navi.com/store/{slug_s}",
                    "women": w,
                    "men": m,
                    "total": tot,
                    "women_ratio_pct": wpct_s,
                    "_score": (1 if w >= m else 0, w - m, w),
                }
                if best_cand is None or cand_obj["_score"] > best_cand["_score"]:
                    best_cand = cand_obj

        tot_area = w_sum + m_sum
        wpct_area = round((w_sum / tot_area) * 100, 1) if tot_area >= 6 else None
        if tot_area >= 6:
            if w_sum > m_sum:
                walkin_status = "women_lead"
                walkin_label = "女性優位・相席スムーズ"
            elif w_sum == m_sum:
                walkin_status = "balanced"
                walkin_label = "男女同数・バランス良好"
            else:
                walkin_status = "men_glut"
                walkin_label = "男性先行・近隣比較推奨"
            data_status = "measured"
        elif open_count > 0 and measured_count > 0:
            walkin_status = "open_low"
            walkin_label = "営業中・立ち上がり時間帯"
            data_status = "measured"
        elif measured_count > 0:
            walkin_status = "closed"
            walkin_label = "営業時間外"
            data_status = "measured"
        else:
            walkin_status = "insufficient"
            walkin_label = "リアルタイム計測待機中"
            data_status = "insufficient"

        best_store_public = None
        if best_cand is not None:
            best_store_public = {k: v for k, v in best_cand.items() if not k.startswith("_")}

        aiseki_path = str(area_spec.get("aiseki_path") or f"/area/{area_spec.get('aiseki_area_slug') or 'shinjuku'}")
        concierge_area = str(area_spec.get("aiseki_concierge_area") or "shinjuku")
        areas_out.append({
            "area_id": area_id,
            "label": area_spec.get("label") or area_id,
            "short_label": area_spec.get("short_label") or area_id,
            "walkin_coverage": area_spec.get("walkin_coverage") or "direct",
            "data_status": data_status,
            "walkin_status": walkin_status,
            "walkin_label": walkin_label,
            "store_count": len(candidate_stores),
            "open_store_count": open_count,
            "women_total": w_sum,
            "men_total": m_sum,
            "total_guests": tot_area,
            "women_ratio_pct": wpct_area,
            "best_store": best_store_public,
            "area_url": f"https://aiseki-navi.com{aiseki_path}",
            "concierge_url": f"https://aiseki-navi.com/concierge?area={concierge_area}",
        })

    return {
        "schema_version": "1.0",
        "feed": "aiseki_navi.area_night",
        "crosswalk_version": cw.get("crosswalk_version") or "2026-11-10.v1",
        "generated_at": dt_now.isoformat(timespec="seconds"),
        "last_updated": last_updated or "--:--",
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "data_policy": "zero_guesswork: 公式公開データのリアルタイム計測値のみ。合計6名未満の比率算出や未計測値の補完は行いません。",
        "active_store_count": total_active_stores,
        "areas": areas_out,
    }


def sync_area_night_daily_rollups(days: int = 28) -> int:
    """`store_hourly_rollups` から7日チャンクで `area_night_daily_rollups` を集計・同期する。"""
    if not local_db._db_available():
        return 0
    local_db._ensure_moat_rollup_schema()
    cw = load_crosswalk()
    hb, hs = _hidden_sets()

    store_to_areas: dict[str, list[str]] = {}
    for area_spec in cw.get("areas") or []:
        aid = str(area_spec.get("area_id") or "").strip()
        if not aid:
            continue
        for sn in area_spec.get("aiseki_stores") or []:
            canon = store_master.to_internal_store_name(sn) or sn
            if canon and not _is_hidden(canon, hb, hs):
                store_to_areas.setdefault(canon, []).append(aid)

    if not store_to_areas:
        return 0

    today_jst = datetime.datetime.now(_JST).date()
    total_days = max(1, min(90, int(days or 28)))
    now_iso = datetime.datetime.now(_JST).isoformat(timespec="seconds")

    # 7日チャンクで読み出し、512MB Fly Postgres VM のメモリスパイクを防ぐ
    agg: dict[tuple[str, str], list[float]] = {}
    for offset in range(0, total_days, 7):
        end_d = (today_jst - datetime.timedelta(days=offset)).isoformat()
        start_d = (today_jst - datetime.timedelta(days=min(total_days, offset + 7))).isoformat()
        rows = local_db._fetch_all_rows(
            """
            SELECT business_date, hour_jst, store_name, sample_count, avg_women, avg_men
            FROM store_hourly_rollups
            WHERE business_date >= ? AND business_date <= ?
              AND (hour_jst >= 18 OR hour_jst <= 4)
              AND sample_count > 0
            """,
            (start_d, end_d),
        )
        for r in rows or []:
            bdate = str(r[0] or "").strip()
            sname = str(r[2] or "").strip()
            canon = store_master.to_internal_store_name(sname) or sname
            aids = store_to_areas.get(canon)
            if not bdate or not aids:
                continue
            sc = int(r[3] or 0)
            aw = float(r[4] or 0.0)
            am = float(r[5] or 0.0)
            tot = aw + am
            is_wl = 1 if (tot >= 6.0 and aw > am) else 0
            is_mg = 1 if (tot >= 6.0 and am > aw) else 0
            for aid in aids:
                bucket = agg.setdefault((bdate, aid), [0, 0, 0.0, 0.0, 0, 0])
                bucket[0] += 1
                bucket[1] += sc
                bucket[2] += aw
                bucket[3] += am
                bucket[4] += is_wl
                bucket[5] += is_mg

    if not agg:
        return 0

    upsert_rows = []
    for (bdate, aid), vals in sorted(agg.items()):
        sh = int(vals[0])
        sc = int(vals[1])
        w_avg = round(vals[2] / sh, 2) if sh > 0 else 0.0
        m_avg = round(vals[3] / sh, 2) if sh > 0 else 0.0
        wpct = round((w_avg / (w_avg + m_avg)) * 100, 1) if (w_avg + m_avg) > 0 else 0.0
        upsert_rows.append((bdate, aid, sh, sc, w_avg, m_avg, wpct, int(vals[4]), int(vals[5]), now_iso))

    sql_sqlite = """
        INSERT INTO area_night_daily_rollups
        (business_date, area_id, store_hours, sample_count, avg_women, avg_men, avg_wpct,
         women_lead_store_hours, men_glut_store_hours, updated_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(business_date, area_id) DO UPDATE SET
          store_hours = excluded.store_hours,
          sample_count = excluded.sample_count,
          avg_women = excluded.avg_women,
          avg_men = excluded.avg_men,
          avg_wpct = excluded.avg_wpct,
          women_lead_store_hours = excluded.women_lead_store_hours,
          men_glut_store_hours = excluded.men_glut_store_hours,
          updated_at = excluded.updated_at
    """
    if local_db._is_pg():
        pg_sql = sql_sqlite.replace("?", "%s")
        with turbo_store.pg_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(pg_sql, upsert_rows)
            conn.commit()
    else:
        with local_db._db_lock:
            conn = local_db.get_connection()
            try:
                conn.executemany(sql_sqlite, upsert_rows)
                conn.commit()
            finally:
                conn.close()
    return len(upsert_rows)


def upsert_partner_area_evening_payload(payload: dict[str, Any], source_url: str = _PARTNER_EXCHANGE_URL) -> int:
    """検証済みのマチナビ `area-evening.v1.json` ペイロードを `partner_area_evening_daily` へ保存する。"""
    if not isinstance(payload, dict) or payload.get("schema_version") != "1.0" or payload.get("feed") != "machinavi.area_evening":
        raise ValueError("invalid partner area-evening payload schema")
    areas = payload.get("areas")
    if not isinstance(areas, list):
        raise ValueError("partner area-evening payload missing areas list")

    local_db._ensure_moat_rollup_schema()
    cw_areas = {str(a.get("area_id")) for a in (load_crosswalk().get("areas") or []) if a.get("area_id")}
    now_iso = datetime.datetime.now(_JST).isoformat(timespec="seconds")
    default_date = str(payload.get("observed_date_jst") or datetime.datetime.now(_JST).date().isoformat())[:10]
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", default_date):
        default_date = datetime.datetime.now(_JST).date().isoformat()

    rows_to_save = []
    for item in areas:
        if not isinstance(item, dict):
            continue
        aid = str(item.get("area_id") or "").strip()
        if not aid or aid not in cw_areas:
            continue
        obs_date = str(item.get("observed_date_jst") or default_date)[:10]
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", obs_date):
            obs_date = default_date
        trailing = item.get("same_dow_trailing") if isinstance(item.get("same_dow_trailing"), dict) else item
        sc = max(0, int(trailing.get("sample_count", 0) or 0))
        wl = max(0, int(trailing.get("women_lead_count", 0) or 0))
        bal = max(0, int(trailing.get("balanced_count", 0) or 0))
        mnf = max(0, int(trailing.get("men_nearly_full_count", 0) or 0))
        cl = max(0, int(trailing.get("closed_count", 0) or 0))
        d_status = "measured" if (sc > 0 and wl + bal + mnf + cl == sc) else "insufficient"
        di_pt = round(((wl - (mnf + cl)) / sc) * 100.0, 1) if (d_status == "measured" and sc >= _MIN_PREPAID_SAMPLES) else None
        rows_to_save.append((obs_date, aid, sc, wl, bal, mnf, cl, di_pt, d_status, source_url[:200], now_iso))

    if not rows_to_save:
        return 0

    sql_sqlite = """
        INSERT INTO partner_area_evening_daily
        (observed_date_jst, area_id, sample_count, women_lead_count, balanced_count,
         men_nearly_full_count, closed_count, prepaid_di_pt, data_status, source_url, fetched_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(observed_date_jst, area_id) DO UPDATE SET
          sample_count = excluded.sample_count,
          women_lead_count = excluded.women_lead_count,
          balanced_count = excluded.balanced_count,
          men_nearly_full_count = excluded.men_nearly_full_count,
          closed_count = excluded.closed_count,
          prepaid_di_pt = excluded.prepaid_di_pt,
          data_status = excluded.data_status,
          source_url = excluded.source_url,
          fetched_at = excluded.fetched_at
    """
    if local_db._is_pg():
        pg_sql = sql_sqlite.replace("?", "%s")
        with turbo_store.pg_connection() as conn:
            with conn.cursor() as cur:
                cur.executemany(pg_sql, rows_to_save)
            conn.commit()
    else:
        with local_db._db_lock:
            conn = local_db.get_connection()
            try:
                conn.executemany(sql_sqlite, rows_to_save)
                conn.commit()
            finally:
                conn.close()
    with _di_cache_lock:
        _di_cache.clear()
    return len(rows_to_save)


def pull_partner_area_evening_feed(
    feed_url: str = _PARTNER_EXCHANGE_URL,
    timeout: float = _PARTNER_PULL_TIMEOUT_SEC,
) -> dict[str, Any]:
    """マチナビ公開静的フィード `area-evening.v1.json` を非汚染UAで安全に取得してDBへ保存する。"""
    parsed = urllib.parse.urlsplit(str(feed_url or "").strip())
    if parsed.scheme != "https" or parsed.hostname not in _ALLOWED_PARTNER_HOSTS:
        raise ValueError(f"disallowed partner feed URL: {feed_url!r}")
    req = urllib.request.Request(
        feed_url,
        headers={
            "User-Agent": "AisekiOpsSentinel/1.0 (+https://aiseki-navi.com/stats/weekend-index)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        raw_bytes = resp.read(_MAX_PARTNER_FEED_BYTES + 1)
    if len(raw_bytes) > _MAX_PARTNER_FEED_BYTES:
        raise ValueError("partner feed response exceeds 256KB limit")
    payload = json.loads(raw_bytes.decode("utf-8"))
    saved = upsert_partner_area_evening_payload(payload, source_url=feed_url)
    return {"ok": True, "saved_rows": saved, "generated_at": payload.get("generated_at")}


def maybe_trigger_partner_pull_async(testing: bool = False) -> None:
    """公開リクエストをブロックせず15分に1回だけバックグラウンドで姉妹フィードを同期する。"""
    global _last_partner_pull_mono
    if (
        testing
        or os.environ.get("DISABLE_SCHEDULER") == "1"
        or os.environ.get("DISABLE_SHEETS") == "1"
    ):
        return
    now_m = time.monotonic()
    with _partner_pull_lock:
        if now_m - _last_partner_pull_mono < _PARTNER_PULL_DEBOUNCE_SEC:
            return
        _last_partner_pull_mono = now_m

    def _worker():
        try:
            sync_area_night_daily_rollups(days=28)
        except Exception as err:
            logging.getLogger(__name__).warning("sync_area_night_daily_rollups async error: %s", err)
        try:
            pull_partner_area_evening_feed()
        except Exception as err:
            logging.getLogger(__name__).info("pull_partner_area_evening_feed async skipped: %s", err)

    threading.Thread(target=_worker, daemon=True).start()


def build_weekend_supply_demand_di(days: int = 28, now_jst: datetime.datetime | None = None) -> dict[str, Any]:
    """週末・夜間の「予約制イベント（マチナビ）× 当日相席ラウンジ（相席ナビ）」2軸需給DI指数を算出する。"""
    norm_days = min((7, 14, 28, 90), key=lambda d: abs(d - max(7, min(90, int(days or 28)))))
    hb, hs = _hidden_sets()
    cache_key = (local_db._active_db_key(), norm_days, hb, hs)
    now_m = time.monotonic()
    if now_jst is None:
        with _di_cache_lock:
            hit = _di_cache.get(cache_key)
            if hit and (now_m - hit[0]) < _DI_CACHE_TTL:
                return hit[1]

    dt_now = now_jst.astimezone(_JST) if now_jst and now_jst.tzinfo else (now_jst.replace(tzinfo=_JST) if now_jst else datetime.datetime.now(_JST))
    cutoff_date = (dt_now.date() - datetime.timedelta(days=norm_days)).isoformat()
    cw = load_crosswalk()

    # 1) Walk-in 軸: store_hourly_rollups から直接集計（未バックフィル環境でも即座に実測値を反映）
    walkin_by_area: dict[str, list[float]] = {}
    if local_db._db_available():
        local_db._ensure_moat_rollup_schema()
        store_to_areas: dict[str, list[str]] = {}
        for area_spec in cw.get("areas") or []:
            aid = str(area_spec.get("area_id") or "").strip()
            for sn in area_spec.get("aiseki_stores") or []:
                canon = store_master.to_internal_store_name(sn) or sn
                if canon and not _is_hidden(canon, hb, hs):
                    store_to_areas.setdefault(canon, []).append(aid)

        try:
            rows = local_db._fetch_all_rows(
                """
                SELECT store_name, sample_count, avg_women, avg_men
                FROM store_hourly_rollups
                WHERE business_date >= ?
                  AND (hour_jst >= 18 OR hour_jst <= 4)
                  AND sample_count > 0
                """,
                (cutoff_date,),
            )
        except Exception:
            rows = []

        for r in rows or []:
            sname = str(r[0] or "").strip()
            canon = store_master.to_internal_store_name(sname) or sname
            aids = store_to_areas.get(canon)
            if not aids:
                continue
            sc = int(r[1] or 0)
            aw = float(r[2] or 0.0)
            am = float(r[3] or 0.0)
            tot = aw + am
            is_active = 1 if tot >= 6.0 else 0
            is_wl = 1 if (tot >= 6.0 and aw > am) else 0
            is_mg = 1 if (tot >= 6.0 and am > aw) else 0
            for aid in aids:
                b = walkin_by_area.setdefault(aid, [0, 0, 0.0, 0.0, 0, 0, 0])
                b[0] += 1          # store_hours
                b[1] += sc         # sample_count
                b[2] += aw
                b[3] += am
                b[4] += is_wl
                b[5] += is_mg
                b[6] += is_active

    # 2) Prepaid 軸: partner_area_evening_daily から最新スナップショットを取得
    prepaid_by_area: dict[str, dict[str, Any]] = {}
    if local_db._db_available():
        try:
            p_rows = local_db._fetch_all_rows(
                """
                SELECT area_id, sample_count, women_lead_count, balanced_count,
                       men_nearly_full_count, closed_count, prepaid_di_pt, data_status,
                       source_url, fetched_at, observed_date_jst
                FROM partner_area_evening_daily
                WHERE observed_date_jst >= ?
                ORDER BY observed_date_jst DESC
                """,
                (cutoff_date,),
            )
            for pr in p_rows or []:
                aid = str(pr[0] or "").strip()
                if aid and aid not in prepaid_by_area:
                    prepaid_by_area[aid] = {
                        "sample_count": int(pr[1] or 0),
                        "women_lead_count": int(pr[2] or 0),
                        "balanced_count": int(pr[3] or 0),
                        "men_nearly_full_count": int(pr[4] or 0),
                        "closed_count": int(pr[5] or 0),
                        "prepaid_di_pt": float(pr[6]) if pr[6] is not None else None,
                        "data_status": str(pr[7] or "insufficient"),
                        "source_url": pr[8],
                        "fetched_at": pr[9],
                        "observed_date_jst": pr[10],
                    }
        except Exception:
            prepaid_by_area = {}

    areas_out: list[dict[str, Any]] = []
    measured_walkin_areas = 0
    measured_prepaid_areas = 0

    for area_spec in cw.get("areas") or []:
        aid = str(area_spec.get("area_id") or "").strip()
        if not aid:
            continue
        w_vals = walkin_by_area.get(aid) or [0, 0, 0.0, 0.0, 0, 0, 0]
        sh = int(w_vals[0])
        sc_w = int(w_vals[1])
        active_sh = int(w_vals[6])
        wl_sh = int(w_vals[4])
        mg_sh = int(w_vals[5])
        avg_w = round(w_vals[2] / sh, 1) if sh > 0 else None
        avg_m = round(w_vals[3] / sh, 1) if sh > 0 else None
        avg_wpct = round((w_vals[2] / (w_vals[2] + w_vals[3])) * 100.0, 1) if (w_vals[2] + w_vals[3]) > 0 else None

        if sh >= _MIN_WALKIN_STORE_HOURS and active_sh > 0:
            walkin_status = "measured"
            walkin_di = round(((wl_sh - mg_sh) / active_sh) * 100.0, 1)
            measured_walkin_areas += 1
        else:
            walkin_status = "insufficient"
            walkin_di = None

        p_info = prepaid_by_area.get(aid) or {}
        p_sc = int(p_info.get("sample_count", 0) or 0)
        p_wl = int(p_info.get("women_lead_count", 0) or 0)
        p_bal = int(p_info.get("balanced_count", 0) or 0)
        p_mnf = int(p_info.get("men_nearly_full_count", 0) or 0)
        p_cl = int(p_info.get("closed_count", 0) or 0)
        if p_info.get("data_status") == "measured" and p_sc >= _MIN_PREPAID_SAMPLES:
            prepaid_status = "measured"
            prepaid_di = round(((p_wl - (p_mnf + p_cl)) / p_sc) * 100.0, 1)
            measured_prepaid_areas += 1
        else:
            prepaid_status = "insufficient"
            prepaid_di = None

        if walkin_di is not None and prepaid_di is not None:
            if prepaid_di < 0 and walkin_di > 0:
                regime_label = "予約制は男性満席傾向 → 当日相席ラウンジが女性優位で狙い目"
            elif prepaid_di > 0 and walkin_di < 0:
                regime_label = "当日ラウンジは男性先行 → 事前予約制イベント（女性先行枠）が有利"
            elif walkin_di >= 10.0 and prepaid_di >= 10.0:
                regime_label = "予約制・当日相席ともに女性先行（双方好条件）"
            else:
                regime_label = "予約制・当日相席ともに標準バランス推移"
        elif walkin_di is not None:
            regime_label = (
                f"当日相席DI {walkin_di:+.1f}pt（女性比平均 {avg_wpct}%）｜予約制側データ蓄積中"
                if avg_wpct is not None
                else f"当日相席DI {walkin_di:+.1f}pt｜予約制側データ蓄積中"
            )
        else:
            regime_label = "観測サンプル蓄積中（閾値到達後にDI指数を算出）"

        aiseki_path = str(area_spec.get("aiseki_path") or f"/area/{area_spec.get('aiseki_area_slug') or 'shinjuku'}")
        mn_path = str(area_spec.get("machinavi_path") or "/areas/")
        areas_out.append({
            "area_id": aid,
            "label": area_spec.get("label") or aid,
            "short_label": area_spec.get("short_label") or aid,
            "walkin_coverage": area_spec.get("walkin_coverage") or "direct",
            "coverage_note": area_spec.get("coverage_note"),
            "walkin_data_status": walkin_status,
            "walkin_store_hours": sh,
            "walkin_sample_count": sc_w,
            "walkin_avg_women": avg_w,
            "walkin_avg_men": avg_m,
            "walkin_avg_wpct": avg_wpct,
            "walkin_women_lead_hours": wl_sh,
            "walkin_men_glut_hours": mg_sh,
            "walkin_di_pt": walkin_di,
            "prepaid_data_status": prepaid_status,
            "prepaid_sample_count": p_sc,
            "prepaid_women_lead_count": p_wl,
            "prepaid_balanced_count": p_bal,
            "prepaid_men_nearly_full_count": p_mnf,
            "prepaid_closed_count": p_cl,
            "prepaid_di_pt": prepaid_di,
            "regime_summary": regime_label,
            "aiseki_area_url": f"https://aiseki-navi.com{aiseki_path}",
            "machinavi_area_url": f"https://machinavi-data.com{mn_path}",
        })

    payload = {
        "schema_version": "1.0",
        "report_id": "weekend_supply_demand_di",
        "title": "全国主要都市「予約制街コン・婚活 × 当日相席ラウンジ」2軸需給DI指数（Diffusion Index）",
        "crosswalk_version": cw.get("crosswalk_version") or "2026-11-10.v1",
        "generated_at": dt_now.isoformat(timespec="seconds"),
        "window_days": norm_days,
        "cutoff_date": cutoff_date,
        "license": "https://creativecommons.org/licenses/by/4.0/",
        "isBasedOn": [
            "https://aiseki-navi.com/stats/monthly.csv",
            "https://machinavi-data.com/data/exchange/area-evening.v1.json",
        ],
        "methodology": {
            "walkin_di_formula": "(女性優位時間数 - 男性先行時間数) ÷ 有効営業店舗時間数 × 100（-100〜+100pt、最小閾値: 12店舗時間以上）",
            "prepaid_di_formula": "(女性先行枠数 - (男性満席間近枠数 + 受付終了枠数)) ÷ 公開残席観測数 × 100（-100〜+100pt、最小閾値: 20観測以上）",
            "zero_guesswork_policy": "閾値未満のエリアは推定値を出さず data_status='insufficient' として明示します。",
        },
        "summary": {
            "total_areas": len(areas_out),
            "measured_walkin_areas": measured_walkin_areas,
            "measured_prepaid_areas": measured_prepaid_areas,
        },
        "areas": areas_out,
    }
    if measured_walkin_areas > 0 and now_jst is None:
        with _di_cache_lock:
            _di_cache[cache_key] = (now_m, payload)
    return payload


def build_weekend_supply_demand_csv(days: int = 28) -> str:
    """`GET /stats/weekend-index.csv` 用の CC BY 4.0 オープンデータ CSV を生成する。"""
    rep = build_weekend_supply_demand_di(days=days)
    buf = io.StringIO()
    writer = csv.writer(buf, lineterminator="\n")
    writer.writerow([
        "area_id",
        "area_label",
        "window_days",
        "walkin_data_status",
        "walkin_store_hours",
        "walkin_sample_count",
        "walkin_avg_women",
        "walkin_avg_men",
        "walkin_avg_wpct",
        "walkin_di_pt",
        "prepaid_data_status",
        "prepaid_sample_count",
        "prepaid_women_lead_count",
        "prepaid_balanced_count",
        "prepaid_men_nearly_full_count",
        "prepaid_closed_count",
        "prepaid_di_pt",
        "aiseki_area_url",
        "machinavi_area_url",
    ])
    w_days = rep.get("window_days", 28)
    for a in rep.get("areas") or []:
        writer.writerow([
            a.get("area_id", ""),
            a.get("label", ""),
            w_days,
            a.get("walkin_data_status", "insufficient"),
            a.get("walkin_store_hours", 0),
            a.get("walkin_sample_count", 0),
            "" if a.get("walkin_avg_women") is None else a.get("walkin_avg_women"),
            "" if a.get("walkin_avg_men") is None else a.get("walkin_avg_men"),
            "" if a.get("walkin_avg_wpct") is None else a.get("walkin_avg_wpct"),
            "" if a.get("walkin_di_pt") is None else a.get("walkin_di_pt"),
            a.get("prepaid_data_status", "insufficient"),
            a.get("prepaid_sample_count", 0),
            a.get("prepaid_women_lead_count", 0),
            a.get("prepaid_balanced_count", 0),
            a.get("prepaid_men_nearly_full_count", 0),
            a.get("prepaid_closed_count", 0),
            "" if a.get("prepaid_di_pt") is None else a.get("prepaid_di_pt"),
            a.get("aiseki_area_url", ""),
            a.get("machinavi_area_url", ""),
        ])
    return buf.getvalue()


def build_monthly_frozen_snapshot(yyyy_mm: str) -> dict[str, Any] | None:
    """`GET /stats/monthly/<yyyy_mm>` 用の月次一次統計アーカイブ（AI引用・被リンク固定URL用）を生成する。"""
    ym = str(yyyy_mm or "").strip()
    if not re.fullmatch(r"202[5-9]-(?:0[1-9]|1[0-2])", ym):
        return None
    today_jst = datetime.datetime.now(_JST).date()
    cur_ym = today_jst.strftime("%Y-%m")
    if ym > cur_ym:
        return None

    hb, hs = _hidden_sets()
    cache_key = (local_db._active_db_key(), ym, hb, hs)
    now_m = time.monotonic()
    with _monthly_snap_lock:
        hit = _monthly_snap_cache.get(cache_key)
        if hit and (now_m - hit[0]) < _MONTHLY_SNAP_TTL:
            return hit[1]

    rows = []
    if local_db._db_available():
        local_db._ensure_moat_rollup_schema()
        try:
            rows = local_db._fetch_all_rows(
                """
                SELECT store_name,
                       COUNT(*) AS store_hours,
                       SUM(sample_count) AS total_samples,
                       AVG(avg_women) AS mean_w,
                       MAX(max_women) AS peak_w,
                       AVG(avg_men) AS mean_m,
                       MAX(max_men) AS peak_m,
                       MIN(business_date) AS first_date,
                       MAX(business_date) AS last_date
                FROM store_hourly_rollups
                WHERE business_date LIKE ?
                  AND sample_count > 0
                GROUP BY store_name
                """,
                (f"{ym}-%",),
            )
        except Exception as err:
            logging.getLogger(__name__).warning("build_monthly_frozen_snapshot query failed: %s", err)
            rows = []

    master = store_master.load() or {}
    stores_out: list[dict[str, Any]] = []
    total_samples_all = 0
    total_hours_all = 0
    first_d_all: str | None = None
    last_d_all: str | None = None

    for r in rows or []:
        raw_name = str(r[0] or "").strip()
        canon = store_master.to_internal_store_name(raw_name) or raw_name
        if not canon or "ソウル" in canon or _is_hidden(canon, hb, hs):
            continue
        sh = int(r[1] or 0)
        sc = int(r[2] or 0)
        if sh <= 0 or sc <= 0:
            continue
        mw = round(float(r[3] or 0.0), 1)
        pw = int(r[4] or 0)
        mm = round(float(r[5] or 0.0), 1)
        pm = int(r[6] or 0)
        wpct = round((mw / (mw + mm)) * 100.0, 1) if (mw + mm) > 0 else 0.0
        f_d = str(r[7] or "")
        l_d = str(r[8] or "")
        if f_d and (first_d_all is None or f_d < first_d_all):
            first_d_all = f_d
        if l_d and (last_d_all is None or l_d > last_d_all):
            last_d_all = l_d
        total_hours_all += sh
        total_samples_all += sc
        info = master.get(canon) or {}
        stores_out.append({
            "seo_name": store_master.seo_name(canon),
            "slug": store_master.slug_of(canon),
            "area_label": info.get("area_label") or "全国",
            "brand_label": store_master.seo_name(canon).split(" ")[0],
            "store_hours": sh,
            "sample_count": sc,
            "avg_women": mw,
            "peak_women": pw,
            "avg_men": mm,
            "peak_men": pm,
            "avg_wpct": wpct,
        })

    stores_out.sort(key=lambda x: (x["peak_women"], x["avg_women"], x["sample_count"]), reverse=True)
    year_s, month_s = ym.split("-")
    snap = {
        "yyyy_mm": ym,
        "year": int(year_s),
        "month": int(month_s),
        "title_ja": f"{int(year_s)}年{int(month_s)}月 全国相席ラウンジ月次一次統計アーカイブ",
        "is_current_month": (ym == cur_ym),
        "data_status": "measured" if stores_out else "insufficient",
        "store_count": len(stores_out),
        "total_store_hours": total_hours_all,
        "total_samples": total_samples_all,
        "first_date": first_d_all,
        "last_date": last_d_all,
        "stores": stores_out,
        "canonical_url": f"https://aiseki-navi.com/stats/monthly/{ym}",
        "license": "https://creativecommons.org/licenses/by/4.0/",
    }
    if stores_out:
        with _monthly_snap_lock:
            _monthly_snap_cache[cache_key] = (now_m, snap)
    return snap
