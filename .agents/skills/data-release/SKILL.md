---
name: data-release
description: >-
  相席ナビ（https://aiseki-navi.com）専用の月次・年末一次統計リリース＆CC BY 4.0被リンク獲得キット自動生成スキル。
  ユーザーが `/data-release` または `/data-release <YYYY-MM>` と入力したときや、「月次データリリースを出して」「統計白書を更新して」「被リンク・メディア引用用の調査レポートを作って」と依頼したときに必ず有効化する。
  90日実測ログ・1h永久ロールアップ・6大ドーム公演相関・現地待ち分数・本番CSV/llms-factsから推測値ゼロの統計パッケージ（JSON・記事原稿・HTML/Markdown引用キット）を生成し、公開統計エンドポイントの健全性検証まで一気通貫で実行する。
---

# `/data-release` 月次・白書 一次統計リリース ＆ 被リンク獲得キット生成 Runbook

『相席ナビ』をナイトライフ・相席市場の **「公式・一次統計インフラ（メディア・ブロガー・AI検索の第1引用元）」** として定着させ、自然被リンクと指名検索を獲得するための専用ワークフローです。

- **リポジトリルート**: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`
- **統計パッケージ生成スクリプト**: [scripts/build_monthly_release.py](./scripts/build_monthly_release.py)
- **本番公開統計ベリファイスクリプト**: [scripts/verify_public_stats.py](./scripts/verify_public_stats.py)

---

## Step 1: 月次一次統計パッケージ（JSON・リリース原稿・引用キット）の自動生成

1. 対象年月（例：`2026-11`）を指定してスクリプトを実行し、本番 `/stats/monthly.csv?staff_test=1`・`/api/llms-facts.json?staff_test=1`（非汚染UA）およびローカルDBから実測値を集計する：
   ```bash
   DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/data-release/scripts/build_monthly_release.py --month <YYYY-MM> --days 30
   ```
2. `.agents/reports/release_<YYYY-MM>/` 配下に生成された3ファイルを確認する：
   - `release.json`：実測店舗数・総サンプル数 `sample_n`・ピーク店舗TOP10・高女子率店舗・予測精度・ドーム公演予定（未算出項目は `null`）
   - `release.md`：報道・メディア向けの調査リリース原稿ドラフト（ダミー数値ゼロ、ブランド正式名称＆初出カナ併記済み）
   - `citation_kit.md`：外部ブログ・メディア・note用の **CC BY 4.0 コピペ引用スニペット（HTML / Markdown / 1文クレジット）** および 16:9 図解アイキャッチ生成コマンド

---

## Step 2: サイト内統計白書更新 または 月次調査コラムの実装（任意・指示時）

ユーザーがWebサイト上への新規リリース記事公開も希望する場合は、以下を守る（**AGENTS.md §9 厳守**）：
1. **中立性の絶対維持（`/link/` 混入完全禁止）**：
   - 一次統計ページ（`/stats`, `/stats/year-end-2026`）および調査リリース記事には、外部アフィリエイトリンク（`/link/...`）を一切含めない。
2. **CC BY 4.0 ライセンス ＆ 生データCSV導線の明記**：
   - 記事末尾・データ表直下に `ライセンス: CC BY 4.0（出典: 相席ナビ編集部 https://aiseki-navi.com）` と `/stats/monthly.csv?days=30` へのダウンロードリンクを設置する。
3. `/article` スキルの Step 2〜4 に従い、16:9 図解アイキャッチ生成とルート・サイトマップ登録を行う。

---

## Step 3: Claude Fable 5.2 による報道リリースクオリティ推敲

公開前に `release.md`（または作成したテンプレート）を Fable 5.2（`--mode flagship --fast-no-tools`）で推敲し、AI臭さや誇張を削ぎ落とした信頼性の高い調査レポート文体に仕上げる：
```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode flagship --fast-no-tools \
  --files .agents/reports/release_<YYYY-MM>/release.md \
  --prompt "報道機関や外部ブロガーがそのまま引用したくなる客観的・端正な一次調査レポート文体に磨き上げて"
```

---

## Step 4: 本番公開統計エンドポイントの非汚染スモーク検証 ＆ 完了報告

デプロイ後（または月次点検時）、必ずベリファイスクリプトを実行して `/stats/monthly.csv`・`/stats/year-end-2026`・`/api/llms-facts.json` の全項目（HTTP 200・データ行数>0・`schema_version="2.1"`・CC BY 4.0 表記・`/link/` ゼロ・NG略称ゼロ）を検証する：
```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/data-release/scripts/verify_public_stats.py --days 30
```
完了報告では、主要な実測ハイライトと `citation_kit.md` の引用スニペット・SNS告知案をユーザーへ提示する。
