// Supersedes aiseki-navi-v47..v62 (purged automatically on activate)
const CACHE_NAME = 'aiseki-navi-v63';
const SHELL_KEY = '/';
const LIVE_FRESH_TTL_MS = 45000;
const PREFETCH_DEDUP_MS = 25000;
const FAST_NAV_PATHS = new Set([
  '/grooming',
  '/stats',
  '/guide',
  '/columns',
  '/privacy'
]);
const LIVE_SSR_EXACT = new Set([
  '/',
  '/for-women',
  '/areas',
  '/oriental-lounge',
  '/ag',
  '/jis'
]);
const LIVE_SSR_PREFIXES = [
  '/price-comparison',
  '/store/',
  '/area/'
];
const CORE_ASSETS = [
  '/static/style.css?v=20261029_1',
  '/static/calm.css?v=18',
  '/static/portal.css?v=7',
  '/static/app.js?v=20261029_2',
  '/static/affiliate_config.js?v=12',
  '/static/manifest.json?v=2'
];
const OPTIONAL_EXTERNAL_ASSETS = [
  'https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap'
];

function isStaffOrPrivateNoStore(resp) {
  if (!resp || !resp.headers) return false;
  if (resp.headers.get('X-Staff-View') === '1') return true;
  if (resp.headers.get('X-Robots-Tag') === 'noindex, nofollow') return true;
  const cc = (resp.headers.get('Cache-Control') || '').toLowerCase();
  return cc.includes('private') && cc.includes('no-store');
}

function isFastEditorialPath(pathname) {
  return FAST_NAV_PATHS.has(pathname) || pathname.startsWith('/guide/');
}

function isLiveSsrPath(pathname) {
  return LIVE_SSR_EXACT.has(pathname) || LIVE_SSR_PREFIXES.some((p) => pathname === p || pathname.startsWith(p));
}

// Install: Cache critical CSS/JS assets only (avoid competing for bandwidth with large icons or duplicate HTML fetch)
self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      await cache.addAll(CORE_ASSETS);
      for (const extUrl of OPTIONAL_EXTERNAL_ASSETS) {
        try {
          await cache.add(extUrl);
        } catch (e) {}
      }
    })
  );
});

// Activate: Immediately claim clients and purge old obsolete caches
self.addEventListener('activate', (event) => {
  event.waitUntil(
    Promise.all([
      self.clients.claim(),
      // Enable navigation preload if supported to eliminate Service Worker cold-start delay
      'navigationPreload' in self.registration ? self.registration.navigationPreload.enable() : Promise.resolve(),
      caches.keys().then((keys) => {
        return Promise.all(
          keys.map((key) => {
            if (key !== CACHE_NAME) {
              console.log('[SW] Deleting old cache:', key);
              return caches.delete(key);
            }
          })
        );
      })
    ])
  );
});

function getCachedAgeMs(resp) {
  if (!resp || !resp.headers) return Infinity;
  const ts = Number(resp.headers.get('X-Sw-Stored-At') || 0);
  if (!ts || Number.isNaN(ts)) return Infinity;
  return Math.max(0, Date.now() - ts);
}

async function putHtmlWithTimestamp(cacheKey, resp) {
  if (!resp || !resp.ok || resp.redirected || isStaffOrPrivateNoStore(resp)) return;
  try {
    const body = await resp.clone().arrayBuffer();
    const headers = new Headers(resp.headers);
    headers.set('X-Sw-Stored-At', String(Date.now()));
    const stamped = new Response(body, {
      status: resp.status,
      statusText: resp.statusText,
      headers
    });
    const cache = await caches.open(CACHE_NAME);
    await cache.put(cacheKey, stamped);
  } catch (e) {}
}

// Fetch the latest HTML (navigation preload first) and write to CacheStorage asynchronously without blocking UI response
function fetchAndCache(event, cacheKey, forceFresh) {
  return (async () => {
    let preload = null;
    try {
      preload = event.preloadResponse ? await event.preloadResponse : null;
    } catch (e) {}
    const resp = preload || await fetch(event.request, forceFresh ? { cache: 'no-cache' } : undefined);
    if (resp && resp.ok && !resp.redirected && !isStaffOrPrivateNoStore(resp)) {
      const copy = resp.clone();
      event.waitUntil(putHtmlWithTimestamp(cacheKey, copy));
    }
    return resp;
  })();
}

function timeout(ms) {
  return new Promise((_, reject) => setTimeout(() => reject(new Error('timeout')), ms));
}

self.addEventListener('fetch', (event) => {
  if (event.request.method !== 'GET') {
    return;
  }

  // Bypass cross-origin requests (e.g. Google AdSense) except Google Fonts
  const reqUrl = new URL(event.request.url);
  if (reqUrl.origin !== self.location.origin && !reqUrl.hostname.includes('fonts.g')) {
    return;
  }

  // Always bypass Service Worker for staff monitor, dynamic OGP/carousel images, and ads.txt
  if (reqUrl.pathname.startsWith('/staff') || reqUrl.pathname.startsWith('/ogp/') || reqUrl.pathname === '/ads.txt') {
    return;
  }

  // Always fetch fresh data for API calls (never cache live rankings)
  if (reqUrl.pathname.startsWith('/api/')) {
    event.respondWith(
      fetch(event.request).catch(() => new Response(JSON.stringify({ error: "Offline" }), {
        headers: { 'Content-Type': 'application/json' }
      }))
    );
    return;
  }

  // Background warm-prefetch from touchstart/pointerdown/idle (works on iOS Safari & Android Chrome)
  if (event.request.headers.get('X-Sw-Prefetch') === '1') {
    const pfKey = reqUrl.pathname === '/' ? SHELL_KEY : (reqUrl.pathname + reqUrl.search);
    event.respondWith(
      (async () => {
        try {
          const existing = await caches.match(pfKey);
          if (existing && (isFastEditorialPath(reqUrl.pathname) || getCachedAgeMs(existing) < PREFETCH_DEDUP_MS)) {
            return new Response('', { status: 204 });
          }
          const resp = await fetch(reqUrl.pathname + reqUrl.search, {
            cache: 'no-cache',
            credentials: 'same-origin',
            headers: { 'X-Sw-Prefetch': '1' }
          });
          await putHtmlWithTimestamp(pfKey, resp);
        } catch (e) {}
        return new Response('', { status: 204 });
      })()
    );
    return;
  }

  // Navigation requests (HTML page visits from bookmarks / home screen / in-site tabs)
  if (event.request.mode === 'navigate') {
    const cacheKey = reqUrl.pathname === '/' ? SHELL_KEY : (reqUrl.pathname + reqUrl.search);
    // Static guide & editorial pages: Stale-While-Revalidate
    if (isFastEditorialPath(reqUrl.pathname)) {
      const networkPromise = fetchAndCache(event, cacheKey, false);
      event.waitUntil(networkPromise.catch(() => {}));
      event.respondWith(
        (async () => {
          const cached = await caches.match(cacheKey) || await caches.match(reqUrl.pathname);
          if (cached) return cached;
          return networkPromise;
        })()
      );
      return;
    }

    // Top page ("/") and live SSR pages (/for-women, /price-comparison, /store/*, /areas):
    // If warmed within the last 45s (via touchstart/idle prefetch or recent visit), serve in ~5ms and revalidate in background!
    const isLiveSsr = isLiveSsrPath(reqUrl.pathname);
    const pageCacheKey = cacheKey;
    event.respondWith(
      (async () => {
        const cachedFresh = await caches.match(pageCacheKey) || (reqUrl.pathname === '/' ? null : await caches.match(reqUrl.pathname));
        if (cachedFresh && getCachedAgeMs(cachedFresh) < LIVE_FRESH_TTL_MS) {
          const bgPromise = fetchAndCache(event, pageCacheKey, isLiveSsr);
          event.waitUntil(bgPromise.catch(() => {}));
          return cachedFresh;
        }
        const networkPromise = fetchAndCache(event, pageCacheKey, isLiveSsr);
        event.waitUntil(networkPromise.catch(() => {}));
        try {
          return await Promise.race([networkPromise, timeout(isLiveSsr ? 1800 : 1200)]);
        } catch (e) {
          if (cachedFresh) return cachedFresh;
          try {
            return await networkPromise;
          } catch (e2) {
            if (reqUrl.pathname === '/') {
              const fallback = await caches.match(SHELL_KEY);
              if (fallback) return fallback;
            }
            throw e2;
          }
        }
      })()
    );
    return;
  }

  // Stale-while-revalidate for static assets (guarantee valid Response even when offline and uncached)
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      const fetchPromise = fetch(event.request).then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200 && !isStaffOrPrivateNoStore(networkResponse)) {
          const copy = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy)).catch(() => {});
        }
        return networkResponse;
      });

      if (cachedResponse) {
        fetchPromise.catch(() => {});
        return cachedResponse;
      }
      return fetchPromise.catch(() => Response.error());
    })
  );
});
