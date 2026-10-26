"""
price_comparison.py - Cross-Brand Lounge Price, Men's Event & Live Cost-Performance Comparison Engine.

Compares per-store official pricing across all 4 time slots:
  1. Open ~ 20:00 (slot_18_20)
  2. 20:00 ~ 22:00 (slot_20_22)
  3. 22:00 ~ 24:00 (slot_22_24)
  4. 24:00 ~ Close / Last (slot_24_last)
plus solo/waiting fee per 10 min, charge rules, 1-person solo entry policy, active men's discounts &
women's magnet events (from events.py), and real-time gender balance across all 51 domestic stores.
"""

import re
import datetime
import store_master
import store_hours

# Minimum female headcount thresholds to avoid over-valuing tiny sample sizes (e.g. 2 women / 0 men = 100%)
# where a guest may match once but have zero rotation/change options afterward.
MIN_WOMEN_FOR_RATIO_EVAL = 10  # ~5 pairs minimum to qualify for ratio bonus & cluster recommendation
MIN_WOMEN_FOR_TOP_PICK = 14    # ~7 pairs minimum for comfortable multi-table rotation depth

OFFICIAL_BRAND_URLS = {
    "OLG": "https://oriental-lounge.com/",
    "ag": "https://ag-ree.jp/",
    "JIS": "https://jis.bar/",
    "XIX": "https://xix-thelounge.com/",
    "ALFA": "https://aiseki-hiroshima.com/",
    "ヤタコイ": "https://yatakoi.com/",
    "CLOVERS": "https://bar-clovers.com/",
}

# Brand-level baseline price & vibe specifications (domestic stores)
BRAND_PRICE_SPECS = {
    "OLG": {
        "brand_code": "OLG",
        "brand_label": "オリエンタルラウンジ",
        "brand_slug": "oriental-lounge",
        "charge_yen": 550,
        "charge_str": "550円（公式アプリ会員0円）",
        "wait_10m_yen": 220,
        "wait_fee_str": "220円または330円 / 10分（店舗・曜日別／飲み放題込）",
        "has_wait_fee": True,
        "slot_18_20": "平日440円〜700円 / 金土440円〜880円",
        "slot_20_22": "平日550円〜700円 / 金土600円〜1,100円",
        "slot_22_24": "平日660円〜880円 / 金土700円〜1,200円",
        "slot_24_last": "平日660円〜990円 / 金土770円〜1,500円",
        "weekday_10m_yen": 660,
        "weekday_10m_str": "440円〜990円 / 10分（4時間帯・店舗別）",
        "weekend_10m_yen": 880,
        "weekend_10m_str": "440円〜1,500円 / 10分（4時間帯・店舗別）",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約2,640円〜5,940円",
        "est_60m_weekend_label": "約2,640円〜9,000円",
        "solo_rule": "店舗別（恵比寿・渋谷本店・新宿・大阪駅前・名古屋錦の5店は2名以上必須／他24店は+1,100円で1名OK）",
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
        "wait_fee_str": "220円または330円 / 10分（店舗・曜日別／飲み放題込）",
        "has_wait_fee": True,
        "slot_18_20": "平日440円〜660円 / 金土600円〜770円",
        "slot_20_22": "平日550円〜660円 / 金土660円〜880円",
        "slot_22_24": "平日660円〜770円 / 金土750円〜990円",
        "slot_24_last": "平日770円〜880円 / 金土880円〜990円",
        "weekday_10m_yen": 600,
        "weekday_10m_str": "440円〜880円 / 10分（4時間帯・店舗別）",
        "weekend_10m_yen": 770,
        "weekend_10m_str": "600円〜990円 / 10分（4時間帯・店舗別）",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約2,640円〜5,280円",
        "est_60m_weekend_label": "約3,600円〜5,940円",
        "solo_rule": "店舗別（渋谷・上野・名古屋は2名以上必須／仙台は平日のみ1名OK／梅田・金沢・広島・沖縄は+1,100円で1名OK）",
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
        "charge_str": "550円（テーブルチャージ／大宮U30は0円）",
        "wait_10m_yen": 330,
        "wait_fee_str": "220円または330円 / 10分（店舗別／飲み放題込）",
        "has_wait_fee": True,
        "slot_18_20": "平日550円〜770円 / 金土660円〜880円",
        "slot_20_22": "平日550円〜770円 / 金土660円〜990円",
        "slot_22_24": "平日550円〜880円 / 金土660円〜990円",
        "slot_24_last": "平日550円〜880円 / 金土660円〜990円",
        "weekday_10m_yen": 660,
        "weekday_10m_str": "550円〜880円 / 10分（時間帯・店舗別）",
        "weekend_10m_yen": 770,
        "weekend_10m_str": "660円〜990円 / 10分（時間帯・店舗別）",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約3,850円〜5,830円",
        "est_60m_weekend_label": "約4,510円〜6,490円",
        "solo_rule": "新宿店のみ2名以上推奨／他9店は1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "本格バー空間・店舗別の男性割引が豊富",
        "default_men_perk": "店舗別の男性早割・20代割・相席料金OFF・定額プラン等のキャンペーンが充実",
        "default_women_perk": "女性完全0円＋JCBギフト券・豪華プレゼント抽選・フード無料企画",
    },
    "XIX": {
        "brand_code": "XIX",
        "brand_label": "相席ラウンジ XIX",
        "brand_slug": "",
        "charge_yen": 550,
        "charge_str": "550円（LINE会員登録で0円）",
        "wait_10m_yen": 220,
        "wait_fee_str": "220円 / 10分（税込・飲み放題込）",
        "has_wait_fee": True,
        "slot_18_20": "平日500円 / 週末600円",
        "slot_20_22": "平日600円 / 週末700円",
        "slot_22_24": "平日660円 / 週末770円",
        "slot_24_last": "平日770円 / 週末880円",
        "weekday_10m_yen": 600,
        "weekday_10m_str": "500円〜770円 / 10分（入店時刻単価が退店迄適用）",
        "weekend_10m_yen": 700,
        "weekend_10m_str": "600円〜880円 / 10分（金土日・祝・祝前日）",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,000円〜4,620円（平日20代早割あり）",
        "est_60m_weekend_label": "約3,600円〜5,280円",
        "solo_rule": "1名入店OK",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "岡山エリア定番・入店時刻の単価が退店までずっと適用",
        "default_men_perk": "入店時間帯の単価が退店まで固定＆平日20代男性早割・定額キャンペーン",
        "default_women_perk": "女性完全無料＋豪華コスメ・テーマパーク券が当たる相席スクラッチ",
    },
    "ヤタコイ": {
        "brand_code": "ヤタコイ",
        "brand_label": "ヤタコイ（YATAKOI）",
        "brand_slug": "",
        "charge_yen": 0,
        "charge_str": "平日0円 / 金土祝前日500円",
        "wait_10m_yen": 0,
        "wait_fee_str": "定額パス料金に込み（時間内の追加待機課金なし）",
        "has_wait_fee": False,
        "is_flat_rate": True,
        "slot_18_20": "18〜19時 2,980円｜19〜20時 4,800円（定額・週末+500円）",
        "slot_20_22": "〜21時 4,800円｜21時〜 U24:4,800円 / 25歳〜:7,000円",
        "slot_22_24": "定額 U24:4,800円 / 25歳〜:7,000円（週末+500円）",
        "slot_24_last": "定額 U24:4,800円 / 25歳〜:7,000円（週末+500円）",
        "weekday_10m_yen": 0,
        "weekday_10m_str": "定額デイパス 2,980円〜7,000円（飲み放題込）",
        "weekend_10m_yen": 0,
        "weekend_10m_str": "定額デイパス 3,480円〜7,500円（週末チャージ込）",
        "est_60m_weekday": 4800,
        "est_60m_weekend": 5300,
        "est_60m_weekday_label": "定額2,980円〜4,800円（21時以降25歳〜7,000円）",
        "est_60m_weekend_label": "定額3,480円〜5,300円（21時以降25歳〜7,500円）",
        "solo_rule": "1名入店OK（定額デイパス制）",
        "age_range": "20代前半〜後半（男性35歳以下中心）",
        "vibe_short": "屋台×お祭り空間・10分課金ではなく安心の定額デイパス制",
        "default_men_perk": "18〜19時入店は定額2,980円！10分課金ではなく時間内は待機中も追加料金なし",
        "default_women_perk": "女性無料＋屋台フード＆ゲーム感覚で盛り上がれるお祭り空間",
    },
    "CLOVERS": {
        "brand_code": "CLOVERS",
        "brand_label": "相席CLOVERS広島",
        "brand_slug": "",
        "charge_yen": 660,
        "charge_str": "660円（公式Instagram登録で0円）",
        "wait_10m_yen": 220,
        "wait_fee_str": "220円 / 10分（税込・飲み放題込）",
        "has_wait_fee": True,
        "slot_18_20": "日〜木275円(半額) / 金土祝前660円",
        "slot_20_22": "日〜木275円(半額) / 金土祝前660円",
        "slot_22_24": "日〜木550円 / 金土祝前660円",
        "slot_24_last": "日〜木550円 / 金土祝前660円",
        "weekday_10m_yen": 550,
        "weekday_10m_str": "550円 / 10分（日〜木22時迄は半額275円）",
        "weekend_10m_yen": 660,
        "weekend_10m_str": "660円 / 10分（終日均一・税込）",
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
        "brand_slug": "",
        "charge_yen": 550,
        "charge_str": "550円（公式LINE登録で0円）",
        "wait_10m_yen": 220,
        "wait_fee_str": "220円 / 10分（税込・飲み放題込）",
        "has_wait_fee": True,
        "slot_18_20": "平日500円 / 金土祝前550円",
        "slot_20_22": "平日600円 / 金土祝前650円",
        "slot_22_24": "平日650円 / 金土祝前750円",
        "slot_24_last": "平日750円 / 金土祝前850円",
        "weekday_10m_yen": 600,
        "weekday_10m_str": "500円〜750円 / 10分（税込・飲み放題込）",
        "weekend_10m_yen": 650,
        "weekend_10m_str": "550円〜850円 / 10分（税込・飲み放題込）",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 3900,
        "est_60m_weekday_label": "約3,000円〜4,500円",
        "est_60m_weekend_label": "約3,300円〜5,100円",
        "solo_rule": "1名入店OK（シングルチャージ+1,000円・相席時+150円/10分）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "広島・新天地6-1（グランポルト5F）／カラオケ個室＆20時迄500円の好コスパ",
        "default_men_perk": "男性相席Open〜20時は10分500円（税込・飲み放題込）＆公式LINE登録でチャージ無料",
        "default_women_perk": "女性完全無料＋特製ビュッフェ・スイーツ食べ飲み放題＆カラオケ個室完備",
    },
}

# Per-store official price specifications for all 51 domestic stores across all 4 time slots:
#   slot_18_20: Open ~ 20:00 (10-min aiseki rate: weekday / Fri-Sat-Hol)
#   slot_20_22: 20:00 ~ 22:00 (10-min aiseki rate: weekday / Fri-Sat-Hol)
#   slot_22_24: 22:00 ~ 24:00 (10-min aiseki rate: weekday / Fri-Sat-Hol)
#   slot_24_last: 24:00 ~ Close (10-min aiseki rate: weekday / Fri-Sat-Hol)
STORE_PRICE_TABLE = {
    # --- Tokyo: Shibuya / Ebisu / Shinjuku ---
    "OLG 恵比寿": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日550円 / 金土880円",
        "slot_20_22": "平日660円 / 金土1,100円",
        "slot_22_24": "平日880円 / 金土1,200円",
        "slot_24_last": "平日990円 / 金土1,500円",
        "early_10m_str": "〜20時 550/880円｜20〜22時 660/1,100円",
        "late_10m_str": "22〜24時 880/1,200円｜24時〜 990/1,500円",
        "weekday_10m_str": "550円→660円→880円→990円 / 10分",
        "weekend_10m_str": "880円→1,100円→1,200円→1,500円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 6600,
        "est_60m_weekday_label": "約3,300円〜5,940円",
        "est_60m_weekend_label": "約5,280円〜9,000円",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代後半〜30代半ば",
        "vibe_short": "全国最高峰のハイクラス店・モデル/丸の内OL層",
    },
    "OLG 渋谷本店": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日550円 / 金土770円",
        "slot_20_22": "平日660円 / 金土880円",
        "slot_22_24": "平日770円 / 金土1,100円",
        "slot_24_last": "平日880円 / 金土1,200円",
        "early_10m_str": "〜20時 550/770円｜20〜22時 660/880円",
        "late_10m_str": "22〜24時 770/1,100円｜24時〜 880/1,200円",
        "weekday_10m_str": "550円→660円→770円→880円 / 10分",
        "weekend_10m_str": "770円→880円→1,100円→1,200円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,300円〜5,280円",
        "est_60m_weekend_label": "約4,620円〜7,200円",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "道玄坂の旗艦メガ店舗・常時大人数で高回転",
    },
    "OLG ag渋谷": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日440円 / 金土660円",
        "slot_20_22": "平日550円 / 金土770円",
        "slot_22_24": "平日660円 / 金土990円",
        "slot_24_last": "平日770円 / 金土990円",
        "early_10m_str": "〜20時 440/660円｜20〜22時 550/770円",
        "late_10m_str": "22〜24時 660/990円｜24時〜 770/990円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "660円→770円→990円→990円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約3,960円〜5,940円",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "19〜25歳中心（Z世代限定）",
        "vibe_short": "渋谷本店より全時間帯で10分110円〜210円安いZ世代特化店",
    },
    "OLG 新宿": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日550円 / 金土660円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土990円",
        "early_10m_str": "〜20時 550/660円｜20〜22時 660/770円",
        "late_10m_str": "22〜24時 770/880円｜24時〜 770/990円",
        "weekday_10m_str": "550円→660円→770円→770円 / 10分",
        "weekend_10m_str": "660円→770円→880円→990円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,300円〜4,620円",
        "est_60m_weekend_label": "約3,960円〜5,940円",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "歌舞伎町最大の動員数・渋谷/恵比寿より週末単価が安い",
    },
    "JIS SHINJUKU": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日770円 / 金土880円",
        "slot_20_22": "平日770円 / 金土880円",
        "slot_22_24": "平日880円 / 金土990円",
        "slot_24_last": "平日880円 / 金土990円",
        "early_10m_str": "Open〜22時 770/880円",
        "late_10m_str": "22時〜Last 880/990円",
        "weekday_10m_str": "770円→770円→880円→880円 / 10分",
        "weekend_10m_str": "880円→880円→990円→990円 / 10分",
        "est_60m_weekday": 5170,
        "est_60m_weekend": 5830,
        "est_60m_weekday_label": "約5,170円〜5,830円",
        "est_60m_weekend_label": "約5,830円〜6,490円",
        "solo_rule": "2名以上推奨（公式：新宿店のみ2名以上での利用お願い）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "歌舞伎町・本格バー空間（1名入店は西新宿店がスムーズ）",
    },
    "JIS NISHISHINJUKU": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日770円 / 金土880円（※20代は550/660円）",
        "slot_20_22": "平日770円 / 金土880円（※20代は550/660円）",
        "slot_22_24": "平日880円 / 金土990円（※20代は550/660円）",
        "slot_24_last": "平日880円 / 金土990円（※20代は550/660円）",
        "early_10m_str": "Open〜22時 770/880円（20代550/660円）",
        "late_10m_str": "22時〜Last 880/990円（20代550/660円）",
        "weekday_10m_str": "770円→770円→880円→880円 / 10分（20代550円）",
        "weekend_10m_str": "880円→880円→990円→990円 / 10分（20代660円）",
        "est_60m_weekday": 5170,
        "est_60m_weekend": 5830,
        "est_60m_weekday_label": "約5,170円〜5,830円（20代約3,850円）",
        "est_60m_weekend_label": "約5,830円〜6,490円（20代約4,510円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代半ば",
        "vibe_short": "新宿西口・1名入店OK＆20代男性は終日550/660円の特割あり",
    },
    # --- Tokyo East & Suburban Kanto ---
    "OLG 上野": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日550円 / 金土770円",
        "slot_20_22": "平日660円 / 金土880円",
        "slot_22_24": "平日770円 / 金土990円",
        "slot_24_last": "平日880円 / 金土990円",
        "early_10m_str": "〜20時 550/770円｜20〜22時 660/880円",
        "late_10m_str": "22〜24時 770/990円｜24時〜 880/990円",
        "weekday_10m_str": "550円→660円→770円→880円 / 10分",
        "weekend_10m_str": "770円→880円→990円→990円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,300円〜5,280円",
        "est_60m_weekend_label": "約4,620円〜5,940円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "上野仲町通り・社会人OLが多く1名入店も可能",
    },
    "OLG ag上野": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日550円 / 金土770円",
        "slot_20_22": "平日660円 / 金土880円",
        "slot_22_24": "平日770円 / 金土990円",
        "slot_24_last": "平日880円 / 金土990円",
        "early_10m_str": "〜20時 550/770円｜20〜22時 660/880円",
        "late_10m_str": "22〜24時 770/990円｜24時〜 880/990円",
        "weekday_10m_str": "550円→660円→770円→880円 / 10分",
        "weekend_10m_str": "770円→880円→990円→990円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,300円〜5,280円",
        "est_60m_weekend_label": "約4,620円〜5,940円",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "オリエンタル上野に隣接・20代前半の女子大生/若手中心（1名ならオリエンタル上野へ）",
    },
    "OLG 錦糸町": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日440円 / 金土550円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 440/550円｜20〜22時 550/660円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "550円→660円→770円→880円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約3,300円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "都内オリエンタル最安！20時前440円・20〜22時550円の穴場",
    },
    "OLG 町田": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日550円 / 金土660円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日660円 / 金土880円",
        "slot_24_last": "平日770円 / 金土990円",
        "early_10m_str": "〜20時 550/660円｜20〜22時 660/770円",
        "late_10m_str": "22〜24時 660/880円｜24時〜 770/990円",
        "weekday_10m_str": "550円→660円→660円→770円 / 10分",
        "weekend_10m_str": "660円→770円→880円→990円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,300円〜4,620円",
        "est_60m_weekend_label": "約3,960円〜5,940円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "西東京・神奈川北部の拠点／平日は24時まで660円据え置き",
    },
    "OLG 柏": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日550円 / 金土660円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日660円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 550/660円｜20〜22時 660/770円",
        "late_10m_str": "22〜24時 660/880円｜24時〜 770/880円",
        "weekday_10m_str": "550円→660円→660円→770円 / 10分",
        "weekend_10m_str": "660円→770円→880円→880円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,300円〜4,620円",
        "est_60m_weekend_label": "約3,960円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "常磐線・千葉北西部の中心店／単独時220円＆20時前550円",
    },
    "OLG 横浜": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日550円 / 金土660円",
        "slot_20_22": "平日660円 / 金土880円",
        "slot_22_24": "平日770円 / 金土990円",
        "slot_24_last": "平日880円 / 金土1,100円",
        "early_10m_str": "〜20時 550/660円｜20〜22時 660/880円",
        "late_10m_str": "22〜24時 770/990円｜24時〜 880/1,100円",
        "weekday_10m_str": "550円→660円→770円→880円 / 10分",
        "weekend_10m_str": "660円→880円→990円→1,100円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,300円〜5,280円",
        "est_60m_weekend_label": "約3,960円〜6,600円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "横浜駅西口・神奈川トップの集客力と華やかな客層",
    },
    "OLG 大宮": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日550円 / 金土660円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日660円 / 金土880円",
        "slot_24_last": "平日770円 / 金土990円",
        "early_10m_str": "〜20時 550/660円｜20〜22時 660/770円",
        "late_10m_str": "22〜24時 660/880円｜24時〜 770/990円",
        "weekday_10m_str": "550円→660円→660円→770円 / 10分",
        "weekend_10m_str": "660円→770円→880円→990円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,300円〜4,620円",
        "est_60m_weekend_label": "約3,960円〜5,940円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "北関東・埼玉最大の集客力！平日は24時まで660円据え置き",
    },
    "JIS OMIYA": {
        "charge_str": "550円（U30はチャージ0円）",
        "wait_fee_str": "220円 / 10分（1名入店時は単独330円/10分）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日660円 / 金土880円（〜21時）",
        "slot_20_22": "平日660円(21時〜770円) / 金土880円(21時〜990円)",
        "slot_22_24": "平日770円 / 金土990円",
        "slot_24_last": "平日770円 / 金土990円",
        "early_10m_str": "Open〜21時 660/880円",
        "late_10m_str": "21時〜Close 770/990円",
        "weekday_10m_str": "〜21時660円 / 21時〜770円（10分）",
        "weekend_10m_str": "〜21時880円 / 21時〜990円（10分）",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5830,
        "est_60m_weekday_label": "約4,510円〜5,170円",
        "est_60m_weekend_label": "約5,830円〜6,490円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "大宮駅東口・21時境目の2段階料金＆U30チャージ0円企画あり",
    },
    "OLG 宇都宮": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日440円 / 金土550円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 440/550円｜20〜22時 550/660円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "550円→660円→770円→880円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約3,300円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "オリオン通り・20時前440円〜の北関東リーズナブル設定",
    },
    "OLG 高崎": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日440円 / 金土550円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 440/550円｜20〜22時 550/660円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "550円→660円→770円→880円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約3,300円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "高崎駅西口・20時前440円、20〜22時550円で地元女性と相席",
    },
    # --- Osaka / Kyoto / Kobe (Kansai) ---
    "OLG 大阪駅前": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日700円 / 金土800円",
        "slot_20_22": "平日700円 / 金土800円",
        "slot_22_24": "平日880円 / 金土990円",
        "slot_24_last": "平日880円 / 金土990円",
        "early_10m_str": "Open〜22時 700/800円",
        "late_10m_str": "22時〜Close 880/990円",
        "weekday_10m_str": "700円→700円→880円→880円 / 10分",
        "weekend_10m_str": "800円→800円→990円→990円 / 10分",
        "est_60m_weekday": 4200,
        "est_60m_weekend": 4800,
        "est_60m_weekday_label": "約4,200円（22時〜5,280円）",
        "est_60m_weekend_label": "約4,800円（22時〜5,940円）",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "梅田・お初天神の王道大型店／2名以上限定で綺麗め社会人OL層が中心",
    },
    "OLG ag梅田": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日660円 / 金土770円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土990円",
        "early_10m_str": "Open〜22時 660/770円",
        "late_10m_str": "22〜24時 770/880円｜24時〜 770/990円",
        "weekday_10m_str": "660円→660円→770円→770円 / 10分",
        "weekend_10m_str": "770円→770円→880円→990円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円〜4,620円",
        "est_60m_weekend_label": "約4,620円〜5,940円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "阪急東通り・1名入店OK＆大阪駅前店より全時間帯で安くZ世代が密集",
    },
    "OLG 天満": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日600円 / 金土700円",
        "slot_20_22": "平日600円 / 金土700円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "Open〜22時 600/700円",
        "late_10m_str": "22時〜Close 770/880円",
        "weekday_10m_str": "600円→600円→770円→770円 / 10分",
        "weekend_10m_str": "700円→700円→880円→880円 / 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,600円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,200円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "梅田から1駅・1名入店OK＆大阪駅前店より10分あたり100円以上安い穴場",
    },
    "JIS UMEDA": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日660円 / 金土770円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "Open〜22時 660/770円",
        "late_10m_str": "22時〜Last 770/880円",
        "weekday_10m_str": "660円→660円→770円→770円 / 10分",
        "weekend_10m_str": "770円→770円→880円→880円 / 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（22時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（22時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "お初天神・オリエンタル大阪駅前と徒歩1分で比較可能（1名入店OK）",
    },
    "JIS CHAYAMACHI": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日660円 / 金土770円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "Open〜22時 660/770円",
        "late_10m_str": "22時〜Last 770/880円",
        "weekday_10m_str": "660円→660円→770円→770円 / 10分",
        "weekend_10m_str": "770円→770円→880円→880円 / 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（22時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（22時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "阪急茶屋町・ファッション/美容系女子が多い洗練空間",
    },
    "YATAKOI UMEDA": {
        "charge_str": "平日0円 / 金土祝前日500円",
        "wait_fee_str": "定額パス料金に込み（時間内の追加待機課金なし）",
        "wait_10m_yen": 0,
        "has_wait_fee": False,
        "is_flat_rate": True,
        "slot_18_20": "18〜19時 2,980円｜19〜20時 4,800円（定額・週末+500円）",
        "slot_20_22": "〜21時 4,800円｜21時〜 U24:4,800円 / 25歳〜:7,000円",
        "slot_22_24": "定額 U24:4,800円 / 25歳〜:7,000円（週末+500円）",
        "slot_24_last": "定額 U24:4,800円 / 25歳〜:7,000円（週末+500円）",
        "early_10m_str": "18〜19時入店 定額2,980円｜〜21時 4,800円",
        "late_10m_str": "21時〜 定額 U24:4,800円 / 25歳〜:7,000円",
        "weekday_10m_str": "定額デイパス 2,980円〜7,000円（飲み放題込）",
        "weekend_10m_str": "定額デイパス 3,480円〜7,500円（週末チャージ込）",
        "est_60m_weekday": 4800,
        "est_60m_weekend": 5300,
        "est_60m_weekday_label": "定額2,980円〜4,800円（21時以降25歳〜7,000円）",
        "est_60m_weekend_label": "定額3,480円〜5,300円（21時以降25歳〜7,500円）",
        "solo_rule": "1名入店OK（定額デイパス制）",
        "age_range": "20代前半〜後半（男性35歳以下中心）",
        "vibe_short": "茶屋町・10分課金ではなく定額デイパス制！18〜19時入店は2,980円",
    },
    "OLG 心斎橋": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日600円 / 金土660円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 600/660円｜20〜22時 660/770円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "600円→660円→660円→770円 / 10分",
        "weekend_10m_str": "660円→770円→770円→880円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,600円〜4,620円",
        "est_60m_weekend_label": "約3,960円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "東心斎橋・24時まで平日660円/金土770円据え置き＆20時迄600/660円",
    },
    "OLG 難波": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日660円 / 金土770円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "Open〜22時 660/770円",
        "late_10m_str": "22時〜Close 770/880円",
        "weekday_10m_str": "660円→660円→770円→770円 / 10分",
        "weekend_10m_str": "770円→770円→880円→880円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,620円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "道頓堀すぐ・週末も単独時220円/10分でミナミ随一の集客",
    },
    "JIS NAMBA": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日660円 / 金土770円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "Open〜22時 660/770円",
        "late_10m_str": "22時〜Last 770/880円",
        "weekday_10m_str": "660円→660円→770円→770円 / 10分",
        "weekend_10m_str": "770円→770円→880円→880円 / 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（22時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（22時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "宗右衛門町・オリエンタル難波/心斎橋と徒歩3分で比較可能（U29割あり）",
    },
    "OLG 京都": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日500円 / 金土600円",
        "slot_20_22": "平日600円 / 金土700円",
        "slot_22_24": "平日800円 / 金土900円",
        "slot_24_last": "平日880円 / 金土990円",
        "early_10m_str": "〜20時 500/600円｜20〜22時 600/700円",
        "late_10m_str": "22〜24時 800/900円｜24時〜 880/990円",
        "weekday_10m_str": "500円→600円→800円→880円 / 10分",
        "weekend_10m_str": "600円→700円→900円→990円 / 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,000円〜5,280円",
        "est_60m_weekend_label": "約3,600円〜5,940円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "河原町・木屋町／20時前500円・22時前600円と早め入店が超割安",
    },
    "OLG 神戸": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日600円 / 金土700円",
        "slot_20_22": "平日600円 / 金土700円",
        "slot_22_24": "平日800円 / 金土900円",
        "slot_24_last": "平日880円 / 金土990円",
        "early_10m_str": "Open〜22時 600/700円",
        "late_10m_str": "22〜24時 800/900円｜24時〜 880/990円",
        "weekday_10m_str": "600円→600円→800円→880円 / 10分",
        "weekend_10m_str": "700円→700円→900円→990円 / 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,600円〜5,280円",
        "est_60m_weekend_label": "約4,200円〜5,940円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "三宮駅前・神戸らしい上品なOL/コンサバ層が多い人気店",
    },
    # --- Nagoya / Chubu / Hokuriku ---
    "OLG 名古屋 錦": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日550円 / 金土770円",
        "slot_20_22": "平日660円 / 金土880円",
        "slot_22_24": "平日770円 / 金土1,100円",
        "slot_24_last": "平日880円 / 金土1,200円",
        "early_10m_str": "〜20時 550/770円｜20〜22時 660/880円",
        "late_10m_str": "22〜24時 770/1,100円｜24時〜 880/1,200円",
        "weekday_10m_str": "550円→660円→770円→880円 / 10分",
        "weekend_10m_str": "770円→880円→1,100円→1,200円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,300円〜5,280円",
        "est_60m_weekend_label": "約4,620円〜7,200円",
        "solo_rule": "2名以上必須（男性1名入店不可）",
        "age_range": "20代半ば〜30代前半",
        "vibe_short": "錦三丁目のラグジュアリー店・2名以上限定で落ち着いた客層",
    },
    "OLG 名古屋 栄": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日600円 / 金土770円",
        "slot_20_22": "平日660円 / 金土880円",
        "slot_22_24": "平日770円 / 金土990円",
        "slot_24_last": "平日880円 / 金土1,100円",
        "early_10m_str": "〜20時 600/770円｜20〜22時 660/880円",
        "late_10m_str": "22〜24時 770/990円｜24時〜 880/1,100円",
        "weekday_10m_str": "600円→660円→770円→880円 / 10分",
        "weekend_10m_str": "770円→880円→990円→1,100円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 5280,
        "est_60m_weekday_label": "約3,600円〜5,280円",
        "est_60m_weekend_label": "約4,620円〜6,600円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "栄三丁目・名古屋3店舗で唯一男性1名入店OK！週末22時以降も錦より割安",
    },
    "OLG ag名古屋": {
        "wait_fee_str": "平日220円 / 金土330円（10分）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日660円 / 金土770円（19時Open）",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日880円 / 金土990円",
        "early_10m_str": "Open(19時)〜22時 660/770円",
        "late_10m_str": "22〜24時 770/880円｜24時〜 880/990円",
        "weekday_10m_str": "660円→660円→770円→880円 / 10分",
        "weekend_10m_str": "770円→770円→880円→990円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約3,960円〜5,280円",
        "est_60m_weekend_label": "約4,620円〜5,940円",
        "solo_rule": "2名以上必須（男性1名入店不可・栄店へ案内）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "栄住吉・名古屋3店舗で週末20時以降の料金が最安＆Z世代女性が集結",
    },
    "OLG 静岡": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日440円 / 金土550円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日660円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 440/550円｜20〜22時 550/660円",
        "late_10m_str": "22〜24時 660/880円｜24時〜 770/880円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "550円→660円→880円→880円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約3,300円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "呉服町・20時までの早割タイムは10分440円と驚異の低単価",
    },
    "OLG 浜松": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日440円 / 金土440円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日660円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 440/440円｜20〜22時 550/660円",
        "late_10m_str": "22〜24時 660/880円｜24時〜 770/880円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "440円→660円→880円→880円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約2,640円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "鍛冶町・Open〜20時は週末でも10分440円均一！",
    },
    "OLG ag金沢": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日440円 / 金土700円",
        "slot_20_22": "平日550円 / 金土770円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 440/700円｜20〜22時 550/770円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "700円→770円→770円→880円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4620,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約4,200円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "北陸・金沢片町の唯一の大型相席ラウンジ／20時前440円〜",
    },
    # --- Chugoku (Hiroshima / Okayama) ---
    "OLG ag広島": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日500円 / 金土600円",
        "slot_20_22": "平日600円 / 金土700円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 500/600円｜20〜22時 600/700円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "500円→600円→660円→770円 / 10分",
        "weekend_10m_str": "600円→700円→770円→880円 / 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,000円〜4,620円",
        "est_60m_weekend_label": "約3,600円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半（19〜26歳中心）",
        "vibe_short": "流川・Z世代女性が集まる全国チェーン／20時迄は10分500円",
    },
    "ALFA HIROSHIMA": {
        "charge_str": "550円（公式LINE登録で0円）",
        "wait_fee_str": "220円 / 10分（税込・飲み放題込）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日500円 / 金土祝前550円",
        "slot_20_22": "平日600円 / 金土祝前650円",
        "slot_22_24": "平日650円 / 金土祝前750円",
        "slot_24_last": "平日750円 / 金土祝前850円",
        "early_10m_str": "〜20時 500/550円｜20〜22時 600/650円",
        "late_10m_str": "22〜24時 650/750円｜24時〜 750/850円",
        "weekday_10m_str": "500円→600円→650円→750円 / 10分",
        "weekend_10m_str": "550円→650円→750円→850円 / 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 3900,
        "est_60m_weekday_label": "約3,000円〜4,500円",
        "est_60m_weekend_label": "約3,300円〜5,100円",
        "solo_rule": "1名入店OK（シングルチャージ+1,000円・相席時+150円/10分）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "新天地6-1／カラオケ個室＆20時迄10分500円〜の地元人気店",
    },
    "CLOVERS HIROSHIMA": {
        "charge_str": "660円（公式Instagram登録で0円）",
        "wait_fee_str": "220円 / 10分（税込・飲み放題込）",
        "wait_10m_yen": 220,
        "slot_18_20": "日〜木275円(半額) / 金土祝前660円",
        "slot_20_22": "日〜木275円(半額) / 金土祝前660円",
        "slot_22_24": "日〜木550円 / 金土祝前660円",
        "slot_24_last": "日〜木550円 / 金土祝前660円",
        "early_10m_str": "日〜木22時迄 275円(半額) / 金土祝前660円",
        "late_10m_str": "22時〜Last 550/660円",
        "weekday_10m_str": "275円(半額)→275円→550円→550円 / 10分",
        "weekend_10m_str": "660円 / 10分（終日均一・税込）",
        "est_60m_weekday": 2750,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約1,650円〜3,300円（22時迄半額）",
        "est_60m_weekend_label": "約3,960円",
        "solo_rule": "1名入店OK",
        "age_range": "20代〜30代前半",
        "vibe_short": "新天地1-9／日〜木22時迄の男性半額＆QUOカード企画が名物",
    },
    "OLG 岡山": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日500円 / 金土600円",
        "slot_20_22": "平日600円 / 金土700円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 500/600円｜20〜22時 600/700円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "500円→600円→660円→770円 / 10分",
        "weekend_10m_str": "600円→700円→770円→880円 / 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,000円〜4,620円",
        "est_60m_weekend_label": "約3,600円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "岡山駅前・20時前500円〜で高級感ある内装と安定集客",
    },
    "XIX OKAYAMA": {
        "charge_str": "550円（LINE会員登録で0円）",
        "wait_fee_str": "220円 / 10分（税込・飲み放題込）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日500円 / 週末600円（※平日20代早割あり）",
        "slot_20_22": "平日600円 / 週末700円（※平日20代早割あり）",
        "slot_22_24": "平日660円 / 週末770円",
        "slot_24_last": "平日770円 / 週末880円",
        "early_10m_str": "〜20時 500/600円｜20〜22時 600/700円（入店時刻単価が退店迄固定）",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "500円→600円→660円→770円 / 10分（入店時刻単価固定）",
        "weekend_10m_str": "600円→700円→770円→880円 / 10分（金土日・祝・祝前日）",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,000円〜4,620円（平日20代早割あり）",
        "est_60m_weekend_label": "約3,600円〜5,280円",
        "solo_rule": "1名入店OK",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "岡山定番・入店時刻の単価が退店まで固定＆平日20代早割がお得",
    },
    # --- Kyushu & Okinawa ---
    "OLG 福岡": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日700円 / 金土800円",
        "slot_20_22": "平日700円 / 金土800円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "Open〜22時 700/800円",
        "late_10m_str": "22時〜Close 770/880円",
        "weekday_10m_str": "700円→700円→770円→770円 / 10分",
        "weekend_10m_str": "800円→800円→880円→880円 / 10分",
        "est_60m_weekday": 4200,
        "est_60m_weekend": 4800,
        "est_60m_weekday_label": "約4,200円（22時〜4,620円）",
        "est_60m_weekend_label": "約4,800円（22時〜5,280円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "天神・大名の九州最大級店舗！常時女性数が全国トップクラス",
    },
    "JIS FUKUOKA": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日770円 / 金土880円",
        "slot_20_22": "平日770円 / 金土880円",
        "slot_22_24": "平日770円 / 金土880円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "Open〜22時 770/880円",
        "late_10m_str": "22時〜Last 770/880円",
        "weekday_10m_str": "770円 / 10分（終日均一）",
        "weekend_10m_str": "880円 / 10分（終日均一）",
        "est_60m_weekday": 5170,
        "est_60m_weekend": 5830,
        "est_60m_weekday_label": "約5,170円（終日均一）",
        "est_60m_weekend_label": "約5,830円（終日均一）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "天神大名・22時以降も単価が上がらない終日フラット料金",
    },
    "OLG 小倉": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日500円 / 金土550円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日660円 / 金土880円",
        "early_10m_str": "〜20時 500/550円｜20〜22時 550/660円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 660/880円",
        "weekday_10m_str": "500円→550円→660円→660円 / 10分",
        "weekend_10m_str": "550円→660円→770円→880円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,000円〜3,960円",
        "est_60m_weekend_label": "約3,300円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "小倉魚町・天神より10分単価が150円〜200円安く高コスパ",
    },
    "OLG 熊本": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日500円 / 金土600円",
        "slot_20_22": "平日600円 / 金土700円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 500/600円｜20〜22時 600/700円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "500円→600円→660円→770円 / 10分",
        "weekend_10m_str": "600円→700円→770円→880円 / 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約3,000円〜4,620円",
        "est_60m_weekend_label": "約3,600円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "熊本下通・20時までの早割は10分500円！JIS熊本と隣接競合",
    },
    "JIS KUMAMOTO": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日660円 / 金土770円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "Open〜24時 660/770円",
        "late_10m_str": "24時〜Last 770/880円",
        "weekday_10m_str": "660円→660円→660円→770円 / 10分",
        "weekend_10m_str": "770円→770円→770円→880円 / 10分",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（24時〜5,170円）",
        "est_60m_weekend_label": "約5,170円（24時〜5,830円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "熊本下通・24時まで単価が上がらない＆20代半額企画あり",
    },
    "OLG 長崎": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日440円 / 金土500円",
        "slot_20_22": "平日550円 / 金土600円",
        "slot_22_24": "平日660円 / 金土700円",
        "slot_24_last": "平日770円 / 金土800円",
        "early_10m_str": "〜20時 440/500円｜20〜22時 550/600円",
        "late_10m_str": "22〜24時 660/700円｜24時〜 770/800円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "500円→600円→700円→800円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3600,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約3,000円〜4,800円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "思案橋・全国オリエンタルで週末単価が最安クラス（22時迄500円〜600円）",
    },
    "OLG 大分": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日550円 / 金土660円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日660円 / 金土770円",
        "early_10m_str": "Open〜22時 550/660円",
        "late_10m_str": "22時〜Close 660/770円",
        "weekday_10m_str": "550円→550円→660円→660円 / 10分",
        "weekend_10m_str": "660円→660円→770円→770円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "大分都町・24時以降も単価が上がらない明朗2段階システム",
    },
    "OLG 宮崎": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日550円 / 金土660円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日660円 / 金土770円",
        "early_10m_str": "Open〜22時 550/660円",
        "late_10m_str": "22時〜Close 660/770円",
        "weekday_10m_str": "550円→550円→660円→660円 / 10分",
        "weekend_10m_str": "660円→660円→770円→770円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,300円（22時〜3,960円）",
        "est_60m_weekend_label": "約3,960円（22時〜4,620円）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "宮崎ニシタチ・22時迄550/660円、深夜も660/770円据え置き",
    },
    "OLG 鹿児島": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日440円 / 金土600円",
        "slot_20_22": "平日550円 / 金土700円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日770円 / 金土880円",
        "early_10m_str": "〜20時 440/600円｜20〜22時 550/700円",
        "late_10m_str": "22〜24時 660/770円｜24時〜 770/880円",
        "weekday_10m_str": "440円→550円→660円→770円 / 10分",
        "weekend_10m_str": "600円→700円→770円→880円 / 10分",
        "est_60m_weekday": 3300,
        "est_60m_weekend": 4200,
        "est_60m_weekday_label": "約2,640円〜4,620円",
        "est_60m_weekend_label": "約3,600円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "鹿児島天文館・平日20時迄は10分440円の早割単価",
    },
    "OLG ag沖縄": {
        "wait_fee_str": "330円 / 10分（平日・週末とも）",
        "wait_10m_yen": 330,
        "slot_18_20": "平日660円 / 金土660円",
        "slot_20_22": "平日660円 / 金土660円",
        "slot_22_24": "平日770円 / 金土770円",
        "slot_24_last": "平日880円 / 金土880円",
        "early_10m_str": "Open〜22時 660/660円",
        "late_10m_str": "22〜24時 770/770円｜24時〜 880/880円",
        "weekday_10m_str": "660円→660円→770円→880円 / 10分",
        "weekend_10m_str": "660円→660円→770円→880円 / 10分",
        "est_60m_weekday": 3960,
        "est_60m_weekend": 3960,
        "est_60m_weekday_label": "約3,960円〜5,280円",
        "est_60m_weekend_label": "約3,960円〜5,280円",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜後半",
        "vibe_short": "那覇松山・平日と週末が完全同額！時間帯のみで3段階変動",
    },
    # --- Hokkaido / Tohoku / Shikoku ---
    "JIS SAPPORO": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日660円 / 金土770円",
        "slot_20_22": "平日660円 / 金土770円",
        "slot_22_24": "平日660円 / 金土770円",
        "slot_24_last": "平日660円 / 金土770円",
        "early_10m_str": "終日均一 平日660円 / 金土770円",
        "late_10m_str": "終日均一 平日660円 / 金土770円",
        "weekday_10m_str": "660円 / 10分（終日均一）",
        "weekend_10m_str": "770円 / 10分（終日均一）",
        "est_60m_weekday": 4510,
        "est_60m_weekend": 5170,
        "est_60m_weekday_label": "約4,510円（終日均一）",
        "est_60m_weekend_label": "約5,170円（終日均一）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "すすきの最大級の大箱・深夜帯も単価が上がらない終日フラット料金",
    },
    "OLG ag仙台": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日550円 / 金土700円",
        "slot_20_22": "平日600円 / 金土750円",
        "slot_22_24": "平日700円 / 金土750円",
        "slot_24_last": "平日770円 / 金土990円",
        "early_10m_str": "〜20時 550/700円｜20〜22時 600/750円",
        "late_10m_str": "22〜24時 700/750円｜24時〜 770/990円",
        "weekday_10m_str": "550円→600円→700円→770円 / 10分",
        "weekend_10m_str": "700円→750円→750円→990円 / 10分",
        "est_60m_weekday": 3600,
        "est_60m_weekend": 4500,
        "est_60m_weekday_label": "約3,300円〜4,620円",
        "est_60m_weekend_label": "約4,200円〜5,940円",
        "solo_rule": "平日のみ1名OK（シングルチャージ+1,100円）／週末は2名以上必須",
        "age_range": "20代前半〜後半",
        "vibe_short": "仙台・国分町の東北最大店！週末20〜24時は750円据え置き",
    },
    "JIS MATSUYAMA": {
        "wait_fee_str": "220円 / 10分（平日・週末とも）",
        "wait_10m_yen": 220,
        "slot_18_20": "平日550円 / 金土660円",
        "slot_20_22": "平日550円 / 金土660円",
        "slot_22_24": "平日550円 / 金土660円",
        "slot_24_last": "平日550円 / 金土660円",
        "early_10m_str": "終日均一 平日550円 / 金土660円",
        "late_10m_str": "終日均一 平日550円 / 金土660円",
        "weekday_10m_str": "550円 / 10分（終日均一）",
        "weekend_10m_str": "660円 / 10分（終日均一）",
        "est_60m_weekday": 3850,
        "est_60m_weekend": 4510,
        "est_60m_weekday_label": "約3,850円（終日均一）",
        "est_60m_weekend_label": "約4,510円（終日均一）",
        "solo_rule": "1名入店OK（シングルチャージ+1,100円）",
        "age_range": "20代前半〜30代前半",
        "vibe_short": "松山・大街道の四国唯一店／終日550/660円均一＆女性比率が全国屈指",
    },
}

# Major urban comparison clusters (covering all 51 domestic stores on /price-comparison)
COMPARISON_CLUSTERS = [
    {
        "id": "shinjuku",
        "tab_label": "新宿・西新宿（3店）",
        "title": "新宿・西新宿エリア（徒歩5分圏内・3店舗の4時間帯料金＆イベント比較）",
        "desc": "東口（歌舞伎町）のオリエンタルラウンジ新宿・JIS新宿と、西口側のJIS西新宿が徒歩圏内で競合する激戦区。全店とも単独（待機）時は330円/10分ですが、相席10分単価は「オリエンタル新宿（〜20時 550/660円、20〜22時 660/770円、22〜24時 770/880円）」に対し「JIS新宿・西新宿（〜22時 770/880円、22時〜 880/990円 ※JIS西新宿は20代終日550/660円）」。男性1名（ソロ）入店はJIS西新宿のみOKです。",
        "stores": ["OLG 新宿", "JIS SHINJUKU", "JIS NISHISHINJUKU"],
    },
    {
        "id": "shibuya",
        "tab_label": "渋谷・恵比寿（3店）",
        "title": "渋谷・恵比寿エリア（3店舗の4時間帯料金＆イベント比較）",
        "desc": "同じオリエンタル系列でも4時間帯の単価が劇的に異なる代表エリア！週末24時以降の10分単価は「ag渋谷（990円）＜ 渋谷本店（1,200円）＜ 恵比寿（1,500円）」、Open〜20時も「ag渋谷（440/660円）＜ 渋谷本店（550/770円）＜ 恵比寿（550/880円）」と階段状に変わります。3店舗とも単独時は330円/10分・男性2名以上必須（1名入店不可）です。",
        "stores": ["OLG 渋谷本店", "OLG ag渋谷", "OLG 恵比寿"],
    },
    {
        "id": "osaka-kita",
        "tab_label": "梅田・茶屋町（6店）",
        "title": "大阪キタ（梅田・茶屋町・天満 6店舗の4時間帯料金＆イベント比較）",
        "desc": "西日本最大の激戦区。オリエンタル大阪駅前（〜22時 700/800円・男性2名以上必須）に対し、1名入店OKのag梅田（〜22時 660/770円）・天満店（〜22時 600/700円）・JIS 2店舗（梅田・茶屋町：〜22時 660/770円）、さらに時間内は待機追加課金なしの定額デイパス制「ヤタコイ梅田（18〜19時入店 2,980円〜）」まで横並び比較できます。",
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
        "tab_label": "難波・心斎橋・京神（5店）",
        "title": "大阪ミナミ（心斎橋・難波）・京都・神戸エリア（5店舗比較）",
        "desc": "心斎橋店は「〜20時 600/660円 → 20〜24時 660/770円 → 24時〜 770/880円」と24時まで単価据え置き！難波店は週末も単独（待機）料金が220円/10分と割安です。京都店もOpen〜20時は平日500円/金土600円と早く入るほどお得になります。",
        "stores": ["OLG 心斎橋", "OLG 難波", "JIS NAMBA", "OLG 京都", "OLG 神戸"],
    },
    {
        "id": "nagoya",
        "tab_label": "名古屋 栄・錦（6店）",
        "title": "名古屋（栄・錦）・静岡・浜松・金沢エリア（6店舗の4時間帯料金＆イベント比較）",
        "desc": "名古屋の3店舗は「錦店（〜20時 550/770円、22時以降 770〜880/1,100〜1,200円・1名不可）」「栄店（名古屋で唯一男性1名入店OK！〜20時 600/770円）」「ag名古屋（19時Open・週末22時以降 880〜990円と最安・1名不可）」と役割が明確。静岡・浜松・金沢はOpen〜20時が平日440円（浜松は週末も440円）です。",
        "stores": ["OLG 名古屋 栄", "OLG 名古屋 錦", "OLG ag名古屋", "OLG 静岡", "OLG 浜松", "OLG ag金沢"],
    },
    {
        "id": "fukuoka",
        "tab_label": "福岡天神・九州（10店）",
        "title": "福岡（天神・小倉）・熊本・九州・沖縄エリア（10店舗の4時間帯料金＆イベント比較）",
        "desc": "福岡・天神ではオリエンタル福岡（単独220円｜相席〜22時 700/800円、22時〜 770/880円）とJIS福岡（単独330円｜相席終日 770/880円均一）が競合。九州他県はOpen〜20時の早割が強く、長崎・鹿児島は「平日20時まで440円」、熊本・小倉は「平日20時まで500円」、大分・宮崎は「22時まで550/660円」で飲めます。",
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
        "tab_label": "上野・錦糸町・町田（5店）",
        "title": "上野・錦糸町・町田・柏エリア（東京東＆近郊5店舗の4時間帯料金比較）",
        "desc": "上野エリアはオリエンタル上野のみ男性1名入店OK（隣接のag上野は2名以上必須）。錦糸町店は都内オリエンタルで唯一「単独220円｜Open〜20時 平日440円/金土550円、20〜22時 平日550円/金土660円」の地方店並み単価！町田・柏も単独220円＆平日は24時まで660円据え置きです。",
        "stores": ["OLG 上野", "OLG ag上野", "OLG 錦糸町", "OLG 町田", "OLG 柏"],
    },
    {
        "id": "omiya-yokohama",
        "tab_label": "大宮・横浜・北関東（5店）",
        "title": "大宮・横浜・宇都宮・高崎エリア（5店舗の4時間帯料金＆イベント比較）",
        "desc": "大宮駅東口では「オリエンタル大宮（単独220円｜〜20時 550/660円）」と「JIS大宮（U30チャージ0円・単独220円※1名入店時は330円｜〜21時 660/880円）」が競合。宇都宮・高崎はOpen〜20時が平日440円/金土550円の北関東価格です。",
        "stores": ["OLG 大宮", "JIS OMIYA", "OLG 横浜", "OLG 宇都宮", "OLG 高崎"],
    },
    {
        "id": "hiroshima-okayama",
        "tab_label": "広島・岡山（5店）",
        "title": "広島（新天地・流川 3店舗）・岡山（2店舗）の4時間帯料金＆開催中イベント比較",
        "desc": "全5店舗すべて単独（待機）時は220円/10分！広島・新天地（流川）は「ag広島（〜20時 500/600円）」「ALFA広島（〜20時 500/550円）」「CLOVERS広島（日〜木22時迄 半額275円/10分・金土祝前660円）」が徒歩2分圏内。岡山もオリエンタル岡山とXIX岡山（入店時刻の単価が退店まで固定・〜20時 500/600円）を比較できます。",
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
        "tab_label": "札幌・仙台・松山（3店）",
        "title": "札幌（すすきの）・仙台（国分町）・松山エリア（3店舗の4時間帯料金＆イベント比較）",
        "desc": "3店舗とも単独（待機）時は220円/10分！札幌すすきの「JIS札幌（終日 平日660円/金土770円均一・1名OK）」、仙台国分町「ag仙台（〜20時 550/700円、20〜24時 600〜700/750円・平日のみ1名OK）」、四国・大街道「JIS松山（終日 平日550円/金土660円均一・1名OK）」を比較できます。",
        "stores": [
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


def get_current_time_slot_info(now_jst: datetime.datetime = None) -> dict:
    """Return the active pricing time bracket key and human-readable label for the current JST hour."""
    if now_jst is None:
        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    h = now_jst.hour
    if 0 <= h < 7:
        return {
            "slot_key": "slot_24_last",
            "slot_label": "24:00〜Close（深夜帯）",
            "short_label": "24時〜Close",
        }
    elif h < 20:
        return {
            "slot_key": "slot_18_20",
            "slot_label": "Open〜20:00（早割・1回転目）",
            "short_label": "Open〜20時",
        }
    elif h < 22:
        return {
            "slot_key": "slot_20_22",
            "slot_label": "20:00〜22:00（1回転目コア帯）",
            "short_label": "20時〜22時",
        }
    else:
        return {
            "slot_key": "slot_22_24",
            "slot_label": "22:00〜24:00（ピーク帯）",
            "short_label": "22時〜24時",
        }


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
    now_jst: datetime.datetime = None,
) -> dict:
    """Build a complete store-specific 4-slot price + active events + live cost-performance verdict object."""
    info = store_master.get(store_name) or {}
    brand_raw = info.get("brand") or ("ag" if "ag" in store_name else store_name.split(" ")[0])
    if brand_raw == "YATAKOI":
        brand_raw = "ヤタコイ"
    base_spec = BRAND_PRICE_SPECS.get(brand_raw, BRAND_PRICE_SPECS["OLG"])
    store_override = STORE_PRICE_TABLE.get(store_name, {})

    # Merge brand baseline with official per-store 4-slot price table
    charge_str = store_override.get("charge_str", base_spec["charge_str"])
    wait_10m_yen = store_override.get("wait_10m_yen", base_spec["wait_10m_yen"])
    wait_fee_str = store_override.get("wait_fee_str", base_spec["wait_fee_str"])
    is_flat_rate = bool(store_override.get("is_flat_rate", base_spec.get("is_flat_rate", False)))
    has_wait_fee = False if is_flat_rate else bool(wait_10m_yen > 0 or base_spec.get("has_wait_fee", True))

    slot_18_20 = store_override.get("slot_18_20", base_spec.get("slot_18_20", ""))
    slot_20_22 = store_override.get("slot_20_22", base_spec.get("slot_20_22", ""))
    slot_22_24 = store_override.get("slot_22_24", base_spec.get("slot_22_24", ""))
    slot_24_last = store_override.get("slot_24_last", base_spec.get("slot_24_last", ""))

    early_10m_str = store_override.get("early_10m_str", f"〜20時 {slot_18_20} / 20〜22時 {slot_20_22}")
    late_10m_str = store_override.get("late_10m_str", f"22〜24時 {slot_22_24} / 24時〜 {slot_24_last}")
    weekday_10m_str = store_override.get("weekday_10m_str", base_spec["weekday_10m_str"])
    weekend_10m_str = store_override.get("weekend_10m_str", base_spec["weekend_10m_str"])
    est_60m_weekday = store_override.get("est_60m_weekday", base_spec["est_60m_weekday"])
    est_60m_weekend = store_override.get("est_60m_weekend", base_spec["est_60m_weekend"])
    est_60m_weekday_label = store_override.get("est_60m_weekday_label", base_spec["est_60m_weekday_label"])
    est_60m_weekend_label = store_override.get("est_60m_weekend_label", base_spec["est_60m_weekend_label"])
    solo_rule = store_override.get("solo_rule", base_spec.get("solo_rule", "1名入店OK"))
    age_range = store_override.get("age_range", base_spec["age_range"])
    vibe_short = store_override.get("vibe_short", base_spec["vibe_short"])

    slot_info = get_current_time_slot_info(now_jst)
    active_slot_key = slot_info["slot_key"]
    active_slot_label = slot_info["short_label"]
    active_slot_rate = {
        "slot_18_20": slot_18_20,
        "slot_20_22": slot_20_22,
        "slot_22_24": slot_22_24,
        "slot_24_last": slot_24_last,
    }.get(active_slot_key, slot_20_22)

    live_store = live_store or {}
    women = int(live_store.get("women") or 0)
    men = int(live_store.get("men") or 0)
    total = women + men
    wpct = round((women / total) * 100) if total > 0 else 0
    w_diff = int(live_store.get("women_diff") or 0)
    m_diff = int(live_store.get("men_diff") or 0)

    # Collect strictly store-matched events non-blockingly using preloaded pool (store_only=True prevents unverified global notices from leaking)
    store_evs = []
    try:
        import events
        store_evs = events.get_events_for_store(
            store_name,
            store_only=True,
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
    wait_penalty = 0
    if men > women and total > 0:
        wait_penalty = wait_10m_yen * 2  # 20 mins of waiting adds +440~660 yen

    effective_weekday_60m = est_60m_weekday + wait_penalty
    effective_weekend_60m = est_60m_weekend + wait_penalty

    # Score for ranking "Best Value Right Now" within an area cluster (higher is better)
    # Apply minimum headcount threshold (MIN_WOMEN_FOR_RATIO_EVAL = 10) so stores below ~5 pairs of women
    # are never over-scored when next-table rotation options are limited.
    meets_min_headcount = women >= MIN_WOMEN_FOR_RATIO_EVAL
    official_url = OFFICIAL_BRAND_URLS.get(base_spec["brand_code"], "https://oriental-lounge.com/")
    hours_info = store_hours.get_business_hours_status(store_name, now_jst=now_jst, total_guests=total)
    is_open_now = hours_info["is_open_now"]
    hours_label = hours_info["hours_label"]
    today_hours_label = hours_info["today_hours_label"]
    hours_spec = hours_info["schedule_spec"]

    price_advantage = max(0, (5800 - effective_weekend_60m) // 30)
    event_bonus = 18 if has_live_men_perk else (10 if has_live_women_event else 0)
    if total == 0:
        value_score = round(price_advantage + event_bonus - (25 if is_open_now else 40), 1)
    elif not meets_min_headcount:
        low_crowd_adj = women * 1.2 - (10 if men > women else 0) - 25
        value_score = round(price_advantage + event_bonus + low_crowd_adj, 1)
    else:
        ratio_score = (wpct - 50) * 1.8 + min(women, 45) * 1.1
        if women >= MIN_WOMEN_FOR_TOP_PICK:
            ratio_score += 8
        if women > men:
            ratio_score += 20
        elif men > women + 4:
            ratio_score -= 22
        value_score = round(price_advantage + event_bonus + ratio_score, 1)

    # Human-readable real-time verification verdict reflecting per-store 4-slot rates, business hours & minimum headcount
    if total == 0:
        if is_open_now:
            verdict_badge = "営業中（0 : 0）・空席"
            verdict_cls = "verdict-low"
            verdict_comment = (
                f"公式ホームページの営業時間内（本日 {today_hours_label}）でオープン中ですが、現在の店内人数は 0 : 0 です。"
                f"女性{MIN_WOMEN_FOR_RATIO_EVAL}名以上への増加を待つか、近隣ライバル店と比較してください。"
            )
        else:
            verdict_badge = "営業時間外"
            verdict_cls = "verdict-neutral"
            if is_flat_rate:
                verdict_comment = (
                    f"現在は営業時間外（公式営業時間：{hours_label}）。定額デイパス制（{wait_fee_str}）で、"
                    f"①Open〜20時（{slot_18_20}）→②20〜22時（{slot_20_22}）→③④22時〜（{slot_22_24}）。{solo_rule}。"
                )
            else:
                verdict_comment = (
                    f"現在は営業時間外（公式営業時間：{hours_label}）。単独（待機）時は{wait_fee_str}、"
                    f"相席10分はOpen〜20時（{slot_18_20}）→20〜22時（{slot_20_22}）→22〜24時（{slot_22_24}）→24時〜（{slot_24_last}）。{solo_rule}。"
                )
    elif not meets_min_headcount:
        if women >= men:
            verdict_badge = f"少人数・様子見推奨（女性{women}名）"
            verdict_cls = "verdict-low"
            verdict_comment = (
                f"現在女性{women}名／男性{men}名（女性比{wpct}%）。比率は高めですが店内の女性数が基準（{MIN_WOMEN_FOR_RATIO_EVAL}名）未満のため、"
                f"初回相席後に席替えできる次の相手が限られる可能性があります。女性{MIN_WOMEN_FOR_RATIO_EVAL}名以上への増加を待つか近隣店との比較が安全です。"
            )
        else:
            verdict_badge = f"少人数・男性先行（女性{women}名）"
            verdict_cls = "verdict-warn"
            if is_flat_rate:
                verdict_comment = (
                    f"現在女性{women}名／男性{men}名と少人数かつ男性先行の状態です。"
                    f"定額パス制ですが待ち時間が発生する可能性があるため、女性{MIN_WOMEN_FOR_RATIO_EVAL}名以上の近隣店もあわせてご確認ください。"
                )
            else:
                verdict_comment = (
                    f"現在女性{women}名／男性{men}名と母数が少なく男性先行のため、単独待機（{wait_fee_str}）になりやすい状態です。"
                    f"女性{MIN_WOMEN_FOR_RATIO_EVAL}名以上集まっている近隣店を優先するのがおすすめです。"
                )
    elif women >= men and women >= MIN_WOMEN_FOR_TOP_PICK and est_60m_weekend <= 4620:
        verdict_badge = "好条件｜低単価・女性優位"
        verdict_cls = "verdict-hot"
        verdict_comment = (
            f"現在女性{women}名（女性比{wpct}%）と母数・比率ともに十分。単独待機時間（{wait_fee_str}）を抑えて相席しやすく、"
            f"席替え候補も確保しやすいうえ、現在枠（{active_slot_label}）の単価も「{active_slot_rate}」と好条件です。"
        )
    elif women >= men and women >= MIN_WOMEN_FOR_TOP_PICK:
        verdict_badge = "好条件｜女性優位・安定回転"
        verdict_cls = "verdict-hot"
        if is_flat_rate:
            verdict_comment = (
                f"定額デイパス制（{active_slot_rate}）で時間内の待機追加課金なし。さらに現在女性{women}名（女性比{wpct}%）と母数も十分なため、"
                f"入店直後からの相席と席替え（ローテーション）ともに期待しやすい状態です。"
            )
        else:
            verdict_comment = (
                f"相席前の単独時は{wait_fee_str}がかかりますが、現在は女性{women}名（女性比{wpct}%）と組数・比率ともに十分なため、"
                f"待ち時間を抑えた相席と席替えがしやすい好条件です（現在枠：{active_slot_label} {active_slot_rate}）。"
            )
    elif men > women + 3:
        verdict_badge = "男性先行・待機時間に注意" if is_flat_rate else "男性先行・単独待機料金に注意"
        verdict_cls = "verdict-warn"
        if is_flat_rate:
            verdict_comment = (
                f"現在男性が+{men - women}名先行中。定額パス制のため時間内の追加待機課金はありませんが、"
                f"すぐ相席したい場合は近隣ライバル店で女性比率・女性数が多い店舗も比較してみてください。"
            )
        else:
            verdict_comment = (
                f"現在男性が+{men - women}名先行中。相席までの単独待機時間も{wait_fee_str}が加算されるため、"
                f"近隣ライバル店で女性{MIN_WOMEN_FOR_RATIO_EVAL}名以上かつ女性優位の店舗へ回る方が実質総額を抑えられます。"
            )
    elif has_live_women_event and women >= 15 and wpct >= 55:
        verdict_badge = "イベント集客好調・女性多数"
        verdict_cls = "verdict-hot"
        verdict_comment = (
            f"キャンペーン効果で実際に女性{women}名（女性比{wpct}%）が来店中。"
            f"母数が多く席替え候補も豊富なため、単独待機時間を抑えて座りやすい狙い目です。"
        )
    elif women > men:
        verdict_badge = f"女性+{women - men}名リード・相席圏内"
        verdict_cls = "verdict-good"
        verdict_comment = (
            f"現在女性{women}名／男性{men}名（女性比{wpct}%・約5〜6組規模）。単独待機を抑えて初回相席に入りやすいバランスです。"
            f"複数回の席替えも重視する場合は女性{MIN_WOMEN_FOR_TOP_PICK}名以上の店舗もあわせて比較してみてください。"
        )
    else:
        verdict_badge = "男女拮抗・単価比較推奨"
        verdict_cls = "verdict-neutral"
        if is_flat_rate:
            verdict_comment = (
                f"現在女性{women}名／男性{men}名。定額パス制（{wait_fee_str}）で追加課金の心配はありませんが、"
                f"即相席したい場合は同エリア内の女性リード店舗もチェックしてみてください。"
            )
        else:
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
        "official_url": official_url,
        "is_open_now": is_open_now,
        "hours_label": hours_label,
        "today_hours_label": today_hours_label,
        "hours_spec": hours_spec,
        "women": women,
        "men": men,
        "total": total,
        "wpct": wpct,
        "meets_min_headcount": meets_min_headcount,
        "women_diff": w_diff,
        "men_diff": m_diff,
        "charge_str": charge_str,
        "wait_10m_yen": wait_10m_yen,
        "wait_fee_str": wait_fee_str,
        "has_wait_fee": has_wait_fee,
        "is_flat_rate": is_flat_rate,
        "slot_18_20": slot_18_20,
        "slot_20_22": slot_20_22,
        "slot_22_24": slot_22_24,
        "slot_24_last": slot_24_last,
        "active_slot_key": active_slot_key,
        "active_slot_label": active_slot_label,
        "active_slot_rate": active_slot_rate,
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


_clusters_cache = {"key": None, "data": None}
_nearby_comp_cache = {}


def _build_live_cache_sig(live_stores, all_evs, now_jst):
    live_sig = tuple(
        (s.get("name"), int(s.get("women") or 0), int(s.get("men") or 0), int(s.get("women_diff") or 0), int(s.get("men_diff") or 0))
        for s in (live_stores or [])
    )
    ev_sig = (len(all_evs or []), all_evs[0].get("title") if all_evs else "")
    jst_tz = datetime.timezone(datetime.timedelta(hours=9))
    eff_dt = now_jst if now_jst is not None else datetime.datetime.now(jst_tz)
    time_sig = eff_dt.strftime("%Y-%m-%d %H:%M")
    try:
        hp = store_hours.get_business_hours_status("OLG 新宿", now_jst=now_jst, total_guests=0)
        sh_sig = (id(store_hours.get_business_hours_status), hp.get("is_open_now"), hp.get("today_hours_label"))
    except Exception:
        sh_sig = None
    return (live_sig, ev_sig, time_sig, sh_sig)


def build_area_comparison_clusters(live_stores: list = None, now_jst: datetime.datetime = None) -> dict:
    """Build all 10 area comparison clusters + live event verification summary + full 51-store catalog."""
    all_evs = []
    try:
        import events
        all_evs = events.fetch_all_events(allow_blocking=False)
    except Exception:
        all_evs = []

    cache_key = _build_live_cache_sig(live_stores, all_evs, now_jst)
    if _clusters_cache["key"] == cache_key and _clusters_cache["data"] is not None:
        return _clusters_cache["data"]

    live_map = {s.get("name"): s for s in (live_stores or [])}

    current_slot_info = get_current_time_slot_info(now_jst)
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
                now_jst=now_jst,
            )
            profiles.append(prof)
            all_profiles_map[sname] = prof

        best_prof = None
        qualified = [p for p in profiles if p["meets_min_headcount"]]
        if qualified:
            best_prof = max(
                qualified,
                key=lambda x: (x["women"] >= MIN_WOMEN_FOR_TOP_PICK, x["value_score"], x["women"]),
            )
            best_prof["is_best_in_cluster"] = True

        clusters_out.append({
            "id": cl["id"],
            "tab_label": cl["tab_label"],
            "title": cl["title"],
            "desc": cl["desc"],
            "stores": profiles,
            "best_store": best_prof,
            "has_any_open": any(p["is_open_now"] for p in profiles),
            "has_any_guests": any(p["total"] > 0 for p in profiles),
        })

    # Ensure every domestic store in store_master is included in the full national price list
    all_domestic_profiles = []
    for sname in STORE_PRICE_TABLE.keys():
        if not store_master.get(sname):
            continue
        prof = all_profiles_map.get(sname)
        if not prof:
            prof = get_store_price_profile(
                sname,
                live_store=live_map.get(sname),
                all_events_Pool=all_evs,
                now_jst=now_jst,
            )
            all_profiles_map[sname] = prof
        all_domestic_profiles.append(prof)

    # Build the Event Verification Board (stores with active scraped events + live crowd proof)
    verified_event_stores = [
        p for p in all_profiles_map.values()
        if p["has_live_men_perk"] or p["has_live_women_event"]
    ]
    verified_event_stores.sort(
        key=lambda x: (x["meets_min_headcount"], x["has_live_men_perk"], x["women"], x["wpct"]),
        reverse=True,
    )
    if len(verified_event_stores) < 6:
        fallback_extra = [
            p for p in sorted(
                all_profiles_map.values(),
                key=lambda x: (x["meets_min_headcount"], x["women"], x["value_score"]),
                reverse=True,
            )
            if p not in verified_event_stores
        ]
        verified_event_stores.extend(fallback_extra[: (6 - len(verified_event_stores))])

    result_payload = {
        "clusters": clusters_out,
        "verified_event_stores": verified_event_stores[:8],
        "brand_specs": [BRAND_PRICE_SPECS["OLG"], BRAND_PRICE_SPECS["ag"], BRAND_PRICE_SPECS["JIS"]],
        "all_domestic_stores": all_domestic_profiles,
        "current_slot": current_slot_info,
        "min_women_eval": MIN_WOMEN_FOR_RATIO_EVAL,
        "min_women_top": MIN_WOMEN_FOR_TOP_PICK,
        "jp_holidays": sorted(store_hours.JP_HOLIDAYS),
    }
    _clusters_cache["key"] = cache_key
    _clusters_cache["data"] = result_payload
    return result_payload


def get_nearby_comparison_for_store(store_name: str, live_stores: list = None, now_jst: datetime.datetime = None) -> dict:
    """Return the store's own price profile + all sibling stores in the same urban cluster
    for embedding directly on `/store/<slug>`."""
    all_evs = []
    try:
        import events
        all_evs = events.fetch_all_events(allow_blocking=False)
    except Exception:
        all_evs = []

    nb_key = (store_name, _build_live_cache_sig(live_stores, all_evs, now_jst))
    cached_nb = _nearby_comp_cache.get(nb_key)
    if cached_nb is not None:
        return cached_nb

    live_map = {s.get("name"): s for s in (live_stores or [])}

    target_cluster = None
    for cl in COMPARISON_CLUSTERS:
        if store_name in cl["stores"]:
            target_cluster = cl
            break

    if not target_cluster:
        self_prof = get_store_price_profile(
            store_name,
            live_store=live_map.get(store_name),
            all_events_Pool=all_evs,
            now_jst=now_jst,
        )
        res = {
            "self_profile": self_prof,
            "cluster_id": "shinjuku",
            "cluster_title": f"{self_prof['area_label']}エリアの料金・イベント比較",
            "stores": [self_prof],
            "jp_holidays": sorted(store_hours.JP_HOLIDAYS),
        }
        if len(_nearby_comp_cache) > 120:
            _nearby_comp_cache.clear()
        _nearby_comp_cache[nb_key] = res
        return res

    profiles = []
    self_prof = None
    for sname in target_cluster["stores"]:
        if not store_master.get(sname):
            continue
        p = get_store_price_profile(
            sname,
            live_store=live_map.get(sname),
            all_events_Pool=all_evs,
            now_jst=now_jst,
        )
        p["is_current_store"] = (sname == store_name)
        if sname == store_name:
            self_prof = p
        profiles.append(p)

    qualified = [p for p in profiles if p["meets_min_headcount"]]
    if qualified:
        best_p = max(
            qualified,
            key=lambda x: (x["women"] >= MIN_WOMEN_FOR_TOP_PICK, x["value_score"], x["women"]),
        )
        best_p["is_best_in_cluster"] = True

    res = {
        "self_profile": self_prof or (profiles[0] if profiles else None),
        "cluster_id": target_cluster["id"],
        "cluster_title": target_cluster["title"],
        "stores": profiles,
        "jp_holidays": sorted(store_hours.JP_HOLIDAYS),
    }
    if len(_nearby_comp_cache) > 120:
        _nearby_comp_cache.clear()
    _nearby_comp_cache[nb_key] = res
    return res
