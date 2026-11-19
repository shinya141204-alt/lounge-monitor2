/* ==========================================================================
   staff_workspace.js — 相席ナビ 管理ワークスペース共通サブタブ・ディープリンク制御
   - インライン on*= 属性ゼロ（イベント委譲）
   - ?tab=<id> クエリ ＆ #hash アンカー自動解決
   - hidden="until-found" ＆ beforematch（ページ内検索連動）対応
   - 左右矢印キー / Home / End キーボード操作対応
   ========================================================================== */
(function () {
    'use strict';

    var SUPPORTS_UNTIL_FOUND = typeof HTMLElement !== 'undefined' && ('onbeforematch' in HTMLElement.prototype);

    function hidePanel(panel) {
        if (!panel) return;
        if (SUPPORTS_UNTIL_FOUND) {
            panel.setAttribute('hidden', 'until-found');
        } else {
            panel.hidden = true;
        }
        panel.removeAttribute('tabindex');
    }

    function showPanel(panel) {
        if (!panel) return;
        panel.removeAttribute('hidden');
        panel.hidden = false;
    }

    function syncUrlTabParam(tabId, paramName) {
        if (!window.history || !window.history.replaceState) return;
        try {
            var url = new URL(window.location.href);
            var defaultTab = null;
            var firstTab = document.querySelector('.ws-workspace [role="tab"][data-ws-tab]');
            if (firstTab) defaultTab = firstTab.getAttribute('data-ws-tab');
            if (!tabId || tabId === defaultTab) {
                url.searchParams.delete(paramName);
            } else {
                url.searchParams.set(paramName, tabId);
            }
            window.history.replaceState(null, '', url.pathname + url.search + url.hash);
        } catch (e) {}
    }

    function initWorkspace(ws) {
        if (!ws || ws.dataset.wsBound === '1') return;
        ws.dataset.wsBound = '1';

        var paramName = ws.getAttribute('data-ws-param') || 'tab';
        var tablist = ws.querySelector('[role="tablist"]');
        if (!tablist) return;

        var tabs = Array.prototype.slice.call(tablist.querySelectorAll('[role="tab"][data-ws-tab]'));
        var panels = Array.prototype.slice.call(ws.querySelectorAll('.ws-tabpanel[data-ws-panel]'));
        var expandBtn = ws.querySelector('[data-ws-expand-all]');
        if (!tabs.length || !panels.length) return;

        function setExpandAll(expanded) {
            ws.setAttribute('data-ws-all-expanded', expanded ? 'true' : 'false');
            if (expandBtn) {
                expandBtn.setAttribute('aria-pressed', expanded ? 'true' : 'false');
                expandBtn.textContent = expanded ? 'タブ切替に戻す' : '全タブ展開';
            }
            if (expanded) {
                panels.forEach(function (p) { showPanel(p); });
            } else {
                var activeTab = tabs.find(function (t) { return t.getAttribute('aria-selected') === 'true'; }) || tabs[0];
                activateTab(activeTab.getAttribute('data-ws-tab'), { updateUrl: false, focusTab: false });
            }
        }

        function activateTab(tabId, opts) {
            opts = opts || {};
            var targetTab = tabs.find(function (t) { return t.getAttribute('data-ws-tab') === tabId; }) || tabs[0];
            if (!targetTab) return;
            var activeId = targetTab.getAttribute('data-ws-tab');

            ws.setAttribute('data-ws-all-expanded', 'false');
            if (expandBtn) {
                expandBtn.setAttribute('aria-pressed', 'false');
                expandBtn.textContent = '全タブ展開';
            }

            tabs.forEach(function (t) {
                var isSel = (t === targetTab);
                t.setAttribute('aria-selected', isSel ? 'true' : 'false');
                t.setAttribute('tabindex', isSel ? '0' : '-1');
            });

            panels.forEach(function (p) {
                if (p.getAttribute('data-ws-panel') === activeId) {
                    showPanel(p);
                } else {
                    hidePanel(p);
                }
            });

            if (opts.scrollTabIntoView !== false && typeof targetTab.scrollIntoView === 'function') {
                try {
                    targetTab.scrollIntoView({ block: 'nearest', inline: 'nearest' });
                } catch (e) {}
            }
            if (opts.focusTab) {
                try { targetTab.focus({ preventScroll: true }); } catch (e) { targetTab.focus(); }
            }
            if (opts.updateUrl !== false) {
                syncUrlTabParam(activeId, paramName);
            }
        }

        function resolveTabForHash(hashStr) {
            if (!hashStr || hashStr.length <= 1) return null;
            var rawId = '';
            try {
                rawId = decodeURIComponent(hashStr.slice(1));
            } catch (e) {
                rawId = hashStr.slice(1);
            }
            if (!rawId) return null;

            // Legacy /staff/analytics#ops-governance-hub bookmark redirect to /staff/ops
            if (rawId === 'ops-governance-hub' && window.location.pathname === '/staff/analytics') {
                window.location.replace('/staff/ops#ops-governance-hub');
                return null;
            }

            var directTab = tabs.find(function (t) { return t.getAttribute('data-ws-tab') === rawId; });
            if (directTab) return rawId;

            var targetEl = document.getElementById(rawId);
            if (!targetEl) return null;
            var parentPanel = targetEl.closest('.ws-tabpanel[data-ws-panel]');
            if (parentPanel && ws.contains(parentPanel)) {
                return parentPanel.getAttribute('data-ws-panel');
            }
            return null;
        }

        // Click on tab
        tablist.addEventListener('click', function (e) {
            var btn = e.target.closest('[role="tab"][data-ws-tab]');
            if (!btn || !tablist.contains(btn)) return;
            activateTab(btn.getAttribute('data-ws-tab'), { updateUrl: true, focusTab: false });
        });

        // Keyboard arrow navigation inside tablist
        tablist.addEventListener('keydown', function (e) {
            var current = e.target.closest('[role="tab"][data-ws-tab]');
            if (!current) return;
            var idx = tabs.indexOf(current);
            if (idx < 0) return;
            var nextIdx = -1;
            if (e.key === 'ArrowRight') nextIdx = (idx + 1) % tabs.length;
            else if (e.key === 'ArrowLeft') nextIdx = (idx - 1 + tabs.length) % tabs.length;
            else if (e.key === 'Home') nextIdx = 0;
            else if (e.key === 'End') nextIdx = tabs.length - 1;
            if (nextIdx >= 0) {
                e.preventDefault();
                activateTab(tabs[nextIdx].getAttribute('data-ws-tab'), { updateUrl: true, focusTab: true });
            }
        });

        // Expand all toggle
        if (expandBtn) {
            expandBtn.addEventListener('click', function () {
                var isExpanded = ws.getAttribute('data-ws-all-expanded') === 'true';
                setExpandAll(!isExpanded);
            });
        }

        // Find-in-page beforematch auto-reveal
        ws.addEventListener('beforematch', function (e) {
            var matchedPanel = e.target && e.target.closest ? e.target.closest('.ws-tabpanel[data-ws-panel]') : null;
            if (matchedPanel && ws.contains(matchedPanel)) {
                activateTab(matchedPanel.getAttribute('data-ws-panel'), { updateUrl: true, focusTab: false, scrollTabIntoView: true });
            }
        });

        // External / in-page [data-ws-goto-tab] triggers
        ws.addEventListener('click', function (e) {
            var trigger = e.target.closest('[data-ws-goto-tab]');
            if (!trigger) return;
            var destTab = trigger.getAttribute('data-ws-goto-tab');
            if (destTab) {
                e.preventDefault();
                activateTab(destTab, { updateUrl: true, focusTab: true });
                var anchor = trigger.getAttribute('data-ws-scroll-to');
                if (anchor) {
                    var el = document.getElementById(anchor);
                    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
            }
        });

        // Initial tab resolution priority: 1) #hash target inside a panel -> 2) ?tab= query -> 3) HTML default aria-selected="true"
        var initialTabId = resolveTabForHash(window.location.hash);
        var hadHashMatch = Boolean(initialTabId);
        if (!initialTabId) {
            try {
                var sp = new URLSearchParams(window.location.search);
                var qTab = sp.get(paramName);
                if (qTab && tabs.some(function (t) { return t.getAttribute('data-ws-tab') === qTab; })) {
                    initialTabId = qTab;
                }
            } catch (e) {}
        }
        if (!initialTabId) {
            var preset = tabs.find(function (t) { return t.getAttribute('aria-selected') === 'true'; }) || tabs[0];
            initialTabId = preset.getAttribute('data-ws-tab');
        }
        activateTab(initialTabId, { updateUrl: false, focusTab: false, scrollTabIntoView: false });

        if (hadHashMatch && window.location.hash.length > 1) {
            var hTarget = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
            if (hTarget) {
                setTimeout(function () {
                    hTarget.scrollIntoView({ block: 'start' });
                }, 30);
            }
        }

        window.addEventListener('hashchange', function () {
            var hashTab = resolveTabForHash(window.location.hash);
            if (hashTab) {
                activateTab(hashTab, { updateUrl: true, focusTab: false });
                var tEl = document.getElementById(decodeURIComponent(window.location.hash.slice(1)));
                if (tEl) tEl.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        });
        ws._activateStaffTab = activateTab;
    }

    window.activateStaffWorkspaceTab = function (tabId, scrollId) {
        var ws = document.querySelector('.ws-workspace');
        if (ws && typeof ws._activateStaffTab === 'function') {
            ws._activateStaffTab(tabId, { updateUrl: true, focusTab: false });
            if (scrollId) {
                var el = document.getElementById(scrollId);
                if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
            }
        }
    };

    if (typeof window.toggleStaffTheme !== 'function') {
        window.toggleStaffTheme = function () {
            var isLight = document.body.classList.toggle('theme-light');
            document.documentElement.classList.toggle('theme-light', isLight);
            try {
                localStorage.setItem('olg_view_theme', isLight ? 'light' : 'default');
            } catch (e) {}
            var btn = document.getElementById('staff-theme-toggle-btn');
            if (btn) btn.setAttribute('aria-pressed', isLight ? 'true' : 'false');
        };
    }

    function bootAllWorkspaces() {
        var list = document.querySelectorAll('.ws-workspace');
        for (var i = 0; i < list.length; i++) {
            initWorkspace(list[i]);
        }
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', bootAllWorkspaces);
    } else {
        bootAllWorkspaces();
    }
})();
