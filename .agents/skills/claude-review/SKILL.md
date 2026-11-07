---
name: claude-review
description: >-
  相席ナビ（https://aiseki-navi.com）専用の「Antigravity × Claude Code（Opus 5.5 ＆ 最上位 Fable 5.1）」クロスレビュー・全体戦略・数理セキュリティ監査・看板コラム脱AI推敲・アーキテクチャ相談・安全直接編集スキル。
  ユーザーが `/claude-review` と入力したときや、「Claudeにも見せて」「Claudeでレビューして」「Claudeに書き換えさせて」「Fableで見て」「全体戦略を練って」「最終監査して」「勝負記事を徹底推敲して」「Claudeの使用量は？」と依頼したときに必ず有効化する。
  既定は読み取り専用（`--permission-mode plan`）で安全に監査し、直接書き込みが必要な場合は `--allow-edit`（`acceptEdits`＋ファイル編集限定・`Bash`/`.env`/`fly.toml` 物理遮断）で Claude に対象ファイルを直接編集させ、Antigravity 側で統合テスト・デプロイを行う。
---

# `/claude-review` Antigravity × Claude Code（Opus 5.5 × Fable 5.1）連携ワークフロー

メインの実装・データ集計・テスト・デプロイは **Antigravity** が担い、別モデル視点でのコード監査・文章推敲・グロース全体戦略・設計壁打ちを **Claude Code**（既定：読み取り専用 `--permission-mode plan` ／ 明示指定時：ファイル編集限定 `--allow-edit`）に担当させるハイブリッド連携スキルです。

## モデル使い分け原則（Opus 5.5 vs 最上位 Fable 5.1）
| 区分 | 使用モデル | モード指定 | 対象タスク（自動適用基準） |
| :--- | :--- | :--- | :--- |
| **切り札① 全体戦略** | **`claude-fable-5-1`**（1Mトークン） | `--mode strategy` | **収益化・SEO・データモート引用獲得・導線設計の「全体戦略」**（PV→回遊→`/link/` CVR 診断、CC BY 4.0 一次統計の被リンク戦略、検索意図ピラー設計） |
| **切り札② 最終監査** | **`claude-fable-5-1`**（1Mトークン） | `--mode audit` | **複雑な数理ロジック・大型改修の「最終バグ・セキュリティ監査」**（予測・統計境界条件、Fly PG 512MB OOM安全性、認可・`?staff_test=1` 非汚染・キャッシュ完全同期の最終検閲） |
| **切り札③ 勝負記事推敲** | **`claude-fable-5-1`**（1Mトークン） | `--mode flagship` | **看板コラム・note 勝負記事の「構成＆脱AIの徹底推敲」**（冒頭15秒の引き、3軸×4層因果骨格、AI優等生構文の1文単位改稿、Web/note分離・ブランド規約検閲） |
| **切り札④ 自由深層設計** | **`claude-fable-5-1`**（1Mトークン） | `--mode architect` | **スキル・共通ルール策定、新機能アーキテクチャ設計、自由テーマの深層壁打ち**（マネタイズ固定枠に縛られない汎用設計モード） |
| **日常レビュー** | `claude-opus-5-5` | `--mode code` / `article` / `consult` | 日常の小〜中規模コード確認、定例記事の軽め添削、単発アイデア壁打ち（高速・低レート消費） |

> **昇格ルール**: `--fable` フラグ明示時は `code → audit`、`article → flagship`、`consult → strategy` へ自動昇格します（`--no-promote` で全昇格抑止可。汎用設計は `--mode architect` を使用）。

## 高速インラインバンドル ＆ ログ自動保存仕様
- `--files` で指定されたファイルは Python 側で自動的にプロンプト内へ一括インライン添付（Fable 最大 `120,000` 文字 ／ Opus 最大 `40,000` 文字）され、`stdin` 経由で渡されるため `ARG_MAX` 制限や Read 往復によるタイムアウトを防ぎます。
- 添付ファイルのみで完結させツール往復を完全ゼロにして最速応答を得たい場合は **`--fast-no-tools`** を追加します。
- すべてのレビュー出力は `.agents/skills/claude-review/logs/<JST日時>_<mode>.md`（Git除外済み）にも自動アーカイブされます。

---

## Step 1: 接続・Fable対応チェック ＆ 残り使用量確認
```bash
# インストール・認証・Fable 5.1 カタログ確認
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py --doctor

# 5時間枠・7日間枠の残り使用可能量（%）と次回リセット時刻（JST）確認
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py --usage
```

---

## Step 2: 切り札 Fable 5.1 モードの実行例

```bash
# ① 全体戦略（データモート×SEO×収益導線）
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode strategy \
  --files AGENTS.md affiliate_manager.py pdca_engine.py routes/portal_bp.py \
  --prompt "一次統計ページからのサイト内回遊と2ステップ収益導線の優先改善3手を提案して"

# ② 大型改修の最終バグ・セキュリティ監査（最新コミットまたは作業差分付き）
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode audit --git-diff \
  --files local_db.py moat_intelligence.py routes/portal_bp.py \
  --prompt "Fly Postgres 512MB でのOOMリスク、深夜日跨ぎ境界、キャッシュ整合性を重点監査して"

# ③ 看板コラム・note 勝負記事の徹底推敲（添付のみで完結するため --fast-no-tools 推奨）
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode flagship --fast-no-tools \
  --files templates/guide_lounge_vs_screened_date.html \
  --prompt "冒頭15秒の引き込み、AI優等生構文の完全排除、男女双方の本音のリアリティを徹底推敲して"

# ④ 自由テーマ・アーキテクチャ深層相談
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode architect --fast-no-tools \
  --files AGENTS.md \
  --prompt "次期アップデートに向けた設計案を整理して"

# ⑤ 直接書き込みモード（`--allow-edit`: コラム改稿や対象ファイルの直接修正を Claude 自身に保存させる場合）
# ※ `Read,Edit,Write,Glob,Grep` のみ解禁し、`Bash` / `WebFetch` / `.env` / `fly.toml` 変更は物理遮断されます。
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode flagship --allow-edit \
  --files templates/guide_lounge_vs_screened_date.html \
  --prompt "AIっぽい接続詞や煽り語を生身の編集長文体へ直接書き換えて保存して（実測数値は変更禁止）"
```

---

## Step 3: 指摘の取捨選択・`--allow-edit` 差分の検証・Antigravity 側での統合反映
1. **読み取り専用モード（既定）の場合**: Claude（Fable 5.1 / Opus 5.5）から返ってきた分析・監査・改稿案のうち、相席ナビの実データ・設計原則に合致する本質的な指摘を **Antigravity が選別** し、`edit_file` で安全に適用する。
2. **`--allow-edit`（直接書き込みモード）の場合**: Claude 実行完了後、必ず `git diff` で変更差分（数値改変ゼロ・ブランド表記・キャッシュクエリ整合）を Antigravity が検閲し、`run_ops_sentinel.py --pre-deploy` で全監査・単体テストを通してから報告・デプロイする。
