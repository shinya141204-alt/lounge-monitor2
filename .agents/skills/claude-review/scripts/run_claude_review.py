#!/usr/bin/env python3
"""
run_claude_review.py - Antigravity から Claude Code (`claude -p`) を安全に読み取り専用で呼び出す連携ラッパー

特徴:
- `--permission-mode plan` により、Claude Code 側での勝手なファイル書き換え・コマンド破壊を防止（競合ゼロ保証）
- 日常の高速レビュー (`claude-opus-5-5`) と、切り札の 100万トークン最上位モデル (`claude-fable-5-1`) のハイブリッド運用に対応
- 切り札 Fable 5.1 専用モードを 3大領域すべてに完備:
  1. `strategy` : 収益化・SEO・導線設計の「全体戦略」
  2. `audit`    : 複雑な数理ロジック・大型改修の「最終バグ・セキュリティ監査」
  3. `flagship` : 看板コラム・note 勝負記事の「構成＆脱AIの徹底推敲」
- `--usage` で現在の Claude Max 5時間枠 / 7日間枠の残り使用可能量（%）と次回リセット時刻（JST）を即座に確認可能
"""

import argparse
import datetime
import glob
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[4]
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

FABLE_NATIVE_MODES = {"strategy", "audit", "flagship", "article-fable"}
ALL_MODES = ("code", "article", "consult", "strategy", "audit", "flagship", "article-fable")


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


def resolve_effective_mode(mode: str, model_name: str, force_fable: bool) -> str:
    """Promote standard modes (`code`/`article`/`consult`) to deep Fable presets when Fable is selected."""
    if mode == "article-fable":
        return "flagship"
    if force_fable or is_fable_model(model_name):
        if mode == "code":
            return "audit"
        if mode == "article":
            return "flagship"
        if mode == "consult":
            return "strategy"
    return mode


def _inspect_cached_catalog() -> dict[str, dict]:
    """Read local Claude model catalog cache (if present) without exposing any credentials."""
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

    print("ℹ️ 初回ログインがまだの場合は、ターミナルで一度だけ `claude`（または `~/.local/bin/claude auth login`）を実行してブラウザ認証を完了してください。")
    return 0


def run_usage_check() -> int:
    """Query live Claude rate-limit utilization via a minimal Haiku stream-json probe."""
    claude_bin = find_claude_bin()
    if not claude_bin:
        print("❌ Claude Code (`claude`) が未インストールです。", file=sys.stderr)
        return 2

    cmd = [
        claude_bin,
        "-p",
        "OK",
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
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )
    except subprocess.TimeoutExpired:
        print("⚠️ 使用量チェックがタイムアウトしました。", file=sys.stderr)
        return 3

    if proc.returncode != 0:
        print(f"⚠️ 使用量チェック失敗 (exit={proc.returncode}): {(proc.stderr or proc.stdout or '').strip()}", file=sys.stderr)
        return proc.returncode

    rate_events: dict[str, dict] = {}
    for line in (proc.stdout or "").splitlines():
        line = line.strip()
        if not line.startswith("{"):
            continue
        try:
            ev = json.loads(line)
        except Exception:
            continue
        if ev.get("type") == "rate_limit_event":
            info = ev.get("rate_limit_info") or {}
            rl_type = str(info.get("rateLimitType") or "unknown")
            rate_events[rl_type] = info

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


def build_prompt(effective_mode: str, user_prompt: str, files: list[str], use_git_diff: bool, deep_context: bool) -> str:
    file_hint = ""
    if files:
        joined = ", ".join(files)
        file_hint = f"\n対象ファイル: {joined}\n（必ず Read / Grep ツールで対象ファイルの実コード・周辺呼び出し元および CLAUDE.md を直接確認した上で回答してください）\n"

    diff_snippet = ""
    if use_git_diff:
        max_diff_chars = 80000 if deep_context else 24000
        try:
            diff_proc = subprocess.run(
                ["git", "diff", "HEAD"],
                capture_output=True,
                text=True,
                cwd=str(REPO_ROOT),
                timeout=12,
            )
            raw_diff = (diff_proc.stdout or "").strip()
            if not raw_diff:
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
        base = (
            "あなたは『相席ナビ (https://aiseki-navi.com)』のチーフ・グロース＆マネタイズ戦略アーキテクトです。\n"
            "100万トークンの長大コンテキストを活かし、ファイルを一切編集せず、必要に応じて `CLAUDE.md`、`affiliate_manager.py`、`static/affiliate_config.js`、`pdca_engine.py`、`routes/portal_bp.py`、および関連テンプレート群を横断的に読み解き、**収益化・SEO・導線設計の全体戦略** を日本語で構造提案してください：\n"
            "1. **ファネル全体のボトルネック診断**: 『検索/SNS流入 → リアルタイム状況・データコラム閲覧 → 2ステップ内部クッション（/grooming・/for-women・店舗詳細等） → /link/<id> 成果送客』の流れで、現在もっとも機会損失が大きい離脱ポイントの特定（根拠ファイル・行番号付き）\n"
            "2. **SEO・LLMO・情報設計の全体最適化**: 検索意図クラスター（エリア×時間帯、ブランド比較、料金・ドレスコード、2軒目・審査制デート比較等）に対する不足ピラー、内部リンクの孤立・カニバリ解消、構造化データ強化策\n"
            "3. **押し売り感ゼロで CVR を引き上げる導線設計**: リアルタイム人数を見に来た男女それぞれの心理モード（入店前・滞在中・退店2軒目・別日の真剣出会い検討）に自然に寄り添うマイクロコピーと配置改善（CLAUDE.md の『コラム本文への /link/ 直貼り禁止・2ステップ原則』を厳守すること）\n"
            "4. **優先実行ロードマップ**: 【すぐ実装すべき高インパクト3手】と【中長期で競合が模倣できないデータ堀（Moat）施策】を、期待効果×実装コストとともに具体的に提示\n"
        )
    elif effective_mode == "audit":
        base = (
            "あなたは『相席ナビ (https://aiseki-navi.com)』のプリンシパル数理アルゴリズム＆セキュリティ最終監査官です。\n"
            "ファイルを一切編集せず、対象コードおよび関連モジュールを深く読み込み、**複雑な数理ロジック・大型改修の最終バグ・セキュリティ監査** を辛口かつ具体的に日本語で行ってください：\n"
            "1. **数理・統計ロジック＆境界条件の厳密検証**: ゼロ除算、サンプル希少時の過剰適合・外れ値発散、ベイズ平滑化・時間減衰・クリップ境界、タイムゾーン（JST/UTC）混在、深夜営業の日跨ぎ（0:00〜5:00）境界条件、丸め誤差や NaN/None 伝播の有無\n"
            "2. **多層データストア・並行処理の整合性**: PostgreSQL / SQLite WAL / Redis / Google Sheets 間のキャッシュ不整合、スレッドロック順序（デッドロック・競合状態）、SQL方言差、単体テスト実行時の本番データ汚染リスク\n"
            "3. **セキュリティ・認証・計測汚染リスク**: 管理API認可漏れ、XSS/CSRF/SSRF/オープンリダイレクト、シークレット露出、`/link/<id>` のボット・自己クリック汚染ガード、`sw.js` / `app.js` / `index.html` のキャッシュバスティング3点同期漏れ\n"
            "4. **重大度別の具体的修正パッチ案**: 発見した懸念を `[Critical / High / Medium / Low]` に分類し、ファイル名・行番号・再現条件・そのまま適用できる修正コード案を提示（問題なければ『重大な懸念なし』と明記）\n"
        )
    elif effective_mode == "flagship":
        base = (
            "あなたは『相席ナビ (https://aiseki-navi.com)』の統括編集デスク（雑誌編集長クラスの構成＆脱AI文体ディレクター）です。\n"
            "ファイルを一切編集せず、対象の看板コラムHTMLおよび/または note 勝負記事原稿（さらに必要に応じて既存コラム群との重複有無）を精読し、**構成＆脱AIの徹底推敲** を日本語で行ってください：\n"
            "1. **企画骨格＆冒頭15秒の引き込み診断**: 最初の3行で読者が『まさに自分の夜の体験だ』と引き込まれる導入か。既存記事の手垢導入（エレベーターが開いた瞬間〜等の使い回し）になっていないか。『①60〜120分の時間波形ドラマ × ②男女双方の本音の非対称 × ③街・ブランド別カルチャー相性』の3軸×4層因果が立体的に噛み合っているか、構成の組み替え案を含めて評価\n"
            "2. **徹底的な脱AI・手垢表現の摘出と生身の改稿案**: AI特有の優等生構文（『〜ため、』『〜傾向にあり』『〜と言えるでしょう』『〜という構図』）、量産煽り語（『徹底解説』『完全攻略』『完全ガイド』『間違いなし』『最適解』『必見』『3秒で』）、硬い学術語（『サンクコスト』『二面市場』等）、同じ語尾の連続を1文単位で摘出し、**そのままコピペ置換できる生身の大人（編集長くわもん）の書き換え文案** をすべて提示\n"
            "3. **Web版とnote版の役割分離＆会話の体温**: Web版が検索読者向けの明快なデータ検証リファレンスとして機能しつつ、note版がマニュアル臭ゼロの一人称エッセイとして独立しているか。`.dialogue-box` の男女の会話がリアルな20〜30代の息遣い（甘すぎるファンタジーや説教臭さゼロ）になっているか添削\n"
            "4. **ブランド正式名称・著者クレジット・導線規約の完全検閲**: 単体『オリエンタル』・『OLG』・『YATAKOI』・『オリラジ』が0件か、本文初出で『ag（アグ）』『JIS（ジス）』『ASOBIBAR（アソビバー）』とカナ併記されているか、著者表記が『くわもん（相席ナビ編集長）』に統一され個人本名や店舗勤務経歴が混入していないか、コラム本文に `/link/` 直貼りが無いかを厳密チェック\n"
        )
    elif effective_mode == "code":
        base = (
            "あなたは『相席ナビ (aiseki-navi.com)』のシニアセキュリティ＆アーキテクチャ監査官です。\n"
            "ファイルを直接編集せず、以下の観点で辛口かつ具体的なコードレビューを日本語で行ってください：\n"
            "1. 重大なバグ・例外握りつぶし・エッジケース漏れ\n"
            "2. セキュリティ・認証・計測汚染リスク（/link/ 汚染、機密露出、XSS/CSRF 等）\n"
            "3. パフォーマンス・キャッシュ整合性（sw.js / app.js 同期、N+1、ロック競合）\n"
            "4. そのまま適用できる具体的な改善案（問題なければ『重大な懸念なし』と明記）\n"
        )
    elif effective_mode == "article":
        base = (
            "あなたは『相席ナビ』の辛口デスク編集者です。\n"
            "対象コラムまたは note 原稿を読み、ファイルを書き換えずに以下の観点で日本語で添削してください：\n"
            "1. AIっぽい手垢表現・煽り語（「徹底解説」「完全攻略」「完全ガイド」「間違いなし」「3秒で」等）・硬い学術用語（「サンクコスト」「二面市場」等）・同じ構文の繰り返し箇所の指摘と自然な書き換え案\n"
            "2. ブランド正式名称＆初出カナ併記ルール確認（単体『オリエンタル』・『OLG』・『YATAKOI』・『オリラジ』が0件か、本文初出で『ag（アグ）』『JIS（ジス）』『ASOBIBAR（アソビバー）』と併記されているか）\n"
            "3. 著者クレジット確認（『くわもん（相席ナビ編集長）』に統一され、個人本名ローマ字や店舗勤務経歴が混入していないか）\n"
            "4. 男女どちらか一方に偏りすぎていないか（フラットな人間観察と自然な `.dialogue-box` 会話になっているか）\n"
            "5. 読者が最初の15秒で引き込まれる導入改善案\n"
        )
    else:
        base = (
            "あなたは『相席ナビ (aiseki-navi.com)』の技術・グロース戦略アドバイザーです。\n"
            "CLAUDE.md の前提を踏まえ、ファイルを編集せずに別視点のセカンドオピニオンを日本語で簡潔に提示してください。\n"
        )

    extra = f"\n【追加指示・重点論点】\n{user_prompt}\n" if user_prompt else ""
    return f"{base}{file_hint}{extra}{diff_snippet}"


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run read-only Claude Code review / Fable 5.1 deep strategy, audit, and flagship editorial polish"
    )
    parser.add_argument("--doctor", action="store_true", help="Check Claude Code installation, Fable availability, and auth status")
    parser.add_argument("--usage", action="store_true", help="Check live 5h / 7d Claude rate-limit remaining percentage")
    parser.add_argument(
        "--mode",
        choices=ALL_MODES,
        default="code",
        help=(
            "Review preset mode: "
            "daily Opus modes = code | article | consult; "
            "flagship Fable 5.1 modes = strategy (全体戦略) | audit (数理・最終バグ監査) | flagship (看板コラム・note徹底推敲)"
        ),
    )
    parser.add_argument("--fable", action="store_true", help="Shortcut to run with Claude Fable 5.1 (`claude-fable-5-1`) and deep preset")
    parser.add_argument("--files", nargs="*", default=[], help="Target file paths relative to repo root")
    parser.add_argument("--git-diff", action="store_true", help="Include current git diff (or latest commit diff)")
    parser.add_argument("--prompt", default="", help="Custom focus or question for Claude Code")
    parser.add_argument(
        "--model",
        default=None,
        help="Claude model or alias (`fable`, `fable-1m`, `opus`, `sonnet`, `haiku`, or full ID). Auto-selects Fable 5.1 for strategy/audit/flagship, Opus 5.5 otherwise.",
    )
    parser.add_argument(
        "--effort",
        choices=["low", "medium", "high", "xhigh", "max"],
        default=None,
        help="Reasoning effort level (defaults to `high` for Fable 5.1 modes)",
    )
    parser.add_argument(
        "--timeout",
        type=int,
        default=None,
        help="Timeout seconds (auto-defaults to 420s for Fable 5.1, 180s for Opus/Sonnet)",
    )
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

    selected_model = resolve_model(args.model, args.mode, args.fable)
    use_fable = is_fable_model(selected_model)
    effective_mode = resolve_effective_mode(args.mode, selected_model, args.fable)
    timeout_sec = args.timeout if args.timeout is not None else (420 if use_fable else 180)
    effort_level = args.effort or ("high" if use_fable else None)

    full_prompt = build_prompt(
        effective_mode=effective_mode,
        user_prompt=args.prompt,
        files=args.files,
        use_git_diff=args.git_diff,
        deep_context=use_fable,
    )

    cmd = [
        claude_bin,
        "-p",
        full_prompt,
        "--permission-mode",
        "plan",
        "--model",
        selected_model,
        "--output-format",
        "text",
    ]
    if effort_level:
        cmd.extend(["--effort", effort_level])
    if use_fable:
        cmd.extend(["--fallback-model", "claude-opus-5-5"])

    env = os.environ.copy()
    local_bin = str(Path.home() / ".local" / "bin")
    if local_bin not in env.get("PATH", ""):
        env["PATH"] = f"{local_bin}:{env.get('PATH', '')}"

    print(
        f"[claude-review] model={selected_model} mode={effective_mode}"
        f"{f' effort={effort_level}' if effort_level else ''} timeout={timeout_sec}s (read-only plan mode)",
        file=sys.stderr,
    )

    try:
        proc = subprocess.run(
            cmd,
            cwd=str(REPO_ROOT),
            env=env,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
    except subprocess.TimeoutExpired:
        print(
            f"⚠️ Claude Code ({selected_model}) の応答が {timeout_sec} 秒でタイムアウトしました。`--timeout 600` を指定するか対象ファイルを絞って再実行してください。",
            file=sys.stderr,
        )
        return 3

    stdout = (proc.stdout or "").strip()
    stderr = (proc.stderr or "").strip()

    if proc.returncode != 0:
        combined = f"{stdout}\n{stderr}".strip()
        if any(k in combined.lower() for k in ("login", "auth", "not logged in", "api key", "unauthorized")):
            print(
                "⚠️ Claude Code の初回ログインが未完了です。\n"
                "Mac のターミナルで以下のコマンドを1回だけ実行し、ブラウザ画面でログインを完了してください：\n\n"
                f"  {claude_bin} auth login\n"
                "（またはターミナルで `claude` と打って起動してもログインできます）",
                file=sys.stderr,
            )
            return 4
        print(f"⚠️ Claude Code 実行エラー (exit={proc.returncode}, model={selected_model}):\n{combined}", file=sys.stderr)
        return proc.returncode

    print(stdout)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
