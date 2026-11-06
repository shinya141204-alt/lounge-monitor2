#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
audit_anti_ai_ui.py - 相席ナビ 脱AIデザイン・未定義CSS変数・キャッシュ完全同期・UI機能性 自動監査スクリプト

使い方:
  1) フル監査（静的4検査 ＋ Playwright実ブラウザUI回帰テスト一式）:
     python3 .agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py
  2) 高速クイック監査（1秒以内：静的4検査のみ）:
     python3 .agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py --quick
"""

import argparse
import glob
import os
import re
import subprocess
import sys

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))

EMOJI_RE = re.compile(
    r"[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF]"
)
NEON_GRADIENT_RE = re.compile(
    r"linear-gradient\([^)]*(?:#ec4899|#8b5cf6|#f43f5e)[^)]*\)",
    re.IGNORECASE,
)
HTML_OVERFLOW_RE = re.compile(
    r"(?:^|\}|\*/)\s*html\s*\{[^}]*overflow(?:-[xy])?\s*:",
    re.MULTILINE | re.DOTALL,
)


def scan_file_for_ai_patterns(filepath: str) -> dict:
    with open(filepath, "r", encoding="utf-8") as fp:
        raw = fp.read()
    clean = re.sub(r"<!--.*?-->|\{#.*?#\}|/\*.*?\*/", "", raw, flags=re.S)

    heading_or_btn_blocks = re.findall(
        r"<(?:button|h1|h2|h3|th)\b[^>]*>(.*?)</(?:button|h1|h2|h3|th)>",
        clean,
        flags=re.I | re.S,
    )
    control_emojis = []
    for blk in heading_or_btn_blocks:
        txt = re.sub(r"<[^>]+>", "", blk)
        found = [ch for ch in EMOJI_RE.findall(txt) if ch not in ("✕", "✖", "×")]
        if found:
            control_emojis.extend(found)

    neon_grads = NEON_GRADIENT_RE.findall(clean)
    return {
        "file": os.path.relpath(filepath, REPO_ROOT),
        "control_emoji_count": len(control_emojis),
        "sample_emojis": "".join(control_emojis[:10]),
        "neon_gradient_count": len(neon_grads),
    }


def check_calm_css_scroll_safety() -> list:
    issues = []
    calm_path = os.path.join(REPO_ROOT, "static", "calm.css")
    if os.path.exists(calm_path):
        with open(calm_path, "r", encoding="utf-8") as fp:
            css = re.sub(r"/\*.*?\*/", "", fp.read(), flags=re.S)
        if HTML_OVERFLOW_RE.search(css):
            issues.append("CRITICAL: static/calm.css の html セレクタに overflow 指定があります（PC縦スクロール不能の原因）")
    return issues


def check_undefined_css_vars() -> list:
    """Detect any CSS custom property var(--foo) that is never defined anywhere in static/*.css or templates."""
    defined = set()
    css_files = sorted(glob.glob(os.path.join(REPO_ROOT, "static", "*.css")))
    html_files = sorted(glob.glob(os.path.join(REPO_ROOT, "templates", "*.html")))
    js_files = sorted(glob.glob(os.path.join(REPO_ROOT, "static", "*.js")))

    def_re = re.compile(r"(--[a-zA-Z0-9_-]+)\s*:")
    js_set_re = re.compile(r"""setProperty\(\s*['"](--[a-zA-Z0-9_-]+)['"]""")
    use_no_fallback_re = re.compile(r"var\(\s*(--[a-zA-Z0-9_-]+)\s*\)")

    for p in css_files + html_files:
        with open(p, "r", encoding="utf-8") as fp:
            txt = re.sub(r"/\*.*?\*/|<!--.*?-->", "", fp.read(), flags=re.S)
        defined.update(def_re.findall(txt))

    for p in js_files + html_files:
        with open(p, "r", encoding="utf-8") as fp:
            txt = fp.read()
        defined.update(js_set_re.findall(txt))

    issues = []
    for p in css_files:
        with open(p, "r", encoding="utf-8") as fp:
            txt = re.sub(r"/\*.*?\*/", "", fp.read(), flags=re.S)
        rel = os.path.relpath(p, REPO_ROOT)
        for vname in sorted(set(use_no_fallback_re.findall(txt))):
            if vname not in defined:
                issues.append(f"{rel}: 未定義のCSSカスタムプロパティ参照 var({vname})")

    for p in html_files:
        with open(p, "r", encoding="utf-8") as fp:
            raw = re.sub(r"<!--.*?-->|\{#.*?#\}", "", fp.read(), flags=re.S)
        style_blocks = re.findall(r"<style\b[^>]*>(.*?)</style>", raw, flags=re.I | re.S)
        rel = os.path.relpath(p, REPO_ROOT)
        joined = "\n".join(style_blocks)
        for vname in sorted(set(use_no_fallback_re.findall(joined))):
            if vname not in defined:
                issues.append(f"{rel}: 未定義のCSSカスタムプロパティ参照 var({vname})")
    return issues


def check_cache_version_sync() -> list:
    """Verify cache-busting query versions match across sw.js, index.html, _nav_loader.html, all templates, and test suite."""
    issues = []
    sw_path = os.path.join(REPO_ROOT, "static", "sw.js")
    idx_path = os.path.join(REPO_ROOT, "templates", "index.html")
    nav_path = os.path.join(REPO_ROOT, "templates", "_nav_loader.html")
    test_path = os.path.join(REPO_ROOT, "tests", "test_ui_audit_regression.py")

    if not (os.path.exists(sw_path) and os.path.exists(idx_path)):
        return issues

    with open(sw_path, "r", encoding="utf-8") as fp:
        sw_txt = fp.read()
    with open(idx_path, "r", encoding="utf-8") as fp:
        idx_txt = fp.read()

    asset_patterns = {
        "calm.css": r"/static/calm\.css(?:\?v=[^\"'\s>)]+)?",
        "portal.css": r"/static/portal\.css(?:\?v=[^\"'\s>)]+)?",
        "style.css": r"/static/style\.css(?:\?v=[^\"'\s>)]+)?",
        "app.js": r"/static/app\.js(?:\?v=[^\"'\s>)]+)?",
        "affiliate_config.js": r"/static/affiliate_config\.js(?:\?v=[^\"'\s>)]+)?",
    }

    canonical = {}
    for name, pat in asset_patterns.items():
        m = re.search(pat, sw_txt)
        if not m:
            issues.append(f"static/sw.js に {name} のプリキャッシュ定義が見つかりません")
        else:
            canonical[name] = m.group(0)

    sw_url_m = re.search(r"/sw\.js\?v=[^\"'\s>)]+", idx_txt)
    if sw_url_m:
        canonical["sw.js"] = sw_url_m.group(0)
        sw_ver = canonical["sw.js"].split("?v=", 1)[-1]
        expected_cache = f"aiseki-navi-v{sw_ver}"
        if f"CACHE_NAME = '{expected_cache}'" not in sw_txt and f'CACHE_NAME = "{expected_cache}"' not in sw_txt:
            issues.append(f"static/sw.js の CACHE_NAME が {expected_cache} と不一致です")

    if os.path.exists(nav_path) and "sw.js" in canonical:
        with open(nav_path, "r", encoding="utf-8") as fp:
            nav_txt = fp.read()
        for m in re.findall(r"/sw\.js(?:\?v=[^\"'\s>)]+)?", nav_txt):
            if m != canonical["sw.js"]:
                issues.append(f"templates/_nav_loader.html の sw.js 参照 ({m}) が正本 ({canonical['sw.js']}) と不一致です")

    for tpath in sorted(glob.glob(os.path.join(REPO_ROOT, "templates", "*.html"))):
        fname = os.path.basename(tpath)
        with open(tpath, "r", encoding="utf-8") as fp:
            txt = re.sub(r"\{#.*?#\}|<!--.*?-->|/\*.*?\*/", "", fp.read(), flags=re.S)
        for name, pat in asset_patterns.items():
            if name not in canonical:
                continue
            for ref in re.findall(pat, txt):
                if ref != canonical[name]:
                    issues.append(f"templates/{fname}: {ref} != 正本 {canonical[name]}")
        if "sw.js" in canonical:
            for ref in re.findall(r"/sw\.js(?:\?v=[^\"'\s>)]+)?", txt):
                if ref != canonical["sw.js"]:
                    issues.append(f"templates/{fname}: {ref} != 正本 {canonical['sw.js']}")

    if os.path.exists(test_path):
        with open(test_path, "r", encoding="utf-8") as fp:
            test_txt = fp.read()
        for name, cval in canonical.items():
            if cval not in test_txt:
                issues.append(f"tests/test_ui_audit_regression.py の期待定数に '{cval}' が同期されていません")

    return issues


def main():
    parser = argparse.ArgumentParser(description="相席ナビ 脱AIデザイン＆機能性UX 監査ツール")
    parser.add_argument("--target", help="重点チェックするテンプレートまたはCSSの相対パス", default="")
    parser.add_argument("--quick", action="store_true", help="高速静的検査（スクロール・絵文字・未定義CSS変数・キャッシュ同期）のみ実行")
    parser.add_argument("--skip-tests", action="store_true", help="--quick と同義（後方互換）")
    args = parser.parse_args()

    quick_mode = bool(args.quick or args.skip_tests)
    has_error = False

    print("=== [1/5] ルートCSS・PC縦スクロール安全性チェック ===")
    scroll_issues = check_calm_css_scroll_safety()
    if scroll_issues:
        for iss in scroll_issues:
            print(f"  ❌ {iss}")
        has_error = True
    else:
        print("  ✅ static/calm.css の html ルートスクロール安全性 OK")

    print("\n=== [2/5] 未定義CSSカスタムプロパティ（var(--...)）静的検査 ===")
    var_issues = check_undefined_css_vars()
    if var_issues:
        for iss in var_issues[:20]:
            print(f"  ❌ {iss}")
        has_error = True
    else:
        print("  ✅ 全CSS・テンプレート内のフォールバックなし var(--...) 定義整合 OK")

    print("\n=== [3/5] キャッシュバスティング完全同期チェック（sw.js / index / _nav_loader / 全HTML / テスト） ===")
    cache_issues = check_cache_version_sync()
    if cache_issues:
        for iss in cache_issues[:25]:
            print(f"  ❌ {iss}")
        has_error = True
    else:
        print("  ✅ 全アセットクエリ文字列・Service Worker・テスト期待値 完全一致 OK")

    print("\n=== [4/5] 脱AIっぽさ（見出し・ボタン内の絵文字密度／派手グラデ）ホットスポット診断 ===")
    targets = []
    if args.target:
        tpath = os.path.join(REPO_ROOT, args.target) if not os.path.isabs(args.target) else args.target
        if os.path.exists(tpath):
            targets.append(tpath)
    else:
        tdir = os.path.join(REPO_ROOT, "templates")
        for fn in sorted(os.listdir(tdir)):
            if fn.endswith(".html") and not fn.startswith("staff_"):
                targets.append(os.path.join(tdir, fn))

    rows = [scan_file_for_ai_patterns(p) for p in targets]
    rows.sort(key=lambda r: (r["neon_gradient_count"], r["control_emoji_count"]), reverse=True)
    for r in rows[:10]:
        if r["control_emoji_count"] > 0 or r["neon_gradient_count"] > 0:
            print(
                f"  - {r['file']}: 見出し/ボタン絵文字={r['control_emoji_count']}個 ({r['sample_emojis']}) "
                f"| インライン派手グラデ={r['neon_gradient_count']}箇所"
            )
    if all(r["control_emoji_count"] == 0 and r["neon_gradient_count"] == 0 for r in rows):
        print("  ✅ 対象ファイルに見出し/ボタン絵文字過多・派手グラデなし")

    if has_error:
        sys.exit(1)

    if not quick_mode:
        print("\n=== [5/5] UI回帰テストスイート（WCAGコントラスト・Playwright実画面検証） ===")
        cmd = [sys.executable, "-m", "unittest", "tests.test_ui_audit_regression"]
        safe_env = {**os.environ, "DISABLE_SHEETS": "1", "DISABLE_SCHEDULER": "1"}
        res = subprocess.run(cmd, cwd=REPO_ROOT, env=safe_env)
        if res.returncode != 0:
            print("  ❌ UI回帰テストで失敗が検出されました。修正してから再実行してください。")
            sys.exit(res.returncode)
        print("  ✅ 全UI・コントラスト・スクロール回帰テスト合格！")
    else:
        print("\n✅ クイック静的UI監査（1〜4）すべて合格！")


if __name__ == "__main__":
    main()
