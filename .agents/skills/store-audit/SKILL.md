---
name: store-audit
description: >-
  相席ナビ（https://aiseki-navi.com）専用の店舗マスタ・営業時間・14日ロールアップドリフト・料金改定・別業態比較定義・新店舗ズレ自動点検スキル。
  ユーザーが `/store-audit` と入力したときや、各ブランド（オリエンタルラウンジ・ag・JIS・ASOBIBAR・ヤタコイ・CLOVERS・ALFA・XIX等）の営業時間変更、料金改定、新店舗追加、閉店チェックを依頼したときに必ず有効化する。
  `data_audit.py`・`store_hourly_rollups`・本番 `/api/status?staff_test=1`（非汚染UA）を用いて構造整合性・継続営業時間ドリフト・未知店舗・公式改定アナウンスを一括診断し、必要なマスタ更新からテスト・デプロイまで実行する。
---

# `/store-audit` 店舗マスタ・営業時間・料金・新店舗ズレ点検 Runbook

ユーザーが `/store-audit` または店舗情報・営業時間・料金の点検・更新を依頼した際は、以下の **Step 1 〜 Step 4** を実行してください。

- **リポジトリルート**: `/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター`
- **一括監査スクリプト**: [scripts/run_full_store_audit.py](./scripts/run_full_store_audit.py)

---

## Step 1: 静的マスタ・ロールアップ履歴 ＆ 本番ライブドリフト監査の実行

1. まずローカル静的マスタ・スラッグ整合性・別業態比較定義・ロールアップ営業時間ドリフト監査を実行する：
   ```bash
   DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/store-audit/scripts/run_full_store_audit.py --rollup-days 14
   ```
2. 続けて本番サイトのライブ稼働データと照合し、**「営業時間外なのに客が入っている店舗」**や**「公式サイトに出現した未登録の新店舗」**も検知する場合は `--live` を付けて実行する：
   ```bash
   DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター/.agents/skills/store-audit/scripts/run_full_store_audit.py --live --rollup-days 14
   ```
   * **非汚染保証**: スクリプト内部で自動的に `https://aiseki-navi.com/api/status?staff_test=1` と明示ボットUA `AisekiNavi-StoreAudit/2.0 (curl/8.0)` を使用するため、本番の人間PV計測を一切汚染しません。

---

## Step 2: 検知項目の診断とブランド表記規約の確認

レポートで以下のいずれかが検出された場合、原因を特定して修正案を整理する：

- **`missing_hours` / `slug_mismatch`**：`static/store_master.json`・`store_master.STORE_SLUGS`・`store_hours.py` 間の不整合
- **`missing_price` / `incomplete_price_fields`**：国内店舗が `price_comparison.py`（`STORE_PRICE_TABLE`）に未登録、または必須7項目が欠損
- **`adjacent_format_issue`**：`price_comparison.ADJACENT_FORMAT_SPECS`（別業態横断比較定義）の必須フィールド欠損
- **`unknown_live_store`**：スクレイピング先に新店舗または店名変更が発生
- **`hours_drift` / `hours_drift_recurring`**：設定上の営業時間外に合計6名以上の来店が観測（特に14日ロールアップで2日以上再現している場合は営業時間変更の可能性大）
- **`official_revision_notice` / `event_history_recent`**：公式ニュースや `store_event_history` に料金改定・営業時間変更・閉店・新キャンペーンが出現

> **ブランド表記の絶対規約**：
> 公開表示名（`seo_name`・記事・UI）では単体の『オリエンタル』や略称『OLG』『ORIENTAL』『YATAKOI』『オリラジ』を絶対に使わず、必ず正式名称 **『オリエンタルラウンジ』**・**『ヤタコイ』** に統一すること。

---

## Step 3: マスタ更新時の同期ルール（新店舗追加・閉店・改定時）

店舗の追加・変更を行う場合は、必ず以下のファイルをセットで更新する：

1. **`static/store_master.json` ＆ `store_master.py`**：内部キー、スラッグ（`slug`）、地域（`region` / `area`）、正式表示名、住所・URLを登録
2. **`store_hours.py`**：`STORE_BUSINESS_HOURS` に曜日別・祝前日の営業時間を登録
3. **`price_comparison.py`**：国内店舗は `STORE_PRICE_TABLE` に全時間帯料金・1名入店可否・チャージを漏れなく登録
4. **`moat_intelligence.py`**（新店舗追加時）：`STORE_P99_CAPACITY` にフロア収容目安を追加

---

## Step 4: 再監査 ＆ 全テスト ＆ 本番デプロイ

ファイルを更新した場合は、必ず以下を実行して `重大エラー: 0件` と全テスト通過を確認してから GitHub `main` プッシュ ＆ Fly.io デプロイを行う：

```bash
cd /Users/kuwanoshinya/.gemini/相席業務改善/相席モニター && \
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 python3 .agents/skills/store-audit/scripts/run_full_store_audit.py && \
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest discover -s tests
```
