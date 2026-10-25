const CACHE_NAME = 'aiseki-navi-v40';
const SHELL_KEY = '/';
const FAST_NAV_PATHS = new Set([
  '/guide/brand-comparison',
  '/columns',
  '/grooming',
  '/stats',
  '/guide'
]);
const LIVE_SSR_PREFIXES = [
  '/price-comparison',
  '/store/',
  '/areas',
  '/area/',
  '/oriental-lounge',
  '/ag',
  '/jis'
];
const ASSETS_TO_CACHE = [
  '/static/style.css?v=20261023_13',
  '/static/calm.css?v=7',
  '/static/portal.css?v=6',
  '/static/app.js?v=20261026_4',
  '/static/affiliate_config.js?v=8',
  '/static/manifest.json?v=2',
  '/static/icon-192.png?v=2',
  '/static/icon-512.png?v=2',
  'https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap'
];

// Install: Cache critical shell assets (+ the top page HTML as the app shell)
self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then(async (cache) => {
      await cache.addAll(ASSETS_TO_CACHE);
      try {
        const shell = await fetch(SHELL_KEY, { cache: 'no-store' });
        if (shell && shell.ok) await cache.put(SHELL_KEY, shell);
      } catch (e) {}
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

// Fetch the latest HTML (navigation preload first) and store it in the cache
function fetchAndCache(event, cacheKey, forceFresh) {
  return (async () => {
    const preload = forceFresh ? null : await event.preloadResponse;
    const resp = preload || await fetch(event.request, forceFresh ? { cache: 'no-store' } : undefined);
    if (resp && resp.ok) {
      const copy = resp.clone();
      const cache = await caches.open(CACHE_NAME);
      await cache.put(cacheKey, copy);
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

  // Always bypass Service Worker for staff monitor, auth routes, and ads.txt
  if (reqUrl.pathname.startsWith('/staff') || reqUrl.pathname === '/ads.txt') {
    return;
  }

  // Always fetch fresh data for API calls (never cache live rankings)
  if (event.request.url.includes('/api/')) {
    event.respondWith(
      fetch(event.request).catch(() => new Response(JSON.stringify({ error: "Offline" }), {
        headers: { 'Content-Type': 'application/json' }
      }))
    );
    return;
  }

  // Navigation requests (HTML page visits from bookmarks / home screen)
  if (event.request.mode === 'navigate') {
    // Top page ("/") and primary global navigation tabs: Stale-While-Revalidate
    // Serve cached HTML immediately (<10ms) if available, and update cache in the background.
    const cacheKey = reqUrl.pathname === '/' ? SHELL_KEY : (reqUrl.pathname + reqUrl.search);
    if (reqUrl.pathname === '/' || FAST_NAV_PATHS.has(reqUrl.pathname)) {
      const networkPromise = fetchAndCache(event, cacheKey);
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

    // Other pages (including live SSR pages like /price-comparison, /store/*, /areas):
    // always fetch fresh network HTML first (with offline fallback)
    const isLiveSsr = LIVE_SSR_PREFIXES.some((p) => reqUrl.pathname === p || reqUrl.pathname.startsWith(p));
    const pageCacheKey = reqUrl.pathname + reqUrl.search;
    const networkPromise = fetchAndCache(event, pageCacheKey, isLiveSsr);
    event.waitUntil(networkPromise.catch(() => {}));
    event.respondWith(
      (async () => {
        try {
          return await Promise.race([networkPromise, timeout(isLiveSsr ? 3500 : 1500)]);
        } catch (e) {
          const cached = await caches.match(pageCacheKey) || await caches.match(reqUrl.pathname);
          if (cached) return cached;
          try {
            return await networkPromise;
          } catch (e2) {
            const fallback = await caches.match(SHELL_KEY);
            if (fallback) return fallback;
            throw e2;
          }
        }
      })()
    );
    return;
  }

  // Stale-while-revalidate for static assets
  event.respondWith(
    caches.match(event.request).then((cachedResponse) => {
      const fetchPromise = fetch(event.request).then((networkResponse) => {
        if (networkResponse && networkResponse.status === 200) {
          const copy = networkResponse.clone();
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, copy));
        }
        return networkResponse;
      }).catch(() => null);

      return cachedResponse || fetchPromise;
    })
  );
});
