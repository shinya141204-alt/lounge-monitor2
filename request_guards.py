"""
request_guards.py - app.py 行数上限（2,800行）を守るためのリクエスト防御ヘルパー群

- JSON/フォーム本文サイズ上限（MAX_CONTENT_LENGTH）と 413 ハンドラ
- 壊れた時刻文字列で 500 にならない安全な JST naive パーサ
- 同一リクエスト内での共有バス（Redis）多重同期スキップ
- コールドスタート時の段階的スナップショット復元（OOM・重クエリ回避）
"""
import datetime

from flask import abort, g, has_request_context, jsonify, request

# 二段防御:
#   1) MAX_CONTENT_LENGTH = 2MB（アプリ全体のハード上限。管理画面からの長文記事公開でも 413 にしない）
#   2) 公開系の POST/PUT/PATCH は before_request で 256KB 超を 413（/api/track 等の濫用防止）
MAX_JSON_BODY_BYTES = 256 * 1024  # 256KB（公開エンドポイント上限）
MAX_ADMIN_BODY_BYTES = 2 * 1024 * 1024  # 2MB（アプリ全体ハード上限＝管理系上限）
ADMIN_BODY_PATH_PREFIXES = (
    "/staff",
    "/api/staff/",
    "/api/ops/",
    "/api/affiliate/",
    "/api/sheets/",
    "/api/editorial/",
    "/api/sns-editorial/",
    "/api/column-editorial/",
    "/api/pdca/",
)
_BODY_METHODS = frozenset(("POST", "PUT", "PATCH"))


def _is_admin_body_path(path: str) -> bool:
    p = path or ""
    for pre in ADMIN_BODY_PATH_PREFIXES:
        base = pre.rstrip("/")
        # `/staff` matches `/staff` and `/staff/...` only (not `/staff-xyz`)
        if p == base or p.startswith(base + "/"):
            return True
    return False

JST = datetime.timezone(datetime.timedelta(hours=9))
_TS_FORMATS = (
    "%Y-%m-%d %H:%M:%S",
    "%Y-%m-%dT%H:%M:%S",
    "%Y-%m-%d %H:%M:%S.%f",
    "%Y-%m-%dT%H:%M:%S.%f",
    "%Y-%m-%d %H:%M",
    "%Y/%m/%d %H:%M:%S",
)


def _wants_json() -> bool:
    try:
        if request.path.startswith("/api/"):
            return True
        return "application/json" in (request.headers.get("Accept") or "")
    except Exception:
        return False


def register_request_guards(app, render_http_error_fn=None):
    """MAX_CONTENT_LENGTH（既存設定があれば尊重。未設定なら 2MB）・公開系 256KB 上限フック・413 ハンドラを登録する。
    注意: Flask の既定 config は `MAX_CONTENT_LENGTH=None` を最初から持つため `setdefault` だけでは上限が効かない。"""
    app.config.setdefault("MAX_CONTENT_LENGTH", MAX_ADMIN_BODY_BYTES)
    if app.config.get("MAX_CONTENT_LENGTH") is None:
        app.config["MAX_CONTENT_LENGTH"] = MAX_ADMIN_BODY_BYTES

    @app.before_request
    def _enforce_public_body_limit():
        """管理系パス以外の POST/PUT/PATCH は宣言 Content-Length が 256KB 超なら本文を読む前に 413。
        （Content-Length 無しの chunked 送信は MAX_CONTENT_LENGTH=2MB のハード上限で防御）"""
        try:
            if request.method not in _BODY_METHODS:
                return None
            cl = request.content_length
            if cl is None or cl <= MAX_JSON_BODY_BYTES:
                return None
            hard_cap = app.config.get("MAX_CONTENT_LENGTH") or MAX_ADMIN_BODY_BYTES
            # Werkzeug enforces MAX_CONTENT_LENGTH only when the body is read; reject declared oversize up front
            if cl <= hard_cap and _is_admin_body_path(request.path):
                return None
        except Exception:
            return None
        abort(413)

    @app.errorhandler(413)
    def _handle_413_payload_too_large(e):
        if _wants_json():
            try:
                limit = MAX_ADMIN_BODY_BYTES if _is_admin_body_path(request.path) else MAX_JSON_BODY_BYTES
            except Exception:
                limit = MAX_JSON_BODY_BYTES
            return jsonify({"error": "payload_too_large", "max_bytes": limit}), 413
        if render_http_error_fn is not None:
            try:
                return render_http_error_fn(
                    e, 413, "Payload Too Large",
                    "送信データが大きすぎます",
                    "送信内容のサイズが上限を超えています。内容を短くして再度お試しください。",
                )
            except Exception:
                pass
        return "Payload Too Large", 413

    return app


def parse_jst_ts(raw_ts) -> "datetime.datetime | None":
    """`YYYY-MM-DD HH:MM:SS` / ISO（T区切り・マイクロ秒・TZ付き）を JST naive datetime に変換。
    不正値・空値・`"21:30"` のような時刻のみの文字列は例外を出さず None を返す。"""
    if raw_ts is None:
        return None
    if isinstance(raw_ts, datetime.datetime):
        dt = raw_ts
    else:
        s = str(raw_ts).strip()
        if not s or len(s) > 64:
            return None
        dt = None
        for fmt in _TS_FORMATS:
            try:
                dt = datetime.datetime.strptime(s, fmt)
                break
            except ValueError:
                continue
        if dt is None:
            try:
                dt = datetime.datetime.fromisoformat(s.replace("Z", "+00:00"))
            except (ValueError, TypeError):
                return None
    if dt.tzinfo is not None:
        try:
            dt = dt.astimezone(JST).replace(tzinfo=None)
        except (ValueError, OverflowError):
            return None
    return dt


def should_skip_duplicate_bus_sync(has_live_data: bool = True) -> bool:
    """同一リクエスト内で既に共有バス同期が成功していれば True。
    安全弁: ローカルの `full_data` が空なら（テスト・コールドスタート）決してスキップしない。"""
    if not has_live_data:
        return False
    try:
        if not has_request_context():
            return False
        return bool(getattr(g, "_bus_synced", False))
    except Exception:
        return False


def mark_bus_synced_for_request() -> None:
    try:
        if has_request_context():
            g._bus_synced = True
    except Exception:
        pass


def staged_snapshot_around(local_db_mod, windows=(180, 1440, 10080), min_stores: int = 45):
    """直近3時間 → 24時間 → 7日 の順に段階的に探索し、`min_stores` 店舗以上のスナップショットが
    見つかった時点で打ち切る（毎回7日分を一括走査しない）。見つからなければ最も店舗数の多い結果（または {}）。
    min_stores=45: 早朝コールドスタート時に営業中の少数店舗だけで打ち切り、閉店中店舗が欠落するのを防ぐ。"""
    best = {}
    for minutes in windows:
        try:
            snap = local_db_mod.get_snapshot_around(0, minutes)
        except Exception:
            snap = None
        if snap:
            if len(snap) >= len(best):
                best = snap
            if len(snap) >= min_stores:
                return snap
    return best
