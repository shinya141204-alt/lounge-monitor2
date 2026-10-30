// Supersedes aiseki-navi-v47..v69 (purged automatically on activate)
const CACHE_NAME = 'aiseki-navi-v70';
const SHELL_KEY = '/';
const LIVE_FRESH_TTL_MS = 45000;
const PREFETCH_DEDUP_MS = 25000;
const EDITORIAL_MAX_AGE_MS = 1200000;
const MAX_DYNAMIC_CACHE_ENTRIES = 65;

const FAST_NAV_PATHS = new Set([
  '/grooming',
  '/guide',
  '/privacy'
]);
const LIVE_SSR_EXACT = new Set([
  '/',
  '/concierge',
  '/for-women',
  '/areas',
  '/stats',
  '/columns',
  '/oriental-lounge',
  '/ag',
  '/jis',
  '/asobibar'
]);
const LIVE_SSR_PREFIXES = [
  '/price-comparison',
  '/store/',
  '/area/'
];
const CORE_ASSETS = [
  '/static/style.css?v=20261030_5',
  '/static/calm.css?v=20',
  '/static/portal.css?v=9',
  '/static/app.js?v=20261030_5',
  '/static/affiliate_config.js?v=12',
  '/static/manifest.json?v=2'
];
const CORE_ASSET_SET = new Set(CORE_ASSETS);
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

function isProtectedAssetRequest(req) {
  try {
    const u = new URL(req.url);
    if (u.origin === self.location.origin) {
      return CORE_ASSET_SET.has(u.pathname + u.search);
    }
    return OPTIONAL_EXTERNAL_ASSETS.includes(req.url);
  } catch (e) {
    return false;
  }
}

async function trimDynamicCache(cache) {
  try {
    const keys = await cache.keys();
    const dynamicKeys = keys.filter((req) => !isProtectedAssetRequest(req));
    if (dynamicKeys.length <= MAX_DYNAMIC_CACHE_ENTRIES) return;
    const overflow = dynamicKeys.length - MAX_DYNAMIC_CACHE_ENTRIES;
    for (let i = 0; i < overflow; i++) {
      await cache.delete(dynamicKeys[i]);
    }
  } catch (e) {}
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

// Allow authenticated staff pages to purge cached public HTML (preventing stale ad-bearing pages)
self.addEventListener('message', (event) => {
  if (event.data && event.data.type === 'PURGE_HTML_CACHE') {
    event.waitUntil(
      caches.open(CACHE_NAME).then(async (cache) => {
        const keys = await cache.keys();
        await Promise.all(
          keys.map((req) => {
            try {
              const u = new URL(req.url);
              if (u.origin === self.location.origin && !u.pathname.startsWith('/static/')) {
                return cache.delete(req);
              }
            } catch (e) {}
            return Promise.resolve();
          })
        );
      }).catch(() => {})
    );
  }
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
    await trimDynamicCache(cache);
  } catch (e) {}
}

// Fetch the latest HTML (navigation preload first) and write to CacheStorage asynchronously without blocking UI response
function fetchAndCache(event, cacheKey, forceFresh, skipStore) {
  return (async () => {
    let preload = null;
    try {
      preload = event.preloadResponse ? await event.preloadResponse : null;
    } catch (e) {}
    const resp = preload || await fetch(event.request, forceFresh ? { cache: 'no-cache' } : undefined);
    if (!skipStore && resp && resp.ok && !resp.redirected && !isStaffOrPrivateNoStore(resp)) {
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

  // Always bypass Service Worker for staff monitor, affiliate redirects, dynamic OGP/carousel images, and crawler metadata files
  if (
    reqUrl.pathname.startsWith('/staff') ||
    reqUrl.pathname === '/link' ||
    reqUrl.pathname.startsWith('/link/') ||
    reqUrl.pathname.startsWith('/ogp/') ||
    reqUrl.pathname === '/ads.txt' ||
    reqUrl.pathname === '/robots.txt' ||
    reqUrl.pathname === '/sitemap.xml' ||
    reqUrl.pathname === '/llms.txt' ||
    reqUrl.pathname === '/llms-full.txt'
  ) {
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
    const pfKey = reqUrl.pathname + reqUrl.search;
    event.respondWith(
      (async () => {
        try {
          const existing = await caches.match(pfKey);
          const maxDedupMs = isFastEditorialPath(reqUrl.pathname) ? EDITORIAL_MAX_AGE_MS : PREFETCH_DEDUP_MS;
          if (existing && getCachedAgeMs(existing) < maxDedupMs) {
            return new Response('', { status: 204 });
          }
          const resp = await fetch(pfKey, {
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
    const cacheKey = reqUrl.pathname + reqUrl.search;
    const hasTrackingParam = reqUrl.searchParams.has('s') || reqUrl.searchParams.has('utm_source');

    // Static guide & editorial pages: bounded Stale-While-Revalidate (20 min freshness cap)
    if (isFastEditorialPath(reqUrl.pathname)) {
      const networkPromise = fetchAndCache(event, cacheKey, hasTrackingParam, hasTrackingParam);
      event.waitUntil(networkPromise.catch(() => {}));
      event.respondWith(
        (async () => {
          const cached = await caches.match(cacheKey);
          if (!hasTrackingParam && cached && getCachedAgeMs(cached) < EDITORIAL_MAX_AGE_MS) {
            return cached;
          }
          try {
            return await networkPromise;
          } catch (e) {
            if (cached) return cached;
            const pathFallback = await caches.match(reqUrl.pathname);
            if (pathFallback) return pathFallback;
            throw e;
          }
        })()
      );
      return;
    }

    // Top page ("/") and live SSR pages (/for-women, /price-comparison, /store/*, /areas, /stats, /columns):
    // Match exact URL+query only (never serve default Shinjuku cache for ?area=umeda, nor skip ?s= inflow tracking)
    const isLiveSsr = isLiveSsrPath(reqUrl.pathname);
    const pageCacheKey = cacheKey;
    event.respondWith(
      (async () => {
        const cachedFresh = await caches.match(pageCacheKey);
        if (!hasTrackingParam && cachedFresh && getCachedAgeMs(cachedFresh) < LIVE_FRESH_TTL_MS) {
          const bgPromise = fetchAndCache(event, pageCacheKey, isLiveSsr, false);
          event.waitUntil(bgPromise.catch(() => {}));
          return cachedFresh;
        }
        const networkPromise = fetchAndCache(event, pageCacheKey, isLiveSsr || hasTrackingParam, hasTrackingParam);
        event.waitUntil(networkPromise.catch(() => {}));
        try {
          return await Promise.race([networkPromise, timeout(isLiveSsr ? 1800 : 1200)]);
        } catch (e) {
          if (!hasTrackingParam && cachedFresh) return cachedFresh;
          try {
            return await networkPromise;
          } catch (e2) {
            if (cachedFresh) return cachedFresh;
            const fallback = await caches.match(reqUrl.pathname === '/' ? SHELL_KEY : reqUrl.pathname);
            if (fallback) return fallback;
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
          caches.open(CACHE_NAME).then(async (cache) => {
            await cache.put(event.request, copy);
            await trimDynamicCache(cache);
          }).catch(() => {});
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
