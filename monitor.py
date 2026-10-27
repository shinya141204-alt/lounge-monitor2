import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime
import sys
import threading
import socket
import requests.packages.urllib3.util.connection as urllib3_cn

# Force IPv4 to prevent hanging on broken IPv6 networks (common on Render free instances)
def allowed_gai_family():
    return socket.AF_INET
urllib3_cn.allowed_gai_family = allowed_gai_family

# Global error log for debugging Render timeouts
fetch_errors = {}

# URLs to monitor
ORIENTAL_URL = "https://oriental-lounge.com/"
JIS_URL = "https://jis.bar/"
XIX_URL = "https://aiseki-okayama.conohawing.com/aiseki/parts/get_cs_info.php"
ALFA_URL = "https://aiseki-hiroshima.com/wp/display.php"
YATAKOI_URL = "https://asobibar-823d1.firebaseio.com/shops/chayamachi.json"

_HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,ja-JP;q=0.9',
}

# Cache for OLG data (fallback when connection fails)
_olg_cache = {
    'data': [],
    'last_success': None
}

_http_tls = threading.local()


def _get_http_session():
    """Returns a thread-local requests.Session for HTTP Keep-Alive, falling back to `requests` when patched in tests."""
    if getattr(requests.get, '__module__', '') != 'requests.api':
        return requests
    session = getattr(_http_tls, 'session', None)
    if session is None:
        session = requests.Session()
        _http_tls.session = session
    return session


def get_oriental_data():
    global _olg_cache
    _olg_cache['served_stale'] = False
    try:
        response = _get_http_session().get(ORIENTAL_URL, timeout=(3, 7), headers=_HEADERS)
        response.raise_for_status()
    except Exception as e:
        import logging; logging.error(f"Error fetching Oriental data: {e}")
        if _olg_cache['data']:
            import logging; logging.error(f"Using cached OLG data from {_olg_cache['last_success']}")
            _olg_cache['served_stale'] = True
            _olg_cache['stale_reason'] = str(e)
            return _olg_cache['data']
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    stores = soup.select('a.card.wave-anime-wrap')
    
    if not stores:
        import logging; logging.error(f"WARNING: OLG page returned {len(response.content)} bytes but no store elements found")
        if _olg_cache['data']:
            _olg_cache['served_stale'] = True
            _olg_cache['stale_reason'] = 'no store elements found (HTML structure changed?)'
            return _olg_cache['data']
        return []
    
    store_data = []
    for store in stores:
        try:
            name_tag = store.select_one('h4')
            if not name_tag:
                continue
            name = name_tag.get_text(strip=True)
            
            men_tag = store.select_one('.num-male')
            men_count = int(men_tag.get_text(strip=True)) if men_tag else 0
            
            women_tag = store.select_one('.num-female')
            women_count = int(women_tag.get_text(strip=True)) if women_tag else 0
            
            store_data.append({
                'name': f"OLG {name}",
                'men': men_count,
                'women': women_count,
                'source': 'oriental'
            })
        except ValueError:
            continue
    
    if store_data:
        _olg_cache['data'] = store_data
        _olg_cache['last_success'] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        return store_data
    
    if _olg_cache['data']:
        return _olg_cache['data']
    return []


def get_jis_data():
    try:
        response = _get_http_session().get(JIS_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
    except requests.RequestException as e:
        import logging; logging.error(f"Error fetching JIS data: {e}")
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    scripts = soup.find_all('script')
    target_script = None
    for script in scripts:
        if script.string and 'var datas =' in script.string:
            target_script = script.string
            break
            
    if not target_script:
        import logging; logging.error("Could not find JIS data script")
        return []

    store_data = []
    
    import re
    import json
    
    match = re.search(r'var datas\s*=\s*({.*?});', target_script, re.DOTALL)
    if match:
        json_str = match.group(1)
        try:
            data = json.loads(json_str)
            for store_key, store_info in data.items():
                if 'shared' in store_info:
                    men_count = store_info['shared'].get('mens_customer_num', 0)
                    women_count = store_info['shared'].get('ladys_customer_num', 0)
                    
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
                    name = name_map.get(store_key, store_key.upper())
                    
                    store_data.append({
                        'name': f"JIS {name}",
                        'men': men_count,
                        'women': women_count,
                        'source': 'jis'
                    })
        except json.JSONDecodeError as e:
            import logging; logging.error(f"Error parsing JIS JSON: {e}")
            
    return store_data

def get_xix_data():
    try:
        response = _get_http_session().get(XIX_URL, headers=_HEADERS, timeout=(3, 7))
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
        import logging; logging.error(f"Error fetching XIX data: {e}")
    return []

def get_alfa_data():
    try:
        response = _get_http_session().get(ALFA_URL, headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
        try:
            data = response.json()
        except Exception:
            import json
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
        import logging; logging.error(f"Error fetching ALFA data: {e}")
    return []

def get_yatakoi_data(now_jst=None):
    try:
        response = _get_http_session().get(YATAKOI_URL, headers=_HEADERS, timeout=(3, 7))
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
        import logging; logging.error(f"Error fetching Yatakoi data: {e}")
    return []

def get_clovers_data():
    """Scrape 相席CLOVERS (Hiroshima) from bar-clovers.com"""
    try:
        response = _get_http_session().get("https://bar-clovers.com/", headers=_HEADERS, timeout=(3, 7))
        response.raise_for_status()
    except Exception as e:
        import logging; logging.error(f"Error fetching CLOVERS data: {e}")
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    visit_table = soup.select_one('table.visit')
    if not visit_table:
        import logging; logging.error("Could not find visit table on CLOVERS site")
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
        import logging; logging.error(f"Error parsing CLOVERS data: {e}")
    
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
    
    global fetch_errors
    fetch_errors.clear()
    
    results_by_name = {}
    threads = []
    lock = threading.Lock()

    def run_fetcher(fn):
        name = fn.__name__
        try:
            res = fn()
            with lock:
                if res:
                    results_by_name[name] = res
                    import logging; logging.info(f"  ✓ {name}: {len(res)} stores")
                else:
                    fetch_errors[name] = "Returned empty"
                    import logging; logging.error(f"  ✗ {name}: returned empty")
        except Exception as e:
            with lock:
                fetch_errors[name] = str(e)
            import logging; logging.error(f"  ✗ {name}: {e}")

    # Launch all 6 scrapers in parallel
    for fn in fetchers:
        t = threading.Thread(target=run_fetcher, args=(fn,), daemon=True)
        t.start()
        threads.append((fn.__name__, t))

    # Wait up to 12 seconds total for all parallel threads
    deadline = time.time() + 12.0
    for name, t in threads:
        remaining = max(0.1, deadline - time.time())
        t.join(timeout=remaining)
        if t.is_alive():
            with lock:
                fetch_errors[name] = "Hard timeout (>12s)"
            import logging; logging.error(f"  ✗ {name}: parallel timeout exceeded 12s!")

    data = []
    for fn in fetchers:
        res = results_by_name.get(fn.__name__)
        if res:
            data.extend(res)

    # Health monitoring: treat stale-cache fallback as a failure so outages are detected
    try:
        import health
        health_results = dict(results_by_name)
        if _olg_cache.get('served_stale'):
            health_results.pop('get_oriental_data', None)
            fetch_errors['get_oriental_data'] = f"stale cache: {_olg_cache.get('stale_reason')}"
        health.record_fetch(health_results, dict(fetch_errors), [fn.__name__ for fn in fetchers])
        health.check_unknown_stores([s.get('name') for s in data])
    except Exception as e:
        import logging; logging.error(f"health check failed: {e}")

    return data



def find_store_with_max_women(data):
    if not data:
        return None
    sorted_data = sorted(data, key=lambda x: x['women'], reverse=True)
    return sorted_data[0]
