const CACHE_NAME = 'aiseki-navi-v36';
const SHELL_KEY = '/';
const ASSETS_TO_CACHE = [
  '/static/style.css?v=20261023_13',
  '/static/calm.css?v=5',
  '/static/app.js?v=20261025_1',
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
function fetchAndCache(event, cacheKey) {
  return (async () => {
    const preload = await event.preloadResponse;
    const resp = preload || await fetch(event.request);
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
    // Top page (PWA start_url "/?v=pwa"): App-shell "stale-while-revalidate".
    // Show the cached page instantly, update it in the background.
    // app.js then fetches /api/status immediately when the embedded data is old.
    if (reqUrl.pathname === '/') {
      const networkPromise = fetchAndCache(event, SHELL_KEY);
      event.waitUntil(networkPromise.catch(() => {}));
      event.respondWith(
        (async () => {
          const cached = await caches.match(SHELL_KEY);
          if (cached) return cached;
          return networkPromise;
        })()
      );
      return;
    }

    // Other pages: network-first, but fall back to cache if the network is slow (>4s) or offline
    const networkPromise = fetchAndCache(event, reqUrl.pathname);
    event.waitUntil(networkPromise.catch(() => {}));
    event.respondWith(
      (async () => {
        try {
          return await Promise.race([networkPromise, timeout(4000)]);
        } catch (e) {
          const cached = await caches.match(reqUrl.pathname);
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
