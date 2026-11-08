#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
run_claude_review.py - Antigravity から Claude Code (`claude -p`) を安全・高速に呼び出す連携ラッパー v2.1

主な特徴:
- 既定は `--permission-mode plan`（完全読み取り専用）で、勝手なファイル書き換え・コマンド破壊を完全防止
- `--allow-edit` 指定時のみ `--permission-mode acceptEdits` ＋ `--tools Read,Edit,Write,Glob,Grep` を有効化し、
  `Bash` / `WebFetch` や `.env`・認証鍵・`fly.toml` への変更を物理遮断した安全な「ファイル編集限定モード」で直接書き込みを実行
- 対象ファイル自動インライン添付（読み取り専用時：Fable 最大 120,000 文字 / Opus 最大 40,000 文字）＋ `stdin` 投入により、
  Read ツール往復によるタイムアウトと macOS `ARG_MAX` 上限エラーを解消（`--fast-no-tools` でツール往復ゼロ推論にも対応）
- 日常レビュー (`claude-opus-5-5`) と切り札 100万トークン最上位モデル (`claude-fable-5-1`) のハイブリッド運用:
  1. `strategy`       : 収益化・SEO・データモート引用獲得・導線設計の「全体戦略」
  2. `audit`          : 複雑な数理ロジック・Fly PG 512MB OOM安全性・大型改修の「最終バグ・セキュリティ監査」
  3. `flagship`       : 看板コラム・note 勝負記事の「構成＆脱AIの徹底推敲（`--allow-edit` で直接改稿保存も可）」
  4. `consult --fable`: スキル策定・自由テーマの深層アーキテクチャ相談（`strategy` へ強制変換せず維持）
- レビュー・編集結果を `.agents/skills/claude-review/logs/`（Git除外）へ自動アーカイブし、所要秒・コスト・変更ファイルを要約表示
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
LOG_DIR = Path(__file__).resolve().parents[1] / "logs"
JST = datetime.timezone(datetime.timedelta(hours=9))

CANDIDATE_CLAUDE_PATHS = [
    shutil.which("claude"),
    str(Path.home() / ".local" / "bin" / "claude"),
    "/opt/homebrew/bin/claude",
    "/usr/local/bin/claude",
]

MODEL_ALIASES = {
    "fable": "claude-fable-5-1",
    "fable-5-1": "claude-fable-5-1",
    "claude-fable": "claude-fable-5-1",
    "fable-1m": "claude-fable-5-1[1m]",
    "opus": "claude-opus-5-5",
    "opus-5-5": "claude-opus-5-5",
    "claude-opus": "claude-opus-5-5",
    "sonnet": "claude-sonnet-5-5",
    "sonnet-5-5": "claude-sonnet-5-5",
    "claude-sonnet": "claude-sonnet-5-5",
    "haiku": "claude-haiku-5-5",
    "haiku-5-5": "claude-haiku-5-5",
    "claude-haiku": "claude-haiku-5-5",
}

FABLE_NATIVE_MODES = {"strategy", "audit", "flagship", "architect", "article-fable"}
ALL_MODES = ("code", "article", "consult", "strategy", "audit", "flagship", "architect", "article-fable")
SAFE_EDIT_TOOLS = "Read,Edit,Write,Glob,Grep"
EDIT_DISALLOWED_SPECS = (
    "Bash,WebFetch,Edit(.env),Write(.env),Edit(.env.*),Write(.env.*),"
    "Edit(*credentials*),Write(*credentials*),Edit(*secret*),Write(*secret*),"
    "Edit(fly.toml),Write(fly.toml)"
)


def find_claude_bin() -> str | None:
    for p in CANDIDATE_CLAUDE_PATHS:
        if p and os.path.isfile(p) and os.access(p, os.X_OK):
            return p
    return None


def resolve_model(raw_model: str | None, mode: str, force_fable: bool) -> str:
    if force_fable:
        if raw_model and "1m" in raw_model.lower():
            return "claude-fable-5-1[1m]"
        return "claude-fable-5-1"
    if raw_model:
        cleaned = raw_model.strip()
        return MODEL_ALIASES.get(cleaned.lower(), cleaned)
    if mode in FABLE_NATIVE_MODES:
        return "claude-fable-5-1"
    return "claude-opus-5-5"


def is_fable_model(model_name: str) -> bool:
    return "fable" in (model_name or "").lower()


def resolve_effective_mode(
    mode: str,
    model_name: str | bool = "",
    force_fable: bool = False,
    *,
    no_promote: bool = False,
) -> str:
    """Promote standard modes (`code`/`article`/`consult`) to deep Fable presets when Fable is selected,
    preserving full backward compatibility with `(mode, model_name, force_fable)` callers.
    Use `--mode architect` or `--no-promote` for general Fable 5.1 architecture/skill consultations."""
    if isinstance(model_name, bool):
        force_fable = model_name
        model_name = ""
    if mode == "article-fable":
        return "flagship"
    if no_promote or mode == "architect":
        return mode
    if force_fable or is_fable_model(str(model_name)):
        if mode == "code":
            return "audit"
        if mode == "article":
            return "flagship"
        if mode == "consult":
            return "strategy"
    return mode


def _inspect_cached_catalog() -> dict[str, dict]:
    catalog_dir = Path.home() / ".claude" / "cache" / "model-catalog"
    found: dict[str, dict] = {}
    if not catalog_dir.is_dir():
        return found
    for path_str in sorted(glob.glob(str(catalog_dir / "*.json"))):
        try:
            raw = json.loads(Path(path_str).read_text(encoding="utf-8"))
            items = []
            if isinstance(raw, dict):
                items = (
                    (((raw.get("catalog") or {}).get("config") or {}).get("models"))
                    or raw.get("models")
                    or raw.get("data")
                    or []
                )
            elif isinstance(raw, list):
                items = raw
            if not isinstance(items, list):
                continue
            for item in items:
                if isinstance(item, dict) and item.get("id"):
                    found[str(item["id"])] = item
        except Exception:
            continue
    return found


def run_doctor() -> int:
    claude_bin = find_claude_bin()
    if not claude_bin:
        print("❌ Claude Code (`claude`) が見つかりません。")
        print("以下のコマンドで公式ネイティブ版をインストールしてください：")
        print("  curl -fsSL https://claude.ai/install.sh | bash")
        return 1

    print(f"✅ Claude Code バイナリ検出: {claude_bin}")
    try:
        ver = subprocess.run(
            [claude_bin, "--version"],
            capture_output=True,
            text=True,
            timeout=10,
        )
        print(f"✅ バージョン: {(ver.stdout or ver.stderr).strip()}")
    except Exception as exc:
        print(f"⚠️ バージョン確認エラー: {exc}")
        return 1

    catalog = _inspect_cached_catalog()
    if "claude-fable-5-1" in catalog:
        fable_info = catalog["claude-fable-5-1"]
        caps = fable_info.get("capabilities") if isinstance(fable_info.get("capabilities"), dict) else {}
        ctx_len = (
            caps.get("max_input_tokens")
            or caps.get("context_window")
            or fable_info.get("max_input_tokens")
            or 1000000
        )
        disp_name = fable_info.get("name") or "Claude Fable 5.1"
        print(f"✅ 最上位モデル検出: {disp_name} (`claude-fable-5-1` / 最大入力 {int(ctx_len):,} トークン)")
    else:
        print("ℹ️ モデル構成: 日常=claude-opus-5-5 / 切り札=claude-fable-5-1（`--fable` または `strategy`/`audit`/`flagship` モード）")

    try:
        auth = subprocess.run(
            [claude_bin, "auth", "status"],
            capture_output=True,
            text=True,
            timeout=10,
            cwd=str(REPO_ROOT),
        )
        out = (auth.stdout or auth.stderr or "").strip()
        if out:
            start_i = out.find("{")
            end_i = out.rfind("}")
            if start_i != -1 and end_i != -1:
                data = json.loads(out[start_i : end_i + 1])
                if data.get("loggedIn"):
                    sub_type = data.get("subscriptionType") or data.get("authMethod") or "ok"
                    print(f"✅ ログイン状態: 認証済み ({sub_type})")
                    return 0
                print("⚠️ ログイン状態: 未ログイン（ターミナルで `claude auth login` を1回実行してください）")
                return 0
    except Exception:
        pass

    print("ℹ️ 初回ログインがまだの場合は、ターミナルで一度だけ `claude`（または `~/.local/bin/claude auth login`）を実行してください。")
    return 0


def _extract_rate_windows(stdout_text: str) -> dict[str, dict]:
    """Extract rate-limit windows from Claude stream-json output (supports v2.1+ unifiedWindows & legacy flat events)."""
    rate_events: dict[str, dict] = {}
    for line in (stdout_text or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") != "rate_limit_event":
            continue
        info = ev.get("rate_limit_info") or {}
        unified = info.get("unifiedWindows")
        if isinstance(unified, dict):
            for win_key, win_info in unified.items():
                if not isinstance(win_info, dict):
                    continue
                merged = dict(win_info)
                merged.setdefault("status", info.get("status") or "allowed")
                if merged.get("resetsAt") is None and info.get("resetsAt") is not None:
                    merged["resetsAt"] = info.get("resetsAt")
                k = str(win_key)
                prev = rate_events.get(k)
                if prev is None or merged.get("utilization") is not None or prev.get("utilization") is None:
                    rate_events[k] = merged

        rl_type = str(info.get("rateLimitType") or "").strip()
        if rl_type:
            prev = rate_events.get(rl_type)
            if prev is None or (prev.get("utilization") is None and info.get("utilization") is not None):
                rate_events[rl_type] = info
    return rate_events


def run_usage_check() -> int:
    """Query live Claude rate-limit utilization via a minimal Haiku stream-json probe."""
    claude_bin = find_claude_bin()
    if not claude_bin:
        print("❌ Claude Code (`claude`) が未インストールです。", file=sys.stderr)
        return 2

    cmd = [
        claude_bin,
        "-p",
        "--model",
        "claude-haiku-5-5",
        "--tools",
        "",
        "--output-format",
        "stream-json",
        "--verbose",
    ]
    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

    try:
        proc = subprocess.run(
            cmd,
            input="OK",
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("⚠️ 使用量チェックがタイムアウトしました。", file=sys.stderr)
        return 3

    rate_events = _extract_rate_windows(proc.stdout or "")
    if proc.returncode != 0 and not rate_events:
        print(f"⚠️ 使用量チェック失敗 (exit={proc.returncode}): {(proc.stderr or proc.stdout or '').strip()}", file=sys.stderr)
        return proc.returncode

    if not rate_events:
        print("ℹ️ レートリミットイベントが返却されませんでした（現在十分に余裕があります）。")
        return 0

    label_map = {
        "five_hour": "⏱️ 5時間枠（短期バースト上限）",
        "seven_day": "📅 7日間枠（週間トータル上限）",
        "seven_day_opus": "💎 7日間枠（上位モデル専用枠）",
    }
    print("📊 Claude Code 現在の使用量・残り枠ステータス")
    for key in ("five_hour", "seven_day", "seven_day_opus"):
        if key not in rate_events:
            continue
        info = rate_events.pop(key)
        _print_rate_line(label_map.get(key, key), info)
    for key, info in rate_events.items():
        _print_rate_line(f"🔹 {key}", info)
    return 0


def _print_rate_line(title: str, info: dict) -> None:
    util = info.get("utilization")
    status = info.get("status") or "allowed"
    resets_at = info.get("resetsAt")
    reset_str = ""
    if isinstance(resets_at, (int, float)) and resets_at > 0:
        dt_jst = datetime.datetime.fromtimestamp(float(resets_at), tz=JST)
        reset_str = f" ｜ 次回リセット: {dt_jst.strftime('%m/%d %H:%M')} JST"
    if isinstance(util, (int, float)):
        used_pct = round(float(util) * 100, 1)
        rem_pct = max(0.0, round(100.0 - used_pct, 1))
        print(f"  - {title}: 使用済み {used_pct:.1f}% ／ 残り {rem_pct:.1f}% ({status}){reset_str}")
    else:
        status_ja = "余裕あり（警告閾値未満・通常利用可）" if status == "allowed" else status
        print(f"  - {title}: {status_ja}{reset_str}")


def _inline_files(files: list[str], max_total_chars: int) -> tuple[str, list[str]]:
    """Bundle target files directly into prompt text to eliminate slow multi-turn Read roundtrips."""
    if not files or max_total_chars <= 0:
        return "", []
    blocks = []
    omitted = []
    used = 0
    ext_lang = {
        ".py": "python",
        ".js": "javascript",
        ".html": "html",
        ".css": "css",
        ".md": "markdown",
        ".json": "json",
        ".toml": "toml",
    }
    for rel in files:
        fpath = (REPO_ROOT / rel) if not os.path.isabs(rel) else Path(rel)
        if not fpath.is_file():
            omitted.append(f"{rel} (missing)")
            continue
        try:
            content = fpath.read_text(encoding="utf-8", errors="replace")
        except Exception:
            omitted.append(f"{rel} (read error)")
            continue
        remaining = max_total_chars - used
        if remaining <= 800:
            omitted.append(rel)
            continue
        truncated_note = ""
        if len(content) > remaining:
            content = content[:remaining]
            truncated_note = f"\n... (truncated at {remaining:,} chars; use Read tool only if tail lines are required)"
        lang = ext_lang.get(fpath.suffix.lower(), "")
        blocks.append(f"### FILE: `{rel}`\n````{lang}\n{content}{truncated_note}\n````")
        used += len(content)
    if not blocks:
        return "", omitted
    return "\n\n【インライン添付ファイル本文（再Read不要）】\n" + "\n\n".join(blocks) + "\n", omitted


def build_prompt(
    effective_mode: str,
    user_prompt: str,
    files: list[str],
    use_git_diff: bool,
    deep_context: bool,
    *,
    diff_base: str = "",
    inline_files: bool = True,
    fast_no_tools: bool = False,
    allow_edit: bool = False,
) -> str:
    file_hint = ""
    inlined_bundle = ""
    if files:
        joined = ", ".join(files)
        if allow_edit:
            file_hint = (
                f"\n編集対象ファイル: {joined}\n"
                "（必ず最初に `Read` ツールで対象ファイルの最新内容を読み込んでから、`Edit` / `Write` ツールで直接書き換えて保存してください。"
                " 指定された対象ファイル以外の無関係なファイルや `.env`・認証情報・`fly.toml` は絶対に編集しないでください）\n"
            )
        elif inline_files:
            budget = 120000 if deep_context else 40000
            inlined_bundle, omitted = _inline_files(files, budget)
            if fast_no_tools:
                file_hint = f"\n対象ファイル: {joined}\n（ファイル全文は下記にインライン添付済みです。添付内容のみを根拠に直接回答してください）\n"
            elif omitted:
                file_hint = (
                    f"\n対象ファイル: {joined}\n"
                    f"（大半は下記にインライン添付済みです。未添付/省略分 [{', '.join(omitted)}] または外部呼び出し元の確認が必要な場合のみ最小限の Read/Grep を使用してください）\n"
                )
            else:
                file_hint = (
                    f"\n対象ファイル: {joined}\n"
                    "（対象ファイル全文は下記にインライン添付済みのため再Readは不要です。周辺モジュールの呼び出し元確認が必要な場合のみ最小限の Read/Grep を使用してください）\n"
                )
        else:
            file_hint = f"\n対象ファイル: {joined}\n（Read / Grep ツールで対象ファイルの実コードおよび AGENTS.md を確認した上で回答してください）\n"
    elif allow_edit:
        file_hint = (
            "\n（必ず最初に `Read` ツールで編集対象ファイルおよび `AGENTS.md` を確認してから、`Edit` / `Write` ツールで直接変更を保存してください。"
            " `.env`・認証情報・`fly.toml` および指示外の無関係なファイルは絶対に編集しないでください）\n"
        )

    diff_snippet = ""
    if use_git_diff or diff_base:
        max_diff_chars = 80000 if deep_context else 24000
        git_cmd = ["git", "diff", diff_base] if diff_base else ["git", "diff", "HEAD"]
        try:
            diff_proc = subprocess.run(
                git_cmd,
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                timeout=12,
            )
            raw_diff = (diff_proc.stdout or "").strip()
            if not raw_diff and not diff_base:
                diff_proc = subprocess.run(
                    ["git", "show", "--stat", "--patch", "-n", "1", "HEAD"],
                    capture_output=True,
                    text=True,
                    cwd=str(REPO_ROOT),
                    timeout=12,
                )
                raw_diff = (diff_proc.stdout or "").strip()
            if raw_diff:
                if len(raw_diff) > max_diff_chars:
                    raw_diff = raw_diff[:max_diff_chars] + f"\n... (diff truncated at {max_diff_chars:,} chars)"
                diff_snippet = f"\n\n【対象 Git Diff】\n```diff\n{raw_diff}\n```\n"
        except Exception:
            pass

    if effective_mode == "strategy":
        action_clause = (
            "必要に応じて指定ドキュメントへ `Edit` / `Write` で構造化保存しつつ、`AGENTS.md` の規約（推測値ゼロ、コラム/一次統計への `/link/` 直貼り禁止、CC BY 4.0 一次統計インフラ活用）を踏まえ、**収益化・SEO・データモート引用獲得・導線設計の全体戦略** を日本語で構造提案してください：\n"
            if allow_edit
            else "ファイルを一切編集せず、`AGENTS.md` の規約（推測値ゼロ、コラム/一次統計への `/link/` 直貼り禁止、CC BY 4.0 一次統計インフラ活用）を踏まえ、**収益化・SEO・データモート引用獲得・導線設計の全体戦略** を日本語で構造提案してください：\n"
        )
        base = (
            "あなたは『相席ナビ (https://aiseki-navi.com)』のチーフ・グロース＆マネタイズ戦略アーキテクトです。\n"
            + action_clause
            + "1. **ファネル＆データモート活用診断**: 『検索/SNS/被リンク流入 → リアルタイム状況・データコラム・一次統計（`/stats/monthly.csv`, `/stats/year-end-2026`）閲覧 → 2ステップ内部クッション（`/grooming`・`/for-women`・店舗詳細等） → `/link/<id>` 成果送客』における最大ボトルネック特定\n"
            "2. **SEO・LLMO・自然被リンク獲得戦略**: 90日予測精度・1h永久ロールアップ・6大ドーム相関・別業態横断比較を活かした検索クラスター補強と引用獲得策\n"
            "3. **押し売り感ゼロで CVR を引き上げる導線設計**: 男女それぞれの心理モード（入店前・滞在中・2軒目・別日の真剣出会い検討）に寄り添うマイクロコピーと配置改善\n"
            "4. **優先実行ロードマップ**: 【すぐ実装すべき高インパクト3手】と【中長期のデータ堀強化策】を期待効果×実装コストとともに提示\n"
        )
    elif effective_mode == "audit":
        action_clause = (
            "対象コードを精査し、発見した明確な不具合や安全上の懸念を指定ファイルへ `Read` → `Edit` で直接修正保存した上で、**複雑な数理ロジック・大型改修の最終バグ・セキュリティ監査と修正報告** を日本語で行ってください（`Bash` 実行は無効化されているため、テスト実行要否は報告に含めてください）：\n"
            if allow_edit
            else "ファイルを一切編集せず、対象コードを深く精査し、**複雑な数理ロジック・大型改修の最終バグ・セキュリティ監査** を辛口かつ具体的に日本語で行ってください：\n"
        )
        base = (
            "あなたは『相席ナビ (https://aiseki-navi.com)』のプリンシパル数理アルゴリズム＆セキュリティ最終監査官です。\n"
            + action_clause
            + "1. **数理・統計ロジック＆境界条件の厳密検証**: ゼロ除算、サンプル希少時の発散、未算出時のダミー固定値混入、JST/UTC混在、深夜営業の日跨ぎ（0:00〜5:00）境界条件、丸め誤差や None 伝播の有無\n"
            "2. **多層DB・Fly Postgres 512MB OOM安全性**: 全期間一括 `SELECT *` によるOOMリスク（最大7日連続日付バッチ分割の遵守）、PG/SQLite方言差、ロック競合、空データの長時間キャッシュ混入、テスト時の `DISABLE_SHEETS=1` 汚染防止\n"
            "3. **セキュリティ・プライバシー・計測非汚染・キャッシュ完全同期**: 管理API認可、XSS/CSRF/SSRF、14日投票匿名化＆IPレート制限、本番アクセス時の `?staff_test=1` 非汚染、`sw.js` / `index.html` / `_nav_loader.html` / 全テンプレート / 回帰テストのキャッシュバスティング完全同期漏れ\n"
            "4. **重大度別の報告**: 発見・修正した箇所を `[Critical / High / Medium / Low]` に分類し、ファイル名・行番号・変更内容を明記（問題なければ『重大な懸念なし』と明記）\n"
        )
    elif effective_mode == "flagship":
        action_clause = (
            "対象の看板コラムHTMLおよび/または note 勝負記事原稿を `Read` で精読し、`Edit` / `Write` ツールで **構成＆脱AIの徹底推敲をファイルへ直接反映・保存** した上で、変更要約を日本語で報告してください（実測数値の改ざんや推測値の創作は絶対禁止）：\n"
            if allow_edit
            else "ファイルを一切編集せず、対象の看板コラムHTMLおよび/または note 勝負記事原稿を精読し、**構成＆脱AIの徹底推敲** を日本語で行ってください：\n"
        )
        base = (
            "あなたは『相席ナビ (https://aiseki-navi.com)』の統括編集デスク（雑誌編集長クラスの構成＆脱AI文体ディレクター）です。\n"
            + action_clause
            + "1. **企画骨格＆冒頭15秒の引き込み診断**: 最初の3行で読者が引き込まれるか。手垢導入の使い回しがないか。『①60〜120分の時間波形ドラマ × ②男女双方の本音の非対称 × ③街・ブランド別カルチャー相性』の立体構成評価\n"
            "2. **徹底的な脱AI・手垢表現の摘出と生身の改稿**: AI優等生構文（『〜ため、』『〜傾向にあり』『〜と言えるでしょう』）、量産煽り語（『徹底解説』『完全攻略』『完全ガイド』『間違いなし』『必見』『3秒で』）、硬い学術語を排除し、編集長くわもんの生身の文体へ磨き上げる\n"
            "3. **Web版とnote版の役割分離＆会話の体温**: `.dialogue-box` の男女会話がリアルな20〜30代の息遣いになっているか磨く\n"
            "4. **ブランド正式名称・著者クレジット・導線規約の完全検閲**: 単体『オリエンタル』・『OLG』・『YATAKOI』・『オリラジ』が0件か、初出『ag（アグ）』『JIS（ジス）』『ASOBIBAR（アソビバー）』併記、著者『くわもん（相席ナビ編集長）』統一、本文への `/link/` 直貼りゼロを厳密チェック\n"
        )
    elif effective_mode == "code":
        action_clause = (
            "対象ファイルを `Read` で確認した上で、`Edit` / `Write` ツールで直接修正・実装を保存し、以下の観点に基づく変更内容を日本語で簡潔に報告してください：\n"
            if allow_edit
            else "ファイルを直接編集せず、以下の観点で辛口かつ具体的なコードレビューを日本語で行ってください：\n"
        )
        base = (
            "あなたは『相席ナビ (aiseki-navi.com)』のシニアセキュリティ＆アーキテクチャ監査官です。\n"
            + action_clause
            + "1. 重大なバグ・例外握りつぶし・エッジケース漏れ・OOMリスク\n"
            "2. セキュリティ・認証・計測汚染リスク（`?staff_test=1` 漏れ、`/link/` 汚染、XSS/CSRF 等）\n"
            "3. キャッシュ完全同期（`sw.js` / `index.html` / `_nav_loader.html` / 全テンプレート）とロック競合\n"
            "4. 適用した改善点または具体的な改善案（問題なければ『重大な懸念なし』と明記）\n"
        )
    elif effective_mode == "article":
        action_clause = (
            "対象コラムまたは note 原稿を `Read` で読み、`Edit` / `Write` ツールで以下の観点に沿って直接推敲・書き換え保存した上で、変更点を日本語で報告してください（実測数値は絶対に変えないこと）：\n"
            if allow_edit
            else "対象コラムまたは note 原稿を読み、ファイルを書き換えずに以下の観点で日本語で添削してください：\n"
        )
        base = (
            "あなたは『相席ナビ』の辛口デスク編集者です。\n"
            + action_clause
            + "1. AIっぽい手垢表現・煽り語（「徹底解説」「完全攻略」「完全ガイド」「間違いなし」「3秒で」等）の排除と自然な文体への書き換え\n"
            "2. ブランド正式名称＆初出カナ併記ルール確認（単体『オリエンタル』・『OLG』・『YATAKOI』・『オリラジ』0件、初出カナ併記）\n"
            "3. 著者クレジット確認（『くわもん（相席ナビ編集長）』統一、本名・現場勤務経歴ゼロ）と直 `/link/` ゼロ確認\n"
            "4. 男女フラットな人間観察と自然な `.dialogue-box` 会話、冒頭15秒の導入改善\n"
        )
    else:
        action_clause = (
            "`AGENTS.md` の規約を踏まえ、指示された対象ファイルを `Read` → `Edit` / `Write` で直接編集・作成した上で、実施内容を日本語で簡潔に報告してください。\n"
            if allow_edit
            else "`AGENTS.md` の規約を踏まえ、ファイルを編集せずに構造的・実践的な設計提案またはセカンドオピニオンを日本語で提示してください。\n"
        )
        base = (
            "あなたは『相席ナビ (https://aiseki-navi.com)』のプリンシパル技術・AIハーネス＆グロース設計アドバイザーです。\n"
            + action_clause
        )

    extra = f"\n【追加指示・重点論点】\n{user_prompt}\n" if user_prompt else ""
    return f"{base}{file_hint}{extra}{diff_snippet}{inlined_bundle}"


def _resolve_extra_edit_dirs(files: list[str]) -> list[str]:
    """Return external parent directories when `--allow-edit` targets explicit files outside `REPO_ROOT`."""
    repo_resolved = REPO_ROOT.resolve()
    dirs: list[str] = []
    for raw in files or []:
        if not raw:
            continue
        p = Path(raw).expanduser().resolve() if os.path.isabs(raw) or str(raw).startswith("~") else (REPO_ROOT / raw).resolve()
        try:
            p.relative_to(repo_resolved)
        except ValueError:
            parent_str = str(p.parent)
            if parent_str not in dirs and p.parent.is_dir():
                dirs.append(parent_str)
    return dirs


def build_claude_command(
    claude_bin: str,
    selected_model: str,
    *,
    allow_edit: bool = False,
    fast_no_tools: bool = False,
    effort_level: str | None = None,
    use_fable: bool = False,
    extra_dirs: list[str] | None = None,
) -> list[str]:
    """Construct safe `claude -p` CLI arguments for either read-only (`plan`) or file-edit (`acceptEdits`) execution."""
    cmd = [
        claude_bin,
        "-p",
        "--permission-mode",
        "acceptEdits" if allow_edit else "plan",
        "--permission-prompts",
        "none",
    ]
    if allow_edit:
        cmd.extend(["--tools", SAFE_EDIT_TOOLS])
        cmd.extend(["--disallowedTools", EDIT_DISALLOWED_SPECS])
        for d in extra_dirs or []:
            if d:
                cmd.extend(["--add-dir", str(d)])
    elif fast_no_tools:
        cmd.extend(["--tools", ""])
    if effort_level:
        cmd.extend(["--effort", effort_level])
    if use_fable:
        cmd.extend(["--fallback-model", "claude-opus-5-5"])
    cmd.extend([
        "--model",
        selected_model,
        "--output-format",
        "json",
    ])
    return cmd


def _git_porcelain_snapshot(extra_files: list[str] | None = None) -> dict[str, tuple[str, float]]:
    """Capture `(status_code, mtime)` per path so post-edit reporting detects both repo and explicit external file edits."""
    snap: dict[str, tuple[str, float]] = {}
    try:
        res = subprocess.run(
            ["git", "status", "--porcelain"],
            cwd=str(REPO_ROOT),
            capture_output=True,
            text=True,
            timeout=10,
        )
        for line in (res.stdout or "").splitlines():
            if len(line) < 4:
                continue
            status_code = line[:2]
            rel_path = line[3:].strip().strip('"')
            if " -> " in rel_path:
                rel_path = rel_path.split(" -> ", 1)[1].strip()
            fpath = REPO_ROOT / rel_path
            mtime = fpath.stat().st_mtime if fpath.exists() else 0.0
            snap[rel_path] = (status_code, mtime)
    except Exception:
        pass
    for raw in extra_files or []:
        if not raw:
            continue
        p = Path(raw).expanduser() if (os.path.isabs(raw) or str(raw).startswith("~")) else (REPO_ROOT / raw)
        if p.exists():
            snap.setdefault(str(raw), ("EX", p.stat().st_mtime))
    return snap


def _detect_touched_repo_files(
    before: dict[str, tuple[str, float]],
    after: dict[str, tuple[str, float]],
) -> list[str]:
    touched: list[str] = []
    for rel_path, after_meta in sorted(after.items()):
        if rel_path not in before or before[rel_path] != after_meta:
            touched.append(rel_path)
    return touched


def _save_review_log(
    mode: str,
    model: str,
    text: str,
    *,
    allow_edit: bool = False,
    touched_files: list[str] | None = None,
) -> Path | None:
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        ts = datetime.datetime.now(JST).strftime("%Y%m%d_%H%M%S")
        safe_mode = "".join(c for c in mode if c.isalnum() or c in ("-", "_")) or "review"
        suffix = f"{safe_mode}_edit" if allow_edit else safe_mode
        out_path = LOG_DIR / f"{ts}_{suffix}.md"
        perm_tag = "acceptEdits" if allow_edit else "plan"
        touched_tag = f" touched={','.join(touched_files)}" if touched_files else ""
        header = f"<!-- model={model} mode={mode} permission={perm_tag}{touched_tag} created_at={ts} JST -->\n\n"
        out_path.write_text(header + text + "\n", encoding="utf-8")
        return out_path
    except Exception:
        return None


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Claude Code review (`plan` read-only by default, or safe file-only `--allow-edit`)"
    )
    parser.add_argument("--doctor", action="store_true", help="Check Claude Code installation, Fable availability, and auth status")
    parser.add_argument("--usage", action="store_true", help="Check live 5h / 7d Claude rate-limit remaining percentage")
    parser.add_argument(
        "--mode",
        choices=ALL_MODES,
        default=None,
        help="Preset mode: code | article | consult | strategy | audit | flagship | architect",
    )
    parser.add_argument("--fable", action="store_true", help="Run with Claude Fable 5.1 (`claude-fable-5-1`); promotes code->audit and article->flagship")
    parser.add_argument("--no-promote", action="store_true", help="Keep exact `--mode` even when `--fable` is set")
    parser.add_argument(
        "--allow-edit",
        action="store_true",
        help="Allow Claude to directly Edit/Write files (`--permission-mode acceptEdits`, Bash/WebFetch/.env/fly.toml blocked)",
    )
    parser.add_argument("--files", nargs="*", default=[], help="Target file paths relative to repo root (auto-inlined in read-only mode)")
    parser.add_argument("--no-inline", action="store_true", help="Do not inline `--files` contents; let Claude Read them via tool calls")
    parser.add_argument("--fast-no-tools", action="store_true", help="Disable all Claude tool calls (`--tools ''`) for ultra-fast single-turn inference on inlined files")
    parser.add_argument("--git-diff", action="store_true", help="Include current git diff (or latest commit diff)")
    parser.add_argument("--diff-base", default="", help="Include `git diff <ref>` against a specific commit/branch")
    parser.add_argument("--prompt", default="", help="Custom focus or question for Claude Code")
    parser.add_argument("--model", default=None, help="Claude model or alias (`fable`, `fable-1m`, `opus`, `sonnet`, `haiku`)")
    parser.add_argument("--effort", choices=["low", "medium", "high", "xhigh", "max"], default=None, help="Reasoning effort level")
    parser.add_argument("--timeout", type=int, default=None, help="Timeout seconds (auto-scaled by model and prompt size)")
    args = parser.parse_args()

    if args.doctor:
        return run_doctor()
    if args.usage:
        return run_usage_check()

    claude_bin = find_claude_bin()
    if not claude_bin:
        print(
            "❌ Claude Code (`claude`) が未インストールです。`curl -fsSL https://claude.ai/install.sh | bash` を実行してください。",
            file=sys.stderr,
        )
        return 2

    effective_fast_no_tools = bool(args.fast_no_tools)
    if args.allow_edit and effective_fast_no_tools:
        print(
            "[claude-review] notice: `--allow-edit` 指定のため `--fast-no-tools` を解除し、ファイル編集ツール (Read,Edit,Write,Glob,Grep) を有効化します。",
            file=sys.stderr,
        )
        effective_fast_no_tools = False

    # When `--allow-edit` is active, Claude's `Edit` tool requires `Read` first; avoid duplicate prompt inlining.
    effective_inline = (not args.no_inline) and (not args.allow_edit)

    raw_mode = args.mode
    if not raw_mode:
        raw_mode = "code" if (args.files or args.git_diff or args.diff_base) else "consult"

    selected_model = resolve_model(args.model, raw_mode, args.fable)
    use_fable = is_fable_model(selected_model)
    effective_mode = resolve_effective_mode(raw_mode, selected_model, args.fable, no_promote=args.no_promote)
    effort_level = args.effort or ("high" if use_fable else None)

    full_prompt = build_prompt(
        effective_mode,
        args.prompt,
        args.files,
        args.git_diff,
        use_fable,
        diff_base=args.diff_base,
        inline_files=effective_inline,
        fast_no_tools=effective_fast_no_tools,
        allow_edit=args.allow_edit,
    )

    if args.timeout is not None:
        timeout_sec = args.timeout
    else:
        base_t = 480 if (use_fable and args.allow_edit) else (420 if use_fable else (240 if args.allow_edit else 180))
        extra_t = (len(full_prompt) // 50000) * 60
        timeout_sec = min(900, base_t + extra_t)

    extra_dirs = _resolve_extra_edit_dirs(args.files) if args.allow_edit else []
    cmd = build_claude_command(
        claude_bin,
        selected_model,
        allow_edit=args.allow_edit,
        fast_no_tools=effective_fast_no_tools,
        effort_level=effort_level,
        use_fable=use_fable,
        extra_dirs=extra_dirs,
    )

    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"
    env.setdefault("DISABLE_SHEETS", "1")
    env.setdefault("DISABLE_SCHEDULER", "1")

    print(
        f"[claude-review] model={selected_model} mode={effective_mode}"
        f" perm={'acceptEdits' if args.allow_edit else 'plan'}"
        f"{f' effort={effort_level}' if effort_level else ''}"
        f" inline={'on' if effective_inline else 'off'}"
        f"{' tools=edit-safe' if args.allow_edit else (' tools=none' if effective_fast_no_tools else '')}"
        f" prompt_chars={len(full_prompt):,} timeout={timeout_sec}s",
        file=sys.stderr,
    )

    git_before = _git_porcelain_snapshot(args.files) if args.allow_edit else {}
    t0 = time.monotonic()
    try:
        proc = subprocess.run(
            cmd,
            input=full_prompt,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        print(
            f"⚠️ Claude Code ({selected_model}) の応答が {timeout_sec} 秒でタイムアウトしました。"
            " 添付ファイルのみで完結する場合は `--fast-no-tools` を付けるか、`--timeout 900` を指定してください。",
            file=sys.stderr,
        )
        return 3
    elapsed = round(time.monotonic() - t0, 1)

    stdout_raw = (proc.stdout or "").strip()
    stderr_raw = (proc.stderr or "").strip()

    if proc.returncode != 0:
        combined = f"{stdout_raw}\n{stderr_raw}".strip()
        lower_c = combined.lower()
        if any(p in lower_c for p in ("not logged in", "please run `claude auth login`", "claude auth login", "unauthorized", "invalid api key")):
            print(
                "⚠️ Claude Code のログイン認証が必要です。ターミナルで以下を実行してください：\n"
                f"  {claude_bin} auth login",
                file=sys.stderr,
            )
            return 4
        print(f"⚠️ Claude Code 実行エラー (exit={proc.returncode}, model={selected_model}):\n{combined}", file=sys.stderr)
        return proc.returncode

    touched_files: list[str] = []
    if args.allow_edit:
        git_after = _git_porcelain_snapshot(args.files)
        touched_files = _detect_touched_repo_files(git_before, git_after)

    result_text = stdout_raw
    meta_parts = [f"elapsed={elapsed}s"]
    if stdout_raw.startswith("{"):
        try:
            payload = json.loads(stdout_raw)
            if isinstance(payload, dict) and "result" in payload:
                result_text = str(payload.get("result") or "").strip()
                cost = payload.get("total_cost_usd")
                turns = payload.get("num_turns")
                denials = payload.get("permission_denials")
                if turns is not None:
                    meta_parts.append(f"turns={turns}")
                if isinstance(cost, (int, float)):
                    meta_parts.append(f"est_cost_usd={cost:.4f}")
                if isinstance(denials, list) and denials:
                    meta_parts.append(f"permission_denials={len(denials)}")
        except Exception:
            pass

    if args.allow_edit:
        meta_parts.append(f"edited={','.join(touched_files) if touched_files else 'none'}")

    saved_path = _save_review_log(
        effective_mode,
        selected_model,
        result_text,
        allow_edit=args.allow_edit,
        touched_files=touched_files,
    )
    if saved_path:
        meta_parts.append(f"saved={saved_path.relative_to(REPO_ROOT)}")
    print(f"[claude-review] done ({', '.join(meta_parts)})", file=sys.stderr)
    print(result_text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
