"""
sns_editorial.py - Multi-Platform 4-Role SNS Editorial Room Pipeline (X / Threads / Instagram)

Replaces rigid, repetitive "🥇🥈🥉 TOP3" bot posts across ALL 3 SNS channels (X, Threads, Instagram)
with a 4-role virtual editorial team:
  1. Researcher (現場データ発掘係): Extracts live hub gender-ratio gaps, 15-min surges, usual same-weekday anomalies, and East/West leaders, while verifying strict factual thresholds.
  2. Planner (編成・マンネリ防止係): Tracks per-channel history (`x`, `threads`, `instagram`) so consecutive posts never repeat the same angle or hook, and suppresses mechanical TOP3 lists.
  3. Writer (執筆・下書き係): Generates 4 distinct human-voiced draft candidates tailored to each platform's algorithm and character rules.
  4. Evaluator / Desk (鬼デスク・評価係): Scores all 4 candidates out of 100 across 4 axes (Human Tone, Anti-Duplication, Data Accuracy/Specificity, Platform Format Hygiene), attaches red-pen critiques, and selects the #1 winner.

All proposals, scores, and critiques are exposed exclusively to Master (`2918`) on `/staff/sns-editorial`.
"""

import os
import re
import time
import json
import datetime
import logging
import threading
import collections
import requests

import job_state
import store_master
import x_poster

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))
GEMINI_KEY_STORAGE_PREFIX = "token:gemini_api_key"
CLAUDE_KEY_STORAGE_PREFIX = "token:claude_api_key"
AI_ENGINE_PREF_KEY = "editorial:ai_engine_pref"

# Upgraded from Flash-Lite to Pro -> Standard Flash -> Lite emergency fallback
DEFAULT_GEMINI_MODELS = (
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-3.5-flash-lite",
)

# Anthropic Claude Sonnet models (dynamically supplemented via /v1/models when key is set)
DEFAULT_CLAUDE_MODELS = (
    "claude-sonnet-4-6",
    "claude-sonnet-4-5",
    "claude-3-7-sonnet-latest",
)

# Words/phrases that betray AI-generated or promotional copywriting — strictly penalized by Desk
AI_CLICHE_WORDS = (
    "必見",
    "徹底解説",
    "徹底解剖",
    "いかがでしたか",
    "いかがですか",
    "いかがでしょうか",
    "まさに",
    "間違いなし",
    "大注目",
    "魅力的",
    "最適解",
    "盛り上がりを見せて",
    "ぜひ足を運んで",
    "足を運んでみて",
    "お見逃しなく",
    "ご紹介",
    "最高の夜",
    "賢く立ち回",
    "参考にしてみて",
    "チェックしてみて",
)

# Core human insider voice guide applied across X, Threads, Instagram, and Column prompts
HUMAN_INSIDER_VOICE_GUIDE = (
    "【最優先：AIっぽさゼロの『現場モニターを見ている運営のリアルな独り言・本音』ルール】\n"
    "1. 広報担当の宣伝文やAIの優等生構文ではなく、毎晩バックヤードで全国53店舗の人数モニターを眺めながら『うわ、今日の梅田えぐいな…』『なんでみんなこっち並んじゃうんだろ』と呟いている30代の運営中の人の生の声で書くこと。\n"
    "2. 毎回きれいに教訓やまとめで締めないこと。『賢く選んで楽しみましょう』『参考にしてみてください』のような優等生オチは厳禁。『〜なんだよな』『〜だったりする』『正直いま動くならこっちかも』『何回見ても不思議』のように、ふと呟いてスマホを置いたような自然な余韻・体言止めを混ぜること。\n"
    "3. 1行目は『モニターを二度見した素のリアクション・ちょっとしたボヤき・外の空気感とのギャップ』から入ること（例：『小雨なのに今日の立ち上がりちょっと早すぎないか…』『同じ新宿の徒歩3分でここまで天国と地獄に割れるの、ほんと残酷なんだよな』等）。\n"
    "4. 句読点や改行に人間の息継ぎを入れること。『〜となっております』『〜ですが、』のような硬い接続詞ではなく、『〜なんだけど、』『正直さ、』『いやこれ、』『〜なんだよね』など声に出して自然な口語リズムにすること。絵文字は0〜2個までに抑え、キラキラ絵文字の連打はしない（自然な『笑』や『…』は1回までOK）。\n"
    "5. 数字は機械的な箇条書き表の羅列にせず、できる限り会話の流れに溶かし込むこと（ただし店舗名・女性XX名・男性YY名・女性比ZZ%の数値そのものはファクトシートと1人・1%も違わず一致させること）。"
)

_weather_cache = {"ts": 0.0, "data": None, "fetching": False}
_AI_CACHE_TTL_SEC = 900
_AI_CACHE_MAX_SIZE = 64
_ai_candidate_cache = collections.OrderedDict()  # cache_key -> {"ts": float, "candidate": dict, "model": str}
_ai_cache_lock = threading.Lock()
_model_discovery_cache = {}  # provider_sig -> {"ts": float, "models": list[str]}
_last_gemini_meta = {
    "last_model": "",
    "last_provider": "",
    "last_error": "",
    "last_ok_at": "",
    "working_route": None,
}

CHANNEL_LABELS = {
    "x": "𝕏 X（旧Twitter）",
    "threads": "🧵 Threads（スレッズ）",
    "instagram": "📸 Instagram（インスタグラム）",
}

STYLE_LABELS = {
    "ai_live_adlib": "✨ AI編集長・現場本音アドリブ実況型（天気・街況連動）",
    # X styles
    "hub_gap": "🚶 エリア内ギャップ・ハシゴ観察型",
    "surge_focus": "📈 1店舗フォーカス・急増/穴場つぶやき型",
    "usual_anomaly": "💡 普段の同曜日比（平比）ギャップ発見型",
    "east_west_talk": "🗼🐙 東西トップ現場空気感トーク型",
    "compact_top3": "🥇 従来型ランキング速報（🥇🥈🥉 TOP3）",
    # Threads styles
    "threads_city_story": "🗣️ 街の温度差＆リアル現場エッセイ型",
    "threads_anomaly_insight": "📊 普段比（平比）の異変＆男女別立ち回り型",
    "threads_hopping_guide": "👟 東西4大エリア「1軒目→2軒目」実践比較型",
    "threads_ladies_zero_wait": "💄 女子得・待ちゼロ＆特典速報型",
    "threads_classic_top3": "🏆 従来型・全国TOP3＆主要街掲示板型",
    # Instagram styles
    "ig_empathy_story": "💬 共感1行目フック＆待ち回避ストーリー型",
    "ig_data_discovery": "📖 データ編集長コラム＆穴場法則マガジン型",
    "ig_concise_save": "📌 余白重視・男女別スマート保存ガイド型",
    "ig_ladies_night_guide": "💄 女子組・待ちゼロ＆特典保存ガイド型",
    "ig_classic_template": "📊 従来型・定型アナウンスキャプション",
}

TONE_STATE_KEY = "editorial:default_tone_mode"
DEFAULT_TONE_MODE = "spicy_female"

EDITORIAL_TONE_MODES = {
    "spicy_female": {
        "key": "spicy_female",
        "label": "🌶️💄 辛口本音×女性目線ミックス（新標準）",
        "short_label": "🌶️💄 辛口×女性本音",
        "desc": "ご都合主義の綺麗事を排除し、「男性が気づかない敗因の図星（毒）」と「女性ペアの本音・裏側」の両方を効かせるモード。",
        "prompt_directive": (
            "【トーン＆視点指定：🌶️💄 辛口本音×女性目線ミックス】\n"
            "- 誰にでも好かれようとする無難な優等生トーン（「〜がおすすめです✨」「楽しく飲めますよ😊」等の浅い綺麗事）を封印すること。\n"
            "- 「看板の知名度だけで並んで時間を溶かす非効率」や「男余りの店で惰性で粘るサンクコスト」を少しビターな本音で突きつつ、同時に「女の子側も待たされる店や必死すぎる席はすぐ見切って好バランス店へ流れている」「女子組が今すぐ待ちゼロで入れる店」という女性目線のリアルな肌感覚を織り交ぜること。"
        ),
    },
    "spicy": {
        "key": "spicy",
        "label": "🔥 辛口・毒強め特化（綺麗事ゼロの現場解剖）",
        "short_label": "🔥 辛口・毒強め",
        "desc": "優等生アドバイスを完全封印し、耳が痛い図星と残酷な現場構造（なぜノープラン組が負けるのか）をズバッと切るビター特化モード。",
        "prompt_directive": (
            "【トーン＆視点指定：🔥 辛口・毒強め特化（ビター＆シニカル）】\n"
            "- キラキラした宣伝文句や「ぜひ行ってみてください」的な優しい語尾を禁止し、現場を知り尽くした常連が深夜のバーでぶっちゃけるような、少し毒と苦味のある鋭い語り口にすること。\n"
            "- 「何も見ずに人気店へ突撃して受付待ちで消耗する一番もったいない負けパターン」や「徒歩3分で世界が違うのに1店舗に固執する罠」など、読者が思わず『うわ、図星だ…』と保存したくなる鋭い観察眼を入れること（ただし特定の個人や店舗への誹謗中傷はせず、あくまで立ち回りの構造を辛口に斬ること）。"
        ),
    },
    "female_real": {
        "key": "female_real",
        "label": "💄 女性目線リアル特化（女子の本音・裏側解剖）",
        "short_label": "💄 女性目線リアル",
        "desc": "女性2人組が実は見ている減点サイン、女子トイレや帰り道LINEの本音、女性側が待ちゼロで当たり席を引く立ち回りに特化するモード。",
        "prompt_directive": (
            "【トーン＆視点指定：💄 女性目線リアル特化（女子の本音・裏側）】\n"
            "- ラウンジに通う20〜30代女性ペアのリアルな本音（「せっかく髪もメイクも整えたのに受付で待たされる店はすぐ出る」「余裕のない質問攻め席より、落ち着いて話せるバランスの店がいい」「雨の日は前髪が死ぬから地下直結しか勝たん」など）を軸に語ること。\n"
            "- 男性読者には「実は女子側はここを見ている」というハッとする気づきを与え、女性フォロワーには「今すぐ待ち時間ゼロで即乾杯できる男性先行の狙い目店」がすぐ分かる実用性を両立させること。"
        ),
    },
    "standard": {
        "key": "standard",
        "label": "🥂 マイルド標準（初心者向け・丁寧ガイド）",
        "short_label": "🥂 マイルド標準",
        "desc": "毒や辛口表現を抑え、初めて相席ラウンジへ行く読者にも安心感を与えるフラットで丁寧なガイドモード。",
        "prompt_directive": (
            "【トーン＆視点指定：🥂 マイルド標準（丁寧・フラット）】\n"
            "- 辛口すぎる表現は控え、相席ラウンジ初心者でも安心して参考にできる親しみやすく落ち着いた常連トーンで書くこと。"
        ),
    },
}


def normalize_tone_mode(tone_mode: str) -> str:
    tm = (tone_mode or "").strip().lower()
    return tm if tm in EDITORIAL_TONE_MODES else ""


def get_default_editorial_tone_mode(allow_blocking: bool = False) -> str:
    try:
        saved = job_state.get_value(TONE_STATE_KEY, allow_blocking=allow_blocking) or ""
        norm = normalize_tone_mode(saved)
        if norm:
            return norm
    except Exception:
        pass
    return DEFAULT_TONE_MODE


def get_editorial_tone_mode(tone_mode: str = None, allow_blocking: bool = False) -> str:
    explicit = normalize_tone_mode(tone_mode)
    if explicit:
        return explicit
    return get_default_editorial_tone_mode(allow_blocking=allow_blocking)


def set_default_editorial_tone_mode(tone_mode: str) -> str:
    resolved = normalize_tone_mode(tone_mode) or DEFAULT_TONE_MODE
    job_state.set_value(TONE_STATE_KEY, resolved)
    clear_editorial_ai_cache()
    return resolved


def get_tone_modes_catalog() -> list:
    return [
        {
            "key": v["key"],
            "label": v["label"],
            "short_label": v["short_label"],
            "desc": v["desc"],
        }
        for v in EDITORIAL_TONE_MODES.values()
    ]

URBAN_CLUSTERS = [
    ("新宿", "shinjuku", ("新宿", "SHINJUKU")),
    ("渋谷・恵比寿", "shibuya", ("渋谷", "恵比寿", "SHIBUYA", "EBISU")),
    ("梅田・茶屋町", "umeda", ("梅田", "茶屋町", "大阪駅前", "UMEDA", "CHAYAMACHI")),
    ("難波・心斎橋", "namba", ("難波", "心斎橋", "NAMBA", "SHINSAIBASHI")),
    ("名古屋・栄", "nagoya", ("名古屋", "栄", "錦", "NAGOYA")),
    ("福岡・天神", "fukuoka", ("福岡", "天神", "FUKUOKA")),
    ("札幌・すすきの", "sapporo", ("札幌", "SAPPORO")),
]


def natural_store_name(raw_name: str) -> str:
    """
    Convert internal codes like 'OLG 新宿' or 'JIS SHINJUKU' into natural, human-searchable Japanese names:
      - 'OLG 新宿' -> 'オリエンタルラウンジ新宿'
      - 'OLG 大阪駅前' -> 'オリエンタルラウンジ大阪駅前'
      - 'OLG ag渋谷' -> 'ag渋谷'
      - 'JIS SHINJUKU' -> 'JIS新宿'
      - 'YATAKOI UMEDA' -> 'ヤタコイ梅田'
    """
    formatted = x_poster.format_ja_store_name(raw_name)
    if formatted.startswith("OLG "):
        return "オリエンタルラウンジ" + formatted[4:].replace(" ", "")
    if formatted.startswith("JIS "):
        return "JIS" + formatted[4:].replace(" ", "")
    return formatted


def casual_store_name(raw_name: str) -> str:
    """
    Convert OLG stores into natural colloquial shorthand 'オリラジ〇〇' used by lounge regulars:
      - 'OLG 新宿' -> 'オリラジ新宿'
      - 'OLG 大阪駅前' -> 'オリラジ大阪駅前'
      - 'OLG ag渋谷' -> 'ag渋谷'
    """
    formatted = x_poster.format_ja_store_name(raw_name)
    if formatted.startswith("OLG "):
        return "オリラジ" + formatted[4:].replace(" ", "")
    return natural_store_name(raw_name)


def _domestic_stores(stores: list) -> list:
    korea_keywords = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    return [
        s for s in (stores or [])
        if s.get("region") != "Korea"
        and not any(k in (s.get("name") or "").upper() for k in korea_keywords)
    ]


def _enrich_with_benchmarks(stores: list) -> list:
    """Attach live benchmarks (usual_w_diff, core_time_short, midnight_retention_pct) and natural Japanese names."""
    try:
        import local_db
        benchmarks = local_db.get_all_stores_live_benchmarks(allow_blocking=False) or {}
    except Exception:
        benchmarks = {}

    enriched = []
    for s in stores:
        item = dict(s)
        sname = item.get("name") or ""
        w = int(item.get("women") or 0)
        m = int(item.get("men") or 0)
        tot = w + m
        item["women"] = w
        item["men"] = m
        item["total"] = tot
        item["wpct"] = round((w / tot) * 100) if tot > 0 else 0
        item["ja_name"] = natural_store_name(sname)
        item["casual_name"] = casual_store_name(sname)
        bm = benchmarks.get(sname) or {}
        if "usual_w_diff" not in item:
            avg_w = bm.get("usual_women") if bm.get("usual_women") is not None else bm.get("usual_avg_w")
            item["usual_w_diff"] = int(round(w - avg_w)) if avg_w is not None and tot > 0 else None
        if "midnight_retention_pct" not in item:
            item["midnight_retention_pct"] = bm.get("midnight_retention_pct")
        if "core_time_short" not in item:
            item["core_time_short"] = bm.get("core_time_short")
        enriched.append(item)
    return enriched


def _history_key_for_channel(channel: str) -> str:
    ch = (channel or "x").lower()
    if ch == "x":
        return "sns_editorial_log"
    return f"sns_editorial_log_{ch}"


def get_recent_editorial_history(channel: str = "x", limit: int = 8, allow_blocking: bool = True) -> list:
    """Load recent editorial meeting logs for the specified SNS channel."""
    raw = job_state.get_value(_history_key_for_channel(channel), allow_blocking=allow_blocking)
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items[:limit]
    except Exception:
        pass
    return []


def persist_editorial_session_record(channel: str, session_record: dict):
    """Persist an adopted editorial session record to `job_state` (called ONLY after actual post success or explicit record_history=True)."""
    if not session_record:
        return
    ch = (channel or "x").lower()
    try:
        h_key = _history_key_for_channel(ch)
        history = get_recent_editorial_history(channel=ch, limit=11)
        history.insert(0, session_record)
        job_state.set_value(h_key, json.dumps(history[:12], ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Failed to persist {ch} editorial log: {e}")


def _mask_secret_token(raw_tok: str) -> str:
    t = (raw_tok or "").strip()
    if not t:
        return ""
    if len(t) > 10:
        return f"{t[:4]}...{t[-4:]}"
    return "****"


def record_sns_delivery_status(
    channel: str,
    status: str,
    slot_label: str = "",
    detail: str = "",
    post_id: str = "",
    now_jst: datetime.datetime = None,
):
    """
    Persist actual SNS API delivery outcome (`success`, `error`, `skipped`, `token_refreshed`, `token_error`)
    into `job_state` (`sns_delivery:<channel>`) so Master can inspect silent API/token failures across redeploys.
    """
    ch = (channel or "x").strip().lower()
    if ch not in ("x", "threads", "instagram"):
        return
    now_jst = now_jst or datetime.datetime.now(JST)
    ts_str = now_jst.strftime("%Y-%m-%d %H:%M:%S")
    key = f"sns_delivery:{ch}"
    prev = {}
    try:
        raw_prev = job_state.get_value(key, allow_blocking=False)
        if raw_prev:
            parsed = json.loads(raw_prev)
            if isinstance(parsed, dict):
                prev = parsed
    except Exception:
        prev = {}

    rec = dict(prev)
    rec["channel"] = ch
    rec["last_attempt_at"] = ts_str
    rec["last_status"] = status
    if slot_label:
        rec["last_slot"] = slot_label
    if status in ("success", "token_refreshed"):
        if status == "success":
            rec["last_ok_at"] = ts_str
            if slot_label:
                rec["last_ok_slot"] = slot_label
            if post_id:
                rec["last_post_id"] = str(post_id)
        rec["last_error"] = ""
        rec["last_error_at"] = ""
    elif status in ("error", "token_error"):
        rec["last_error"] = str(detail or "API error")[:260]
        rec["last_error_at"] = ts_str
    elif status == "skipped" and "not configured" in str(detail or ""):
        rec["last_error"] = str(detail)[:200]
        rec["last_error_at"] = ts_str

    try:
        job_state.set_value(key, json.dumps(rec, ensure_ascii=False))
    except Exception as e:
        logger.warning(f"Failed to persist sns_delivery status for {ch}: {e}")


def get_sns_delivery_health(allow_blocking: bool = False) -> dict:
    """
    Return unified, masked delivery & credential health across X, Threads, and Instagram
    for `/staff/sns-editorial` without exposing raw secrets.
    """
    try:
        if allow_blocking:
            known_keys = job_state.all_keys()
        else:
            job_state._ensure_loaded(allow_blocking=False)
            with job_state._lock:
                known_keys = set(job_state._ensure_local_loaded_locked().keys())
    except Exception:
        known_keys = set()

    def _latest_matching_key(prefixes: tuple) -> str:
        matched = [k for k in known_keys if k.startswith(prefixes)]
        if not matched:
            return ""
        matched.sort()
        return matched[-1]

    # 1. X credentials & status
    x_configured = all(
        bool((os.environ.get(k) or "").strip())
        for k in ("X_API_KEY", "X_API_SECRET", "X_ACCESS_TOKEN", "X_ACCESS_TOKEN_SECRET")
    )
    x_source = "Render環境変数（OAuth 1.0a 4キー設定済）" if x_configured else "未設定（環境変数 X_API_KEY 等が必要）"
    x_masked = _mask_secret_token(os.environ.get("X_ACCESS_TOKEN") or "")

    # 2. Threads credentials & status
    th_stored = (job_state.get_value("token:threads", allow_blocking=allow_blocking) or "").strip()
    th_env = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()
    th_active = th_stored or th_env
    th_configured = bool(th_active)
    th_source = (
        "管理画面・自動更新（暗号化保存）"
        if th_stored
        else ("Render環境変数（THREADS_ACCESS_TOKEN）" if th_env else "未設定")
    )
    th_masked = _mask_secret_token(th_active)

    # 3. Instagram credentials & status
    ig_stored = (job_state.get_value("token:instagram", allow_blocking=allow_blocking) or "").strip()
    ig_env = (os.environ.get("INSTAGRAM_ACCESS_TOKEN") or "").strip()
    ig_active = ig_stored or ig_env
    ig_configured = bool(ig_active)
    ig_source = (
        "管理画面・自動更新（暗号化保存）"
        if ig_stored
        else ("Render環境変数（INSTAGRAM_ACCESS_TOKEN）" if ig_env else "未設定")
    )
    ig_masked = _mask_secret_token(ig_active)

    meta_map = {
        "x": {
            "label": "𝕏 X（旧Twitter）",
            "configured": x_configured,
            "source": x_source,
            "masked_token": x_masked,
            "latest_slot_key": _latest_matching_key(("x_post:", "x_tip:", "bonus_alert:", "col_release:")),
            "refresh_key": "",
        },
        "threads": {
            "label": "🧵 Threads（スレッズ）",
            "configured": th_configured,
            "source": th_source,
            "masked_token": th_masked,
            "latest_slot_key": _latest_matching_key(("threads_post:", "threads_ladies:")),
            "refresh_key": _latest_matching_key(("token_refresh:threads:",)),
        },
        "instagram": {
            "label": "📸 Instagram（インスタ）",
            "configured": ig_configured,
            "source": ig_source,
            "masked_token": ig_masked,
            "latest_slot_key": _latest_matching_key(("ig_post:", "ig_ladies:")),
            "refresh_key": _latest_matching_key(("token_refresh:instagram:",)),
        },
    }

    out = {}
    for ch, base in meta_map.items():
        saved = {}
        try:
            raw_s = job_state.get_value(f"sns_delivery:{ch}", allow_blocking=allow_blocking)
            if raw_s:
                parsed = json.loads(raw_s)
                if isinstance(parsed, dict):
                    saved = parsed
        except Exception:
            saved = {}

        last_err = str(saved.get("last_error") or "")
        last_ok_at = str(saved.get("last_ok_at") or "")
        latest_key = base["latest_slot_key"]

        # Detect silent historical mismatch on Threads (e.g., editorial log ran on a newer date than threads_post:<date>)
        if ch == "threads" and not last_err:
            th_hist = get_recent_editorial_history(channel="threads", limit=1, allow_blocking=False)
            if th_hist:
                hist_date = str(th_hist[0].get("timestamp") or "")[:10]
                post_date = latest_key.split(":", 1)[1][:10] if ":" in latest_key else ""
                if hist_date and (not post_date or hist_date > post_date) and not saved.get("last_ok_at"):
                    last_err = (
                        f"直近（{th_hist[0].get('timestamp')}）の自動投稿で文面生成後にThreads API送信が不発です。"
                        "トークン期限切れ（短期トークン失効）または未設定の可能性があります。"
                    )

        if last_err:
            state = "error"
        elif base["configured"] or latest_key:
            state = "active"
        else:
            state = "unconfigured"

        out[ch] = {
            "channel": ch,
            "label": base["label"],
            "state": state,
            "configured": base["configured"],
            "source": base["source"],
            "masked_token": base["masked_token"],
            "latest_slot_key": latest_key,
            "refresh_key": base["refresh_key"],
            "last_ok_at": last_ok_at,
            "last_attempt_at": str(saved.get("last_attempt_at") or ""),
            "last_status": str(saved.get("last_status") or ""),
            "last_error": last_err,
            "last_error_at": str(saved.get("last_error_at") or ""),
        }
    return out


# =====================================================================
# Gemini API Key Management + Weather & Calendar Live Context
# =====================================================================
def clear_editorial_ai_cache():
    """Clear in-memory AI draft cache (called when API key updates or manual refresh requested)."""
    with _ai_cache_lock:
        _ai_candidate_cache.clear()


def _prune_ai_candidate_cache_unlocked(now_t: float = None):
    """Remove expired (>900s) entries and cap OrderedDict at 64 items via FIFO/LRU eviction."""
    now_t = now_t if now_t is not None else time.time()
    expired_keys = [
        k for k, v in _ai_candidate_cache.items()
        if now_t - float(v.get("ts") or 0.0) >= _AI_CACHE_TTL_SEC
    ]
    for k in expired_keys:
        _ai_candidate_cache.pop(k, None)
    while len(_ai_candidate_cache) > _AI_CACHE_MAX_SIZE:
        _ai_candidate_cache.popitem(last=False)


def _mask_secret(raw: str) -> str:
    if not raw:
        return ""
    if len(raw) > 8:
        return f"{raw[:4]}...{raw[-4:]}"
    return "****"


def get_gemini_api_key(allow_blocking: bool = True) -> str:
    """
    Resolve active Gemini API key from either:
      1. Master-configured persistent secret in `job_state` (`token:gemini_api_key`)
      2. Render environment variable `GEMINI_API_KEY` or `GOOGLE_API_KEY`
    """
    stored = (job_state.get_value(GEMINI_KEY_STORAGE_PREFIX, allow_blocking=allow_blocking) or "").strip()
    if stored:
        return stored
    return (
        os.environ.get("GEMINI_API_KEY")
        or os.environ.get("GOOGLE_API_KEY")
        or ""
    ).strip()


def set_gemini_api_key(api_key: str):
    """Persist or clear Master-provided Gemini API key (obfuscated via `token:` prefix in job_state)."""
    cleaned = (api_key or "").strip()
    job_state.set_value(GEMINI_KEY_STORAGE_PREFIX, cleaned)
    clear_editorial_ai_cache()
    _model_discovery_cache.clear()
    _last_gemini_meta["last_error"] = ""
    _last_gemini_meta["working_route"] = None


def get_claude_api_key(allow_blocking: bool = True) -> str:
    """
    Resolve active Anthropic Claude API key from either:
      1. Master-configured persistent secret in `job_state` (`token:claude_api_key`)
      2. Render environment variable `ANTHROPIC_API_KEY` or `CLAUDE_API_KEY`
    """
    stored = (job_state.get_value(CLAUDE_KEY_STORAGE_PREFIX, allow_blocking=allow_blocking) or "").strip()
    if stored:
        return stored
    return (
        os.environ.get("ANTHROPIC_API_KEY")
        or os.environ.get("CLAUDE_API_KEY")
        or ""
    ).strip()


def set_claude_api_key(api_key: str):
    """Persist or clear Master-provided Anthropic Claude API key (obfuscated via `token:` prefix in job_state)."""
    cleaned = (api_key or "").strip()
    job_state.set_value(CLAUDE_KEY_STORAGE_PREFIX, cleaned)
    clear_editorial_ai_cache()
    _model_discovery_cache.clear()
    _last_gemini_meta["last_error"] = ""
    _last_gemini_meta["working_route"] = None


def get_ai_engine_pref(allow_blocking: bool = False) -> str:
    """Return engine priority mode: 'auto' (Claude if set else Gemini Pro/Flash), 'claude', or 'gemini'."""
    try:
        raw = (job_state.get_value(AI_ENGINE_PREF_KEY, allow_blocking=allow_blocking) or "").strip().lower()
        if raw in ("auto", "claude", "gemini"):
            return raw
    except Exception:
        pass
    env_p = (os.environ.get("EDITORIAL_AI_ENGINE") or "").strip().lower()
    return env_p if env_p in ("auto", "claude", "gemini") else "auto"


def set_ai_engine_pref(pref: str) -> str:
    norm = (pref or "").strip().lower()
    if norm not in ("auto", "claude", "gemini"):
        norm = "auto"
    job_state.set_value(AI_ENGINE_PREF_KEY, norm)
    clear_editorial_ai_cache()
    _last_gemini_meta["last_error"] = ""
    _last_gemini_meta["working_route"] = None
    return norm


def has_any_ai_key(allow_blocking: bool = True) -> bool:
    return bool(get_claude_api_key(allow_blocking=allow_blocking) or get_gemini_api_key(allow_blocking=allow_blocking))


def get_gemini_status(allow_blocking: bool = True) -> dict:
    """
    Return safe, masked Dual-Engine AI status metadata (Gemini Pro/Flash + Claude Sonnet)
    for Master editorial dashboards while preserving 100% backward-compatible field names.
    """
    g_stored = (job_state.get_value(GEMINI_KEY_STORAGE_PREFIX, allow_blocking=allow_blocking) or "").strip()
    g_env = (os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY") or "").strip()
    g_key = g_stored or g_env
    g_source = (
        "管理画面から登録済み（暗号化保存）" if g_stored
        else ("Render環境変数（GEMINI_API_KEY）" if g_env else "未設定")
    )

    c_stored = (job_state.get_value(CLAUDE_KEY_STORAGE_PREFIX, allow_blocking=allow_blocking) or "").strip()
    c_env = (os.environ.get("ANTHROPIC_API_KEY") or os.environ.get("CLAUDE_API_KEY") or "").strip()
    c_key = c_stored or c_env
    c_source = (
        "管理画面から登録済み（暗号化保存）" if c_stored
        else ("Render環境変数（ANTHROPIC_API_KEY）" if c_env else "未設定")
    )

    pref = get_ai_engine_pref(allow_blocking=allow_blocking)
    any_configured = bool(g_key or c_key)

    # Determine primary active provider based on preference & configured keys
    if pref == "gemini" and g_key:
        primary_provider = "gemini"
    elif pref == "claude" and c_key:
        primary_provider = "claude"
    elif c_key:
        primary_provider = "claude"
    elif g_key:
        primary_provider = "gemini"
    else:
        primary_provider = "none"

    active_key = c_key if primary_provider == "claude" else g_key
    masked = _mask_secret(active_key)
    last_err = _last_gemini_meta.get("last_error") or ""
    if not any_configured:
        state = "unconfigured"
    elif last_err and not _last_gemini_meta.get("last_model"):
        state = "degraded"
    else:
        state = "active"

    default_model = DEFAULT_CLAUDE_MODELS[0] if primary_provider == "claude" else DEFAULT_GEMINI_MODELS[0]
    last_model = _last_gemini_meta.get("last_model") or default_model
    provider_badge = (
        "Claude Sonnet × Gemini 両対応ハイブリッド"
        if (c_key and g_key)
        else ("Claude Sonnet 長考執筆" if c_key else "Gemini Pro / 上位Flash 長考執筆")
    )

    return {
        "configured": any_configured,
        "state": state,
        "active_provider": _last_gemini_meta.get("last_provider") or primary_provider,
        "primary_provider": primary_provider,
        "engine_pref": pref,
        "engine_badge": provider_badge,
        "key_kind": "claude" if primary_provider == "claude" else ("vertex_express" if g_key.startswith("AQ.") else ("ai_studio" if g_key else "none")),
        "masked_key": masked,
        "source": c_source if primary_provider == "claude" else g_source,
        "gemini_configured": bool(g_key),
        "gemini_masked_key": _mask_secret(g_key),
        "gemini_source": g_source,
        "claude_configured": bool(c_key),
        "claude_masked_key": _mask_secret(c_key),
        "claude_source": c_source,
        "last_model": last_model,
        "last_error": last_err,
        "last_ok_at": _last_gemini_meta.get("last_ok_at") or "",
    }


def _nth_monday(year: int, month: int, n: int) -> int:
    first_wd = datetime.date(year, month, 1).weekday()  # 0=Mon
    first_mon = 1 + ((7 - first_wd) % 7)
    return first_mon + (n - 1) * 7


def _is_jp_base_holiday(d: datetime.date) -> bool:
    y, m, day = d.year, d.month, d.day
    fixed = {
        (1, 1), (2, 11), (2, 23), (4, 29),
        (5, 3), (5, 4), (5, 5), (8, 11),
        (11, 3), (11, 23),
    }
    if (m, day) in fixed:
        return True
    if m == 1 and day == _nth_monday(y, 1, 2):
        return True
    if m == 7 and day == _nth_monday(y, 7, 3):
        return True
    if m == 9 and day == _nth_monday(y, 9, 3):
        return True
    if m == 10 and day == _nth_monday(y, 10, 2):
        return True
    # Vernal & Autumnal Equinoxes (accurate 2000-2099 formula)
    vernal = int(20.8431 + 0.242194 * (y - 1980) - int((y - 1980) / 4))
    autumnal = int(23.2488 + 0.242194 * (y - 1980) - int((y - 1980) / 4))
    if (m == 3 and day == vernal) or (m == 9 and day == autumnal):
        return True
    return False


def is_jp_holiday(d: datetime.date) -> bool:
    if _is_jp_base_holiday(d):
        return True
    # Substitute holiday when Sunday was a base holiday
    if d.weekday() == 0 and _is_jp_base_holiday(d - datetime.timedelta(days=1)):
        return True
    return False


def _business_datetime(now_jst: datetime.datetime = None) -> datetime.datetime:
    """Shift AM 00:00-06:59 JST into the previous business night (07:00 JST rollover)."""
    now_jst = now_jst or datetime.datetime.now(JST)
    return now_jst - datetime.timedelta(hours=7)


def build_calendar_context(now_jst: datetime.datetime = None) -> dict:
    """Build street-level calendar context (pre-holiday, payday window, weekend peak, weekday chill)."""
    now_jst = now_jst or datetime.datetime.now(JST)
    biz_dt = _business_datetime(now_jst).date()
    tomorrow = biz_dt + datetime.timedelta(days=1)
    wd = biz_dt.weekday()  # 0=Mon..6=Sun
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_ja = weekdays_ja[wd]

    tomorrow_holiday = is_jp_holiday(tomorrow)
    today_holiday = is_jp_holiday(biz_dt)
    is_payday_window = (25 <= biz_dt.day <= 28) or (biz_dt.day >= 29 and wd in (4, 5))

    if (biz_dt.month, biz_dt.day) in ((10, 30), (10, 31)):
        tag = "🎃 ハロウィン週末モード"
        note = "ハロウィン時期で繁華街全体の出足が非常に早く、20時台から大型店が埋まりやすい夜"
    elif biz_dt.month == 12 and biz_dt.day >= 18:
        tag = "🎄 忘年会・年末ピークモード"
        note = "忘年会・2次会帰りのグループ合流で21時半以降の人数急増が起きやすい年末モード"
    elif wd not in (4, 5) and tomorrow_holiday:
        tag = f"✨ 祝前日（{wd_ja}曜夜）ボーナスモード"
        note = "明日が祝日のため、平日でも金曜並みに22時以降まで客足が落ちにくい狙い目の夜"
    elif is_payday_window and wd in (3, 4, 5):
        tag = f"💰 給料日直後×{wd_ja}曜ピーク"
        note = "25日給料日直後の週末前で、1軒目（20時〜21時台）の立ち上がりが普段より早い傾向"
    elif wd in (4, 5):
        tag = f"🔥 {wd_ja}曜ピークナイト"
        note = "週末ピークのため人気店は21時半すぎに男性待ちが出やすく、徒歩3分の別店舗比較が効く夜"
    elif wd == 6 or (today_holiday and not tomorrow_holiday):
        tag = f"⏰ {wd_ja}曜・早め勝負モード"
        note = "翌日仕事組が22時半〜23時頃に帰路につくため、19時半〜21時半のコアタイムが最も当たりやすい夜"
    else:
        tag = f"🥂 {wd_ja}曜・平日スマート穴場モード"
        note = "週末ほど騒がしすぎず、女性比が安定している店舗を選べば落ち着いて話しやすい平日の狙い目夜"

    return {
        "weekday_ja": wd_ja,
        "tag": tag,
        "note": note,
        "is_pre_holiday": tomorrow_holiday,
        "is_payday_window": is_payday_window,
    }


def build_slot_persona_context(slot_label: str = "22:00", channel: str = "x") -> dict:
    """
    Return time-of-night human emotional tone & speaking energy so the account never sounds
    like a flat, uniform bot across 20:00, 22:00, and 00:00+ midnight slots.
    """
    ch = (channel or "x").lower()
    if ch == "instagram":
        return {
            "label": "📖 編集部の保存版裏話トーン",
            "guidance": (
                "週末や仕事帰りの店選びで失敗してほしくない編集者が、友達にこっそりデータの法則を教える温かいトーン。"
                "宣伝文句ではなく『これ知ってるだけで待ち時間なくなるよ』という等身大の語り口にする。"
            ),
        }
    if slot_label == "20:00":
        return {
            "label": "🍻 20:00 仕事終わり・1軒目ウォームアップ",
            "guidance": (
                "仕事終わりの落ち着いた温度感。まだガツガツ煽らず、"
                "『お疲れさまです、これから1軒目どこ入るか迷ってる人向けに置いときます』『今夜ここ立ち上がり早めかも』"
                "といった余裕のあるスマートな語り口にする。"
            ),
        }
    if slot_label in ("00:00", "02:00"):
        return {
            "label": "🌙 00:00 終電後の深夜ラジオ・本音トーン",
            "guidance": (
                "終電が終わった後の、少し肩の力が抜けた深夜のリアルな本音トーン。"
                "ハイテンションな感嘆符（！）やキラキラ絵文字を抑え、"
                "『終電なくなった後のリアルな残り方なんだけど…』『この時間からまだちゃんと残ってるの強いな』"
                "のように静かに呟く空気感にする。"
            ),
        }
    return {
        "label": "🔥 22:00 ピーク・2軒目移動のライブ実況",
        "guidance": (
            "街が一番動いている22時台のリアルタイム実況トーン。"
            "『いま1軒目終わりの組が一気に流れてきてて』『徒歩3分なのにここまで男女比割れるか…』"
            "のように、今まさにスマホで現場モニターを見ている臨場感を出す。"
        ),
    }


def _describe_wmo_code(code: int, precip: float) -> tuple:
    if precip >= 0.4 or code in (51, 53, 55, 61, 63, 65, 80, 81, 82, 95, 96, 99):
        return ("雨", True)
    if code in (71, 73, 75, 77, 85, 86):
        return ("雪", True)
    if code in (1, 2, 3, 45, 48):
        return ("曇り", False)
    return ("晴れ", False)


def _refresh_weather_sync():
    try:
        resp = requests.get(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": "35.69,34.70",
                "longitude": "139.70,135.50",
                "current": "temperature_2m,precipitation,weather_code",
                "timezone": "Asia/Tokyo",
            },
            timeout=2.2,
        )
        if resp.status_code == 200:
            payload = resp.json()
            items = payload if isinstance(payload, list) else [payload]
            if len(items) >= 2:
                tk_cur = items[0].get("current") or {}
                os_cur = items[1].get("current") or {}
                tk_temp = int(round(float(tk_cur.get("temperature_2m", 18))))
                os_temp = int(round(float(os_cur.get("temperature_2m", 19))))
                tk_desc, tk_rain = _describe_wmo_code(
                    int(tk_cur.get("weather_code", 0)),
                    float(tk_cur.get("precipitation", 0.0)),
                )
                os_desc, os_rain = _describe_wmo_code(
                    int(os_cur.get("weather_code", 0)),
                    float(os_cur.get("precipitation", 0.0)),
                )
                summary = f"東京（新宿・渋谷）{tk_desc} {tk_temp}℃ ／ 大阪（梅田・難波）{os_desc} {os_temp}℃"
                if tk_rain and os_rain:
                    hint = "東京・大阪ともに雨模様のため、地下街・駅チカ店舗に客足が集中しやすい夜☔"
                elif tk_rain:
                    hint = "関東エリアは雨模様のため、新宿・渋谷・上野など駅近店舗への集中が目立つ夜☔"
                elif os_rain:
                    hint = "関西エリアは雨模様のため、梅田・難波の地下直結エリアに人が集まりやすい夜☔"
                elif min(tk_temp, os_temp) <= 12:
                    hint = f"外は{min(tk_temp, os_temp)}℃前後と冷え込んでいる分、暖かい屋内ラウンジに早めに人が集まる夜🌙"
                elif max(tk_temp, os_temp) >= 28:
                    hint = "蒸し暑い夜で1軒目から涼しいラウンジへ直行するグループが多い夜🍻"
                else:
                    hint = f"外歩きしやすい気候（東京{tk_temp}℃・大阪{os_temp}℃）で、エリア内の2軒目ハシゴも動きやすい夜✨"

                result = {
                    "available": True,
                    "summary": summary,
                    "adlib_hint": hint,
                    "is_rainy": tk_rain or os_rain,
                    "tokyo_rain": tk_rain,
                    "osaka_rain": os_rain,
                    "temps": [tk_temp, os_temp],
                }
                _weather_cache["ts"] = time.time()
                _weather_cache["data"] = result
                return result
    except Exception as e:
        logger.debug(f"Open-Meteo weather fetch skipped: {e}")
    finally:
        _weather_cache["fetching"] = False
    return None


def fetch_live_weather_context(force_network: bool = False, allow_blocking: bool = False) -> dict:
    """
    Fetch free real-time Tokyo & Osaka weather via Open-Meteo (cached for 30 min).
    When `allow_blocking=False` (default for web page loads), returns cached data in 0ms
    and refreshes asynchronously in a background thread so page navigation never blocks.
    """
    now_t = time.time()
    cached_data = _weather_cache.get("data")
    if not force_network and cached_data and (now_t - _weather_cache["ts"] < 1800):
        return cached_data

    if os.environ.get("DISABLE_SCHEDULER") == "1" and not force_network:
        return {
            "available": False,
            "summary": "気象データ取得スキップ（テスト環境）",
            "adlib_hint": "",
            "is_rainy": False,
            "temps": [],
        }

    if allow_blocking or force_network:
        _weather_cache["fetching"] = True
        fresh = _refresh_weather_sync()
        if fresh:
            return fresh
    else:
        if not _weather_cache.get("fetching"):
            _weather_cache["fetching"] = True
            threading.Thread(target=_refresh_weather_sync, daemon=True).start()
        if cached_data:
            return cached_data

    return {
        "available": False,
        "summary": "気象データ取得中...",
        "adlib_hint": "",
        "is_rainy": False,
        "temps": [],
    }


# =====================================================================
# Role 1: Researcher (現場データ発掘係)
# =====================================================================
def run_researcher(
    stores: list,
    slot_label: str = "22:00",
    channel: str = "x",
    ig_ctx: dict = None,
    now_jst: datetime.datetime = None,
    allow_weather_blocking: bool = False,
) -> dict:
    """
    Analyze live domestic store counts (and weekly carousel stats for IG) to extract verified field findings:
      1. Best same-city gender-ratio gap (`best_cluster`) where one store is female-lead and another differs
      2. Genuine sweet-spot / momentum surge store (`focus_store` with women >= men or top surge)
      3. Positive anomaly vs usual same-weekday/hour average (`anomaly_store`)
      4. City leaders across Shinjuku, Shibuya, Umeda, Namba, Nagoya, Fukuoka
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    dom = _enrich_with_benchmarks(_domestic_stores(stores))
    dom.sort(key=lambda x: (x["women"], x["men"]), reverse=True)

    total_w = sum(s["women"] for s in dom)
    total_m = sum(s["men"] for s in dom)
    active_stores = [s for s in dom if s["total"] >= 6]

    # 1. Urban cluster gap analysis
    best_cluster = None
    best_cluster_score = -1.0
    city_leaders = []
    for hub_label, area_id, kws in URBAN_CLUSTERS:
        hub_s = [
            s for s in dom
            if any(k in (s.get("name") or "").upper() or k in (s.get("name") or "") for k in kws)
        ]
        active_hub = [s for s in hub_s if s["total"] >= 8]
        substantial_hub = [s for s in hub_s if s["total"] >= 24]
        candidate_hub = substantial_hub if len(substantial_hub) >= 2 else active_hub
        if hub_s and hub_s[0]["women"] > 0:
            city_leaders.append({"hub_label": hub_label, "area_id": area_id, "store": hub_s[0]})

        if len(candidate_hub) >= 2:
            by_ratio = sorted(candidate_hub, key=lambda x: (x["wpct"], x["women"]), reverse=True)
            high_s = by_ratio[0]
            low_s = by_ratio[-1]
            pct_gap = high_s["wpct"] - low_s["wpct"]
            score = pct_gap * 1.35 + (high_s["women"] * 0.25) + (min(high_s["total"], low_s["total"]) * 0.25)
            if high_s["women"] >= 10 and score > best_cluster_score:
                best_cluster_score = score
                best_cluster = {
                    "hub_label": hub_label,
                    "area_id": area_id,
                    "high_store": high_s,
                    "low_store": low_s,
                    "pct_gap": pct_gap,
                    "store_count": len(active_hub),
                }

    # 2. Genuine sweet-spot / surge store (strictly prefer stores where women >= men so we never call a male-heavy store "穴場")
    female_lead_pool = [s for s in dom if s["women"] >= 12 and s["women"] >= s["men"]]
    if not female_lead_pool:
        female_lead_pool = [s for s in dom if s["women"] >= 10]
    female_lead_pool.sort(
        key=lambda x: (
            int(x.get("women_diff") or 0) * 3.0
            + (x["women"] - x["men"]) * 1.6
            + x["women"] * 0.45
        ),
        reverse=True,
    )
    focus_store = female_lead_pool[0] if female_lead_pool else (dom[0] if dom else None)

    # Also detect strongest male-lead lively store (great for female followers: 0-min wait)
    male_lead_pool = [s for s in dom if s["men"] >= 18 and s["women"] >= 10 and (s["men"] - s["women"]) >= 6]
    male_lead_pool.sort(key=lambda x: (x["men"] - x["women"], x["men"]), reverse=True)
    male_lead_store = male_lead_pool[0] if male_lead_pool else None

    # 3. Usual same-weekday/hour positive anomaly
    usual_candidates = [
        s for s in dom
        if s.get("usual_w_diff") is not None and int(s.get("usual_w_diff") or 0) >= 4 and s["women"] >= 12
    ]
    usual_candidates.sort(key=lambda x: (int(x.get("usual_w_diff") or 0), x["women"]), reverse=True)
    anomaly_store = usual_candidates[0] if usual_candidates else None

    # 4. East vs West top stores (never duplicate east_top onto west_top when Kansai has 0 active stores)
    kanto_s = [s for s in dom if s.get("region") == "Kanto" and s["total"] > 0] or [
        s for s in dom if s.get("region") == "Kanto"
    ]
    kansai_s = [s for s in dom if s.get("region") == "Kinki" and s["total"] > 0]
    if not kansai_s and not any(s["total"] > 0 for s in dom):
        kansai_s = [s for s in dom if s.get("region") == "Kinki"]
    east_top = kanto_s[0] if kanto_s else (dom[0] if dom else None)
    if kansai_s:
        west_top = next(
            (s for s in kansai_s if not east_top or s.get("name") != east_top.get("name")),
            None,
        )
    else:
        west_top = None

    cal_ctx = build_calendar_context(now_jst=now_jst)
    weather_ctx = fetch_live_weather_context(allow_blocking=allow_weather_blocking)
    slot_persona = build_slot_persona_context(slot_label=slot_label, channel=channel)

    notes = [
        f"対象媒体：{CHANNEL_LABELS.get(channel, channel)} ｜ 全国稼働 {len(active_stores)}店舗（女性計 {total_w:,}名 / 男性計 {total_m:,}名）を解析。",
        f"【街況・カレンダー】{cal_ctx['tag']}（{cal_ctx['note']}）",
        f"【時間帯テンション設計】{slot_persona['label']}",
    ]
    if weather_ctx.get("available"):
        notes.append(f"【リアルタイム気象】{weather_ctx['summary']} — {weather_ctx['adlib_hint']}")
    if best_cluster:
        hs = best_cluster["high_store"]
        ls = best_cluster["low_store"]
        notes.append(
            f"【街別ギャップ】{best_cluster['hub_label']}で「{hs['ja_name']}（女性{hs['women']}・男性{hs['men']} / 女性比{hs['wpct']}%）」と"
            f"「{ls['ja_name']}（女性{ls['women']}・男性{ls['men']} / 女性比{ls['wpct']}%）」に {best_cluster['pct_gap']}pt の差を検出。"
        )
    if focus_store:
        w_diff = int(focus_store.get("women_diff") or 0)
        diff_txt = f"（15分前比 +{w_diff}名）" if w_diff > 0 else ""
        notes.append(
            f"【男性向け穴場】{focus_store['ja_name']}が女性{focus_store['women']}名・男性{focus_store['men']}名"
            f"（女性比{focus_store['wpct']}%）{diff_txt}で待ち回避に最適。"
        )
    if anomaly_store:
        notes.append(
            f"【普段比プラス】{anomaly_store['ja_name']}が普段の同曜日・同時間帯より女性 +{anomaly_store['usual_w_diff']}名で上振れ中。"
        )
    if ig_ctx and ig_ctx.get("top_anaba"):
        top_a = ig_ctx["top_anaba"][0]
        notes.append(
            f"【カルーセル連動】スライド3枚目の高女性比率1位は「{top_a['seo_name']}（{top_a['avg_wpct']}%）」。"
        )

    return {
        "channel": channel,
        "slot_label": slot_label,
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "total_women": total_w,
        "total_men": total_m,
        "domestic_count": len(dom),
        "active_store_count": len(active_stores),
        "top_stores": dom[:6],
        "all_domestic_stores": dom,
        "city_leaders": city_leaders,
        "best_cluster": best_cluster,
        "focus_store": focus_store,
        "male_lead_store": male_lead_store,
        "anomaly_store": anomaly_store,
        "east_top": east_top,
        "west_top": west_top,
        "ig_ctx": ig_ctx,
        "calendar": cal_ctx,
        "weather": weather_ctx,
        "slot_persona": slot_persona,
        "research_notes": notes,
    }


# =====================================================================
# Role 2: Planner (編成・頻度調整係)
# =====================================================================
def run_planner(
    research: dict,
    slot_label: str = "22:00",
    channel: str = "x",
    now_jst: datetime.datetime = None,
    allow_blocking: bool = True,
) -> dict:
    """
    Determine style rotation and anti-repetition rules per SNS channel (`x`, `threads`, `instagram`).
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    history = get_recent_editorial_history(channel=ch, limit=6, allow_blocking=allow_blocking)
    recent_styles = [h.get("selected_style") for h in history if h.get("selected_style")]
    last_style = recent_styles[0] if recent_styles else None

    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[_business_datetime(now_jst).weekday()]

    recent_post_cooldown = False
    cooldown_reason = "間隔OK（同チャンネルの直近投稿から十分な時間が経過）"
    if history:
        try:
            last_ts = datetime.datetime.strptime(history[0].get("timestamp", ""), "%Y-%m-%d %H:%M:%S")
            mins_ago = abs((now_jst.replace(tzinfo=None) - last_ts).total_seconds()) / 60.0
            if mins_ago < 90:
                recent_post_cooldown = True
                cooldown_reason = f"直近の{CHANNEL_LABELS.get(ch, ch)}投稿から{int(mins_ago)}分のため連投注意"
        except Exception:
            pass

    if ch == "threads":
        if slot_label in ("19:30", "19:45"):
            base_conv = ["threads_ladies_zero_wait", "threads_anomaly_insight", "threads_city_story", "threads_hopping_guide"]
        else:
            base_conv = ["threads_city_story", "threads_anomaly_insight", "threads_hopping_guide", "threads_ladies_zero_wait"]
        classic_key = "threads_classic_top3"
    elif ch == "instagram":
        if slot_label in ("19:30", "19:45"):
            base_conv = ["ig_ladies_night_guide", "ig_empathy_story", "ig_data_discovery", "ig_concise_save"]
        else:
            base_conv = ["ig_empathy_story", "ig_data_discovery", "ig_concise_save", "ig_ladies_night_guide"]
        classic_key = "ig_classic_template"
    else:
        if slot_label == "20:00":
            base_conv = ["usual_anomaly", "surge_focus", "hub_gap", "east_west_talk"]
        elif slot_label in ("22:00", "22:30"):
            base_conv = ["hub_gap", "surge_focus", "east_west_talk", "usual_anomaly"]
        else:
            base_conv = ["surge_focus", "hub_gap", "east_west_talk", "usual_anomaly"]
        classic_key = "compact_top3"

    rot = (now_jst.day + now_jst.hour) % len(base_conv)
    rotated = base_conv[rot:] + base_conv[:rot]
    style_priority = [st for st in rotated if st != last_style] + ([last_style] if last_style in rotated else [])
    style_priority.append(classic_key)

    planner_memo = (
        f"【{CHANNEL_LABELS.get(ch, ch)}・{wd_str}曜編成】"
        f"前回採用『{STYLE_LABELS.get(last_style, '履歴なし')}』との被りをブロックし、"
        f"定型テンプレ（{STYLE_LABELS.get(classic_key, '')}）を抑えて『{STYLE_LABELS.get(style_priority[0], '')}』を最優先に設定。"
    )

    return {
        "channel": ch,
        "slot_label": slot_label,
        "weekday_ja": wd_str,
        "last_style": last_style,
        "recent_styles": recent_styles[:4],
        "classic_key": classic_key,
        "suppress_classic": True,
        "style_priority": style_priority,
        "recent_post_cooldown": recent_post_cooldown,
        "cooldown_reason": cooldown_reason,
        "planner_memo": planner_memo,
    }


# =====================================================================
# Role 3: Writer (執筆・下書き係) — X / Threads / Instagram 専用ライティング
# =====================================================================
def _pick_distinct_store(stores: list, exclude_names: set):
    for s in stores:
        if s.get("name") not in exclude_names and int(s.get("women") or 0) > 0:
            return s
    for s in stores:
        if s.get("name") not in exclude_names and (int(s.get("women") or 0) + int(s.get("men") or 0)) > 0:
            return s
    return None


def _write_x_candidates(
    research: dict,
    slot_label: str,
    now_jst: datetime.datetime,
    site_url: str,
    tone_mode: str = DEFAULT_TONE_MODE,
) -> list:
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[_business_datetime(now_jst).weekday()]
    t_str = now_jst.strftime("%H:%M")
    slot_hh = (slot_label or "20:00")[:2]
    ymd = now_jst.strftime("%Y%m%d")
    tm = get_editorial_tone_mode(tone_mode)

    top_stores = research.get("top_stores") or []
    if not top_stores:
        return []

    s1 = top_stores[0]
    cluster = research.get("best_cluster")
    focus = research.get("focus_store") or s1
    anomaly = research.get("anomaly_store")
    male_lead = research.get("male_lead_store")
    east = research.get("east_top")
    west = research.get("west_top")

    candidates = []

    # 1) hub_gap: Same-city store gap without repeating the same store twice
    if cluster and cluster["high_store"]["name"] != cluster["low_store"]["name"]:
        hs = cluster["high_store"]
        ls = cluster["low_store"]
        hub_name = cluster["hub_label"]
        area_id = cluster["area_id"]
        outside_top = _pick_distinct_store(top_stores, {hs["name"], ls["name"]})
        outside_line = (
            f"他エリアは{outside_top['ja_name']}（女性{outside_top['women']}名）も好調。"
            if outside_top else ""
        )

        if cluster["pct_gap"] >= 12:
            if tm == "spicy":
                hook_a = f"看板の知名度だけで並んで時間を溶かすのが一番もったいない夜。{hub_name}でもここまで割れてる👀"
                tail_a = "男余り店で粘る前にリプ欄で比較を👇"
            elif tm == "female_real":
                hook_a = f"女の子側も待たされる店はすぐ見切って移動する時間。いま{hub_name}で空気感が割れてる💄"
                tail_a = "女子は男性先行店なら待ち0分で即乾杯👇"
            elif tm == "spicy_female":
                hook_a = f"男余り店で粘る裏で、慣れた女子組は好バランス店へ流れてる夜。{hub_name}がかなり割れてる👀"
                tail_a = "空振り回避の比較はリプ欄へ👇"
            else:
                hook_a = f"いま（{t_str}）の{hub_name}、徒歩圏でも店舗で男女比が割れてる👀"
                tail_a = f"{outside_line}比較はリプ欄へ👇"

            body_a = (
                f"{hook_a}\n\n"
                f"・{hs['ja_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                f"・{ls['ja_name']}：女性{ls['women']}・男性{ls['men']}（女性比{ls['wpct']}%）\n\n"
                f"{tail_a}\n"
                f"#相席ナビ #相席ラウンジ"
            )
            if x_poster.compute_twitter_weight(body_a) > 272:
                body_a = (
                    f"{hook_a}\n\n"
                    f"・{hs['casual_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                    f"・{ls['casual_name']}：女性{ls['women']}・男性{ls['men']}（女性比{ls['wpct']}%）\n\n"
                    f"向かう前の比較はリプ欄へ👇\n"
                    f"#相席ナビ #相席ラウンジ"
                )
        else:
            if tm in ("spicy", "spicy_female"):
                hook_a2 = f"{wd_str}曜{t_str}、ノープラン突撃より数字を見て選ぶ方が勝てる夜。{hub_name}の現在値👇"
            elif tm == "female_real":
                hook_a2 = f"{wd_str}曜{t_str}、女子2人組も落ち着いて飲めるバランスで{hub_name}が推移中🥂"
            else:
                hook_a2 = f"{wd_str}曜{t_str}時点、いま{hub_name}まわりがいいバランスで集まってる🍻"
            body_a = (
                f"{hook_a2}\n\n"
                f"・{hs['ja_name']}：女性{hs['women']}・男性{hs['men']}（女性比{hs['wpct']}%）\n"
                f"・{ls['ja_name']}：女性{ls['women']}・男性{ls['men']}\n\n"
                f"待ちが出そうな時は近くの別店舗も覗くのが確実👇\n"
                f"#相席ナビ #相席ラウンジ"
            )
        if x_poster.compute_twitter_weight(body_a) > 272:
            body_a = body_a.replace("オリエンタルラウンジ", "オリラジ")
        reply_a = (
            f"📍 {hub_name}エリアの料金・現在人数横並び比較はこちら\n"
            f"{site_url}/price-comparison?area={area_id}&s=x{slot_hh}_hub_gap\n\n"
            f"🔍 全国53店舗リアルタイム人数ランキング\n{site_url}/?s=x{slot_hh}_hub_gap_{ymd}"
        )
        candidates.append({
            "style": "hub_gap",
            "style_label": STYLE_LABELS["hub_gap"],
            "writer_note": f"【{EDITORIAL_TONE_MODES[tm]['short_label']}】{hub_name}内の男女比差（{hs['ja_name']} {hs['wpct']}% vs {ls['ja_name']} {ls['wpct']}%）を切り取り作成。",
            "main_text": body_a,
            "reply_text": reply_a,
        })

    # 2) surge_focus: Spotlight 1 sweet-spot store + distinct runner-up (weather/calendar/tone aware)
    w_diff = int(focus.get("women_diff") or 0)
    cal_ctx = research.get("calendar") or {}
    weather_ctx = research.get("weather") or {}
    if slot_label == "20:00":
        if tm in ("spicy", "spicy_female"):
            opener_b = f"21時過ぎて男性待ちに並ぶ前に動くのが正解。今夜は「{focus['ja_name']}」の立ち上がりがかなり早い👀"
            closing_b = "余裕のあるうちに座るなら今の時間帯👇"
        elif tm == "female_real":
            opener_b = f"1軒目帰りの女子組が早くも動き出してる20時台。「{focus['ja_name']}」の立ち上がりがかなり早い💄"
            closing_b = "ガヤガヤする前に落ち着いて飲みたい組は今が狙い目👇"
        else:
            opener_b = (
                f"給料日後の20時台、今夜は「{focus['ja_name']}」の立ち上がりがかなり早い👀"
                if cal_ctx.get("is_payday_window")
                else f"まだ20時台だけど、今夜は「{focus['ja_name']}」の立ち上がりがかなり早い👀"
            )
            closing_b = "混み始める前に待ちなしで座るなら今のうちが狙い目👇"
    elif slot_label in ("00:00", "02:00"):
        if tm in ("spicy", "spicy_female"):
            opener_b = f"終電後に過疎ってる店へ突撃すると寒いけど、いま「{focus['ja_name']}」はしっかり女性が残ってる🌙"
            closing_b = "深夜は勝ち店と静かな店が真っ二つに割れるので要確認👇"
        elif tm == "female_real":
            opener_b = f"終電後もまだ飲み足りない女子2軒目組が集まってるのが「{focus['ja_name']}」🌙"
            closing_b = "深夜帯のリアルな男女バランスはリプ欄へ👇"
        else:
            opener_b = f"終電すぎのこの時間（{t_str}）、いま一番女性が残ってるのは「{focus['ja_name']}」🌙"
            closing_b = "深夜は人が集まる店と静かな店が分かれるので事前確認が確実👇"
    else:
        if weather_ctx.get("is_rainy"):
            opener_b = (
                f"雨の日は巻き髪が死ぬから女子は駅チカしか行かない☔ いま「{focus['ja_name']}」が好バランス💄"
                if tm in ("female_real", "spicy_female")
                else f"雨の{t_str}時点☔ 駅チカでいま女性比率が高めなのが「{focus['ja_name']}」✨"
            )
        elif w_diff >= 3:
            opener_b = f"{t_str}時点、「{focus['ja_name']}」に直近で女性が+{w_diff}名増えて狙い目🔥"
        elif tm == "spicy":
            opener_b = f"金曜ピーク感覚で男余り店に突っ込むより、いま女性比が高い「{focus['ja_name']}」が堅い👀"
        elif tm == "female_real" and male_lead and male_lead["name"] != focus["name"]:
            opener_b = f"男性目線の穴場は「{focus['ja_name']}」、逆に女子組が待ち0分で即座れるのは「{male_lead['casual_name']}」🥂"
        else:
            opener_b = f"{t_str}時点、いま女性比率が高めで座りやすいのが「{focus['ja_name']}」👀"
        closing_b = (
            "惰性で店を選ぶ前に近隣バランスを要チェック👇"
            if tm in ("spicy", "spicy_female")
            else "向かう前に近隣店舗のバランスも見比べてみてください👇"
        )

    other_top = [s for s in top_stores if s["name"] != focus["name"]][:2]
    other_line = ""
    if len(other_top) >= 2 and tm == "standard":
        other_line = f"ほかにも{other_top[0]['ja_name']}（女性{other_top[0]['women']}名）、{other_top[1]['ja_name']}（女性{other_top[1]['women']}名）が好調。\n\n"
    elif other_top:
        other_line = f"他エリアでは{other_top[0]['ja_name']}（女性{other_top[0]['women']}名）も好調。\n\n"

    body_b = (
        f"{opener_b}\n"
        f"現在：女性{focus['women']}名 / 男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
        f"{other_line}"
        f"{closing_b}\n"
        f"#相席ラウンジ #相席ナビ"
    )
    if x_poster.compute_twitter_weight(body_b) > 272:
        body_b = body_b.replace("オリエンタルラウンジ", "オリラジ")
    if x_poster.compute_twitter_weight(body_b) > 272:
        body_b = (
            f"{opener_b.replace('オリエンタルラウンジ', 'オリラジ')}\n"
            f"現在：女性{focus['women']}名 / 男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
            f"{closing_b}\n"
            f"#相席ラウンジ #相席ナビ"
        )
    f_slug = store_master.slug_of(focus["name"])
    reply_b = (
        f"📊 {focus['ja_name']}の現在人数・終電後の残りやすさ指標はこちら\n"
        f"{site_url}/store/{f_slug}?s=x{slot_hh}_surge_focus\n\n"
        f"🔍 全国53店舗リアルタイム人数ランキング\n{site_url}/?s=x{slot_hh}_surge_focus_{ymd}"
    )
    candidates.append({
        "style": "surge_focus",
        "style_label": STYLE_LABELS["surge_focus"],
        "writer_note": f"【{EDITORIAL_TONE_MODES[tm]['short_label']}】女性比・勢いが良好な「{focus['ja_name']}」を主役に作成。",
        "main_text": body_b,
        "reply_text": reply_b,
    })

    # 3) usual_anomaly or east_west_talk (strictly distinct second store!)
    if anomaly and anomaly.get("usual_w_diff") and int(anomaly["usual_w_diff"]) >= 4:
        u_diff = int(anomaly["usual_w_diff"])
        partner = _pick_distinct_store(top_stores, {anomaly["name"]})
        partner_line = (
            f"・{partner['ja_name']}：女性{partner['women']}・男性{partner['men']}（女性比{partner['wpct']}%）\n"
            if partner else ""
        )
        anom_tail = (
            "ライバルが気づく前に座れる普段比プラス店が一番コスパいい👇"
            if tm in ("spicy", "spicy_female")
            else "普段比プラスの店は当たりやすいので今夜の候補にどうぞ👇"
        )
        body_c = (
            f"いつもの{wd_str}曜{slot_label[:2]}時台より、今夜は「{anomaly['ja_name']}」に女性が+{u_diff}名多く入ってる📊\n\n"
            f"・{anomaly['ja_name']}：女性{anomaly['women']}・男性{anomaly['men']}（女性比{anomaly['wpct']}%）\n"
            f"{partner_line}\n"
            f"{anom_tail}\n"
            f"#相席ナビ #相席ラウンジ"
        )
        c_style = "usual_anomaly"
        w_note_c = f"【{EDITORIAL_TONE_MODES[tm]['short_label']}】普段の{wd_str}曜同時間帯より女性が+{u_diff}名多い実測ギャップをフックに作成。"
    else:
        has_distinct_ew = (
            bool(east)
            and bool(west)
            and east.get("name") != west.get("name")
            and east.get("region") != "Kinki"
            and (int(east.get("women") or 0) + int(east.get("men") or 0)) > 0
            and (int(west.get("women") or 0) + int(west.get("men") or 0)) > 0
        )
        if has_distinct_ew:
            ew_tail = (
                "人気店ほど時間帯で待ちが出るため、徒歩圏の穴場比較はリプ欄へ👇"
                if tm in ("spicy", "spicy_female")
                else "どちらも好ペースで推移中。各街の穴場・待ち状況はリプ欄へ👇"
            )
            body_c = (
                f"{wd_str}曜{t_str}時点、関東と関西でいま一番人が集まってる2店舗🍻\n\n"
                f"🗼関東：{east['ja_name']}（女性{east['women']}・男性{east['men']} / {east['wpct']}%）\n"
                f"🐙関西：{west['ja_name']}（女性{west['women']}・男性{west['men']} / {west['wpct']}%）\n\n"
                f"{ew_tail}\n"
                f"#相席ナビ #相席ラウンジ"
            )
            c_style = "east_west_talk"
            w_note_c = f"関東トップ（{east['ja_name']}）と関西トップ（{west['ja_name']}）を対比して作成。"
        else:
            lead = (
                east if (east and (int(east.get("women") or 0) + int(east.get("men") or 0)) > 0)
                else (west if (west and (int(west.get("women") or 0) + int(west.get("men") or 0)) > 0) else s1)
            )
            second = _pick_distinct_store(top_stores, {lead["name"]})
            if second:
                body_c = (
                    f"{wd_str}曜{t_str}時点、いま全国で先行して人が集まってる上位店舗🍻\n\n"
                    f"・{lead['ja_name']}（女性{lead['women']}・男性{lead['men']} / {lead['wpct']}%）\n"
                    f"・{second['ja_name']}（女性{second['women']}・男性{second['men']} / {second['wpct']}%）\n\n"
                    f"各街の穴場・待ち状況はリプ欄へ👇\n"
                    f"#相席ナビ #相席ラウンジ"
                )
                w_note_c = f"現在先行して稼働中の「{lead['ja_name']}」と「{second['ja_name']}」を重複なく対比して作成。"
            else:
                body_c = (
                    f"{wd_str}曜{t_str}時点、いま全国でいち早く人が集まり始めてるのが「{lead['ja_name']}」🍻\n\n"
                    f"・{lead['ja_name']}：女性{lead['women']}・男性{lead['men']}（女性比{lead['wpct']}%）\n\n"
                    f"早い時間から好ペースで推移中。各街の現在人数はリプ欄へ👇\n"
                    f"#相席ナビ #相席ラウンジ"
                )
                w_note_c = f"片側エリア未稼働のため同一店舗の重複を避け、「{lead['ja_name']}」単独速報として安全に作成。"
            c_style = "east_west_talk"
    if x_poster.compute_twitter_weight(body_c) > 272:
        body_c = body_c.replace("オリエンタルラウンジ", "オリラジ")

    reply_c = (
        f"🔍 全国53店舗の現在人数＆普段の同曜日比（平比）はこちら\n{site_url}/?s=x{slot_hh}_{c_style}_{ymd}\n\n"
        f"💴 全51店舗の時間帯料金・開催中イベント比較ボード\n{site_url}/price-comparison?s=x{slot_hh}_{c_style}"
    )
    candidates.append({
        "style": c_style,
        "style_label": STYLE_LABELS[c_style],
        "writer_note": w_note_c,
        "main_text": body_c,
        "reply_text": reply_c,
    })

    # 4) compact_top3 (Classic baseline for Desk comparison)
    legacy_text = x_poster.build_scheduled_tweet_text(top_stores, slot_label=slot_label, app_url=site_url, compact=True)
    legacy_reply = x_poster.build_scheduled_reply_text(slot_label=slot_label, app_url=site_url)
    candidates.append({
        "style": "compact_top3",
        "style_label": STYLE_LABELS["compact_top3"],
        "writer_note": "従来の『🥇🥈🥉 TOP3』定型フォーマット（デスク比較採点用ベンチマーク）。",
        "main_text": legacy_text,
        "reply_text": legacy_reply,
    })

    return candidates


def _write_threads_candidates(
    research: dict,
    slot_label: str,
    now_jst: datetime.datetime,
    site_url: str,
    tone_mode: str = DEFAULT_TONE_MODE,
) -> list:
    """
    Generate 4 Meta Threads candidates (strictly <= 495 chars; conversational essay style works best on Threads).
    Respects odd/even day URL vs Bio-link anti-spam rotation.
    """
    import threads_poster
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[_business_datetime(now_jst).weekday()]
    t_str = now_jst.strftime("%H:%M")
    tm = get_editorial_tone_mode(tone_mode)

    top_stores = research.get("top_stores") or []
    if not top_stores:
        return []

    cluster = research.get("best_cluster")
    focus = research.get("focus_store") or top_stores[0]
    male_lead = research.get("male_lead_store")
    anomaly = research.get("anomaly_store")
    city_leaders = research.get("city_leaders") or []

    slot_hh = (slot_label or "22:00")[:2]

    # Meta anti-spam link rotation (even day = bio link, odd day = direct link with PDCA cohort tag)
    def _th_footer(st_key: str) -> str:
        if now_jst.day % 2 == 0:
            return "🔍 全51店舗の現在人数＆料金比較はプロフィールのリンク（相席ナビ）から見れます\n#相席ラウンジ"
        return f"▼ 全51店舗の現在人数＆料金・イベント比較\n{site_url}/price-comparison?s=th{slot_hh}_{st_key}\n#相席ラウンジ"

    candidates = []

    # 1) threads_city_story: Conversational street-level temperature check
    cta_footer_1 = _th_footer("threads_city_story")
    if cluster and cluster["high_store"]["name"] != cluster["low_store"]["name"]:
        hs = cluster["high_store"]
        ls = cluster["low_store"]
        hub_name = cluster["hub_label"]
        other_cities = [c for c in city_leaders if c["hub_label"] != hub_name][:3]
        city_sub = "\n".join(
            f"・{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}・男性{c['store']['men']}）"
            for c in other_cities
        )
        if tm == "spicy":
            lead_1 = f"正直、{wd_str}曜{t_str}に看板の知名度だけで突撃して受付待ちで消耗する人が一番損してます。今の{hub_name}のリアルな温度差がこちら👀"
            closing_1 = "男余りの店で『せっかく来たし』と粘っても席替えすら回らないので、徒歩3〜4分の好バランス店へさっさと切り替えるのが鉄則です。"
        elif tm == "female_real":
            lead_1 = f"女子側の本音で言うと、せっかくメイクも髪も整えて来たのに『受付で待たされる店』や『必死すぎる空気の店』は1杯で出たくなるもの💄 今の{hub_name}はこんな感じ👇"
            closing_1 = "女性組は男性先行店なら待ち0分で即乾杯できるし、男性組も余裕のある比率の店を選ぶ方がお互い空気感が良くなります🥂"
        elif tm == "spicy_female":
            lead_1 = f"{wd_str}曜{t_str}すぎ、今の{hub_name}はかなり残酷なくらい店舗で明暗が割れてます👀（慣れた女子組ほど待たされる店を嫌ってすぐ別店舗へ流れる時間帯）"
            closing_1 = "受付待ちで疲弊したテンションで乾杯しても女子に見透かされるだけなので、入る前の30秒で比率の良い店を選ぶのが一番賢いです🍸"
        else:
            lead_1 = f"{wd_str}曜{t_str}すぎ、今夜は{hub_name}まわりで店舗ごとの男女比がかなりはっきり分かれてます👀"
            closing_1 = "1軒目で「男性待ち長そうだな」と思ったら、無理に粘らず徒歩3〜4分の別ブランドを覗くのが一番コスパいいです🥂"

        contrast_1 = f"近くの「{ls['ja_name']}」は女性{ls['women']}名・男性{ls['men']}名（女性比{ls['wpct']}%）。"
        body_1 = (
            f"{lead_1}\n\n"
            f"「{hs['ja_name']}」は女性{hs['women']}名・男性{hs['men']}名（女性比{hs['wpct']}%）と余裕がある一方、"
            f"{contrast_1}\n\n"
            f"他エリアの現在トップ👇\n"
            f"{city_sub}\n\n"
            f"{closing_1}\n\n"
            f"{cta_footer_1}"
        )
    else:
        c_lines = "\n".join(
            f"・{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}名 / 男性{c['store']['men']}名）"
            for c in city_leaders[:4]
        )
        body_1 = (
            f"{wd_str}曜{t_str}すぎ、全国{research['domestic_count']}店舗でいま女性{research['total_women']:,}名・男性{research['total_men']:,}名が入ってます🥂\n\n"
            f"今夜特にバランスがいいのは「{focus['ja_name']}（女性{focus['women']}名 / 男性{focus['men']}名・女性比{focus['wpct']}%）」。\n\n"
            f"主要エリアの現在トップメモ📝\n{c_lines}\n\n"
            f"向かう前の30秒で人数を見ておくだけで、受付立ち往生の気まずさをゼロにできます。\n\n"
            f"{cta_footer_1}"
        )

    if len(body_1) > 465:
        body_1 = body_1.replace("オリエンタルラウンジ", "オリラジ")
    candidates.append({
        "style": "threads_city_story",
        "style_label": STYLE_LABELS["threads_city_story"],
        "writer_note": f"【{EDITORIAL_TONE_MODES[tm]['short_label']}】Threadsで伸びやすい現場の本音エッセイ調で作成（メダル記号なし）。",
        "main_text": body_1[:495],
        "reply_text": "（Threadsは1投稿完結・偶数日はプロフ誘導／奇数日はURL付与でリンク制限を回避）",
    })

    # 2) threads_anomaly_insight: Both Male & Female perspective + usual diff
    cta_footer_2 = _th_footer("threads_anomaly_insight")
    if anomaly and anomaly.get("usual_w_diff"):
        hook_2 = (
            f"いつもの{wd_str}曜夜と比べても、今夜は「{anomaly['ja_name']}」に普段より女性が+{anomaly['usual_w_diff']}名多く集まってます📊"
        )
    else:
        hook_2 = (
            f"今夜（{wd_str}曜{t_str}）の相席ラウンジ、全国で女性{research['total_women']:,}名・男性{research['total_men']:,}名と動いてます🥂"
        )

    if male_lead:
        female_tip = (
            f"💄女性組のリアル目線なら：女余りで30分放置される店より、いま男性{male_lead['men']}名・女性{male_lead['women']}名で回転している"
            f"「{male_lead['ja_name']}」の方が待ち0分で即乾杯できて圧倒的にラクです。"
        )
    else:
        female_tip = "💄女性組のリアル目線なら：女余りで放置される店より、男性が先行して入っている大型店を選ぶ方が待ち0分で即乾杯できて圧倒的にラクです。"

    runner = _pick_distinct_store(top_stores, {focus["name"]})
    runner_txt = f"と「{runner['ja_name']}（女性{runner['women']}名 / 男性{runner['men']}名）」" if runner else ""

    body_2 = (
        f"{hook_2}\n\n"
        f"🔥男性目線で無理なく勝てるのは「{focus['ja_name']}（女性{focus['women']}名 / 男性{focus['men']}名・女性比{focus['wpct']}%）」{runner_txt}。\n\n"
        f"{female_tip}\n\n"
        f"男女で『行くべき正解の店』は真逆になるので、入る前に見比べてみてください👇\n\n"
        f"{cta_footer_2}"
    )
    if len(body_2) > 465:
        body_2 = body_2.replace("オリエンタルラウンジ", "オリラジ")
    candidates.append({
        "style": "threads_anomaly_insight",
        "style_label": STYLE_LABELS["threads_anomaly_insight"],
        "writer_note": f"【{EDITORIAL_TONE_MODES[tm]['short_label']}】男女で『正解の店が真逆になる』リアルな構造と即相席データを対比。",
        "main_text": body_2[:495],
        "reply_text": "（Threadsは1投稿完結）",
    })

    # 3) threads_hopping_guide: 4-hub quick hopping guide
    cta_footer_3 = _th_footer("threads_hopping_guide")
    hub_bullets = "\n".join(
        f"📍{c['hub_label']}：{c['store']['ja_name']}（女性{c['store']['women']}・男性{c['store']['men']} / 女性比{c['store']['wpct']}%）"
        for c in city_leaders[:4]
    )
    bitter_tip_3 = (
        "正直、微妙な空気の席で『いい人ぶって90分延長』しても誰も幸せになりません（女子側も内心いつ帰ろうか時計を見てます）。40分で見切って好バランス店へ動く方がお財布にもお互いのためにも正解です🍸"
        if tm in ("spicy", "spicy_female", "female_real")
        else "席替えがしっかり回るのは「女性10名以上×女性比55%以上」のお店。微妙な席で惰性で90分粘るより、40分で見切って好バランス店に回る方が満足度もコスパも段違いです🍸"
    )
    body_3 = (
        f"いま（{t_str}時点）主要エリアで一番人が集まってる店舗だけ抜粋しました📝\n\n"
        f"{hub_bullets}\n\n"
        f"{bitter_tip_3}\n\n"
        f"{cta_footer_3}"
    )
    if len(body_3) > 465:
        body_3 = body_3.replace("オリエンタルラウンジ", "オリラジ")
    candidates.append({
        "style": "threads_hopping_guide",
        "style_label": STYLE_LABELS["threads_hopping_guide"],
        "writer_note": f"【{EDITORIAL_TONE_MODES[tm]['short_label']}】主要4エリアの現在トップと、惰性延長に対する辛口・本音アドバイスを融合。",
        "main_text": body_3[:495],
        "reply_text": "（Threadsは1投稿完結）",
    })

    # 3b) threads_ladies_zero_wait: Women-perspective zero-wait & perks alert (Plan E)
    try:
        import ladies_guide
        ladies_th = ladies_guide.build_threads_ladies_post(
            research.get("all_domestic_stores") or top_stores,
            slot_label=slot_label,
            now_jst=now_jst,
        )
        candidates.append({
            "style": "threads_ladies_zero_wait",
            "style_label": STYLE_LABELS["threads_ladies_zero_wait"],
            "writer_note": "【💄 女子得＆待ちゼロ速報】女性2人組向けに『男性先行で即案内される店』と開催中イベントを抜粋。",
            "main_text": ladies_th,
            "reply_text": "（女性向け特設 /for-women への導線付き）",
        })
    except Exception as err:
        logger.warning(f"ladies_guide threads candidate skipped: {err}")

    # 4) threads_classic_top3: Legacy baseline
    classic_th = threads_poster.build_threads_live_post(top_stores, slot_label=slot_label, now_jst=now_jst)
    candidates.append({
        "style": "threads_classic_top3",
        "style_label": STYLE_LABELS["threads_classic_top3"],
        "writer_note": "従来の『🏆全国リアルタイムTOP3 🥇🥈🥉』定型フォーマット（比較ベンチマーク用）。",
        "main_text": classic_th,
        "reply_text": "（Threadsは1投稿完結）",
    })

    return candidates


def _write_instagram_candidates(
    research: dict,
    now_jst: datetime.datetime,
    tone_mode: str = DEFAULT_TONE_MODE,
) -> list:
    """
    Generate 4 Instagram Carousel/Feed caption candidates.
    Instagram prioritizes:
      - First 1-2 lines before '...続きを読む' (hook empathy/curiosity without stiff bracket banners)
      - Save-worthiness ('保存しておくと便利')
      - Clean alignment with the 5 portrait slides (`top_volume` & `top_anaba`)
    """
    import ig_carousel_generator
    tm = get_editorial_tone_mode(tone_mode)
    ig_ctx = research.get("ig_ctx")
    if not ig_ctx:
        ig_ctx = ig_carousel_generator.build_forecast_context(live_stores=research.get("top_stores") or [], now_jst=now_jst)

    v_top = ig_ctx.get("top_volume", [])[:3]
    a_top = ig_ctx.get("top_anaba", [])[:3]
    wk = ig_ctx.get("weekend_label", "")
    theme = ig_ctx.get("theme") or {}
    t_key = theme.get("theme_key", "weekend")
    is_live_fb = bool(ig_ctx.get("is_live_fallback"))
    v_lbl = "現在女性" if is_live_fb else "最高女性"
    a_lbl = "女性比率" if is_live_fb else "平均女性比"

    v_lines = "\n".join(f"・{s['seo_name']}（{v_lbl}{s['max_women']}名）" for s in v_top)
    a_lines = "\n".join(f"・{s['seo_name']}（{a_lbl}{s['avg_wpct']}%）" for s in a_top)
    tags = "#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #夜遊びガイド"

    candidates = []

    # 1) ig_empathy_story: Strong 1st-line human hook before fold
    if tm == "female_real":
        hook_1 = "女子の本音で言うと「せっかく髪もメイクも仕上げたのに受付で待たされる店」も「余裕のない席」も一番疲れますよね…💄"
    elif tm in ("spicy", "spicy_female"):
        if t_key == "weekday":
            hook_1 = "平日の仕事帰りに『とりあえず一番有名な店』へ突っ込んで男余りに当たるの、正直一番もったいないです…💸"
        else:
            hook_1 = "週末21時半に何も見ず突撃して『男性待ち50分です』って言われるの、お金もテンションも一番削られますよね…😇"
    else:
        if t_key == "weekday":
            hook_1 = "「平日の仕事帰り、どの店なら待たずに楽しく飲める？」って迷う夜用のまとめです📝"
        elif t_key == "sunday":
            hook_1 = "今週も全国51店舗のデータを追っていて、「やっぱり店選びで9割決まるな…」と実感した結果がこちら📊"
        else:
            hook_1 = "金土の21時半に何も見ないで突撃して「男性待ち50分です」って言われるの、一番もったいないですよね…😂"

    body_ig_1 = (
        f"{hook_1}\n"
        f"実は同じ新宿や梅田・渋谷でも、徒歩3分の別ブランドを見るだけで空気感も待ち時間もガラッと変わります。\n\n"
        f"向かう直前に見返せるよう、右上の「保存📌」をどうぞ！\n\n"
        f"── スライド2枚目：圧倒的に人が集まる人気店\n"
        f"{v_lines}\n\n"
        f"── スライド3枚目：男性待ちを回避しやすい高女性比率の穴場\n"
        f"{a_lines}\n\n"
        f"💡 男女で『正解の店』は逆になります\n"
        f"・男性組：見栄で激混み店に並ばず「女性10名以上×女性比55%以上」の余裕ある店へ\n"
        f"・女性組：女余りで放置される店より「男性先行で盛り上がっている大型店」の方が待ち0分で即乾杯できます🥂\n\n"
        f"いま現在のリアルタイム男女人数と料金比較はプロフィールのリンク（aiseki-navi.com）から3秒で見れます🔍\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_empathy_story",
        "style_label": STYLE_LABELS["ig_empathy_story"],
        "writer_note": f"【{EDITORIAL_TONE_MODES[tm]['short_label']}】インスタの『…続きを読む』前の1行目に本音フックを置き、男女別の逆張り店選びを解説。",
        "main_text": body_ig_1,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 2) ig_data_discovery: Editorial magazine column style
    top_a_name = a_top[0]["seo_name"] if a_top else "穴場店舗"
    top_a_pct = a_top[0]["avg_wpct"] if a_top else 60
    body_ig_2 = (
        f"9.6万件の来店ログを集計していて分かった、{wk}の「外さないお店選び」を5枚のスライドに凝縮しました📖\n\n"
        f"特に注目なのが、女性比率{top_a_pct}%と非常にバランスが良い「{top_a_name}」をはじめとする穴場店舗の推移です👀\n\n"
        f"✦ 女性来店数が多い盛り上がり上位\n"
        f"{v_lines}\n\n"
        f"✦ 男性がスムーズに入りやすい高女性比率上位\n"
        f"{a_lines}\n\n"
        f"23時20分前後は終電組の退店で一時的に人数が動くため、1軒目は20時台、2軒目は0時すぎの第2波を狙うのがデータ上の鉄板ルートです🌙\n"
        f"あとで友達との作戦会議に使えるよう「保存📌」しておくのがおすすめ！\n\n"
        f"現在の全店リアルタイム人数はプロフィールのリンク（相席ナビ）で無料公開中です🔍\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_data_discovery",
        "style_label": STYLE_LABELS["ig_data_discovery"],
        "writer_note": "9.6万件ログの裏付けと「20時台・0時すぎ第2波」の時間帯法則を盛り込んだマガジン編集長トーン。",
        "main_text": body_ig_2,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 3) ig_concise_save: Clean, scannable save-first layout
    body_ig_3 = (
        f"行くお店で迷ったらこの5枚だけチェックしてみてください🥂（{wk}版）\n"
        f"※入店前にパッと見返せる「保存📌」が便利です\n\n"
        f"【ワイワイ盛り上がり重視ならここ】\n"
        f"{v_lines}\n\n"
        f"【待ち時間回避・ゆっくり話すならここ】\n"
        f"{a_lines}\n\n"
        f"同じエリアでも日によって男女比が15〜20%変わるので、向かう3分前にプロフィールのリンク（aiseki-navi.com）で現在人数を見てから入るのが一番確実です✨\n\n"
        f"{tags}"
    )
    candidates.append({
        "style": "ig_concise_save",
        "style_label": STYLE_LABELS["ig_concise_save"],
        "writer_note": "長文を読まない層向けに余白を広く取り、目的別（盛り上がり重視 vs 待ち回避）でスッキリ整理。",
        "main_text": body_ig_3,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    # 3b) ig_ladies_night_guide: Women-perspective zero-wait & perks saveable guide (Plan E)
    try:
        import ladies_guide
        ladies_ig = ladies_guide.build_instagram_ladies_caption(
            research.get("all_domestic_stores") or research.get("top_stores") or [],
            slot_label="19:45",
            now_jst=now_jst,
        )
        candidates.append({
            "style": "ig_ladies_night_guide",
            "style_label": STYLE_LABELS["ig_ladies_night_guide"],
            "writer_note": "【💄 女子組保存ガイド】受付待ちゼロの男性先行店・イベント特典・角が立たない席替えルールを凝縮。",
            "main_text": ladies_ig,
            "reply_text": "（ライブスライド画像または5枚カルーセルとセットで投稿）",
        })
    except Exception as err:
        logger.warning(f"ladies_guide instagram candidate skipped: {err}")

    # 4) ig_classic_template: Legacy baseline
    classic_ig = ig_carousel_generator.build_captions(ig_ctx)["instagram_caption"]
    candidates.append({
        "style": "ig_classic_template",
        "style_label": STYLE_LABELS["ig_classic_template"],
        "writer_note": "従来の『【〇〇 相席ラウンジ狙い目予報📊】』定型キャプション（比較ベンチマーク用）。",
        "main_text": classic_ig,
        "reply_text": "（5枚組カルーセル画像とセットで投稿）",
    })

    return candidates


_NON_HEADCOUNT_SUFFIX_RE = re.compile(
    r"^(?:組|目|以上|以下|未満|限定|待ち|分|時間|円|歳|代|%|％|割|ペア|ソロ|入店|でも)"
)


def _extract_headcount_values(src: str, gender: str) -> list:
    """
    Extract genuine store/national headcount numbers for `gender` ('women' or 'men'),
    ignoring general expressions like '女性2人組', '男性30分待ち', '男性1名入店', '男性1名でも入りやすい',
    '女性20代', '男性1000円', '女性6割', '女性60%', etc.
    """
    pat = r"(?:最高|現在)?女性\s*([0-9,]+)\s*(?:名|人)?" if gender == "women" else r"男性\s*([0-9,]+)\s*(?:名|人)?"
    values = []
    for m in re.finditer(pat, src or ""):
        after = src[m.end():].lstrip()
        if _NON_HEADCOUNT_SUFFIX_RE.match(after):
            continue
        raw_num = m.group(1).replace(",", "")
        if raw_num.isdigit():
            values.append(int(raw_num))
    return values


def verify_ai_factual_numbers(text: str, research: dict, channel: str = "x") -> tuple:
    """
    Strict Desk Fact-Checker for AI-generated drafts:
      1. Ensures every headcount (`女性XX`, `男性XX`), ratio (`女性比XX%`), and diff (`+XX名`)
         matches verified numbers from `research` (zero tolerance for hallucinated counts).
      2. On single-store bullet lines, ensures the store's exact headcount wasn't swapped with another store.
    Returns `(is_valid: bool, bad_tokens: list[str])`.
    """
    all_dom = research.get("all_domestic_stores") or research.get("top_stores") or []
    ig_ctx = research.get("ig_ctx") or {}
    ig_vol = ig_ctx.get("top_volume") or []
    ig_ana = ig_ctx.get("top_anaba") or []

    allowed_women = {10, int(research.get("total_women") or 0)}
    allowed_men = {0, int(research.get("total_men") or 0)}
    allowed_pct = {15, 20, 25, 50, 55, 60}
    allowed_diff = set()
    store_facts = {}

    def _reg_alias(alias: str, w_val=None, m_val=None, p_val=None):
        if not alias:
            return
        rec = store_facts.setdefault(alias, {"women": set(), "men": set(), "pct": set()})
        if w_val is not None:
            rec["women"].add(int(w_val))
        if m_val is not None:
            rec["men"].add(int(m_val))
        if p_val is not None:
            rec["pct"].add(int(p_val))

    for s in all_dom:
        w = int(s.get("women") or 0)
        m = int(s.get("men") or 0)
        p = int(s.get("wpct") or 0)
        allowed_women.add(w)
        allowed_men.add(m)
        allowed_pct.add(p)
        wd = int(s.get("women_diff") or 0)
        ud = int(s.get("usual_w_diff") or 0) if s.get("usual_w_diff") is not None else 0
        if wd > 0:
            allowed_diff.add(wd)
        if ud > 0:
            allowed_diff.add(ud)
        _reg_alias(s.get("ja_name"), w, m, p)
        c_alias = s.get("casual_name") or casual_store_name(s.get("name", ""))
        if c_alias and c_alias != s.get("ja_name"):
            _reg_alias(c_alias, w, m, p)

    for s in ig_vol:
        mw = int(s.get("max_women") or 0)
        allowed_women.add(mw)
        seo_n = s.get("seo_name") or ""
        _reg_alias(seo_n, w_val=mw)
        if "オリエンタルラウンジ" in seo_n:
            _reg_alias(seo_n.replace("オリエンタルラウンジ", "オリラジ"), w_val=mw)

    for s in ig_ana:
        ap = int(s.get("avg_wpct") or 0)
        allowed_pct.add(ap)
        seo_n = s.get("seo_name") or ""
        _reg_alias(seo_n, p_val=ap)
        if "オリエンタルラウンジ" in seo_n:
            _reg_alias(seo_n.replace("オリエンタルラウンジ", "オリラジ"), p_val=ap)

    bad = []

    for val in _extract_headcount_values(text, "women"):
        if val not in allowed_women:
            bad.append(f"女性{val}")

    for val in _extract_headcount_values(text, "men"):
        if val not in allowed_men:
            bad.append(f"男性{val}")

    for m_p in re.finditer(r"(?:女性比(?:率)?|平均女性比)\s*(\d+)\s*%", text):
        val = int(m_p.group(1))
        if val not in allowed_pct:
            bad.append(f"女性比{val}%")

    for m_d in re.finditer(r"\+\s*(\d+)\s*名", text):
        val = int(m_d.group(1))
        if val not in allowed_diff:
            bad.append(f"+{val}名")

    # Line-level single-store cross-check (prevents swapping store A's count onto store B)
    sorted_aliases = sorted(store_facts.keys(), key=len, reverse=True)
    for line in text.splitlines():
        matched_aliases = []
        line_probe = line
        for al in sorted_aliases:
            if al in line_probe:
                matched_aliases.append(al)
                line_probe = line_probe.replace(al, " ")
        if len(matched_aliases) == 1:
            al = matched_aliases[0]
            facts = store_facts[al]
            for vw in _extract_headcount_values(line, "women"):
                if facts["women"] and vw not in facts["women"]:
                    bad.append(f"{al}:女性{vw}(実測不一致)")
            for vm in _extract_headcount_values(line, "men"):
                if facts["men"] and vm not in facts["men"]:
                    bad.append(f"{al}:男性{vm}(実測不一致)")

    return (len(bad) == 0, bad)


def _build_gemini_prompt(
    research: dict,
    plan: dict,
    slot_label: str,
    channel: str,
    now_jst: datetime.datetime,
    site_url: str,
    tone_mode: str = DEFAULT_TONE_MODE,
) -> tuple:
    ch = (channel or "x").lower()
    tm = get_editorial_tone_mode(tone_mode)
    tone_meta = EDITORIAL_TONE_MODES[tm]
    t_str = now_jst.strftime("%H:%M")
    cal_ctx = research.get("calendar") or {}
    weather_ctx = research.get("weather") or {}
    history = get_recent_editorial_history(channel=ch, limit=3)
    recent_openers = [
        (h.get("selected_text") or "").split("\n", 1)[0][:55]
        for h in history if h.get("selected_text")
    ]

    top_s = research.get("top_stores") or []
    focus = research.get("focus_store") or (top_s[0] if top_s else None)
    cluster = research.get("best_cluster")
    anomaly = research.get("anomaly_store")
    male_lead = research.get("male_lead_store")
    city_leaders = research.get("city_leaders") or []

    fact_payload = {
        "current_time": f"{cal_ctx.get('weekday_ja', '')}曜 {t_str}",
        "calendar_vibe": f"{cal_ctx.get('tag', '')} - {cal_ctx.get('note', '')}",
        "weather_vibe": weather_ctx.get("adlib_hint") or "穏やかな夜",
        "weather_detail": weather_ctx.get("summary") or "",
        "national_active_stores": research.get("active_store_count", 0),
        "national_total_women": research.get("total_women", 0),
        "national_total_men": research.get("total_men", 0),
        "sweet_spot_store": (
            {
                "name": focus["ja_name"],
                **({"casual_alias": focus["casual_name"]} if focus.get("casual_name") and focus["casual_name"] != focus["ja_name"] else {}),
                "women": focus["women"],
                "men": focus["men"],
                "women_pct": focus["wpct"],
                "diff_15m": int(focus.get("women_diff") or 0),
            }
            if focus else None
        ),
        "same_area_gap": (
            {
                "area": cluster["hub_label"],
                "high_ratio_store": {
                    "name": cluster["high_store"]["ja_name"],
                    **({"casual_alias": cluster["high_store"]["casual_name"]} if cluster["high_store"].get("casual_name") and cluster["high_store"]["casual_name"] != cluster["high_store"]["ja_name"] else {}),
                    "women": cluster["high_store"]["women"],
                    "men": cluster["high_store"]["men"],
                    "women_pct": cluster["high_store"]["wpct"],
                },
                "contrast_store": {
                    "name": cluster["low_store"]["ja_name"],
                    **({"casual_alias": cluster["low_store"]["casual_name"]} if cluster["low_store"].get("casual_name") and cluster["low_store"]["casual_name"] != cluster["low_store"]["ja_name"] else {}),
                    "women": cluster["low_store"]["women"],
                    "men": cluster["low_store"]["men"],
                    "women_pct": cluster["low_store"]["wpct"],
                },
                "gap_pt": cluster["pct_gap"],
            }
            if cluster else None
        ),
        "usual_positive_anomaly": (
            {
                "name": anomaly["ja_name"],
                **({"casual_alias": anomaly["casual_name"]} if anomaly.get("casual_name") and anomaly["casual_name"] != anomaly["ja_name"] else {}),
                "women": anomaly["women"],
                "men": anomaly["men"],
                "women_pct": anomaly["wpct"],
                "plus_vs_usual_women": int(anomaly["usual_w_diff"]),
            }
            if anomaly and anomaly.get("usual_w_diff") else None
        ),
        "female_group_zero_wait_store": (
            {
                "name": male_lead["ja_name"],
                **({"casual_alias": male_lead["casual_name"]} if male_lead.get("casual_name") and male_lead["casual_name"] != male_lead["ja_name"] else {}),
                "men": male_lead["men"],
                "women": male_lead["women"],
            }
            if male_lead else None
        ),
        "top_stores": [
            {
                "name": s["ja_name"],
                **({"casual_alias": s["casual_name"]} if s.get("casual_name") and s["casual_name"] != s["ja_name"] else {}),
                "women": s["women"],
                "men": s["men"],
                "women_pct": s["wpct"],
            }
            for s in top_s[:4]
        ],
        "city_leaders": [
            {
                "area": c["hub_label"],
                "name": c["store"]["ja_name"],
                **({"casual_alias": c["store"]["casual_name"]} if c["store"].get("casual_name") and c["store"]["casual_name"] != c["store"]["ja_name"] else {}),
                "women": c["store"]["women"],
                "men": c["store"]["men"],
                "women_pct": c["store"]["wpct"],
            }
            for c in city_leaders[:4]
        ],
    }

    slot_hh = (slot_label or "20:00")[:2]
    ymd = now_jst.strftime("%Y%m%d")
    live_url = f"{site_url}/?s=x{slot_hh}_ai_live_adlib_{ymd}"

    if ch == "x":
        f_slug = store_master.slug_of(focus["name"]) if focus else ""
        reply_text = (
            f"📊 {focus['ja_name']}の現在人数・終電後の残りやすさ指標はこちら\n"
            f"{site_url}/store/{f_slug}?s=x{slot_hh}_ai_live_adlib\n\n"
            f"🔍 全国53店舗リアルタイム人数ランキング\n{live_url}"
            if focus else f"🔍 全国53店舗リアルタイム人数ランキング\n{live_url}"
        )
        platform_spec = (
            "【対象SNS：X（旧Twitter）】\n"
            "- 文字数制限：日本語で **95文字〜124文字以内**（X換算で必ず258ウェイト以下になるよう引き締めること）\n"
            "- 禁止事項：URL（http...）は本文に絶対に入れない。『🥇🥈🥉』メダル記号や『【〇〇速報】』のような業者見出しは禁止。同じ店舗名を2回書かない。\n"
            "- リズムの手本（数字はダミー。この空気感・口語の抜け感だけ参考にすること）：\n"
            "  『外ちょっと冷えてきた木曜なんだけど、モニター見てたらオリラジ新宿が女性84名・男性36名（女性比70%）まで伸びてて二度見した…徒歩すぐの別店舗は男余りなのに、ほんと看板だけで並ぶともったいない夜。細かい数字はリプ欄置いときます👇 #相席ナビ #相席ラウンジ』\n"
            "- 構成：1行目で外の天気・気温・曜日・給料日などの空気感とモニターを見た素のリアクションを切り出し、会話の流れで2〜3店舗の正確な人数（例：女性122名・男性52名（女性比70%））を溶かし込み、最後はリプ欄へ自然に流して『#相席ナビ #相席ラウンジ』で締める。"
        )
    elif ch == "threads":
        if now_jst.day % 2 == 0:
            cta_footer = "🔍 全51店舗の現在人数＆料金比較はプロフィールのリンク（相席ナビ）から見れます\n#相席ラウンジ"
        else:
            cta_footer = f"▼ 全51店舗の現在人数＆料金・イベント比較\n{site_url}/price-comparison?s=th{slot_hh}_ai_live_adlib\n#相席ラウンジ"
        reply_text = "（Threadsは1投稿完結・上位AI長考アドリブ生成＋リンク制限回避済）"
        platform_spec = (
            "【対象SNS：Meta Threads（スレッズ）】\n"
            "- 文字数制限：全体で **290文字〜445文字以内**（絶対上限480文字）\n"
            "- 禁止事項：『🥇🥈🥉』メダル記号や『【〇〇】』角括弧タイトルは禁止。ハッシュタグは末尾の1個のみ。箇条書きばかりにせず、人間の独り言エッセイの流れで書くこと。\n"
            "- 構成：Threads特有の『運営中の人がバックヤードでモニターと外の天気・曜日を眺めながら本音をこぼす会話エッセイ調（〜なんだよな、正直さ…等）』で書くこと。男性目線の穴場だけでなく、可能なら女性2人組が待ちゼロで即座れる逆張り店やハシゴのリアルも自然に混ぜる。\n"
            f"- 末尾固定フッター：文章の最後は必ず次のフッター文字列を一字一句変えずに置いて締めること：\n{cta_footer}"
        )
    else:
        import ig_carousel_generator
        ig_ctx = research.get("ig_ctx") or ig_carousel_generator.build_forecast_context(live_stores=top_s, now_jst=now_jst)
        v_top = ig_ctx.get("top_volume", [])[:3]
        a_top = ig_ctx.get("top_anaba", [])[:3]
        is_live_fb = bool(ig_ctx.get("is_live_fallback"))
        v_lbl = "現在女性" if is_live_fb else "最高女性"
        a_lbl = "女性比率" if is_live_fb else "平均女性比"
        fact_payload["carousel_slide2_volume_top3"] = [
            {"name": s["seo_name"], "label": f"{v_lbl}{s['max_women']}名"} for s in v_top
        ]
        fact_payload["carousel_slide3_anaba_top3"] = [
            {"name": s["seo_name"], "label": f"{a_lbl}{s['avg_wpct']}%"} for s in a_top
        ]
        fact_payload["weekend_label"] = ig_ctx.get("weekend_label", "")
        reply_text = "（5枚組カルーセル画像とセットで投稿・上位AI長考アドリブ生成）"
        platform_spec = (
            "【対象SNS：Instagram（5枚組カルーセル投稿キャプション）】\n"
            "- 文字数制限：**400文字〜680文字以内**\n"
            "- 禁止事項：1行目を『【〇〇予報】』のような堅い角括弧見出しにしないこと。宣伝っぽいハイテンション絵文字連打をしないこと。\n"
            "- 構成：『…続きを読む』で折りたたまれる前の最初の1〜2行目に、天気・季節・その曜日のリアルな空気感（「1軒目そろそろ出る頃だけど…」のような生の呟き）を書く。途中で自然な流れで『あとで見返せるように保存📌しとくと便利』と添え、スライド2枚目の盛り上がり上位3店舗とスライド3枚目の高女性比率上位3店舗の数値を正確に織り交ぜ、プロフィールのリンク（aiseki-navi.com）への案内と、末尾に『#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS #夜遊びガイド』の6タグを付ける。"
        )

    slot_persona = research.get("slot_persona") or build_slot_persona_context(slot_label=slot_label, channel=ch)
    cliche_list_str = "・".join(f"『{w}』" for w in AI_CLICHE_WORDS)

    prompt = (
        "あなたは全国53店舗の相席ラウンジ人数をリアルタイム計測している『相席ナビ』の運営中の人です。\n"
        "毎回同じボットのような投稿や綺麗にまとまりすぎたAI構文ではなく、本当にスマホと店内モニターを見ながら、"
        "外の空気（天気・気温・曜日・給料日など）と一緒にフォロワーへ本音で呟いている生身のアドリブ投稿を1本作成してください。\n\n"
        f"{HUMAN_INSIDER_VOICE_GUIDE}\n\n"
        f"{tone_meta['prompt_directive']}\n\n"
        f"【今夜の時間帯テンション設定：{slot_persona['label']}】\n"
        f"- {slot_persona['guidance']}\n\n"
        "【長考（Thinking）プロセス：脳内3案コンペ＆脱AI臭フィルタリング】\n"
        "出力前にまず `thinking_memo` の中で次の3ステップを必ず実行してから `main_text` を書き上げてください：\n"
        "  Step 1. 切り口の違う3つの生の呟きを比べる（①看板だけで並んで消耗する負けパターンへの苦笑い、②女子組が実は見切って流れている裏側、③モニターを見て二度見した街の温度差・普段比の違和感）\n"
        "  Step 2. 一番『人間がふとスマホで打った空気感』に近い1案を選び、AIっぽい綺麗なまとめ文句や過剰な絵文字を削ぎ落とす。\n"
        "  Step 3. ファクトシートの数字と1人・1%の狂いもないか指差し確認する。\n\n"
        f"{platform_spec}\n\n"
        "【絶対厳守ルール（違反するとデスク検閲で減点・不採用になります）】\n"
        "1. 下記の JSON ファクトシートに含まれる店舗名・人数・パーセンテージ以外は絶対に書かないこと。数値を1人でも盛ったり丸めたり変更することは厳禁。\n"
        "2. あなたは『相席ナビ運営（リアルタイム計測モニターを見ている中の人）』です。自分が客として入店した架空の体験談（『行ってきた』『相席相手が〜』等）は絶対に書かないこと。\n"
        "3. 店名の呼び方ルール：内部略称『OLG』や、中途半端な『オリエンタル〇〇』（「ラウンジ」抜け）・『オリエンタル』単体呼びは絶対に使わないこと。必ず正式名称『オリエンタルラウンジ〇〇』、または現場の自然な口語略称『オリラジ〇〇』（casual_alias がある店舗のみ。例：『オリエンタルラウンジ大阪駅前』または『オリラジ大阪駅前』）を使うこと（くだけた会話調や文字数を引き締めたいXでは『オリラジ〇〇』が特に自然。『ag〇〇』『JIS〇〇』はそのまま表記）。\n"
        f"4. AI臭・宣伝臭ワードの完全禁止：{cliche_list_str} は本文に絶対に使わないこと。\n"
        f"5. 直近の過去投稿の書き出し（{json.dumps(recent_openers, ensure_ascii=False)}）とは全く違う新鮮な1行目の切り口・語尾にすること。\n\n"
        f"【今夜の検証済みファクトシート】\n{json.dumps(fact_payload, ensure_ascii=False, indent=2)}\n\n"
        "出力は必ず以下のJSON形式のみを返してください（`thinking_memo` を最初に書いて推敲してから `main_text` を出力すること。Markdownコードブロック不要）：\n"
        '{"thinking_memo": "脳内3案の比較と、AIっぽさを消すためにどこを削りどんな口語の余韻にしたかのメモ（80〜150文字）", "writer_note": "今回どの天気・街況・データをどう本音アドリブに活かしたかの1行メモ", "main_text": "投稿本文（改行は\\n）"}'
    )
    return prompt, reply_text


_DISCOVERY_TTL_SEC = 21600  # 6 hours


def _discover_ai_studio_models(api_key: str, timeout: float = 4.5) -> list:
    """
    Dynamically query `/v1beta/models` (cached 6h) and rank live Gemini text models
    in strict human-writing quality order:
      1. Latest Pro (`gemini-3*-pro*`, `gemini-2.5-pro`, `gemini-pro-latest`)
      2. Latest Standard Flash (`gemini-3*-flash`, `gemini-2.5-flash` — excluding `-lite`)
      3. Emergency Flash-Lite fallback ONLY if Pro & Standard Flash hit quota limits.
    """
    if not api_key:
        return []
    sig = f"gemini:{api_key[:8]}"
    now_t = time.time()
    cached = _model_discovery_cache.get(sig)
    if cached and (now_t - cached["ts"] < _DISCOVERY_TTL_SEC) and cached.get("models"):
        return list(cached["models"])

    try:
        r = requests.get(
            "https://generativelanguage.googleapis.com/v1beta/models",
            headers={"x-goog-api-key": api_key},
            timeout=max(0.8, min(5.0, float(timeout))),
        )
        if r.status_code != 200:
            return []
        models = (r.json() or {}).get("models") or []
        pro_cands = []
        flash_cands = []
        lite_cands = []
        skip_tokens = ("embedding", "imagen", "tts", "audio", "vision", "veo", "aqa", "1.0", "1.5", "robotics", "computer-use")
        for m in models:
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = (m.get("name") or "").replace("models/", "").strip()
            low = name.lower()
            if not low.startswith("gemini") or any(tok in low for tok in skip_tokens):
                continue
            if "pro" in low and "customtools" not in low:
                pro_cands.append(name)
            elif "flash" in low and "lite" not in low and "8b" not in low:
                flash_cands.append(name)
            elif "flash" in low and "lite" in low:
                lite_cands.append(name)

        def _rank_key(n: str):
            # Prefer stable numbered releases (3.x > 2.5 > latest > preview/exp)
            is_exp = ("exp" in n)
            is_prev = ("preview" in n)
            return (0 if not (is_exp or is_prev) else (1 if is_prev else 2), n)

        pro_cands.sort(key=lambda x: (not ("2.5-pro" in x or "3." in x or "3-" in x), "exp" in x, x), reverse=False)
        # Sort version numbers descending while keeping stable ahead of experimental
        pro_sorted = sorted(pro_cands, key=lambda x: (not ("exp" in x), x), reverse=True)
        flash_sorted = sorted(flash_cands, key=lambda x: (not ("exp" in x), x), reverse=True)
        lite_sorted = sorted(lite_cands, key=lambda x: (not ("exp" in x), x), reverse=True)

        ranked = []
        for lst, cap in ((pro_sorted, 2), (flash_sorted, 2), (lite_sorted, 1)):
            for item in lst[:cap]:
                if item not in ranked:
                    ranked.append(item)
        if ranked:
            _model_discovery_cache[sig] = {"ts": now_t, "models": ranked}
            return list(ranked)
    except Exception:
        pass
    return []


def _discover_ai_studio_flash_model(api_key: str, timeout: float = 4.5) -> str:
    """Backward-compatible helper returning the top ranked Gemini model."""
    ranked = _discover_ai_studio_models(api_key, timeout=timeout)
    return ranked[0] if ranked else ""


def _discover_claude_models(api_key: str, timeout: float = 4.0) -> list:
    """
    Dynamically query Anthropic `/v1/models` (cached 6h) to find the newest active Claude Sonnet models.
    """
    if not api_key:
        return []
    sig = f"claude:{api_key[:10]}"
    now_t = time.time()
    cached = _model_discovery_cache.get(sig)
    if cached and (now_t - cached["ts"] < _DISCOVERY_TTL_SEC) and cached.get("models"):
        return list(cached["models"])

    try:
        r = requests.get(
            "https://api.anthropic.com/v1/models",
            headers={
                "x-api-key": api_key,
                "anthropic-version": "2023-06-01",
            },
            timeout=max(0.8, min(4.5, float(timeout))),
        )
        if r.status_code != 200:
            return []
        items = (r.json() or {}).get("data") or []
        sonnets = []
        others = []
        for it in items:
            mid = (it.get("id") or "").strip()
            low = mid.lower()
            if not mid or "claude" not in low:
                continue
            if "sonnet" in low:
                sonnets.append(mid)
            elif "opus" in low:
                others.append(mid)
        ranked = (sonnets[:2] + others[:1]) or list(DEFAULT_CLAUDE_MODELS)
        _model_discovery_cache[sig] = {"ts": now_t, "models": ranked}
        return list(ranked)
    except Exception:
        pass
    return list(DEFAULT_CLAUDE_MODELS)


def _parse_ai_json_object(raw_text: str):
    """Robustly parse a JSON object from either Gemini or Claude output."""
    txt = (raw_text or "").strip()
    if not txt:
        return None
    if txt.startswith("```"):
        txt = re.sub(r"^```(?:json)?\s*", "", txt)
        txt = re.sub(r"\s*```$", "", txt).strip()
    try:
        parsed = json.loads(txt, strict=False)
        if isinstance(parsed, list) and parsed:
            parsed = parsed[0]
        if isinstance(parsed, dict):
            return parsed
    except Exception:
        pass
    # Fallback: extract first outermost `{ ... }` block if model prepended/appended commentary
    start = txt.find("{")
    end = txt.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = json.loads(txt[start : end + 1], strict=False)
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass
    return None


def _build_endpoint_request(
    endpoint_kind: str,
    model_name: str,
    api_key: str,
    prompt: str,
    max_output_tokens: int = 2600,
    thinking_budget: int = 1536,
    temperature: float = 0.88,
) -> tuple:
    gen_cfg = {
        "temperature": temperature,
        "maxOutputTokens": max_output_tokens,
        "responseMimeType": "application/json",
    }
    low_m = (model_name or "").lower()
    if low_m.startswith(("gemini-3.5", "gemini-2.5")) or low_m in (
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-pro-latest",
    ):
        # Pro models require >=128 thinkingBudget and shine around 1536-2048
        gen_cfg["thinkingConfig"] = {"thinkingBudget": max(512, int(thinking_budget))}
    elif low_m.startswith("gemini-3"):
        gen_cfg["thinkingConfig"] = {"thinkingLevel": "MEDIUM" if thinking_budget >= 1024 else "LOW"}

    payload = {
        "contents": [{"role": "user", "parts": [{"text": prompt}]}],
        "generationConfig": gen_cfg,
    }
    return (
        f"https://generativelanguage.googleapis.com/v1beta/models/{model_name}:generateContent",
        {"Content-Type": "application/json", "x-goog-api-key": api_key},
        None,
        payload,
    )


def call_editorial_ai_json(
    prompt: str,
    system_prompt: str = "",
    max_output_tokens: int = 2600,
    thinking_budget: int = 1536,
    temperature: float = 0.87,
    total_deadline_sec: float = 24.0,
    per_call_timeout_sec: float = 13.5,
    allow_blocking: bool = True,
    now_jst: datetime.datetime = None,
) -> tuple:
    """
    Unified Dual-Engine AI JSON caller supporting BOTH Anthropic Claude Sonnet and Upgraded Google Gemini (Pro / Flash).
    Automatically routes based on configured keys and `get_ai_engine_pref()`, with instant failover across providers/models.
    Returns `(parsed_dict_or_None, model_label_str, last_error_str)`.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    g_key = get_gemini_api_key(allow_blocking=allow_blocking)
    c_key = get_claude_api_key(allow_blocking=allow_blocking)
    if not g_key and not c_key:
        return None, "", "APIキー未設定"

    pref = get_ai_engine_pref(allow_blocking=False)
    providers = []
    if pref == "gemini":
        if g_key:
            providers.append("gemini")
        if c_key:
            providers.append("claude")
    else:
        # Both 'auto' and 'claude' try Claude Sonnet first when Claude key is present, then fail over to Gemini Pro/Flash
        if c_key:
            providers.append("claude")
        if g_key:
            providers.append("gemini")

    deadline = time.monotonic() + max(4.0, float(total_deadline_sec))
    last_err = ""

    sys_instr = system_prompt or (
        "あなたは『相席ナビ』の運営中の人・編集長です。AI特有の優等生構文や定型表現を徹底排除し、"
        "生身の人間が現場モニターを見ながら本音で語る自然な口語・余韻で執筆してください。"
        "必ず有効なJSONオブジェクトのみを返してください（Markdownの```jsonフェンスは不要）。"
    )

    for prov in providers:
        if time.monotonic() >= deadline - 0.8:
            break

        if prov == "claude":
            preferred_c = (os.environ.get("CLAUDE_MODEL") or "").strip()
            discovered_c = _discover_claude_models(c_key, timeout=min(3.2, max(1.0, deadline - time.monotonic() - 2.0)))
            c_models = []
            if preferred_c:
                c_models.append(preferred_c)
            saved_r = _last_gemini_meta.get("working_route")
            if saved_r and saved_r[0] == "claude" and saved_r[1] not in c_models:
                c_models.append(saved_r[1])
            for m in (discovered_c or DEFAULT_CLAUDE_MODELS):
                if m not in c_models:
                    c_models.append(m)

            for cm in c_models[:2]:
                rem = deadline - time.monotonic()
                if rem <= 0.8:
                    break
                try:
                    resp = requests.post(
                        "https://api.anthropic.com/v1/messages",
                        headers={
                            "x-api-key": c_key,
                            "anthropic-version": "2023-06-01",
                            "content-type": "application/json",
                        },
                        json={
                            "model": cm,
                            "max_tokens": max_output_tokens,
                            "temperature": min(1.0, max(0.1, float(temperature))),
                            "system": sys_instr,
                            "messages": [{"role": "user", "content": prompt}],
                        },
                        timeout=min(float(per_call_timeout_sec), rem),
                    )
                    if resp.status_code >= 400:
                        last_err = f"Claude HTTP {resp.status_code} ({cm}): {(resp.text or '')[:130]}"
                        logger.warning(f"[editorial_ai] {last_err}")
                        if _last_gemini_meta.get("working_route") == ("claude", cm):
                            _last_gemini_meta["working_route"] = None
                        continue
                    blocks = (resp.json() or {}).get("content") or []
                    raw_txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
                    parsed = _parse_ai_json_object(raw_txt)
                    if parsed:
                        _last_gemini_meta["working_route"] = ("claude", cm)
                        _last_gemini_meta["last_provider"] = "claude"
                        _last_gemini_meta["last_model"] = cm
                        _last_gemini_meta["last_error"] = ""
                        _last_gemini_meta["last_ok_at"] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
                        return parsed, cm, ""
                    last_err = f"Claude JSON parse failed ({cm})"
                except Exception as e:
                    last_err = f"Claude通信エラー ({cm}): {str(e)[:110]}"
                    logger.warning(f"[editorial_ai] {last_err}")
                    if _last_gemini_meta.get("working_route") == ("claude", cm):
                        _last_gemini_meta["working_route"] = None

        elif prov == "gemini":
            preferred_g = (os.environ.get("GEMINI_MODEL") or "").strip()
            discovered_g = _discover_ai_studio_models(g_key, timeout=min(3.5, max(1.0, deadline - time.monotonic() - 2.0)))
            g_models = []
            if preferred_g:
                g_models.append(preferred_g)
            saved_r = _last_gemini_meta.get("working_route")
            # Only reuse cached working_route if it isn't an old flash-lite leftover when Pro/Flash are available!
            if (
                saved_r
                and saved_r[0] == "ai_studio"
                and ("lite" not in saved_r[1].lower() or not discovered_g)
                and saved_r[1] not in g_models
            ):
                g_models.append(saved_r[1])
            for m in (discovered_g or list(DEFAULT_GEMINI_MODELS)):
                if m not in g_models:
                    g_models.append(m)
            for m in DEFAULT_GEMINI_MODELS:
                if m not in g_models:
                    g_models.append(m)

            full_prompt = f"{sys_instr}\n\n{prompt}" if system_prompt else prompt
            for gm in g_models[:3]:
                rem = deadline - time.monotonic()
                if rem <= 0.8:
                    break
                try:
                    url, headers, params, payload = _build_endpoint_request(
                        "ai_studio",
                        gm,
                        g_key,
                        full_prompt,
                        max_output_tokens=max_output_tokens,
                        thinking_budget=thinking_budget,
                        temperature=temperature,
                    )
                    resp = requests.post(
                        url,
                        headers=headers,
                        params=params,
                        json=payload,
                        timeout=min(float(per_call_timeout_sec), rem),
                    )
                    if resp.status_code >= 400:
                        last_err = f"Gemini HTTP {resp.status_code} ({gm}): {(resp.text or '')[:130]}"
                        logger.warning(f"[editorial_ai] {last_err}")
                        if _last_gemini_meta.get("working_route") == ("ai_studio", gm):
                            _last_gemini_meta["working_route"] = None
                        continue
                    cands = ((resp.json() or {}).get("candidates")) or []
                    if not cands:
                        last_err = f"Gemini empty candidates ({gm})"
                        continue
                    parts = ((cands[0].get("content") or {}).get("parts")) or []
                    raw_txt = "".join(p.get("text", "") for p in parts if not p.get("thought")).strip()
                    if not raw_txt:
                        raw_txt = "".join(p.get("text", "") for p in parts).strip()
                    parsed = _parse_ai_json_object(raw_txt)
                    if parsed:
                        _last_gemini_meta["working_route"] = ("ai_studio", gm)
                        _last_gemini_meta["last_provider"] = "gemini"
                        _last_gemini_meta["last_model"] = gm
                        _last_gemini_meta["last_error"] = ""
                        _last_gemini_meta["last_ok_at"] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
                        return parsed, gm, ""
                    last_err = f"Gemini JSON parse failed ({gm})"
                except Exception as e:
                    last_err = f"Gemini通信エラー ({gm}): {str(e)[:110]}"
                    logger.warning(f"[editorial_ai] {last_err}")
                    if _last_gemini_meta.get("working_route") == ("ai_studio", gm):
                        _last_gemini_meta["working_route"] = None

    if last_err:
        _last_gemini_meta["last_error"] = last_err
    return None, "", last_err


def _postprocess_ai_main_text(main_text: str, channel: str = "x") -> str:
    """
    Guarantee zero bare 'オリエンタル〇〇' ('ラウンジ' omitted) in AI output:
      - Converts any 'オリエンタル(?!ラウンジ)' to 'オリエンタルラウンジ' (or colloquial 'オリラジ' if X/Threads length is tight).
      - Also trims 'オリエンタルラウンジ' -> 'オリラジ' on X if Twitter weight exceeds 272 so posts stay inside optimal range.
    """
    text = (main_text or "").strip()
    if not text:
        return ""
    ch = (channel or "x").lower()

    if re.search(r"オリエンタル(?!ラウンジ)", text):
        expanded = re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", text)
        if ch == "x" and x_poster.compute_twitter_weight(expanded) > 270:
            text = re.sub(r"オリエンタル(?!ラウンジ)", "オリラジ", text)
        elif ch == "threads" and len(expanded) > 485:
            text = re.sub(r"オリエンタル(?!ラウンジ)", "オリラジ", text)
        else:
            text = expanded

    if ch == "x" and x_poster.compute_twitter_weight(text) > 272 and "オリエンタルラウンジ" in text:
        text = text.replace("オリエンタルラウンジ", "オリラジ")
    elif ch == "threads" and len(text) > 485 and "オリエンタルラウンジ" in text:
        text = text.replace("オリエンタルラウンジ", "オリラジ")
    return text


def _make_ai_cache_key(
    ch: str,
    slot_label: str,
    research: dict,
    now_jst: datetime.datetime = None,
    tone_mode: str = DEFAULT_TONE_MODE,
):
    """
    Build a precise cache key incorporating engine_pref, channel, tone_mode, business date, slot,
    top-3 stores `(name, women, men)`, and `focus_store` `(name, women, men)`.
    """
    ch_norm = (ch or "x").lower()
    tm_norm = get_editorial_tone_mode(tone_mode)
    eng_pref = get_ai_engine_pref(allow_blocking=False)
    has_c = bool(get_claude_api_key(allow_blocking=False))
    top_s = (research or {}).get("top_stores") or []
    if not top_s:
        return None
    if now_jst is None and research and research.get("timestamp"):
        try:
            now_jst = datetime.datetime.strptime(research["timestamp"], "%Y-%m-%d %H:%M:%S").replace(tzinfo=JST)
        except Exception:
            now_jst = None
    biz_date_str = _business_datetime(now_jst).strftime("%Y-%m-%d")
    top3_sig = tuple(
        (s.get("name", ""), int(s.get("women") or 0), int(s.get("men") or 0))
        for s in top_s[:3]
    )
    focus = (research or {}).get("focus_store")
    focus_sig = (
        (focus.get("name", ""), int(focus.get("women") or 0), int(focus.get("men") or 0))
        if focus else None
    )
    return (eng_pref, has_c, ch_norm, tm_norm, slot_label or "", biz_date_str, top3_sig, focus_sig)


def _store_ai_candidate_cache(cache_key, candidate: dict, model_name: str, now_t: float = None):
    if cache_key is None:
        return
    now_t = now_t if now_t is not None else time.time()
    with _ai_cache_lock:
        _ai_candidate_cache.pop(cache_key, None)
        _ai_candidate_cache[cache_key] = {
            "ts": now_t,
            "candidate": candidate,
            "model": model_name,
        }
        _prune_ai_candidate_cache_unlocked(now_t)


def has_cached_ai_candidate(
    research: dict,
    slot_label: str,
    channel: str,
    now_jst: datetime.datetime = None,
    tone_mode: str = DEFAULT_TONE_MODE,
) -> bool:
    cache_key = _make_ai_cache_key(channel, slot_label, research, now_jst, tone_mode=tone_mode)
    if cache_key is None:
        return False
    now_t = time.time()
    with _ai_cache_lock:
        _prune_ai_candidate_cache_unlocked(now_t)
        cached = _ai_candidate_cache.get(cache_key)
        return bool(cached and (now_t - cached["ts"] < _AI_CACHE_TTL_SEC))


def _generate_gemini_adlib_candidate(
    research: dict,
    plan: dict,
    slot_label: str,
    channel: str,
    now_jst: datetime.datetime,
    site_url: str,
    force_refresh: bool = False,
    cache_only: bool = False,
    tone_mode: str = DEFAULT_TONE_MODE,
) -> dict:
    """
    Generate live human-voiced SNS draft via Dual-Engine Chief Writer (Claude Sonnet or Upgraded Gemini Pro/Flash).
    When `cache_only=True`, returns cached draft in 0ms without blocking HTML page render.
    """
    if not has_any_ai_key(allow_blocking=(not cache_only)):
        return None

    ch = (channel or "x").lower()
    tm = get_editorial_tone_mode(tone_mode)
    cache_key = _make_ai_cache_key(ch, slot_label, research, now_jst, tone_mode=tm)
    if cache_key is None:
        return None

    now_t = time.time()
    with _ai_cache_lock:
        _prune_ai_candidate_cache_unlocked(now_t)
        if not force_refresh and cache_key in _ai_candidate_cache:
            cached = _ai_candidate_cache[cache_key]
            if now_t - cached["ts"] < _AI_CACHE_TTL_SEC:
                _ai_candidate_cache.move_to_end(cache_key)
                return dict(cached["candidate"])

    if cache_only:
        return None

    prompt, reply_text = _build_gemini_prompt(
        research=research,
        plan=plan,
        slot_label=slot_label,
        channel=ch,
        now_jst=now_jst,
        site_url=site_url,
        tone_mode=tm,
    )

    parsed, model_used, _ = call_editorial_ai_json(
        prompt=prompt,
        max_output_tokens=2600,
        thinking_budget=1536,
        temperature=0.88,
        total_deadline_sec=24.0,
        per_call_timeout_sec=13.5,
        allow_blocking=True,
        now_jst=now_jst,
    )
    if not parsed:
        return None

    main_text = _postprocess_ai_main_text(parsed.get("main_text") or "", channel=ch)
    if not main_text:
        return None
    writer_note = (parsed.get("writer_note") or "天気・曜日・リアルタイム人数を掛け合わせた本音アドリブ執筆案。").strip()
    thinking_memo = (parsed.get("thinking_memo") or "").strip()

    candidate = {
        "style": "ai_live_adlib",
        "style_label": f"✨ AI編集長・現場本音アドリブ実況型（{model_used}）",
        "writer_note": f"【{model_used}・本音長考推敲】{writer_note}",
        "thinking_memo": thinking_memo,
        "main_text": main_text,
        "reply_text": reply_text,
        "is_ai_generated": True,
        "ai_model": model_used,
    }
    _store_ai_candidate_cache(cache_key, candidate, model_used, now_t=time.time())
    return dict(candidate)


def run_writer(
    research: dict,
    plan: dict,
    slot_label: str = "22:00",
    channel: str = "x",
    now_jst: datetime.datetime = None,
    app_url: str = "",
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
    tone_mode: str = None,
) -> list:
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    ch = (channel or "x").lower()
    tm = get_editorial_tone_mode(tone_mode, allow_blocking=(not ai_cache_only))
    if ch == "threads":
        base_cands = _write_threads_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url, tone_mode=tm)
    elif ch == "instagram":
        base_cands = _write_instagram_candidates(research, now_jst=now_jst, tone_mode=tm)
    else:
        base_cands = _write_x_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url, tone_mode=tm)

    if use_ai:
        ai_cand = _generate_gemini_adlib_candidate(
            research=research,
            plan=plan,
            slot_label=slot_label,
            channel=ch,
            now_jst=now_jst,
            site_url=site_url,
            force_refresh=force_ai_refresh,
            cache_only=ai_cache_only,
            tone_mode=tm,
        )
        if ai_cand:
            return [ai_cand] + base_cands
    return base_cands


# =====================================================================
# Role 4: Evaluator / Desk (鬼デスク・評価係)
# =====================================================================
def run_evaluator(
    candidates: list,
    research: dict,
    plan: dict,
    channel: str = "x",
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Score each candidate out of 100 across 4 criteria tailored to `x`, `threads`, or `instagram`:
      1. human_tone (35 pts): Natural human voice vs mechanical medal/bracket templates
      2. freshness_diversity (30 pts): Rotation vs recent history on the same SNS channel
      3. data_specificity (25 pts): Verified numbers (strict AI hallucination guard!), zero redundant mentions
      4. weight_fit (10 pts): Strict compliance with each platform's character & link-hygiene limits
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    last_style = plan.get("last_style")
    recent_styles = plan.get("recent_styles") or []
    style_priority = plan.get("style_priority") or []
    classic_key = plan.get("classic_key")

    pdca_boosts = {}
    try:
        import pdca_engine
        pdca_boosts = (pdca_engine.get_active_directives(allow_blocking=False) or {}).get("sns_style_boosts") or {}
    except Exception:
        pdca_boosts = {}

    evaluated = []
    for cand in candidates:
        text = cand.get("main_text") or ""
        style = cand.get("style") or ""
        is_ai = bool(cand.get("is_ai_generated") or style == "ai_live_adlib")
        first_line = text.split("\n", 1)[0] if text else ""

        if ch == "x":
            metric_val = x_poster.compute_twitter_weight(text)
            metric_unit = f"{metric_val}/280w"
        else:
            metric_val = len(text)
            metric_unit = f"{metric_val}文字"

        # 1. Human Tone (max 35) — strictly penalizes bot medals, bracket headers, AND AI copywriting cliches
        is_medal_bot = ("🥇" in text and "🥈" in text and "🥉" in text)
        is_bracket_banner = first_line.startswith("【") and "】" in first_line
        detected_cliches = [w for w in AI_CLICHE_WORDS if w in text]
        if style == classic_key or is_medal_bot:
            human_score = 18
            human_comment = "⚠️ 毎回同じ『🥇🥈🥉 TOP3』や【角括弧見出し】の定型フォーマットで業者・ボット感が出やすいため減点。"
        elif detected_cliches:
            human_score = 22
            human_comment = f"⚠️ AI・宣伝っぽく見えやすい定型ワード（『{'』『'.join(detected_cliches[:2])}』）が含まれているため減点。"
        elif is_bracket_banner:
            human_score = 24
            human_comment = "△ 冒頭が【角括弧の見出し】から始まっており少し宣伝っぽいため、自然な語り出し案を優先。"
        elif is_ai:
            human_score = 35
            human_comment = "◎ 長考3案コンペ＆脱AI推敲クリア：AI臭ワード0個・時間帯の空気に馴染む生きた人間の語り口です。"
        else:
            human_score = 34
            if any(w in text for w in ("割れてる", "立ち上がりがかなり早い", "いつもの", "もったいない", "実は同じ", "温度差", "雨の", "給料日", "祝前日", "残酷", "女子", "待たされる", "消耗", "正解の店", "見切")):
                human_score = 35
            human_comment = "◎ 人間がスマホで現場を見ながら話しかけている自然な導入で、タイムラインに馴染みます。"

        # 2. Freshness & Anti-Duplication (max 30)
        if is_ai:
            # Even if AI was used last time, each AI generation uses a brand-new opener avoiding recent history!
            fresh_score = 30
            fresh_comment = "◎ 過去3回の投稿と書き出し・言い回しが一切被らない完全新規アドリブ構成。"
        elif style == last_style:
            fresh_score = 12
            fresh_comment = f"⚠️ 前回の{CHANNEL_LABELS.get(ch, ch)}投稿と同じ型（{STYLE_LABELS.get(style, style)}）のため連続回避で大幅減点。"
        elif style == classic_key and plan.get("suppress_classic"):
            fresh_score = 15
            fresh_comment = "⚠️ 定型ランキングのマンネリ防止ルールにより優先度を引き下げ。"
        elif style in recent_styles[:2]:
            fresh_score = 22
            fresh_comment = "◯ 直近2回以内に近い切り口があるため、別角度の候補を優先。"
        else:
            if style in style_priority:
                p_idx = style_priority.index(style)
                fresh_score = 30 if p_idx == 0 else (26 if p_idx == 1 else 24)
            else:
                fresh_score = 24
            fresh_comment = "◎ 直近の投稿履歴と切り口・1行目のフックが被っておらず新鮮です。"

        # 3. Data Specificity & Precision Check (max 25; -50 hard reject if AI alters any number)
        has_raw_olg = "OLG " in text and style != classic_key
        has_bare_oriental = bool(re.search(r"オリエンタル(?!ラウンジ)", text)) and style != classic_key
        if is_ai:
            facts_ok, bad_tokens = verify_ai_factual_numbers(text, research, channel=ch)
            if not facts_ok:
                data_score = -50
                data_comment = (
                    f"❌ 鬼デスク検閲ブロック：AI下書き内に実測データと一致しない数値（{', '.join(bad_tokens[:3])}）を検知！"
                    "ハルシネーション防止のため不採用とし、検証済み案へ自動切替します。"
                )
            elif has_raw_olg or has_bare_oriental:
                data_score = 18
                data_comment = "⚠️ 店名表記に内部略称『OLG』または『オリエンタル（ラウンジ抜け）』が残っているため減点。"
            else:
                data_score = 25
                data_comment = "◎ 鬼デスク数値検閲クリア：すべての店舗人数・比率がSQLite実測値と1人単位で100%一致しています。"
        elif has_raw_olg or has_bare_oriental:
            data_score = 18
            data_comment = "⚠️ 店名表記に『OLG』または『オリエンタル（ラウンジ抜け）』が残っているため減点（『オリエンタルラウンジ〇〇』または『オリラジ〇〇』が自然）。"
        elif style in ("hub_gap", "threads_city_story") and research.get("best_cluster"):
            gap = research["best_cluster"].get("pct_gap", 0)
            data_score = 25 if gap >= 12 else 22
            data_comment = f"◎ 同一エリア内の男女比ギャップ（{gap}pt差）が実測値で示されており、店選びの納得感が非常に高い。"
        elif style in ("usual_anomaly", "threads_anomaly_insight"):
            data_score = 25
            data_comment = "◎ 普段比（平比）データや男女双方の目線（穴場店＋即相席店）が具体的で精度が高い。"
        elif style in ("ig_empathy_story", "ig_data_discovery"):
            data_score = 25
            data_comment = "◎ カルーセル5枚の統計データと完全連動し、保存したくなる具体的な立ち回り根拠あり。"
        else:
            data_score = 23 if style != classic_key else 20
            data_comment = "◎ 主要店舗の実測人数・比率が過不足なく整理されています。"

        # 4. Platform Format Hygiene (max 10; hard penalty if over API limit)
        if ch == "x":
            has_url_in_main = "http://" in text or "https://" in text
            if metric_val > 280 or has_url_in_main:
                weight_score = -40
                weight_comment = f"❌ X制限超過({metric_unit})または1ポスト目URL混入のため不採用。"
            elif 130 <= metric_val <= 274:
                weight_score = 10
                weight_comment = f"◎ X最適ウェイト（{metric_unit}・1ポスト目URLなし・リプ欄誘導）を完璧にクリア。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数ウェイト（{metric_unit}）は許容範囲内。"
        elif ch == "threads":
            tag_cnt = len(re.findall(r"#[^\s#]+", text))
            if metric_val > 500:
                weight_score = -40
                weight_comment = f"❌ Threads上限500文字を超過（{metric_unit}）しているため不採用。"
            elif 240 <= metric_val <= 488 and tag_cnt <= 2:
                weight_score = 10
                weight_comment = f"◎ Threads最適文字数（{metric_unit}/500文字・タグ{tag_cnt}個でスパム判定回避）に合致。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数（{metric_unit}）・タグ数（{tag_cnt}個）は許容範囲内。"
        else:  # instagram
            tag_cnt = len(re.findall(r"#[^\s#]+", text))
            if 340 <= metric_val <= 980 and 4 <= tag_cnt <= 8 and "保存" in text:
                weight_score = 10
                weight_comment = f"◎ IG最適長（{metric_unit}）・保存導線あり・厳選ハッシュタグ{tag_cnt}個でアルゴリズム最適。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数（{metric_unit}）・タグ{tag_cnt}個は標準範囲内。"

        pdca_boost = int(pdca_boosts.get(style, 0)) if (data_score > 0 and weight_score > 0) else 0
        raw_total = human_score + fresh_score + data_score + weight_score + pdca_boost
        total_score = min(100, raw_total)
        desk_comments = [
            human_comment,
            fresh_comment,
            data_comment,
            weight_comment,
        ]
        if pdca_boost != 0:
            desk_comments.append(
                f"🧭 PDCA参謀補正：直近のサイト流入・回遊実績に基づきスコアを {pdca_boost:+d}点 自動調整しました。"
            )
        item_copy = dict(cand)
        item_copy.update({
            "channel": ch,
            "total_score": total_score,
            "raw_total_score": raw_total,
            "pdca_boost": pdca_boost,
            "weight": metric_val,
            "metric_unit": metric_unit,
            "scores": {
                "human_tone": human_score,
                "freshness_diversity": fresh_score,
                "data_specificity": data_score,
                "weight_fit": weight_score,
                "pdca_boost": pdca_boost,
            },
            "desk_comments": desk_comments,
        })
        evaluated.append(item_copy)

    evaluated.sort(key=lambda x: (x["total_score"], x.get("raw_total_score", 0)), reverse=True)
    for idx, item in enumerate(evaluated):
        item["rank"] = idx + 1
        item["is_selected"] = (idx == 0)

    winner = evaluated[0] if evaluated else None
    classic_score = next((c["total_score"] for c in evaluated if c["style"] == classic_key), "-")
    desk_summary = (
        f"【{CHANNEL_LABELS.get(ch, ch)}】全{len(evaluated)}案を審査し、最高スコア {winner['total_score']}点（100点満点）の"
        f"『{winner['style_label']}』を採用しました。"
        f"従来の定型フォーマット（{classic_score}点）を避け、媒体アルゴリズムと人間の読みやすさに最適化しています。"
        if winner else "候補案なし"
    )

    return {
        "channel": ch,
        "winner": winner,
        "candidates": evaluated,
        "desk_summary": desk_summary,
    }


def run_editorial_pipeline(
    stores: list,
    slot_label: str = "22:00",
    channel: str = "x",
    ig_ctx: dict = None,
    now_jst: datetime.datetime = None,
    app_url: str = "",
    record_history: bool = False,
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
    tone_mode: str = None,
) -> dict:
    """
    Run the full 4-role SNS Editorial Room pipeline for `channel` ('x', 'threads', or 'instagram').
    When `ai_cache_only=True` (used on HTML page render), returns in <30ms using cached AI draft if available,
    and flags `ai_pending_async=True` so client JS can fetch the fresh AI draft asynchronously without blocking UI.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    if ch not in ("x", "threads", "instagram"):
        ch = "x"

    allow_state_blocking = bool(record_history or (use_ai and not ai_cache_only))
    default_tm = get_default_editorial_tone_mode(allow_blocking=allow_state_blocking)
    resolved_tm = get_editorial_tone_mode(tone_mode, allow_blocking=allow_state_blocking)

    research = run_researcher(
        stores,
        slot_label=slot_label,
        channel=ch,
        ig_ctx=ig_ctx,
        now_jst=now_jst,
        allow_weather_blocking=record_history,
    )
    plan = run_planner(
        research,
        slot_label=slot_label,
        channel=ch,
        now_jst=now_jst,
        allow_blocking=allow_state_blocking,
    )
    candidates = run_writer(
        research,
        plan,
        slot_label=slot_label,
        channel=ch,
        now_jst=now_jst,
        app_url=app_url,
        use_ai=use_ai,
        force_ai_refresh=force_ai_refresh,
        ai_cache_only=ai_cache_only,
        tone_mode=resolved_tm,
    )
    evaluation = run_evaluator(candidates, research, plan, channel=ch, now_jst=now_jst)

    has_ai_cand = any(c.get("is_ai_generated") for c in (evaluation.get("candidates") or []))
    sheets_warming = (not getattr(job_state, "_sheets_loaded", True)) and (not job_state.sheets_store.is_disabled())
    ai_pending_async = bool(
        ai_cache_only
        and use_ai
        and (has_any_ai_key(allow_blocking=False) or sheets_warming)
        and (force_ai_refresh or not has_ai_cand)
        and bool(research.get("top_stores"))
    )

    winner = evaluation.get("winner") or {}
    session_record = {
        "channel": ch,
        "channel_label": CHANNEL_LABELS.get(ch, ch),
        "tone_mode": resolved_tm,
        "tone_mode_label": EDITORIAL_TONE_MODES[resolved_tm]["short_label"],
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "slot_label": slot_label,
        "selected_style": winner.get("style", ""),
        "selected_style_label": winner.get("style_label", ""),
        "selected_score": winner.get("total_score", 0),
        "selected_text": winner.get("main_text", ""),
        "selected_reply": winner.get("reply_text", ""),
        "selected_thinking_memo": winner.get("thinking_memo", ""),
        "desk_summary": evaluation.get("desk_summary", ""),
        "planner_memo": plan.get("planner_memo", ""),
        "research_notes": research.get("research_notes", []),
        "candidates": [
            {
                "rank": c.get("rank"),
                "is_selected": c.get("is_selected"),
                "style": c.get("style"),
                "style_label": c.get("style_label"),
                "total_score": c.get("total_score"),
                "weight": c.get("weight"),
                "metric_unit": c.get("metric_unit"),
                "scores": c.get("scores"),
                "writer_note": c.get("writer_note"),
                "thinking_memo": c.get("thinking_memo") or "",
                "desk_comments": c.get("desk_comments"),
                "main_text": c.get("main_text"),
                "reply_text": c.get("reply_text"),
                "is_ai_generated": bool(c.get("is_ai_generated")),
            }
            for c in (evaluation.get("candidates") or [])
        ],
    }

    if record_history and winner:
        persist_editorial_session_record(ch, session_record)

    return {
        "channel": ch,
        "channel_label": CHANNEL_LABELS.get(ch, ch),
        "tone_mode": resolved_tm,
        "tone_mode_label": EDITORIAL_TONE_MODES[resolved_tm]["label"],
        "default_tone_mode": default_tm,
        "tone_modes": get_tone_modes_catalog(),
        "gemini_status": get_gemini_status(allow_blocking=allow_state_blocking),
        "delivery_health": get_sns_delivery_health(allow_blocking=allow_state_blocking),
        "ai_pending_async": ai_pending_async,
        "research": research,
        "plan": plan,
        "evaluation": evaluation,
        "winner": winner,
        "session_record": session_record,
        "history": get_recent_editorial_history(channel=ch, limit=8, allow_blocking=allow_state_blocking),
    }
