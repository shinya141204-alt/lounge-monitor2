/**
 * cta_tracker.js - Lightweight, privacy-safe viewport impression tracker for `/link/<slot>` CTAs.
 * Fires once per `(slot_id, page_path)` when a CTA link becomes >=35% visible in the viewport.
 */
(function initAisekiCtaTracker() {
    if (typeof window === 'undefined' || typeof document === 'undefined') return;
    if (navigator.webdriver) return;
    try {
        if (window.IS_STAFF) return;
        if (window.APP_CONFIG && (window.APP_CONFIG.isStaff || window.APP_CONFIG.isStaffSession)) return;
        if (window.location && window.location.search && window.location.search.indexOf('staff_test=1') !== -1) return;
    } catch (e) {}

    var seenKeys = new Set();
    var observedHrefByEl = new WeakMap();
    var io = null;
    var scanScheduled = false;

    function extractSlotId(href) {
        if (!href) return '';
        var raw = String(href).trim();
        var pathPart = raw;
        if (raw.indexOf('://') !== -1) {
            try {
                var u = new URL(raw, window.location.origin);
                if (u.origin !== window.location.origin) return '';
                pathPart = u.pathname || '';
            } catch (e) {
                return '';
            }
        }
        var m = pathPart.match(/^\/link\/([A-Za-z0-9_-]+)/);
        return m ? m[1].toLowerCase() : '';
    }

    function sendImpression(slotId) {
        if (!slotId) return;
        if (document.body && document.body.classList.contains('staff-mode')) return;
        var pathname = (window.location && window.location.pathname) ? window.location.pathname : '/';
        var dedupKey = slotId + ' @ ' + pathname;
        if (seenKeys.has(dedupKey)) return;
        seenKeys.add(dedupKey);

        var bodyStr = JSON.stringify({
            event: 'affiliate_impression',
            label: dedupKey
        });
        try {
            if (navigator.sendBeacon) {
                var blob = new Blob([bodyStr], { type: 'application/json' });
                if (navigator.sendBeacon('/api/track', blob)) return;
            }
        } catch (e) {}
        try {
            if (window.fetch) {
                window.fetch('/api/track', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: bodyStr,
                    keepalive: true,
                    credentials: 'same-origin'
                }).catch(function () {});
            }
        } catch (e) {}
    }

    function isElementRenderable(el) {
        if (!el || !el.isConnected) return false;
        if (el.offsetWidth <= 0 && el.offsetHeight <= 0 && el.getClientRects().length === 0) return false;
        return true;
    }

    function scanLinks() {
        scanScheduled = false;
        if (!io || !document.body) return;
        if (document.body.classList.contains('staff-mode')) return;
        var links = document.querySelectorAll('a[href*="/link/"]');
        for (var i = 0; i < links.length; i++) {
            var a = links[i];
            var curHref = a.getAttribute('href') || '';
            var slotId = extractSlotId(curHref);
            if (!slotId) continue;
            var prevHref = observedHrefByEl.get(a);
            if (prevHref === curHref) continue;
            if (prevHref !== undefined) {
                try { io.unobserve(a); } catch (e) {}
            }
            observedHrefByEl.set(a, curHref);
            io.observe(a);
        }
    }

    function scheduleScan() {
        if (scanScheduled) return;
        scanScheduled = true;
        if (window.requestAnimationFrame) {
            window.requestAnimationFrame(scanLinks);
        } else {
            setTimeout(scanLinks, 120);
        }
    }

    function boot() {
        if (!('IntersectionObserver' in window)) return;
        if (document.body && document.body.classList.contains('staff-mode')) return;

        io = new IntersectionObserver(function (entries) {
            for (var i = 0; i < entries.length; i++) {
                var entry = entries[i];
                if (!entry.isIntersecting || entry.intersectionRatio < 0.35) continue;
                var el = entry.target;
                if (!isElementRenderable(el)) continue;
                var slotId = extractSlotId(el.getAttribute('href') || '');
                if (slotId) {
                    sendImpression(slotId);
                }
            }
        }, { threshold: [0.35] });

        scanLinks();

        if ('MutationObserver' in window && document.body) {
            var mo = new MutationObserver(scheduleScan);
            mo.observe(document.body, {
                childList: true,
                subtree: true,
                attributes: true,
                attributeFilter: ['href', 'class', 'style']
            });
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', boot, { once: true });
    } else {
        boot();
    }
})();
