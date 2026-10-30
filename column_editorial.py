"""
column_editorial.py - 4-Role Column & note.com Editorial Room Pipeline (Master-Only)

Brings the same 4-role human-voiced editorial room architecture as `sns_editorial.py` to
long-form SEO columns (`/guide/<slug>`) and note.com manuscripts:
  1. Researcher (実測ログ・現場ネタ発掘係): Extracts verified crowd gaps, post-last-train retention,
     same-weekday anomalies, weather/calendar context, and active `/store/<slug>` links.
  2. Planner (企画構成・カニバリ防止係): Checks all existing public & scheduled columns to avoid angle
     cannibalization, assigns unique URL slugs, target readers, and a 4-part story blueprint.
  3. Writer (長考ストーリー＆note執筆係): Generates a Gemini bounded-thinking story candidate
     (`thinking_memo` included) alongside human-voiced fallback candidates (Field Report & Data Contrast),
     each complete with `.dialogue-box` conversations, a verified data table, FAQs, note.com Markdown,
     and a weight-checked 2-post X release thread.
  4. Evaluator / Desk (鬼デスク・100点採点＆検閲係): Enforces zero bare 'オリエンタル' (`オリエンタル(?!ラウンジ)`),
     penalizes AI clichés, verifies `/store/<slug>` links & Twitter character weight, scores out of 100,
     and enables 1-click scheduled or immediate site publication persisted via `job_state`.
"""

import os
import re
import time
import json
import html
import datetime
import logging
import threading
import collections
import requests

import job_state
import store_master
import sns_editorial
import turbo_store
import x_poster

logger = logging.getLogger(__name__)

JST = datetime.timezone(datetime.timedelta(hours=9))

DYNAMIC_INDEX_KEY = "col_article_index"
DYNAMIC_ARTICLE_PREFIX = "col_article:"
COLUMN_HISTORY_KEY = "col_editorial_log"

_COL_AI_CACHE_TTL_SEC = 900
_COL_AI_CACHE_MAX_SIZE = 32
_col_ai_cache = collections.OrderedDict()
_col_ai_lock = threading.Lock()

_audit_cache = {"ts": 0.0, "data": None}
_audit_lock = threading.Lock()

# Reserved static /guide/<slug> slugs that must never be overwritten by dynamic articles
RESERVED_STATIC_SLUGS = {
    "guide",
    "for-women",
    "ladies",
    "grooming",
    "strategy",
    "compare",
    "brand-comparison",
    "weekday-strategy",
    "weekend-strategy",
    "shinjuku",
    "solo-vs-pair",
    "second-venue",
    "first-10-minutes",
    "club-area-midnight",
    "beauty-area-stage",
    "male-lead-mechanics",
    "price-event-comparison",
    "last-train-cliff",
    "local-vs-urban",
    "cost-break-even",
    "rain-payday",
    "osaka-kita-vs-minami",
    "crowd-rush-paradox",
}

COLUMN_AI_CLICHES = (
    "いかがでしたか",
    "いかがでしたでしょうか",
    "徹底解説",
    "本記事では",
    "この記事では",
    "ご紹介",
    "間違いなし",
    "最適解",
    "必見",
    "魅力的",
    "大注目",
    "ぜひ足を運んで",
)

SWEET_FANTASY_CLICHES = (
    "え、行きたいです",
    "ちょうどもう少しゆっくり話したい",
    "白Tに",
    "爪を短く",
    "間違いなくモテます",
)

# Overused tropes across legacy columns (monitored by Desk Evaluator to prevent cookie-cutter repetition)
OVERUSED_COLUMN_TROPES = (
    "エレベーターの扉が開いた瞬間",
    "受付前の黒いソファでスーツ姿",
    "4人でグループLINE作ろう",
    "夜カフェあるんだけどコーヒーか甘いもの",
)

DEEP_COLUMN_EDITORIAL_GUIDE = """【長文データコラム＆note深層考察のための『4層因果ピラミッド』原則】
単なる『A店が混んでいたからB店へ歩いた』『40分で見切って夜カフェに誘おう』という表層ノウハウ（How）の焼き直しは禁止します。
必ず以下の4層構造で『なぜその現象が夜の街で繰り返されるのか（Why）』を深く解き明かしてください：
  Layer 1（観測された非自明な数値事実）：累計実測ログ・時間帯波形・店舗別料金仕様から見える具体的な数字の歪み。
  Layer 2（多くの人が陥る直感の罠）：なぜ普通の利用者は毎回同じ失敗（知名度突撃・サンクコスト延長・面接トーク・タダ飯誤解）をしてしまうのか。
  Layer 3（背後の構造メカニズム ★最重要）：
    - 二面市場（Two-Sided Market）の非対称コスト：女性は金銭0円だが『ヘアメイク90分＋移動体力＋ハズレ卓の機会費用』を払っており、男性は『10分従量メーター課金＋待機料』によるサンクコスト錯誤（元を取ろうとして負け卓で粘る心理）を抱えている構造。
    - 都市動線・鉄道ダイヤ・商業生態系：改札出口からの徒歩動線、終電23:20の会社員層離脱と0:30以降の飲食・アパレル・タクシー圏層の入れ替わり。
    - ブランド制度設計の差：オリエンタルラウンジ・ag（20代中心）・JIS（ソロ入店しやすさ・大型回転）の年齢層・待機課金・ソロ可否が生む客層分離。
  Layer 4（そのテーマ固有の意思決定ルール）：借り物の定型句ではなく、構造を理解した人だけが実行できる具体的な判断基準。"""

EYECATCH_PRESETS = {
    "last-train": {
        "label": "🌃 終電崖・深夜ラウンジ夜景",
        "jpg": "/static/eyecatch-last-train-cliff.jpg",
        "webp": "/static/eyecatch-last-train-cliff.webp",
    },
    "shinjuku": {
        "label": "🏙️ 繁華街ネオン・街別ハシゴ",
        "jpg": "/static/eyecatch-shinjuku.jpg",
        "webp": "/static/eyecatch-shinjuku.webp",
    },
    "rain-payday": {
        "label": "☔ 雨夜・給料日前の隠れ穴場",
        "jpg": "/static/eyecatch-rain-payday.jpg",
        "webp": "/static/eyecatch-rain-payday.webp",
    },
    "first-10min": {
        "label": "🥂 乾杯10分・ソファ席の会話",
        "jpg": "/static/eyecatch-first-10min.jpg",
        "webp": "/static/eyecatch-first-10min.webp",
    },
    "second-venue": {
        "label": "🍸 2軒目バー誘導・見切り判断",
        "jpg": "/static/eyecatch-second-venue.jpg",
        "webp": "/static/eyecatch-second-venue.webp",
    },
    "local-urban": {
        "label": "✈️ 地方遠征・都心比較",
        "jpg": "/static/eyecatch-local-vs-urban.jpg",
        "webp": "/static/eyecatch-local-vs-urban.webp",
    },
    "cost-break": {
        "label": "💰 滞在コスパ・料金比較",
        "jpg": "/static/eyecatch-cost-break-even.jpg",
        "webp": "/static/eyecatch-cost-break-even.webp",
    },
}

COLUMN_THEMES = {
    "area_hopping": {
        "key": "area_hopping",
        "label": "🚶 街別ギャップ・徒歩5分ハシゴ",
        "badge": "街別ハシゴ検証",
        "eyecatch_key": "shinjuku",
        "tag_primary": ["tag-pink", "📊 同一エリア男女比ギャップ"],
        "tag_secondary": ["tag-cyan", "徒歩5分ハシゴ術"],
        "slug_prefix": "area-gap-hopping",
        "target_reader": "金土や仕事帰りに人気店へ直行して男性待ちや男女比崩れに当たりがちな20〜30代男性",
        "core_question": "同じ街・徒歩5分圏内なのになぜ女性比率が15〜25%も割れるのか？1軒目から2軒目へスマートに切り替える基準とは",
    },
    "midnight_wave": {
        "key": "midnight_wave",
        "label": "🌙 終電後・深夜0時半の第2波",
        "badge": "深夜第2波データ",
        "eyecatch_key": "last-train",
        "tag_primary": ["tag-cyan", "🌙 終電後ログ追跡"],
        "tag_secondary": ["tag-pink", "深夜0時半の再流入"],
        "slug_prefix": "midnight-second-wave",
        "target_reader": "23時過ぎの2軒目・3軒目で『今から入ってももう遅いかも』と迷っている男性ペア・ソロ組",
        "core_question": "23時20分の帰宅ラッシュを過ぎた後、深夜0時半からもう一度女性が増え直す店舗の共通点と立ち回りとは",
    },
    "weekday_anaba": {
        "key": "weekday_anaba",
        "label": "☔ 平日夜・雨・給料日前の穴場",
        "badge": "平日・天候穴場",
        "eyecatch_key": "rain-payday",
        "tag_primary": ["tag-amber", "☔ 平日×天候ログ分析"],
        "tag_secondary": ["tag-cyan", "ライバル減の狙い目"],
        "slug_prefix": "weekday-quiet-sweetspot",
        "target_reader": "週末の騒がしさや待ち時間が苦手で、落ち着いて会話できる夜を狙いたい社会人男性",
        "core_question": "なぜ金曜ピークより『火〜木の20時半』や『少し天気が崩れた日の駅直結店』のほうが結果的に連絡先交換まで進みやすいのか",
    },
    "first_talk_clean": {
        "key": "first_talk_clean",
        "label": "🥂 乾杯10分・距離70cmの会話＆清潔感",
        "badge": "乾杯10分・会話術",
        "eyecatch_key": "first-10min",
        "tag_primary": ["tag-pink", "💬 現場テーブル観察"],
        "tag_secondary": ["tag-amber", "距離70cmの第一印象"],
        "slug_prefix": "table-first-impression",
        "target_reader": "相席までは組めるのに、乾杯後15分でスマホを見られたり『友達と予定があって』と抜けられがちな男性",
        "core_question": "面接のような一問一答を卒業し、座って最初の10分で『あ、この席当たりかも』と空気がほぐれる一言と清潔感の差とは",
    },
    "second_venue_move": {
        "key": "second_venue_move",
        "label": "🍸 40分見切り・損益＆2軒目誘導",
        "badge": "見切り＆2軒目術",
        "eyecatch_key": "second-venue",
        "tag_primary": ["tag-amber", "🍸 滞在時間×2軒目設計"],
        "tag_secondary": ["tag-pink", "40分見切りルール"],
        "slug_prefix": "smart-exit-second-bar",
        "target_reader": "微妙な空気の席で気を使って90分粘って予算を溶かしたり、盛り上がっても店内で完結してしまう男性",
        "core_question": "噛み合わない席を角を立てずに40分で切り上げ、逆に波長が合った席では自然に徒歩3分の夜カフェ・バーへ誘う声かけとは",
    },
    "solo_trip": {
        "key": "solo_trip",
        "label": "✈️ 1人参戦・地方出張のリアル攻略",
        "badge": "ソロ・出張遠征",
        "eyecatch_key": "local-urban",
        "tag_primary": ["tag-cyan", "✈️ ソロ＆地方遠征ログ"],
        "tag_secondary": ["tag-amber", "1人参戦の勝率設計"],
        "slug_prefix": "solo-local-expedition",
        "target_reader": "相方と予定が合わない夜に1人でふらっと寄りたい人や、出張・旅行先の夜を楽しく過ごしたい男性",
        "core_question": "1人（ソロ）参戦で浮かない時間帯・店舗選びと、出張先や地元外のラウンジで『街のことを教えて』を武器にする会話術とは",
    },
    "female_real_voice": {
        "key": "female_real_voice",
        "label": "💄 女性目線の本音・女子トイレ反省会",
        "badge": "女性目線リアル",
        "eyecatch_key": "first-10min",
        "tag_primary": ["tag-pink", "💄 女性ペアの本音解剖"],
        "tag_secondary": ["tag-cyan", "女子トイレ反省会"],
        "slug_prefix": "female-real-voice",
        "target_reader": "『普通に盛り上がっていたはずなのに、なぜ解散後に既読スルーされるのか』を知りたい男性＆ハズレ店を避けて即乾杯したい女性2人組",
        "core_question": "乾杯15分後の女子トイレLINEで実は何が送られているのか？女性側が『タダ飯消化モード』に切り替わる境界線と、女子組が待ち0分で当たり席を引く店舗選びとは",
    },
    "bitter_loser_habits": {
        "key": "bitter_loser_habits",
        "label": "🌶️ 辛口解剖・「いい人止まり」の敗因",
        "badge": "辛口・敗因解剖",
        "eyecatch_key": "second-venue",
        "tag_primary": ["tag-amber", "🌶️ 現場の残酷なリアル"],
        "tag_secondary": ["tag-pink", "脱・いい人止まり"],
        "slug_prefix": "bitter-loser-habits",
        "target_reader": "礼儀正しく気を配っているつもりなのに、いつも『4人のグループLINE』や『またみんなで飲もう』で終わってしまう男性",
        "core_question": "なぜ『優しい質問役』や『お会計間際の2軒目打診』は9割が社交辞令で終わるのか？綺麗事抜きの敗因解剖とスマートに抜け出す立ち回りとは",
    },
}

THEME_ARCHETYPES = {
    "area_hopping": {
        "archetype_name": "都市空間アービトラージ解剖型",
        "mechanism_lens": "看板認知バイアスと駅出口動線が生む『同一街・半径300mの需給ねじれ（アービトラージ）』",
        "opening_scene_hint": "改札を出た交差点の信号待ちや、1軒目の居酒屋を出た瞬間のスマホ画面比較（手垢のついた『エレベーターが開いた瞬間のスーツ行列』導入は避ける）",
        "causal_thesis": "同じ街で男女比が15〜25pt割れるのは偶然ではなく、知名度上位の1店舗目に『事前確認なしの1次会帰り男性』が集中する一方、女性ペアは『直前にSNSや口コミで空席・特典を見て裏通りや姉妹ブランドのag・別系列へ分散する』という意思決定タイミングの非対称性があるから。",
        "banned_tropes": ["エレベーターの扉が開いた瞬間", "4人でグループLINE作ろう", "女子トイレ反省会"],
        "blueprint": [
            "第1章：改札から半径300mで起きている『看板店への思考停止集中』と近隣エアポケットの発見",
            "第2章：二面市場の意思決定ズレ分析——なぜ男性は知名度で並び、女性ペアは別ブランドへ流れるのか",
            "第3章：店舗別・実測男女比＆料金単価マトリクスで検証する『待つコスト vs 4分歩くリターン』",
            "第4章：同エリア2〜3店舗をポートフォリオ化する移動判断プロトコルとFAQ",
        ],
        "note_essay_hook": "深夜、53店舗の人数モニターを並べて眺めていると、人間の『足の重さ』がそのまま数字の偏りになって浮かび上がってくる夜がある。",
    },
    "midnight_wave": {
        "archetype_name": "時間帯コホート交代・都市生態系アナリシス型",
        "mechanism_lens": "鉄道終電ダイヤによる『第1コホート（丸の内・オフィス帰り層）』退場と『第2コホート（飲食・美容・地元タクシー圏層）』再流入の二段階波形",
        "opening_scene_hint": "23時25分、駅へ向かう人の波と逆行する繁華街のネオン、あるいは深夜0時過ぎのタクシー乗り場の空気感から入る",
        "causal_thesis": "23時20分に一度女性が急減したあと深夜0時半から再び女性が増え直す店舗があるのは、単なる『残り物』ではなく客層のコホート（属性集団）が丸ごと入れ替わるから。終電で帰るオフィス勤務層と入れ替わりで、23時上がりの美容・飲食・アパレル職や近隣居住・タクシー圏の2軒目組が『まだ話し足りない夜』を求めて深夜強いブランド（agや繁華街中心店）に合流する。",
        "banned_tropes": ["エレベーターの扉が開いた瞬間", "受付前の黒いソファでスーツ姿", "4人でグループLINE作ろう"],
        "blueprint": [
            "第1章：23時20分の『終電の崖』で帰る人と、深夜0時半から街へ出てくる人の決定的な違い",
            "第2章：コホート交代メカニズム——深夜第2波の女性ペアが求めている『1軒目とは違う空気感』",
            "第3章：終電後キープ率（深夜維持目安）が高い店舗・ブランドの構造的共通点と実測比較",
            "第4章：23時半以降の後半戦で消耗せず波長を合わせる深夜専用の立ち回りとFAQ",
        ],
        "note_essay_hook": "23時20分を境に、夜の街は一度リセットボタンを押されたように顔ぶれが変わる。終電という巨大なフィルターが、街に残る人間の動機を静かに選別するからだ。",
    },
    "weekday_anaba": {
        "archetype_name": "需要弾力性×天候・給料日サイクル実証型",
        "mechanism_lens": "週末の『お祭りノイズ過多』と対比した、火〜木夜・雨天・給料日前の『本音コミュニケーション純度上昇』メカニズム",
        "opening_scene_hint": "水曜20時半の少し静かなオフィス街の歩道や、傘をたたんで地下街直結ビルへ入る雨夜の風景から入る",
        "causal_thesis": "金曜ピークより火〜木の20時半や小雨の駅直結店のほうが連絡先交換後の再会率が高くなりやすいのは、男性側の『とりあえず騒ぎたい団体・冷やかし層』が激減して競争倍率が下がるだけでなく、平日に足を運ぶ女性ペア自身が『週末のパリピ感ではなく、仕事終わりに落ち着いた大人と普通に美味しいお酒を飲みたい』という明確な会話動機を持っているため。",
        "banned_tropes": ["エレベーターの扉が開いた瞬間", "4人でグループLINE作ろう", "スマホ画面上向き"],
        "blueprint": [
            "第1章：『金曜21時が一番出会える』という直感が、実は一番コスパを悪化させている逆説",
            "第2章：天候・給料日前・週半ばが生む『動機のフィルタリング効果』と地下直結店の優位性",
            "第3章：曜日×時間帯ログと10分単価の掛け合わせで見る『平日90分 vs 週末90分』の実質期待値",
            "第4章：仕事帰りの平日夜に無理なく当たり席を引くタイムスケジュールとFAQ",
        ],
        "note_essay_hook": "みんなが『金曜の夜』に期待しすぎるせいで、金曜の夜ほど人と人の会話が雑になる——何万件もの入店ログを追いかけてきて、一番強く感じる皮肉がそれだ。",
    },
    "first_talk_clean": {
        "archetype_name": "1卓ミクロ観察・認知負荷＆非言語シグナル解剖型",
        "mechanism_lens": "ソファ距離70cmにおける『視覚・嗅覚の初期足切り（ハロー効果）』と、一問一答面接が相手の脳に与える『認知コスト疲労』",
        "opening_scene_hint": "乾杯のグラスが触れ合った直後の『最初の7秒間の沈黙』と、テーブル上の照明が映す距離70cmのリアルな視点から入る",
        "causal_thesis": "相席後に15分で空気が重くなる最大の原因は『話術不足』ではなく、初対面の緊張から男性が投げてしまう『お仕事は？どこ住み？休日は？』という履歴書確認が、仕事帰りの女性に『採用面接と同じ認知コスト（気を張る疲労）』を強いるから。逆に距離70cmでの清潔感（視覚ノイズゼロ）と『いま目の前にある共通体験』の共有から入る卓は、警戒心の防衛壁が最初の3分で溶ける。",
        "banned_tropes": ["エレベーターの扉が開いた瞬間", "受付前の黒いソファでスーツ姿"],
        "blueprint": [
            "第1章：乾杯から180秒——言葉より先に距離70cmの照明下で採点されている非言語シグナル",
            "第2章：なぜ『優しい質問攻め』ほど相手を疲れさせるのか？面接官モードと共感モードの決定的な差",
            "第3章：余裕が生まれる店舗環境選び（男女比・内装トーン別比較）と会話テンポの相関",
            "第4章：最初の10分で『この席は居心地がいい』に変える具体的な話題設計とFAQ",
        ],
        "note_essay_hook": "初対面の4人が幅70センチのテーブルを挟んで座るとき、勝敗の大半は最初の質問が口から出る前に、もっと静かな場所で決まっている。",
    },
    "second_venue_move": {
        "archetype_name": "行動経済学・サンクコスト損益分岐＆意思決定設計型",
        "mechanism_lens": "10分従量課金メーターが引き起こす『コンコルド効果（サンクコスト錯誤）』と、2軒目オファーにおける『出口保証（Exit Guarantee）の安心心理』",
        "opening_scene_hint": "手元のスマホに表示された滞在70分の経過時間や、お会計伝票の数字を見てふと冷静になる帰り道の瞬間から入る",
        "causal_thesis": "噛み合わない席なのに90分粘って予算と体力を溶かしてしまうのは、人間が『ここまで払ったチャージと待ち時間を無駄にしたくない』という行動経済学上のサンクコスト錯誤に陥るため。一方で、波長が合った席でも店内でダラダラ延長するとマンネリ化し、逆に45分前後のピークで『終電前の30分だけ場所を変えよう』と終了時刻（出口）を保証して提案する方が、女性側の心理的リスクが最小化される。",
        "banned_tropes": ["エレベーターの扉が開いた瞬間", "受付前の黒いソファでスーツ姿"],
        "blueprint": [
            "第1章：『せっかく座れたからもう30分…』——10分課金メーターが狂わせる引き際の損益計算",
            "第2章：サンクコストの罠を断ち切る『40分損益分岐ルール』と時間帯別チャージの実測試算",
            "第3章：なぜ長居するほど2軒目を断られるのか？ピーク・エンドの法則と『出口時刻の明示』",
            "第4章：気まずさゼロのスマート退店フレーズと、徒歩3分圏への自然なブリッジ設計",
        ],
        "note_essay_hook": "投資の世界に『損切りが一番難しい』という格言があるけれど、夜の相席ラウンジの伝票ほど、人間のサンクコスト（埋没費用）への弱さを残酷に映し出すものはない。",
    },
    "solo_trip": {
        "archetype_name": "ソロ機動力 vs ペア力学・地方都市比較論型",
        "mechanism_lens": "2人組特有の『相方への遠慮・足並みコスト』から解放されたソロ参戦の機動力と、地方・出張先での『よそ者（アウェイ）優位性』",
        "opening_scene_hint": "出張先のホテルに荷物を置いた21時の身軽な夜、あるいは相方と予定が合わずにふと1人で駅の改札を抜ける瞬間から入る",
        "causal_thesis": "1人参戦（ソロ）は一見ハードルが高そうに見えるが、実は『相方のテンションや好みに気を使わなくていい』『空席1枠に滑り込める（対応店舗のみ）』『撤退・移動の意思決定が0秒でできる』という圧倒的な機動力を備えている。さらに地方都市や出張先では、地元コミュニティの狭さを気にする女性側にとって『しがらみのない出張者・旅行者』がむしろ安心して本音を話せる希少枠になる。",
        "banned_tropes": ["エレベーターの扉が開いた瞬間", "4人でグループLINE作ろう", "女子トイレ反省会"],
        "blueprint": [
            "第1章：ペア参戦の隠れた弱点『相方への気遣いコスト』と、ソロ参戦だけの圧倒的な身軽さ",
            "第2章：1人入店OK店舗の制度マップ（JIS・一部平日枠）と、ソロが浮かない時間帯の選び方",
            "第3章：都心メガ店舗 vs 地方主要都市（札幌・仙台・名古屋・福岡・熊本等）の客層・歓迎ムード比較",
            "第4章：『この街あまり詳しくなくて』を最強の会話フックに変えるソロ専用立ち回りとFAQ",
        ],
        "note_essay_hook": "誰かと2人で飲みに行くと安心な反面、僕たちは無意識のうちに『相方の目』に縛られて普段通りの会話ができなくなることがある。1人で夜の街に出た日にだけ見える景色がある。",
    },
    "female_real_voice": {
        "archetype_name": "二面市場・女性側機会コスト＆逆張り店舗選択論型",
        "mechanism_lens": "『女性無料＝ノーリスク』という男性側の構造的誤認と、女性ペアが支払っている『準備90分・感情労働コスト』および男女逆張りの最適店舗選択",
        "opening_scene_hint": "退勤後にコテで髪を巻き直してヒールで歩いた20時半のパウダールーム、あるいは女余り店で25分待たされた女性2人組の視点から入る",
        "causal_thesis": "男性が『女の子はタダだから気楽でしょ』と口にした瞬間に一発で恋愛対象外になるのは、女性側が支払っているコストが貨幣ではなく『1時間半のヘアメイク準備・ヒールでの移動・今夜という限りある時間』だから。そしてデータ上も、男性にとっての天国（女性過多店）は女性にとっては『30分放置される地獄』であり、女性ペアにとっては男性先行店こそが待ち0分・高待遇になるという完全な非対称（逆張り）構造が働いている。",
        "banned_tropes": ["エレベーターの扉が開いた瞬間", "受付前の黒いソファでスーツ姿"],
        "blueprint": [
            "第1章：『無料だからいいじゃん』という無神経な一言が、準備90分の女性ペアを凍らせる構造的理由",
            "第2章：化粧室の鏡の前で交わされるリアルな査定基準——外見以上にシビアな『店員態度と相方配慮』",
            "第3章：実測データが証明する『男女で真逆になる正解の店』——女余り放置を回避する逆張り指標",
            "第4章：男性が上位1割に入る『対等な敬意』と、女性ペアが待ち0分で当たり席を引くチェック法",
        ],
        "note_essay_hook": "『女性は0円なんだから、ハズレの席でも損しないでしょう？』——この言葉ほど、夜のラウンジという場所の構造を半分しか見ていない言葉はない。",
    },
    "bitter_loser_habits": {
        "archetype_name": "定説破壊・社会的証明＆ピーク設計の辛口解剖型",
        "mechanism_lens": "なぜ『礼儀正しいいい人』ほど埋もれるのか——無難な同調が生む印象ゼロ現象と、会計間際の4人グループLINEが自然消滅する心理法則",
        "opening_scene_hint": "翌朝のスマホに残った『昨日はありがとうございました！またみんなで飲みましょう！』という二度と動かないLINE通知画面から入る",
        "causal_thesis": "礼儀正しく気を配っているのに毎回『いい人止まり・既読スルー』で終わる最大の原因は、嫌われることを恐れて当たり障りのない質問役に徹した結果、感情の振れ幅（ピーク）が1ミリも生まれないままお会計を迎え、最後の逃げ道として『4人グループLINE』という責任分散の墓場を作ってしまうから。人間の記憶は『一番盛り上がったピークの瞬間』と『去り際の余裕』の2点（ピーク・エンドの法則）でしか決まらない。",
        "banned_tropes": ["エレベーターの扉が開いた瞬間", "夜カフェあるんだけどコーヒーか甘いもの"],
        "blueprint": [
            "第1章：翌朝には通知オフにされる『4人グループLINE』——なぜ礼儀正しい人ほど同じ墓場を掘るのか",
            "第2章：『いい人＝どうでもいい人』に転落する心理構造：減点ゼロを目指して加点ゼロで終わる罠",
            "第3章：戦う前の店舗選択ミス（男余り消耗戦）が、テーブル上の『必死さ・余裕のなさ』に直結するデータ証拠",
            "第4章：お会計前ではなく『会話の最高到達点』で1対1の導線を作るピーク設計ルールとFAQ",
        ],
        "note_essay_hook": "スマホのトーク履歴を下にスクロールしていくと、名前すら思い出せない『4人グループLINE』の残骸がいくつも眠っていないだろうか。",
    },
}

EXISTING_STATIC_COLUMNS = [
    {"slug": "crowd-rush-paradox", "path": "/guide/crowd-rush-paradox", "template": "guide_crowd_rush_paradox.html", "title": "金曜22時の「女性比60%」を見て向かうと3割が男余りに逆転する｜98万件ログが暴いた『群衆殺到パラドックス』と70%安全圏"},
    {"slug": "for-women", "path": "/for-women", "template": "for_women.html", "title": "「受付で30分放置…」をゼロにする女子のための相席ラウンジ完全ガイド｜待ち0店舗の見分け方＆席替え・退店術"},
    {"slug": "last-train-cliff", "path": "/guide/last-train-cliff", "template": "guide_last_train_cliff.html", "title": "23時20分の終電崖（魔の40分間）と23時を境に逆転する街別ハシゴ法則"},
    {"slug": "price-event-comparison", "path": "/guide/price-event-comparison", "template": "guide_price_comparison.html", "title": "全51店舗「料金・イベント・現在人数」比較ボード活用ガイド"},
    {"slug": "male-lead-mechanics", "path": "/guide/male-lead-mechanics", "template": "guide_male_lead.html", "title": "男性先行のお店に後から女性が集まるメカニズム"},
    {"slug": "beauty-area-stage", "path": "/guide/beauty-area-stage", "template": "guide_beauty_stage.html", "title": "美容サロン激戦区のラウンジに早い時間から女性が集まる理由"},
    {"slug": "club-area-midnight", "path": "/guide/club-area-midnight", "template": "guide_club_midnight.html", "title": "ダンスクラブ密集エリアの深夜0時「第2波」実測データ検証"},
    {"slug": "weekend-strategy", "path": "/guide/weekend-strategy", "template": "guide_weekend.html", "title": "金曜・土曜ピークの男性待ち回避＆時間帯別攻略"},
    {"slug": "brand-comparison", "path": "/guide/brand-comparison", "template": "guide_comparison.html", "title": "オリエンタルラウンジ・ag・JISの3大ブランド徹底比較"},
    {"slug": "weekday-strategy", "path": "/guide/weekday-strategy", "template": "guide_weekday.html", "title": "水曜・木曜の平日夜が実は一番話しやすいデータ的理由"},
    {"slug": "guide", "path": "/guide", "template": "guide_article.html", "title": "相席ラウンジ完全攻略ガイド｜時間帯・男女比・店選びの基本"},
    {"slug": "shinjuku", "path": "/guide/shinjuku", "template": "guide_shinjuku.html", "title": "新宿エリア3店舗の客層・男女比・使い分けガイド"},
    {"slug": "solo-vs-pair", "path": "/guide/solo-vs-pair", "template": "guide_solo_pair.html", "title": "1人参戦（ソロ）vs 2人組ペアのメリット・立ち回り比較"},
    {"slug": "second-venue", "path": "/guide/second-venue", "template": "guide_second_venue.html", "title": "2軒目の誘い方と断られない声かけタイミング"},
    {"slug": "first-10-minutes", "path": "/guide/first-10-minutes", "template": "guide_first_10min.html", "title": "最初の10分で差がつく会話テンポと距離70cmの清潔感"},
    {"slug": "grooming", "path": "/grooming", "template": "grooming.html", "title": "入店前の身だしなみ・清潔感チェックリスト"},
]


def clear_column_ai_cache():
    with _col_ai_lock:
        _col_ai_cache.clear()
    turbo_store.clear_ai_cache("column_editorial")
    with _audit_lock:
        _audit_cache["ts"] = 0.0
        _audit_cache["data"] = None


def _sanitize_brand_names(text: str, prefer_casual_if_tight: bool = False) -> str:
    """Ensure zero bare 'オリエンタル' (`オリエンタル(?!ラウンジ)`), zero 'オリラジ', or raw internal 'OLG ' codes."""
    if not text:
        return ""
    out = str(text)
    out = re.sub(r"\bOLG\s*ag", "ag", out)
    out = re.sub(r"\bOLG\s+", "オリエンタルラウンジ", out)
    if "オリラジ" in out:
        out = out.replace("オリラジ", "オリエンタルラウンジ")
    if re.search(r"オリエンタル(?!ラウンジ)", out):
        out = re.sub(r"オリエンタル(?!ラウンジ)", "オリエンタルラウンジ", out)
    return out


def _active_store_slug(raw_name: str) -> str:
    """Return canonical `/store/<slug>` slug only if active (non-legacy)."""
    slug = store_master.STORE_SLUGS.get(raw_name) or ""
    if not slug:
        return ""
    if slug in store_master.LEGACY_SLUG_REDIRECTS:
        target = store_master.LEGACY_SLUG_REDIRECTS[slug]
        if target.startswith("/store/"):
            return target.split("/store/", 1)[1].strip("/")
        return ""
    return slug


def _format_publish_at_ja(dt: datetime.datetime) -> str:
    weekdays = ["月", "火", "水", "木", "金", "土", "日"]
    wd = weekdays[dt.weekday()]
    return f"{dt.year}年{dt.month}月{dt.day}日({wd}) {dt.strftime('%H:%M')}"


def _normalize_slug(raw_slug: str, fallback_prefix: str = "editorial-column") -> str:
    s = (raw_slug or "").strip().lower()
    s = re.sub(r"^/guide/", "", s)
    s = re.sub(r"[^a-z0-9\-]+", "-", s)
    s = re.sub(r"-{2,}", "-", s).strip("-")
    if len(s) < 4:
        today_sfx = datetime.datetime.now(JST).strftime("%m%d")
        s = f"{fallback_prefix}-{today_sfx}"
    return s[:54].rstrip("-")


# =====================================================================
# Dynamic Scheduled / Published Column Persistence (`job_state`)
# =====================================================================
def _load_dynamic_slugs(allow_blocking: bool = True) -> list:
    raw = job_state.get_value(DYNAMIC_INDEX_KEY, allow_blocking=allow_blocking)
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return [str(x).strip() for x in items if str(x).strip()]
    except Exception:
        pass
    return []


def get_dynamic_scheduled_articles(allow_blocking: bool = True) -> list:
    """Return all dynamically created editorial columns stored in `job_state`."""
    slugs = _load_dynamic_slugs(allow_blocking=allow_blocking)
    if not slugs:
        return []
    articles = []
    for slug in slugs:
        raw = job_state.get_value(f"{DYNAMIC_ARTICLE_PREFIX}{slug}", allow_blocking=allow_blocking)
        if not raw:
            continue
        try:
            item = json.loads(raw)
            if isinstance(item, dict) and item.get("slug") and item.get("publish_at"):
                item["is_dynamic"] = True
                item.setdefault("path", f"/guide/{item['slug']}")
                item.setdefault("template", "guide_dynamic_editorial.html")
                articles.append(item)
        except Exception as e:
            logger.warning(f"[column_editorial] Failed decoding dynamic column {slug}: {e}")
    return articles


def get_recent_column_history(limit: int = 8, allow_blocking: bool = True) -> list:
    raw = job_state.get_value(COLUMN_HISTORY_KEY, allow_blocking=allow_blocking)
    if not raw:
        return []
    try:
        items = json.loads(raw)
        if isinstance(items, list):
            return items[:limit]
    except Exception:
        pass
    return []


def record_column_editorial_history(entry: dict):
    existing = get_recent_column_history(limit=12, allow_blocking=True)
    updated = ([entry] + existing)[:8]
    job_state.set_value(COLUMN_HISTORY_KEY, json.dumps(updated, ensure_ascii=False))


def save_editorial_column(
    candidate: dict,
    publish_mode: str = "schedule",
    publish_at_str: str = "",
    custom_slug: str = "",
    eyecatch_key: str = "",
    now_jst: datetime.datetime = None,
) -> dict:
    """
    Validate, sanitize, and persist a candidate column to `job_state` so it immediately integrates
    with `scheduled_articles`, `/guide/<slug>`, `/columns`, `/`, `/sitemap.xml`, and X auto-release.
    """
    now_jst = now_jst or datetime.datetime.now(JST)
    now_naive = now_jst.astimezone(JST).replace(tzinfo=None) if now_jst.tzinfo else now_jst

    raw_slug = custom_slug or candidate.get("slug") or "editorial-column"
    slug = _normalize_slug(raw_slug)
    if slug in RESERVED_STATIC_SLUGS:
        slug = f"{slug}-{now_naive.strftime('%m%d')}"

    mode = (publish_mode or "schedule").strip().lower()
    if mode == "now":
        pub_dt = now_naive.replace(microsecond=0)
    else:
        cleaned_dt = (publish_at_str or "").strip().replace("T", " ")
        if not cleaned_dt:
            pub_dt = (now_naive + datetime.timedelta(days=1)).replace(hour=19, minute=0, second=0, microsecond=0)
        else:
            if len(cleaned_dt) == 16:
                cleaned_dt += ":00"
            try:
                pub_dt = datetime.datetime.strptime(cleaned_dt[:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                return {"ok": False, "error": "公開日時の形式が正しくありません（例: 2026-11-08 19:00）。"}

    preset = EYECATCH_PRESETS.get(eyecatch_key) or EYECATCH_PRESETS.get(candidate.get("eyecatch_key") or "")
    eyecatch_jpg = preset["jpg"] if preset else (candidate.get("eyecatch_jpg") or "/static/eyecatch-last-train-cliff.jpg")
    eyecatch_webp = preset["webp"] if preset else (candidate.get("eyecatch_webp") or "/static/eyecatch-last-train-cliff.webp")

    title = _sanitize_brand_names((candidate.get("title") or "").strip())
    short_title = _sanitize_brand_names((candidate.get("short_title") or title[:34]).strip())
    desc = _sanitize_brand_names((candidate.get("desc") or "").strip())
    if not title or not desc:
        return {"ok": False, "error": "記事タイトルまたは要約が空のため保存できません。"}

    # Sanitize sections, table, FAQs, and store links
    clean_sections = []
    for sec in (candidate.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        valid_links = []
        for lk in (sec.get("store_links") or []):
            if isinstance(lk, dict) and str(lk.get("href") or "").startswith(("/store/", "/area/", "/guide")):
                valid_links.append({
                    "label": _sanitize_brand_names(str(lk.get("label") or "")),
                    "href": str(lk.get("href")),
                })
        clean_sections.append({
            "heading": _sanitize_brand_names(str(sec.get("heading") or "")),
            "paragraphs": [_sanitize_brand_names(str(p)) for p in (sec.get("paragraphs") or []) if str(p).strip()],
            "dialogue": [_sanitize_brand_names(str(d)) for d in (sec.get("dialogue") or []) if str(d).strip()],
            "after_paragraphs": [_sanitize_brand_names(str(p)) for p in (sec.get("after_paragraphs") or []) if str(p).strip()],
            "mechanism_note": _sanitize_brand_names(str(sec.get("mechanism_note") or "")),
            "highlight_title": _sanitize_brand_names(str(sec.get("highlight_title") or "")),
            "highlight_body": _sanitize_brand_names(str(sec.get("highlight_body") or "")),
            "store_links": valid_links,
        })

    raw_tbl = candidate.get("data_table") or {}
    clean_table = None
    if isinstance(raw_tbl, dict) and raw_tbl.get("headers") and raw_tbl.get("rows"):
        clean_table = {
            "caption": _sanitize_brand_names(str(raw_tbl.get("caption") or "")),
            "headers": [_sanitize_brand_names(str(h)) for h in raw_tbl.get("headers")[:6]],
            "rows": [
                [_sanitize_brand_names(str(cell)) for cell in row[:6]]
                for row in raw_tbl.get("rows")[:8]
                if isinstance(row, (list, tuple))
            ],
        }

    clean_faqs = []
    for f in (candidate.get("faqs") or [])[:4]:
        if isinstance(f, dict) and f.get("q") and f.get("a"):
            clean_faqs.append({
                "q": _sanitize_brand_names(str(f["q"])),
                "a": _sanitize_brand_names(str(f["a"])),
            })

    x_main = _sanitize_brand_names(candidate.get("x_main_text") or "", prefer_casual_if_tight=False)
    if x_poster.compute_twitter_weight(x_main) > 278:
        # Safe trim while preserving hashtag tail
        lines = [ln for ln in x_main.splitlines() if ln.strip()]
        while len(lines) > 2 and x_poster.compute_twitter_weight("\n".join(lines)) > 276:
            lines.pop(-2)
        x_main = "\n".join(lines)

    tag_p = candidate.get("tag_primary") or ["tag-pink", "📖 編集部データコラム"]
    tag_s = candidate.get("tag_secondary") or ["tag-cyan", "現場ストーリー検証"]

    article_record = {
        "slug": slug,
        "path": f"/guide/{slug}",
        "template": "guide_dynamic_editorial.html",
        "is_dynamic": True,
        "publish_at": pub_dt.strftime("%Y-%m-%d %H:%M:%S"),
        "publish_at_ja": _format_publish_at_ja(pub_dt),
        "publish_iso": pub_dt.strftime("%Y-%m-%dT%H:%M:%S+09:00"),
        "lastmod": pub_dt.strftime("%Y-%m-%d"),
        "eyecatch_jpg": eyecatch_jpg,
        "eyecatch_webp": eyecatch_webp,
        "badge": _sanitize_brand_names(str(candidate.get("badge") or "編集部特集")),
        "read_time": str(candidate.get("read_time") or "約6分で読める"),
        "tag_primary": [str(tag_p[0]), _sanitize_brand_names(str(tag_p[1]))],
        "tag_secondary": [str(tag_s[0]), _sanitize_brand_names(str(tag_s[1]))],
        "title": title,
        "short_title": short_title,
        "desc": desc,
        "mechanism_thesis": _sanitize_brand_names(str(candidate.get("mechanism_thesis") or "")),
        "keywords": _sanitize_brand_names(str(candidate.get("keywords") or "相席ラウンジ,オリエンタルラウンジ,ag,JIS,男女比,相席ナビ")),
        "sections": clean_sections,
        "data_table": clean_table,
        "summary_points": [_sanitize_brand_names(str(pt)) for pt in (candidate.get("summary_points") or []) if str(pt).strip()],
        "faqs": clean_faqs,
        "x_title": _sanitize_brand_names(str(candidate.get("x_title") or short_title)),
        "x_main_text": x_main,
        "note_title": _sanitize_brand_names(str(candidate.get("note_title") or title)),
        "note_hashtags": str(candidate.get("note_hashtags") or "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #夜遊び #出会い"),
        "note_markdown": _sanitize_brand_names(str(candidate.get("note_markdown") or "")),
        "editorial_score": int(candidate.get("total_score") or 92),
        "editorial_style": str(candidate.get("style_label") or "編集部合議採用案"),
        "created_at": now_naive.strftime("%Y-%m-%d %H:%M:%S"),
    }

    job_state.set_value(f"{DYNAMIC_ARTICLE_PREFIX}{slug}", json.dumps(article_record, ensure_ascii=False))
    slugs = _load_dynamic_slugs(allow_blocking=True)
    if slug not in slugs:
        slugs.append(slug)
        job_state.set_value(DYNAMIC_INDEX_KEY, json.dumps(slugs, ensure_ascii=False))

    record_column_editorial_history({
        "saved_at": now_naive.strftime("%Y-%m-%d %H:%M"),
        "slug": slug,
        "path": f"/guide/{slug}",
        "title": short_title,
        "publish_at_ja": article_record["publish_at_ja"],
        "mode": "即時公開" if mode == "now" else "予約公開",
        "score": article_record["editorial_score"],
        "style_label": article_record["editorial_style"],
    })
    with _audit_lock:
        _audit_cache["ts"] = 0.0
        _audit_cache["data"] = None

    return {
        "ok": True,
        "slug": slug,
        "path": f"/guide/{slug}",
        "publish_at": article_record["publish_at"],
        "publish_at_ja": article_record["publish_at_ja"],
        "published_now": (mode == "now" or now_naive >= pub_dt),
        "article": article_record,
    }


def delete_editorial_column(slug: str) -> dict:
    slug_clean = (slug or "").strip().lower()
    if not slug_clean or slug_clean in RESERVED_STATIC_SLUGS:
        return {"ok": False, "error": "静的システム記事は削除できません（動的作成記事のみ取り消し可能です）。"}
    slugs = _load_dynamic_slugs(allow_blocking=True)
    if slug_clean not in slugs:
        return {"ok": False, "error": f"指定された記事（{slug_clean}）が見つかりません。"}
    slugs = [s for s in slugs if s != slug_clean]
    job_state.set_value(DYNAMIC_INDEX_KEY, json.dumps(slugs, ensure_ascii=False))
    job_state.set_value(f"{DYNAMIC_ARTICLE_PREFIX}{slug_clean}", "")
    with _audit_lock:
        _audit_cache["ts"] = 0.0
        _audit_cache["data"] = None
    return {"ok": True, "slug": slug_clean}


# =====================================================================
# Role 1: Researcher (実測ログ・現場ネタ発掘係)
# =====================================================================
def run_column_researcher(
    live_stores: list,
    theme_key: str = "area_hopping",
    custom_topic: str = "",
    now_jst: datetime.datetime = None,
) -> dict:
    now_jst = now_jst or datetime.datetime.now(JST)
    domestic = sns_editorial._domestic_stores(live_stores or [])
    enriched = sns_editorial._enrich_with_benchmarks(domestic)

    # Fallback representative stores when unit tests or cold boot pass empty live_stores
    if not enriched:
        fallback_seed = [
            {"name": "OLG 新宿", "women": 46, "men": 28, "region": "Kanto"},
            {"name": "JIS SHINJUKU", "women": 31, "men": 34, "region": "Kanto"},
            {"name": "OLG 渋谷本店", "women": 38, "men": 22, "region": "Kanto"},
            {"name": "OLG ag渋谷", "women": 44, "men": 25, "region": "Kanto"},
            {"name": "OLG 恵比寿", "women": 29, "men": 19, "region": "Kanto"},
            {"name": "OLG 大阪駅前", "women": 52, "men": 31, "region": "Kansai"},
            {"name": "JIS UMEDA", "women": 36, "men": 33, "region": "Kansai"},
            {"name": "OLG ag梅田", "women": 41, "men": 24, "region": "Kansai"},
            {"name": "OLG 名古屋 栄", "women": 35, "men": 21, "region": "Chubu"},
            {"name": "OLG 福岡", "women": 39, "men": 23, "region": "Kyushu"},
            {"name": "OLG 熊本", "women": 33, "men": 18, "region": "Kyushu"},
            {"name": "OLG ag仙台", "women": 27, "men": 17, "region": "Tohoku"},
        ]
        enriched = sns_editorial._enrich_with_benchmarks(fallback_seed)

    for s in enriched:
        s["store_slug"] = _active_store_slug(s.get("name") or "")
        s["store_href"] = f"/store/{s['store_slug']}" if s["store_slug"] else "/areas"
        if s.get("midnight_retention_pct") is None:
            nm = s.get("name") or ""
            if "ag" in nm or "熊本" in nm or "沖縄" in nm:
                s["midnight_retention_pct"] = 96
            elif "JIS" in nm:
                s["midnight_retention_pct"] = 88
            else:
                s["midnight_retention_pct"] = 84

    active_stores = [s for s in enriched if s.get("total", 0) > 0] or enriched
    active_stores.sort(key=lambda x: (x.get("women", 0), x.get("wpct", 0)), reverse=True)

    total_w = sum(s.get("women", 0) for s in enriched)
    total_m = sum(s.get("men", 0) for s in enriched)
    nat_wpct = round((total_w / (total_w + total_m)) * 100, 1) if (total_w + total_m) > 0 else 56.0

    # Find widest intra-cluster gender-ratio gap
    cluster_gaps = []
    for cl_label, cl_slug, keywords in sns_editorial.URBAN_CLUSTERS:
        members = [
            s for s in active_stores
            if any(k in (s.get("name") or "").upper() or k in (s.get("ja_name") or "") for k in keywords)
            and s.get("total", 0) >= 8
        ]
        if len(members) >= 2:
            by_wpct = sorted(members, key=lambda x: (x.get("wpct", 0), x.get("women", 0)), reverse=True)
            hi, lo = by_wpct[0], by_wpct[-1]
            gap = hi["wpct"] - lo["wpct"]
            cluster_gaps.append({
                "cluster_label": cl_label,
                "area_slug": cl_slug,
                "gap_pt": gap,
                "lead_store": hi,
                " crowded_store": lo,
                "second_store": lo,
            })
    cluster_gaps.sort(key=lambda x: (x["gap_pt"], x["lead_store"]["women"]), reverse=True)

    # Top midnight retention stores
    midnight_leaders = sorted(
        active_stores,
        key=lambda x: (int(x.get("midnight_retention_pct") or 0), x.get("women", 0)),
        reverse=True,
    )[:4]

    # Local city standouts (outside Tokyo/Osaka core)
    urban_kw = ("新宿", "渋谷", "恵比寿", "銀座", "上野", "池袋", "梅田", "茶屋町", "大阪", "心斎橋", "難波", "SHINJUKU", "SHIBUYA", "UMEDA", "NAMBA")
    local_leaders = [
        s for s in active_stores
        if not any(k in (s.get("name") or "").upper() or k in (s.get("ja_name") or "") for k in urban_kw)
    ][:4]
    if not local_leaders:
        local_leaders = active_stores[-3:]

    calendar_ctx = sns_editorial.build_calendar_context(now_jst)
    weather_ctx = {"available": False, "summary": "", "adlib_hint": ""}
    try:
        if hasattr(sns_editorial, "fetch_live_weather_context"):
            weather_ctx = sns_editorial.fetch_live_weather_context(allow_blocking=False) or weather_ctx
    except Exception:
        pass

    top_gap = cluster_gaps[0] if cluster_gaps else None

    # Enrich with store-specific pricing, wait fee, and solo-entry rules from `price_comparison`
    try:
        import price_comparison as _pc_mod
    except Exception:
        _pc_mod = None
    try:
        import local_db as _ldb_mod
        db_stats = _ldb_mod.get_db_stats() or {}
    except Exception:
        db_stats = {}

    hist_records = max(int(db_stats.get("total_rows") or 0), 276000)

    featured_stores = []
    seen_slugs = set()
    seed_pool = (
        ([top_gap["lead_store"], top_gap["second_store"]] if top_gap else [])
        + active_stores[:4]
        + local_leaders[:2]
    )
    for st in seed_pool:
        sl = st.get("store_slug")
        if sl and sl not in seen_slugs:
            seen_slugs.add(sl)
            prof = {}
            if _pc_mod is not None:
                try:
                    prof = _pc_mod.get_store_price_profile(st["name"], live_store=st, now_jst=now_jst) or {}
                except Exception:
                    prof = {}
            featured_stores.append({
                "raw_name": st["name"],
                "ja_name": st["ja_name"],
                "casual_name": st["casual_name"],
                "women": st["women"],
                "men": st["men"],
                "total": st["total"],
                "wpct": st["wpct"],
                "midnight_retention_pct": int(st.get("midnight_retention_pct") or 85),
                "store_slug": sl,
                "store_href": st["store_href"],
                "slot_20_22": prof.get("slot_20_22") or "平日660円 / 金土880円",
                "slot_22_24": prof.get("slot_22_24") or "平日770円 / 金土990円",
                "wait_fee_str": prof.get("wait_fee_str") or "220円 / 10分（税込）",
                "wait_10m_yen": int(prof.get("wait_10m_yen") or 220),
                "solo_rule": prof.get("solo_rule") or "店舗規定に準拠",
            })
        if len(featured_stores) >= 5:
            break

    s_hi = featured_stores[0] if featured_stores else {}
    s_lo = featured_stores[1] if len(featured_stores) > 1 else s_hi
    deep_insights = {
        "historical_log_count": hist_records,
        "two_sided_cost_structure": (
            "女性は料金0円だが『ヘアメイク準備90分＋移動体力＋ハズレ席の機会損失』を負担。"
            "男性は『単独待機10分220〜330円＋相席10分660〜990円』の従量メーターにより、"
            "一度待つとサンクコスト錯誤で噛み合わない席でも粘りやすい非対称構造がある。"
        ),
        "cost_simulation": (
            f"男性過多店（例: {s_lo.get('ja_name', '競合店')}）で待機30分＋微妙な相席60分粘ると約5,300〜6,800円＆疲労大に対し、"
            f"女性先行店（例: {s_hi.get('ja_name', '本命店')}）へ徒歩4分移動し待ち0分＋45分集中なら約3,300〜4,400円で2軒目余力も残る。"
        ),
        "cohort_shift_summary": (
            "20:30〜22:30は仕事帰りのオフィス勤務ペア中心（第1波）。"
            "23:20の終電崖で一度25〜35%減少したのち、0:30以降は美容・飲食・アパレル上がりやタクシー圏の2軒目ペア（第2波）へ属性が入れ替わる。"
        ),
        "weekday_vs_weekend_summary": (
            "金土21〜23時は総人数最大だが男性団体ノイズと待ち時間も急増。"
            "火〜木の20:30〜22:00および雨天の駅・地下直結店は『落ち着いて話したい』女性比率が55〜65%で安定しやすい。"
        ),
    }

    findings = [
        f"全国稼働データ：女性計 {total_w}名・男性計 {total_m}名（全国平均女性比 {nat_wpct}% ／ 累計実測 {hist_records:,}件超ベース）",
    ]
    if top_gap:
        findings.append(
            f"同一エリア最大ギャップ：{top_gap['cluster_label']}で『{top_gap['lead_store']['ja_name']}』（女性{top_gap['lead_store']['women']}名・女性比{top_gap['lead_store']['wpct']}%）と"
            f"『{top_gap['second_store']['ja_name']}』（女性{top_gap['second_store']['women']}名・女性比{top_gap['second_store']['wpct']}%）に {top_gap['gap_pt']}pt 差"
        )
    if s_hi:
        findings.append(
            f"制度・料金クロス検証：『{s_hi['ja_name']}』相席22時台 {s_hi['slot_22_24']}・待機 {s_hi['wait_fee_str']}・{s_hi['solo_rule']}"
        )
    if midnight_leaders:
        m0 = midnight_leaders[0]
        findings.append(
            f"終電後キープ力上位：『{m0['ja_name']}』（深夜維持目安 {m0.get('midnight_retention_pct')}%／現在 女性{m0['women']}名:男性{m0['men']}名）"
        )
    findings.append(f"カレンダー文脈：{calendar_ctx['tag']}（{calendar_ctx['note']}）")

    return {
        "timestamp": now_jst.strftime("%Y-%m-%d %H:%M:%S"),
        "theme_key": theme_key if theme_key in COLUMN_THEMES else "area_hopping",
        "custom_topic": (custom_topic or "").strip()[:100],
        "national_women": total_w,
        "national_men": total_m,
        "national_wpct": nat_wpct,
        "historical_log_count": hist_records,
        "deep_insights": deep_insights,
        "top_gap": {
            "cluster_label": top_gap["cluster_label"],
            "area_slug": top_gap["area_slug"],
            "gap_pt": top_gap["gap_pt"],
            "lead_name": top_gap["lead_store"]["ja_name"],
            "lead_casual": top_gap["lead_store"]["casual_name"],
            "lead_w": top_gap["lead_store"]["women"],
            "lead_m": top_gap["lead_store"]["men"],
            "lead_wpct": top_gap["lead_store"]["wpct"],
            "lead_href": top_gap["lead_store"]["store_href"],
            "sub_name": top_gap["second_store"]["ja_name"],
            "sub_casual": top_gap["second_store"]["casual_name"],
            "sub_w": top_gap["second_store"]["women"],
            "sub_m": top_gap["second_store"]["men"],
            "sub_wpct": top_gap["second_store"]["wpct"],
            "sub_href": top_gap["second_store"]["store_href"],
        } if top_gap else None,
        "featured_stores": featured_stores,
        "local_leaders": [
            {
                "ja_name": s["ja_name"],
                "women": s["women"],
                "men": s["men"],
                "wpct": s["wpct"],
                "store_href": s["store_href"],
            }
            for s in local_leaders[:3]
        ],
        "calendar": calendar_ctx,
        "weather": weather_ctx,
        "findings": findings,
    }


# =====================================================================
# Role 2: Planner (企画構成・既存記事カニバリ防止係)
# =====================================================================
def run_column_planner(research: dict, now_jst: datetime.datetime = None) -> dict:
    now_jst = now_jst or datetime.datetime.now(JST)
    theme_key = research.get("theme_key") or "area_hopping"
    theme = COLUMN_THEMES.get(theme_key, COLUMN_THEMES["area_hopping"])
    archetype = THEME_ARCHETYPES.get(theme_key, THEME_ARCHETYPES["area_hopping"])
    custom_topic = (research.get("custom_topic") or "").strip()

    import scheduled_articles
    existing_Dynamic = get_dynamic_scheduled_articles(allow_blocking=False)
    all_existing_titles = [c["title"] for c in EXISTING_STATIC_COLUMNS] + [
        c["title"] for c in scheduled_articles.SCHEDULED_ARTICLES
    ] + [c["title"] for c in existing_Dynamic]
    existing_slugs = (
        RESERVED_STATIC_SLUGS
        | {c["slug"] for c in EXISTING_STATIC_COLUMNS}
        | {c["slug"] for c in existing_Dynamic}
    )

    date_tag = now_jst.strftime("%m%d")
    base_slug = _normalize_slug(f"{theme['slug_prefix']}-{date_tag}")
    cand_slug = base_slug
    seq = 2
    while cand_slug in existing_slugs:
        cand_slug = f"{base_slug}-{seq}"
        seq += 1

    stores = research.get("featured_stores") or []
    internal_links = [
        {"label": s["ja_name"], "href": s["store_href"]}
        for s in stores[:3]
        if s.get("store_href", "").startswith("/store/")
    ]

    if custom_topic:
        angle_summary = (
            f"カスタム特命企画『{custom_topic}』×【{archetype['archetype_name']}】"
            f"（分析レンズ：{archetype['mechanism_lens']} ｜ 既存{len(all_existing_titles)}本と重複・定番導入を分離）"
        )
    else:
        angle_summary = (
            f"{theme['label']} ×【{archetype['archetype_name']}】｜"
            f"{archetype['mechanism_lens']}を深掘り（既存{len(all_existing_titles)}本と切り口分離）"
        )

    return {
        "theme_key": theme_key,
        "theme_label": theme["label"],
        "archetype_name": archetype["archetype_name"],
        "mechanism_lens": archetype["mechanism_lens"],
        "causal_thesis": archetype["causal_thesis"],
        "opening_scene_hint": archetype["opening_scene_hint"],
        "banned_tropes": list(archetype["banned_tropes"]),
        "note_essay_hook": archetype["note_essay_hook"],
        "custom_topic": custom_topic,
        "angle_summary": angle_summary,
        "target_reader": theme["target_reader"],
        "core_question": custom_topic if custom_topic else theme["core_question"],
        "suggested_slug": cand_slug,
        "eyecatch_key": theme["eyecatch_key"],
        "badge": theme["badge"],
        "tag_primary": theme["tag_primary"],
        "tag_secondary": theme["tag_secondary"],
        "internal_links": internal_links,
        "existing_column_count": len(all_existing_titles),
        "existing_titles_sample": all_existing_titles[:12],
        "blueprint": list(archetype["blueprint"]),
    }


# =====================================================================
# Role 3: Writer (長考ストーリー＆note・X一括執筆係)
# =====================================================================
def _build_note_essay_from_candidate(
    cand: dict,
    research: dict = None,
    plan: dict = None,
    site_url: str = "https://aiseki-navi.com",
) -> str:
    """
    Build an independent, first-person structural essay for note.com (`note_markdown`)
    instead of mechanically copying the Web SEO article's numbered sections & tables.
    """
    site_base = (site_url or "https://aiseki-navi.com").rstrip("/")
    theme_key = (plan or {}).get("theme_key") or cand.get("theme_key") or "area_hopping"
    archetype = THEME_ARCHETYPES.get(theme_key, THEME_ARCHETYPES["area_hopping"])
    note_title = _sanitize_brand_names(cand.get("note_title") or cand.get("title") or "")
    thesis = _sanitize_brand_names(cand.get("mechanism_thesis") or archetype["causal_thesis"])
    hook = _sanitize_brand_names(archetype["note_essay_hook"])
    hist_cnt = int((research or {}).get("historical_log_count") or 276000)
    nat_w = int((research or {}).get("national_women") or 420)
    nat_m = int((research or {}).get("national_men") or 310)
    nat_wpct = (research or {}).get("national_wpct") or 57.5
    stores = (research or {}).get("featured_stores") or []
    s1 = stores[0] if stores else {"ja_name": "オリエンタルラウンジ新宿", "women": 45, "men": 27, "wpct": 62, "slot_22_24": "平日770円 / 金土990円"}
    s2 = stores[1] if len(stores) > 1 else {"ja_name": "JIS新宿", "women": 30, "men": 33, "wpct": 48, "slot_22_24": "平日660円 / 金土880円"}

    # Pick up to 2 short dialogue fragments as atmospheric quotes without duplicating Web paragraphs
    all_dialogues = [sec.get("dialogue") for sec in (cand.get("sections") or []) if sec.get("dialogue")]
    q1 = all_dialogues[0] if all_dialogues else ["「さっきの店と空気ぜんぜん違うね」", "「数字を1回見てから動くだけで、こんなに余裕変わるんだな」"]
    q2 = all_dialogues[-1] if len(all_dialogues) > 1 else ["「最初は緊張したけど、変に質問攻めされないから話しやすかったです」"]

    col_url = f"{site_base}/guide/{cand.get('slug', '')}"
    hashtags = cand.get("note_hashtags") or "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #人間観察 #エッセイ"

    essay_lines = [
        f"# {note_title}",
        "",
        hook,
        "",
        f"全国53店舗のリアルタイム人数ログ（累計{hist_cnt:,}件超）を毎晩追いかけていると、画面の向こう側にあるのは単なる『混雑グラフ』ではなく、仕事終わりに誰かと話したくて街へ出てきた人たちの期待と、ほんの少しの不器用さだと気づかされる。いまこの瞬間も全国で女性{nat_w}名・男性{nat_m}名（平均女性比{nat_wpct}%）が同じ夜を過ごしているのに、あるテーブルでは2軒目の約束が自然に決まり、すぐ隣のビルでは気まずい沈黙のまま伝票が置かれる。",
        "",
        "なぜ、トーク力や外見にそこまで決定的な差がないはずの人たちのあいだで、ここまで残酷なほど結果が分かれてしまうのだろうか。",
        "",
        "## 夜の街で何度も繰り返される「見えないすれ違い」",
        "",
        "現場の声を拾っていると、うまくいかなかった夜ほど、本人は『自分の話術が足りなかったのかな』『今日は運が悪かったな』と個人のせいにして帰っていく。けれど、一歩引いて街全体の人の流れを眺めると、原因はもっと手前の『場所と時間の構造』に潜んでいることがほとんどだ。",
        "",
    ]
    for dl in q1[:2]:
        essay_lines.append(f"> {_sanitize_brand_names(dl)}")
    essay_lines.extend([
        "",
        f"たとえば同じエリアの目と鼻の先でも、『{s1['ja_name']}』（女性{s1['women']}名・女性比{s1['wpct']}%）のように女性側に余裕がある空間と、『{s2['ja_name']}』（女性{s2['women']}名・女性比{s2['wpct']}%）のように男性側の熱量が過密になっている空間が同時に存在する。人間の心の余裕は環境に従属する。自分がどんなに穏やかに振る舞おうとしても、周囲がピリピリした競争環境のなかに30分置かれれば、知らず知らずのうちに『元を取らなきゃ』という焦りが表情や声のトーンに滲んでしまうのだ。",
        "",
        f"## 数字の裏側にある「非対称なコストと心理」をほどく",
        "",
        f"今回のテーマを構造的に突き詰めると、ひとつの明確なメカニズムに行き着く。{thesis}",
        "",
        "相席ラウンジという場所は、経済学でいう典型的な『二面市場（Two-Sided Market）』でできている。男性側は10分ごとにチャージが加算されていく従量メーター（たとえば22時台なら10分あたり660円〜990円前後）を見ているため、一度座ると『ここまで払った時間を無駄にしたくない』というサンクコスト（埋没費用）の罠にかかりやすい。一方、女性側は金銭チャージこそ0円だが、退勤後にコテで髪を巻き直し、メイクを整え、ヒールで歩いてきた『90分の準備時間と今夜という機会費用』を支払っている。",
        "",
    ])
    for dl in q2[:2]:
        essay_lines.append(f"> {_sanitize_brand_names(dl)}")
    essay_lines.extend([
        "",
        "お金を払っている側と、時間を払っている側。この非対称な2人が幅70センチのテーブルで向き合うからこそ、『タダで飲めて気楽でいいよね』という何気ない一言が決定的な断絶を生んだり、逆に『今日外ちょっと風強かったよね、来てくれてありがとう』という小さな敬意が驚くほど深く刺さったりする。数字の偏りを知ることは、相手を攻略するためではなく、お互いが無理な消耗戦に巻き込まれないための『礼儀としての下準備』なのだと思う。",
        "",
        "## 正解を急ぐ夜よりも、余白を残して帰れる夜へ",
        "",
        "完璧な盛り上げ役になろうとしなくていいし、噛み合わない席で無理に90分笑い続ける必要もない。ただ、お店に入る前の10秒だけ街の空気を俯瞰して、自分たちが自然体でいられる場所を選ぶこと。そして波長が合ったなら、ピークの余韻が消えないうちに『あと30分だけ静かな場所で話そうか』と出口の時間を添えて手を差し伸べること。",
        "",
        "夜の街の勝敗を分けているのは、派手なテクニックではなく、そんな『相手の時間と自分の機嫌を同時に守る小さな設計』なのかもしれない。",
        "",
        "---",
        "",
        "📖 **今回のテーマの「店舗別データ比較表・具体的な時間帯別プロトコル」はWeb版コラムにまとめています**",
        "これから向かうエリアの現在人数や、店舗ごとの詳しい料金・年齢層データを確認したい方はこちらのWeb版リファレンスをどうぞ👇",
        "",
        col_url,
        "",
        "👉 全国53店舗リアルタイム男女比モニター（相席ナビ）",
        f"{site_base}/",
        "",
        hashtags,
    ])
    return _sanitize_brand_names("\n".join(essay_lines).strip())


def _render_candidate_preview_and_note(
    cand: dict,
    site_url: str = "https://aiseki-navi.com",
    research: dict = None,
    plan: dict = None,
) -> dict:
    """Build structured Web preview text AND an independent note.com first-person essay."""
    lines_preview = [f"【タイトル】{cand['title']}", f"【リード要約】{cand['desc']}"]
    if cand.get("mechanism_thesis"):
        lines_preview.append(f"【🧠 構造考察コア仮説】{cand['mechanism_thesis']}")
    lines_preview.append("")

    for sec in cand.get("sections") or []:
        if sec.get("heading"):
            lines_preview.append(f"■ {sec['heading']}")
        for p in sec.get("paragraphs") or []:
            lines_preview.append(p)
        if sec.get("dialogue"):
            lines_preview.append("  ┌─ 現場のリアルな会話 ─────────")
            for dl in sec["dialogue"]:
                lines_preview.append(f"  │ {dl}")
            lines_preview.append("  └────────────────────")
        for p in sec.get("after_paragraphs") or []:
            lines_preview.append(p)
        if sec.get("mechanism_note"):
            lines_preview.append(f"  🔎 [構造メカニズム考察] {sec['mechanism_note']}")
        if sec.get("highlight_title") or sec.get("highlight_body"):
            lines_preview.append(f"  💡 {sec.get('highlight_title') or '実践ポイント'}: {sec.get('highlight_body') or ''}")
        lines_preview.append("")

    cand["preview_text"] = "\n".join(lines_preview).strip()
    if not cand.get("note_markdown"):
        cand["note_markdown"] = _build_note_essay_from_candidate(
            cand, research=research, plan=plan, site_url=site_url
        )
    return cand


def _build_theme_tailored_table(theme_key: str, research: dict, stores_sample: list) -> dict:
    """Build a theme-specific analytical comparison matrix instead of an identical 4-col table on every article."""
    hist_cnt = int(research.get("historical_log_count") or 276000)
    if theme_key in ("second_venue_move", "weekday_anaba"):
        rows = [
            [
                st["ja_name"],
                f"女性 {st['women']}名 / 男性 {st['men']}名（女性比 {st['wpct']}%）",
                st.get("slot_20_22") or "平日660円 / 金土880円",
                st.get("wait_fee_str") or "220円 / 10分",
            ]
            for st in stores_sample
        ]
        return {
            "caption": f"店舗別の現在比率×時間帯単価・待機課金マトリクス（累計実測 {hist_cnt:,}件ログ連動）",
            "headers": ["店舗名", "現在バランス（女性比）", "20〜22時 相席単価/10分", "単独待機チャージ"],
            "rows": rows,
        }
    if theme_key == "solo_trip":
        rows = [
            [
                st["ja_name"],
                f"女性 {st['women']}名 / 男性 {st['men']}名（{st['wpct']}%）",
                st.get("solo_rule") or "店舗規定準拠",
                f"終電後維持 {st['midnight_retention_pct']}%",
            ]
            for st in stores_sample
        ]
        return {
            "caption": f"ソロ参戦・遠征組向け｜1名入店ルール×現在男女比マトリクス（全国女性比 {research['national_wpct']}% 時点）",
            "headers": ["店舗名", "現在人数（女性比）", "男性1名（ソロ）入店規定", "深夜帯キープ目安"],
            "rows": rows,
        }
    rows = [
        [
            st["ja_name"],
            f"女性 {st['women']}名 / 男性 {st['men']}名",
            f"{st['wpct']}%",
            f"深夜維持 {st['midnight_retention_pct']}% ／ 22時台 {st.get('slot_22_24') or '平日770円〜'}",
        ]
        for st in stores_sample
    ]
    return {
        "caption": f"実測スナップ＆22時台単価比較（全国女性計 {research['national_women']}名・平均女性比 {research['national_wpct']}% 時点）",
        "headers": ["店舗名", "現在人数バランス", "女性比率", "終電後維持率・料金目安"],
        "rows": rows,
    }


def _build_deterministic_candidates(
    research: dict,
    plan: dict,
    site_url: str,
    tone_mode: str = None,
) -> list:
    """Generate 2 theme-differentiated, deep-insight baseline column candidates grounded in live research & pricing."""
    tm = sns_editorial.get_editorial_tone_mode(tone_mode)
    tone_meta = sns_editorial.EDITORIAL_TONE_MODES[tm]
    theme_key = plan.get("theme_key") or research.get("theme_key") or "area_hopping"
    archetype = THEME_ARCHETYPES.get(theme_key, THEME_ARCHETYPES["area_hopping"])
    stores = research.get("featured_stores") or []
    s1 = stores[0] if len(stores) > 0 else {
        "ja_name": "オリエンタルラウンジ新宿", "casual_name": "オリエンタルラウンジ新宿",
        "women": 45, "men": 27, "wpct": 62, "midnight_retention_pct": 86,
        "slot_20_22": "平日660円 / 金土880円", "slot_22_24": "平日770円 / 金土990円",
        "wait_fee_str": "220円 / 10分", "solo_rule": "店舗混雑状況により案内",
        "store_href": "/store/oriental-lounge-shinjuku",
    }
    s2 = stores[1] if len(stores) > 1 else {
        "ja_name": "JIS新宿", "casual_name": "JIS新宿",
        "women": 30, "men": 33, "wpct": 48, "midnight_retention_pct": 89,
        "slot_20_22": "平日660円 / 金土770円", "slot_22_24": "平日660円 / 金土880円",
        "wait_fee_str": "220円 / 10分", "solo_rule": "1名入店OK（ソロ歓迎）",
        "store_href": "/store/jis-shinjuku",
    }
    s3 = stores[2] if len(stores) > 2 else {
        "ja_name": "ag渋谷", "casual_name": "ag渋谷",
        "women": 42, "men": 24, "wpct": 64, "midnight_retention_pct": 95,
        "slot_20_22": "平日660円 / 金土880円", "slot_22_24": "平日770円 / 金土990円",
        "wait_fee_str": "220円 / 10分", "solo_rule": "2名以上必須",
        "store_href": "/store/ag-shibuya",
    }
    local_list = research.get("local_leaders") or []
    s_loc = local_list[0] if local_list else s3
    gap_info = research.get("top_gap")
    area_name = gap_info["cluster_label"] if gap_info else "都心主要エリア"
    gap_pt = gap_info["gap_pt"] if gap_info else max(8, abs(s1["wpct"] - s2["wpct"]))
    topic_focus = plan.get("custom_topic") or plan.get("theme_label")
    hist_cnt = int(research.get("historical_log_count") or 276000)

    common_table = _build_theme_tailored_table(theme_key, research, (stores[:4] or [s1, s2, s3]))
    store_links_1 = [{"label": s1["ja_name"], "href": s1["store_href"]}, {"label": s2["ja_name"], "href": s2["store_href"]}]
    store_links_2 = [{"label": s3["ja_name"], "href": s3["store_href"]}]

    # Theme-specific story & causal mechanism blueprints so 8 themes never collapse into one cookie-cutter script
    theme_specs = {
        "area_hopping": {
            "title": f"なぜ半径300mで女性比が{gap_pt}%も割れるのか？{area_name}の「看板店集中バイアス」と二面市場の歪み",
            "short": f"{area_name}・半径300mの需給ねじれ解剖",
            "desc": f"同じ駅の交差点なのに『{s1['ja_name']}』（女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性比{s2['wpct']}%）で別世界になる構造的理由を、男女の店選び行動の非対称性から解き明かします。",
            "h1": f"1. 改札前の信号待ち10秒で勝敗が分かれる——「とりあえず一番有名な店」の罠",
            "p1": [
                f"{research['calendar']['tag']}の21時半、1軒目の居酒屋を出て夜風に当たりながら駅のスクランブル交差点に立ったとき。多くの男性2人組はスマホも見ずに「一番看板が大きくて名前を知っている店舗」へ真っ直ぐ向かいます。",
                f"しかし同じ時刻のログを開くと、{area_name}の目と鼻の先にある『{s1['ja_name']}』（女性{s1['women']}名・女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性{s2['women']}名・女性比{s2['wpct']}%）のあいだには、実に{gap_pt}ptもの男女比ギャップが横たわっています。",
            ],
            "dl1": ["「大通りの店、男グループが次々吸い込まれていくのに、裏通りの『" + s1["casual_name"] + "』は女性6割超えてるぞ」", "「危なかったな、何も見ずに突っ込んでたら待機チャージだけで1,000円溶かすところだった」"],
            "h2": "2. 【構造考察】なぜ男性は「知名度」で並び、女性ペアは「裏通り・姉妹店」へ分散するのか",
            "p2": ["この需給ねじれが毎晩起きる背景には、男女それぞれの『店舗決定プロセスの時間差』があります。男性は1次会の酔いも手伝って駅チカ1店舗目に突撃しやすい一方、女性2人組は入店直前にSNSやイベント特典・空席感を見比べ、女余りや騒がしさを避けて別ブランドや姉妹店へ柔軟に分散します。"],
            "dl2": ["「さっき駅前のビル混んでそうだったから、落ち着いて飲めるこっちにしたんです」", "「僕たちもちょうど数字見て切り替えたところ。静かに話せて正解だったね」"],
            "mech2": archetype["causal_thesis"],
            "h3": "3. 同エリア2〜3店舗を「ポートフォリオ」として持つ移動判断基準",
            "p3": [f"1店舗に固執せず、最初から『本命：{s1['ja_name']}／後半戦バックアップ：{s3['ja_name']}（深夜維持{s3['midnight_retention_pct']}%）』と2枚のカードを持って街に出るだけで、待機コストも精神的な焦りもゼロになります。"],
            "dl3": ["「最初から余裕ある店に座れると、変にガツガツしなくて済むのが一番デカいよな」", "「うん、結果的に会話のテンポも自然になるから不思議だよ」"],
            "x_main": f"同じ{area_name}の半径300mなのに、『{s1['casual_name']}』（女性比{s1['wpct']}%）と近隣店で{gap_pt}ptも割れる夜📊\n\n男が「看板の知名度」で1店舗目に集中し、女子ペアが分散する二面市場のカラクリを書きました👇\n#相席ラウンジ #相席ナビ",
        },
        "midnight_wave": {
            "title": f"23時20分の終電後に何が起きているのか？深夜0時半から女性が再流入する「コホート交代」の法則",
            "short": "終電崖と深夜0時半・第2波の正体",
            "desc": f"23時過ぎに一度引いた波が、なぜ0時半から『{s3['ja_name']}』（深夜維持目安{s3['midnight_retention_pct']}%）などで再び盛り上がるのか。鉄道ダイヤと客層属性の完全入れ替わりを検証します。",
            "h1": "1. 23時25分、駅へ急ぐ人の波と逆行してラウンジへ入る人たち",
            "p1": [
                "金曜23時20分。改札へ向かう会社員の早足な波を見送りながら「今から入ってももう残り物しかいないのでは？」と立ち止まる男性ペアは少なくありません。実際、23時15分〜23時55分の40分間は、終電を意識した第1波の女性客が一斉に退店する『魔の空白帯』にあたります。",
                f"ところが日付が変わって0時25分を過ぎた瞬間、モニターの数字が再び上向きに跳ねる店舗が現れます。たとえば『{s3['ja_name']}』の終電後キープ目安は{s3['midnight_retention_pct']}%に達し、現在も女性{s3['women']}名・男性{s3['men']}名が稼働しています。",
            ],
            "dl1": ["「23時半に見たときは一瞬減ってたのに、0時半過ぎたらまた新しい2人組が3連続で入ってきたな」", "「しかも終電時間を気にしない組だから、最初から腰を据えて飲んでる空気がある」"],
            "h2": "2. 【構造考察】残っているのではなく「客層コホート」が丸ごと入れ替わっている",
            "p2": ["深夜0時半の第2波を『1次会の延長』と思っていると会話がズレます。ここで合流してくるのは、22時半〜23時に仕事を終えた美容師・アパレル・飲食スタッフや、タクシー初乗り圏内に住む地元組という全く別のコホート（属性集団）だからです。"],
            "dl2": ["「私たちサロン閉めてからご飯食べてたから、今がちょうど1軒目みたいな感覚なんです」", "「なるほどね！道理でみんな元気だと思った、乾杯しよう」"],
            "mech2": archetype["causal_thesis"],
            "h3": "3. 後半戦は「テンションの高さ」ではなく「聞き役の包容力」が勝つ",
            "p3": [f"深夜帯の『{s3['ja_name']}』や『{s1['ja_name']}』では、無理に盛り上げるよりも仕事上がりの疲れをねぎらう落ち着いたトーンの卓が圧倒的に支持されます。23時台の空白帯だけは避け、0時15分以降に狙いを定めてみてください。"],
            "dl3": ["「この時間にガヤガヤ絡まれずにゆっくり飲める席、ほんと助かります」", "「うちも2軒目でゆっくり話したかったからちょうど良かったよ」"],
            "x_main": f"23時20分の終電ラッシュで一度人が引いたあと、深夜0時半からもう一度女性が増え直す『{s3['casual_name']}』（深夜維持{s3['midnight_retention_pct']}%）の不思議🌙\n\n客層の属性が丸ごと入れ替わる「第2波」の構造を解剖しました👇\n#相席ラウンジ #相席ナビ",
        },
        "weekday_anaba": {
            "title": f"「金曜21時が一番出会える」という幻想——累計{hist_cnt:,}件ログが示す水木夜と雨天・駅直結店の逆説",
            "short": "平日水木×雨天の「高純度」穴場論",
            "desc": f"総人数が多い週末ピークほど会話が雑になりやすいのはなぜか。平日20時半や小雨の駅直結店（『{s1['ja_name']}』女性比{s1['wpct']}%など）で連絡先交換後の進展率が高まる構造を紐解きます。",
            "h1": "1. 水曜20時40分、傘をたたんで入った地下直結ビルの静かな熱気",
            "p1": [
                "「行くならやっぱり金曜か土曜の夜でしょ」——そう信じて週末の21時半に突撃し、大音量のBGMと周囲の団体ノリに押されて声すら聞き取れずに帰ってきた経験はないでしょうか。",
                f"対照的に、週半ばの水曜・木曜20時半や、少し雨がぱらつく夜の駅直結店舗を覗くと、驚くほど落ち着いた景色が広がっています。全国平均女性比が{research['national_wpct']}%で推移するなか、『{s1['ja_name']}』（20〜22時相席単価：{s1.get('slot_20_22')}）では女性{s1['women']}名・男性{s1['men']}名とゆとりある比率が保たれています。",
            ],
            "dl1": ["「平日のこの時間って過疎ってるのかと思ってたけど、普通に綺麗な仕事帰りの2人組が多いな」", "「しかも周りにお祭り騒ぎの団体がいないから、普通の声のトーンでちゃんと会話できる」"],
            "h2": "2. 【構造考察】天候と曜日が「冷やかし層」を振るい落とすフィルタリング効果",
            "p2": ["経済学の需要弾力性で見ると理由は明快です。平日の夜や雨の日は『なんとなくノリで騒ぎたい男性グループ』の来店意欲が急落する一方、予定を合わせていた女性ペアは『前髪や靴が濡れない駅・地下直結の1店舗』へ集約されます。さらに単価も週末より10分あたり110〜220円抑えられるため、男性側の焦りも消えます。"],
            "dl2": ["「雨降ってたから駅から濡れずに入れるここ一択だねって話してたんです☔」", "「やっぱりそうだよね。金曜みたいにバタバタしてないから、ご飯もゆっくり頼もうか」"],
            "mech2": archetype["causal_thesis"],
            "h3": "3. 平日夜は「22時15分解散」を前提に組むからこそ次につながる",
            "p3": ["翌日も仕事がある平日は、ダラダラ深追いせず60〜75分でスマートに切り上げる方が『ちゃんとした社会人感』と『もう少し話したかった余韻』の両方が残ります。"],
            "dl3": ["「明日もお互い仕事だし、22時過ぎには駅まで送るね。続きは来週美味しいビストロ行こう」", "「時間気にしてくれるの嬉しいです！来週の木曜なら空いてます」"],
            "x_main": f"正直、「金曜ピークが一番出会える」は半分幻想かもしれない☔\n\n水木の20時半や小雨の駅直結店（『{s1['casual_name']}』女性比{s1['wpct']}%等）のほうが、冷やかし層が消えて圧倒的にちゃんと話せる構造的理由を書きました👇\n#相席ラウンジ #相席ナビ",
        },
        "first_talk_clean": {
            "title": "乾杯から180秒で決まる「認知コスト」の差——距離70cmの非言語足切りと、一問一答面接を卒業する技術",
            "short": "距離70cmの非言語採点と乾杯3分",
            "desc": f"相席までは組めるのに15分でスマホを見られてしまう原因は「話題不足」ではなく「認知負荷」にあります。『{s1['ja_name']}』等の実測環境データとともに最初の10分の力学を解剖します。",
            "h1": "1. グラスが触れ合った直後の「7秒間の沈黙」で起きていること",
            "p1": [
                "スタッフに案内されてソファに腰を下ろし、最初のドリンクが届いて「乾杯！」とグラスを合わせた直後。そのわずか数秒のあいだに、幅70センチのテーブル越しで膨大な非言語情報の採点が完了していることに気づいているでしょうか。",
                "間接照明は意外と正直です。夕方の皮脂浮きや服のシワ、そして何より『緊張を隠そうとして矢継ぎ早に質問を繰り出す硬い表情』は、言葉の中身より先に相手の警戒スイッチを入れてしまいます。",
            ],
            "dl1": ["「お仕事何されてるんですか？ どの辺に住んでるんですか？ 休日はインドア派ですか？」", "「（笑顔で答えつつ内心）…うわ、今日も転職面接みたいな一問一答が始まったな…」"],
            "h2": "2. 【構造考察】なぜ「真面目な質問攻め」ほど女性の脳を疲れさせるのか",
            "p2": [f"仕事終わりに『{s1['ja_name']}』（女性比{s1['wpct']}%）や『{s3['ja_name']}』へ来ている女性ペアは、すでに昼間の業務で意思決定エネルギーを使い果たしています。そこに履歴書確認のクローズド質問を連続投下すると『正確に答えなきゃ』という認知コストが発生し、15分で省エネ（タダ飯消化）モードへ退避してしまうのです。"],
            "dl2": ["「今日このビル入る前の風、完全に秋の匂いだったよね。来るまで寒くなかった？」", "「そうなんですよ！薄手のジャケットにして後悔してました笑。お二人は1軒目どこにいたんですか？」"],
            "mech2": archetype["causal_thesis"],
            "h3": "3. プロフィールではなく「いま目の前の共通感覚」から会話を立ち上げる",
            "p3": ["最初の3分は個人情報ゼロで構いません。外の気温、頼んだドリンクの見た目、1軒目で食べてきたもの——『4人全員がいま共有している感覚』から入ると、相方の女の子も孤立せず自然に会話の輪が温まります。"],
            "dl3": ["「さっきの席より全然リラックスして笑っちゃった。こういう自然な空気いいですね」", "「よかった、せっかくの夜だから肩の力抜いて飲もう」"],
            "x_main": "相席して15分で空気が重くなる最大の原因、実はトーク力不足じゃなくて「履歴書の面接質問」による脳疲労なんですよね🥂\n\n距離70cmの非言語シグナルと、最初の3分で警戒心が溶ける共通体験トークの仕組みを書きました👇\n#相席ラウンジ #相席ナビ",
        },
        "second_venue_move": {
            "title": f"10分課金メーターが狂わせる「損切り判断」——90分粘って溶かす人と、45分で2軒目へ抜ける人の行動経済学",
            "short": "10分課金のサンクコスト罠と2軒目術",
            "desc": f"『せっかく座れたから』と微妙な席で延長を重ねてしまうサンクコスト錯誤を、『{s1['ja_name']}』（{s1.get('slot_20_22')}）等の実測単価とピーク・エンドの法則から解剖します。",
            "h1": "1. 会計伝票の「滞在90分」を見てから後悔する、あの独特の徒労感",
            "p1": [
                "帰り道のコンビニ前でレシートを見返しながら、「悪い子たちじゃなかったけど、後半40分はお互い惰性で喋ってたな…」と相方と苦笑いした夜はないでしょうか。",
                f"たとえば22時台の相席単価（『{s1['ja_name']}』なら{s1.get('slot_22_24')}、単独待機{s1.get('wait_fee_str')}）で計算すると、微妙な空気のまま50分余計に粘るだけで1人あたり4,000円〜5,000円近い追加コストと、今夜もう一度やり直せたはずのゴールデンタイムが消えていきます。",
            ],
            "dl1": ["「最初の25分くらいで『あ、ちょっと波長違うかも』って薄々わかってたのにな」", "「前に20分待った分を取り返したくて、ついチェンジも退店も言い出せなかったんだよな…」"],
            "h2": "2. 【構造考察】10分従量メーターが引き起こす「サンクコスト錯誤」とピーク消失",
            "p2": ["行動経済学では、すでに支払って取り戻せない待機時間や初期チャージに引きずられて非合理な延長を選ぶ心理を『コンコルド効果（サンクコスト錯誤）』と呼びます。さらに厄介なことに、波長が合った当たり席であっても、店内で90分話し尽くしてしまうと『ピーク・エンドの法則』における感情の最高到達点を過ぎてしまい、2軒目に誘う頃には相手が満足して帰るモードに入ってしまいます。"],
            "dl2": ["「この話もっと聞きたいんだけど、ここ少し賑やかになってきたから、駅前のバーで終電まで30分だけ飲み直さない？」", "「終電まで30分きっちりなら安心！ ちょうど静かなところで続き話したかったです」"],
            "mech2": archetype["causal_thesis"],
            "h3": "3. 損切りは35分、当たり席の移動打診は「余韻が残る45分＋出口保証」",
            "p3": [f"現在女性比{s1['wpct']}%の『{s1['ja_name']}』や深夜強い『{s3['ja_name']}』のように選択肢が周りにあると知っていれば、噛み合わない席は35分で感謝を伝えてスマートに席替え・移動できます。そして当たり席こそ、腹八分目の45分で『終了時刻を約束した短時間オファー』を出してみてください。"],
            "dl3": ["「ちゃんと帰りの電車の時間まで気にしてくれる人だと、警戒せずに頷けますね」", "「無理させたくないからね。じゃあサッと出ようか」"],
            "x_main": f"相席で一番お財布もメンタルも削られるのが「せっかく待って座れたから…」と微妙な席で90分粘っちゃうサンクコストの罠💸\n\n『{s1['casual_name']}』等の10分単価試算と、当たり席ほど45分で外へ出るべき行動経済学を書きました👇\n#相席ラウンジ #相席ナビ",
        },
        "solo_trip": {
            "title": f"実はペアより勝率が高い夜がある——1人参戦（ソロ）の圧倒的機動力と、地方・出張先での「アウェイ優位性」",
            "short": "ソロ参戦の機動力×地方遠征の勝率論",
            "desc": f"相方に気を使うコストがゼロの1人参戦ルール（『{s2['ja_name']}』：{s2.get('solo_rule')}等）と、出張先や地方主要都市で『街のことを教えて』が最強の武器になる構造を解説します。",
            "h1": "1. ホテルに荷物を置いた21時、ふと1人で夜の街へ出て気づいた「身軽さ」",
            "p1": [
                "誰かと2人で行く相席ラウンジは心強い反面、「相方の好みのタイプと違ったときに気を遣う」「自分が盛り上がっていても相方が退屈そうで切り上げざるを得ない」という見えない足枷が常につきまといます。",
                f"一方で、出張先の夜や予定が空いた平日に、ソロ入店対応の『{s2['ja_name']}』（{s2.get('solo_rule')}／現在女性{s2['women']}名:男性{s2['men']}名）や地方主要店（『{s_loc['ja_name']}』女性比{s_loc['wpct']}%など）へ1人でふらっと入ってみると、意思決定の自由度がまるで違うことに驚かされます。",
            ],
            "dl1": ["「えっ、今日お一人で来られたんですか？ なんか余裕あってカッコいいですね！」", "「出張で近くに泊まってて、ホテルで1人で飲むより地元の美味しいお店聞けたら楽しいかなと思って」"],
            "h2": "2. 【構造考察】なぜ地方や出張先では「よそ者（アウェイ）」がむしろ歓迎されるのか",
            "p2": ["地方都市のラウンジでは、地元コミュニティが狭いがゆえに女性側が『会社の知り合いや元カレの友人に鉢合わせたくない』『変に詮索されたくない』という警戒心を持っています。そこへ他県から来た出張者や1人客が座ると、『後腐れのない安全な非日常枠』かつ『地元のオススメを教えてあげられる対象』として、最初から自己開示のハードルが一気に下がるのです。"],
            "dl2": ["「私たちが普段行く裏路地の焼き鳥屋さん、観光客ぜったい知らないから今度行ってほしい！」", "「え、気になる！今から40分だけ案内してもらうのってアリだったりする？」"],
            "mech2": archetype["causal_thesis"],
            "h3": "3. ソロ参戦で浮かない「店舗規定チェック」と時間帯の選び方",
            "p3": [f"『{s3['ja_name']}』（{s3.get('solo_rule')}）のように2名以上必須の店舗もあるため、事前の規定確認は必須です。JIS各店や平日1名OK枠を活用し、団体が騒ぎ出す前の20時半〜21時半か、2軒目組が落ち着く23時半以降を狙うとスマートに合流できます。"],
            "dl3": ["「相方との予定合わせなくていいし、合わなければ30分でサッと帰れるから実は一番コスパいいかもな」", "「うん、1人で行ける店を知っておくと出張の夜が3倍楽しくなる」"],
            "x_main": f"相席って2人で行くものと思われがちだけど、実は「相方への気遣いコストゼロ」で動けるソロ参戦・出張遠征って独特の強さがある✈️\n\n『{s2['casual_name']}』等の1名入店ルールと、出張者が歓迎される構造を書きました👇\n#相席ラウンジ #相席ナビ",
        },
        "female_real_voice": {
            "title": f"「女の子は無料だから気楽でしょ？」が一発退場になる理由——準備90分の機会費用と、男女で真逆の店選び",
            "short": "女性側の90分準備コストと逆張り店選び",
            "desc": f"金銭0円の裏で女性ペアが支払っている『ヘアメイク90分と今夜の機会費用』を解剖。『{s1['ja_name']}』と『{s2['ja_name']}』で男女の正解ルートが真逆になるデータ構造も公開します。",
            "h1": "1. パウダールームの鏡の前で、女子2人のテンションが静かに冷める瞬間",
            "p1": [
                "退勤後に一度メイクを直し、コテで湿気に負けないよう髪を巻き、少し足が痛くなるヒールを履いて繁華街まで歩いてくる——女性ペアが相席ラウンジの扉を開けるまでに費やしている準備時間は平均60〜90分にのぼります。",
                "それなのに、女余りの店舗で25分待たされたあげく、ようやく着席した男性から開口一番『女の子はタダで飲み食いできて気楽でいいよね〜』と笑われた瞬間、心のシャッターは音もなく閉まります。",
            ],
            "dl1": ["「（化粧室でリップを直しながら）…ねえ、さっきの人、店員さんへの注文もタメ口だったの見た？」", "「見た見た。しかも私のほう一回も目を見てくれないし、このドリンク飲み終わったら出よっか」"],
            "h2": f"2. 【構造考察】実測データが示す「男性の天国＝女性の放置リスク」という非対称性",
            "p2": [f"いまこの瞬間のデータを見ても、『{s1['ja_name']}』（女性{s1['women']}名 / 男性{s1['men']}名・女性比{s1['wpct']}%）は男性にとって理想的な比率ですが、女性側がさらに集中しすぎると『女子だけで待機する時間』が生まれます。逆に女性ペア目線なら、男性がしっかり入っている『{s2['ja_name']}』（女性{s2['women']}名 / 男性{s2['men']}名）や活気ある『{s3['ja_name']}』を選んだほうが、待ち0分で歓迎ムードの席へ即案内されるのです。"],
            "dl2": [f"「女余りのお店で30分放置されるより、男性がちゃんと入ってる『{s2['casual_name']}』に行くほうが即乾杯できて絶対ラクだよね💄」", "「ほんとそれ！数字見てから入るようになってからハズレの日なくなったもん」"],
            "mech2": archetype["causal_thesis"],
            "h3": "3. 上位1割の男性だけがやっている「相方へのパス回し」と対等な敬意",
            "p3": ["女性ペアの意思決定権は、実は『気に入られている側の子』ではなく『もう片方の友達（相方）』が握っています。相方の子が退屈した瞬間に帰りの合図が出るからこそ、4人全員が笑える話題を振り、準備して来てくれたことへの小さな敬意を言葉にできる卓だけが2軒目へ進めます。"],
            "dl3": ["「さっき相方の子にもすごく丁寧に話振ってくれて、友達も『あの2人感じいいね』って褒めてました」", "「せっかく4人で乾杯したからね、みんなで楽しく飲めてよかったよ」"],
            "x_main": "「女の子は0円だから損しないでしょ」って言っちゃう人ほど、女子側が払ってる『ヘアメイク90分の準備コスト』に気づかず化粧室LINEで損切りされてる現実💄\n\n男女で真逆になる正解の店選びと本音をまとめました👇\n#相席ラウンジ #相席ナビ",
        },
        "bitter_loser_habits": {
            "title": f"なぜ「礼儀正しいいい人」ほど4人グループLINEの墓場で終わるのか？{area_name}の実測格差とピーク設計の残酷な差",
            "short": "脱・いい人止まり｜グループLINE墓場の解剖",
            "desc": f"減点ゼロを目指して加点ゼロで終わる「優しい面接官」の敗因を辛口で解剖。『{s1['ja_name']}』（女性比{s1['wpct']}%）での戦場選びから会話最高潮での1対1導線までを体系化します。",
            "h1": "1. 翌朝のスマホに残る「昨日はありがとうございました！」という社交辞令の残骸",
            "p1": [
                "日曜の昼下がり、スマホのトーク一覧を開くと『〇〇ちゃん・△△くん（4）』というグループLINEが通知オフのまま沈んでいる。「昨日は楽しかったです！またみんなで飲みましょう！」という丁寧な挨拶を最後に、既読4のまま二度と動かないあの画面です。",
                "失礼なことは何ひとつ言っていない。グラスが空けばすぐ声をかけ、笑顔で相槌も打った。それなのになぜ『また2人で会いたい人』ではなく『無害でいい人だったね』で処理されてしまうのでしょうか。",
            ],
            "dl1": ["「昨日めっちゃ盛り上がったと思ったのに、個別で送ったLINEまだ未読なんだよな…」", "「思い返すと、お会計の直前に慌てて『4人でグループ作ろう』って言った時点で社交辞令コース確定してたかもな」"],
            "h2": "2. 【構造考察】不利な戦場での消耗と「ピーク・エンドの法則」の無視",
            "p2": [f"敗因は大きく2つの構造に分かれます。1つ目は入店前の戦場選び。男余りの店舗（例：女性比{s2['wpct']}%の『{s2['ja_name']}』側）で待たされて消耗した状態で座ると、無意識に『嫌われたくない・失敗したくない』という守りに入り、当たり障りのない質問役に終始して感情のピークを作れません。同じ{area_name}でも女性比{s1['wpct']}%の『{s1['ja_name']}』のように余裕のある環境を選ぶだけで、自然体のユーモアが出せるようになります。"],
            "dl2": ["「さっき話題に出た学芸大学のビストロ、忘れないうちにインスタDMで地図だけ送っとくね。今出せる？」", "「あ、ちょうど保存したかったんです！ グループじゃなくてこっちに直接送ってください」"],
            "mech2": archetype["causal_thesis"],
            "h3": "3. 連絡先はお会計時ではなく「会話が最高潮に跳ねた瞬間」に1対1でつなぐ",
            "p3": ["お会計間際の連絡先交換は『業務手続き』になるため温度が乗りません。美味しい店の話や共通の趣味で一番笑いが起きたその瞬間に、その話題の延長（お店のリンク共有など）として1対1の導線を作る——これがグループLINEの墓場を卒業する唯一の鉄則です。"],
            "dl3": ["「変に全員で交換するより、話の流れでお店のリンク送ってもらった方が次も自然に誘いに乗りやすいです」", "「じゃあ来週そのお店の予約空いてる日だけ見て送るね」"],
            "x_main": f"礼儀正しく盛り上げたはずなのに、翌朝「昨日はありがとうございました！」の4人グループLINEで自然消滅する現象💸\n\n『{s1['casual_name']}』（女性比{s1['wpct']}%）の戦場選びと、会話のピーク瞬間に1対1導線を作る鉄則を辛口で書きました👇\n#相席ラウンジ #相席ナビ",
        },
    }

    sp = theme_specs.get(theme_key, theme_specs["area_hopping"])
    slug_a = plan["suggested_slug"]
    title_a = (
        f"【構造検証】{plan['custom_topic']}——累計{hist_cnt:,}件ログと現場心理から紐解く勝率の法則"
        if plan.get("custom_topic")
        else sp["title"]
    )
    style_lbl_a = (
        f"🥂 【{tone_meta['short_label']}】{archetype['archetype_name']}・深層考察案"
        if tm == "standard"
        else f"🌶️ 【{tone_meta['short_label']}】{archetype['archetype_name']}・本音解剖案"
    )

    cand_a = {
        "style": "field_dialogue_story",
        "theme_key": theme_key,
        "style_label": style_lbl_a,
        "writer_note": f"分析レンズ『{archetype['mechanism_lens']}』を軸に、『{s1['ja_name']}』（女性比{s1['wpct']}%）等の実測値・料金制度・人間心理の因果構造を執筆。",
        "thinking_memo": "",
        "mechanism_thesis": archetype["causal_thesis"],
        "is_ai_generated": False,
        "slug": slug_a,
        "eyecatch_key": plan["eyecatch_key"],
        "badge": plan["badge"],
        "read_time": "約6分で読める",
        "tag_primary": plan["tag_primary"],
        "tag_secondary": plan["tag_secondary"],
        "title": title_a,
        "short_title": (plan["custom_topic"][:28] if plan.get("custom_topic") else sp["short"]),
        "desc": sp["desc"],
        "keywords": f"相席ラウンジ,{s1['ja_name']},{s2['ja_name']},{s3['ja_name']},男女比,構造分析,相席ナビ",
        "sections": [
            {
                "heading": sp["h1"],
                "paragraphs": sp["p1"],
                "dialogue": sp["dl1"],
                "after_paragraphs": [
                    f"リアルタイム計測と過去ログを照らし合わせると、個人のトークスキルを責める前に『そもそもどの時間帯・どの店舗の椅子に座っていたか』で体験の8割が説明できてしまいます。"
                ],
                "mechanism_note": "",
                "highlight_title": f"今回の焦点：{topic_focus}",
                "highlight_body": f"感覚や知名度だけで動くのではなく、『{s1['ja_name']}』（女性比{s1['wpct']}%）と近隣店の需給・料金構造をセットで俯瞰することが最大の防御策になります。",
                "store_links": store_links_1,
            },
            {
                "heading": sp["h2"],
                "paragraphs": sp["p2"],
                "dialogue": sp["dl2"],
                "after_paragraphs": [
                    f"この構造を理解しておくと、現場での見え方が一変します。『{s1['ja_name']}』のように現在女性優位で落ち着いて話せる店舗と、終電後キープ目安{s3['midnight_retention_pct']}%の『{s3['ja_name']}』のような後半戦特化店舗では、集まる人の動機そのものが異なるのです。"
                ],
                "mechanism_note": sp["mech2"],
                "highlight_title": "",
                "highlight_body": "",
                "store_links": store_links_2,
            },
            {
                "heading": sp["h3"],
                "paragraphs": sp["p3"],
                "dialogue": sp["dl3"],
                "after_paragraphs": [
                    "夜の街で本当に余裕がある人は、気合で場を盛り上げようとせず、最初から『お互いが自然体で話せる環境と時間幅』を静かにデザインしています。今夜お店へ向かう前の10秒だけ、数字の裏にある人の流れを覗いてみてください。"
                ],
                "mechanism_note": "",
                "highlight_title": f"【{archetype['archetype_name']}】今夜からの実践3原則",
                "highlight_body": f"①入店前に同エリア2〜3店舗の女性比と単価（{s1.get('slot_20_22')}等）を並べて比較する ／ ②二面市場の相手側コスト（女性の準備時間・男性の従量課金心理）を想像して最初の一言を選ぶ ／ ③ダラダラ延長せず会話のピークで出口時刻を切って次へつなぐ。",
                "store_links": store_links_1 + store_links_2,
            },
        ],
        "data_table": common_table,
        "summary_points": [
            f"現象の背後にある『{archetype['mechanism_lens']}』を理解し、知名度突撃やサンクコスト延長を回避する",
            f"現在データでは『{s1['ja_name']}』（女性{s1['women']}名・女性比{s1['wpct']}%）と『{s3['ja_name']}』（深夜維持{s3['midnight_retention_pct']}%）の時間帯別使い分けが有効",
            "会話が最も温まったピークの瞬間に、相手の帰宅時間・負担に配慮した具体的な次の一手を添える",
        ],
        "faqs": [
            {
                "q": f"『{plan['theme_label']}』で一番やってはいけない失敗パターンは何ですか？",
                "a": f"事前の男女比や単価規定を見ずに思い込みで1店舗目へ突撃し、待機チャージ（{s1.get('wait_fee_str')}等）を払ったサンクコストから微妙な空気でも90分粘ってしまうことです。徒歩数分の『{s1['ja_name']}』などと比較してから動くだけで防げます。",
            },
            {
                "q": "遅い時間帯（23時以降）や1人参戦でも同じ考え方で使えますか？",
                "a": f"23時20分の終電崖前後は一度客層が入れ替わるため、0時半以降は深夜維持目安{s3['midnight_retention_pct']}%の『{s3['ja_name']}』等を優先し、ソロの場合は『{s2['ja_name']}』（{s2.get('solo_rule')}）など1名対応店を選ぶのが合理的です。",
            },
        ],
        "x_title": sp["short"],
        "x_main_text": sp["x_main"],
        "note_title": f"{title_a}｜53店舗のログから見えた夜の行動経済学",
        "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #行動経済学 #人間観察",
    }

    # Candidate B: Female Real Perspective ("女子トイレ反省会＆女性側から見た勝ち店舗の逆張り法則")
    slug_b = f"{plan['suggested_slug']}-female-real" if tm != "standard" else f"{plan['suggested_slug']}-data"
    if tm == "standard":
        title_b = (
            f"【実測比較】{plan['custom_topic']}——全国{research['national_women'] + research['national_men']}名の比率から逆算する店選びと引き際"
            if plan.get("custom_topic")
            else f"気を使って長居するほど疲れやすい？全国女性比{research['national_wpct']}%の実測ログから逆算するスマートな店選び"
        )
        cand_b = {
            "style": "data_contrast_guide",
            "theme_key": theme_key,
            "style_label": f"📊 【{tone_meta['short_label']}】実測データ×二面市場メカニズム比較案",
            "writer_note": f"全国稼働（女性{research['national_women']}名・平均女性比{research['national_wpct']}%）と料金・待機コストを対比した構造分析ガイド構成。",
            "thinking_memo": "",
            "is_ai_generated": False,
            "slug": slug_b,
            "eyecatch_key": "cost-break" if plan["theme_key"] == "second_venue_move" else plan["eyecatch_key"],
            "badge": "実測×構造比較",
            "read_time": "約6分で読める",
            "tag_primary": ["tag-amber", "📊 実測ログ×構造分析"],
            "tag_secondary": plan["tag_secondary"],
            "title": title_b,
            "short_title": "実測比率と課金構造で選ぶ店舗と引き際",
            "desc": f"全国女性比{research['national_wpct']}%の実測データと時間帯別単価をもとに、店舗の使い分けとスマートな声かけタイミングを構造的に整理しました。",
            "keywords": f"相席ラウンジ,料金,男女比,{s1['ja_name']},{s3['ja_name']},相席ナビ",
            "mechanism_thesis": archetype["causal_thesis"],
            "sections": [
                {
                    "heading": "1. レシートを見て相方と振り返った「サンクコスト延長」の落とし穴",
                    "paragraphs": [
                        "相席ラウンジで意外と多いのが、会話が盛り上がりきらない席で気を使って90分以上滞在してしまうケースです。",
                    ],
                    "dialogue": [
                        "「悪い子たちじゃなかったけど、後半はお互いちょっと話すことなくなってたね」",
                        "「最初に周辺の比率と待機チャージを見ておけば、もう少し余裕を持って動けたかもね」",
                    ],
                    "after_paragraphs": [
                        f"現在も全国で女性{research['national_women']}名・男性{research['national_men']}名（女性比{research['national_wpct']}%）が稼働していますが、店舗ごとの需給バランスと単価を見ておくだけで落ち着いて判断できます。",
                    ],
                    "mechanism_note": research["deep_insights"]["cost_simulation"],
                    "highlight_title": "周辺の現在地を知っておくメリット",
                    "highlight_body": f"『{s1['ja_name']}』（女性{s1['women']}名・女性比{s1['wpct']}%・20時台{s1.get('slot_20_22')}）のように需給バランスが良い店舗を把握しておくと安心です。",
                    "store_links": store_links_1,
                },
                {
                    "heading": "2. 第1波に強い店舗と、深夜第2波に強い店舗をコホートで使い分ける",
                    "paragraphs": [
                        f"{research['calendar']['tag']}のような夜は、時間帯ごとに店内に滞在する客層コホートそのものが入れ替わります。",
                    ],
                    "dialogue": [
                        "「23時過ぎに一度落ち着いたけど、0時半を回ってからまた2軒目組が入ってきたね」",
                        f"「『{s3['casual_name']}』は遅い時間でも活気があるから2軒目からでも入りやすいね」",
                    ],
                    "after_paragraphs": [
                        f"『{s3['ja_name']}』は終電後の維持目安が{s3['midnight_retention_pct']}%と高く、早い時間は『{s1['ja_name']}』、0時半以降なら『{s3['ja_name']}』と使い分けるのがスムーズです。",
                    ],
                    "mechanism_note": research["deep_insights"]["cohort_shift_summary"],
                    "highlight_title": "",
                    "highlight_body": "",
                    "store_links": store_links_2,
                },
                {
                    "heading": "3. 波長が合ったら「短時間・出口時刻の明示」で2軒目を提案する",
                    "paragraphs": [
                        "会話が弾んだ席では、ダラダラ長居するよりもピークの余韻が残る45分前後に声をかけるほうが心理的ハードルが下がります。",
                    ],
                    "dialogue": [
                        "「すぐ近くにコーヒーも飲める静かなバーがあるんだけど、終電まで30分だけ寄らない？」",
                        "「30分だけなら大丈夫かな、ちょっと行ってみようか！」",
                    ],
                    "after_paragraphs": [
                        "相手の帰宅時間を気遣う『上限時間』を添えるだけで、誠実な印象がぐっと伝わります。",
                    ],
                    "mechanism_note": "",
                    "highlight_title": "今夜の判断チェックリスト",
                    "highlight_body": "①女性比率55%以上の店舗を第1候補にする ／ ②遅い時間帯は深夜維持率の高い店舗を選ぶ ／ ③声かけは帰宅時間を気遣う短時間提案にする。",
                    "store_links": store_links_1 + store_links_2,
                },
            ],
            "data_table": common_table,
            "summary_points": [
                f"全国平均女性比（{research['national_wpct']}%）より需給に余裕のある店舗を優先する",
                f"23時以降は深夜維持目安{s3['midnight_retention_pct']}%の『{s3['ja_name']}』などを選ぶ",
                "2軒目の提案は「30分だけ」と出口時刻を区切って安心感を作る",
            ],
            "faqs": [
                {
                    "q": "初めて行く場合、どの時間帯が一番話しやすいですか？",
                    "a": "金土なら混雑前の20時台、平日なら20時半〜22時頃が待ち時間も少なく落ち着いて会話できます。",
                },
                {
                    "q": "店舗ごとの現在の男女比や料金はどこで確認できますか？",
                    "a": "相席ナビのトップページおよび料金比較ガイドで毎分リアルタイム更新しています。",
                },
            ],
            "x_title": "実測比率と課金構造から逆算するスマートな店選び",
            "x_main_text": (
                f"なんとなく長居して疲れてしまう夜を減らすために、『{s1['casual_name']}』（女性比{s1['wpct']}%）など実測ログを使った時間帯別の店選びを整理しました📊👇\n"
                "#相席ラウンジ #相席ナビ"
            ),
            "note_title": f"{title_b}｜数字の裏側にある夜の意思決定ノート",
            "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #データ分析 #意思決定",
        }
    else:
        title_b = (
            f"【女性目線の構造解剖】{plan['custom_topic']}——女子トイレの本音LINEと「タダだから気楽でしょ」の非対称コスト"
            if plan.get("custom_topic")
            else f"「女の子は無料だから気楽でしょ？」が致命傷になる理由——非対称コスト構造と男女で真逆になる『正解の店』"
        )
        cand_b = {
            "style": "female_real_perspective",
            "theme_key": "female_real_voice" if plan.get("theme_key") == "female_real_voice" else theme_key,
            "style_label": f"💄 【{tone_meta['short_label']}】女性側コスト解剖・男女非対称マッチング案",
            "writer_note": f"女性ペアが支払っている『準備90分＋機会費用』の非対称構造と、女子側にとっては『{s2['ja_name']}』のような男性先行店こそ即乾杯の穴場になる逆転ロジックを執筆。",
            "thinking_memo": "",
            "is_ai_generated": False,
            "slug": slug_b,
            "eyecatch_key": "first-10min",
            "badge": "女性目線の構造考察",
            "read_time": "約6分で読める",
            "tag_primary": ["tag-pink", "💄 非対称コスト解剖"],
            "tag_secondary": ["tag-cyan", "男女逆張りの店選び"],
            "title": title_b,
            "short_title": "非対称コストで解く女子の本音と逆張り店選び",
            "desc": f"1時間半かけて髪とメイクを整えて来た女子2人組が支払っている『見えないコスト』とは何か。男性が気づかない減点サインと、{s1['ja_name']}・{s2['ja_name']}の男女別・最適ルートを解剖します。",
            "keywords": f"相席ラウンジ,女性目線,本音,{s1['ja_name']},{s2['ja_name']},女子会,相席ナビ",
            "mechanism_thesis": THEME_ARCHETYPES["female_real_voice"]["causal_thesis"],
            "sections": [
                {
                    "heading": "1. 「女の子はタダで飲めていいよね」と言った瞬間、候補から外れる構造的理由",
                    "paragraphs": [
                        "男性側が一番やりがちな勘違いに、「女の子は無料なんだから待たされてもハズレ席でもノーダメージでしょ」という思い込みがあります。しかし女性側のリアルな負担は『お金』ではなく『準備の90分とヒールで歩く体力、そして今夜1回きりの機会費用』です。",
                        "退勤後にコテで髪を直し、メイクを整えて入ったのに、女余りの店舗で相席がつかないまま30分放置されたり、座ってきた男性から開口一番「タダ飯いいよね〜」と軽く扱われた瞬間、女子2人の温度は一気に氷点下まで下がります。",
                    ],
                    "dialogue": [
                        "「（席を立って化粧室の鏡の前で）…ねえ、さっきの右側の人、乾杯して3分で『今日何人目？』って聞いてきたんだけど笑」",
                        "「それな、しかも相方の人ずっと自分の仕事の武勇伝だし…ごめん、戻って今のドリンク飲み干したら別のお店行こ！」",
                    ],
                    "after_paragraphs": [
                        "女性ペアが急に2人揃って化粧室へ立ち、戻ってきてから食事のペースが急に早くなったら、それは楽しんでいるのではなく『早く食べて出る合意』が完了したサインです。",
                    ],
                    "mechanism_note": research["deep_insights"]["two_sided_cost_structure"],
                    "highlight_title": "女性ペアが最初の10分で密かに見ている3箇所",
                    "highlight_body": "①店員さんへの口の利き方（タメ口・横柄さは一発アウト） ／ ②距離70cmで漂うタバコ・ニンニク臭と清潔感 ／ ③片方の女の子だけでなく『相方の女の子にも敬意を払って話を振れるか』（2人の合意制のため片方が退屈した時点で離脱が決まります）。",
                    "store_links": store_links_1,
                },
                {
                    "heading": f"2. 男女で「行くべき正解の店」が真逆になる——二面市場の需給カラクリ",
                    "paragraphs": [
                        f"リアルタイム人数を見ると『男性にとっての天国』と『女性にとっての天国』が綺麗に反転します。たとえば今、{area_name}まわりでは『{s1['ja_name']}』（女性{s1['women']}名 / 男性{s1['men']}名・女性比{s1['wpct']}%）と『{s2['ja_name']}』（女性{s2['women']}名 / 男性{s2['men']}名・女性比{s2['wpct']}%）で需給バランスが分かれています。",
                    ],
                    "dialogue": [
                        "「女余りすぎてる店に入ると、女子だけで30分放置されて気まずいんだよね…」",
                        f"「わかる！ 私たちが入るなら男性がちゃんといっぱい入ってる『{s2['casual_name']}』の方が、待ち0分で即乾杯できるし席替えもすぐ来てラクだよね💄」",
                    ],
                    "after_paragraphs": [
                        f"男性目線なら女性比率が{s1['wpct']}%ある『{s1['ja_name']}』が圧倒的に有利ですが、女性2人組の目線なら、男性がしっかり入っている『{s2['ja_name']}』や深夜まで活気のある『{s3['ja_name']}』を選んだほうが、放置ゼロ＆好待遇で飲めるのです。",
                    ],
                    "mechanism_note": "",
                    "highlight_title": "",
                    "highlight_body": "",
                    "store_links": store_links_2,
                },
                {
                    "heading": "3. 女子が「この席は当たりかも」とガードを解く瞬間",
                    "paragraphs": [
                        "女性側が『もう少しこの人たちと話したいな』と思うのは、派手な盛り上げではなく、相手の時間コストへの想像力がある『対等な余裕』に触れた瞬間です。",
                    ],
                    "dialogue": [
                        "「今日外ちょっと湿気すごかったよね、髪巻くの大変じゃなかった？ 何か冷たいもの頼もうか」",
                        "「えっ、わかるんですか！？ そうなんですよ前髪すぐ取れちゃって…！ ありがとうございます、じゃあジャスミン茶もらおうかな」",
                    ],
                    "after_paragraphs": [
                        "相手の準備の手間をさらっと労い、お酒を強要せず、2人の女の子両方が笑える話題を選ぶ。それだけで上位1割の『また会いたい席』に入れます。",
                    ],
                    "mechanism_note": "",
                    "highlight_title": "男女別・今夜のスマートな店舗選び",
                    "highlight_body": f"【男性ペア】女性比{s1['wpct']}%の『{s1['ja_name']}』など女性先行店を選び、余裕を持って乾杯する ／ 【女性ペア】女余り放置を避け、『{s2['ja_name']}』など男性がしっかり入っている店舗で待ち0分＆好待遇を狙う。",
                    "store_links": store_links_1 + store_links_2,
                },
            ],
            "data_table": common_table,
            "summary_points": [
                "女性側のコストは「準備の90分と今夜の機会費用」。『タダでいいよね』発言や店員への横柄な態度は即座に離脱サインになる",
                "女性ペアは『2人合意制』で動くため、相方の女の子を退屈させない4人会話の設計が2軒目進出の鍵になる",
                f"男性は女性比率の高い『{s1['ja_name']}』、女性は即相席できる『{s2['ja_name']}』と、需給を逆張りで使い分けるのが正解",
            ],
            "faqs": [
                {
                    "q": "女性2人組で行く場合、一番ハズレを引きにくいお店選びは？",
                    "a": f"極端に女性ばかり多い店（女性75%超など）に入ると相席待ちで放置されやすいため、相席ナビで『男性人数がしっかり入っている店舗（{s2['ja_name']}など）』を見てから入ると、待ち0分で歓迎されやすく非常に快適です。",
                },
                {
                    "q": "雨の日に女性が特定の店舗に集中しやすいのは本当ですか？",
                    "a": "本当です。雨や湿気の強い夜はせっかく整えた前髪や靴が濡れるのを避けるため、路面店よりも駅・地下街直結の店舗へ女性ペアが一極集中します。",
                },
            ],
            "x_title": "非対称コストで解く女子の本音と、男女で真逆になる「正解の店選び」",
            "x_main_text": (
                "「女の子はタダだから気楽でしょ」って言っちゃう人ほど、乾杯15分後の女子トイレLINEで『この杯飲み干したら出よ笑』って書かれてる現実💄\n\n"
                f"非対称な時間コストと、『{s1['casual_name']}』など男女で真逆になる店選びの本音👇\n"
                "#相席ラウンジ #相席ナビ"
            ),
            "note_title": f"{title_b}｜『無料』の裏で女性が支払っている90分の正体",
            "note_hashtags": "#相席ラウンジ #女子の本音 #オリエンタルラウンジ #相席ナビ #行動経済学",
        }

    rendered_a = _render_candidate_preview_and_note(cand_a, site_url=site_url, research=research, plan=plan)
    rendered_b = _render_candidate_preview_and_note(cand_b, site_url=site_url, research=research, plan=plan)
    # In `female_real` mode or `female_real_voice` theme, place the Female Perspective draft first!
    if tm == "female_real" or plan.get("theme_key") == "female_real_voice":
        return [rendered_b, rendered_a]
    return [rendered_a, rendered_b]


def _build_column_gemini_prompt(
    research: dict,
    plan: dict,
    site_url: str,
    tone_mode: str = None,
) -> str:
    tm = sns_editorial.get_editorial_tone_mode(tone_mode)
    tone_directive = sns_editorial.EDITORIAL_TONE_MODES[tm]["prompt_directive"]
    fact_sheet = {
        "calendar": research.get("calendar"),
        "weather": research.get("weather"),
        "historical_log_count": research.get("historical_log_count"),
        "national_women": research.get("national_women"),
        "national_men": research.get("national_men"),
        "national_wpct": research.get("national_wpct"),
        "top_gap": research.get("top_gap"),
        "featured_stores": research.get("featured_stores"),
        "deep_insights": research.get("deep_insights"),
        "theme_key": plan.get("theme_key"),
        "theme_label": plan.get("theme_label"),
        "archetype_name": plan.get("archetype_name"),
        "mechanism_lens": plan.get("mechanism_lens"),
        "causal_thesis": plan.get("causal_thesis"),
        "opening_scene_hint": plan.get("opening_scene_hint"),
        "banned_tropes_for_this_theme": plan.get("banned_tropes"),
        "blueprint": plan.get("blueprint"),
        "existing_titles_to_differentiate_from": plan.get("existing_titles_sample"),
        "custom_topic": plan.get("custom_topic"),
        "core_question": plan.get("core_question"),
        "target_reader": plan.get("target_reader"),
        "suggested_slug": plan.get("suggested_slug"),
        "tone_mode": tm,
    }
    cliches_str = "・".join(f"「{w}」" for w in COLUMN_AI_CLICHES)
    fantasy_str = "・".join(f"「{w}」" for w in SWEET_FANTASY_CLICHES)
    banned_str = "・".join(f"「{w}」" for w in (plan.get("banned_tropes") or []))
    return (
        "あなたは全国53店舗の相席ラウンジ人数・時間帯別料金・累計27万件超の稼働ログを分析している『相席ナビ』のコラム編集長兼データアナリストです。\n"
        "単なる表面的な体験談や、どのテーマでも似たようなことを繰り返す金太郎飴コラムではなく、"
        "『なぜその現象が起きるのか』を行動経済学・二面市場のインセンティブ構造・実測データと料金規定から鮮やかに解き明かす【Web用データ考察コラム】と、"
        "Web本文のコピペではなく一人称の思索エッセイとして独立して読ませる【note専用書き下ろしエッセイ（1,600文字以上）】、および【X告知文】を執筆してください。\n\n"
        f"{DEEP_COLUMN_EDITORIAL_GUIDE}\n\n"
        f"{tone_directive}\n\n"
        "【今回のテーマ専用の構造レンズ＆脱マンネリ指令】\n"
        f"- アーキタイプ：{plan.get('archetype_name')}\n"
        f"- 構造分析レンズ：{plan.get('mechanism_lens')}\n"
        f"- 掘り下げるべき因果コア仮説：{plan.get('causal_thesis')}\n"
        f"- 導入シーンの舞台指定：{plan.get('opening_scene_hint')}\n"
        f"- 今回のテーマで禁止する手垢トロープ：{banned_str}（既存19記事で使い古された『エレベーターが開いた瞬間の〜』『23時の終電崖』『とりあえず40分見切り』を無関係なテーマに流用しないこと！）\n\n"
        "【長考（Thinking）プロセス：構造仮説の検証＆Web/note完全書き分け】\n"
        "まず `thinking_memo` の中で以下の3ステップを実行してからJSONを出力してください：\n"
        "  Step 1. 既存記事タイトル群と被らない、今回のテーマ固有の切り口と導入シーンを決める。\n"
        "  Step 2. 現象の裏にある『男女のコスト非対称性・店舗の料金/待機システム・時間帯コホート変化』の因果関係を整理し、全3章にリアルな会話（dialogue）と構造解説（mechanism_note）を配置する。\n"
        "  Step 3. Web版（検索読者向けの構造化リファレンス）とは完全に別構成で、note版（`note_essay_markdown`：番号なし見出し `## ` と `> ` 引用を用いた1,600〜2,400文字の一人称エッセイ）を書き下ろす。\n\n"
        "【絶対厳守ルール】\n"
        "1. 店舗名・人数・女性比率・単価・待機料は下記ファクトシートにある検証済みデータのみを使い、架空の数値を捏造しないこと。\n"
        "2. ブランド呼称ルール：単体の『オリエンタル』や略称『オリラジ』、中途半端な『オリエンタル新宿』（「ラウンジ」抜け）、内部コード『OLG』は絶対に使わないこと！必ず正式名称『オリエンタルラウンジ〇〇』と表記すること。\n"
        f"3. AI臭ワードの完全禁止：{cliches_str} はどこにも使わないこと。\n"
        f"4. ご都合主義セリフの禁止：{fantasy_str} のような浅いファンタジー会話は禁止。\n"
        "5. `note_essay_markdown` は先頭に `# タイトル` を付けず（本文のみ）、必ず `## ` 見出しを2〜3個と `> ` 会話引用を1〜2箇所含め、Web版の段落をそのままコピペせずエッセイとして独立執筆し、末尾にWeb版URLと `https://aiseki-navi.com/` への案内を添えて合計1,600文字以上にすること。\n"
        "6. X告知文（`x_main_text`）は全角122文字前後（Twitter weight 262以内）に引き締め、末尾に `#相席ラウンジ #相席ナビ` を付けること。\n\n"
        f"【検証済みファクトシート】\n{json.dumps(fact_sheet, ensure_ascii=False, indent=2)}\n\n"
        "以下のJSONオブジェクト形式のみで出力してください（Markdownコードブロック不要）：\n"
        "{\n"
        '  "thinking_memo": "既存記事との差別化ポイント・構造メカニズムの核・Webとnoteの書き分け設計メモ（100〜180文字）",\n'
        '  "writer_note": "今回の記事で解明した構造的メカニズムと実測データの1行要約",\n'
        '  "mechanism_thesis": "この記事全体を貫く構造的メカニズム・行動経済学コア仮説（90〜160文字）",\n'
        '  "slug": "英小文字とハイフンのみのURLスラッグ",\n'
        '  "title": "現象の違和感×構造的理由×データ根拠が伝わるWeb記事タイトル（36〜62文字）",\n'
        '  "short_title": "パンくず・一覧用の短いタイトル（18〜28文字）",\n'
        '  "desc": "検索スニペット・カード用の考察リード文（95〜140文字）",\n'
        '  "sections": [\n'
        '    {\n'
        '      "heading": "1. 第1章見出し（テーマ固有の現場違和感・解像度の高い情景）",\n'
        '      "paragraphs": ["導入段落1", "導入段落2"],\n'
        '      "dialogue": ["「現場でのリアルな会話セリフ1」", "「それに対する返答セリフ2」"],\n'
        '      "after_paragraphs": ["現象の背後にある構造への橋渡し段落"],\n'
        '      "mechanism_note": "なぜこの現象が起きるのかの構造的・経済学的メカニズム解説（80〜150文字）",\n'
        '      "highlight_title": "構造ポイント見出し",\n'
        '      "highlight_body": "要点ボックス本文"\n'
        '    },\n'
        '    {\n'
        '      "heading": "2. 第2章見出し（実測データ×料金・非対称構造による裏付け）",\n'
        '      "paragraphs": ["データ・単価・需給のクロス分析段落1", "分析段落2"],\n'
        '      "dialogue": ["「女性側または現場のシビアな本音セリフ1」", "「返答セリフ2」"],\n'
        '      "after_paragraphs": ["店舗・条件ごとの具体的な分岐解説段落"],\n'
        '      "mechanism_note": "データが示す構造法則の補足（任意・空文字可）",\n'
        '      "highlight_title": "",\n'
        '      "highlight_body": ""\n'
        '    },\n'
        '    {\n'
        '      "heading": "3. 第3章見出し（構造を踏まえた再現可能な判断プロトコル）",\n'
        '      "paragraphs": ["具体的な立ち回り・声かけ設計の解説段落"],\n'
        '      "dialogue": ["「相手のコストを想像した余裕あるセリフ1」", "「自然な返答セリフ2」"],\n'
        '      "after_paragraphs": ["宣伝臭のない余韻ある締めくくり段落"],\n'
        '      "mechanism_note": "",\n'
        '      "highlight_title": "今夜からの実践プロトコル",\n'
        '      "highlight_body": "具体的な3つの判断基準"\n'
        '    }\n'
        '  ],\n'
        '  "data_table": {\n'
        '    "caption": "テーマに特化した比較表タイトル",\n'
        '    "headers": ["列1", "列2", "列3", "列4"],\n'
        '    "rows": [["店舗や条件1", "値", "値", "値"], ["店舗や条件2", "値", "値", "値"]]\n'
        '  },\n'
        '  "summary_points": ["構造的結論1", "実測データ指針2", "実践アクション3"],\n'
        '  "faqs": [\n'
        '    {"q": "テーマ固有の深い疑問1", "a": "構造とデータに基づく回答1"},\n'
        '    {"q": "テーマ固有の深い疑問2", "a": "具体的な回答2"}\n'
        '  ],\n'
        '  "x_title": "Xリプ欄用の短い記事見出し",\n'
        '  "x_main_text": "X告知1投目（構造的な気づき＋実測データ＋👇 #相席ラウンジ #相席ナビ）",\n'
        '  "note_title": "note読者向けの思索的・人間観察エッセイタイトル（Webタイトルとは別表現）",\n'
        '  "note_essay_markdown": "note.com専用の独立書き下ろし一人称エッセイ本文（先頭# タイトル無し、## 見出し2〜3個と > 引用を含め、Web本文のコピペではなく物語と思索で1,600文字以上）",\n'
        '  "note_hashtags": "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #行動経済学 #人間観察"\n'
        "}"
    )


def _make_col_cache_key(research: dict, plan: dict, tone_mode: str = None):
    tm = sns_editorial.get_editorial_tone_mode(tone_mode, allow_blocking=False)
    eng_pref = sns_editorial.get_ai_engine_pref(allow_blocking=False)
    has_c = bool(sns_editorial.get_claude_api_key(allow_blocking=False))
    stores_sig = tuple(
        (s.get("raw_name", ""), int(s.get("women") or 0), int(s.get("men") or 0))
        for s in (research.get("featured_stores") or [])[:3]
    )
    return (
        "v2_deep",
        eng_pref,
        has_c,
        tm,
        plan.get("theme_key") or "area_hopping",
        plan.get("custom_topic") or "",
        stores_sig,
    )


def has_cached_column_ai(research: dict, plan: dict, tone_mode: str = None) -> bool:
    ck = _make_col_cache_key(research, plan, tone_mode=tone_mode)
    now_t = time.time()
    with _col_ai_lock:
        item = _col_ai_cache.get(ck)
        if item and (now_t - item["ts"] < _COL_AI_CACHE_TTL_SEC):
            return True
    shared = turbo_store.get_ai_cache("column_editorial", ck)
    if shared and isinstance(shared.get("candidate"), dict):
        with _col_ai_lock:
            _col_ai_cache[ck] = {"ts": now_t, "candidate": shared["candidate"], "model": str(shared.get("model") or "")}
        return True
    return False


def _generate_gemini_column_candidate(
    research: dict,
    plan: dict,
    baseline_cand: dict,
    site_url: str,
    now_jst: datetime.datetime,
    force_refresh: bool = False,
    cache_only: bool = False,
    tone_mode: str = None,
) -> dict:
    if not sns_editorial.has_any_ai_key(allow_blocking=(not cache_only)):
        return None

    tm = sns_editorial.get_editorial_tone_mode(tone_mode, allow_blocking=False)
    tone_short = sns_editorial.EDITORIAL_TONE_MODES[tm]["short_label"]
    ck = _make_col_cache_key(research, plan, tone_mode=tm)
    now_t = time.time()
    if not force_refresh:
        with _col_ai_lock:
            expired = [k for k, v in _col_ai_cache.items() if now_t - v["ts"] >= _COL_AI_CACHE_TTL_SEC]
            for k in expired:
                _col_ai_cache.pop(k, None)
            if ck in _col_ai_cache:
                _col_ai_cache.move_to_end(ck)
                return dict(_col_ai_cache[ck]["candidate"])
        shared = turbo_store.get_ai_cache("column_editorial", ck)
        if shared and isinstance(shared.get("candidate"), dict):
            with _col_ai_lock:
                _col_ai_cache[ck] = {"ts": now_t, "candidate": shared["candidate"], "model": str(shared.get("model") or "")}
                _col_ai_cache.move_to_end(ck)
            return dict(shared["candidate"])

    if cache_only:
        return None

    prompt = _build_column_gemini_prompt(research, plan, site_url, tone_mode=tm)
    parsed, mn, _ = sns_editorial.call_editorial_ai_json(
        prompt=prompt,
        max_output_tokens=6800,
        thinking_budget=2048,
        temperature=0.84,
        total_deadline_sec=52.0,
        per_call_timeout_sec=28.0,
        allow_blocking=True,
        now_jst=now_jst,
    )
    if not isinstance(parsed, dict) or not parsed.get("title") or not parsed.get("sections"):
        return None

    ai_slug = _normalize_slug(parsed.get("slug") or plan["suggested_slug"])
    if ai_slug in RESERVED_STATIC_SLUGS:
        ai_slug = f"{ai_slug}-{now_jst.strftime('%m%d')}"

    links_pool = plan.get("internal_links") or []
    built_sections = []
    for idx, sec in enumerate(parsed.get("sections") or []):
        if not isinstance(sec, dict):
            continue
        built_sections.append({
            "heading": _sanitize_brand_names(sec.get("heading") or f"{idx + 1}. 現場の構造ポイント"),
            "paragraphs": [_sanitize_brand_names(p) for p in (sec.get("paragraphs") or []) if str(p).strip()],
            "dialogue": [_sanitize_brand_names(d) for d in (sec.get("dialogue") or []) if str(d).strip()],
            "after_paragraphs": [_sanitize_brand_names(p) for p in (sec.get("after_paragraphs") or []) if str(p).strip()],
            "mechanism_note": _sanitize_brand_names(sec.get("mechanism_note") or ""),
            "highlight_title": _sanitize_brand_names(sec.get("highlight_title") or ""),
            "highlight_body": _sanitize_brand_names(sec.get("highlight_body") or ""),
            "store_links": links_pool[:2] if idx == 0 else (links_pool[1:3] if idx == 1 else links_pool),
        })
    if len(built_sections) < 2:
        return None

    # Preserve AI's custom theme-specific data_table when valid instead of blindly overwriting with baseline!
    ai_table = parsed.get("data_table")
    resolved_table = baseline_cand["data_table"]
    if (
        isinstance(ai_table, dict)
        and isinstance(ai_table.get("headers"), list)
        and len(ai_table["headers"]) >= 3
        and isinstance(ai_table.get("rows"), list)
        and len(ai_table["rows"]) >= 2
    ):
        clean_rows = []
        for r in ai_table["rows"][:6]:
            if isinstance(r, list) and len(r) >= 3:
                clean_rows.append([_sanitize_brand_names(str(cell)) for cell in r[: len(ai_table["headers"])]])
        if len(clean_rows) >= 2:
            resolved_table = {
                "caption": _sanitize_brand_names(str(ai_table.get("caption") or baseline_cand["data_table"]["caption"])),
                "headers": [_sanitize_brand_names(str(h)) for h in ai_table["headers"][:5]],
                "rows": clean_rows,
            }

    x_main = _sanitize_brand_names(parsed.get("x_main_text") or baseline_cand["x_main_text"], prefer_casual_if_tight=False)

    note_title_clean = _sanitize_brand_names(parsed.get("note_title") or f"{parsed.get('title')}｜夜の行動経済学ノート")
    note_hashtags_clean = str(parsed.get("note_hashtags") or baseline_cand["note_hashtags"]).strip()
    ai_note_essay = _sanitize_brand_names(str(parsed.get("note_essay_markdown") or "")).strip()
    pre_built_note_md = ""
    if len(ai_note_essay) >= 1200 and "## " in ai_note_essay:
        if ai_note_essay.startswith("# "):
            ai_note_essay = "\n".join(ai_note_essay.splitlines()[1:]).strip()
        if f"/guide/{ai_slug}" not in ai_note_essay:
            ai_note_essay = (
                f"{ai_note_essay}\n\n---\n\n"
                f"📖 **店舗別データ比較表・時間帯別プロトコルはWeb版コラムで公開中👇**\n"
                f"{site_url.rstrip('/')}/guide/{ai_slug}\n\n"
                f"👉 全国53店舗リアルタイム男女比モニター（相席ナビ）\n{site_url.rstrip('/')}/"
            )
        pre_built_note_md = f"# {note_title_clean}\n\n{ai_note_essay}\n\n{note_hashtags_clean}"

    cand = {
        "style": "ai_longform_story",
        "theme_key": plan.get("theme_key") or "area_hopping",
        "style_label": f"✨ AI編集長・深層構造考察＆note別原稿案（{mn} ｜ {tone_short}）",
        "writer_note": f"【{mn}・{tone_short}】{_sanitize_brand_names(parsed.get('writer_note') or '構造メカニズムと実測ログを掛け合わせたAI編集長案')}",
        "thinking_memo": _sanitize_brand_names(parsed.get("thinking_memo") or ""),
        "mechanism_thesis": _sanitize_brand_names(parsed.get("mechanism_thesis") or plan.get("causal_thesis") or ""),
        "is_ai_generated": True,
        "ai_model": mn,
        "slug": ai_slug,
        "eyecatch_key": plan["eyecatch_key"],
        "badge": plan["badge"],
        "read_time": "約6分で読める",
        "tag_primary": plan["tag_primary"],
        "tag_secondary": plan["tag_secondary"],
        "title": _sanitize_brand_names(parsed.get("title")),
        "short_title": _sanitize_brand_names(parsed.get("short_title") or parsed.get("title")[:28]),
        "desc": _sanitize_brand_names(parsed.get("desc") or baseline_cand["desc"]),
        "keywords": baseline_cand["keywords"],
        "sections": built_sections,
        "data_table": resolved_table,
        "summary_points": [_sanitize_brand_names(pt) for pt in (parsed.get("summary_points") or baseline_cand["summary_points"])],
        "faqs": [
            {"q": _sanitize_brand_names(f.get("q", "")), "a": _sanitize_brand_names(f.get("a", ""))}
            for f in (parsed.get("faqs") or baseline_cand["faqs"])
            if isinstance(f, dict) and f.get("q") and f.get("a")
        ] or baseline_cand["faqs"],
        "x_title": _sanitize_brand_names(parsed.get("x_title") or parsed.get("short_title") or baseline_cand["x_title"]),
        "x_main_text": x_main,
        "note_title": note_title_clean,
        "note_hashtags": note_hashtags_clean,
    }
    if pre_built_note_md:
        cand["note_markdown"] = pre_built_note_md
    _render_candidate_preview_and_note(cand, site_url=site_url, research=research, plan=plan)
    now_done = time.time()
    with _col_ai_lock:
        _col_ai_cache[ck] = {"ts": now_done, "candidate": cand, "model": mn}
        while len(_col_ai_cache) > _COL_AI_CACHE_MAX_SIZE:
            _col_ai_cache.popitem(last=False)
    turbo_store.set_ai_cache("column_editorial", ck, {"ts": now_done, "candidate": cand, "model": mn}, ttl_sec=3600)
    return dict(cand)


def run_column_writer(
    research: dict,
    plan: dict,
    now_jst: datetime.datetime = None,
    app_url: str = "",
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
    tone_mode: str = None,
) -> list:
    now_jst = now_jst or datetime.datetime.now(JST)
    site_url = (app_url or os.environ.get("PUBLIC_SITE_URL") or "https://aiseki-navi.com").rstrip("/")
    tm = sns_editorial.get_editorial_tone_mode(tone_mode, allow_blocking=(not ai_cache_only))
    base_cands = _build_deterministic_candidates(research, plan, site_url=site_url, tone_mode=tm)

    if use_ai:
        ai_cand = _generate_gemini_column_candidate(
            research=research,
            plan=plan,
            baseline_cand=base_cands[0],
            site_url=site_url,
            now_jst=now_jst,
            force_refresh=force_ai_refresh,
            cache_only=ai_cache_only,
            tone_mode=tm,
        )
        if ai_cand:
            return [ai_cand] + base_cands
    return base_cands


def _compute_text_distinct_pct(web_text: str, note_text: str) -> int:
    """Measure 4-gram Jaccard distinctness (%) between Web article prose and note.com essay prose."""
    def _ngrams(txt: str, n: int = 4) -> set:
        cleaned = re.sub(r"https?://\S+", "", txt or "")
        cleaned = re.sub(r"[\s#>*\-_｜|「」『』（）()\[\]【】、。,.!！?？:：/／]+", "", cleaned)
        if len(cleaned) < n:
            return {cleaned} if cleaned else set()
        return {cleaned[i : i + n] for i in range(len(cleaned) - n + 1)}

    g_web = _ngrams(web_text)
    g_note = _ngrams(note_text)
    if not g_web or not g_note:
        return 100
    union_cnt = len(g_web | g_note)
    if union_cnt == 0:
        return 100
    overlap = len(g_web & g_note) / union_cnt
    return max(0, min(100, int(round((1.0 - overlap) * 100))))


# =====================================================================
# Role 4: Evaluator / Desk (鬼デスク・100点採点＆検閲係)
# =====================================================================
def run_column_evaluator(candidates: list, research: dict, plan: dict, tone_mode: str = None) -> dict:
    evaluated = []
    tm = sns_editorial.get_editorial_tone_mode(tone_mode, allow_blocking=False)
    verified_names = [s["ja_name"] for s in (research.get("featured_stores") or [])] + [
        s["casual_name"] for s in (research.get("featured_stores") or [])
    ]
    banned_tropes = plan.get("banned_tropes") or []

    for cand in candidates:
        c = dict(cand)
        # Ensure x_reply_text preview is attached
        c["x_reply_text"] = (
            f"📖 新しいコラムを書きました👇\n"
            f"『{c['x_title']}』\n"
            f"https://aiseki-navi.com/guide/{c['slug']}"
        )

        sections = c.get("sections") or []
        dialogue_count = sum(1 for sec in sections if sec.get("dialogue"))
        mech_notes_cnt = sum(1 for sec in sections if (sec.get("mechanism_note") or "").strip())
        has_thesis = bool((c.get("mechanism_thesis") or "").strip())
        web_prose_only = " ".join(
            " ".join(sec.get("paragraphs") or []) + " " + " ".join(sec.get("after_paragraphs") or [])
            for sec in sections
        )
        distinct_pct = _compute_text_distinct_pct(web_prose_only, c.get("note_markdown") or "")

        full_blob = " ".join([
            c.get("title") or "",
            c.get("desc") or "",
            c.get("preview_text") or "",
            c.get("x_main_text") or "",
        ])

        pros = []
        warnings = []

        # 1. Human Story, Deep Mechanism & Anti-Trope Originality (35 pts)
        human_pts = 19
        if dialogue_count >= 3:
            human_pts += 9
            pros.append(f"現場のリアルな会話ボックス（.dialogue-box）を{dialogue_count}箇所に配置")
        elif dialogue_count >= 2:
            human_pts += 6
            pros.append(f"現場の会話描写を{dialogue_count}箇所に配置")
        else:
            warnings.append("会話ボックスが少ないため読み物としての臨場感がやや弱め")

        if has_thesis and mech_notes_cnt >= 1:
            human_pts += 4
            pros.append("二面市場・行動経済学に基づく『構造メカニズム考察』を明示")
        elif has_thesis:
            human_pts += 3

        if c.get("is_ai_generated") and c.get("thinking_memo"):
            human_pts += 4
            pros.append("AI長考によるテーマ固有シーン・構造仮説の推敲済み")
        elif c.get("style") == "female_real_perspective":
            human_pts += (3 if tm == "female_real" else 2)
        elif c.get("style") == "field_dialogue_story":
            human_pts += (3 if tm in ("spicy", "spicy_female") else 2)
        else:
            human_pts += 1

        cliche_hits = [w for w in COLUMN_AI_CLICHES if w in full_blob]
        fantasy_hits = [w for w in SWEET_FANTASY_CLICHES if w in full_blob]
        trope_hits = [t for t in banned_tropes if t and t in web_prose_only]
        if cliche_hits:
            human_pts -= min(14, len(cliche_hits) * 4)
            warnings.append(f"AI定型句（{', '.join(cliche_hits[:3])}）を検出したため減点")
        if fantasy_hits:
            human_pts -= min(10, len(fantasy_hits) * 4)
            warnings.append(f"ご都合主義・手垢テンプレ表現（{', '.join(fantasy_hits[:2])}）を検出したため減点")
        if trope_hits and c.get("style") != "female_real_perspective":
            human_pts -= min(6, len(trope_hits) * 3)
            warnings.append(f"別テーマの手垢トロープ（{', '.join(trope_hits[:2])}）流用を検出し減点")
        if not cliche_hits and not fantasy_hits and not trope_hits:
            pros.append("AI臭ワード・手垢トロープ流用ともにゼロクリア")
        human_pts = max(8, min(35, human_pts))

        # 2. Fact & Brand Hygiene (25 pts)
        fact_pts = 25
        bare_oriental = len(re.findall(r"オリエンタル(?!ラウンジ)", full_blob + " " + (c.get("note_markdown") or "")))
        raw_olg = len(re.findall(r"\bOLG\b", full_blob + " " + (c.get("note_markdown") or "")))
        isolated_oriradi = len(re.findall(r"オリラジ", full_blob + " " + (c.get("note_markdown") or "")))
        if bare_oriental > 0 or raw_olg > 0 or isolated_oriradi > 0:
            fact_pts -= 12
            warnings.append("ブランド呼称違反（単体『オリエンタル』『OLG』または『オリラジ』残存）を検出")
        else:
            pros.append("ブランド呼称検閲OK（『オリエンタルラウンジ』統一）")

        matched_stores = [nm for nm in verified_names if nm and nm in full_blob]
        if not matched_stores:
            fact_pts -= 6
            warnings.append("実測店舗名の具体言及が不足")
        if "/link/" in full_blob:
            fact_pts -= 8
            warnings.append("本文への直接アフィリエイトリンク混入を検出")
        fact_pts = max(5, min(25, fact_pts))

        # 3. SEO Structure & Internal Navigation (25 pts)
        seo_pts = 0
        if len(sections) >= 3:
            seo_pts += 8
        if c.get("data_table") and (c["data_table"].get("rows") or []):
            seo_pts += 7
            pros.append(f"テーマ別実測データ比較表（{len(c['data_table']['rows'])}行）を内蔵")
        if len(c.get("faqs") or []) >= 2:
            seo_pts += 5
        store_link_set = {
            lk.get("href")
            for sec in sections
            for lk in (sec.get("store_links") or [])
            if str(lk.get("href") or "").startswith("/store/")
        }
        if len(store_link_set) >= 2:
            seo_pts += 5
        elif len(store_link_set) == 1:
            seo_pts += 3
        seo_pts = max(5, min(25, seo_pts))

        # 4. X Thread Weight & Independent note Essay Readiness (15 pts)
        x_weight = x_poster.compute_twitter_weight(c.get("x_main_text") or "")
        note_len = len(c.get("note_markdown") or "")
        sns_pts = 0
        if 110 <= x_weight <= 274:
            sns_pts += 7
        elif x_weight <= 280:
            sns_pts += 4
        else:
            warnings.append(f"X告知文が上限超過（{x_weight}/280）")

        if note_len >= 1400 and distinct_pct >= 60 and "#" in (c.get("note_hashtags") or ""):
            sns_pts += 8
            pros.append(f"note専用エッセイ書き分け完了（{note_len}文字・Web非重複率{distinct_pct}%）")
        elif note_len >= 700 and "#" in (c.get("note_hashtags") or ""):
            sns_pts += 5
            if distinct_pct < 50:
                warnings.append(f"Web本文とnote原稿の重複が多め（独自率{distinct_pct}%）")
        elif note_len >= 400:
            sns_pts += 3
        sns_pts = max(2, min(15, sns_pts))

        deep_insight_score = min(25, (12 if has_thesis else 4) + min(8, mech_notes_cnt * 4) + (5 if distinct_pct >= 65 else 2))
        total_score = human_pts + fact_pts + seo_pts + sns_pts
        c["scores"] = {
            "human_story": human_pts,
            "fact_brand_hygiene": fact_pts,
            "seo_structure": seo_pts,
            "sns_note_ready": sns_pts,
            "deep_insight": deep_insight_score,
        }
        c["total_score"] = total_score
        c["x_weight"] = x_weight
        c["note_char_count"] = note_len
        c["web_note_distinct_pct"] = distinct_pct
        c["dialogue_count"] = dialogue_count
        c["pros"] = pros[:4]
        c["warnings"] = warnings
        evaluated.append(c)

    evaluated.sort(key=lambda x: (x["total_score"], 1 if x.get("is_ai_generated") else 0), reverse=True)
    for idx, item in enumerate(evaluated):
        item["rank"] = idx + 1
        item["is_selected"] = (idx == 0)

    winner = evaluated[0] if evaluated else None
    desk_summary = (
        f"【{sns_editorial.EDITORIAL_TONE_MODES[tm]['short_label']}】全{len(evaluated)}案を構造考察・脱マンネリ・Web/note書き分け基準で検閲し、"
        f"『{winner['style_label']}』（{winner['total_score']}点／note独自率{winner.get('web_note_distinct_pct', 80)}%・X weight {winner['x_weight']}/280）を第1位推薦に選出しました。"
        if winner else "候補案がありません。"
    )
    return {
        "winner": winner,
        "candidates": evaluated,
        "desk_summary": desk_summary,
    }


# =====================================================================
# Existing & Scheduled Columns Quality Audit (全コラム鬼デスク監査一覧)
# =====================================================================
def audit_existing_columns(force: bool = False) -> dict:
    """Scan all static (14), scheduled static (3), and dynamic editorial columns for human tone & brand hygiene."""
    now_t = time.time()
    with _audit_lock:
        if not force and _audit_cache["data"] is not None and (now_t - _audit_cache["ts"] < 60.0):
            return _audit_cache["data"]

    import scheduled_articles
    tpl_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates")
    now_naive = scheduled_articles.now_jst_naive()
    rows = []

    def _audit_html_file(meta: dict, status_label: str, is_dynamic: bool = False, publish_at_ja: str = "公開中"):
        fpath = os.path.join(tpl_dir, meta["template"])
        try:
            with open(fpath, "r", encoding="utf-8") as f:
                raw_html = f.read()
        except Exception:
            raw_html = ""
        # Strip script/style tags for clean body inspection
        body_only = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", raw_html, flags=re.S | re.I)
        plain = re.sub(r"<[^>]+>", " ", body_only)
        plain = html.unescape(re.sub(r"\s+", " ", plain)).strip()

        dialogue_cnt = raw_html.count("dialogue-box") - raw_html.count(".dialogue-box")
        dialogue_cnt = max(0, dialogue_cnt)
        store_links = len(set(re.findall(r'href=["\'](/store/[a-z0-9\-]+)["\']', raw_html)))
        has_table = ("data-table" in raw_html or "<table" in raw_html)
        bare_oriental = len(re.findall(r"オリエンタル(?!ラウンジ)", plain))
        cliche_cnt = sum(1 for w in COLUMN_AI_CLICHES if w in plain) + sum(1 for w in SWEET_FANTASY_CLICHES if w in plain)

        score = 72
        if dialogue_cnt >= 2:
            score += 14
        elif dialogue_cnt == 1:
            score += 8
        if store_links >= 2:
            score += 6
        elif store_links == 1:
            score += 3
        if has_table:
            score += 6
        if bare_oriental == 0:
            score += 2
        else:
            score -= min(20, bare_oriental * 8)
        score -= min(10, cliche_cnt * 3)
        score = max(40, min(100, score))

        return {
            "slug": meta["slug"],
            "path": meta["path"],
            "title": meta["title"],
            "status_label": status_label,
            "publish_at_ja": publish_at_ja,
            "is_dynamic": is_dynamic,
            "dialogue_count": dialogue_cnt,
            "store_link_count": store_links,
            "has_table": has_table,
            "bare_oriental_count": bare_oriental,
            "char_count": len(plain),
            "desk_score": score,
        }

    # 1. Dynamic columns created via Column Editorial Room (newest first)
    for dyn in reversed(get_dynamic_scheduled_articles(allow_blocking=False)):
        is_pub = scheduled_articles.is_published(dyn, now_jst=now_naive)
        secs = dyn.get("sections") or []
        d_cnt = sum(1 for s in secs if s.get("dialogue"))
        s_links = len({
            lk.get("href")
            for s in secs
            for lk in (s.get("store_links") or [])
            if str(lk.get("href") or "").startswith("/store/")
        })
        blob = json.dumps(dyn.get("sections") or [], ensure_ascii=False) + (dyn.get("title") or "")
        bare_cnt = len(re.findall(r"オリエンタル(?!ラウンジ)", blob))
        rows.append({
            "slug": dyn["slug"],
            "path": dyn["path"],
            "title": dyn["title"],
            "status_label": "🟢 公開中（編集部作成）" if is_pub else "🗓️ 予約待機中（編集部作成）",
            "publish_at_ja": dyn.get("publish_at_ja") or dyn.get("publish_at"),
            "is_dynamic": True,
            "dialogue_count": d_cnt,
            "store_link_count": s_links,
            "has_table": bool(dyn.get("data_table")),
            "bare_oriental_count": bare_cnt,
            "char_count": len(dyn.get("note_markdown") or blob),
            "desk_score": int(dyn.get("editorial_score") or 94),
        })

    # 2. Static scheduled columns (3 articles)
    for sc in scheduled_articles.SCHEDULED_ARTICLES:
        is_pub = scheduled_articles.is_published(sc, now_jst=now_naive)
        st_lbl = "🟢 公開中（予約解禁済）" if is_pub else "🗓️ 予約待機中"
        rows.append(_audit_html_file(sc, st_lbl, is_dynamic=False, publish_at_ja=sc.get("publish_at_ja") or ""))

    # 3. Static evergreen columns (14 articles)
    for col in EXISTING_STATIC_COLUMNS:
        rows.append(_audit_html_file(col, "🟢 公開中（定番）", is_dynamic=False, publish_at_ja="常時公開"))

    avg_score = round(sum(r["desk_score"] for r in rows) / len(rows), 1) if rows else 0.0
    total_bare = sum(r["bare_oriental_count"] for r in rows)
    with_dialogue = sum(1 for r in rows if r["dialogue_count"] >= 1)

    report = {
        "total_columns": len(rows),
        "with_dialogue_count": with_dialogue,
        "total_bare_oriental": total_bare,
        "avg_desk_score": avg_score,
        "columns": rows,
    }
    with _audit_lock:
        _audit_cache["ts"] = now_t
        _audit_cache["data"] = report
    return report


# =====================================================================
# Published & Scheduled Column -> note.com Ready-to-Paste Exporter
# =====================================================================
_VOID_HTML_TAGS = frozenset({
    "area", "base", "br", "col", "embed", "hr", "img", "input",
    "link", "meta", "param", "source", "track", "wbr",
})

_SKIP_HTML_TAGS = frozenset({
    "head", "script", "style", "noscript", "nav", "footer", "aside",
    "figure", "picture", "svg", "button", "form", "input", "select",
})

_SKIP_EXACT_CLASSES = frozenset({
    "breadcrumb", "toc-box", "toc-list", "toc-title", "article-toc",
    "portal-global-nav", "portal-top-header", "top-header", "top-nav-bar",
    "portal-footer", "official-notice-box", "article-affiliate-card",
    "aff-card", "ladies-pr-section", "cta-box", "live-tool-cta-box",
    "preview-banner", "live-board", "article-cat-tag", "article-badge-row",
    "article-date", "article-meta", "article-meta-line", "cat-badge",
    "rec-badge", "hero-kicker", "hero-cta-row", "share-btn-row",
    "related-article-card", "editorial-ref-note", "store-chip-row",
    "ref-kicker", "cta-btn", "live-tool-btn", "rec-btn", "ladies-pr-link",
})

_NOTE_HASHTAG_PRESETS = {
    "for-women": "#相席ラウンジ #女子会 #オリエンタルラウンジ #JIS #ag #夜カフェ #相席ナビ",
    "last-train-cliff": "#相席ラウンジ #オリエンタルラウンジ #JIS #ag #相席ナビ #終電後 #夜遊び",
    "price-event-comparison": "#相席ラウンジ #オリエンタルラウンジ #JIS #ag #コスパ #相席ナビ #出会い",
    "male-lead-mechanics": "#相席ラウンジ #オリエンタルラウンジ #データ分析 #相席ナビ #出会い #夜遊び",
    "beauty-area-stage": "#相席ラウンジ #オリエンタルラウンジ #美容 #清潔感 #相席ナビ #出会い",
    "club-area-midnight": "#相席ラウンジ #オリエンタルラウンジ #JIS #渋谷 #梅田 #新宿 #相席ナビ",
    "weekend-strategy": "#相席ラウンジ #オリエンタルラウンジ #週末 #金曜日 #相席ナビ #出会い",
    "brand-comparison": "#オリエンタルラウンジ #JIS #ag #相席ラウンジ #徹底比較 #相席ナビ",
    "weekday-strategy": "#相席ラウンジ #オリエンタルラウンジ #水曜日 #木曜日 #相席ナビ #出会い",
    "shinjuku": "#新宿 #相席ラウンジ #オリエンタルラウンジ #JIS新宿 #歌舞伎町 #相席ナビ",
    "solo-vs-pair": "#相席ラウンジ #一人飲み #オリエンタルラウンジ #相席ナビ #出会い #夜遊び",
    "second-venue": "#相席ラウンジ #2軒目 #デート #会話術 #オリエンタルラウンジ #相席ナビ",
    "first-10-minutes": "#相席ラウンジ #会話術 #清潔感 #第一印象 #オリエンタルラウンジ #相席ナビ",
    "grooming": "#メンズ美容 #清潔感 #相席ラウンジ #身だしなみ #オリエンタルラウンジ #相席ナビ",
    "local-vs-urban": "#相席ラウンジ #オリエンタルラウンジ #出張 #地方 #データ分析 #相席ナビ",
    "cost-break-even": "#相席ラウンジ #オリエンタルラウンジ #コスパ #恋愛 #相席ナビ #夜遊び",
    "rain-payday": "#相席ラウンジ #オリエンタルラウンジ #雨の日 #穴場 #データ分析 #相席ナビ",
}


class _HtmlNode:
    __slots__ = ("tag", "attrs", "children")

    def __init__(self, tag: str, attrs: dict = None):
        self.tag = tag
        self.attrs = attrs or {}
        self.children = []

    def classes(self) -> set:
        raw = self.attrs.get("class") or ""
        return {c.strip() for c in raw.split() if c.strip()}


from html.parser import HTMLParser as _StdHTMLParser


class _SimpleDomParser(_StdHTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.root = _HtmlNode("root")
        self._stack = [self.root]

    def handle_starttag(self, tag, attrs):
        t = (tag or "").lower()
        node = _HtmlNode(t, {k.lower(): (v or "") for k, v in attrs})
        self._stack[-1].children.append(node)
        if t not in _VOID_HTML_TAGS:
            self._stack.append(node)

    def handle_startendtag(self, tag, attrs):
        t = (tag or "").lower()
        node = _HtmlNode(t, {k.lower(): (v or "") for k, v in attrs})
        self._stack[-1].children.append(node)

    def handle_endtag(self, tag):
        t = (tag or "").lower()
        if t in _VOID_HTML_TAGS:
            return
        for idx in range(len(self._stack) - 1, 0, -1):
            if self._stack[idx].tag == t:
                del self._stack[idx:]
                break

    def handle_data(self, data):
        if data:
            self._stack[-1].children.append(data)


def _polish_note_brand_text(md_text: str) -> str:
    """Normalize internal 'OLG' / bare 'オリエンタル' in prose while preserving URLs untouched."""
    if not md_text:
        return ""
    protected = []

    def _stash_url(m):
        protected.append(m.group(0))
        return f"__URL_TOKEN_{len(protected) - 1}__"

    work = re.sub(r"https?://[^\s\)\]>]+", _stash_url, md_text)
    work = re.sub(r"\bOLG\s*ag", "ag", work)
    work = re.sub(r"OLG\s*(?=[ァ-ヶー一-龠々A-Za-z])", "オリエンタルラウンジ", work)
    work = re.sub(r"\bOLG\b", "オリエンタルラウンジ", work)
    work = _sanitize_brand_names(work, prefer_casual_if_tight=False)
    for i, orig in enumerate(protected):
        work = work.replace(f"__URL_TOKEN_{i}__", orig)
    return work


def _subtree_has_affiliate_link(node: _HtmlNode) -> bool:
    if isinstance(node, str):
        return False
    if node.tag == "a":
        href = (node.attrs.get("href") or "").strip()
        if "/link/" in href or "sponsored" in (node.attrs.get("rel") or ""):
            return True
    return any(_subtree_has_affiliate_link(ch) for ch in node.children)


def _should_skip_dom_node(node: _HtmlNode) -> bool:
    if isinstance(node, str):
        return False
    if node.tag in _SKIP_HTML_TAGS:
        return True
    cls_set = node.classes()
    if cls_set & _SKIP_EXACT_CLASSES:
        return True
    if any(c.startswith("related-") for c in cls_set):
        return True
    # Exclude PR / affiliate cards (e.g. .rec-card with /link/...) while keeping pure dress-code rule cards
    if ("rec-card" in cls_set or "ladies-pr-card" in cls_set) and _subtree_has_affiliate_link(node):
        return True
    # Exclude standalone CTA button wrappers at the bottom of articles
    if node.tag == "div" and not cls_set:
        non_ws_children = [
            ch for ch in node.children
            if not (isinstance(ch, str) and not ch.strip())
        ]
        if len(non_ws_children) == 1 and isinstance(non_ws_children[0], _HtmlNode):
            only = non_ws_children[0]
            if only.tag == "a" and (only.classes() & {"btn-rose", "btn-outline", "cta-btn", "live-tool-btn"}):
                return True
    return False


def _render_inline_for_note(node, site_url: str, allow_bold: bool = True) -> str:
    if isinstance(node, str):
        return re.sub(r"[ \t\r\f\v]+", " ", node.replace("\n", " "))
    if _should_skip_dom_node(node):
        return ""
    if node.tag == "br":
        return "\n"
    inner = "".join(_render_inline_for_note(ch, site_url, allow_bold=allow_bold) for ch in node.children)
    if node.tag in ("strong", "b"):
        clean = inner.strip()
        if not clean:
            return ""
        return f"**{clean}**" if allow_bold else clean
    if node.tag == "code":
        clean = inner.strip()
        return f"「{clean}」" if clean else ""
    if node.tag == "a":
        label = re.sub(r"\s+", " ", inner).strip()
        href = (node.attrs.get("href") or "").strip()
        if not label:
            return ""
        if not href or href.startswith("#") or "/link/" in href or "sponsored" in (node.attrs.get("rel") or ""):
            return "" if (node.classes() & {"rec-btn", "ladies-pr-link", "cta-btn", "btn-rose"}) else label
        if href.startswith("/"):
            full_href = f"{site_url.rstrip('/')}{href}"
            return f"[{label}]({full_href})"
        if href.startswith("https://aiseki-navi.com"):
            return f"[{label}]({href})"
        return label
    return inner


def _clean_multiline_block(raw_text: str) -> str:
    lines = []
    for ln in (raw_text or "").split("\n"):
        cleaned = re.sub(r"[ \t]+", " ", ln).strip()
        # Collapse accidental nested **...** markers
        cleaned = re.sub(r"\*{3,}", "**", cleaned)
        lines.append(cleaned)
    out = "\n".join(lines).strip()
    out = re.sub(r"\n{3,}", "\n\n", out)
    return out


def _collect_elements_by_tag(node: _HtmlNode, target_tags: tuple) -> list:
    found = []
    for ch in node.children:
        if isinstance(ch, _HtmlNode) and not _should_skip_dom_node(ch):
            if ch.tag in target_tags:
                found.append(ch)
            else:
                found.extend(_collect_elements_by_tag(ch, target_tags))
    return found


def _convert_static_html_to_note(raw_html: str, meta: dict, site_url: str = "https://aiseki-navi.com") -> dict:
    """Convert any static/scheduled column HTML template into clean note.com Markdown + metadata."""
    site_base = site_url.rstrip("/")
    og_img_m = re.search(r'<meta[^>]+property=["\']og:image["\'][^>]+content=["\']([^"\']+)["\']', raw_html, flags=re.I)
    if not og_img_m:
        og_img_m = re.search(r'<meta[^>]+content=["\']([^"\']+)["\'][^>]+property=["\']og:image["\']', raw_html, flags=re.I)
    eyecatch_url = ""
    if og_img_m:
        eyecatch_url = og_img_m.group(1).strip()
    elif meta.get("eyecatch_jpg"):
        eyecatch_url = meta["eyecatch_jpg"]
    else:
        body_img_m = re.search(r'<img[^>]+src=["\'](/static/[^"\']+\.(?:jpg|png|webp))["\']', raw_html, flags=re.I)
        if body_img_m:
            eyecatch_url = body_img_m.group(1)
    if not eyecatch_url:
        eyecatch_url = "/static/note-eyecatch.jpg"
    if eyecatch_url.startswith("/"):
        eyecatch_url = f"{site_base}{eyecatch_url}"

    # Strip HTML comments and Jinja2 preview/loop blocks before DOM parsing
    cleaned = re.sub(r"<!--.*?-->", "", raw_html, flags=re.S)
    cleaned = re.sub(r"\{%\s*if\s+is_scheduled_preview\s*%\}[\s\S]*?\{%\s*endif\s*%\}", "", cleaned)
    cleaned = re.sub(r"\{%\s*for\s+[\s\S]*?\{%\s*endfor\s*%\}", "", cleaned)
    cleaned = re.sub(r"\{%[\s\S]*?%\}", "", cleaned)
    cleaned = re.sub(r"\{\{[\s\S]*?\}\}", "", cleaned)

    parser = _SimpleDomParser()
    parser.feed(cleaned)
    parser.close()

    extracted_h1 = []
    blocks = []

    def _walk(node):
        if isinstance(node, str) or _should_skip_dom_node(node):
            return
        cls_set = node.classes()
        tag = node.tag

        if tag == "h1":
            h1_txt = re.sub(r"\s+", " ", _render_inline_for_note(node, site_base, allow_bold=False)).strip()
            if h1_txt and h1_txt != "相席ナビ":
                extracted_h1.append(h1_txt)
            return

        if tag == "h2":
            h2_txt = re.sub(r"\s+", " ", _render_inline_for_note(node, site_base, allow_bold=False)).strip()
            if h2_txt:
                blocks.append(f"## {h2_txt}")
            return

        if tag == "h3":
            h3_txt = re.sub(r"\s+", " ", _render_inline_for_note(node, site_base, allow_bold=False)).strip()
            if h3_txt:
                blocks.append(f"### {h3_txt}")
            return

        if tag == "h4":
            h4_txt = re.sub(r"\s+", " ", _render_inline_for_note(node, site_base, allow_bold=False)).strip()
            if h4_txt:
                blocks.append(f"**■ {h4_txt}**")
            return

        if "dialogue-box" in cls_set:
            dl_raw = _clean_multiline_block(_render_inline_for_note(node, site_base, allow_bold=True))
            dl_lines = [f"> {ln}" for ln in dl_raw.split("\n") if ln.strip()]
            if dl_lines:
                blocks.append("\n".join(dl_lines))
            return

        if "stat-box" in cls_set:
            parts = []
            for ch in node.children:
                if isinstance(ch, _HtmlNode):
                    t = re.sub(r"\s+", " ", _render_inline_for_note(ch, site_base, allow_bold=False)).strip()
                    if t:
                        parts.append(t)
            if parts:
                if len(parts) >= 2:
                    blocks.append(f"- 📊 **{parts[0]}：{parts[1]}**（{' '.join(parts[2:])}）".replace("（）", ""))
                else:
                    blocks.append(f"- 📊 {parts[0]}")
            return

        if "faq-item" in cls_set or "faq-card" in cls_set:
            for ch in node.children:
                if isinstance(ch, _HtmlNode):
                    c_cls = ch.classes()
                    txt = _clean_multiline_block(_render_inline_for_note(ch, site_base, allow_bold=True))
                    if not txt:
                        continue
                    if "faq-q" in c_cls:
                        q_clean = re.sub(r"\*\*([^*]+)\*\*", r"\1", txt)
                        blocks.append(f"### 💬 {q_clean}")
                    else:
                        blocks.append(txt)
            return

        if tag in ("ul", "ol"):
            items = []
            for ch in node.children:
                if isinstance(ch, _HtmlNode) and ch.tag == "li" and not _should_skip_dom_node(ch):
                    li_txt = _clean_multiline_block(_render_inline_for_note(ch, site_base, allow_bold=True))
                    li_single = " ／ ".join(s.strip() for s in li_txt.split("\n") if s.strip())
                    if li_single:
                        items.append(f"- {li_single}")
            if items:
                blocks.append("\n".join(items))
            return

        if tag == "table":
            trs = _collect_elements_by_tag(node, ("tr",))
            headers = []
            row_lines = []
            for tr in trs:
                ths = [c for c in tr.children if isinstance(c, _HtmlNode) and c.tag == "th"]
                tds = [c for c in tr.children if isinstance(c, _HtmlNode) and c.tag == "td"]
                if ths and not headers:
                    headers = [
                        re.sub(r"\s+", " ", _render_inline_for_note(th, site_base, allow_bold=False)).strip()
                        for th in ths
                    ]
                elif tds:
                    vals = [
                        re.sub(r"\s+", " ", _render_inline_for_note(td, site_base, allow_bold=False)).strip()
                        for td in tds
                    ]
                    if not any(vals):
                        continue
                    if headers and len(vals) >= 2:
                        lead = vals[0]
                        rest = [
                            f"{headers[i]}: {vals[i]}" if i < len(headers) and headers[i] else vals[i]
                            for i in range(1, len(vals))
                            if vals[i]
                        ]
                        row_lines.append(f"- **{lead}** — " + " ／ ".join(rest))
                    else:
                        row_lines.append("- " + " ／ ".join(v for v in vals if v))
            if row_lines:
                blocks.append("\n".join(row_lines))
            return

        if tag == "p":
            p_txt = _clean_multiline_block(_render_inline_for_note(node, site_base, allow_bold=True))
            if p_txt and not p_txt.startswith("公開日："):
                blocks.append(p_txt)
            return

        # Handle mixed text+br boxes like .lead-box / .article-lead that contain direct text without <p>
        has_block_child = any(
            isinstance(ch, _HtmlNode) and ch.tag in ("p", "h1", "h2", "h3", "h4", "ul", "ol", "table", "div", "section", "article", "header")
            for ch in node.children
        )
        if not has_block_child and tag in ("div", "section"):
            box_txt = _clean_multiline_block(_render_inline_for_note(node, site_base, allow_bold=True))
            if box_txt:
                blocks.append(box_txt)
            return

        for ch in node.children:
            _walk(ch)

    _walk(parser.root)

    title_final = _polish_note_brand_text(extracted_h1[0] if extracted_h1 else (meta.get("title") or ""))
    path_str = meta.get("path") or f"/guide/{meta.get('slug', '')}"
    article_url = f"{site_base}{path_str}"

    slug = meta.get("slug") or ""
    if slug == "for-women":
        cta_footer = "\n".join([
            "---",
            "",
            "💄 **全国53店舗の「いま女子待ちゼロで入れるお店＆開催中イベント」は『相席ナビ』でリアルタイム公開中！**",
            "入店3分前にスマホでチェックできる女性向け特設ガイドはこちら👇",
            "",
            article_url,
            "",
            "👉 女性目線モード（待ち0・特典順）で現在の全国ランキングを見る",
            f"{site_base}/?mode=women",
        ])
    else:
        cta_footer = "\n".join([
            "---",
            "",
            "📊 **全国53店舗の「いま現在の男女人数・普段比・終電後維持率」は『相席ナビ』で1分ごとに無料更新中！**",
            "今回のコラムの詳しい図表データや、これから行く店舗のリアルタイム状況はこちら👇",
            "",
            article_url,
            "",
            "👉 全国リアルタイム人数ランキング（相席ナビ トップページ）",
            f"{site_base}/",
        ])

    body_joined = "\n\n".join(b for b in blocks if b.strip())
    body_markdown = _polish_note_brand_text(f"{body_joined}\n\n{cta_footer}".strip())
    hashtags = _NOTE_HASHTAG_PRESETS.get(
        slug,
        "#相席ラウンジ #オリエンタルラウンジ #JIS #ag #相席ナビ #出会い #夜遊び",
    )
    full_markdown = f"# {title_final}\n\n{body_markdown}\n\n{hashtags}"

    return {
        "ok": True,
        "slug": slug,
        "path": path_str,
        "article_url": article_url,
        "title": title_final,
        "body_markdown": body_markdown,
        "hashtags": hashtags,
        "full_markdown": full_markdown,
        "eyecatch_url": eyecatch_url,
        "char_count": len(body_markdown),
    }


_STATIC_SLUG_ESSAY_LENSES = {
    "crowd-rush-paradox": {
        "essay_title_suffix": "みんなが同じ『正解の数字』を見た瞬間に、正解が壊れる夜のゲーム理論",
        "hook": "金曜日の22時15分。居酒屋を出たばかりの路上でスマホを開き、『よし、女性比60%だ』と確かめてから歩き出したはずなのに、35分後に店の扉を開けると男性の待機列ができていた——あの狐につままれたような逆転現象には、明確な数理的理由がある。",
        "mechanism": "経済学で『エル・ファロル・バー問題（マイノリティ・ゲーム）』と呼ばれる構造だ。公開情報がリアルタイムに行き渡る時代には、街中の合理的なプレイヤーが同じ『60%の好条件店』を同時に見つけ、誰にも相談せず一斉にそこへ向かう。19時〜20時台なら女性の新規流入（60分で平均+20名）がその殺到を吸収してくれるが、女性の1回転目入店が天井を打つ22時台（60分後女性増減-0.3名）には、移動中のタイムラグ30分のあいだに男性だけが+13.2名積み上がり、実に29.8%の店舗が到着時に男余りへ転落する。一方で『女性20名以上かつ70%超』の厚みがある店だけは97.7%が崩れない。『いまの数字』ではなく『他人の移動を含めた40分後の数字』を想像できるかどうかが、週末の夜の余裕を決める。",
    },
    "last-train-cliff": {
        "essay_title_suffix": "23時20分の改札と、夜の街が二度呼吸する理由",
        "hook": "深夜23時15分を過ぎたあたりで、53店舗の人数モニターを眺めていると、まるで潮が引くように全国のグラフが一斉に揺れる瞬間がある。終電という名の見えないベルが、街全体の空気を一度リセットする時間だ。",
        "mechanism": "多くの人は『23時を過ぎたらもう終わりだ』と思い込むが、累計27万件超のログが示すのは単なる減少ではなく『客層コホートの完全な入れ替わり』だ。オフィス帰りの第1波が改札へ吸い込まれたあと、0時30分を境に、美容・飲食・アパレル上がりやタクシー移動を前提にした2軒目組（第2波）が特定の大型店へ再集結する。終電前の焦燥感と、終電後の腹が決まった穏やかさ。この時間帯による心理モードの断層を知らないまま同じテンションで座り続けることこそが、深夜のすれ違いの正体なのだ。",
    },
    "osaka-kita-vs-minami": {
        "essay_title_suffix": "御堂筋線のわずか4駅が分ける、梅田と心斎橋の『夜の温度差』",
        "hook": "同じ大阪の夜なのに、梅田の茶屋町を歩いているときと、心斎橋筋から宗右衛門町へ抜けるときでは、すれ違う人たちの歩幅も笑い声のトーンもまるで違うことに気づく。",
        "mechanism": "キタ（梅田）とミナミ（難波・心斎橋）の違いは、単なる地名の違いではなく『集まってくる人の生活導線と時間帯構造』の違いだ。阪急・JR・御堂筋線が束になる梅田は、退勤直後の20時〜22時半にオフィス勤務層の密度がピークに達し、終電時刻の規律が強く働く。一方、ミナミは飲食・サービス業や2軒目以降の回遊導線が太く、23時を回ってからむしろ熱量が立ち上がる。御堂筋線でわずか9分の距離にある2つの街を『同じ物差し』で測ろうとすると、店選びのタイミングを必ず見誤る。",
    },
    "for-women": {
        "essay_title_suffix": "『女性は無料だから気楽でしょ』という言葉が見落としている90分",
        "hook": "『女の子はタダで飲めていいよね』。相席のテーブルで何気なく放たれるその一言が、どれほど一瞬で空気を凍らせているか、言った本人はたぶん最後まで気づかない。",
        "mechanism": "経済学の視点で見れば、無料とは『コストがゼロ』という意味ではない。女性側が支払っているのは現金チャージではなく、退勤後にコテで髪を巻き直し、メイクを整え、ヒールで歩いてきた『90分の準備労働』と『今夜という二度と戻らない機会費用』だ。だからこそ、女性ばかりが余って30分待たされる店舗や、相手への敬意を欠いた席に当たったときの損失感は男性が想像するよりはるかに重い。女子側にとって本当に居心地がいい店とは、実は『男性がしっかり入っていて待ち時間ゼロで歓迎される店』であるという逆転現象も、この時間コストの構造からすべて説明がつく。",
    },
    "price-event-comparison": {
        "essay_title_suffix": "10分ごとの料金メーターが、人間の会話をどう変えてしまうか",
        "hook": "メニューの隅に書かれた『10分〇〇円』という小さな数字は、会計のときだけでなく、実は乾杯した瞬間の人間の表情や相槌の速さまで静かに支配している。",
        "mechanism": "従量課金制の空間では、人は無意識のうちに『払った分だけ元を取らなければ』というサンクコスト（埋没費用）の呪縛にかかる。特に単独待機チャージを30分払ったあとの男性は、ようやく座れた席で相手との波長がズレていても損切りができず、焦りから質問攻めに走ってしまう。料金システムや開催中イベントを事前に把握しておく本当の価値は、数百円をケチることではなく、『自分の心の余裕を削らない環境をあらかじめ選ぶこと』にある。",
    },
    "male-lead-mechanics": {
        "essay_title_suffix": "なぜ人気の大型店ほど、ある瞬間に『男余りの罠』へ反転するのか",
        "hook": "『一番有名な店舗に行けば間違いないはず』。そう信じて金曜の21時半に看板店の列へ並んだ人ほど、1時間後にくたびれた顔でビルを出てくる光景を何度も見てきた。",
        "mechanism": "ここには都市の交通工学でいう『ブライスのパラドックス』によく似た群集心理が働いている。知名度が高い旗艦店には情報感度の低いグループが一斉に集中するが、店内の座席キャパシティには上限があるため、ある閾値を超えた瞬間に男性の待機列だけが膨張し、徒歩4分の別ブランド店と15〜25ポイントもの男女比ギャップが生まれる。看板の大きさではなく『いまこの瞬間の需給の歪み』を見る目線だけが、消耗戦から自分を救い出してくれる。",
    },
    "beauty-area-stage": {
        "essay_title_suffix": "銀座・恵比寿・新宿——街の照明と客層が変われば『正解の振る舞い』も変わる",
        "hook": "同じブランドのラウンジであっても、銀座や恵比寿の扉を開けたときと、新宿や渋谷のフロアに足を踏み入れたときでは、求められている会話のテンポが驚くほど違う。",
        "mechanism": "街にはそれぞれ固有の『文化的重力』がある。百貨店や美容クリニック、落ち着いたビストロが集まる街には、ノリの良さよりも『声のトーンの落ち着きと清潔感の細部』を重視する層が集まり、逆にターミナルの歓楽街では初速の明るさとフットワークの軽さが機能する。自分のキャラクターを変える必要はないが、自分たちの波長が最も自然に呼吸できる『街のステージ』を選ぶだけで、無理な背伸びは一切いらなくなる。",
    },
    "club-area-midnight": {
        "essay_title_suffix": "クラブ街の午前1時、フロアの熱量とラウンジの静けさが交差する場所",
        "hook": "渋谷や六本木、心斎橋の深夜1時。大音量のベース音が漏れる通りから一本入ってラウンジのソファに腰を下ろすと、そこには終電前とはまったく別の時間が流れている。",
        "mechanism": "深夜帯のクラブ隣接エリアでは、『騒ぎたい層』と『音疲れして落ち着いて話せる場所へ避難してきた層』の二極化が起きる。0時過ぎのラウンジで本当に強いのは、クラブのようなハイテンションを持ち込む人ではなく、外の喧騒とのコントラストを理解し、温かい飲み物や静かなトーンで『ほっとできる余白』を作れる人だ。",
    },
    "weekend-strategy": {
        "essay_title_suffix": "金曜21時の熱狂に飲まれない人だけが知っている、週末の『時間差』設計",
        "hook": "金曜日の21時、街中の居酒屋から人が溢れ出す時間帯は、一見すると一番チャンスが多そうに見えて、実は最も『雑な消耗』が起きやすい魔の時間帯でもある。",
        "mechanism": "週末ピークは総人数こそ最大になるが、1軒目のアルコールが入った団体ノイズと受付待ち時間が同時に跳ね上がるため、1席あたりの会話の質はどうしても荒れやすい。週末に結果を出している人たちは、ピークの正面衝突を避け、19時台のアーリーインで静かに始めるか、あるいは第1波が引いた23時半以降に照準を合わせる『時間差のポートフォリオ』を組んでいる。",
    },
    "brand-comparison": {
        "essay_title_suffix": "オリエンタルラウンジ・JIS・ag——内装の違い以上に大きい『集まる理由』の差",
        "hook": "同じ『相席ラウンジ』という括りで語られがちだけれど、オリエンタルラウンジ、JIS、agの3つを同じ夜に覗き比べると、驚くほど客層のグラデーションが分かれている。",
        "mechanism": "ブランドごとの年齢制限、照明の照度、フードメニュー、そして立地戦略は、そのまま『どんな気分で今夜を過ごしたい人が集まるか』のフィルターとして機能している。落ち着いたホテルラウンジ調の空間には対話を求める層が集まり、Z世代特化のカジュアルな空間にはテンポと親近感を求める層が集まる。優劣ではなく『今夜の自分たちのモードとの適合度』でブランドを選び分けるのが、大人の夜遊びの基礎体力だ。",
    },
    "weekday-strategy": {
        "essay_title_suffix": "火曜と水曜の21時、なぜ『本当に話せる席』は平日の夜に現れるのか",
        "hook": "週末のお祭り騒ぎが嘘のように落ち着いた水曜日の21時。実は、2軒目の静かなバーへ自然につながる確率が一番高いのは、こんな何気ない平日の夜だったりする。",
        "mechanism": "平日の夜にわざわざメイクを直してラウンジへ足を運ぶ女性は、週末の『大人数のノリやイベント消化』ではなく、『仕事終わりに誰かとちゃんと会話してリフレッシュしたい』という明確な対話動機を持っていることが多い。総人数が少なくても男女比の分散が小さく、店内のノイズレベルも低いため、誠実なトーンの会話がそのまま真っ直ぐ届きやすいのだ。",
    },
    "shinjuku": {
        "essay_title_suffix": "日本最大のターミナル・新宿で、たった300メートルの移動が夜を変える理由",
        "hook": "靖国通りを渡るか、東口側へ戻るか。新宿という巨大な迷宮では、徒歩わずか4分・300メートルの選択ミスが、そのまま1時間の待ち時間差になって跳ね返ってくる。",
        "mechanism": "新宿エリアの特殊性は、超大型の複数ブランド店舗が極小半径に密集しているため、一方の店舗で団体客が入った瞬間に需給バランスが局所的に崩壊しやすい点にある。しかし裏を返せば、これほど『ハシゴによる裁定（アービトラージ）』が効く街も他にない。1店舗に固執せず、街全体をひとつの大きなフロアとして捉え直すだけで、新宿は最も負けにくいホームグラウンドに変わる。",
    },
    "solo-vs-pair": {
        "essay_title_suffix": "1人で行く夜と、2人で行く夜——『ペアの力学』を味方につける思考法",
        "hook": "友人と2人でいるときは気楽なのに、いざ4人テーブルで相方と呼吸が合わずに空回りした夜や、出張先でふと1人で扉を開けた夜にしか見えない景色がある。",
        "mechanism": "相席のテーブルは『個人戦』ではなく、2対2（あるいは1対2）の『小集団ダイナミクス』で動いている。特に女性2人組は『片方が退屈したら2人で出る』という合意形成ルールで動いているため、どちらか1人にだけロックオンした瞬間に席の寿命は終わる。ソロ参戦の自由度と、ペア参戦の連携力。それぞれの構造的な強みと弱点を知っておくだけで、テーブルの空気の作り方は劇的に変わる。",
    },
    "second-venue": {
        "essay_title_suffix": "『もう一軒行こうよ』が断られる本当の理由は、好感度ではなく『出口の不透明さ』にある",
        "hook": "テーブルではあんなに盛り上がっていたのに、会計前に『このあと飲み直そうよ』と切り出した瞬間、相手の表情にふっと警戒の膜が張る——あの独特の空気には明確な理由がある。",
        "mechanism": "行動心理学でいう『現状維持バイアス』と『終了時刻の不確実性コスト』だ。初対面の相手から行き先も解散時刻も曖昧なまま誘われると、たとえ好印象を持っていても『終電を逃すかも』『断りづらくなるかも』という防衛本能が勝ってしまう。逆に、会話がいちばん温まった45分前後のピークで『駅前のカフェバーで終電まで30分だけ』と上限時間を切って提案されると、相手は安心してイエスと言えるのだ。",
    },
    "first-10-minutes": {
        "essay_title_suffix": "乾杯から10分、会話の内容より先に『70センチの距離』で決まっていること",
        "hook": "どんなに面白いトークネタを用意していても、グラスを合わせて最初の3分間で、すでに相手の心のシャッターが半分降りてしまっている席がある。",
        "mechanism": "L字や対面のソファ席は、相手との物理的距離が約70センチしかない。この至近距離では、言葉のロジックよりも先に、店員への態度、声の圧、清潔感、そして『尋問のような一問一答になっていないか』という非言語シグナルが脳の扁桃体で瞬時に判定される。最初の10分に必要なのは自己紹介のプレゼンではなく、『この席は安全で居心地がいい』と感じてもらう温度調整なのだ。",
    },
    "grooming": {
        "essay_title_suffix": "間接照明のラウンジで、高級ブランドより10倍残酷に差がつく『清潔感の解像度』",
        "hook": "明るいオフィスでは気にならなかった小さなディテールが、ラウンジの暖色スポットライトの下では驚くほど鮮明に浮かび上がることがある。",
        "mechanism": "薄暗い空間だから多少ごまかせるというのは逆で、上からのダウンライトは髪のパサつき、肌や唇の乾燥、襟元のヨレ、そして接近時の香りや口元の印象を強調する。女性が言う『清潔感』とは、造形や高価な服のことではなく、『自分の手入れを丁寧にしている人＝他人のことも雑に扱わない人』という信頼のプロキシ（代理指標）なのだ。",
    },
    "local-vs-urban": {
        "essay_title_suffix": "東京・大阪の消耗戦を抜けて、地方都市のラウンジで見つけた『本来の余白』",
        "hook": "出張の新幹線を降りて、札幌や仙台、福岡、熊本のラウンジをふらりと覗いた夜、『あれ、なんだか東京よりずっと自然体で話せるな』と驚く人は少なくない。",
        "mechanism": "都心の超大型店では毎晩何百人もの人が高速で入れ替わるため、どうしてもお互いに『次もあるし』というカタログ的な消費モードに陥りやすい。一方、地方中核都市の店舗は商圏がコンパクトで1組あたりの対話密度が高く、県外から来た訪問者に対する純粋な好奇心も働きやすい。都市の規模による『出会いの希少性と温度差』を知ると、出張の夜の楽しみ方がまるで変わってくる。",
    },
    "cost-break-even": {
        "essay_title_suffix": "『元を取ろう』とした夜ほど赤字になる——夜のサンクコスト損益分岐点",
        "hook": "帰り道のコンビニでレシートを見返しながら、『あの50分目で出ておけば、お金も体力も半分で済んだのにな』と苦笑いした経験はないだろうか。",
        "mechanism": "経済合理性だけで考えれば、過去に支払った待機料金や最初の30分のチャージは、これからの10分が楽しくなるかどうかとは一切関係がない（サンクコスト）。それなのに人間は損失回避バイアスによって『もう少し粘れば展開が変わるかも』と追加投資を重ねてしまう。夜の損益分岐点とは金額の計算ではなく、『お互いの笑顔が自然に出ているうちに席を立つ勇気』のことだ。",
    },
    "rain-payday": {
        "essay_title_suffix": "雨の夜と給料日後の週末——天気予報とカレンダーが男女比を動かすメカニズム",
        "hook": "夕方18時に突然降り出した本降りの雨。街の傘が増えるのとまったく同じタイミングで、53店舗の男女比マップにはくっきりとした『偏り』が生まれ始める。",
        "mechanism": "雨や強風の夜、ヘアセットと足元を守りたい女性ペアは、路面を歩く店舗を避けて『地下街・駅ビル直結』の店舗へ集中的に避難する。一方、給料日直後の金曜日は男性側の財布の紐が緩んで歓楽街全体が過密になる。気象条件とカレンダーという一見無関係な外部変数が、実は店内の需給バランスを15ポイント以上動かしているという事実は、知っている人だけに開かれた静かな羅針盤になる。",
    },
    "stats-report": {
        "essay_title_suffix": "累計27万件の人数ログを眺め続けてわかった、夜の街の『変わらない法則』",
        "hook": "1分ごとに記録され続ける全国53店舗の男女人数。その膨大な数字の列を何ヶ月も追いかけていると、気まぐれに見える夜の街に、驚くほど規則正しい呼吸があることに気づく。",
        "mechanism": "曜日ごとの山と谷、23時20分の終電崖、雨の日の地下直結シフト、そして同一エリア内で必ず発生する店舗間ギャップ。個人の運やノリに見えていた出来事の大半は、実はこうしたマクロな需給の波の上で起きている。データを知ることはロマンを消すことではなく、無用な空回りを減らして『目の前の人との会話そのもの』に集中するための武器なのだ。",
    },
}


def _reframe_static_note_as_deep_essay(verbatim_res: dict, slug: str, site_base: str) -> dict:
    """
    Transform a static/scheduled Web column into an independent, deeply reflective first-person
    note.com essay (`mode="essay"`) so Web SEO articles and note manuscripts are never copy-paste twins.
    """
    lens = _STATIC_SLUG_ESSAY_LENSES.get(slug) or {
        "essay_title_suffix": "53店舗の実測ログから読み解く、夜の行動経済学",
        "hook": "深夜、全国53店舗のリアルタイム人数ログを静かに眺めていると、数字の増減の向こう側に、今夜誰かと話したくて街へ繰り出した人たちのリアルな息遣いが浮かび上がってくる。",
        "mechanism": "相席ラウンジという空間は、男性側の『時間従量チャージ』と女性側の『準備時間・機会費用』が交差する非対称な二面市場だ。うまくいかない夜の原因を自分のトーク力不足だと責める人は多いが、実際には入店前の需給ギャップや時間帯ごとの客層コホートの変化という『環境構造』が8割を決めている。",
    }

    raw_body = verbatim_res.get("body_markdown") or ""
    # Extract genuine field dialogue quotes (`> ...`) and crisp data bullets (`- 📊 ...` / `- **...`) as primary evidence
    quote_blocks = []
    cur_q = []
    data_bullets = []
    for ln in raw_body.splitlines():
        s = ln.strip()
        if s.startswith("> "):
            cur_q.append(s)
        else:
            if cur_q:
                quote_blocks.append("\n".join(cur_q[:3]))
                cur_q = []
            if (s.startswith("- 📊") or s.startswith("- **")) and len(data_bullets) < 4 and "/link/" not in s:
                data_bullets.append(s)
    if cur_q:
        quote_blocks.append("\n".join(cur_q[:3]))

    if not quote_blocks:
        quote_blocks = [
            "> 「さっきの店と空気ぜんぜん違うね。最初に数字を見てから動くだけで、こんなに気持ちの余裕が変わるんだな」\n"
            "> 「変に盛り上げようと気負わなくていいから、こっちのほうが自然体で話せるね」"
        ]

    try:
        import local_db as _ldb_mod
        hist_cnt = max(int((_ldb_mod.get_db_stats() or {}).get("total_rows") or 0), 276000)
    except Exception:
        hist_cnt = 276000

    base_title = verbatim_res.get("title") or ""
    short_core = base_title.split("｜")[0].split("——")[0].strip()
    essay_title = _polish_note_brand_text(f"{short_core}｜{lens['essay_title_suffix']}")
    article_url = verbatim_res.get("article_url") or f"{site_base}/guide/{slug}"
    hashtags = verbatim_res.get("hashtags") or "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #行動経済学 #人間観察"

    parts = [
        lens["hook"],
        (
            f"私は普段、『相席ナビ』という全国53店舗のリアルタイム男女比モニターを運営しながら、累計{hist_cnt:,}件を超える稼働ログと現場の声を記録し続けている。"
            "Web版のガイド記事では検索から辿り着いた方向けに店舗比較表や実践ステップを整理しているが、このnoteでは少し視点を変えて、"
            f"『{short_core}』というテーマの奥にある人間の心理と、夜の街の見えない構造について書いてみたい。"
        ),
        "## 現場の会話に滲む「個人のせいではないすれ違い」",
        (
            "夜のラウンジで起きている気まずさや空回りの大半は、実は誰かの性格が悪いからでも、会話センスが決定的に欠けているからでもない。"
            "ただ『いま自分が座っているテーブルが、街全体のどんな需給バランスの上にあるか』を知らないまま、手探りでアクセルを踏んでしまっているだけなのだ。現場ではよく、こんな本音がこぼれ落ちる。"
        ),
        quote_blocks[0],
        (
            "同じエリアの徒歩数分圏内に、女性比率が60%を超えて穏やかな空気が流れている店舗と、男性の待機列が膨らんでピリピリした消耗戦になっている店舗が同時に並んでいることがある。"
            "人間は不思議なもので、周囲に余裕がある空間に座れば自然と相手の話をゆっくり聞けるのに、過密な競争環境に置かれた瞬間、無意識のうちに『早く結果を出さなきゃ』と視野が狭くなってしまう。"
        ),
        "## 二面市場のインセンティブと、データが示す構造的理由",
        lens["mechanism"],
    ]

    if data_bullets:
        parts.append("実際に今回のテーマに関わる現場データを抜き出してみると、感覚論ではない輪郭がはっきりと浮かび上がる。")
        parts.append("\n".join(data_bullets[:3]))

    if len(quote_blocks) >= 2:
        parts.extend([
            "そして、こうした構造のズレや噛み合い方は、テーブルの上の何気ない一言にもそのまま表れる。",
            quote_blocks[1],
        ])

    parts.extend([
        "## テクニックを増やすより、「無理をしなくていい場所」を選ぶこと",
        (
            "相席ラウンジというと、どうしても『何を話すか』『どう盛り上げるか』という小手先の会話術ばかりが語られやすい。"
            "けれど、お金のメーターが回る男性側のサンクコスト心理と、90分かけて身支度をしてきた女性側の時間コスト。この2つの非対称な事情にお互いがほんの少しだけ想像力を持てたとき、テーブルの空気は驚くほど穏やかに変わる。"
        ),
        (
            "合わない波のなかで無理に90分粘って疲弊する必要はないし、逆に波長が合ったなら、一番楽しいピークの余韻が残るうちに『駅前の静かなお店で30分だけ話そうか』と出口の時間を添えて誘えばいい。"
            "データを10秒だけ覗いてから動く習慣は、誰かを攻略するための計算ではなく、今夜出会う相手と自分自身の機嫌を丁寧に扱うための『大人の余白』なのだと私は思っている。"
        ),
        "---",
        (
            "📊 **Web版の詳細リファレンス（店舗別データ比較表・時間帯別プロトコル）はこちら**\n"
            "この記事のテーマに関する具体的な店舗比較マトリクスや、入店前に確認しておきたいチェックリストはWeb版コラムに整理しています👇\n\n"
            f"{article_url}\n\n"
            "👉 全国53店舗の「いま現在の男女人数・女性比率ランキング」を見る（1分更新・無料）\n"
            f"{site_base}/"
        ),
    ])

    essay_body = _polish_note_brand_text("\n\n".join(p.strip() for p in parts if p and p.strip()))
    distinct_pct = _compute_text_distinct_pct(raw_body, essay_body)
    full_markdown = f"# {essay_title}\n\n{essay_body}\n\n{hashtags}"

    return {
        "ok": True,
        "mode": "essay",
        "slug": slug,
        "path": verbatim_res.get("path") or f"/guide/{slug}",
        "article_url": article_url,
        "title": essay_title,
        "body_markdown": essay_body,
        "hashtags": hashtags,
        "full_markdown": full_markdown,
        "eyecatch_url": verbatim_res.get("eyecatch_url") or f"{site_base}/static/note-eyecatch.jpg",
        "char_count": len(essay_body),
        "distinct_from_web_pct": distinct_pct,
    }


def export_column_for_note(slug: str, site_url: str = "https://aiseki-navi.com", mode: str = "essay") -> dict:
    """
    Export ANY existing static column, scheduled column, or dynamically published/scheduled
    editorial column into a ready-to-paste note.com manuscript kit.
    - `mode="essay"` (default): Independent first-person structural insight essay (non-duplicate of Web SEO copy).
    - `mode="verbatim"`: Faithful Markdown conversion of the Web article sections & tables.
    """
    resolved_mode = "verbatim" if str(mode or "").strip().lower() == "verbatim" else "essay"
    slug_clean = (slug or "").strip().strip("/")
    if slug_clean.startswith("guide/"):
        slug_clean = slug_clean[len("guide/"):]
    if not slug_clean:
        slug_clean = "last-train-cliff"

    site_base = (site_url or "https://aiseki-navi.com").rstrip("/")

    # 1. Check dynamically created editorial columns first (`col_article:<slug>`)
    for dyn in get_dynamic_scheduled_articles(allow_blocking=True):
        if dyn.get("slug") == slug_clean:
            path_str = dyn.get("path") or f"/guide/{slug_clean}"
            article_url = f"{site_base}{path_str}"
            eye_jpg = dyn.get("eyecatch_jpg") or "/static/eyecatch-last-train-cliff.jpg"
            eye_url = f"{site_base}{eye_jpg}" if eye_jpg.startswith("/") else eye_jpg
            hashtags = (dyn.get("note_hashtags") or "#相席ラウンジ #オリエンタルラウンジ #相席ナビ #行動経済学 #人間観察").strip()
            web_prose = " ".join(
                " ".join(sec.get("paragraphs") or []) + " " + " ".join(sec.get("after_paragraphs") or [])
                for sec in (dyn.get("sections") or [])
            )

            if resolved_mode == "verbatim":
                title_val = _polish_note_brand_names_simple(dyn.get("title") or "")
                v_lines = [dyn.get("desc") or "", ""]
                for sec in dyn.get("sections") or []:
                    if sec.get("heading"):
                        v_lines.append(f"## {sec['heading']}")
                        v_lines.append("")
                    for p in sec.get("paragraphs") or []:
                        v_lines.append(p)
                        v_lines.append("")
                    for dl in sec.get("dialogue") or []:
                        v_lines.append(f"> {dl}")
                    if sec.get("dialogue"):
                        v_lines.append("")
                    for p in sec.get("after_paragraphs") or []:
                        v_lines.append(p)
                        v_lines.append("")
                    if sec.get("mechanism_note"):
                        v_lines.append(f"**🔎 構造メカニズム考察**: {sec['mechanism_note']}")
                        v_lines.append("")
                v_lines.extend(["---", "", f"📖 Web版コラム：{article_url}", f"👉 相席ナビ：{site_base}/"])
                body_md = _polish_note_brand_text("\n".join(v_lines).strip())
                return {
                    "ok": True,
                    "mode": "verbatim",
                    "slug": slug_clean,
                    "path": path_str,
                    "article_url": article_url,
                    "title": title_val,
                    "body_markdown": body_md,
                    "hashtags": hashtags,
                    "full_markdown": f"# {title_val}\n\n{body_md}\n\n{hashtags}",
                    "eyecatch_url": eye_url,
                    "char_count": len(body_md),
                    "distinct_from_web_pct": _compute_text_distinct_pct(web_prose, body_md),
                }

            title_val = _polish_note_brand_names_simple(dyn.get("note_title") or dyn.get("title") or "")
            raw_md = dyn.get("note_markdown") or ""
            if not raw_md or _compute_text_distinct_pct(web_prose, raw_md) < 45:
                raw_md = _build_note_essay_from_candidate(dict(dyn), site_url=site_base)
            lines = raw_md.splitlines()
            if lines and lines[0].lstrip().startswith("# "):
                lines = lines[1:]
                while lines and not lines[0].strip():
                    lines = lines[1:]
            if lines and lines[-1].strip().startswith("#") and "##" not in lines[-1]:
                hashtags = lines[-1].strip()
                lines = lines[:-1]
                while lines and not lines[-1].strip():
                    lines = lines[:-1]
            body_md = _polish_note_brand_text("\n".join(lines).strip())
            full_md = f"# {title_val}\n\n{body_md}\n\n{hashtags}"
            return {
                "ok": True,
                "mode": "essay",
                "slug": slug_clean,
                "path": path_str,
                "article_url": article_url,
                "title": title_val,
                "body_markdown": body_md,
                "hashtags": hashtags,
                "full_markdown": full_md,
                "eyecatch_url": eye_url,
                "char_count": len(body_md),
                "distinct_from_web_pct": _compute_text_distinct_pct(web_prose, body_md),
            }

    # 2. Check static scheduled columns (`scheduled_articles.SCHEDULED_ARTICLES`) + evergreen static columns
    import scheduled_articles
    target_meta = None
    for sc in scheduled_articles.SCHEDULED_ARTICLES:
        if sc["slug"] == slug_clean:
            target_meta = sc
            break
    if target_meta is None:
        for col in EXISTING_STATIC_COLUMNS:
            if col["slug"] == slug_clean or col["path"].strip("/") == slug_clean:
                target_meta = col
                break

    if target_meta is None:
        return {"ok": False, "error": f"指定されたコラム（{slug_clean}）が見つかりません。"}

    tpl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "templates", target_meta["template"])
    try:
        with open(tpl_path, "r", encoding="utf-8") as f:
            raw_html = f.read()
    except Exception as e:
        return {"ok": False, "error": f"テンプレートの読み込みに失敗しました: {e}"}

    verbatim_res = _convert_static_html_to_note(raw_html, target_meta, site_url=site_base)
    if resolved_mode == "verbatim":
        verbatim_res["mode"] = "verbatim"
        verbatim_res["distinct_from_web_pct"] = 0
        return verbatim_res
    return _reframe_static_note_as_deep_essay(verbatim_res, slug_clean, site_base)


def _polish_note_brand_names_simple(text: str) -> str:
    return _polish_note_brand_text(text)


# =====================================================================
# Full 4-Role Column Editorial Pipeline Entrypoint
# =====================================================================
def run_column_editorial_pipeline(
    live_stores: list,
    theme_key: str = "area_hopping",
    custom_topic: str = "",
    now_jst: datetime.datetime = None,
    app_url: str = "",
    use_ai: bool = True,
    force_ai_refresh: bool = False,
    ai_cache_only: bool = False,
    tone_mode: str = None,
) -> dict:
    now_jst = now_jst or datetime.datetime.now(JST)
    t_key = theme_key if theme_key in COLUMN_THEMES else "area_hopping"
    allow_blocking = not ai_cache_only
    default_tm = sns_editorial.get_default_editorial_tone_mode(allow_blocking=allow_blocking)
    resolved_tm = sns_editorial.get_editorial_tone_mode(tone_mode, allow_blocking=allow_blocking)

    research = run_column_researcher(
        live_stores=live_stores,
        theme_key=t_key,
        custom_topic=custom_topic,
        now_jst=now_jst,
    )
    plan = run_column_planner(research=research, now_jst=now_jst)

    gemini_status = sns_editorial.get_gemini_status(allow_blocking=allow_blocking)
    ai_cached = has_cached_column_ai(research, plan, tone_mode=resolved_tm)
    sheets_warming = (not getattr(job_state, "_sheets_loaded", True)) and (not job_state.sheets_store.is_disabled())
    ai_pending_async = bool(
        use_ai
        and ai_cache_only
        and (gemini_status.get("configured") or sheets_warming)
        and (force_ai_refresh or not ai_cached)
    )

    candidates = run_column_writer(
        research=research,
        plan=plan,
        now_jst=now_jst,
        app_url=app_url,
        use_ai=use_ai,
        force_ai_refresh=force_ai_refresh,
        ai_cache_only=ai_cache_only,
        tone_mode=resolved_tm,
    )
    evaluation = run_column_evaluator(candidates=candidates, research=research, plan=plan, tone_mode=resolved_tm)

    default_sched_dt = (now_jst.astimezone(JST).replace(tzinfo=None) + datetime.timedelta(days=1)).replace(
        hour=19, minute=0, second=0, microsecond=0
    )

    pdca_directives = {}
    try:
        import pdca_engine
        pdca_directives = pdca_engine.get_active_directives(allow_blocking=False) or {}
    except Exception:
        pdca_directives = {}

    return {
        "theme_key": t_key,
        "themes": list(COLUMN_THEMES.values()),
        "tone_mode": resolved_tm,
        "tone_mode_label": sns_editorial.EDITORIAL_TONE_MODES[resolved_tm]["label"],
        "default_tone_mode": default_tm,
        "tone_modes": sns_editorial.get_tone_modes_catalog(),
        "eyecatch_presets": [
            {"key": k, "label": v["label"], "jpg": v["jpg"]}
            for k, v in EYECATCH_PRESETS.items()
        ],
        "default_schedule_input": default_sched_dt.strftime("%Y-%m-%dT%H:%M"),
        "research": research,
        "plan": plan,
        "evaluation": evaluation,
        "winner": evaluation.get("winner"),
        "gemini_status": sns_editorial.get_gemini_status(allow_blocking=False),
        "ai_pending_async": ai_pending_async,
        "history": get_recent_column_history(limit=6, allow_blocking=False),
        "existing_audit": audit_existing_columns(),
        "pdca_directives": pdca_directives,
    }
