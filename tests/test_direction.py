"""Preference publication and actual downstream model prompts, in temporary state."""

import json
import tempfile
import unittest
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch

import agent
import email_triage
import it_jobs_triage
import weekly_trend
import lib.direction as direction


class TestDirection(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, name in (("DIRECTION_FILE", "current-direction.md"),
                          ("DIRECTION_SMALL_FILE", "current-direction-small.md"),
                          ("DIRECTION_AUDIT_FILE", "audit.jsonl")):
            self.stack.enter_context(patch.object(direction, key, self.root / name))
        self.full = "Prioritize accessibility engineering. Exclude sales roles."
        self.small = "Watch for accessibility; skip sales."

    def test_missing_and_legacy_direction(self):
        self.assertEqual(direction.load_direction_pair(), ("", ""))
        direction.DIRECTION_FILE.write_text(self.full)
        self.assertEqual(direction.load_direction_pair(), (self.full, self.full))
        direction.DIRECTION_SMALL_FILE.write_text(self.small)
        self.assertEqual(direction.load_direction_pair(), (self.full, self.small))

    def test_update_publishes_both_and_audits(self):
        with patch.object(direction, "call_deepseek", side_effect=[self.full, self.small]):
            result = direction.update_current_direction("Accessibility please", "fake-key")
        self.assertEqual(result, self.full)
        self.assertEqual(direction.load_direction_pair(), (self.full, self.small))
        self.assertEqual(direction.DIRECTION_FILE.read_text().strip(), self.full)
        self.assertEqual(direction.DIRECTION_SMALL_FILE.read_text().strip(), self.small)
        audit = json.loads(direction.DIRECTION_AUDIT_FILE.read_text())
        self.assertEqual(audit["feedback"], "Accessibility please")
        self.assertTrue(json.loads(direction.direction_record_path().read_text())["revision"])

    def test_failed_compression_preserves_entire_previous_version(self):
        direction.save_direction_pair("Old full", "Old small")
        before = {p: p.read_bytes() for p in self.root.iterdir() if p.is_file()}
        with patch.object(direction, "call_deepseek", side_effect=[self.full, RuntimeError("offline")]):
            with self.assertRaises(RuntimeError):
                direction.update_current_direction("new feedback", "fake-key")
        for path, content in before.items():
            self.assertEqual(path.read_bytes(), content)
        self.assertEqual(direction.load_direction_pair(), ("Old full", "Old small"))
        self.assertFalse(direction.DIRECTION_AUDIT_FILE.exists())

    def test_empty_responses_do_not_publish(self):
        for responses in ([""], [self.full, ""], [self.full, "no change"]):
            with self.subTest(responses=responses), patch.object(direction, "call_deepseek", side_effect=responses):
                with self.assertRaises(ValueError):
                    direction.update_current_direction("feedback", "fake-key")
                self.assertFalse(direction.direction_record_path().exists())

    def test_authoritative_pair_is_independent_of_export_files(self):
        direction.save_direction_pair(self.full, self.small)
        direction.DIRECTION_FILE.write_text("stale export")
        direction.DIRECTION_SMALL_FILE.unlink()
        self.assertEqual(direction.load_direction_pair(), (self.full, self.small))
        attachments = weekly_trend._get_direction_attachments()
        self.assertEqual([a["content"] for a in attachments], [self.full, self.small])

    def test_atomic_publish_failure_keeps_previous_pair(self):
        direction.save_direction_pair("Old full", "Old small")
        with patch.object(direction, "call_deepseek", side_effect=[self.full, self.small]), patch(
                "lib.state.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                direction.update_current_direction("feedback", "fake-key")
        self.assertEqual(direction.load_direction_pair(), ("Old full", "Old small"))

    def test_actual_triage_prompts_receive_correct_form_and_precedence(self):
        direction.save_direction_pair(self.full, self.small)
        criteria = self.root / "criteria.md"
        criteria.write_text("Candidate Profile: older preference for sales.")
        result = '{"decision":"skip","reason":"test","message":"","tags":{}}'
        calls = [
            (it_jobs_triage, lambda: it_jobs_triage.flash_incremental("Role\nCompany\nDetails", "Engineering", "Research", "fake-key"), self.small),
            (it_jobs_triage, lambda: it_jobs_triage.pro_full_eval("Role details", "fake-key"), self.full),
            (email_triage, lambda: email_triage.flash_header_triage("from", "subject", "fake-key"), self.small),
            (email_triage, lambda: email_triage.pro_body_triage("from", "subject", "body", "fake-key"), self.full),
        ]
        for module, invoke, expected in calls:
            with self.subTest(module=module.__name__, expected=expected), patch.object(
                    module, "CRITERIA_FILE", criteria), patch.object(module, "call_deepseek", return_value=result) as model:
                invoke()
                prompt = model.call_args.args[1]
                self.assertIn(expected, prompt)
                self.assertIn("override conflicting", prompt)

    def test_absent_delta_does_not_change_base_prompt(self):
        with patch.object(it_jobs_triage, "call_deepseek", return_value='{"decision":"disqualified"}') as model:
            it_jobs_triage.flash_incremental("Role", "Design", "Research", "fake-key")
        self.assertNotIn("Current preference deltas", model.call_args.args[1])

    def test_weekly_report_and_brief_receive_full_direction(self):
        direction.save_direction_pair(self.full, self.small)
        agg = {"total_tagged": 1, "role_types": {}, "skills": [], "tech_stack": [], "domains": {},
               "seniority": {}, "remote": 0, "onsite": 1}
        with patch.object(weekly_trend, "call_deepseek", return_value="report") as model:
            weekly_trend.build_report("Profile", agg, [], "fake-key")
        self.assertIn(self.full, model.call_args.args[1])
        self.assertIn(self.full, agent.build_system_prompt())

    def test_next_evaluation_reads_updated_preferences(self):
        with patch.object(it_jobs_triage, "call_deepseek", return_value='{"decision":"disqualified"}') as model:
            direction.save_direction_pair("Old", "Old compressed")
            it_jobs_triage.flash_incremental("Role", "Design", "Research", "fake-key")
            direction.save_direction_pair(self.full, self.small)
            it_jobs_triage.flash_incremental("Role", "Design", "Research", "fake-key")
        self.assertIn("Old compressed", model.call_args_list[0].args[1])
        self.assertIn(self.small, model.call_args_list[1].args[1])


if __name__ == "__main__":
    unittest.main()
