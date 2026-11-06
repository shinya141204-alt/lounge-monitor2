/**
 * cta_tracker.js - Lightweight, privacy-safe viewport impression & 2-step cushion bridge tracker.
 * 1) Fires `affiliate_impression` once per `(slot_id, page_path)` when a `/link/<slot>` CTA is >=35% visible.
 * 2) Fires `cushion_click` when a visitor clicks a 2-step editorial bridge into `/grooming` or `/for-women`.
 */
(function initAisekiCtaTracker() {
    if (typeof window === 'undefined' || typeof document === 'undefined') return;
    if (window.__aisekiCtaTrackerInit) return;
    window.__aisekiCtaTrackerInit = true;
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

    function postTrackPayload(eventName, labelStr) {
        if (!eventName || !labelStr) return;
        if (document.body && document.body.classList.contains('staff-mode')) return;
        var bodyStr = JSON.stringify({
            event: eventName,
            label: labelStr
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

    function extractCushionTarget(href) {
        if (!href) return '';
        var raw = String(href).trim();
        if (!raw || raw.charAt(0) === '#') return '';
        try {
            var u = new URL(raw, window.location.origin);
            if (u.origin !== window.location.origin) return '';
            var p = (u.pathname || '').replace(/\/+$/, '') || '/';
            if (p !== '/grooming' && p !== '/for-women') return '';
            var curPath = ((window.location && window.location.pathname) || '/').replace(/\/+$/, '') || '/';
            if (curPath === p || curPath.indexOf('/staff') === 0) return '';
            var frag = (u.hash || '').replace(/^#/, '').replace(/[^A-Za-z0-9_-]/g, '').slice(0, 28);
            return frag ? (p + '#' + frag) : p;
        } catch (e) {
            return '';
        }
    }

    function sendImpression(slotId) {
        if (!slotId) return;
        if (document.body && document.body.classList.contains('staff-mode')) return;
        var pathname = (window.location && window.location.pathname) ? window.location.pathname : '/';
        var dedupKey = slotId + ' @ ' + pathname;
        if (seenKeys.has(dedupKey)) return;
        seenKeys.add(dedupKey);
        postTrackPayload('affiliate_impression', dedupKey);
    }

    function isEditorialBridgeAnchor(anchor) {
        if (!anchor || typeof anchor.closest !== 'function') return false;
        if (anchor.hasAttribute('data-cushion')) return true;
        if (anchor.closest('nav, header, footer, .portal-nav, .portal-footer, .guide-footer, .audience-switch-bar')) return false;
        return Boolean(anchor.closest('.editorial-bridge-box, .editorial-bridge-actions, .cta-row, .share-bar-row, .related-article-card, .pr-context-foot, .ladies-pr-section, article, main'));
    }

    function onDocumentClick(ev) {
        if (!ev || !ev.target || typeof ev.target.closest !== 'function') return;
        if (document.body && document.body.classList.contains('staff-mode')) return;
        var anchor = ev.target.closest('a[href]');
        if (!anchor || !isEditorialBridgeAnchor(anchor)) return;
        var targetHub = extractCushionTarget(anchor.getAttribute('href') || '');
        if (!targetHub) return;
        var srcPath = ((window.location && window.location.pathname) ? window.location.pathname : '/').slice(0, 48);
        var dedupKey = 'cushion:' + targetHub + ' @ ' + srcPath;
        if (seenKeys.has(dedupKey)) return;
        seenKeys.add(dedupKey);
        postTrackPayload('cushion_click', targetHub + ' @ ' + srcPath);
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
        if (document.body && document.body.classList.contains('staff-mode')) return;
        document.addEventListener('click', onDocumentClick, { capture: true, passive: true });
        if (!('IntersectionObserver' in window)) return;

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
