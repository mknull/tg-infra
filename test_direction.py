#!/usr/bin/env python3
"""Tests for CurrentDirection load, update, compress, and triage integration."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT = Path(__file__).resolve().parent
DIRECTION_FILE = PROJECT / "state" / "current-direction.md"
DIRECTION_SMALL_FILE = PROJECT / "state" / "current-direction-small.md"
DIRECTION_AUDIT = PROJECT / "state" / "audit" / "direction.jsonl"


class TestLoadCurrentDirection(unittest.TestCase):

    def setUp(self):
        DIRECTION_FILE.unlink(missing_ok=True)

    def tearDown(self):
        DIRECTION_FILE.unlink(missing_ok=True)

    def test_loads_file_when_present(self):
        DIRECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        DIRECTION_FILE.write_text("# Current direction\n\nPrioritize ML research.")
        from lib import load_current_direction
        text = load_current_direction()
        self.assertIn("ML research", text)

    def test_returns_empty_string_when_missing(self):
        from lib import load_current_direction
        text = load_current_direction()
        self.assertEqual(text, "")


class TestCompressDirection(unittest.TestCase):

    def setUp(self):
        DIRECTION_FILE.unlink(missing_ok=True)
        DIRECTION_SMALL_FILE.unlink(missing_ok=True)

    @patch("lib.call_deepseek")
    def test_compresses_full_to_one_sentence(self, mock_call):
        mock_call.return_value = "Prioritize ML research. Skip: data engineering, frontend."
        full = "The candidate wants ML research roles. Also exploring comp bio. No data engineering."
        from lib import compress_current_direction
        result = compress_current_direction(full, "fake-key")
        self.assertIn("ML research", result)

    @patch("lib.call_deepseek")
    def test_saves_small_file(self, mock_call):
        mock_call.return_value = "Prioritize ML research."
        from lib import compress_current_direction
        compress_current_direction("Full direction...", "fake-key")
        self.assertTrue(DIRECTION_SMALL_FILE.exists())

    @patch("lib.call_deepseek")
    def test_no_change_returns_no_change(self, mock_call):
        mock_call.return_value = "no change"
        from lib import compress_current_direction
        result = compress_current_direction("empty", "fake-key")
        self.assertTrue(DIRECTION_SMALL_FILE.exists())
        self.assertIn("no change", DIRECTION_SMALL_FILE.read_text())


class TestUpdateDirection(unittest.TestCase):

    def setUp(self):
        DIRECTION_FILE.unlink(missing_ok=True)
        DIRECTION_AUDIT.unlink(missing_ok=True)
        DIRECTION_AUDIT.parent.mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        DIRECTION_FILE.unlink(missing_ok=True)
        DIRECTION_AUDIT.unlink(missing_ok=True)

    def _mock_compress(self, full_text, api_key):
        """Helper: actually write the small file like the real function does."""
        DIRECTION_SMALL_FILE.parent.mkdir(parents=True, exist_ok=True)
        DIRECTION_SMALL_FILE.write_text("Prioritize ML research.\n")
        return "Prioritize ML research."

    @patch("lib.call_deepseek")
    @patch("lib.compress_current_direction")
    def test_writes_new_direction_and_small(self, mock_compress, mock_pro):
        """After update, both files exist."""
        DIRECTION_FILE.write_text("Old: prioritize data science.")
        mock_pro.return_value = "Prioritize ML research. Explore comp bio."
        mock_compress.side_effect = self._mock_compress

        from lib import update_current_direction
        update_current_direction("I want ML research, not data science", "fake-key")

        full = DIRECTION_FILE.read_text()
        self.assertIn("ML research", full)
        self.assertTrue(DIRECTION_SMALL_FILE.exists())

    @patch("lib.call_deepseek")
    @patch("lib.compress_current_direction")
    def test_audit_record_written(self, mock_compress, mock_pro):
        """Direction change logged to audit."""
        DIRECTION_FILE.write_text("Old direction.")
        mock_pro.return_value = "New direction."
        mock_compress.side_effect = self._mock_compress

        from lib import update_current_direction
        update_current_direction("change it", "fake-key")

        self.assertTrue(DIRECTION_AUDIT.exists())
        records = [json.loads(l) for l in
                   DIRECTION_AUDIT.read_text().splitlines() if l.strip()]
        self.assertEqual(len(records), 1)
        self.assertIn("feedback", records[0])
        self.assertEqual(records[0]["feedback"], "change it")

    @patch("lib.call_deepseek")
    @patch("lib.compress_current_direction")
    def test_no_file_creates_first_direction(self, mock_compress, mock_pro):
        """No existing direction → create from feedback."""
        mock_pro.return_value = "First direction: ML research roles."
        mock_compress.side_effect = self._mock_compress

        from lib import update_current_direction
        update_current_direction("I like ML research", "fake-key")

        self.assertTrue(DIRECTION_FILE.exists())


class TestDirectionInTriagePrompt(unittest.TestCase):

    def setUp(self):
        DIRECTION_FILE.unlink(missing_ok=True)
        DIRECTION_SMALL_FILE.unlink(missing_ok=True)

    def tearDown(self):
        DIRECTION_FILE.unlink(missing_ok=True)
        DIRECTION_SMALL_FILE.unlink(missing_ok=True)

    def test_flash_prompt_includes_direction_small(self):
        """When direction small exists, flash prompt includes it."""
        DIRECTION_SMALL_FILE.parent.mkdir(parents=True, exist_ok=True)
        DIRECTION_SMALL_FILE.write_text("Skip: data engineering, frontend.")

        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "triage", PROJECT / "it-jobs-triage.py")
        triage = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(triage)

        direction = triage._load_direction_small()
        self.assertIn("Skip", direction)


if __name__ == "__main__":
    unittest.main(verbosity=2)
