"""
analytics.py - 自前の軽量アクセス集計（個人情報なし）

「どの記事が読まれ、どのボタン・アフィリエイトがクリックされているか」を
日別・イベント別・ラベル別の件数だけで集計します（IP・端末IDなどは保存しません）。

・メモリ上で加算 → 10分ごとに Google Sheets 'Analytics Daily' へまとめて追記
・集計画面（/staff/analytics）は Sheets の値＋未送信分を合算して表示
"""
import datetime
import time
import threading
import logging
from collections import defaultdict

import sheets_store
import turbo_store

SHEET_TITLE = 'Analytics Daily'
HEADER = ['date_jst', 'event', 'label', 'count']

ALLOWED_EVENTS = {
    'page_view',        # 記事・ポータルページの閲覧（サーバー側で記録）
    'affiliate_click',  # /link/<id> 経由のアフィリエイトクリック（サーバー側で記録）
    'store_open',       # 店舗詳細モーダルを開いた
    'map_cafe',         # 2軒目: 夜カフェのマップ検索
    'map_bar',          # 2軒目: バー・バルのマップ検索
    'official_click',   # 公式サイトへのリンク
    'sns_click',        # X/Threads/note への導線
    'share',            # LINE/X 共有・コピー
    'vote',             # 現場投票
    'sns_inflow',       # ?s= 識別子付きのSNS投稿からのサイト流入・CV追跡
}

_lock = threading.Lock()
_pending = defaultdict(int)   # (date, event, label) -> count（未フラッシュ分）
_sheets_retry = defaultdict(int)  # Postgres保存済み・Sheetsのみ再送待ち分
_flushed = defaultdict(int)   # 起動後に送信済みの分（Sheets読込失敗時の表示用）
_SHEET_ROWS_CACHE_TTL = 900.0
_sheet_rows_cache = {'ts': 0.0, 'rows': None}
_sheet_refresh_in_flight = False
_pg_seeded_from_sheets = False


def _refresh_sheet_rows_async():
    """Refresh Analytics Daily rows from Google Sheets in a background daemon thread (Stale-While-Revalidate)."""
    global _sheet_refresh_in_flight
    if sheets_store.is_disabled():
        return
    with _lock:
        if _sheet_refresh_in_flight:
            return
        _sheet_refresh_in_flight = True

    def _worker():
        global _sheet_refresh_in_flight
        try:
            rows = sheets_store.read_rows_or_none(SHEET_TITLE, HEADER)
            if rows is not None:
                with _lock:
                    _sheet_rows_cache['ts'] = time.monotonic()
                    _sheet_rows_cache['rows'] = rows
        except Exception as err:
            logging.warning(f"[analytics] async sheet refresh failed: {err}")
        finally:
            with _lock:
                _sheet_refresh_in_flight = False

    threading.Thread(target=_worker, daemon=True).start()


def _today():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime('%Y-%m-%d')


def track(event, label=''):
    if event not in ALLOWED_EVENTS:
        return False
    label = str(label or '')[:80]
    with _lock:
        _pending[(_today(), event, label)] += 1
    return True


def flush():
    """未送信分を PostgreSQL / Sheets に追記。失敗したら戻して次回再送。"""
    cutoff_3d = (
        datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        - datetime.timedelta(days=3)
    ).strftime('%Y-%m-%d')
    with _lock:
        for k in [k for k in _flushed.keys() if k[0] <= cutoff_3d]:
            del _flushed[k]
        new_items = list(_pending.items())
        _pending.clear()
        retry_items = list(_sheets_retry.items())
        _sheets_retry.clear()
    if not new_items and not retry_items:
        return 0

    pg_saved = False
    if new_items:
        pg_saved = turbo_store.analytics_add_batch(new_items)

    merged_for_sheets = defaultdict(int)
    for k, c in retry_items:
        merged_for_sheets[k] += c
    for k, c in new_items:
        merged_for_sheets[k] += c

    rows = [[d, e, l, c] for (d, e, l), c in merged_for_sheets.items()]
    ok = sheets_store.append_rows(SHEET_TITLE, HEADER, rows)
    with _lock:
        if ok:
            for key, c in merged_for_sheets.items():
                if key[0] > cutoff_3d:
                    _flushed[key] += c
            _sheet_rows_cache['ts'] = 0.0
        elif pg_saved:
            # Already persisted safely in PostgreSQL/turbo_store; only retry Google Sheets mirror next tick
            for key, c in merged_for_sheets.items():
                _sheets_retry[key] += c
        else:
            for key, c in merged_for_sheets.items():
                _pending[key] += c
    if not ok and not pg_saved:
        logging.warning('[analytics] flush failed; will retry')
    return len(rows) if (ok or pg_saved) else 0


_PAGE_TITLES = {
    '/': '🔥 トップページ（全国リアルタイム人数ランキング）',
    '/concierge': '🧭 今夜どこ行く？3秒穴場＆徒歩ハシゴ診断',
    '/columns': '📚 コラム・攻略記事一覧ページ',
    '/areas': '📍 全国エリア一覧ディレクトリ',
    '/grooming': '✨ 相席ラウンジ前の身だしなみ・清潔感特集',
    '/stats': '📊 全国ラウンジ統計・混雑データレポート',
    '/price-comparison': '💴 店舗別料金・開催中イベント比較',
    '/oriental-lounge': '🥂 オリエンタルラウンジ全店リアルタイム一覧',
    '/ag': '⚡ ag（アグ）全店リアルタイム一覧',
    '/jis': '💎 JIS（ジス）全店リアルタイム一覧',
    '/guide': '📖 相席ラウンジ完全攻略ガイド（基本編）',
    '/guide/strategy': '📖 相席ラウンジ完全攻略ガイド（基本編）',
    '/guide/brand-comparison': '📖 3大ブランド徹底比較（オリエンタルラウンジ・ag・JIS）',
    '/guide/weekday-strategy': '📖 水曜・木曜の平日穴場攻略コラム',
    '/guide/weekend-strategy': '📖 金曜・土曜ピークの待ち時間回避＆ハシゴ攻略',
    '/guide/shinjuku': '📖 新宿エリア3店舗の使い分け・穴場ガイド',
    '/guide/solo-vs-pair': '📖 1人参戦（ソロ）vs 2人組のメリット・勝率比較',
    '/guide/second-venue': '📖 2軒目の誘い方・断られない声かけタイミング',
    '/guide/first-10-minutes': '📖 最初の10分で差がつく会話術・第一印象の作り方',
    '/guide/club-area-midnight': '📖 ダンスクラブ密集エリアの深夜0時「第2波」実測データ検証',
    '/guide/beauty-area-stage': '📖 美容サロン激戦区のラウンジが「自分磨きの舞台」になる理由',
    '/guide/male-lead-mechanics': '📖 男性先行・男性比率が高いお店に女性が集まるデータ法則',
    '/guide/price-event-comparison': '📖 全51店舗「料金・イベント・現在人数」比較ボード活用ガイド',
    '/guide/last-train-cliff': '📖 23時20分の終電崖（魔の40分間）と23時で逆転する街別ハシゴ法則',
    '/guide/local-vs-urban': '📖 地方店vs都心大型店｜深夜0時に女性が121%へ逆転増加する一極集中の法則',
    '/guide/cost-break-even': '📖 90分粘るvs40分で見切る｜男性の滞在時間と料金の損益分岐点検証',
    '/guide/rain-payday': '📖 雨の日・給料日前・連休中日の男女比変動と隠れ穴場日データ',
    '/for-women': '💄 女子のための相席ラウンジ完全ガイド＆待ちゼロ店舗ボード',
    '/privacy': '⚖️ 運営者情報・プライバシーポリシー',
}

_AFFILIATE_NAMES = {
    'spu': '👔 SPU スタイルアップ便（メンズ服コーデ）',
    'spu_styleup': '👔 SPU スタイルアップ便（メンズ服コーデ）',
    'orbis': '🧴 ORBIS Mr.（オルビス ミスター スキンケア）',
    'mens_skincare_clinic': '🧴 ORBIS Mr.（オルビス ミスター スキンケア）',
    'hair_removal': '🪒 メンズ医療脱毛・ヒゲ脱毛クリニック',
    'mens_hair_removal': '🪒 メンズ医療脱毛・ヒゲ脱毛クリニック',
    'hair_removal_trial': '🪒 メンズヒゲ脱毛 初回体験トライアル枠',
    'dating': '💝 バチェラーデート（審査制マッチングアプリ）',
    'dating_instant': '💝 近くで今日会える恋活・マッチングアプリ',
    'dating_bachelor': '🥂 審査制・ハイクラスデートアプリ（バチェラーデート等）',
    'dating_casual': '🍻 今日すぐ近くで飲める恋活アプリ（即時合流枠）',
    'women_dating_bachelor': '💄🥂 女性向け｜審査制・完全無料ハイクラスデート枠',
    'women_dating': '💄🥂 女性向け｜審査制・完全無料ハイクラスデート枠',
    'women_beauty_clinic': '💄💎 女性向け｜美容医療・美肌クリニック＆医療脱毛枠',
    'women_clinic': '💄💎 女性向け｜美容医療・美肌クリニック＆医療脱毛枠',
    'women_hair_removal': '💄💎 女性向け｜美容医療・美肌クリニック＆医療脱毛枠',
    'women_cosme_salon': '💄🧴 女性向け｜デパコス・ご褒美スキンケア＆サロン枠',
    'women_cosme': '💄🧴 女性向け｜デパコス・ご褒美スキンケア＆サロン枠',
    'women_skincare': '💄🧴 女性向け｜デパコス・ご褒美スキンケア＆サロン枠',
}

_AREA_NAMES = {
    'shinjuku': '新宿・西新宿エリア',
    'shibuya': '渋谷・恵比寿エリア',
    'shibuya-ebisu': '渋谷・恵比寿エリア',
    'ginza': '銀座・有楽町エリア',
    'ikebukuro': '池袋エリア',
    'ueno': '上野・町田エリア',
    'tokyo-east': '上野・銀座・錦糸町・町田',
    'kanto': '横浜・大宮・千葉・北関東エリア',
    'yokohama-kanto': '横浜・大宮・柏・北関東',
    'osaka': '大阪（梅田・茶屋町・心斎橋・難波）エリア',
    'umeda': '大阪・梅田・茶屋町・天満',
    'namba': '大阪・難波・心斎橋エリア',
    'namba-shinsaibashi': '難波・心斎橋エリア',
    'yokohama': '横浜エリア',
    'omiya': '大宮エリア',
    'sapporo': '札幌・すすきのエリア',
    'sendai': '仙台・国分町エリア',
    'hiroshima': '広島・流川・紙屋町エリア',
    'kyoto-kobe': '京都・神戸エリア',
    'nagoya': '名古屋（栄・錦）エリア',
    'fukuoka': '福岡（天神・中洲・小倉）エリア',
    'fukuoka-kyushu': '福岡・九州・沖縄エリア',
    'hokkaido-tohoku': '札幌・仙台エリア',
    'chugoku-shikoku': '広島・岡山・松山・高松エリア',
    'kyushu-okinawa': '熊本・宮崎・鹿児島・沖縄エリア',
}

_SNS_NAMES = {
    'x': '𝕏 公式X（旧Twitter）',
    'threads': '🧵 公式Threads（スレッド）',
    'note': '📝 公式noteコラム',
    'instagram': '📸 公式Instagram',
}

_INFLOW_STYLE_JA = {
    'ai_live_adlib': 'AI長考アドリブ実況型',
    'hub_gap': '街別ギャップ・ハシゴ型',
    'surge_focus': '1店舗急増・穴場フォーカス型',
    'usual_anomaly': '普段比（平比）ギャップ型',
    'east_west_talk': '東西トップ対比トーク型',
    'compact_top3': '定型TOP3ランキング型',
    'threads_city_story': '街の温度差エッセイ型',
    'threads_anomaly_insight': '普段比異変＆立ち回り型',
    'threads_hopping_guide': '1軒目→2軒目比較型',
    'threads_classic_top3': '定型TOP3掲示板型',
    'threads_ladies_zero_wait': '女子得・待ちゼロ＆特典速報型',
    'ig_ladies_night_guide': '女子組・待ちゼロ＆特典保存ガイド型',
    'ladies_night': '女子得・待ちゼロ速報枠',
    'live': 'リアルタイム速報枠',
    'tip': '解説コラム紹介枠',
    'rel': '新着コラム解禁告知',
    'wk': '日曜・週間統計白書',
}


def normalize_inflow_source(raw_s: str) -> str:
    """
    Normalize `?s=` query parameter into a clean, date-independent cohort tag so PDCA can aggregate
    clicks across days by channel + slot + editorial style:
      - 'x22_hub_gap_20261028' -> 'x22_hub_gap'
      - '20261028_2200'        -> 'x22_live'
      - 'th22_threads_city_story' -> 'th22_threads_city_story'
      - 'x18_tip'              -> 'x18_tip'
    """
    import re
    s = re.sub(r'[^a-zA-Z0-9_\-]', '', (raw_s or '').strip())[:48]
    if not s:
        return ''
    # Legacy OGP cache-buster format: YYYYMMDD_HHMM
    m_leg = re.match(r'^\d{8}_(\d{2})\d{2}$', s)
    if m_leg:
        return f"x{m_leg.group(1)}_live"
    # Strip trailing _YYYYMMDD or _MMDD cache-buster suffix
    s = re.sub(r'_(\d{8}|\d{4})$', '', s)
    return s[:40]


def describe_inflow_tag(tag: str) -> str:
    """Human-readable label for a normalized inflow tag like 'x22_hub_gap' or 'th22_threads_city_story'."""
    import re
    t = (tag or '').strip()
    if t.startswith('cv:'):
        inner = describe_inflow_tag(t[3:])
        return f"🎯 アフィリエイトCV（流入元: {inner}）"
    m_x = re.match(r'^x(\d{2})_(.+)$', t)
    if m_x:
        hr, st = m_x.group(1), m_x.group(2)
        return f"𝕏 X {hr}:00枠（{_INFLOW_STYLE_JA.get(st, st)}）"
    m_th = re.match(r'^th(\d{2})_(.+)$', t)
    if m_th:
        hr, st = m_th.group(1), m_th.group(2)
        return f"🧵 Threads {hr}:00枠（{_INFLOW_STYLE_JA.get(st, st)}）"
    if t == 'x_rel':
        return "𝕏 X 新着コラム解禁スレッド"
    if t == 'x_wk':
        return "𝕏 X 日曜・週間データ白書スレッド"
    if t.startswith('ig'):
        return f"📸 Instagram（{t}）"
    return f"📥 外部SNS流入（{t}）"


def describe_label(event, raw_label):
    """プログラム用のパスやIDを直感的な日本語名称に変換する。"""
    label = (raw_label or '').strip()
    if event == 'page_view':
        if label in _PAGE_TITLES:
            return {'title': _PAGE_TITLES[label], 'sub': label, 'href': label}
        if label.startswith('/guide/'):
            gslug = label.split('/guide/', 1)[1].strip('/')
            try:
                import scheduled_articles
                sitem = scheduled_articles.get_article_by_slug(gslug, allow_blocking=False)
                if sitem:
                    return {'title': f"📖 {sitem.get('short_title') or sitem.get('title')}", 'sub': label, 'href': label}
            except Exception:
                pass
        if label.startswith('/area/'):
            slug = label.split('/area/', 1)[1].strip('/')
            aname = _AREA_NAMES.get(slug, slug)
            return {'title': f'📍 エリア別：{aname}', 'sub': label, 'href': label}
        if label.startswith('/store/'):
            slug = label.split('/store/', 1)[1].strip('/')
            try:
                import store_master
                sname = store_master.store_by_slug(slug)
                if sname:
                    return {'title': f"🏪 店舗ページ：{store_master.seo_name(sname)}", 'sub': label, 'href': label}
            except Exception:
                pass
            return {'title': f'🏪 店舗ページ：{slug}', 'sub': label, 'href': label}
        return {'title': label or '不明なページ', 'sub': '', 'href': label if label.startswith('/') else ''}

    if event == 'affiliate_click':
        if '@' in label:
            item_id, ref_path = [p.strip() for p in label.split('@', 1)]
        else:
            item_id, ref_path = label, '-'
        item_title = _AFFILIATE_NAMES.get(item_id.lower(), f'💰 広告案件 ({item_id})')
        try:
            import affiliate_manager
            cid = affiliate_manager.canonical_slot_id(item_id)
            if cid:
                ov = affiliate_manager._load_overrides(allow_blocking=False).get(cid)
                if isinstance(ov, dict) and (ov.get('memo') or '').strip():
                    item_title = f"{item_title} ［{ov.get('memo').strip()}］"
        except Exception:
            pass
        ref_title = _PAGE_TITLES.get(ref_path, ref_path)
        return {'title': item_title, 'sub': f'クリック元: {ref_title}', 'href': ''}

    if event in ('store_open', 'map_cafe', 'map_bar', 'official_click', 'vote'):
        sname = label.split(':', 1)[0].strip() if ':' in label else label
        vtype = label.split(':', 1)[1].strip() if (event == 'vote' and ':' in label) else ''
        vtype_ja = {'smooth': 'すぐ入れた', 'waiting': '少し待ち', 'lively': '大盛況'}.get(vtype, vtype)
        try:
            import store_master
            info = store_master.get(sname)
            if info:
                seo = store_master.seo_name(sname)
                area = info.get('area_label', '')
                slug = store_master.slug_of(sname)
                title_suffix = f'（{vtype_ja}）' if vtype_ja else ''
                return {
                    'title': f'🏪 {seo}{title_suffix}',
                    'sub': f'{area}（{sname}）' if area else sname,
                    'href': f'/store/{slug}' if slug else ''
                }
        except Exception:
            pass
        return {'title': label or '-', 'sub': '', 'href': ''}

    if event == 'sns_inflow':
        if '@' in label:
            src_tag, dest_path = [p.strip() for p in label.split('@', 1)]
        else:
            src_tag, dest_path = label.strip(), ''
        return {
            'title': describe_inflow_tag(src_tag),
            'sub': f'着地ページ: {dest_path}' if dest_path else 'SNS流入コンバージョン',
            'href': dest_path if dest_path.startswith('/') else '',
        }

    if event == 'sns_click':
        return {'title': _SNS_NAMES.get(label.lower(), f'📣 {label}'), 'sub': 'SNS外部リンク', 'href': ''}

    if event == 'share':
        return {'title': f'🔗 シェア・コピー（{label or "汎用"}）', 'sub': '', 'href': ''}

    return {'title': label or '-', 'sub': '', 'href': ''}


def summary(days=7, allow_blocking=True):
    """直近 days 日の集計: {'by_event': {...}, 'by_label': {...}, 'by_label_pretty': {...}, 'funnel': {...}, 'revenue': {...}, 'daily_series': [...]}"""
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    start = (now_jst - datetime.timedelta(days=days - 1)).strftime('%Y-%m-%d')
    today_str = now_jst.strftime('%Y-%m-%d')
    yesterday_str = (now_jst - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    totals = defaultdict(int)
    is_mocked_sheets = getattr(sheets_store.read_rows, '__module__', '') != 'sheets_store'
    fast_rows = None if is_mocked_sheets else turbo_store.analytics_query_since(start)

    if fast_rows is not None:
        if not fast_rows and turbo_store.is_postgres_active() and not sheets_store.is_disabled():
            global _pg_seeded_from_sheets
            if not _pg_seeded_from_sheets:
                _pg_seeded_from_sheets = True
                def _seed_pg_analytics():
                    try:
                        hist_rows = sheets_store.read_rows_or_none(SHEET_TITLE, HEADER)
                        if hist_rows:
                            turbo_store.analytics_replace_from_sheets(hist_rows)
                    except Exception:
                        pass
                threading.Thread(target=_seed_pg_analytics, daemon=True).start()
        for r in fast_rows:
            if len(r) >= 4 and r[0] >= start:
                try:
                    totals[(r[0], r[1], r[2])] += int(r[3])
                except ValueError:
                    continue
    else:
        if is_mocked_sheets:
            sheet_rows = sheets_store.read_rows(SHEET_TITLE, HEADER)
        elif sheets_store.is_disabled():
            sheet_rows = []
        else:
            now_mono = time.monotonic()
            with _lock:
                cached_ts = _sheet_rows_cache['ts']
                cached_rows = _sheet_rows_cache['rows']
            if cached_rows is not None and cached_ts > 0.0 and (now_mono - cached_ts < _SHEET_ROWS_CACHE_TTL):
                sheet_rows = cached_rows
            elif cached_rows is not None and not allow_blocking:
                _refresh_sheet_rows_async()
                sheet_rows = cached_rows
            elif not allow_blocking:
                _refresh_sheet_rows_async()
                sheet_rows = []
            else:
                sheet_rows = sheets_store.read_rows(SHEET_TITLE, HEADER)
                with _lock:
                    _sheet_rows_cache['ts'] = time.monotonic()
                    _sheet_rows_cache['rows'] = sheet_rows
        if sheet_rows:
            for r in sheet_rows:
                if len(r) >= 4 and r[0] >= start:
                    try:
                        totals[(r[0], r[1], r[2])] += int(r[3])
                    except ValueError:
                        continue
        else:
            with _lock:
                for k, c in _flushed.items():
                    totals[k] += c
    with _lock:
        for k, c in _pending.items():
            totals[k] += c

    by_event = defaultdict(int)
    by_label = defaultdict(lambda: defaultdict(int))
    by_day = defaultdict(lambda: defaultdict(int))
    article_views = 0
    for (d, e, l), c in totals.items():
        if d < start:
            continue
        by_event[e] += c
        by_label[e][l] += c
        by_day[d][e] += c
        if e == 'page_view' and l and l != '/':
            article_views += c

    by_label_sorted = {e: sorted(v.items(), key=lambda x: -x[1])[:15] for e, v in by_label.items()}
    by_label_pretty = {}
    for e, items in by_label_sorted.items():
        max_c = items[0][1] if items else 1
        ev_tot = max(1, by_event.get(e, 1))
        pretty_list = []
        for raw_lbl, cnt in items:
            desc = describe_label(e, raw_lbl)
            pretty_list.append({
                'raw': raw_lbl,
                'title': desc['title'],
                'sub': desc['sub'],
                'href': desc['href'],
                'count': cnt,
                'bar_pct': max(4, round((cnt / max_c) * 100)),
                'share_pct': round((cnt / ev_tot) * 100, 1),
            })
        by_label_pretty[e] = pretty_list

    pv_total = by_event.get('page_view', 0)
    store_open_total = by_event.get('store_open', 0)
    aff_total = by_event.get('affiliate_click', 0)
    map_total = by_event.get('map_cafe', 0) + by_event.get('map_bar', 0)
    official_total = by_event.get('official_click', 0)
    sns_total = by_event.get('sns_click', 0)
    share_total = by_event.get('share', 0)
    vote_total = by_event.get('vote', 0)

    engagement_total = store_open_total + article_views + vote_total
    action_total = aff_total + map_total + official_total + sns_total + share_total

    funnel = {
        'pv_total': pv_total,
        'article_views': article_views,
        'store_open_total': store_open_total,
        'engagement_total': engagement_total,
        'engagement_rate_pct': round((engagement_total / pv_total) * 100, 1) if pv_total > 0 else 0.0,
        'action_total': action_total,
        'action_rate_pct': round((action_total / pv_total) * 100, 1) if pv_total > 0 else 0.0,
        'aff_total': aff_total,
        'aff_ctr_pct': round((aff_total / pv_total) * 100, 2) if pv_total > 0 else 0.0,
        'map_total': map_total,
        'official_total': official_total,
        'sns_total': sns_total,
        'share_total': share_total,
        'vote_total': vote_total,
    }

    # Estimated revenue simulation (AdSense RPM ~350 yen / 1,000 PV, Affiliate ~90 yen expected value per click = 3% CVR * 3,000 yen)
    est_adsense_yen = round(pv_total * 0.35)
    est_aff_yen = round(aff_total * 90)
    est_total_yen = est_adsense_yen + est_aff_yen
    active_days_count = max(1, len(by_day))
    est_monthly_pace_yen = round((est_total_yen / active_days_count) * 30)

    revenue = {
        'est_adsense_yen': est_adsense_yen,
        'est_aff_yen': est_aff_yen,
        'est_total_yen': est_total_yen,
        'est_monthly_pace_yen': est_monthly_pace_yen,
    }

    # Build daily series (newest first for table, chronological for chart)
    sorted_days = sorted(by_day.keys())
    max_day_pv = max([by_day[d].get('page_view', 0) for d in sorted_days], default=1) or 1
    daily_series = []
    for d in sorted_days:
        evs = by_day[d]
        dpv = evs.get('page_view', 0)
        dopen = evs.get('store_open', 0)
        daff = evs.get('affiliate_click', 0)
        dmap = evs.get('map_cafe', 0) + evs.get('map_bar', 0)
        dsns = evs.get('sns_click', 0) + evs.get('official_click', 0) + evs.get('share', 0)
        daily_series.append({
            'date': d,
            'short_date': d[5:].replace('-', '/'),
            'is_today': d == today_str,
            'is_yesterday': d == yesterday_str,
            'pv': dpv,
            'store_open': dopen,
            'affiliate': daff,
            'map': dmap,
            'sns_official': dsns,
            'pv_bar_pct': max(4, round((dpv / max_day_pv) * 100)) if dpv > 0 else 0,
        })

    return {
        'days': days,
        'since': start,
        'today': today_str,
        'by_event': dict(sorted(by_event.items(), key=lambda x: -x[1])),
        'by_label': by_label_sorted,
        'all_by_label': {e: dict(v) for e, v in by_label.items()},
        'by_label_pretty': by_label_pretty,
        'by_day': {d: dict(v) for d, v in sorted(by_day.items(), reverse=True)},
        'daily_series': daily_series,
        'funnel': funnel,
        'revenue': revenue,
    }


def reset_for_tests():
    global _pg_seeded_from_sheets
    with _lock:
        _pending.clear()
        _sheets_retry.clear()
        _flushed.clear()
        _sheet_rows_cache['ts'] = 0.0
        _sheet_rows_cache['rows'] = None
        _pg_seeded_from_sheets = False
