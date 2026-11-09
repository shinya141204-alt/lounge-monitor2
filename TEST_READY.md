# 相席ナビ (AISEKI NAVI) R1〜R5包括E2Eテストスイート準備完了報告 (TEST_READY.md)

## 1. 概要
相席ナビ（AISEKI NAVI）の要求仕様書（`ORIGINAL_REQUEST.md`）およびプロジェクト設計書（`PROJECT.md`）に基づき、製品全体の振る舞いを不透明ボックス（Opaque-box）で検証する包括的な **4階層E2Eテストスイート (`tests/test_e2e_requirements_r5.py`)** の設計・実装・配置が完了しました。

外部API呼び出しやネットワーク通信に一切依存せず、安全なセッションモック・RAMリングバッファ・ローカルDBトランザクション・完全匿名ビーコン機構を精密にシミュレートし、全59件の厳格な検証テストケースを網羅しています。

---

## 2. テスト実行コマンド

### 2.1 E2Eテストスイート単体実行（推奨）
```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest tests/test_e2e_requirements_r5.py
```

### 2.2 全テストスイート一括実行（全体回帰テスト）
```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest discover -s tests
```

---

## 3. 4層階層アプローチ（テスト設計構成）サマリー

本テストスイートは、要求仕様書およびプロジェクト設計書で義務付けられた4階層アプローチに厳格に準拠して設計されています。

| Tier | 分類 | テスト件数 | 検証対象 |
|:---|:---|:---:|:---|
| **Tier 1** | 機能網羅（Feature Coverage） | 29件 | F1〜F16（R1〜R5）の主要正常系・個別機能の孤立動作確認 |
| **Tier 2** | 境界値・極限ケース（Boundary & Corner Cases） | 21件 | ゼロ除算・負数・未定義・欠損・タイムアウト・ボット除外・閉店間際異常値防衛 |
| **Tier 3** | 複数機能連携（Cross-Feature Combinations） | 5件 | 機能間インターフェース（インテント×60分後予測、寒暖差×終演サージ、待ち逆転×回転モメンタム等） |
| **Tier 4** | 実世界シナリオ（Real-World Application Scenarios） | 4件 | 金曜夜梅田ハシゴ動線、初冬神宮ナイター急冷サージ、JIS女性待ち逆転コンプライアンス、監査不変条件 |
| **合計** | **全階層合計** | **59件** | **（目標50件以上を完全達成）** |

---

## 4. テスト実行現況（2026-11-10 TDDフェーズ時点）

- **総テスト数**: 59件
- **構文・インポート・ランタイムエラー**: **0件（完全完走）**
- **実行結果**: **41件 PASS / 18件 FAIL** (Ran in ~1.17s)
- **FAILのステータス**:
  - テストコード側の欠陥ではなく、**「Workerによる新機能実装待ち（18件）」** を TDD（テスト駆動開発）の赤（Red）状態として正確に検出している正常な状態です。
  - 実装コードがマージされた時点で、各テストが自動的にグリーン（PASS）へ転換します。

---

## 5. 機能別チェックリスト (F1 〜 F16)

### R1: JIS詳細メトリクス収集・集計・表示
- [ ] **F1**: 1組平均人数算出・判定・表示 (`test_01`, `test_02`) — *Worker M1 実装待ち*
- [ ] **F2**: VIP相席成立率差の算出・表示 (`test_03`, `test_04`) — *Worker M1 実装待ち*
- [ ] **F3**: 女性待ち逆転現象の検知・表示 (`test_05`, `test_06`) — *Worker M1 実装待ち*
- [ ] **F4**: JIS詳細メトリクスの永久ロールアップ保存 (`test_07`, `test_08`) — *Worker M1 実装待ち*
- [x] JIS境界値安全性（ゼロ組数、負数、外れ値9999名、VIPゼロ）(`test_30`, `test_31`, `test_32`, `test_33`)

### R2: 3秒ハシゴ診断 先行需要インテント集計
- [ ] **F5**: 先行需要インテント匿名集計 (`test_09`, `test_10`) — *Worker M2 実装待ち*
- [x] **F5 (プライバシー)**: IPアドレス・User-Agent・個人識別子の保存ゼロ保証 (`test_11`)
- [ ] **F6**: ハシゴ診断インテントと60分後予測連携 (`test_12`, `test_51`) — *Worker M2 実装待ち*
- [ ] **F7**: フロントエンド ビーコン送信・許可イベント追加 (`test_13`, `test_14`, `test_15`) — *Worker M2 実装待ち*
- [x] ハシゴ診断境界値（未許可イベント拒絶、長大ラベルサニタイズ、ボット・スタッフ除外）(`test_36`, `test_37`, `test_38`, `test_39`)

### R3: 席回転スピード指数・寒暖差・都市イベント拡張
- [ ] **F8**: 席回転スピード指数（回転モメンタム指数）算出 (`test_16`) — *Worker M3 実装待ち*
- [ ] **F9**: 席回転スピード指数の店舗カード・詳細表示 (`test_17`, `test_18`) — *Worker M3 実装待ち*
- [ ] **F10**: 前日比寒暖差（Δ℃）の追加通信ゼロ算出 (`test_19`, `test_20`) — *Worker M3 実装待ち*
- [ ] **F11**: 都市イベント静的マスタ拡張と2次会サージ連携 (`test_21`, `test_22`) — *Worker M3 実装待ち*
- [x] 席回転境界値（営業時間外除外、6名未満除外、閉店間際退店モード、ラグ欠損フォールバック）(`test_40`, `test_41`, `test_42`, `test_43`)
- [x] 気象・イベント境界値（欠損フォールバック、-3.0℃急冷閾値、極端気温、予定なし日付）(`test_44`, `test_45`, `test_46`, `test_47`, `test_48`)

### R4: 梅田・茶屋町 ASOBIBAR vs ヤタコイ 回遊シフト
- [ ] **F12**: 梅田・茶屋町 ASOBIBAR vs ヤタコイ 分離保存 (`test_23`, `test_24`) — *Worker M4 実装待ち*
- [ ] **F13**: 梅田・茶屋町 2フロア回遊シフト分析・表示 (`test_25`, `test_26`) — *Worker M4 実装待ち*
- [x] 茶屋町境界値（Firebase空辞書耐性、席数ゼロ防衛、トップレベル合算値互換）(`test_49`, `test_50`)

### R5: コードベース制約・品質ゲート・本番デプロイ
- [x] **F14**: 4階層E2Eテストスイート構築＆全階層網羅 (`test_27`)
- [x] **F15**: ops-sentinel スクリプト存在＆実行可能性 (`test_28`)
- [x] **F16**: 本番非汚染スモークテスト仕様（`?staff_test=1` + `curl/8.0`）準拠 (`test_29`)
- [x] `app.py` 行数上限 2,800行以下厳守 (`test_59`)
- [x] `turbo_store.py:302` 不変条件厳守 (`test_59`)
- [x] ブランド正式名称厳守（単体「OLG」の露出ゼロ）(`test_58`)
- [x] 一次統計面（`/stats`）における直 `/link/` ゼロ厳守 (`test_55`)

---

## 6. エスカレーション事項（Worker実装待ち一覧 / TDD Red 項目）

本E2Eテストスイートにより、現在未実装となっている機能が以下の通り明確に特定されています。各担当Workerへの実装引継ぎをお願いします。

### 6.1 【Milestone 1】JIS詳細メトリクス担当（Worker M1）
1. `monitor.py`: `get_jis_data()` において `pos_telemetry` に `avg_party_size_men`, `avg_party_size_women`, `avg_party_size_total`, `party_style`, `normal_match_rate`, `vip_match_rate`, `match_rate_diff`, `is_women_waiting`, `is_reverse_waiting` を追加格納すること。
2. `local_db.py`: `store_hourly_rollups` に新カラム（`avg_women_waiting_groups`, `max_women_waiting_groups`, `women_waiting_detected`, `avg_party_size`, `avg_normal_match_rate`, `avg_vip_match_rate`, `avg_match_rate_diff`）をマイグレーション追加し、`rollup_store_hourly_stats()` で集計すること。
3. `templates/store_detail.html`: JIS店舗詳細にソロ/ペア傾向、VIP相席率、女性待ちアラートを表示すること。

### 6.2 【Milestone 2】3秒ハシゴ診断インテント担当（Worker M2）
1. `analytics.py`: `ALLOWED_EVENTS` に `'hashigo_intent'` を追加し、`get_lead_demand_heat(area, hours)` を実装すること。
2. `app.py`: `_BEACON_ALLOWED_EVENTS` に `'hashigo_intent'` を追加すること（2,800行以下厳守）。
3. `moat_intelligence.py`: `predict_store_60m_dynamics()` に引数 `lead_intent_heat` を受け取り、予測バイアスを反映すること。
4. `templates/concierge.html`: 診断実行時の非同期ビーコン（`/api/track`）送信コードを追加すること。

### 6.3 【Milestone 3】回転スピード・寒暖差・都市イベント担当（Worker M3）
1. `moat_intelligence.py`: `calculate_turnover_momentum_index()` を実装し、`enrich_stores_with_moat_signals()` で各店舗に付与すること。
2. `moat_intelligence.py`: `_fetch_open_meteo_all_cities()` のパラメータに `past_days=1&hourly=temperature_2m` を追加し、前日差分 `temp_diff_c` および `is_cold_surge` を店舗 `weather_brief` に格納すること。
3. `arena_calendar.py`: `STATIC_CITY_EVENTS` を定義（神宮、甲子園、日産、国立、花火大会）。
4. `moat_intelligence.py`: `MAJOR_ARENA_HUBS` に上記スタジアムを追加。
5. `templates/index.html` & `templates/store_detail.html`: 席回転ピルおよび急冷サージバッジを表示すること。

### 6.4 【Milestone 4】茶屋町回遊シフト担当（Worker M4）
1. `monitor.py`: `get_yatakoi_data()` においてトップレベル合算値互換を維持しつつ、`pos_telemetry`（`telemetry_type = 'yatakoi_pos'`）に `yatakoi_men`, `yatakoi_women`, `asobibar_men`, `asobibar_women` を格納すること。
2. `moat_intelligence.py`: `get_chayamachi_shift_analysis()` を実装すること。
3. `templates/store_detail.html`: 梅田茶屋町ビル2フロア回遊シフト表示カードを追加すること。

---

## 7. 最終合格判定基準 (Acceptance Criteria)

Worker M1〜M4 による全機能の実装完了後、以下のテスト実行コマンドにより **59件すべてのテストが `OK` (100% PASS)** となることを確認してください。

```bash
cd "/Users/kuwanoshinya/.gemini/相席業務改善/相席モニター"
DISABLE_SHEETS=1 DISABLE_SCHEDULER=1 PYTHONPATH=. python3 -m unittest tests/test_e2e_requirements_r5.py
```

**期待される最終実行結果**:
```
Ran 59 tests in ~1.2s
OK
```
