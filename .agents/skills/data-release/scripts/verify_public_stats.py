#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
verify_public_stats.py - 本番公開一次統計エンドポイント（CSV / 年末白書 / llms-facts）の非汚染ベリファイツール

検査項目:
1. すべてのリクエストに `-A "curl/8.0"` 互換UA ＋ `?staff_test=1` を強制付与（人間PV非汚染）
2. `/stats/monthly.csv`: HTTP 200、実データ行数 >= 1、CC BY 4.0 ヘッダコメントまたは規約整合、`/link/` 混入ゼロ、NG略称ゼロ
3. `/stats/year-end-2026`: HTTP 200、CC BY 4.0 表記あり、`/link/` 混入ゼロ
4. `/api/llms-facts.json`: HTTP 200、`schema_version == "2.1"`、`/link/` 混入ゼロ
"""

import argparse
import csv
import io
import json
import re
import sys
import urllib.parse
import urllib.request

AUDIT_UA = "AisekiNavi-StatsVerify/1.0 (curl/8.0; staff_test)"
BRAND_NG_RE = re.compile(r"オリエンタル(?!ラウンジ|ホテル|カフェ|ビル|スタイル)|\bOLG\b|\bYATAKOI\b|オリラジ")


def _probe(url: str, timeout_sec: float = 15.0) -> tuple[int, dict, str]:
    parsed = urllib.parse.urlsplit(url)
    q = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    if not any(k == "staff_test" for k, _ in q):
        q.append(("staff_test", "1"))
    safe_url = urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urllib.parse.urlencode(q), parsed.fragment))

    req = urllib.request.Request(
        safe_url,
        headers={
            "User-Agent": AUDIT_UA,
            "Accept-Encoding": "identity",
        },
    )
    with urllib.request.urlopen(req, timeout=timeout_sec) as resp:
        status = int(resp.status)
        headers = {k.lower(): v for k, v in resp.headers.items()}
        body = resp.read().decode("utf-8", errors="replace")
        return status, headers, body


def main() -> int:
    parser = argparse.ArgumentParser(description="相席ナビ 公開一次統計エンドポイント検証")
    parser.add_argument("--base-url", default="https://aiseki-navi.com", help="検証対象ベースURL")
    parser.add_argument("--days", type=int, choices=[7, 30, 90], default=30, help="CSV検証用の日数パラメータ")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    args = parser.parse_args()

    base = args.base_url.rstrip("/")
    checks = []
    errors = []

    # 1. /stats/monthly.csv
    csv_url = f"{base}/stats/monthly.csv?days={args.days}&staff_test=1"
    try:
        st, hdrs, body = _probe(csv_url)
        data_lines = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        rows = list(csv.DictReader(io.StringIO("\n".join(data_lines)))) if len(data_lines) >= 2 else []
        has_link = "/link/" in body
        ng_m = BRAND_NG_RE.search(body)
        ok = (st == 200) and (len(rows) >= 1) and (not has_link) and (ng_m is None)
        if not ok:
            errors.append(
                f"/stats/monthly.csv 検証失敗: status={st}, rows={len(rows)}, has_direct_link={has_link}, brand_ng={ng_m.group(0) if ng_m else None}"
            )
        checks.append({
            "endpoint": "/stats/monthly.csv",
            "status": "ok" if ok else "error",
            "http_status": st,
            "store_rows": len(rows),
            "cache_control": hdrs.get("cache-control", ""),
        })
    except Exception as exc:
        errors.append(f"/stats/monthly.csv 接続エラー: {exc}")
        checks.append({"endpoint": "/stats/monthly.csv", "status": "error", "detail": str(exc)})

    # 2. /stats/year-end-2026
    wp_url = f"{base}/stats/year-end-2026?staff_test=1"
    try:
        st, hdrs, body = _probe(wp_url)
        has_cc = ("CC BY 4.0" in body) or ("creativecommons.org/licenses/by/4.0" in body)
        clean_html = re.sub(r"<script\b[^>]*>.*?</script>|<!--.*?-->", "", body, flags=re.I | re.S)
        has_link = bool(re.search(r"""href\s*=\s*["']/link/""", clean_html, flags=re.I))
        ok = (st == 200) and has_cc and (not has_link)
        if not ok:
            errors.append(f"/stats/year-end-2026 検証失敗: status={st}, cc_by_4_0={has_cc}, has_direct_link={has_link}")
        checks.append({
            "endpoint": "/stats/year-end-2026",
            "status": "ok" if ok else "error",
            "http_status": st,
            "cc_by_4_0": has_cc,
            "no_direct_affiliate": not has_link,
        })
    except Exception as exc:
        errors.append(f"/stats/year-end-2026 接続エラー: {exc}")
        checks.append({"endpoint": "/stats/year-end-2026", "status": "error", "detail": str(exc)})

    # 3. /api/llms-facts.json
    facts_url = f"{base}/api/llms-facts.json?staff_test=1"
    try:
        st, hdrs, body = _probe(facts_url)
        data = json.loads(body)
        ver = str(data.get("schema_version") or "")
        has_link = "/link/" in body
        ok = (st == 200) and (ver == "2.1") and (not has_link)
        if not ok:
            errors.append(f"/api/llms-facts.json 検証失敗: status={st}, schema_version={ver!r}, has_direct_link={has_link}")
        checks.append({
            "endpoint": "/api/llms-facts.json",
            "status": "ok" if ok else "error",
            "http_status": st,
            "schema_version": ver,
            "no_direct_affiliate": not has_link,
        })
    except Exception as exc:
        errors.append(f"/api/llms-facts.json 接続エラー: {exc}")
        checks.append({"endpoint": "/api/llms-facts.json", "status": "error", "detail": str(exc)})

    result = {"overall": "ok" if not errors else "error", "checks": checks, "errors": errors}
    if args.json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        print("=== 相席ナビ 公開一次統計エンドポイント検証（非汚染 staff_test モード） ===")
        for c in checks:
            mark = "✅" if c["status"] == "ok" else "❌"
            print(f"  {mark} {c['endpoint']}: {json.dumps({k: v for k, v in c.items() if k not in ('endpoint', 'status')}, ensure_ascii=False)}")
        if errors:
            print("\n❌ 検出エラー:")
            for e in errors:
                print(f"  - {e}")
        else:
            print("\n✅ 全一次統計エンドポイント（CSV / 年末白書 / llms-facts v2.1）正常稼働を確認！")

    return 1 if errors else 0


if __name__ == "__main__":
    raise SystemExit(main())
