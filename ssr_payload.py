"""SSR payload slimming and deferred card pool builders for top/staff pages."""

# Fields only used by the server-side card template (or by nothing on the client).
# Dropped from `window.INITIAL_STATUS_DATA` embedded in the top page to keep HTML compact.
CLIENT_STATUS_DROP_KEYS = (
    'ssr_title', 'ssr_brand', 'ssr_brand_cls', 'ssr_disp', 'ssr_is_dup', 'ssr_ladies_pct', 'ssr_men_pct',
    'seo_name', 'fetched_at', 'pos_telemetry',
)
CLIENT_STATUS_DROP_NESTED = {
    'forecast_60m': ('model_version',),
    'wait_estimate': ('instant_match_prob_pct',),
}


def client_status_payload(status):
    if not isinstance(status, dict) or not isinstance(status.get('ranking'), list):
        return status
    slim = dict(status)
    ranking = []
    for st in status['ranking']:
        if not isinstance(st, dict):
            ranking.append(st)
            continue
        item = {k: v for k, v in st.items() if k not in CLIENT_STATUS_DROP_KEYS}
        for parent, keys in CLIENT_STATUS_DROP_NESTED.items():
            sub = item.get(parent)
            if isinstance(sub, dict):
                item[parent] = {k: v for k, v in sub.items() if k not in keys}
        ranking.append(item)
    slim['ranking'] = ranking
    return slim


def build_deferred_card_pool(status, is_staff=False, start_idx=20):
    """Build compact card descriptors for stores ranked beyond SSR_RANK_LIMIT so pinned stores render real cards on first paint (zero empty black placeholder boxes)."""
    if not isinstance(status, dict) or not isinstance(status.get('ranking'), list):
        return []
    out = []
    for s in status['ranking'][start_idx:]:
        if not isinstance(s, dict):
            continue
        name = s.get('seo_name') or s.get('name') or ''
        if not name:
            continue
        w = int(s.get('women') or 0)
        m = int(s.get('men') or 0)
        we = s.get('wait_estimate') if isinstance(s.get('wait_estimate'), dict) else {}
        fc = s.get('forecast_60m') if isinstance(s.get('forecast_60m'), dict) else {}
        votes = s.get('votes') if isinstance(s.get('votes'), dict) else {}
        wait_m = int(we.get('est_wait_men_min') or 0)
        if w == 0 and m == 0:
            hl = '営業時間前または準備中の時間帯です'
        elif w >= m + 12 and wait_m == 0:
            hl = f'いま女性が{w}名いて、男性もスムーズに入れます'
        elif wait_m == 0 and w >= m:
            hl = '男女バランスが良く、待ち時間なしで案内中です'
        elif wait_m > 0:
            hl = '男性はやや混雑（女性はすぐ相席しやすい状況）'
        else:
            hl = f'現在女性が{w}名・男性が{m}名入店中です'

        ssr_rank = s.get('ssr_rank')
        if not is_staff and ssr_rank:
            rk = f"{int(ssr_rank):02d}"
        elif ssr_rank:
            rk = str(ssr_rank)
        else:
            rk = '-'

        w_diff = int(s.get('women_diff') or 0)
        m_diff = int(s.get('men_diff') or 0)
        v_smooth = int(votes.get('smooth') or 0)
        v_wait = int(votes.get('waiting') or 0)
        mt = ''
        mtt = ''
        if w_diff >= 1:
            mt = f'↗ +{w_diff}名'
            mtt = f'直近約15分で女性が+{w_diff}名増えています'
        elif v_smooth > 0 and v_smooth >= v_wait:
            mt = '即入店'
            mtt = '直近の現場レポートで「待ちなしで入れた」が報告されています'

        has_guest = (w + m) > 0 or int(we.get('est_men_wait_groups') or 0) > 0
        w_badge = (we.get('wait_badge_men') or '') if (we and has_guest) else ''
        f_badge = (fc.get('badge_men') or '') if fc else ''

        entry = {
            'n': name,
            'rk': rk,
            'bc': s.get('ssr_brand_cls') or '',
            'br': s.get('ssr_brand') or '',
            'ar': s.get('ssr_area') or '',
            'ti': s.get('ssr_title') or name,
            'hl': hl,
            'w': w,
            'm': m,
            'wp': int(s.get('ssr_ladies_pct') or 0),
            'mp': int(s.get('ssr_men_pct') or 0),
        }
        if s.get('ssr_is_dup'):
            entry['dup'] = 1
            entry['dp'] = s.get('ssr_disp') or ''
        if not is_staff and s.get('ssr_event_badge'):
            entry['eb'] = s.get('ssr_event_badge')
            entry['et'] = s.get('ssr_event_title') or ''
        if mt:
            entry['mt'] = mt
            entry['mtt'] = mtt
        # Only measured values are exposed (no fabricated core-time / midnight retention fallbacks)
        if s.get('has_measured_core') and s.get('core_time_short'):
            entry['ct'] = s.get('core_time_short')
        if s.get('has_measured_midnight') and s.get('midnight_retention_pct') is not None:
            entry['mr'] = s.get('midnight_retention_pct')
            entry['ml'] = s.get('midnight_label_short') or ''
        if w_badge:
            entry['wb'] = w_badge
            entry['wt'] = we.get('wait_tone') or 'neutral'
            entry['wti'] = we.get('pos_summary_short') or ''
        if f_badge:
            entry['fb'] = f_badge
            entry['ft'] = fc.get('badge_tone') or 'neutral'
            entry['fti'] = fc.get('reason_short') or ''
        if w_diff > 0:
            entry['wd'] = w_diff
        if m_diff > 0:
            entry['md'] = m_diff
        if s.get('usual_w_diff') is not None:
            entry['uwd'] = int(s.get('usual_w_diff'))
            entry['uw'] = s.get('usual_women')
            entry['usl'] = s.get('usual_slot_label') or ''
        if s.get('usual_m_diff') is not None:
            entry['umd'] = int(s.get('usual_m_diff'))
            entry['um'] = s.get('usual_men')
            entry['usl'] = s.get('usual_slot_label') or ''
        out.append(entry)
    return out
