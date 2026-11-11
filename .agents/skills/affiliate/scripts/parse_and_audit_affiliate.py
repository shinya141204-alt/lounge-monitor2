#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
parse_and_audit_affiliate.py - アフィリエイトHTMLタグ解析 ＆ 全枠整合性・薬機法・直/link/禁止・ブランド表記 自動監査スクリプト

使い方:
  1) ASPタグの解析:
     python3 .agents/skills/affiliate/scripts/parse_and_audit_affiliate.py --tag '<a href="...">...</a>'
  2) 全アフィリエイト枠・薬機法・2ステップ導線・ブランド表記の一括監査:
     python3 .agents/skills/affiliate/scripts/parse_and_audit_affiliate.py --audit
"""

import argparse
import glob
import json
import os
import re
import sys

os.environ.setdefault("DISABLE_SHEETS", "1")
os.environ.setdefault("DISABLE_SCHEDULER", "1")

REPO_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "..", ".."))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)

# 薬機法・景表法・医療広告ガイドラインでNGとなりやすい医療効能断定・誇大表現パターン
NG_COMPLIANCE_PATTERNS = [
    (r"二日酔い[がを]?(?:治る|防ぐ|解消|ゼロ|なくなる|効く)", "薬機法NG（医薬品効能の標榜：『乾杯前後のコンディションサポート』『翌朝すっきり習慣』等へ置換）"),
    (r"肝臓[がを]?(?:治す|回復|改善|強くする)", "薬機法NG（臓器機能改善の断定）"),
    (r"永久に毛が生えない|100%\s*脱毛|完全に無毛", "医療広告ガイドライン・景表法NG（完全保証・誇大表現）"),
    (r"ニキビが治る|シミが消える|肌荒れを治療", "薬機法NG（化粧品での治療効能標榜：『うるおいを与える』『テカリ・乾燥を防ぐ』等へ置換）"),
    (r"絶対に出会える|100%\s*付き合える|必ず彼女ができる", "景表法NG（断定・優良誤認リスク）"),
    (r"(?:飲むだけで|塗るだけで)?(?:確実に痩せる|脂肪が燃える|体重が激減)", "薬機法・景表法NG（痩身効能の断定）"),
    (r"肌が若返る|細胞が再生する|アンチエイジング効果[がを]保証", "薬機法NG（老化回復・細胞再生の標榜）"),
    (r"医学的に効果が実証済み|医師も100%認める", "景表法・医療広告ガイドラインNG（権威付け優良誤認）"),
    (r"(?:必ず|100%|即日確実に)モテる", "景表法NG（断定表現）"),
]

# 公開可視テキストにおけるブランド略称・旧称NGパターン（AGENTS.md §3）
BRAND_NG_TEXT_PATTERNS = [
    (r"オリエンタル(?!ラウンジ|ホテル|カフェ|ビル|スタイル)", "ブランド表記NG（単体『オリエンタル』は禁止 → 『オリエンタルラウンジ』へ統一）"),
    (r"\bOLG\b", "ブランド表記NG（内部コード『OLG』の公開露出禁止 → 『オリエンタルラウンジ』等へ統一）"),
    (r"\bYATAKOI\b", "ブランド表記NG（英語『YATAKOI』は禁止 → 『ヤタコイ』へ統一）"),
    (r"オリラジ", "ブランド表記NG（旧略称『オリラジ』は禁止 → 『オリエンタルラウンジ』へ統一）"),
    (r"XIX[（(](?:シックス|キックス|ナインティーン)[）)]", "ブランド読み仮名NG（『XIX』の読みは『ジックス』 → 『XIX（ジックス）』へ統一）"),
]

DIRECT_LINK_RE = re.compile(r"""(?:href\s*=\s*["']|url\s*\(\s*["']?)/link/[a-zA-Z0-9_-]+""", re.I)


def parse_asp_tag(raw: str) -> dict:
    text = (raw or "").strip()
    href_match = re.search(r'<a\b[^>]*\bhref=["\']([^"\']+)["\']', text, flags=re.I)
    dest_url = href_match.group(1).strip() if href_match else ""
    if not dest_url and re.match(r"^https?://", text, flags=re.I):
        dest_url = text.splitlines()[0].strip()

    imgs = re.findall(r"<img\b[^>]*>", text, flags=re.I)
    banner_img = ""
    tracking_img = ""
    for img_tag in imgs:
        src_m = re.search(r'\bsrc=["\']([^"\']+)["\']', img_tag, flags=re.I)
        if not src_m:
            continue
        src = src_m.group(1).strip()
        is_1x1 = (
            re.search(r'\bwidth=["\']?1["\']?\b', img_tag, flags=re.I)
            and re.search(r'\bheight=["\']?1["\']?\b', img_tag, flags=re.I)
        ) or ("0.gif" in src)
        if is_1x1 and not tracking_img:
            tracking_img = src
        elif not banner_img:
            banner_img = src

    asp = "unknown"
    lower_u = dest_url.lower()
    if "a8.net" in lower_u:
        asp = "A8.net"
    elif "accesstrade.net" in lower_u:
        asp = "アクセストレード"
    elif "valuecommerce.com" in lower_u:
        asp = "バリューコマース"
    elif "afb" in lower_u or "afi-b.com" in lower_u:
        asp = "afb"
    elif "rentracks" in lower_u:
        asp = "レントラックス"

    return {
        "asp": asp,
        "dest_url": dest_url,
        "banner_img": banner_img,
        "tracking_img": tracking_img,
    }


def _strip_to_visible_public_text(html_raw: str) -> str:
    """Remove scripts, styles, comments, Jinja blocks, and HTML tags so only public visible prose is scanned."""
    s = re.sub(r"<!--.*?-->", " ", html_raw, flags=re.S)
    s = re.sub(r"\{#.*?#\}", " ", s, flags=re.S)
    s = re.sub(r"\{%.*?%\}", " ", s, flags=re.S)
    s = re.sub(r"\{\{.*?\}\}", " ", s, flags=re.S)
    s = re.sub(r"<script\b[^>]*>.*?</script>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<style\b[^>]*>.*?</style>", " ", s, flags=re.I | re.S)
    s = re.sub(r"<[^>]+>", " ", s)
    return s


def _strip_negated_rule_quotes(text: str) -> str:
    """Exclude explicit editorial notes explaining forbidden/bad examples in meta/docs if any."""
    return re.sub(r"（例[:：][^）]*）", " ", text)


def run_audit() -> int:
    import affiliate_manager
    import app as flask_app
    import pdca_engine

    errors = []
    warnings = []

    print("=== [1/5] affiliate_manager.py ＆ app.py ＆ pdca_engine.py 整合性チェック ===")
    for slot in affiliate_manager.AFFILIATE_SLOTS:
        sid = slot["slot_id"]
        url = slot.get("default_url") or ""
        ok, msg = affiliate_manager.validate_destination_url(url)
        if not ok or not url:
            errors.append(f"枠 '{sid}' の default_url が不正または空です: {msg}")
        if sid not in flask_app.AFFILIATE_REDIRECTS:
            warnings.append(f"枠 '{sid}' が app.py の AFFILIATE_REDIRECTS フォールバック辞書に未記載です。")
        for al in slot.get("aliases", []):
            resolved = affiliate_manager.canonical_slot_id(al)
            if resolved != sid:
                errors.append(f"エイリアス '{al}' の解決先が不一致です: {resolved} != {sid}")
        cat_key = pdca_engine._classify_offer_category(sid)
        if not cat_key or cat_key == "other":
            warnings.append(f"枠 '{sid}' が pdca_engine.OFFER_CATEGORIES で未分類 ('{cat_key}') です。")

    status_check = affiliate_manager.get_affiliate_slots_status(allow_blocking=False)
    for st in status_check.get("slots", []):
        if st.get("is_duplicate_url"):
            warnings.append(
                f"枠 '{st['slot_id']}' のURLが他枠（{', '.join(st.get('shared_with_labels') or [])}）と重複しています（別ASP案件への分離を推奨）。"
            )
        if st.get("is_unmonetized_url"):
            warnings.append(
                f"枠 '{st['slot_id']}' のURL ({st.get('active_url')}) がASP計測タグなしの公式直URLです（/staff/analytics でASPリンク設定を推奨）。"
            )

    print(f"  登録スロット数: {len(affiliate_manager.AFFILIATE_SLOTS)}枠")

    print("\n=== [2/5] static/affiliate_config.js リンク＆トラッキング検証 ===")
    cfg_path = os.path.join(REPO_ROOT, "static", "affiliate_config.js")
    with open(cfg_path, "r", encoding="utf-8") as fp:
        cfg_raw = fp.read()

    link_ids = re.findall(r"url:\s*['\"]/link/([a-z0-9_]+)['\"]", cfg_raw)
    for lid in link_ids:
        if not affiliate_manager.canonical_slot_id(lid) and lid not in flask_app.AFFILIATE_REDIRECTS:
            errors.append(f"static/affiliate_config.js 内の '/link/{lid}' が affiliate_manager / app.py で解決できません。")
    print(f"  affiliate_config.js 内リンク枠数: {len(link_ids)}件")

    print("\n=== [3/5] 薬機法・景表法NG表現スキャン（全公開テンプレート・設定ファイル） ===")
    public_templates = [
        p for p in sorted(glob.glob(os.path.join(REPO_ROOT, "templates", "*.html")))
        if not os.path.basename(p).startswith("staff_")
    ]
    scan_targets = [
        cfg_path,
        os.path.join(REPO_ROOT, "affiliate_manager.py"),
        os.path.join(REPO_ROOT, "pdca_engine.py"),
    ] + public_templates

    for path in scan_targets:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fp:
            content = fp.read()
        rel = os.path.relpath(path, REPO_ROOT)
        target_text = _strip_to_visible_public_text(content) if path.endswith(".html") else content
        for pat, reason in NG_COMPLIANCE_PATTERNS:
            m = re.search(pat, target_text)
            if m:
                errors.append(f"{rel}: NG表現『{m.group(0)}』を検出 — {reason}")
    print(f"  走査ファイル数: {len(scan_targets)}件")

    print("\n=== [4/5] 編集コラム・一次統計ページの直 `/link/` 混入ゼロ検査（AGENTS.md §4.2 / §9） ===")
    no_direct_link_files = (
        sorted(glob.glob(os.path.join(REPO_ROOT, "templates", "guide_*.html")))
        + sorted(glob.glob(os.path.join(REPO_ROOT, "templates", "stats_*.html")))
        + [os.path.join(REPO_ROOT, "scheduled_articles.py")]
    )
    direct_violations = 0
    for path in no_direct_link_files:
        if not os.path.exists(path):
            continue
        with open(path, "r", encoding="utf-8") as fp:
            raw = fp.read()
        cleaned = re.sub(r"<!--.*?-->|\{#.*?#\}", "", raw, flags=re.S)
        found = DIRECT_LINK_RE.findall(cleaned)
        if found:
            direct_violations += len(found)
            rel = os.path.relpath(path, REPO_ROOT)
            errors.append(f"{rel}: 直 `/link/` 参照を検出 ({found[0]}) — コラム・一次統計ページには直貼りせず2ステップ内部導線にしてください。")
    print(f"  検査対象: {len(no_direct_link_files)}ファイル / 違反: {direct_violations}件")

    print("\n=== [5/5] 公開テンプレート可視テキストのブランドNG略称検閲（AGENTS.md §3） ===")
    brand_violations = 0
    for path in public_templates:
        fn = os.path.basename(path)
        if fn.startswith("_"):
            continue
        with open(path, "r", encoding="utf-8") as fp:
            raw = fp.read()
        vis = _strip_negated_rule_quotes(_strip_to_visible_public_text(raw))
        rel = os.path.relpath(path, REPO_ROOT)
        for pat, reason in BRAND_NG_TEXT_PATTERNS:
            m = re.search(pat, vis)
            if m:
                brand_violations += 1
                ctx_start = max(0, m.start() - 12)
                ctx_end = min(len(vis), m.end() + 12)
                snippet = re.sub(r"\s+", " ", vis[ctx_start:ctx_end]).strip()
                errors.append(f"{rel}: 『{snippet}』 — {reason}")
    print(f"  公開HTML検査: {len(public_templates)}ファイル / ブランド表記違反: {brand_violations}件")

    if warnings:
        print("\n⚠️ 注意事項:")
        for w in warnings:
            print(f"  - {w}")

    if errors:
        print("\n❌ エラーが検出されました:")
        for e in errors:
            print(f"  - {e}")
        return 1

    print("\n✅ 全アフィリエイト枠のURL解決・薬機法・直/link/ゼロ・ブランド表記コンプライアンス監査 OK！")
    return 0


def main():
    parser = argparse.ArgumentParser(description="相席ナビ アフィリエイトタグ解析＆統合コンプライアンス監査スクリプト")
    parser.add_argument("--tag", help="解析するASP HTMLタグまたはURL", default="")
    parser.add_argument("--audit", action="store_true", help="全アフィリエイト枠・薬機法・直/link/・ブランド表記監査を実行")
    args = parser.parse_args()

    if args.tag:
        parsed = parse_asp_tag(args.tag)
        print(json.dumps(parsed, ensure_ascii=False, indent=2))
        if not args.audit:
            return

    sys.exit(run_audit())


if __name__ == "__main__":
    main()
