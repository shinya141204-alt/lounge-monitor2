import requests
from bs4 import BeautifulSoup
import time
from datetime import datetime
import sys


# URLs to monitor
ORIENTAL_URL = "https://oriental-lounge.com/"
JIS_URL = "https://jis.bar/"
XIX_URL = "https://aiseki-okayama.conohawing.com/aiseki/parts/get_cs_info.php"
ALFA_URL = "https://aiseki-hiroshima.com/wp/display.php"
YATAKOI_URL = "https://asobibar-823d1.firebaseio.com/shops/chayamachi.json"

def get_oriental_data():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}
        response = requests.get(ORIENTAL_URL, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching Oriental data: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    # Stores are in 'a' tags with class 'card' and 'wave-anime-wrap'
    stores = soup.select('a.card.wave-anime-wrap')
    
    store_data = []
    
    for store in stores:
        try:
            name_tag = store.select_one('h4')
            if not name_tag:
                continue
            name = name_tag.get_text(strip=True)
            
            # Men count
            men_tag = store.select_one('.num-male')
            men_count = int(men_tag.get_text(strip=True)) if men_tag else 0
            
            # Women count
            women_tag = store.select_one('.num-female')
            women_count = int(women_tag.get_text(strip=True)) if women_tag else 0
            
            store_data.append({
                'name': f"Oriental {name}",
                'men': men_count,
                'women': women_count,
                'source': 'oriental'
            })
        except ValueError:
            continue
            
    return store_data


def get_jis_data():
    try:
        # JIS usually requires User-Agent
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}
        response = requests.get(JIS_URL, headers=headers, timeout=10)
        response.raise_for_status()
    except requests.RequestException as e:
        print(f"Error fetching JIS data: {e}", file=sys.stderr)
        return []

    soup = BeautifulSoup(response.content, 'html.parser')
    
    # JIS data is in a script tag with "var datas ="
    scripts = soup.find_all('script')
    target_script = None
    for script in scripts:
        if script.string and 'var datas =' in script.string:
            target_script = script.string
            break
            
    if not target_script:
        print("Could not find JIS data script", file=sys.stderr)
        return []

    store_data = []
    
    # Extract JSON string: var datas = { ... };
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
                    
                    # Map store keys to readable names if possible, or uppercase key
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
            print(f"Error parsing JIS JSON: {e}", file=sys.stderr)
            
    return store_data

def get_xix_data():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}
        response = requests.get(XIX_URL, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        # XIX API returns a list of objects, we take the first one
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
        print(f"Error fetching XIX data: {e}", file=sys.stderr)
    return []

def get_alfa_data():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}
        response = requests.get(ALFA_URL, headers=headers, timeout=10)
        response.raise_for_status()
        # Handle UTF-8 BOM if present
        try:
            data = response.json()
        except requests.exceptions.JSONDecodeError:
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
        print(f"Error fetching ALFA data: {e}", file=sys.stderr)
    return []

def get_yatakoi_data():
    try:
        headers = {'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.114 Safari/537.36'}
        response = requests.get(YATAKOI_URL, headers=headers, timeout=10)
        response.raise_for_status()
        data = response.json()
        
        if data:
            men = int(data.get('males', 0))
            women = int(data.get('females', 0))
            # Some stores might have extra 'ykMales'/'ykFemales' to add
            yk_men = int(data.get('ykMales', 0))
            yk_women = int(data.get('ykFemales', 0))
            
            return [{
                'name': "YATAKOI UMEDA",
                'men': men + yk_men,
                'women': women + yk_women,
                'source': 'yatakoi'
            }]
    except Exception as e:
        print(f"Error fetching Yatakoi data: {e}", file=sys.stderr)
    return []

def get_all_data():
    data = []
    # Add existing
    data.extend(get_oriental_data())
    data.extend(get_jis_data())
    
    # Add new sources
    data.extend(get_xix_data())
    data.extend(get_alfa_data())
    data.extend(get_yatakoi_data())
    
    return data

def find_store_with_max_women(data):
    if not data:
        return None
    
    # Sort by women count descending
    sorted_data = sorted(data, key=lambda x: x['women'], reverse=True)
    return sorted_data[0]

def main():
    print("Starting Oriental Lounge Monitor (Interval: 5 minutes)")
    print("Press Ctrl+C to stop.")
    print("-" * 50)
    
    try:
        while True:
            timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            data = get_all_data()
            
            if data:
                top_store = find_store_with_max_women(data)
                
                if top_store:
                    print(f"[{timestamp}] Most Ladies: {top_store['name']}")
                    print(f"  Ladies: {top_store['women']} / Gentlemen: {top_store['men']}")
                else:
                    print(f"[{timestamp}] No store data found.")
            else:
                 print(f"[{timestamp}] Failed to retrieve data.")
            
            print("-" * 50)
            # Sleep for 5 minutes (300 seconds)
            time.sleep(300)
            
    except KeyboardInterrupt:
        print("\nStopping monitor...")

if __name__ == "__main__":
    main()
