---
name: growth-pdca
description: >-
  相席ナビ（https://aiseki-navi.com）Webメディア専用の需要シグナル×収益EPC横断分析＆次週必勝アクション策定スキル（※店舗内部業務の `/lounge-pdca` とは別）。
  ユーザーが `/growth-pdca` と入力したときや、「相席ナビのPDCAを回して」「次にどの記事を書くべき？」「CVR・収益を上げる次の一手は？」と依頼したときに必ず有効化する。
  ウォッチ登録条件（`store_watch_intents`）・現地投票・本番ライブ需要・記事在庫・アフィリエイト枠別EPC（＋任意のSearch Console CSV）を非汚染で統合分析し、最優先3手（新規記事／リライト／2ステップ導線改善）を数値根拠付きで算出する。
---

# `/growth-pdca` 相席ナビ 需要×収益クロスPDCA ＆ 次週必勝3手策定 Runbook

ユーザーの生の行動シグナル（「どの店のどんな条件を待っているか」「どのページが読まれ、どの2ステップ導線で離脱しているか」）と収益期待値（EPC）を掛け合わせ、勘ではなく実数値で次の一手を決めるグロース参謀スキルです。
（※オリエンタルラウンジ長崎店の社内ミーティング報告用スキル `/lounge-pdca` とは完全に独立しています。社内データをこちらに混ぜないでください。）

- **リポジトリルート**: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`
- **Search Console API 自動取得スクリプト**: [scripts/fetch_gsc_api.py](./scripts/fetch_gsc_api.py)
- **需要・収益シグナル収集スクリプト**: [scripts/collect_demand_signals.py](./scripts/collect_demand_signals.py)
- **優先機会ランキングスクリプト**: [scripts/rank_opportunities.py](./scripts/rank_opportunities.py)

---

## Step 1: 需要シグナル ＆ 収益パフォーマンスの非汚染収集

（任意・GSC API閲覧権限付与済みの場合）まず Search Console API から直近28日のクエリ・ページCSVを自動取得し、続けて本番ライブ稼働状況（`?staff_test=1` 非汚染取得）・ウォッチ条件分布・現地投票・既存コラム網羅状況・`pdca_engine.py` の診断データを `signals.json` に集約する：

```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/growth-pdca/scripts/fetch_gsc_api.py --days 28
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/growth-pdca/scripts/collect_demand_signals.py --days 14 --live --gsc-csv /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/reports/gsc_queries_latest.csv
```

---

## Step 2: 機会スコアリング（需要 × 未充足度 × EPC）と最優先3手の算出

収集した `signals.json` から機会ランキングレポート（`.agents/reports/pdca_<YYYY-Www>.md`）を生成する：

```bash
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/growth-pdca/scripts/rank_opportunities.py
```

さらに深い全体戦略レビューを行う場合は、生成されたレポートと `signals.json` を **Claude Fable 5.1（`--mode strategy --fast-no-tools`）** に渡してセカンドオピニオンを取る：

```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode strategy --fast-no-tools \
  --files .agents/reports/signals_latest.json \
  --prompt "収集された実需シグナルと記事ギャップから、今週もっともCVRと検索流入を伸ばす3手を検証して"
```

---

## Step 3: 採択アクションの専門スキルへの委譲・実装

選出された上位3手をユーザーへ提示し、承認後ただちに各専門スキルで実行する：
1. **新規データコラム作成** → `/article` スキル（`templates/guide_*.html` ＋ note独立エッセイ）
2. **クッションページ・店舗詳細のUI/導線改善** → `/ui`（`editorial-ui-ux`）スキル
3. **アフィリエイト案件差し替え・未収益化枠のASP化** → `/affiliate` スキル

---

## Step 4: 前回サイクルとの差分トラッキング

翌週以降に再実行すると、`rank_opportunities.py` が直近の過去 `signals_*.json` と比較して記事カバー率や需要推移の差分を自動サマリ化します。
