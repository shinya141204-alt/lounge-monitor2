# -*- coding: utf-8 -*-
"""
tests/test_skills_and_harness.py - AIハーネス（AGENTS.md・全8スキル・付属スクリプト・store_master冪等性）自動回帰テスト
"""

import importlib.util
import os
import subprocess
import sys
import unittest
from pathlib import Path

import store_master

REPO_ROOT = Path(__file__).resolve().parents[1]
GLOBAL_SKILLS_DIR = Path.home() / ".gemini" / "config" / "skills"

EXPECTED_SKILLS = (
    "article",
    "affiliate",
    "editorial-ui-ux",
    "store-audit",
    "claude-review",
    "data-release",
    "growth-pdca",
    "ops-sentinel",
)


def _load_script_module(rel_path: str, mod_name: str):
    full_path = REPO_ROOT / rel_path
    spec = importlib.util.spec_from_file_location(mod_name, str(full_path))
    mod = importlib.util.module_from_spec(spec)
    assert spec and spec.loader
    spec.loader.exec_module(mod)
    return mod


class SkillsAndHarnessTests(unittest.TestCase):
    """共通ルール AGENTS.md と全8スキル・スクリプト群の整合性テスト"""

    def test_agents_md_budget_and_claude_symlink(self):
        agents_path = REPO_ROOT / "AGENTS.md"
        claude_path = REPO_ROOT / "CLAUDE.md"
        self.assertTrue(agents_path.is_file(), "AGENTS.md が存在すること")
        raw_bytes = agents_path.read_bytes()
        self.assertLess(len(raw_bytes), 24000, "AGENTS.md は Antigravity 上限 24,000 bytes 未満であること")
        self.assertGreater(len(raw_bytes), 5000, "AGENTS.md に十分な規約密度があること")
        self.assertTrue(claude_path.is_symlink(), "CLAUDE.md は AGENTS.md へのシンボリックリンクであること")
        self.assertEqual(claude_path.resolve(), agents_path.resolve())

        text = raw_bytes.decode("utf-8")
        for sname in EXPECTED_SKILLS:
            self.assertIn(sname, text, f"AGENTS.md §7 にスキル '{sname}' が記載されていること")

    def test_all_eight_skills_exist_and_sync_with_global_wrappers(self):
        for sname in EXPECTED_SKILLS:
            repo_skill = REPO_ROOT / ".agents" / "skills" / sname / "SKILL.md"
            self.assertTrue(repo_skill.is_file(), f"リポジトリ正本 {repo_skill} が存在すること")
            repo_txt = repo_skill.read_text(encoding="utf-8")
            self.assertTrue(repo_txt.startswith("---\n"), f"{sname}/SKILL.md に YAML フロントマターがあること")
            self.assertIn(f"name: {sname}", repo_txt)

            if GLOBAL_SKILLS_DIR.is_dir():
                glob_skill = GLOBAL_SKILLS_DIR / sname / "SKILL.md"
                self.assertTrue(glob_skill.is_file(), f"グローバル薄ラッパー {glob_skill} が2階層同期されていること")
                glob_txt = glob_skill.read_text(encoding="utf-8")
                self.assertIn(f"name: {sname}", glob_txt)
                self.assertIn(f".agents/skills/{sname}/SKILL.md", glob_txt)

    def test_store_master_seo_name_is_idempotent(self):
        samples = [
            ("OLG ag渋谷", "ag（アグ）渋谷"),
            ("OLG 新宿", "オリエンタルラウンジ 新宿"),
            ("JIS SHINJUKU", "JIS（ジス）新宿"),
            ("ASOBIBAR 梅田", "ASOBIBAR（アソビバー）梅田"),
            ("YATAKOI UMEDA", "ヤタコイ 梅田茶屋町"),
        ]
        for raw, expected in samples:
            first = store_master.seo_name(raw)
            second = store_master.seo_name(first)
            self.assertEqual(first, expected)
            self.assertEqual(second, expected, f"seo_name('{first}') が二重変換されないこと: got '{second}'")

    def test_claude_review_inline_bundle_and_mode_promotion_rules(self):
        cr = _load_script_module(".agents/skills/claude-review/scripts/run_claude_review.py", "cr_mod")
        self.assertEqual(cr.resolve_effective_mode("architect", "claude-fable-5-1", True), "architect")
        self.assertEqual(cr.resolve_effective_mode("consult", "claude-fable-5-1", True, no_promote=True), "consult")
        self.assertEqual(cr.resolve_effective_mode("code", "claude-fable-5-1", True), "audit")
        self.assertEqual(cr.resolve_effective_mode("article", "claude-fable-5-1", True), "flagship")
        self.assertEqual(cr.resolve_effective_mode("code", "claude-fable-5-1", True, no_promote=True), "code")

        bundle, omitted = cr._inline_files(["AGENTS.md"], max_total_chars=50000)
        self.assertIn("### FILE: `AGENTS.md`", bundle)
        self.assertEqual(omitted, [])

    def test_store_audit_enforces_staff_test_and_audit_ua(self):
        sa = _load_script_module(".agents/skills/store-audit/scripts/run_full_store_audit.py", "sa_mod")
        safe_u = sa._ensure_staff_test_url("https://aiseki-navi.com/api/status")
        self.assertIn("staff_test=1", safe_u)
        self.assertIn("curl/8.0", sa.AUDIT_UA)
        self.assertNotIn("Mozilla/5.0", sa.AUDIT_UA)

    def test_ops_sentinel_robots_link_checker(self):
        os_mod = _load_script_module(".agents/skills/ops-sentinel/scripts/run_ops_sentinel.py", "ops_mod")
        good_robots = "User-agent: *\nDisallow: /link/\n\nUser-agent: GPTBot\nDisallow: /link/\n"
        ok, missing = os_mod._check_robots_disallow_link(good_robots)
        self.assertTrue(ok)
        self.assertEqual(missing, [])

        bad_robots = "User-agent: *\nDisallow: /link/\n\nUser-agent: BadBot\nDisallow: /staff\n"
        ok2, missing2 = os_mod._check_robots_disallow_link(bad_robots)
        self.assertFalse(ok2)
        self.assertIn("BadBot", missing2)

    def test_static_audits_pass_cleanly(self):
        safe_env = {**os.environ, "DISABLE_SHEETS": "1", "DISABLE_SCHEDULER": "1", "PYTHONPATH": str(REPO_ROOT)}
        for cmd in (
            [sys.executable, ".agents/skills/affiliate/scripts/parse_and_audit_affiliate.py", "--audit"],
            [sys.executable, ".agents/skills/editorial-ui-ux/scripts/audit_anti_ai_ui.py", "--quick"],
            [sys.executable, ".agents/skills/store-audit/scripts/run_full_store_audit.py"],
        ):
            res = subprocess.run(cmd, cwd=str(REPO_ROOT), env=safe_env, capture_output=True, text=True)
            self.assertEqual(res.returncode, 0, f"Audit failed ({cmd}):\n{res.stdout}\n{res.stderr}")


if __name__ == "__main__":
    unittest.main()
