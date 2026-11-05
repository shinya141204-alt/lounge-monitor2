"""store_insights.py - 店舗ページ（/store/<slug>）用の「店舗固有データ分析文」生成

相席ナビが記録している monitor_logs（日時・店舗・男性人数・女性人数）だけを材料に、
その店舗にしか当てはまらない数値（ピーク曜日・時間帯、終電後の女性残存率、平日と週末の差、
同じエリアの他店との比較）を計算し、短い分析文に組み立てる。

方針:
- 推測値で穴埋めしない。記録が足りない指標は文章ごと省略し、店舗全体で記録が少なければ None を返す。
- 0時〜6時台の記録は前日の営業日として扱う。
- 男女とも 0 名の記録（営業時間外・取得失敗）は集計に使わない。
- 女性比率は男女合計 6 名以上の記録のみで計算する（少人数時のぶれを避けるため）。
"""
import datetime
import statistics
import threading
import time
from collections import defaultdict

WEEKDAYS_JA = ['月', '火', '水', '木', '金', '土', '日']
BIZ_HOURS_ORDER = [17, 18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5]
WEEKEND_BIZ_WD = (4, 5)  # 金曜・土曜の夜（日付が変わった後も前日扱い）

DEFAULT_DAYS = 28
MIN_ROWS = 120
MIN_BIZ_DAYS = 5
MIN_RATIO_TOTAL = 6

_CACHE_TTL_SEC = 3600
_cache = {}
_cache_lock = threading.Lock()


def _parse_ts(ts):
    if isinstance(ts, datetime.datetime):
        return ts.replace(tzinfo=None)
    s = str(ts or '')
    if len(s) < 16:
        return None
    try:
        return datetime.datetime(int(s[0:4]), int(s[5:7]), int(s[8:10]), int(s[11:13]), int(s[14:16]))
    except (ValueError, TypeError):
        return None


def _mean(vals):
    return (sum(vals) / len(vals)) if vals else None


def _fmt_hour(h: int) -> str:
    return f'{h}時' if h >= 7 else f'翌{h}時'


def _biz_hour_index(h: int) -> int:
    return BIZ_HOURS_ORDER.index(h) if h in BIZ_HOURS_ORDER else -1


def compute_store_stats(store_name: str, days: int = DEFAULT_DAYS, rows=None) -> dict:
    """Return per-store aggregate stats, or None when there is not enough recorded data."""
    if rows is None:
        import local_db
        rows = local_db.get_store_history(store_name, days=days)
    obs = []
    for r in rows or []:
        dt = _parse_ts(r.get('ts'))
        if dt is None:
            continue
        try:
            w = int(r.get('women') or 0)
            m = int(r.get('men') or 0)
        except (TypeError, ValueError):
            continue
        if w + m <= 0 or w < 0 or m < 0:
            continue
        biz_dt = dt - datetime.timedelta(days=1) if dt.hour <= 6 else dt
        obs.append((biz_dt.date(), biz_dt.weekday(), dt.hour, dt.strftime('%H:%M'), w, m))

    by_day = defaultdict(list)
    for o in obs:
        by_day[o[0]].append(o)
    valid_days = {d: lst for d, lst in by_day.items() if len(lst) >= 6}
    if len(obs) < MIN_ROWS or len(valid_days) < MIN_BIZ_DAYS:
        return None

    # 1) 営業日ごとのピーク（最大女性数）
    day_peaks = []
    for d, lst in sorted(valid_days.items()):
        peak = max(lst, key=lambda o: (o[4], -_biz_hour_index(o[2])))
        day_peaks.append({
            'date': d, 'wd': d.weekday(), 'peak_w': peak[4], 'peak_m': peak[5],
            'peak_hm': peak[3], 'peak_hour': peak[2],
        })

    # 2) 曜日別
    wd_table = []
    for wd in range(7):
        dps = [p for p in day_peaks if p['wd'] == wd]
        if not dps:
            continue
        ratios = [o[4] / (o[4] + o[5]) * 100 for d, lst in valid_days.items() if d.weekday() == wd
                  for o in lst if 21 <= o[2] <= 23 and o[4] + o[5] >= MIN_RATIO_TOTAL]
        peak_idx = sorted(_biz_hour_index(p['peak_hour']) for p in dps)
        med_idx = peak_idx[len(peak_idx) // 2]
        wd_table.append({
            'wd': wd,
            'label': f'{WEEKDAYS_JA[wd]}曜',
            'days': len(dps),
            'avg_peak_w': round(_mean([p['peak_w'] for p in dps]), 1),
            'max_peak_w': max(p['peak_w'] for p in dps),
            'typical_peak_hour': BIZ_HOURS_ORDER[med_idx] if med_idx >= 0 else None,
            'typical_peak_label': _fmt_hour(BIZ_HOURS_ORDER[med_idx]) + '台' if med_idx >= 0 else '―',
            'core_wpct': round(_mean(ratios)) if len(ratios) >= 5 else None,
        })

    # 3) 時間帯別の平均女性・男性人数
    hour_acc = defaultdict(lambda: [0, 0, 0])
    for o in obs:
        a = hour_acc[o[2]]
        a[0] += o[4]
        a[1] += o[5]
        a[2] += 1
    hourly = []
    for h in BIZ_HOURS_ORDER:
        a = hour_acc.get(h)
        if a and a[2] >= 5:
            hourly.append({'hour': h, 'avg_w': round(a[0] / a[2], 1), 'avg_m': round(a[1] / a[2], 1), 'n': a[2]})
    peak_hour = max(hourly, key=lambda x: x['avg_w']) if hourly else None
    ramp_hour = None
    if peak_hour and peak_hour['avg_w'] > 0:
        for x in hourly:
            if x['avg_w'] >= peak_hour['avg_w'] * 0.6:
                ramp_hour = x
                break

    # 4) 終電後の女性残存率（21〜22時台平均 → 0〜1時台平均）
    ret_ratios = []
    for d, lst in valid_days.items():
        pre = [o[4] for o in lst if o[2] in (21, 22)]
        post = [o[4] for o in lst if o[2] in (0, 1)]
        if len(pre) >= 2 and len(post) >= 2 and _mean(pre) >= 3:
            ret_ratios.append(_mean(post) / _mean(pre) * 100)
    retention_pct = round(statistics.median(ret_ratios)) if len(ret_ratios) >= 4 else None
    late_ratios = []
    for d, lst in valid_days.items():
        pre = [o[4] for o in lst if o[2] in (21, 22)]
        late = [o[4] for o in lst if o[2] in (2, 3)]
        if len(pre) >= 2 and len(late) >= 2 and _mean(pre) >= 3:
            late_ratios.append(_mean(late) / _mean(pre) * 100)
    late_retention_pct = round(statistics.median(late_ratios)) if len(late_ratios) >= 4 else None

    # 5) 平日と週末
    wkend = [p['peak_w'] for p in day_peaks if p['wd'] in WEEKEND_BIZ_WD]
    wkday = [p['peak_w'] for p in day_peaks if p['wd'] not in WEEKEND_BIZ_WD]

    def _core_wpct(pred):
        vals = [o[4] / (o[4] + o[5]) * 100 for d, lst in valid_days.items() if pred(d.weekday())
                for o in lst if 21 <= o[2] <= 23 and o[4] + o[5] >= MIN_RATIO_TOTAL]
        return round(_mean(vals)) if len(vals) >= 10 else None

    # 6) 時間帯ごとの女性比率・男性先行率
    def _band_wpct(hours):
        vals = [o[4] / (o[4] + o[5]) * 100 for o in obs if o[2] in hours and o[4] + o[5] >= MIN_RATIO_TOTAL]
        return round(_mean(vals)) if len(vals) >= 10 else None

    core_obs = [o for o in obs if 21 <= o[2] <= 23 and o[4] + o[5] >= MIN_RATIO_TOTAL]
    men_first_rate = round(sum(1 for o in core_obs if o[5] > o[4]) / len(core_obs) * 100) if len(core_obs) >= 20 else None
    ten_plus_days = sum(1 for p in day_peaks if p['peak_w'] >= 10)

    best_day = max(day_peaks, key=lambda p: p['peak_w'])
    all_dates = sorted(valid_days.keys())
    return {
        'store_name': store_name,
        'n_rows': len(obs),
        'n_days': len(valid_days),
        'period_start': all_dates[0].isoformat(),
        'period_end': all_dates[-1].isoformat(),
        'avg_peak_w': round(_mean([p['peak_w'] for p in day_peaks]), 1),
        'avg_peak_m': round(_mean([p['peak_m'] for p in day_peaks]), 1),
        'best_day': {
            'label': f"{best_day['date'].month}/{best_day['date'].day}({WEEKDAYS_JA[best_day['wd']]})",
            'peak_w': best_day['peak_w'], 'peak_m': best_day['peak_m'],
            'peak_hm': ('翌' + best_day['peak_hm']) if best_day['peak_hour'] <= 6 else best_day['peak_hm'],
        },
        'ten_plus_days': ten_plus_days,
        'weekday_table': wd_table,
        'hourly': hourly,
        'peak_hour': peak_hour,
        'ramp_hour': ramp_hour,
        'retention_pct': retention_pct,
        'retention_days': len(ret_ratios),
        'late_retention_pct': late_retention_pct,
        'weekend_avg_peak_w': round(_mean(wkend), 1) if len(wkend) >= 2 else None,
        'weekday_avg_peak_w': round(_mean(wkday), 1) if len(wkday) >= 3 else None,
        'weekend_core_wpct': _core_wpct(lambda wd: wd in WEEKEND_BIZ_WD),
        'weekday_core_wpct': _core_wpct(lambda wd: wd not in WEEKEND_BIZ_WD),
        'wpct_early': _band_wpct((18, 19, 20)),
        'wpct_core': _band_wpct((21, 22)),
        'wpct_lasttrain': _band_wpct((23, 0)),
        'wpct_late': _band_wpct((1, 2, 3)),
        'men_first_rate': men_first_rate,
        'top_nights': [
            {
                'label': f"{p['date'].month}/{p['date'].day}({WEEKDAYS_JA[p['wd']]})",
                'hm': ('翌' + p['peak_hm']) if p['peak_hour'] <= 6 else p['peak_hm'],
                'w': p['peak_w'], 'm': p['peak_m'],
            }
            for p in sorted(day_peaks, key=lambda p: (-p['peak_w'], p['date']))[:3]
        ],
        'min_peak_w': min(p['peak_w'] for p in day_peaks),
        'max_peak_w': max(p['peak_w'] for p in day_peaks),
        'men_peak_hour': (max(hourly, key=lambda x: x['avg_m'])['hour'] if hourly else None),
        'first_hour': hourly[0] if hourly else None,
        'last_active_hour': next((x for x in reversed(hourly) if x['avg_w'] >= 1.0), None),
    }


def get_store_stats_cached(store_name: str, days: int = DEFAULT_DAYS):
    now = time.monotonic()
    key = (store_name, days)
    with _cache_lock:
        hit = _cache.get(key)
        if hit and now - hit[0] < _CACHE_TTL_SEC:
            return hit[1]
    try:
        stats = compute_store_stats(store_name, days=days)
    except Exception:
        stats = None
    with _cache_lock:
        _cache[key] = (now, stats)
        if len(_cache) > 256:
            for k, _ in sorted(_cache.items(), key=lambda kv: kv[1][0])[:64]:
                _cache.pop(k, None)
    return stats


def clear_cache():
    with _cache_lock:
        _cache.clear()


def _area_comparison(store_name: str, stats: dict, area_store_names, area_label: str, seo_name_fn, stats_fn):
    peers = []
    for n in area_store_names:
        if n == store_name:
            continue
        st = stats_fn(n)
        if st:
            peers.append((n, st))
    if not peers:
        return None
    group = peers + [(store_name, stats)]
    ranked = sorted(group, key=lambda kv: kv[1]['avg_peak_w'], reverse=True)
    rank = [n for n, _ in ranked].index(store_name) + 1
    ret_vals = [st['retention_pct'] for _, st in group if st.get('retention_pct') is not None]
    core_vals = [st['wpct_core'] for _, st in group if st.get('wpct_core') is not None]
    top_other = next((kv for kv in ranked if kv[0] != store_name), None)
    return {
        'area_label': area_label,
        'n_stores': len(group),
        'rank_by_peak': rank,
        'top_other_name': seo_name_fn(top_other[0]) if top_other else None,
        'top_other_peak_w': top_other[1]['avg_peak_w'] if top_other else None,
        'area_median_peak_w': round(statistics.median([st['avg_peak_w'] for _, st in group]), 1),
        'area_median_retention': round(statistics.median(ret_vals)) if len(ret_vals) >= 2 else None,
        'area_median_core_wpct': round(statistics.median(core_vals)) if len(core_vals) >= 2 else None,
    }


def build_store_insight(store_name: str, seo_name: str, area_label: str = '', area_store_names=None,
                        seo_name_fn=None, stats_fn=None, nearest=None) -> dict:
    """Build the narrative block for /store/<slug>. Returns None when data is insufficient."""
    stats_fn = stats_fn or get_store_stats_cached
    seo_name_fn = seo_name_fn or (lambda n: n)
    st = stats_fn(store_name)
    if not st:
        return None

    sections = []
    s = seo_name

    # --- 1. 曜日の傾向 ---
    wdt = [r for r in st['weekday_table'] if r['days'] >= 2]
    if len(wdt) >= 3:
        best = max(wdt, key=lambda r: r['avg_peak_w'])
        worst = min(wdt, key=lambda r: r['avg_peak_w'])
        txt = (f"{s}で女性が最も多く集まったのは{best['label']}日の夜で、営業日ごとの最大女性数の平均は{best['avg_peak_w']}名"
               f"（記録{best['days']}日分、最多{best['max_peak_w']}名）でした。")
        if worst['wd'] != best['wd']:
            gap = round(best['avg_peak_w'] - worst['avg_peak_w'], 1)
            if worst['avg_peak_w'] > 0 and best['avg_peak_w'] / worst['avg_peak_w'] >= 1.8:
                txt += f"反対に{worst['label']}日は平均{worst['avg_peak_w']}名にとどまり、曜日による差が{gap}名と大きい店舗です。曜日を選ぶだけで店内の人数がかなり変わります。"
            elif gap >= 2:
                txt += f"最も少ない{worst['label']}日は平均{worst['avg_peak_w']}名で、差は{gap}名です。"
            else:
                txt += f"最も少ない{worst['label']}日でも平均{worst['avg_peak_w']}名あり、曜日による差は{gap}名と小さめです。"
        if best.get('typical_peak_hour') is not None:
            txt += f"{best['label']}日のピークは{_fmt_hour(best['typical_peak_hour'])}台に来ることが多くなっています。"
        sections.append({'heading': '曜日による違い', 'text': txt,
                         'score': min(3.0, best['avg_peak_w'] / max(worst['avg_peak_w'], 0.5) - 1.0) / 1.5})

    # --- 2. 時間帯の傾向 ---
    ph, rh = st.get('peak_hour'), st.get('ramp_hour')
    if ph:
        txt = f"時間帯別に平均すると、女性が最も多いのは{_fmt_hour(ph['hour'])}台（平均{ph['avg_w']}名・男性{ph['avg_m']}名）です。"
        if rh and rh['hour'] != ph['hour']:
            txt += f"{_fmt_hour(rh['hour'])}台にはピークの6割（平均{rh['avg_w']}名）に達しているため、混み始める前に入るなら{_fmt_hour(rh['hour'])}台より前が目安になります。"
        if ph['hour'] <= 5:
            txt += "日付が変わってから人数が最大になる、深夜型の店舗です。"
        elif ph['hour'] <= 20:
            txt += "早い時間に人数が最大になり、その後は緩やかに減っていく前半型の店舗です。"
        bands = [('18〜20時台', st.get('wpct_early')), ('21〜22時台', st.get('wpct_core')),
                 ('23〜0時台', st.get('wpct_lasttrain')), ('翌1〜3時台', st.get('wpct_late'))]
        bands = [(lbl, v) for lbl, v in bands if v is not None]
        if len(bands) >= 2:
            hi = max(bands, key=lambda b: b[1])
            lo = min(bands, key=lambda b: b[1])
            txt += f"女性比率は{hi[0]}が{hi[1]}%で最も高く、{lo[0]}は{lo[1]}%でした（男女合計6名以上の記録で計算）。"
        fh, la, mph = st.get('first_hour'), st.get('last_active_hour'), st.get('men_peak_hour')
        if fh and fh['hour'] != ph['hour'] and fh['avg_w'] > 0:
            txt += f"記録の始まる{_fmt_hour(fh['hour'])}台は平均{fh['avg_w']}名で、ピークまでに約{round(ph['avg_w'] / fh['avg_w'], 1)}倍に増えます。"
        if mph is not None and mph != ph['hour']:
            lag = _biz_hour_index(mph) - _biz_hour_index(ph['hour'])
            if lag > 0:
                txt += f"男性の平均人数が最も多いのは{_fmt_hour(mph)}台で、女性のピークより{lag}時間遅れて男性が増える傾向があります。"
            else:
                txt += f"男性の平均人数が最も多いのは{_fmt_hour(mph)}台で、女性より{abs(lag)}時間早く男性のピークが来ます。"
        if la and la['hour'] != ph['hour']:
            txt += f"女性が平均1名以上記録されている最後の時間帯は{_fmt_hour(la['hour'])}台です。"
        sections.append({'heading': '時間帯による違い', 'text': txt,
                         'score': abs(_biz_hour_index(ph['hour']) - _biz_hour_index(22)) / 2.0})

    # --- 3. 終電後 ---
    ret = st.get('retention_pct')
    if ret is not None:
        txt = f"21〜22時台の女性数を100としたとき、0〜1時台に残っている女性数は中央値で{ret}%でした（{st['retention_days']}営業日分）。"
        if ret >= 100:
            txt += "終電の時間を過ぎても女性が減らず、むしろ増える日が多い店舗です。終電を気にしないで済む場合は、深夜帯も選択肢になります。"
        elif ret >= 75:
            txt += "終電後も女性の多くが残る傾向があり、23時以降に入っても極端に人が減ることは少なめです。"
        elif ret >= 50:
            txt += "終電の前後で女性の数が目に見えて減ります。0時を過ぎてから入る場合は、近隣店の人数も確認しておくと安心です。"
        else:
            txt += "終電の前に女性の多くが帰る傾向がはっきりしています。この店舗を選ぶなら23時台までの入店が目安です。"
        late = st.get('late_retention_pct')
        if late is not None:
            txt += f"さらに翌2〜3時台では{late}%まで{'下がります' if late < ret else '維持されています'}。"
        sections.append({'heading': '終電後に女性がどれだけ残るか', 'text': txt, 'score': abs(ret - 85) / 30.0})

    # --- 4. 平日と週末 ---
    we, wk = st.get('weekend_avg_peak_w'), st.get('weekday_avg_peak_w')
    if we is not None and wk is not None:
        txt = f"金曜・土曜の夜の最大女性数は平均{we}名、それ以外の曜日は平均{wk}名でした。"
        if wk > 0 and we / wk >= 1.5:
            txt += f"週末は平日の約{round(we / wk, 1)}倍の人数になり、週末の集客に大きく頼っている店舗です。"
        elif we >= wk:
            txt += f"週末のほうが{round(we - wk, 1)}名多いものの、平日との差はそれほど大きくありません。"
        else:
            txt += f"平日のほうが{round(wk - we, 1)}名多く、週末に混むとは限らない店舗です。"
        wcw, wdw = st.get('weekend_core_wpct'), st.get('weekday_core_wpct')
        if wcw is not None and wdw is not None:
            if abs(wcw - wdw) >= 3:
                txt += f"21〜23時台の女性比率は週末{wcw}%・平日{wdw}%で、{'週末' if wcw > wdw else '平日'}のほうが女性の割合が高くなっています。"
            else:
                txt += f"21〜23時台の女性比率は週末{wcw}%・平日{wdw}%とほぼ同じです。"
        sections.append({'heading': '平日と週末の差', 'text': txt, 'score': abs(we / max(wk, 0.5) - 1.8) / 0.6})

    # --- 5. 男性先行・規模感 ---
    mfr = st.get('men_first_rate')
    if mfr is not None:
        txt = f"21〜23時台の記録のうち、男性が女性より多かった時間の割合は{mfr}%です。"
        if mfr >= 60:
            txt += "男性が先に集まりやすく、女性の到着を待つ時間が発生しやすい店舗といえます。"
        elif mfr <= 30:
            txt += "女性が同数以上いる時間が長く、男性が待ちにくい店舗です。"
        else:
            txt += "男女どちらかに大きく偏ることは少ない店舗です。"
        txt += (f"記録期間中、最大女性数が10名以上になった営業日は{st['n_days']}日中{st['ten_plus_days']}日で、"
                f"日ごとの最大女性数は少ない日で{st['min_peak_w']}名、多い日で{st['max_peak_w']}名でした。")
        tops = st.get('top_nights') or []
        if tops:
            txt += '最も混んだ夜は、' + '、'.join(
                f"{t['label']} {t['hm']}頃（女性{t['w']}名・男性{t['m']}名）" for t in tops
            ) + 'です。'
        sections.append({'heading': '男女のバランスと店の規模', 'text': txt, 'score': abs(mfr - 25) / 20.0})

    # --- 6. 同エリア比較 ---
    if area_store_names:
        cmp_ = _area_comparison(store_name, st, area_store_names, area_label, seo_name_fn, stats_fn)
        if cmp_:
            txt = (f"{cmp_['area_label'] or '同じエリア'}で記録がある{cmp_['n_stores']}店舗のうち、"
                   f"{s}の最大女性数の平均（{st['avg_peak_w']}名）は{cmp_['rank_by_peak']}番目です。")
            if cmp_['rank_by_peak'] == 1 and cmp_['top_other_name']:
                txt += f"2番目の{cmp_['top_other_name']}（{cmp_['top_other_peak_w']}名）を上回り、エリアで最も人が集まる店舗です。"
            elif cmp_['top_other_name']:
                txt += f"エリアで最も多いのは{cmp_['top_other_name']}（{cmp_['top_other_peak_w']}名）で、エリアの中央値は{cmp_['area_median_peak_w']}名です。"
            amr = cmp_.get('area_median_retention')
            if amr is not None and ret is not None:
                diff = ret - amr
                if diff >= 10:
                    txt += f"終電後の残存率はエリア中央値（{amr}%）より{diff}ポイント高く、深夜の受け皿になりやすい店舗です。"
                elif diff <= -10:
                    txt += f"終電後の残存率はエリア中央値（{amr}%）より{abs(diff)}ポイント低いため、深夜は近隣店へ移るのも一つの方法です。"
                else:
                    txt += f"終電後の残存率はエリア中央値（{amr}%）と同程度です。"
            amc = cmp_.get('area_median_core_wpct')
            if amc is not None and st.get('wpct_core') is not None:
                txt += f"21〜22時台の女性比率は{st['wpct_core']}%（エリア中央値{amc}%）です。"
            sections.append({'heading': f"{cmp_['area_label'] or '同じエリア'}の他店との比較", 'text': txt,
                             'score': 1.5 if cmp_['rank_by_peak'] in (1, cmp_['n_stores']) else 0.5})

    # --- 7. 最寄り店舗との比較（直線距離） ---
    near_parts = []
    for n, km in (nearest or []):
        ost = stats_fn(n)
        if not ost:
            continue
        dist = (f"約{round(km * 1000 / 50) * 50}m" if km < 1 else f"約{round(km, 1)}km") if km is not None else '近隣'
        part = f"{seo_name_fn(n)}（直線{dist}）は最大女性数の平均{ost['avg_peak_w']}名"
        diff = round(ost['avg_peak_w'] - st['avg_peak_w'], 1)
        part += f"で{s}より{abs(diff)}名{'多く' if diff > 0 else '少なく'}" if abs(diff) >= 0.5 else "で同程度"
        o_ph = ost.get('peak_hour')
        if o_ph:
            part += f"、ピークは{_fmt_hour(o_ph['hour'])}台"
        if ost.get('retention_pct') is not None:
            part += f"、終電後の残存率は{ost['retention_pct']}%"
        near_parts.append(part + 'です。')
    if near_parts:
        txt = ''.join(near_parts)
        if ph:
            txt += f"{s}自身のピークは{_fmt_hour(ph['hour'])}台なので、時間帯がずれる店舗を組み合わせると、混む時間を避けて移動しやすくなります。"
        sections.append({'heading': '最寄りの店舗との比較', 'text': txt, 'score': 0.8})

    if len(sections) < 2:
        return None
    # その店舗で平均から最も外れている（＝特徴的な）指標から順に並べる
    sections.sort(key=lambda sec: -sec.get('score', 0))

    weekday_rows = [r for r in st['weekday_table']]
    hourly_rows = [
        {'label': _fmt_hour(x['hour']) + '台', 'avg_w': x['avg_w'], 'avg_m': x['avg_m'],
         'wpct': round(x['avg_w'] / (x['avg_w'] + x['avg_m']) * 100) if (x['avg_w'] + x['avg_m']) > 0 else None}
        for x in st['hourly']
    ]
    return {
        'sections': sections,
        'weekday_rows': weekday_rows,
        'hourly_rows': hourly_rows,
        'n_rows': st['n_rows'],
        'n_days': st['n_days'],
        'period_start': st['period_start'],
        'period_end': st['period_end'],
    }
