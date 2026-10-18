"""Installation/reinstallation behavior with fake model responses and real files."""

import io
import json
import logging
import shutil
import tempfile
import unittest
import urllib.error
from contextlib import ExitStack, redirect_stdout
from pathlib import Path
from unittest.mock import patch

import generate_profile as generator
import it_jobs_triage as triage
import lib.direction as direction
from lib.matching import build_flash_updates, matching_gaps
from lib.setup_matching import run_setup
from lib.state import apply_text_updates

PROJECT = Path(__file__).resolve().parents[1]


class TestMatchingSetup(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        self.state = self.root / "state"
        self.state.mkdir()
        for name in ("channels.json.example", "feeds.json.example", "it-jobs-criteria.md.example", "email-triage-criteria.md.example"):
            shutil.copy2(PROJECT / name, self.root / name)
        self.channels = self.state / "channels.json"
        self.feeds = self.state / "feeds.json"
        shutil.copy2(self.root / "channels.json.example", self.channels)
        shutil.copy2(self.root / "feeds.json.example", self.feeds)
        config = json.loads(self.channels.read_text())
        config["channels"][0]["username"] = "design_jobs"
        config["channels"][0]["description"] = "Design and technology vacancies"
        self.channels.write_text(json.dumps(config))
        self.stack.enter_context(patch.multiple(generator, PROJECT_DIR=self.root, STATE_DIR=self.state,
            GEN_DIR=self.root / "source", _SOURCE_DIR=self.root / "source", CHANNELS_FILE=self.channels,
            CRITERIA_TELEGRAM=self.state / "it-jobs-criteria.md", CRITERIA_EMAIL=self.state / "email-triage-criteria.md",
            SKILLS_DIR=self.root / "skills"))
        for key, name in (("DIRECTION_FILE", "current-direction.md"), ("DIRECTION_SMALL_FILE", "current-direction-small.md"),
                          ("DIRECTION_AUDIT_FILE", "audit.jsonl")):
            self.stack.enter_context(patch.object(direction, key, self.state / name))
        self.stack.enter_context(patch.object(generator, "load_env", return_value={"DEEPSEEK_API_KEY": "fake-key", "USER_NAME": "Jane"}))
        self.docs = self.root / "documents"
        self.docs.mkdir()
        (self.docs / "cv.txt").write_text("Jane: accessibility designer. Skills: user research, prototyping.")
        self.prompts = []

    def fake_model(self, model, prompt, key, timeout=90):
        self.prompts.append(prompt)
        if "List every fact" in prompt:
            return "Jane is an accessibility designer skilled in research and prototyping."
        if "career analyst" in prompt:
            return "\n".join(f"###FILE: {name}###\nJane: accessibility, user research, prototyping."
                             for name in generator.PROFILE_FILES)
        if "Generate Flash job-filter preferences" in prompt:
            sources = json.loads(prompt.split("SOURCES:\n", 1)[1].split("\n\nReturn ONLY JSON:", 1)[0])
            return json.dumps({"sources": [{"id": source["id"], "desired_roles": "Accessibility designer",
                                            "acceptable_roles": "User researcher, prototyping specialist"} for source in sources]})
        return "# Candidate Profile\nJane: accessibility design.\n## Evaluation\nSend relevant design and research vacancies."

    def seed_profile(self):
        (self.root / "source").mkdir(exist_ok=True)
        for name in generator.PROFILE_FILES:
            (self.root / "source" / name).write_text("Jane: accessibility designer, user research, prototyping.")

    def run_ui(self, answers, model=None):
        output = io.StringIO()
        with redirect_stdout(output), patch("builtins.input", side_effect=answers), patch.object(
                generator, "call_deepseek", side_effect=model or self.fake_model) as call:
            code = run_setup(generator)
        return code, output.getvalue(), call

    def test_first_install_review_apply_and_actual_flash_prompt(self):
        before_feeds = json.loads(self.feeds.read_text())
        code, output, _ = self.run_ui(["y", str(self.docs), "", "a"])
        self.assertEqual(code, 0)
        self.assertIn("Proposed matching settings", output)
        self.assertIn("Accessibility designer", output)
        self.assertEqual(matching_gaps(self.root), [])
        for entry, original in zip(json.loads(self.feeds.read_text())["feeds"], before_feeds["feeds"]):
            self.assertEqual(entry["url"], original["url"])
            self.assertEqual(entry.get("include_categories"), original.get("include_categories"))
        channel = json.loads(self.channels.read_text())["channels"][0]
        with patch.object(triage, "call_deepseek", return_value='{"decision":"pass_to_pro"}') as model:
            triage.flash_incremental("Designer\nCompany\nDetails", channel["desired_roles"], channel["acceptable_roles"], "fake-key")
        self.assertIn("Accessibility designer", model.call_args.args[1])
        generation_prompt = next(p for p in self.prompts if "Generate Flash job-filter" in p)
        self.assertIn("Jane: accessibility", generation_prompt)
        self.assertIn("Design and technology vacancies", generation_prompt)

    def test_first_install_later_preserves_all_settings(self):
        before = self.channels.read_bytes(), self.feeds.read_bytes()
        code, output, _ = self.run_ui(["y", str(self.docs), "", "l"])
        self.assertEqual(code, 1)
        self.assertEqual((self.channels.read_bytes(), self.feeds.read_bytes()), before)
        self.assertFalse((self.root / "source").exists())
        self.assertIn("No settings changed", output)

    def test_reinstall_default_keep_makes_no_model_calls(self):
        self.run_ui(["y", str(self.docs), "", "a"])
        before = generator.generation_snapshot()
        code, _, model = self.run_ui([""])
        self.assertEqual(code, 0)
        model.assert_not_called()
        self.assertEqual(generator.generation_snapshot(), before)

    def test_complete_missing_keeps_custom_field_and_existing_criteria(self):
        self.seed_profile()
        config = json.loads(self.channels.read_text())
        config["channels"][0]["desired_roles"] = "My manually chosen roles"
        self.channels.write_text(json.dumps(config))
        generator.CRITERIA_TELEGRAM.write_text("Existing personalized criteria")
        code, _, _ = self.run_ui(["c", "", "a"])
        self.assertEqual(code, 0)
        channel = json.loads(self.channels.read_text())["channels"][0]
        self.assertEqual(channel["desired_roles"], "My manually chosen roles")
        self.assertEqual(channel["acceptable_roles"], "User researcher, prototyping specialist")
        self.assertEqual(generator.CRITERIA_TELEGRAM.read_text(), "Existing personalized criteria")

    def test_regenerate_is_reviewed_and_retains_existing_direction(self):
        self.run_ui(["y", str(self.docs), "", "a"])
        direction.save_direction_pair("Prefer nonprofit employers", "Nonprofits first")
        before = direction.direction_record_path().read_bytes()
        code, output, _ = self.run_ui(["r", "", "", "a"])
        self.assertEqual(code, 0)
        self.assertIn("Proposed matching settings", output)
        self.assertEqual(direction.direction_record_path().read_bytes(), before)
        self.assertTrue(any("Prefer nonprofit employers" in p for p in self.prompts))

    def test_revision_changes_inputs_before_application(self):
        self.seed_profile()
        with patch.object(direction, "call_deepseek", side_effect=["Exclude sales; prioritize design", "Design, no sales"]):
            code, _, _ = self.run_ui(["c", "", "r", "Exclude sales", "a"])
        self.assertEqual(code, 0)
        self.assertTrue(any("Exclude sales; prioritize design" in p for p in self.prompts))
        self.assertEqual(direction.load_current_direction_small(), "Design, no sales")

    def test_invalid_json_is_corrected_once(self):
        self.seed_profile()
        attempts = []
        def model(*args, **kwargs):
            if "Generate Flash" in args[1]:
                attempts.append(args[1])
                if len(attempts) == 1:
                    return "not JSON"
            return self.fake_model(*args, **kwargs)
        code, _, _ = self.run_ui(["c", "", "a"], model)
        self.assertEqual(code, 0)
        self.assertEqual(len(attempts), 2)
        self.assertIn("failed validation: invalid JSON", attempts[1])

    def test_exhausted_invalid_json_leaves_config_and_reports_failure(self):
        self.seed_profile()
        before = generator.generation_snapshot()
        with self.assertLogs(level=logging.WARNING) as logs:
            code, output, model = self.run_ui(["c", "", "l"], lambda *args, **kw: "invalid")
        self.assertEqual(code, 1)
        self.assertEqual(model.call_count, 2)
        self.assertEqual(generator.generation_snapshot(), before)
        self.assertIn("after two attempts: invalid JSON", "\n".join(logs.output))
        self.assertIn("invalid JSON", (self.state / "setup.log").read_text())
        self.assertIn("Setup incomplete", output)

    def test_authentication_error_has_actionable_message_and_no_automatic_retry(self):
        self.seed_profile()
        def failed(*args, **kwargs):
            raise urllib.error.HTTPError("https://example.invalid", 401, "Unauthorized", {}, None)
        with self.assertLogs(level=logging.ERROR) as logs:
            code, _, model = self.run_ui(["c", "", "l"], failed)
        self.assertEqual(code, 1)
        self.assertEqual(model.call_count, 1)
        self.assertIn("Check DEEPSEEK_API_KEY", "\n".join(logs.output))

    def test_unknown_ids_missing_fields_and_empty_values_never_write(self):
        for response in ({"sources": []}, {"sources": [{"id": "rss:unknown", "desired_roles": "A", "acceptable_roles": "B"}]},
                         {"sources": [{"id": "telegram:design_jobs", "desired_roles": ""}]}, []):
            before = self.channels.read_bytes(), self.feeds.read_bytes()
            with self.subTest(response=response), patch.object(generator, "call_deepseek", return_value=json.dumps(response)) as model:
                with self.assertRaises(ValueError):
                    build_flash_updates(self.root, "Jane's profile", "fake-key", model)
                self.assertEqual(model.call_count, 2)
            self.assertEqual((self.channels.read_bytes(), self.feeds.read_bytes()), before)

    def test_disabled_sources_are_preserved(self):
        config = json.loads(self.feeds.read_text())
        config["feeds"][0]["enabled"] = False
        original = dict(config["feeds"][0])
        self.feeds.write_text(json.dumps(config))
        self.seed_profile()
        self.run_ui(["c", "", "a"])
        self.assertEqual(json.loads(self.feeds.read_text())["feeds"][0], original)

    def test_cli_stage2_generates_flash_without_telegram_config(self):
        self.channels.unlink()
        self.seed_profile()
        with patch.object(generator, "call_deepseek", side_effect=self.fake_model):
            generator.stage2_generate_criteria("Jane", "fake-key")
        self.assertEqual(json.loads(self.feeds.read_text())["feeds"][0]["desired_roles"], "Accessibility designer")

    def test_stale_proposal_cannot_overwrite_new_settings(self):
        self.seed_profile()
        def changed(*args, **kwargs):
            result = self.fake_model(*args, **kwargs)
            generator.CRITERIA_TELEGRAM.write_text("Concurrent edit")
            return result
        before = self.channels.read_bytes()
        with self.assertLogs(level=logging.ERROR) as logs:
            code, _, _ = self.run_ui(["c", "", "a", "l"], changed)
        self.assertEqual(code, 1)
        self.assertEqual(self.channels.read_bytes(), before)
        self.assertEqual(generator.CRITERIA_TELEGRAM.read_text(), "Concurrent edit")
        self.assertIn("changed during generation", "\n".join(logs.output))

    def test_failed_second_replace_rolls_back_first(self):
        before = self.channels.read_text(), self.feeds.read_text()
        import os
        replace = os.replace
        def fail_once(src, dst):
            if dst == self.feeds:
                raise OSError("disk full")
            return replace(src, dst)
        with patch("lib.state.os.replace", side_effect=fail_once), self.assertRaises(OSError):
            apply_text_updates({self.channels: "new", self.feeds: "new"},
                               {self.channels: before[0], self.feeds: before[1]})
        self.assertEqual((self.channels.read_text(), self.feeds.read_text()), before)


if __name__ == "__main__":
    unittest.main()
