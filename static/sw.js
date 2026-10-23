const CACHE_NAME = 'aiseki-navi-v33';
const ASSETS_TO_CACHE = [
  '/static/style.css?v=20261023_13',
  '/static/calm.css?v=5',
  '/static/app.js?v=20261023_20',
  '/static/manifest.json?v=2',
  '/static/icon-192.png?v=2',
  '/static/icon-512.png?v=2',
  'https://fonts.googleapis.com/css2?family=Outfit:wght@300;400;600;700&display=swap'
];

// Install: Cache critical shell assets
self.addEventListener('install', (event) => {
  self.skipWaiting();
  event.waitUntil(
    caches.open(CACHE_NAME).then((cache) => {
      return cache.addAll(ASSETS_TO_CACHE);
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
    event.respondWith(
      (async () => {
        try {
          // Use navigation preload response if available (eliminates SW cold-start penalty)
          const preloadResponse = await event.preloadResponse;
          if (preloadResponse) return preloadResponse;
          return await fetch(event.request);
        } catch (e) {
          // Fallback to cached shell when offline
          const cached = await caches.match(event.request);
          if (cached) return cached;
          const fallback = await caches.match('/');
          if (fallback) return fallback;
          throw e;
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
          caches.open(CACHE_NAME).then((cache) => cache.put(event.request, networkResponse.clone()));
        }
        return networkResponse;
      }).catch(() => null);

      return cachedResponse || fetchPromise;
    })
  );
});
