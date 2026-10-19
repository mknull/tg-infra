#!/usr/bin/env python3
"""Tests for unified 3-line incremental flash strategy."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "triage", _PROJECT / "it_jobs_triage.py")
triage = importlib.util.module_from_spec(_spec)
sys.modules["triage"] = triage
_spec.loader.exec_module(triage)


class TestFlashIncremental(unittest.TestCase):

    def test_disqualifies_on_first_window(self):
        """Flash sees 3 lines and returns disqualified."""
        mock_raw = '{"decision": "disqualified", "reason": "not a job posting"}'
        with patch.object(triage, "call_deepseek", return_value=mock_raw):
            flag, reason, _windows = triage.flash_incremental(
                "line1\nline2\nline3\nline4",
                "ML roles", "data science",
                "fake-key"
            )
        self.assertFalse(flag)
        self.assertIn("not a job", reason)

    def test_reads_more_then_passes(self):
        """Flash asks for more on first window, then passes on second."""
        responses = [
            '{"decision": "read_more", "reason": "need more context"}',
            '{"decision": "pass_to_pro", "reason": "looks like a relevant job"}',
        ]
        with patch.object(triage, "call_deepseek", side_effect=responses):
            flag, reason, _windows = triage.flash_incremental(
                "line1\nline2\nline3\nline4\nline5\nline6",
                "ML roles", "data science",
                "fake-key"
            )
        self.assertTrue(flag)
        self.assertIn("relevant", reason)

    def test_reads_more_then_disqualifies(self):
        """Flash asks for more, then disqualifies."""
        responses = [
            '{"decision": "read_more", "reason": "unclear"}',
            '{"decision": "disqualified", "reason": "seeking work, not hiring"}',
        ]
        with patch.object(triage, "call_deepseek", side_effect=responses):
            flag, reason, _windows = triage.flash_incremental(
                "line1\nline2\nline3\nline4\nline5\nline6",
                "ML roles", "data science",
                "fake-key"
            )
        self.assertFalse(flag)
        self.assertIn("seeking", reason)

    def test_exhausts_content_with_read_more(self):
        """Reaches end of content while still reading more — escalates to pro."""
        with patch.object(triage, "call_deepseek",
                          return_value='{"decision": "read_more", "reason": "still unclear"}'):
            flag, reason, _windows = triage.flash_incremental(
                "short\npost\n",
                "ML roles", "data science",
                "fake-key"
            )
        self.assertTrue(flag)

    def test_large_rss_read_more_has_bounded_calls_and_preserves_full_pro_input(self):
        from rss_poller import parse_feed
        from tests.test_rss_subscriptions import xml
        posting = parse_feed(xml("&lt;br/&gt;".join(
            ["Ambiguous vacancy details; responsibilities still to follow."] * 3000 +
            ["LAST REQUIREMENT: fluent Greek"])), "https://example.org/rss")[0]["content"]
        calls = []
        def model(model, prompt, key):
            calls.append((model, prompt))
            if model == triage.FLASH_MODEL:
                return '{"decision":"read_more","reason":"need more context"}'
            return '{"decision":"skip","reason":"criteria mismatch"}'
        with patch.object(triage, "call_deepseek", side_effect=model), \
                patch.object(triage, "direction_context", return_value=""), \
                patch.object(triage, "CRITERIA_FILE") as criteria:
            criteria.read_text.return_value = "Match the user's criteria."
            result = triage.evaluate_message(posting, {}, "fake-key")
        self.assertEqual([model for model, _ in calls], [triage.FLASH_MODEL] * 4 + [triage.PRO_MODEL])
        self.assertIn(posting, calls[-1][1])
        self.assertIn("budget reached", result["flash"]["reason"])
        self.assertTrue(result["flash"]["windows"][-1]["budget_exhausted"])
        self.assertEqual(result["pro"]["decision"], "skip")
        self.assertLess(sum(len(prompt) for _, prompt in calls[:-1]), 10_000)

    def test_long_lines_cannot_bypass_flash_character_budget(self):
        for posting in ("A" * 12_001, "Title\nCompany\nIntro\n" + "B" * 12_001):
            with self.subTest(prefix=posting[:10]):
                with patch.object(triage, "call_deepseek", return_value='{"decision":"read_more","reason":"unclear"}') as model, \
                        patch.object(triage, "direction_context", return_value=""):
                    flag, reason, windows = triage.flash_incremental(posting, "Design", "Research", "fake-key")
                self.assertTrue(flag)
                self.assertIn("budget reached", reason)
                self.assertEqual(model.call_count, 0 if posting.startswith("A") else 1)
                for call in model.call_args_list:
                    self.assertLessEqual(len(call.args[1].split("Posting so far:\n", 1)[1]), 12_000)

    def test_character_budget_keeps_a_clear_decision_on_last_allowed_window(self):
        with patch.object(triage, "call_deepseek", return_value='{"decision":"disqualified","reason":"not hiring"}') as model:
            flag, _, windows = triage.flash_incremental("A" * 12_000, "Design", "Research", "fake-key")
        self.assertFalse(flag)
        self.assertEqual(model.call_count, 1)
        self.assertNotIn("budget_exhausted", windows[-1])

    def test_empty_content(self):
        """Empty content immediately disqualified."""
        flag, reason, _windows = triage.flash_incremental(
            "", "ML roles", "data science", "fake-key")
        self.assertFalse(flag)
        self.assertIn("empty", reason)

    def test_whitespace_only_content(self):
        """Whitespace-only lines count as empty."""
        flag, reason, _windows = triage.flash_incremental(
            "   \n\n  \n", "ML roles", "data science", "fake-key")
        self.assertFalse(flag)
        self.assertIn("empty", reason)

    def test_prompt_includes_desired_and_acceptable_roles(self):
        """The flash prompt injects desired and acceptable roles."""
        prompt = triage.FLASH_PROMPT.format(
            desired_roles="ML research", acceptable_roles="data science",
            seen="line1\nline2\nline3")
        self.assertIn("ML research", prompt)
        self.assertIn("data science", prompt)

    def test_single_line_content(self):
        """Single line of content is processed as one window."""
        with patch.object(triage, "call_deepseek",
                          return_value='{"decision": "pass_to_pro", "reason": "good job"}'):
            flag, reason, _windows = triage.flash_incremental(
                "Senior ML Engineer at TechCorp",
                "ML roles", "data science",
                "fake-key"
            )
        self.assertTrue(flag)

    def test_error_does_not_pass_to_pro_but_is_not_a_skip(self):
        """API error must not escalate to Pro — but it is a DEAD LETTER, not a skip.

        bug-hunt (dead-letterness): a Flash error is a *non-evaluation*. The loop
        correctly refuses to escalate (flag=False), but main() must label it a
        dead letter, never forge a "skip" — a non-evaluation made terminal is the
        behaviour we are hunting to eliminate.
        """
        with patch.object(triage, "call_deepseek",
                          side_effect=Exception("API timeout")):
            flag, reason, _windows = triage.flash_incremental(
                "line1\nline2\nline3\nline4",
                "ML roles", "data science",
                "fake-key"
            )
        self.assertFalse(flag)
        self.assertIn("api timeout", reason.lower())

    def test_windows_recorded_in_history(self):
        """Each window decision is recorded in the returned list."""
        responses = [
            '{"decision": "read_more", "reason": "need more"}',
            '{"decision": "pass_to_pro", "reason": "looks relevant"}',
        ]
        with patch.object(triage, "call_deepseek", side_effect=responses):
            flag, reason, windows = triage.flash_incremental(
                "line1\nline2\nline3\nline4\nline5\nline6",
                "ML", "data",
                "fake-key"
            )
        self.assertTrue(flag)
        self.assertEqual(len(windows), 2)
        self.assertEqual(windows[0]["decision"], "read_more")
        self.assertEqual(windows[0]["window_start"], 1)
        self.assertEqual(windows[0]["window_end"], 3)
        self.assertEqual(windows[1]["decision"], "pass_to_pro")
        self.assertEqual(windows[1]["window_start"], 4)


if __name__ == "__main__":
    unittest.main(verbosity=2)
