"""
affiliate_manager.py - Centralized Affiliate Slot Routing, Override Persistence & Cannibalization Detector.

Solves URL overlap across distinct user intents (e.g. 'immediate tonight drink app' vs 'screened 1-on-1 cafe date')
and allows the owner to update or test ASP destination URLs directly from `/staff/analytics` with zero redeploy.

Priority order for `/link/<item_id>`:
  1. Runtime override saved via `/staff/analytics` (persisted in `job_state` across Render redeploys, 0ms RAM cache)
  2. Render environment variables (`AFFILIATE_URL_*`)
  3. Built-in fallback URLs
"""

import json
import os
import threading
import urllib.parse

import job_state

AFFILIATE_OVERRIDES_STATE_KEY = "aff_slot_overrides"

_lock = threading.Lock()
_mem_overrides = None  # dict[slot_id, {"url": str, "memo": str, "updated_at": str}]

# Canonical 10 affiliate slots (5 men's slots + 3 women's slots + 2 unisex slots) aligned with site UI & reader intent
AFFILIATE_SLOTS = [
    {
        "slot_id": "dating_instant",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": ["dating", "dating_casual"],
        "category_label": "🍻 即時合流・定番恋活アプリ枠",
        "title": "今日これから・近くで飲める恋活アプリ（満席・男余り時の受け皿）",
        "placement": "店舗詳細（男性先行時） / トップPR枠 / コンシェルジュ / 身だしなみガイド①",
        "recommended_asp_examples": "タップル / ペアーズ / Dine（ダイン） / with 等の即時マッチング系案件",
        "env_keys": ["AFFILIATE_URL_DATING_INSTANT", "AFFILIATE_URL_DATING_CASUAL", "AFFILIATE_URL_DATING"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9",
    },
    {
        "slot_id": "dating_bachelor",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": [],
        "category_label": "🥂 審査制・1対1カフェデート枠",
        "title": "メッセージ不要・週1回デートが決まる審査制アプリ（バチェラーデート等）",
        "placement": "トップ4位下PR枠 / 店舗詳細（営業時間外・少人数・ソロ制限時） / 身だしなみガイド②",
        "recommended_asp_examples": "バチェラーデート / 東カレデート 等の審査制・ハイクラス系案件",
        "env_keys": ["AFFILIATE_URL_DATING_BACHELOR", "AFFILIATE_URL_DATING"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEK9C+21TRSI+5SFC+61RI9",
    },
    {
        "slot_id": "mens_hair_removal",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": ["hair_removal", "hair_removal_trial"],
        "category_label": "🪒 メンズ医療ヒゲ脱毛枠",
        "title": "仕事帰り・距離70cmの青ヒゲ対策（メンズ医療ヒゲ脱毛 初回プラン）",
        "placement": "トップPR枠 / 最初の10分・美容エリア・男性先行コラム / 身だしなみガイド",
        "recommended_asp_examples": "ゴリラクリニック / メンズエミナル / 湘南美容クリニックヒゲ脱毛 等",
        "env_keys": ["AFFILIATE_URL_HAIR_REMOVAL", "AFFILIATE_URL_HAIR_REMOVAL_TRIAL"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEL1F+61B8KY+328C+2HCB1D",
    },
    {
        "slot_id": "mens_skincare_clinic",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": ["orbis"],
        "category_label": "🧴 メンズスキンケア枠",
        "title": "夕方のテカリ・乾燥ケア（ORBIS Mr. トライアルセット等）",
        "placement": "店舗詳細（女性優位時） / コンシェルジュ / 身だしなみガイド / おすすめモーダル",
        "recommended_asp_examples": "ORBIS Mr.（オルビス ミスター） / バルクオム 初回トライアル等",
        "env_keys": ["AFFILIATE_URL_ORBIS"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BEL1F+67V08I+1USQ+2Z90WX",
    },
    {
        "slot_id": "spu_styleup",
        "audience": "men",
        "audience_label": "🕺 男性向け",
        "aliases": ["spu"],
        "category_label": "👔 メンズ服・コーデ定期便枠",
        "title": "相席で浮かないきれいめ私服（SPU スタイルアップ便等）",
        "placement": "身だしなみガイド / おすすめモーダル（服装・身だしなみタブ）",
        "recommended_asp_examples": "SPU スタイルアップ便 / UWear（ユーウェア） 等",
        "env_keys": ["AFFILIATE_URL_SPU"],
        "default_url": "https://h.accesstrade.net/sp/cc?rk=0100oo8500p144",
    },
    {
        "slot_id": "women_dating_bachelor",
        "audience": "women",
        "audience_label": "💄 女性向け",
        "aliases": ["women_dating"],
        "category_label": "🥂 女性向け｜審査制・完全無料ハイクラスデート枠",
        "title": "【女性完全無料】顔写真非公開・審査制で週1回ハイスペック男性と1対1カフェデート",
        "placement": "/for-women 女性特設ガイド / トップ『💄女性目線モード』おすすめモーダル",
        "recommended_asp_examples": "バチェラーデート（女性無料登録案件） / 東カレデート / ゴージャス 等",
        "env_keys": ["AFFILIATE_URL_WOMEN_DATING"],
        "default_url": "https://www.bachelorapp.net/",
    },
    {
        "slot_id": "women_beauty_clinic",
        "audience": "women",
        "audience_label": "💄 女性向け",
        "aliases": ["women_clinic", "women_hair_removal"],
        "category_label": "💎 女性向け｜美容医療・美肌クリニック＆医療脱毛枠",
        "title": "ラウンジの間接照明でも映える水光肌・医療脱毛（初回カウンセリング＆体験プラン）",
        "placement": "/for-women 女性特設ガイド / トップ『💄女性目線モード』おすすめモーダル",
        "recommended_asp_examples": "湘南美容クリニック（美肌/医療脱毛） / エミナルクリニック / 品川美容外科 等",
        "env_keys": ["AFFILIATE_URL_WOMEN_BEAUTY_CLINIC", "AFFILIATE_URL_WOMEN_CLINIC"],
        "default_url": "https://www.s-b-c.net/",
    },
    {
        "slot_id": "women_cosme_salon",
        "audience": "women",
        "audience_label": "💄 女性向け",
        "aliases": ["women_cosme", "women_skincare"],
        "category_label": "🧴 女性向け｜デパコス・ご褒美スキンケア＆サロン枠",
        "title": "夜遊び帰りのメイク崩れ・乾燥リセット（人気スキンケア・ヘアケア初回セット・サロン予約）",
        "placement": "/for-women 女性特設ガイド / トップ『💄女性目線モード』おすすめモーダル",
        "recommended_asp_examples": "サロン専売ヘアケア・高保湿スキンケア初回トライアル / ネイル・ヘアサロン予約 等",
        "env_keys": ["AFFILIATE_URL_WOMEN_COSME"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BERA3+6J68QA+5GZ4+5YZ75",
    },
    {
        "slot_id": "night_care_supplement",
        "audience": "common",
        "audience_label": "🥂 男女共通",
        "aliases": ["hangover_supplement", "supplement", "night_supplement", "women_night_supplement"],
        "category_label": "🥂 乾杯・翌朝すっきりナイトケアサプリ枠",
        "title": "飲み放題・2軒目バーの翌朝対策（乾杯前後のナイトケア・コンディションサポートサプリ）",
        "placement": "/grooming 事前準備ガイド / /for-women 女性特設ガイド / トップおすすめモーダル（男女両方）",
        "recommended_asp_examples": "乾杯サポート・ナイトケアサプリ（オルニチン・ウコン・酵素・ビタミン系案件）",
        "env_keys": ["AFFILIATE_URL_NIGHT_SUPPLEMENT", "AFFILIATE_URL_SUPPLEMENT"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BELTJ+7RTR3M+3H28+5ZU29",
    },
    {
        "slot_id": "second_venue_reservation",
        "audience": "common",
        "audience_label": "🥂 男女共通",
        "aliases": ["ikyu_restaurant", "ikyu", "second_venue", "women_second_venue"],
        "category_label": "🍽️ 2軒目バー・大人ディナー予約枠（一休.comレストラン）",
        "title": "相席後の2軒目バー・後日デート・女子会ディナー予約（一休.comレストラン）",
        "placement": "全店舗2軒目セクション / /grooming 事前準備ガイド / /for-women 女性特設ガイド / トップおすすめモーダル（男女両方）",
        "recommended_asp_examples": "一休.comレストラン / OZmall / 食べログ 等のネット予約案件",
        "env_keys": ["AFFILIATE_URL_SECOND_VENUE", "AFFILIATE_URL_IKYU"],
        "default_url": "https://px.a8.net/svt/ejp?a8mat=4BGDZ4+9R9ROY+1OK+NVP2P",
    },
]

_SLOT_BY_ID = {s["slot_id"]: s for s in AFFILIATE_SLOTS}
_ALIAS_TO_CANONICAL = {}
for _s in AFFILIATE_SLOTS:
    _ALIAS_TO_CANONICAL[_s["slot_id"]] = _s["slot_id"]
    for _al in _s["aliases"]:
        _ALIAS_TO_CANONICAL[_al] = _s["slot_id"]

KNOWN_ASP_HOST_MARKERS = (
    "a8.net",
    "accesstrade.net",
    "valuecommerce.com",
    "afi-b.com",
    "afb.ne.jp",
    "rentracks.jp",
    "moshimo.com",
    "felmat.net",
    "tcs-asp.net",
    "linksynergy.com",
    "j-a-net.jp",
    "circuit-x",
    "infotop.jp",
    "fam-ad.com",
    "ad-nex.com",
    "smart-c.jp",
    "zucks.net",
)

SLOT_TRACKING_PIXELS = {
    "dating_instant": "https://www15.a8.net/0.gif?a8mat=4BEK9C+21TRSI+5SFC+61RI9",
    "mens_hair_removal": "https://www17.a8.net/0.gif?a8mat=4BEL1F+61B8KY+328C+2HCB1D",
    "mens_skincare_clinic": "https://www18.a8.net/0.gif?a8mat=4BEL1F+67V08I+1USQ+2Z90WX",
    "women_cosme_salon": "https://www10.a8.net/0.gif?a8mat=4BERA3+6J68QA+5GZ4+5YZ75",
    "night_care_supplement": "https://www19.a8.net/0.gif?a8mat=4BELTJ+7RTR3M+3H28+5ZU29",
    "second_venue_reservation": "https://www15.a8.net/0.gif?a8mat=4BGDZ4+9R9ROY+1OK+NVP2P",
}

_MEN_SOLO_RESTRICTED_NAMES = frozenset({
    "OLG 恵比寿",
    "OLG 渋谷本店",
    "OLG ag渋谷",
    "OLG 新宿",
    "JIS SHINJUKU",
    "OLG ag上野",
    "OLG 大阪駅前",
    "OLG 名古屋 錦",
    "OLG ag名古屋",
})


def canonical_slot_id(item_id: str) -> str:
    clean = (item_id or "").strip().lower()
    return _ALIAS_TO_CANONICAL.get(clean, "")


def is_asp_tracking_url(raw_url: str) -> bool:
    """Return True if raw_url points to a known affiliate tracking host (or explicit tracking query)."""
    u = (raw_url or "").strip().lower()
    if not u:
        return False
    try:
        host = (urllib.parse.urlparse(u).netloc or "").lower()
    except Exception:
        host = ""
    if any(marker in host for marker in KNOWN_ASP_HOST_MARKERS):
        return True
    if any(k in u for k in ("a8mat=", "aff_id=", "click_id=", "rk=0100")):
        return True
    return False


def is_slot_monetized(slot_id: str, allow_blocking: bool = False) -> bool:
    """Return True when the resolved URL of `slot_id` points to a real ASP tracking endpoint (not a 0-yen direct URL)."""
    return is_asp_tracking_url(resolve_affiliate_url(slot_id, allow_blocking=allow_blocking))


def are_slots_sharing_url(slot_a: str, slot_b: str, allow_blocking: bool = False) -> bool:
    """Return True when two slots currently resolve to the exact same destination URL."""
    ua = resolve_affiliate_url(slot_a, allow_blocking=allow_blocking)
    ub = resolve_affiliate_url(slot_b, allow_blocking=allow_blocking)
    return bool(ua and ub and ua == ub)


def build_for_women_top_picks(allow_blocking: bool = False) -> list:
    """
    Build the 2 compact high-intent offer cards placed immediately below the LIVE Zero-Wait Board on `/for-women`.
    Prioritizes active ASP-monetized slots so clicks never leak to 0-yen direct brand URLs, while automatically
    promoting `women_beauty_clinic` (Regina/Rize etc.) the moment its ASP tag is configured!
    """
    active_pixels = {}
    for _sid, _px in SLOT_TRACKING_PIXELS.items():
        if resolve_affiliate_url(_sid, allow_blocking=allow_blocking) == ((_SLOT_BY_ID.get(_sid) or {}).get("default_url") or ""):
            active_pixels[_sid] = _px

    cards = [
        {
            "badge": "0次会カフェ・女子会ディナーに",
            "title": "駅チカ夜カフェ＆ダイニング限定プラン予約（一休.comレストラン）",
            "desc": "合流前の0次会カフェや、ラウンジ帰りの女子会ディナーをWEB優待プランでスマートに空席確保。",
            "cta_label": "人気プラン・空席を見る ›",
            "href": "/link/women_second_venue?from=/for-women",
            "tracking_pixel": active_pixels.get("second_venue_reservation", ""),
        }
    ]

    if is_slot_monetized("women_beauty_clinic", allow_blocking=allow_blocking):
        cards.append({
            "badge": "近距離ライト70cmでも映える肌へ",
            "title": "美容皮膚科・医療脱毛 初回カウンセリング＆体験プラン",
            "desc": "向かい合わせの暖色スポット照明でもうぶ毛・毛穴が気にならない水光肌へ。全国駅チカ院の初回プラン。",
            "cta_label": "初回キャンペーンを見る ›",
            "href": "/link/women_beauty_clinic?from=/for-women",
            "tracking_pixel": active_pixels.get("women_beauty_clinic", ""),
        })
    else:
        cards.append({
            "badge": "夜遊び帰りの髪・肌リセット",
            "title": "サロン品質ヘア＆ナイトスキンケア 初回セット",
            "desc": "空調の効いた店内でお酒やスイーツを楽しんだ夜も、帰宅後すぐツヤとうるおいを整える人気サロンケア。",
            "cta_label": "公式ショップで詳細を見る ›",
            "href": "/link/women_cosme_salon?from=/for-women",
            "tracking_pixel": active_pixels.get("women_cosme_salon", ""),
        })

    return cards


def build_store_smart_cta(
    store_obj: dict,
    nearby_stores: list = None,
    area_slug: str = None,
    now_jst=None,
) -> dict:
    """
    Build the context-aware Smart CTA card placed immediately below the live crowd hero on `/store/<slug>`.
    Adapts to 4 real-time visitor states (plus a `women_variant` payload for female-mode / `/for-women` visitors):
      1. `men_glut`: Men outnumber women (`total >= 6` and `men > women`) or wait groups > 0 -> `dating_instant` + nearby female-lead store
      2. `off_or_low`: Closed or low headcount (`women < 6` / `total < 6`) -> `dating_bachelor` (honest copy matching active LP)
      3. `late_second_venue`: 22:30-04:59 JST with good female ratio -> `second_venue_reservation` (Ikyu Restaurant)
      4. `prime_grooming`: Before 22:30 JST with good female ratio -> `mens_skincare_clinic` + `/grooming`
    """
    import datetime as _dt

    JST = _dt.timezone(_dt.timedelta(hours=9))
    if now_jst is None:
        now_jst = _dt.datetime.now(JST)
    elif getattr(now_jst, "tzinfo", None) is None:
        now_jst = now_jst.replace(tzinfo=JST)
    else:
        now_jst = now_jst.astimezone(JST)

    sname = str((store_obj or {}).get("name") or "")
    seo_name = str((store_obj or {}).get("seo_name") or sname or "当店")
    slug = str((store_obj or {}).get("slug") or "").strip()
    women = int((store_obj or {}).get("women") or 0)
    men = int((store_obj or {}).get("men") or 0)
    tot = women + men
    is_open_now = bool((store_obj or {}).get("is_open_now"))
    we = (store_obj or {}).get("wait_estimate") or {}
    est_wait_groups = int((we.get("est_men_wait_groups") if isinstance(we, dict) else 0) or 0)
    is_solo_restricted = sname in _MEN_SOLO_RESTRICTED_NAMES

    nearby_list = [ns for ns in (nearby_stores or []) if isinstance(ns, dict) and ns.get("slug")]
    best_female_alt = None
    best_open_alt = None
    best_women_zero_wait_alt = None
    for ns in nearby_list:
        if not ns.get("is_open_now", True):
            continue
        ow = int(ns.get("women") or 0)
        om = int(ns.get("men") or 0)
        if ow >= 6 and ow >= om:
            if best_female_alt is None or (ow - om, ow) > (
                int(best_female_alt.get("women") or 0) - int(best_female_alt.get("men") or 0),
                int(best_female_alt.get("women") or 0),
            ):
                best_female_alt = ns
        if om >= 5 and om >= ow:
            if best_women_zero_wait_alt is None or (om - ow, om) > (
                int(best_women_zero_wait_alt.get("men") or 0) - int(best_women_zero_wait_alt.get("women") or 0),
                int(best_women_zero_wait_alt.get("men") or 0),
            ):
                best_women_zero_wait_alt = ns
        if ow >= 6:
            if best_open_alt is None or ow > int(best_open_alt.get("women") or 0):
                best_open_alt = ns

    from_param = f"/store/{slug}" if slug else "/areas"
    is_late_night = (now_jst.hour > 22) or (now_jst.hour == 22 and now_jst.minute >= 30) or (now_jst.hour < 5)
    bachelor_shares_instant = are_slots_sharing_url("dating_bachelor", "dating_instant", allow_blocking=False)

    if tot >= 6 and (men > women or est_wait_groups > 0):
        slot_id = "dating_instant"
        mode = "men_glut"
        diff_m = max(1, men - women)
        kicker = (
            f"現在 男性+{diff_m}名の男性先行｜待機回避の選択肢"
            if men > women
            else "現在 受付待機リスクあり｜時間ロス回避の選択肢"
        )
        title = "待機課金の列に並ぶ前に｜近くで今日これから飲める恋活アプリを併用"
        sub = "男性先行の時間帯は受付待ちや単独待機料金が発生しやすくなります。近隣で今日これから合流できる恋活アプリを入れておくと、混んでいる夜も予定を無駄にせず動けます。"
        cta_label = "近くで今日飲めるアプリを見る ›"
        if best_female_alt:
            sublink_href = f"/store/{best_female_alt['slug']}"
            sublink_label = f"近隣の女性優位店：{best_female_alt['seo_name']}（女{best_female_alt['women']} : 男{best_female_alt['men']}） ›"
        elif area_slug:
            sublink_href = f"/concierge?area={area_slug}"
            sublink_label = "近隣の穴場・徒歩ハシゴ3秒診断を見る ›"
        else:
            sublink_href = "/concierge"
            sublink_label = "近隣の穴場・徒歩ハシゴ3秒診断を見る ›"
    elif (not is_open_now) or tot < 6 or women < 6:
        slot_id = "dating_bachelor"
        mode = "off_or_low"
        if bachelor_shares_instant:
            if is_solo_restricted:
                kicker = "男性1名利用・営業時間外の選択肢"
                sub = f"{seo_name}は男性2名以上での入店が基本です。1人で動きたい日や営業時間外は、相方を探さず1対1でカフェや食事の予定を組める定番恋活・デートアプリも選択肢になります。"
            elif not is_open_now:
                kicker = "営業時間外・事前予定づくりの選択肢"
                sub = "営業時間外のうちに次の予定を組むなら、近隣エリアで気の合う相手と1対1の食事・カフェをセッティングできる定番恋活アプリも便利です。"
            else:
                kicker = "少人数時間帯・1対1で会いたい日の選択肢"
                sub = "立ち上がり前の少人数時間帯や、複数人の相席より最初から1対1で落ち着いて話したい日に選ばれている恋活・デートアプリです。"
            title = "相席より1対1で落ち着いて会いたい日の「定番恋活・デートアプリ」"
            cta_label = "1対1恋活・デートアプリの公式詳細 ›"
        else:
            if is_solo_restricted:
                kicker = "男性1名利用・営業時間外の選択肢"
                sub = f"{seo_name}は男性2名以上での入店が基本です。1人で動きたい日や営業時間外は、希望日時を入れるだけでAIが1対1デートを調整する審査制アプリも選択肢になります。"
            elif not is_open_now:
                kicker = "営業時間外・事前予定づくりの選択肢"
                sub = "営業時間外のうちに次の予定を組むなら、事前のメッセージ往復なしでカフェやバーでの1対1デートが決まる審査制アプリも便利です。"
            else:
                kicker = "少人数時間帯・1対1で会いたい日の選択肢"
                sub = "立ち上がり前の少人数時間帯や、複数人の相席より最初から1対1で落ち着いて話したい日に選ばれている審査制デートアプリです。"
            title = "メッセージ不要で週1回デートが決まる「審査制1対1カフェデート」"
            cta_label = "審査制1対1デートの公式詳細 ›"
        if best_open_alt:
            sublink_href = f"/store/{best_open_alt['slug']}"
            sublink_label = f"周辺で稼働中の店舗：{best_open_alt['seo_name']}（女{best_open_alt['women']} : 男{best_open_alt['men']}） ›"
        else:
            sublink_href = "/guide/lounge-vs-screened-date"
            sublink_label = "相席ラウンジ vs 1対1デート徹底比較 ›"
    elif is_late_night:
        slot_id = "second_venue_reservation"
        mode = "late_second_venue"
        kicker = f"現在 女性{women}名で稼働中｜2軒目バー・後日デートの席確保に"
        title = "着席45分後の2軒目・次回ディナーで迷わない「一休.comレストラン」"
        sub = "夜間・週末の繁華街は飛び込みだと2軒目が満席になりがちです。駅近くの隠れ家バーや連絡先交換後の次回ディナーをスマホで空席予約できます。"
        cta_label = "空席・プランを見る ›"
        sublink_href = "#store-second-venue-section"
        sublink_label = f"{seo_name}から歩ける2軒目マップ・誘い方を見る ›"
    else:
        slot_id = "mens_skincare_clinic"
        mode = "prime_grooming"
        kicker = f"現在 女性{women}名で好条件｜入店前の最終チェック＆清潔感ケア"
        title = "ソファ席70cmで好印象をキープする「夕方の皮脂テカリ・乾燥ケア」"
        sub = "女性が先行しておりスムーズに相席しやすい時間帯です。入店前に口元・夕方の皮脂・靴先を整えておくと最初の10分の会話が弾みやすくなります。日頃のベースケアには定番のORBIS Mr.も有効です。"
        cta_label = "トライアルセット・公式詳細 ›"
        sublink_href = "/grooming"
        sublink_label = "入店3分前の服装・身だしなみチェックリスト ›"

    href = f"/link/{slot_id}?from={from_param}"
    active_pixels = {}
    for _sid, _px in SLOT_TRACKING_PIXELS.items():
        if resolve_affiliate_url(_sid, allow_blocking=False) == ((_SLOT_BY_ID.get(_sid) or {}).get("default_url") or ""):
            active_pixels[_sid] = _px
    tracking_pixel = active_pixels.get(slot_id, "")

    # Build female-visitor variant (`women_variant`) so `/for-women` referrals and `aiseki_audience_mode === 'women'`
    # see relevant, monetized offers instead of men's skincare / men's waiting-fee copy!
    clinic_monetized = is_slot_monetized("women_beauty_clinic", allow_blocking=False)
    wdating_monetized = is_slot_monetized("women_dating_bachelor", allow_blocking=False)

    if is_open_now and tot >= 6 and women > men + 6:
        # Female-crowded store -> warn women about reception wait & offer nearby male-lead store + cafe/salon
        w_link_id = "women_second_venue"
        w_canon = "second_venue_reservation"
        w_kicker = f"現在 女性{women}名で女子組集中｜受付待ち回避＆0次会カフェ"
        w_title = "受付で待つ前に｜駅チカ夜カフェ・女子会ディナー空席確認（一休.comレストラン）"
        w_sub = "女性比率が高すぎる時間帯は受付で20〜30分待つことがあります。近隣の男性先行店へ回るか、駅近くのカフェ・ダイニングでゆっくり乾杯してから動くのが快適です。"
        w_cta_label = "周辺のカフェ・ディナー空席を見る ›"
        if best_women_zero_wait_alt:
            w_sublink_href = f"/store/{best_women_zero_wait_alt['slug']}"
            w_sublink_label = f"近隣の女子待ちゼロ候補：{best_women_zero_wait_alt['seo_name']}（男{best_women_zero_wait_alt['men']} : 女{best_women_zero_wait_alt['women']}） ›"
        else:
            w_sublink_href = "/for-women#live-board"
            w_sublink_label = "いま女子待ちゼロで入れる店舗ボードを見る ›"
    elif (not is_open_now) or tot < 6:
        if wdating_monetized:
            w_link_id = "women_dating_bachelor"
            w_canon = "women_dating_bachelor"
            w_kicker = "女性向け｜営業時間外・1人で動きたい日の選択肢"
            w_title = "友達と予定を合わせず1対1でお茶できる「審査制カフェデート（女性無料）」"
            w_sub = "営業時間外やペアの予定が合わない週末は、顔写真非公開・メッセージ往復なしで審査通過男性と1対1カフェデートができる無料サービスも人気です。"
            w_cta_label = "女性無料の公式詳細を見る ›"
        elif clinic_monetized:
            w_link_id = "women_beauty_clinic"
            w_canon = "women_beauty_clinic"
            w_kicker = "女性向け｜間接照明70cmでも自信が持てる肌メンテナンス"
            w_title = "ラウンジの暖色ライトでも映える「美容皮膚科・医療脱毛 初回体験プラン」"
            w_sub = "向かい合わせ70cmのソファ席でもうぶ毛や毛穴が気にならない水光肌へ。全国駅チカで仕事帰りにも通いやすい初回プランです。"
            w_cta_label = "初回キャンペーンを見る ›"
        else:
            w_link_id = "women_cosme_salon"
            w_canon = "women_cosme_salon"
            w_kicker = "女性向け｜お出かけ前後のツヤ髪・ナイトスキンケア"
            w_title = "間接照明70cmでも映える「サロン品質ヘア＆ナイトスキンケア」"
            w_sub = "ラウンジや夜カフェの暖色スポット照明では、毛先のツヤと肌のうるおいが一番きれいに映えます。夜遊び帰りのリセットにも人気のサロンケアです。"
            w_cta_label = "公式ショップで詳細を見る ›"
        w_sublink_href = "/for-women#live-board"
        w_sublink_label = "女子待ちゼロ店舗ボード＆席替えフレーズ集 ›"
    elif clinic_monetized and not is_late_night:
        w_link_id = "women_beauty_clinic"
        w_canon = "women_beauty_clinic"
        w_kicker = f"現在 男性{men}名・女子組が入りやすい時間帯｜自分磨きメモ"
        w_title = "ソファ席70cmの間接照明でも映える「医療脱毛・美肌初回プラン」"
        w_sub = "男性がしっかり入っており待ち時間少なく案内されやすい状況です。近距離ライトの席でも自信が持てる駅チカ医療脱毛・美肌ケアの初回プランもチェックできます。"
        w_cta_label = "初回キャンペーンを見る ›"
        w_sublink_href = "/for-women#change-seat-manual"
        w_sublink_label = "角が立たない席替え＆スマート退店フレーズを見る ›"
    else:
        w_link_id = "women_second_venue"
        w_canon = "second_venue_reservation"
        w_kicker = f"現在 男性{men}名で稼働中｜0次会カフェ・女子会ディナー予約に"
        w_title = "合流前のカフェ＆ご褒美女子会に「一休.comレストラン 限定プラン」"
        w_sub = "男性客が入っておりスムーズに着席しやすい時間帯です。相席前の0次会カフェや、友達とのご褒美ディナー・アフタヌーンティー探しにも使えます。"
        w_cta_label = "人気プラン・空席を見る ›"
        w_sublink_href = "#store-second-venue-section"
        w_sublink_label = f"{seo_name}周辺の夜カフェ・2軒目スポットを見る ›"

    women_variant = {
        "slot_id": w_canon,
        "kicker": w_kicker,
        "title": w_title,
        "sub": w_sub,
        "cta_label": w_cta_label,
        "href": f"/link/{w_link_id}?from={from_param}",
        "tracking_pixel": active_pixels.get(w_canon, ""),
        "sublink_href": w_sublink_href,
        "sublink_label": w_sublink_label,
        "secondary_href": "/for-women",
        "secondary_label": "女性向け待ちゼロ店＆席替えガイド ›",
    }

    return {
        "mode": mode,
        "slot_id": slot_id,
        "is_solo_restricted": is_solo_restricted,
        "bachelor_shares_instant": bachelor_shares_instant,
        "kicker": kicker,
        "title": title,
        "sub": sub,
        "cta_label": cta_label,
        "href": href,
        "tracking_pixel": tracking_pixel,
        "active_pixels": active_pixels,
        "sublink_href": sublink_href,
        "sublink_label": sublink_label,
        "women_sublink_href": "/for-women",
        "women_sublink_label": "女性ペア向け・待ちゼロ店＆無料デート特設 ›",
        "women_variant": women_variant,
    }


def build_surface_smart_cta(
    stores: list,
    source_path: str,
    surface_label: str = "対象店舗",
    area_slug: str = "",
    now_jst=None,
    allow_blocking: bool = False,
) -> dict:
    """
    Build a context-aware Smart CTA + 2-step editorial bridge card for high-intent tool/list surfaces
    (`/price-comparison`, `/stats`, `/oriental-lounge`, `/ag`, `/jis`, `/asobibar`).
    Adapts automatically to aggregate live ratio, JST time-of-day, URL-sharing honesty, and women audience mode.
    """
    import datetime as _dt

    JST = _dt.timezone(_dt.timedelta(hours=9))
    if now_jst is None:
        now_jst = _dt.datetime.now(JST)
    elif getattr(now_jst, "tzinfo", None) is None:
        now_jst = now_jst.replace(tzinfo=JST)
    else:
        now_jst = now_jst.astimezone(JST)

    clean_path = (source_path or "/").strip().split("?", 1)[0].rstrip("/") or "/"
    valid_stores = [
        s for s in (stores or [])
        if isinstance(s, dict) and s.get("region") != "Korea" and "ソウル" not in str(s.get("name") or "")
    ]
    active_stores = [s for s in valid_stores if (int(s.get("women") or 0) + int(s.get("men") or 0)) >= 6]
    sum_w = sum(int(s.get("women") or 0) for s in active_stores)
    sum_m = sum(int(s.get("men") or 0) for s in active_stores)
    male_heavy_cnt = sum(1 for s in active_stores if int(s.get("men") or 0) > int(s.get("women") or 0))

    is_late_night = (now_jst.hour >= 22) or (now_jst.hour < 5)
    is_evening_prep = 17 <= now_jst.hour < 22
    bachelor_shares_instant = are_slots_sharing_url("dating_bachelor", "dating_instant", allow_blocking=allow_blocking)
    concierge_href = f"/concierge?area={area_slug}" if area_slug else "/concierge"

    if len(active_stores) >= 2 and (sum_m > sum_w or male_heavy_cnt >= max(2, len(active_stores) // 2)):
        slot_id = "dating_instant"
        mode = "men_glut"
        kicker = f"現在 {surface_label}は男性先行傾向｜待機チャージ回避の選択肢"
        title = "単独待機10分330円を重ねる前に｜近くで今日飲める恋活アプリを併用"
        sub = "複数店舗で男性が先行している時間帯は、受付待ちや単独待機コストがかさみやすくなります。徒歩圏の穴場店へ回るか、同エリアで今から合流できる恋活アプリを予備に持っておくと無駄がありません。"
        cta_label = "近くで今日飲めるアプリを見る ›"
        sublink_href = concierge_href
        sublink_label = "いま待ちなしで入れる徒歩圏の穴場3秒診断を見る ›"
    elif len(active_stores) >= 1 and is_late_night:
        slot_id = "second_venue_reservation"
        mode = "late_second_venue"
        kicker = f"現在 {surface_label}で女性{sum_w}名稼働中｜着席45分後の席確保に"
        title = "2軒目バー・次回ディナーで迷わない「一休.comレストラン」空席検索"
        sub = "22時以降の繁華街は飛び込みだと2軒目が満席になりがちです。相席で会話が弾んだあとにスマートに案内できる駅チカの隠れ家バー・厳選ダイニングをスマホで押さえられます。"
        cta_label = "周辺の空席・プランを見る ›"
        sublink_href = "/guide/second-venue"
        sublink_label = "着席45分で自然に2軒目へ誘うフレーズ・立ち回り ›"
    elif len(active_stores) >= 1 and is_evening_prep:
        slot_id = "mens_skincare_clinic"
        mode = "prime_grooming"
        kicker = f"現在 {surface_label}で女性{sum_w}名｜入店前の清潔感＆服装確認"
        title = "ソファ席70cmの間接照明で差がつく「夕方の皮脂テカリ・口元ケア」"
        sub = "仕事帰りにそのまま入店する夜は、おでこ・鼻まわりの皮脂と口元の清潔感だけで最初の10分の会話継続率が変わります。日頃のベースケアには定番のORBIS Mr.も選ばれています。"
        cta_label = "初回トライアル・公式詳細 ›"
        sublink_href = "/grooming#sebum-care"
        sublink_label = "ブランド別NG服装規定・入店3分前チェックリスト ›"
    else:
        slot_id = "dating_bachelor"
        mode = "off_or_low"
        if bachelor_shares_instant:
            kicker = "営業時間外・1名利用や事前予定づくりの選択肢"
            title = "相席の賑やかさより1対1で落ち着いて話したい週に「定番恋活アプリ」"
            sub = "立ち上がり前の時間帯や相方と予定が合わない日は、単独チャージをかけずに近隣エリアで1対1のカフェ・食事相手を探せる定番アプリも効率的です。"
            cta_label = "1対1恋活・デートアプリの公式詳細 ›"
        else:
            kicker = "営業時間外・1名利用や事前予定づくりの選択肢"
            title = "メッセージ不要で週1回デートが決まる「審査制1対1カフェデート」"
            sub = "立ち上がり前の時間帯や相方と予定が合わない日は、希望日時を入れるだけでAIが1対1のカフェ・バーデートを調整する審査制アプリも便利です。"
            cta_label = "審査制1対1デートの公式詳細 ›"
        sublink_href = "/grooming#backup-apps"
        sublink_label = "入店前の準備アイテム＆満席時の代替サービス比較 ›"

    active_pixels = {}
    for _sid, _px in SLOT_TRACKING_PIXELS.items():
        if resolve_affiliate_url(_sid, allow_blocking=allow_blocking) == ((_SLOT_BY_ID.get(_sid) or {}).get("default_url") or ""):
            active_pixels[_sid] = _px

    clinic_monetized = is_slot_monetized("women_beauty_clinic", allow_blocking=allow_blocking)
    wdating_monetized = is_slot_monetized("women_dating_bachelor", allow_blocking=allow_blocking)

    if clinic_monetized and not is_late_night:
        w_link_id = "women_beauty_clinic"
        w_canon = "women_beauty_clinic"
        w_kicker = "女性向け｜間接照明70cmでも自信が持てる肌メンテナンス"
        w_title = "ラウンジの暖色ライトでも映える「美容皮膚科・医療脱毛 初回体験」"
        w_sub = "向かい合わせ70cmのソファ席でもうぶ毛や毛穴が気にならない水光肌へ。全国主要駅チカで通いやすい初回プランです。"
        w_cta_label = "初回キャンペーンを見る ›"
    elif wdating_monetized and len(active_stores) == 0:
        w_link_id = "women_dating_bachelor"
        w_canon = "women_dating_bachelor"
        w_kicker = "女性向け｜1人で動きたい週末の選択肢"
        w_title = "友達と予定を合わせず1対1でお茶できる「審査制カフェデート（女性無料）」"
        w_sub = "顔写真非公開・メッセージ往復なしで、審査通過男性との週末カフェデートをセッティングできる女性無料サービスです。"
        w_cta_label = "女性無料の公式詳細を見る ›"
    elif is_late_night:
        w_link_id = "women_cosme_salon"
        w_canon = "women_cosme_salon"
        w_kicker = "女性向け｜夜遊び帰りの髪・肌リセット"
        w_title = "空調の効いた店内帰りも翌朝まとまる「サロン品質ナイトヘア＆肌ケア」"
        w_sub = "お酒やスイーツを楽しんだ夜も、帰宅後シンプルなステップでツヤ髪とうるおい肌をキープできる人気サロンケアです。"
        w_cta_label = "公式ショップで詳細を見る ›"
    else:
        w_link_id = "women_second_venue"
        w_canon = "second_venue_reservation"
        w_kicker = "女性向け｜合流前の0次会カフェ・女子会ディナー予約に"
        w_title = "受付で待つより駅チカカフェで乾杯してから｜一休.comレストラン"
        w_sub = "相席前の0次会カフェやアフタヌーンティー、友達とのご褒美女子会ディナーをWEB限定プランでスマートに空席確保できます。"
        w_cta_label = "人気プラン・空席を見る ›"

    women_variant = {
        "slot_id": w_canon,
        "kicker": w_kicker,
        "title": w_title,
        "sub": w_sub,
        "cta_label": w_cta_label,
        "href": f"/link/{w_link_id}?from={clean_path}",
        "tracking_pixel": active_pixels.get(w_canon, ""),
        "sublink_href": "/for-women#live-board",
        "sublink_label": "いま女子待ちゼロで入れる全国LIVEボードを見る ›",
    }

    return {
        "mode": mode,
        "slot_id": slot_id,
        "bachelor_shares_instant": bachelor_shares_instant,
        "kicker": kicker,
        "title": title,
        "sub": sub,
        "cta_label": cta_label,
        "href": f"/link/{slot_id}?from={clean_path}",
        "tracking_pixel": active_pixels.get(slot_id, ""),
        "sublink_href": sublink_href,
        "sublink_label": sublink_label,
        "women_sublink_href": "/for-women#live-board",
        "women_sublink_label": "女性向け・待ちゼロ店舗＆無料ガイド ›",
        "women_variant": women_variant,
    }


def validate_destination_url(raw_url: str) -> tuple:
    """Validate that an affiliate destination URL uses http/https and has a valid network host."""
    u = (raw_url or "").strip()
    if not u:
        return True, ""
    if len(u) > 1000 or any(ord(c) < 32 for c in u):
        return False, "URLに不正な制御文字が含まれているか長すぎます。"
    parsed = urllib.parse.urlparse(u)
    if parsed.scheme not in ("https", "http") or not parsed.netloc or "." not in parsed.netloc:
        return False, "https:// または http:// から始まる有効なASPリンクを入力してください。"
    return True, u


def _load_overrides(allow_blocking: bool = False) -> dict:
    global _mem_overrides
    with _lock:
        if _mem_overrides is not None:
            return dict(_mem_overrides)
    raw = job_state.get_value(AFFILIATE_OVERRIDES_STATE_KEY, allow_blocking=allow_blocking, default="") or ""
    parsed = {}
    if raw:
        try:
            data = json.loads(raw)
            if isinstance(data, dict):
                parsed = data
        except Exception:
            parsed = {}
    if raw or getattr(job_state, "_sheets_loaded", False) or job_state.sheets_store.is_disabled():
        with _lock:
            _mem_overrides = dict(parsed)
    return parsed


def reset_cache_for_tests():
    global _mem_overrides
    with _lock:
        _mem_overrides = None


def _resolve_slot_info(slot_meta: dict, overrides: dict) -> dict:
    sid = slot_meta["slot_id"]
    ov = overrides.get(sid) if isinstance(overrides.get(sid), dict) else {}
    ov_url = (ov.get("url") or "").strip()
    ov_memo = (ov.get("memo") or "").strip()
    ov_updated = (ov.get("updated_at") or "").strip()

    if ov_url:
        return {
            "url": ov_url,
            "source": "override",
            "source_label": "🟢 管理画面カスタム設定",
            "memo": ov_memo,
            "updated_at": ov_updated,
        }

    # Check primary dedicated env var first (index 0) before fallback shared env var
    for idx, env_k in enumerate(slot_meta["env_keys"]):
        env_val = (os.environ.get(env_k) or "").strip()
        if env_val:
            return {
                "url": env_val,
                "source": "env" if idx == 0 else "env_fallback",
                "source_label": f"🔵 環境変数 ({env_k})",
                "memo": ov_memo,
                "updated_at": ov_updated,
            }

    return {
        "url": slot_meta["default_url"],
        "source": "default",
        "source_label": "⚪ 初期デフォルトURL",
        "memo": ov_memo,
        "updated_at": ov_updated,
    }


def resolve_affiliate_url(item_id: str, allow_blocking: bool = False) -> str:
    """Resolve `/link/<item_id>` to its active destination URL in <0.05ms."""
    cid = canonical_slot_id(item_id)
    if not cid:
        return ""
    slot_meta = _SLOT_BY_ID[cid]
    overrides = _load_overrides(allow_blocking=allow_blocking)
    return _resolve_slot_info(slot_meta, overrides)["url"]


def set_affiliate_slot_override(slot_id: str, target_url: str, memo: str = "") -> dict:
    """Save or reset (when target_url is empty) a destination URL override for a canonical slot."""
    global _mem_overrides
    import datetime
    cid = canonical_slot_id(slot_id)
    if not cid:
        return {"ok": False, "error": f"不明なアフィリエイト枠IDです: {slot_id}"}
    ok_url, cleaned_url_or_err = validate_destination_url(target_url)
    if not ok_url:
        return {"ok": False, "error": cleaned_url_or_err}

    if not job_state.is_storage_healthy():
        return {
            "ok": False,
            "error": "永続ストレージの初期同期が完了していないため、一時的に保存をブロックしました。数秒後に再度お試しください。",
        }

    cleaned_memo = str(memo or "").strip()[:80]
    now_str = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d %H:%M")

    with _lock:
        prior_mem = dict(_mem_overrides) if isinstance(_mem_overrides, dict) else {}
    loaded = _load_overrides(allow_blocking=True)
    overrides = dict(prior_mem)
    overrides.update(loaded)
    if cleaned_url_or_err:
        overrides[cid] = {
            "url": cleaned_url_or_err,
            "memo": cleaned_memo,
            "updated_at": now_str,
        }
    else:
        if cleaned_memo:
            overrides[cid] = {"url": "", "memo": cleaned_memo, "updated_at": now_str}
        else:
            overrides.pop(cid, None)

    with _lock:
        _mem_overrides = dict(overrides)
    job_state.set_value(AFFILIATE_OVERRIDES_STATE_KEY, json.dumps(overrides, ensure_ascii=False))

    slot_meta = _SLOT_BY_ID[cid]
    resolved = _resolve_slot_info(slot_meta, overrides)
    return {
        "ok": True,
        "slot_id": cid,
        "resolved": resolved,
        "message": (
            f"『{slot_meta['category_label']}』の遷移先URLを更新しました（再デプロイ不要・即時反映済み）！"
            if cleaned_url_or_err
            else f"『{slot_meta['category_label']}』をデフォルトURLへリセットしました。"
        ),
    }


def get_affiliate_slots_status(analytics_data: dict = None, allow_blocking: bool = False) -> dict:
    """
    Build complete status table for `/staff/analytics`:
      - Current resolved URL, source badge, memo, period impressions / clicks / CTR (summing aliases)
      - Automatic detection when two distinct slots share the exact same URL (cannibalization alert)
      - Automatic detection when a slot resolves to a non-ASP direct brand URL (0-yen fallback alert)
    """
    overrides = _load_overrides(allow_blocking=allow_blocking)

    # Aggregate clicks & impressions per canonical slot from `analytics.summary()` (`all_by_label` preferred)
    slot_clicks = {s["slot_id"]: 0 for s in AFFILIATE_SLOTS}
    slot_impressions = {s["slot_id"]: 0 for s in AFFILIATE_SLOTS}
    if isinstance(analytics_data, dict):
        all_labels = analytics_data.get("all_by_label") or {}
        by_label = analytics_data.get("by_label") or {}

        raw_aff_pairs = all_labels.get("affiliate_click") or by_label.get("affiliate_click") or []
        pair_iter = raw_aff_pairs.items() if isinstance(raw_aff_pairs, dict) else raw_aff_pairs
        for raw_lbl, cnt in pair_iter:
            item_part = str(raw_lbl).split("@", 1)[0].strip().lower()
            cid = canonical_slot_id(item_part)
            if cid in slot_clicks:
                slot_clicks[cid] += int(cnt or 0)

        raw_imp_pairs = all_labels.get("affiliate_impression") or by_label.get("affiliate_impression") or []
        imp_iter = raw_imp_pairs.items() if isinstance(raw_imp_pairs, dict) else raw_imp_pairs
        for raw_lbl, cnt in imp_iter:
            item_part = str(raw_lbl).split("@", 1)[0].strip().lower()
            cid = canonical_slot_id(item_part)
            if cid in slot_impressions:
                slot_impressions[cid] += int(cnt or 0)

    resolved_map = {s["slot_id"]: _resolve_slot_info(s, overrides) for s in AFFILIATE_SLOTS}
    # Group duplicate detection by (audience, url) so men's vs women's funnels don't trigger false cross-gender alerts,
    # while still catching any URL cannibalization inside the men's 5 slots or inside the women's 3 slots!
    url_to_slots = {}
    for s in AFFILIATE_SLOTS:
        sid = s["slot_id"]
        aud = s.get("audience") or "men"
        u = resolved_map[sid]["url"]
        url_to_slots.setdefault((aud, u), []).append(sid)

    slots_out = []
    duplicate_pair_count = 0
    unmonetized_slot_count = 0
    total_clicks = 0
    total_impressions = 0
    for s in AFFILIATE_SLOTS:
        sid = s["slot_id"]
        aud = s.get("audience") or "men"
        rinfo = resolved_map[sid]
        shared_with = [other for other in url_to_slots.get((aud, rinfo["url"]), []) if other != sid]
        is_dup = len(shared_with) > 0
        if is_dup:
            duplicate_pair_count += 1
        is_unmonetized = not is_asp_tracking_url(rinfo["url"])
        if is_unmonetized:
            unmonetized_slot_count += 1
        clk = slot_clicks.get(sid, 0)
        imp = slot_impressions.get(sid, 0)
        total_clicks += clk
        total_impressions += imp
        ctr_pct = min(100.0, round((clk / imp) * 100, 2)) if imp > 0 else 0.0
        shared_labels = [_SLOT_BY_ID[o]["category_label"] for o in shared_with]
        slots_out.append({
            "slot_id": sid,
            "audience": aud,
            "audience_label": s.get("audience_label") or ("💄 女性向け" if aud == "women" else "🕺 男性向け"),
            "aliases": s["aliases"],
            "test_path": f"/link/{sid}",
            "category_label": s["category_label"],
            "title": s["title"],
            "placement": s["placement"],
            "recommended_asp_examples": s["recommended_asp_examples"],
            "active_url": rinfo["url"],
            "has_override": rinfo["source"] == "override",
            "override_url": rinfo["url"] if rinfo["source"] == "override" else "",
            "source": rinfo["source"],
            "source_label": rinfo["source_label"],
            "memo": rinfo["memo"],
            "updated_at": rinfo["updated_at"],
            "clicks": clk,
            "impressions": imp,
            "ctr_pct": ctr_pct,
            "is_duplicate_url": is_dup,
            "is_unmonetized_url": is_unmonetized,
            "shared_with_labels": shared_labels,
        })

    return {
        "slots": slots_out,
        "total_slots": len(slots_out),
        "has_duplicates": duplicate_pair_count > 0,
        "duplicate_slot_count": duplicate_pair_count,
        "has_unmonetized": unmonetized_slot_count > 0,
        "unmonetized_slot_count": unmonetized_slot_count,
        "total_clicks": total_clicks,
        "total_impressions": total_impressions,
        "overall_ctr_pct": min(100.0, round((total_clicks / total_impressions) * 100, 2)) if total_impressions > 0 else 0.0,
    }
