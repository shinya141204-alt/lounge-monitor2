# 相席ナビ 運用メモ

## 環境変数（Render の Environment で設定）

| 変数 | 用途 | 既定値 |
|---|---|---|
| `ALERT_WEBHOOK_URL` | 取得失敗・店舗数の急減・新店舗の検知・X投稿の失敗を通知する Discord / Slack の Webhook URL | 未設定（ログ出力のみ） |
| `ALERT_FAIL_THRESHOLD` | 何回連続で失敗したら「停止」として通知するか（1回＝営業時間中は約1分） | `5` |
| `ALERT_COOLDOWN_MIN` | 同じ通知を再送しない時間（分） | `60` |
| `LOCAL_DB_PATH` | SQLite の保存先。Render の Persistent Disk を使う場合は `/var/data/lounge_data.db` など | プロジェクト直下 |
| `STAFF_PASSCODE` | 現場スタッフ用ログインパスコード（`/staff` で現場データのみ閲覧可・AdSense非表示） | `5555` |
| `MASTER_PASSCODE` | マスター（オーナー）用ログインパスコード（データ監査・アクセス解析・SNSキット・予約キュー等全機能） | `2918` |
| `ADMIN_API_SECRET` | 管理・強制トリガーAPI用のシークレットトークン（本番では変更推奨） | `lounge2026` |
| `VOTE_LIMIT_PER_VOTER` / `VOTE_LIMIT_PER_IP` | 現場投票のレート制限（10分あたり） | `8` / `40` |
| `DISABLE_SCHEDULER` / `DISABLE_SHEETS` | テスト用。`1` でバックグラウンド処理と Sheets 連携を止める | 未設定 |

> **重要（Gunicorn ワーカー数の制約）**：プロセス内で APScheduler（1分ごとのスクレイピング・X/Threads/Instagram定時投稿）とインメモリ最新キャッシュを動かしているため、`Procfile` の `--workers 1` は必ず `1` のまま維持してください（複数ワーカーにするとスケジューラが多重起動します）。同時接続性能は `--threads 8` で担保しています。

## データの保存先（再デプロイで消えないもの）

既存のスプレッドシート「Lounge Monitor Data」に、次のワークシートが自動で作られます。

- **Crowd Votes**：現場投票の全履歴（起動時に直近24時間分を復元）
- **Job State**：X自動投稿・ひとことTips・note告知の「投稿済み」記録（二重投稿の防止）
- **Analytics Daily**：日別・イベント別のクリック数／閲覧数（10分ごとにまとめて追記）

## 店舗マスタ

`static/store_master.json` が、地域・エリア表示・現在地からの距離・マップ検索の起点を決める唯一の正です。
新しい店舗が増えたら、ここに1件追加してください（未登録の店舗名が出てくると通知が届きます）。

## 監視

- `GET /api/health`：生存確認用（常に 200）
- `GET /api/health/deep`：データが古い、またはいずれかの系列が停止中なら **503**。cron-job.org や UptimeRobot で監視すると、止まったときに気づけます。
- `GET /staff/analytics`：アクセス集計（スタッフとしてログインが必要）

## テスト

```bash
python3 -m unittest discover -s tests -v
```

外部への通信は行いません。`node` がある環境では `static/app.js` の構文チェックも行います（`NODE_BIN` で node の場所を指定できます）。
