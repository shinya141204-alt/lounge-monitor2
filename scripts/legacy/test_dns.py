import socket
import time

urls = [
    "oriental-lounge.com",
    "jis.bar",
    "aiseki-okayama.conohawing.com",
    "aiseki-hiroshima.com",
    "asobibar-823d1.firebaseio.com"
]

for url in urls:
    start = time.time()
    try:
        ip = socket.gethostbyname(url)
        print(f"{url}: {ip} (took {time.time()-start:.2f}s)")
    except Exception as e:
        print(f"{url}: ERROR {e} (took {time.time()-start:.2f}s)")
