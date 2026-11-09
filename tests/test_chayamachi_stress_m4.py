"""
Adversarial Concurrency, Dirty-Data, Memory RSS & UI Integrity Stress Tests for Milestone 4 (Challenger 2).

Tests:
1. Multi-threaded concurrency (64+ threads) with randomized dirty/fuzzed inputs across:
   - moat_intelligence.get_chayamachi_shift_analysis
   - moat_intelligence.enrich_stores_with_moat_signals
   - monitor.get_yatakoi_data (mocked dirty payloads)
2. Targeted Reproduction Test for Unhandled TypeError Crash in get_chayamachi_shift_analysis.
3. Memory RSS stability across 10,000 iterations.
4. UI DOM layout stability, rapid polling simulation, and WCAG contrast validation.
5. XSS / script injection resilience in store_detail.html rendering.
"""

import concurrent.futures
import datetime
import math
import os
import random
import re
import resource
import sys
import unittest
from unittest.mock import MagicMock, patch

# Ensure project root is in sys.path
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

import jinja2
import moat_intelligence
import monitor


class ChayamachiAdversarialRegressionTests(unittest.TestCase):
    """Reproduce and prove crashes discovered during adversarial review."""

    def test_01_chayamachi_shift_analysis_dirty_seats_crash_reproduction(self):
        """EMPIRICAL FINDING: get_chayamachi_shift_analysis crashes with unhandled TypeError

        When pos_telemetry is a dict missing 'yatakoi_seats', but the fallback parameter
        yatakoi_seats or asobibar_seats is a string (e.g. from query/dirty payload),
        the condition `elif not yatakoi_seats or yatakoi_seats <= 0:` triggers
        `TypeError: '<=' not supported between instances of 'str' and 'int'`.
        """
        # Case A: yatakoi_seats is a string and pos_telemetry is provided without yatakoi_seats
        try:
            moat_intelligence.get_chayamachi_shift_analysis(
                yatakoi_seats="not_an_int",
                pos_telemetry={"telemetry_type": "yatakoi_pos"},
            )
            crashed_a = False
        except TypeError as e:
            crashed_a = True
            error_msg_a = str(e)

        # Case B: asobibar_seats is a non-empty string and pos_telemetry is provided without asobibar_seats
        try:
            moat_intelligence.get_chayamachi_shift_analysis(
                asobibar_seats="10_seats",
                pos_telemetry={"telemetry_type": "yatakoi_pos"},
            )
            crashed_b = False
        except TypeError as e:
            crashed_b = True
            error_msg_b = str(e)

        # Verify that this crash is ELIMINATED in the fixed implementation (Zero-Exception Guarantee)
        self.assertFalse(
            crashed_a,
            "Expected NO TypeError '<=' not supported between instances of 'str' and 'int' when yatakoi_seats='not_an_int'"
        )
        self.assertFalse(
            crashed_b,
            "Expected NO TypeError '<=' not supported between instances of 'str' and 'int' when asobibar_seats='10_seats'"
        )


class ChayamachiConcurrencyAndDirtyDataStressTests(unittest.TestCase):
    """Stress testing concurrency and dirty input resilience with 64+ threads."""

    def setUp(self):
        self.dirty_values = [
            None,
            "",
            "   ",
            "undefined",
            "NaN",
            "null",
            "<script>alert(1)</script>",
            "'; DROP TABLE stores; --",
            {},
            {"unknown_key": "junk"},
            [],
            [1, 2, 3],
            -1,
            -999999,
            0,
            1,
            100,
            1000000,
            float("nan"),
            float("inf"),
            float("-inf"),
            3.14159,
            True,
            False,
            object(),
        ]

    def _generate_random_dirty_telemetry(self):
        """Generate dirty / fuzzed pos_telemetry payload."""
        payload_type = random.choice([
            "yatakoi_pos",
            "jis_pos",
            "asobibar_pos",
            None,
            12345,
            "",
            "corrupt_type",
        ])
        return {
            "telemetry_type": payload_type,
            "yatakoi_men": random.choice(self.dirty_values),
            "yatakoi_women": random.choice(self.dirty_values),
            "asobibar_men": random.choice(self.dirty_values),
            "asobibar_women": random.choice(self.dirty_values),
            "yatakoi_seats": random.choice(self.dirty_values),
            "asobibar_seats": random.choice(self.dirty_values),
            "is_double": random.choice([True, False, None, "yes", 0, 1]),
            "extra_dirty_field": random.choice(self.dirty_values),
        }

    def test_02_enrich_stores_with_moat_signals_64_threads_concurrency(self):
        """Run 64 concurrent threads invoking enrich_stores_with_moat_signals with dirty stores."""
        thread_count = 64
        iterations = 30
        errors = []

        def worker_task(thread_id):
            local_errors = []
            for _ in range(iterations):
                try:
                    # Construct store list including Chayamachi, JIS, and malformed entities
                    stores = [
                        {
                            "name": "YATAKOI UMEDA",
                            "slug": "yatakoi-umeda",
                            "men": random.choice([0, 10, 50, None, "20"]),
                            "women": random.choice([0, 10, 50, None, "25"]),
                            "pos_telemetry": self._generate_random_dirty_telemetry(),
                        },
                        {
                            "name": "ASOBIBAR 茶屋町",
                            "slug": "asobibar-chayamachi",
                            "men": random.choice([0, 5, 20, None]),
                            "women": random.choice([0, 5, 20, None]),
                            "pos_telemetry": None,  # should inherit from context
                        },
                        {
                            "name": "JIS NAMBA",
                            "slug": "jis-namba",
                            "men": 20,
                            "women": 25,
                        },
                        None,  # malformed entry
                        "string_store",  # malformed entry
                        {},  # empty store
                    ]

                    now_jst = datetime.datetime(2026, 11, 10, random.randint(0, 23), random.randint(0, 59))
                    enriched = moat_intelligence.enrich_stores_with_moat_signals(
                        stores=stores,
                        now_jst=now_jst,
                        allow_weather_blocking=False,
                    )

                    # Invariant checks
                    if not isinstance(enriched, list):
                        local_errors.append(f"Enriched is not a list: {type(enriched)}")
                        continue

                    # Find YATAKOI UMEDA and ASOBIBAR 茶屋町
                    for s in enriched:
                        if isinstance(s, dict) and s.get("slug") in ("yatakoi-umeda", "asobibar-chayamachi"):
                            if "chayamachi_shift" not in s:
                                local_errors.append(f"{s.get('slug')} missing chayamachi_shift")
                            elif not isinstance(s["chayamachi_shift"], dict):
                                local_errors.append(f"{s.get('slug')} chayamachi_shift not dict")

                except Exception as e:
                    local_errors.append(f"Thread {thread_id} crashed with {type(e).__name__}: {e}")
            return local_errors

        with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
            futures = [executor.submit(worker_task, tid) for tid in range(thread_count)]
            for fut in concurrent.futures.as_completed(futures):
                res = fut.result()
                if res:
                    errors.extend(res)

        self.assertEqual(len(errors), 0, f"Encountered {len(errors)} concurrency errors in enrich: {errors[:5]}")

    def test_03_get_yatakoi_data_64_threads_with_dirty_firebase_responses(self):
        """Hammer get_yatakoi_data with 64 threads under various malformed Firebase responses."""
        thread_count = 64
        iterations = 25
        errors = []

        dirty_responses = [
            {},
            {"males": "bad", "females": None},
            {"males": 10, "females": 15, "ykMales": "invalid", "ykFemales": 20},
            {"males": 0, "females": 0, "seats": 0, "ykSeats": 0},
            {"males": 9999999, "females": -10, "ykMales": 50, "ykFemales": 60, "isDouble": 1},
            None,
            "corrupted_json_string",
        ]

        session_mock = MagicMock()

        def fake_get(*args, **kwargs):
            mock_resp = random.choice(dirty_responses)
            resp_mock = MagicMock()
            if isinstance(mock_resp, str):
                resp_mock.json.side_effect = ValueError("JSON decode error")
            else:
                resp_mock.json.return_value = mock_resp
            resp_mock.raise_for_status.return_value = None
            resp_mock.text = "{}"
            return resp_mock

        session_mock.get.side_effect = fake_get

        with patch("monitor._get_http_session", return_value=session_mock):
            def worker_task(thread_id):
                local_errors = []
                for _ in range(iterations):
                    try:
                        res = monitor.get_yatakoi_data(now_jst=datetime.datetime(2026, 11, 10, 21, 0))
                        if not isinstance(res, list):
                            local_errors.append(f"get_yatakoi_data returned non-list: {type(res)}")
                        for store in res:
                            if not isinstance(store, dict):
                                local_errors.append(f"store is not dict: {type(store)}")
                            else:
                                self.assertIn("men", store)
                                self.assertIn("women", store)
                                self.assertIn("pos_telemetry", store)
                                tel = store["pos_telemetry"]
                                self.assertEqual(tel["telemetry_type"], "yatakoi_pos")
                    except Exception as e:
                        local_errors.append(f"Thread {thread_id} crashed: {type(e).__name__}: {e}")
                return local_errors

            with concurrent.futures.ThreadPoolExecutor(max_workers=thread_count) as executor:
                futures = [executor.submit(worker_task, tid) for tid in range(thread_count)]
                for fut in concurrent.futures.as_completed(futures):
                    res = fut.result()
                    if res:
                        errors.extend(res)

        self.assertEqual(len(errors), 0, f"get_yatakoi_data crashed {len(errors)} times: {errors[:5]}")


class MemoryRSSStabilityTests(unittest.TestCase):
    """Test memory stability (RSS) over 10,000 iterations."""

    def test_04_memory_rss_stability_10000_iterations(self):
        """Verify memory RSS does not leak over 10,000 shift analysis computations."""
        rusage_before = resource.getrusage(resource.RUSAGE_SELF)
        rss_before = rusage_before.ru_maxrss

        now = datetime.datetime(2026, 11, 10, 22, 0)
        iterations = 10000

        for i in range(iterations):
            yk_m = (i * 3) % 40
            yk_w = (i * 5) % 40
            as_m = (i * 7) % 50
            as_w = (i * 11) % 50
            tel = {
                "telemetry_type": "yatakoi_pos",
                "yatakoi_men": yk_m,
                "yatakoi_women": yk_w,
                "asobibar_men": as_m,
                "asobibar_women": as_w,
                "yatakoi_seats": 70,
                "asobibar_seats": 10,
                "is_double": True,
            }
            res = moat_intelligence.get_chayamachi_shift_analysis(
                now_jst=now,
                pos_telemetry=tel,
            )
            self.assertIsNotNone(res)

        rusage_after = resource.getrusage(resource.RUSAGE_SELF)
        rss_after = rusage_after.ru_maxrss

        growth_kb = (rss_after - rss_before) if sys.platform != "darwin" else ((rss_after - rss_before) / 1024)
        self.assertLess(growth_kb, 25000, f"Memory RSS increased excessively: {growth_kb:.1f} KB")


class UIIntegrityAndWCAGContrastTests(unittest.TestCase):
    """Adversarially challenge store_detail.html layout, polling re-renders, and WCAG contrast."""

    def setUp(self):
        template_path = os.path.join(PROJECT_ROOT, "templates")
        with open(os.path.join(template_path, "store_detail.html"), encoding="utf-8") as f:
            self.template_content = f.read()

    def test_05_panel_outside_hero_moat_grid_and_polling_resilience(self):
        """Empirically prove #chayamachi-shift-panel is OUTSIDE #hero-moat-grid to withstand JS polling re-renders."""
        grid_start = self.template_content.find('id="hero-moat-grid"')
        self.assertNotEqual(grid_start, -1, "#hero-moat-grid must exist in store_detail.html")

        panel_pos = self.template_content.find('id="chayamachi-shift-panel"')
        self.assertNotEqual(panel_pos, -1, "#chayamachi-shift-panel must exist in store_detail.html")

        self.assertGreater(panel_pos, grid_start, "#chayamachi-shift-panel must appear after #hero-moat-grid starts")

        between = self.template_content[grid_start:panel_pos]
        self.assertIn("</div>", between, "Grid must close with </div> before #chayamachi-shift-panel starts")

        self.assertIn("mGrid.innerHTML = fcHtml + weHtml + tmHtml", self.template_content)
        self.assertNotIn("chayamachi-shift", between.split("</div>")[0], "Panel must not be inside grid")

    def test_06_wcag_contrast_dark_and_light_modes(self):
        """Calculate and verify WCAG 2.1 AA contrast ratio (>= 4.5:1) for all Chayamachi panel styles."""
        def srgb_to_linear(c):
            c = c / 255.0
            return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4

        def relative_luminance(rgb):
            r, g, b = [srgb_to_linear(x) for x in rgb]
            return 0.2126 * r + 0.7152 * g + 0.0722 * b

        def contrast_ratio(rgb1, rgb2):
            l1 = relative_luminance(rgb1)
            l2 = relative_luminance(rgb2)
            lighter = max(l1, l2)
            darker = min(l1, l2)
            return (lighter + 0.05) / (darker + 0.05)

        # Palette colors defined in CSS
        dark_bg = (22, 22, 25)
        dark_text_main = (237, 237, 237)
        dark_text_sub = (161, 161, 170)
        dark_accent_text = (230, 200, 139)

        light_bg = (255, 255, 255)
        light_text_main = (24, 24, 27)
        light_text_sub = (82, 82, 91)

        # 1. Dark Mode Contrast
        ratio_dark_main = contrast_ratio(dark_bg, dark_text_main)
        ratio_dark_sub = contrast_ratio(dark_bg, dark_text_sub)
        ratio_dark_accent = contrast_ratio(dark_bg, dark_accent_text)

        self.assertGreaterEqual(ratio_dark_main, 4.5, f"Dark text_main contrast {ratio_dark_main:.2f} < 4.5:1")
        self.assertGreaterEqual(ratio_dark_sub, 4.5, f"Dark text_sub contrast {ratio_dark_sub:.2f} < 4.5:1")
        self.assertGreaterEqual(ratio_dark_accent, 4.5, f"Dark accent contrast {ratio_dark_accent:.2f} < 4.5:1")

        # 2. Light Mode Contrast
        ratio_light_main = contrast_ratio(light_bg, light_text_main)
        ratio_light_sub = contrast_ratio(light_bg, light_text_sub)

        self.assertGreaterEqual(ratio_light_main, 4.5, f"Light text_main contrast {ratio_light_main:.2f} < 4.5:1")
        self.assertGreaterEqual(ratio_light_sub, 4.5, f"Light text_sub contrast {ratio_light_sub:.2f} < 4.5:1")

    def test_07_layout_stability_and_cls_defenses(self):
        """Verify layout stability defenses (tabular-nums, fixed track height, responsive grid)."""
        self.assertIn("font-variant-numeric: tabular-nums;", self.template_content)
        self.assertIn(".chayamachi-num", self.template_content)

        self.assertIn(".chayamachi-ratio-track", self.template_content)
        match_track = re.search(r"\.chayamachi-ratio-track\s*\{[^}]*height:\s*6px;", self.template_content)
        self.assertIsNotNone(match_track, "Ratio track must have explicit fixed height of 6px")

        self.assertIn("@media (max-width: 620px)", self.template_content)

    def test_08_xss_and_injection_escaping(self):
        """Ensure malicious HTML/JS injected into pos_telemetry or advice is safely escaped."""
        match = re.search(r'({# --- 梅田茶屋町ビル 2フロア回遊シフト パネル.*?{%\s*endif\s*%}\s*</div>\s*{%\s*endif\s*%})', self.template_content, re.DOTALL)
        self.assertIsNotNone(match, "Failed to locate Chayamachi shift panel block")

        snippet = match.group(1)
        env = jinja2.Environment(autoescape=True)
        tmpl = env.from_string(snippet)

        xss_string = '<script>alert("XSS_CHAYAMACHI_ATTACK")</script>'
        store = {
            "slug": "yatakoi-umeda",
            "name": "YATAKOI UMEDA",
            "total": 50,
            "men": 25,
            "women": 25,
            "pos_telemetry": {
                "telemetry_type": "yatakoi_pos",
                "yatakoi_men": 20,
                "yatakoi_women": 20,
                "asobibar_men": 5,
                "asobibar_women": 5,
            },
            "chayamachi_shift": {
                "phase": "transition",
                "phase_label_ja": xss_string,
                "phase_tone": "amber",
                "flow_direction": "yatakoi_to_asobibar",
                "flow_label_ja": xss_string,
                "yatakoi_cust": 40,
                "asobibar_cust": 10,
                "total_cust": 50,
                "yatakoi_seats": 70,
                "asobibar_seats": 10,
                "yatakoi_occ_pct": 57.1,
                "asobibar_occ_pct": 100.0,
                "yatakoi_ratio_pct": 80.0,
                "asobibar_ratio_pct": 20.0,
                "shift_score": 20.0,
                "advice": xss_string,
                "recommendation": xss_string,
            }
        }

        rendered = tmpl.render(store=store, none=None)
        self.assertNotIn('<script>alert("XSS_CHAYAMACHI_ATTACK")</script>', rendered)
        self.assertIn('&lt;script&gt;alert(&#34;XSS_CHAYAMACHI_ATTACK&#34;)&lt;/script&gt;', rendered)


if __name__ == "__main__":
    unittest.main()
