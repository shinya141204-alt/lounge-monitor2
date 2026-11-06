#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_ops_sentinel.py - 相席ナビ 本番非汚染ヘルスプローブ ＆ デプロイ前ワンコマンド総合品質ゲート

使い方:
  1) 本番公開面の非汚染ヘルスチェック:
     python3 .agents/skills/ops-sentinel/scripts/run_ops_sentinel.py --live
  2) デプロイ前ワンコマンド総合ゲート（全監査＋全単体テスト一括実行）:
     python3 .agents/skills/ops-sentinel/scripts/run_ops_sentinel.py --pre-deploy [--quick-ui]
"""

import argparse
import csv
import datetime
import io
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request
from pathlib import Path

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

REPO_ROOT = Path(__file__).resolve().parents[4]
JST = datetime.timezone(datetime.timedelta(hours=9))
AUDIT_UA = "AisekiNavi-OpsSentinel/1.0 (curl/8.0; staff_test)"


def _safe_probe(url: str, timeout_sec: float = 15.0) -> tuple[int, dict, str]:
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
        return int(resp.status), {k.lower(): v for k, v in resp.headers.items()}, resp.read().decode("utf-8", errors="replace")


def _check_robots_disallow_link(robots_txt: str) -> tuple[bool, list[str]]:
    """Ensure every User-agent group in robots.txt explicitly disallows `/link/`."""
    groups = []
    cur_agents = []
    cur_disallows = []
    for raw_ln in robots_txt.splitlines():
        ln = raw_ln.split("#", 1)[0].strip()
        if not ln or ":" not in ln:
            continue
        k, v = [p.strip() for p in ln.split(":", 1)]
        kl = k.lower()
        if kl == "user-agent":
            if cur_agents and cur_disallows:
                groups.append((cur_agents, cur_disallows))
                cur_agents, cur_disallows = [], []
            cur_agents.append(v)
        elif kl in ("disallow", "allow"):
            if kl == "disallow":
                cur_disallows.append(v)
    if cur_agents:
        groups.append((cur_agents, cur_disallows))

    missing = []
    for agents, dis in groups:
        if not any(d == "/link/" or d.startswith("/link") or d == "/" for d in dis):
            missing.extend(agents)
    return (len(groups) > 0 and len(missing) == 0), missing


def run_live_probes(base_url: str) -> dict:
    base = base_url.rstrip("/")
    results = []
    errors = []

    # 1. /api/status
    try:
        st, _, body = _safe_probe(f"{base}/api/status?staff_test=1")
        data = json.loads(body)
        stores = data.get("ranking") or data.get("stores") or data.get("data") or []
        ts_str = str(data.get("timestamp") or "")
        ok = (st == 200) and (len(stores) >= 45)
        if not ok:
            errors.append(f"/api/status 異常: status={st}, stores={len(stores)}")
        results.append({"probe": "/api/status", "status": "ok" if ok else "error", "stores": len(stores), "timestamp": ts_str})
    except Exception as exc:
        errors.append(f"/api/status エラー: {exc}")
        results.append({"probe": "/api/status", "status": "error", "detail": str(exc)})

    # 2. /api/llms-facts.json
    try:
        st, _, body = _safe_probe(f"{base}/api/llms-facts.json?staff_test=1")
        data = json.loads(body)
        ver = str(data.get("schema_version") or "")
        no_link = "/link/" not in body
        ok = (st == 200) and (ver == "2.1") and no_link
        if not ok:
            errors.append(f"/api/llms-facts.json 異常: status={st}, schema_version={ver!r}, no_link={no_link}")
        results.append({"probe": "/api/llms-facts.json", "status": "ok" if ok else "error", "schema_version": ver})
    except Exception as exc:
        errors.append(f"/api/llms-facts.json エラー: {exc}")
        results.append({"probe": "/api/llms-facts.json", "status": "error", "detail": str(exc)})

    # 3. /stats/monthly.csv
    try:
        st, hdrs, body = _safe_probe(f"{base}/stats/monthly.csv?days=30&staff_test=1")
        data_lines = [ln for ln in body.splitlines() if ln.strip() and not ln.strip().startswith("#")]
        rows = list(csv.DictReader(io.StringIO("\n".join(data_lines)))) if len(data_lines) >= 2 else []
        ok = (st == 200) and (len(rows) >= 1) and ("/link/" not in body)
        if not ok:
            errors.append(f"/stats/monthly.csv 異常: status={st}, rows={len(rows)} (Fly Postgres 接続確認が必要)")
        results.append({
            "probe": "/stats/monthly.csv",
            "status": "ok" if ok else "error",
            "rows": len(rows),
            "cache_control": hdrs.get("cache-control", ""),
        })
    except Exception as exc:
        errors.append(f"/stats/monthly.csv エラー: {exc}")
        results.append({"probe": "/stats/monthly.csv", "status": "error", "detail": str(exc)})

    # 4. /robots.txt
    try:
        st, _, body = _safe_probe(f"{base}/robots.txt?staff_test=1")
        ok_rob, missing_ua = _check_robots_disallow_link(body)
        ok = (st == 200) and ok_rob
        if not ok:
            errors.append(f"/robots.txt 異常: status={st}, Disallow /link/ 欠落グループ={missing_ua}")
        results.append({"probe": "/robots.txt", "status": "ok" if ok else "error", "missing_disallow_link": missing_ua})
    except Exception as exc:
        errors.append(f"/robots.txt エラー: {exc}")
        results.append({"probe": "/robots.txt", "status": "error", "detail": str(exc)})

    # 5. /sitemap.xml
    try:
        st, _, body = _safe_probe(f"{base}/sitemap.xml?staff_test=1")
        urls = re.findall(r"<loc>(.*?)</loc>", body)
        has_core = all(
            any(u.endswith(suf) for u in urls)
            for suf in ("/stats", "/stats/year-end-2026", "/price-comparison", "/columns")
        )
        ok = (st == 200) and (len(urls) >= 50) and has_core
        if not ok:
            errors.append(f"/sitemap.xml 異常: status={st}, url_count={len(urls)}, has_core={has_core}")
        results.append({"probe": "/sitemap.xml", "status": "ok" if ok else "error", "url_count": len(urls)})
    except Exception as exc:
        errors.append(f"/sitemap.xml エラー: {exc}")
        results.append({"probe": "/sitemap.xml", "status": "error", "detail": str(exc)})

    return {"overall": "ok" if not errors else "error", "probes": results, "errors": errors}


def run_pre_deploy_gate(quick_ui: bool = False) -> tuple[bool, list[dict]]:
    safe_env = {
        **os.environ,
        "DISABLE_SHEETS": "1",
        "DISABLE_SCHEDULER": "1",
        "PYTHONPATH": str(REPO_ROOT),
    }
    ui_cmd = [sys.executable, ".agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py"]
    if quick_ui:
        ui_cmd.append("--quick")

    steps = [
        ("AGENTS.md サイズ上限 (<24KB) ＆ CLAUDE.md シンボリックリンク検査", None),
        ("アフィリエイト・薬機法・直/link/・ブランド表記監査", [sys.executable, ".agents/skills/affiliate/scripts/parse_and_audit_affiliate.py", "--audit"]),
        ("脱AI UI・未定義CSS変数・キャッシュ完全同期監査", ui_cmd),
        ("店舗マスタ・営業時間・料金・別業態比較監査", [sys.executable, ".agents/skills/store-audit/scripts/run_full_store_audit.py"]),
        ("データモートDB整合性・14日匿名化チェック", [sys.executable, ".agents/skills/ops-sentinel/scripts/check_data_integrity.py"]),
        ("全単体テストスイート実行", [sys.executable, "-m", "unittest", "discover", "-s", "tests"]),
    ]

    step_results = []
    for label, cmd in steps:
        print(f"\n▶ [GATE] {label}")
        if cmd is None:
            agents_p = REPO_ROOT / "AGENTS.md"
            claude_p = REPO_ROOT / "CLAUDE.md"
            sz = len(agents_p.read_bytes()) if agents_p.exists() else 999999
            is_sym = claude_p.is_symlink()
            ok = (sz < 24000) and is_sym
            print(f"  AGENTS.md={sz:,} bytes (<24,000), CLAUDE.md symlink={is_sym} -> {'OK' if ok else 'FAIL'}")
            step_results.append({"step": label, "status": "ok" if ok else "error"})
            if not ok:
                return False, step_results
            continue

        proc = subprocess.run(cmd, cwd=str(REPO_ROOT), env=safe_env)
        ok = proc.returncode == 0
        step_results.append({"step": label, "status": "ok" if ok else "error", "exit_code": proc.returncode})
        if not ok:
            print(f"  ❌ ゲート失敗: {label} (exit={proc.returncode})")
            return False, step_results

    return True, step_results


def main() -> int:
    parser = argparse.ArgumentParser(description="相席ナビ 運用守護神 Ops Sentinel")
    parser.add_argument("--live", action="store_true", help="本番公開エンドポイントの非汚染ヘルスチェックを実行")
    parser.add_argument("--pre-deploy", action="store_true", help="デプロイ前ワンコマンド全品質ゲートを実行")
    parser.add_argument("--quick-ui", action="store_true", help="--pre-deploy 時にUI監査を高速静的モード(--quick)で実行")
    parser.add_argument("--base-url", default="https://aiseki-navi.com", help="本番ベースURL")
    parser.add_argument("--json", action="store_true", help="JSON形式で出力")
    args = parser.parse_args()

    if not args.live and not args.pre_deploy:
        args.live = True

    now_jst = datetime.datetime.now(JST)
    summary = {"checked_at": now_jst.strftime("%Y-%m-%d %H:%M:%S JST")}
    exit_code = 0

    if args.pre_deploy:
        ok_gate, gate_steps = run_pre_deploy_gate(quick_ui=args.quick_ui)
        summary["pre_deploy_gate"] = {"overall": "ok" if ok_gate else "error", "steps": gate_steps}
        if not ok_gate:
            exit_code = 1

    if args.live and exit_code == 0:
        live_res = run_live_probes(args.base_url)
        summary["live_probes"] = live_res
        if not args.json:
            print(f"\n=== 相席ナビ 本番非汚染ヘルスチェック ({summary['checked_at']}) ===")
            for p in live_res["probes"]:
                mark = "✅" if p["status"] == "ok" else "❌"
                meta = {k: v for k, v in p.items() if k not in ("probe", "status")}
                print(f"  {mark} {p['probe']}: {json.dumps(meta, ensure_ascii=False)}")
            if live_res["errors"]:
                print("\n❌ 検出エラー:")
                for e in live_res["errors"]:
                    print(f"  - {e}")
            else:
                print("\n✅ 本番全主要エンドポイント正常稼働を確認！")
        if live_res["overall"] != "ok":
            exit_code = 1

    reports_dir = REPO_ROOT / ".agents" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    log_file = reports_dir / f"sentinel_{now_jst.strftime('%Y%m%d_%H%M%S')}.json"
    log_file.write_text(json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
