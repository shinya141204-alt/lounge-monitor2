import requests
import time
from bs4 import BeautifulSoup

HEADERS = {
    'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36',
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'ja,ja-JP;q=0.9',
}

start = time.time()
try:
    res = requests.get('https://oriental-lounge.com/', headers=HEADERS, timeout=10)
    print(f"Status: {res.status_code}")
    soup = BeautifulSoup(res.content, 'html.parser')
    stores = soup.select('a.card.wave-anime-wrap')
    print(f"Stores found: {len(stores)}")
except Exception as e:
    print(f"Error: {e}")
print(f"Time: {time.time() - start:.2f}s")
