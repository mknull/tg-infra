#!/usr/bin/env python3
"""Branch coverage: partial branches in lib, weekly-trend, outlook-auth."""

import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_PROJECT = Path(__file__).resolve().parent


# ---------------------------------------------------------------------------
# lib/config.py — env var fallback, exception, missing file
# ---------------------------------------------------------------------------

class TestConfigBranches(unittest.TestCase):

    def setUp(self):
        self.env_file = _PROJECT / "state" / ".env"
        self.orig = None
        if self.env_file.exists():
            self.orig = self.env_file.read_text()

    def tearDown(self):
        if self.orig is not None:
            self.env_file.write_text(self.orig)

    def test_get_config_env_var_first(self):
        """Environment variable takes priority over .env."""
        with patch.dict("os.environ", {"USER_NAME": "env-user"}, clear=False):
            import lib.config as cfg
            result = cfg._get_config("USER_NAME", "default")
            self.assertEqual(result, "env-user")

    def test_get_config_falls_back_to_env_file(self):
        self.env_file.write_text("USER_NAME=file-user\n")
        with patch.dict("os.environ", {}, clear=True):
            import lib.config as cfg
            result = cfg._get_config("USER_NAME", "default")
            self.assertEqual(result, "file-user")

    def test_get_config_returns_default_when_missing(self):
        with patch.dict("os.environ", {}, clear=True):
            self.env_file.unlink(missing_ok=True)
            import lib.config as cfg
            result = cfg._get_config("NONEXISTENT_KEY", "fallback")
            self.assertEqual(result, "fallback")


# ---------------------------------------------------------------------------
# lib/api.py — extract_json error path
# ---------------------------------------------------------------------------

class TestAPIBranches(unittest.TestCase):

    def test_extract_json_raises_on_no_braces(self):
        from lib.api import extract_json
        with self.assertRaises(ValueError):
            extract_json("plain text with no json")


# ---------------------------------------------------------------------------
# lib/delivery.py — config merge, file attachment, ref-map
# ---------------------------------------------------------------------------

class TestDeliveryPartialBranches(unittest.TestCase):

    def setUp(self):
        self.config_path = _PROJECT / "state" / "delivery.json"
        self.orig = None
        if self.config_path.exists():
            self.orig = self.config_path.read_text()

    def tearDown(self):
        if self.orig is not None:
            self.config_path.write_text(self.orig)
        else:
            self.config_path.unlink(missing_ok=True)
        (_PROJECT / "state" / "ref-map.jsonl").unlink(missing_ok=True)

    def test_config_merge_preserves_defaults(self):
        """User config partial override preserves unset keys."""
        self.config_path.write_text(json.dumps({
            "routes": {"job_match": "email"},
        }))
        from lib.delivery import load_delivery_config
        cfg = load_delivery_config()
        self.assertEqual(cfg["routes"]["job_match"], "email")
        self.assertEqual(cfg["routes"]["weekly_report"], "email")  # default

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram_document", return_value="doc_42")
    def test_deliver_file_attachment(self, mock_doc, mock_audit):
        """File attachment path routes to send_telegram_document."""
        from lib.delivery import deliver
        result = deliver("brief", "caption text",
                         file_bytes=b"pdf-content",
                         file_name="brief.pdf")
        self.assertEqual(result, "doc_42")
        mock_doc.assert_called_once()

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram", return_value="msg_99")
    def test_deliver_writes_ref_map_with_ref(self, mock_send, mock_audit):
        """Ref parameter triggers ref-map write on success."""
        from lib.delivery import deliver
        result = deliver("job_match", "posting", ref="internal-123")
        self.assertEqual(result, "msg_99")
        ref_map = _PROJECT / "state" / "ref-map.jsonl"
        self.assertTrue(ref_map.exists())
        records = [json.loads(l) for l in
                   ref_map.read_text().splitlines() if l.strip()]
        self.assertEqual(records[0]["ref"], "internal-123")


# ---------------------------------------------------------------------------
# weekly-trend.py — aggregation, extraction, main() flow
# ---------------------------------------------------------------------------

WEEKLY_TAG = {
    "role_title": "Frontend Lead",
    "role_type": "engineering",
    "seniority": "lead",
    "skills": ["typescript", "react"],
    "tech_stack": ["react", "node"],
    "domain": "frontend",
    "location": "remote",
    "remote": True,
    "salary_range": "€60k-80k",
}

WEEKLY_RECORD_SKIP = {
    "msg_id": "test-skip-1",
    "source": "telegram",
    "sender": "test",
    "preview": "Accounting role",
    "arrived_at": "2026-06-01T10:00:00Z",
    "flash": {"decision": "skip", "reason": "not relevant"},
}

WEEKLY_RECORD_SEND = {
    "msg_id": "test-send-1",
    "source": "telegram",
    "sender": "test",
    "preview": "Frontend role",
    "arrived_at": "2026-06-01T11:00:00Z",
    "flash": {"decision": "flag", "reason": "looks relevant"},
    "pro": {"decision": "send", "reason": "matches profile",
            "tags": dict(WEEKLY_TAG)},
}


class TestWeeklyBranches(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "wt", _PROJECT / "weekly-trend.py")
        cls.wt = importlib.util.module_from_spec(spec)
        import sys
        sys.modules["wt_test"] = cls.wt
        spec.loader.exec_module(cls.wt)

    def test_extract_tagged_filters_records_with_tags(self):
        """Only records with pro.tags are returned."""
        tagged, sends = self.wt.extract_tagged([
            WEEKLY_RECORD_SKIP,
            WEEKLY_RECORD_SEND,
        ])
        self.assertEqual(len(tagged), 1)
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]["pro"]["tags"]["role_title"],
                         "Frontend Lead")

    def test_extract_tagged_empty_list(self):
        """Empty input returns empty output."""
        tagged, sends = self.wt.extract_tagged([])
        self.assertEqual(len(tagged), 0)
        self.assertEqual(len(sends), 0)

    def test_extract_tagged_skips_missing_tags(self):
        """Records without tags dict are skipped."""
        record = dict(WEEKLY_RECORD_SEND)
        record["pro"]["tags"] = None
        tagged, sends = self.wt.extract_tagged([record])
        self.assertEqual(len(tagged), 0)

    def test_aggregate_counts_correctly(self):
        """Aggregation produces correct counts and sets."""
        tagged = [WEEKLY_RECORD_SEND, WEEKLY_RECORD_SEND]
        agg = self.wt.aggregate(tagged)
        self.assertEqual(agg["total_tagged"], 2)
        self.assertIn("typescript", agg["skills"])
        self.assertIn("engineering", agg["role_types"])
        self.assertEqual(agg["remote"], 2)
        self.assertEqual(agg["onsite"], 0)

    def test_aggregate_empty_input(self):
        """Empty list returns zeros."""
        agg = self.wt.aggregate([])
        self.assertEqual(agg["total_tagged"], 0)

    def test_build_raw_section_includes_recent_matches(self):
        """Raw stats section includes role titles from sends."""
        sends = [WEEKLY_RECORD_SEND]
        result = self.wt.build_raw_section(
            {"role_types": {}, "skills": ["typescript"],
             "tech_stack": ["react"], "domains": {}, "seniority": {},
             "remote": 1, "onsite": 0}, sends)
        self.assertIn("Frontend Lead", result)

    def test_build_report_returns_text(self):
        """build_report calls DeepSeek and returns text."""
        agg = {
            "total_tagged": 1,
            "role_types": {"engineering": 1},
            "skills": ["typescript"],
            "tech_stack": ["react"],
            "domains": {"frontend": 1},
            "seniority": {"lead": 1},
            "remote": 1,
            "onsite": 0,
        }
        with patch.object(self.wt, "call_deepseek",
                          return_value="Trend analysis paragraph."):
            result = self.wt.build_report("Profile text", agg,
                                           [WEEKLY_RECORD_SEND], "fake-key")
        self.assertIn("Trend analysis", result)

    def test_build_smell_section_with_no_issues_returns_empty(self):
        """Clean week produces empty smell section."""
        with patch.object(self.wt, "call_deepseek",
                          return_value="No suspicious patterns detected."):
            result = self.wt._build_smell_section(
                [WEEKLY_RECORD_SEND], [WEEKLY_RECORD_SEND],
                "profile", "direction", "fake-key")
        self.assertEqual(result, "")


# ---------------------------------------------------------------------------
# outlook-auth.py — device code flow
# ---------------------------------------------------------------------------

class TestOutlookAuth(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        import importlib.util
        spec = importlib.util.spec_from_file_location(
            "oa", _PROJECT / "outlook-auth.py")
        cls.oa = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(cls.oa)

    @patch("urllib.request.urlopen")
    def test_device_code_flow_success(self, mock_urlopen):
        """Full device-code flow: request code → poll → save."""
        device_resp = MagicMock()
        device_resp.read.return_value = json.dumps({
            "device_code": "dev-code-123",
            "user_code": "ABC123",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 900,
            "interval": 5,
        }).encode()
        token_resp = MagicMock()
        token_resp.read.return_value = json.dumps({
            "access_token": "outlook-at",
            "refresh_token": "outlook-rt",
            "expires_in": 3600,
        }).encode()
        mock_urlopen.return_value.__enter__.side_effect = [
            device_resp, token_resp]

        with patch.object(self.oa, "save_token") as mock_save, \
             patch.dict("os.environ", {"OUTLOOK_CLIENT_ID": "fake-cid"}):
            self.oa.main()
            mock_save.assert_called_once()
            self.assertEqual(mock_save.call_args[0][0]["access_token"],
                             "outlook-at")

    @patch("urllib.request.urlopen")
    def test_device_code_timeout(self, mock_urlopen):
        """Poll loop times out after expires_in."""
        device_resp = MagicMock()
        device_resp.read.return_value = json.dumps({
            "device_code": "dev-code-456",
            "user_code": "XYZ789",
            "verification_uri": "https://microsoft.com/devicelogin",
            "expires_in": 1,
            "interval": 1,
        }).encode()
        pending_resp = MagicMock()
        pending_resp.read.return_value = json.dumps({
            "error": "authorization_pending",
        }).encode()
        mock_urlopen.return_value.__enter__.side_effect = [
            device_resp, pending_resp]

        with patch.dict("os.environ", {"OUTLOOK_CLIENT_ID": "fake-cid"}):
            with self.assertRaises(SystemExit):
                self.oa.main()


# ---------------------------------------------------------------------------
# lib/direction.py — compress empty text
# ---------------------------------------------------------------------------

class TestDirectionBranches(unittest.TestCase):

    def test_compress_empty_text_returns_no_change(self):
        from lib.direction import compress_current_direction
        result = compress_current_direction("", "fake-key")
        self.assertEqual(result, "no change")


if __name__ == "__main__":
    unittest.main(verbosity=2)
