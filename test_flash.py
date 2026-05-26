#!/usr/bin/env python3
"""Tests for unified 3-line incremental flash strategy."""

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "triage", _PROJECT / "it-jobs-triage.py")
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

    def test_error_returns_default_skip(self):
        """API error → skip (conservative, don't pass bad data to Pro)."""
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
