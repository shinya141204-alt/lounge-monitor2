---
name: growth-pdca
description: >-
  相席ナビ（https://aiseki-navi.com）Webメディア専用の「フェーズ連動型・流入拡大最優先」グロースPDCA＆次週必勝アクション策定スキル（※店舗内部業務の `/lounge-pdca` とは別）。
  ユーザーが `/growth-pdca` と入力したときや、「相席ナビのPDCAを回して」「流入を増やす次の一手は？」「次にどの記事・動画・SEOをやるべき？」と依頼したときに必ず有効化する。
  現在のグロースフェーズ（Phase 1：流入拡大最優先［目標 100〜300 PV/日］→ Phase 2：回遊定着 → Phase 3：収益最大化）に基づき、外部純新規流入・前週比（WoW）・Search Consoleお宝クエリ・ショート動画誘導・ドーム公演＆店舗需要ギャップを非汚染で統合分析し、最優先3手を数値根拠付きで算出する。
---

# `/growth-pdca` 相席ナビ フェーズ連動・流入最優先グロースPDCA Runbook

外部からの純新規流入（Google/AI検索SEO・縦型ショート動画・SNS `?s=`）と前週比（WoW）成長率を North Star 指標とし、成長フェーズに合わせて「今週打つべき必勝3手」を算出するグロース参謀スキルです。
（※オリエンタルラウンジ長崎店の社内ミーティング報告用スキル `/lounge-pdca` とは完全に独立しています。社内データをこちらに混ぜないでください。）

- **リポジトリルート**: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`
- **評価フェーズ設計（`pdca_engine.py` と完全共通）**:
  - **Phase 1：流入拡大最優先（`< 300 PV/日`・第1マイルストーン `100〜300 PV/日`）**:
    - 配点比率：純新規流入・WoW成長 `35pt` ／ 検索SEO・AI検索基盤 `30pt` ／ ショート動画・SNSプロフ誘導 `25pt` ／ 有効回遊・定着率 `10pt`
    - 最優先3手：①Google/AI検索CTR改善（GSC 3〜30位クエリ・イベントサージ） ②需要ギャップ特命コラム ③縦型ショート動画（3Dもしも＋15秒ギャップ実証）プロフ誘導
    - 収益導線提案の制限：期間 `50 PV` 以上の高流入ページが発生している場合のみTOP3へ参入し、未収益化アフィリ枠整備等は次点バックログへ分離する。
  - **Phase 2：回遊定着＆2ステップ強化（`300〜999 PV/日`）** ／ **Phase 3：収益CVR・EPC最大化（`1,000+ PV/日`）**: PV達成後に自動移行（`/staff/pdca` で手動固定も可）。
- **Search Console API 自動取得スクリプト**: [scripts/fetch_gsc_api.py](./scripts/fetch_gsc_api.py)
- **需要・流入シグナル収集スクリプト**: [scripts/collect_demand_signals.py](./scripts/collect_demand_signals.py)
- **優先機会ランキングスクリプト**: [scripts/rank_opportunities.py](./scripts/rank_opportunities.py)

---

## Step 1: 外部流入 ＆ 検索・現場需要シグナルの非汚染収集

（任意・GSC API閲覧権限付与済みの場合）まず Search Console API から直近28日のクエリ・ページCSVを自動取得し、続けて本番ライブ稼働状況（`?staff_test=1` 非汚染取得）・ウォッチ条件分布・ドーム公演予定・`pdca_engine.py` のフェーズ診断データを `signals_latest.json` に集約する：

```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/growth-pdca/scripts/fetch_gsc_api.py --days 28
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/growth-pdca/scripts/collect_demand_signals.py --days 14 --live --gsc-csv /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/reports/gsc_queries_latest.csv
```

---

## Step 2: フェーズ連動スコアリングと最優先3手の算出

収集した `signals_latest.json` から週次グロースアクションプラン（`.agents/reports/pdca_<YYYY-Www>.md`）を生成する：

```bash
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/growth-pdca/scripts/rank_opportunities.py
```

さらに全体戦略のセカンドオピニオンを取る場合は、**Claude Fable 5.2（`--mode strategy --fast-no-tools`）** に渡して検証する：

```bash
python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/claude-review/scripts/run_claude_review.py \
  --mode strategy --fast-no-tools \
  --files .agents/reports/signals_latest.json \
  --prompt "Phase 1（100〜300 PV/日突破・検索SEOと縦型ショート動画最優先）の基準で、今週もっとも純新規流入を伸ばす3手を検証して"
```

---

## Step 3: 採択アクションの専門スキルへの委譲・実装

選出された上位3手をユーザーへ提示し、承認後ただちに各専門スキルで実行する：
1. **検索クエリCTR改善・店舗/エリア比較UI改善・ショート動画スタジオ整備** → `/ui`（`editorial-ui-ux`）スキル
2. **需要ギャップ・ドーム終演サージの新規データコラム作成** → `/article` スキル（`templates/guide_*.html` ＋ note独立エッセイ）
3. **（高PV記事発生時またはPhase 2以降）アフィリエイト導線・ASP案件差し替え** → `/affiliate` スキル
