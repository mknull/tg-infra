#!/usr/bin/env python3
"""End-to-end integration tests — real code paths, mocked external APIs.

These tests verify that the plumbing is connected correctly:
channels.json → flash prompt, criteria → pro prompt, deliver() routing,
audit health with synthetic data, CurrentDirection load → compress → update.
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import json
import os
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_PROJECT = Path(__file__).resolve().parents[1]


def _criteria_path() -> Path:
    """Real generated criteria if present, else the committed example fixture.

    Generation needs gitignored source/ documents and a live DeepSeek key, so
    CI can never produce the real file. The .example is a representative
    template that exercises the same prompt-assembly path (the API is mocked).
    """
    real = _PROJECT / "state" / "it-jobs-criteria.md"
    return real if real.exists() else _PROJECT / "it-jobs-criteria.md.example"


def _load_module(filename: str, alias: str):
    spec = importlib.util.spec_from_file_location(alias, _PROJECT / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class TestChannelsToFlashPrompt(unittest.TestCase):
    """Verify channels.json flows into assembled flash prompts."""

    @classmethod
    def setUpClass(cls):
        cls.triage = _load_module("it-jobs-triage.py", "e2e_flash")

    def test_real_channels_json_parses(self):
        """Every channel in the real channels.json can produce a flash prompt."""
        with open(_PROJECT / "state" / "channels.json") as f:
            channels = json.loads(f.read())["channels"]

        self.assertGreater(len(channels), 0, "no channels configured")

        for ch in channels:
            prompt = self.triage.FLASH_PROMPT.format(
                desired_roles=ch["desired_roles"],
                acceptable_roles=ch["acceptable_roles"],
                seen="line1\nline2\nline3",
            )
            self.assertIn(ch["desired_roles"], prompt)
            self.assertIn(ch["acceptable_roles"], prompt)
            self.assertIn("disqualified", prompt)
            self.assertIn("read_more", prompt)
            self.assertIn("pass_to_pro", prompt)

    def test_flash_incremental_disqualifies_with_real_config(self):
        """Flash runs with real channels.json roles and returns disqualified."""
        with patch.object(self.triage, "call_deepseek",
                          return_value='{"decision": "disqualified", "reason": "not relevant"}'):
            with open(_PROJECT / "state" / "channels.json") as f:
                channels = json.loads(f.read())["channels"]
            ch = channels[0]
            flag, reason, windows = self.triage.flash_incremental(
                "Senior Accountant\nCPA required\n5 years experience",
                ch["desired_roles"], ch["acceptable_roles"], "fake-key")
        self.assertFalse(flag)
        self.assertEqual(len(windows), 1)

    def test_flash_incremental_passes_to_pro_with_real_config(self):
        """Flash passes a relevant role to Pro using real channels.json roles."""
        with patch.object(self.triage, "call_deepseek",
                          return_value='{"decision": "pass_to_pro", "reason": "ML research role"}'):
            with open(_PROJECT / "state" / "channels.json") as f:
                channels = json.loads(f.read())["channels"]
            ch = channels[0]
            flag, reason, windows = self.triage.flash_incremental(
                "ML Research Scientist\nDeep learning, probabilistic models\nPyTorch, Python",
                ch["desired_roles"], ch["acceptable_roles"], "fake-key")
        self.assertTrue(flag)
        self.assertIn("ML", reason)


class TestProPromptAssembly(unittest.TestCase):
    """Verify criteria file and direction flow into Pro prompts."""

    @classmethod
    def setUpClass(cls):
        cls.triage = _load_module("it-jobs-triage.py", "e2e_pro")

    def test_criteria_file_loads(self):
        """Criteria file is readable and has expected structure."""
        content = _criteria_path().read_text()
        self.assertIn("Candidate", content)
        self.assertGreater(len(content), 500)

    def test_pro_full_eval_produces_tags(self):
        """Pro eval with real criteria returns structured tags."""
        mock_json = json.dumps({
            "decision": "send",
            "reason": "matches ML research profile",
            "message": "A good role at a good company.",
            "tags": {
                "role_title": "ML Research Scientist",
                "role_type": "research",
                "seniority": "mid",
                "skills": ["python", "pytorch"],
                "tech_stack": ["pytorch", "python"],
                "domain": "probabilistic_ml",
                "location": "remote",
                "remote": True,
                "salary_range": "€50k-70k",
            },
        })
        with patch.object(self.triage, "call_deepseek", return_value=mock_json), \
             patch.object(self.triage, "CRITERIA_FILE", _criteria_path()):
            decision, reason, message, tags = self.triage.pro_full_eval(
                "ML Research Scientist. PyTorch. Remote.", "fake-key")

        self.assertEqual(decision, "send")
        self.assertIsInstance(tags, dict)
        self.assertIn("skills", tags)
        self.assertIn("seniority", tags)


class TestDeliveryRouting(unittest.TestCase):
    """Verify deliver() routes correctly with real config."""

    def tearDown(self):
        ref_map = _PROJECT / "state" / "ref-map.jsonl"
        ref_map.unlink(missing_ok=True)

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram", return_value="42")
    def test_job_match_routes_to_telegram(self, mock_send, mock_audit):
        """job_match → Telegram via delivery config."""
        from lib.delivery import deliver
        result = deliver("job_match", "Test posting")
        self.assertEqual(result, "42")

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_email")
    @patch("lib.delivery.ensure_valid_token", return_value="fake-token")
    @patch("lib.delivery.load_env", return_value={
        "OUTLOOK_CLIENT_ID": "x", "OUTLOOK_EMAIL": "test@example.com"})
    def test_weekly_report_routes_to_email(self, mock_env, mock_token,
                                           mock_send, mock_audit):
        """weekly_report → Email via delivery config."""
        from lib.delivery import deliver
        result = deliver("weekly_report", "Trends...")
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("email:"))


class TestCurrentDirectionFlow(unittest.TestCase):
    """Verify direction load → compress → update pipeline."""

    def setUp(self):
        for f in ("current-direction.md", "current-direction-small.md"):
            (_PROJECT / "state" / f).unlink(missing_ok=True)
        (_PROJECT / "state" / "audit" / "direction.jsonl").unlink(missing_ok=True)

    def test_roundtrip_load_update_compress(self):
        """Full direction lifecycle: create → compress → update."""
        import lib.direction as d

        # Create initial direction
        d.DIRECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
        d.DIRECTION_FILE.write_text("Initial direction: ML research roles.\n")

        with patch.object(d, "call_deepseek",
                          return_value="Explore computational biology."):
            new = d.update_current_direction("add comp bio", "fake-key")
            self.assertIn("computational biology", new)

        # Compression file should exist
        self.assertTrue(d.DIRECTION_SMALL_FILE.exists())
        small = d.DIRECTION_SMALL_FILE.read_text()
        self.assertGreater(len(small), 3)

        # Direction audit record written
        self.assertTrue(d.DIRECTION_AUDIT_FILE.exists())
        records = [json.loads(l) for l in
                   d.DIRECTION_AUDIT_FILE.read_text().splitlines() if l.strip()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["feedback"], "add comp bio")


class TestAuditHealthWithSyntheticData(unittest.TestCase):
    """Verify audit --health detects problems in synthetic data."""

    def setUp(self):
        self.audit_file = _PROJECT / "state" / "audit" / "telegram.jsonl"
        self.original_exists = self.audit_file.exists()
        if self.original_exists:
            self.backup = self.audit_file.read_text()

    def tearDown(self):
        if self.original_exists:
            self.audit_file.write_text(self.backup)
        else:
            self.audit_file.unlink(missing_ok=True)

    def test_detects_duplicate_evals(self):
        """Two evaluations of the same msg_id → flagged as duplicate."""
        self.audit_file.parent.mkdir(parents=True, exist_ok=True)
        base = {
            "msg_id": "test-dup-001",
            "source": "telegram",
            "sender": "test",
            "preview": "Test posting",
            "arrived_at": "2026-05-30T10:00:00Z",
            "flash": {"model": "flash", "decision": "flag",
                       "reason": "test", "at": "2026-05-30T10:00:01Z"},
            "pro": {"model": "pro", "decision": "send",
                     "reason": "test", "at": "2026-05-30T10:00:02Z"},
        }
        # Same msg_id, different eval timestamps = duplicate
        self.audit_file.write_text(
            json.dumps({**base, "flash": {**base["flash"],
                       "at": "2026-05-30T10:00:01Z"}}) + "\n" +
            json.dumps({**base, "flash": {**base["flash"],
                       "at": "2026-05-30T10:01:01Z"}}) + "\n"
        )
        exit_code = os.system(f"{_PROJECT}/audit --health --days 1 2>/dev/null")
        self.assertNotEqual(0, exit_code, "should detect duplicate evals")


class TestGenerateProfilePromptStructure(unittest.TestCase):
    """Verify generate-profile.py prompts are well-formed."""

    @classmethod
    def setUpClass(cls):
        cls.gen = _load_module("generate-profile.py", "e2e_profile")

    def test_extract_prompt_is_well_formed(self):
        """_EXTRACT_PROMPT has the required structure."""
        prompt = self.gen._EXTRACT_PROMPT
        self.assertIn("{document}", prompt)
        self.assertIn("DOCUMENT:", prompt)
        self.assertGreater(len(prompt), 100)

    def test_source_prompt_is_well_formed(self):
        """_SOURCE_PROMPT has the required structure."""
        prompt = self.gen._SOURCE_PROMPT
        self.assertIn("{facts}", prompt)
        self.assertIn("###FILE:", prompt)
        self.assertIn("interests.txt", prompt)
        self.assertIn("skills.txt", prompt)
        self.assertIn("tech_stack.txt", prompt)

    def test_source_prompt_does_not_prescribe_themes(self):
        """The synthesis prompt instructs method, not content."""
        prompt = self.gen._SOURCE_PROMPT
        self.assertNotIn("mechanistic models of cognition", prompt)
        self.assertNotIn("deep-learning implementation", prompt)
        # Check it's method-driven
        self.assertIn("discover", prompt.lower())


if __name__ == "__main__":
    unittest.main(verbosity=2)
