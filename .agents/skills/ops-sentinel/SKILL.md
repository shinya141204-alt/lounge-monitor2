---
name: ops-sentinel
description: >-
  相席ナビ（https://aiseki-navi.com）専用の運用品質・本番非汚染プローブ・データモート整合性・デプロイ前後1コマンド品質ゲートスキル。
  ユーザーが `/ops-sentinel` と入力したときや、「本番の健全性をチェックして」「データが欠けていないか見て」「デプロイ前の総合チェックをして」「Fly Postgresや統計エンドポイントは正常？」と依頼したときに必ず有効化する。
  本番エンドポイントの非汚染監視（`/api/status` 鮮度・`/stats/monthly.csv` 行数・`/api/llms-facts.json` v2.1・`/robots.txt` `/link/` 遮断・サイトマップ整合）、DB整合性（1hロールアップ連続性・90日保持・14日IPハッシュ匿名化）、およびデプロイ前の全監査＋単体テスト一括実行を担う。
---

# `/ops-sentinel` 運用品質・データモート健全性・デプロイ前後1コマンド守護神 Runbook

Fly Postgres のメモリ逼迫による空CSV出力、スクレイパー停止、14日匿名化ジョブ漏れ、デプロイ前のキャッシュ番号ズレなどを **1コマンドで事前・事後に完全ブロック** する運用守護スキルです。

- **リポジトリルート**: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`
- **本番非汚染プローブ＆デプロイ前総合ゲート**: [scripts/run_ops_sentinel.py](./scripts/run_ops_sentinel.py)
- **データモートDB整合性・匿名化チェッカー**: [scripts/check_data_integrity.py](./scripts/check_data_integrity.py)

---

## Step 1: 本番公開エンドポイントの非汚染ヘルスチェック（`--live`）

本番サイトの死活・鮮度・一次統計出力・ボット遮断規約を人間PV非汚染（`-A "curl/8.0"` 互換UA ＋ `?staff_test=1`）で一括点検する：

```bash
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/ops-sentinel/scripts/run_ops_sentinel.py --live
```

検査される項目：
1. `/api/status?staff_test=1`：HTTP 200、稼働店舗数 `>= 50`、スナップショット鮮度
2. `/api/llms-facts.json?staff_test=1`：`schema_version == "2.1"`、`/link/` 混入ゼロ
3. `/stats/monthly.csv?days=30&staff_test=1`：データ行数 `>= 1`（DB停止時の0行検知）、`Cache-Control` 適正
4. `/robots.txt?staff_test=1`：全 `User-agent:` ブロックにおける `Disallow: /link/` 維持
5. `/sitemap.xml?staff_test=1`：HTTP 200、主要ピラールート網羅

---

## Step 2: データモートDB整合性・14日匿名化・ロールアップ欠損点検

ローカルDB（または接続先DB）に対し、データモート規約（AGENTS.md §8）が守られているかを点検する：

```bash
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/ops-sentinel/scripts/check_data_integrity.py --days 90
```

---

## Step 3: デプロイ前ワンコマンド品質ゲート（`--pre-deploy`）

本番デプロイ前に、①アフィリエイト＆薬機法＆直 `/link/` ＆ブランド表記監査、②脱AI UI＆未定義CSS変数＆キャッシュ完全同期監査、③店舗マスタ＆別業態定義監査、④データモートDB整合性チェック、⑤全単体テストスイート（`--quick` 指定時は高速UI検査のみ）を直列実行し、1つでも失敗したら即停止する：

```bash
# 高速ゲート（UI静的検査＋全監査＋全unittest）
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/ops-sentinel/scripts/run_ops_sentinel.py --pre-deploy --quick-ui

# フルゲート（Playwright実ブラウザ回帰テストまで全実行）
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/ops-sentinel/scripts/run_ops_sentinel.py --pre-deploy
```

---

## Step 4: 異常検出時の復旧 ＆ Claude Fable 5.2 最終監査連携

1. Step 1〜3 で `error` または `warn` が検出された場合は、原因モジュール（`local_db.py`, `routes/portal_bp.py` 等）や Fly Postgres VM 状態（`/Users/kuwanoshinya/.fly/bin/flyctl status -a aiseki-navi-tokyo-db`）を確認して修正する。
2. 重要なロジック修正を行った場合は `run_claude_review.py --mode audit --git-diff` で最終検閲を通し、再度 `--pre-deploy` → デプロイ → `--live` で `error: 0` を確認して `.agents/reports/` のレポートとともにユーザーへ報告する。
