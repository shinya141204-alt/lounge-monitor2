#!/usr/bin/env bash
# ==============================================================================
# 相席ナビ「プラン竹」東京データセンター（Fly.io `nrt`）構築＆無停止移行スクリプト
# ※ Google Cloud (GCP) は一切使用しません（Fly.io 東京 + Fly Postgres 東京 + Upstash Redis 東京）
# ==============================================================================
set -euo pipefail

APP_NAME="${FLY_APP_NAME:-aiseki-navi-tokyo}"
REGION="nrt"

echo "=================================================================="
echo "🚀 相席ナビ プラン竹：東京リージョン (${REGION}) セットアップ開始"
echo "   対象アプリ名: ${APP_NAME}"
echo "=================================================================="

if ! command -v flyctl >/dev/null 2>&1; then
  echo "⚠️  flyctl がインストールされていません。先に以下を実行してください："
  echo "   curl -L https://fly.io/install.sh | sh"
  exit 1
fi

echo ""
echo "[Step 1] Fly.io ログイン状態を確認中..."
flyctl auth whoami

echo ""
echo "[Step 2] 東京リージョン (${REGION}) にアプリ '${APP_NAME}' を作成（既存ならスキップ）..."
if ! flyctl apps list | awk '{print $1}' | grep -qx "${APP_NAME}"; then
  flyctl apps create "${APP_NAME}"
else
  echo "   -> 既に '${APP_NAME}' が存在します。"
fi

echo ""
echo "[Step 3] PostgreSQL 東京 (${REGION}) の作成・アタッチ確認..."
echo "   ※ まだ作成していない場合は以下を実行してください："
echo "   flyctl mpg create --name ${APP_NAME}-db --region ${REGION}"
echo "   （または従来の unmanaged: flyctl postgres create --name ${APP_NAME}-pg --region ${REGION} && flyctl postgres attach ${APP_NAME}-pg -a ${APP_NAME}）"

echo ""
echo "[Step 4] Upstash Redis 東京 (${REGION}) の作成確認..."
echo "   ※ まだ作成していない場合は以下を実行し、発行された rediss:// URL を REDIS_URL にセットしてください："
echo "   flyctl redis create --name ${APP_NAME}-redis --region ${REGION} --no-replicas"
echo "   flyctl secrets set REDIS_URL='rediss://...' -a ${APP_NAME}"

echo ""
echo "[Step 5] アプリケーション（Web 2ワーカー + 専用 Worker 1台）を東京へデプロイ中..."
flyctl deploy --config fly.toml -a "${APP_NAME}" --region "${REGION}"

echo ""
echo "[Step 6] Google Sheets から過去30日ログ・暗号化SNSトークン・PV集計を東京DBへ初期移行中..."
flyctl ssh console -a "${APP_NAME}" -C "python3 scripts/migrate_sheets_to_db.py --days 30"

echo ""
echo "[Step 7] カスタムドメイン SSL 証明書の発行登録（エックスサーバードメイン切替準備）..."
flyctl certs add aiseki-navi.com -a "${APP_NAME}" || true
flyctl certs add www.aiseki-navi.com -a "${APP_NAME}" || true
flyctl ips list -a "${APP_NAME}"

echo ""
echo "✅ 東京環境 (${APP_NAME}.fly.dev) のデプロイとデータ移行が完了しました！"
echo "続いてエックスサーバードメインのDNSレコード編集画面で："
echo "  1) @ (Aレコード)   -> 上記で表示された IPv4 アドレスへ変更"
echo "  2) @ (AAAAレコード)-> 上記で表示された IPv6 アドレスを追加（任意・推奨）"
echo "  3) www (CNAME)     -> ${APP_NAME}.fly.dev へ変更"
echo "を保存すると、ダウンタイム0秒で東京サーバーへ切り替わります！"
