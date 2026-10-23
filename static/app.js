/**
 * 相席ナビ (AISEKI NAVI) - Main Application Script
 * Cached & Compressed for High-Speed Performance
 */

        const IS_STAFF = (window.APP_CONFIG && window.APP_CONFIG.isStaff) || false;
        let currentFilter = 'All';
        let currentSearch = '';
        let currentSort = 'women';

        // --- Store Master (static/store_master.json): single source of truth for area label, coordinates & map anchor ---
        window.STORE_MASTER = {};
        let _brandInfoCache = Object.create(null);
        let _storeEventsCache = Object.create(null);
        let _lastRenderedSignature = '';
        let _updateUiRafId = null;
        let _pendingUiData = null;

        function getStoreMasterInfo(storeName) {
            return (window.STORE_MASTER && window.STORE_MASTER[String(storeName || '').trim()]) || null;
        }
        (function loadStoreMaster() {
            fetch('/static/store_master.json?v=20261023')
                .then(r => r.ok ? r.json() : null)
                .then(d => {
                    if (d && d.stores) {
                        window.STORE_MASTER = d.stores;
                        _brandInfoCache = Object.create(null);
                        if (window.lastData && typeof updateUI === 'function') updateUI(window.lastData);
                    }
                })
                .catch(() => {});
        })();

        // --- First-party analytics (event + short label only, no personal data) ---
        function trackEvent(event, label) {
            if (IS_STAFF) return;
            try {
                const body = JSON.stringify({ event: String(event || ''), label: String(label || '').slice(0, 80) });
                if (navigator.sendBeacon) {
                    navigator.sendBeacon('/api/track', new Blob([body], { type: 'application/json' }));
                } else {
                    fetch('/api/track', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body, keepalive: true }).catch(() => {});
                }
            } catch (e) {}
        }
        window.trackEvent = trackEvent;

        document.addEventListener('click', (e) => {
            const a = e.target && e.target.closest ? e.target.closest('a, button') : null;
            if (!a) return;
            const id = a.id || '';
            const href = a.getAttribute('href') || '';
            const store = (typeof currentModalStoreName !== 'undefined' && currentModalStoreName) || '';
            if (id === 'modal-map-cafe-btn') trackEvent('map_cafe', store);
            else if (id === 'modal-map-bar-btn') trackEvent('map_bar', store);
            else if (id === 'modal-official-link' || id === 'modal-top-official-link') trackEvent('official_click', store);
            else if (href.includes('x.com/aiseki_navi')) trackEvent('sns_click', 'x');
            else if (href.includes('threads.net')) trackEvent('sns_click', 'threads');
            else if (href.includes('note.com/')) trackEvent('sns_click', 'note');
        }, true);

        // --- Store Events & Campaign State ---
        window.allEvents = [];
        let _eventsFetched = false;

        const AREA_ALIASES = {
            '西新宿': ['西新宿', 'nishishinjuku'],
            '新宿': ['新宿', 'shinjuku'],
            '茶屋町': ['茶屋町', 'chayamachi'],
            '梅田': ['梅田', 'umeda'],
            '大宮': ['大宮', 'omiya'],
            '渋谷': ['渋谷', 'shibuya'],
            '恵比寿': ['恵比寿', 'ebisu'],
            '上野': ['上野', 'ueno'],
            '六本木': ['六本木', 'roppongi'],
            '銀座': ['銀座', 'ginza'],
            '池袋': ['池袋', 'ikebukuro'],
            '町田': ['町田', 'machida'],
            '横浜': ['横浜', 'yokohama'],
            '千葉': ['千葉', 'chiba'],
            '柏': ['柏', 'kashiwa'],
            '錦糸町': ['錦糸町', 'kinshicho'],
            '難波': ['難波', 'なんば', 'namba'],
            '心斎橋': ['心斎橋', 'shinsaibashi'],
            '京都': ['京都', 'kyoto'],
            '神戸': ['神戸', 'kobe'],
            '福岡': ['福岡', '天神', 'fukuoka', 'tenjin'],
            '小倉': ['小倉', 'kokura'],
            '熊本': ['熊本', 'kumamoto'],
            '松山': ['松山', 'matsuyama'],
            '札幌': ['札幌', 'sapporo'],
            '仙台': ['仙台', 'sendai'],
            '名古屋': ['名古屋', 'nagoya'],
            '栄': ['栄', '名古屋栄', 'sakae'],
            '錦': ['錦', '名古屋錦', 'nishiki'],
            '広島': ['広島', 'hiroshima'],
            '岡山': ['岡山', 'okayama'],
            '宇都宮': ['宇都宮', 'utsunomiya'],
            '高崎': ['高崎', 'takasaki'],
            '金沢': ['金沢', 'kanazawa'],
            '静岡': ['静岡', 'shizuoka'],
            '浜松': ['浜松', 'hamamatsu'],
            '天満': ['天満', 'tenma'],
            '大阪駅前': ['大阪駅前', 'osaka'],
            '長崎': ['長崎', 'nagasaki'],
            '大分': ['大分', 'oita'],
            '宮崎': ['宮崎', 'miyazaki'],
            '鹿児島': ['鹿児島', 'kagoshima'],
            '沖縄': ['沖縄', 'okinawa']
        };

        // --- Geolocation Coordinates for Each Major Area & Brand Hub ---
        const AREA_COORDINATES = {
            '西新宿': { lat: 35.6918, lng: 139.6974 },
            '新宿': { lat: 35.6938, lng: 139.7034 },
            '茶屋町': { lat: 34.7058, lng: 135.4988 },
            '梅田': { lat: 34.7025, lng: 135.4983 },
            '大宮': { lat: 35.9063, lng: 139.6240 },
            '渋谷': { lat: 35.6595, lng: 139.7005 },
            '恵比寿': { lat: 35.6467, lng: 139.7101 },
            '上野': { lat: 35.7118, lng: 139.7744 },
            '六本木': { lat: 35.6628, lng: 139.7314 },
            '銀座': { lat: 35.6719, lng: 139.7649 },
            '池袋': { lat: 35.7295, lng: 139.7109 },
            '町田': { lat: 35.5420, lng: 139.4457 },
            '横浜': { lat: 35.4658, lng: 139.6220 },
            '千葉': { lat: 35.6131, lng: 140.1134 },
            '柏': { lat: 35.8622, lng: 139.9710 },
            '錦糸町': { lat: 35.6967, lng: 139.8144 },
            '難波': { lat: 34.6669, lng: 135.5013 },
            'なんば': { lat: 34.6669, lng: 135.5013 },
            '心斎橋': { lat: 34.6750, lng: 135.5005 },
            '京都': { lat: 35.0037, lng: 135.7701 },
            '河原町': { lat: 35.0037, lng: 135.7701 },
            '神戸': { lat: 34.6946, lng: 135.1934 },
            '三宮': { lat: 34.6946, lng: 135.1934 },
            '福岡': { lat: 33.5897, lng: 130.3986 },
            '天神': { lat: 33.5897, lng: 130.3986 },
            '中洲': { lat: 33.5930, lng: 130.4060 },
            '小倉': { lat: 33.8863, lng: 130.8828 },
            '熊本': { lat: 32.8032, lng: 130.7079 },
            '松山': { lat: 33.8392, lng: 132.7656 },
            '高松': { lat: 34.3428, lng: 134.0465 },
            '札幌': { lat: 43.0554, lng: 141.3533 },
            'すすきの': { lat: 43.0554, lng: 141.3533 },
            '仙台': { lat: 38.2619, lng: 140.8710 },
            '国分町': { lat: 38.2619, lng: 140.8710 },
            '名古屋': { lat: 35.1706, lng: 136.9039 },
            '栄': { lat: 35.1685, lng: 136.9080 },
            '錦': { lat: 35.1712, lng: 136.9045 },
            '広島': { lat: 34.3928, lng: 132.4636 },
            '岡山': { lat: 34.6663, lng: 133.9185 },
            '宇都宮': { lat: 36.5590, lng: 139.8984 },
            '高崎': { lat: 36.3225, lng: 139.0127 },
            '金沢': { lat: 36.5613, lng: 136.6562 },
            '静岡': { lat: 34.9717, lng: 138.3891 },
            '浜松': { lat: 34.7037, lng: 137.7348 },
            '天満': { lat: 34.7050, lng: 135.5130 },
            '大阪駅前': { lat: 34.7010, lng: 135.4980 },
            '長崎': { lat: 32.7448, lng: 129.8737 },
            '大分': { lat: 33.2382, lng: 131.6067 },
            '宮崎': { lat: 31.9111, lng: 131.4239 },
            '鹿児島': { lat: 31.5966, lng: 130.5571 },
            '沖縄': { lat: 26.2166, lng: 127.6842 },
            '那覇': { lat: 26.2166, lng: 127.6842 },
            'カンナム': { lat: 37.4979, lng: 127.0276 },
            '江南': { lat: 37.4979, lng: 127.0276 },
            'ホンデ': { lat: 37.5563, lng: 126.9236 },
            '弘大': { lat: 37.5563, lng: 126.9236 },
            'ソウル': { lat: 37.5665, lng: 126.9780 }
        };

        const REGION_DEFAULT_COORDINATES = {
            'Kanto': { lat: 35.6895, lng: 139.6917 },
            'Kinki': { lat: 34.6937, lng: 135.5023 },
            'Chubu': { lat: 35.1815, lng: 136.9066 },
            'Kyushu': { lat: 33.5904, lng: 130.4017 },
            'Hokkaido': { lat: 43.0642, lng: 141.3469 },
            'Tohoku': { lat: 38.2682, lng: 140.8694 },
            'Chugoku': { lat: 34.3853, lng: 132.4553 },
            'Shikoku': { lat: 33.8416, lng: 132.7657 },
            'Korea': { lat: 37.5665, lng: 126.9780 }
        };

        // Japanese map for English store names
        const EN_TO_JA_COORDS = {
            'NISHISHINJUKU': '西新宿', 'SHINJUKU': '新宿', 'SHIBUYA': '渋谷',
            'EBISU': '恵比寿', 'UENO': '上野', 'MACHIDA': '町田', 'KASHIWA': '柏',
            'OMIYA': '大宮', 'YOKOHAMA': '横浜', 'UTSUNOMIYA': '宇都宮',
            'TAKASAKI': '高崎', 'SAPPORO': '札幌', 'SENDAI': '仙台',
            'NAGOYA': '名古屋', 'SHIZUOKA': '静岡', 'HAMAMATSU': '浜松',
            'KANAZAWA': '金沢', 'CHAYAMACHI': '茶屋町', 'UMEDA': '梅田',
            'NAMBA': '難波', 'SHINSAIBASHI': '心斎橋', 'TENMA': '天満',
            'KYOTO': '京都', 'KOBE': '神戸', 'OKAYAMA': '岡山',
            'HIROSHIMA': '広島', 'MATSUYAMA': '松山', 'FUKUOKA': '福岡',
            'KOKURA': '小倉', 'NAGASAKI': '長崎', 'OITA': '大分',
            'KUMAMOTO': '熊本', 'MIYAZAKI': '宮崎', 'KAGOSHIMA': '鹿児島',
            'OKINAWA': '沖縄', 'SEOUL': 'ソウル', 'GANGNAM': 'カンナム',
            'HONGDAE': 'ホンデ'
        };

        function getStoreCoordinates(store) {
            const master = getStoreMasterInfo(store.name);
            if (master && typeof master.lat === 'number' && typeof master.lng === 'number') {
                return { lat: master.lat, lng: master.lng };
            }
            const rawName = String(store.name || '').toUpperCase();
            let searchTarget = rawName;

            // Expand romanized tokens to Japanese (e.g. JIS UMEDA -> JIS UMEDA 梅田)
            const words = rawName.replace(/[^A-Z0-9]/g, ' ').split(/\s+/);
            for (const w of words) {
                if (EN_TO_JA_COORDS[w]) {
                    searchTarget += ' ' + EN_TO_JA_COORDS[w];
                }
            }

            // Check keys sorted by length descending so '西新宿' matches before '新宿'
            const sortedAreaKeys = Object.keys(AREA_COORDINATES).sort((a, b) => b.length - a.length);
            for (const key of sortedAreaKeys) {
                if (searchTarget.includes(key.toUpperCase())) {
                    return AREA_COORDINATES[key];
                }
            }

            // Fallback to region center
            if (store.region && REGION_DEFAULT_COORDINATES[store.region]) {
                return REGION_DEFAULT_COORDINATES[store.region];
            }
            return { lat: 35.6895, lng: 139.6917 };
        }

        function calculateDistanceKm(lat1, lon1, lat2, lon2) {
            const R = 6371; // Earth radius in km
            const dLat = (lat2 - lat1) * Math.PI / 180;
            const dLon = (lon2 - lon1) * Math.PI / 180;
            const a = Math.sin(dLat / 2) * Math.sin(dLat / 2) +
                      Math.cos(lat1 * Math.PI / 180) * Math.cos(lat2 * Math.PI / 180) *
                      Math.sin(dLon / 2) * Math.sin(dLon / 2);
            const c = 2 * Math.atan2(Math.sqrt(a), Math.sqrt(1 - a));
            return R * c;
        }

        let userLocation = null;
        let isLocating = false;

        function setNearbySort() {
            if (userLocation) {
                currentSort = 'nearby';
                document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
                const btn = document.getElementById('nearby-sort-btn');
                if (btn) btn.classList.add('active');
                if (window.lastData) updateUI(window.lastData);
                return;
            }

            if (!navigator.geolocation) {
                alert('お使いのブラウザ・端末は位置情報機能に対応していません。');
                return;
            }

            if (isLocating) return;
            isLocating = true;
            const btn = document.getElementById('nearby-sort-btn');
            if (btn) btn.textContent = '取得中...';

            navigator.geolocation.getCurrentPosition(
                (pos) => {
                    isLocating = false;
                    userLocation = {
                        lat: pos.coords.latitude,
                        lng: pos.coords.longitude
                    };
                    if (btn) btn.textContent = '現在地順';
                    currentSort = 'nearby';
                    document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
                    if (btn) btn.classList.add('active');
                    if (window.lastData) updateUI(window.lastData);
                },
                (err) => {
                    isLocating = false;
                    if (btn) btn.textContent = '現在地順';
                    console.warn('Geolocation error:', err);
                    if (err.code === 1) {
                        alert('位置情報の利用が許可されませんでした。ブラウザや端末の設定で位置情報を許可して再度お試しください。');
                    } else {
                        alert('現在地を取得できませんでした。電波の良い場所で再度お試しください。');
                    }
                },
                { enableHighAccuracy: false, timeout: 8000, maximumAge: 60000 }
            );
        }

        const JIS_STORE_TO_AREAS = {
            'JIS SHINJUKU': ['新宿', 'shinjuku'],
            'JIS NISHISHINJUKU': ['西新宿', 'nishishinjuku'],
            'JIS UMEDA': ['梅田', 'umeda'],
            'JIS CHAYAMACHI': ['茶屋町', 'chayamachi'],
            'JIS NAMBA': ['難波', 'なんば', 'namba'],
            'JIS FUKUOKA': ['福岡', '天神', 'fukuoka', 'tenjin'],
            'JIS SAPPORO': ['札幌', 'sapporo'],
            'JIS OMIYA': ['大宮', 'omiya'],
            'JIS KUMAMOTO': ['熊本', 'kumamoto'],
            'JIS MATSUYAMA': ['松山', 'matsuyama']
        };

        function _storeMatchesArea(storeName, areaKeyword) {
            const cleanStore = String(storeName).toUpperCase().replace(/\s+/g, '');
            const cleanKw = String(areaKeyword).toUpperCase().replace(/\s+/g, '');

            // 1. Direct exact matching for JIS shops
            const cleanJis = String(storeName).toUpperCase().trim();
            if (JIS_STORE_TO_AREAS[cleanJis]) {
                const ak = String(areaKeyword).toLowerCase().trim();
                return JIS_STORE_TO_AREAS[cleanJis].includes(ak);
            }

            // 2. ag brand separation (prevent OLG vs ag collision in same cities)
            const isAgStore = cleanStore.includes('AG');
            const isAgKw = cleanKw.includes('AG');
            const multiAgCities = ['名古屋', '上野', '渋谷', '梅田', '広島', '沖縄'];
            for (const city of multiAgCities) {
                if (cleanStore.includes(city) && cleanKw.includes(city)) {
                    if (isAgStore !== isAgKw) return false;
                }
            }

            // 3. Nagoya Sakae vs Nishiki separation
            if (cleanStore.includes('栄')) {
                if (cleanKw.includes('錦') || cleanKw.includes('NISHIKI')) return false;
            }
            if (cleanStore.includes('錦')) {
                if (cleanKw.includes('栄') || cleanKw.includes('SAKAE')) return false;
            }

            // 4. Direct substring match
            if (cleanKw.includes(cleanStore) || cleanStore.includes(cleanKw)) return true;

            // 5. Alias mapping match
            const sn = String(storeName).toLowerCase().replace(/\s+/g, '');
            const ak = String(areaKeyword).toLowerCase().replace(/\s+/g, '');
            for (const [key, aliases] of Object.entries(AREA_ALIASES)) {
                if (key.toLowerCase() === ak || aliases.some(a => a.toLowerCase() === ak)) {
                    if (aliases.some(alias => sn.includes(alias.toLowerCase()))) return true;
                }
            }
            return false;
        }

        async function loadEvents() {
            if (_eventsFetched && window.allEvents.length > 0) return window.allEvents;
            try {
                const res = await fetch('/api/events');
                if (res.ok) {
                    const data = await res.json();
                    window.allEvents = data.events || [];
                    _storeEventsCache = Object.create(null);
                    _eventsFetched = true;
                    // Re-render ranking to display event badges on cards
                    if (window.lastData && window.lastData.ranking) {
                        updateUI(window.lastData);
                    }
                }
            } catch (err) {
                console.warn('Failed to load events:', err);
            }
            return window.allEvents;
        }

        function getEventsForStore(storeName) {
            if (!window.allEvents || window.allEvents.length === 0) return [];
            const key = String(storeName || '').trim();
            if (_storeEventsCache[key] && _storeEventsCache[key].all) {
                return _storeEventsCache[key].all;
            }
            const clean = key.toUpperCase();
            const isJis = clean.includes('JIS');
            const isOlg = clean.includes('OLG') || clean.includes('ORIENTAL') || clean.includes('AG');

            const all = window.allEvents.filter(ev => {
                // Filter out ubiquitous default perks like "女性限定フード食べ放題"
                const t = ev.title || '';
                if (t.includes('フード食べ放題') || t.includes('女性限定フード')) return false;

                if (isJis && ev.brand === 'JIS') {
                    if (ev.store_name && ev.store_name.toUpperCase().trim() === clean) return true;
                    if (!ev.target_stores || ev.target_stores.length === 0) return true;
                    return ev.target_stores.some(area => _storeMatchesArea(storeName, area));
                }
                if (isOlg && ev.brand === 'OLG') {
                    if (!ev.target_stores || ev.target_stores.length === 0) return true;
                    return ev.target_stores.some(area => _storeMatchesArea(storeName, area));
                }
                return false;
            });
            const specific = all.filter(e => e.target_stores && e.target_stores.length > 0 && e.target_label !== '全店共通' && e.target_label !== 'JIS全店');
            _storeEventsCache[key] = { all, specific };
            return all;
        }

        // Returns only STORE-SPECIFIC events (excludes all-store / brand-wide events)
        function getStoreSpecificEvents(storeName) {
            if (!window.allEvents || window.allEvents.length === 0) return [];
            const key = String(storeName || '').trim();
            if (_storeEventsCache[key] && _storeEventsCache[key].specific) {
                return _storeEventsCache[key].specific;
            }
            getEventsForStore(storeName);
            return (_storeEventsCache[key] && _storeEventsCache[key].specific) || [];
        }

        // --- Staff Events Visibility Control ---
        // In staff mode, events are hidden by default to prioritize analytics and operations
        let staffHideEvents = IS_STAFF ? (localStorage.getItem('olg_staff_hide_events') !== 'false') : false;

        function shouldShowEvents() {
            if (IS_STAFF && staffHideEvents) return false;
            return true;
        }

        function toggleStaffEvents() {
            if (!IS_STAFF) return;
            staffHideEvents = !staffHideEvents;
            try {
                localStorage.setItem('olg_staff_hide_events', staffHideEvents ? 'true' : 'false');
            } catch (e) {}
            applyStaffEventsState();
            if (typeof showRefreshToast === 'function') {
                showRefreshToast(staffHideEvents ? '🔒 イベント非表示に設定しました（業務集中）' : '🎁 イベント表示をONにしました');
            }
            if (window.lastData && window.lastData.ranking) {
                updateUI(window.lastData);
            }
        }

        function applyStaffEventsState() {
            if (!IS_STAFF) return;
            document.body.classList.toggle('staff-events-hidden', staffHideEvents);

            const toggleBtn = document.getElementById('staff-toggle-events-btn');
            const toggleIcon = document.getElementById('staff-toggle-events-icon');
            const toggleLabel = document.getElementById('staff-toggle-events-label');
            const headerEventsBtn = document.getElementById('events-btn');

            if (headerEventsBtn) {
                headerEventsBtn.style.display = staffHideEvents ? 'none' : '';
            }

            if (toggleBtn && toggleLabel) {
                toggleBtn.style.background = '';
                toggleBtn.style.borderColor = '';
                toggleBtn.style.color = '';
                if (staffHideEvents) {
                    toggleBtn.classList.remove('active');
                    toggleBtn.classList.add('events-hidden');
                    if (toggleIcon) toggleIcon.textContent = '🔒';
                    toggleLabel.textContent = 'イベント非表示: ON（業務集中モード）';
                } else {
                    toggleBtn.classList.remove('events-hidden');
                    toggleBtn.classList.add('active');
                    if (toggleIcon) toggleIcon.textContent = '🎁';
                    toggleLabel.textContent = 'イベント表示中（タップで非表示切替）';
                }
            }
        }

        // Apply staff events state immediately on load
        if (IS_STAFF) {
            applyStaffEventsState();
            document.addEventListener('DOMContentLoaded', applyStaffEventsState);
        }
        const THEME_ORDER = ['default', 'list', 'light'];
        const THEME_LABELS = {
            default: '通常モード（カード）',
            list: 'リストモード（1行表示）',
            light: 'ライトモード（白背景）'
        };
        let currentTheme = localStorage.getItem('olg_view_theme') || (localStorage.getItem('olg_compact_view') === 'true' ? 'list' : 'default');
        if (!THEME_ORDER.includes(currentTheme)) currentTheme = 'default';
        let pinnedStores = JSON.parse(localStorage.getItem('olg_pinned_stores')) || [];

        function applyThemeClasses(themeName) {
            document.body.classList.remove('compact-mode', 'theme-list', 'theme-flat', 'theme-light');
            if (themeName === 'list') {
                document.body.classList.add('compact-mode', 'theme-list');
            } else if (themeName === 'light') {
                document.body.classList.add('theme-light');
            }
            document.querySelectorAll('.theme-pill-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.theme === themeName);
            });
        }

        // Apply saved theme immediately on load
        applyThemeClasses(currentTheme);

        function setViewTheme(themeName, notify = false) {
            if (!THEME_ORDER.includes(themeName)) themeName = 'default';
            currentTheme = themeName;
            try {
                localStorage.setItem('olg_view_theme', currentTheme);
                localStorage.setItem('olg_compact_view', currentTheme === 'list' ? 'true' : 'false');
            } catch (e) {}
            applyThemeClasses(currentTheme);
            if (notify && typeof showRefreshToast === 'function') {
                showRefreshToast(`表示切替: ${THEME_LABELS[currentTheme]}`);
            }
        }

        // Domain change announcement banner logic
        function initDomainNotice() {
            if (IS_STAFF) return;
            try {
                if (!localStorage.getItem('hide_domain_notice_aiseki_navi_v1')) {
                    const banner = document.getElementById('domain-notice-banner');
                    if (banner) banner.classList.remove('hidden');
                }
            } catch (e) {}
        }
        function dismissDomainNotice() {
            const banner = document.getElementById('domain-notice-banner');
            if (banner) {
                banner.style.opacity = '0';
                banner.style.transform = 'translateY(-6px)';
                setTimeout(() => {
                    banner.classList.add('hidden');
                }, 200);
            }
            try {
                localStorage.setItem('hide_domain_notice_aiseki_navi_v1', '1');
            } catch (e) {}
        }
        function initHowToOnboarding() {
            if (IS_STAFF) return;
            try {
                const seen = localStorage.getItem('seen_how_to_guide_v1');
                if (!seen) {
                    setTimeout(() => {
                        openHowToModal();
                    }, 900);
                }
            } catch (e) {}
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => {
                initDomainNotice();
                initHowToOnboarding();
            });
        } else {
            initDomainNotice();
            initHowToOnboarding();
        }

        function toggleCompact() {
            const idx = THEME_ORDER.indexOf(currentTheme);
            const nextTheme = THEME_ORDER[(idx + 1) % THEME_ORDER.length];
            setViewTheme(nextTheme, true);
        }

        // Save Pinned Stores
        function togglePin(storeName) {
            if (pinnedStores.includes(storeName)) {
                pinnedStores = pinnedStores.filter(name => name !== storeName);
            } else {
                pinnedStores.push(storeName);
            }
            localStorage.setItem('olg_pinned_stores', JSON.stringify(pinnedStores));

            // Re-render immediately
            if (window.lastData) {
                updateUI(window.lastData);
            }
        }

        function handleSearch(query) {
            currentSearch = query.toLowerCase();
            if (window.lastData) updateUI(window.lastData);
        }

        function setSort(sortType) {
            currentSort = sortType;
            document.querySelectorAll('.sort-btn').forEach(b => b.classList.remove('active'));
            document.querySelector(`.sort-btn[data-sort="${sortType}"]`).classList.add('active');
            if (window.lastData) updateUI(window.lastData);
        }

        // Add filter button listeners
        document.querySelectorAll('.filter-btn').forEach(btn => {
            btn.addEventListener('click', (e) => {
                // Remove active class from all
                document.querySelectorAll('.filter-btn').forEach(b => b.classList.remove('active'));
                // Add active to clicked
                e.target.classList.add('active');

                currentFilter = e.target.dataset.region;

                // Re-render with cached data if available (or wait for next fetch)
                if (window.lastData) {
                    updateUI(window.lastData);
                }
            });
        });

        let isInitialLoad = true;
        let _renderedFromSSR = false;
        const clientAnalysisCache = {}; // storeName -> { data, fetchedAt }
        const CLIENT_CACHE_TTL = 10 * 60 * 1000; // 10 minutes
        let prefetchTriggered = false;

        // Instant Load: Render server-embedded INITIAL_STATUS_DATA (0ms, zero network roundtrip) or localStorage cache
        function restoreCachedStatus() {
            try {
                if (window.INITIAL_STATUS_DATA && Array.isArray(window.INITIAL_STATUS_DATA.ranking) && window.INITIAL_STATUS_DATA.ranking.length > 0) {
                    window.lastData = window.INITIAL_STATUS_DATA;
                    isInitialLoad = false;
                    _renderedFromSSR = true;
                    updateUI(window.INITIAL_STATUS_DATA);
                    const loadingEl = document.getElementById('loading');
                    const contentEl = document.getElementById('content');
                    const errEl = document.getElementById('error-message');
                    if (loadingEl) loadingEl.classList.add('hidden');
                    if (contentEl) contentEl.classList.remove('hidden');
                    if (errEl) errEl.classList.add('hidden');
                    try {
                        localStorage.setItem('olg_cached_status', JSON.stringify({
                            data: window.INITIAL_STATUS_DATA,
                            savedAt: Date.now()
                        }));
                    } catch (e) {}
                    return;
                }
                const savedRaw = localStorage.getItem('olg_cached_status');
                if (savedRaw) {
                    const saved = JSON.parse(savedRaw);
                    if (saved && saved.data && saved.savedAt && (Date.now() - saved.savedAt < 30 * 60 * 1000)) {
                        window.lastData = saved.data;
                        isInitialLoad = false;
                        updateUI(saved.data);
                        const loadingEl = document.getElementById('loading');
                        const contentEl = document.getElementById('content');
                        if (loadingEl) loadingEl.classList.add('hidden');
                        if (contentEl) contentEl.classList.remove('hidden');
                    }
                }
            } catch (e) {}
        }

        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', restoreCachedStatus);
        } else {
            restoreCachedStatus();
        }

        async function fetchWithTimeout(url, timeoutMs = 15000) {
            const controller = new AbortController();
            const timer = setTimeout(() => controller.abort(), timeoutMs);
            try {
                const response = await fetch(url, { signal: controller.signal });
                clearTimeout(timer);
                return response;
            } catch (e) {
                clearTimeout(timer);
                throw e;
            }
        }

        async function prefetchKeyStores(ranking) {
            if (prefetchTriggered || !ranking || ranking.length === 0) return;
            prefetchTriggered = true;

            // Prioritize pinned stores + top 3 stores
            const targets = [...pinnedStores];
            for (let i = 0; i < Math.min(3, ranking.length); i++) {
                if (!targets.includes(ranking[i].name)) {
                    targets.push(ranking[i].name);
                }
            }

            // Wait 3.5s after initial render so UI stays completely snappy, then prefetch sequentially during idle
            await new Promise(r => setTimeout(r, 3500));
            for (const storeName of targets.slice(0, 5)) {
                if (clientAnalysisCache[storeName] && (Date.now() - clientAnalysisCache[storeName].fetchedAt < CLIENT_CACHE_TTL)) {
                    continue;
                }
                try {
                    const res = await fetch('/api/analysis/' + encodeURIComponent(storeName));
                    if (res.ok) {
                        const d = await res.json();
                        if (!d.error) {
                            clientAnalysisCache[storeName] = { data: d, fetchedAt: Date.now() };
                        }
                    }
                } catch (e) {}
                await new Promise(r => setTimeout(r, 400));
            }
        }

        async function fetchData() {
            try {
                const response = await fetchWithTimeout('/api/status', 15000);
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();

                if (data.status === 'loading') {
                    const loadingMsg = document.getElementById('loading').querySelector('p');
                    if (loadingMsg) {
                        if (!window._loadRetryCount) window._loadRetryCount = 0;
                        window._loadRetryCount++;
                        const dots = '.'.repeat((window._loadRetryCount % 3) + 1);
                        const elapsed = window._loadRetryCount * 3;
                        if (elapsed < 15) {
                            loadingMsg.textContent = `サーバー起動中${dots} まもなくデータを取得します`;
                        } else if (elapsed < 45) {
                            loadingMsg.textContent = `データ取得中${dots} もうしばらくお待ちください`;
                        } else {
                            loadingMsg.textContent = `接続に時間がかかっています${dots} 自動でリトライ中`;
                        }
                    }
                    setTimeout(fetchData, 2500);
                    return;
                }

                if (data.error || data.status !== 'success') {
                    const list = document.getElementById('ranking-list');
                    if (list.children.length === 0) {
                        showError();
                    }
                    return;
                }

                isInitialLoad = false;
                window.lastData = data;
                try {
                    localStorage.setItem('olg_cached_status', JSON.stringify({
                        data: data,
                        savedAt: Date.now()
                    }));
                } catch (e) {}

                updateUI(data);
                document.getElementById('loading').classList.add('hidden');
                document.getElementById('content').classList.remove('hidden');
                document.getElementById('error-message').classList.add('hidden');

                // Fetch events metadata once in background (non-blocking cached endpoint)
                loadEvents();
            } catch (e) {
                console.error("Fetch error:", e);
                if (isInitialLoad) {
                    const loadingEl = document.getElementById('loading');
                    const loadingMsg = loadingEl.querySelector('p');
                    if (loadingMsg) {
                        loadingMsg.textContent = 'サーバー起動中... 再接続しています';
                    }
                    setTimeout(fetchData, 2500);
                } else {
                    console.warn("Background fetch failed, keeping existing data");
                }
            }
        }

        let toastTimer = null;
        function showRefreshToast(message, isError = false) {
            const toast = document.getElementById('refresh-toast');
            if (!toast) return;
            toast.textContent = message;
            toast.classList.toggle('error', isError);
            toast.classList.remove('hidden');
            // trigger transition
            requestAnimationFrame(() => {
                toast.classList.add('show');
            });
            if (toastTimer) clearTimeout(toastTimer);
            toastTimer = setTimeout(() => {
                toast.classList.remove('show');
                setTimeout(() => toast.classList.add('hidden'), 300);
            }, 2500);
        }

        async function manualRefresh() {
            const btn = document.getElementById('refresh-btn');
            const labelSpan = btn ? btn.querySelector('span') : null;
            if (btn && btn.disabled) return;
            
            if (btn) {
                btn.disabled = true;
                btn.classList.add('spinning');
            }
            if (labelSpan) labelSpan.textContent = '更新中...';
            showRefreshToast('最新データを取得中...');
            
            await fetchData();
            
            if (btn) {
                btn.classList.remove('spinning');
                btn.disabled = false;
            }
            if (labelSpan) labelSpan.textContent = '更新';
            const timeStr = (window.lastData && window.lastData.timestamp)
                ? window.lastData.timestamp.split(' ')[1]
                : new Date().toLocaleTimeString('ja-JP', { hour: '2-digit', minute: '2-digit', second: '2-digit' });
            showRefreshToast(`最新データに更新しました (${timeStr})`);
        }

        // Map English location names to readable Japanese
        const EN_TO_JA_MAP = {
            'NISHISHINJUKU': '西新宿',
            'SHINJUKU': '新宿',
            'SHIBUYA': '渋谷',
            'EBISU': '恵比寿',
            'UENO': '上野',
            'MACHIDA': '町田',
            'KASHIWA': '柏',
            'OMIYA': '大宮',
            'YOKOHAMA': '横浜',
            'UTSUNOMIYA': '宇都宮',
            'TAKASAKI': '高崎',
            'SAPPORO': '札幌',
            'SENDAI': '仙台',
            'NAGOYA': '名古屋',
            'SHIZUOKA': '静岡',
            'HAMAMATSU': '浜松',
            'KANAZAWA': '金沢',
            'CHAYAMACHI': '梅田茶屋町',
            'UMEDA': '梅田',
            'NAMBA': '難波',
            'SHINSAIBASHI': '心斎橋',
            'TENMA': '天満',
            'KYOTO': '京都',
            'KOBE': '神戸',
            'OKAYAMA': '岡山',
            'HIROSHIMA': '広島',
            'MATSUYAMA': '松山',
            'FUKUOKA': '福岡天神',
            'KOKURA': '小倉',
            'NAGASAKI': '長崎',
            'OITA': '大分',
            'KUMAMOTO': '熊本',
            'MIYAZAKI': '宮崎',
            'KAGOSHIMA': '鹿児島',
            'OKINAWA': '沖縄',
            'SEOUL': 'ソウル',
            'GANGNAM': '江南(カンナム)',
            'HONGDAE': '弘大(ホンデ)'
        };

        // Map store name keywords to clear Prefecture/Area labels
        const AREA_LABEL_RULES = [
            { keys: ['札幌', 'SAPPORO'], label: '北海道・札幌' },
            { keys: ['仙台', 'SENDAI'], label: '宮城・仙台' },
            { keys: ['西新宿', 'NISHISHINJUKU', '新宿', 'SHINJUKU'], label: '東京・新宿' },
            { keys: ['渋谷', 'SHIBUYA'], label: '東京・渋谷' },
            { keys: ['恵比寿', 'EBISU'], label: '東京・恵比寿' },
            { keys: ['上野', 'UENO'], label: '東京・上野' },
            { keys: ['町田', 'MACHIDA'], label: '東京・町田' },
            { keys: ['横浜', 'YOKOHAMA'], label: '神奈川・横浜' },
            { keys: ['大宮', 'OMIYA'], label: '埼玉・大宮' },
            { keys: ['柏', 'KASHIWA'], label: '千葉・柏' },
            { keys: ['宇都宮', 'UTSUNOMIYA'], label: '栃木・宇都宮' },
            { keys: ['高崎', 'TAKASAKI'], label: '群馬・高崎' },
            { keys: ['名古屋', '錦', '栄', 'NAGOYA'], label: '愛知・名古屋' },
            { keys: ['静岡', 'SHIZUOKA'], label: '静岡' },
            { keys: ['浜松', 'HAMAMATSU'], label: '静岡・浜松' },
            { keys: ['金沢', 'KANAZAWA'], label: '石川・金沢' },
            { keys: ['茶屋町', 'CHAYAMACHI', '梅田', 'UMEDA'], label: '大阪・梅田' },
            { keys: ['天満', 'TENMA'], label: '大阪・天満' },
            { keys: ['心斎橋', 'SHINSAIBASHI'], label: '大阪・心斎橋' },
            { keys: ['難波', 'NAMBA'], label: '大阪・難波' },
            { keys: ['京都', 'KYOTO'], label: '京都' },
            { keys: ['神戸', '三宮', 'KOBE'], label: '兵庫・神戸' },
            { keys: ['岡山', 'OKAYAMA'], label: '岡山' },
            { keys: ['広島', 'HIROSHIMA'], label: '広島' },
            { keys: ['松山', 'MATSUYAMA'], label: '愛媛・松山' },
            { keys: ['福岡', '天神', 'FUKUOKA'], label: '福岡・天神' },
            { keys: ['小倉', 'KOKURA'], label: '福岡・小倉' },
            { keys: ['長崎', 'NAGASAKI'], label: '長崎' },
            { keys: ['大分', 'OITA'], label: '大分' },
            { keys: ['熊本', 'KUMAMOTO'], label: '熊本' },
            { keys: ['宮崎', 'MIYAZAKI'], label: '宮崎' },
            { keys: ['鹿児島', 'KAGOSHIMA'], label: '鹿児島' },
            { keys: ['沖縄', '那覇', 'OKINAWA'], label: '沖縄' },
            { keys: ['ソウル', 'カンナム', 'ホンデ', '江南', '弘大', 'SEOUL', 'GANGNAM', 'HONGDAE'], label: '韓国・ソウル' }
        ];

        // Pre-compiled regex pairs for O(1) setup during localizeStoreName
        const EN_TO_JA_REGEXPS = Object.entries(EN_TO_JA_MAP).map(([enKey, jaVal]) => ({
            regex: new RegExp(`\\b${enKey}\\b`, 'gi'),
            jaVal
        }));

        function localizeStoreName(cleanName) {
            let localized = cleanName.trim();
            const upper = localized.toUpperCase();
            if (EN_TO_JA_MAP[upper]) {
                return EN_TO_JA_MAP[upper];
            }
            if (!/[A-Za-z]/.test(localized)) {
                return localized;
            }
            for (let i = 0; i < EN_TO_JA_REGEXPS.length; i++) {
                const entry = EN_TO_JA_REGEXPS[i];
                localized = localized.replace(entry.regex, entry.jaVal);
            }
            return localized;
        }

        function detectAreaLabel(rawName, localizedName) {
            const master = getStoreMasterInfo(rawName);
            if (master && master.area_label) return master.area_label;
            const combined = `${rawName} ${localizedName}`.toUpperCase();
            for (const rule of AREA_LABEL_RULES) {
                for (const k of rule.keys) {
                    if (combined.includes(k.toUpperCase())) {
                        return rule.label;
                    }
                }
            }
            return '';
        }

        function detectBrandInfo(storeOrName) {
            const rawSource = (typeof storeOrName === 'object' ? (storeOrName.source || '') : '').toUpperCase();
            const name = typeof storeOrName === 'object' ? (storeOrName.name || '') : String(storeOrName || '');
            const cacheKey = `${rawSource}|${name}`;
            if (_brandInfoCache[cacheKey]) {
                return _brandInfoCache[cacheKey];
            }
            let code = 'OTHER';
            let cls = 'brand-other';
            let stripped = name;

            if (rawSource === 'ORIENTAL' || name.startsWith('OLG ') || name.startsWith('ag ')) {
                if (/^OLG\s+ag/i.test(name) || /^ag\s*/i.test(name)) {
                    code = 'ag';
                    cls = 'brand-ag';
                    stripped = name.replace(/^OLG\s+ag\s*/i, '').replace(/^ag\s*/i, '');
                } else {
                    code = 'OLG';
                    cls = 'brand-olg';
                    stripped = name.replace(/^OLG\s+/i, '');
                }
            } else if (rawSource === 'JIS' || name.startsWith('JIS ')) {
                code = 'JIS';
                cls = 'brand-jis';
                stripped = name.replace(/^JIS\s+/i, '');
            } else if (rawSource === 'XIX' || name.startsWith('XIX ')) {
                code = 'XIX';
                cls = 'brand-xix';
                stripped = name.replace(/^XIX\s+/i, '');
            } else if (rawSource === 'ALFA' || name.includes('ALFA')) {
                code = 'ALFA';
                cls = 'brand-alfa';
                stripped = name.replace(/^ALFA\s+/i, '');
            } else if (rawSource === 'YATAKOI' || name.includes('ヤタコイ') || name.includes('YATAKOI')) {
                code = 'ヤタコイ';
                cls = 'brand-yatakoi';
                stripped = name.replace(/^ヤタコイ\s+|^YATAKOI\s+/i, '');
            } else if (rawSource === 'CLOVERS' || name.includes('CLOVERS')) {
                code = 'CLOVERS';
                cls = 'brand-clovers';
                stripped = name.replace(/^CLOVERS\s+/i, '');
            } else {
                const prefix = name.split(' ')[0];
                code = prefix.substring(0, 6);
            }

            const cleanName = localizeStoreName(stripped || name);
            const areaLabel = detectAreaLabel(name, cleanName);
            const fullDisplayTitle = `${code} ${cleanName}`;
            const result = { code, cls, cleanName, areaLabel, fullDisplayTitle };
            _brandInfoCache[cacheKey] = result;
            return result;
        }

        function escapeHtml(str) {
            return String(str)
                .replace(/&/g, '&amp;')
                .replace(/</g, '&lt;')
                .replace(/>/g, '&gt;')
                .replace(/"/g, '&quot;')
                .replace(/'/g, '&#39;');
        }

        function renderStore(store, index, container, isPinned = false, globalIndex = null, isDuplicateName = false) {
            const template = document.getElementById('ranking-item-template');
            const node = template.content.cloneNode(true);
            const item = node.querySelector('.ranking-item');

            const pinBtn = node.querySelector('.pin-btn');
            if (pinnedStores.includes(store.name)) {
                pinBtn.classList.add('active');
            }

            pinBtn.onclick = (e) => {
                e.stopPropagation();
                togglePin(store.name);
            };

            const analysisBtn = node.querySelector('.analysis-btn');
            analysisBtn.onclick = (e) => {
                e.stopPropagation();
                openModal(store.name);
            };

            item.style.cursor = 'pointer';
            item.onclick = () => openModal(store.name);

            const displayRank = globalIndex !== null ? globalIndex + 1 : index + 1;

            if (!isPinned) {
                if (index === 0) item.classList.add('rank-1');
                if (index === 1) item.classList.add('rank-2');
                if (index === 2) item.classList.add('rank-3');
            } else {
                item.classList.add('pinned-item');
            }

            node.querySelector('.rank-badge').textContent = displayRank;

            const brandInfo = detectBrandInfo(store);
            const badgeEl = node.querySelector('.brand-badge');
            badgeEl.textContent = brandInfo.code;
            badgeEl.classList.add(brandInfo.cls);

            const areaEl = node.querySelector('.area-tag');
            if (brandInfo.areaLabel) {
                areaEl.textContent = brandInfo.areaLabel;
            } else {
                areaEl.classList.add('hidden');
            }

            // Distance Tag (GPS Geolocation - ONLY visible when sorted by nearby)
            const distTag = node.querySelector('.distance-tag');
            if (distTag) {
                if (currentSort === 'nearby' && typeof store._distKm === 'number' && userLocation) {
                    distTag.classList.remove('hidden');
                    if (store._distKm < 1.0) {
                        distTag.textContent = `${Math.round(store._distKm * 1000)}m`;
                    } else {
                        distTag.textContent = `${store._distKm.toFixed(1)}km`;
                    }
                } else {
                    distTag.classList.add('hidden');
                }
            }

            // 30-min Momentum Tag (Shows when female count is actively rising)
            const momTag = node.querySelector('.momentum-tag');
            if (momTag) {
                const wDiff = Number(store.women_diff || 0);
                const votes = store.votes || {};
                if (wDiff >= 3) {
                    momTag.classList.remove('hidden');
                    momTag.textContent = `↗ +${wDiff}名`;
                    momTag.title = `直近約30分で女性が${wDiff}名増えています`;
                } else if (votes.smooth > 0 && votes.smooth >= (votes.waiting || 0)) {
                    momTag.classList.remove('hidden');
                    momTag.textContent = `即入店報告`;
                } else {
                    momTag.classList.add('hidden');
                }
            }

            const baseName = brandInfo.cleanName || store.name;
            const storeNameEl = node.querySelector('.store-name');
            storeNameEl.textContent = isDuplicateName
                ? `${brandInfo.code} ${baseName}`
                : baseName;

            // Display event badge ONLY if events are visible and current store has store-specific promotion
            if (shouldShowEvents()) {
                const matchedEvs = getStoreSpecificEvents(store.name);
                if (matchedEvs && matchedEvs.length > 0) {
                    const eventBadge = document.createElement('span');
                    eventBadge.className = 'event-tag-badge';
                    const hasDiscount = matchedEvs.some(e => e.tag === '割引・特典');
                    eventBadge.textContent = hasDiscount ? '特典' : 'フェア';
                    eventBadge.title = matchedEvs[0].title;
                    storeNameEl.appendChild(eventBadge);
                }
            }

            const ladiesCount = store.women || 0;
            const menCount = store.men || 0;
            const totalCount = ladiesCount + menCount;

            node.querySelector('.stat.ladies .count').textContent = ladiesCount;
            node.querySelector('.stat.men .count').textContent = menCount;

            // Gender ratio bar calculation
            const ladiesBar = node.querySelector('.bar-fill-ladies');
            const menBar = node.querySelector('.bar-fill-men');
            if (totalCount > 0) {
                const ladiesPct = Math.round((ladiesCount / totalCount) * 100);
                const menPct = 100 - ladiesPct;
                ladiesBar.style.width = ladiesPct + '%';
                menBar.style.width = menPct + '%';
            } else {
                ladiesBar.style.width = '0%';
                menBar.style.width = '0%';
            }

            container.appendChild(node);
        }

        function _computeUiSignature(data) {
            const stores = (data && data.ranking) || [];
            const storeSig = stores.map(s => {
                const v = s.votes || {};
                return `${s.name}:${s.women}:${s.men}:${s.women_diff || 0}:${v.smooth || 0}:${v.waiting || 0}`;
            }).join('|');
            const locSig = userLocation ? `${userLocation.lat.toFixed(3)},${userLocation.lng.toFixed(3)}` : '';
            const masterCount = window.STORE_MASTER ? Object.keys(window.STORE_MASTER).length : 0;
            const evCount = shouldShowEvents() ? ((window.allEvents && window.allEvents.length) || 0) : -1;
            return `${data.timestamp || ''}#${currentFilter}#${currentSort}#${currentSearch}#${pinnedStores.join(',')}#${locSig}#${masterCount}#${evCount}#${storeSig}`;
        }

        function updateUI(data) {
            if (!data || !data.ranking) return;
            const rankingList = document.getElementById('ranking-list');
            // Render synchronously on the very first paint so there is zero visual delay
            if (rankingList && rankingList.children.length === 0) {
                _performUpdateUI(data);
                return;
            }
            // Coalesce rapid back-to-back updateUI calls (e.g. store_master + status + events arriving in the same tick)
            _pendingUiData = data;
            if (_updateUiRafId !== null) return;
            _updateUiRafId = requestAnimationFrame(() => {
                _updateUiRafId = null;
                if (_pendingUiData) {
                    _performUpdateUI(_pendingUiData);
                }
            });
        }

        function _performUpdateUI(data) {
            const rankingList = document.getElementById('ranking-list');
            const pinnedList = document.getElementById('pinned-list');
            const pinnedContainer = document.getElementById('pinned-list-container');

            const stores = data.ranking;
            if (!stores || stores.length === 0) return;

            const nextSig = _computeUiSignature(data);
            if (nextSig === _lastRenderedSignature && rankingList.children.length > 0) {
                const lastUpdateEl = document.getElementById('last-update');
                if (lastUpdateEl && data.timestamp) {
                    lastUpdateEl.textContent = data.timestamp.split(' ')[1] || '--:--';
                }
                return;
            }
            _lastRenderedSignature = nextSig;

            rankingList.innerHTML = '';
            pinnedList.innerHTML = '';

            // Count occurrences of each cleanName across all stores so duplicate names get their brand prefixed
            const cleanNameCounts = {};
            stores.forEach(s => {
                const bInfo = detectBrandInfo(s);
                const key = bInfo.cleanName || s.name;
                cleanNameCounts[key] = (cleanNameCounts[key] || 0) + 1;
            });

            let filteredStores = stores.filter(store => {
                const matchRegion = currentFilter === 'All' || store.region === currentFilter;
                if (!matchRegion) return false;
                if (currentSearch === '') return true;
                const bInfo = detectBrandInfo(store);
                const searchable = `${store.name} ${bInfo.code} ${bInfo.cleanName} ${bInfo.areaLabel}`.toLowerCase();
                return searchable.includes(currentSearch);
            });

            // Pre-calculate distance if user location is available
            if (userLocation) {
                stores.forEach(s => {
                    const coords = getStoreCoordinates(s);
                    s._distKm = calculateDistanceKm(userLocation.lat, userLocation.lng, coords.lat, coords.lng);
                });
            }

            if (currentSort === 'nearby' && userLocation) {
                filteredStores.sort((a, b) => {
                    const distA = (typeof a._distKm === 'number') ? a._distKm : 99999;
                    const distB = (typeof b._distKm === 'number') ? b._distKm : 99999;
                    if (Math.abs(distA - distB) > 0.05) return distA - distB;
                    return (b.women - a.women) || (b.men - a.men);
                });
            } else if (currentSort === 'men') {
                filteredStores.sort((a, b) => (b.men - a.men) || (b.women - a.women));
            } else if (currentSort === 'ratio') {
                filteredStores.sort((a, b) => {
                    const totalA = a.women + a.men;
                    const totalB = b.women + b.men;
                    const pctA = totalA === 0 ? -1 : (a.women / totalA);
                    const pctB = totalB === 0 ? -1 : (b.women / totalB);
                    if (Math.abs(pctB - pctA) > 0.001) return pctB - pctA;
                    return b.women - a.women;
                });
            } else {
                filteredStores.sort((a, b) => (b.women - a.women) || (b.men - a.men));
            }

            if (filteredStores.length === 0) {
                rankingList.innerHTML = '<p style="color: #666; width: 100%; text-align: center;">該当する店舗がありません</p>';
                pinnedContainer.classList.add('hidden');
                return;
            }

            const pinnedItems = [];
            const otherItems = [];

            filteredStores.forEach((store, realIndex) => {
                const bInfo = detectBrandInfo(store);
                const key = bInfo.cleanName || store.name;
                const isDup = (cleanNameCounts[key] || 0) > 1;
                if (pinnedStores.includes(store.name)) {
                    pinnedItems.push({ store, rank: realIndex, isDup });
                } else {
                    otherItems.push({ store, rank: realIndex, isDup });
                }
            });

            if (pinnedItems.length > 0) {
                pinnedContainer.classList.remove('hidden');
                const pinnedFrag = document.createDocumentFragment();
                pinnedItems.forEach(item => {
                    renderStore(item.store, -1, pinnedFrag, true, item.rank, item.isDup);
                });
                pinnedList.appendChild(pinnedFrag);
            } else {
                pinnedContainer.classList.add('hidden');
            }

            const rankingFrag = document.createDocumentFragment();
            otherItems.forEach((item, idx) => {
                renderStore(item.store, item.rank, rankingFrag, false, item.rank, item.isDup);
                // In staff mode, skip all native in-feed PR cards completely
                if (!IS_STAFF && typeof AFFILIATE_MASTER !== 'undefined' && AFFILIATE_MASTER.enabled) {
                    const currentRank = idx + 1;
                    const slot = AFFILIATE_MASTER.infeedSlots.find(s => s.afterRank === currentRank);
                    if (slot) {
                        const prEl = createInfeedPrElement(slot);
                        if (prEl) rankingFrag.appendChild(prEl);
                    }
                }
            });
            rankingList.appendChild(rankingFrag);

            const lastUpdateEl = document.getElementById('last-update');
            if (lastUpdateEl) {
                lastUpdateEl.textContent = data.timestamp ? data.timestamp.split(' ')[1] : '--:--';
            }
        }

        // --- High-Speed Affiliate & Promotion Master Integration ---
        let currentGuideCategory = 'all';

        function getAffiliateMaster() {
            if (typeof AFFILIATE_MASTER !== 'undefined') {
                return AFFILIATE_MASTER;
            }
            return {
                enabled: !IS_STAFF,
                categories: [
                    { id: 'all', name: 'すべて', icon: '' },
                    { id: 'beauty', name: 'メンズ脱毛・美肌', icon: '', highlight: true },
                    { id: 'dating', name: '恋活・即日出会い', icon: '', highlight: true },
                    { id: 'grooming', name: 'ファッション・身だしなみ', icon: '' }
                ],
                items: [],
                infeedSlots: []
            };
        }

        function createInfeedPrElement(slotConfig) {
            if (IS_STAFF) return null;
            const master = getAffiliateMaster();
            if (!master.enabled) return null;

            const item = (master.items || []).find(i => i.id === slotConfig.itemId) || (master.items || [])[0];
            if (!item) return null;

            const wrapper = document.createElement('a');
            wrapper.className = `ranking-item pr-card-item ${slotConfig.highlight ? 'pr-card-highlight' : ''}`;
            wrapper.href = item.url;
            if (item.url && item.url.startsWith('http')) {
                wrapper.target = '_blank';
                wrapper.rel = 'noopener noreferrer sponsored';
            } else {
                wrapper.onclick = (e) => {
                    e.preventDefault();
                    openGuideModal(item.category);
                };
            }

            const title = item.infeedTitle || item.shortTitle || item.name;
            const tag = item.infeedTag || slotConfig.tagText || item.tag || 'PR';
            const sub = item.infeedSub || slotConfig.subText || item.badge || 'おすすめ';
            const cta = item.infeedCta || '詳細 ›';
            const badge = slotConfig.badgeText || 'PR';

            wrapper.innerHTML = `
                <div class="rank-badge pr-badge-box">${escapeHtml(badge)}</div>
                <div class="store-info">
                    <div class="store-meta-row">
                        <span class="brand-badge pr-tag-pill">${escapeHtml(tag)}</span>
                        <span class="area-tag pr-sub-pill">${escapeHtml(sub)}</span>
                    </div>
                    <h3 class="store-name pr-title">${escapeHtml(title)}</h3>
                </div>
                <div class="pr-cta-box">
                    <span class="pr-cta-btn">${escapeHtml(cta)}</span>
                </div>
                <div class="occupancy-bar pr-occupancy-bar">
                    <div class="bar-fill-ladies" style="width: 70%"></div>
                    <div class="bar-fill-men" style="width: 30%"></div>
                </div>
            `;
            return wrapper;
        }

        function renderModalPrSlot(storeName) {
            if (IS_STAFF) return;
            const slot = document.getElementById('modal-pr-slot');
            if (!slot) return;
            const master = getAffiliateMaster();
            if (!master.enabled || !master.items || master.items.length === 0) {
                slot.classList.add('hidden');
                return;
            }
            slot.classList.remove('hidden');

            // Dynamic rotation based on time of day:
            // 20:00 - 05:59 JST -> Prioritize Dating App (when lounges are busy/full)
            // 06:00 - 19:59 JST -> Prioritize Grooming / Hair Removal
            const currentHour = new Date().getHours();
            const isNightTime = (currentHour >= 20 || currentHour < 6);
            const targetCategory = isNightTime ? 'dating' : 'beauty';

            const matchedItem = (master.items || []).find(i => i.category === targetCategory) || master.items[0];
            const isExternal = matchedItem.url && matchedItem.url.startsWith('http');

            slot.innerHTML = `
                <a href="${escapeHtml(matchedItem.url)}" ${isExternal ? 'target="_blank" rel="noopener noreferrer sponsored"' : ''} class="modal-pr-link" ${!isExternal ? `onclick="event.preventDefault(); openGuideModal('${targetCategory}')"` : ''}>
                    <span class="pr-tag-pill">${escapeHtml(matchedItem.badge || 'PR')}</span>
                    <span class="modal-pr-text">${escapeHtml(matchedItem.shortTitle || matchedItem.name)}</span>
                    <span class="modal-pr-arrow">›</span>
                </a>
            `;
        }

        function openGuideModal(initialCategory = 'all') {
            currentGuideCategory = initialCategory;
            renderGuideModal();
            document.getElementById('guide-modal').classList.remove('hidden');
        }

        function filterGuideCategory(catId) {
            currentGuideCategory = catId;
            renderGuideModal();
        }

        function renderGuideModal() {
            const master = getAffiliateMaster();
            const tabsEl = document.getElementById('guide-category-tabs');
            const listEl = document.getElementById('guide-app-list');

            // Render category tabs
            if (tabsEl && master.categories) {
                tabsEl.innerHTML = master.categories.map(cat => {
                    const isActive = cat.id === currentGuideCategory;
                    const highlightClass = cat.highlight ? 'tab-pill-highlight' : '';
                    const iconHtml = cat.icon ? `<span class="tab-icon">${cat.icon}</span>` : '';
                    return `
                        <button type="button" class="guide-tab-pill ${isActive ? 'active' : ''} ${highlightClass}" onclick="filterGuideCategory('${cat.id}')">
                            ${iconHtml}
                            <span class="tab-label">${escapeHtml(cat.name)}</span>
                        </button>
                    `;
                }).join('');
            }

            // Filter items by category
            let filteredItems = master.items || [];
            if (currentGuideCategory !== 'all') {
                filteredItems = filteredItems.filter(i => i.category === currentGuideCategory);
            }

            // Render item cards
            if (listEl) {
                if (filteredItems.length === 0) {
                    listEl.innerHTML = `<p class="guide-empty-msg">このジャンルの提携プログラムは順次追加予定です。</p>`;
                    return;
                }

                listEl.innerHTML = filteredItems.map(item => {
                    const isExt = item.url && item.url.startsWith('http');
                    const pointsHtml = (item.points && item.points.length > 0) ? `
                        <ul class="guide-points-list">
                            ${item.points.map(p => `<li><span class="check-icon">✓</span> ${escapeHtml(p)}</li>`).join('')}
                        </ul>
                    ` : '';

                    return `
                        <div class="guide-app-card ${item.featured ? 'card-featured' : ''} category-${item.category}">
                            <div class="guide-app-header-row">
                                <span class="guide-app-badge">${escapeHtml(item.badge || 'おすすめ')}</span>
                                <span class="guide-category-tag">${escapeHtml(item.tag || '')}</span>
                            </div>
                            <div class="guide-app-main">
                                <div class="guide-app-info">
                                    <h4 class="guide-app-name">${escapeHtml(item.name)}</h4>
                                    <p class="guide-app-target"><strong>おすすめ：</strong>${escapeHtml(item.target)}</p>
                                    <p class="guide-app-desc">${escapeHtml(item.desc)}</p>
                                    ${pointsHtml}
                                </div>
                                <div class="guide-app-cta-wrap">
                                    <a href="${escapeHtml(item.url)}" ${isExt ? 'target="_blank" rel="noopener noreferrer sponsored"' : `onclick="event.preventDefault(); showRefreshToast('アフィリエイトURL設定完了後に公式ページへ遷移します')"`} class="guide-app-cta">
                                        ${escapeHtml(item.cta || '詳細・キャンペーンを見る ›')}
                                    </a>
                                </div>
                            </div>
                        </div>
                    `;
                }).join('');
            }
        }

        function closeGuideModal() {
            document.getElementById('guide-modal').classList.add('hidden');
        }

        // Mobile Menu / Drawer Modal
        function openMenuModal() {
            const modal = document.getElementById('menu-modal');
            if (modal) {
                modal.classList.remove('hidden');
                document.body.style.overflow = 'hidden';
            }
        }

        function closeMenuModal() {
            const modal = document.getElementById('menu-modal');
            if (modal) {
                modal.classList.add('hidden');
                document.body.style.overflow = '';
            }
        }

        // Editorial Articles Selector Modal
        function openArticlesModal() {
            const modal = document.getElementById('articles-modal');
            if (modal) {
                modal.classList.remove('hidden');
                document.body.style.overflow = 'hidden';
            }
        }

        function closeArticlesModal() {
            const modal = document.getElementById('articles-modal');
            if (modal) {
                modal.classList.add('hidden');
                document.body.style.overflow = '';
            }
        }

        // Beginner / How-To Guide Modal
        function openHowToModal() {
            const modal = document.getElementById('how-to-modal');
            if (modal) {
                modal.classList.remove('hidden');
                document.body.style.overflow = 'hidden';
            }
        }

        function closeHowToModal(fromUserAction = false) {
            const modal = document.getElementById('how-to-modal');
            if (modal) {
                modal.classList.add('hidden');
                document.body.style.overflow = '';
            }
            const cb = document.getElementById('how-to-dont-show-cb');
            if (fromUserAction || (cb && cb.checked)) {
                try {
                    localStorage.setItem('seen_how_to_guide_v1', '1');
                } catch (e) {}
            }
        }

        function getOfficialBrandUrl(brandCode, rawName) {
            const name = String(rawName || '');
            if (brandCode === 'ag' || /^ag\s/i.test(name)) return 'https://ag-ree.jp/';
            if (brandCode === 'OLG') return 'https://oriental-lounge.com/';
            if (brandCode === 'JIS') return 'https://jis.bar/';
            if (brandCode === 'XIX') return 'https://xix-lounge.com/';
            if (brandCode === 'ALFA') return 'https://alfa-lounge.com/';
            if (brandCode === 'ヤタコイ') return 'https://yatakoi.com/';
            if (brandCode === 'CLOVERS') return 'https://clovers-lounge.jp/';
            return 'https://oriental-lounge.com/';
        }

        // --- SNS Share Functions (Ranking & Individual Store) ---
        function buildRankingShareText() {
            const regionNames = {
                'All': '全国', 'Kanto': '関東', 'Kinki': '関西', 'Chubu': '中部',
                'Kyushu': '九州', 'Hokkaido': '北海道', 'Tohoku': '東北',
                'Chugoku': '中国', 'Shikoku': '四国', 'Korea': '韓国'
            };
            const areaTitle = regionNames[currentFilter] || '全国';
            let text = `いま${areaTitle}で人が集まってる相席ラウンジTOP3🍻\n`;
            if (window.lastData && window.lastData.ranking) {
                const list = window.lastData.ranking.filter(s => currentFilter === 'All' || s.region === currentFilter).slice(0, 3);
                const medals = ['🥇', '🥈', '🥉'];
                list.forEach((s, i) => {
                    const b = detectBrandInfo(s);
                    text += `${medals[i]} ${b.fullDisplayTitle}（女性${s.women}名 / 男性${s.men}名）\n`;
                });
            }
            text += `#相席ナビ #相席ラウンジ`;
            return text;
        }

        async function shareCurrentRanking(platform) {
            const shareUrl = window.location.origin + window.location.pathname;
            const shareText = buildRankingShareText();

            if (platform === 'native' && navigator.share) {
                try {
                    await navigator.share({ title: '相席ナビ｜全国ラウンジの今わかる人数ランキング', text: shareText, url: shareUrl });
                    return;
                } catch (e) {}
            }
            if (platform === 'line') {
                const lineUrl = `https://social-plugins.line.me/lineit/share?url=${encodeURIComponent(shareUrl)}&text=${encodeURIComponent(shareText)}`;
                window.open(lineUrl, '_blank', 'noopener,noreferrer');
            } else if (platform === 'x' || platform === 'native') {
                const xUrl = `https://twitter.com/intent/tweet?text=${encodeURIComponent(shareText + '\n' + shareUrl)}`;
                window.open(xUrl, '_blank', 'noopener,noreferrer');
            } else if (platform === 'copy') {
                try {
                    await navigator.clipboard.writeText(`${shareText}\n${shareUrl}`);
                    showRefreshToast('✓ ランキングとURLをコピーしました');
                } catch (e) {
                    showRefreshToast('URL: ' + shareUrl);
                }
            }
        }

        let currentModalStoreName = '';

        function shareStoreStatus(platform) {
            trackEvent('share', platform);
            const shareUrl = window.location.origin + window.location.pathname;
            const bInfo = detectBrandInfo(currentModalStoreName);
            let liveInfo = '';
            if (window.lastData && window.lastData.ranking) {
                const found = window.lastData.ranking.find(s => s.name === currentModalStoreName);
                if (found) {
                    liveInfo = `いま女性${found.women}名 / 男性${found.men}名が入店中🍸`;
                }
            }

            // Check if store has active events from Step 1 crawler (prioritize store-specific)
            const evs = getStoreSpecificEvents(currentModalStoreName);
            let eventText = '';
            if (evs && evs.length > 0) {
                const topEv = evs[0];
                eventText = `\n🎉 開催中イベント: ${topEv.title}`;
            }

            const cleanArea = (bInfo.cleanName || '').replace(/店$/, '').trim();
            const areaTag = cleanArea ? ` #${cleanArea}相席` : '';
            const brandTag = bInfo.code ? ` #${bInfo.code}` : '';
            const tags = `#相席ナビ${brandTag}${areaTag} #相席ラウンジ`;
            const text = `${bInfo.fullDisplayTitle}、${liveInfo}${eventText}\n現在の人数と時間帯別の傾向グラフはこちら👇\n${shareUrl}\n${tags}`;

            if (platform === 'line') {
                const lineUrl = `https://social-plugins.line.me/lineit/share?url=${encodeURIComponent(shareUrl)}&text=${encodeURIComponent(text)}`;
                window.open(lineUrl, '_blank', 'noopener,noreferrer');
            } else if (platform === 'copy') {
                try {
                    navigator.clipboard.writeText(text).then(() => {
                        showRefreshToast('✓ 店舗の人数テキストをコピーしました');
                    }).catch(() => {
                        showRefreshToast('URL: ' + shareUrl);
                    });
                } catch (e) {
                    showRefreshToast('URL: ' + shareUrl);
                }
            } else {
                const xUrl = `https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}`;
                window.open(xUrl, '_blank', 'noopener,noreferrer');
            }
        }

        function openLegalModal() {
            document.getElementById('legal-modal').classList.remove('hidden');
        }

        function closeLegalModal() {
            document.getElementById('legal-modal').classList.add('hidden');
        }

        // --- SNS Viral Post Generator Modal Logic ---
        function generatePresetText(preset) {
            const shareUrl = window.location.origin + window.location.pathname;
            const stores = (window.lastData && window.lastData.ranking) ? [...window.lastData.ranking] : [];
            const ts = (window.lastData && window.lastData.timestamp) ? window.lastData.timestamp.split(' ')[1].slice(0, 5) : '現在';

            if (stores.length === 0) {
                return `全国の相席ラウンジ現在人数「相席ナビ」\n${shareUrl}\n#相席ナビ #相席ラウンジ`;
            }

            const medals = ['🥇', '🥈', '🥉'];
            let list = [];
            let header = '';
            let subnote = '';
            let tags = '#相席ナビ #相席ラウンジ';
            let isAnaba = false;
            let showEvents = true;

            if (preset === 'events') {
                const eventStores = stores.filter(s => {
                    const evs = getStoreSpecificEvents(s.name);
                    return evs && evs.length > 0;
                }).sort((a, b) => (b.women - a.women) || (b.men - a.men));

                if (eventStores.length > 0) {
                    list = eventStores.slice(0, 3);
                    header = `${ts}時点、今日イベント・特典をやっていて人が集まってる3店舗🎉`;
                    subnote = `キャンペーン中で普段より雰囲気良さそうな店舗はこちら👇`;
                    tags = `#相席ナビ #相席ラウンジ #イベント`;
                } else {
                    list = stores.slice().sort((a, b) => (b.women - a.women) || (b.men - a.men)).slice(0, 3);
                    header = `${ts}時点、いま女性が多く入っている上位3店舗🍻`;
                    tags = `#相席ナビ #相席ラウンジ`;
                }
            } else if (preset === 'timely') {
                const now = new Date();
                const jstHour = (now.getUTCHours() + 9) % 24;

                if (jstHour >= 17 && jstHour < 20) {
                    header = `${ts}時点、仕事帰りに待ち時間なしで入りやすい3店舗🍸`;
                    subnote = `混み始める前の早い時間から人が入っている店舗です👇`;
                    tags = `#相席ナビ #相席ラウンジ #仕事帰り`;
                    list = stores.slice().sort((a, b) => (b.women - a.women) || (b.men - a.men)).slice(0, 3);
                } else if (jstHour >= 20 && jstHour < 24) {
                    header = `${ts}時点、いま一番盛り上がっている上位3店舗🍻`;
                    subnote = `ピーク時間で人が増えているので、向かう前に人数チェック推奨です👇`;
                    tags = `#相席ナビ #相席ラウンジ`;
                    list = stores.slice().sort((a, b) => (b.women - a.women) || (b.men - a.men)).slice(0, 3);
                } else if (jstHour >= 0 && jstHour < 6) {
                    header = `${ts}時点、終電後もまだ賑わっている3店舗🌙`;
                    subnote = `2軒目・3軒目で朝まで飲めるお店を探している方はこちら👇`;
                    tags = `#相席ナビ #相席ラウンジ #深夜営業`;
                    list = stores.filter(s => (s.women || 0) + (s.men || 0) > 0)
                                 .sort((a, b) => (b.women - a.women) || (b.men - a.men))
                                 .slice(0, 3);
                    if (list.length === 0) list = stores.slice(0, 3);
                } else {
                    header = `${ts}時点、いま人が入っている注目3店舗☀️`;
                    subnote = `今日どこ行くか迷っている方は、各店の現在人数を参考にどうぞ👇`;
                    tags = `#相席ナビ #相席ラウンジ`;
                    list = stores.slice().sort((a, b) => (b.women - a.women) || (b.men - a.men)).slice(0, 3);
                }
            } else if (preset === 'kanto') {
                list = stores.filter(s => s.region === 'Kanto').sort((a, b) => (b.women - a.women) || (b.men - a.men)).slice(0, 3);
                header = `${ts}時点、関東でいま女性が多い上位3店舗🗼`;
                tags = `#相席ナビ #相席ラウンジ #新宿 #渋谷`;
            } else if (preset === 'kansai') {
                list = stores.filter(s => s.region === 'Kinki').sort((a, b) => (b.women - a.women) || (b.men - a.men)).slice(0, 3);
                header = `${ts}時点、関西でいま女性が多い上位3店舗🐙`;
                tags = `#相席ナビ #相席ラウンジ #梅田 #難波`;
            } else if (preset === 'anaba') {
                isAnaba = true;
                list = stores.filter(s => (s.women || 0) >= 8).sort((a, b) => {
                    const rA = a.women / Math.max(1, a.women + a.men);
                    const rB = b.women / Math.max(1, b.women + b.men);
                    return (rB - rA) || (b.women - a.women);
                }).slice(0, 3);
                header = `${ts}時点、いま女性比率が高めで入りやすい穴場3店舗✨`;
                tags = `#相席ナビ #相席ラウンジ #穴場`;
            } else {
                list = stores.slice().sort((a, b) => (b.women - a.women) || (b.men - a.men)).slice(0, 3);
                header = `${ts}時点、いま女性が多く入っている上位3店舗🍻`;
                tags = `#相席ナビ #相席ラウンジ`;
            }

            const lines = [header];
            if (subnote) {
                lines.push(subnote);
            }

            list.forEach((s, i) => {
                const b = detectBrandInfo(s);
                const w = s.women || 0;
                const m = s.men || 0;
                const tot = w + m;
                const pct = tot > 0 ? Math.round((w / tot) * 100) : 0;
                
                let lineStr = '';
                if (isAnaba) {
                    lineStr = `${medals[i]} ${b.fullDisplayTitle}（女${pct}%｜女性${w} / 男性${m}）`;
                } else {
                    lineStr = `${medals[i]} ${b.fullDisplayTitle}（女性${w}名 / 男性${m}名）`;
                }

                if (showEvents) {
                    const matchedEvs = getStoreSpecificEvents(s.name);
                    if (matchedEvs && matchedEvs.length > 0) {
                        const topEv = matchedEvs[0];
                        lineStr += `\n   └ 📢 ${topEv.title}`;
                    }
                }
                lines.push(lineStr);
            });

            lines.push(`\n全国の現在人数・男女比はこちら👇\n${shareUrl}\n${tags}`);
            return lines.join('\n');
        }

        function selectSnsPreset(preset) {
            document.querySelectorAll('.sns-tab-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.preset === preset);
            });
            const ta = document.getElementById('sns-preview-textarea');
            if (ta) {
                ta.value = generatePresetText(preset);
            }
        }

        function openSnsModal(initialPreset) {
            const evStores = (window.lastData && window.lastData.ranking) 
                ? window.lastData.ranking.filter(s => getStoreSpecificEvents(s.name).length > 0) 
                : [];
            let defaultPreset = initialPreset;
            if (!defaultPreset) {
                if (evStores.length > 0) {
                    defaultPreset = 'events';
                } else if (currentFilter === 'Kanto') {
                    defaultPreset = 'kanto';
                } else if (currentFilter === 'Kinki') {
                    defaultPreset = 'kansai';
                } else {
                    defaultPreset = 'timely';
                }
            }
            selectSnsPreset(defaultPreset);
            document.getElementById('sns-modal').classList.remove('hidden');
        }

        function closeSnsModal() {
            document.getElementById('sns-modal').classList.add('hidden');
        }

        async function postGeneratedSns(platform) {
            const ta = document.getElementById('sns-preview-textarea');
            const text = ta ? ta.value : generatePresetText('events');
            if (platform === 'x') {
                const xUrl = `https://twitter.com/intent/tweet?text=${encodeURIComponent(text)}`;
                window.open(xUrl, '_blank', 'noopener,noreferrer');
            } else if (platform === 'line') {
                const lineUrl = `https://social-plugins.line.me/lineit/share?text=${encodeURIComponent(text)}`;
                window.open(lineUrl, '_blank', 'noopener,noreferrer');
            } else {
                try {
                    await navigator.clipboard.writeText(text);
                    showRefreshToast('✓ 速報テキストをコピーしました');
                } catch (e) {}
            }
        }

        function showError() {
            document.getElementById('loading').classList.add('hidden');
            document.getElementById('content').classList.add('hidden');
            document.getElementById('error-message').classList.remove('hidden');
        }

        // Initial fetch (if already rendered from fresh server-embedded data, skip redundant initial /api/status roundtrip)
        if (_renderedFromSSR) {
            loadEvents();
        } else {
            fetchData();
        }

        // Poll every 30 seconds
        setInterval(fetchData, 30000);

        // --- Analysis Modal Logic ---
        let dailyChartInstance = null;
        let weekdayAnalysisChartInstance = null;
        let hourlyWeekdayChartInstance = null;
        let currentAnalysisData = null;
        let allDates = [];
        let visibleDateCount = 7;
        let currentData = null;

        async function openModal(storeName) {
            loadChartJs(); // Pre-trigger Chart.js download in parallel with data fetch
            currentModalStoreName = storeName;
            trackEvent('store_open', storeName);
            const modal = document.getElementById('analysis-modal');
            const modalContent = modal.querySelector('.modal-content');
            const title = document.getElementById('modal-store-name');
            const banner = document.getElementById('live-comparison-banner');
            const bInfo = detectBrandInfo(storeName);

            const officialUrl = getOfficialBrandUrl(bInfo.code, storeName);
            const officialLinkEl = document.getElementById('modal-official-link');
            if (officialLinkEl) officialLinkEl.href = officialUrl;
            const topOfficialLinkEl = document.getElementById('modal-top-official-link');
            if (topOfficialLinkEl) topOfficialLinkEl.href = officialUrl;
            renderModalPrSlot(storeName);
            renderModalEvents(storeName);
            renderModalCrowdVotes(storeName);
            renderModalSecondVenues(storeName);

            modal.classList.remove('hidden');
            if (modalContent) modalContent.scrollTop = 0;

            // Check frontend cache first for instant (0ms) display
            const cached = clientAnalysisCache[storeName];
            if (cached && (Date.now() - cached.fetchedAt < CLIENT_CACHE_TTL)) {
                title.textContent = bInfo.fullDisplayTitle;
                currentAnalysisData = cached.data;
                await renderCharts(cached.data);
                return;
            }

            title.textContent = bInfo.fullDisplayTitle + " 読み込み中...";
            banner.classList.add('hidden');

            try {
                const response = await fetch('/api/analysis/' + encodeURIComponent(storeName));
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();

                if (data.error) {
                    title.textContent = "Error: " + data.error;
                    return;
                }

                clientAnalysisCache[storeName] = { data: data, fetchedAt: Date.now() };
                title.textContent = bInfo.fullDisplayTitle;
                currentAnalysisData = data;
                await renderCharts(data);

            } catch (e) {
                console.error(e);
                title.textContent = "データの取得に失敗しました";
            }
        }

        function closeModal() {
            document.getElementById('analysis-modal').classList.add('hidden');
            if (dailyChartInstance) { dailyChartInstance.destroy(); dailyChartInstance = null; }
            if (weekdayAnalysisChartInstance) { weekdayAnalysisChartInstance.destroy(); weekdayAnalysisChartInstance = null; }
            if (hourlyWeekdayChartInstance) { hourlyWeekdayChartInstance.destroy(); hourlyWeekdayChartInstance = null; }
            currentAnalysisData = null;
        }

        // --- Live User Crowd Votes (UGC) & Store-Pinpoint 2nd Venue Map Search (anchor from store_master.json) ---
        function getFullBrandStoreQuery(bInfo) {
            const brandFull = bInfo.code === 'OLG' ? 'オリエンタルラウンジ' : bInfo.code;
            return `${brandFull}${bInfo.cleanName} ${bInfo.areaLabel || ''}`.trim();
        }

        function renderModalSecondVenues(storeName) {
            const headingEl = document.getElementById('modal-second-venue-heading');
            const cafeBtn = document.getElementById('modal-map-cafe-btn');
            const barBtn = document.getElementById('modal-map-bar-btn');
            if (!cafeBtn && !barBtn) return;

            const bInfo = detectBrandInfo(storeName);
            const rawKey = String(storeName || '').trim();
            const master = getStoreMasterInfo(rawKey);
            const storeAnchor = (master && master.map_anchor) || getFullBrandStoreQuery(bInfo);
            const shortStoreLabel = `${bInfo.code} ${bInfo.cleanName}`;
            const storeLabel = String(bInfo.cleanName || '').endsWith('店') ? bInfo.cleanName : `${bInfo.cleanName}店`;

            if (headingEl) {
                headingEl.textContent = `${shortStoreLabel} の近くで2軒目を探す`;
            }
            if (cafeBtn) {
                cafeBtn.textContent = `${storeLabel}近くの夜カフェをマップ検索 ↗`;
                cafeBtn.href = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(storeAnchor + ' 近く 夜カフェ スイーツ カフェバー')}`;
            }
            if (barBtn) {
                barBtn.textContent = `${storeLabel}近くのバー・バルをマップ検索 ↗`;
                barBtn.href = `https://www.google.com/maps/search/?api=1&query=${encodeURIComponent(storeAnchor + ' 近く 隠れ家バー ワインバル ダイニングバー')}`;
            }
        }

        function updateVoteNumbersUI(votes) {
            const v = votes || { smooth: 0, waiting: 0, lively: 0 };
            const sEl = document.getElementById('vote-count-smooth');
            const wEl = document.getElementById('vote-count-waiting');
            const lEl = document.getElementById('vote-count-lively');
            if (sEl) sEl.textContent = v.smooth || 0;
            if (wEl) wEl.textContent = v.waiting || 0;
            if (lEl) lEl.textContent = v.lively || 0;
        }

        function renderModalCrowdVotes(storeName) {
            const feedbackEl = document.getElementById('modal-vote-feedback');
            if (feedbackEl) feedbackEl.classList.add('hidden');

            // Votes are already embedded in each store object from /api/status (and updated every 30s)
            let initialVotes = { smooth: 0, waiting: 0, lively: 0 };
            if (window.lastData && Array.isArray(window.lastData.ranking)) {
                const found = window.lastData.ranking.find(s => s.name === storeName);
                if (found && found.votes) initialVotes = found.votes;
            }
            updateVoteNumbersUI(initialVotes);
        }

        // Anonymous per-device ID (random, stored only in this browser) used for duplicate-vote prevention.
        // More reliable than IP on mobile networks where many users share one IP.
        function getVoterId() {
            try {
                let id = localStorage.getItem('aiseki_voter_id');
                if (!id) {
                    id = (window.crypto && crypto.randomUUID) ? crypto.randomUUID() : (Date.now().toString(36) + Math.random().toString(36).slice(2));
                    localStorage.setItem('aiseki_voter_id', id);
                }
                return id;
            } catch (e) {
                return '';
            }
        }

        async function submitCrowdVote(voteType) {
            if (!currentModalStoreName) return;
            const feedbackEl = document.getElementById('modal-vote-feedback');
            try {
                const res = await fetch('/api/vote', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                        store_name: currentModalStoreName,
                        vote_type: voteType,
                        voter_id: getVoterId()
                    })
                });
                const d = await res.json();
                if (d && d.votes) {
                    updateVoteNumbersUI(d.votes);
                    if (window.lastData && Array.isArray(window.lastData.ranking)) {
                        const found = window.lastData.ranking.find(s => s.name === currentModalStoreName);
                        if (found) found.votes = d.votes;
                    }
                }
                if (feedbackEl) {
                    feedbackEl.classList.remove('hidden');
                    if (d.status === 'duplicate' && d.reason === 'rate_limited') {
                        feedbackEl.textContent = '短時間に多くの投票があったため、少し時間をおいてお試しください';
                    } else if (d.status === 'duplicate') {
                        feedbackEl.textContent = '✓ 先ほど投票済みです（ご協力ありがとうございます）';
                    } else {
                        feedbackEl.textContent = '✓ 現場状況を共有しました。ありがとうございます';
                    }
                }
            } catch (e) {
                if (feedbackEl) {
                    feedbackEl.classList.remove('hidden');
                    feedbackEl.textContent = '通信エラーが発生しました';
                }
            }
        }
        window.submitCrowdVote = submitCrowdVote;

        // Render events relevant to the current store inside analysis modal
        function renderModalEvents(storeName) {
            const section = document.getElementById('modal-event-section');
            const list = document.getElementById('modal-event-list');
            if (!section || !list) return;

            // In staff mode with events disabled, hide event section completely
            if (!shouldShowEvents()) {
                section.classList.add('hidden');
                list.innerHTML = '';
                return;
            }

            // ONLY show genuine store-specific events for this store (NO fallback to global/all-store events)
            const rawEvs = getStoreSpecificEvents(storeName);
            if (!rawEvs || rawEvs.length === 0) {
                section.classList.add('hidden');
                list.innerHTML = '';
                return;
            }

            // Deduplicate events by title (strip bracketed store tag e.g. 【名古屋栄】 to prevent identical duplicate cards)
            const seenTitles = new Set();
            const evs = [];
            for (const ev of rawEvs) {
                const baseTitle = String(ev.title || '').replace(/【[^】]+】/g, '').trim();
                if (baseTitle && seenTitles.has(baseTitle)) continue;
                if (baseTitle) seenTitles.add(baseTitle);
                evs.push(ev);
            }

            // Smart Sort:
            // 1. Women-specific events first (so ladies perks like JCB gifts / food free are at the top)
            // 2. High-value discounts & perks next
            // 3. Campaigns & general events
            const sortedEvs = [...evs].sort((a, b) => {
                const aWomen = (a.gender === 'women' || (a.title && a.title.includes('女性'))) ? 2 : 0;
                const bWomen = (b.gender === 'women' || (b.title && b.title.includes('女性'))) ? 2 : 0;
                if (bWomen !== aWomen) return bWomen - aWomen;
                const aDisc = a.tag === '割引・特典' ? 1 : 0;
                const bDisc = b.tag === '割引・特典' ? 1 : 0;
                if (bDisc !== aDisc) return bDisc - aDisc;
                return 0;
            });

            let html = '';
            // Render ALL store events (no cut-off)
            sortedEvs.forEach(ev => {
                let bannerHtml = '';
                if (ev.banner_url) {
                    bannerHtml = `
                        <div class="event-banner-wrap">
                            <img src="${escapeHtml(ev.banner_url)}" alt="${escapeHtml(ev.title)}" class="event-banner-img" loading="lazy" decoding="async" onerror="this.closest('.event-item-card').classList.remove('has-banner'); this.parentElement.remove();">
                        </div>
                    `;
                }

                let genderBadge = '';
                if (ev.gender === 'women' || (ev.title && ev.title.includes('女性'))) {
                    genderBadge = '<span class="event-pill-tag tag-women" style="background:rgba(216,161,170,0.14); color:#d8a1aa; border:1px solid rgba(216,161,170,0.3); font-weight:700;">女性限定</span>';
                } else if (ev.gender === 'men' || (ev.title && (ev.title.includes('男性') || ev.title.includes('🚹')))) {
                    genderBadge = '<span class="event-pill-tag tag-men" style="background:rgba(147,177,201,0.14); color:#93b1c9; border:1px solid rgba(147,177,201,0.3); font-weight:700;">男性限定</span>';
                }

                html += `
                    <div class="event-item-card ${ev.banner_url ? 'has-banner' : ''}">
                        ${bannerHtml}
                        <div class="event-item-main">
                            <div class="event-item-left">
                                <div class="event-item-title">${escapeHtml(ev.title)}</div>
                                <div class="event-item-meta">
                                    <span class="event-pill-tag ${escapeHtml(ev.tag_cls || 'tag-event')}">${escapeHtml(ev.tag || 'イベント')}</span>
                                    ${genderBadge}
                                    <span class="event-store-label">${escapeHtml(ev.target_label || '対象店舗')}</span>
                                    <span style="color:var(--text-secondary); font-size:0.72rem;">${escapeHtml(ev.date_str || '')}</span>
                                </div>
                            </div>
                            <a href="${escapeHtml(ev.url)}" target="_blank" rel="noopener noreferrer" class="event-cta-btn" onclick="event.stopPropagation(); window.open(this.href, '_blank'); return true;">
                                <span>公式詳細</span>
                                <span>↗</span>
                            </a>
                        </div>
                    </div>
                `;
            });
            list.innerHTML = html;
            section.classList.remove('hidden');
        }

        // --- All Events Modal Logic ---
        let currentEventsFilter = 'ALL';

        function renderAllEventsList(filterCategory = 'ALL') {
            currentEventsFilter = filterCategory;
            const container = document.getElementById('all-events-container');
            if (!container) return;

            if (!window.allEvents || window.allEvents.length === 0) {
                container.innerHTML = '<p style="text-align:center; color:var(--text-secondary); padding:20px;">現在公開中のイベント情報はありません。</p>';
                return;
            }

            let filtered = window.allEvents;
            if (filterCategory === 'JIS') {
                filtered = window.allEvents.filter(ev => ev.brand === 'JIS');
            } else if (filterCategory === 'OLG') {
                filtered = window.allEvents.filter(ev => ev.brand === 'OLG');
            } else if (filterCategory === 'DISCOUNT') {
                filtered = window.allEvents.filter(ev => ev.tag === '割引・特典');
            } else if (filterCategory === 'WOMEN') {
                filtered = window.allEvents.filter(ev => ev.gender === 'women' || (ev.title && ev.title.includes('女性')));
            }

            if (filtered.length === 0) {
                container.innerHTML = '<p style="text-align:center; color:var(--text-secondary); padding:20px;">該当するイベントはありません。</p>';
                return;
            }

            let html = '';
            filtered.forEach(ev => {
                let bannerHtml = '';
                if (ev.banner_url) {
                    bannerHtml = `
                        <div class="event-banner-wrap">
                            <img src="${escapeHtml(ev.banner_url)}" alt="${escapeHtml(ev.title)}" class="event-banner-img" loading="lazy" decoding="async" onerror="this.closest('.event-item-card').classList.remove('has-banner'); this.parentElement.remove();">
                        </div>
                    `;
                }

                let genderBadge = '';
                if (ev.gender === 'women' || (ev.title && ev.title.includes('女性'))) {
                    genderBadge = '<span class="event-pill-tag tag-women" style="background:rgba(216,161,170,0.14); color:#d8a1aa; border:1px solid rgba(216,161,170,0.3); font-size:0.68rem; padding:1px 5px;">女性限定</span>';
                } else if (ev.gender === 'men' || (ev.title && (ev.title.includes('男性') || ev.title.includes('🚹')))) {
                    genderBadge = '<span class="event-pill-tag tag-men" style="background:rgba(147,177,201,0.14); color:#93b1c9; border:1px solid rgba(147,177,201,0.3); font-size:0.68rem; padding:1px 5px;">男性限定</span>';
                }

                html += `
                    <div class="event-item-card ${ev.banner_url ? 'has-banner' : ''}">
                        ${bannerHtml}
                        <div class="event-item-main">
                            <div class="event-item-left">
                                <div class="event-item-title">${escapeHtml(ev.title)}</div>
                                <div class="event-item-meta">
                                    <span class="event-pill-tag ${escapeHtml(ev.tag_cls || 'tag-event')}">${escapeHtml(ev.tag || 'イベント')}</span>
                                    ${genderBadge}
                                    <span class="brand-badge ${ev.brand === 'OLG' ? 'brand-olg' : 'brand-jis'}" style="font-size:0.68rem; padding:1px 5px;">${escapeHtml(ev.brand)}</span>
                                    <span class="event-store-label">${escapeHtml(ev.target_label || '全店')}</span>
                                    <span style="color:var(--text-secondary); font-size:0.72rem;">${escapeHtml(ev.date_str || '')}</span>
                                </div>
                            </div>
                            <a href="${escapeHtml(ev.url)}" target="_blank" rel="noopener noreferrer" class="event-cta-btn" onclick="event.stopPropagation(); window.open(this.href, '_blank'); return true;">
                                <span>公式詳細</span>
                                <span>↗</span>
                            </a>
                        </div>
                    </div>
                `;
            });
            container.innerHTML = html;
        }

        function filterEventsModal(category, btnEl) {
            document.querySelectorAll('#events-category-tabs .events-tab-btn').forEach(btn => {
                btn.classList.remove('active');
            });
            if (btnEl) btnEl.classList.add('active');
            renderAllEventsList(category);
        }

        async function openEventsModal() {
            const modal = document.getElementById('events-modal');
            const container = document.getElementById('all-events-container');
            modal.classList.remove('hidden');

            if (!window.allEvents || window.allEvents.length === 0) {
                container.innerHTML = '<div style="text-align:center; padding:20px;"><div class="spinner" style="margin:0 auto 10px auto;"></div>最新イベント＆各店限定バナーを読み込み中...</div>';
                await loadEvents();
            }

            renderAllEventsList('ALL');
            document.querySelectorAll('#events-category-tabs .events-tab-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.filter === 'ALL');
            });
        }

        function closeEventsModal() {
            document.getElementById('events-modal').classList.add('hidden');
        }

        // Close on clicking background
        document.getElementById('analysis-modal').addEventListener('click', (e) => {
            if (e.target.id === ('analysis-modal')) closeModal();
        });
        document.getElementById('comparison-modal').addEventListener('click', (e) => {
            if (e.target.id === ('comparison-modal')) closeComparisonModal();
        });
        document.getElementById('legal-modal').addEventListener('click', (e) => {
            if (e.target.id === ('legal-modal')) closeLegalModal();
        });
        document.getElementById('guide-modal').addEventListener('click', (e) => {
            if (e.target.id === ('guide-modal')) closeGuideModal();
        });
        document.getElementById('articles-modal')?.addEventListener('click', (e) => {
            if (e.target.id === ('articles-modal')) closeArticlesModal();
        });
        document.getElementById('sns-modal').addEventListener('click', (e) => {
            if (e.target.id === ('sns-modal')) closeSnsModal();
        });
        document.getElementById('events-modal')?.addEventListener('click', (e) => {
            if (e.target.id === ('events-modal')) closeEventsModal();
        });
        document.getElementById('how-to-modal')?.addEventListener('click', (e) => {
            if (e.target.id === ('how-to-modal')) closeHowToModal();
        });
        document.getElementById('menu-modal')?.addEventListener('click', (e) => {
            if (e.target.id === ('menu-modal')) closeMenuModal();
        });

        // Close any active modal with ESC key
        document.addEventListener('keydown', (e) => {
            if (e.key === 'Escape') {
                closeModal();
                closeComparisonModal();
                closeLegalModal();
                closeGuideModal();
                closeArticlesModal();
                closeMenuModal();
                closeSnsModal();
                closeEventsModal();
                closeHowToModal();
            }
        });

        // Helper to adapt Chart colors dynamically based on light/dark mode
        function getChartThemeColors() {
            const isLight = document.body.classList.contains('theme-light');
            return {
                textColor: isLight ? '#1f1e1b' : '#ebe8e3',
                subTextColor: isLight ? '#6b665e' : '#9b978f',
                gridColor: isLight ? 'rgba(28, 27, 24, 0.08)' : 'rgba(255, 255, 255, 0.08)',
                axisLineColor: isLight ? 'rgba(28, 27, 24, 0.15)' : 'rgba(255, 255, 255, 0.15)',
                womenBar: isLight ? '#c98994' : 'rgba(216, 161, 170, 0.85)',
                menBar: isLight ? '#7d9cb8' : 'rgba(147, 177, 201, 0.85)',
                womenLine: isLight ? '#b05a68' : '#d8a1aa',
                womenFill: isLight ? 'rgba(176, 90, 104, 0.15)' : 'rgba(216, 161, 170, 0.16)',
                menLine: isLight ? '#4a6f8f' : '#93b1c9',
                menFill: isLight ? 'rgba(74, 111, 143, 0.15)' : 'rgba(147, 177, 201, 0.16)'
            };
        }

        // --- Robust Chart.js Loader (self-hosted /static/chart.min.js, 100% immune to adblockers) ---
        let _chartJsLoadingPromise = null;
        function loadChartJs() {
            if (window.Chart) return Promise.resolve(window.Chart);
            if (!_chartJsLoadingPromise) {
                _chartJsLoadingPromise = new Promise((resolve, reject) => {
                    const existing = document.querySelector('script[src*="chart.min.js"]') || document.querySelector('script[src*="chart.js"]');
                    if (existing) {
                        let checks = 0;
                        const interval = setInterval(() => {
                            if (window.Chart) {
                                clearInterval(interval);
                                resolve(window.Chart);
                            } else if (++checks > 60) {
                                clearInterval(interval);
                                injectScript();
                            }
                        }, 50);
                        existing.addEventListener('load', () => {
                            clearInterval(interval);
                            resolve(window.Chart);
                        }, { once: true });
                        existing.addEventListener('error', () => {
                            clearInterval(interval);
                            injectScript();
                        }, { once: true });
                    } else {
                        injectScript();
                    }

                    function injectScript() {
                        const script = document.createElement('script');
                        script.src = '/static/chart.min.js?v=20261021';
                        script.async = true;
                        script.onload = () => resolve(window.Chart);
                        script.onerror = (err) => {
                            _chartJsLoadingPromise = null;
                            console.error('Failed to load local Chart.js:', err);
                            reject(err);
                        };
                        document.head.appendChild(script);
                    }
                });
            }
            return _chartJsLoadingPromise;
        }

        // Warm up Chart.js quietly 3.5s after initial render so the first store modal click opens with 0ms script lag
        setTimeout(() => {
            if ('requestIdleCallback' in window) {
                window.requestIdleCallback(() => loadChartJs(), { timeout: 4000 });
            } else {
                loadChartJs();
            }
        }, 3500);

        // --- Comparison Modal Logic ---
        let comparisonChartInstance = null;

        async function openComparisonModal() {
            const modal = document.getElementById('comparison-modal');
            const summaryContainer = document.getElementById('comparison-brand-summary');
            modal.classList.remove('hidden');

            if (summaryContainer) {
                summaryContainer.innerHTML = '<div style="text-align:center; padding:15px; color:var(--text-secondary);"><div class="spinner" style="margin:0 auto 8px auto;"></div>系列データを集計中...</div>';
            }

            // Ensure we have ranking data
            if (!window.lastData || !window.lastData.ranking || window.lastData.ranking.length === 0) {
                try {
                    const res = await fetch('/api/status');
                    if (res.ok) {
                        window.lastData = await res.json();
                    }
                } catch (e) {
                    console.error('Failed to load status for comparison:', e);
                }
            }

            const ranking = (window.lastData && window.lastData.ranking) ? window.lastData.ranking : [];
            let storesToCompare = ranking;
            if (currentFilter !== 'All') {
                const filtered = storesToCompare.filter(s => s.region === currentFilter);
                if (filtered.length > 0) {
                    storesToCompare = filtered;
                }
            }

            const brandTotals = {};
            storesToCompare.forEach(store => {
                const brand = (store.source || 'Other').toUpperCase();
                if (!brandTotals[brand]) {
                    brandTotals[brand] = { women: 0, men: 0, count: 0 };
                }
                brandTotals[brand].women += (store.women || 0);
                brandTotals[brand].men += (store.men || 0);
                brandTotals[brand].count += 1;
            });

            const brands = Object.keys(brandTotals).sort((a, b) => brandTotals[b].women - brandTotals[a].women);

            // 1. Instant HTML Brand Summary Cards (Works 100% independently of Chart.js)
            if (summaryContainer) {
                if (brands.length === 0) {
                    summaryContainer.innerHTML = '<p style="text-align:center; color:var(--text-secondary);">該当する店舗データがありません</p>';
                } else {
                    let cardsHtml = '';
                    brands.forEach(b => {
                        const info = brandTotals[b];
                        const total = info.women + info.men;
                        const ratio = total > 0 ? Math.round((info.women / total) * 100) : 0;
                        cardsHtml += `
                            <div class="brand-summary-card">
                                <div class="brand-card-title">
                                    <span>${escapeHtml(b)}</span>
                                    <span style="font-size:0.7rem; font-weight:normal; opacity:0.7;">(${info.count}店舗)</span>
                                </div>
                                <div class="brand-card-counts">
                                    <span class="w-count">♀ ${info.women}</span>
                                    <span class="m-count">♂ ${info.men}</span>
                                </div>
                                <div class="brand-card-sub">女性比率 ${ratio}% ｜ 計 ${total}名</div>
                            </div>
                        `;
                    });
                    summaryContainer.innerHTML = cardsHtml;
                }
            }

            // 2. Load Chart.js and render bar chart
            try {
                await loadChartJs();
            } catch (err) {
                console.error('Failed to load Chart.js:', err);
            }

            if (!window.Chart) {
                console.warn('Chart.js not available yet for comparison chart');
                return;
            }

            const labels = brands.map(b => `${b} (${brandTotals[b].count}店舗)`);
            const womenData = brands.map(b => brandTotals[b].women);
            const menData = brands.map(b => brandTotals[b].men);

            if (comparisonChartInstance) {
                comparisonChartInstance.destroy();
                comparisonChartInstance = null;
            }

            const th = getChartThemeColors();
            const canvas = document.getElementById('comparisonChart');
            if (!canvas) return;
            const ctx = canvas.getContext('2d');

            comparisonChartInstance = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'Ladies',
                            data: womenData,
                            backgroundColor: th.womenBar,
                            borderRadius: 4
                        },
                        {
                            label: 'Men',
                            data: menData,
                            backgroundColor: th.menBar,
                            borderRadius: 4
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    animation: { duration: 250 },
                    scales: {
                        x: {
                            stacked: false,
                            grid: { display: false },
                            ticks: { color: th.textColor, font: { weight: '600' } }
                        },
                        y: {
                            stacked: false,
                            grid: { color: th.gridColor },
                            ticks: { color: th.subTextColor }
                        }
                    },
                    plugins: {
                        legend: { display: true, labels: { color: th.textColor, boxWidth: 14 } }
                    }
                }
            });
        }

        function closeComparisonModal() {
            document.getElementById('comparison-modal').classList.add('hidden');
            if (comparisonChartInstance) { comparisonChartInstance.destroy(); comparisonChartInstance = null; }
        }

        // --- Weekly Report Modal Logic ---
        async function openReportModal() {
            const modal = document.getElementById('report-modal');
            const content = document.getElementById('report-content');
            const period = document.getElementById('report-period');
            
            modal.classList.remove('hidden');
            content.innerHTML = '<div class="spinner"></div>';
            period.textContent = '集計中...';

            try {
                const response = await fetch('/api/weekly-report');
                if (!response.ok) throw new Error(`HTTP ${response.status}`);
                const data = await response.json();

                if (data.status !== 'success') {
                    content.innerHTML = `<p style="color: var(--accent-red); text-align: center;">Error: ${escapeHtml(data.error)}</p>`;
                    return;
                }

                const report = data.data;
                period.textContent = `集計期間: ${report.period}`;

                let html = `
                    <h3 style="margin-top: 0;">ブランド別サマリー（日別ピーク平均の合計）</h3>
                    <table class="report-table">
                        <tr>
                            <th>Brand</th>
                            <th>店舗数</th>
                            <th>平均 女性</th>
                            <th>平均 男性</th>
                        </tr>
                `;
                for (const [brand, stats] of Object.entries(report.by_brand)) {
                    html += `
                        <tr>
                            <td>${escapeHtml(brand)}</td>
                            <td>${Number(stats.store_count)}</td>
                            <td style="color:#ffb7b7; font-weight:600;">${Number(stats.total_avg_women)}</td>
                            <td style="color:#b7caff;">${Number(stats.total_avg_men)}</td>
                        </tr>
                    `;
                }
                html += `</table>
                    <h3 style="margin-top: 20px;">店舗別ピークランキング</h3>
                    <table class="report-table">
                        <tr>
                            <th>店舗名</th>
                            <th>Brand</th>
                            <th>平均 女性</th>
                            <th>平均 男性</th>
                            <th>最高 女性</th>
                            <th>ピーク時間</th>
                            <th>稼働日</th>
                        </tr>
                `;
                report.stores.forEach(store => {
                    html += `
                        <tr>
                            <td>${escapeHtml(store.name)}</td>
                            <td>${escapeHtml(store.brand)}</td>
                            <td style="color:#ffb7b7; font-weight:600;">${Number(store.avg_women)}</td>
                            <td style="color:#b7caff;">${Number(store.avg_men)}</td>
                            <td>${Number(store.max_women)}</td>
                            <td>${escapeHtml(store.peak_hour)}</td>
                            <td>${Number(store.days_with_data)}日</td>
                        </tr>
                    `;
                });
                html += '</table>';
                content.innerHTML = html;

            } catch (e) {
                console.error(e);
                content.innerHTML = '<p style="color: var(--accent-red); text-align: center;">レポートの読み込みに失敗しました</p>';
            }
        }

        function closeReportModal() {
            document.getElementById('report-modal').classList.add('hidden');
        }

        document.getElementById('report-modal').addEventListener('click', (e) => {
            if (e.target.id === ('report-modal')) closeReportModal();
        });

        function renderLiveComparisonBanner(data) {
            const banner = document.getElementById('live-comparison-banner');
            const liveStore = (window.lastData && window.lastData.ranking)
                ? window.lastData.ranking.find(s => s.name === data.store_name)
                : null;
            if (!liveStore) {
                banner.classList.add('hidden');
                return;
            }

            // Compute current JST business day and hour
            const now = new Date();
            const currentHour = now.getHours();
            const bizDate = new Date(now);
            if (currentHour <= 6) {
                bizDate.setDate(bizDate.getDate() - 1);
            }
            const jsDay = bizDate.getDay(); // 0=Sun, 1=Mon...
            const dayKeys = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
            const dayLabels = ['日曜', '月曜', '火曜', '水曜', '木曜', '金曜', '土曜'];
            const dayKey = dayKeys[jsDay];
            const dayLabel = dayLabels[jsDay];

            const avgW = (data.hourly_by_weekday && data.hourly_by_weekday[dayKey] && data.hourly_by_weekday[dayKey][currentHour] !== undefined)
                ? data.hourly_by_weekday[dayKey][currentHour]
                : (data.hourly ? (data.hourly[currentHour] || 0) : 0);

            const diffW = +(liveStore.women - avgW).toFixed(1);
            let diffBadge = '';
            if (avgW > 0 || liveStore.women > 0) {
                if (diffW >= 5) {
                    diffBadge = `<span class="diff-badge hot">普段の${dayLabel}${currentHour}時台平均(${avgW}人)より +${diffW}人 ↗</span>`;
                } else if (diffW > 0) {
                    diffBadge = `<span class="diff-badge up">普段の${dayLabel}${currentHour}時台平均(${avgW}人)より +${diffW}人 ↗</span>`;
                } else if (diffW < 0) {
                    diffBadge = `<span class="diff-badge down">普段の${dayLabel}${currentHour}時台平均(${avgW}人)より ${diffW}人</span>`;
                } else {
                    diffBadge = `<span class="diff-badge neutral">普段の${dayLabel}${currentHour}時台平均(${avgW}人)並み</span>`;
                }
            }

            banner.innerHTML = `
                <div class="live-banner-counts">
                    <span>現在: <strong class="ladies-text">女性 ${liveStore.women}名</strong> / <strong class="men-text">男性 ${liveStore.men}名</strong></span>
                    ${diffBadge}
                </div>
            `;
            banner.classList.remove('hidden');
        }

        async function renderCharts(data) {
            renderLiveComparisonBanner(data);
            generateDateButtons(data);

            try {
                await loadChartJs();
            } catch (err) {
                console.error('Failed to load Chart.js:', err);
            }

            if (window.Chart) {
                renderWeekdayChart(data);

                // Auto-select current business weekday for hourly chart
                const now = new Date();
                if (now.getHours() <= 6) now.setDate(now.getDate() - 1);
                const dayKeys = ['Sun', 'Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat'];
                selectHourlyWeekday(dayKeys[now.getDay()]);
            }
        }

        function renderWeekdayChart(data) {
            if (!window.Chart) return;
            if (weekdayAnalysisChartInstance) {
                weekdayAnalysisChartInstance.destroy();
            }
            const ctx = document.getElementById('weekdayChart').getContext('2d');
            
            const days = ['Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun'];
            const displayDays = ['月', '火', '水', '木', '金', '土', '日'];
            
            const womenData = days.map(d => data.weekday[d] || 0);
            const menData = days.map(d => data.weekday_men ? (data.weekday_men[d] || 0) : 0);

            const th = getChartThemeColors();
            weekdayAnalysisChartInstance = new Chart(ctx, {
                type: 'bar',
                data: {
                    labels: displayDays,
                    datasets: [
                        {
                            label: 'Ladies (ピーク平均)',
                            data: womenData,
                            backgroundColor: th.womenBar,
                            borderRadius: 4
                        },
                        {
                            label: 'Men (ピーク平均)',
                            data: menData,
                            backgroundColor: th.menBar,
                            borderRadius: 4
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    onClick: (evt, elements) => {
                        if (elements && elements.length > 0) {
                            const idx = elements[0].index;
                            selectHourlyWeekday(days[idx]);
                        }
                    },
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { color: th.textColor, font: { weight: '600' } }
                        },
                        y: {
                            beginAtZero: true,
                            grid: { color: th.gridColor },
                            ticks: { color: th.subTextColor }
                        }
                    },
                    plugins: {
                        legend: { display: true, labels: { color: th.textColor, boxWidth: 12 } }
                    }
                }
            });
        }

        function selectHourlyWeekday(dayKey) {
            if (!currentAnalysisData) return;

            document.querySelectorAll('#weekday-filter-buttons .chart-filter-btn').forEach(btn => {
                btn.classList.toggle('active', btn.dataset.day === dayKey);
            });

            if (!window.Chart) return;

            // Business hours order: 18:00 -> 23:00, 00:00 -> 06:00
            const businessHours = [18, 19, 20, 21, 22, 23, 0, 1, 2, 3, 4, 5, 6];
            const labels = businessHours.map(h => `${h}時`);

            let womenSeries = [];
            let menSeries = [];

            if (dayKey === 'ALL') {
                womenSeries = businessHours.map(h => (currentAnalysisData.hourly && currentAnalysisData.hourly[h]) || 0);
                menSeries = businessHours.map(h => (currentAnalysisData.hourly_men && currentAnalysisData.hourly_men[h]) || 0);
            } else {
                const dayW = (currentAnalysisData.hourly_by_weekday && currentAnalysisData.hourly_by_weekday[dayKey]) || {};
                const dayM = (currentAnalysisData.hourly_men_by_weekday && currentAnalysisData.hourly_men_by_weekday[dayKey]) || {};
                womenSeries = businessHours.map(h => dayW[h] || 0);
                menSeries = businessHours.map(h => dayM[h] || 0);
            }

            if (hourlyWeekdayChartInstance) {
                hourlyWeekdayChartInstance.destroy();
            }

            const th = getChartThemeColors();
            const ctx = document.getElementById('hourlyWeekdayChart').getContext('2d');
            hourlyWeekdayChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'Ladies 平均',
                            data: womenSeries,
                            backgroundColor: th.womenFill,
                            borderColor: th.womenLine,
                            borderWidth: 2,
                            pointRadius: 2.5,
                            fill: true,
                            tension: 0.25
                        },
                        {
                            label: 'Men 平均',
                            data: menSeries,
                            backgroundColor: th.menFill,
                            borderColor: th.menLine,
                            borderWidth: 2,
                            pointRadius: 2.5,
                            fill: true,
                            tension: 0.25
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        x: {
                            grid: { display: false },
                            ticks: { color: th.textColor }
                        },
                        y: {
                            beginAtZero: true,
                            grid: { color: th.gridColor },
                            ticks: { color: th.subTextColor }
                        }
                    },
                    plugins: {
                        legend: { display: true, labels: { color: th.textColor, boxWidth: 12 } },
                        tooltip: { mode: 'index', intersect: false }
                    },
                    interaction: { mode: 'nearest', axis: 'x', intersect: false }
                }
            });
        }

        function generateDateButtons(data) {
            currentData = data;
            const container = document.getElementById('date-buttons');
            container.innerHTML = '';

            const dates = new Set();
            (data.recent_trend || []).forEach(d => {
                const dateStr = d.x.split(' ')[0];
                dates.add(dateStr);
            });

            allDates = Array.from(dates).sort().reverse();
            visibleDateCount = 7;

            renderDateButtons(true);
        }

        function renderDateButtons(autoSelectFirst = false) {
            const container = document.getElementById('date-buttons');
            container.innerHTML = '';
            const dayNames = ['日', '月', '火', '水', '木', '金', '土'];

            const visibleDates = allDates.slice(0, visibleDateCount);

            visibleDates.forEach((dateStr, idx) => {
                const parts = dateStr.split('-');
                const date = new Date(parseInt(parts[0]), parseInt(parts[1]) - 1, parseInt(parts[2]));
                const dayName = dayNames[date.getDay()];

                const btn = document.createElement('button');
                btn.className = 'date-btn';
                if (autoSelectFirst && idx === 0) btn.classList.add('active');
                btn.innerHTML = `${parts[1]}/${parts[2]}<span class="day-name">${dayName}</span>`;
                btn.onclick = (e) => showDailyChart(dateStr, currentData, e);
                container.appendChild(btn);
            });

            if (allDates.length > visibleDateCount) {
                const loadMoreBtn = document.createElement('button');
                loadMoreBtn.className = 'date-btn load-more-btn';
                loadMoreBtn.innerHTML = `もっと見る<span class="day-name">+${allDates.length - visibleDateCount}</span>`;
                loadMoreBtn.onclick = () => {
                    visibleDateCount += 7;
                    renderDateButtons(false);
                };
                container.appendChild(loadMoreBtn);
            }

            if (autoSelectFirst && visibleDates.length > 0) {
                showDailyChart(visibleDates[0], currentData, null);
            }
        }

        async function showDailyChart(dateStr, data, evt) {
            if (evt && evt.target) {
                document.querySelectorAll('.date-btn').forEach(b => b.classList.remove('active'));
                evt.target.closest('.date-btn').classList.add('active');
            }

            document.getElementById('daily-chart-container').classList.remove('hidden');

            try {
                if (!window.Chart) await loadChartJs();
            } catch (e) {
                console.error(e);
            }
            if (!window.Chart) return;

            // Filter data for selected date
            const womenData = (data.recent_trend || []).filter(d => d.x.startsWith(dateStr));
            const menData = (data.recent_trend_men || []).filter(d => d.x.startsWith(dateStr));

            // Create labels (time only)
            const labels = womenData.map(d => {
                const timeParts = d.x.split(' ')[1].split(':');
                return `${timeParts[0]}:${timeParts[1]}`;
            });
            const womenValues = womenData.map(d => d.y);
            const menValues = menData.map(d => d.y);

            if (dailyChartInstance) dailyChartInstance.destroy();

            const th = getChartThemeColors();
            const ctx = document.getElementById('dailyChart').getContext('2d');
            dailyChartInstance = new Chart(ctx, {
                type: 'line',
                data: {
                    labels: labels,
                    datasets: [
                        {
                            label: 'Ladies',
                            data: womenValues,
                            backgroundColor: th.womenFill,
                            borderColor: th.womenLine,
                            borderWidth: 1.5,
                            pointRadius: 2,
                            fill: true,
                            tension: 0.1
                        },
                        {
                            label: 'Men',
                            data: menValues,
                            backgroundColor: th.menFill,
                            borderColor: th.menLine,
                            borderWidth: 1.5,
                            pointRadius: 2,
                            fill: true,
                            tension: 0.1
                        }
                    ]
                },
                options: {
                    responsive: true,
                    maintainAspectRatio: false,
                    scales: {
                        y: {
                            beginAtZero: true,
                            grid: { color: th.gridColor },
                            ticks: { color: th.subTextColor }
                        },
                        x: {
                            grid: { display: false },
                            ticks: {
                                color: th.textColor,
                                maxTicksLimit: 12,
                                maxRotation: 0,
                                autoSkip: true
                            }
                        }
                    },
                    plugins: {
                        legend: { display: true, labels: { color: th.textColor, boxWidth: 12 } },
                        tooltip: {
                            mode: 'index',
                            intersect: false,
                        }
                    },
                    interaction: {
                        mode: 'nearest',
                        axis: 'x',
                        intersect: false
                    }
                }
            });
        }

        // Pull to refresh logic
        let touchstartY = 0;
        let touchendY = 0;
        const pullThreshold = 80;
        let isPulling = false;

        const pullIndicator = document.getElementById('pull-indicator');

        document.addEventListener('touchstart', e => {
            if (window.scrollY === 0) {
                touchstartY = e.changedTouches[0].screenY;
                isPulling = true;
            }
        }, {passive: true});

        document.addEventListener('touchmove', e => {
            if (!isPulling) return;
            const y = e.changedTouches[0].screenY;
            if (y > touchstartY + pullThreshold) {
                pullIndicator.classList.add('show');
            } else {
                pullIndicator.classList.remove('show');
            }
        }, {passive: true});

        document.addEventListener('touchend', e => {
            if (!isPulling) return;
            touchendY = e.changedTouches[0].screenY;
            isPulling = false;
            
            if (touchendY > touchstartY + pullThreshold && window.scrollY === 0) {
                // Trigger refresh
                manualRefresh();
                setTimeout(() => {
                    pullIndicator.classList.remove('show');
                }, 1000); // Hide after a bit
            } else {
                pullIndicator.classList.remove('show');
            }
        });

        // Kick off background events fetch on page startup
        if ('requestIdleCallback' in window) {
            window.requestIdleCallback(() => loadEvents(), { timeout: 1500 });
        } else {
            setTimeout(loadEvents, 800);
        }

        // ==========================================
        // PWA Installation & Add to Home Banner Control
        // ==========================================
        let deferredPwaPrompt = null;

        window.addEventListener('beforeinstallprompt', (e) => {
            // Prevent Chrome mini-infobar on mobile
            e.preventDefault();
            deferredPwaPrompt = e;
            checkPwaBannerVisibility();
        });

        function isPwaStandalone() {
            return (window.matchMedia && window.matchMedia('(display-mode: standalone)').matches) ||
                   (window.navigator.standalone === true);
        }

        function isIosDevice() {
            const ua = window.navigator.userAgent.toLowerCase();
            return /iphone|ipad|ipod/.test(ua);
        }

        function checkPwaBannerVisibility() {
            if (IS_STAFF) return; // Hide for staff mode
            if (isPwaStandalone()) return; // Already installed as PWA

            const banner = document.getElementById('pwa-install-banner');
            if (!banner) return;

            // Check if dismissed within the last 7 days
            try {
                const dismissedAt = localStorage.getItem('pwa_dismissed_at');
                if (dismissedAt) {
                    const elapsed = Date.now() - parseInt(dismissedAt, 10);
                    if (elapsed < 7 * 24 * 60 * 60 * 1000) return;
                }
            } catch (e) {}

            banner.classList.remove('hidden');
        }

        function triggerPwaInstall() {
            if (deferredPwaPrompt) {
                deferredPwaPrompt.prompt();
                deferredPwaPrompt.userChoice.then((choiceResult) => {
                    if (choiceResult.outcome === 'accepted') {
                        dismissPwaBanner();
                    }
                    deferredPwaPrompt = null;
                });
            } else if (isIosDevice()) {
                openPwaGuideModal();
            } else {
                openPwaGuideModal();
            }
        }

        function dismissPwaBanner() {
            const banner = document.getElementById('pwa-install-banner');
            if (banner) banner.classList.add('hidden');
            try {
                localStorage.setItem('pwa_dismissed_at', Date.now().toString());
            } catch (e) {}
        }

        function openPwaGuideModal() {
            const modal = document.getElementById('pwa-guide-modal');
            if (modal) modal.classList.remove('hidden');
        }

        function closePwaGuideModal() {
            const modal = document.getElementById('pwa-guide-modal');
            if (modal) modal.classList.add('hidden');
        }

        // Initialize PWA banner check shortly after page load
        setTimeout(checkPwaBannerVisibility, 2000);
