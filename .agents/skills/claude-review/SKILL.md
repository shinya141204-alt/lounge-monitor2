---
name: claude-review
description: >-
  相席ナビ（https://aiseki-navi.com）専用の「Antigravity × Claude Code（Opus 5.5 ＆ 最上位 Fable 5.1）」クロスレビュー・全体戦略・数理セキュリティ監査・看板コラム脱AI推敲スキル。
  ユーザーが `/claude-review` と入力したときや、「Claudeにも見せて」「Claudeでレビューして」「Fableで見て」「全体戦略を練って」「最終監査して」「勝負記事を徹底推敲して」「Claudeの使用量は？」と依頼したときに必ず有効化する。
  日常レビューは高速な `claude-opus-5-5`、①収益化・SEO・導線設計の全体戦略（`strategy`）・②複雑な数理ロジック・大型改修の最終バグ・セキュリティ監査（`audit`）・③看板コラム・note勝負記事の構成＆脱AI徹底推敲（`flagship`）は100万トークン対応の最上位モデル `claude-fable-5-1` を読み取り専用（`--permission-mode plan`）で自動呼び出し、競合ゼロで Antigravity 側へ統合反映する。
---

# `/claude-review` Antigravity × Claude Code（Opus 5.5 × Fable 5.1）連携ワークフロー

メインの実装・データ集計・テスト・デプロイは **Antigravity** が担い、別モデル視点でのコード監査・文章推敲・グロース全体戦略を **Claude Code**（読み取り専用モード `--permission-mode plan`）に担当させるハイブリッド連携スキルです。

## モデル使い分け原則（Opus 5.5 vs 最上位 Fable 5.1）
| 区分 | 使用モデル | モード指定 | 対象タスク（自動適用基準） |
| :--- | :--- | :--- | :--- |
| **切り札① 全体戦略** | **`claude-fable-5-1`**（1Mトークン） | `--mode strategy` | **収益化・SEO・導線設計の「全体戦略」**（PV→回遊→`/link/` CVR ボトルネック診断、検索意図ピラー設計、内部リンク構造改革） |
| **切り札② 最終監査** | **`claude-fable-5-1`**（1Mトークン） | `--mode audit` | **複雑な数理ロジック・大型改修の「最終バグ・セキュリティ監査」**（予測・統計モデルの境界条件、DB/ロック競合、認可・`/link/` 計測汚染・キャッシュ3点同期の最終検閲） |
| **切り札③ 勝負記事推敲** | **`claude-fable-5-1`**（1Mトークン） | `--mode flagship` | **看板コラム・note 勝負記事の「構成＆脱AIの徹底推敲」**（冒頭15秒の引き、3軸×4層因果の骨格診断、AI優等生構文の1文単位の生身改稿、Web/note役割分離・ブランド規約検閲） |
| **日常レビュー** | `claude-opus-5-5` | `--mode code` / `article` / `consult` | 日常の小〜中規模コード確認、定例記事の軽め添削、単発アイデア壁打ち（高速・低レート消費） |

> どの日常モード（`code` / `article` / `consult`）でも `--fable` フラグを付与すると、自動的に `claude-fable-5-1` および対応する深層モード（`audit` / `flagship` / `strategy`）へ昇格します。

## プロジェクト＆スクリプトパス
- リポジトリルート: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`
- 共通ルールファイル: `AGENTS.md` および `CLAUDE.md`（シンボリックリンクで完全同期）
- 連携ラッパースクリプト: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py`

---

## Step 1: 接続・Fable対応チェック ＆ 残り使用量確認
- **インストール・認証・Fable 5.1 カタログ確認**:
  ```bash
  python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py --doctor
  ```
- **現在の5時間枠・7日間枠の残り使用可能量（%）と次回リセット時刻（JST）確認**:
  ユーザーが「Claudeの使用量あとどれくらい？」と聞いたときや、大型 Fable タスクを連続実行する前に確認する：
  ```bash
  python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py --usage
  ```

---

## Step 2: 切り札 Fable 5.1 モード（3大重点領域）

ラッパースクリプトは常に `--permission-mode plan`（読み取り専用）で起動し、Fable 5.1 実行時は自動で `--effort high`・タイムアウト `420秒`・Git diff 上限 `80,000文字`・過負荷時フォールバック `--fallback-model claude-opus-5-5` を適用します。

### ① 収益化・SEO・導線設計の「全体戦略」（`--mode strategy`）
サイト全体のマネタイズ導線、SEOピラー戦略、2ステップクッションページのCVR改善ロードマップを設計するときに使用：
```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode strategy \
  --files CLAUDE.md affiliate_manager.py pdca_engine.py routes/portal_bp.py templates/index.html templates/store_detail.html \
  --prompt "現在の検索流入から /grooming・/for-women・2軒目予約への送客ボトルネックを特定し、押し売り感ゼロでCVRを高める優先3施策を提案して"
```

### ② 複雑な数理ロジック・大型改修の「最終バグ・セキュリティ監査」（`--mode audit`）
60分後予測・統計ロジック・DBスキーマ・認証・複数ファイル横断の大型改修を本番デプロイする前の最終関門として使用（より深い推論が必要な場合は `--effort xhigh` も指定可能）：
```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode audit \
  --git-diff \
  --files moat_intelligence.py local_db.py app.py \
  --prompt "深夜0〜5時の日跨ぎ境界条件、サンプル希少時のゼロ除算・バイアス発散、SQLite/PostgreSQL方言差とロック競合を重点監査して"
```

### ③ 看板コラム・note 勝負記事の「構成＆脱AIの徹底推敲」（`--mode flagship`）
サイトの顔となるピラー記事や、noteでスキ・シェアを狙う勝負エッセイの骨格組み替え＆1文単位の生身改稿に使用：
```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode flagship \
  --files templates/guide_lounge_vs_screened_date.html \
  --prompt "冒頭15秒の引き込み、AI優等生構文の完全排除、男女双方の本音のリアリティを辛口デスク視点で徹底推敲し、コピペ置換できる改稿案を出して"
```

---

## Step 3: 日常の高速レビューモード（Opus 5.5：`code` / `article` / `consult`）

日常の細かな差分確認や軽めの添削では、レート消費を抑えて素早く返る標準モードを使用する：
```bash
# 日常のコード差分チェック (claude-opus-5-5)
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode code --git-diff --files routes/portal_bp.py

# 通常記事のクイック添削 (claude-opus-5-5)
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode article --files templates/guide_weekend.html

# 単発のUI/企画壁打ち (claude-opus-5-5)
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode consult --prompt "店舗詳細ページの回遊率を高めるマイクロUI案を3つ提案して"
```

---

## Step 4: 指摘の取捨選択と Antigravity 側での統合反映
1. Claude（Fable 5.1 / Opus 5.5）から返ってきた分析・監査・改稿案のうち、相席ナビの実データ・設計原則に合致する本質的な指摘だけを **Antigravity が吟味して選別** する（過剰な複雑化や事実誤認は採用しない）。
2. 修正を反映する場合は Antigravity の `edit_file` で安全に適用し、標準の単体テスト・アフィリエイト監査・UI監査を通してから、ユーザーへ **「Fable / Opus の主な指摘」と「反映した改善点」** を分かりやすく日本語で報告する。
