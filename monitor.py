import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, wait as futures_wait
import json
import logging
import re
import sys
import threading
import socket
import certifi
import urllib3
import requests.packages.urllib3.util.connection as urllib3_cn

try:
    import lxml  # noqa: F401
    _BS_PARSER = 'lxml'
except ImportError:
    _BS_PARSER = 'html.parser'

# Force IPv4 to prevent hanging on broken IPv6 networks (common on Render free instances)
def allowed_gai_family():
    return socket.AF_INET
urllib3_cn.allowed_gai_family = allowed_gai_family

# Global error log for debugging Render timeouts
fetch_errors = {}
_fetch_errors_lock = threading.Lock()

# URLs to monitor
ORIENTAL_URL = "https://oriental-lounge.com/"
AG_OFFICIAL_URL = "https://ag-ree.jp/"
JIS_URL = "https://jis.bar/"
XIX_URL = "https://aiseki-okayama.conohawing.com/aiseki/parts/get_cs_info.php"
ALFA_URL = "https://aiseki-hiroshima.com/wp/display.php"
YATAKOI_URL = "https://asobibar-823d1.firebaseio.com/shops/chayamachi.json"

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,ja-JP;q=0.9',
}

_ORIENTAL_HEADERS = {
    **_HEADERS,
    'Cookie': 'user_language=ja; first_visit_done=1',
}

# Cache for OLG data (fallback when connection fails)
_olg_cache = {
    'data': [],
    'last_success': None
}

_http_tls = threading.local()
_FETCHER_SESSIONS = {}
_fetcher_session_lock = threading.Lock()
_FETCH_EXECUTOR = ThreadPoolExecutor(max_workers=6, thread_name_prefix='scraper')

_doh_lock = threading.Lock()
_doh_ip_cache = {'ips': [], 'expires_at': 0.0}
_doh_pools = {}

_JIS_DATAS_RE = re.compile(r'var\s+datas\s*=\s*(\{.*?\});', re.DOTALL)
_IPV4_RE = re.compile(r'^\d{1,3}(?:\.\d{1,3}){3}$')
_NON_DIGIT_RE = re.compile(r'[^\d]')


def _get_http_session(fetcher_key: str = 'default'):
    """Returns a persistent per-fetcher requests.Session for 100% HTTP Keep-Alive reuse across cycles,
    falling back to `requests` when patched in unit tests."""
    if getattr(requests.get, '__module__', '') != 'requests.api':
        return requests
    with _fetcher_session_lock:
        session = _FETCHER_SESSIONS.get(fetcher_key)
        if session is None:
            session = requests.Session()
            _FETCHER_SESSIONS[fetcher_key] = session
        return session


def _reset_http_session(fetcher_key: str = 'default'):
    with _fetcher_session_lock:
        old = _FETCHER_SESSIONS.pop(fetcher_key, None)
        if old is not None:
            try:
                old.close()
            except Exception:
                pass
        fresh = requests.Session()
        _FETCHER_SESSIONS[fetcher_key] = fresh
        return fresh


def _parse_int_count(tag) -> int:
    if not tag:
        return 0
    digits = _NON_DIGIT_RE.sub('', tag.get_text(strip=True) or '')
    return int(digits) if digits else 0


def _extract_oriental_stores_from_html(raw_html: bytes) -> list:
    soup = BeautifulSoup(raw_html, _BS_PARSER)
    # Primary selector + resilient fallbacks in case Oriental Lounge removes animation classes
    stores = (
        soup.select('a.card.wave-anime-wrap')
        or soup.select('a.card[href*="/stores/"]')
        or [c for c in soup.select('.card') if c.select_one('.num-male, .num-female')]
    )
    if not stores:
        return []

    store_data = []
    for store in stores:
        try:
            name_tag = store.select_one('h4, .card-title h3, .card-title h4')
            if not name_tag:
                continue
            name = name_tag.get_text(strip=True)
            if not name:
                continue
            men_count = _parse_int_count(store.select_one('.num-male, [class*="num-male"]'))
            women_count = _parse_int_count(store.select_one('.num-female, [class*="num-female"]'))
            store_data.append({
                'name': f"OLG {name}",
                'men': men_count,
                'women': women_count,
                'source': 'oriental'
            })
        except ValueError:
            continue
    return store_data


def _resolve_doh_ipv4(hostname: str) -> list:
    """Resolve authoritative IPv4 A records via RFC 8484 JSON DoH (Google & Cloudflare)
    when the container's local VPC DNS resolver serves a stale NS cache during domain migration."""
    now = time.monotonic()
    with _doh_lock:
        if _doh_ip_cache['ips'] and now < _doh_ip_cache['expires_at']:
            return list(_doh_ip_cache['ips'])

    doh_endpoints = (
        f"https://dns.google/resolve?name={hostname}&type=A",
        f"https://cloudflare-dns.com/dns-query?name={hostname}&type=A",
    )
    sess = _get_http_session('doh')
    resolved_ips = []
    for endpoint in doh_endpoints:
        try:
            resp = sess.get(
                endpoint,
                headers={'Accept': 'application/dns-json', 'User-Agent': _HEADERS['User-Agent']},
                timeout=(3, 5),
            )
            if resp.status_code != 200:
                continue
            payload = resp.json()
            for ans in (payload.get('Answer') or []):
                if int(ans.get('type') or 0) == 1:
                    ip = str(ans.get('data') or '').strip()
                    if _IPV4_RE.match(ip) and ip not in resolved_ips:
                        resolved_ips.append(ip)
            if resolved_ips:
                break
        except Exception as err:
            logging.warning(f"DoH lookup failed via {endpoint}: {err}")

    if resolved_ips:
        with _doh_lock:
            _doh_ip_cache['ips'] = list(resolved_ips)
            _doh_ip_cache['expires_at'] = now + 600.0
    return resolved_ips


def _fetch_oriental_via_doh() -> tuple:
    """Fetch oriental-lounge.com directly from its authoritative IPv4 address with full TLS
    certificate & SNI hostname verification (`CERT_REQUIRED` + `assert_hostname='oriental-lounge.com'`)."""
    ips = _resolve_doh_ipv4('oriental-lounge.com')
    if not ips:
        return [], "DoH returned no A records"

    last_doh_err = "DoH pool exhausted"
    req_headers = {
        **_ORIENTAL_HEADERS,
        'Host': 'oriental-lounge.com',
        'Accept-Encoding': 'gzip',
    }
    for ip in ips:
        try:
            with _doh_lock:
                pool = _doh_pools.get(ip)
                if pool is None:
                    pool = urllib3.HTTPSConnectionPool(
                        ip,
                        port=443,
                        maxsize=2,
                        timeout=urllib3.Timeout(connect=4.0, read=10.0),
                        cert_reqs='CERT_REQUIRED',
                        ca_certs=certifi.where(),
                        server_hostname='oriental-lounge.com',
                        assert_hostname='oriental-lounge.com',
                    )
                    _doh_pools[ip] = pool

            path = '/'
            resp = None
            for _ in range(3):
                resp = pool.request('GET', path, headers=req_headers, redirect=False, decode_content=True)
                if resp.status in (301, 302, 303, 307, 308):
                    loc = resp.headers.get('Location') or ''
                    if loc.startswith('https://oriental-lounge.com/'):
                        path = loc[len('https://oriental-lounge.com'):] or '/'
                        continue
                    elif loc.startswith('/'):
                        path = loc
                        continue
                break

            if resp is None or resp.status != 200:
                status_c = getattr(resp, 'status', 'none')
                last_doh_err = f"DoH IP {ip} HTTP {status_c}"
                continue

            parsed = _extract_oriental_stores_from_html(resp.data)
            if parsed:
                logging.info(f"Recovered Oriental Lounge via authoritative DoH IP {ip}: {len(parsed)} stores")
                return parsed, None
            preview = (resp.data or b'')[:120].decode('utf-8', errors='replace').strip()
            last_doh_err = f"DoH IP {ip} returned 0 stores ({len(resp.data or b'')} bytes; preview={preview!r})"
        except Exception as err:
            last_doh_err = f"DoH IP {ip} error: {err}"
            with _doh_lock:
                bad_pool = _doh_pools.pop(ip, None)
                if bad_pool is not None:
                    try:
                        bad_pool.close()
                    except Exception:
                        pass

    with _doh_lock:
        _doh_ip_cache['expires_at'] = 0.0
    return [], last_doh_err


def _fetch_ag_official_fallback() -> list:
    """Official fallback scraping live headcounts for all 8 ag stores directly from https://ag-ree.jp/."""
    try:
        resp = _get_http_session('ag').get(AG_OFFICIAL_URL, headers=_HEADERS, timeout=(4, 8))
        resp.raise_for_status()
        soup = BeautifulSoup(resp.content, _BS_PARSER)
        ag_stores = []
        seen_names = set()
        for card in soup.select('a.top_restaurant'):
            ttl_node = card.select_one('.top_restaurant_ttl2')
            if not ttl_node:
                continue
            city = ttl_node.get_text(strip=True).replace('店', '').strip()
            if not city:
                continue
            spans = card.select('.customers_num span')
            if len(spans) < 2:
                continue
            men_cnt = _parse_int_count(spans[0])
            women_cnt = _parse_int_count(spans[1])
            canon_name = f"OLG ag{city}"
            if canon_name in seen_names:
                continue
            seen_names.add(canon_name)
            ag_stores.append({
                'name': canon_name,
                'men': men_cnt,
                'women': women_cnt,
                'source': 'oriental',
            })
        return ag_stores
    except Exception as err:
        logging.warning(f"ag-ree.jp fallback failed: {err}")
        return []


def get_oriental_data():
    global _olg_cache
    _olg_cache['served_stale'] = False
    is_mocked = getattr(requests.get, '__module__', '') != 'requests.api'
    last_err = None

    for attempt in range(2):
        try:
            if attempt > 0 and not is_mocked:
                _reset_http_session('oriental')
                time.sleep(0.4)
            response = _get_http_session('oriental').get(ORIENTAL_URL, timeout=(4, 10), headers=_ORIENTAL_HEADERS)
            response.raise_for_status()
            store_data = _extract_oriental_stores_from_html(response.content)
            if store_data:
                _olg_cache['data'] = store_data
                _olg_cache['last_success'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
                return store_data
            preview = (response.content or b'')[:120].decode('utf-8', errors='replace').strip()
            last_err = f"no store elements found ({len(response.content)} bytes; preview={preview!r})"
        except Exception as e:
            last_err = str(e)

    # Fallback 1: Bypass stale local VPC NS cache via RFC 8484 DoH + verified TLS SNI pool
    if not is_mocked:
        doh_stores, doh_err = _fetch_oriental_via_doh()
        if doh_stores:
            _olg_cache['data'] = doh_stores
            _olg_cache['last_success'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return doh_stores
        last_err = f"{last_err} | DoH: {doh_err}"

        # Fallback 2: Official ag-ree.jp live counts for the 8 ag stores if oriental-lounge.com itself is in maintenance
        ag_live = _fetch_ag_official_fallback()
        if ag_live:
            ag_by_name = {s['name']: s for s in ag_live}
            if _olg_cache['data']:
                merged = [ag_by_name.get(s['name'], s) for s in _olg_cache['data']]
                existing_names = {s['name'] for s in merged}
                for ag_s in ag_live:
                    if ag_s['name'] not in existing_names:
                        merged.append(ag_s)
                _olg_cache['served_stale'] = True
                _olg_cache['stale_reason'] = f"{last_err} (ag live={len(ag_live)})"
                return merged
            _olg_cache['served_stale'] = True
            _olg_cache['stale_reason'] = f"{last_err} (ag-only={len(ag_live)})"
            return ag_live

    logging.error(f"Error fetching Oriental data: {last_err}")
    if _olg_cache['data']:
        logging.error(f"Using cached OLG data from {_olg_cache['last_success']}")
        _olg_cache['served_stale'] = True
        _olg_cache['stale_reason'] = str(last_err)
        return _olg_cache['data']
    return []


def get_jis_data():
    try:
        response = _get_http_session('jis').get(JIS_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
    except requests.RequestException as e:
        logging.error(f"Error fetching JIS data: {e}")
        return []

    raw_text = response.text or ''
    match = _JIS_DATAS_RE.search(raw_text)
    if not match:
        soup = BeautifulSoup(response.content, _BS_PARSER)
        for script in soup.find_all('script'):
            if script.string and 'var datas =' in script.string:
                match = _JIS_DATAS_RE.search(script.string)
                if match:
                    break

    if not match:
        logging.error("Could not find JIS data script")
        return []

    store_data = []
    name_map = {
        'sapporo_b1': 'SAPPORO',
        'omiya': 'OMIYA',
        'shinjuku': 'SHINJUKU',
        'nishishinjuku': 'NISHISHINJUKU',
        'umeda': 'UMEDA',
        'namba': 'NAMBA',
        'chayamachi': 'CHAYAMACHI',
        'fukuoka': 'FUKUOKA',
        'kumamoto': 'KUMAMOTO',
        'matsuyama': 'MATSUYAMA'
    }
    try:
        data = json.loads(match.group(1))
        for store_key, store_info in data.items():
            if 'shared' in store_info:
                men_count = store_info['shared'].get('mens_customer_num', 0)
                women_count = store_info['shared'].get('ladys_customer_num', 0)
                name = name_map.get(store_key, store_key.upper())
                store_data.append({
                    'name': f"JIS {name}",
                    'men': men_count,
                    'women': women_count,
                    'source': 'jis'
                })
    except json.JSONDecodeError as e:
        logging.error(f"Error parsing JIS JSON: {e}")

    return store_data

def get_xix_data():
    try:
        response = _get_http_session('xix').get(XIX_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
        data = response.json()
        
        if data and len(data) > 0:
            info = data[0]
            men_count = int(info.get('m_cnt', 0))
            women_count = int(info.get('w_cnt', 0))
            return [{
                'name': "XIX OKAYAMA",
                'men': men_count,
                'women': women_count,
                'source': 'xix'
            }]
    except Exception as e:
        logging.error(f"Error fetching XIX data: {e}")
    return []

def get_alfa_data():
    try:
        response = _get_http_session('alfa').get(ALFA_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            data = json.loads(response.content.decode('utf-8-sig'))
        
        men_count = int(data.get('man_num', 0))
        women_count = int(data.get('woman_num', 0))
        
        return [{
            'name': "ALFA HIROSHIMA",
            'men': men_count,
            'women': women_count,
            'source': 'alfa'
        }]
    except Exception as e:
        logging.error(f"Error fetching ALFA data: {e}")
    return []

def get_yatakoi_data(now_jst=None):
    try:
        response = _get_http_session('yatakoi').get(YATAKOI_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
        data = response.json()
        
        if data:
            # Yatakoi's Firebase node (asobibar-823d1/shops/chayamachi) is manually updated and frequently
            # leaves stale counts frozen after official closing time (02:00 Sun-Thu / 03:00 Fri-Sat).
            # Zero out counts when outside official business hours so ghost counts never pollute late-night rankings.
            try:
                import store_hours
                h_status = store_hours.get_business_hours_status("YATAKOI UMEDA", now_jst=now_jst, total_guests=0)
                if not h_status.get("scheduled_open"):
                    return [{
                        'name': "YATAKOI UMEDA",
                        'men': 0,
                        'women': 0,
                        'source': 'yatakoi'
                    }]
            except Exception:
                pass

            men = int(data.get('males', 0))
            women = int(data.get('females', 0))
            yk_men = int(data.get('ykMales', 0))
            yk_women = int(data.get('ykFemales', 0))
            
            return [{
                'name': "YATAKOI UMEDA",
                'men': men + yk_men,
                'women': women + yk_women,
                'source': 'yatakoi'
            }]
    except Exception as e:
        logging.error(f"Error fetching Yatakoi data: {e}")
    return []

def get_clovers_data():
    """Scrape 相席CLOVERS (Hiroshima) from bar-clovers.com"""
    try:
        response = _get_http_session('clovers').get("https://bar-clovers.com/", headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
    except Exception as e:
        logging.error(f"Error fetching CLOVERS data: {e}")
        return []

    soup = BeautifulSoup(response.content, _BS_PARSER)
    
    visit_table = soup.select_one('table.visit')
    if not visit_table:
        logging.error("Could not find visit table on CLOVERS site")
        return []
    
    try:
        rows = visit_table.select('tr')
        if len(rows) >= 2:
            cells = rows[1].select('td')
            if len(cells) >= 2:
                men_count = int(cells[0].get_text(strip=True))
                women_count = int(cells[1].get_text(strip=True))
                return [{
                    'name': 'CLOVERS HIROSHIMA',
                    'men': men_count,
                    'women': women_count,
                    'source': 'clovers'
                }]
    except (ValueError, IndexError) as e:
        logging.error(f"Error parsing CLOVERS data: {e}")
    
    return []

def _run_with_timeout(fn, timeout=30):
    """Run fn in a daemon thread with a hard timeout. Returns result or raises."""
    result_holder = [None]
    error_holder = [None]
    
    def wrapper():
        try:
            result_holder[0] = fn()
        except Exception as e:
            error_holder[0] = e
    
    t = threading.Thread(target=wrapper, daemon=True)
    t.start()
    t.join(timeout=timeout)
    
    if t.is_alive():
        # Thread is still running - abandon it (daemon thread will die with process)
        raise TimeoutError(f"Exceeded {timeout}s hard timeout")
    
    if error_holder[0]:
        raise error_holder[0]
    
    return result_holder[0]

def get_all_data():
    fetchers = [
        get_oriental_data,
        get_jis_data,
        get_xix_data,
        get_alfa_data,
        get_yatakoi_data,
        get_clovers_data,
    ]

    results_by_name = {}
    local_errors = {}
    lock = threading.Lock()

    def run_fetcher(fn):
        name = fn.__name__
        try:
            res = fn()
            with lock:
                if res:
                    results_by_name[name] = res
                    logging.info(f"  ✓ {name}: {len(res)} stores")
                else:
                    local_errors[name] = "Returned empty"
                    logging.error(f"  ✗ {name}: returned empty")
        except Exception as e:
            with lock:
                local_errors[name] = str(e)
            logging.error(f"  ✗ {name}: {e}")

    # Submit all 6 scrapers to reusable pool (zero per-minute OS thread churn)
    future_map = {_FETCH_EXECUTOR.submit(run_fetcher, fn): fn.__name__ for fn in fetchers}
    done, not_done = futures_wait(future_map.keys(), timeout=18.0)
    if not_done:
        for fut in not_done:
            fname = future_map[fut]
            with lock:
                local_errors[fname] = "Hard timeout (>18s)"
            logging.error(f"  ✗ {fname}: parallel timeout exceeded 18s!")

    data = []
    for fn in fetchers:
        res = results_by_name.get(fn.__name__)
        if res:
            data.extend(res)

    if _olg_cache.get('served_stale'):
        local_errors['get_oriental_data'] = f"stale cache: {_olg_cache.get('stale_reason')}"

    with _fetch_errors_lock:
        fetch_errors.clear()
        fetch_errors.update(local_errors)

    # Health monitoring: treat stale-cache fallback as a failure so outages are detected
    try:
        import health
        health_results = dict(results_by_name)
        if _olg_cache.get('served_stale'):
            health_results.pop('get_oriental_data', None)
        health.record_fetch(health_results, dict(local_errors), [fn.__name__ for fn in fetchers])
        health.check_unknown_stores([s.get('name') for s in data])
    except Exception as e:
        logging.error(f"health check failed: {e}")

    return data



def find_store_with_max_women(data):
    if not data:
        return None
    sorted_data = sorted(data, key=lambda x: x['women'], reverse=True)
    return sorted_data[0]
