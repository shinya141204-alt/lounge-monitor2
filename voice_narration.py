"""
voice_narration.py - ElevenLabs Custom Voice Clone Narration Engine for Aiseki Navi Reels & TikTok.

Features:
1. Secure Master configuration via `job_state` (`token:elevenlabs_api_key` encrypted/obfuscated via `obf1:`)
   with seamless fallback to `ELEVENLABS_API_KEY` / `ELEVENLABS_VOICE_ID` environment variables.
2. Live fact-grounded 8.5s narration script builder across all 6 Reel/TikTok templates (`live_top5`,
   `area_battle`, `ladies_zero_wait`, `tiktok_gap_shock`, `tiktok_ladies_real`, `tiktok_midnight_cliff`)
   with zero fabricated metrics and strict official brand naming (`オリエンタルラウンジ`, `ag`, `JIS`, `ASOBIBAR`, `ヤタコイ`).
3. Dedicated Japanese TTS phonetic sanitizer (`to_tts_phonetic_japanese`) so Latin brand names (`ag`->`アグ`,
   `JIS`->`ジス`, `XIX`->`ジックス`), Japanese nightlife districts (`茶屋町`->`ちゃやまち`, `心斎橋`->`しんさいばし`),
   and counters/units (`%`->`パーセント`, `pt`->`ポイント`, `2軒目`->`にけんめ`) are pronounced naturally.
4. Disk (`/tmp/aiseki_reels`) + `turbo_store` TTS audio cache keyed by `(voice_id, model_id, phonetic_text)`
   to conserve ElevenLabs Starter credits across previews and multi-platform publishing.
5. Strict fail-open guarantee: any missing key, quota exhaustion, or network timeout cleanly falls back
   to the procedural Lounge Chill BGM without failing video rendering or scheduled cron posts.
"""

import collections
import datetime
import hashlib
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.parse
import wave

import requests

import job_state
import turbo_store

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))
TTS_TMP_CACHE_DIR = "/tmp/aiseki_reels"
TTS_SHARED_CACHE_TTL_SEC = 7200
MAX_NARRATION_CHARS = 220

ELEVENLABS_API_BASE = "https://api.elevenlabs.io/v1"
ELEVENLABS_KEY_STORAGE_PREFIX = "token:elevenlabs_api_key"
ELEVENLABS_VOICE_ID_KEY = "elevenlabs_voice_id"
ELEVENLABS_VOICE_NAME_KEY = "elevenlabs_voice_name"
ELEVENLABS_NARRATION_ENABLED_KEY = "elevenlabs_narration_enabled"
ELEVENLABS_MODEL_ID_KEY = "elevenlabs_model_id_v2"
_LEGACY_ELEVENLABS_MODEL_ID_KEY = "elevenlabs_model_id"

DEFAULT_ELEVENLABS_MODEL = "eleven_v4"
SUPPORTED_ELEVENLABS_MODELS = collections.OrderedDict([
    ("eleven_v4", "Eleven v4（最新・日本語の漢字と抑揚が最も正確／推奨）"),
    ("eleven_v4_turbo", "Eleven v4 Turbo（高速・自然な日本語読み上げ）"),
    ("eleven_v3", "Eleven v3（感情・表現力重視）"),
    ("eleven_flash_v2_5", "Flash v2.5（超高速・クレジット50%節約）"),
    ("eleven_multilingual_v2", "Multilingual v2（従来モデル）"),
])
_LANGUAGE_CODE_MODELS = frozenset({
    "eleven_v4",
    "eleven_v4_turbo",
    "eleven_v3",
    "eleven_flash_v2_5",
    "eleven_turbo_v2_5",
})

_VOICE_ID_RE = re.compile(r"^[A-Za-z0-9_-]{6,64}$")
_FULL_BRAND_OLG = "オリエンタルラウンジ"
_STANDALONE_OLG_RE = re.compile(_FULL_BRAND_OLG[:6] + r"(?!ラウンジ)")
_DURATION_STDERR_RE = re.compile(r"Duration:\s*(\d+):(\d+):(\d+(?:\.\d+)?)")
_REDUNDANT_BRAND_RUBY_RE = re.compile(
    r"(?:ORIENTAL\s*LOUNGE|オリエンタルラウンジ|ASOBIBAR|アソビバー|CLOVERS|クローバーズ|YATAKOI|ヤタコイ|ALFA|アルファ|XIX|JIS|ag)\s*[（(]\s*"
    r"(?:ORIENTAL\s*LOUNGE|オリエンタルラウンジ|アソビバー|クローバーズ|ヤタコイ|アルファ|ジックス|シックス|キックス|ジス|アグ)\s*[）)]",
    re.IGNORECASE,
)

# Strip emojis, variation selectors, and decorative box/bullet glyphs before TTS synthesis
_EMOJI_STRIP_RE = re.compile(
    "["
    "\U0001F300-\U0001FAFF"
    "\U00002600-\U000027BF"
    "\U0000FE00-\U0000FE0F"
    "\U0000200D"
    "👑⚡🔥💄🎵🥂✨📍🟢🔴📌🔍📋🎬🔄⬇️⬆️▶⏸◎△"
    "]+"
)

# Collapse brand + parenthetical reading into single spoken brand before general replacements
_BRAND_RUBY_COLLAPSE_RULES = (
    (re.compile(r"オリエンタルラウンジ\s*[（(]\s*ORIENTAL\s*LOUNGE\s*[）)]", re.IGNORECASE), "オリエンタルラウンジ"),
    (re.compile(r"(?<![A-Za-z0-9])ASOBIBAR\s*[（(]\s*アソビバー\s*[）)]", re.IGNORECASE), "アソビバー"),
    (re.compile(r"(?<![A-Za-z0-9])CLOVERS\s*[（(]\s*クローバーズ\s*[）)]", re.IGNORECASE), "クローバーズ"),
    (re.compile(r"(?<![A-Za-z0-9])ALFA\s*[（(]\s*アルファ\s*[）)]", re.IGNORECASE), "アルファ"),
    (re.compile(r"(?<![A-Za-z0-9])XIX\s*[（(]\s*(?:ジックス|シックス|キックス)\s*[）)]", re.IGNORECASE), "ジックス"),
    (re.compile(r"(?<![A-Za-z0-9])JIS\s*[（(]\s*ジス\s*[）)]", re.IGNORECASE), "ジス"),
    (re.compile(r"(?<![A-Za-z0-9])ag\s*[（(]\s*アグ\s*[）)]", re.IGNORECASE), "アグ"),
)

# Brand & Latin token phonetic replacements (applied only to TTS input, never to visual captions)
_BRAND_PHONETIC_RULES = (
    (re.compile(r"(?<![A-Za-z0-9])ORIENTAL\s*LOUNGE(?![A-Za-z0-9])", re.IGNORECASE), "オリエンタルラウンジ"),
    (re.compile(r"(?<![A-Za-z0-9])OLG(?![A-Za-z0-9])", re.IGNORECASE), "オリエンタルラウンジ"),
    (re.compile(r"オリラジ"), "オリエンタルラウンジ"),
    (re.compile(r"(?<![A-Za-z0-9])YATAKOI(?![A-Za-z0-9])", re.IGNORECASE), "ヤタコイ"),
    (re.compile(r"(?<![A-Za-z0-9])ASOBIBAR(?![A-Za-z0-9])", re.IGNORECASE), "アソビバー"),
    (re.compile(r"(?<![A-Za-z0-9])CLOVERS(?![A-Za-z0-9])", re.IGNORECASE), "クローバーズ"),
    (re.compile(r"(?<![A-Za-z0-9])ALFA(?![A-Za-z0-9])", re.IGNORECASE), "アルファ"),
    (re.compile(r"(?<![A-Za-z0-9])XIX(?![A-Za-z0-9])", re.IGNORECASE), "ジックス"),
    (re.compile(r"(?<![A-Za-z0-9])JIS(?![A-Za-z0-9])", re.IGNORECASE), "ジス"),
    (re.compile(r"(?<![A-Za-z0-9])ag(?![A-Za-z0-9])", re.IGNORECASE), "アグ"),
)

# Space-separated compound store locations (applied before general place rules so spaces inside `名古屋 栄` or `名古屋 錦` never leave single kanji `栄`/`錦` misread as `えい`/`きん`)
_SPACE_COMPOUND_PLACE_RULES = (
    (re.compile(r"梅田\s*茶屋町"), "うめだちゃやまち"),
    (re.compile(r"名古屋\s*住吉"), "なごやすみよし"),
    (re.compile(r"名古屋\s*栄"), "なごやさかえ"),
    (re.compile(r"名古屋\s*錦"), "なごやにしき"),
    (re.compile(r"神戸\s*三宮"), "こうべさんのみや"),
    (re.compile(r"福岡\s*天神"), "ふくおかてんじん"),
    (re.compile(r"大阪\s*駅前"), "おおさかえきまえ"),
    (re.compile(r"渋谷\s*本店"), "しぶやほんてん"),
)

# Complete domestic 61-store & nightlife district readings (longest-match-first so compound names never split wrongly)
_PLACE_PHONETIC_RULES = (
    ("梅田茶屋町", "うめだちゃやまち"),
    ("名古屋住吉", "なごやすみよし"),
    ("名古屋栄", "なごやさかえ"),
    ("名古屋錦", "なごやにしき"),
    ("神戸三宮", "こうべさんのみや"),
    ("福岡天神", "ふくおかてんじん"),
    ("大阪駅前", "おおさかえきまえ"),
    ("渋谷本店", "しぶやほんてん"),
    ("西新宿", "にししんじゅく"),
    ("歌舞伎町", "かぶきちょう"),
    ("心斎橋", "しんさいばし"),
    ("道頓堀", "どうとんぼり"),
    ("茶屋町", "ちゃやまち"),
    ("河原町", "かわらまち"),
    ("木屋町", "きやまち"),
    ("錦糸町", "きんしちょう"),
    ("宇都宮", "うつのみや"),
    ("宇田川", "うだがわ"),
    ("道玄坂", "どうげんざか"),
    ("六本木", "ろっぽんぎ"),
    ("恵比寿", "えびす"),
    ("名古屋", "なごや"),
    ("鹿児島", "かごしま"),
    ("三宮", "さんのみや"),
    ("名駅", "めいえき"),
    ("錦店", "にしきてん"),
    ("住吉", "すみよし"),
    ("栄店", "さかえてん"),
    ("新宿", "しんじゅく"),
    ("渋谷", "しぶや"),
    ("上野", "うえの"),
    ("町田", "まちだ"),
    ("横浜", "よこはま"),
    ("大宮", "おおみや"),
    ("船橋", "ふなばし"),
    ("高崎", "たかさき"),
    ("札幌", "さっぽろ"),
    ("仙台", "せんだい"),
    ("静岡", "しずおか"),
    ("浜松", "はままつ"),
    ("金沢", "かなざわ"),
    ("梅田", "うめだ"),
    ("難波", "なんば"),
    ("天満", "てんま"),
    ("京都", "きょうと"),
    ("神戸", "こうべ"),
    ("岡山", "おかやま"),
    ("広島", "ひろしま"),
    ("松山", "まつやま"),
    ("福岡", "ふくおか"),
    ("天神", "てんじん"),
    ("中洲", "なかす"),
    ("小倉", "こくら"),
    ("長崎", "ながさき"),
    ("大分", "おおいた"),
    ("熊本", "くまもと"),
    ("宮崎", "みやざき"),
    ("沖縄", "おきなわ"),
    ("那覇", "なは"),
    ("柏", "かしわ"),
)

# High-frequency multi-reading kanji compounds in Aiseki Navi scripts (prevents on/kun misreadings while preserving word boundaries)
_DOMAIN_KANJI_PHONETIC_RULES = (
    ("相席ナビ", "あいせきナビ"),
    ("相席ラウンジ", "あいせきラウンジ"),
    ("相席バー", "あいせきバー"),
    ("相席速報", "あいせき速報"),
    ("相席率", "あいせきりつ"),
    ("相席", "あいせき"),
    ("営業時間外", "えいぎょうじかんがい"),
    ("営業時間", "えいぎょうじかん"),
    ("集計準備中", "しゅうけいじゅんびちゅう"),
    ("現在人数", "げんざいにんずう"),
    ("合計人数", "ごうけいにんずう"),
    ("人数トップ", "にんずうトップ"),
    ("人数", "にんずう"),
    ("女性比率", "じょせいひりつ"),
    ("男性比率", "だんせいひりつ"),
    ("女性比", "じょせいひ"),
    ("男性比", "だんせいひ"),
    ("男女比", "だんじょひ"),
    ("比率差", "ひりつさ"),
    ("深夜維持率", "しんやいじりつ"),
    ("男性先行店", "だんせいせんこうてん"),
    ("女性先行店", "じょせいせんこうてん"),
    ("男性先行", "だんせいせんこう"),
    ("女性先行", "じょせいせんこう"),
    ("先行店", "せんこうてん"),
    ("混雑店", "こんざつてん"),
    ("狙い目店", "ねらいめてん"),
    ("穴場店", "あなばてん"),
    ("上位店", "じょういてん"),
    ("注目店舗", "ちゅうもくてんぽ"),
    ("全店舗", "ぜんてんぽ"),
    ("全店", "ぜんてん"),
    ("本店", "ほんてん"),
    ("お店選び", "おみせえらび"),
    ("店選び", "みせえらび"),
    ("お店", "おみせ"),
    ("受付待ちゼロ圏", "うけつけまちゼロけん"),
    ("待ちゼロ圏", "まちゼロけん"),
    ("受付待ち", "うけつけまち"),
    ("待ち時間", "まちじかん"),
    ("即案内圏", "そくあんないけん"),
    ("即案内", "そくあんない"),
    ("即乾杯圏", "そくかんぱいけん"),
    ("即乾杯", "そくかんぱい"),
    ("ゼロ圏", "ゼロけん"),
    ("終電崖", "しゅうでんがけ"),
    ("終電後", "しゅうでんご"),
    ("終電前", "しゅうでんまえ"),
    ("終電", "しゅうでん"),
    ("今夜", "こんや"),
    ("今すぐ", "いますぐ"),
    ("今いちばん", "いまいちばん"),
    ("今一番", "いまいちばん"),
    ("今の", "いまの"),
    ("今日", "きょう"),
    ("本日", "ほんじつ"),
    ("明日", "あした"),
    ("祝前日", "しゅくぜんじつ"),
    ("平日", "へいじつ"),
    ("週末", "しゅうまつ"),
    ("狙い目", "ねらいめ"),
    ("穴場", "あなば"),
    ("同じ駅", "おなじえき"),
    ("同じ時間", "おなじじかん"),
    ("同じ夜", "おなじよる"),
    ("同じ", "おなじ"),
    ("行く前", "いくまえ"),
    ("行くなら", "いくなら"),
    ("向かう直前", "むかうちょくぜん"),
    ("向かう前", "むかうまえ"),
    ("入る前", "はいるまえ"),
    ("入りやすい", "はいりやすい"),
    ("入れる", "はいれる"),
    ("見れます", "みれます"),
    ("見比べると", "みくらべると"),
    ("見比べて", "みくらべて"),
    ("見返して", "みかえして"),
    ("見つかります", "みつかります"),
    ("活況中", "かっきょうちゅう"),
    ("活況", "かっきょう"),
    ("最優勢", "さいゆうせい"),
    ("毎分更新", "まいふんこうしん"),
    ("毎分", "まいふん"),
    ("席替え", "せきがえ"),
    ("飲み放題", "のみほうだい"),
    ("夜カフェ", "よるカフェ"),
    ("激戦区", "げきせんく"),
    ("対決", "たいけつ"),
    ("以降", "いこう"),
    ("続いて", "つづいて"),
    ("過ぎ", "すぎ"),
    ("避ける", "さける"),
)

_PRE_COUNTER_RULES = (
    (re.compile(r"aiseki-navi\.com", re.IGNORECASE), "あいせきナビドットコム"),
    (re.compile(r"プロフ(?!ィール)"), "プロフィール"),
    (re.compile(r"待ち0圏"), "まちゼロけん"),
    (re.compile(r"待ち0"), "まちゼロ"),
    (re.compile(r"(?<![A-Za-z])vs(?![A-Za-z])", re.IGNORECASE), "対"),
)

_UNIT_DIGITS_KANA = ("ゼロ", "いち", "に", "さん", "よん", "ご", "ろく", "なな", "はち", "きゅう")
_TENS_KANA = ("", "じゅう", "にじゅう", "さんじゅう", "よんじゅう", "ごじゅう", "ろくじゅう", "ななじゅう", "はちじゅう", "きゅうじゅう")
_HUNDREDS_KANA = ("", "ひゃく", "にひゃく", "さんびゃく", "よんひゃく", "ごひゃく", "ろっぴゃく", "ななひゃく", "はっぴゃく", "きゅうひゃく")
_THOUSANDS_KANA = ("", "せん", "にせん", "さんぜん", "よんせん", "ごせん", "ろくせん", "ななせん", "はっせん", "きゅうせん")


def _int_to_ja_kana(n: int) -> str:
    """Convert non-negative integer (`0..99999999`) to standard spoken Japanese Hiragana."""
    if n < 0:
        return "マイナス" + _int_to_ja_kana(-n)
    if n == 0:
        return "ゼロ"
    parts = []
    if n >= 10000:
        man = n // 10000
        parts.append(_int_to_ja_kana(man) + "まん")
        n %= 10000
    if n >= 1000:
        parts.append(_THOUSANDS_KANA[n // 1000])
        n %= 1000
    if n >= 100:
        parts.append(_HUNDREDS_KANA[n // 100])
        n %= 100
    if n >= 10:
        parts.append(_TENS_KANA[n // 10])
        n %= 10
    if n > 0:
        parts.append(_UNIT_DIGITS_KANA[n])
    return "".join(parts)


def _int_prefix_without_last_digit(n: int) -> str:
    hi = (n // 10) * 10
    return "" if hi == 0 else _int_to_ja_kana(hi)


def _spoken_counter_phrase(n: int, unit: str) -> str:
    """Render exact Japanese counter euphony (促音便・撥音便・連濁) for numbers + units."""
    u_low = unit.lower()
    if unit in ("%", "％", "パーセント"):
        if n > 0 and n % 100 == 0 and n < 1000:
            return _int_to_ja_kana(n) + "パーセント"
        return _int_to_ja_kana(n) + "パーセント"
    if u_low == "pt" or unit == "ポイント":
        return _int_to_ja_kana(n) + "ポイント"
    if unit == "名":
        return _int_to_ja_kana(n) + "めい"
    if unit == "位":
        return _int_to_ja_kana(n) + "い"
    if unit == "秒":
        return _int_to_ja_kana(n) + "びょう"
    if unit == "円":
        return _int_to_ja_kana(n) + "えん"
    if unit == "倍":
        return _int_to_ja_kana(n) + "ばい"
    if unit == "代":
        return _int_to_ja_kana(n) + "だい"
    if unit == "時間":
        if n % 10 == 4:
            return _int_prefix_without_last_digit(n) + "よじかん"
        if n % 10 == 9:
            return _int_prefix_without_last_digit(n) + "くじかん"
        return _int_to_ja_kana(n) + "じかん"
    if unit == "時":
        d = n % 10
        if d == 4:
            return _int_prefix_without_last_digit(n) + "よじ"
        if d == 7:
            return _int_prefix_without_last_digit(n) + "しちじ"
        if d == 9:
            return _int_prefix_without_last_digit(n) + "くじ"
        return _int_to_ja_kana(n) + "じ"
    if unit == "分":
        if n == 0:
            return "ゼロふん"
        if n % 10 == 0:
            if n == 100:
                return "ひゃっぷん"
            tens_prefix = _int_to_ja_kana(n)
            if tens_prefix.endswith("じゅう"):
                return tens_prefix[:-3] + "じゅっぷん"
            return tens_prefix + "ふん"
        d = n % 10
        pfx = _int_prefix_without_last_digit(n)
        if d == 1:
            return pfx + "いっぷん"
        if d == 3:
            return pfx + "さんぷん"
        if d == 4:
            return pfx + "よんぷん"
        if d == 6:
            return pfx + "ろっぷん"
        if d == 8:
            return pfx + "はっぷん"
        return _int_to_ja_kana(n) + "ふん"
    if unit in ("人", "人組"):
        sfx = "ぐみ" if unit == "人組" else ""
        if n == 1:
            return "ひとり" + sfx
        if n == 2:
            return "ふたり" + sfx
        if n % 10 == 4:
            return _int_prefix_without_last_digit(n) + "よにん" + sfx
        if n % 10 == 7:
            return _int_prefix_without_last_digit(n) + "ななにん" + sfx
        return _int_to_ja_kana(n) + "にん" + sfx
    if unit in ("店舗", "店"):
        tail = "ぽ" if unit == "店舗" else ""
        if n == 0:
            return "ゼロてん" + tail
        if n % 10 == 0:
            base = _int_to_ja_kana(n)
            if base.endswith("じゅう"):
                return base[:-3] + "じゅってん" + tail
            if n == 100:
                return "ひゃってん" + tail
            return base + "てん" + tail
        d = n % 10
        pfx = _int_prefix_without_last_digit(n)
        if d == 1:
            return pfx + "いってん" + tail
        if d == 8:
            return pfx + "はってん" + tail
        return _int_to_ja_kana(n) + "てん" + tail
    if unit in ("軒", "軒目"):
        tail = "め" if unit == "軒目" else ""
        if n % 10 == 0 and n > 0:
            base = _int_to_ja_kana(n)
            if base.endswith("じゅう"):
                return base[:-3] + "じゅっけん" + tail
        d = n % 10
        pfx = _int_prefix_without_last_digit(n)
        if d == 1:
            return pfx + "いっけん" + tail
        if d == 3:
            return pfx + "さんげん" + tail
        if d == 6:
            return pfx + "ろっけん" + tail
        if d == 8:
            return pfx + "はっけん" + tail
        return _int_to_ja_kana(n) + "けん" + tail
    if unit in ("歳", "才"):
        if n == 20:
            return "はたち"
        if n % 10 == 0 and n > 0:
            base = _int_to_ja_kana(n)
            if base.endswith("じゅう"):
                return base[:-3] + "じゅっさい"
        d = n % 10
        pfx = _int_prefix_without_last_digit(n)
        if d == 1:
            return pfx + "いっさい"
        if d == 8:
            return pfx + "はっさい"
        return _int_to_ja_kana(n) + "さい"
    if unit == "組":
        return _int_to_ja_kana(n) + "くみ"
    return _int_to_ja_kana(n) + unit


_FULLWIDTH_DIGIT_TRANS = str.maketrans("０１２３４５６７８９％", "0123456789%")
_DECIMAL_COUNTER_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,4})\.(\d{1,2})\s*(パーセント|[%％]|ポイント|pt(?![A-Za-z])|秒|分|時間|倍)?",
    re.IGNORECASE,
)
_INT_COUNTER_RE = re.compile(
    r"(?<![A-Za-z0-9])(\d{1,6})\s*(パーセント|[%％]|ポイント|pt(?![A-Za-z])|人組|店舗|軒目|時間|名|人|位|時|分|秒|店|軒|組|歳|才|代|円|倍)",
    re.IGNORECASE,
)
_BARE_INT_RE = re.compile(r"(?<![A-Za-z0-9])(\d{1,5})(?![A-Za-z0-9])")


def _expand_japanese_numbers_and_counters(text: str) -> str:
    """Convert numeric expressions and Japanese counters into unambiguous Hiragana/Katakana readings."""
    if not text:
        return ""
    s = text.translate(_FULLWIDTH_DIGIT_TRANS)
    s = re.sub(r"(?<=\d),(?=\d{3}\b)", "", s)

    def _repl_dec(m: re.Match) -> str:
        whole = int(m.group(1))
        frac_str = m.group(2)
        unit = m.group(3) or ""
        whole_k = "れい" if whole == 0 else _int_to_ja_kana(whole)
        frac_k = "".join("れい" if ch == "0" else _UNIT_DIGITS_KANA[int(ch)] for ch in frac_str)
        base = f"{whole_k}てん{frac_k}"
        if not unit:
            return base
        u_low = unit.lower()
        if unit in ("%", "％", "パーセント"):
            return base + "パーセント"
        if u_low == "pt" or unit == "ポイント":
            return base + "ポイント"
        if unit == "秒":
            return base + "びょう"
        if unit == "分":
            return base + "ふん"
        if unit == "時間":
            return base + "じかん"
        if unit == "倍":
            return base + "ばい"
        return base + unit

    s = _DECIMAL_COUNTER_RE.sub(_repl_dec, s)

    def _repl_int_counter(m: re.Match) -> str:
        n = int(m.group(1))
        unit = m.group(2)
        return _spoken_counter_phrase(n, unit)

    s = _INT_COUNTER_RE.sub(_repl_int_counter, s)
    # Catch standalone `%` or `pt` left after kana numbers (e.g. `五十%`)
    s = re.sub(r"[%％]", "パーセント", s)
    s = re.sub(r"(?<![A-Za-z])pt(?![A-Za-z])", "ポイント", s, flags=re.IGNORECASE)
    s = s.replace("ポイント差", "ポイントさ").replace("パーセント差", "パーセントさ")
    s = _BARE_INT_RE.sub(lambda m: _int_to_ja_kana(int(m.group(1))), s)
    return s

VOICE_CLONE_SAMPLE_SCRIPT = (
    "こんばんは、相席ナビのリアルタイム速報です。\n"
    "今夜お店に向かう前に知っておきたい、全国の現在人数と男女比をお伝えします。\n\n"
    "まず新宿・渋谷エリアでは、オリエンタルラウンジ新宿がいま女性24名、男性18名で、女性比率57パーセントとかなり良いバランスです。\n"
    "同じ駅でも徒歩4分離れたアグ渋谷やジス大宮を見比べると、受付待ちゼロでそのまま案内されやすい穴場が見つかります。\n\n"
    "続いて大阪・梅田と難波エリア。茶屋町や心斎橋の周辺では、21時を過ぎてから一気に人数が伸びてきました。\n"
    "女子ふたり組で2軒目や夜カフェ代わりに寄るなら、女性が多すぎるお店より、男性先行で盛り上がっている店舗を選ぶほうが席替えもスムーズです。\n\n"
    "そして23時20分の終電を過ぎたあとも、アソビバーやヤタコイ、ジックスなど深夜に強いラウンジはまだまだ活況が続いています。\n"
    "全国60店舗以上のいま現在の人数と料金シミュレーターは、プロフィールのリンク、相席ナビから3秒でチェックできます。\n"
    "今夜のお店選びで失敗しないように、ぜひ保存して向かう直前に見返してみてください。"
)

VOICE_CLONE_RECORDING_GUIDE = {
    "title": "自分の声クローン作成ガイド＆専用収録台本（スマホ1分半で完了）",
    "estimated_duration_sec": 85,
    "steps": [
        "静かな室内（エアコンや反響音の少ない場所）で iPhone「ボイスメモ」等を開き、スマホを口元から15〜20cm離して下の台本をそのまま読み上げます（噛んだ箇所は少し間を空けて言い直してOK・1分〜1分半で十分です）。",
        "アナウンサーのように張る必要はなく、「友達に今夜の穴場店を落ち着いて教える自然なトーン」で読むとリール・TikTokで一番人間味が出ます。",
        "ElevenLabs（Starterプラン以上）の「Voices → Add a new voice → Instant Voice Clone」に録音ファイルをアップロードして保存後、この画面の「声一覧を取得」ボタンを押すとワンタップでVoice IDが反映されます。",
    ],
    "script_text": VOICE_CLONE_SAMPLE_SCRIPT,
}

_status_lock = threading.Lock()
_runtime_meta = {
    "last_error": "",
    "last_synth_at": "",
    "last_duration_sec": 0.0,
    "last_cached": False,
}


def _mask_secret(raw: str) -> str:
    if not raw:
        return ""
    if len(raw) > 8:
        return f"{raw[:4]}...{raw[-4:]}"
    return "****"


def _sanitize_error_text(err_msg: str, api_key: str = "") -> str:
    txt = str(err_msg or "").strip()
    if api_key and len(api_key) >= 6:
        txt = txt.replace(api_key, "***")
    txt = re.sub(r"sk_[A-Za-z0-9]{10,}", "sk_***", txt)
    return txt[:220]


_ROMAN_CITY_RULES = (
    (re.compile(r"(?<![A-Za-z])NISHISHINJUKU(?![A-Za-z])", re.IGNORECASE), "西新宿"),
    (re.compile(r"(?<![A-Za-z])CHAYAMACHI(?![A-Za-z])", re.IGNORECASE), "茶屋町"),
    (re.compile(r"(?<![A-Za-z])SHINSAIBASHI(?![A-Za-z])", re.IGNORECASE), "心斎橋"),
    (re.compile(r"(?<![A-Za-z])SHINJUKU(?![A-Za-z])", re.IGNORECASE), "新宿"),
    (re.compile(r"(?<![A-Za-z])SHIBUYA(?![A-Za-z])", re.IGNORECASE), "渋谷"),
    (re.compile(r"(?<![A-Za-z])UMEDA(?![A-Za-z])", re.IGNORECASE), "梅田"),
    (re.compile(r"(?<![A-Za-z])NAMBA(?![A-Za-z])", re.IGNORECASE), "難波"),
    (re.compile(r"(?<![A-Za-z])FUKUOKA(?![A-Za-z])", re.IGNORECASE), "福岡"),
    (re.compile(r"(?<![A-Za-z])SAPPORO(?![A-Za-z])", re.IGNORECASE), "札幌"),
    (re.compile(r"(?<![A-Za-z])MATSUYAMA(?![A-Za-z])", re.IGNORECASE), "松山"),
    (re.compile(r"(?<![A-Za-z])KUMAMOTO(?![A-Za-z])", re.IGNORECASE), "熊本"),
    (re.compile(r"(?<![A-Za-z])HIROSHIMA(?![A-Za-z])", re.IGNORECASE), "広島"),
    (re.compile(r"(?<![A-Za-z])KANAZAWA(?![A-Za-z])", re.IGNORECASE), "金沢"),
    (re.compile(r"(?<![A-Za-z])NAGASAKI(?![A-Za-z])", re.IGNORECASE), "長崎"),
    (re.compile(r"(?<![A-Za-z])OKAYAMA(?![A-Za-z])", re.IGNORECASE), "岡山"),
    (re.compile(r"(?<![A-Za-z])OKINAWA(?![A-Za-z])", re.IGNORECASE), "沖縄"),
    (re.compile(r"(?<![A-Za-z])NAGOYA(?![A-Za-z])", re.IGNORECASE), "名古屋"),
    (re.compile(r"(?<![A-Za-z])SENDAI(?![A-Za-z])", re.IGNORECASE), "仙台"),
    (re.compile(r"(?<![A-Za-z])KOKURA(?![A-Za-z])", re.IGNORECASE), "小倉"),
    (re.compile(r"(?<![A-Za-z])OMIYA(?![A-Za-z])", re.IGNORECASE), "大宮"),
    (re.compile(r"(?<![A-Za-z])EBISU(?![A-Za-z])", re.IGNORECASE), "恵比寿"),
    (re.compile(r"(?<![A-Za-z])KYOTO(?![A-Za-z])", re.IGNORECASE), "京都"),
    (re.compile(r"(?<![A-Za-z])TENMA(?![A-Za-z])", re.IGNORECASE), "天満"),
    (re.compile(r"(?<![A-Za-z])KOBE(?![A-Za-z])", re.IGNORECASE), "神戸"),
    (re.compile(r"(?<![A-Za-z])UENO(?![A-Za-z])", re.IGNORECASE), "上野"),
    (re.compile(r"(?<![A-Za-z])OITA(?![A-Za-z])", re.IGNORECASE), "大分"),
)


def enforce_spoken_brand_rule(text: str) -> str:
    """Guarantee official brand names in spoken narration scripts (`オリエンタルラウンジ`, `ヤタコイ`)."""
    if not text:
        return ""
    cleaned = str(text)
    cleaned = re.sub(r"(?:ORIENTAL|オリエンタルラウンジ|オリエンタル)\s*LOUNGE", _FULL_BRAND_OLG, cleaned, flags=re.IGNORECASE)
    cleaned = _STANDALONE_OLG_RE.sub(_FULL_BRAND_OLG, cleaned)
    cleaned = re.sub(r"(?<![A-Za-z0-9])OLG(?![A-Za-z0-9])", _FULL_BRAND_OLG, cleaned, flags=re.IGNORECASE)
    cleaned = cleaned.replace("オリラジ", _FULL_BRAND_OLG)
    cleaned = re.sub(r"(?<![A-Za-z0-9])YATAKOI(?![A-Za-z0-9])", "ヤタコイ", cleaned, flags=re.IGNORECASE)
    return cleaned


def sanitize_voice_id(raw_voice_id: str) -> str:
    cand = (raw_voice_id or "").strip()
    if not cand:
        return ""
    if _VOICE_ID_RE.match(cand):
        return cand
    return ""


def get_elevenlabs_api_key(allow_blocking: bool = False) -> str:
    try:
        stored = (job_state.get_value(ELEVENLABS_KEY_STORAGE_PREFIX, allow_blocking=allow_blocking) or "").strip()
        if stored:
            return stored
    except Exception:
        pass
    return (os.environ.get("ELEVENLABS_API_KEY") or "").strip()


def get_elevenlabs_voice_id(allow_blocking: bool = False) -> str:
    try:
        stored = sanitize_voice_id(job_state.get_value(ELEVENLABS_VOICE_ID_KEY, allow_blocking=allow_blocking) or "")
        if stored:
            return stored
    except Exception:
        pass
    return sanitize_voice_id(os.environ.get("ELEVENLABS_VOICE_ID") or "")


def get_elevenlabs_voice_name(allow_blocking: bool = False) -> str:
    try:
        return (job_state.get_value(ELEVENLABS_VOICE_NAME_KEY, allow_blocking=allow_blocking) or "").strip()[:80]
    except Exception:
        return ""


def get_elevenlabs_model_id(allow_blocking: bool = False) -> str:
    try:
        stored = (job_state.get_value(ELEVENLABS_MODEL_ID_KEY, allow_blocking=allow_blocking) or "").strip()
        if stored in SUPPORTED_ELEVENLABS_MODELS:
            return stored
    except Exception:
        pass
    try:
        legacy = (job_state.get_value(_LEGACY_ELEVENLABS_MODEL_ID_KEY, allow_blocking=allow_blocking) or "").strip()
        # Preserve intentional Flash/v3 selections; auto-upgrade legacy `eleven_multilingual_v2` default to `eleven_v4`
        if legacy in ("eleven_v3", "eleven_flash_v2_5", "eleven_v4", "eleven_v4_turbo"):
            return legacy
    except Exception:
        pass
    env_m = (os.environ.get("ELEVENLABS_MODEL_ID") or "").strip()
    if env_m in SUPPORTED_ELEVENLABS_MODELS:
        return env_m
    return DEFAULT_ELEVENLABS_MODEL


def is_voice_narration_enabled(allow_blocking: bool = False) -> bool:
    """Return user's toggle preference (defaults to True automatically once both API key and Voice ID exist)."""
    try:
        raw = (job_state.get_value(ELEVENLABS_NARRATION_ENABLED_KEY, allow_blocking=allow_blocking) or "").strip().lower()
        if raw in ("1", "true", "on", "yes"):
            return True
        if raw in ("0", "false", "off", "no"):
            return False
    except Exception:
        pass
    env_flag = (os.environ.get("ELEVENLABS_NARRATION_ENABLED") or "").strip().lower()
    if env_flag in ("0", "false", "off", "no"):
        return False
    return bool(get_elevenlabs_api_key(allow_blocking=allow_blocking) and get_elevenlabs_voice_id(allow_blocking=allow_blocking))


def is_voice_narration_active(allow_blocking: bool = False) -> bool:
    """True iff API key + valid Voice ID are configured AND narration toggle is ON."""
    if not is_voice_narration_enabled(allow_blocking=allow_blocking):
        return False
    return bool(
        get_elevenlabs_api_key(allow_blocking=allow_blocking)
        and get_elevenlabs_voice_id(allow_blocking=allow_blocking)
    )


def set_voice_narration_config(
    api_key: str = None,
    voice_id: str = None,
    voice_name: str = None,
    enabled: bool = None,
    model_id: str = None,
    clear_api_key: bool = False,
) -> dict:
    """Persist ElevenLabs narration settings in `job_state` (`token:` prefix obfuscates the API key)."""
    if clear_api_key:
        job_state.set_value(ELEVENLABS_KEY_STORAGE_PREFIX, "")
    elif api_key is not None:
        cleaned_key = str(api_key).strip()
        if cleaned_key:
            job_state.set_value(ELEVENLABS_KEY_STORAGE_PREFIX, cleaned_key)

    if voice_id is not None:
        raw_vid = str(voice_id).strip()
        if raw_vid == "":
            job_state.set_value(ELEVENLABS_VOICE_ID_KEY, "")
        else:
            valid_vid = sanitize_voice_id(raw_vid)
            if not valid_vid:
                raise ValueError("Voice ID の形式が正しくありません（半角英数字6〜64文字）。")
            job_state.set_value(ELEVENLABS_VOICE_ID_KEY, valid_vid)

    if voice_name is not None:
        job_state.set_value(ELEVENLABS_VOICE_NAME_KEY, str(voice_name).strip()[:80])

    if model_id is not None:
        norm_model = str(model_id).strip()
        if norm_model in SUPPORTED_ELEVENLABS_MODELS:
            job_state.set_value(ELEVENLABS_MODEL_ID_KEY, norm_model)

    if enabled is not None:
        job_state.set_value(ELEVENLABS_NARRATION_ENABLED_KEY, "1" if bool(enabled) else "0")

    with _status_lock:
        _runtime_meta["last_error"] = ""

    return get_voice_narration_status(allow_blocking=True)


def get_voice_narration_status(allow_blocking: bool = False) -> dict:
    """Return safe, masked configuration & status payload for Master UI (never exposes raw API key)."""
    stored_key = ""
    try:
        stored_key = (job_state.get_value(ELEVENLABS_KEY_STORAGE_PREFIX, allow_blocking=allow_blocking) or "").strip()
    except Exception:
        pass
    env_key = (os.environ.get("ELEVENLABS_API_KEY") or "").strip()
    eff_key = stored_key or env_key
    key_source = "job_state" if stored_key else ("env" if env_key else "none")

    vid = get_elevenlabs_voice_id(allow_blocking=allow_blocking)
    vname = get_elevenlabs_voice_name(allow_blocking=allow_blocking)
    model_id = get_elevenlabs_model_id(allow_blocking=allow_blocking)
    enabled = is_voice_narration_enabled(allow_blocking=allow_blocking)
    configured = bool(eff_key and vid)
    active = bool(configured and enabled)

    with _status_lock:
        last_err = _runtime_meta["last_error"]
        last_at = _runtime_meta["last_synth_at"]
        last_dur = _runtime_meta["last_duration_sec"]
        last_cached = _runtime_meta["last_cached"]

    return {
        "configured": configured,
        "has_api_key": bool(eff_key),
        "key_source": key_source,
        "masked_key": _mask_secret(eff_key),
        "voice_id": vid,
        "voice_name": vname,
        "model_id": model_id,
        "model_label": SUPPORTED_ELEVENLABS_MODELS.get(model_id, model_id),
        "supported_models": [
            {"id": k, "label": v} for k, v in SUPPORTED_ELEVENLABS_MODELS.items()
        ],
        "enabled": enabled,
        "active": active,
        "last_error": last_err,
        "last_synth_at": last_at,
        "last_duration_sec": last_dur,
        "last_cached": last_cached,
        "recording_guide": VOICE_CLONE_RECORDING_GUIDE,
    }


def get_reel_cache_voice_signature(
    narration_override: bool = None,
    custom_narration_text: str = None,
    allow_blocking: bool = False,
) -> str:
    """Compute cache-key fragment so toggling voice narration or editing script invalidates stale MP4s."""
    has_cfg = bool(
        get_elevenlabs_api_key(allow_blocking=allow_blocking)
        and get_elevenlabs_voice_id(allow_blocking=allow_blocking)
    )
    if narration_override is False:
        return "bgm_only"
    wants_voice = True if narration_override is True else is_voice_narration_enabled(allow_blocking=allow_blocking)
    if not (wants_voice and has_cfg):
        return "bgm_only"

    vid = get_elevenlabs_voice_id(allow_blocking=allow_blocking)
    mid = get_elevenlabs_model_id(allow_blocking=allow_blocking)
    cleaned_custom = normalize_narration_script_input(custom_narration_text or "")
    custom_sig = (
        hashlib.sha256(cleaned_custom.encode("utf-8")).hexdigest()[:12]
        if cleaned_custom
        else "auto"
    )
    return f"el2:{vid}:{mid}:{custom_sig}"


def normalize_narration_script_input(raw_text: str) -> str:
    if not raw_text:
        return ""
    compact = re.sub(r"\s+", " ", str(raw_text)).strip()
    if not compact:
        return ""
    return enforce_spoken_brand_rule(compact[:MAX_NARRATION_CHARS])


def to_tts_phonetic_japanese(script_text: str) -> str:
    """
    Convert a display narration script into natural spoken Japanese for ElevenLabs TTS
    without altering visual captions:
      1. Collapse duplicate brand rubies (`JIS（ジス）` -> `ジス`, `ag（アグ）` -> `アグ`, `XIX（ジックス）` -> `ジックス`).
      2. Convert Latin brand tokens (`ag`, `JIS`, `XIX`, `ASOBIBAR`, `CLOVERS`, `ALFA`).
      3. Strip `[` and `]` so `eleven_v4`/`eleven_v3` never interpret square brackets as inline audio tags.
      4. Convert ambiguous domain kanji (`相席`->`あいせき`, `今夜`->`こんや`, `現在人数`->`げんざいにんずう`) and all 61 store place names.
      5. Expand digits + Japanese counters (`24名`->`にじゅうよんめい`, `1位`->`いちい`, `23時20分`->`にじゅうさんじにじゅっぷん`).
    """
    if not script_text:
        return ""
    txt = enforce_spoken_brand_rule(str(script_text))
    txt = _EMOJI_STRIP_RE.sub("", txt)
    # Remove markdown/quote brackets and `[` `]` (inline audio-tag delimiters in Eleven v3/v4)
    for ch in ("『", "』", "「", "」"):
        txt = txt.replace(ch, "")
    for ch in ("【", "】", "[", "]", "［", "］"):
        txt = txt.replace(ch, " ")

    for pattern, repl in _BRAND_RUBY_COLLAPSE_RULES:
        txt = pattern.sub(repl, txt)

    for pattern, repl in _BRAND_PHONETIC_RULES:
        txt = pattern.sub(repl, txt)

    for pattern, repl in _ROMAN_CITY_RULES:
        txt = pattern.sub(repl, txt)

    for pattern, repl in _PRE_COUNTER_RULES:
        txt = pattern.sub(repl, txt)

    for pattern, repl in _SPACE_COMPOUND_PLACE_RULES:
        txt = pattern.sub(repl, txt)

    for src, kana in _DOMAIN_KANJI_PHONETIC_RULES:
        txt = txt.replace(src, kana)

    for src, kana in _PLACE_PHONETIC_RULES:
        txt = txt.replace(src, kana)

    txt = _expand_japanese_numbers_and_counters(txt)

    # Clean empty parentheses left after ruby collapse and collapse extra spaces around Japanese text
    txt = re.sub(r"[（(]\s*[）)]", "", txt)
    txt = re.sub(r"[ \t\u3000]+", " ", txt).strip()
    txt = re.sub(r"(?<=[\u3040-\u30ff\u4e00-\u9fff])\s+(?=[\u3040-\u30ff\u4e00-\u9fff])", "", txt)
    return txt


_PAREN_BRAND_RUBY_STRIP_RE = re.compile(
    r"\s*[（(]\s*(?:ORIENTAL\s*LOUNGE|オリエンタルラウンジ|アソビバー|クローバーズ|ヤタコイ|アルファ|ジックス|シックス|キックス|ジス|アグ)\s*[）)]",
    re.IGNORECASE,
)


def _spoken_store_name(store_entry: dict) -> str:
    if not isinstance(store_entry, dict):
        return "注目店舗"
    base = str(store_entry.get("ja_name") or store_entry.get("casual_name") or store_entry.get("raw_name") or "").strip()
    base = enforce_spoken_brand_rule(base)
    base = _PAREN_BRAND_RUBY_STRIP_RE.sub("", base)
    for pattern, repl in _ROMAN_CITY_RULES:
        base = pattern.sub(repl, base)
    base = re.sub(r"\s+", "", base)
    return base or "注目店舗"


def _short_zone_name(label: str) -> str:
    s = str(label or "").strip()
    return s.replace("エリア", "") or "主要エリア"


def build_reel_narration_script(ctx: dict) -> str:
    """
    Build a concise (~44–64 chars, ~6.4–7.7s spoken) live-fact narration script strictly synchronized
    with the stores and numbers drawn on Scene 1 & Scene 2 cards of the 8.5s 9:16 video:
      - 0.1s Hook -> Exact on-screen #1/#2 Store & Headcount -> Profile Link CTA (`相席ナビ`).
      - Strictly zero fabricated metrics (`data_status == 'insufficient'` returns honest off-hours guidance).
    """
    if not isinstance(ctx, dict) or ctx.get("data_status") == "insufficient" or ctx.get("is_fallback"):
        return enforce_spoken_brand_rule(
            "ただいま営業時間外のため集計準備中です。18時以降の現在人数と男女比は、プロフの相席ナビで毎分更新します。"
        )

    rt = str(ctx.get("reel_type") or "live_top5")

    if rt == "area_battle":
        zones = ctx.get("area_battles") or []
        leader = next((z for z in zones if z.get("is_leader")), zones[0] if zones else None)
        if leader and int(leader.get("total") or 0) > 0:
            lead_n = _short_zone_name(leader.get("label"))
            lead_w = int(leader.get("women") or 0)
            lead_m = int(leader.get("men") or 0)
            bs = leader.get("best_store")
            if isinstance(bs, dict) and (bs.get("ja_name") or bs.get("raw_name")):
                bs_name = _spoken_store_name(bs)
                script = (
                    f"新宿・梅田・渋谷の今夜対決。1位は{lead_n}で女性{lead_w}名、"
                    f"狙い目は{bs_name}。全店はプロフの相席ナビへ。"
                )
            else:
                script = (
                    f"新宿・梅田・渋谷の今夜対決。1位は{lead_n}で女性{lead_w}名、"
                    f"男性{lead_m}名。全店はプロフの相席ナビへ。"
                )
            return enforce_spoken_brand_rule(script)

    if rt == "ladies_zero_wait":
        l_top = ctx.get("ladies_top5") or []
        if l_top:
            s1 = l_top[0]
            s1_name = _spoken_store_name(s1)
            script = (
                f"女子2人組の今夜おすすめ。1位は{s1_name}で男性{int(s1.get('men') or 0)}名、"
                f"女性{int(s1.get('women') or 0)}名。特典はプロフの相席ナビへ。"
            )
            return enforce_spoken_brand_rule(script)

    if rt == "tiktok_gap_shock":
        gp_list = ctx.get("gap_pairs") or []
        if gp_list:
            gp = gp_list[0]
            hi = gp.get("heaven_store") or {}
            hi_name = _spoken_store_name(hi)
            gap_pt = int(gp.get("gap_pt") or 0)
            hi_w = int(hi.get("women") or 0)
            hi_pct = int(hi.get("wpct") or 0)
            if gp.get("is_same_hub"):
                script = (
                    f"同じ駅でも男女比が{gap_pt}pt差。狙い目は{hi_name}で女性{hi_w}名、"
                    f"女性比{hi_pct}%。プロフの相席ナビへ。"
                )
            else:
                script = (
                    f"同じ時間でも男女比が{gap_pt}pt差。狙い目は{hi_name}で女性{hi_w}名、"
                    f"女性比{hi_pct}%。プロフの相席ナビへ。"
                )
            return enforce_spoken_brand_rule(script)

    if rt == "tiktok_ladies_real":
        l_top = ctx.get("ladies_top5") or []
        if l_top:
            s1 = l_top[0]
            s1_name = _spoken_store_name(s1)
            script = (
                f"女子ペアで受付待ちを避けるなら男性先行店。1位は{s1_name}で"
                f"男性{int(s1.get('men') or 0)}名、女性{int(s1.get('women') or 0)}名。プロフの相席ナビへ。"
            )
            return enforce_spoken_brand_rule(script)

    if rt == "tiktok_midnight_cliff":
        m_top = ctx.get("midnight_top4") or ctx.get("top5_volume") or []
        if m_top:
            s1 = m_top[0]
            s1_name = _spoken_store_name(s1)
            w_cnt = int(s1.get("women") or 0)
            m_cnt = int(s1.get("men") or 0)
            ret = s1.get("midnight_retention_pct")
            if ret is not None and int(ret) > 0:
                script = (
                    f"23時過ぎの2軒目速報。1位は深夜維持率{int(ret)}%の{s1_name}で女性{w_cnt}名。"
                    f"プロフの相席ナビへ。"
                )
            else:
                script = (
                    f"23時過ぎの2軒目速報。1位は{s1_name}で女性{w_cnt}名、男性{m_cnt}名。"
                    f"プロフの相席ナビへ。"
                )
            return enforce_spoken_brand_rule(script)

    # Default: `live_top5` — strictly speak #1 (and #2) from `top5_volume` rendered on Scene 1 & Scene 2 cards
    top5 = ctx.get("top5_volume") or []
    if top5:
        s1 = top5[0]
        s1_name = _spoken_store_name(s1)
        w1 = int(s1.get("women") or 0)
        m1 = int(s1.get("men") or 0)
        if len(top5) >= 2 and top5[1].get("raw_name") != s1.get("raw_name"):
            s2 = top5[1]
            s2_name = _spoken_store_name(s2)
            w2 = int(s2.get("women") or 0)
            script = (
                f"今夜の全国速報。1位は{s1_name}で女性{w1}名、"
                f"続いて{s2_name}が女性{w2}名。現在人数はプロフの相席ナビへ。"
            )
        else:
            script = (
                f"今夜の全国速報。1位は{s1_name}で女性{w1}名、男性{m1}名。"
                f"全店の現在人数はプロフの相席ナビから3秒で見れます。"
            )
        return enforce_spoken_brand_rule(script)

    return enforce_spoken_brand_rule(
        "全国の相席ラウンジ現在人数と男女比は、プロフの相席ナビで毎分リアルタイム更新中です。"
    )


def probe_audio_duration_sec(audio_path: str, ffmpeg_bin: str = None) -> float:
    """Measure audio file duration in seconds via wave / ffprobe / ffmpeg stderr (returns 0.0 if unknown)."""
    if not audio_path or not os.path.isfile(audio_path):
        return 0.0

    if audio_path.lower().endswith(".wav"):
        try:
            with wave.open(audio_path, "rb") as wf:
                fr = wf.getframerate()
                if fr > 0:
                    return round(wf.getnframes() / float(fr), 3)
        except Exception:
            pass

    ffprobe_cand = None
    if ffmpeg_bin:
        cand = os.path.join(os.path.dirname(ffmpeg_bin), "ffprobe")
        if os.path.isfile(cand) and os.access(cand, os.X_OK):
            ffprobe_cand = cand
    if not ffprobe_cand:
        ffprobe_cand = shutil.which("ffprobe")

    if ffprobe_cand:
        try:
            res = subprocess.run(
                [
                    ffprobe_cand,
                    "-v", "error",
                    "-show_entries", "format=duration",
                    "-of", "default=noprint_wrappers=1:nokey=1",
                    audio_path,
                ],
                capture_output=True,
                text=True,
                timeout=4,
            )
            if res.returncode == 0 and res.stdout.strip():
                val = float(res.stdout.strip().splitlines()[0])
                if val > 0:
                    return round(val, 3)
        except Exception:
            pass

    eff_ffmpeg = ffmpeg_bin or shutil.which("ffmpeg")
    if eff_ffmpeg:
        try:
            res = subprocess.run(
                [eff_ffmpeg, "-hide_banner", "-i", audio_path],
                capture_output=True,
                text=True,
                timeout=4,
            )
            m = _DURATION_STDERR_RE.search(res.stderr or "")
            if m:
                dur = int(m.group(1)) * 3600 + int(m.group(2)) * 60 + float(m.group(3))
                if dur > 0:
                    return round(dur, 3)
        except Exception:
            pass

    # Fallback heuristic for 128kbps MP3 (`16,000` bytes/sec)
    try:
        sz = os.path.getsize(audio_path)
        if sz > 512:
            return round(sz / 16000.0, 2)
    except Exception:
        pass
    return 0.0


def list_elevenlabs_voices(api_key: str = None, timeout: float = 8.0) -> dict:
    """Fetch available voices from the user's ElevenLabs account, prioritizing cloned/custom voices."""
    eff_key = (api_key or "").strip() or get_elevenlabs_api_key(allow_blocking=True)
    if not eff_key:
        return {"ok": False, "voices": [], "error": "ElevenLabs APIキーが未設定です。"}

    url = f"{ELEVENLABS_API_BASE}/voices"
    try:
        resp = requests.get(
            url,
            headers={
                "xi-api-key": eff_key,
                "Accept": "application/json",
            },
            timeout=timeout,
        )
        if resp.status_code != 200:
            detail = _sanitize_error_text(resp.text, eff_key)
            return {
                "ok": False,
                "voices": [],
                "error": f"ElevenLabs音声一覧の取得に失敗しました（HTTP {resp.status_code}）: {detail}",
            }
        data = resp.json() or {}
        raw_voices = data.get("voices") or []
        cat_rank = {
            "cloned": 0,
            "professional": 1,
            "generated": 2,
            "high_quality": 3,
            "premade": 4,
        }
        parsed = []
        for v in raw_voices:
            if not isinstance(v, dict):
                continue
            vid = sanitize_voice_id(str(v.get("voice_id") or ""))
            if not vid:
                continue
            cat = str(v.get("category") or "premade").lower()
            parsed.append({
                "voice_id": vid,
                "name": str(v.get("name") or vid)[:80],
                "category": cat,
                "is_cloned": cat in ("cloned", "professional"),
                "preview_url": str(v.get("preview_url") or "")[:300],
                "_rank": cat_rank.get(cat, 5),
            })
        parsed.sort(key=lambda item: (item["_rank"], item["name"]))
        for item in parsed:
            item.pop("_rank", None)
        return {"ok": True, "voices": parsed}
    except Exception as exc:
        return {
            "ok": False,
            "voices": [],
            "error": f"ElevenLabs通信エラー: {_sanitize_error_text(str(exc), eff_key)}",
        }


def _write_atomic_tts_file(target_path: str, audio_bytes: bytes) -> str:
    os.makedirs(TTS_TMP_CACHE_DIR, exist_ok=True)
    tmp_path = f"{target_path}.{os.getpid()}.{threading.get_ident()}.tmp"
    with open(tmp_path, "wb") as f_out:
        f_out.write(audio_bytes)
    os.replace(tmp_path, target_path)
    return target_path


def synthesize_narration_audio(
    script_text: str,
    voice_id: str = None,
    api_key: str = None,
    model_id: str = None,
    timeout: float = 14.0,
    force_refresh: bool = False,
) -> dict:
    """
    Synthesize spoken MP3 narration via ElevenLabs (with `/tmp/aiseki_reels` + `turbo_store` cache).
    Never raises on API/network failures; returns `{"ok": False, "error": ...}` for fail-open BGM fallback.
    """
    eff_key = (api_key or "").strip() or get_elevenlabs_api_key(allow_blocking=False)
    eff_vid = sanitize_voice_id(voice_id or "") or get_elevenlabs_voice_id(allow_blocking=False)
    eff_model = (model_id or "").strip()
    if eff_model not in SUPPORTED_ELEVENLABS_MODELS:
        eff_model = get_elevenlabs_model_id(allow_blocking=False)

    clean_script = normalize_narration_script_input(script_text)
    phonetic_text = to_tts_phonetic_japanese(clean_script)

    if not eff_key or not eff_vid:
        return {
            "ok": False,
            "error": "ElevenLabs APIキーまたはVoice IDが未設定です。",
            "script_text": clean_script,
            "phonetic_text": phonetic_text,
        }
    if not phonetic_text:
        return {
            "ok": False,
            "error": "読み上げ台本が空です。",
            "script_text": clean_script,
            "phonetic_text": "",
        }

    cache_raw = f"v2|{eff_vid}|{eff_model}|{phonetic_text}"
    tts_hash = hashlib.sha256(cache_raw.encode("utf-8")).hexdigest()[:24]
    os.makedirs(TTS_TMP_CACHE_DIR, exist_ok=True)
    disk_mp3_path = os.path.join(TTS_TMP_CACHE_DIR, f"tts_{tts_hash}.mp3")
    shared_key = f"reel_tts:{tts_hash}"
    now_ts = time.time()

    if not force_refresh:
        if os.path.isfile(disk_mp3_path):
            try:
                if (now_ts - os.path.getmtime(disk_mp3_path)) < TTS_SHARED_CACHE_TTL_SEC and os.path.getsize(disk_mp3_path) > 512:
                    with open(disk_mp3_path, "rb") as f_in:
                        cached_bytes = f_in.read()
                    dur = probe_audio_duration_sec(disk_mp3_path)
                    with _status_lock:
                        _runtime_meta["last_cached"] = True
                        _runtime_meta["last_duration_sec"] = dur
                    return {
                        "ok": True,
                        "cached": True,
                        "audio_bytes": cached_bytes,
                        "audio_path": disk_mp3_path,
                        "duration_sec": dur,
                        "script_text": clean_script,
                        "phonetic_text": phonetic_text,
                        "voice_id": eff_vid,
                        "model_id": eff_model,
                    }
            except Exception:
                pass

        try:
            shared_hit = turbo_store.get_media_blob(shared_key)
            if shared_hit and shared_hit[0] and len(shared_hit[0]) > 512:
                cached_bytes = bytes(shared_hit[0])
                _write_atomic_tts_file(disk_mp3_path, cached_bytes)
                dur = probe_audio_duration_sec(disk_mp3_path)
                with _status_lock:
                    _runtime_meta["last_cached"] = True
                    _runtime_meta["last_duration_sec"] = dur
                return {
                    "ok": True,
                    "cached": True,
                    "audio_bytes": cached_bytes,
                    "audio_path": disk_mp3_path,
                    "duration_sec": dur,
                    "script_text": clean_script,
                    "phonetic_text": phonetic_text,
                    "voice_id": eff_vid,
                    "model_id": eff_model,
                }
        except Exception:
            pass

    safe_vid = urllib.parse.quote(eff_vid, safe="")
    url = f"{ELEVENLABS_API_BASE}/text-to-speech/{safe_vid}?output_format=mp3_44100_128"

    def _build_tts_body(m_id: str, minimal: bool = False) -> dict:
        if minimal or m_id in ("eleven_v4", "eleven_v4_turbo", "eleven_v3"):
            v_settings = {"stability": 0.50, "similarity_boost": 0.80}
        else:
            v_settings = {
                "stability": 0.50,
                "similarity_boost": 0.82,
                "style": 0.12,
                "use_speaker_boost": True,
            }
        body = {
            "text": phonetic_text,
            "model_id": m_id,
            "voice_settings": v_settings,
        }
        if m_id in _LANGUAGE_CODE_MODELS:
            body["language_code"] = "ja"
            if not minimal:
                body["apply_text_normalization"] = "on"
        return body

    headers = {
        "xi-api-key": eff_key,
        "Content-Type": "application/json",
        "Accept": "audio/mpeg",
    }

    try:
        used_model = eff_model
        resp = requests.post(url, json=_build_tts_body(used_model, minimal=False), headers=headers, timeout=timeout)
        if resp.status_code in (400, 404, 422):
            # Fallback 1: minimal payload for same model (drops `apply_text_normalization` / `style` if rejected)
            resp = requests.post(url, json=_build_tts_body(used_model, minimal=True), headers=headers, timeout=timeout)
        if resp.status_code in (400, 404, 422):
            # Fallback 2: if a specific Voice Library / plan rejects `eleven_v4`, fall back cleanly to `eleven_v3` or `eleven_flash_v2_5`
            for alt_model in ("eleven_v3", "eleven_flash_v2_5", "eleven_multilingual_v2"):
                if alt_model == used_model:
                    continue
                alt_resp = requests.post(url, json=_build_tts_body(alt_model, minimal=True), headers=headers, timeout=timeout)
                if alt_resp.status_code == 200 and alt_resp.content and len(alt_resp.content) >= 512:
                    resp = alt_resp
                    used_model = alt_model
                    break

        if resp.status_code != 200 or not resp.content or len(resp.content) < 512:
            err_detail = _sanitize_error_text(resp.text if resp.status_code != 200 else "empty audio payload", eff_key)
            msg = f"ElevenLabs音声合成エラー（HTTP {resp.status_code}）: {err_detail}"
            logger.warning(f"[voice_narration] {msg}")
            with _status_lock:
                _runtime_meta["last_error"] = msg
            return {
                "ok": False,
                "error": msg,
                "script_text": clean_script,
                "phonetic_text": phonetic_text,
                "voice_id": eff_vid,
                "model_id": used_model,
            }

        audio_bytes = bytes(resp.content)
        _write_atomic_tts_file(disk_mp3_path, audio_bytes)
        try:
            turbo_store.set_media_blob(
                shared_key,
                audio_bytes,
                mime_type="audio/mpeg",
                ttl_sec=TTS_SHARED_CACHE_TTL_SEC,
            )
        except Exception:
            pass

        dur = probe_audio_duration_sec(disk_mp3_path)
        now_str = datetime.datetime.now(JST).strftime("%Y-%m-%d %H:%M:%S")
        with _status_lock:
            _runtime_meta["last_error"] = ""
            _runtime_meta["last_synth_at"] = now_str
            _runtime_meta["last_duration_sec"] = dur
            _runtime_meta["last_cached"] = False

        return {
            "ok": True,
            "cached": False,
            "audio_bytes": audio_bytes,
            "audio_path": disk_mp3_path,
            "duration_sec": dur,
            "script_text": clean_script,
            "phonetic_text": phonetic_text,
            "voice_id": eff_vid,
            "model_id": used_model,
        }
    except Exception as exc:
        msg = f"ElevenLabs通信タイムアウトまたは接続失敗: {_sanitize_error_text(str(exc), eff_key)}"
        logger.warning(f"[voice_narration] {msg}")
        with _status_lock:
            _runtime_meta["last_error"] = msg
        return {
            "ok": False,
            "error": msg,
            "script_text": clean_script,
            "phonetic_text": phonetic_text,
            "voice_id": eff_vid,
            "model_id": eff_model,
        }
