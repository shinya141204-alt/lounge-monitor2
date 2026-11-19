import copy
import datetime
import glob
import json
import os
import re
import shutil
import tempfile
import unittest
from unittest import mock

import app as app_module

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TEMPLATES_DIR = os.path.join(REPO_ROOT, "templates")

DOM_EVENT_ATTRS = frozenset({
    "onabort", "onblur", "onchange", "onclick", "oncontextmenu", "ondblclick",
    "onerror", "onfocus", "oninput", "oninvalid", "onkeydown", "onkeypress",
    "onkeyup", "onload", "onmousedown", "onmouseenter", "onmouseleave",
    "onmousemove", "onmouseout", "onmouseover", "onmouseup", "onreset",
    "onresize", "onscroll", "onselect", "onsubmit", "ontoggle", "onwheel",
})
ON_ATTR_RE = re.compile(r'(?<![a-zA-Z0-9_-])(on[a-z]+)\s*=\s*["\']', re.I)

PER_FILE_ON_ATTR_CAPS = {
    "index.html": 135,
    "staff_sns_editorial.html": 44,
    "staff_column_editorial.html": 30,
    "price_comparison.html": 21,
    "staff_analytics.html": 18,
    "_iso_underground_map.html": 10,
    "stats_report.html": 7,
    "staff_sns_kit.html": 5,
    "concierge.html": 3,
    "_master_nav.html": 1,
    "_portal_nav.html": 1,
    "columns.html": 1,
    "error.html": 1,
    "guide_article.html": 1,
    "guide_comparison.html": 1,
    "guide_weekday.html": 1,
    "staff_login.html": 1,
    "staff_pdca.html": 0,
    "store_detail.html": 0,
}


def _read_text(path: str) -> str:
    with open(path, encoding="utf-8") as f:
        return f.read()


def _count_dom_on_attrs(html_text: str) -> int:
    return sum(
        1 for m in ON_ATTR_RE.finditer(html_text)
        if m.group(1).lower() in DOM_EVENT_ATTRS
    )


class TestM3UiSecurityAndCls(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="m3_test_")
        self._orig_db = getattr(app_module.local_db, "DB_PATH", None)
        app_module.local_db.DB_PATH = os.path.join(self._tmpdir, "test.db")
        app_module.local_db.init_db()
        with app_module.data_lock:
            self._saved_latest = copy.deepcopy(app_module.latest_data)
        self.client = app_module.app.test_client()

    def tearDown(self):
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        if self._orig_db is not None:
            app_module.local_db.DB_PATH = self._orig_db
        app_module.clear_fast_http_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_m3_p1_02_store_detail_dom_xss_eliminated_and_delegated(self):
        """M3-P1-02: store_detail.html から escapeJs / inline onclick copyDetailText を撤廃し data-* 委譲へ安全化"""
        sd_txt = _read_text(os.path.join(TEMPLATES_DIR, "store_detail.html"))

        self.assertEqual(_count_dom_on_attrs(sd_txt), 0)
        self.assertNotIn("function escapeJs", sd_txt)
        self.assertNotIn("escapeJs(", sd_txt)
        self.assertNotIn('onclick="copyDetailText', sd_txt)
        self.assertIn('data-copy-text="{{ v.name }}"', sd_txt)
        self.assertIn('data-copy-text="${escapeHtml(v.name)}"', sd_txt)
        self.assertIn('data-copy-text="${escapeHtml(t.phrase || \'\')}"', sd_txt)
        self.assertIn("target.closest('[data-copy-text]')", sd_txt)
        self.assertIn("target.closest('[data-detail-action]')", sd_txt)
        self.assertIn("btn.textContent = '✓ コピー完了'", sd_txt)

        resp = self.client.get("/store/oriental-lounge-shinjuku")
        self.assertEqual(resp.status_code, 200)
        html = resp.get_data(as_text=True)
        resp.close()
        self.assertIn("/static/store_detail.css?v=1", html)
        self.assertNotIn("escapeJs(", html)
        for legacy_inline in (
            'onclick="copyDetailText',
            'onclick="submitDetailVote',
            'onclick="submitDetailWaitMinutes',
            'onclick="armStoreWatchIntent',
            'onclick="toggleBackgroundPushWatch',
            'onclick="switchNearbySlot',
            'onclick="switchDetailSecondTab',
        ):
            self.assertNotIn(legacy_inline, html)

    def test_m3_p1_03_staff_pdca_noopener_and_safe_paths(self):
        """M3-P1-03: staff_pdca.html / _master_nav.html の target=_blank に rel=noopener noreferrer と JS/Jinja2 多層オープンリダイレクト拒否があること"""
        pdca_txt = _read_text(os.path.join(TEMPLATES_DIR, "staff_pdca.html"))
        nav_txt = _read_text(os.path.join(TEMPLATES_DIR, "_master_nav.html"))

        self.assertEqual(_count_dom_on_attrs(pdca_txt), 0)
        for txt_name, txt in (("staff_pdca.html", pdca_txt), ("_master_nav.html", nav_txt)):
            blank_tags = re.findall(r"<a\b[^>]*target=[\"']_blank[\"'][^>]*>", txt, flags=re.I)
            self.assertGreaterEqual(len(blank_tags), 2, txt_name)
            for tag in blank_tags:
                self.assertRegex(tag, r'rel=["\'][^"\']*\bnoopener\b[^"\']*["\']', txt_name)
                self.assertRegex(tag, r'rel=["\'][^"\']*\bnoreferrer\b[^"\']*["\']', txt_name)
        self.assertIn("a.rel = 'noopener noreferrer'", pdca_txt)
        self.assertIn("function isSafePdcaInternalPath(p)", pdca_txt)
        self.assertIn("if (!isSafePdcaInternalPath(p)) return;", pdca_txt)
        self.assertIn("p.includes('\\\\')", pdca_txt)
        self.assertIn("u.origin === window.location.origin", pdca_txt)
        self.assertIn("/[\\x00-\\x1f\\x7f]/", pdca_txt)
        self.assertIn("/%5c/i", pdca_txt)
        self.assertIn("data-pdca-action", pdca_txt)
        self.assertIn("data-pdca-change", pdca_txt)

        # Jinja2 SSR 5箇所すべてで /\ や制御文字・%5c・//・外部URL・javascript: を拒否し、/guide/shinjuku のみリンク描画すること
        import pdca_engine
        real_pdca = pdca_engine.run_pdca_cycle(
            live_stores=[], days=14, apply_actions=False, use_ai=False, record_log=False
        )
        evil_payloads = [
            "/\\evil.example.com",
            "\\/evil.example.com",
            "/%5cevil.example.com",
            "/%5Cevil.example.com",
            "/\n/evil.example.com",
            "/\r/evil.example.com",
            "/\t/evil.example.com",
            "/\x00/evil.example.com",
            "//evil.example.com",
            "https://evil.com",
            "javascript:alert(1)",
        ]
        safe_path = "/guide/shinjuku"
        injected = copy.deepcopy(real_pdca)
        diag = injected.setdefault("diagnostics", {})
        act = injected.setdefault("active_directives", {})
        act["priority_column_paths"] = [safe_path] + evil_payloads
        diag["offer_breakdown"] = [
            {"category_key": "safe", "label": "SAFE", "clicks": 5, "share_pct": 50, "bar_pct": 50, "top_source_path": safe_path},
        ] + [
            {"category_key": f"e{i}", "label": f"EVIL{i}", "clicks": 1, "share_pct": 5, "bar_pct": 5, "top_source_path": ep}
            for i, ep in enumerate(evil_payloads)
        ]
        diag["monetization_gaps"] = [
            {"path": safe_path, "title": "SAFE_GAP", "pv": 100, "aff_clicks": 0, "cvr_pct": 0.0},
        ] + [
            {"path": ep, "title": f"EVIL_GAP_{i}", "pv": 100, "aff_clicks": 0, "cvr_pct": 0.0}
            for i, ep in enumerate(evil_payloads)
        ]
        perf_rows = [
            {"path": safe_path, "title": "SAFE_PERF", "status_badge": "OK", "pv": 100, "aff_clicks": 2, "ctr_pct": 2.0}
        ] + [
            {"path": ep, "title": f"EVIL_PERF_{i}", "status_badge": "OK", "pv": 10, "aff_clicks": 0, "ctr_pct": 0.0}
            for i, ep in enumerate(evil_payloads)
        ]
        diag["article_perf"] = perf_rows[:6]
        diag["all_articles_perf"] = perf_rows

        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True
        with mock.patch("pdca_engine.run_pdca_cycle", return_value=injected):
            resp = self.client.get("/staff/pdca")
            self.assertEqual(resp.status_code, 200)
            html = resp.get_data(as_text=True)
            resp.close()

        self.assertIn('href="/guide/shinjuku" target="_blank" rel="noopener noreferrer"', html)
        for ep in evil_payloads:
            self.assertNotIn(f'href="{ep}"', html)
            self.assertNotIn(f"href='{ep}'", html)
        self.assertNotIn("evil.example.com\" target=\"_blank\"", html)
        self.assertNotIn("evil.com\" target=\"_blank\"", html)
        self.assertNotIn("javascript:alert(1)\" target=\"_blank\"", html)
        for tag in re.findall(r"<a\b[^>]*target=[\"']_blank[\"'][^>]*>", html, flags=re.I):
            self.assertRegex(tag, r'rel=["\'][^"\']*\bnoopener\b[^"\']*["\']')
            self.assertRegex(tag, r'rel=["\'][^"\']*\bnoreferrer\b[^"\']*["\']')

    def test_m3_p2_01_inline_on_attr_monotonic_ratchet(self):
        """M3-P2-01: 全テンプレートの DOM on*= 属性が総数 <= 281 かつファイル別上限を超えないこと"""
        total = 0
        for p in sorted(glob.glob(os.path.join(TEMPLATES_DIR, "*.html"))):
            fn = os.path.basename(p)
            txt = _read_text(p)
            cnt = _count_dom_on_attrs(txt)
            total += cnt
            cap = PER_FILE_ON_ATTR_CAPS.get(fn, 0)
            self.assertLessEqual(
                cnt,
                cap,
                f"templates/{fn} has {cnt} inline on*= attributes (cap={cap}); use addEventListener delegation instead.",
            )
        self.assertLessEqual(total, 281, f"Total inline on*= attributes regressed to {total} (> 281)")

    def test_m3_p1_04_nav_loader_staff_test_isolation_without_session_storage(self):
        """M3-P1-04: _nav_loader.html が ?staff_test=1 時にプリフェッチ抑止・遷移引継ぎを行い sessionStorage へ永続保存しないこと"""
        nav_txt = _read_text(os.path.join(TEMPLATES_DIR, "_nav_loader.html"))
        self.assertIn("function hasStaffTestQuery()", nav_txt)
        self.assertIn("if (IS_STAFF_VIEW || hasStaffTestQuery()) return false;", nav_txt)
        self.assertIn("if (IS_STAFF_VIEW || hasStaffTestQuery()) return;", nav_txt)
        self.assertIn("destUrl.searchParams.set('staff_test', '1')", nav_txt)
        self.assertNotIn("touchstart", nav_txt)
        self.assertIn("/sw.js?v=122", nav_txt)
        self.assertNotRegex(nav_txt, r"(sessionStorage|localStorage)\.(setItem|getItem)\([^)]*staff_test")

    def test_m3_p1_04_guide_externalization_and_metadata_preserved(self):
        """M3-P1-04: 全37 guide_*.html が guide.css?v=1, meta color-scheme, fetchpriority=high を保持すること"""
        guide_css = _read_text(os.path.join(REPO_ROOT, "static", "guide.css"))
        store_css = _read_text(os.path.join(REPO_ROOT, "static", "store_detail.css"))
        for css_txt in (guide_css, store_css):
            self.assertNotIn("{{", css_txt)
            self.assertNotIn("{%", css_txt)

        guides = sorted(glob.glob(os.path.join(TEMPLATES_DIR, "guide_*.html")))
        self.assertEqual(len(guides), 37)
        for gp in guides:
            txt = _read_text(gp)
            fn = os.path.basename(gp)
            self.assertIn('<meta name="color-scheme" content="dark light">', txt, fn)
            self.assertIn('/static/guide.css?v=1', txt, fn)
            self.assertIn('fetchpriority="high"', txt, fn)
            self.assertNotIn('html.theme-light{background-color:#f6f6f7;}', txt, fn)

        idx_txt = _read_text(os.path.join(TEMPLATES_DIR, "index.html"))
        self.assertNotIn('href="/staff/logout"', idx_txt)
        self.assertIn('action="/staff/logout"', idx_txt)
        self.assertIn("var isAllClosed = dayEl && !dayEl.classList.contains('hidden');", idx_txt)
        self.assertIn("(wCnt + mCnt) > 0", idx_txt)

    def test_m3_p1_01_ssr_csr_predicate_consistency_across_5_states(self):
        """M3-P1-01: app.js と SSR (5状態: 昼間全閉/夜間営業/halted/seed/staff) の all_closed_mode 述語が一致すること"""
        app_js = _read_text(os.path.join(REPO_ROOT, "static", "app.js"))
        self.assertIn("function _isKoreaStore(", app_js)
        self.assertIn("_isPreopenZeroStore(s) && !_isKoreaStore(s)", app_js)
        self.assertIn("!data.is_seed && domStores.length > 0", app_js)
        self.assertIn("!data.is_seed && allDomStores.length > 0 && allDomStores.every(s => _isPreopenZeroStore(s))", app_js)
        self.assertIn("staleState === 'live' && !data.is_seed && !daytime10m", app_js)
        self.assertIn("const initStaleState = syncStaleHeaderAndBanner(window.INITIAL_STATUS_DATA);", app_js)
        self.assertIn("_computeUiSignature(window.INITIAL_STATUS_DATA) + `#stale:${initStaleState}`", app_js)
        self.assertIn("_computeUiSignature(window.lastData) + `#stale:${staleState}`", app_js)
        self.assertIn("const canHydrateSsrSeedDom =", app_js)
        self.assertNotIn("initFastPortalNavigation", app_js)

        now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
        live_ts = (now_jst - datetime.timedelta(seconds=20)).strftime("%Y-%m-%d %H:%M:%S")
        halted_ts = (now_jst - datetime.timedelta(hours=2)).strftime("%Y-%m-%d %H:%M:%S")

        import store_hours
        import store_master
        orig_hours_fn = store_hours.get_business_hours_status
        dt_day = datetime.datetime(2026, 11, 19, 12, 0, 0, tzinfo=datetime.timezone(datetime.timedelta(hours=9)))

        base_stores = copy.deepcopy(self._saved_latest.get("full_data") or [])
        if len(base_stores) < 60:
            base_stores = [
                {
                    "name": k,
                    "women": 0,
                    "men": 0,
                    "source": "test",
                    "region": app_module.detect_region(k),
                }
                for k in store_master.load().keys()
            ]

        def _make_stores(open_count: int):
            out = copy.deepcopy(base_stores)
            opened = 0
            for st in out:
                nm = (st.get("name") or "").lower()
                is_kr = st.get("region") == "Korea" or ("カンナム" in nm) or ("ホンデ" in nm)
                if not is_kr and opened < open_count:
                    st["women"] = 18
                    st["men"] = 20
                    st["is_open_now"] = True
                    opened += 1
                else:
                    st["women"] = 0
                    st["men"] = 0
                    st["is_open_now"] = False
            return out

        states = [
            ("daytime_all_closed", live_ts, False, 0, False, False),
            ("night_open", live_ts, False, 5, True, True),
            ("night_1_open", live_ts, False, 1, True, True),
            ("halted_daytime", halted_ts, False, 0, False, False),
            ("seed_daytime", live_ts, True, 0, True, False),
            ("seed_night", live_ts, True, 5, True, True),
        ]
        with mock.patch(
            "store_hours.get_business_hours_status",
            side_effect=lambda s, now_jst=None, total_guests=0: orig_hours_fn(s, now_jst=dt_day, total_guests=total_guests),
        ):
            for label, ts, is_seed, open_n, expect_banner_hidden, expect_hero in states:
                with app_module.data_lock:
                    app_module.latest_data["full_data"] = _make_stores(open_n)
                    app_module.latest_data["last_updated"] = ts
                    app_module.latest_data["is_seed"] = is_seed
                app_module.clear_fast_http_caches()
                r = self.client.get("/?staff_test=1")
                self.assertEqual(r.status_code, 200, label)
                html = r.get_data(as_text=True)
                r.close()
                if expect_banner_hidden:
                    self.assertIn('id="daytime-forecast-banner" class="daytime-forecast-banner hidden"', html, label)
                else:
                    self.assertNotIn('id="daytime-forecast-banner" class="daytime-forecast-banner hidden"', html, label)
                if expect_hero:
                    self.assertIn(" bento-hero-card", html, label)
                else:
                    self.assertNotIn(" bento-hero-card", html, label)

                ranking_block = html[html.index('id="ranking-list"'):html.index('id="pc-store-inspector"')]
                if label in ("night_1_open", "seed_daytime"):
                    self.assertIn("オリエンタルラウンジ ソウル 江南(カンナム)", ranking_block, label)
                    self.assertIn("オリエンタルラウンジ ソウル 弘大(ホンデ)", ranking_block, label)
                m_head = re.search(r"var deferredHeadPool = (.*?);\n", html)
                self.assertIsNotNone(m_head, label)
                head_pool = json.loads(m_head.group(1))
                if label == "seed_daytime":
                    self.assertEqual(len(head_pool), 20, label)
                    self.assertTrue(all(item.get("ti") and item.get("sl") is not None for item in head_pool), label)
                elif label == "night_1_open":
                    self.assertEqual(len(head_pool), 19, label)
                elif label == "daytime_all_closed":
                    self.assertEqual(head_pool, [], label)


if __name__ == "__main__":
    unittest.main()
