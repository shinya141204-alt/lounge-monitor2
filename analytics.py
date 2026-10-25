"""
analytics.py - 自前の軽量アクセス集計（個人情報なし）

「どの記事が読まれ、どのボタン・アフィリエイトがクリックされているか」を
日別・イベント別・ラベル別の件数だけで集計します（IP・端末IDなどは保存しません）。

・メモリ上で加算 → 10分ごとに Google Sheets 'Analytics Daily' へまとめて追記
・集計画面（/staff/analytics）は Sheets の値＋未送信分を合算して表示
"""
import datetime
import threading
import logging
from collections import defaultdict

import sheets_store

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
}

_lock = threading.Lock()
_pending = defaultdict(int)   # (date, event, label) -> count（Sheets未送信）
_flushed = defaultdict(int)   # 起動後に送信済みの分（Sheets読込失敗時の表示用）


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
    """未送信分を Sheets に追記。失敗したら戻して次回再送。"""
    with _lock:
        if not _pending:
            return 0
        items = list(_pending.items())
        _pending.clear()
    rows = [[d, e, l, c] for (d, e, l), c in items]
    ok = sheets_store.append_rows(SHEET_TITLE, HEADER, rows)
    with _lock:
        for key, c in items:
            if ok:
                _flushed[key] += c
            else:
                _pending[key] += c
    if not ok:
        logging.warning('[analytics] flush failed; will retry')
    return len(rows) if ok else 0


_PAGE_TITLES = {
    '/': '🔥 トップページ（全国リアルタイム人数ランキング）',
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
    '/guide/brand-comparison': '📖 3大ブランド徹底比較（オリエンタル・ag・JIS）',
    '/guide/weekday-strategy': '📖 水曜・木曜の平日穴場攻略コラム',
    '/guide/weekend-strategy': '📖 金曜・土曜ピークの待ち時間回避＆ハシゴ攻略',
    '/guide/shinjuku': '📖 新宿エリア4店舗の使い分け・穴場ガイド',
    '/guide/solo-vs-pair': '📖 1人参戦（ソロ）vs 2人組のメリット・勝率比較',
    '/guide/second-venue': '📖 2軒目の誘い方・断られない声かけタイミング',
    '/guide/first-10-minutes': '📖 最初の10分で差がつく会話術・第一印象の作り方',
    '/guide/club-area-midnight': '📖 ダンスクラブ密集エリアの深夜0時「第2波」実測データ検証',
    '/guide/beauty-area-stage': '📖 美容サロン激戦区のラウンジが「自分磨きの舞台」になる理由',
    '/guide/male-lead-mechanics': '📖 男性先行・男性比率が高いお店に女性が集まるデータ法則',
    '/guide/price-event-comparison': '📖 全51店舗「料金・イベント・現在人数」比較ボード活用ガイド',
    '/guide/local-vs-urban': '📖 地方店vs都心大型店｜深夜0時に女性が121%へ逆転増加する一極集中の法則',
    '/guide/cost-break-even': '📖 90分粘るvs40分で見切る｜男性の滞在時間と料金の損益分岐点検証',
    '/guide/rain-payday': '📖 雨の日・給料日前・連休中日の男女比変動と隠れ穴場日データ',
    '/privacy': '⚖️ 運営者情報・プライバシーポリシー',
}

_AFFILIATE_NAMES = {
    'spu': '👔 SPU スタイルアップ便（メンズ服コーデ）',
    'spu_styleup': '👔 SPU スタイルアップ便（メンズ服コーデ）',
    'orbis': '🧴 ORBIS Mr.（オルビス ミスター スキンケア）',
    'mens_skincare_clinic': '🧴 ORBIS Mr.（オルビス ミスター スキンケア）',
    'hair_removal': '🪒 メンズ医療脱毛・ヒゲ脱毛クリニック',
    'mens_hair_removal': '🪒 メンズ医療脱毛・ヒゲ脱毛クリニック',
    'dating': '💝 バチェラーデート（審査制マッチングアプリ）',
    'dating_instant': '💝 バチェラーデート（審査制マッチングアプリ）',
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
    'namba-shinsaibashi': '難波・心斎橋エリア',
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


def describe_label(event, raw_label):
    """プログラム用のパスやIDを直感的な日本語名称に変換する。"""
    label = (raw_label or '').strip()
    if event == 'page_view':
        if label in _PAGE_TITLES:
            return {'title': _PAGE_TITLES[label], 'sub': label, 'href': label}
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
        ref_title = _PAGE_TITLES.get(ref_path, ref_path)
        return {'title': item_title, 'sub': f'クリック元: {ref_title}', 'href': ''}

    if event in ('store_open', 'map_cafe', 'map_bar', 'official_click', 'vote'):
        sname = label.split(':', 1)[0].strip() if ':' in label else label
        try:
            import store_master
            info = store_master.get(sname)
            if info:
                seo = store_master.seo_name(sname)
                area = info.get('area_label', '')
                slug = store_master.slug_of(sname)
                return {
                    'title': f'🏪 {seo}',
                    'sub': f'{area}（{sname}）' if area else sname,
                    'href': f'/store/{slug}' if slug else ''
                }
        except Exception:
            pass
        return {'title': label or '-', 'sub': '', 'href': ''}

    if event == 'sns_click':
        return {'title': _SNS_NAMES.get(label.lower(), f'📣 {label}'), 'sub': 'SNS外部リンク', 'href': ''}

    if event == 'share':
        return {'title': f'🔗 シェア・コピー（{label or "汎用"}）', 'sub': '', 'href': ''}

    return {'title': label or '-', 'sub': '', 'href': ''}


def summary(days=7):
    """直近 days 日の集計: {'by_event': {...}, 'by_label': {...}, 'by_label_pretty': {...}, 'funnel': {...}, 'revenue': {...}, 'daily_series': [...]}"""
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    start = (now_jst - datetime.timedelta(days=days - 1)).strftime('%Y-%m-%d')
    today_str = now_jst.strftime('%Y-%m-%d')
    yesterday_str = (now_jst - datetime.timedelta(days=1)).strftime('%Y-%m-%d')

    totals = defaultdict(int)
    sheet_rows = sheets_store.read_rows(SHEET_TITLE, HEADER)
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
        'by_label_pretty': by_label_pretty,
        'by_day': {d: dict(v) for d, v in sorted(by_day.items(), reverse=True)},
        'daily_series': daily_series,
        'funnel': funnel,
        'revenue': revenue,
    }


def reset_for_tests():
    with _lock:
        _pending.clear()
        _flushed.clear()
