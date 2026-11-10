"""Public portal pages, store/brand/area directories, concierge APIs, legal pages, and editorial guides Blueprint."""

import csv
import io
import logging
import math
import threading
import time
import urllib.parse

from flask import Blueprint, Response, abort, jsonify, make_response, redirect, render_template, request, session, url_for

from portal_guides import AREA_GUIDES, BRAND_GUIDES, get_stores_for_area
from routes._bridge import core

portal_bp = Blueprint('portal_bp', __name__)

STORE_PAGE_NEARBY_LIMIT = 6
STORE_PAGE_COMPARE_LIMIT = 4

_MONTHLY_CSV_CACHE = {}
_MONTHLY_CSV_LOCK = threading.Lock()
_MONTHLY_CSV_TTL = 900.0

# Public-request forecast backfill debounce (15 min) so cold /stats or /api/forecast-accuracy
# traffic cannot continuously re-spawn heavy history scans on the 512MB Fly Postgres VM.
_last_public_backfill_mono = 0.0
_PUBLIC_BACKFILL_DEBOUNCE_SEC = 900.0
_public_backfill_lock = threading.Lock()


def _maybe_trigger_public_backfill(rep) -> None:
    """Trigger async forecast backfill from public traffic at most once per 15 minutes (never in testing)."""
    global _last_public_backfill_mono
    if core.app.testing or not isinstance(rep, dict):
        return
    try:
        if int((rep.get('summary') or {}).get('resolved_count', 0) or 0) != 0:
            return
    except (TypeError, ValueError):
        return
    now_mono = time.monotonic()
    with _public_backfill_lock:
        if now_mono - _last_public_backfill_mono < _PUBLIC_BACKFILL_DEBOUNCE_SEC:
            return
        _last_public_backfill_mono = now_mono
    try:
        core.local_db.trigger_forecast_backfill_async(days=30, min_existing=80, max_rows=4500)
    except Exception as err:
        logging.getLogger(__name__).warning('public forecast backfill trigger failed: %s', err)


def _gov_hidden_sets() -> tuple:
    """Fetch governance hidden brand/store sets once (never raises)."""
    try:
        import ops_governance
        return set(ops_governance.get_hidden_brands() or ()), set(ops_governance.get_hidden_stores() or ())
    except Exception:
        return set(), set()


def _is_hidden_with(store_name: str, hb: set, hs: set) -> bool:
    """Signature-independent hidden check using pre-fetched sets (avoids N× job_state reads in loops)."""
    if not store_name or (not hb and not hs):
        return False
    try:
        import ops_governance
        import store_master
        canon = store_master.to_internal_store_name(store_name) or store_name
        if canon in hs or store_name in hs:
            return True
        return ops_governance.ops_brand_key_of(canon) in hb
    except Exception:
        return False


def clear_monthly_csv_cache():
    with _MONTHLY_CSV_LOCK:
        _MONTHLY_CSV_CACHE.clear()


def _nearest_stores_with_km(sname, candidates, limit):
    """Return [(store_name, km_or_None), ...] ordered by straight-line distance from `sname`."""
    import store_master
    base = store_master.get(sname) or {}
    blat, blng = base.get('lat'), base.get('lng')
    cands = [c for c in candidates if c and c != sname]
    if blat is None or blng is None:
        return [(c, None) for c in cands[:limit]]

    def _dist(name):
        meta = store_master.get(name) or {}
        lat, lng = meta.get('lat'), meta.get('lng')
        if lat is None or lng is None:
            return float('inf')
        p1, p2 = math.radians(blat), math.radians(lat)
        dp, dl = p2 - p1, math.radians(lng - blng)
        a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
        return 6371.0 * 2 * math.asin(math.sqrt(min(1.0, a)))

    scored = sorted(((c, _dist(c)) for c in cands), key=lambda t: t[1])
    return [(c, (None if d == float('inf') else d)) for c, d in scored[:limit]]


def _nearest_store_names(sname, candidates, limit):
    """Order candidate store names by straight-line distance from `sname` (store_master lat/lng)."""
    return [n for n, _ in core._nearest_stores_with_km(sname, candidates, limit)]


def _trim_nearby_comparison(sname, comparison, limit):
    """Keep the current store and its nearest rivals (max `limit` columns).
    The full cluster board stays available on /price-comparison."""
    if not comparison or not comparison.get('stores'):
        return comparison
    stores = list(comparison['stores'])
    if len(stores) <= limit:
        return comparison
    by_name = {st.get('name') or st.get('store_name'): st for st in stores}
    keep = [st for st in stores if st.get('is_current_store')]
    rest_names = [n for n, st in by_name.items() if n and st not in keep]
    for n in core._nearest_store_names(sname, rest_names, len(rest_names)):
        if len(keep) >= limit:
            break
        keep.append(by_name[n])
    trimmed = dict(comparison)
    trimmed['stores'] = keep[:limit]
    trimmed['total_cluster_stores'] = len(stores)
    return trimmed


@portal_bp.route('/oriental-lounge')
def brand_oriental_lounge():
    return _render_brand_page('oriental-lounge')


@portal_bp.route('/ag')
def brand_ag():
    return _render_brand_page('ag')


@portal_bp.route('/jis')
def brand_jis():
    return _render_brand_page('jis')


@portal_bp.route('/asobibar')
def brand_asobibar():
    return _render_brand_page('asobibar')


def _render_brand_page(brand_slug):
    import ops_governance
    import store_hours
    import store_master
    if ops_governance.is_brand_hidden(brand_slug):
        resp = make_response(
            render_template(
                'error.html',
                code=410,
                title='このページの掲載は終了しました',
                message='運営方針に基づき、対象ブランドの掲載を取り下げています。',
            ),
            410,
        )
        resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return resp
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    brand = BRAND_GUIDES.get(brand_slug)
    if not brand:
        return redirect(url_for('index'))

    def _render():
        with core.data_lock:
            raw_copy = list(core.latest_data.get('full_data') or [])
            last_updated = core.latest_data.get('last_updated') or '--:--'
        raw_stores = ops_governance.filter_visible_stores(raw_copy)

        master = store_master.load() or {}
        live_map = {s.get('name'): s for s in raw_stores}
        hb, hs = _gov_hidden_sets()
        all_names = [
            n for n in dict.fromkeys(list(live_map.keys()) + list(master.keys()))
            if n and not _is_hidden_with(n, hb, hs)
        ]

        matched = []
        for sname in all_names:
            if store_master.brand_key_of(sname) == brand_slug:
                s_live = live_map.get(sname, {})
                info = master.get(sname, {})
                reg = info.get('region') or s_live.get('region') or core.detect_region(sname)
                w_cnt = int(s_live.get('women', 0))
                m_cnt = int(s_live.get('men', 0))
                h_info = store_hours.get_business_hours_status(sname, total_guests=(w_cnt + m_cnt))
                matched.append({
                    'name': sname,
                    'seo_name': store_master.seo_name(sname),
                    'slug': store_master.slug_of(sname),
                    'region': reg,
                    'area_label': info.get('area_label') or s_live.get('area') or '全国',
                    'women': w_cnt,
                    'men': m_cnt,
                    'women_diff': int(s_live.get('women_diff', 0) or 0),
                    'men_diff': int(s_live.get('men_diff', 0) or 0),
                    'is_open_now': h_info['is_open_now'],
                    'today_hours_label': h_info['today_hours_label'],
                    'hours_label': h_info['hours_label'],
                })

        matched.sort(key=lambda x: (x.get('region') != 'Korea', x['women'], x['men'], bool(x.get('is_open_now'))), reverse=True)
        dom_rank = 0
        for item in matched:
            if item.get('region') != 'Korea':
                dom_rank += 1
                item['rank_num'] = dom_rank
            else:
                item['rank_num'] = None
        total_w = sum(x['women'] for x in matched)
        total_m = sum(x['men'] for x in matched)
        import affiliate_manager
        import price_comparison
        brand_code_map = {
            'oriental-lounge': 'OLG',
            'ag': 'ag',
            'jis': 'JIS',
            'asobibar': 'ASOBIBAR',
        }
        brand_spec = price_comparison.BRAND_PRICE_SPECS.get(brand_code_map.get(brand_slug, ''))
        surface_smart_cta = affiliate_manager.build_surface_smart_cta(
            matched,
            source_path=f"/{brand_slug}",
            surface_label=f"{brand['name']}全店",
            allow_blocking=False,
        )

        return render_template(
            'brand_page.html',
            brand=brand,
            brand_spec=brand_spec,
            stores=matched,
            store_count=len(matched),
            total_women=total_w,
            total_men=total_m,
            last_updated=last_updated,
            surface_smart_cta=surface_smart_cta,
            is_staff=bool(session.get('is_staff')),
        )
    import affiliate_manager as _am_brand
    brand_cache_sig = (
        'brand',
        brand_slug,
        _am_brand.are_slots_sharing_url('dating_bachelor', 'dating_instant', allow_blocking=False),
        _am_brand.is_slot_monetized('women_beauty_clinic', allow_blocking=False),
        _am_brand.is_slot_monetized('women_dating_bachelor', allow_blocking=False),
    )
    return core._get_cached_html_or_render(brand_cache_sig, _render)


@portal_bp.route('/store/<store_slug>')
def store_detail_page(store_slug):
    import ops_governance
    import store_hours
    import store_master
    legacy_target = store_master.legacy_redirect_for_slug(store_slug)
    if legacy_target:
        return core._redirect_preserving_query(legacy_target, code=301)

    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    with core.data_lock:
        raw_copy = list(core.latest_data.get('full_data') or [])
        last_updated = core.latest_data.get('last_updated') or '--:--'
    raw_stores = ops_governance.filter_visible_stores(raw_copy)

    sname = store_master.store_by_slug(store_slug, live_stores=raw_stores)
    if not sname:
        abort(404)
    if ops_governance.is_store_hidden(sname):
        resp = make_response(
            render_template(
                'error.html',
                code=410,
                title='この店舗ページの掲載は終了しました',
                message='運営方針に基づき、当該店舗の掲載を取り下げています。',
            ),
            410,
        )
        resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return resp

    def _render():
        store_analytics = None
        try:
            store_analytics = core.local_db.get_store_7day_analytics(sname)
        except Exception:
            pass
        info = store_master.get(sname) or {}
        live_map = {s.get('name'): s for s in raw_stores}
        s_live = live_map.get(sname, {})

        women = int(s_live.get('women', 0))
        men = int(s_live.get('men', 0))
        tot = women + men
        wpct = round((women / tot) * 100) if tot > 0 else 50
        hours_info = store_hours.get_business_hours_status(sname, total_guests=tot)

        bkey = store_master.brand_key_of(sname)
        raw_brand = info.get('brand') or ''
        if bkey == 'oriental-lounge':
            brand_label = 'オリエンタルラウンジ'
            brand_slug = 'oriental-lounge'
            official_url = 'https://oriental-lounge.com/'
        elif bkey == 'ag':
            brand_label = 'ag（アグ）'
            brand_slug = 'ag'
            official_url = 'https://ag-ree.jp/'
        elif bkey == 'jis':
            brand_label = 'JIS（ジス）'
            brand_slug = 'jis'
            official_url = 'https://jis.bar/'
        elif bkey == 'asobibar':
            brand_label = 'ASOBIBAR（アソビバー）'
            brand_slug = 'asobibar'
            official_url = 'https://asobibar.net/'
        else:
            brand_label = raw_brand or '相席ラウンジ'
            brand_slug = ''
            official_url = {
                'ALFA': 'https://aiseki-hiroshima.com/',
                'CLOVERS': 'https://bar-clovers.com/',
                'XIX': 'https://xix-thelounge.com/',
                'YATAKOI': 'https://yatakoi.com/',
                'ヤタコイ': 'https://yatakoi.com/',
            }.get(raw_brand, 'https://oriental-lounge.com/')

        if tot == 0:
            if hours_info['is_open_now']:
                crowd_comment = f"営業中｜現在 0 : 0（本日 {hours_info['today_hours_label']}）"
            else:
                crowd_comment = f"営業時間外（本日 {hours_info['today_hours_label']}）"
        elif women < 10:
            if women >= men:
                crowd_comment = f'少人数・立ち上がり中（女性{women}名｜基準10名未満）'
            else:
                crowd_comment = f'少人数・男性先行（女性{women}名 / 男性{men}名）'
        elif women > men:
            crowd_comment = f'女性優位・好バランス（女性+{women - men}名）'
        elif women == men:
            crowd_comment = '男女比1:1 バランス良好'
        else:
            crowd_comment = '男性先行・近隣店舗との比較推奨'

        map_anchor = info.get('map_anchor') or store_master.seo_name(sname)
        map_url = f"https://www.google.com/maps/search/?api=1&query={urllib.parse.quote(map_anchor)}"

        area_slug = info.get('area_slug')
        area_info = AREA_GUIDES.get(area_slug) if area_slug else None

        nearby_limit = core.STORE_PAGE_NEARBY_LIMIT
        nearby_stores = []
        if area_slug:
            area_live = [o for o in get_stores_for_area(area_slug, raw_stores) if o.get('name') and o.get('name') != sname]
            nearest_names = core._nearest_store_names(sname, [o.get('name') for o in area_live], nearby_limit)
            area_live.sort(key=lambda o: nearest_names.index(o.get('name')) if o.get('name') in nearest_names else 999)
            for other_s in area_live[:nearby_limit]:
                oname = other_s.get('name')
                if oname and oname != sname:
                    ow = int(other_s.get('women', 0))
                    om = int(other_s.get('men', 0))
                    otot = ow + om
                    oh_info = store_hours.get_business_hours_status(oname, total_guests=otot)
                    nearby_stores.append({
                        'name': oname,
                        'seo_name': store_master.seo_name(oname),
                        'slug': store_master.slug_of(oname),
                        'women': ow,
                        'men': om,
                        'women_diff': int(other_s.get('women_diff', 0) or 0),
                        'men_diff': int(other_s.get('men_diff', 0) or 0),
                        'wpct': round((ow / otot) * 100) if otot > 0 else 0,
                        'is_open_now': oh_info['is_open_now'],
                        'today_hours_label': oh_info['today_hours_label'],
                        'hours_label': oh_info['hours_label'],
                        'hours_spec': oh_info['schedule_spec'],
                    })

        store_obj = {
            'name': sname,
            'seo_name': store_master.seo_name(sname),
            'slug': store_master.slug_of(sname),
            'area_label': info.get('area_label') or s_live.get('area') or '全国',
            'brand_label': brand_label,
            'brand_slug': brand_slug,
            'official_url': official_url,
            'women': women,
            'men': men,
            'women_diff': int(s_live.get('women_diff', 0) or 0),
            'men_diff': int(s_live.get('men_diff', 0) or 0),
            'total': tot,
            'wpct': wpct if tot > 0 else 0,
            'crowd_comment': crowd_comment,
            'map_url': map_url,
            'is_open_now': hours_info['is_open_now'],
            'today_hours_label': hours_info['today_hours_label'],
            'hours_label': hours_info['hours_label'],
            'hours_spec': hours_info['schedule_spec'],
            'opening_hours_specification': hours_info.get('opening_hours_specification') or store_hours.get_store_opening_hours_specification(sname),
            'lat': info.get('lat'),
            'lng': info.get('lng'),
            'map_anchor': map_anchor,
            'pos_telemetry': s_live.get('pos_telemetry'),
        }
        try:
            core._attach_store_benchmarks(store_obj, sname, women, men, core.local_db.get_all_stores_live_benchmarks(allow_blocking=False))
        except Exception:
            core._attach_store_benchmarks(store_obj, sname, women, men, {})
        try:
            import moat_intelligence
            moat_intelligence.enrich_stores_with_moat_signals(
                [store_obj], allow_weather_blocking=False, context_stores=raw_stores
            )
        except Exception:
            pass

        nearby_comparison = None
        try:
            import price_comparison
            nearby_comparison = price_comparison.get_nearby_comparison_for_store(sname, live_stores=raw_stores)
            nearby_comparison = core._trim_nearby_comparison(sname, nearby_comparison, core.STORE_PAGE_COMPARE_LIMIT)
        except Exception:
            pass

        store_insight = None
        try:
            import store_insights
            area_peer_names = [
                n for n, meta in store_master.load().items()
                if area_slug and (meta or {}).get('area_slug') == area_slug
            ]
            store_insight = store_insights.build_store_insight(
                sname,
                store_master.seo_name(sname),
                area_label=(area_info or {}).get('short_name') or store_obj['area_label'],
                area_store_names=area_peer_names,
                seo_name_fn=store_master.seo_name,
                nearest=core._nearest_stores_with_km(
                    sname, [n for n in store_master.load().keys() if 'ソウル' not in n], 3
                ),
            )
        except Exception as e:
            logging.warning(f"[store_insight] {sname}: {e}")

        import store_dossiers
        store_dossier = store_dossiers.get_store_dossier(sname)

        has_sufficient_store_data = bool(
            tot > 0
            or store_insight
            or store_dossier
            or (store_analytics and int(store_analytics.get('max_7d_women') or 0) > 0)
        )

        store_smart_cta = None
        if 'ソウル' not in sname:
            try:
                import affiliate_manager
                store_smart_cta = affiliate_manager.build_store_smart_cta(
                    store_obj,
                    nearby_stores=nearby_stores,
                    area_slug=area_slug,
                )
            except Exception as e:
                logging.warning(f"[store_smart_cta] {sname}: {e}")

        store_votes = {'smooth': 0, 'waiting': 0, 'lively': 0, 'reported_wait_avg': None, 'reported_wait_samples': 0}
        watch_counts = {'store_name': sname, 'watching_count': 0}
        if 'ソウル' not in sname:
            try:
                store_votes = core.local_db.get_recent_votes(store_name=sname, hours=4) or store_votes
            except Exception:
                pass
            try:
                watch_counts = core.local_db.get_watch_intent_counts(store_name=sname, hours=6) or watch_counts
            except Exception:
                pass

        related_guides = []
        if brand_slug:
            related_guides.append({
                'href': f'/{brand_slug}',
                'title': f'{brand_label}全店の現在人数・料金早見一覧',
            })
        if area_slug == 'shinjuku' or '新宿' in sname:
            related_guides.append({
                'href': '/guide/shinjuku',
                'title': '新宿の相席ラウンジ3店舗データ比較｜東口2店と西新宿の使い分け',
            })
        elif area_slug == 'shibuya':
            related_guides.append({
                'href': '/guide/shibuya-ebisu-comparison',
                'title': '渋谷・恵比寿5店舗の時間波形と山手線1駅比較',
            })
        elif area_slug == 'tokyo-east':
            related_guides.append({
                'href': '/guide/ueno-kinshicho-machida',
                'title': '上野・錦糸町・町田・柏 沿線5店舗の実測データ比較',
            })
        elif area_slug in ('kanto', 'yokohama', 'omiya'):
            related_guides.append({
                'href': '/guide/yokohama-omiya-kanto',
                'title': '横浜・大宮・宇都宮・高崎 関東5店舗の時間波形・料金比較',
            })
        elif area_slug in ('osaka', 'umeda', 'namba'):
            try:
                _osaka_item = core.scheduled_articles.get_article_by_slug('osaka-kita-vs-minami')
                if _osaka_item and core.scheduled_articles.is_published(_osaka_item):
                    related_guides.append({
                        'href': '/guide/osaka-kita-vs-minami',
                        'title': '大阪キタ（梅田）vsミナミ（難波・心斎橋）実測データ比較',
                    })
            except Exception:
                pass
        elif area_slug == 'nagoya':
            related_guides.append({
                'href': '/guide/nagoya-sakae-nishiki',
                'title': '名古屋（栄・錦・住吉）5店舗の時間波形と料金比較',
            })
        elif area_slug == 'fukuoka':
            related_guides.append({
                'href': '/guide/fukuoka-tenjin-kyushu',
                'title': '福岡・天神と九州・沖縄11店舗の3ブランド使い分け・実測比較',
            })
        elif area_slug in ('chugoku-shikoku', 'hiroshima'):
            related_guides.append({
                'href': '/guide/hiroshima-okayama-matsuyama',
                'title': '広島・岡山・松山の6店舗比較｜新天地3店競合と料金構造',
            })
        elif area_slug in ('hokkaido-tohoku', 'sapporo', 'sendai'):
            related_guides.append({
                'href': '/guide/sapporo-sendai-north',
                'title': '札幌すすきの・仙台国分町｜JIS札幌・ag仙台の時間波形と曜日ルール',
            })

        _single_hub_keywords = (
            '金沢', '静岡', '浜松', '新潟', '熊本', '鹿児島', '宮崎', '長崎', '大分',
            '小倉', '沖縄', '松山', '岡山', '高崎', '宇都宮', '柏', '町田',
        )
        _adult_keywords = ('恵比寿', '銀座', '西新宿', '京都', '神戸', '大阪駅前', '梅田')
        if any(k in sname for k in _single_hub_keywords):
            try:
                _loc_item = core.scheduled_articles.get_article_by_slug('local-vs-urban')
                if _loc_item and core.scheduled_articles.is_published(_loc_item):
                    related_guides.append({
                        'href': '/guide/local-vs-urban',
                        'title': '地方1店舗都市と都心大型店の違い｜深夜0時に女性が121%へ増える理由',
                    })
            except Exception:
                pass
        if any(k in sname for k in _adult_keywords):
            try:
                _th_item = core.scheduled_articles.get_article_by_slug('thirties-lounge-guide')
                if _th_item and core.scheduled_articles.is_published(_th_item):
                    related_guides.append({
                        'href': '/guide/thirties-lounge-guide',
                        'title': '30代・アラサーの相席ラウンジ店選び｜落ち着いて話せる店舗と時間帯',
                    })
            except Exception:
                pass

        related_guides.extend([
            {
                'href': '/guide/brand-waveform-asymmetry',
                'title': '5大ブランドの時間波形と男女で真逆になる最適店舗選択',
            },
            {
                'href': '/guide/brand-comparison',
                'title': 'オリエンタルラウンジ・ag（アグ）・JIS（ジス）比較ガイド',
            },
            {
                'href': '/guide/solo-vs-pair',
                'title': '男性1人（ソロ）入店vs2人組の料金・相席率データ検証',
            },
            {
                'href': '/guide/dress-code-cleanliness',
                'title': '服装規定（ドレスコード）と距離70cmの清潔感チェック',
            },
            {
                'href': '/guide/lounge-vs-screened-date',
                'title': '相席ラウンジvs審査制1対1デート 時間帯・予算別の期待値比較',
            },
        ])
        related_guides = related_guides[:6]

        import second_venues
        store_second_venues = None
        try:
            store_second_venues = second_venues.get_second_venues_for_store(sname)
        except Exception as e:
            logging.warning(f"[second_venues] {sname}: {e}")

        return render_template(
            'store_detail.html',
            store=store_obj,
            store_analytics=store_analytics,
            store_insight=store_insight,
            store_dossier=store_dossier,
            has_sufficient_store_data=has_sufficient_store_data,
            store_smart_cta=store_smart_cta,
            store_second_venues=store_second_venues,
            store_votes=store_votes,
            watch_counts=watch_counts,
            area_info=area_info,
            area_slug=area_slug,
            nearby_stores=nearby_stores,
            nearby_comparison=nearby_comparison,
            related_guides=related_guides,
            jp_holidays=sorted(store_hours.JP_HOLIDAYS),
            last_updated=last_updated,
            is_staff=bool(session.get('is_staff')),
        )
    import affiliate_manager
    store_mon_sig = (
        affiliate_manager.is_slot_monetized('women_beauty_clinic', allow_blocking=False),
        affiliate_manager.is_slot_monetized('women_dating', allow_blocking=False),
        affiliate_manager.are_slots_sharing_url('dating_bachelor', 'dating_instant', allow_blocking=False),
    )
    return core._get_cached_html_or_render(('store', store_slug, store_mon_sig), _render)


@portal_bp.route('/columns')
def columns_index():
    """Portal page listing all editorial guides, comparisons, and area features."""
    is_staff = bool(session.get('is_staff'))
    is_master = bool(session.get('is_master'))
    pub_items = core.scheduled_articles.get_published_articles()
    up_items = core.scheduled_articles.get_upcoming_articles() if is_staff else []
    cache_sig = (
        'columns',
        is_staff,
        is_master,
        tuple(it.get('slug') for it in pub_items),
        tuple(it.get('slug') for it in up_items),
    )
    return core._get_cached_html_or_render(
        cache_sig,
        lambda: render_template(
            'columns.html',
            area_guides=AREA_GUIDES,
            published_scheduled=pub_items,
            upcoming_scheduled=up_items,
            is_staff=is_staff,
            is_master=is_master,
        ),
    )


@portal_bp.route('/api/forecast-accuracy')
def api_forecast_accuracy():
    """Public JSON report of 60-minute AI crowd forecast vs. actual outcome verification."""
    raw_days = max(1, min(120, request.args.get('days', 90, type=int) or 90))
    days = min((7, 14, 21, 30, 60, 90, 120), key=lambda d: abs(d - raw_days))
    store_q = (request.args.get('store') or '').strip() or None
    if store_q is not None and (len(store_q) > 80 or any(ord(c) < 32 or ord(c) == 127 for c in store_q)):
        return jsonify({'ok': False, 'error': 'Invalid store parameter'}), 400
    limit_q = max(5, min(100, request.args.get('limit', 30, type=int) or 30))
    try:
        import store_master
        rep = core.local_db.get_forecast_accuracy_report(
            days=days,
            store_name=store_q,
            limit_recent=limit_q,
            allow_blocking=True,
            auto_backfill=False,
        )
        if store_q is None:
            _maybe_trigger_public_backfill(rep)

        def _clean_forecast_rep(d):
            if not isinstance(d, dict):
                return d
            out = dict(d)
            if 'store_filter' in out and out['store_filter']:
                out['store_filter'] = store_master.seo_name(out['store_filter'])
            for list_key in ('by_store', 'recent_resolved', 'pending_forecasts', 'bias_adjustments'):
                if list_key in out and isinstance(out[list_key], list):
                    new_l = []
                    for it in out[list_key]:
                        if isinstance(it, dict):
                            it_copy = dict(it)
                            if 'store_name' in it_copy:
                                it_copy['store_name'] = store_master.seo_name(it_copy['store_name'])
                            new_l.append(it_copy)
                        else:
                            new_l.append(it)
                    out[list_key] = new_l
            if 'summary' in out and isinstance(out['summary'], dict):
                s_copy = dict(out['summary'])
                for sk in ('top_underperforming', 'top_overperforming', 'best_store', 'worst_store'):
                    if sk in s_copy and s_copy[sk]:
                        s_copy[sk] = store_master.seo_name(str(s_copy[sk]))
                out['summary'] = s_copy
            return out

        clean_rep = _clean_forecast_rep(rep)
        return jsonify({'ok': True, **clean_rep})
    except Exception as err:
        # Never echo raw DB/driver exception text (schema, DSN fragments) to public clients.
        logging.getLogger(__name__).exception('api_forecast_accuracy failed: %s', err)
        return jsonify({'ok': False, 'error': 'Forecast accuracy report temporarily unavailable'}), 500


@portal_bp.route('/stats')
def stats_whitepaper():
    """SEO-indexed primary statistical data whitepaper & weekly ranking page."""
    import weekly_report
    core.trigger_background_fetch_if_needed()
    raw_fc_days = request.args.get('days', 90, type=int) or 90
    fc_days = min((30, 60, 90), key=lambda d: abs(d - raw_fc_days))
    cached_rep = (getattr(weekly_report, '_report_cache', None) or {}).get('data')
    rep_sig = cached_rep.get('generated_at') if isinstance(cached_rep, dict) else None
    # Signature probe is strictly non-blocking (memory cache only) so /stats cache hits never touch the DB.
    try:
        fc_probe = core.local_db.get_forecast_accuracy_report(
            days=fc_days,
            limit_recent=20,
            allow_blocking=False,
            auto_backfill=False,
        )
    except Exception:
        fc_probe = None
    fc_probe_summary = (fc_probe.get('summary') or {}) if isinstance(fc_probe, dict) else {}
    fc_sig = (fc_days, fc_probe_summary.get('resolved_count', 0), fc_probe_summary.get('pending_count', 0))

    def _render():
        import ops_governance
        try:
            fc_acc = core.local_db.get_forecast_accuracy_report(
                days=fc_days,
                limit_recent=20,
                allow_blocking=True,
                auto_backfill=False,
            )
            _maybe_trigger_public_backfill(fc_acc)
        except Exception:
            fc_acc = None
        hb, hs = _gov_hidden_sets()
        report_data = None
        try:
            res = weekly_report.generate_weekly_report()
            if res and 'error' not in res:
                report_data = dict(res)
                if isinstance(report_data.get('anaba_stores'), list):
                    report_data['anaba_stores'] = [
                        s for s in report_data['anaba_stores']
                        if not _is_hidden_with((s or {}).get('name', ''), hb, hs)
                    ]
                if isinstance(report_data.get('stores'), list):
                    report_data['stores'] = [
                        s for s in report_data['stores']
                        if not _is_hidden_with((s or {}).get('name', ''), hb, hs)
                    ]
        except Exception:
            report_data = None
        try:
            moat_coverage = core.local_db.get_moat_coverage_summary()
        except Exception:
            moat_coverage = {}
        with core.data_lock:
            raw_live = list(core.latest_data.get('full_data') or [])
            last_updated = core.latest_data.get('last_updated') or '--:--'
        import store_master
        master = store_master.load() or {}
        live_stores = []
        for s_raw in ops_governance.filter_visible_stores(raw_live):
            s_copy = dict(s_raw)
            sname = s_copy.get('name') or ''
            try:
                w_cnt = max(0, int(float(s_copy.get('women', 0) or 0)))
            except Exception:
                w_cnt = 0
            try:
                m_cnt = max(0, int(float(s_copy.get('men', 0) or 0)))
            except Exception:
                m_cnt = 0
            if s_copy.get('total') is None:
                s_copy['total'] = w_cnt + m_cnt
            if sname:
                if not s_copy.get('slug'):
                    s_copy['slug'] = store_master.slug_of(sname)
                if not s_copy.get('seo_name'):
                    s_copy['seo_name'] = store_master.seo_name(sname)
                if not s_copy.get('area_label'):
                    info = master.get(sname) or {}
                    s_copy['area_label'] = info.get('area_label') or s_copy.get('area') or ''
            live_stores.append(s_copy)
        import affiliate_manager
        surface_smart_cta = affiliate_manager.build_surface_smart_cta(
            live_stores,
            source_path='/stats',
            surface_label='全国主要店',
            allow_blocking=False,
        )
        import embed_widgets
        try:
            heatmap_initial = embed_widgets.get_heatmap_payload(scope='all', days=90, metric='ratio', theme='dark')
        except Exception as err:
            logging.getLogger(__name__).warning('stats heatmap payload failed: %s', err)
            heatmap_initial = None
        try:
            heatmap_scopes = embed_widgets.list_heatmap_scopes()
        except Exception as err:
            logging.getLogger(__name__).warning('stats heatmap scopes failed: %s', err)
            heatmap_scopes = []
        return render_template(
            'stats_report.html',
            report=report_data,
            forecast_accuracy=fc_acc,
            forecast_days=fc_days,
            moat_coverage=moat_coverage,
            live_stores=live_stores,
            last_updated=last_updated,
            surface_smart_cta=surface_smart_cta,
            is_staff=bool(session.get('is_staff')),
            heatmap_initial=heatmap_initial,
            heatmap_scopes=heatmap_scopes,
        )
    import affiliate_manager as _am_stats
    import embed_widgets as _ew_stats
    # Memory-only probe (never touches DB): None until first compute, then total bucket count,
    # so the /stats HTML cache re-renders once heatmap data appears or changes.
    try:
        heatmap_sig = _ew_stats.peek_cached_total_buckets(90)
    except Exception:
        heatmap_sig = None
    stats_cache_sig = (
        'stats',
        rep_sig,
        fc_sig,
        heatmap_sig,
        _am_stats.are_slots_sharing_url('dating_bachelor', 'dating_instant', allow_blocking=False),
        _am_stats.is_slot_monetized('women_beauty_clinic', allow_blocking=False),
        _am_stats.is_slot_monetized('women_dating_bachelor', allow_blocking=False),
    )
    return core._get_cached_html_or_render(stats_cache_sig, _render)


@portal_bp.route('/stats/monthly.csv')
def stats_monthly_csv():
    """CC BY 4.0 open primary statistical CSV per domestic store for researchers, journalists & AI grounding."""
    import ops_governance
    raw_days = request.args.get('days', 30, type=int) or 30
    days = min((7, 30, 90), key=lambda d: abs(d - raw_days))
    hidden_sig = tuple(sorted(ops_governance.get_hidden_stores()))
    cache_key = (days, hidden_sig, core.local_db._active_db_key())
    now_m = time.monotonic()

    with _MONTHLY_CSV_LOCK:
        hit = _MONTHLY_CSV_CACHE.get(cache_key)
        if hit and (now_m - hit[0]) < _MONTHLY_CSV_TTL:
            csv_body = hit[1]
        else:
            csv_body = None

    if csv_body is None:
        try:
            rows = core.local_db.get_monthly_store_stats_rows(days=days)
        except Exception:
            rows = []

        hidden_slugs = set()
        try:
            import store_master
            for hs in hidden_sig:
                sl = store_master.slug_of(hs)
                if sl:
                    hidden_slugs.add(sl)
        except Exception:
            pass

        fieldnames = [
            'store_slug',
            'store_name',
            'brand_name',
            'area_label',
            'window_days',
            'sample_count',
            'avg_women',
            'peak_women',
            'avg_men',
            'peak_men',
            'avg_female_ratio_pct',
            'store_url',
        ]
        buf = io.StringIO()
        writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction='ignore', lineterminator='\n')
        writer.writeheader()
        for r in rows:
            if r.get('store_slug') and r.get('store_slug') in hidden_slugs:
                continue
            writer.writerow(r)
        csv_body = buf.getvalue()
        ttl_offset = 0.0 if rows else (_MONTHLY_CSV_TTL - 5.0)
        with _MONTHLY_CSV_LOCK:
            if len(_MONTHLY_CSV_CACHE) >= 12:
                _MONTHLY_CSV_CACHE.clear()
            _MONTHLY_CSV_CACHE[cache_key] = (now_m - ttl_offset, csv_body)

    has_data_rows = csv_body.count('\n') >= 2
    resp = Response(csv_body, mimetype='text/csv; charset=utf-8')
    resp.headers['Cache-Control'] = 'public, max-age=900' if has_data_rows else 'no-cache, max-age=0'
    resp.headers['Content-Disposition'] = 'inline; filename="aiseki_navi_monthly_store_stats.csv"'
    resp.headers['Access-Control-Allow-Origin'] = '*'
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Link'] = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'
    return resp


@portal_bp.route('/stats/year-end-2026')
def stats_year_end_2026():
    """2026 Year-End (Bounenkai, Bonus Window, Christmas & Dome Post-Show) primary statistical whitepaper."""
    core.trigger_background_fetch_if_needed()

    def _render():
        import ops_governance
        try:
            wp_data = core.local_db.get_year_end_whitepaper_data()
        except Exception:
            wp_data = {}
        if isinstance(wp_data.get('top_peak_stores'), list):
            wp_data = dict(wp_data)
            wp_data['top_peak_stores'] = [
                s for s in wp_data['top_peak_stores']
                if s.get('store_slug')
            ]
        try:
            fc_acc = core.local_db.get_forecast_accuracy_report(
                days=90,
                limit_recent=8,
                allow_blocking=True,
                auto_backfill=False,
            )
        except Exception:
            fc_acc = None
        with core.data_lock:
            last_updated = core.latest_data.get('last_updated') or '--:--'
        return render_template(
            'stats_year_end_2026.html',
            wp=wp_data,
            forecast_accuracy=fc_acc,
            last_updated=last_updated,
            is_staff=bool(session.get('is_staff')),
        )

    is_staff = bool(session.get('is_staff'))
    return core._get_cached_html_or_render(('stats_year_end_2026', is_staff), _render)


_CC_BY_LINK_HEADER = '<https://creativecommons.org/licenses/by/4.0/>; rel="license"'


def _heatmap_query_params():
    """Normalize embed heatmap query params (scope/store slugs are validated again inside embed_widgets)."""
    import embed_widgets
    scope = (request.args.get('scope') or 'all').strip()[:80] or 'all'
    store = (request.args.get('store') or '').strip()[:80] or None
    metric = embed_widgets.normalize_metric(request.args.get('metric'))
    theme = embed_widgets.normalize_theme(request.args.get('theme'))
    days = embed_widgets.clamp_days(request.args.get('days', embed_widgets.DEFAULT_DAYS, type=int))
    return scope, store, metric, theme, days


def _is_embed_rate_limited() -> bool:
    if core._is_staff_test_request():
        return False
    try:
        return not core.turbo_store.check_sliding_rate_limit('embed_heatmap_ip', core.get_client_ip(), limit=120, window_sec=60)
    except Exception:
        return False


def _apply_embed_cache_control(resp, has_data: bool) -> None:
    if session.get('is_staff') or session.get('is_master') or 'Set-Cookie' in resp.headers:
        resp.headers['Cache-Control'] = 'private, no-cache, max-age=0'
    else:
        resp.headers['Cache-Control'] = 'public, max-age=900' if has_data else 'no-cache, max-age=0'


@portal_bp.route('/embed/heatmap')
def embed_heatmap():
    """CC BY 4.0 曜日×時間帯別 実測男女比ヒートマップ（外部 iframe 埋め込み＆単体公開ページ）。"""
    if _is_embed_rate_limited():
        resp = make_response('Too Many Requests', 429)
        resp.headers['Content-Security-Policy'] = "base-uri 'self'; object-src 'none'; frame-ancestors *"
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['Retry-After'] = '60'
        return resp
    import embed_widgets
    scope, store, metric, theme, days = _heatmap_query_params()
    try:
        payload = embed_widgets.get_heatmap_payload(scope=scope, store=store, days=days, metric=metric, theme=theme)
    except LookupError:
        abort(404)
    resp = make_response(render_template('embed_heatmap.html', heatmap=payload, metric=metric, theme=theme))
    # Set before app.after_request `setdefault` so third-party sites may frame this page (CSP L2
    # frame-ancestors supersedes the default X-Frame-Options: SAMEORIGIN in all modern browsers).
    resp.headers['Content-Security-Policy'] = "base-uri 'self'; object-src 'none'; frame-ancestors *"
    _apply_embed_cache_control(resp, payload['meta']['total_buckets'] > 0)
    resp.headers['Link'] = _CC_BY_LINK_HEADER
    return resp


@portal_bp.route('/api/embed/heatmap.json')
def api_embed_heatmap_json():
    """CORS-open CC BY 4.0 JSON for the weekday x hour heatmap (no affiliate links, no internal store keys)."""
    if _is_embed_rate_limited():
        resp = jsonify({'error': 'rate_limited'})
        resp.status_code = 429
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Cache-Control'] = 'no-store'
        resp.headers['Retry-After'] = '60'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp
    import embed_widgets
    scope, store, metric, theme, days = _heatmap_query_params()
    try:
        payload = embed_widgets.get_heatmap_payload(scope=scope, store=store, days=days, metric=metric, theme=theme)
    except LookupError:
        resp = jsonify({'error': 'unknown_scope_or_store'})
        resp.status_code = 404
        resp.headers['Access-Control-Allow-Origin'] = '*'
        resp.headers['Cache-Control'] = 'no-cache, max-age=0'
        resp.headers['X-Content-Type-Options'] = 'nosniff'
        return resp
    resp = jsonify(payload)
    resp.headers['Access-Control-Allow-Origin'] = '*'
    _apply_embed_cache_control(resp, payload['meta']['total_buckets'] > 0)
    resp.headers['X-Content-Type-Options'] = 'nosniff'
    resp.headers['Link'] = _CC_BY_LINK_HEADER
    return resp


@portal_bp.route('/areas')
@portal_bp.route('/area/<area_slug>')
def area_portal(area_slug=None):
    """Area portal page showing live crowd stats and local lounge guides."""
    if area_slug is not None and area_slug not in AREA_GUIDES:
        abort(404)
    core.trigger_background_fetch_if_needed()

    def _render():
        import ops_governance
        with core.data_lock:
            raw_stores = list(core.latest_data.get('full_data') or [])
            last_updated = core.latest_data.get('last_updated') or '--:--'
        stores = ops_governance.filter_visible_stores(raw_stores)

        area_summaries = []
        for slug, info in AREA_GUIDES.items():
            matched = get_stores_for_area(slug, stores)
            total_w = sum(s.get('women', 0) for s in matched)
            total_m = sum(s.get('men', 0) for s in matched)
            area_summaries.append({
                'slug': slug,
                'info': info,
                'stores': matched,
                'store_count': len(matched),
                'total_women': total_w,
                'total_men': total_m,
            })

        selected_area = None
        if area_slug and area_slug in AREA_GUIDES:
            for item in area_summaries:
                if item['slug'] == area_slug:
                    selected_area = item
                    break

        return render_template(
            'areas.html',
            area_summaries=area_summaries,
            selected_area=selected_area,
            last_updated=last_updated,
        )
    return core._get_cached_html_or_render(('areas', area_slug), _render)


@portal_bp.route('/price-comparison')
def price_comparison_page():
    """Urban cluster cross-brand price, men's event & live cost-performance comparison board."""
    import price_comparison
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    raw_area_param = (request.args.get('area') or '').strip().lower()
    valid_ids = {c['id'] for c in price_comparison.COMPARISON_CLUSTERS}
    has_explicit_valid_area = bool(raw_area_param and raw_area_param in valid_ids)
    selected_area = raw_area_param if has_explicit_valid_area else 'shinjuku'

    def _render():
        import ops_governance
        with core.data_lock:
            raw_live = list(core.latest_data.get('full_data') or [])
            last_updated = core.latest_data.get('last_updated') or '--:--'
        live_stores = ops_governance.filter_visible_stores(raw_live)

        data = price_comparison.build_area_comparison_clusters(live_stores=live_stores)
        import affiliate_manager
        sel_cluster = next((c for c in (data.get('clusters') or []) if c.get('id') == selected_area), None)
        cluster_stores = (sel_cluster.get('stores') if isinstance(sel_cluster, dict) else None) or live_stores
        cluster_label = (sel_cluster.get('label') if isinstance(sel_cluster, dict) else None) or '対象エリア'
        surface_smart_cta = affiliate_manager.build_surface_smart_cta(
            cluster_stores,
            source_path='/price-comparison',
            surface_label=cluster_label,
            area_slug=selected_area,
            allow_blocking=False,
        )

        return render_template(
            'price_comparison.html',
            data=data,
            selected_area=selected_area,
            last_updated=last_updated,
            surface_smart_cta=surface_smart_cta,
            is_staff=bool(session.get('is_staff')),
        )
    import affiliate_manager as _am_pc
    pc_cache_sig = (
        f"price_comp_{raw_area_param if has_explicit_valid_area else '_root'}",
        _am_pc.are_slots_sharing_url('dating_bachelor', 'dating_instant', allow_blocking=False),
        _am_pc.is_slot_monetized('women_beauty_clinic', allow_blocking=False),
        _am_pc.is_slot_monetized('women_dating_bachelor', allow_blocking=False),
    )
    return core._get_cached_html_or_render(pc_cache_sig, _render)


@portal_bp.route('/for-women')
def for_women_guide():
    """Dedicated SEO & real-time zero-wait lounge guide for female pairs (Plan C & D)."""
    import affiliate_manager
    import ladies_guide
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()
    monetized_women_clinic = affiliate_manager.is_slot_monetized('women_beauty_clinic', allow_blocking=False)
    monetized_women_dating = affiliate_manager.is_slot_monetized('women_dating_bachelor', allow_blocking=False)

    def _render():
        import ops_governance
        with core.data_lock:
            raw_live = list(core.latest_data.get('full_data') or [])
            last_updated = core.latest_data.get('last_updated') or '--:--'
        live_stores = ops_governance.filter_visible_stores(raw_live)
        board = ladies_guide.build_ladies_live_board(live_stores=live_stores)
        top_pr_cards = affiliate_manager.build_for_women_top_picks(allow_blocking=False)
        return render_template(
            'for_women.html',
            board=board,
            top_pr_cards=top_pr_cards,
            monetized_women_clinic=monetized_women_clinic,
            monetized_women_dating=monetized_women_dating,
            last_updated=last_updated,
            is_staff=bool(session.get('is_staff')),
        )

    cache_sig = (
        'for_women',
        monetized_women_clinic,
        monetized_women_dating,
    )
    return core._get_cached_html_or_render(cache_sig, _render)


@portal_bp.route('/ladies')
def ladies_legacy_redirect():
    """301 Permanent redirect from short alias /ladies to canonical /for-women."""
    return core._redirect_preserving_query('/for-women', code=301)


def _normalize_concierge_params(concierge_matcher):
    area = concierge_matcher.normalize_concierge_area(request.args.get('area', 'shinjuku'))
    raw_aud = (request.args.get('audience') or 'men').strip().lower()
    audience = 'women' if raw_aud == 'women' else 'men'
    raw_tim = (request.args.get('timing') or 'now').strip().lower()
    timing = raw_tim if raw_tim in ('now', 'prime_21', 'late_23') else 'now'
    raw_ps = (request.args.get('party_size') or 'pair').strip().lower()
    party_size = raw_ps if raw_ps in ('solo', 'pair', 'group') else 'pair'
    raw_vb = (request.args.get('vibe') or 'any').strip().lower()
    vibe = raw_vb if raw_vb in ('any', 'lively', 'calm') else 'any'
    return area, audience, timing, party_size, vibe


@portal_bp.route('/concierge')
def concierge_page():
    """Interactive tonight's lounge & walking-distance hopping concierge (cached 60s)."""
    import concierge_matcher
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()

    area, audience, timing, party_size, vibe = _normalize_concierge_params(concierge_matcher)
    cache_extra = f"concierge_{area}_{audience}_{timing}_{party_size}_{vibe}"

    def _render():
        import ops_governance
        with core.data_lock:
            raw_stores = list(core.latest_data.get('full_data') or [])
            last_upd = core.latest_data.get('last_updated') or '--:--'
        stores = [dict(s) for s in ops_governance.filter_visible_stores(raw_stores)]
        res = concierge_matcher.diagnose_tonight_plan(
            stores,
            area=area,
            audience=audience,
            timing=timing,
            party_size=party_size,
            vibe=vibe,
        )
        return render_template(
            'concierge.html',
            initial_result=res,
            concierge_areas=res.get('available_areas', []),
            last_updated=last_upd,
            is_staff=bool(session.get('is_staff')),
        )

    return core._get_cached_html_or_render(cache_extra, _render, ttl_sec=60.0)


@portal_bp.route('/api/concierge')
def api_concierge():
    """Ultra-fast (<5ms) JSON endpoint for interactive lounge & hopping diagnosis (cached 30s)."""
    import concierge_matcher
    import ops_governance
    core.restore_latest_data_if_empty()
    core.trigger_background_fetch_if_needed()

    area, audience, timing, party_size, vibe = _normalize_concierge_params(concierge_matcher)

    with core.data_lock:
        last_upd = core.latest_data.get('last_updated') or ''

    gov_sig = (tuple(sorted(ops_governance.get_hidden_brands())), tuple(sorted(ops_governance.get_hidden_stores())))
    cache_key = ('api_concierge', area, audience, timing, party_size, vibe, last_upd, gov_sig)

    def _build_concierge_dict():
        import store_master
        with core.data_lock:
            raw_stores = list(core.latest_data.get('full_data') or [])
            cur_last_upd = core.latest_data.get('last_updated') or ''
        stores = [dict(s) for s in ops_governance.filter_visible_stores(raw_stores)]
        res = concierge_matcher.diagnose_tonight_plan(
            stores,
            area=area,
            audience=audience,
            timing=timing,
            party_size=party_size,
            vibe=vibe,
        )

        def _sanitize_cand(c):
            if isinstance(c, dict):
                c_copy = dict(c)
                if 'raw_name' in c_copy:
                    c_copy['raw_name'] = store_master.seo_name(c_copy['raw_name'])
                return c_copy
            return c

        if 'primary' in res and res['primary']:
            res['primary'] = _sanitize_cand(res['primary'])
        if 'backup' in res and res['backup']:
            res['backup'] = _sanitize_cand(res['backup'])
        if 'other_candidates' in res and isinstance(res['other_candidates'], list):
            res['other_candidates'] = [_sanitize_cand(c) for c in res['other_candidates']]

        res['last_updated'] = cur_last_upd
        return res, 200

    return core._cached_json_response(cache_key, _build_concierge_dict, ttl_sec=30.0)


@portal_bp.route('/grooming')
def grooming_guide():
    """Dedicated editorial guide for men's grooming, fashion, and backup dating apps."""
    raw_mode = (request.args.get('mode') or '').strip().lower()
    mode = raw_mode if raw_mode in ('prep', 'waiting', 'second') else 'prep'
    return core._get_cached_html_or_render(
        ('grooming', mode),
        lambda: render_template('grooming.html', active_mode=mode),
    )


@portal_bp.route('/privacy')
def privacy_page():
    """Dedicated legal, privacy policy, and operator information page."""
    return core._get_cached_html_or_render('privacy', lambda: render_template('privacy.html'))


@portal_bp.route('/about')
def about_page():
    """Operator information, data collection method, update frequency and editorial policy."""
    def _render_about():
        import store_master
        master_map = store_master.load() or {}
        total_st = len(master_map)
        dom_st = sum(1 for m in master_map.values() if (m or {}).get('region') != 'Korea')
        kr_st = max(0, total_st - dom_st)
        total_rows = 0
        try:
            db_stats = core.local_db.get_db_stats() or {}
            total_rows = int(db_stats.get('count') or db_stats.get('total_rows') or 0)
        except Exception:
            total_rows = 0
        return render_template(
            'about.html',
            total_stores=total_st,
            domestic_stores=dom_st,
            overseas_stores=kr_st,
            historical_rows=total_rows,
        )
    return core._get_cached_html_or_render('about', _render_about)


def _render_contact_page(form_values=None, errors=None, sent=False, status=200):
    import contact_form
    html = render_template(
        'contact.html',
        csrf_token=contact_form.issue_token(core.app.secret_key),
        categories=contact_form.CATEGORIES,
        form_values=form_values or {},
        errors=errors or [],
        sent=sent,
        max_message_len=contact_form.MAX_MESSAGE_LEN,
        max_name_len=contact_form.MAX_NAME_LEN,
        max_email_len=contact_form.MAX_EMAIL_LEN,
    )
    resp = make_response(html, status)
    resp.headers['Cache-Control'] = 'private, no-store'
    return resp


@portal_bp.route('/contact', methods=['GET', 'POST'])
def contact_page():
    """Contact form (stateless HMAC CSRF token, honeypot, rate limit, length limits; stored via Sheets + webhook)."""
    import contact_form
    if request.method == 'GET':
        return _render_contact_page(sent=request.args.get('sent') == '1')

    if (request.content_length or 0) > contact_form.MAX_REQUEST_BYTES:
        return _render_contact_page(errors=['送信内容が大きすぎます。'], status=413)
    if not contact_form.is_same_origin(request.headers.get('Origin'), request.headers.get('Referer'), request.host):
        return _render_contact_page(errors=['不正な送信元です。ページを再読み込みしてから送信してください。'], status=400)

    form = request.form
    token_err = contact_form.verify_token(core.app.secret_key, form.get('csrf_token'))
    data, errors = contact_form.validate(form)
    if token_err == 'too_fast':
        token_err = 'invalid'
    if token_err:
        return _render_contact_page(
            form_values=data,
            errors=['フォームの有効期限が切れました。お手数ですが内容を確認して再度送信してください。'],
            status=400,
        )
    # Honeypot: bots fill the hidden field. Pretend success without storing anything.
    if (form.get('website') or '').strip():
        return redirect('/contact?sent=1', code=303)
    if errors:
        return _render_contact_page(form_values=data, errors=errors, status=400)

    client_ip = core.get_client_ip()
    if not contact_form.check_rate_limit(client_ip, category=data.get('category', '')):
        return _render_contact_page(
            form_values=data,
            errors=['短時間に送信が集中しています。10分ほど時間をおいてから再度お試しください。'],
            status=429,
        )
    contact_form.store_inquiry(
        data,
        contact_form.hash_ip(core.app.secret_key, client_ip),
        user_agent=request.headers.get('User-Agent', ''),
    )
    return redirect('/contact?sent=1', code=303)


@portal_bp.route('/guide')
def guide_article():
    """Official comprehensive lounge strategy editorial manual."""
    return core._get_cached_html_or_render('guide_article', lambda: render_template('guide_article.html'))


@portal_bp.route('/guide/strategy')
def guide_strategy_legacy_redirect():
    """301 Permanent redirect from legacy /guide/strategy to canonical /guide."""
    return core._redirect_preserving_query('/guide', code=301)


@portal_bp.route('/guide/weekday-strategy')
def guide_weekday_strategy():
    """Editorial guide for weekday lounge strategy."""
    return core._get_cached_html_or_render('guide_weekday', lambda: render_template('guide_weekday.html'))


@portal_bp.route('/guide/brand-comparison')
def guide_brand_comparison():
    """Editorial guide comparing Oriental Lounge, ag, and JIS."""
    return core._get_cached_html_or_render('guide_comparison', lambda: render_template('guide_comparison.html'))


@portal_bp.route('/guide/compare')
def guide_compare_legacy_redirect():
    """301 Permanent redirect from legacy /guide/compare to canonical /guide/brand-comparison."""
    return core._redirect_preserving_query('/guide/brand-comparison', code=301)


@portal_bp.route('/guide/shinjuku')
def guide_shinjuku():
    """Editorial guide comparing the 3 Shinjuku lounge stores."""
    return core._get_cached_html_or_render('guide_shinjuku', lambda: render_template('guide_shinjuku.html'))


@portal_bp.route('/guide/solo-vs-pair')
def guide_solo_vs_pair():
    """Editorial guide comparing solo vs 2-person lounge visits."""
    return core._get_cached_html_or_render('guide_solo_pair', lambda: render_template('guide_solo_pair.html'))


@portal_bp.route('/guide/second-venue')
def guide_second_venue():
    """Editorial guide on inviting to a second venue smoothly."""
    return core._get_cached_html_or_render('guide_second_venue', lambda: render_template('guide_second_venue.html'))


@portal_bp.route('/guide/first-10-minutes')
def guide_first_10min():
    """Editorial guide on first 10 minutes conversation and grooming."""
    return core._get_cached_html_or_render('guide_first_10min', lambda: render_template('guide_first_10min.html'))


@portal_bp.route('/guide/club-area-midnight')
def guide_club_area_midnight():
    """Editorial data study on dance club proximity and midnight 2nd-wave female inflows."""
    return core._get_cached_html_or_render('guide_club_midnight', lambda: render_template('guide_club_midnight.html'))


@portal_bp.route('/guide/weekend-strategy')
def guide_weekend_strategy():
    """Editorial data study guide for Friday/Saturday peak hours."""
    return core._get_cached_html_or_render('guide_weekend', lambda: render_template('guide_weekend.html'))


@portal_bp.route('/guide/beauty-area-stage')
def guide_beauty_area_stage():
    """Editorial data study on beauty salon districts, early-evening surges, and men's grooming."""
    return core._get_cached_html_or_render('guide_beauty_stage', lambda: render_template('guide_beauty_stage.html'))


@portal_bp.route('/guide/male-lead-mechanics')
def guide_male_lead_mechanics():
    """Editorial data study on why male-leading stores attract more women and why women no longer wait."""
    return core._get_cached_html_or_render('guide_male_lead', lambda: render_template('guide_male_lead.html'))


@portal_bp.route('/guide/price-event-comparison')
def guide_price_event_comparison():
    """Editorial guide explaining the 51-store price, 4-time-slot, solo rule, event & minimum 10-women comparison board."""
    return core._get_cached_html_or_render('guide_price_comparison', lambda: render_template('guide_price_comparison.html'))


@portal_bp.route('/guide/last-train-cliff')
def guide_last_train_cliff():
    """Editorial story & 9.6-million-point data study on the 23:20 last-train cliff, 00:30 second wave, and post-23:00 brand reversal."""
    return core._get_cached_html_or_render('guide_last_train_cliff', lambda: render_template('guide_last_train_cliff.html'))


@portal_bp.route('/guide/crowd-rush-paradox')
def guide_crowd_rush_paradox():
    """Editorial data study on the Friday/Saturday 22:00 crowd-rush paradox (29.8% flip risk) and the 70% buffer law."""
    return core._get_cached_html_or_render('guide_crowd_rush_paradox', lambda: render_template('guide_crowd_rush_paradox.html'))


@portal_bp.route('/guide/brand-waveform-asymmetry')
def guide_brand_waveform_asymmetry():
    """Editorial data study on 5-brand hourly crowd waveforms (20:00-01:00 crossover) and male/female asymmetric venue fit."""
    return core._get_cached_html_or_render('guide_brand_waveform_asymmetry', lambda: render_template('guide_brand_waveform_asymmetry.html'))


@portal_bp.route('/guide/lounge-vs-screened-date')
def guide_lounge_vs_screened_date():
    """Editorial data comparison between multi-person lounges and 1-on-1 screened dating apps across time slots and budgets."""
    return core._get_cached_html_or_render('guide_lounge_vs_screened_date', lambda: render_template('guide_lounge_vs_screened_date.html'))


@portal_bp.route('/guide/dress-code-cleanliness')
def guide_dress_code_cleanliness():
    """Editorial practical guide on 5-brand dress codes and 70cm sofa spotlight cleanliness checks."""
    return core._get_cached_html_or_render('guide_dress_code_cleanliness', lambda: render_template('guide_dress_code_cleanliness.html'))


@portal_bp.route('/guide/shibuya-ebisu-comparison')
def guide_shibuya_ebisu_comparison():
    """Editorial area pillar comparing 5 Shibuya & Ebisu lounges/bars across waveforms, age demographics, and solo rules."""
    return core._get_cached_html_or_render('guide_shibuya_ebisu_comparison', lambda: render_template('guide_shibuya_ebisu_comparison.html'))


@portal_bp.route('/guide/ueno-kinshicho-machida')
def guide_ueno_kinshicho_machida():
    """Editorial area pillar comparing 5 East Tokyo & suburban terminal lounges (Ueno x2, Kinshicho, Machida, Kashiwa)."""
    return core._get_cached_html_or_render('guide_ueno_kinshicho_machida', lambda: render_template('guide_ueno_kinshicho_machida.html'))


@portal_bp.route('/guide/yokohama-omiya-kanto')
def guide_yokohama_omiya_kanto():
    """Editorial area pillar comparing 5 Greater Kanto mega-terminal lounges (Yokohama, Omiya x2, Utsunomiya, Takasaki)."""
    return core._get_cached_html_or_render('guide_yokohama_omiya_kanto', lambda: render_template('guide_yokohama_omiya_kanto.html'))


@portal_bp.route('/guide/nagoya-sakae-nishiki')
def guide_nagoya_sakae_nishiki():
    """Editorial area pillar comparing 5 Nagoya Sakae, Nishiki & Sumiyoshi lounges/bars along the 700m north-south axis."""
    return core._get_cached_html_or_render('guide_nagoya_sakae_nishiki', lambda: render_template('guide_nagoya_sakae_nishiki.html'))


@portal_bp.route('/guide/fukuoka-tenjin-kyushu')
def guide_fukuoka_tenjin_kyushu():
    """Editorial area pillar comparing all 11 Fukuoka Tenjin, Kyushu & Okinawa lounges across 3 brands and solo-entry rules."""
    return core._get_cached_html_or_render('guide_fukuoka_tenjin_kyushu', lambda: render_template('guide_fukuoka_tenjin_kyushu.html'))


@portal_bp.route('/guide/hiroshima-okayama-matsuyama')
def guide_hiroshima_okayama_matsuyama():
    """Editorial area pillar comparing 6 Chugoku & Shikoku lounges (Hiroshima 3-store 185m cluster, Okayama x2, Matsuyama)."""
    return core._get_cached_html_or_render('guide_hiroshima_okayama_matsuyama', lambda: render_template('guide_hiroshima_okayama_matsuyama.html'))


@portal_bp.route('/guide/sapporo-sendai-north')
def guide_sapporo_sendai_north():
    """Editorial area pillar comparing JIS Sapporo (Susukino) and ag Sendai (Kokubuncho) single-hub waveforms and solo rules."""
    return core._get_cached_html_or_render('guide_sapporo_sendai_north', lambda: render_template('guide_sapporo_sendai_north.html'))


def _render_scheduled_guide(slug: str):
    """Render a scheduled editorial guide if published, or allow noindex preview for authenticated staff."""
    item = core.scheduled_articles.get_article_by_slug(slug)
    if not item:
        abort(404)
    if core.scheduled_articles.is_published(item):
        return core._get_cached_html_or_render(
            ('sched_guide', slug, item.get('created_at') or item.get('publish_at')),
            lambda: render_template(item['template'], is_scheduled_preview=False, scheduled_item=item),
        )
    if core.is_admin_authorized():
        resp = make_response(render_template(item['template'], is_scheduled_preview=True, scheduled_item=item))
        resp.headers['X-Robots-Tag'] = 'noindex, nofollow'
        return resp
    abort(404)


@portal_bp.route('/guide/local-vs-urban')
def guide_local_vs_urban():
    """Scheduled editorial data study comparing local 1-store cities vs urban flagship clusters."""
    return _render_scheduled_guide('local-vs-urban')


@portal_bp.route('/guide/cost-break-even')
def guide_cost_break_even():
    """Scheduled editorial data study on stay-duration cost break-even and store-hopping."""
    return _render_scheduled_guide('cost-break-even')


@portal_bp.route('/guide/rain-payday')
def guide_rain_payday():
    """Scheduled editorial data study on rainy nights, pre-payday windows, and 3-day-weekend Sundays."""
    return _render_scheduled_guide('rain-payday')


@portal_bp.route('/guide/osaka-kita-vs-minami')
def guide_osaka_kita_vs_minami():
    """Scheduled editorial data study comparing all 9 Osaka Kita vs Minami lounges across 276k logs."""
    return _render_scheduled_guide('osaka-kita-vs-minami')


@portal_bp.route('/guide/table-change-protocol')
def guide_table_change_protocol():
    """Scheduled editorial data study on table change / change protocols and optimal store conditions."""
    return _render_scheduled_guide('table-change-protocol')


@portal_bp.route('/guide/thirties-lounge-guide')
def guide_thirties_lounge_guide():
    """Scheduled editorial data study on thirties & around-30 demographic strategy and venue selection."""
    return _render_scheduled_guide('thirties-lounge-guide')


@portal_bp.route('/guide/sunday-lounge-strategy')
def guide_sunday_lounge_strategy():
    """Scheduled editorial data study on Sunday crowd drop-off at 21:30 and Monday-off salon/apparel demographics."""
    return _render_scheduled_guide('sunday-lounge-strategy')


@portal_bp.route('/guide/line-exchange-reality')
def guide_line_exchange_reality():
    """Scheduled editorial data study on post-lounge LINE ghosting mechanics and contact protocols."""
    return _render_scheduled_guide('line-exchange-reality')


@portal_bp.route('/guide/<slug>')
def guide_dynamic_scheduled(slug):
    """Dynamic route serving scheduled or live columns published from the Column Editorial Room."""
    item = core.scheduled_articles.get_article_by_slug(slug)
    if not item:
        abort(404)
    return _render_scheduled_guide(slug)
