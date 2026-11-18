"""
gcp_vertex_bridge.py - GCP Credit Consumption Bridge (Vertex AI Gemini + Google Cloud TTS).

Allows Aiseki Navi (`aiseki-navi-tokyo` on Fly.io) to consume Google Cloud Platform (GCP)
billing credits on project `loungemonitor` using the existing service account
(`GOOGLE_CREDENTIALS_B64` / `GOOGLE_CREDENTIALS_JSON` / `google_credentials.json`),
without migrating core web/worker/Postgres/Redis infrastructure off Fly.io:

  1. Vertex AI Gemini Priority Routing (`aiplatform.googleapis.com`):
     - Routes Gemini text/JSON (`sns_editorial`, `pdca_engine`, `column_editorial`) and
       multimodal screenshot OCR (`sns_insights_loop`) through Vertex AI first so usage
       draws down GCP promotional/trial credits (`$0` out-of-pocket).
     - Instant `<1ms` circuit-breaker failover to AI Studio (`generativelanguage.googleapis.com`)
       or Anthropic Claude on 401/403/404/429/5xx/timeouts or daily safety cap.

  2. Google Cloud Text-to-Speech (`texttospeech.googleapis.com`):
     - Synthesizes natural Japanese narration (`ja-JP-Chirp3-HD-Puck` / `ja-JP-Chirp3-HD-Aoede` /
       `ja-JP-Neural2-C` / `ja-JP-Neural2-B`) billed against GCP credits, selectable either as
       automatic fallback (`auto_fallback`) or primary TTS (`gcp_primary`).

Security, Cost-Safety & Unit-Test Isolation Guarantees:
  - Never exposes raw private keys or OAuth bearer tokens in status dicts, logs, or HTTP responses.
  - Enforces daily safety caps (`GCP_VERTEX_DAILY_CALL_CAP` & `GCP_TTS_DAILY_CHAR_CAP`) in `job_state`
    so even if a paid card is linked after promotional credits expire, runaway billing is impossible.
  - Strictly dormant when `DISABLE_SHEETS=1` / `PYTEST_CURRENT_TEST` is set unless explicitly enabled
    via `_test_credentials_override` or `ALLOW_GCP_BRIDGE_IN_TESTS=1`.
"""

import base64
import collections
import datetime
import hashlib
import json
import logging
import os
import re
import threading
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

import requests

import job_state
import turbo_store

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"
CLOUD_TTS_ENDPOINT = "https://texttospeech.googleapis.com/v1/text:synthesize"

GCP_VERTEX_ENABLED_KEY = "gcp_vertex_ai_enabled"
GCP_VERTEX_LOCATION_KEY = "gcp_vertex_location"
GCP_TTS_MODE_KEY = "gcp_cloud_tts_mode"
GCP_TTS_VOICE_KEY = "gcp_cloud_tts_voice"
GCP_RESET_EPOCH_KEY = "gcp_bridge_reset_epoch"
GCP_DAILY_USAGE_PREFIX = "gcp_daily_usage:"

DEFAULT_VERTEX_PRO_MODELS: Tuple[str, ...] = (
    "gemini-3.1-pro-preview",
    "gemini-3-flash-preview",
)
DEFAULT_VERTEX_FAST_MODELS: Tuple[str, ...] = (
    "gemini-3-flash-preview",
    "gemini-3.1-pro-preview",
)

SUPPORTED_GCP_TTS_MODES = collections.OrderedDict([
    ("auto_fallback", "自動バックアップ（ElevenLabs優先・未設定/上限時はGCP音声へ自動切替）"),
    ("gcp_primary", "GCPクレジット最優先（最初からGoogle Cloud HD音声を使用・ElevenLabs消費ゼロ）"),
    ("off", "OFF（GCP音声合成を使用しない）"),
])

SUPPORTED_GCP_TTS_VOICES = collections.OrderedDict([
    ("ja-JP-Chirp3-HD-Puck", "Chirp 3 HD 男性（自然・テンポ良／推奨）"),
    ("ja-JP-Chirp3-HD-Aoede", "Chirp 3 HD 女性（自然・クリア）"),
    ("ja-JP-Neural2-C", "Neural2 男性（落ち着いた報道トーン）"),
    ("ja-JP-Neural2-B", "Neural2 女性（明るい案内トーン）"),
])
_FEMALE_VOICES = frozenset({"ja-JP-Chirp3-HD-Aoede", "ja-JP-Neural2-B"})
DEFAULT_GCP_TTS_VOICE = "ja-JP-Chirp3-HD-Puck"
DEFAULT_GCP_TTS_MODE = "auto_fallback"
DEFAULT_VERTEX_LOCATION = "global"

MAX_TTS_SPOKEN_CHARS = 450
_TOKEN_CACHE_TTL_SEC = 3000.0
_COOLDOWN_TTL_SEC = 600.0
_RATE_LIMIT_COOLDOWN_SEC = 180.0
_GOOGLE_AUTH_TIMEOUT_SEC = 8.0

_lock = threading.Lock()
_token_cache: Dict[str, Any] = {
    "access_token": "",
    "expires_mono": 0.0,
    "project_id": "",
    "client_email": "",
    "cred_sig": "",
}
_unhealthy_vertex_models: Dict[str, float] = {}
_vertex_project_cooldown_until: float = 0.0
_tts_cooldown_until: float = 0.0
_seen_reset_epoch: str = ""

_runtime_meta: Dict[str, Any] = {
    "vertex_last_model": "",
    "vertex_last_ok_at": "",
    "vertex_last_error": "",
    "vertex_calls_ok": 0,
    "tts_last_voice": "",
    "tts_last_ok_at": "",
    "tts_last_error": "",
    "tts_calls_ok": 0,
}

_test_credentials_override: Optional[Dict[str, Any]] = None
_test_token_override: Optional[str] = None


def _now_jst(now_jst: Optional[datetime.datetime] = None) -> datetime.datetime:
    if isinstance(now_jst, datetime.datetime):
        return now_jst.astimezone(JST) if now_jst.tzinfo else now_jst.replace(tzinfo=JST)
    return datetime.datetime.now(JST)


def _now_jst_str(now_jst: Optional[datetime.datetime] = None) -> str:
    return _now_jst(now_jst).strftime("%Y-%m-%d %H:%M:%S")


def reset_gcp_bridge_state_for_tests() -> None:
    """Reset in-memory caches, circuit breakers, and test overrides."""
    global _vertex_project_cooldown_until, _tts_cooldown_until, _seen_reset_epoch
    global _test_credentials_override, _test_token_override
    with _lock:
        _token_cache.update({
            "access_token": "",
            "expires_mono": 0.0,
            "project_id": "",
            "client_email": "",
            "cred_sig": "",
        })
        _unhealthy_vertex_models.clear()
        _vertex_project_cooldown_until = 0.0
        _tts_cooldown_until = 0.0
        _seen_reset_epoch = ""
        _runtime_meta.update({
            "vertex_last_model": "",
            "vertex_last_ok_at": "",
            "vertex_last_error": "",
            "vertex_calls_ok": 0,
            "tts_last_voice": "",
            "tts_last_ok_at": "",
            "tts_last_error": "",
            "tts_calls_ok": 0,
        })
        _test_credentials_override = None
        _test_token_override = None


def _is_unit_test_isolated() -> bool:
    """
    Deterministic environment-based test guard (never checks `sys.modules` so transitive
    imports in production libraries can never silence Vertex AI / Cloud TTS).
    """
    if _test_credentials_override is not None:
        return False
    if os.environ.get("ALLOW_GCP_BRIDGE_IN_TESTS") == "1":
        return False
    if os.environ.get("DISABLE_SHEETS") == "1" or os.environ.get("PYTEST_CURRENT_TEST"):
        return True
    return False


def _sync_cross_process_reset_epoch(allow_blocking: bool = False) -> None:
    """Clear local process cooldowns if Master saved settings on another worker/machine."""
    global _vertex_project_cooldown_until, _tts_cooldown_until, _seen_reset_epoch
    try:
        ep = str(job_state.get_value(GCP_RESET_EPOCH_KEY, allow_blocking=allow_blocking, default="") or "")
    except Exception:
        return
    if not ep:
        return
    with _lock:
        if ep != _seen_reset_epoch:
            _seen_reset_epoch = ep
            _unhealthy_vertex_models.clear()
            _vertex_project_cooldown_until = 0.0
            _tts_cooldown_until = 0.0


def _load_service_account_info() -> Tuple[Optional[Dict[str, Any]], str]:
    """Load GCP Service Account JSON dict and return `(info_dict, source_label)`."""
    if _is_unit_test_isolated():
        return None, "disabled_in_tests"

    # Never fall through to real env/disk credentials if `_test_credentials_override` is set
    if _test_credentials_override is not None:
        if isinstance(_test_credentials_override, dict) and _test_credentials_override.get("project_id"):
            return dict(_test_credentials_override), "test_override"
        return None, "invalid_test_override"

    raw_json = (os.environ.get("GOOGLE_CREDENTIALS_JSON") or "").strip()
    if raw_json:
        try:
            parsed = json.loads(raw_json)
            if isinstance(parsed, dict) and parsed.get("client_email"):
                return parsed, "env:GOOGLE_CREDENTIALS_JSON"
        except Exception:
            pass

    raw_b64 = (os.environ.get("GOOGLE_CREDENTIALS_B64") or "").strip()
    if raw_b64:
        try:
            decoded = base64.b64decode(raw_b64).decode("utf-8")
            parsed = json.loads(decoded)
            if isinstance(parsed, dict) and parsed.get("client_email"):
                return parsed, "env:GOOGLE_CREDENTIALS_B64"
        except Exception:
            pass

    base_dir = os.path.dirname(os.path.abspath(__file__))
    for cand in (
        os.path.join(base_dir, "google_credentials.json"),
        "/etc/secrets/google_credentials.json",
    ):
        if os.path.isfile(cand):
            try:
                with open(cand, "r", encoding="utf-8") as f:
                    parsed = json.load(f)
                if isinstance(parsed, dict) and parsed.get("client_email"):
                    return parsed, f"file:{os.path.basename(cand)}"
            except Exception:
                continue

    return None, "unconfigured"


def _get_daily_caps() -> Tuple[int, int]:
    try:
        v_cap = max(10, int(os.environ.get("GCP_VERTEX_DAILY_CALL_CAP") or 400))
    except ValueError:
        v_cap = 400
    try:
        t_cap = max(500, int(os.environ.get("GCP_TTS_DAILY_CHAR_CAP") or 30000))
    except ValueError:
        t_cap = 30000
    return v_cap, t_cap


def _load_daily_usage(date_str: str, allow_blocking: bool = False) -> Dict[str, int]:
    raw = job_state.get_value(f"{GCP_DAILY_USAGE_PREFIX}{date_str}", allow_blocking=allow_blocking, default="")
    if not raw:
        return {"vertex_calls": 0, "tts_chars": 0}
    try:
        d = json.loads(raw)
        if isinstance(d, dict):
            return {
                "vertex_calls": max(0, int(d.get("vertex_calls") or 0)),
                "tts_chars": max(0, int(d.get("tts_chars") or 0)),
            }
    except Exception:
        pass
    return {"vertex_calls": 0, "tts_chars": 0}


def _record_daily_usage(date_str: str, add_vertex_calls: int = 0, add_tts_chars: int = 0) -> None:
    try:
        cur = _load_daily_usage(date_str, allow_blocking=False)
        cur["vertex_calls"] = int(cur.get("vertex_calls") or 0) + max(0, int(add_vertex_calls))
        cur["tts_chars"] = int(cur.get("tts_chars") or 0) + max(0, int(add_tts_chars))
        job_state.set_value(f"{GCP_DAILY_USAGE_PREFIX}{date_str}", json.dumps(cur))
    except Exception:
        pass


def is_gcp_service_account_configured() -> bool:
    info, _ = _load_service_account_info()
    return bool(info and info.get("project_id") and info.get("client_email"))


def is_vertex_ai_enabled(allow_blocking: bool = False) -> bool:
    if _is_unit_test_isolated():
        return False
    raw = str(job_state.get_value(GCP_VERTEX_ENABLED_KEY, allow_blocking=allow_blocking, default="") or "").strip().lower()
    if raw in ("0", "false", "off", "no"):
        return False
    if raw in ("1", "true", "on", "yes"):
        return True
    env_def = (os.environ.get("GCP_VERTEX_AI_ENABLED") or "1").strip().lower()
    return env_def not in ("0", "false", "off", "no")


def get_vertex_location(allow_blocking: bool = False) -> str:
    raw = str(job_state.get_value(GCP_VERTEX_LOCATION_KEY, allow_blocking=allow_blocking, default="") or "").strip().lower()
    if not raw:
        raw = (os.environ.get("GCP_VERTEX_LOCATION") or DEFAULT_VERTEX_LOCATION).strip().lower()
    if re.match(r"^[a-z0-9-]{3,32}$", raw):
        return raw
    return DEFAULT_VERTEX_LOCATION


def get_gcp_tts_mode(allow_blocking: bool = False) -> str:
    if _is_unit_test_isolated():
        return "off"
    raw = str(job_state.get_value(GCP_TTS_MODE_KEY, allow_blocking=allow_blocking, default="") or "").strip().lower()
    if raw in SUPPORTED_GCP_TTS_MODES:
        return raw
    env_mode = (os.environ.get("GCP_CLOUD_TTS_MODE") or DEFAULT_GCP_TTS_MODE).strip().lower()
    return env_mode if env_mode in SUPPORTED_GCP_TTS_MODES else DEFAULT_GCP_TTS_MODE


def get_gcp_tts_voice(allow_blocking: bool = False) -> str:
    raw = str(job_state.get_value(GCP_TTS_VOICE_KEY, allow_blocking=allow_blocking, default="") or "").strip()
    if raw in SUPPORTED_GCP_TTS_VOICES:
        return raw
    env_v = (os.environ.get("GCP_CLOUD_TTS_VOICE") or DEFAULT_GCP_TTS_VOICE).strip()
    return env_v if env_v in SUPPORTED_GCP_TTS_VOICES else DEFAULT_GCP_TTS_VOICE


def update_gcp_credit_settings(
    vertex_enabled: Optional[bool] = None,
    vertex_location: Optional[str] = None,
    tts_mode: Optional[str] = None,
    tts_voice: Optional[str] = None,
) -> Dict[str, Any]:
    """Persist Master GCP Credit settings and broadcast cooldown reset across processes."""
    global _vertex_project_cooldown_until, _tts_cooldown_until, _seen_reset_epoch
    if vertex_enabled is not None:
        job_state.set_value(GCP_VERTEX_ENABLED_KEY, "1" if bool(vertex_enabled) else "0")
    if vertex_location is not None:
        loc = str(vertex_location).strip().lower()
        if re.match(r"^[a-z0-9-]{3,32}$", loc):
            job_state.set_value(GCP_VERTEX_LOCATION_KEY, loc)
    if tts_mode is not None:
        norm_mode = str(tts_mode).strip().lower()
        if norm_mode in SUPPORTED_GCP_TTS_MODES:
            job_state.set_value(GCP_TTS_MODE_KEY, norm_mode)
    if tts_voice is not None:
        norm_v = str(tts_voice).strip()
        if norm_v in SUPPORTED_GCP_TTS_VOICES:
            job_state.set_value(GCP_TTS_VOICE_KEY, norm_v)

    new_epoch = str(int(time.time() * 1000))
    job_state.set_value(GCP_RESET_EPOCH_KEY, new_epoch)

    with _lock:
        _seen_reset_epoch = new_epoch
        _unhealthy_vertex_models.clear()
        _vertex_project_cooldown_until = 0.0
        _tts_cooldown_until = 0.0
        _runtime_meta["vertex_last_error"] = ""
        _runtime_meta["tts_last_error"] = ""

    return get_gcp_credit_bridge_status(allow_blocking=True)


def _invalidate_cached_token() -> None:
    with _lock:
        _token_cache["access_token"] = ""
        _token_cache["expires_mono"] = 0.0


def mint_gcp_access_token(force_refresh: bool = False) -> Tuple[str, str, str, str]:
    """
    Mint or return cached OAuth2 Bearer token for `cloud-platform` scope.
    Returns `(access_token, project_id, client_email, error_message)`.
    """
    info, _ = _load_service_account_info()
    if not info:
        return "", "", "", "GCPサービスアカウント（GOOGLE_CREDENTIALS_B64）が未設定です"

    project_id = str(info.get("project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    client_email = str(info.get("client_email") or "").strip()
    key_id = str(info.get("private_key_id") or "").strip()
    if not project_id or not client_email:
        return "", project_id, client_email, "サービスアカウントJSONに project_id または client_email がありません"

    if _test_token_override:
        return _test_token_override, project_id, client_email, ""

    cred_sig = hashlib.sha256(f"{project_id}:{client_email}:{key_id}".encode("utf-8")).hexdigest()[:16]
    now_mono = time.monotonic()

    with _lock:
        if (
            not force_refresh
            and _token_cache["access_token"]
            and _token_cache["cred_sig"] == cred_sig
            and _token_cache["expires_mono"] > now_mono
        ):
            return (
                _token_cache["access_token"],
                _token_cache["project_id"],
                _token_cache["client_email"],
                "",
            )

    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account

        class _BoundedAuthReq(GoogleAuthRequest):
            def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
                eff_timeout = _GOOGLE_AUTH_TIMEOUT_SEC if timeout is None else min(float(timeout), _GOOGLE_AUTH_TIMEOUT_SEC)
                return super().__call__(url, method=method, body=body, headers=headers, timeout=eff_timeout, **kwargs)

        creds = service_account.Credentials.from_service_account_info(
            info,
            scopes=[CLOUD_PLATFORM_SCOPE],
        )
        creds.refresh(_BoundedAuthReq())
        tok = str(creds.token or "").strip()
        if not tok:
            return "", project_id, client_email, "GCPアクセストークンが空でした"

        with _lock:
            _token_cache.update({
                "access_token": tok,
                "expires_mono": time.monotonic() + _TOKEN_CACHE_TTL_SEC,
                "project_id": project_id,
                "client_email": client_email,
                "cred_sig": cred_sig,
            })
        return tok, project_id, client_email, ""
    except Exception as exc:
        return "", project_id, client_email, f"GCPサービスアカウント認証エラー: {type(exc).__name__}"


def get_gcp_access_token(
    scopes: Tuple[str, ...] = (CLOUD_PLATFORM_SCOPE,),
    timeout_sec: float = _GOOGLE_AUTH_TIMEOUT_SEC,
    now_jst: Optional[datetime.datetime] = None,
) -> Tuple[str, str]:
    """
    Mint an OAuth2 Bearer token for arbitrary Google API scopes (e.g. `webmasters.readonly`
    for Google Search Console) using the configured GCP service account with bounded timeout.
    Returns `(access_token, error_message)`.
    """
    clean_scopes = tuple(str(s).strip() for s in (scopes or (CLOUD_PLATFORM_SCOPE,)) if str(s).strip())
    if clean_scopes == (CLOUD_PLATFORM_SCOPE,):
        tok, _, _, err = mint_gcp_access_token()
        return tok, err

    info, _ = _load_service_account_info()
    if not info:
        return "", "GCPサービスアカウント（GOOGLE_CREDENTIALS_B64）が未設定です"
    if _test_token_override:
        return _test_token_override, ""

    try:
        from google.auth.transport.requests import Request as GoogleAuthRequest
        from google.oauth2 import service_account

        bound_sec = max(2.0, min(float(timeout_sec or _GOOGLE_AUTH_TIMEOUT_SEC), 15.0))

        class _ScopedBoundedAuthReq(GoogleAuthRequest):
            def __call__(self, url, method="GET", body=None, headers=None, timeout=None, **kwargs):
                eff_timeout = bound_sec if timeout is None else min(float(timeout), bound_sec)
                return super().__call__(url, method=method, body=body, headers=headers, timeout=eff_timeout, **kwargs)

        creds = service_account.Credentials.from_service_account_info(info, scopes=list(clean_scopes))
        creds.refresh(_ScopedBoundedAuthReq())
        tok = str(creds.token or "").strip()
        if not tok:
            return "", "アクセストークンが空でした"
        return tok, ""
    except Exception as exc:
        return "", f"サービスアカウント認証エラー: {type(exc).__name__}"


def build_vertex_generate_content_url(project_id: str, model_name: str, location: str = "global") -> str:
    loc = (location or "global").strip().lower()
    host = "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"
    return (
        f"https://{host}/v1/projects/{project_id}/locations/{loc}"
        f"/publishers/google/models/{model_name}:generateContent"
    )


def resolve_vertex_candidate_models(prefer_fast: bool = False, ignore_cooldown: bool = False) -> List[str]:
    """
    Return ordered list of healthy Vertex AI Gemini models.
    Excludes cooled-down models (unless `ignore_cooldown=True`) so an outage fails over in <1ms.
    """
    custom_m = (os.environ.get("VERTEX_GEMINI_MODEL") or "").strip()
    base = list(DEFAULT_VERTEX_FAST_MODELS if prefer_fast else DEFAULT_VERTEX_PRO_MODELS)
    if custom_m and custom_m not in base:
        base.insert(0, custom_m)
    elif custom_m in base:
        base.remove(custom_m)
        base.insert(0, custom_m)

    if ignore_cooldown:
        return base

    now_mono = time.monotonic()
    with _lock:
        for k, exp in list(_unhealthy_vertex_models.items()):
            if now_mono >= exp:
                _unhealthy_vertex_models.pop(k, None)
        return [m for m in base if m not in _unhealthy_vertex_models]


def _classify_gcp_http_error(status_code: int, body_text: str, service_label: str, project_id: str) -> str:
    low = (body_text or "").lower()
    if status_code == 403:
        if "billing" in low:
            return f"GCPプロジェクト『{project_id}』の請求先（クレジット枠）が未リンクまたは上限到達です（自動で既存キーへフォールバック済）"
        if "has not been used" in low or "service_disabled" in low or "api is disabled" in low:
            return f"GCPプロジェクト『{project_id}』で {service_label} が未有効化です（自動で既存キーへフォールバック済）"
        return f"GCPサービスアカウントに {service_label} のIAM権限がありません（HTTP 403）"
    if status_code == 429:
        return f"{service_label} レート上限（HTTP 429・自動で既存キーへフォールバック済）"
    if status_code == 404:
        return f"{service_label} 指定モデル未対応（HTTP 404）"
    return f"{service_label} HTTP {status_code}"


def call_vertex_gemini_parts(
    parts: List[Dict[str, Any]],
    system_instruction: str = "",
    max_output_tokens: int = 2600,
    thinking_budget: int = 1024,
    temperature: float = 0.85,
    prefer_fast: bool = False,
    total_deadline_sec: float = 16.0,
    per_call_timeout_sec: float = 12.0,
    allow_blocking: bool = False,
    json_parser: Optional[Callable[[str], Optional[Dict[str, Any]]]] = None,
    now_jst: Optional[datetime.datetime] = None,
    ignore_cooldown: bool = False,
) -> Tuple[Optional[Dict[str, Any]], str, str]:
    """
    Execute a Vertex AI `generateContent` call (text or multimodal inlineData) and parse JSON response.
    Returns `(parsed_dict_or_None, model_label, error_str)`.
    Never raises; returns `(None, "", reason)` immediately if disabled, capped, or cooled down.
    """
    global _vertex_project_cooldown_until
    if not ignore_cooldown and not is_vertex_ai_enabled(allow_blocking=allow_blocking):
        return None, "", "vertex_disabled"
    if _is_unit_test_isolated():
        return None, "", "vertex_disabled"

    _sync_cross_process_reset_epoch(allow_blocking=False)

    now_mono = time.monotonic()
    if not ignore_cooldown:
        with _lock:
            if _vertex_project_cooldown_until > now_mono:
                return None, "", "vertex_cooldown"

    today_str = _now_jst(now_jst).strftime("%Y-%m-%d")
    v_cap, _ = _get_daily_caps()
    usage = _load_daily_usage(today_str, allow_blocking=False)
    if not ignore_cooldown and usage["vertex_calls"] >= v_cap:
        return None, "", f"Vertex AI 日次安全上限（{v_cap}回/日）に到達したため既存キーへ自動切替中"

    models = resolve_vertex_candidate_models(prefer_fast=prefer_fast, ignore_cooldown=ignore_cooldown)
    if not models:
        return None, "", "vertex_cooldown"

    access_token, project_id, _, tok_err = mint_gcp_access_token(force_refresh=ignore_cooldown)
    if tok_err or not access_token or not project_id:
        with _lock:
            _runtime_meta["vertex_last_error"] = tok_err or "no_token"
        return None, "", tok_err or "no_token"

    location = get_vertex_location(allow_blocking=allow_blocking)
    deadline = time.monotonic() + max(3.0, float(total_deadline_sec))

    out_tokens = max(2048, int(max_output_tokens or 2048))
    tb_req = int(thinking_budget or 0)
    last_err = ""
    retried_401 = False

    for idx, m_name in enumerate(models):
        rem = deadline - time.monotonic()
        if rem <= 1.0:
            break
        has_next = idx < len(models) - 1
        call_timeout = min(float(per_call_timeout_sec), max(1.8, rem - (2.0 if has_next else 0.2)))

        gen_cfg: Dict[str, Any] = {
            "temperature": float(temperature),
            "maxOutputTokens": max(4096, out_tokens + 2048),
            "responseMimeType": "application/json",
        }
        low_m = m_name.lower()
        if low_m.startswith("gemini-3") or "carbon" in low_m or "argon" in low_m:
            gen_cfg["thinkingConfig"] = {
                "thinkingLevel": "MEDIUM" if (tb_req >= 1024 and not prefer_fast) else "LOW"
            }

        body: Dict[str, Any] = {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": gen_cfg,
        }
        if system_instruction:
            body["systemInstruction"] = {"parts": [{"text": system_instruction}]}

        url = build_vertex_generate_content_url(project_id, m_name, location=location)
        headers = {
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        }
        try:
            resp = requests.post(url, headers=headers, json=body, timeout=(3.5, call_timeout))

            # On 401, invalidate stale OAuth token and retry once with freshly minted token
            if resp.status_code == 401 and not retried_401 and (deadline - time.monotonic()) > 2.0:
                retried_401 = True
                _invalidate_cached_token()
                fresh_tok, _, _, re_err = mint_gcp_access_token(force_refresh=True)
                if fresh_tok and not re_err:
                    access_token = fresh_tok
                    headers["Authorization"] = f"Bearer {access_token}"
                    resp = requests.post(
                        url,
                        headers=headers,
                        json=body,
                        timeout=(3.5, max(1.5, min(float(per_call_timeout_sec), deadline - time.monotonic()))),
                    )

            if resp.status_code == 400 and "thinkingconfig" in (resp.text or "").lower() and (deadline - time.monotonic()) > 2.0:
                retry_cfg = dict(gen_cfg)
                retry_cfg.pop("thinkingConfig", None)
                retry_body = dict(body, generationConfig=retry_cfg)
                resp = requests.post(
                    url,
                    headers=headers,
                    json=retry_body,
                    timeout=(3.5, max(1.5, min(float(per_call_timeout_sec), deadline - time.monotonic()))),
                )

            if resp.status_code >= 400:
                friendly = _classify_gcp_http_error(resp.status_code, resp.text or "", "Vertex AI API", project_id)
                last_err = f"{friendly} ({m_name})"
                logger.warning(f"[gcp_vertex_bridge] {last_err}")
                with _lock:
                    _runtime_meta["vertex_last_error"] = last_err
                    if resp.status_code in (401, 403):
                        _vertex_project_cooldown_until = time.monotonic() + _COOLDOWN_TTL_SEC
                        return None, "", last_err
                    if resp.status_code == 429:
                        _vertex_project_cooldown_until = time.monotonic() + _RATE_LIMIT_COOLDOWN_SEC
                        _unhealthy_vertex_models[m_name] = time.monotonic() + _RATE_LIMIT_COOLDOWN_SEC
                        return None, "", last_err
                    _unhealthy_vertex_models[m_name] = time.monotonic() + _COOLDOWN_TTL_SEC
                continue

            _record_daily_usage(today_str, add_vertex_calls=1)
            cands = ((resp.json() or {}).get("candidates")) or []
            if not cands:
                last_err = f"Vertex AI empty candidates ({m_name})"
                with _lock:
                    _runtime_meta["vertex_last_error"] = last_err
                continue
            resp_parts = ((cands[0].get("content") or {}).get("parts")) or []
            raw_txt = "".join(str(p.get("text") or "") for p in resp_parts if not p.get("thought")).strip()
            if not raw_txt:
                last_err = f"Vertex AI empty text ({m_name})"
                with _lock:
                    _runtime_meta["vertex_last_error"] = last_err
                continue

            parsed = json_parser(raw_txt) if callable(json_parser) else _fallback_parse_json_obj(raw_txt)
            if isinstance(parsed, dict) and parsed:
                ts_ok = _now_jst_str(now_jst)
                with _lock:
                    _unhealthy_vertex_models.pop(m_name, None)
                    _runtime_meta["vertex_last_model"] = m_name
                    _runtime_meta["vertex_last_ok_at"] = ts_ok
                    _runtime_meta["vertex_last_error"] = ""
                    _runtime_meta["vertex_calls_ok"] = int(_runtime_meta.get("vertex_calls_ok") or 0) + 1
                return parsed, f"vertex:{m_name}", ""
            last_err = f"Vertex AI JSON解析失敗 ({m_name})"
            with _lock:
                _runtime_meta["vertex_last_error"] = last_err
        except Exception as exc:
            last_err = f"Vertex AI通信エラー ({m_name}): {type(exc).__name__}"
            logger.warning(f"[gcp_vertex_bridge] {last_err}")
            with _lock:
                _unhealthy_vertex_models[m_name] = time.monotonic() + _RATE_LIMIT_COOLDOWN_SEC
                _runtime_meta["vertex_last_error"] = last_err

    # If all candidate models failed, open short project-level breaker so subsequent calls fail over in <1ms
    with _lock:
        if not resolve_vertex_candidate_models(prefer_fast=prefer_fast, ignore_cooldown=False):
            _vertex_project_cooldown_until = max(_vertex_project_cooldown_until, time.monotonic() + _RATE_LIMIT_COOLDOWN_SEC)

    return None, "", last_err or "vertex_unavailable"


def _fallback_parse_json_obj(raw_text: str) -> Optional[Dict[str, Any]]:
    txt = (raw_text or "").strip()
    if not txt:
        return None
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt).strip()
    try:
        obj = json.loads(txt, strict=False)
        if isinstance(obj, list) and obj and isinstance(obj[0], dict):
            return obj[0]
        if isinstance(obj, dict):
            return obj
    except Exception:
        pass
    s_idx = txt.find("{")
    e_idx = txt.rfind("}")
    if s_idx != -1 and e_idx > s_idx:
        try:
            obj = json.loads(txt[s_idx : e_idx + 1], strict=False)
            if isinstance(obj, dict):
                return obj
        except Exception:
            pass
    return None


def synthesize_gcp_cloud_tts(
    script_text: str,
    phonetic_text: str = "",
    voice_name: Optional[str] = None,
    timeout: float = 14.0,
    force_refresh: bool = False,
    cache_dir: str = "/tmp/aiseki_reels",
    cache_ttl_sec: int = 7200,
    duration_prober: Optional[Callable[[str], float]] = None,
    ignore_cooldown: bool = False,
) -> Dict[str, Any]:
    """
    Synthesize Japanese MP3 narration via Google Cloud Text-to-Speech API (`Chirp 3 HD` / `Neural2`)
    using GCP service account credits (`$0` ElevenLabs credit consumption).
    """
    global _tts_cooldown_until
    if _is_unit_test_isolated():
        return {"ok": False, "error": "GCP Cloud TTS is disabled in isolated unit tests."}

    _sync_cross_process_reset_epoch(allow_blocking=False)

    eff_voice = (voice_name or "").strip()
    if eff_voice not in SUPPORTED_GCP_TTS_VOICES:
        eff_voice = get_gcp_tts_voice(allow_blocking=False)

    spoken_text = (phonetic_text or script_text or "").strip()[:MAX_TTS_SPOKEN_CHARS]
    if not spoken_text:
        return {"ok": False, "error": "読み上げ台本が空です。", "script_text": script_text, "phonetic_text": ""}

    cache_raw = f"gcp_tts_v1|{eff_voice}|{spoken_text}"
    tts_hash = hashlib.sha256(cache_raw.encode("utf-8")).hexdigest()[:24]
    os.makedirs(cache_dir, exist_ok=True)
    disk_mp3_path = os.path.join(cache_dir, f"gcp_tts_{tts_hash}.mp3")
    shared_key = f"reel_tts:gcp_{tts_hash}"
    now_ts = time.time()

    def _measure_dur(p: str) -> float:
        """Strictly return measured audio duration from `ffprobe`/`ffmpeg` (`0.0` if unavailable; zero bitrate guessing)."""
        if callable(duration_prober):
            try:
                val = float(duration_prober(p))
                return val if val > 0 else 0.0
            except Exception:
                return 0.0
        return 0.0

    if not force_refresh:
        if os.path.isfile(disk_mp3_path):
            try:
                if (now_ts - os.path.getmtime(disk_mp3_path)) < cache_ttl_sec and os.path.getsize(disk_mp3_path) > 512:
                    with open(disk_mp3_path, "rb") as f_in:
                        cached_bytes = f_in.read()
                    return {
                        "ok": True,
                        "cached": True,
                        "provider": "gcp_cloud_tts",
                        "audio_bytes": cached_bytes,
                        "audio_path": disk_mp3_path,
                        "duration_sec": _measure_dur(disk_mp3_path),
                        "script_text": script_text,
                        "phonetic_text": spoken_text,
                        "voice_id": eff_voice,
                        "model_id": "gcp_cloud_tts",
                    }
            except Exception:
                pass
        try:
            shared_hit = turbo_store.get_media_blob(shared_key)
            if shared_hit and shared_hit[0] and len(shared_hit[0]) > 512:
                cached_bytes = bytes(shared_hit[0])
                tmp_p = f"{disk_mp3_path}.{os.getpid()}.{threading.get_ident()}.tmp"
                with open(tmp_p, "wb") as f_out:
                    f_out.write(cached_bytes)
                os.replace(tmp_p, disk_mp3_path)
                return {
                    "ok": True,
                    "cached": True,
                    "provider": "gcp_cloud_tts",
                    "audio_bytes": cached_bytes,
                    "audio_path": disk_mp3_path,
                    "duration_sec": _measure_dur(disk_mp3_path),
                    "script_text": script_text,
                    "phonetic_text": spoken_text,
                    "voice_id": eff_voice,
                    "model_id": "gcp_cloud_tts",
                }
        except Exception:
            pass

    now_mono = time.monotonic()
    if not ignore_cooldown:
        with _lock:
            if not force_refresh and _tts_cooldown_until > now_mono:
                return {
                    "ok": False,
                    "error": "Google Cloud TTS クールダウン中のためBGMへフォールバックしました。",
                    "cooldown_active": True,
                    "script_text": script_text,
                    "phonetic_text": spoken_text,
                }

    today_str = _now_jst().strftime("%Y-%m-%d")
    _, t_cap = _get_daily_caps()
    usage = _load_daily_usage(today_str, allow_blocking=False)
    if not ignore_cooldown and (usage["tts_chars"] + len(spoken_text)) > t_cap:
        return {
            "ok": False,
            "error": f"Cloud TTS 日次文字数上限（{t_cap}文字/日）に到達したためフォールバックしました。",
            "script_text": script_text,
            "phonetic_text": spoken_text,
        }

    access_token, project_id, _, tok_err = mint_gcp_access_token(force_refresh=ignore_cooldown)
    if tok_err or not access_token:
        return {"ok": False, "error": tok_err or "GCP認証トークン未取得", "script_text": script_text, "phonetic_text": spoken_text}

    headers = {
        "Authorization": f"Bearer {access_token}",
        "Content-Type": "application/json",
    }
    gender_matched_neural = "ja-JP-Neural2-B" if eff_voice in _FEMALE_VOICES else "ja-JP-Neural2-C"
    voice_candidates = [eff_voice]
    if gender_matched_neural not in voice_candidates:
        voice_candidates.append(gender_matched_neural)

    last_err = ""
    for cand_voice in voice_candidates:
        audio_cfg: Dict[str, Any] = {"audioEncoding": "MP3"}
        if "Chirp" not in cand_voice:
            audio_cfg["speakingRate"] = 1.05
        req_json = {
            "input": {"text": spoken_text},
            "voice": {"languageCode": "ja-JP", "name": cand_voice},
            "audioConfig": audio_cfg,
        }
        try:
            resp = requests.post(CLOUD_TTS_ENDPOINT, headers=headers, json=req_json, timeout=(3.5, float(timeout)))
            if resp.status_code == 401:
                _invalidate_cached_token()
                fresh_tok, _, _, re_err = mint_gcp_access_token(force_refresh=True)
                if fresh_tok and not re_err:
                    headers["Authorization"] = f"Bearer {fresh_tok}"
                    resp = requests.post(CLOUD_TTS_ENDPOINT, headers=headers, json=req_json, timeout=(3.5, float(timeout)))

            if resp.status_code == 200:
                b64_audio = str((resp.json() or {}).get("audioContent") or "").strip()
                audio_bytes = base64.b64decode(b64_audio) if b64_audio else b""
                if len(audio_bytes) >= 512:
                    _record_daily_usage(today_str, add_tts_chars=len(spoken_text))
                    tmp_p = f"{disk_mp3_path}.{os.getpid()}.{threading.get_ident()}.tmp"
                    with open(tmp_p, "wb") as f_out:
                        f_out.write(audio_bytes)
                    os.replace(tmp_p, disk_mp3_path)
                    try:
                        turbo_store.set_media_blob(
                            shared_key,
                            audio_bytes,
                            mime_type="audio/mpeg",
                            ttl_sec=cache_ttl_sec,
                        )
                    except Exception:
                        pass
                    dur = _measure_dur(disk_mp3_path)
                    with _lock:
                        _runtime_meta["tts_last_voice"] = cand_voice
                        _runtime_meta["tts_last_ok_at"] = _now_jst_str()
                        _runtime_meta["tts_last_error"] = ""
                        _runtime_meta["tts_calls_ok"] = int(_runtime_meta.get("tts_calls_ok") or 0) + 1
                    return {
                        "ok": True,
                        "cached": False,
                        "provider": "gcp_cloud_tts",
                        "audio_bytes": audio_bytes,
                        "audio_path": disk_mp3_path,
                        "duration_sec": dur,
                        "script_text": script_text,
                        "phonetic_text": spoken_text,
                        "voice_id": cand_voice,
                        "model_id": "gcp_cloud_tts",
                    }
                last_err = f"Cloud TTS 空音声レスポンス ({cand_voice})"
            else:
                last_err = _classify_gcp_http_error(resp.status_code, resp.text or "", "Cloud Text-to-Speech API", project_id)
                if resp.status_code in (401, 403, 429):
                    with _lock:
                        _tts_cooldown_until = time.monotonic() + _COOLDOWN_TTL_SEC
                        _runtime_meta["tts_last_error"] = last_err
                    break
        except Exception as exc:
            last_err = f"Cloud TTS通信エラー ({cand_voice}): {type(exc).__name__}"

    with _lock:
        _runtime_meta["tts_last_error"] = last_err
    return {
        "ok": False,
        "error": last_err or "Cloud TTS合成失敗",
        "script_text": script_text,
        "phonetic_text": spoken_text,
    }


def get_gcp_credit_bridge_status(allow_blocking: bool = False) -> Dict[str, Any]:
    """Return safe, secret-free GCP Credit Bridge status for Master dashboards."""
    _sync_cross_process_reset_epoch(allow_blocking=allow_blocking)
    info, cred_source = _load_service_account_info()
    project_id = str((info or {}).get("project_id") or os.environ.get("GOOGLE_CLOUD_PROJECT") or "").strip()
    client_email = str((info or {}).get("client_email") or "").strip()
    sa_configured = bool(project_id and client_email)

    vertex_on = is_vertex_ai_enabled(allow_blocking=allow_blocking)
    location = get_vertex_location(allow_blocking=allow_blocking)
    tts_mode = get_gcp_tts_mode(allow_blocking=allow_blocking)
    tts_voice = get_gcp_tts_voice(allow_blocking=allow_blocking)

    today_str = _now_jst().strftime("%Y-%m-%d")
    v_cap, t_cap = _get_daily_caps()
    usage = _load_daily_usage(today_str, allow_blocking=allow_blocking)

    now_mono = time.monotonic()
    with _lock:
        v_cd = max(0, int(_vertex_project_cooldown_until - now_mono))
        t_cd = max(0, int(_tts_cooldown_until - now_mono))
        meta_snap = dict(_runtime_meta)

    return {
        "service_account_configured": sa_configured,
        "credential_source": cred_source,
        "project_id": project_id,
        "client_email": client_email,
        "vertex_enabled": vertex_on,
        "vertex_active": bool(sa_configured and vertex_on and v_cd == 0 and usage["vertex_calls"] < v_cap),
        "vertex_location": location,
        "vertex_cooldown_sec": v_cd,
        "vertex_models": list(DEFAULT_VERTEX_PRO_MODELS),
        "vertex_daily_calls": usage["vertex_calls"],
        "vertex_daily_cap": v_cap,
        "vertex_last_model": meta_snap["vertex_last_model"],
        "vertex_last_ok_at": meta_snap["vertex_last_ok_at"],
        "vertex_last_error": meta_snap["vertex_last_error"],
        "vertex_calls_ok": meta_snap["vertex_calls_ok"],
        "tts_mode": tts_mode,
        "tts_mode_label": SUPPORTED_GCP_TTS_MODES.get(tts_mode, tts_mode),
        "supported_tts_modes": [{"id": k, "label": v} for k, v in SUPPORTED_GCP_TTS_MODES.items()],
        "tts_voice": tts_voice,
        "tts_voice_label": SUPPORTED_GCP_TTS_VOICES.get(tts_voice, tts_voice),
        "supported_tts_voices": [{"id": k, "label": v} for k, v in SUPPORTED_GCP_TTS_VOICES.items()],
        "tts_daily_chars": usage["tts_chars"],
        "tts_daily_cap": t_cap,
        "tts_cooldown_sec": t_cd,
        "tts_last_voice": meta_snap["tts_last_voice"],
        "tts_last_ok_at": meta_snap["tts_last_ok_at"],
        "tts_last_error": meta_snap["tts_last_error"],
        "tts_calls_ok": meta_snap["tts_calls_ok"],
        "console_urls": {
            "vertex_api": f"https://console.cloud.google.com/apis/library/aiplatform.googleapis.com?project={project_id or 'loungemonitor'}",
            "tts_api": f"https://console.cloud.google.com/apis/library/texttospeech.googleapis.com?project={project_id or 'loungemonitor'}",
            "billing_credits": f"https://console.cloud.google.com/billing?project={project_id or 'loungemonitor'}",
        },
    }


def run_gcp_credit_connectivity_test() -> Dict[str, Any]:
    """
    Master-only live diagnostic probe verifying both Vertex AI Gemini (`generateContent`)
    and Google Cloud TTS (`text:synthesize`) against the configured GCP project credits.
    Always bypasses cooldown (`ignore_cooldown=True`) so Master tests real live connectivity.
    """
    t0 = time.monotonic()
    access_token, project_id, client_email, tok_err = mint_gcp_access_token(force_refresh=True)
    if tok_err or not access_token:
        return {
            "ok": False,
            "error": tok_err or "GCPサービスアカウント認証に失敗しました",
            "status": get_gcp_credit_bridge_status(allow_blocking=True),
        }

    parsed, v_model, v_err = call_vertex_gemini_parts(
        parts=[{"text": 'Return JSON {"gcp_vertex_ok": true}'}],
        max_output_tokens=256,
        thinking_budget=0,
        temperature=0.0,
        prefer_fast=True,
        total_deadline_sec=12.0,
        per_call_timeout_sec=10.0,
        allow_blocking=True,
        ignore_cooldown=True,
    )
    vertex_ok = bool(isinstance(parsed, dict) and parsed.get("gcp_vertex_ok") is True)

    tts_res = synthesize_gcp_cloud_tts(
        script_text="相席ナビの音声疎通テストです。",
        phonetic_text="あいせきナビのおんせいそつうテストです。",
        timeout=10.0,
        force_refresh=True,
        ignore_cooldown=True,
    )
    tts_ok = bool(tts_res.get("ok"))
    elapsed_ms = int((time.monotonic() - t0) * 1000)

    return {
        "ok": bool(vertex_ok and tts_ok),
        "partial_ok": bool(vertex_ok or tts_ok),
        "project_id": project_id,
        "client_email": client_email,
        "elapsed_ms": elapsed_ms,
        "vertex_ok": vertex_ok,
        "vertex_model": v_model,
        "vertex_error": "" if vertex_ok else (v_err or "Vertex AI応答異常"),
        "tts_ok": tts_ok,
        "tts_voice": tts_res.get("voice_id") or "",
        "tts_error": "" if tts_ok else str(tts_res.get("error") or "Cloud TTS応答異常"),
        "status": get_gcp_credit_bridge_status(allow_blocking=True),
    }
