#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
daily_pv_report.py - 相席ナビ 日次PV・人気ページ・アフィリエイトCTR分析レポート生成スクリプト

特徴:
  - Google Sheets ('Analytics Daily') の本番フラッシュ済み集計行を読み取り専用で取得（本番サイトへのアクセスゼロ＝人間PV非汚染）
  - 対象日（既定: 前日＝朝9:00実行時点で深夜・朝5時閉店分まで確定した1日分）のPV・前日比・前週同曜日比・直近7日平均を算出
  - 人気ページTOP10、下層コラム/店舗ページTOP10、注目店舗TOP5、2ステップクッション送客、アフィリエイト枠別CTRを出力
  - AGENTS.md 準拠（推測値ゼロ、ブランド正式名称厳守、Notion互換Markdown出力）
"""

import argparse
import datetime
import json
import os
import re
import sys
from collections import defaultdict
from pathlib import Path

# スケジューラ起動を抑止しつつ、Google Sheets ('Analytics Daily') 読み取りは有効化
os.environ["DISABLE_SCHEDULER"] = "1"
os.environ.pop("DISABLE_SHEETS", None)

REPO_ROOT = Path(__file__).resolve().parents[4]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))
os.chdir(str(REPO_ROOT))

import affiliate_manager
import analytics
import sheets_store
import store_master

JST = datetime.timezone(datetime.timedelta(hours=9))
WEEKDAY_JA = ("月", "火", "水", "木", "金", "土", "日")

_LEADING_EMOJI_RE = re.compile(
    r"^[\s\U0001F300-\U0001FAFF\u2600-\u27BF\uFE0F\u200D🔥🧭📚📍✨📊💴🥂⚡💎🍸📖💄⚖️🏪💰📣🔗𝕏🧵📝📸👔🧴🪒💝🍻🍽️]+"
)


def _clean_title(text: str) -> str:
    s = _LEADING_EMOJI_RE.sub("", str(text or "")).strip()
    # ブランド表記ガード（AGENTS.md §3）
    s = re.sub(r"(?<![A-Za-z0-9_])OLG\s*", "オリエンタルラウンジ ", s)
    s = re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", s)
    s = s.replace("オリラジ", "オリエンタルラウンジ").replace("YATAKOI", "ヤタコイ")
    return s or "-"


def _fmt_signed_diff(cur: int, ref: int, has_ref_data: bool = True) -> str:
    if not has_ref_data:
        return "比較データ蓄積中"
    diff = cur - ref
    sign = "+" if diff > 0 else ""
    if ref > 0:
        pct = round((diff / ref) * 100, 1)
        pct_sign = "+" if pct > 0 else ""
        return f"{sign}{diff:,} ({pct_sign}{pct}%)"
    if cur == 0:
        return "±0 (0.0%)"
    return f"{sign}{diff:,} (新規)"


def _date_with_wd(date_str: str) -> str:
    try:
        dt = datetime.date.fromisoformat(date_str)
        return f"{date_str} ({WEEKDAY_JA[dt.weekday()]})"
    except ValueError:
        return date_str


def _load_analytics_totals(start_date_str: str) -> dict:
    """Google Sheets 'Analytics Daily' から start_date_str 以降の (date, event, label) -> count を取得"""
    totals = defaultdict(int)
    rows = sheets_store.read_rows(analytics.SHEET_TITLE, analytics.HEADER) or []
    for r in rows:
        if len(r) < 4:
            continue
        d_str = str(r[0] or "").strip()
        ev = str(r[1] or "").strip()
        lbl = str(r[2] or "").strip()
        if not d_str or d_str < start_date_str or not ev:
            continue
        try:
            cnt = int(float(str(r[3] or "0").replace(",", "").strip()))
        except ValueError:
            continue
        if cnt <= 0:
            continue
        if ev in getattr(analytics, "_STORE_TRACK_EVENTS", ()):
            lbl = analytics.normalize_store_event_label(ev, lbl)
        totals[(d_str, ev, lbl)] += cnt
    return totals


def _day_metrics(totals: dict, date_str: str) -> dict:
    by_ev = defaultdict(int)
    pv_by_path = defaultdict(int)
    store_opens = defaultdict(int)
    cushions = defaultdict(int)
    aff_imp_by_pair = defaultdict(int)
    aff_clk_by_pair = defaultdict(int)
    sns_inflows = defaultdict(int)

    for (d, ev, lbl), cnt in totals.items():
        if d != date_str:
            continue
        by_ev[ev] += cnt
        if ev == "page_view":
            pv_by_path[lbl or "/"] += cnt
        elif ev == "store_open":
            store_opens[lbl] += cnt
        elif ev == "cushion_click":
            cushions[lbl] += cnt
        elif ev == "affiliate_impression":
            if "@" in lbl:
                s_part, p_part = [x.strip() for x in lbl.split("@", 1)]
            else:
                s_part, p_part = lbl.strip(), "/other"
            cid = affiliate_manager.canonical_slot_id(s_part) or s_part.lower()
            surf = analytics.normalize_cta_surface(p_part)
            if cid and surf != "/stats":
                aff_imp_by_pair[(cid, surf)] += cnt
        elif ev == "affiliate_click":
            if "@" in lbl:
                s_part, p_part = [x.strip() for x in lbl.split("@", 1)]
            else:
                s_part, p_part = lbl.strip(), "/other"
            cid = affiliate_manager.canonical_slot_id(s_part) or s_part.lower()
            surf = analytics.normalize_cta_surface(p_part)
            if cid and surf != "/stats":
                aff_clk_by_pair[(cid, surf)] += cnt
        elif ev == "sns_inflow":
            sns_inflows[lbl] += cnt

    pv = by_ev.get("page_view", 0)
    top_pv = pv_by_path.get("/", 0)
    sub_pv = max(0, pv - top_pv)
    guide_pv = sum(c for p, c in pv_by_path.items() if p == "/columns" or p.startswith("/guide"))
    store_page_pv = sum(c for p, c in pv_by_path.items() if p.startswith("/store/"))
    store_open = by_ev.get("store_open", 0)
    cushion = by_ev.get("cushion_click", 0)
    aff_imp = sum(aff_imp_by_pair.values())
    aff_clk = by_ev.get("affiliate_click", 0)
    official_clk = by_ev.get("official_click", 0)
    sns_clk = by_ev.get("sns_click", 0) + by_ev.get("share", 0)
    vote_cnt = by_ev.get("vote", 0)

    return {
        "date": date_str,
        "date_label": _date_with_wd(date_str),
        "has_any_event": sum(by_ev.values()) > 0,
        "pv": pv,
        "top_pv": top_pv,
        "sub_pv": sub_pv,
        "sub_pv_share_pct": round((sub_pv / pv) * 100, 1) if pv > 0 else 0.0,
        "guide_pv": guide_pv,
        "store_page_pv": store_page_pv,
        "store_open": store_open,
        "cushion_click": cushion,
        "aff_imp": aff_imp,
        "aff_click": aff_clk,
        "aff_slot_ctr_pct": round((aff_clk / aff_imp) * 100, 2) if aff_imp > 0 else None,
        "aff_pv_ctr_pct": round((aff_clk / pv) * 100, 2) if pv > 0 else 0.0,
        "est_occurrence_yen": aff_clk * 90,
        "est_confirmed_yen": aff_clk * 30,
        "official_click": official_clk,
        "sns_share_click": sns_clk,
        "vote": vote_cnt,
        "pv_by_path": dict(pv_by_path),
        "store_opens": dict(store_opens),
        "cushions": dict(cushions),
        "aff_imp_by_pair": {f"{k[0]}@{k[1]}": v for k, v in aff_imp_by_pair.items()},
        "aff_clk_by_pair": {f"{k[0]}@{k[1]}": v for k, v in aff_clk_by_pair.items()},
        "sns_inflows": dict(sns_inflows),
    }


def build_daily_report(target_date_str: str | None = None) -> dict:
    now_jst = datetime.datetime.now(JST)
    if target_date_str:
        target_date = datetime.date.fromisoformat(target_date_str)
    else:
        target_date = now_jst.date() - datetime.timedelta(days=1)

    start_28d = (target_date - datetime.timedelta(days=27)).strftime("%Y-%m-%d")
    totals = _load_analytics_totals(start_28d)

    t_str = target_date.strftime("%Y-%m-%d")
    prev_str = (target_date - datetime.timedelta(days=1)).strftime("%Y-%m-%d")
    wow_str = (target_date - datetime.timedelta(days=7)).strftime("%Y-%m-%d")

    cur = _day_metrics(totals, t_str)
    prev = _day_metrics(totals, prev_str)
    wow = _day_metrics(totals, wow_str)

    # 直近7日間 (target_date - 6d .. target_date) と前7日間 (target_date - 13d .. target_date - 7d)
    days_7 = [(target_date - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(6, -1, -1)]
    prev_days_7 = [(target_date - datetime.timedelta(days=i)).strftime("%Y-%m-%d") for i in range(13, 6, -1)]

    series_7d = [_day_metrics(totals, d) for d in days_7]
    prev_series_7d = [_day_metrics(totals, d) for d in prev_days_7]

    pv_7d = sum(x["pv"] for x in series_7d)
    prev_pv_7d = sum(x["pv"] for x in prev_series_7d)
    prev_7d_has_data = any(x["has_any_event"] for x in prev_series_7d)
    avg_pv_7d = round(pv_7d / 7.0, 1)
    aff_clk_7d = sum(x["aff_click"] for x in series_7d)
    aff_imp_7d = sum(x["aff_imp"] for x in series_7d)
    aff_measurable_clk_7d = sum(x["aff_click"] for x in series_7d if x["aff_imp"] > 0)
    cushion_7d = sum(x["cushion_click"] for x in series_7d)
    store_open_7d = sum(x["store_open"] for x in series_7d)
    ctr_7d = min(100.0, round((aff_measurable_clk_7d / aff_imp_7d) * 100, 2)) if aff_imp_7d > 0 else None

    # 人気ページ TOP10（対象日）
    top_pages_day = []
    for path, cnt in sorted(cur["pv_by_path"].items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        desc = analytics.describe_label("page_view", path)
        prev_cnt = prev["pv_by_path"].get(path, 0)
        wow_cnt = wow["pv_by_path"].get(path, 0)
        top_pages_day.append({
            "path": path,
            "title": _clean_title(desc.get("title") or path),
            "pv": cnt,
            "share_pct": round((cnt / cur["pv"]) * 100, 1) if cur["pv"] > 0 else 0.0,
            "dod_diff": cnt - prev_cnt,
            "wow_diff": (cnt - wow_cnt) if wow["has_any_event"] else None,
        })

    # 直近7日の下層ページ（記事・店舗・診断：'/' 以外）TOP10
    subpages_7d_map = defaultdict(int)
    prev_subpages_7d_map = defaultdict(int)
    for x in series_7d:
        for p, c in x["pv_by_path"].items():
            if p != "/":
                subpages_7d_map[p] += c
    for x in prev_series_7d:
        for p, c in x["pv_by_path"].items():
            if p != "/":
                prev_subpages_7d_map[p] += c

    top_subpages_7d = []
    for path, cnt in sorted(subpages_7d_map.items(), key=lambda kv: (-kv[1], kv[0]))[:10]:
        desc = analytics.describe_label("page_view", path)
        p7_cnt = prev_subpages_7d_map.get(path, 0)
        top_subpages_7d.append({
            "path": path,
            "title": _clean_title(desc.get("title") or path),
            "pv_7d": cnt,
            "day_pv": cur["pv_by_path"].get(path, 0),
            "wow_7d_diff": (cnt - p7_cnt) if prev_7d_has_data else None,
        })

    # 注目店舗ランキング（直近7日の store_open + /store/<slug> PV 合算）
    store_interest_7d = defaultdict(lambda: {"open_7d": 0, "page_7d": 0, "day_total": 0})
    for x in series_7d:
        is_t = x["date"] == t_str
        for sname, c in x["store_opens"].items():
            internal = store_master.to_internal_store_name(sname) or sname
            seo = store_master.seo_name(internal) if store_master.get(internal) else _clean_title(sname)
            store_interest_7d[seo]["open_7d"] += c
            if is_t:
                store_interest_7d[seo]["day_total"] += c
        for p, c in x["pv_by_path"].items():
            if p.startswith("/store/"):
                slug = p[len("/store/"):].strip("/")
                st_key = store_master.store_by_slug(slug)
                seo = store_master.seo_name(st_key) if st_key else f"店舗ページ ({slug})"
                store_interest_7d[seo]["page_7d"] += c
                if is_t:
                    store_interest_7d[seo]["day_total"] += c

    top_stores = []
    for seo_name, st_vals in sorted(
        store_interest_7d.items(),
        key=lambda kv: (-(kv[1]["open_7d"] + kv[1]["page_7d"]), -kv[1]["day_total"], kv[0]),
    )[:6]:
        top_stores.append({
            "seo_name": _clean_title(seo_name),
            "total_7d": st_vals["open_7d"] + st_vals["page_7d"],
            "open_7d": st_vals["open_7d"],
            "page_7d": st_vals["page_7d"],
            "day_total": st_vals["day_total"],
        })

    # アフィリエイト枠 × 設置面 CTR（直近7日 ＆ 対象日クリック）
    imp_7d_pair = defaultdict(int)
    clk_7d_pair = defaultdict(int)
    measurable_clk_7d_pair = defaultdict(int)
    for x in series_7d:
        has_imp_day = x["aff_imp"] > 0
        for k, v in x["aff_imp_by_pair"].items():
            imp_7d_pair[k] += v
        for k, v in x["aff_clk_by_pair"].items():
            clk_7d_pair[k] += v
            if has_imp_day:
                measurable_clk_7d_pair[k] += v

    aff_breakdown = []
    all_aff_keys = set(imp_7d_pair.keys()) | set(clk_7d_pair.keys())
    for k in sorted(
        all_aff_keys,
        key=lambda key: (-clk_7d_pair[key], -imp_7d_pair[key], key),
    )[:10]:
        cid, surf = k.split("@", 1)
        imp_c = imp_7d_pair[k]
        clk_c = clk_7d_pair[k]
        m_clk_c = measurable_clk_7d_pair[k]
        day_clk = cur["aff_clk_by_pair"].get(k, 0)
        day_imp = cur["aff_imp_by_pair"].get(k, 0)
        desc = analytics.describe_label("affiliate_impression", f"{cid} @ {surf}")
        surf_ja = analytics._CTA_SURFACE_LABELS.get(surf, surf)
        aff_breakdown.append({
            "slot_id": cid,
            "surface": surf,
            "title": _clean_title(desc.get("title") or cid),
            "surface_label": _clean_title(surf_ja),
            "day_imp": day_imp,
            "day_click": day_clk,
            "imp_7d": imp_c,
            "click_7d": clk_c,
            "ctr_7d_pct": min(100.0, round((m_clk_c / imp_c) * 100, 2)) if imp_c > 0 else None,
        })

    # クッション遷移内訳（直近7日）
    cushions_7d_map = defaultdict(int)
    for x in series_7d:
        for lbl, c in x["cushions"].items():
            cushions_7d_map[lbl] += c
    top_cushions_7d = []
    for lbl, cnt in sorted(cushions_7d_map.items(), key=lambda kv: (-kv[1], kv[0]))[:5]:
        desc = analytics.describe_label("cushion_click", lbl)
        top_cushions_7d.append({
            "raw": lbl,
            "title": _clean_title(desc.get("title") or lbl),
            "sub": _clean_title(desc.get("sub") or ""),
            "count_7d": cnt,
            "day_count": cur["cushions"].get(lbl, 0),
        })

    # 自動インサイト生成（実数値根拠のみ）
    insights = []
    if wow["has_any_event"] and wow["pv"] > 0:
        wow_pct = round(((cur["pv"] - wow["pv"]) / wow["pv"]) * 100, 1)
        direction = "増加" if wow_pct > 0 else ("減少" if wow_pct < 0 else "横ばい")
        insights.append(
            f"対象日（{cur['date_label']}）の総PVは **{cur['pv']:,} PV** で、曜日バイアスを除いた前週同曜日比（{wow['date_label']}: {wow['pv']:,} PV）に対して **{wow_pct:+.1f}%（{direction}）**、直近7日平均（{avg_pv_7d:,} PV/日）比で **{cur['pv'] - avg_pv_7d:+.1f} PV** です。"
        )
    else:
        insights.append(
            f"対象日（{cur['date_label']}）の総PVは **{cur['pv']:,} PV**（前日 {prev['date_label']} 比 {_fmt_signed_diff(cur['pv'], prev['pv'], prev['has_any_event'])}）、直近7日累計は **{pv_7d:,} PV**（平均 **{avg_pv_7d:,} PV/日**）です。"
        )

    if top_subpages_7d:
        lead_sub = top_subpages_7d[0]
        insights.append(
            f"トップページ以外の下層回遊は対象日 **{cur['sub_pv']:,} PV（全体の {cur['sub_pv_share_pct']}%）**。直近7日では「{lead_sub['title']}」（`{lead_sub['path']}`: {lead_sub['pv_7d']:,} PV）が最上位の流入・回遊柱になっています。"
        )

    if aff_clk_7d > 0 or aff_imp_7d > 0:
        ctr_txt = f"{ctr_7d:.2f}%" if ctr_7d is not None else "表示計測蓄積中"
        zero_clk_high_imp = [a for a in aff_breakdown if a["imp_7d"] >= 15 and a["click_7d"] == 0]
        best_clk = next((a for a in aff_breakdown if a["click_7d"] > 0), None)
        tail_note = ""
        if best_clk:
            tail_note += f" 最主力枠は「{best_clk['title']} × {best_clk['surface']}」（7日 {best_clk['click_7d']} click）。"
        if zero_clk_high_imp:
            z0 = zero_clk_high_imp[0]
            tail_note += f" 一方「{z0['title']} × {z0['surface']}」は7日で {z0['imp_7d']} 回表示・0 click のため訴求文言改善の余地があります。"
        insights.append(
            f"収益導線は対象日 **{cur['aff_click']} click**（発生期待目安 {cur['est_occurrence_yen']:,}円 ／ 確定実勢目安 {cur['est_confirmed_yen']:,}円）、直近7日累計 **{aff_clk_7d} click**（枠CTR: **{ctr_txt}**、2ステップクッション遷移: **{cushion_7d}回**）。{tail_note}"
        )
    else:
        insights.append("対象日・直近7日ともにアフィリエイトクリックは 0 件です。人気記事から `/grooming`・`/for-women` への2ステップ導線強化が優先課題です。")

    return {
        "generated_at": now_jst.strftime("%Y-%m-%d %H:%M:%S JST"),
        "target_date": t_str,
        "target_date_label": cur["date_label"],
        "current": cur,
        "previous_day": prev,
        "same_weekday_last_week": wow,
        "window_7d": {
            "since": days_7[0],
            "until": days_7[-1],
            "pv_total": pv_7d,
            "pv_avg_daily": avg_pv_7d,
            "prev_7d_pv_total": prev_pv_7d if prev_7d_has_data else None,
            "store_open_total": store_open_7d,
            "cushion_click_total": cushion_7d,
            "aff_imp_total": aff_imp_7d,
            "aff_click_total": aff_clk_7d,
            "aff_slot_ctr_pct": ctr_7d,
            "est_occurrence_yen": aff_clk_7d * 90,
            "est_confirmed_yen": aff_clk_7d * 30,
            "daily_series": [
                {
                    "date": x["date"],
                    "date_label": x["date_label"],
                    "pv": x["pv"],
                    "sub_pv": x["sub_pv"],
                    "store_open": x["store_open"],
                    "cushion_click": x["cushion_click"],
                    "aff_imp": x["aff_imp"],
                    "aff_click": x["aff_click"],
                }
                for x in series_7d
            ],
        },
        "top_pages_day": top_pages_day,
        "top_subpages_7d": top_subpages_7d,
        "top_stores_7d": top_stores,
        "aff_breakdown": aff_breakdown,
        "top_cushions_7d": top_cushions_7d,
        "insights": insights,
    }


def format_markdown(rep: dict) -> str:
    cur = rep["current"]
    prev = rep["previous_day"]
    wow = rep["same_weekday_last_week"]
    w7 = rep["window_7d"]

    dod_pv = _fmt_signed_diff(cur["pv"], prev["pv"], prev["has_any_event"])
    wow_pv = _fmt_signed_diff(cur["pv"], wow["pv"], wow["has_any_event"])
    w7_diff = (
        _fmt_signed_diff(w7["pv_total"], w7["prev_7d_pv_total"], True)
        if w7["prev_7d_pv_total"] is not None
        else "比較データ蓄積中"
    )
    day_ctr_str = f"{cur['aff_slot_ctr_pct']:.2f}%" if cur["aff_slot_ctr_pct"] is not None else f"対PV {cur['aff_pv_ctr_pct']:.2f}% (枠表示計測0)"
    w7_ctr_str = f"{w7['aff_slot_ctr_pct']:.2f}%" if w7["aff_slot_ctr_pct"] is not None else "表示計測蓄積中"

    lines = []
    lines.append(f"## {rep['target_date_label']} 日次PV・CTR分析レポート")
    lines.append("")
    lines.append(f"- 集計実行時刻: `{rep['generated_at']}`（データ源: Google Sheets `Analytics Daily` 本番非汚染取得）")
    lines.append("")
    lines.append("### 1. コアKPIサマリ（前日確定値 ＆ 直近7日推移）")
    lines.append("")
    lines.append("| 指標 | 対象日実績 | 前日比 (DoD) | 前週同曜日比 (WoW) | 直近7日合計 (1日平均) |")
    lines.append("| --- | --- | --- | --- | --- |")
    lines.append(f"| 総ページビュー (PV) | **{cur['pv']:,} PV** | {dod_pv} | {wow_pv} | **{w7['pv_total']:,} PV** ({w7['pv_avg_daily']:,}/日・前週7日比 {w7_diff}) |")
    lines.append(f"| 下層・記事回遊PV (`/` 以外) | **{cur['sub_pv']:,} PV** ({cur['sub_pv_share_pct']}%) | {_fmt_signed_diff(cur['sub_pv'], prev['sub_pv'], prev['has_any_event'])} | {_fmt_signed_diff(cur['sub_pv'], wow['sub_pv'], wow['has_any_event'])} | コラム系当日 {cur['guide_pv']:,} PV ／ 店舗詳細当日 {cur['store_page_pv']:,} PV |")
    lines.append(f"| 店舗詳細モーダル展開 (`store_open`) | **{cur['store_open']:,} 回** | {_fmt_signed_diff(cur['store_open'], prev['store_open'], prev['has_any_event'])} | {_fmt_signed_diff(cur['store_open'], wow['store_open'], wow['has_any_event'])} | **{w7['store_open_total']:,} 回** |")
    lines.append(f"| 2ステップ内部クッション送客 | **{cur['cushion_click']:,} 回** | {_fmt_signed_diff(cur['cushion_click'], prev['cushion_click'], prev['has_any_event'])} | {_fmt_signed_diff(cur['cushion_click'], wow['cushion_click'], wow['has_any_event'])} | **{w7['cushion_click_total']:,} 回** |")
    lines.append(f"| アフィリエイトクリック ＆ CTR | **{cur['aff_click']:,} click** ({day_ctr_str}) | {_fmt_signed_diff(cur['aff_click'], prev['aff_click'], prev['has_any_event'])} | {_fmt_signed_diff(cur['aff_click'], wow['aff_click'], wow['has_any_event'])} | **{w7['aff_click_total']:,} click** / 表示 {w7['aff_imp_total']:,} (枠CTR {w7_ctr_str}) |")
    lines.append(f"| 推計収益目安 (発生90円 / 確定30円) | 発生 **{cur['est_occurrence_yen']:,}円** / 確定 **{cur['est_confirmed_yen']:,}円** | - | - | 7日発生 **{w7['est_occurrence_yen']:,}円** / 7日確定 **{w7['est_confirmed_yen']:,}円** |")
    lines.append("")

    lines.append("### 2. 直近7日間の日別PV・CV推移")
    lines.append("")
    lines.append("| 日付 | 総PV | 下層PV | 店舗展開 | クッション遷移 | PR枠表示 | PRクリック |")
    lines.append("| --- | --- | --- | --- | --- | --- | --- |")
    for row in w7["daily_series"]:
        bold = "**" if row["date"] == rep["target_date"] else ""
        lines.append(
            f"| {bold}{row['date_label']}{bold} | {bold}{row['pv']:,}{bold} | {row['sub_pv']:,} | {row['store_open']:,} | {row['cushion_click']:,} | {row['aff_imp']:,} | {bold}{row['aff_click']:,}{bold} |"
        )
    lines.append("")

    lines.append(f"### 3. 対象日（{rep['target_date_label']}）の人気ページ TOP10")
    lines.append("")
    if rep["top_pages_day"]:
        lines.append("| 順位 | ページ / パス | 当日PV | 構成比 | 前日差 | 前週同曜日差 |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for idx, p in enumerate(rep["top_pages_day"], 1):
            dod_s = f"{p['dod_diff']:+d}"
            wow_s = f"{p['wow_diff']:+d}" if p["wow_diff"] is not None else "-"
            lines.append(f"| {idx} | {p['title']} (`{p['path']}`) | **{p['pv']:,}** | {p['share_pct']}% | {dod_s} | {wow_s} |")
    else:
        lines.append("- 対象日のページビュー記録はまだありません。")
    lines.append("")

    lines.append("### 4. 直近7日の下層ページ（コラム・診断・店舗）TOP10")
    lines.append("")
    if rep["top_subpages_7d"]:
        lines.append("| 順位 | 下層ページ / パス | 7日合計PV | 当日PV | 前週7日差 |")
        lines.append("| --- | --- | --- | --- | --- |")
        for idx, sp in enumerate(rep["top_subpages_7d"], 1):
            w7_s = f"{sp['wow_7d_diff']:+d}" if sp["wow_7d_diff"] is not None else "-"
            lines.append(f"| {idx} | {sp['title']} (`{sp['path']}`) | **{sp['pv_7d']:,}** | {sp['day_pv']:,} | {w7_s} |")
    else:
        lines.append("- 下層ページの記録はまだありません。")
    lines.append("")

    lines.append("### 5. アフィリエイト枠 × 設置面 CTR内訳（直近7日）")
    lines.append("")
    if rep["aff_breakdown"]:
        lines.append("| 案件枠 (`slot_id`) | 設置面 | 当日 (click/表示) | 7日クリック | 7日表示数 | 7日枠CTR |")
        lines.append("| --- | --- | --- | --- | --- | --- |")
        for a in rep["aff_breakdown"]:
            ctr_cell = f"**{a['ctr_7d_pct']:.2f}%**" if a["ctr_7d_pct"] is not None else "-"
            lines.append(
                f"| {a['title']} (`{a['slot_id']}`) | `{a['surface']}` | {a['day_click']} / {a['day_imp']} | **{a['click_7d']}** | {a['imp_7d']} | {ctr_cell} |"
            )
    else:
        lines.append("- 直近7日のアフィリエイト表示・クリック記録はありません。")
    lines.append("")

    if rep["top_stores_7d"]:
        st_inline = " ／ ".join(
            f"{s['seo_name']}（7日計 {s['total_7d']}・当日 {s['day_total']}）" for s in rep["top_stores_7d"][:5]
        )
        lines.append(f"- **注目店舗TOP5（店舗ページPV＋詳細展開 7日計）**: {st_inline}")
        lines.append("")

    lines.append("### 6. 自動分析インサイト・今日の注目ポイント")
    lines.append("")
    for ins in rep["insights"]:
        lines.append(f"- {ins}")
    lines.append("")
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description="相席ナビ 日次PV・CTR分析レポート生成")
    parser.add_argument("--date", default="", help="対象日 YYYY-MM-DD（省略時はJST前日）")
    parser.add_argument("--format", choices=("markdown", "json"), default="markdown", help="標準出力形式")
    parser.add_argument("--save-json", default="", help="JSON保存先（省略時は .agents/reports/daily_pv_latest.json）")
    args = parser.parse_args()

    target_str = args.date.strip() or None
    rep = build_daily_report(target_str)

    reports_dir = REPO_ROOT / ".agents" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    out_json = Path(args.save_json) if args.save_json else (reports_dir / "daily_pv_latest.json")
    out_json.write_text(json.dumps(rep, ensure_ascii=False, indent=2), encoding="utf-8")

    md_text = format_markdown(rep)
    out_md = reports_dir / f"daily_pv_{rep['target_date']}.md"
    out_md.write_text(md_text, encoding="utf-8")

    if args.format == "json":
        print(json.dumps(rep, ensure_ascii=False, indent=2))
    else:
        print(md_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
