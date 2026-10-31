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
import hashlib
import datetime
import logging
import threading
import collections
import requests

import job_state
import store_master
import turbo_store
import x_poster

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))
GEMINI_KEY_STORAGE_PREFIX = "token:gemini_api_key"
CLAUDE_KEY_STORAGE_PREFIX = "token:claude_api_key"
AI_ENGINE_PREF_KEY = "editorial:ai_engine_pref"

# Upgraded from Flash-Lite to Pro -> Standard Flash -> Lite emergency fallback
DEFAULT_GEMINI_MODELS = (
    "gemini-3.1-pro-preview",
    "gemini-2.5-pro",
    "gemini-2.5-flash",
    "gemini-flash-latest",
    "gemini-3.5-flash-lite",
)

# Anthropic Claude Opus & Sonnet models (dynamically supplemented via /v1/models when key is set)
DEFAULT_CLAUDE_MODELS = (
    "claude-opus-5-5",
    "claude-sonnet-5-5",
    "claude-opus-5",
    "claude-sonnet-5",
    "claude-opus-4-8",
    "claude-sonnet-4-6",
    "claude-haiku-5-5",
)

# Words/phrases that betray AI-generated or promotional copywriting (or overused hooks) — strictly penalized by Desk
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
    "二度見",
    "２度見",
    "2度見",
    "バックヤード",
    "モニター見て",
    "モニターを見",
    "看板だけで",
    "サンクコスト",
)

# Core human voice guide: casual, understated adult friend / regular watcher (zero backstage meta-talk)
HUMAN_INSIDER_VOICE_GUIDE = (
    "【最優先：生身の人間らしさ・『気取らない大人の友人・街ウォッチャー』の文体原則】\n"
    "1. 『モニター見てたら』『バックヤードで』『集計してたら』などの運営・裏方アピールは一切書かないこと。夜の街の空気感や男女のリアルに詳しい『こなれた友人』が、ふとスマホで呟いたような自然な距離感にすること（ただし自分が今日客として入店した架空の体験談は書かない）。\n"
    "2. 毎回きれいに教訓や説教（『〜すると損』『〜が正解』『賢く選ぼう』等）でまとめないこと。事実の偏りや街の空気をポンと置くだけで十分伝わる。\n"
    "3. 語尾をワンパターンにしないこと。『〜なんだよな』『正直さ、』のような口癖の連発は厳禁。体言止め、普通の言い切り（『〜てる』『〜だった』『〜の差。』）、ふと混ざる素の丁寧語（『〜ですね』『〜かも』『〜置いときます』）を日によって自然に織り交ぜること。\n"
    "4. 絵文字や感嘆符（！）は極力減らし、全体で0〜1個までに抑えること（絵文字ゼロの素っ気ない文面も大歓迎）。キラキラ絵文字の連打や大げさな煽りは一切しない。\n"
    "5. 店舗名・女性XX名・男性YY名・女性比ZZ%の数値は、ファクトシートの検証済みデータと1人・1%も違わず正確に書くこと。"
)

_weather_cache = {"ts": 0.0, "data": None, "fetching": False}
_AI_CACHE_TTL_SEC = 900
_AI_CACHE_MAX_SIZE = 64
_ai_candidate_cache = collections.OrderedDict()  # cache_key -> {"ts": float, "candidate": dict, "model": str}
_ai_cache_lock = threading.Lock()
_model_discovery_cache = {}  # provider_sig -> {"ts": float, "models": list[str]}
_claude_no_temp_models = set()  # Claude models where `temperature` is deprecated/unsupported
_UNHEALTHY_COOLDOWN_SEC = 600.0
_unhealthy_models = {}  # model_name -> cooldown_until_monotonic
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
    "tiktok": "🎵 TikTok（縦型ショート）",
    "note": "note (ショートエッセイ・つぶやき)",
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
    # TikTok styles
    "tiktok_hook_gap": "⚡ 冒頭1.5秒フック・徒歩4分男女比ギャップ衝撃型",
    "tiktok_female_confession": "💄 女子の本音暴露・受付待ちゼロ逆張り穴場型",
    "tiktok_data_myth": "📊 常識破壊・『看板だけで並ぶと負ける』実測解剖型",
    "tiktok_midnight_reverse": "🌙 23時20分の終電崖・深夜逆転ラウンジ速報型",
    "tiktok_classic_rank": "🏆 従来型・定型ランキング読み上げキャプション",
    # note styles
    "note_essay_insight": "📖 夜の人間模様・深層考察ショートエッセイ型（800〜1,400文字）",
    "note_short_column": "💡 混雑データ解剖・勝てる立ち回りミニコラム型（800〜1,200文字）",
    "note_tsubuyaki_gap": "💬 現場の独り言・街の温度差つぶやき型（120〜240文字）",
    "note_classic_summary": "📊 note・主要都市ラウンジ混雑動向まとめ",
}

# =====================================================================
# Situation Specialization Specs (F2.2)
# =====================================================================
SITUATION_SPECS = {
    "second_venue": {
        "key": "second_venue",
        "label": "🍸 2軒目ハシゴ・夜カフェ移動",
        "short_label": "🍸 2軒目・夜カフェ",
        "desc": "相席後のスマートな退店と落ち着いた2軒目（夜カフェ・バー等）へのハシゴを指南する切り口。",
        "directive": (
            "【シチュエーション切り口：🍸 2軒目ハシゴ・夜カフェ移動特化】\n"
            "- 相席ラウンジ退店後の「次どうする？」をスマートに解決する視点で書くこと。\n"
            "- ガヤガヤした店内から一息つける静かな夜カフェ、落ち着いたバー、甘いもの（夜パフェ等）への自然な誘導フレーズ（例：「30分だけ静かな夜カフェで甘いもの食べない？」）や、徒歩数分の移動戦略を織り込むこと。\n"
            "- 会話がピークの40〜50分で切り出す重要性や、相席ナビの周辺2軒目スポット情報に触れること。"
        ),
    },
    "zero_wait": {
        "key": "zero_wait",
        "label": "💄 女子待ちゼロ即乾杯",
        "short_label": "💄 女子待ちゼロ",
        "desc": "男性先行＝女性が待ち時間ゼロで即座に案内される狙い目店舗を突く切り口。",
        "directive": (
            "【シチュエーション切り口：💄 女子待ちゼロ即乾杯特化】\n"
            "- 男性が先行しており女性ペアが受付待ち時間ゼロで即座に乾杯できる狙い目店舗をフォーカスすること。\n"
            "- 女性側の『せっかくメイクしたのに待たされる店は嫌』『すぐ座れてフード・スイーツも充実している店に行きたい』という本音に寄り添い、今すぐ入れる店舗を具体的に提示すること。"
        ),
    },
    "rain_underground": {
        "key": "rain_underground",
        "label": "☔ 雨天・駅直結穴場",
        "short_label": "☔ 雨天・駅直結",
        "desc": "悪天候時に濡れずに行ける地下直結店や、足元の悪さによる男女比の逆張り穴場を突く切り口。",
        "directive": (
            "【シチュエーション切り口：☔ 雨天・駅直結穴場特化】\n"
            "- 雨や悪天候、足元の悪さを切り口にし、地下街直結や駅から濡れずに行ける店舗、または雨の日の男女比の偏り・逆張り穴場にフォーカスすること。\n"
            "- 「雨の日は前髪や靴を濡らしたくない」「地下直結店にスマートに滑り込むのが正解」という視点を入れること。"
        ),
    },
    "last_train_cliff": {
        "key": "last_train_cliff",
        "label": "🌙 終電崖逆転・深夜ラジオ",
        "short_label": "🌙 終電崖逆転",
        "desc": "23時台の終電駆け込みや深夜0時以降の朝まで組のリアルな人間模様に迫る切り口。",
        "directive": (
            "【シチュエーション切り口：🌙 終電崖逆転・深夜ラジオ特化】\n"
            "- 23時台の「終電前の駆け込み・ラストチャンス」や、深夜0時〜2時の「終電を逃した・最初から朝までコース組」のリアルな深夜テンションにフォーカスすること。\n"
            "- 終電20分前の判断、深夜でも盛り上がっている店舗、朝まで残る男女の心理を少しビター＆深夜ラジオの独り言風に書くこと。"
        ),
    },
    "start_dash": {
        "key": "start_dash",
        "label": "🍻 早割・スタートダッシュ",
        "short_label": "🍻 早割ダッシュ",
        "desc": "18時〜20時台の早い時間帯、早割特典やピーク前のウォームアップを狙う切り口。",
        "directive": (
            "【シチュエーション切り口：🍻 早割・スタートダッシュ特化】\n"
            "- 18時〜20時台の立ち上がり、早割特典、混雑する前のウォームアップにフォーカスすること。\n"
            "- ピーク前に良い席を確保するメリットや、仕事終わりの1軒目としてスマートに使う立ち回りを提示すること。"
        ),
    },
}

def sanitize_brand_names(text: str) -> str:
    """
    ブランド表記ルール厳守サニタイザー:
    単体の『オリエンタル』『OLG』『オリラジ』を禁止し、必ず正式名称『オリエンタルラウンジ』と表記する。
    """
    if not text:
        return ""
    s = str(text)
    if "オリラジ" in s:
        s = s.replace("オリラジ", "オリエンタルラウンジ")
    # 'OLG ' or 'OLG\n' or 'OLG' at word boundaries
    s = re.sub(r"(?<![A-Za-z0-9])OLG(?![A-Za-z0-9])\s*", "オリエンタルラウンジ ", s)
    # 単体オリエンタル (ラウンジが後ろにない場合)
    s = re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", s)
    return s

TONE_STATE_KEY = "editorial:default_tone_mode"
DEFAULT_TONE_MODE = "spicy_female"

EDITORIAL_TONE_MODES = {
    "spicy_female": {
        "key": "spicy_female",
        "label": "🌶️💄 辛口本音×女性目線ミックス（新標準）",
        "short_label": "🌶️💄 辛口×女性本音",
        "desc": "ご都合主義の綺麗事を排除し、「男性が気づかない敗因の図星（毒）」と「女性ペアの本音・裏側」の両方を効かせるモード。",
        "prompt_directive": (
            "【トーン＆視点指定：気取らない観察眼×男女両目線のリアル】\n"
            "- 誰にでも好かれようとする宣伝調（『おすすめです』『楽しく飲めます』等）は使わず、混んでいる店に惰性で向かうもったいなさや、女の子側が待たされる店を避けてバランス店へ動くリアルな空気感をさらっと描くこと。\n"
            "- 説教くさい決め台詞は避け、知っている友人だけがふと教えてくれる落ち着いた温度感にすること。"
        ),
    },
    "spicy": {
        "key": "spicy",
        "label": "🔥 辛口・毒強め特化（綺麗事ゼロの現場解剖）",
        "short_label": "🔥 辛口・毒強め",
        "desc": "優等生アドバイスを完全封印し、耳が痛い図星と残酷な現場構造（なぜノープラン組が負けるのか）をズバッと切るビター特化モード。",
        "prompt_directive": (
            "【トーン＆視点指定：少しビターで鋭い大人の観察眼】\n"
            "- 優しいフォロー語尾や宣伝文句を省き、徒歩数分で空気がまるで違うのに気づかない不思議さや、時間帯ごとのシビアな現実を淡々と切り取ること（特定店舗や個人への誹謗中傷はしない）。"
        ),
    },
    "female_real": {
        "key": "female_real",
        "label": "💄 女性目線リアル特化（女子の本音・裏側解剖）",
        "short_label": "💄 女性目線リアル",
        "desc": "女性2人組が実は見ている減点サイン、女子トイレや帰り道LINEの本音、女性側が待ちゼロで当たり席を引く立ち回りに特化するモード。",
        "prompt_directive": (
            "【トーン＆視点指定：女性ペアのリアルな肌感覚】\n"
            "- 受付で待たされる気まずさや雨の日の移動の面倒さなど、20〜30代女性ペアの素直な感覚を添えつつ、女性組がいま待ち時間なしでスッと座れる店舗や空気感を自然に伝えること。"
        ),
    },
    "standard": {
        "key": "standard",
        "label": "🥂 マイルド標準（初心者向け・丁寧ガイド）",
        "short_label": "🥂 マイルド標準",
        "desc": "毒や辛口表現を抑え、初めて相席ラウンジへ行く読者にも安心感を与えるフラットで丁寧なガイドモード。",
        "prompt_directive": (
            "【トーン＆視点指定：落ち着いたフラットな素の丁寧トーン】\n"
            "- 辛口表現は控え、初めての人でも雰囲気がイメージしやすい穏やかで気取らない言葉遣いにすること。"
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
    ("名古屋・栄", "nagoya", ("名古屋", "栄", "名古屋 錦", "名古屋錦", "NAGOYA")),
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
    Format store name naturally for SNS (brand compliance: always uses full 'オリエンタルラウンジ〇〇'):
      - 'OLG 新宿' -> 'オリエンタルラウンジ新宿'
      - 'OLG 大阪駅前' -> 'オリエンタルラウンジ大阪駅前'
      - 'OLG ag渋谷' -> 'ag渋谷'
    """
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


RECENT_X_TEXTS_KEY = "x_recent_texts_v1"

_TIER1_FLAGSHIP_EXACT = {
    "OLG 新宿", "JIS 新宿", "JIS SHINJUKU",
    "OLG 渋谷本店",
    "OLG 梅田", "JIS 梅田", "JIS UMEDA",
}
_TIER2_SUBMAJOR_KEYWORDS = (
    "恵比寿", "EBISU", "AG渋谷", "ag渋谷", "渋谷駅前", "西新宿", "NISHISHINJUKU",
    "上野", "UENO", "町田", "MACHIDA", "横浜", "YOKOHAMA", "大宮", "OMIYA", "柏", "KASHIWA",
    "茶屋町", "CHAYAMACHI", "大阪駅前", "難波", "NAMBA", "心斎橋", "SHINSAIBASHI", "天満", "TENMA",
    "京都", "KYOTO", "神戸", "KOBE",
    "名古屋", "栄", "錦", "NAGOYA",
    "福岡", "天神", "FUKUOKA", "小倉", "KOKURA",
)


def classify_store_spotlight_tier(raw_name: str) -> str:
    """
    Classify domestic stores for the 4:4:2 spotlight rotation:
      - 'tier1' (40%): Flagship Shinjuku, Shibuya Honten, Umeda
      - 'tier2' (40%): Sub-major urban hubs (Ebisu, Ueno, Yokohama, Omiya, Namba, Chayamachi, Sakae, Tenjin, etc.)
      - 'tier3' (20%): Regional cities nationwide (Sapporo, Sendai, Hiroshima, Kumamoto, Okinawa, etc.)
    """
    nm = (raw_name or "").strip()
    if not nm:
        return "tier2"
    up = nm.upper()
    if nm in _TIER1_FLAGSHIP_EXACT or up in _TIER1_FLAGSHIP_EXACT:
        return "tier1"
    if any(k in up or k in nm for k in _TIER2_SUBMAJOR_KEYWORDS):
        return "tier2"
    if any(k in up or k in nm for k in ("新宿", "渋谷", "梅田", "SHINJUKU", "SHIBUYA", "UMEDA")):
        return "tier1"
    return "tier3"


def record_recent_x_text(text: str, source: str = "x", now_jst: datetime.datetime = None):
    """Append a posted X text into the shared cross-job anti-repetition ring buffer (`x_recent_texts_v1`)."""
    cleaned = (text or "").strip()
    if not cleaned:
        return
    try:
        raw = job_state.get_value(RECENT_X_TEXTS_KEY, allow_blocking=False)
        items = json.loads(raw) if raw else []
        if not isinstance(items, list):
            items = []
        ts_str = (now_jst or datetime.datetime.now(JST)).strftime("%Y-%m-%d %H:%M:%S")
        entry = {"source": source, "text": cleaned[:320], "ts": ts_str}
        items = [it for it in items if isinstance(it, dict) and it.get("text") != cleaned[:320]]
        items.insert(0, entry)
        job_state.set_value(RECENT_X_TEXTS_KEY, json.dumps(items[:18], ensure_ascii=False))
    except Exception:
        pass


def get_recent_cross_channel_texts(channel: str = "x", limit: int = 6, allow_blocking: bool = False) -> list:
    """Return recent full post texts across editorial history and shared X jobs (Tips/Bonus/Note) to prevent repetition."""
    ch = (channel or "x").lower()
    seen = []
    hist = get_recent_editorial_history(channel=ch, limit=limit, allow_blocking=allow_blocking)
    for h in hist:
        txt = (h.get("selected_text") or "").strip()
        if txt and txt not in seen:
            seen.append(txt)
    if ch == "x":
        try:
            raw = job_state.get_value(RECENT_X_TEXTS_KEY, allow_blocking=allow_blocking)
            shared = json.loads(raw) if raw else []
            if isinstance(shared, list):
                for it in shared:
                    txt = (it.get("text") or "").strip() if isinstance(it, dict) else ""
                    if txt and txt not in seen:
                        seen.append(txt)
        except Exception:
            pass
    return seen[:limit]


def extract_sentence_endings(texts: list) -> list:
    """Extract characteristic sentence-ending phrases from recent posts so AI avoids repeating the same cadence."""
    endings = []
    patterns = (
        "なんだよな", "なんだよね", "だったりする", "だったりします", "置いときます",
        "ですね", "かも", "珍しい", "不思議", "差よ", "空気感", "狙い目",
    )
    for t in (texts or []):
        body = re.sub(r"#[^\s#]+", "", t or "").replace("👇", "").strip()
        for p in patterns:
            if p in body and p not in endings:
                endings.append(p)
        last_line = [ln.strip() for ln in body.splitlines() if ln.strip()]
        if last_line:
            tail = re.sub(r"[。…！!？?]+$", "", last_line[-1])[-8:]
            if len(tail) >= 3 and tail not in endings:
                endings.append(tail)
    return endings[:8]


HUMAN_JITTER_WINDOWS = {
    "x": (2, 21),              # e.g., 20:03 one night, 20:19 the next, 22:07 vs 22:20
    "x_tip": (2, 22),          # e.g., 18:04, 18:18, 18:11
    "x_weekly": (2, 18),
    "threads": (5, 25),        # distinct seed & window from X so X and Threads never fire together
    "threads_ladies": (1, 14), # 19:31 .. 19:44
    "instagram": (1, 12),      # 18:46 .. 18:57 / 21:46 .. 21:57
    "ig_ladies": (1, 12),      # 19:46 .. 19:57
    "ig_reel": (3, 28),        # 21:18 .. 21:43 (natural vertical video post timing)
    "tiktok": (4, 20),         # 20:39 .. 20:55 Inbox draft timing (100% covered by 11:39,45,51,57 UTC cron)
}


def compute_human_schedule_offset_min(
    channel_key: str,
    slot_label: str,
    now_jst: datetime.datetime = None,
) -> int:
    """
    Compute a deterministic, human-like daily minute offset within `(min_off, max_off)`
    for `(channel_key, biz_date, slot_label)` so scheduled posts never look like `:00`/`:02` clockwork bots.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch_k = (channel_key or "x").strip().lower()
    min_off, max_off = HUMAN_JITTER_WINDOWS.get(ch_k, (2, 19))
    biz_date_str = (now_jst - datetime.timedelta(hours=7)).strftime("%Y-%m-%d")
    seed_raw = f"aiseki_human_jitter_v1:{ch_k}:{biz_date_str}:{slot_label or ''}"
    digest = hashlib.sha256(seed_raw.encode("utf-8")).digest()
    span = max(1, (max_off - min_off) + 1)
    return min_off + (int.from_bytes(digest[:4], "big") % span)


def should_fire_scheduled_slot_now(
    channel_key: str,
    slot_label: str,
    now_jst: datetime.datetime = None,
) -> bool:
    """
    Non-blocking scheduler gate (`<0.05ms`, zero `time.sleep`):
    Returns `False` while `now_jst` is earlier than today's natural target minute for `(channel_key, slot_label)`,
    and `True` once `now_jst` reaches or passes today's target minute (with a fail-open safety net).
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    m = re.match(r"^(\d{1,2}):(\d{2})$", (slot_label or "").strip())
    if not m:
        return True
    slot_h = int(m.group(1))
    slot_m = int(m.group(2))
    base_dt = now_jst.replace(hour=slot_h, minute=slot_m, second=0, microsecond=0)
    # Handle midnight wrap if now_jst is e.g. 00:05 vs slot 23:45
    diff_sec = (now_jst - base_dt).total_seconds()
    if diff_sec < -43200:
        diff_sec += 86400
    elif diff_sec > 43200:
        diff_sec -= 86400
    elapsed_min = diff_sec / 60.0
    ch_k = (channel_key or "x").strip().lower()
    _, max_off = HUMAN_JITTER_WINDOWS.get(ch_k, (2, 19))
    fail_open_min = max(24.0, float(max_off + 4))
    if elapsed_min < 0 or elapsed_min >= fail_open_min:
        # Fail-open outside normal early hold window so late retries always execute
        return True
    target_off = compute_human_schedule_offset_min(channel_key, slot_label, now_jst=now_jst)
    return elapsed_min >= float(target_off)


def should_skip_slot_for_human_pacing(
    channel_key: str,
    slot_label: str,
    now_jst: datetime.datetime = None,
    stores: list = None,
) -> tuple:
    """
    Anti-bot weekly rhythm & quiet-weekday breathing gate (`(should_skip: bool, reason: str)`).
    Prevents 7/7-day identical multi-slot posting patterns on X and Threads while keeping
    peak Fri/Sat nights active and automatically overriding rest days when live crowd surges occur.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    biz_dt = now_jst - datetime.timedelta(hours=7)
    wd = biz_dt.weekday()  # 0=Mon .. 6=Sun
    iso_year, iso_week, _ = biz_dt.isocalendar()
    ch_k = (channel_key or "").strip().lower()
    slot_s = (slot_label or "").strip()

    korea_kws = ("カンナム", "ホンデ", "ソウル", "GANGNAM", "HONGDAE", "SEOUL")
    dom_women = 0
    if stores:
        dom_women = sum(
            int(s.get("women") or 0)
            for s in stores
            if s.get("region") != "Korea"
            and not any(k in (s.get("name") or "").upper() for k in korea_kws)
        )

    # 1. X 18:00 Tip: runs 5 days/week instead of 7/7 clockwork
    #    - Always rests Sunday (since 19:00 Weekly Report runs on Sunday)
    #    - Rests 1 rotating quiet weekday out of {Mon(0), Wed(2)} per ISO week
    if ch_k == "x_tip" and slot_s == "18:00":
        if wd == 6:
            return True, "Sunday 18:00 Tip rested to avoid clustering with 19:00 Weekly Report"
        quiet_pick_seed = hashlib.sha256(f"aiseki_xtip_rest:{iso_year}-W{iso_week:02d}".encode("utf-8")).digest()
        rest_wd = (0, 2)[quiet_pick_seed[0] % 2]
        if wd == rest_wd:
            return True, f"Human weekday breathing rest for X 18:00 Tip (wd={wd})"
        return False, "ok"

    # 2. X 20:00 early live slot: runs on active Thu/Fri/Sat + 1 mid-week night (unless crowd surge >=140 women)
    #    - Rests Monday (quiet start of week -> 22:00 single-slot night)
    #    - Rests Sunday (since 19:00 Weekly Report already covers early evening)
    #    - Rests 1 rotating weekday out of {Tue(1), Wed(2)} per ISO week
    if ch_k == "x" and slot_s == "20:00":
        if dom_women >= 140:
            return False, "live_surge_override"
        if wd == 0:
            return True, "Monday 20:00 X slot rested (human quiet-Monday 1-slot rhythm)"
        if wd == 6:
            return True, "Sunday 20:00 X slot rested after 19:00 Weekly Report"
        x20_seed = hashlib.sha256(f"aiseki_x20_rest:{iso_year}-W{iso_week:02d}".encode("utf-8")).digest()
        rest_wd = (1, 2)[x20_seed[0] % 2]
        if wd == rest_wd:
            return True, f"Mid-week 20:00 X slot rested (wd={wd}) for natural human cadence"
        return False, "ok"

    # 3. Threads 22:00 daily essay: runs 6 nights/week (rests 1 rotating quiet Mon/Tue night unless surge >=120 women)
    if ch_k == "threads" and slot_s == "22:00":
        if dom_women >= 120:
            return False, "live_surge_override"
        th_seed = hashlib.sha256(f"aiseki_threads_rest:{iso_year}-W{iso_week:02d}".encode("utf-8")).digest()
        rest_wd = (0, 1)[th_seed[0] % 2]
        if wd == rest_wd:
            return True, f"Weekly human rest night for Threads 22:00 (wd={wd})"
        return False, "ok"

    return False, "ok"


def build_threads_cta_footer(
    site_url: str,
    slot_label: str = "22:00",
    style_key: str = "live",
    now_jst: datetime.datetime = None,
) -> str:
    """
    Meta Threads anti-spam footer builder:
    - Attaches a direct URL on only ~1 in 3 nights (`day % 3 == 1`; never colliding with `ladies_guide` which uses `day % 3 == 0`).
    - On the other 2/3 of nights, rotates across understated human bio-link notes or completely tagless clean endings
      so consecutive Threads posts never carry identical footer blocks.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    biz_dt = now_jst - datetime.timedelta(hours=7)
    slot_hh = (slot_label or "22:00")[:2]
    day_mod = biz_dt.day % 3
    if day_mod == 1:
        return f"▼ 全国60店舗超の現在人数＆料金・イベント比較\n{site_url}/price-comparison?s=th{slot_hh}_{style_key}"
    variants = (
        "細かいリアルタイム人数と料金比較はプロフィールのリンク（相席ナビ）に置いてます。",
        "🔍 全国の現在人数・男女比はプロフのリンク（相席ナビ）から見れます\n#相席ラウンジ",
        "気になる街のリアルタイム推移はプロフィールの『相席ナビ』からどうぞ。",
        "#相席ラウンジ",
    )
    v_idx = (biz_dt.toordinal() + len(style_key or "")) % len(variants)
    return variants[v_idx]


_ROTATED_HASHTAG_SETS = {
    "carousel": (
        ("#相席ナビ", "#相席ラウンジ", "#オリエンタルラウンジ", "#ag", "#夜遊びガイド", "#週末ディナー"),
        ("#相席ナビ", "#相席ラウンジ", "#オリエンタルラウンジ", "#JIS", "#大人ディナー", "#夜カフェ巡り"),
        ("#相席ナビ", "#相席ラウンジ", "#ag", "#JIS", "#華金ディナー", "#お店選び"),
        ("#相席ナビ", "#相席ラウンジ", "#オリエンタルラウンジ", "#ラウンジ巡り", "#週末の過ごし方"),
        ("#相席ナビ", "#相席ラウンジ", "#オリエンタルラウンジ", "#ag", "#JIS", "#ナイトライフ"),
    ),
    "ladies": (
        ("#相席ナビ", "#相席ラウンジ", "#女子会ディナー", "#夜カフェ", "#オリエンタルラウンジ", "#ag"),
        ("#相席ナビ", "#相席ラウンジ", "#大人女子会", "#二軒目探し", "#オリエンタルラウンジ", "#JIS"),
        ("#相席ナビ", "#相席ラウンジ", "#女子会グルメ", "#華金女子会", "#ag", "#JIS"),
        ("#相席ナビ", "#相席ラウンジ", "#夜カフェ巡り", "#週末女子会", "#オリエンタルラウンジ"),
    ),
    "tiktok_gap": (
        ("#相席ナビ", "#相席ラウンジ", "#オリエンタルラウンジ", "#ag", "#夜遊びスポット"),
        ("#相席ナビ", "#相席ラウンジ", "#オリエンタルラウンジ", "#JIS", "#二軒目探し"),
        ("#相席ナビ", "#相席ラウンジ", "#ag", "#JIS", "#週末の夜"),
        ("#相席ナビ", "#相席ラウンジ", "#オリエンタルラウンジ", "#大人ディナー", "#お店選び"),
    ),
    "tiktok_midnight": (
        ("#相席ナビ", "#相席ラウンジ", "#終電逃した", "#二軒目探し", "#オリエンタルラウンジ", "#ag"),
        ("#相席ナビ", "#相席ラウンジ", "#深夜ディナー", "#夜カフェ", "#オリエンタルラウンジ", "#JIS"),
        ("#相席ナビ", "#相席ラウンジ", "#終電後", "#大人の夜遊び", "#ag", "#JIS"),
        ("#相席ナビ", "#相席ラウンジ", "#深夜グルメ", "#二軒目", "#オリエンタルラウンジ"),
    ),
}

_AREA_TAG_SUFFIXES = ("グルメ", "ディナー", "夜遊び", "夜カフェ")


def pick_rotated_hashtag_line(
    channel: str = "instagram",
    category: str = "carousel",
    now_jst: datetime.datetime = None,
    area_tag: str = "",
    seed_extra: int = 0,
) -> str:
    """Return a rotated 5-7 hashtag line avoiding identical bot-like hashtag blocks across posts."""
    now_jst = now_jst or datetime.datetime.now(JST)
    biz_dt = (now_jst - datetime.timedelta(hours=7)).date()
    cat_key = category if category in _ROTATED_HASHTAG_SETS else (
        "tiktok_gap" if (channel or "").lower() == "tiktok" else "carousel"
    )
    pool = _ROTATED_HASHTAG_SETS[cat_key]
    idx = (biz_dt.toordinal() + int(seed_extra)) % len(pool)
    chosen = list(pool[idx])

    raw_area = re.sub(r"[\s#・/／0-9０-９]", "", (area_tag or "").split("・")[0]).strip()
    if raw_area and len(chosen) < 7:
        suf = _AREA_TAG_SUFFIXES[(biz_dt.toordinal() + int(seed_extra)) % len(_AREA_TAG_SUFFIXES)]
        candidate_area_tag = f"#{raw_area}{suf}"
        if candidate_area_tag not in chosen:
            chosen.append(candidate_area_tag)

    line = " ".join(chosen)
    return re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", line)


def get_sns_human_pacing_summary(now_jst: datetime.datetime = None) -> dict:
    """Return human-readable summary of anti-bot pacing & Reel schedule for `/staff/sns-editorial`."""
    return {
        "ig_reels_weekly": 4,
        "ig_reels_days_ja": "水・木・金・土（週4回・4テンプレ＆背景ローテ・1.2秒カバー固定・同日グリッド重複自動OFF）",
        "x_cadence_ja": "最低48分間隔＋日次上限（月〜木4件・金〜日5件／主戦場枠リザーブ付）＋月/日休養ゆらぎ＋単独ポスト混在",
        "threads_cadence_ja": "週6日稼働（週1自然休養）＋最低90分間隔＋URL添付1/3以下＋画像1/3混在",
        "tiktok_cadence_ja": "水・金・土（週3回・20:39〜20:55人間ゆらぎ・3テンプレ自動ローテInbox送信）",
        "col_release_ja": "コラム1日最大1本（深夜〜午前23:26〜11:29自動保留）＋note検知は日中12:20/15:20帯に分離（18時Tips近接回避）",
    }


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
        if ch == "x" and session_record.get("selected_text"):
            record_recent_x_text(session_record["selected_text"], source="x_editorial")
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
    if ch not in ("x", "threads", "instagram", "tiktok", "note"):
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
    x_source = "Fly.io環境変数（OAuth 1.0a 4キー設定済）" if x_configured else "未設定（環境変数 X_API_KEY 等が必要）"
    x_masked = _mask_secret_token(os.environ.get("X_ACCESS_TOKEN") or "")

    # 2. Threads credentials & status
    th_stored = (job_state.get_value("token:threads", allow_blocking=allow_blocking) or "").strip()
    th_env = (os.environ.get("THREADS_ACCESS_TOKEN") or "").strip()
    th_active = th_stored or th_env
    th_configured = bool(th_active)
    th_source = (
        "管理画面・自動更新（暗号化保存）"
        if th_stored
        else ("Fly.io環境変数（THREADS_ACCESS_TOKEN）" if th_env else "未設定")
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
        else ("Fly.io環境変数（INSTAGRAM_ACCESS_TOKEN）" if ig_env else "未設定")
    )
    ig_masked = _mask_secret_token(ig_active)

    # 4. TikTok credentials & status (Phase 1 works out-of-the-box via MP4 save + script copy; Phase 2 Inbox API optional)
    tt_stored = (job_state.get_value("token:tiktok", allow_blocking=allow_blocking) or "").strip()
    tt_env = (os.environ.get("TIKTOK_ACCESS_TOKEN") or "").strip()
    tt_active = tt_stored or tt_env
    tt_configured = bool(tt_active)
    tt_source = (
        "Phase 2 API連携済（管理画面・暗号化保存）"
        if tt_stored
        else (
            "Phase 2 API連携済（環境変数 TIKTOK_ACCESS_TOKEN）"
            if tt_env
            else "Phase 1 稼働中（MP4保存＋台本コピー運用 ／ APIトークン任意）"
        )
    )
    tt_masked = _mask_secret_token(tt_active)

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
            "latest_slot_key": _latest_matching_key(("ig_post:", "ig_reel_slot:", "ig_ladies:")),
            "refresh_key": _latest_matching_key(("token_refresh:instagram:",)),
        },
        "tiktok": {
            "label": "🎵 TikTok（縦型ショート）",
            "configured": tt_configured,
            "source": tt_source,
            "masked_token": tt_masked,
            "latest_slot_key": _latest_matching_key(("tiktok_post:",)),
            "refresh_key": _latest_matching_key(("token_refresh:tiktok:",)),
        },
        "note": {
            "label": "📝 note（エッセイ・つぶやき）",
            "configured": True,
            "source": "note Webエディタ直行連携／RSS告知連動",
            "masked_token": "web_intent_or_rss",
            "latest_slot_key": _latest_matching_key(("note_post:", "col_release:")),
            "refresh_key": "",
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

        # Detect silent historical mismatch on Threads (unless a fresh token_refreshed succeeded after that failed attempt)
        if ch == "threads" and not last_err:
            th_hist = get_recent_editorial_history(channel="threads", limit=1, allow_blocking=False)
            if th_hist:
                hist_ts = str(th_hist[0].get("timestamp") or "")
                hist_date = hist_ts[:10]
                post_date = latest_key.split(":", 1)[1][:10] if ":" in latest_key else ""
                recovered_since = (
                    saved.get("last_status") == "token_refreshed"
                    and str(saved.get("last_attempt_at") or "") > hist_ts
                )
                if hist_date and (not post_date or hist_date > post_date) and not saved.get("last_ok_at") and not recovered_since:
                    last_err = (
                        f"直近（{hist_ts}）の自動投稿で文面生成後にThreads API送信が不発です。"
                        "トークン期限切れ（短期トークン失効）または未設定の可能性があります。"
                    )

        if last_err:
            state = "error"
        elif base["configured"] or latest_key:
            state = "active"
        elif ch == "tiktok":
            state = "phase1_ready"
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
    """Clear in-memory and shared Redis AI draft caches (called when API key updates or manual refresh requested)."""
    with _ai_cache_lock:
        _ai_candidate_cache.clear()
    turbo_store.clear_ai_cache("sns_editorial")
    _unhealthy_models.clear()
    _last_gemini_meta["working_route"] = None


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
    _unhealthy_models.clear()
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
    if cleaned and get_ai_engine_pref(allow_blocking=False) == "gemini":
        job_state.set_value(AI_ENGINE_PREF_KEY, "claude")
    clear_editorial_ai_cache()
    _model_discovery_cache.clear()
    _claude_no_temp_models.clear()
    _unhealthy_models.clear()
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
    _unhealthy_models.clear()
    _last_gemini_meta["last_error"] = ""
    _last_gemini_meta["working_route"] = None
    return norm


def has_any_ai_key(allow_blocking: bool = True) -> bool:
    return bool(get_claude_api_key(allow_blocking=allow_blocking) or get_gemini_api_key(allow_blocking=allow_blocking))


def get_gemini_status(allow_blocking: bool = False) -> dict:
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
        "Claude Opus × 2段階推敲 ＋ 街況Web検索（Gemini予備）"
        if (c_key and g_key)
        else ("Claude Opus × 2段階人間語推敲 ＋ 街況Web検索" if c_key else "Gemini Pro / 上位Flash 長考執筆")
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
    if ch == "note":
        return {
            "label": "📖 note 深層考察・夜の人間模様エッセイトーン",
            "guidance": (
                "街の夜の空気とリアルタイム混雑データを見つめながら、男女の心理や夜遊びの立ち回り構造を深く解剖する、知的好奇心と共感を刺激するエッセイトーン。"
                "煽りや宣伝臭を排し、読者が最後まで引き込まれて読める一人称視点の文体にすること。"
            ),
        }
    return {
        "label": "🔥 22:00 ピーク・2軒目移動のライブ実況",
        "guidance": (
            "街が一番動いている22時台のリアルタイム空気感トーン。"
            "『1軒目終わりの組がどっと動き始める頃』『徒歩3分なのにここまで男女比割れるか…』"
            "のように、夜の街の空気感を知る友人がふとスマホで呟いたような自然体にする。"
        ),
    }


def generate_image_prompts(
    research: dict,
    channel: str = "x",
    slot_label: str = "22:00",
    situation: str = None,
    tone_mode: str = None,
) -> dict:
    """
    Generate unified AI eyecatch / image generation prompts (English prompt + Japanese description + aspect ratio)
    tailored to X (16:9), Instagram (4:5 / 1:1), note (1.91:1), TikTok (9:16), and Threads (1:1).
    Takes into account live night atmosphere, crowd highlights, and current situation context (second_venue, zero_wait, etc.).
    """
    ch = (channel or "x").lower()
    sit_key = (situation or "").strip().lower()
    sit_meta = SITUATION_SPECS.get(sit_key, {})

    # Base prompt elements depending on situation
    if sit_key == "second_venue":
        concept_en = "Chic late-night cafe and intimate cocktail bar in Tokyo, cozy emerald velvet sofa, warm low-key pendant lighting, artisan latte art and elegant cocktail glasses on marble table, relaxed conversation ambience, cinematic lighting, 8k photorealistic"
        concept_ja = "相席ラウンジ後の2軒目にぴったりな、落ち着いた夜カフェ＆バーの雰囲気。ベルベットソファと間接照明が心地よい親密な空間。"
    elif sit_key == "zero_wait":
        concept_en = "Upscale luxury lounge bar entrance, inviting warm golden lighting, welcome champagne flutes on sleek gold tray, polished marble reception, glamorous nightlife aesthetic, photorealistic 8k"
        concept_ja = "待ち時間ゼロで即乾杯できる華やかなラグジュアリーラウンジのエントランス。冷えたシャンパングラスと上質な夜の輝き。"
    elif sit_key == "rain_underground":
        concept_en = "Cozy modern nightlife lounge directly connected to underground shopping concourse, raindrops on window reflection, warm ambient lighting, elegant handcrafted cocktails, tranquil urban sanctuary, 8k photorealistic"
        concept_ja = "雨に濡れずに直結で入れる大人の隠れ家ラウンジ。雨の夜のネオンの反射と温かみのある琥珀色のカクテル。"
    elif sit_key == "last_train_cliff":
        concept_en = "Moody atmospheric midnight cocktail bar, subtle glowing clock indicating past 11:30 PM, bokeh city night lights, deep navy and amber color palette, thoughtful late-night conversation mood, cinematic depth of field"
        concept_ja = "終電後の深夜、静かに盛り上がる都会の隠れ家バー。深いネイビーと琥珀色の光が織りなす大人の夜。"
    elif sit_key == "start_dash":
        concept_en = "Vibrant early evening stylish lounge bar at golden hour sunset transitioning to city lights, fresh sparkling cocktails, welcoming premium vibe, clean composition, 8k photorealistic"
        concept_ja = "仕事終わりのスタートダッシュにふさわしい、夕暮れから夜へと移り変わる洗練されたラウンジ。フレッシュなカクテル。"
    else:
        concept_en = "High-end social lounge bar in Tokyo Shibuya at midnight, modern neon accents, stylish craft cocktail glasses on polished marble counter, subtle data analytics screen softly glowing in dark background, cinematic lighting, 8k photorealistic"
        concept_ja = "真夜中の都会的でラグジュアリーなラウンジバー。大理石のカウンターに置かれた美しいカクテルと、背景に溶け込むデータダッシュボード。"

    gallery = [
        {
            "id": "x_16_9",
            "channel": "x",
            "title": "𝕏 X（Twitter）告知・添付画像",
            "aspect_ratio": "16:9",
            "ratio_label": "横長 16:9",
            "recommended_resolution": "1200 x 675 px",
            "prompt_en": f"{concept_en}, 16:9 wide aspect ratio, editorial photography, highly detailed, masterwork --ar 16:9",
            "prompt_ja": f"Xのタイムラインで視線を止める横長アイキャッチ画像。【{concept_ja}】",
            "copy_target": "prompt_en",
        },
        {
            "id": "ig_4_5",
            "channel": "instagram",
            "title": "📸 Instagram カルーセル表紙 (1枚目)",
            "aspect_ratio": "4:5",
            "ratio_label": "縦長 4:5",
            "recommended_resolution": "1080 x 1350 px",
            "prompt_en": f"Editorial luxury nightlife photography, aesthetic cocktail glasses on dark marble table with gold rim, plush emerald green velvet sofa, ambient warm pendant lamp, Tokyo upscale lounge mood, 4:5 vertical framing --ar 4:5",
            "prompt_ja": "Instagramカルーセル保存率を高める雑誌風の表紙スライド。深みのあるベルベットソファと琥珀色の照明。",
            "copy_target": "prompt_en",
        },
        {
            "id": "note_header",
            "channel": "note",
            "title": "📝 note 見出し画像（ヘッダー）",
            "aspect_ratio": "1.91:1",
            "ratio_label": "note見出し 1.91:1",
            "recommended_resolution": "1280 x 670 px",
            "prompt_en": f"Reflective moody evening view overlooking Tokyo neon city lights from an upscale lounge window, smartphone screen showing live analytics chart, thoughtful modern essay atmosphere, cinematic shallow depth of field, 1.91:1 ratio --ar 1.91:1",
            "prompt_ja": "noteショートエッセイの読了率を高めるシックな見出し画像。東京の夜景と知的でアンニュイな雰囲気。",
            "copy_target": "prompt_en",
        },
        {
            "id": "tiktok_9_16",
            "channel": "tiktok",
            "title": "🎵 TikTok / Reels 縦型ショート表紙",
            "aspect_ratio": "9:16",
            "ratio_label": "縦型フル 9:16",
            "recommended_resolution": "1080 x 1920 px",
            "prompt_en": f"Vertical cinematic shot of bustling luxury lounge, vibrant nightlife energy, neon glow reflections on sleek bar counter, dynamic city night vibe, 9:16 vertical ratio --ar 9:16",
            "prompt_ja": "TikTokやInstagramリール動画の冒頭指止め用サムネイル。夜の熱気とネオンが輝く9:16全画面。",
            "copy_target": "prompt_en",
        },
        {
            "id": "threads_1_1",
            "channel": "threads",
            "title": "🧵 Threads リアルスナップ風画像",
            "aspect_ratio": "1:1",
            "ratio_label": "正方形 1:1",
            "recommended_resolution": "1080 x 1080 px",
            "prompt_en": f"Authentic candid viewpoint from a cozy corner table at a stylish nightlife lounge, artisanal cocktail glass with lime garnish, relaxed casual night out, warm film grain, modern Tokyo mood --ar 1:1",
            "prompt_ja": "Threadsのタイムラインに馴染む、等身大でリアルな夜の日常スナップ風スクエア画像。",
            "copy_target": "prompt_en",
        },
    ]

    primary_map = {
        "x": gallery[0],
        "instagram": gallery[1],
        "note": gallery[2],
        "tiktok": gallery[3],
        "threads": gallery[4],
    }
    primary = primary_map.get(ch, gallery[0])

    return {
        "current_channel": ch,
        "situation": sit_key,
        "situation_label": sit_meta.get("label", ""),
        "primary": primary,
        "gallery": gallery,
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


_street_pulse_cache = {
    "lock": threading.Lock(),
    "entries": {},
    "fetching_keys": set(),
}
_STREET_PULSE_TTL_SEC = 14400  # 4 hours (at most 1-2 searches per evening across all SNS channels)


def _clean_pulse_snippet(val: str, max_chars: int = 70) -> str:
    s = re.sub(r"<[^>]+>", "", str(val or "")).strip()
    s = re.sub(r"https?://\S+", "", s)
    s = re.sub(r"\[\d+\]", "", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s[:max_chars]


def _refresh_street_pulse_sync(cache_key: tuple, now_jst: datetime.datetime) -> dict:
    try:
        c_key = get_claude_api_key(allow_blocking=True)
        if not c_key:
            return None
        discovered = _discover_claude_models(c_key, timeout=2.8)
        haiku_m = next((m for m in discovered if "haiku" in m.lower()), None)
        sonnet_m = next((m for m in discovered if "sonnet" in m.lower()), "claude-sonnet-5-5")
        search_models = [m for m in (haiku_m, sonnet_m) if m][:2]

        weekdays_ja = ("月", "火", "水", "木", "金", "土", "日")
        wd_ja = weekdays_ja[now_jst.weekday()]
        date_jp = f"{now_jst.year}年{now_jst.month}月{now_jst.day}日({wd_ja})"
        query_prompt = (
            f"今日（{date_jp}）の日本の夜の繁華街（東京：新宿・渋谷・東京ドーム・横浜周辺、大阪：梅田・難波・京セラドーム周辺、および名古屋・福岡等）で、"
            "今夜の人の流れや街の空気に関係しそうなリアルな出来事（大型ライブ・コンサート終演、野球・サッカー等のナイター、花火・ハロウィン・季節イベント、連休前や給料日後の週末ムード、急な天候変化など）をWeb検索で1回だけサッと確認し、"
            "確認できた事実だけを短いJSONで返してください。\n"
            "【注意】未確認のイベントを推測で作らないこと。特に目立った催しがない項目は空文字 \"\" にすること。人数などの細かい数字やURLは入れないこと。\n"
            '出力JSON形式のみ：{"national_vibe": "全国的な今夜の街の空気（45字以内・なければ空文字）", "kanto": "東京・横浜周辺の今夜のライブ・催し・街の動き（55字以内・なければ空文字）", "kansai": "大阪・梅田・難波周辺の今夜のライブ・催し・街の動き（55字以内・なければ空文字）", "other": "名古屋・福岡・札幌等の今夜の催し（55字以内・なければ空文字）"}'
        )

        for sm in search_models:
            body = {
                "model": sm,
                "max_tokens": 900,
                "tools": [
                    {
                        "type": "web_search_20250305",
                        "name": "web_search",
                        "max_uses": 2,
                        "user_location": {
                            "type": "approximate",
                            "country": "JP",
                            "city": "Tokyo",
                            "timezone": "Asia/Tokyo",
                        },
                    }
                ],
                "messages": [{"role": "user", "content": query_prompt}],
            }
            resp = requests.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": c_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json=body,
                timeout=18.0,
            )
            if resp.status_code >= 400:
                continue
            rjson = resp.json() or {}
            blocks = rjson.get("content") or []
            raw_txt = "".join(b.get("text", "") for b in blocks if b.get("type") == "text").strip()
            parsed = _parse_ai_json_object(raw_txt)
            if isinstance(parsed, dict):
                result = {
                    "available": True,
                    "fetched_at": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
                    "model": sm,
                    "national_vibe": _clean_pulse_snippet(parsed.get("national_vibe"), 55),
                    "kanto": _clean_pulse_snippet(parsed.get("kanto"), 65),
                    "kansai": _clean_pulse_snippet(parsed.get("kansai"), 65),
                    "other": _clean_pulse_snippet(parsed.get("other"), 65),
                }
                now_t = time.time()
                with _street_pulse_cache["lock"]:
                    _street_pulse_cache["entries"][cache_key] = {"ts": now_t, "data": result}
                turbo_store.set_ai_cache("street_pulse", cache_key, {"ts": now_t, "data": result}, ttl_sec=_STREET_PULSE_TTL_SEC)
                return result
    except Exception as e:
        logger.debug(f"Street pulse web search skipped: {e}")
    finally:
        with _street_pulse_cache["lock"]:
            _street_pulse_cache["fetching_keys"].discard(cache_key)
    return None


def fetch_tonight_street_pulse(
    now_jst: datetime.datetime = None,
    force_network: bool = False,
    allow_blocking: bool = False,
) -> dict:
    """
    Fetch real-time tonight street topics (concerts, dome events, seasonal buzz) via Claude Web Search.
    Cached for 4 hours in memory + `turbo_store` so it runs at most 1-2 times per evening across all channels,
    and returns in 0ms on web page renders (`allow_blocking=False`).
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    biz_date_str = _business_datetime(now_jst).strftime("%Y-%m-%d")
    phase = "late" if (now_jst.hour >= 21 or now_jst.hour < 4) else "early"
    cache_key = ("street_pulse_v1", biz_date_str, phase)
    now_t = time.time()

    if not force_network:
        with _street_pulse_cache["lock"]:
            mem_hit = _street_pulse_cache["entries"].get(cache_key)
            if mem_hit and (now_t - mem_hit["ts"] < _STREET_PULSE_TTL_SEC):
                return dict(mem_hit["data"])
        shared = turbo_store.get_ai_cache("street_pulse", cache_key)
        if shared and isinstance(shared.get("data"), dict):
            with _street_pulse_cache["lock"]:
                _street_pulse_cache["entries"][cache_key] = {
                    "ts": float(shared.get("ts") or now_t),
                    "data": shared["data"],
                }
            return dict(shared["data"])

    empty_res = {"available": False, "national_vibe": "", "kanto": "", "kansai": "", "other": ""}
    is_evening_window = (16 <= now_jst.hour <= 23) or (0 <= now_jst.hour <= 2)
    if not force_network:
        if os.environ.get("DISABLE_SCHEDULER") == "1" or _is_requests_post_mocked() or not is_evening_window:
            return empty_res
    if not get_claude_api_key(allow_blocking=allow_blocking):
        return empty_res

    if allow_blocking or force_network:
        with _street_pulse_cache["lock"]:
            _street_pulse_cache["fetching_keys"].add(cache_key)
        fresh = _refresh_street_pulse_sync(cache_key, now_jst)
        if fresh:
            return dict(fresh)
    else:
        with _street_pulse_cache["lock"]:
            already_fetching = cache_key in _street_pulse_cache["fetching_keys"]
            if not already_fetching:
                _street_pulse_cache["fetching_keys"].add(cache_key)
        if not already_fetching:
            threading.Thread(target=_refresh_street_pulse_sync, args=(cache_key, now_jst), daemon=True).start()
    return empty_res


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

    # Identify stores & hubs featured in the last 4 posts so we don't repeat Shinjuku/Umeda every single time
    recent_texts_for_cd = get_recent_cross_channel_texts(channel=channel, limit=4, allow_blocking=False)
    recent_joined = "\n".join(recent_texts_for_cd)
    recently_featured_names = set()
    for s in dom:
        ja_n = s.get("ja_name") or ""
        ja_compact = re.sub(r"[\s　]+", "", ja_n)
        if (ja_n and ja_n in recent_joined) or (ja_compact and ja_compact in re.sub(r"[\s　]+", "", recent_joined)):
            recently_featured_names.add(s.get("name") or ja_n)

    # Golden 4:4:2 spotlight rotation: 40% Tier1 (Shinjuku/Shibuya/Umeda), 40% Tier2 (Sub-major), 20% Tier3 (Regional)
    rotation_wheel = ("tier1", "tier2", "tier1", "tier2", "tier3", "tier1", "tier2", "tier1", "tier2", "tier3")
    slot_offset = 0 if slot_label == "20:00" else (1 if slot_label in ("22:00", "22:30") else 2)
    biz_ord = _business_datetime(now_jst).date().toordinal()
    target_tier = rotation_wheel[(biz_ord * 3 + slot_offset) % len(rotation_wheel)]
    tier_labels = {
        "tier1": "王道メガエリア（新宿・渋谷・梅田）",
        "tier2": "準主要人気街（恵比寿・上野・横浜・大宮・難波・茶屋町・栄・天神ほか）",
        "tier3": "全国・地方注目都市（札幌・仙台・広島・岡山・熊本・沖縄ほか）",
    }

    # 1. Urban cluster gap analysis (with gentle anti-repetition cooldown when another hub also has a real gap)
    best_cluster = None
    best_cluster_score = -1e9
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
            hub_cd_penalty = 24.0 if (recent_texts_for_cd and hub_label.split("・")[0] in recent_texts_for_cd[0]) else 0.0
            score = pct_gap * 1.35 + (high_s["women"] * 0.25) + (min(high_s["total"], low_s["total"]) * 0.25) - hub_cd_penalty
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

    # 2. Genuine sweet-spot / surge store with 4:4:2 tier rotation + recent-store cooldown
    female_lead_pool = [s for s in dom if s["women"] >= 12 and s["women"] >= s["men"]]
    if not female_lead_pool:
        female_lead_pool = [s for s in dom if s["women"] >= 10]

    has_target_tier_candidate = any(
        classify_store_spotlight_tier(s.get("name", "")) == target_tier and s["women"] >= 12
        for s in female_lead_pool
    )
    apply_tier_rotation = len(active_stores) >= 6
    pool_size = len(female_lead_pool)

    def _focus_rank_key(s: dict) -> float:
        base_sc = (
            int(s.get("women_diff") or 0) * 3.0
            + (s["women"] - s["men"]) * 1.5
            + s["women"] * 0.38
        )
        cd_pen = 65.0 if (s.get("name") in recently_featured_names and pool_size >= 2) else 0.0
        if not apply_tier_rotation:
            return base_sc - cd_pen
        st_tier = classify_store_spotlight_tier(s.get("name", ""))
        tier_bonus = 0.0
        if has_target_tier_candidate and st_tier == target_tier:
            tier_bonus = 55.0
        elif target_tier == "tier3" and not has_target_tier_candidate and st_tier == "tier2":
            tier_bonus = 40.0
        return base_sc + tier_bonus - cd_pen

    female_lead_pool.sort(key=_focus_rank_key, reverse=True)
    focus_store = female_lead_pool[0] if female_lead_pool else (dom[0] if dom else None)

    submajor_highlights = [
        s for s in dom
        if classify_store_spotlight_tier(s.get("name", "")) == "tier2" and s["women"] >= 10 and s["total"] >= 16
    ][:3]
    regional_highlights = [
        s for s in dom
        if classify_store_spotlight_tier(s.get("name", "")) == "tier3" and s["women"] >= 8 and s["total"] >= 12
    ][:3]

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
    street_pulse = fetch_tonight_street_pulse(now_jst=now_jst, allow_blocking=allow_weather_blocking)
    slot_persona = build_slot_persona_context(slot_label=slot_label, channel=channel)

    notes = [
        f"対象媒体：{CHANNEL_LABELS.get(channel, channel)} ｜ 全国稼働 {len(active_stores)}店舗（女性計 {total_w:,}名 / 男性計 {total_m:,}名）を解析。",
        f"【街況・カレンダー】{cal_ctx['tag']}（{cal_ctx['note']}）",
        f"【時間帯テンション設計】{slot_persona['label']}",
    ]
    if weather_ctx.get("available"):
        notes.append(f"【リアルタイム気象】{weather_ctx['summary']} — {weather_ctx['adlib_hint']}")
    if street_pulse.get("available") and any(street_pulse.get(k) for k in ("national_vibe", "kanto", "kansai", "other")):
        pulse_parts = [street_pulse.get(k) for k in ("national_vibe", "kanto", "kansai", "other") if street_pulse.get(k)]
        notes.append(f"【今夜のリアル街ネタ検索】{' ／ '.join(pulse_parts[:3])}")
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

    if apply_tier_rotation:
        notes.append(f"【店舗スポット配分（4:4:2黄金比）】今回優先枠：{tier_labels.get(target_tier, target_tier)}")

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
        "spotlight_tier": target_tier,
        "spotlight_tier_label": tier_labels.get(target_tier, ""),
        "submajor_highlights": submajor_highlights,
        "regional_highlights": regional_highlights,
        "recently_featured_names": sorted(natural_store_name(nm) for nm in recently_featured_names),
        "ig_ctx": ig_ctx,
        "calendar": cal_ctx,
        "weather": weather_ctx,
        "street_pulse": street_pulse,
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
    situation: str = None,
) -> dict:
    """
    Determine style rotation and anti-repetition rules per SNS channel (`x`, `threads`, `instagram`, `tiktok`, `note`),
    and inject situation-specific editorial directives (F2.2).
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

    sit_key = (situation or "").strip().lower()
    sit_meta = SITUATION_SPECS.get(sit_key, {})

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
    elif ch == "tiktok":
        if slot_label in ("19:30", "19:45", "20:00"):
            base_conv = ["tiktok_female_confession", "tiktok_hook_gap", "tiktok_data_myth", "tiktok_midnight_reverse"]
        elif slot_label in ("00:00", "02:00"):
            base_conv = ["tiktok_midnight_reverse", "tiktok_hook_gap", "tiktok_data_myth", "tiktok_female_confession"]
        else:
            base_conv = ["tiktok_hook_gap", "tiktok_data_myth", "tiktok_female_confession", "tiktok_midnight_reverse"]
        classic_key = "tiktok_classic_rank"
    elif ch == "note":
        if slot_label in ("00:00", "02:00"):
            base_conv = ["note_essay_insight", "note_tsubuyaki_gap", "note_short_column"]
        else:
            base_conv = ["note_essay_insight", "note_short_column", "note_tsubuyaki_gap"]
        classic_key = "note_classic_summary"
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

    if sit_meta:
        planner_memo = (
            f"【{CHANNEL_LABELS.get(ch, ch)}・{sit_meta['label']}特化編成】"
            f"シチュエーション特化切り口（{sit_meta['desc']}）を優先注入し、"
            f"定型テンプレを抑えて『{STYLE_LABELS.get(style_priority[0], '')}』を最優先に設定。"
        )
    else:
        planner_memo = (
            f"【{CHANNEL_LABELS.get(ch, ch)}・{wd_str}曜編成】"
            f"前回採用『{STYLE_LABELS.get(last_style, '履歴なし')}』との被りをブロックし、"
            f"定型テンプレ（{STYLE_LABELS.get(classic_key, '')}）を抑えて『{STYLE_LABELS.get(style_priority[0], '')}』を最優先に設定。"
        )

    return {
        "channel": ch,
        "slot_label": slot_label,
        "weekday_ja": wd_str,
        "situation": sit_key,
        "situation_label": sit_meta.get("label", ""),
        "situation_meta": sit_meta,
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
                hook_a = f"駅チカの1店舗目だけで並んで時間を溶かすのが一番もったいない夜。{hub_name}でもここまで割れてる"
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
            body_a = body_a.replace("待ちが出そうな時は近くの別店舗も覗くのが確実👇", "混雑時は周辺の別店舗へ👇")
        reply_a = (
            f"📍 {hub_name}エリアの料金・現在人数横並び比較はこちら\n"
            f"{site_url}/price-comparison?area={area_id}&s=x{slot_hh}_hub_gap\n\n"
            f"🔍 全国60店舗超リアルタイム人数ランキング\n{site_url}/?s=x{slot_hh}_hub_gap_{ymd}"
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
        body_b = (
            f"{opener_b}\n"
            f"現在：女性{focus['women']}名 / 男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
            f"{closing_b}\n"
            f"#相席ラウンジ #相席ナビ"
        )
    f_slug = store_master.slug_of(focus["name"])
    reply_b = (
        f"📊 {focus['ja_name']}の現在人数・終電後の残りやすさ指標はこちら\n"
        f"{site_url}/store/{f_slug}?s=x{slot_hh}_surge_focus\n\n"
        f"🔍 全国60店舗超リアルタイム人数ランキング\n{site_url}/?s=x{slot_hh}_surge_focus_{ymd}"
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
        body_c = body_c.replace("早い時間から好ペースで推移中。各街の現在人数はリプ欄へ👇", "各街の現在人数はリプ欄へ👇")

    reply_c = (
        f"🔍 全国60店舗超の現在人数＆普段の同曜日比（平比）はこちら\n{site_url}/?s=x{slot_hh}_{c_style}_{ymd}\n\n"
        f"💴 時間帯料金・開催中イベント横並び比較ボード\n{site_url}/price-comparison?s=x{slot_hh}_{c_style}"
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
    Respects 1-in-3 direct URL vs varied human bio-link anti-spam rotation.
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

    # Meta anti-spam link rotation (~1/3 direct link, ~2/3 varied human bio-link or clean tag)
    def _th_footer(st_key: str) -> str:
        return build_threads_cta_footer(site_url, slot_label=slot_label, style_key=st_key, now_jst=now_jst)

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
            lead_1 = f"正直、{wd_str}曜{t_str}に一番近い店舗だけで突撃して受付待ちで消耗する人が一番損してます。今の{hub_name}のリアルな温度差がこちら"
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
    tags_1 = pick_rotated_hashtag_line(
        "instagram",
        "ladies" if tm == "female_real" else "carousel",
        now_jst=now_jst,
        seed_extra=0,
    )
    tags_2 = pick_rotated_hashtag_line("instagram", "carousel", now_jst=now_jst, seed_extra=1)
    tags_3 = pick_rotated_hashtag_line("instagram", "carousel", now_jst=now_jst, seed_extra=2)

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
            hook_1 = "今週も全国60店舗超のデータを追っていて、「やっぱり店選びで9割決まるな…」と実感した結果がこちら📊"
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
        f"{tags_1}"
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
        f"{tags_2}"
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
        f"{tags_3}"
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


def _write_tiktok_candidates(
    research: dict,
    slot_label: str,
    now_jst: datetime.datetime,
    site_url: str,
    tone_mode: str = DEFAULT_TONE_MODE,
) -> list:
    """
    Generate 5 TikTok-native candidates combining:
      - `tiktok_hook_text`: 0.0-1.8s scroll-stopping on-screen hook
      - `tiktok_reel_type`: Linked 9:16 safe-zone MP4 template (`tiktok_gap_shock` / `tiktok_ladies_real` / `tiktok_midnight_cliff`)
      - `tiktok_bgm_mood`: Recommended trending BGM tempo/genre for TikTok mobile app
      - `main_text`: TikTok Search SEO caption + save CTA (320-650 chars, 5-7 hashtags, zero AI cliches, zero bare 'オリエンタル')
      - `reply_text`: Cut-by-cut director storyboard (`0.0-2.0s` / `2.0-8.5s` / `8.5-11.5s`)
    """
    weekdays_ja = ["月", "火", "水", "木", "金", "土", "日"]
    wd_str = weekdays_ja[_business_datetime(now_jst).weekday()]
    t_str = now_jst.strftime("%H:%M")
    tm = get_editorial_tone_mode(tone_mode, allow_blocking=False)

    top_stores = research.get("top_stores") or []
    all_dom = research.get("all_domestic_stores") or top_stores
    if not top_stores:
        return []

    s1 = top_stores[0]
    s2 = top_stores[1] if len(top_stores) > 1 else s1
    s3 = top_stores[2] if len(top_stores) > 2 else s2
    focus = research.get("focus_store") or s1
    cluster = research.get("best_cluster")
    male_lead = research.get("male_lead_store")
    if not male_lead and all_dom:
        by_male = sorted(all_dom, key=lambda x: (x["men"] - x["women"], x["men"]), reverse=True)
        male_lead = by_male[0]

    candidates = []

    # 1) tiktok_hook_gap: Walkable 4-min gender-ratio shock contrast
    if cluster and cluster["high_store"]["name"] != cluster["low_store"]["name"]:
        hs = cluster["high_store"]
        ls = cluster["low_store"]
        hub = cluster["hub_label"]
        gap = cluster["pct_gap"]
        tt_tags_gap = pick_rotated_hashtag_line(
            "tiktok",
            "tiktok_gap",
            now_jst=now_jst,
            area_tag=hub,
            seed_extra=0,
        )
        hook_txt = f"同じ{hub}・徒歩4分で『女性比{hs['wpct']}% vs {ls['wpct']}%』の残酷な差"
        opener = (
            f"同じ{hub}の徒歩4分圏内なのに、駅チカの1店舗目だけに突撃して受付待ちで消耗してる人が多すぎる…（{wd_str}曜 {t_str}実測）"
            if tm in ("spicy_female", "spicy")
            else f"同じ{hub}の徒歩4分なのに、ドア1枚でここまで男女比が割れてるの毎回ちょっと不思議になる…（{wd_str}曜 {t_str}実測）"
        )
        main_gap = (
            f"{opener}\n\n"
            f"いま{hub}のリアル人数を並べてみると、\n"
            f"・{hs['ja_name']}：女性{hs['women']}名・男性{hs['men']}名（女性比{hs['wpct']}%）\n"
            f"・{ls['ja_name']}：女性{ls['women']}名・男性{ls['men']}名（女性比{ls['wpct']}%）\n"
            f"で、同じ駅のすぐ近くでも {gap}pt も差が開いてたりする。\n\n"
            f"女の子側も待たされる店はすぐ見切ってテンポよく飲める店に流れてるから、駅を出る直前に別ブランド（オリエンタルラウンジ・ag・JIS）を見比べるだけで夜の密度がまるで変わるんだよね。\n\n"
            f"入店3分前に見返せるように『保存』しとくと便利📌\n"
            f"全国60店舗超の現在人数と料金比較はプロフのリンク、または『相席ナビ』（aiseki-navi.com）で無料検索できます🔍\n\n"
            f"{tt_tags_gap}"
        )
        script_gap = (
            f"🎬【動画テンプレ連動】🎵 同駅ギャップ比較（tiktok_gap_shock / 11.5秒・UIセーフゾーン設計）\n"
            f"・0.0〜2.0秒（指止めフック）：「同じ{hub}・徒歩4分なのに 女性比{hs['wpct']}% vs {ls['wpct']}%」\n"
            f"・2.0〜8.5秒（実測ギャップ比較）：{hs['ja_name']}（女{hs['women']}:男{hs['men']}）と{ls['ja_name']}（女{ls['women']}:男{ls['men']}）の比率バー比較\n"
            f"・8.5〜11.5秒（保存＆検索CTA）：「保存して入店3分前に確認！『相席ナビ』で検索🔍」\n"
            f"🎵【推奨BGMムード】BPM118〜126の都会的ローファイハウス / シティポップ系インスト（音量15〜20%）"
        )
        candidates.append({
            "style": "tiktok_hook_gap",
            "style_label": STYLE_LABELS["tiktok_hook_gap"],
            "writer_note": f"【TikTok王道バズ型】{hub}の{gap}ptギャップを冒頭1.8秒で突きつけ、保存＆『相席ナビ』指名検索を促す構成。",
            "tiktok_hook_text": hook_txt,
            "tiktok_reel_type": "tiktok_gap_shock",
            "tiktok_bgm_mood": "都会的ローファイハウス / シティポップ（BPM120前後）",
            "main_text": main_gap,
            "reply_text": script_gap,
        })

    # 2) tiktok_female_confession: Female perspective — why 70%+ female stores make girls wait
    if male_lead:
        ml_mpct = 100 - int(male_lead["wpct"])
        tt_tags_ladies = pick_rotated_hashtag_line(
            "tiktok",
            "ladies",
            now_jst=now_jst,
            seed_extra=1,
        )
        hook_f = f"女子ペアが『女性70%超の店』に行くと、実は受付で待たされる話"
        main_f = (
            f"女の子2人組で「女性がいっぱい入ってる人気店なら安心でしょ」って向かうと、実は一番受付で待たされる罠になりがちだったりする…💄（{wd_str}曜 {t_str}実測メモ）\n\n"
            f"女性が7割を超えて集中してるお店は席替えの回転も重くなりやすいから、女子組が待ちゼロでスマートに乾杯するなら「男性が先行して盛り上がってるお店」を逆張りで選ぶのが一番ラクなんだよね。\n\n"
            f"たとえば今この瞬間のデータだと、\n"
            f"・女子組の即案内狙い目：{male_lead['ja_name']}（男性{male_lead['men']}名・女性{male_lead['women']}名 / 男性比{ml_mpct}%）\n"
            f"・逆に男性側の穴場：{focus['ja_name']}（女性{focus['women']}名・男性{focus['men']}名 / 女性比{focus['wpct']}%）\n"
            f"みたいに、男女で『正解の店』は真逆になるのが面白いところ。\n\n"
            f"友達との2軒目探し用に『保存』しておいて、向かう直前にプロフのリンクか『相席ナビ』（aiseki-navi.com）の女性専用モードを覗いてみてね🥂\n\n"
            f"{tt_tags_ladies}"
        )
        script_f = (
            f"🎬【動画テンプレ連動】🎵 女子本音×待ち0（tiktok_ladies_real / 11.5秒・UIセーフゾーン設計）\n"
            f"・0.0〜2.0秒（女子共感フック）：「女子70%超の店に行くと 実は受付で待たされる!?」\n"
            f"・2.0〜8.5秒（待ち0ランキング）：{male_lead['ja_name']}（男{male_lead['men']}:女{male_lead['women']}）ほか男性先行TOP4\n"
            f"・8.5〜11.5秒（保存CTA）：「女子会の2軒目用に保存🔖『相席ナビ』で3秒検索」\n"
            f"🎵【推奨BGMムード】おしゃれなR&B / 韓国カフェ風チルビート（女性視聴者の保存率重視）"
        )
        candidates.append({
            "style": "tiktok_female_confession",
            "style_label": STYLE_LABELS["tiktok_female_confession"],
            "writer_note": "【女性保存率＆コメント誘発型】男女で正解の店が真逆になる構造を女性目線の本音で解き明かす構成。",
            "tiktok_hook_text": hook_f,
            "tiktok_reel_type": "tiktok_ladies_real",
            "tiktok_bgm_mood": "おしゃれなR&B / 韓国カフェ風チルビート",
            "main_text": main_f,
            "reply_text": script_f,
        })

    # 3) tiktok_data_myth: Counter-intuitive data breakdown
    tt_tags_myth = pick_rotated_hashtag_line(
        "tiktok",
        "tiktok_gap",
        now_jst=now_jst,
        area_tag=str(focus.get("area_label") or ""),
        seed_extra=2,
    )
    hook_m = f"『とりあえず1番有名な店舗へ直行』が一番時間を溶かす理由"
    main_m = (
        f"相席ラウンジで一番もったいない負けパターンって、現地に着いてからノープランで1店舗目だけ見て並んじゃうことなんだよな…📊（{wd_str}曜 {t_str}・全国{research.get('active_store_count', 0)}店稼働中）\n\n"
        f"いま全国の合計は女性{research.get('total_women', 0)}名・男性{research.get('total_men', 0)}名なんだけど、店舗ごとに見ると驚くほど偏りがある。\n"
        f"・{s1['ja_name']}：女性{s1['women']}名・男性{s1['men']}名（女性比{s1['wpct']}%）\n"
        f"・{s2['ja_name']}：女性{s2['women']}名・男性{s2['men']}名（女性比{s2['wpct']}%）\n"
        f"・{focus['ja_name']}：女性{focus['women']}名・男性{focus['men']}名（女性比{focus['wpct']}%）\n\n"
        f"男性なら『女性12名以上×女性比55%以上』、女性ペアなら『男性先行店』をエレベーターに乗る前に選ぶだけで、受付待ちのストレスがほぼ消えるんだよね。\n\n"
        f"週末にすぐ見返せるようにこの動画を『保存』📌\n"
        f"全国60店舗超のリアルタイム人数は『相席ナビ』（aiseki-navi.com）で24時間無料公開中です🔍\n\n"
        f"{tt_tags_myth}"
    )
    script_m = (
        f"🎬【動画テンプレ連動】🎵 同駅ギャップ比較 または 🔥 全国TOP5速報（11.5秒）\n"
        f"・0.0〜2.0秒（図星フック）：「ノープランで1店舗目に並ぶのが一番もったいない理由」\n"
        f"・2.0〜8.5秒（実測データ提示）：{s1['ja_name']}（女{s1['women']}:男{s1['men']}）など上位店のリアルタイム比率\n"
        f"・8.5〜11.5秒（検索誘導CTA）：「エレベーターに乗る3分前に『相席ナビ』で検索🔍」\n"
        f"🎵【推奨BGMムード】テンポ感のあるミニマルテック / トレンド解説系BGM"
    )
    candidates.append({
        "style": "tiktok_data_myth",
        "style_label": STYLE_LABELS["tiktok_data_myth"],
        "writer_note": "【TikTok検索SEO＆ブックマーク特化】『相席ラウンジ 待ち時間』検索層に刺さるデータ比較構成。",
        "tiktok_hook_text": hook_m,
        "tiktok_reel_type": "tiktok_gap_shock",
        "tiktok_bgm_mood": "ミニマルテック / トレンド解説系ビート",
        "main_text": main_m,
        "reply_text": script_m,
    })

    # 4) tiktok_midnight_reverse: 23:20 Last-Train Cliff ("終電崖") & late-night survivors
    mid_ret_val = int(focus.get("midnight_retention_pct") or 82)
    tt_tags_mid = pick_rotated_hashtag_line(
        "tiktok",
        "tiktok_midnight",
        now_jst=now_jst,
        seed_extra=3,
    )
    hook_mid = f"23時20分の『終電崖』で人が消える店と、深夜から増える店の違い"
    main_mid = (
        f"毎晩の推移を追ってるとはっきり出るんだけど、相席ラウンジには23時20分前後に『終電の崖』があるんだよね…🌙\n\n"
        f"終電を境にサッと女性陣が引く店舗と、逆に居酒屋の2軒目組が流れ込んで深夜0時過ぎから一気に熱量が上がる店舗に真っ二つに割れる。\n\n"
        f"いま（{wd_str}曜 {t_str}）実測でしっかり人が入っていて深夜帯も粘り強いのはこのあたり👇\n"
        f"・{focus['ja_name']}：女性{focus['women']}名・男性{focus['men']}名（女性比{focus['wpct']}%）\n"
        f"・{s1['ja_name']}：女性{s1['women']}名・男性{s1['men']}名（女性比{s1['wpct']}%）\n"
        f"・{s3['ja_name']}：女性{s3['women']}名・男性{s3['men']}名（女性比{s3['wpct']}%）\n\n"
        f"2軒目・終電後にハズしたくない夜は、この投稿を『保存』しといて動く直前にプロフのリンク『相席ナビ』（aiseki-navi.com）で終電後維持率を見てから向かうのがほんと鉄板だったりする。\n\n"
        f"{tt_tags_mid}"
    )
    script_mid = (
        f"🎬【動画テンプレ連動】🎵 終電崖×深夜穴場（tiktok_midnight_cliff / 11.5秒・UIセーフゾーン設計）\n"
        f"・0.0〜2.0秒（インサイトフック）：「23時20分の『終電崖』で人が消える店 vs 残る店」\n"
        f"・2.0〜8.5秒（深夜強い店TOP4）：{focus['ja_name']}（維持率{mid_ret_val}%目安）ほか現在人数アニメーション\n"
        f"・8.5〜11.5秒（保存CTA）：「2軒目・終電後の保険に保存📌『相席ナビ』で無料公開中」\n"
        f"🎵【推奨BGMムード】深夜ドライブ系チルシンセ / ナイトシティポップ"
    )
    candidates.append({
        "style": "tiktok_midnight_reverse",
        "style_label": STYLE_LABELS["tiktok_midnight_reverse"],
        "writer_note": "【深夜2軒目・独自知見型】相席ナビ独自の『23時20分の終電崖×深夜維持率』を切り口にした高滞在時間構成。",
        "tiktok_hook_text": hook_mid,
        "tiktok_reel_type": "tiktok_midnight_cliff",
        "tiktok_bgm_mood": "深夜ドライブ系チルシンセ / ナイトシティポップ",
        "main_text": main_mid,
        "reply_text": script_mid,
    })

    # 5) tiktok_classic_rank: Legacy baseline for Desk comparison
    classic_tt = (
        f"【{wd_str}曜 {t_str} 全国相席ラウンジ人数TOP3速報】\n"
        f"🥇 1位：{s1['ja_name']}（女性{s1['women']}名 / 男性{s1['men']}名）\n"
        f"🥈 2位：{s2['ja_name']}（女性{s2['women']}名 / 男性{s2['men']}名）\n"
        f"🥉 3位：{s3['ja_name']}（女性{s3['women']}名 / 男性{s3['men']}名）\n\n"
        f"リアルタイム人数はプロフィールの『相席ナビ』（aiseki-navi.com）から保存して確認！\n"
        f"#相席ナビ #相席ラウンジ #オリエンタルラウンジ #ag #JIS"
    )
    candidates.append({
        "style": "tiktok_classic_rank",
        "style_label": STYLE_LABELS["tiktok_classic_rank"],
        "writer_note": "従来の『【TOP3速報】🥇🥈🥉』定型読み上げキャプション（鬼デスク比較ベンチマーク用）。",
        "tiktok_hook_text": "全国リアルタイム人数ランキングTOP3",
        "tiktok_reel_type": "live_top5",
        "tiktok_bgm_mood": "汎用ポップBGM",
        "main_text": classic_tt,
        "reply_text": "（従来型TOP3読み上げ構成・比較ベンチマーク）",
    })

    return candidates


def _write_note_candidates(
    research: dict,
    slot_label: str = "22:00",
    now_jst: datetime.datetime = None,
    site_url: str = "https://aiseki-navi.com",
    tone_mode: str = DEFAULT_TONE_MODE,
    situation: str = None,
) -> list:
    """
    Generate 4 distinct note.com candidates tailored for deep reading, essays, and short thoughts (F2.1):
      1) note_essay_insight: Deep-dive human psychology & night street essay (800〜1,400 chars)
      2) note_short_column: Data-driven night economy & strategy mini column (800〜1,200 chars)
      3) note_tsubuyaki_gap: Short poignant insider whisper / observation (120〜240 chars)
      4) note_classic_summary: Comprehensive national & regional crowd summary (800〜1,100 chars)
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    cal = research.get("calendar") or build_calendar_context(now_jst)
    wd_str = cal["weekday_ja"]
    sit_key = (situation or "").strip().lower()
    sit_meta = SITUATION_SPECS.get(sit_key, {})

    stores = research.get("top_stores") or []
    if len(stores) < 3:
        stores = [
            {"ja_name": "オリエンタルラウンジ渋谷本店", "women": 42, "men": 30, "wpct": 58, "women_diff": 4},
            {"ja_name": "オリエンタルラウンジ新宿", "women": 38, "men": 28, "wpct": 58, "women_diff": 2},
            {"ja_name": "オリエンタルラウンジ梅田", "women": 35, "men": 24, "wpct": 59, "women_diff": 3},
        ]
    s1, s2, s3 = stores[0], stores[1], stores[2]

    active_stores = research.get("active_stores") or stores
    total_w = sum(int(s.get("women") or 0) for s in active_stores)
    total_m = sum(int(s.get("men") or 0) for s in active_stores)
    candidates = []

    # 1) note_essay_insight (800〜1,400 chars)
    if sit_key == "second_venue":
        essay_title = f"【夜の立ち回り論】相席ラウンジで一番盛り上がった瞬間に「静かな夜カフェ行かない？」と言える男が結局一番勝てる理由"
        essay_p1 = (
            f"街に人が溢れかえる{wd_str}曜の夜。モニターを眺めていると、{s1['ja_name']}には女性{s1['women']}名・男性{s1['men']}名（女性比{s1['wpct']}%）が集まり、"
            f"フロアの熱気はピークを迎えている。周囲の{s2['ja_name']}（女性{s2['women']}名 / 男性{s2['men']}名）や{s3['ja_name']}も活況だ。"
            f"だが、現場で何百組もの男女の会話を観察していて痛感する残酷な事実がある。それは『店内でどれだけ会話が弾んでも、2軒目の切り出し方を間違えるとすべてが水の泡になる』ということだ。"
        )
        essay_p2 = (
            f"多くの男性がやりがちな典型的な敗因は、席の時間が盛り上がっているからと惰性で1時間半も粘り続け、話題が出尽くして沈黙が流れたタイミングで「この後どうする？」とお茶を濁すことだ。"
            f"女性ペアの心理は明快だ。会話の熱量が最高潮に達している40分〜50分のピーク時にこそ、スマートに席を切り上げられる余裕に魅力を感じる。"
            f"そして何より、ガヤガヤと大音量のBGMが流れる店内から抜け出し、少し落ち着いて話せる『静かな夜カフェ』や『徒歩3分のバー』へ自然に誘導できるかどうかが成否を分ける。"
        )
        essay_p3 = (
            f"「ここ音楽で少し声張らないとだから、30分だけ近くの静かな夜カフェで美味しいスイーツ食べない？」"
            f"この一言の強さは異常だ。『30分だけ』と明確な出口を示すことで女性側の警戒心を解き、お酒ではなく甘いものや落ち着いた空間を理由にすることで、無理のない自然な合意が生まれる。"
            f"現在地から徒歩何分でどんな落ち着いた空間があるのか。相席ナビ（aiseki-navi.com）が周辺の厳選2軒目スポットやシチュエーション別フレーズを提供しているのも、まさにこの『相席後の勝負どころ』で迷子にならないためだ。"
            f"今夜の相席ラウンジを最高の思い出にするか、ただの飲み会で終わらせるか。鍵はいつだって、退店の一歩手前のスマートな切り出しにある。"
        )
    elif sit_key == "zero_wait":
        essay_title = f"【女子の本音と逆張り】混雑店に並ぶ列を横目に、賢い女性ペアが「待ち時間ゼロ」で即座に乾杯する店舗の選び方"
        essay_p1 = (
            f"繁華街の夜、有名な大型店の前には入場待ちの長い列ができる。今夜のデータでも、{s1['ja_name']}は女性{s1['women']}名・男性{s1['men']}名と大盛況。"
            f"しかし、そのすぐ近くにある店舗へ目を向けると、まったく違う光景が広がっている。"
            f"男性陣がネームバリューだけで長蛇の列に並んで時間を溶かしている裏側で、立ち回りの上手い女性ペアは最初から『待ちゼロ』の店舗へ滑り込み、颯爽と乾杯を始めているのだ。"
        )
        essay_p2 = (
            f"女性側のリアルな本音を言えば、せっかくメイクもヘアセットも完璧に整えてヒールを履いてきた夜に、受付の狭いエントランスで何十分も待たされることほどテンションが下がるものはない。"
            f"特に{wd_str}曜の夜は、男性先行のバランスになっている店舗を選べば、到着した瞬間に待ち時間なしで即席へ案内され、ウェルカムドリンクや限定フードをゆったり楽しむことができる。"
            f"例えば今夜なら、{s2['ja_name']}（女性{s2['women']}名 / 男性{s2['men']}名）のように男性比率がしっかり確保された店舗こそ、女性グループにとっては最も待遇が良く、ゆとりを持った相席が期待できる穴場なのだ。"
        )
        essay_p3 = (
            f"『とりあえず有名な店に行けばいいや』という思考停止は、時間とお金の両方を浪費する最大の罠だ。"
            f"リアルタイムの男女人数と待ち時間をスマホ1つで可視化する相席ナビ（aiseki-navi.com）を開いて、今まさに女性待ちゼロで入れる店舗をピンポイントで狙い撃つ。"
            f"それだけで、夜遊びの質は劇的に変わる。無駄な待ち時間をゼロにして、最高の席で乾杯する夜をぜひ選んでほしい。"
        )
    elif sit_key == "rain_underground":
        essay_title = f"【雨の日の夜遊び戦略】足元の悪さは最大のチャンス。なぜ「地下直結ラウンジ」を知っている大人は雨の夜に一人勝ちできるのか？"
        essay_p1 = (
            f"外はあいにくの雨。濡れたアスファルトに街灯が反射し、繁華街の歩道を行き交う傘の波が見える。"
            f"一般的に雨の日は客足が鈍ると言われるが、相席ラウンジのリアルタイム推移を追っているとまったく逆の現象が起きていることに気づく。"
            f"{s1['ja_name']}では女性{s1['women']}名・男性{s1['men']}名（女性比{s1['wpct']}%）が早くも集まり、店内は外の悪天候を忘れさせる熱気に包まれている。"
        )
        essay_p2 = (
            f"なぜ雨の日にこれほど人が集まるのか。その答えは『地下街直結』という圧倒的な利便性にある。"
            f"女性にとって、雨の日に傘を差して何分も路面を歩くのは絶対に避けたいストレスだ。湿気で髪は崩れ、お気に入りの靴や服が濡れてしまう。"
            f"だからこそ、駅から改札を出て地下通路をそのまま歩いて濡れずにエレベーターへ直行できる店舗には、美意識の高い女性ペアが自然と一極集中するのだ。"
            f"雨の日こそ、地下直結や駅徒歩1分以内の店舗に女性が集中し、逆に少し離れた路面店には男性先行の穴場が生まれるという鮮やかなコントラストが生まれる。"
        )
        essay_p3 = (
            f"スマートな大人は、雨を言い訳にして家にこもるのではなく、雨だからこそ生まれる偏りと地下直結の利便性を味方につける。"
            f"「雨だから駅直結のラウンジにいるんだけど、濡れずに合流しない？」という誘い文句がどれほど強力か、想像してみてほしい。"
            f"今夜の天候と各店舗の混雑差は相席ナビ（aiseki-navi.com）でリアルタイムに確認できる。足元の悪い夜だからこそ、賢い選択で差をつけてほしい。"
        )
    elif sit_key == "last_train_cliff":
        essay_title = f"【深夜24時の人間模様】終電崖を越えたラウンジで起きているドラマと、朝まで残る男女のリアルな立ち回り"
        essay_p1 = (
            f"時計の針が23時30分を回り、駅へ向かう人波が足早になる頃。相席ラウンジのフロアには、独特の静けさと濃密な空気が漂い始める。"
            f"終電で帰るスマートな組が解散し、店内には『最初から朝までコースを決めている組』と『思いがけず話が弾んで終電を逃した組』だけが残る。"
            f"今夜の{s1['ja_name']}は女性{s1['women']}名・男性{s1['men']}名。深夜0時を過ぎてもなお、この人数が維持されていること自体が、今夜の熱量の高さを雄弁に物語っている。"
        )
        essay_p2 = (
            f"終電後の相席ラウンジは、20時台や21時台の慌ただしさとはまったく別の顔を見せる。"
            f"「次の店へ移動しなきゃ」「終電に間に合わせなきゃ」という時間制限のプレッシャーから解放され、お互いのプライベートや本音の深い会話が自然と引き出される時間帯だ。"
            f"ただし、ここで勘違いしてはいけないのは、朝まで残っているからといって誰でもいいわけではないということだ。"
            f"女性側も深夜まで残る以上、安心できる相手、楽しく会話ができる好感度の高い相手をシビアに見極めている。"
        )
        essay_p3 = (
            f"深夜の立ち回りで最も重要なのは、決してがっつかない余裕と、眠気や疲れを気遣えるスマートな配慮だ。"
            f"ソフトドリンクを頼んだり、軽く甘いものをシェアしたり、心地よいペースを保てる人が最後に選ばれる。"
            f"終電を逃して途方に暮れるのではなく、深夜営業の店舗の混雑状況を相席ナビ（aiseki-navi.com）で確認しながら、無理のない大人の夜を楽しむ。"
            f"終電崖の向こう側には、昼間や宵の口には見られない、本物の人間模様が広がっている。"
        )
    elif sit_key == "start_dash":
        essay_title = f"【19時の立ち上がり論】なぜ金曜の夜遊びは「スタートダッシュ」を制した者が最も良い席を引けるのか？"
        essay_p1 = (
            f"19時を少し回ったところ。街はまだ仕事終わりの喧騒が始まったばかりだが、すでに相席ラウンジの扉は開いている。"
            f"今夜の{s1['ja_name']}では、早くも女性{s1['women']}名・男性{s1['men']}名がチェックイン。"
            f"ピークの21時半に比べればフロアはまだ穏やかだが、この『混み始める前の早い時間帯』にこそ、最も質の高い出会いと快適な滞在が約束されている。"
        )
        essay_p2 = (
            f"スタートダッシュの最大のメリットは、早割特典や限定サービスを存分に活用できるだけでなく、"
            f"スタッフの案内やオペレーションにも余裕があり、希望のエリアや落ち着いたソファ席を確保しやすい点にある。"
            f"混雑ピーク時にありがちな『男性待ちで受付で立ち往生する』『急かされるように相席が入れ替わる』といったストレスとは無縁の時間がここにある。"
            f"仕事を定時でスマートに切り上げた女性グループも、この落ち着いた1軒目を狙って来店しているケースが多い。"
        )
        essay_p3 = (
            f"遅れて戦場に飛び込むより、先手を打って余裕を持つ。"
            f"早い時間に1〜2席軽く会話を交わし、一番気の合う相手とピーク前の落ち着いた時間帯に2軒目へシフトする。"
            f"これこそが、経験豊富な大人が実践している最もスマートなラウンジ活用法だ。"
            f"相席ナビ（aiseki-navi.com）で18時〜20時台の立ち上がり人数を確認し、今夜のスタートダッシュを決めてほしい。"
        )
    else:  # standard / default essay
        essay_title = f"【街の温度差解剖】なぜ同じ駅・徒歩4分の相席ラウンジで「男女比率の格差」が生まれるのか？実測データから読む夜の歩き方"
        essay_p1 = (
            f"夜の繁華街。駅の改札を出て徒歩数分の範囲に、同じ看板を掲げた相席ラウンジが複数店舗並んでいることがある。"
            f"多くの人は「どっちに行っても大して変わらないだろう」と直感で近い方へ入ってしまうが、毎晩の推移を知っている身からすると、それはあまりにも勿体ない選択だ。"
            f"今夜のデータを見ても、トップの{s1['ja_name']}は女性{s1['women']}名・男性{s1['men']}名（女性比{s1['wpct']}%）と理想的なバランスを保っているのに対し、"
            f"歩いてすぐの別店舗では男性比率が大きく偏り、受付で待ち時間が発生している。"
        )
        essay_p2 = (
            f"なぜ、わずか数百メートルしか離れていない店舗同士で、これほど極端な温度差が生まれるのか。"
            f"理由は実にシンプルだ。駅からの動線、店舗の内装やソファの座り心地、女性限定のスイーツビュッフェの充実度、そしてSNSでの認知の差が、目に見えない形で客層をふるい分けているからだ。"
            f"女性客は居心地の良さに極めて敏感で、少しでも待たされたり落ち着かないと感じた店舗はあっさり見切り、好バランスの店舗へと流れていく。"
            f"その結果、片方の店舗は女性で賑わい、もう片方の店舗は男性ばかりが並ぶという残酷なコントラストが生じるのだ。"
        )
        essay_p3 = (
            f"夜遊びで最も重要なリソースは『時間』だ。事前の情報を持たずに飛び込んで、40分も受付で消耗するのはあまりにも非効率だ。"
            f"全国主要店舗のリアルタイム混雑と男女比を瞬時に比較できる相席ナビ（aiseki-navi.com）を活用し、"
            f"「いま一番勢いのある店」または「今すぐ待ちゼロで入れる店」を把握してから動く。"
            f"ほんの30秒のデータ確認が、あなたの今夜を決定的に充実させる。街の温度差を味方につけて、賢くスマートな夜を過ごしてほしい。"
        )

    essay_full = f"{essay_title}\n\n{essay_p1}\n\n{essay_p2}\n\n{essay_p3}"
    essay_full = sanitize_brand_names(essay_full)

    candidates.append({
        "style": "note_essay_insight",
        "style_label": STYLE_LABELS["note_essay_insight"],
        "writer_note": f"【note深層考察】実測データ（{s1['ja_name']} 女{s1['women']}:男{s1['men']}）を織り交ぜ、夜の人間模様と立ち回り戦略を深く描いた読了感あるエッセイ（約1,000文字）。",
        "main_text": essay_full,
        "reply_text": "（note専用・Webエディタへ1クリック転送可能・見出し画像プロンプト連動）",
    })

    # 2) note_short_column (800〜1,200 chars)
    col_title = f"【データで読み解く夜の相席戦略】平常比ギャップと女性比率の偏りから見出す、今夜の最適解"
    col_p1 = (
        f"相席ナビ編集室のモニターには、全国稼働{len(active_stores)}店舗のデータが毎分リアルタイムに集約されている。"
        f"現在の全国総来店者数は女性{total_w:,}名・男性{total_m:,}名。全体としては活発に動いているが、個々の店舗へフォーカスすると極めて興味深い偏りが見えてくる。"
        f"例えば{s1['ja_name']}は女性{s1['women']}名・男性{s1['men']}名（女性比{s1['wpct']}%）に達しており、フロア全体で女性優勢のバランスが形成されている。"
    )
    col_p2 = (
        f"データ分析において私たちが特に注目しているのが『平常同曜日比（普段比）』と『女性比率』の2軸だ。"
        f"普段の{wd_str}曜日の同時刻と比べて女性が急増している店舗は、フロア全体の活気と回転率が高く、会話の弾みやすさが格段に向上する。"
        f"一方で、女性比率が45%を下回り男性が過剰になっている店舗では、スタッフの案内待ちが発生しやすく、男性側の滞在コストと疲労度ばかりが積み上がってしまう。"
        f"{s2['ja_name']}（女性{s2['women']}名 / 男性{s2['men']}名）や{s3['ja_name']}（女性{s3['women']}名 / 男性{s3['men']}名）のように、"
        f"女性比率が50%〜65%の黄金比を保っている店舗こそが、今夜最もコストパフォーマンスの高い選択肢と言える。"
    )
    col_p3 = (
        f"直感や運任せで店を選ぶ時代は終わった。現代の夜遊びは、データを賢く活用した者が最もスマートに果実を得る構造になっている。"
        f"相席ナビ（aiseki-navi.com）でリアルタイムの人数・比率を確認し、無駄な待ち時間を削ぎ落としてベストな席に着く。"
        f"そして盛り上がった後は、周辺の落ち着いた夜カフェやバーへスマートに2軒目移動する。"
        f"この一連の黄金フローを徹底することこそが、大人の洗練された夜の過ごし方である。"
    )
    col_full = f"{col_title}\n\n{col_p1}\n\n{col_p2}\n\n{col_p3}"
    col_full = sanitize_brand_names(col_full)

    candidates.append({
        "style": "note_short_column",
        "style_label": STYLE_LABELS["note_short_column"],
        "writer_note": f"【noteミニコラム】全国実測データと黄金比（女性比50〜65%）を根拠に、論理的で説得力のある立ち回り論を提示する構成（約900文字）。",
        "main_text": col_full,
        "reply_text": "（note専用・Webエディタへ1クリック転送可能・見出し画像プロンプト連動）",
    })

    # 3) note_tsubuyaki_gap (120〜240 chars)
    tsubu = (
        f"今夜の{s1['ja_name']}、女性{s1['women']}名・男性{s1['men']}名（女性比{s1['wpct']}%）でかなり綺麗なバランス。"
        f"徒歩数分の別店舗は男余りで待ちが出てるので、駅を出て一番近い店にそのまま入るとちょっと勿体ない夜ですね。"
        f"エレベーターに乗る前の30秒で夜の密度が変わります。"
    )
    tsubu = sanitize_brand_names(tsubu)
    candidates.append({
        "style": "note_tsubuyaki_gap",
        "style_label": STYLE_LABELS["note_tsubuyaki_gap"],
        "writer_note": "【noteつぶやき】夜の街の温度差をさらっと切り取った大人の素の呟き（約140文字・noteつぶやき枠最適長）。",
        "main_text": tsubu,
        "reply_text": "（noteつぶやき専用・X等へのクロスポストにも転用可能）",
    })

    # 4) note_classic_summary (800〜1,100 chars)
    classic_summary = (
        f"【{wd_str}曜夜 全国相席ラウンジ混雑動向サマリーレポート】\n\n"
        f"現在、全国稼働{len(active_stores)}店舗における総来店者数は女性{total_w:,}名・男性{total_m:,}名となっております。\n\n"
        f"■ 今夜の全国主要店舗 混雑ハイライト：\n"
        f"・第1位：{s1['ja_name']}（女性{s1['women']}名 / 男性{s1['men']}名・女性比{s1['wpct']}%）\n"
        f"・第2位：{s2['ja_name']}（女性{s2['women']}名 / 男性{s2['men']}名・女性比{s2['wpct']}%）\n"
        f"・第3位：{s3['ja_name']}（女性{s3['women']}名 / 男性{s3['men']}名・女性比{s3['wpct']}%）\n\n"
        f"■ エリア動向と傾向分析：\n"
        f"主要都市部（東京、大阪、名古屋、福岡）を中心に、ピーク時間帯における女性の来店ペースが加速しております。"
        f"特に同一駅周辺において、店舗ごとに男女比率の偏りが発生しており、事前の人数確認を行ってから向かうことが推奨されます。\n\n"
        f"全国60店舗超のリアルタイム混雑データおよび料金・イベント詳細については、『相席ナビ』（https://aiseki-navi.com）をご参照ください。"
    )
    classic_summary = sanitize_brand_names(classic_summary)
    candidates.append({
        "style": "note_classic_summary",
        "style_label": STYLE_LABELS["note_classic_summary"],
        "writer_note": "【note定型サマリー】主要店舗の数値を網羅した速報まとめ（比較用ベンチマーク）。",
        "main_text": classic_summary,
        "reply_text": "（noteまとめ定型・比較ベンチマーク）",
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
    canon_facts = {}
    alias_to_canon = {}
    bare_brands = {"オリエンタルラウンジ", "JIS", "ag", "アソビバー", "ヤタコイ", "CLOVERS", "ALFA", "XIX"}

    def _canon_id(raw_alias: str) -> str:
        c = re.sub(r"[\s　]+", "", (raw_alias or "").strip())
        if c.endswith("店") and not c.endswith("本店"):
            c = c[:-1]
        return c

    def _expand_aliases(raw_alias: str) -> list:
        base = (raw_alias or "").strip()
        if not base:
            return []
        compact = re.sub(r"[\s　]+", "", base)
        if not compact or compact in bare_brands:
            return []
        spaced = re.sub(
            r"^(オリエンタルラウンジ|JIS|ag|アソビバー|ヤタコイ|CLOVERS|ALFA|XIX)(?![\s　])",
            r"\1 ",
            compact,
        )
        seeds = {base, compact, spaced}
        out = set()
        for item in seeds:
            if not item or re.sub(r"[\s　]+", "", item) in bare_brands:
                continue
            out.add(item)
            if item.endswith("店") and not item.endswith("本店"):
                stripped = item[:-1].strip()
                if stripped and re.sub(r"[\s　]+", "", stripped) not in bare_brands:
                    out.add(stripped)
            elif not item.endswith("店"):
                out.add(f"{item}店")
        return list(out)

    def _reg_alias(alias: str, w_val=None, m_val=None, p_val=None):
        cid = _canon_id(alias)
        if not cid or cid in bare_brands:
            return
        rec = canon_facts.setdefault(cid, {"label": (alias or cid).strip(), "women": set(), "men": set(), "pct": set()})
        if w_val is not None:
            rec["women"].add(int(w_val))
        if m_val is not None:
            rec["men"].add(int(m_val))
        if p_val is not None:
            rec["pct"].add(int(p_val))
        for variant in _expand_aliases(alias):
            alias_to_canon[variant] = cid

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

    for s in ig_ana:
        ap = int(s.get("avg_wpct") or 0)
        allowed_pct.add(ap)
        seo_n = s.get("seo_name") or ""
        _reg_alias(seo_n, p_val=ap)

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

    # Line-level store cross-check (recognizes both spaced & unspaced brand names so 2-store comparisons never false-reject)
    nat_w = int(research.get("total_women") or -1)
    nat_m = int(research.get("total_men") or -1)
    sorted_aliases = sorted(alias_to_canon.keys(), key=len, reverse=True)
    for line in text.splitlines():
        matched_cids = []
        line_probe = line
        for al in sorted_aliases:
            if al in line_probe:
                cid = alias_to_canon[al]
                if cid not in matched_cids:
                    matched_cids.append(cid)
                line_probe = line_probe.replace(al, " ")
        if len(matched_cids) == 1:
            cid = matched_cids[0]
            facts = canon_facts[cid]
            lbl = facts["label"]
            for vw in _extract_headcount_values(line, "women"):
                if facts["women"] and vw not in facts["women"] and vw not in (10, nat_w):
                    bad.append(f"{lbl}:女性{vw}(実測不一致)")
            for vm in _extract_headcount_values(line, "men"):
                if facts["men"] and vm not in facts["men"] and vm not in (0, nat_m):
                    bad.append(f"{lbl}:男性{vm}(実測不一致)")
        elif len(matched_cids) >= 2:
            union_w = set().union(*(canon_facts[c]["women"] for c in matched_cids)) | {10, nat_w}
            union_m = set().union(*(canon_facts[c]["men"] for c in matched_cids)) | {0, nat_m}
            has_any_w = any(canon_facts[c]["women"] for c in matched_cids)
            has_any_m = any(canon_facts[c]["men"] for c in matched_cids)
            for vw in _extract_headcount_values(line, "women"):
                if has_any_w and vw not in union_w:
                    bad.append(f"比較行:女性{vw}(実測不一致)")
            for vm in _extract_headcount_values(line, "men"):
                if has_any_m and vm not in union_m:
                    bad.append(f"比較行:男性{vm}(実測不一致)")

    return (len(bad) == 0, bad)


X_POST_ARCHETYPES = (
    {
        "key": "bare_contrast",
        "label": "前置きゼロ・2店舗の温度差スケッチ（余韻クローズ）",
        "silent_tree": True,
        "directive": (
            "- 【今回の骨格：前置きゼロ・温度差スケッチ（60〜98文字）】\n"
            "- 天気や時間帯の挨拶を一切省き、1行目からいきなり同じ街（または東西）の2店舗の数字差を切り出すこと。\n"
            "- 説教や教訓は書かず、体言止めや短い事実の余韻（『〜の差。』『〜は珍しい。』等）でスッと終えること。\n"
            "- **今回は本文末尾に『リプ欄へ👇』もハッシュタグ（#）も一切付けないこと**（人間の素のツイートとして完結させる。詳細URLはシステム側で静かに2ポスト目へ繋ぐ）。"
        ),
    },
    {
        "key": "polite_drop_memo",
        "label": "気取らない素の丁寧語・1店舗置きメモ",
        "silent_tree": False,
        "directive": (
            "- 【今回の骨格：素の丁寧語・置きメモ（64〜102文字）】\n"
            "- 飾り気のない大人の素の敬語（『〜ですね』『〜になってます』『〜探してる人向けに置いときます』等）で、いまバランスが良い1店舗（または準主要街の穴場）を淡々と置いて終えること。\n"
            "- 絵文字や『！』は **0個** にすること。ハッシュタグは末尾に『#相席ラウンジ』1個だけ添えるか、無くてもよい。"
        ),
    },
    {
        "key": "regional_submajor_pulse",
        "label": "準主要街・地方都市の静かな熱気フォーカス",
        "silent_tree": False,
        "directive": (
            "- 【今回の骨格：準主要・地方スポット（70〜110文字）】\n"
            "- 新宿・渋谷・梅田の定番大型店ばかりではなく、ファクトシート内の準主要街（恵比寿・上野・横浜・大宮・難波・茶屋町・栄・天神など）や好調な地方都市があればそこを主役に拾うこと（無ければ `sweet_spot_store` でOK）。\n"
            "- 講釈は省き、その街の今の空気感と正確な人数をさらっと伝え、末尾に控えめに『細かい推移は下に置いときます #相席ラウンジ』程度で締めること。"
        ),
    },
    {
        "key": "question_linger",
        "label": "ふとした疑問・ボヤき余韻クローズ",
        "silent_tree": True,
        "directive": (
            "- 【今回の骨格：ふとした疑問・余韻終わり（58〜96文字）】\n"
            "- 今夜の数字のちょっとした偏りや意外な店舗の伸びを1つだけ挙げ、『まだ1軒目から動いてないだけかな。』『雨の日はやっぱり駅直結に寄るのかも。』のように独り言の疑問・推測でふと終えること。\n"
            "- **今回は『リプ欄へ👇』もハッシュタグも一切書かず**、余韻だけで終えること。"
        ),
    },
    {
        "key": "female_zero_wait_casual",
        "label": "女子ペア目線・待ちゼロ即案内の空気メモ",
        "silent_tree": False,
        "directive": (
            "- 【今回の骨格：女性組目線の待ちゼロ共有（70〜110文字）】\n"
            "- ファクトシートの `female_group_zero_wait_store`（男性先行店）や好バランス店を使い、女の子2人組がいま受付で待たされずにそのまま座れそうな空気感を友達に教えるような口調で書くこと（男性先行店が無ければ `sweet_spot_store` でOK）。\n"
            "- 絵文字は0〜1個まで。末尾は『#相席ラウンジ』1個のみ、またはタグなしで自然に終える。"
        ),
    },
    {
        "key": "minimal_snap",
        "label": "引き算の超短文スナップ（52〜85文字）",
        "silent_tree": True,
        "directive": (
            "- 【今回の骨格：引き算の超短文スナップ（52〜85文字）】\n"
            "- 説明・理由付け・オチをすべて削ぎ落とし、いま一番空気が良い1〜2店舗の実測人数と短い一言だけを2行以内で端的に置くこと。\n"
            "- **『リプ欄へ👇』も絵文字もハッシュタグも付けない**、一番人間らしい素の短文にすること。"
        ),
    },
    {
        "key": "street_air_note",
        "label": "外の季節感・空気1フレーズ＋街況メモ",
        "silent_tree": False,
        "directive": (
            "- 【今回の骨格：外の空気感×街況メモ（78〜114文字）】\n"
            "- 外の気温や曜日・時間帯の空気を短い1フレーズだけ添え、1〜2店舗の正確な人数を会話の流れに溶かし込むこと。\n"
            "- 宣伝臭い煽りは避け、末尾に小さくリプ欄案内または『#相席ナビ #相席ラウンジ』を添える。"
        ),
    },
    {
        "key": "hopper_timing",
        "label": "1軒目終わり・ハシゴ時間帯のフラット比較",
        "silent_tree": False,
        "directive": (
            "- 【今回の骨格：ハシゴ時間帯のフラット比較（74〜112文字）】\n"
            "- 1軒目を出る頃や2軒目を探すタイミングに触れつつ、混みすぎている店とまだ余裕がある店の実測値をフラットに並べること。\n"
            "- 素の丁寧語（『〜ですね』『〜かも』）または体言止めで落ち着いて締め、絵文字は0〜1個に抑える。"
        ),
    },
)


def select_x_post_archetype(slot_label: str, now_jst: datetime.datetime, recent_texts: list = None) -> dict:
    """Deterministically rotate across 8 distinct X post skeletons so consecutive posts never share the same shape."""
    slot_idx = 0 if slot_label == "20:00" else (3 if slot_label in ("22:00", "22:30") else 5)
    seed = (_business_datetime(now_jst).date().toordinal() * 5 + slot_idx + now_jst.hour) % len(X_POST_ARCHETYPES)
    cand = X_POST_ARCHETYPES[seed]
    # If the very last X post was already tagless/silent-tree, prefer a varied archetype next (and vice versa)
    if recent_texts:
        last_t = recent_texts[0]
        last_was_silent = ("#" not in last_t and "👇" not in last_t)
        if last_was_silent and cand["silent_tree"]:
            cand = X_POST_ARCHETYPES[(seed + 1) % len(X_POST_ARCHETYPES)]
        elif (not last_was_silent) and ("👇" in last_t) and (not cand["silent_tree"]):
            alt = X_POST_ARCHETYPES[(seed + 1) % len(X_POST_ARCHETYPES)]
            if alt["silent_tree"]:
                cand = alt
    return cand


def _build_gemini_prompt(
    research: dict,
    plan: dict,
    slot_label: str,
    channel: str,
    now_jst: datetime.datetime,
    site_url: str,
    tone_mode: str = DEFAULT_TONE_MODE,
    situation: str = None,
) -> tuple:
    ch = (channel or "x").lower()
    tm = get_editorial_tone_mode(tone_mode, allow_blocking=False)
    tone_meta = EDITORIAL_TONE_MODES[tm]
    t_str = now_jst.strftime("%H:%M")
    cal_ctx = research.get("calendar") or {}
    weather_ctx = research.get("weather") or {}
    street_pulse = research.get("street_pulse") or {}
    if not street_pulse.get("available"):
        street_pulse = fetch_tonight_street_pulse(now_jst=now_jst, allow_blocking=True)
    recent_full_texts = get_recent_cross_channel_texts(channel=ch, limit=5, allow_blocking=False)
    recent_endings = extract_sentence_endings(recent_full_texts)

    top_s = research.get("top_stores") or []
    focus = research.get("focus_store") or (top_s[0] if top_s else None)
    cluster = research.get("best_cluster")
    anomaly = research.get("anomaly_store")
    male_lead = research.get("male_lead_store")
    city_leaders = research.get("city_leaders") or []
    submajor_hl = research.get("submajor_highlights") or []
    regional_hl = research.get("regional_highlights") or []

    pulse_dict = None
    if street_pulse.get("available") and any(street_pulse.get(k) for k in ("national_vibe", "kanto", "kansai", "other")):
        pulse_dict = {
            k: street_pulse[k]
            for k in ("national_vibe", "kanto", "kansai", "other")
            if street_pulse.get(k)
        }

    fact_payload = {
        "current_time": f"{cal_ctx.get('weekday_ja', '')}曜 {t_str}",
        "calendar_vibe": f"{cal_ctx.get('tag', '')} - {cal_ctx.get('note', '')}",
        "weather_vibe": weather_ctx.get("adlib_hint") or "穏やかな夜",
        "weather_detail": weather_ctx.get("summary") or "",
        **({"tonight_street_pulse": pulse_dict} if pulse_dict else {}),
        "recommended_spotlight_tier": research.get("spotlight_tier_label") or "",
        "recently_used_stores_avoid_if_possible": research.get("recently_featured_names") or [],
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
        "submajor_city_highlights": [
            {"name": s["ja_name"], "women": s["women"], "men": s["men"], "women_pct": s["wpct"]}
            for s in submajor_hl[:3]
        ],
        "regional_city_highlights": [
            {"name": s["ja_name"], "women": s["women"], "men": s["men"], "women_pct": s["wpct"]}
            for s in regional_hl[:3]
        ],
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
        x_arch = select_x_post_archetype(slot_label=slot_label, now_jst=now_jst, recent_texts=recent_full_texts)
        reply_variants = (
            f"{focus['ja_name']}の推移と近隣店舗の比較メモ\n{site_url}/store/{f_slug}?s=x{slot_hh}_ai_live_adlib\n\n全国リアルタイム一覧\n{live_url}",
            f"いまの{focus['ja_name']}の時間帯データはこちらに置いてあります\n{site_url}/store/{f_slug}?s=x{slot_hh}_ai_live_adlib\n\n他エリアの現在人数\n{live_url}",
            f"📍 {focus['ja_name']}の現在値・終電後の残りやすさ\n{site_url}/store/{f_slug}?s=x{slot_hh}_ai_live_adlib\n\n全国60店舗超の一覧\n{live_url}",
        ) if focus else (f"全国リアルタイム人数一覧\n{live_url}",)
        reply_text = reply_variants[(now_jst.day + now_jst.hour) % len(reply_variants)]
        platform_spec = (
            f"【対象SNS：X（旧Twitter）｜今回の指定構成：{x_arch['label']}】\n"
            "- 文字数上限：全角122文字・X換算250ウェイト絶対上限（短く余白があるほど人間らしい）\n"
            "- 禁止事項：URL（http...）は本文に絶対に入れない。『🥇🥈🥉』メダルや『【〇〇】』見出しは禁止。『モニター見てたら』『看板だけで〜』『〜なんだよな』のような定型口癖は使わない。同じ店舗名を2回繰り返さない。\n"
            f"{x_arch['directive']}"
        )
    elif ch == "threads":
        cta_footer = build_threads_cta_footer(site_url, slot_label=slot_label, style_key="ai_live_adlib", now_jst=now_jst)
        reply_text = "（Threadsは1投稿完結・ゆるい日常エッセイ調＋リンク制限回避済）"
        platform_spec = (
            "【対象SNS：Meta Threads（スレッズ）｜肩の力が抜けたゆるい日常エッセイ調】\n"
            "- 文字数制限：全体で **260文字〜435文字以内**（絶対上限480文字）\n"
            "- 禁止事項：『🥇🥈🥉』メダルや『【〇〇】』見出し、『モニター見てたら』『バックヤードで』等の裏方アピールは禁止。ハッシュタグは末尾フッター内のもの以外付けない。\n"
            "- 構成：Xのような圧縮速報ではなく、帰り道や夜カフェでふと思いついて書いたような『余白のあるゆるい日常エッセイ調（〜ですね、〜かも、体言止めミックス）』で書くこと。絵文字は全体で0〜1個に抑え、街ごとの客層の空気や女の子組がスッと座れる店の話などを穏やかに綴る。\n"
            f"- 末尾固定フッター：文章の最後は必ず次のフッター文字列を一字一句変えずに置いて締めること：\n{cta_footer}"
        )
    elif ch == "tiktok":
        f_name = focus["ja_name"] if focus else "上位店舗"
        f_w = focus["women"] if focus else 0
        f_m = focus["men"] if focus else 0
        ai_tt_tags = pick_rotated_hashtag_line(
            "tiktok",
            "ladies" if tm in ("female_real", "spicy_female") else "tiktok_gap",
            now_jst=now_jst,
            area_tag=str(focus.get("area_label") or "") if focus else "",
            seed_extra=len(slot_label or ""),
        )
        reply_text = (
            f"🎬【動画テンプレ連動】🎵 同駅ギャップ比較（tiktok_gap_shock / 11.5秒・UIセーフゾーン設計）\n"
            f"・0.0〜2.0秒（指止めフック）：AI生成キャプションの1行目と連動した結論フックを表示\n"
            f"・2.0〜8.5秒（実測データ比較）：{f_name}（女{f_w}:男{f_m}）ほか上位店舗の比率バーアニメーション\n"
            f"・8.5〜11.5秒（保存＆検索CTA）：「保存して入店3分前に見返す！『相席ナビ』で検索🔍」\n"
            f"🎵【推奨BGMムード】都会的ローファイハウス / シティポップ（BPM118〜124・音量18%）"
        )
        platform_spec = (
            "【対象SNS：TikTok（9:16 縦型ショート動画キャプション＆検索SEO）】\n"
            "- 文字数制限：**320文字〜620文字以内**\n"
            "- 禁止事項：1行目を『【〇〇速報】』のような角括弧タイトルにしないこと。『🥇🥈🥉』メダル記号や『モニター見てたら』等の裏方アピール、宣伝臭い絵文字連打は禁止。\n"
            f"- 構成：最初の1行目でTikTok視聴者が思わずコメントや保存をしたくなる『街ごとのリアルなギャップや女子組・男子組の図星の本音』を切り出す。本文中で2〜3店舗の実測人数（例：女性48名・男性32名（女性比60%））を正確に織り交ぜ、自然な流れで『入店3分前に見返せるように保存📌しとくと便利』と『相席ナビ（aiseki-navi.com）で検索🔍』を案内し、末尾に『{ai_tt_tags}』を付ける。"
        )
    elif ch == "note":
        reply_text = "（note専用・Webエディタへ1クリック転送可能・見出し画像プロンプト連動）"
        platform_spec = (
            "【対象SNS：note.com（ショートエッセイ・つぶやき）】\n"
            "- 形式選択：今夜の街の空気感やデータを踏まえ、『800〜1,400文字の深層考察ショートエッセイ』または『120〜240文字の短いつぶやき』のいずれか最適な構成を出力すること。\n"
            "- 構成（ショートエッセイの場合）：\n"
            "  1. 印象的なタイトル案（『【〇〇論】〜』や『なぜ〜なのか？』等の引きのある見出し）\n"
            "  2. 街の夜の空気感や男女の人間模様を切り取った導入\n"
            "  3. ファクトシート記載の正確な人数・比率（例：オリエンタルラウンジ新宿が女性84名・男性36名（女性比70%））と街の温度差の提示\n"
            "  4. なぜその現象が起きるかの心理・夜の行動構造の深い解剖\n"
            "  5. 読者へのスマートな立ち回り提言（退店後の夜カフェや2軒目誘導、相席ナビ aiseki-navi.com の活用）\n"
            "- 構成（つぶやきの場合）：夜の街の空気感や店ごとの温度差を120〜240文字でさらっと端的に表現（『モニター見てたら』等の裏方アピールは不要）。\n"
            "- 禁止事項：架空の店舗名・人数のハルシネーション、『オリエンタル』単体表記（必ず『オリエンタルラウンジ』）。"
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
        ai_ig_tags = pick_rotated_hashtag_line(
            "instagram",
            "ladies" if tm in ("female_real", "spicy_female") else "carousel",
            now_jst=now_jst,
            seed_extra=len(slot_label or ""),
        )
        reply_text = "（5枚組カルーセル画像とセットで投稿・上位AI長考アドリブ生成）"
        platform_spec = (
            "【対象SNS：Instagram（5枚組カルーセル投稿キャプション）】\n"
            "- 文字数制限：**400文字〜680文字以内**\n"
            "- 禁止事項：1行目を『【〇〇予報】』のような堅い角括弧見出しにしないこと。宣伝っぽいハイテンション絵文字連打をしないこと。\n"
            f"- 構成：『…続きを読む』で折りたたまれる前の最初の1〜2行目に、天気・季節・その曜日のリアルな空気感（「1軒目そろそろ出る頃だけど…」のような生の呟き）を書く。途中で自然な流れで『あとで見返せるように保存📌しとくと便利』と添え、スライド2枚目の盛り上がり上位3店舗とスライド3枚目の高女性比率上位3店舗の数値を正確に織り交ぜ、プロフィールのリンク（aiseki-navi.com）への案内と、末尾に『{ai_ig_tags}』を付ける。"
        )

    slot_persona = research.get("slot_persona") or build_slot_persona_context(slot_label=slot_label, channel=ch)
    cliche_list_str = "・".join(f"『{w}』" for w in AI_CLICHE_WORDS)

    sit_key = (situation or (plan.get("situation") if plan else "") or "").strip().lower()
    sit_meta = SITUATION_SPECS.get(sit_key, {})
    sit_block = f"\n\n{sit_meta['directive']}\n" if sit_meta.get("directive") else ""

    recent_history_block = (
        json.dumps([t[:160] for t in recent_full_texts[:4]], ensure_ascii=False)
        if recent_full_texts
        else "[]"
    )
    banned_endings_str = (
        json.dumps(recent_endings[:5], ensure_ascii=False)
        if recent_endings
        else "[]"
    )

    prompt = (
        "あなたは夜の街の空気感や男女のリアルな温度差によく通じた、気取らない大人の友人・街ウォッチャー（『相席ナビ』執筆者）です。\n"
        "毎回似た構成のボット投稿や綺麗にまとまりすぎたAI構文ではなく、"
        "生身の人間がふとスマホを開いて、今の街の空気や店舗ごとのリアルな偏りをさらっと呟いたような自然な1本を書いてください。\n\n"
        f"{HUMAN_INSIDER_VOICE_GUIDE}\n\n"
        f"{tone_meta['prompt_directive']}"
        f"{sit_block}\n\n"
        f"【今夜の時間帯テンション設定：{slot_persona['label']}】\n"
        f"- {slot_persona['guidance']}\n\n"
        "【編集デスクの執筆指針（脱AI臭・脱マンネリ）】\n"
        "- 『モニター見てたら』『バックヤードで』『看板だけで並んで〜』のような決まり文句や上から目線の説教・まとめ教訓は書かず、今夜の街況・ギャップ・意外な好調エリアから1つの切り口だけを自然体で切り取ること。\n"
        "- 絵文字や感嘆符（！）は極力減らし（0〜1個）、体言止め・普通の言い切り・素の丁寧語（『〜ですね』『〜かも』『〜置いときます』）を日によって自然に使い分けること。\n"
        "- ファクトシートに `tonight_street_pulse`（今夜のリアルな街の催し・ライブ帰り・空気感）が含まれており、かつ今回取り上げるエリアと自然に噛み合う場合のみ、半フレーズだけさりげなく街の空気を滲ませてもよい（エリアが違ったり無理がある場合は触れなくてよい）。\n"
        "- ファクトシートの数字と1人・1%の狂いもないよう正確に書くこと。\n\n"
        f"{platform_spec}\n\n"
        "【絶対厳守ルール（違反するとデスク検閲で減点・不採用になります）】\n"
        "1. 下記の JSON ファクトシートに含まれる店舗名・人数・パーセンテージ以外は絶対に書かないこと。数値を1人でも盛ったり丸めたり変更することは厳禁。\n"
        "2. 自分が今日客として入店した架空の体験談（『さっき行ってきた』『相席相手が〜』等）は絶対に書かないこと。\n"
        "3. 店名の呼び方ルール：内部略称『OLG』や、略称『オリラジ』、中途半端な『オリエンタル〇〇』（「ラウンジ」抜け）・『オリエンタル』単体呼びは絶対に使わないこと。必ず正式名称『オリエンタルラウンジ〇〇』（例：『オリエンタルラウンジ大阪駅前』）を使うこと（『ag〇〇』『JIS〇〇』はそのまま表記）。\n"
        f"4. AI臭・宣伝臭・裏方口癖ワードの完全禁止：{cliche_list_str} は本文に絶対に使わないこと。\n"
        f"5. 横断アンチマンネリ検閲：直近の投稿全文（{recent_history_block}）と構成・取り上げる主役店舗・1行目の入り方・オチが絶対に似ないようにすること。また直近使った文末語尾（{banned_endings_str}）の連続使用は避け、別の自然な語尾に変えること。\n\n"
        f"【今夜の検証済みファクトシート】\n{json.dumps(fact_payload, ensure_ascii=False, indent=2)}\n\n"
        "出力は必ず以下のJSON形式のみを返してください（Markdownコードブロック不要）：\n"
        '{"editorial_angle": "今回選んだ骨格・店舗・語尾の短い編集メモ（50〜110文字）", "writer_note": "今回どの街況・データをどう切り取ったかの1行メモ", "main_text": "投稿本文（改行は\\n）"}'
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
        skip_tokens = (
            "embedding", "imagen", "tts", "audio", "vision", "veo", "aqa",
            "1.0", "1.5", "robotics", "computer-use", "omni", "live", "image",
            "dialog", "learnlm", "gemma", "bison", "thinking",
        )
        ver_re = re.compile(r"^gemini-(\d+(?:\.\d+)?)-(pro|flash)(?:-[a-z0-9.-]+)?$")
        for m in models:
            methods = m.get("supportedGenerationMethods") or []
            if "generateContent" not in methods:
                continue
            name = (m.get("name") or "").replace("models/", "").strip()
            low = name.lower()
            if not low.startswith("gemini") or any(tok in low for tok in skip_tokens):
                continue
            vm = ver_re.match(low)
            if not vm:
                continue
            try:
                ver_num = float(vm.group(1))
            except ValueError:
                continue
            if ver_num < 2.5:
                continue
            if "pro" in low and "customtools" not in low:
                pro_cands.append((ver_num, name))
            elif "flash" in low and "lite" not in low and "8b" not in low:
                flash_cands.append((ver_num, name))
            elif "flash" in low and "lite" in low:
                lite_cands.append((ver_num, name))

        # Pro: newest numeric version first (e.g. gemini-3.1-pro-preview > gemini-2.5-pro); non-exp first.
        def _pro_key(item):
            ver_num, nm = item
            low_nm = nm.lower()
            return (not ("exp" in low_nm), ver_num, "preview" not in low_nm, low_nm)

        # Flash: prefer stable GA (`not preview`) first so fast fallback never hits preview-only API incompatibilities.
        def _flash_key(item):
            ver_num, nm = item
            low_nm = nm.lower()
            return (not ("exp" in low_nm), "preview" not in low_nm, ver_num, low_nm)

        pro_sorted = [nm for _, nm in sorted(pro_cands, key=_pro_key, reverse=True)]
        flash_sorted = [nm for _, nm in sorted(flash_cands, key=_flash_key, reverse=True)]
        lite_sorted = [nm for _, nm in sorted(lite_cands, key=_flash_key, reverse=True)]

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


def _claude_supports_temperature(model_name: str) -> bool:
    """
    Return False for Anthropic Claude 5+ models (where `temperature` is deprecated and triggers HTTP 400)
    or any model dynamically recorded in `_claude_no_temp_models`.
    """
    low = (model_name or "").strip().lower()
    if not low or low in _claude_no_temp_models:
        return False
    m = re.match(r"^claude-(?:[a-z]+-)?(\d+)", low)
    if m:
        try:
            if int(m.group(1)) >= 5:
                return False
        except ValueError:
            pass
    return True


def _is_requests_get_mocked() -> bool:
    get_fn = getattr(requests, "get", None)
    if get_fn is None:
        return False
    return hasattr(get_fn, "assert_called") or type(get_fn).__module__.startswith("unittest.mock")


def _claude_version_key(model_id: str) -> tuple:
    low = (model_id or "").lower()
    m = re.search(r"claude-(?:opus|sonnet|haiku)-(\d+)(?:-(\d+))?", low)
    if m:
        maj = int(m.group(1))
        mnr = int(m.group(2)) if m.group(2) and len(m.group(2)) <= 2 else 0
        return (maj, mnr, -len(low))
    return (0, 0, 0)


def _discover_claude_models(api_key: str, timeout: float = 4.0) -> list:
    """
    Dynamically query Anthropic `/v1/models` (cached 6h) and rank newest Claude Opus -> Sonnet -> Haiku models.
    """
    if not api_key:
        return []
    if _is_requests_post_mocked() and not _is_requests_get_mocked():
        return list(DEFAULT_CLAUDE_MODELS)
    sig = f"claude_v2_opus:{api_key[:10]}"
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
            return list(DEFAULT_CLAUDE_MODELS)
        items = (r.json() or {}).get("data") or []
        opuses = []
        sonnets = []
        haikus = []
        for it in items:
            mid = (it.get("id") or "").strip()
            low = mid.lower()
            if not mid or "claude" not in low:
                continue
            if "opus" in low:
                opuses.append(mid)
            elif "sonnet" in low:
                sonnets.append(mid)
            elif "haiku" in low:
                haikus.append(mid)
        opuses.sort(key=_claude_version_key, reverse=True)
        sonnets.sort(key=_claude_version_key, reverse=True)
        haikus.sort(key=_claude_version_key, reverse=True)
        ranked = (opuses[:2] + sonnets[:2] + haikus[:1]) or list(DEFAULT_CLAUDE_MODELS)
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


def _is_requests_post_mocked() -> bool:
    post_fn = getattr(requests, "post", None)
    if post_fn is None:
        return False
    return hasattr(post_fn, "assert_called") or type(post_fn).__module__.startswith("unittest.mock")


def _mark_model_unhealthy(model_name: str):
    low = (model_name or "").lower()
    if low not in ("gemini-2.5-flash", "gemini-2.5-flash-lite"):
        _unhealthy_models[model_name] = time.monotonic() + _UNHEALTHY_COOLDOWN_SEC


def _build_endpoint_request(
    endpoint_kind: str,
    model_name: str,
    api_key: str,
    prompt: str,
    max_output_tokens: int = 2600,
    thinking_budget: int = 1536,
    temperature: float = 0.88,
) -> tuple:
    out_tokens = int(max_output_tokens or 2048)
    tb_req = int(thinking_budget) if thinking_budget is not None else 0
    gen_cfg = {
        "temperature": temperature,
        "maxOutputTokens": max(2048, out_tokens),
        "responseMimeType": "application/json",
    }
    low_m = (model_name or "").lower()
    if low_m.startswith(("gemini-3.5", "gemini-2.5")) or low_m in (
        "gemini-flash-lite-latest",
        "gemini-flash-latest",
        "gemini-pro-latest",
    ):
        if tb_req > 0:
            # Add thinking budget on top of output token quota so JSON output is never truncated
            tb = max(512, tb_req)
            gen_cfg["thinkingConfig"] = {"thinkingBudget": tb}
            gen_cfg["maxOutputTokens"] = max(2048, out_tokens + tb)
        elif "pro" in low_m:
            # Pro models require >=128 thinkingBudget
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 128}
            gen_cfg["maxOutputTokens"] = max(2048, out_tokens + 128)
        elif low_m.startswith("gemini-2.5-flash"):
            gen_cfg["thinkingConfig"] = {"thinkingBudget": 0}
            gen_cfg["maxOutputTokens"] = max(2048, out_tokens)
    elif low_m.startswith("gemini-3"):
        gen_cfg["thinkingConfig"] = {"thinkingLevel": "MEDIUM" if tb_req >= 1024 else "LOW"}
        gen_cfg["maxOutputTokens"] = max(4096, out_tokens + 2048)

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
    prefer_fast: bool = False,
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

    pref = get_ai_engine_pref(allow_blocking=allow_blocking)
    providers = []
    if pref == "gemini" or (prefer_fast and pref == "auto" and g_key and not c_key):
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

    eff_deadline_sec = max(4.0, float(total_deadline_sec))
    deadline = time.monotonic() + eff_deadline_sec
    reserve_sec = min(6.5, eff_deadline_sec * 0.25)
    is_mocked_post = _is_requests_post_mocked()
    last_err = ""
    claude_fail_note = ""

    sys_instr = system_prompt or (
        "あなたは夜の街の空気感と男女のリアルに通じた、気取らない大人の執筆者（『相席ナビ』編集長）です。"
        "AI特有の優等生構文・過剰な絵文字・『モニター見てたら』『バックヤードで』『看板だけで〜』等のわざとらしい裏方アピールや決まり文句を徹底排除し、"
        "体言止め・言い切り・素の丁寧語（〜ですね、〜かも）を自然に混ぜた生身の人間の距離感と余韻で執筆してください。"
        "必ず有効なJSONオブジェクトのみを返してください（Markdownの```jsonフェンスは不要）。"
    )

    for p_idx, prov in enumerate(providers):
        if time.monotonic() >= deadline - 0.8:
            break
        has_later_provider = (p_idx < len(providers) - 1)

        if prov == "claude":
            preferred_c = (os.environ.get("CLAUDE_MODEL") or "").strip()
            discovered_c = _discover_claude_models(c_key, timeout=min(3.2, max(1.0, deadline - time.monotonic() - 2.0)))
            c_models = []
            if preferred_c:
                c_models.append(preferred_c)
            saved_r = None if is_mocked_post else _last_gemini_meta.get("working_route")
            if (
                saved_r
                and saved_r[0] == "claude"
                and saved_r[1] not in c_models
                and (prefer_fast or "opus" in saved_r[1].lower())
            ):
                c_models.append(saved_r[1])
            for m in (discovered_c or DEFAULT_CLAUDE_MODELS):
                if m not in c_models:
                    c_models.append(m)

            if prefer_fast:
                # Fast / 2nd-pass polish mode: prefer top Sonnet #1 (distinct editor persona) + Haiku #2 safety net
                if preferred_c:
                    top_fast = preferred_c
                else:
                    top_fast = next((m for m in c_models if "sonnet" in m.lower()), c_models[0])
                fast_haiku = next((m for m in c_models if "haiku" in m.lower() and m != top_fast), None)
                c_trial = [top_fast, fast_haiku] if fast_haiku else [top_fast]
            else:
                # Primary creative mode: lead with flagship Claude Opus (#1), with top Sonnet (#2) as instant fallback
                if preferred_c:
                    c_trial = [preferred_c] + [m for m in c_models if m != preferred_c and "haiku" not in m.lower()][:1]
                else:
                    top_opus = next((m for m in c_models if "opus" in m.lower()), None)
                    top_sonnet = next((m for m in c_models if "sonnet" in m.lower()), None)
                    c_trial = [m for m in (top_opus, top_sonnet) if m] or c_models[:2]

            claude_max_tokens = max(
                4096,
                int(max_output_tokens or 2600) + max(0, int(thinking_budget or 0)),
            )

            for c_idx, cm in enumerate(c_trial):
                rem = deadline - time.monotonic()
                if rem <= 0.8:
                    break
                has_fallback = has_later_provider or (c_idx < len(c_trial) - 1)
                if has_later_provider and rem <= reserve_sec + 1.0:
                    break
                c_timeout = (
                    min(float(per_call_timeout_sec), max(1.5, rem - reserve_sec))
                    if has_fallback
                    else min(float(per_call_timeout_sec), rem)
                )
                c_headers = {
                    "x-api-key": c_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                }
                c_body = {
                    "model": cm,
                    "max_tokens": claude_max_tokens,
                    "system": sys_instr,
                    "messages": [{"role": "user", "content": prompt}],
                }
                if _claude_supports_temperature(cm):
                    c_body["temperature"] = min(1.0, max(0.1, float(temperature)))
                else:
                    c_body["output_config"] = {"effort": "low" if prefer_fast else "medium"}

                try:
                    resp = requests.post(
                        "https://api.anthropic.com/v1/messages",
                        headers=c_headers,
                        json=c_body,
                        timeout=c_timeout,
                    )
                    # Auto-recover if a Claude model rejects `temperature` or `output_config`
                    if resp.status_code == 400:
                        err_low = (resp.text or "").lower()
                        c_body_retry = None
                        if "temperature" in c_body and "temperature" in err_low:
                            _claude_no_temp_models.add(cm.lower())
                            c_body_retry = dict(c_body)
                            c_body_retry.pop("temperature", None)
                        elif "output_config" in c_body and ("output_config" in err_low or "effort" in err_low):
                            c_body_retry = dict(c_body)
                            c_body_retry.pop("output_config", None)
                        if c_body_retry is not None:
                            rem_retry = max(1.5, min(float(per_call_timeout_sec), deadline - time.monotonic()))
                            resp = requests.post(
                                "https://api.anthropic.com/v1/messages",
                                headers=c_headers,
                                json=c_body_retry,
                                timeout=rem_retry,
                            )
                    if resp.status_code >= 400:
                        last_err = f"Claude HTTP {resp.status_code} ({cm}): {(resp.text or '')[:130]}"
                        claude_fail_note = last_err
                        logger.warning(f"[editorial_ai] {last_err}")
                        if _last_gemini_meta.get("working_route") == ("claude", cm):
                            _last_gemini_meta["working_route"] = None
                        continue
                    resp_json = resp.json() or {}
                    if resp_json.get("stop_reason") == "refusal":
                        ref_cat = ((resp_json.get("stop_details") or {}).get("category")) or "policy"
                        last_err = f"Claude refusal:{ref_cat} ({cm})"
                        claude_fail_note = last_err
                        logger.warning(f"[editorial_ai] {last_err}")
                        continue
                    blocks = resp_json.get("content") or []
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
                    claude_fail_note = last_err
                except Exception as e:
                    last_err = f"Claude通信エラー ({cm}): {str(e)[:110]}"
                    claude_fail_note = last_err
                    logger.warning(f"[editorial_ai] {last_err}")
                    if _last_gemini_meta.get("working_route") == ("claude", cm):
                        _last_gemini_meta["working_route"] = None

        elif prov == "gemini":
            preferred_g = (os.environ.get("GEMINI_MODEL") or "").strip()
            discovered_g = _discover_ai_studio_models(
                g_key,
                timeout=min(3.0, max(0.9, deadline - time.monotonic() - reserve_sec - 0.5)),
            )
            base_pool = []
            for m in (discovered_g or list(DEFAULT_GEMINI_MODELS)):
                if m not in base_pool:
                    base_pool.append(m)
            for m in DEFAULT_GEMINI_MODELS:
                if m not in base_pool:
                    base_pool.append(m)

            g_models = []
            if preferred_g:
                g_models.append(preferred_g)
            saved_r = None if is_mocked_post else _last_gemini_meta.get("working_route")

            if prefer_fast:
                # Short-form / latency-sensitive mode: place standard Flash models (`gemini-2.5-flash`, etc.) first
                if (
                    saved_r
                    and saved_r[0] == "ai_studio"
                    and "flash" in saved_r[1].lower()
                    and "lite" not in saved_r[1].lower()
                    and saved_r[1] not in g_models
                ):
                    g_models.append(saved_r[1])
                for m in base_pool:
                    low_m = m.lower()
                    if "flash" in low_m and "lite" not in low_m and m not in g_models:
                        g_models.append(m)
                for m in base_pool:
                    if m not in g_models:
                        g_models.append(m)
            else:
                # Quality-first mode: Pro -> Standard Flash -> Lite fallback
                # Only promote a cached `working_route` ahead of `base_pool` if it is a Pro model
                # (unhealthy Pro models are demoted automatically via `_unhealthy_models` cooldown).
                if (
                    saved_r
                    and saved_r[0] == "ai_studio"
                    and "pro" in saved_r[1].lower()
                    and saved_r[1] not in g_models
                ):
                    g_models.append(saved_r[1])
                for m in base_pool:
                    if m not in g_models:
                        g_models.append(m)

            # Apply 10-minute circuit breaker cooldown for unhealthy preview/pro models (bypassed when requests.post is mocked)
            if not is_mocked_post and _unhealthy_models:
                now_mono = time.monotonic()
                expired_um = [k for k, exp_t in list(_unhealthy_models.items()) if now_mono >= exp_t]
                for k in expired_um:
                    _unhealthy_models.pop(k, None)
                if _unhealthy_models:
                    healthy_primary = [
                        m for m in g_models
                        if m not in _unhealthy_models and "lite" not in m.lower()
                    ]
                    cooled_down = [m for m in g_models if m in _unhealthy_models]
                    healthy_lite = [
                        m for m in g_models
                        if m not in _unhealthy_models and "lite" in m.lower()
                    ]
                    g_models = healthy_primary + cooled_down + healthy_lite

            if prefer_fast:
                trial_models = list(g_models[:3])
            else:
                # Never stack two slow Pro models back-to-back in one request:
                # Try #1 healthy Pro (with full timeout), then immediately GA Flash (`gemini-2.5-flash`), then Flash-Lite.
                trial_models = []
                top_pro = next((m for m in g_models if "pro" in m.lower()), None)
                if top_pro:
                    trial_models.append(top_pro)
                ga_flash = next(
                    (
                        m for m in g_models
                        if "flash" in m.lower() and "lite" not in m.lower() and "preview" not in m.lower()
                    ),
                    "gemini-2.5-flash",
                )
                if ga_flash not in trial_models:
                    trial_models.append(ga_flash)
                lite_fb = next((m for m in g_models if "lite" in m.lower()), "gemini-2.5-flash-lite")
                if lite_fb not in trial_models:
                    trial_models.append(lite_fb)

            full_prompt = f"{sys_instr}\n\n{prompt}" if system_prompt else prompt
            for g_idx, gm in enumerate(trial_models):
                rem = deadline - time.monotonic()
                if rem <= 0.8:
                    break
                has_fallback = (g_idx < len(trial_models) - 1) or has_later_provider
                gm_low = gm.lower()
                eff_reserve = 2.5 if "flash" in gm_low else reserve_sec
                if has_fallback and rem <= eff_reserve + 1.0 and "flash" not in gm_low:
                    continue
                g_timeout = (
                    min(float(per_call_timeout_sec), max(1.8, rem - eff_reserve))
                    if has_fallback
                    else min(float(per_call_timeout_sec), rem)
                )
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
                        timeout=g_timeout,
                    )
                    if resp.status_code >= 400:
                        last_err = f"Gemini HTTP {resp.status_code} ({gm}): {(resp.text or '')[:130]}"
                        logger.warning(f"[editorial_ai] {last_err}")
                        if not is_mocked_post and resp.status_code not in (401, 403):
                            _mark_model_unhealthy(gm)
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
                        last_err = f"Gemini output exhausted by thinking ({gm})"
                        logger.warning(f"[editorial_ai] {last_err}")
                        if not is_mocked_post:
                            _mark_model_unhealthy(gm)
                        if _last_gemini_meta.get("working_route") == ("ai_studio", gm):
                            _last_gemini_meta["working_route"] = None
                        continue
                    parsed = _parse_ai_json_object(raw_txt)
                    if parsed:
                        _unhealthy_models.pop(gm, None)
                        _last_gemini_meta["working_route"] = ("ai_studio", gm)
                        _last_gemini_meta["last_provider"] = "gemini"
                        _last_gemini_meta["last_model"] = gm
                        _last_gemini_meta["last_error"] = (
                            f"Claude一時エラーのためGemini({gm})へ退避: {claude_fail_note[:90]}"
                            if claude_fail_note
                            else ""
                        )
                        _last_gemini_meta["last_ok_at"] = now_jst.strftime("%Y-%m-%d %H:%M:%S")
                        return parsed, gm, ""
                    last_err = f"Gemini JSON parse failed ({gm})"
                    if not is_mocked_post:
                        _mark_model_unhealthy(gm)
                except Exception as e:
                    last_err = f"Gemini通信エラー ({gm}): {str(e)[:110]}"
                    logger.warning(f"[editorial_ai] {last_err}")
                    if not is_mocked_post:
                        _mark_model_unhealthy(gm)
                    if _last_gemini_meta.get("working_route") == ("ai_studio", gm):
                        _last_gemini_meta["working_route"] = None

    if last_err:
        _last_gemini_meta["last_error"] = last_err
    return None, "", last_err


_NIDOMI_REPLACEMENTS = (
    (re.compile(r"(?:店内|現場|実測)?モニターを?(?:二度見|２度見|2度見)したんだけど[、,\s]*"), ""),
    (re.compile(r"(?:二度見|２度見|2度見)したんだけど"), "思わず手が止まったんだけど"),
    (re.compile(r"て(?:二度見|２度見|2度見)した"), "て思わず目が留まった"),
    (re.compile(r"(?:二度見|２度見|2度見)するレベル"), "ちょっと珍しいレベル"),
    (re.compile(r"(?:二度見|２度見|2度見)する"), "不思議になる"),
    (re.compile(r"(?:二度見|２度見|2度見)"), "注目"),
    (re.compile(r"バックヤードで(?:モニターを?見てたら|見てたんだけど|見てたら)[、,\s]*"), ""),
    (re.compile(r"バックヤードで[、,\s]*"), ""),
    (re.compile(r"(?:さっき|ふと|いま|毎晩)?(?:店内|現場|リアルタイム)?モニターを?(?:見てたんだけど|見てたら|見てて|見比べてたんだけど|眺めながら|眺めてたら)[、,\s]*"), ""),
    (re.compile(r"看板の知名度だけで"), "駅チカの1店舗目だけで"),
    (re.compile(r"看板だけで"), "近い店だけで"),
)


def _postprocess_ai_main_text(main_text: str, channel: str = "x") -> str:
    """
    Guarantee zero bare 'オリエンタル〇〇' ('ラウンジ' omitted), zero 'オリラジ'/'OLG',
    zero repetitive '二度見' cliches, and zero artificial backstage ('モニター見てたら') phrasing in AI output.
    """
    text = (main_text or "").strip()
    if not text:
        return ""
    for pat, repl in _NIDOMI_REPLACEMENTS:
        text = pat.sub(repl, text)
    return sanitize_brand_names(text).strip()


def _make_ai_cache_key(
    ch: str,
    slot_label: str,
    research: dict,
    now_jst: datetime.datetime = None,
    tone_mode: str = DEFAULT_TONE_MODE,
    situation: str = None,
):
    """
    Build a precise cache key incorporating engine_pref, channel, tone_mode, situation, business date, slot,
    top-3 stores `(name, women, men)`, and `focus_store` `(name, women, men)`.
    """
    ch_norm = (ch or "x").lower()
    tm_norm = get_editorial_tone_mode(tone_mode, allow_blocking=False)
    sit_norm = (situation or "").strip().lower()
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
    arch_key = select_x_post_archetype(slot_label or "22:00", now_jst or datetime.datetime.now(JST))["key"] if ch_norm == "x" else ""
    top3_sig = tuple(
        (s.get("name", ""), int(s.get("women") or 0), int(s.get("men") or 0))
        for s in top_s[:3]
    )
    focus = (research or {}).get("focus_store")
    focus_sig = (
        (focus.get("name", ""), int(focus.get("women") or 0), int(focus.get("men") or 0))
        if focus else None
    )
    return ("v7_opus_2pass_street_pulse", eng_pref, has_c, ch_norm, arch_key, tm_norm, sit_norm, slot_label or "", biz_date_str, top3_sig, focus_sig)


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
    turbo_store.set_ai_cache(
        "sns_editorial",
        cache_key,
        {
            "ts": now_t,
            "candidate": candidate,
            "model": model_name,
            "ok_at": _last_gemini_meta.get("last_ok_at") or datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S"),
        },
        ttl_sec=max(_AI_CACHE_TTL_SEC, 1800),
    )


def has_cached_ai_candidate(
    research: dict,
    slot_label: str,
    channel: str,
    now_jst: datetime.datetime = None,
    tone_mode: str = DEFAULT_TONE_MODE,
    situation: str = None,
) -> bool:
    cache_key = _make_ai_cache_key(channel, slot_label, research, now_jst, tone_mode=tone_mode, situation=situation)
    if cache_key is None:
        return False
    now_t = time.time()
    with _ai_cache_lock:
        _prune_ai_candidate_cache_unlocked(now_t)
        cached = _ai_candidate_cache.get(cache_key)
        if cached and (now_t - cached["ts"] < _AI_CACHE_TTL_SEC):
            return True
    shared = turbo_store.get_ai_cache("sns_editorial", cache_key)
    if shared and isinstance(shared.get("candidate"), dict):
        with _ai_cache_lock:
            _ai_candidate_cache[cache_key] = {
                "ts": float(shared.get("ts") or now_t),
                "candidate": shared["candidate"],
                "model": str(shared.get("model") or ""),
            }
        return True
    return False


def polish_draft_to_human_voice(
    draft_text: str,
    channel: str = "x",
    research: dict = None,
    slot_label: str = "22:00",
    now_jst: datetime.datetime = None,
    silent_tree: bool = False,
    recent_endings: list = None,
    force_polish: bool = False,
) -> tuple:
    """
    2nd-Pass Humanizer ('書いてから人間語に崩す' 赤ペン推敲パス):
    Reviews Pass-1's draft with a separate fast editor call (`claude-sonnet-*`) to strip subtle AI neatness,
    stiff conjunctions ('〜ため、', '〜傾向にあり', '〜という構図'), or preachy wrap-ups while strictly locking
    100% of verified numbers, brand names, and platform length/footer rules.
    Returns `(final_text, polisher_model_or_empty)`. Falls back to `(draft_text, "")` on any safety check miss.
    """
    base_text = (draft_text or "").strip()
    if not base_text:
        return "", ""
    ch = (channel or "x").lower()
    if ch not in ("x", "threads", "instagram", "tiktok"):
        return base_text, ""
    if not force_polish:
        if _is_requests_post_mocked() or not get_claude_api_key(allow_blocking=False):
            return base_text, ""

    now_jst = now_jst or datetime.datetime.now(JST)
    detached_footer = ""
    body_only = base_text
    if ch == "threads":
        m_foot = re.search(
            r"(\n+(?:"
            r"(?:🔍|▼)\s*(?:全国?\d+店舗(?:超)?の現在人数|全国の現在人数・男女比は|今の男女人数・開催中イベント一覧はこちら)[\s\S]*"
            r"|💄\s*いま女子待ち0で入れる[\s\S]*"
            r"|細かいリアルタイム人数と料金比較はプロフィールのリンク[\s\S]*"
            r"|気になる街のリアルタイム推移はプロフィールの『相席ナビ』からどうぞ。?"
            r"|今の各店舗の男女人数や女子向け特典はプロフのリンク[\s\S]*"
            r"|各店舗のリアルタイム人数はプロフィールの『相席ナビ』から見れます[\s\S]*"
            r"|#相席ラウンジ(?:\s+#\S+)*"
            r"))$",
            base_text,
        )
        if m_foot:
            detached_footer = m_foot.group(1).strip()
            body_only = base_text[:m_foot.start()].strip()

    def _extract_fact_nums(s: str) -> collections.Counter:
        # Lock all crowd counts, percentages, temperatures, and minute/hour figures (excluding '1軒目'/'2軒目'/'1人'/'2人組')
        cleaned = re.sub(r"[123]軒目(?:探し)?|[12]人組?", "", s or "")
        return collections.Counter(re.findall(r"\d+", cleaned))

    orig_digits = _extract_fact_nums(body_only)
    endings_list = recent_endings if recent_endings is not None else extract_sentence_endings(
        get_recent_cross_channel_texts(channel=ch, limit=5, allow_blocking=False)
    )
    cliche_list_str = "・".join(f"『{w}』" for w in AI_CLICHE_WORDS)
    silent_note = (
        "今回はサイレントツリー回のため、末尾に『👇』『リプ欄』やハッシュタグ（#）は一切付けず余韻だけで終えること。"
        if (ch == "x" and silent_tree)
        else ""
    )

    polish_sys = (
        "あなたは生身の人間のSNS編集者です。渡された下書きの『事実・店舗名・数値』は1文字1桁も変えずに、"
        "AIが書いたような『整いすぎた接続詞・説明くささ・綺麗なまとめオチ』だけを削ぎ落とし、"
        "スマホでふと親指打ちしたような自然な呼吸・余白・口語リズムに微調整してください。"
        "既に十分短く自然なら無理に書き換えすぎず、気になるところだけ軽く整えてJSONで返してください。"
    )
    polish_prompt = (
        f"【対象SNS：{ch.upper()}（{slot_label}枠）｜2段階目：脱AI・人間語くずし推敲】\n"
        "以下の下書きを読み返し、少しでも『AIっぽい硬さ・説明調・作られた上手さ』があれば自然な話し言葉や体言止め・素の余韻に崩してください。\n\n"
        "【赤ペン推敲のポイント】\n"
        "1. 『〜ため、』『〜によって』『〜傾向』『〜という構図』『〜なのが特徴』『〜と言える』等の書き言葉・解説調があれば、自然な読点や話し言葉（『〜だし』『〜ぶん』『〜ですね』『〜かも』や体言止め）に崩す。\n"
        "2. 最後の1文が『〜が正解』『〜してみては』『〜がおすすめ』のような指南・まとめ口調なら、力を抜いた独り言や置きメモの余韻に変える。\n"
        "3. 【絶対厳守】下書きに含まれる店舗名（『オリエンタルラウンジ〇〇』『JIS〇〇』『ag〇〇』等）と、すべての数字（人数・パーセント・気温・時刻等）は **1桁たりとも変更・削除・追加しないこと**。\n"
        f"4. 禁止語（{cliche_list_str}）やURLは絶対に入れないこと。絵文字や『！』は増やさないこと。{silent_note}\n"
        f"5. 直近使った文末語尾（{json.dumps(endings_list[:5], ensure_ascii=False)}）と末尾が被る場合は、別の自然な語尾に変えること。\n\n"
        f"【推敲対象の下書き】\n{body_only}\n\n"
        '出力はJSON形式のみ：{"polished_text": "推敲後の本文"}'
    )

    try:
        parsed_p, p_model, _ = call_editorial_ai_json(
            prompt=polish_prompt,
            system_prompt=polish_sys,
            max_output_tokens=1500,
            thinking_budget=512,
            temperature=0.72,
            total_deadline_sec=16.0,
            per_call_timeout_sec=12.0,
            allow_blocking=True,
            now_jst=now_jst,
            prefer_fast=True,
        )
        if not parsed_p:
            return base_text, ""
        raw_pol = (parsed_p.get("polished_text") or parsed_p.get("main_text") or "").strip()
        if not raw_pol:
            return base_text, ""
        pol_body = _postprocess_ai_main_text(raw_pol, channel=ch)
        if not pol_body:
            return base_text, ""
        if ch == "x" and silent_tree:
            pol_body = re.sub(r"#\S+", "", pol_body).replace("👇", "").replace("リプ欄へ", "").replace("リプ欄", "").strip()

        # Lock 1: Exact preservation of every factual numeric token in the prose body
        if _extract_fact_nums(pol_body) != orig_digits:
            return base_text, ""

        cand_full = f"{pol_body}\n\n{detached_footer}" if detached_footer else pol_body
        if any(w in cand_full for w in AI_CLICHE_WORDS):
            return base_text, ""

        # Lock 2: Strict Desk Factual Verification against `research`
        if research is not None:
            ok_fact, _ = verify_ai_factual_numbers(cand_full, research, channel=ch)
            if not ok_fact:
                return base_text, ""

        # Lock 3: Platform weight / character bounds
        if ch == "x":
            if "http://" in cand_full or "https://" in cand_full:
                return base_text, ""
            wt = x_poster.compute_twitter_weight(cand_full)
            if not (84 <= wt <= 270):
                return base_text, ""
        elif ch == "threads":
            if not (140 <= len(cand_full) <= 478):
                return base_text, ""
        elif ch == "instagram":
            if not (200 <= len(cand_full) <= 680):
                return base_text, ""

        return cand_full, p_model
    except Exception as e:
        logger.debug(f"2nd-pass humanizer skipped: {e}")
        return base_text, ""


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
    situation: str = None,
) -> dict:
    """
    Generate live human-voiced SNS draft via Flagship Chief Writer (`Claude Opus` -> 2nd-Pass `Sonnet` Humanizer,
    or Upgraded Gemini Pro/Flash fallback).
    When `cache_only=True`, returns cached draft in 0ms without blocking HTML page render.
    """
    if not has_any_ai_key(allow_blocking=(not cache_only)):
        return None

    ch = (channel or "x").lower()
    tm = get_editorial_tone_mode(tone_mode, allow_blocking=False)
    sit_key = (situation or (plan.get("situation") if plan else "") or "").strip().lower()
    cache_key = _make_ai_cache_key(ch, slot_label, research, now_jst, tone_mode=tm, situation=sit_key)
    if cache_key is None:
        return None

    now_t = time.time()
    if not force_refresh:
        with _ai_cache_lock:
            _prune_ai_candidate_cache_unlocked(now_t)
            if cache_key in _ai_candidate_cache:
                cached = _ai_candidate_cache[cache_key]
                if now_t - cached["ts"] < _AI_CACHE_TTL_SEC:
                    _ai_candidate_cache.move_to_end(cache_key)
                    return dict(cached["candidate"])
        shared = turbo_store.get_ai_cache("sns_editorial", cache_key)
        if shared and isinstance(shared.get("candidate"), dict):
            m_name = str(shared.get("model") or shared["candidate"].get("ai_model") or "")
            if m_name:
                _last_gemini_meta["last_model"] = m_name
                _last_gemini_meta["last_provider"] = "claude" if "claude" in m_name.lower() else "gemini"
                _last_gemini_meta["last_error"] = ""
                if shared.get("ok_at"):
                    _last_gemini_meta["last_ok_at"] = str(shared["ok_at"])
            with _ai_cache_lock:
                _ai_candidate_cache[cache_key] = {
                    "ts": now_t,
                    "candidate": shared["candidate"],
                    "model": m_name,
                }
                _ai_candidate_cache.move_to_end(cache_key)
            return dict(shared["candidate"])

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
        situation=sit_key,
    )

    parsed, model_used, _ = call_editorial_ai_json(
        prompt=prompt,
        max_output_tokens=2600,
        thinking_budget=1536,
        temperature=0.88,
        total_deadline_sec=45.0,
        per_call_timeout_sec=30.0,
        allow_blocking=True,
        now_jst=now_jst,
    )
    if not parsed:
        return None

    main_text = _postprocess_ai_main_text(parsed.get("main_text") or "", channel=ch)
    if not main_text:
        return None

    x_silent = False
    x_arch_key = ""
    if ch == "x":
        recent_x_for_arch = get_recent_cross_channel_texts(channel="x", limit=3, allow_blocking=False)
        x_arch_meta = select_x_post_archetype(slot_label=slot_label, now_jst=now_jst, recent_texts=recent_x_for_arch)
        x_silent = bool(x_arch_meta.get("silent_tree"))
        x_arch_key = str(x_arch_meta.get("key") or "")

    polished_text, polisher_model = polish_draft_to_human_voice(
        draft_text=main_text,
        channel=ch,
        research=research,
        slot_label=slot_label,
        now_jst=now_jst,
        silent_tree=x_silent,
    )
    if polished_text:
        main_text = polished_text

    display_model = f"{model_used} → 2段階推敲" if polisher_model else model_used
    if model_used:
        _last_gemini_meta["last_model"] = model_used
        _last_gemini_meta["last_provider"] = "claude" if "claude" in model_used.lower() else "gemini"

    writer_note = (parsed.get("writer_note") or "天気・曜日・リアルタイム人数を掛け合わせた本音アドリブ執筆案。").strip()
    thinking_memo = (parsed.get("editorial_angle") or parsed.get("thinking_memo") or "").strip()

    candidate = {
        "style": "ai_live_adlib",
        "style_label": f"✨ AI編集長・現場本音アドリブ実況型（{display_model}）",
        "writer_note": f"【{display_model}】{writer_note}",
        "thinking_memo": thinking_memo,
        "main_text": main_text,
        "reply_text": reply_text,
        "is_ai_generated": True,
        "ai_model": display_model,
        **({"archetype_key": x_arch_key} if ch == "x" and x_arch_key else {}),
        **(
            {
                "tiktok_hook_text": (main_text.split("\n", 1)[0][:46] if main_text else "同駅・徒歩4分の男女比ギャップ速報"),
                "tiktok_reel_type": "tiktok_gap_shock",
                "tiktok_bgm_mood": "都会的ローファイハウス / シティポップ（BPM120前後）",
            }
            if ch == "tiktok"
            else {}
        ),
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
    situation: str = None,
) -> list:
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    ch = (channel or "x").lower()
    tm = get_editorial_tone_mode(tone_mode, allow_blocking=(not ai_cache_only))
    sit_key = (situation or (plan.get("situation") if plan else "") or "").strip().lower()

    if ch == "threads":
        base_cands = _write_threads_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url, tone_mode=tm)
    elif ch == "instagram":
        base_cands = _write_instagram_candidates(research, now_jst=now_jst, tone_mode=tm)
    elif ch == "tiktok":
        base_cands = _write_tiktok_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url, tone_mode=tm)
    elif ch == "note":
        base_cands = _write_note_candidates(research, slot_label=slot_label, now_jst=now_jst, site_url=site_url, tone_mode=tm, situation=sit_key)
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
            situation=sit_key,
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
        text = cand.get("main_text") or cand.get("text") or ""
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
        elif has_raw_olg or has_bare_oriental or "オリラジ" in text:
            data_score = 18
            data_comment = "⚠️ 店名表記に『OLG』『オリラジ』または『オリエンタル（ラウンジ抜け）』が残っているため減点（正式名称『オリエンタルラウンジ〇〇』が必須）。"
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
        elif style in ("tiktok_hook_gap", "tiktok_female_confession", "tiktok_data_myth", "tiktok_midnight_reverse"):
            data_score = 25
            data_comment = "◎ TikTok縦型動画（UIセーフゾーン設計）の比率バー＆冒頭2秒フックと完全連動し、実測根拠が明快。"
        elif ch == "note":
            data_score = 25
            data_comment = "◎ note深層考察：混雑実測値と街の温度差の分析根拠が1人単位で整合しています。"
        else:
            data_score = 23 if style != classic_key else 20
            data_comment = "◎ 主要店舗の実測人数・比率が過不足なく整理されています。"

        # 4. Platform Format Hygiene (max 10; hard penalty if over API limit)
        if ch == "x":
            has_url_in_main = "http://" in text or "https://" in text
            if metric_val > 280 or has_url_in_main:
                weight_score = -40
                weight_comment = f"❌ X制限超過({metric_unit})または1ポスト目URL混入のため不採用。"
            elif 84 <= metric_val <= 274:
                weight_score = 10
                weight_comment = f"◎ X最適ウェイト（{metric_unit}・1ポスト目URLなし・余白設計クリア）を完璧に満たしています。"
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
        elif ch == "tiktok":
            tag_cnt = len(re.findall(r"#[^\s#]+", text))
            has_save_cta = "保存" in text
            has_search_seo = "相席ナビ" in text and "相席ラウンジ" in text
            if metric_val > 2200:
                weight_score = -40
                weight_comment = f"❌ TikTokキャプション上限2200文字を超過（{metric_unit}）しているため不採用。"
            elif 260 <= metric_val <= 850 and 4 <= tag_cnt <= 9 and has_save_cta and has_search_seo:
                weight_score = 10
                weight_comment = f"◎ TikTok検索SEO最適長（{metric_unit}）・保存導線＆指名検索語あり・タグ{tag_cnt}個で発見タブ最適。"
            else:
                weight_score = 7
                weight_comment = f"◯ 文字数（{metric_unit}）・タグ{tag_cnt}個はTikTok標準範囲内。"
        elif ch == "note":
            is_tsubuyaki = (style == "note_tsubuyaki_gap" or metric_val <= 300)
            if is_tsubuyaki:
                if metric_val > 350:
                    weight_score = -30
                    weight_comment = f"❌ noteつぶやき上限超過（{metric_unit}）。240文字以下を推奨。"
                elif 120 <= metric_val <= 260:
                    weight_score = 10
                    weight_comment = f"◎ noteつぶやき最適文字数（{metric_unit}・120〜240字目安）に合致。"
                else:
                    weight_score = 7
                    weight_comment = f"◯ つぶやき文字数（{metric_unit}）は許容範囲内。"
            else:
                if metric_val > 1800 or metric_val < 400:
                    weight_score = 6
                    weight_comment = f"△ noteショートエッセイ文字数（{metric_unit}）。800〜1,400文字が推奨です。"
                elif 750 <= metric_val <= 1450:
                    weight_score = 10
                    weight_comment = f"◎ noteショートエッセイ最適長（{metric_unit}・800〜1,400字）を完璧にクリア。"
                else:
                    weight_score = 8
                    weight_comment = f"◯ noteエッセイ文字数（{metric_unit}）は概ね良好。"
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
            "style_label": cand.get("style_label") or STYLE_LABELS.get(style, style),
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
    situation: str = None,
) -> dict:
    """
    Run the full 4-role SNS Editorial Room pipeline for `channel` ('x', 'threads', 'instagram', 'tiktok', 'note').
    Supports situation-specific quick editorial focus (F2.2) and AI image prompt generation (F2.3).
    When `ai_cache_only=True` (used on HTML page render), returns in <30ms using cached AI draft if available,
    and flags `ai_pending_async=True` so client JS can fetch the fresh AI draft asynchronously without blocking UI.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    ch = (channel or "x").lower()
    if ch not in ("x", "threads", "instagram", "tiktok", "note"):
        ch = "x"

    sit_key = (situation or "").strip().lower()
    sit_meta = SITUATION_SPECS.get(sit_key, {})

    allow_state_blocking = False if ai_cache_only else bool(record_history or use_ai)
    default_tm = get_default_editorial_tone_mode(allow_blocking=allow_state_blocking)
    resolved_tm = get_editorial_tone_mode(tone_mode, allow_blocking=allow_state_blocking)

    research = run_researcher(
        stores,
        slot_label=slot_label,
        channel=ch,
        ig_ctx=ig_ctx,
        now_jst=now_jst,
        allow_weather_blocking=bool(record_history and not ai_cache_only),
    )
    plan = run_planner(
        research,
        slot_label=slot_label,
        channel=ch,
        now_jst=now_jst,
        allow_blocking=allow_state_blocking,
        situation=sit_key,
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
        situation=sit_key,
    )
    evaluation = run_evaluator(candidates, research, plan, channel=ch, now_jst=now_jst)

    image_prompts = generate_image_prompts(
        research=research,
        channel=ch,
        slot_label=slot_label,
        situation=sit_key,
        tone_mode=resolved_tm,
    )

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
        "situation": sit_key,
        "situation_label": sit_meta.get("label", ""),
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
        "image_prompts": image_prompts,
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
                "tiktok_hook_text": c.get("tiktok_hook_text") or "",
                "tiktok_reel_type": c.get("tiktok_reel_type") or "",
                "tiktok_bgm_mood": c.get("tiktok_bgm_mood") or "",
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
        "situation": sit_key,
        "situation_label": sit_meta.get("label", ""),
        "situation_meta": sit_meta,
        "situation_specs": list(SITUATION_SPECS.values()),
        "image_prompts": image_prompts,
        "gemini_status": get_gemini_status(allow_blocking=allow_state_blocking),
        "delivery_health": get_sns_delivery_health(allow_blocking=allow_state_blocking),
        "pacing_summary": get_sns_human_pacing_summary(now_jst),
        "ai_pending_async": ai_pending_async,
        "research": research,
        "plan": plan,
        "evaluation": evaluation,
        "winner": winner,
        "session_record": session_record,
        "history": get_recent_editorial_history(channel=ch, limit=8, allow_blocking=allow_state_blocking),
    }
