import copy
import os
import shutil
import tempfile
import unittest
from unittest import mock
from urllib.parse import urlsplit

import app as app_module
import ops_governance


class TestStaffAdminRedesign(unittest.TestCase):
    def setUp(self):
        self._tmpdir = tempfile.mkdtemp(prefix="staff_redesign_")
        self._orig_db = getattr(app_module.local_db, "DB_PATH", None)
        app_module.local_db.DB_PATH = os.path.join(self._tmpdir, "test.db")
        app_module.local_db.init_db()
        with app_module.data_lock:
            self._saved_latest = copy.deepcopy(app_module.latest_data)
        ops_governance.invalidate_governance_cache()
        self.client = app_module.app.test_client()

    def tearDown(self):
        with app_module.data_lock:
            app_module.latest_data.clear()
            app_module.latest_data.update(self._saved_latest)
        if self._orig_db is not None:
            app_module.local_db.DB_PATH = self._orig_db
        ops_governance.invalidate_governance_cache()
        app_module.clear_fast_http_caches()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def _login_master(self):
        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = True
            sess["auth_gen"] = app_module._compute_session_auth_gen(True)

    def _login_field_staff(self):
        with self.client.session_transaction() as sess:
            sess["is_staff"] = True
            sess["is_master"] = False
            sess["auth_gen"] = app_module._compute_session_auth_gen(False)

    def test_staff_ops_auth_and_json_contract(self):
        """新規 /staff/ops が未認証・一般スタッフを拒否し、マスターおよび X-Admin-Secret で正常応答すること"""
        r_anon = self.client.get("/staff/ops")
        self.assertEqual(r_anon.status_code, 302)

        self._login_field_staff()
        r_field = self.client.get("/staff/ops")
        self.assertEqual(r_field.status_code, 302)

        # Field staff /staff monitor must NOT expose master navigation links
        r_field_mon = self.client.get("/staff")
        self.assertEqual(r_field_mon.status_code, 200)
        field_html = r_field_mon.get_data(as_text=True)
        self.assertNotIn("/staff/ops", field_html)
        self.assertNotIn("/staff/analytics", field_html)

        self._login_master()
        r_master = self.client.get("/staff/ops")
        self.assertEqual(r_master.status_code, 200)
        ops_html = r_master.get_data(as_text=True)
        self.assertIn("運営・設定コンソール", ops_html)
        self.assertIn('id="ops-governance-hub"', ops_html)
        self.assertIn('id="sheets-retention-manager"', ops_html)

        r_json = self.client.get("/staff/ops?format=json")
        self.assertEqual(r_json.status_code, 200)
        j = r_json.get_json()
        self.assertTrue(j.get("ok"))
        self.assertIn("ops_gov", j)

    def test_six_pillars_and_subtabs_across_all_master_screens(self):
        """全6管理ピラーで共通ドック・aria-current='page'・サブタブ構造・週末画像キット統合が一貫していること"""
        self._login_master()

        expected_current = {
            "/staff/analytics": "/staff/analytics",
            "/staff/pdca": "/staff/pdca",
            "/staff": "/staff",
            "/staff/sns-editorial": "/staff/sns-editorial",
            "/staff/column-editorial": "/staff/column-editorial",
            "/staff/ops": "/staff/ops",
        }
        for route, expected_href in expected_current.items():
            with mock.patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None):
                resp = self.client.get(route)
            self.assertEqual(resp.status_code, 200, route)
            html = resp.get_data(as_text=True)
            resp.close()
            self.assertIn("/static/staff_workspace.css?v=1", html, route)
            self.assertIn("/static/staff_workspace.js?v=1", html, route)
            self.assertIn('role="tablist"', html, route)
            self.assertIn('role="tabpanel"', html, route)
            self.assertIn('hidden="until-found"', html, route)
            self.assertIn(f'href="{expected_href}" class="master-tab-link active" aria-current="page"', html, route)

        # Verify weekend image kit is integrated inside /staff/sns-editorial
        r_sns = self.client.get("/staff/sns-editorial")
        sns_html = r_sns.get_data(as_text=True)
        r_sns.close()
        self.assertIn('id="weekend-kit-section"', sns_html)
        self.assertIn("/staff/sns-kit/slide/1.png", sns_html)
        self.assertIn("/staff/sns-kit/download.zip", sns_html)

        # Verify ?export=slug on /staff/column-editorial pre-selects note_export sub-tab in SSR
        r_col_exp = self.client.get("/staff/column-editorial?export=shinjuku")
        col_exp_html = r_col_exp.get_data(as_text=True)
        r_col_exp.close()
        self.assertIn('id="ws-tab-note-export" data-ws-tab="note_export" aria-selected="true"', col_exp_html)

    def test_playwright_subtab_switching_keyboard_and_deeplinks(self):
        """Playwright 実ブラウザでサブタブ切替・矢印キー移動・?tab=/#hashディープリンク・横はみ出しゼロを検証"""
        try:
            from playwright.sync_api import sync_playwright
        except ImportError:
            self.skipTest("playwright is not installed")

        self._login_master()
        base = "http://aiseki.local"

        def attach_router(context):
            def handle(route):
                parsed = urlsplit(route.request.url)
                if parsed.netloc and parsed.netloc != "aiseki.local":
                    route.abort()
                    return
                path_qs = parsed.path + (("?" + parsed.query) if parsed.query else "")
                resp = self.client.open(path=path_qs, method=route.request.method)
                try:
                    headers = {k: v for k, v in resp.headers.items() if k.lower() not in ("transfer-encoding", "content-encoding", "content-length")}
                    route.fulfill(status=resp.status_code, headers=headers, body=resp.get_data())
                finally:
                    resp.close()

            context.route("**/*", handle)

        with mock.patch.object(app_module, "trigger_background_fetch_if_needed", lambda *a, **kw: None):
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    for vw, vh in ((1280, 900), (390, 844)):
                        ctx = browser.new_context(viewport={"width": vw, "height": vh}, service_workers="block")
                        attach_router(ctx)
                        page = ctx.new_page()

                        # 1. /staff/analytics sub-tab click & keyboard ArrowRight navigation
                        page.goto(base + "/staff/analytics?days=7", wait_until="domcontentloaded")
                        init_state = page.evaluate("""() => ({
                            activeTab: document.querySelector('[role="tab"][aria-selected="true"]')?.dataset.wsTab,
                            overviewHidden: document.getElementById('ws-panel-overview')?.hasAttribute('hidden'),
                            affHidden: document.getElementById('ws-panel-affiliate')?.hasAttribute('hidden'),
                            overflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth)
                        })""")
                        self.assertEqual(init_state["activeTab"], "overview")
                        self.assertFalse(init_state["overviewHidden"])
                        self.assertTrue(init_state["affHidden"])
                        self.assertEqual(init_state["overflowX"], 0)

                        page.click('#ws-tab-affiliate')
                        aff_state = page.evaluate("""() => ({
                            activeTab: document.querySelector('[role="tab"][aria-selected="true"]')?.dataset.wsTab,
                            overviewHidden: document.getElementById('ws-panel-overview')?.hasAttribute('hidden'),
                            affHidden: document.getElementById('ws-panel-affiliate')?.hasAttribute('hidden'),
                            search: window.location.search,
                            overflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth)
                        })""")
                        self.assertEqual(aff_state["activeTab"], "affiliate")
                        self.assertTrue(aff_state["overviewHidden"])
                        self.assertFalse(aff_state["affHidden"])
                        self.assertIn("tab=affiliate", aff_state["search"])
                        self.assertEqual(aff_state["overflowX"], 0)

                        # Keyboard ArrowRight wraps from last tab (affiliate) back to first tab (overview)
                        page.focus('#ws-tab-affiliate')
                        page.keyboard.press("ArrowRight")
                        kb_state = page.evaluate("() => document.querySelector('[role=\"tab\"][aria-selected=\"true\"]')?.dataset.wsTab")
                        self.assertEqual(kb_state, "overview")

                        # 2. Hash deep-link on /staff/sns-editorial#reel-studio-section opens 'video' sub-tab automatically
                        page.goto(base + "/staff/sns-editorial?channel=instagram#reel-studio-section", wait_until="domcontentloaded")
                        sns_hash_state = page.evaluate("""() => ({
                            activeTab: document.querySelector('[role="tab"][aria-selected="true"]')?.dataset.wsTab,
                            videoHidden: document.getElementById('ws-panel-video')?.hasAttribute('hidden'),
                            overflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth)
                        })""")
                        self.assertEqual(sns_hash_state["activeTab"], "video")
                        self.assertFalse(sns_hash_state["videoHidden"])
                        self.assertEqual(sns_hash_state["overflowX"], 0)

                        # Switch to integrated weekend_kit sub-tab
                        page.click('#ws-tab-weekend-kit')
                        wk_state = page.evaluate("""() => ({
                            activeTab: document.querySelector('[role="tab"][aria-selected="true"]')?.dataset.wsTab,
                            slidesCount: document.querySelectorAll('#weekend-kit-section .wk-slide-item').length,
                            overflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth)
                        })""")
                        self.assertEqual(wk_state["activeTab"], "weekend_kit")
                        self.assertEqual(wk_state["slidesCount"], 5)
                        self.assertEqual(wk_state["overflowX"], 0)

                        # 3. /staff/ops sub-tabs & zero overflow
                        page.goto(base + "/staff/ops#sheets-retention-manager", wait_until="domcontentloaded")
                        ops_state = page.evaluate("""() => ({
                            activeTab: document.querySelector('[role="tab"][aria-selected="true"]')?.dataset.wsTab,
                            sysHidden: document.getElementById('ws-panel-system')?.hasAttribute('hidden'),
                            overflowX: Math.max(0, document.documentElement.scrollWidth - window.innerWidth)
                        })""")
                        self.assertEqual(ops_state["activeTab"], "system")
                        self.assertFalse(ops_state["sysHidden"])
                        self.assertEqual(ops_state["overflowX"], 0)

                        ctx.close()
                finally:
                    browser.close()


if __name__ == "__main__":
    unittest.main()
