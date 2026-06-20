#!/usr/bin/env python3
"""Tests for weekly-trend.py — smell investigation and report structure."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "weekly_trend", _PROJECT / "weekly-trend.py")
wt = importlib.util.module_from_spec(_spec)
sys.modules["weekly_trend"] = wt
_spec.loader.exec_module(wt)


class TestSmellInvestigation(unittest.TestCase):

    def test_empty_tagged_returns_empty_smell(self):
        """No tagged records → no smells."""
        result = wt._build_smell_section([], [], [], "profile", "direction", "fake-key")
        self.assertEqual(result, "")

    @patch("weekly_trend.call_deepseek")
    def test_prompt_includes_profile_and_direction(self, mock_call):
        """Smell prompt gets profile context and CurrentDirection."""
        mock_call.return_value = "No suspicious patterns found."
        tagged = [{"pro": {"tags": {"role_title": "ML Eng", "skills": ["python"]},
                            "decision": "send"}}]
        wt._build_smell_section(tagged, [], tagged, "Profile text", "Explore comp bio", "fake-key")
        prompt = mock_call.call_args[0][1]  # second positional arg is the prompt
        self.assertIn("Profile text", prompt)
        self.assertIn("Explore comp bio", prompt)

    @patch("weekly_trend.call_deepseek")
    def test_flags_sent_but_no_engagement(self, mock_call):
        """Job was sent but user didn't engage — surfaced as smell."""
        mock_call.return_value = "Data science roles sent but ignored."
        tagged = [
            {"pro": {"tags": {"role_title": "Data Scientist", "skills": ["sql"]},
                     "decision": "send"}},
            {"pro": {"tags": {"role_title": "Data Scientist", "skills": ["sql"]},
                     "decision": "send"}},
        ]
        # Both sent, but not in sends list (no engagement)
        result = wt._build_smell_section(tagged, [], tagged, "profile", "direction", "fake-key")
        self.assertIn("Data science", result)

    @patch("weekly_trend.call_deepseek")
    def test_flags_flash_disqualified_with_relevant_tags(self, mock_call):
        """Flash said skip but tags match profile — surfaced as false negative risk."""
        mock_call.return_value = "ML role disqualified by Flash, may be relevant."
        tagged = [
            {"flash": {"decision": "skip"},
             "pro": {"tags": {"role_title": "ML Researcher", "skills": ["pytorch"],
                              "domain": "probabilistic_ml"}}},
        ]
        result = wt._build_smell_section(tagged, [], tagged, "profile", "direction", "fake-key")
        self.assertIn("ML", result)

    @patch("weekly_trend.call_deepseek")
    def test_no_smells_returns_empty(self, mock_call):
        """Clean week — no suspicious patterns."""
        mock_call.return_value = "No suspicious patterns detected."
        tagged = [{"pro": {"tags": {"role_title": "X"}, "decision": "send"}}]
        sends = tagged  # all sent jobs were engaged with
        result = wt._build_smell_section(tagged, sends, tagged, "p", "d", "fake-key")
        self.assertEqual(result, "")


class TestDirectionAttachment(unittest.TestCase):

    def setUp(self):
        _PROJECT.joinpath("state", "current-direction.md").unlink(missing_ok=True)
        _PROJECT.joinpath("state", "current-direction-small.md").unlink(missing_ok=True)

    def test_returns_none_when_no_files(self):
        """No direction files → no attachments."""
        result = wt._get_direction_attachments()
        self.assertEqual(result, [])

    def test_returns_both_when_present(self):
        """Both direction files present → two attachments."""
        (_PROJECT / "state").mkdir(parents=True, exist_ok=True)
        (_PROJECT / "state" / "current-direction.md").write_text("Full direction.")
        (_PROJECT / "state" / "current-direction-small.md").write_text("Small direction.")

        result = wt._get_direction_attachments()
        self.assertEqual(len(result), 2)
        names = [a["filename"] for a in result]
        self.assertIn("current-direction.md", names)
        self.assertIn("current-direction-small.txt", names)


if __name__ == "__main__":
    unittest.main(verbosity=2)
