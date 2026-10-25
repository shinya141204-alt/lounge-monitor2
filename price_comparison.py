"""
price_comparison.py - Cross-Brand Lounge Price, Men's Event & Live Cost-Performance Comparison Engine.

Compares per-store official pricing (charge, solo/waiting fee per 10 min, time-bracket weekday/weekend
aiseki rates per 10 min, 60-min total estimate, 1-person solo entry policy), active men's discounts &
women's magnet events (from events.py), and real-time gender balance across all 51 domestic stores.
"""

import re
import store_master

# Brand-level baseline price & vibe specifications (domestic stores)
BRAND_PRICE_SPECS = {
    "OLG": {
        "brand_code": "OLG",
        "brand_label": "オリエンタルラウンジ",
        "brand_slug": "oriental-lounge",
        "charge_yen": 550,
        "charge_str": "550円（公式アプリ会員0円）",
        "wait_10m_yen": 220,
        "wait_fee_str": "220円〜330円 / 10分（単独時・飲み放題込）",
        "has_wait_fee": True,
        "weekday_10m_yen": 660,
        "weekday_10m_str": "440円〜880円 / 10分（店舗・時間帯別）",
        "weekend_10m_yen": 880,
        "weekend_10m_str": "660円〜1,200円 / 10分（店舗・時間帯別）",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,300円〜4,620円",
        "est_60m_weekend_label": "約3,960円〜7,200円",
        "solo_rule": "店舗別（大型店は2名以上必須／他店はシングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "王道ラグジュアリー・OL/社会人中心",
        "default_men_perk": "公式アプリ「SOLOTTE!」会員登録で男性チャージ550円が無料＆200種以上飲み放題込",
        "default_women_perk": "女性チャージ・相席・ドリンク完全0円＋季節スイーツ＆限定フード特典",
    },
    "ag": {
        "brand_code": "ag",
        "brand_label": "ag（アグ）",
        "brand_slug": "ag",
        "charge_yen": 550,
        "charge_str": "550円（公式アプリ会員0円）",
        "wait_10m_yen": 220,
        "wait_fee_str": "220円〜330円 / 10分（単独時・飲み放題込）",
        "has_wait_fee": True,
        "weekday_10m_yen": 600,
        "weekday_10m_str": "500円〜770円 / 10分（店舗・時間帯別）",
        "weekend_10m_yen": 770,
        "weekend_10m_str": "660円〜990円 / 10分（店舗・時間帯別）",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,300円〜4,620円",
        "est_60m_weekend_label": "約3,960円〜5,940円",
        "solo_rule": "1名入店OK（店舗によりシングルチャージ+1,100円）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "Z世代特化・カジュアル＆低価格",
        "default_men_perk": "公式アプリ会員登録でチャージ0円＋U-25/U-30定額・早割等の参加型企画",
        "default_women_perk": "女性完全0円＋韓国コスメ・カフェスイーツ・トレンドイベント多数",
    },
    "JIS": {
        "brand_code": "JIS",
        "brand_label": "JIS（ジス）",
        "brand_slug": "jis",
        "charge_yen": 550,
        "charge_str": "550円（テーブルチャージ）",
        "wait_10m_yen": 330,
        "wait_fee_str": "220円〜330円 / 10分（待機時・飲み放題込）",
        "has_wait_fee": True,
        "weekday_10m_yen": 660,
        "weekday_10m_str": "550円〜880円 / 10分（店舗・時間帯別）",
        "weekend_10m_yen": 770,
        "weekend_10m_str": "660円〜990円 / 10分（店舗・時間帯別）",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約3,850円〜5,830円",
        "est_60m_weekend_label": "約4,510円〜6,490円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "本格バー空間・店舗別の男性割引が豊富",
        "default_men_perk": "店舗別の男性早割・相席料金OFF・定額プラン等のキャンペーンが充実",
        "default_women_perk": "女性完全0円＋JCBギフト券・豪華プレゼント抽選・フード無料企画",
    },
    "XIX": {
        "brand_code": "XIX",
        "brand_label": "相席ラウンジ XIX",
        "brand_slug": "oriental-lounge",
        "charge_yen": 500,
        "charge_str": "500円（LINE会員0円）",
        "wait_10m_yen": 220,
        "wait_fee_str": "0円〜220円 / 10分（単独待機時）",
        "has_wait_fee": True,
        "weekday_10m_yen": 550,
        "weekday_10m_str": "550円 / 10分（U29早割あり）",
        "weekend_10m_yen": 650,
        "weekend_10m_str": "650円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3900,
        "est_60m_weekday_label": "約3,300円（U29早割980円〜）",
        "est_60m_weekend_label": "約3,900円（金曜20代定額あり）",
        "solo_rule": "1名入店OK",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "岡山エリア定番・U29早割＆金曜定額が強力",
        "default_men_perk": "U25/U29男性限定の平日早割（60分980円〜1,980円）＆金曜20代定額プラン",
        "default_women_perk": "女性完全無料＋豪華コスメ・テーマパーク券が当たる相席スクラッチ",
    },
    "ヤタコイ": {
        "brand_code": "ヤタコイ",
        "brand_label": "ヤタコイ（YATAKOI）",
        "brand_slug": "oriental-lounge",
        "charge_yen": 550,
        "charge_str": "550円",
        "wait_10m_yen": 220,
        "wait_fee_str": "220円 / 10分（単独待機時）",
        "has_wait_fee": True,
        "weekday_10m_yen": 550,
        "weekday_10m_str": "550円 / 10分（22時〜660円）",
        "weekend_10m_yen": 660,
        "weekend_10m_str": "660円 / 10分（22時〜770円）",
        "est_60m_weekday": 3850,
        "est_60m_weekend": 4510,
        "est_60m_weekday_label": "約3,850円〜",
        "est_60m_weekend_label": "約4,510円〜",
        "solo_rule": "2名以上推奨（混雑状況による）",
        "age_range": "20代前半〜後半",
        "vibe_short": "屋台×お祭り空間・ノリ重視で話しやすい",
        "default_men_perk": "茶屋町エリアでカジュアルに飲めるエンタメ相席スタイル",
        "default_women_perk": "女性無料＋屋台フード＆ゲーム感覚で盛り上がれる空間",
    },
    "CLOVERS": {
        "brand_code": "CLOVERS",
        "brand_label": "相席CLOVERS広島",
        "brand_slug": "oriental-lounge",
        "charge_yen": 0,
        "charge_str": "550円（アプリ会員0円）",
        "wait_10m_yen": 220,
        "wait_fee_str": "0円〜220円 / 10分（待機時）",
        "has_wait_fee": True,
        "weekday_10m_yen": 550,
        "weekday_10m_str": "550円 / 10分（日〜木22時迄は半額275円）",
        "weekend_10m_yen": 660,
        "weekend_10m_str": "660円 / 10分",
        "est_60m_weekday": 2750,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約1,650円〜3,300円（22時迄半額）",
        "est_60m_weekend_label": "約3,960円",
        "solo_rule": "1名入店OK",
        "age_range": "20代〜30代前半",
        "vibe_short": "広島・新天地1-9／日〜木22時迄の男性半額＆QUOカード企画が名物",
        "default_men_perk": "日〜木22時までの入店で男性相席料金が半額（50%OFF）＆QUOカードプレゼント企画",
        "default_women_perk": "女性完全無料＋限定パフェ・特製スイーツ＆VIPカラオケ個室無料",
    },
    "ALFA": {
        "brand_code": "ALFA",
        "brand_label": "相席ラウンジ ALFA広島",
        "brand_slug": "oriental-lounge",
        "charge_yen": 550,
        "charge_str": "550円（早割・会員特典あり）",
        "wait_10m_yen": 220,
        "wait_fee_str": "0円〜220円 / 10分（待機時）",
        "has_wait_fee": True,
        "weekday_10m_yen": 500,
        "weekday_10m_str": "500円〜600円 / 10分（税込・飲み放題込）",
        "weekend_10m_yen": 600,
        "weekend_10m_str": "600円〜700円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,000円〜3,600円",
        "est_60m_weekend_label": "約3,600円〜4,200円",
        "solo_rule": "1名入店OK（ソロ参戦歓迎）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "広島・新天地6-1（グランポルト5F）／カラオケ個室＆10分500円〜の好コスパ",
        "default_men_perk": "男性相席10分500円〜（税込・飲み放題込）＆早割・1人参戦歓迎プラン",
        "default_women_perk": "女性完全無料＋特製ビュッフェ・スイーツ食べ飲み放題＆カラオケ個室完備",
    },
}

# Per-store official price specifications for all 51 domestic stores
# Format per store:
#   wait_fee_str: Solo / waiting rate per 10 min (tax incl.)
#   wait_10m_yen: Numeric solo / waiting rate per 10 min used for live wait-cost estimation
#   early_10m_str: Open~22:00 aiseki 10-min rate (weekday / Fri-Sat-Hol)
#   late_10m_str: 22:00~peak aiseki 10-min rate (weekday / Fri-Sat-Hol)
#   weekday_10m_str: Combined weekday 10-min summary (early -> late)
#   weekend_10m_str: Combined Fri/Sat/pre-holiday 10-min summary (early -> late)
#   est_60m_weekday: 60-min aiseki estimate on weekday (20-22h baseline)
#   est_60m_weekend: 60-min aiseki estimate on weekend (22h peak baseline)
#   est_60m_weekday_label: Display label for weekday 60-min estimate
#   est_60m_weekend_label: Display label for weekend 60-min estimate
#   solo_rule: 1-person (solo) entry rule
STORE_PRICE_TABLE = {
    # --- Tokyo: Shibuya / Ebisu / Shinjuku ---
    "OLG 恵比寿": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土1,100円",
        "late_10m_str": "平日880円 / 金土1,200円",
        "weekday_10m_str": "660円（22時〜880円）/ 10分",
        "weekend_10m_str": "1,100円（22時〜1,200円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 6600,
        "est_60m_weekday_label": "約3,960円（22時〜5,280円）",
        "est_60m_weekend_label": "約6,600円（22時〜7,200円）",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代後半〜30代半ば",
        "vibe_short": "全国最高峰のハイクラス店・モデル/丸の内OL層",
    },
    "OLG 渋谷本店": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土880円",
        "late_10m_str": "平日770円 / 金土1,100円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "880円（22時〜1,100円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約5,280円（22時〜6,600円）",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "道玄坂の旗艦メガ店舗・常時大人数で高回転",
    },
    "OLG ag渋谷": {
        "wait_fee_str": "平日330円 / 金土440円（10分）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日550円 / 金土770円",
        "late_10m_str": "平日660円〜770円 / 金土990円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "770円（22時〜990円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,940円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "19〜25歳中心（Z世代限定）",
        "vibe_short": "渋谷本店より60分あたり660円〜1,200円安いZ世代特化店",
    },
    "OLG 新宿": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "歌舞伎町最大の動員数・渋谷/恵比寿より週末単価が安い",
    },
    "JIS SHINJUKU": {
        "wait_fee_str": "330円 / 10分（待機時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日770円 / 金土880円",
        "late_10m_str": "平日880円 / 金土990円",
        "weekday_10m_str": "770円（22時〜880円）/ 10分",
        "weekend_10m_str": "880円（22時〜990円）/ 10分",
        "est_60m_weekday": 5170,
        "est_60m_weekend": 5830,
        "est_60m_weekday_label": "約5,170円（22時〜5,830円）",
        "est_60m_weekend_label": "約5,830円（22時〜6,490円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "歌舞伎町・1名入店OK＆男性割引イベント開催日は大幅お得",
    },
    "JIS NISHISHINJUKU": {
        "wait_fee_str": "330円 / 10分（待機時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日770円 / 金土880円",
        "late_10m_str": "平日880円 / 金土990円",
        "weekday_10m_str": "770円（22時〜880円）/ 10分",
        "weekend_10m_str": "880円（22時〜990円）/ 10分",
        "est_60m_weekday": 5170,
        "est_60m_weekend": 5830,
        "est_60m_weekday_label": "約5,170円（22時〜5,830円）",
        "est_60m_weekend_label": "約5,830円（22時〜6,490円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代半ば",
        "vibe_short": "新宿西口・丸の内/西新宿OL中心で落ち着いた大人空間",
    },
    # --- Tokyo East & Suburban Kanto ---
    "OLG 上野": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日660円 / 金土880円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "880円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "上野仲町通り・社会人OLが多く1名入店も可能",
    },
    "OLG ag上野": {
        "wait_fee_str": "330円 / 10分（単独時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土880円",
        "late_10m_str": "平日770円 / 金土990円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "880円（22時〜990円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約5,280円（22時〜5,940円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "オリエンタル上野に隣接・20代前半の女子大生/若手中心",
    },
    "OLG 錦糸町": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "都内オリエンタルで最安水準の10分550円〜！総武線エリアの穴場",
    },
    "OLG 町田": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日660円〜770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "西東京・神奈川北部の拠点／渋谷店より週末単価が安い",
    },
    "OLG 柏": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日660円 / 金土770円〜880円",
        "weekday_10m_str": "660円 / 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "常磐線・千葉北西部の中心店／単独時220円均一で安心",
    },
    "OLG 横浜": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土880円",
        "late_10m_str": "平日770円 / 金土990円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "880円（22時〜990円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約5,280円（22時〜5,940円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "横浜駅西口・神奈川トップの集客力と華やかな客層",
    },
    "OLG 大宮": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日660円 / 金土880円",
        "weekday_10m_str": "660円 / 10分（終日均一）",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円（22時以降も同額）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "北関東・埼玉最大の集客力！平日は22時以降も660円均一",
    },
    "JIS OMIYA": {
        "wait_fee_str": "220円〜330円 / 10分（待機時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（22時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（22時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "大宮駅東口でオリエンタル大宮と競合・イベント開催日が狙い目",
    },
    "OLG 宇都宮": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "オリオン通り・10分550円〜の北関東リーズナブル設定",
    },
    "OLG 高崎": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "高崎駅西口・平日60分3,300円〜で地元女性とマッチング",
    },
    # --- Osaka / Kyoto / Kobe (Kansai) ---
    "OLG 大阪駅前": {
        "wait_fee_str": "330円 / 10分（単独時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日700円 / 金土800円",
        "late_10m_str": "平日880円 / 金土990円",
        "weekday_10m_str": "700円（22時〜880円）/ 10分",
        "weekend_10m_str": "800円（22時〜990円）/ 10分",
        "est_60m_weekday": 4200,
        "est_60m_weekend": 4800,
        "est_60m_weekday_label": "約4,200円（22時〜5,280円）",
        "est_60m_weekend_label": "約4,800円（22時〜5,940円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "梅田・お初天神の王道店／綺麗め社会人OL層が中心",
    },
    "OLG ag梅田": {
        "wait_fee_str": "330円 / 10分（単独時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円（24時〜990円）",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "阪急東通り・大阪駅前店より10分単価が安くZ世代が密集",
    },
    "OLG 天満": {
        "wait_fee_str": "330円 / 10分（単独時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日600円 / 金土700円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "600円（22時〜770円）/ 10分",
        "weekend_10m_str": "700円（22時〜880円）/ 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,600円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,200円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "梅田から1駅・大阪駅前店より10分あたり100円以上安い穴場",
    },
    "JIS UMEDA": {
        "wait_fee_str": "330円 / 10分（待機時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（22時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（22時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "お初天神・オリエンタル大阪駅前と徒歩1分で比較可能",
    },
    "JIS CHAYAMACHI": {
        "wait_fee_str": "330円 / 10分（待機時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（22時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（22時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "阪急茶屋町・ファッション/美容系女子が多い洗練空間",
    },
    "YATAKOI UMEDA": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3850,
        "est_60m_weekend": 4510,
        "est_60m_weekday_label": "約3,850円（22時〜4,510円）",
        "est_60m_weekend_label": "約4,510円（22時〜5,170円）",
        "solo_rule": "2名以上推奨（状況により案内可）",
        "age_range": "20代前半〜後半",
        "vibe_short": "茶屋町・屋台×お祭りエンタメ空間で初対面でも盛り上がる",
    },
    "OLG 心斎橋": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日600円〜660円 / 金土660円〜770円",
        "late_10m_str": "平日660円（24時〜770円）/ 金土770円（24時〜880円）",
        "weekday_10m_str": "600円〜660円 / 10分",
        "weekend_10m_str": "660円〜770円（24時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,600円〜3,960円",
        "est_60m_weekend_label": "約3,960円〜4,620円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "東心斎橋・梅田より10分単価が100円〜200円安くコスパ良好",
    },
    "OLG 難波": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "道頓堀すぐ・週末も単独時220円/10分でミナミ随一の集客",
    },
    "JIS NAMBA": {
        "wait_fee_str": "330円 / 10分（待機時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（22時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（22時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "宗右衛門町・オリエンタル難波/心斎橋と徒歩3分で比較可能",
    },
    "OLG 京都": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日600円 / 金土700円",
        "late_10m_str": "平日800円 / 金土900円（24時〜990円）",
        "weekday_10m_str": "600円（22時〜800円）/ 10分",
        "weekend_10m_str": "700円（22時〜900円）/ 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,600円（22時〜4,800円）",
        "est_60m_weekend_label": "約4,200円（22時〜5,400円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "河原町・木屋町／22時前の早め入店なら600円/10分と割安",
    },
    "OLG 神戸": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日600円 / 金土700円",
        "late_10m_str": "平日800円 / 金土900円（24時〜990円）",
        "weekday_10m_str": "600円（22時〜800円）/ 10分",
        "weekend_10m_str": "700円（22時〜900円）/ 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,600円（22時〜4,800円）",
        "est_60m_weekend_label": "約4,200円（22時〜5,400円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "三宮駅前・神戸らしい上品なOL/コンサバ層が多い人気店",
    },
    # --- Nagoya / Chubu / Hokuriku ---
    "OLG 名古屋 錦": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日550円〜660円 / 金土770円〜880円",
        "late_10m_str": "平日770円 / 金土1,100円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "880円（22時〜1,100円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,300円〜3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約5,280円（22時〜6,600円）",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "錦三丁目のラグジュアリー店・2名以上限定で落ち着いた客層",
    },
    "OLG 名古屋 栄": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日600円〜660円 / 金土770円〜880円",
        "late_10m_str": "平日770円 / 金土990円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "880円（22時〜990円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,600円〜3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約5,280円（22時〜5,940円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "栄三丁目・錦店と異なり男性1名入店OK＆週末22時以降も990円",
    },
    "OLG ag名古屋": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "栄住吉・名古屋3店舗で週末料金が最も安くZ世代女性が集結",
    },
    "OLG 静岡": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日440円〜550円 / 金土550円〜660円",
        "late_10m_str": "平日660円〜770円 / 金土880円",
        "weekday_10m_str": "440円〜550円（22時〜660円）/ 10分",
        "weekend_10m_str": "550円〜660円（22時〜880円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約2,640円〜3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,300円〜3,960円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "呉服町・20時までの早割タイムは10分440円と驚異の低単価",
    },
    "OLG 浜松": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日440円〜550円 / 金土440円〜660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "440円〜550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約2,640円〜3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "鍛冶町・Open〜20時は10分440円！週末ピークも770円と良心的",
    },
    "OLG ag金沢": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土770円",
        "late_10m_str": "平日660円 / 金土880円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "北陸・金沢片町の唯一の大型相席ラウンジ／平日10分550円〜",
    },
    # --- Chugoku (Hiroshima / Okayama) ---
    "OLG ag広島": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日500円〜600円 / 金土600円〜700円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "500円〜600円（22時〜660円）/ 10分",
        "weekend_10m_str": "600円〜700円（22時〜770円）/ 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,000円〜3,600円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,600円〜4,200円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "流川・Z世代女性が集まる全国チェーン／20時迄は10分500円",
    },
    "ALFA HIROSHIMA": {
        "wait_fee_str": "0円〜220円 / 10分（待機時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日500円 / 金土600円",
        "late_10m_str": "平日600円 / 金土700円",
        "weekday_10m_str": "500円〜600円 / 10分（税込・飲み放題込）",
        "weekend_10m_str": "600円〜700円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,000円〜3,600円",
        "est_60m_weekend_label": "約3,600円〜4,200円",
        "solo_rule": "1名入店OK（ソロ参戦歓迎）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "新天地6-1／カラオケ個室＆10分500円〜の地元人気店",
    },
    "CLOVERS HIROSHIMA": {
        "wait_fee_str": "0円〜220円 / 10分（待機時）",
        "wait_10m_yen": 220,
        "early_10m_str": "日〜木22時迄 275円（半額）/ 金土600円",
        "late_10m_str": "平日550円 / 金土660円",
        "weekday_10m_str": "550円 / 10分（日〜木22時迄は半額275円）",
        "weekend_10m_str": "660円 / 10分",
        "est_60m_weekday": 2750,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約1,650円〜3,300円（日〜木22時迄半額）",
        "est_60m_weekend_label": "約3,960円",
        "solo_rule": "1名入店OK",
        "age_range": "20代〜30代前半",
        "vibe_short": "新天地1-9／日〜木22時迄の男性半額＆QUOカード企画が名物",
    },
    "OLG 岡山": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日600円 / 金土700円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "600円（22時〜660円）/ 10分",
        "weekend_10m_str": "700円（22時〜770円）/ 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,600円（22時〜3,960円）",
        "est_60m_weekend_label": "約4,200円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "岡山駅前・高級感ある内装と安定した女性集客",
    },
    "XIX OKAYAMA": {
        "wait_fee_str": "0円〜220円 / 10分（待機時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円（U29早割あり）/ 金土650円",
        "late_10m_str": "平日550円 / 金土650円",
        "weekday_10m_str": "550円 / 10分（U29早割あり）",
        "weekend_10m_str": "650円 / 10分（金曜20代定額あり）",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3900,
        "est_60m_weekday_label": "約3,300円（U29早割980円〜）",
        "est_60m_weekend_label": "約3,900円（金曜20代定額あり）",
        "solo_rule": "1名入店OK",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "岡山定番・U29平日早割（60分980円〜1,980円）が圧倒的お得",
    },
    # --- Kyushu & Okinawa ---
    "OLG 福岡": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日700円 / 金土800円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "700円（22時〜770円）/ 10分",
        "weekend_10m_str": "800円（22時〜880円）/ 10分",
        "est_60m_weekday": 4200,
        "est_60m_weekend": 4800,
        "est_60m_weekday_label": "約4,200円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,800円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "天神・大名の九州最大級店舗！常時女性数が全国トップクラス",
    },
    "JIS FUKUOKA": {
        "wait_fee_str": "220円〜330円 / 10分（待機時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日660円 / 金土770円",
        "late_10m_str": "平日770円 / 金土880円",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（22時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（22時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "天神大名・オリエンタル福岡と徒歩2分で男女比を比較可能",
    },
    "OLG 小倉": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日500円〜550円 / 金土550円〜660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "500円〜550円（22時〜660円）/ 10分",
        "weekend_10m_str": "550円〜660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,000円〜3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,300円〜3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "小倉魚町・天神より10分単価が150円〜200円安く高コスパ",
    },
    "OLG 熊本": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日500円〜600円 / 金土600円〜700円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "500円〜600円（22時〜660円）/ 10分",
        "weekend_10m_str": "600円〜700円（22時〜770円）/ 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,000円〜3,600円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,600円〜4,200円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "熊本下通・20時までの早割は10分500円！JIS熊本と隣接競合",
    },
    "JIS KUMAMOTO": {
        "wait_fee_str": "220円 / 10分（待機時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3850,
        "est_60m_weekend": 4510,
        "est_60m_weekday_label": "約3,850円（22時〜4,510円）",
        "est_60m_weekend_label": "約4,510円（22時〜5,170円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "熊本下通・オリエンタル熊本と徒歩1分でハシゴ比較が可能",
    },
    "OLG 長崎": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日440円〜550円 / 金土500円〜600円",
        "late_10m_str": "平日660円 / 金土700円",
        "weekday_10m_str": "440円〜550円（22時〜660円）/ 10分",
        "weekend_10m_str": "500円〜600円（22時〜700円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3600,
        "est_60m_weekday_label": "約2,640円〜3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,000円〜3,600円（22時〜4,200円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "思案橋・全国オリエンタルで週末単価が最安クラス（22時迄500円〜600円）",
    },
    "OLG 大分": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "大分都町・22時前入店なら平日60分3,300円とリーズナブル",
    },
    "OLG 宮崎": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "宮崎ニシタチ・女性比率が高く温暖でフレンドリーな客層",
    },
    "OLG 鹿児島": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日440円〜550円 / 金土600円〜700円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "440円〜550円（22時〜660円）/ 10分",
        "weekend_10m_str": "600円〜700円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約2,640円〜3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,600円〜4,200円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "鹿児島天文館・平日20時迄は10分440円の早割単価",
    },
    "OLG ag沖縄": {
        "wait_fee_str": "330円 / 10分（単独時）",
        "wait_10m_yen": 330,
        "early_10m_str": "平日660円 / 金土660円",
        "late_10m_str": "平日770円 / 金土770円（24時〜880円）",
        "weekday_10m_str": "660円（22時〜770円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円・24時〜880円）/ 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "那覇松山・平日と週末の基本単価が同額！地元＆観光女子が融合",
    },
    # --- Hokkaido / Tohoku / Shikoku ---
    "OLG ag札幌": {
        "wait_fee_str": "220円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円（U30定額あり）",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "すすきの・22時迄のU30定額プラン＆10分550円〜でJIS札幌と競合",
    },
    "JIS SAPPORO": {
        "wait_fee_str": "220円 / 10分（待機時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3850,
        "est_60m_weekend": 4510,
        "est_60m_weekday_label": "約3,850円（22時〜4,510円）",
        "est_60m_weekend_label": "約4,510円（22時〜5,170円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "すすきの最大級の大箱・25歳以上や社会人層にも強い定番店",
    },
    "OLG ag仙台": {
        "wait_fee_str": "220円〜330円 / 10分（単独時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日600円 / 金土770円",
        "late_10m_str": "平日660円〜770円 / 金土880円",
        "weekday_10m_str": "600円（22時〜660円）/ 10分",
        "weekend_10m_str": "770円（22時〜880円）/ 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,600円（22時〜3,960円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "仙台・国分町の東北最大店！週末は東北全域から女性が集結",
    },
    "JIS MATSUYAMA": {
        "wait_fee_str": "220円 / 10分（待機時）",
        "wait_10m_yen": 220,
        "early_10m_str": "平日550円 / 金土660円",
        "late_10m_str": "平日660円 / 金土770円",
        "weekday_10m_str": "550円（22時〜660円）/ 10分",
        "weekend_10m_str": "660円（22時〜770円）/ 10分",
        "est_60m_weekday": 3850,
        "est_60m_weekend": 4510,
        "est_60m_weekday_label": "約3,850円（22時〜4,510円）",
        "est_60m_weekend_label": "約4,510円（22時〜5,170円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "松山・大街道の四国唯一店／女性比率が全国屈指の高さ",
    },
}

# Major urban comparison clusters (covering all 51 domestic stores on /price-comparison)
COMPARISON_CLUSTERS = [
    {
        "id": "shinjuku",
        "tab_label": "🗼 新宿・西新宿（3店）",
        "title": "新宿・西新宿エリア（徒歩5分圏内・3店舗の料金＆イベント比較）",
        "desc": "東口（歌舞伎町）のオリエンタルラウンジ新宿・JIS新宿と、西口側のJIS西新宿が徒歩圏内で競合する日本一の激戦区。全店とも単独（待機）時は220円〜330円/10分がかかるため、「相席10分単価が安い＆アプリ会員チャージ無料のオリエンタル（2名以上必須）」か「1名入店OK＆男性割引プランが豊富なJIS（東口・西口）」かを現在の女性比率とあわせて比較するのが鉄則です。",
        "stores": ["OLG 新宿", "JIS SHINJUKU", "JIS NISHISHINJUKU"],
    },
    {
        "id": "shibuya",
        "tab_label": "✨ 渋谷・恵比寿（3店）",
        "title": "渋谷・恵比寿エリア（3店舗の店舗別料金＆イベント比較）",
        "desc": "同じオリエンタル系列でも店舗間で料金が大きく異なる代表エリア！週末ピーク（22時以降）の相席10分単価は「ag渋谷（990円）＜ 渋谷本店（1,100円）＜ 恵比寿（1,200円）」と階段状に変わり、さらに渋谷本店・恵比寿は男性1名入店不可（2名以上必須）、ag渋谷は1名入店OKという違いがあります。",
        "stores": ["OLG 渋谷本店", "OLG ag渋谷", "OLG 恵比寿"],
    },
    {
        "id": "osaka-kita",
        "tab_label": "🐙 梅田・茶屋町（6店）",
        "title": "大阪キタ（梅田・茶屋町・天満 6店舗の料金＆イベント比較）",
        "desc": "西日本最大の激戦区。オリエンタル大阪駅前（平日700円〜/10分）に対し、徒歩圏のag梅田（平日660円〜）や1駅隣の天満店（平日600円〜）、茶屋町のヤタコイ（平日550円〜）、さらにJIS 2店舗（梅田・茶屋町）まで、同じ時間帯でも10分単価と男女比が大きく開きます。",
        "stores": [
            "OLG 大阪駅前",
            "OLG ag梅田",
            "JIS UMEDA",
            "JIS CHAYAMACHI",
            "YATAKOI UMEDA",
            "OLG 天満",
        ],
    },
    {
        "id": "osaka-minami",
        "tab_label": "🥂 難波・心斎橋・京神（5店）",
        "title": "大阪ミナミ（心斎橋・難波）・京都・神戸エリア（5店舗比較）",
        "desc": "心斎橋・難波はオリエンタル2店舗（単独220円/10分〜）とJIS難波（待機330円/10分）が徒歩圏内。京都・神戸店も22時前の早め入店なら平日600円/10分（22時以降は800円/10分）と時間帯で大きく単価が変わります。",
        "stores": ["OLG 心斎橋", "OLG 難波", "JIS NAMBA", "OLG 京都", "OLG 神戸"],
    },
    {
        "id": "nagoya",
        "tab_label": "🏯 名古屋 栄・錦（6店）",
        "title": "名古屋（栄・錦）・静岡・浜松・金沢エリア（6店舗の料金＆イベント比較）",
        "desc": "名古屋の3店舗は「錦店（週末22時以降1,100円・1名不可）」「栄店（週末22時以降990円・1名OK）」「ag名古屋（週末22時以降880円・1名OK）」とルールが明確に分かれています。静岡・浜松は20時までの入店で10分440円という全国最安クラスの早割単価が魅力です。",
        "stores": ["OLG 名古屋 栄", "OLG 名古屋 錦", "OLG ag名古屋", "OLG 静岡", "OLG 浜松", "OLG ag金沢"],
    },
    {
        "id": "fukuoka",
        "tab_label": "🍜 福岡天神・九州（10店）",
        "title": "福岡（天神・小倉）・熊本・九州・沖縄エリア（10店舗の料金＆イベント比較）",
        "desc": "福岡・天神（大名）ではオリエンタル福岡とJIS福岡が徒歩2分で競合。また九州エリアは店舗別料金の差が大きく、天神（平日700円〜/10分）に対して小倉・熊本・長崎・大分・宮崎・鹿児島は平日440円〜550円/10分からと非常にリーズナブルな設定になっています。",
        "stores": [
            "OLG 福岡",
            "JIS FUKUOKA",
            "OLG 熊本",
            "JIS KUMAMOTO",
            "OLG 小倉",
            "OLG ag沖縄",
            "OLG 長崎",
            "OLG 大分",
            "OLG 宮崎",
            "OLG 鹿児島",
        ],
    },
    {
        "id": "ueno-east",
        "tab_label": "🏮 上野・錦糸町・町田（5店）",
        "title": "上野・錦糸町・町田・柏エリア（東京東＆近郊5店舗の料金比較）",
        "desc": "上野駅前にはオリエンタル上野とag上野が隣接し、どちらも1名入店OK。さらに錦糸町店は都内オリエンタルで最安水準の「平日550円 / 金土660円（22時前）」で飲める隠れ高コスパ店舗です。",
        "stores": ["OLG 上野", "OLG ag上野", "OLG 錦糸町", "OLG 町田", "OLG 柏"],
    },
    {
        "id": "omiya-yokohama",
        "tab_label": "🌉 大宮・横浜・北関東（5店）",
        "title": "大宮・横浜・宇都宮・高崎エリア（5店舗の料金＆イベント比較）",
        "desc": "大宮駅東口では「オリエンタル大宮（平日は22時以降も660円均一・アプリ会員チャージ0円）」と「JIS大宮」が至近距離で競合。宇都宮・高崎は平日550円/10分〜の北関東特別価格で営業しています。",
        "stores": ["OLG 大宮", "JIS OMIYA", "OLG 横浜", "OLG 宇都宮", "OLG 高崎"],
    },
    {
        "id": "hiroshima-okayama",
        "tab_label": "🍁 広島・岡山（5店）",
        "title": "広島（新天地・流川 3店舗）・岡山（2店舗）の料金＆開催中イベント比較",
        "desc": "広島・新天地（流川）エリアには全国チェーンの「ag（アグ）広島（20時迄500円/10分〜）」に加え、地元で圧倒的人気を誇る「相席ラウンジALFA広島」「相席CLOVERS広島（日〜木22時迄半額275円/10分）」の計3店舗が半径200m圏内に密集！岡山もオリエンタル岡山とXIX岡山（U29早割・金曜定額）の2店舗を同時検証可能です。",
        "stores": [
            "OLG ag広島",
            "ALFA HIROSHIMA",
            "CLOVERS HIROSHIMA",
            "OLG 岡山",
            "XIX OKAYAMA",
        ],
    },
    {
        "id": "sapporo-regional",
        "tab_label": "❄️ 札幌・仙台・松山（4店）",
        "title": "札幌（すすきの）・仙台（国分町）・松山エリア（4店舗の料金＆イベント比較）",
        "desc": "札幌すすきの（ag札幌・JIS札幌の2店が平日550円/10分〜で競合）、仙台国分町（ag仙台）、四国・松山（JIS松山）の店舗別料金と開催中イベント・現在の男女比を比較できます。",
        "stores": [
            "OLG ag札幌",
            "JIS SAPPORO",
            "OLG ag仙台",
            "JIS MATSUYAMA",
        ],
    },
]

_MEN_KEYWORDS = (
    "男性", "メンズ", "men", "早割", "半額", "off", "割引", "チャージ無料",
    "定額", "飲み放題", "フリーパス", "ハッピーアワー", "相席料金", "1人", "ソロ",
    "学割", "20代", "u-25", "u25", "u30", "u29", "ガチャ", "ビンゴ", "ハロウィン", "祭", "フェス",
)

_WOMEN_ONLY_KEYWORDS = (
    "女性限定", "女子会", "レディース", "コスメ", "ネイル", "美容", "スイーツ",
    "パフェ", "アイス", "ギフト券", "jcb", "タクシー代", "プレゼント",
)


def classify_event_for_comparison(ev: dict) -> str:
    """Return 'men_perk' if the event directly benefits men or is a shared venue event,
    or 'women_magnet' if it is a women-targeted attraction campaign."""
    title = (ev.get("title") or "").lower()
    if any(k in title for k in ("男性", "メンズ", "早割", "相席料金", "チャージ無料", "定額", "ハッピーアワー", "半額")):
        return "men_perk"
    if any(k in title for k in _WOMEN_ONLY_KEYWORDS):
        return "women_magnet"
    if any(k in title for k in _MEN_KEYWORDS):
        return "men_perk"
    return "women_magnet"


def _clean_event_title(raw_title: str) -> str:
    t = re.sub(r"\s+", " ", raw_title or "").strip()
    if len(t) > 52:
        return t[:50] + "…"
    return t


def get_store_price_profile(
    store_name: str,
    live_store: dict = None,
    all_events_Pool: list = None,
) -> dict:
    """Build a complete store-specific price + active events + live cost-performance verdict object."""
    info = store_master.get(store_name) or {}
    brand_raw = info.get("brand") or ("ag" if "ag" in store_name else store_name.split(" ")[0])
    if brand_raw == "YATAKOI":
        brand_raw = "ヤタコイ"
    base_spec = BRAND_PRICE_SPECS.get(brand_raw, BRAND_PRICE_SPECS["OLG"])
    store_override = STORE_PRICE_TABLE.get(store_name, {})

    # Merge brand baseline with official per-store price table
    charge_str = store_override.get("charge_str", base_spec["charge_str"])
    wait_10m_yen = store_override.get("wait_10m_yen", base_spec["wait_10m_yen"])
    wait_fee_str = store_override.get("wait_fee_str", base_spec["wait_fee_str"])
    has_wait_fee = bool(wait_10m_yen > 0 or base_spec.get("has_wait_fee", True))
    early_10m_str = store_override.get("early_10m_str", base_spec["weekday_10m_str"])
    late_10m_str = store_override.get("late_10m_str", base_spec["weekend_10m_str"])
    weekday_10m_str = store_override.get("weekday_10m_str", base_spec["weekday_10m_str"])
    weekend_10m_str = store_override.get("weekend_10m_str", base_spec["weekend_10m_str"])
    est_60m_weekday = store_override.get("est_60m_weekday", base_spec["est_60m_weekday"])
    est_60m_weekend = store_override.get("est_60m_weekend", base_spec["est_60m_weekend"])
    est_60m_weekday_label = store_override.get("est_60m_weekday_label", base_spec["est_60m_weekday_label"])
    est_60m_weekend_label = store_override.get("est_60m_weekend_label", base_spec["est_60m_weekend_label"])
    solo_rule = store_override.get("solo_rule", base_spec.get("solo_rule", "1名入店OK"))
    age_range = store_override.get("age_range", base_spec["age_range"])
    vibe_short = store_override.get("vibe_short", base_spec["vibe_short"])

    live_store = live_store or {}
    women = int(live_store.get("women") or 0)
    men = int(live_store.get("men") or 0)
    total = women + men
    wpct = round((women / total) * 100) if total > 0 else 0
    w_diff = int(live_store.get("women_diff") or 0)
    m_diff = int(live_store.get("men_diff") or 0)

    # Collect store-matched events non-blockingly using preloaded pool
    store_evs = []
    try:
        import events
        store_evs = events.get_events_for_store(
            store_name,
            store_only=False,
            allow_blocking=False,
            preloaded_events=all_events_Pool,
        )
    except Exception:
        store_evs = []

    men_perks = []
    women_events = []
    for ev in store_evs:
        cat = classify_event_for_comparison(ev)
        item = {
            "title": _clean_event_title(ev.get("title", "")),
            "full_title": ev.get("title", ""),
            "tag": ev.get("tag", "キャンペーン"),
            "url": ev.get("url", ""),
            "is_store_specific": bool(ev.get("store_name") or ev.get("target_stores")),
            "is_live_scraped": True,
        }
        if cat == "men_perk":
            men_perks.append(item)
        else:
            women_events.append(item)

    has_live_men_perk = len(men_perks) > 0
    has_live_women_event = len(women_events) > 0

    if not men_perks:
        men_perks.append({
            "title": base_spec["default_men_perk"],
            "full_title": base_spec["default_men_perk"],
            "tag": "常設特典",
            "url": "",
            "is_store_specific": False,
            "is_live_scraped": False,
        })
    if not women_events:
        women_events.append({
            "title": base_spec["default_women_perk"],
            "full_title": base_spec["default_women_perk"],
            "tag": "女性特典",
            "url": "",
            "is_store_specific": False,
            "is_live_scraped": False,
        })

    # Compute effective 60-min cost & live verdict
    # All major brands (OLG, ag, JIS) charge solo/waiting fee (220~330 yen / 10m) when men > women
    wait_penalty = 0
    if men > women and total > 0:
        wait_penalty = wait_10m_yen * 2  # 20 mins of waiting adds +440~660 yen

    effective_weekday_60m = est_60m_weekday + wait_penalty
    effective_weekend_60m = est_60m_weekend + wait_penalty

    # Score for ranking "Best Value Right Now" within an area cluster (higher is better)
    price_advantage = max(0, (5800 - effective_weekend_60m) // 30)
    event_bonus = 18 if has_live_men_perk else (10 if has_live_women_event else 0)
    if total > 0:
        ratio_score = (wpct - 50) * 1.8 + min(women, 45) * 1.1
        if women > men:
            ratio_score += 20
        elif men > women + 4:
            ratio_score -= 22
        value_score = round(price_advantage + event_bonus + ratio_score, 1)
    else:
        value_score = round(price_advantage + event_bonus, 1)

    # Human-readable real-time verification verdict reflecting per-store solo/wait & aiseki rates
    if total == 0:
        verdict_badge = "🌙 営業時間外・店舗別料金"
        verdict_cls = "verdict-neutral"
        verdict_comment = (
            f"単独（待機）{wait_fee_str}、60分相席目安は平日{est_60m_weekday_label}／金土{est_60m_weekend_label}（{solo_rule}）。"
            f"夕方オープン後に現在人数が自動反映されます。"
        )
    elif women >= men and women >= 8 and est_60m_weekend <= 4620:
        verdict_badge = "💎 低単価×女性優位で即相席"
        verdict_cls = "verdict-hot"
        verdict_comment = (
            f"現在女性{women}名（女性比{wpct}%）と女性リード中！単独（待機）料金（{wait_fee_str}）を払わず即相席しやすく、"
            f"10分単価も平日{weekday_10m_str}とエリア最安水準です。"
        )
    elif women >= men and women >= 8:
        verdict_badge = "🔥 即相席で単独待機ロス0円"
        verdict_cls = "verdict-hot"
        verdict_comment = (
            f"相席前の単独時は{wait_fee_str}がかかりますが、現在は女性{women}名（女性比{wpct}%）と女性優位のため"
            f"待機ロスなしで即相席しやすい絶好機です！"
        )
    elif men > women + 3:
        verdict_badge = "⚠️ 男性先行・単独待機料金に注意"
        verdict_cls = "verdict-warn"
        verdict_comment = (
            f"現在男性が+{men - women}名先行中。相席までの単独待機時間も{wait_fee_str}が加算されるため、"
            f"下の近隣ライバル店で女性比率が高い店舗へ回る方が実質総額を抑えられます。"
        )
    elif has_live_women_event and women >= 15 and wpct >= 55:
        verdict_badge = "✨ イベント集客効果◎（女性多数）"
        verdict_cls = "verdict-hot"
        verdict_comment = (
            f"キャンペーン効果で実際に女性{women}名（女性比{wpct}%）が来店中。"
            f"単独待機料金（{wait_fee_str}）のロスなくスムーズに座りやすい狙い目です。"
        )
    elif women > men:
        verdict_badge = f"⭕ 女性+{women - men}名リード・即相席圏内"
        verdict_cls = "verdict-good"
        verdict_comment = (
            f"現在女性{women}名／男性{men}名（女性比{wpct}%）。単独待機時間が発生しにくく、"
            f"相席料金（平日{weekday_10m_str}）どおりスマートに飲める好バランスです。"
        )
    else:
        verdict_badge = "💡 拮抗中・近隣店と単価比較推奨"
        verdict_cls = "verdict-neutral"
        verdict_comment = (
            f"現在女性{women}名／男性{men}名。単独待機時は{wait_fee_str}がかかるため、"
            f"即相席したい場合は同エリア内の女性リード店舗もチェックしてみてください。"
        )

    return {
        "name": store_name,
        "seo_name": store_master.seo_name(store_name),
        "short_name": info.get("display_name") or store_name,
        "slug": store_master.slug_of(store_name),
        "area_label": info.get("area_label") or "全国",
        "area_slug": info.get("area_slug") or "",
        "brand_code": base_spec["brand_code"],
        "brand_label": base_spec["brand_label"],
        "brand_slug": base_spec["brand_slug"],
        "women": women,
        "men": men,
        "total": total,
        "wpct": wpct,
        "women_diff": w_diff,
        "men_diff": m_diff,
        "charge_str": charge_str,
        "wait_10m_yen": wait_10m_yen,
        "wait_fee_str": wait_fee_str,
        "has_wait_fee": has_wait_fee,
        "early_10m_str": early_10m_str,
        "late_10m_str": late_10m_str,
        "weekday_10m_str": weekday_10m_str,
        "weekend_10m_str": weekend_10m_str,
        "est_60m_weekday": est_60m_weekday,
        "est_60m_weekend": est_60m_weekend,
        "est_60m_weekday_label": est_60m_weekday_label,
        "est_60m_weekend_label": est_60m_weekend_label,
        "solo_rule": solo_rule,
        "age_range": age_range,
        "vibe_short": vibe_short,
        "men_perks": men_perks[:2],
        "women_events": women_events[:2],
        "has_live_men_perk": has_live_men_perk,
        "has_live_women_event": has_live_women_event,
        "value_score": value_score,
        "verdict_badge": verdict_badge,
        "verdict_cls": verdict_cls,
        "verdict_comment": verdict_comment,
        "is_best_in_cluster": False,
    }


def build_area_comparison_clusters(live_stores: list = None) -> dict:
    """Build all 10 area comparison clusters + live event verification summary + full 51-store catalog."""
    live_map = {s.get("name"): s for s in (live_stores or [])}
    all_evs = []
    try:
        import events
        all_evs = events.fetch_all_events(allow_blocking=False)
    except Exception:
        all_evs = []

    clusters_out = []
    all_profiles_map = {}

    for cl in COMPARISON_CLUSTERS:
        profiles = []
        for sname in cl["stores"]:
            if not store_master.get(sname):
                continue
            prof = get_store_price_profile(
                sname,
                live_store=live_map.get(sname),
                all_events_Pool=all_evs,
            )
            profiles.append(prof)
            all_profiles_map[sname] = prof

        if profiles:
            best_prof = max(profiles, key=lambda x: (x["total"] > 0, x["value_score"], x["women"]))
            best_prof["is_best_in_cluster"] = True

        clusters_out.append({
            "id": cl["id"],
            "tab_label": cl["tab_label"],
            "title": cl["title"],
            "desc": cl["desc"],
            "stores": profiles,
            "best_store": best_prof if profiles else None,
        })

    # Ensure every domestic store in store_master is included in the full national price list
    all_domestic_profiles = []
    for sname in STORE_PRICE_TABLE.keys():
        if not store_master.get(sname):
            continue
        prof = all_profiles_map.get(sname)
        if not prof:
            prof = get_store_price_profile(sname, live_store=live_map.get(sname), all_events_Pool=all_evs)
            all_profiles_map[sname] = prof
        all_domestic_profiles.append(prof)

    # Build the Event Verification Board (stores with active scraped events + live crowd proof)
    verified_event_stores = [
        p for p in all_profiles_map.values()
        if p["has_live_men_perk"] or p["has_live_women_event"]
    ]
    verified_event_stores.sort(
        key=lambda x: (x["has_live_men_perk"], x["women"], x["wpct"]),
        reverse=True,
    )
    if len(verified_event_stores) < 6:
        fallback_extra = [
            p for p in sorted(all_profiles_map.values(), key=lambda x: (x["women"], x["value_score"]), reverse=True)
            if p not in verified_event_stores
        ]
        verified_event_stores.extend(fallback_extra[: (6 - len(verified_event_stores))])

    return {
        "clusters": clusters_out,
        "verified_event_stores": verified_event_stores[:8],
        "brand_specs": [BRAND_PRICE_SPECS["OLG"], BRAND_PRICE_SPECS["ag"], BRAND_PRICE_SPECS["JIS"]],
        "all_domestic_stores": all_domestic_profiles,
    }


def get_nearby_comparison_for_store(store_name: str, live_stores: list = None) -> dict:
    """Return the store's own price profile + all sibling stores in the same urban cluster
    for embedding directly on `/store/<slug>`."""
    live_map = {s.get("name"): s for s in (live_stores or [])}
    all_evs = []
    try:
        import events
        all_evs = events.fetch_all_events(allow_blocking=False)
    except Exception:
        all_evs = []

    target_cluster = None
    for cl in COMPARISON_CLUSTERS:
        if store_name in cl["stores"]:
            target_cluster = cl
            break

    if not target_cluster:
        self_prof = get_store_price_profile(store_name, live_store=live_map.get(store_name), all_events_Pool=all_evs)
        return {
            "self_profile": self_prof,
            "cluster_id": "shinjuku",
            "cluster_title": f"{self_prof['area_label']}エリアの料金・イベント比較",
            "stores": [self_prof],
        }

    profiles = []
    self_prof = None
    for sname in target_cluster["stores"]:
        if not store_master.get(sname):
            continue
        p = get_store_price_profile(sname, live_store=live_map.get(sname), all_events_Pool=all_evs)
        p["is_current_store"] = (sname == store_name)
        if sname == store_name:
            self_prof = p
        profiles.append(p)

    if profiles:
        best_p = max(profiles, key=lambda x: (x["total"] > 0, x["value_score"], x["women"]))
        best_p["is_best_in_cluster"] = True

    return {
        "self_profile": self_prof or (profiles[0] if profiles else None),
        "cluster_id": target_cluster["id"],
        "cluster_title": target_cluster["title"],
        "stores": profiles,
    }
