from __future__ import annotations

import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from voiceover_plan import build_plan, validate_plan


RULES = Path(__file__).resolve().parents[1] / "references" / "protected-phrases.json"
SCRIPT = Path(__file__).with_name("voiceover_plan.py")


class VoiceoverPlanTests(unittest.TestCase):
    def test_preserves_paragraphs_and_protected_phrase(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "02-voiceover-final.txt"
            script.write_text(
                "第一个网页发出去以后，有同学提到，每次使用前还要领账号、找密码。\n\n"
                "这个问题，我们直接改了。\n\n"
                "不用注册，不用登录，打开页面就能使用。\n",
                encoding="utf-8",
            )
            plan = build_plan(script, RULES, 90)
            plan_file = root / "02-voiceover-plan.json"
            plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

            self.assertEqual(validate_plan(script, plan_file, RULES), [])
            self.assertIn("发出去以后", plan["units"][0]["protected_phrases"])
            self.assertEqual(plan["units"][1]["intent"], "firm-resolution")
            self.assertEqual(plan["units"][2]["reference_profile"], "direct-judgment")

    def test_source_edit_invalidates_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "02-voiceover-final.txt"
            script.write_text("原始口播。\n", encoding="utf-8")
            plan = build_plan(script, RULES, 90)
            plan_file = root / "02-voiceover-plan.json"
            plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")
            script.write_text("修改后的口播。\n", encoding="utf-8")

            errors = validate_plan(script, plan_file, RULES)
            self.assertTrue(any("source_sha256" in error for error in errors))

    def test_required_protected_phrase_cannot_be_removed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "02-voiceover-final.txt"
            script.write_text("第一个网页发出去以后，开始使用。\n", encoding="utf-8")
            plan = build_plan(script, RULES, 90)
            plan["units"][0]["protected_phrases"] = []
            plan_file = root / "02-voiceover-plan.json"
            plan_file.write_text(json.dumps(plan, ensure_ascii=False), encoding="utf-8")

            errors = validate_plan(script, plan_file, RULES)
            self.assertTrue(any("required protected phrase" in error for error in errors))

    def test_long_sentence_without_safe_boundary_fails(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            script = Path(temp_dir) / "02-voiceover-final.txt"
            script.write_text("这" * 91, encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "no safe sentence boundary"):
                build_plan(script, RULES, 90)

    def test_approve_command_requires_valid_matching_plan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            script = root / "02-voiceover-final.txt"
            script.write_text("这个问题，我们直接改了。\n", encoding="utf-8")
            plan_file = root / "02-voiceover-plan.json"
            plan_file.write_text(
                json.dumps(build_plan(script, RULES, 90), ensure_ascii=False),
                encoding="utf-8",
            )
            result = subprocess.run(
                [
                    sys.executable,
                    str(SCRIPT),
                    "approve",
                    "--text-file",
                    str(script),
                    "--plan",
                    str(plan_file),
                ],
                capture_output=True,
                text=True,
                encoding="utf-8",
            )
            self.assertEqual(result.returncode, 0, result.stderr)
            approved = json.loads(plan_file.read_text(encoding="utf-8"))
            self.assertEqual(approved["status"], "approved")


if __name__ == "__main__":
    unittest.main()
