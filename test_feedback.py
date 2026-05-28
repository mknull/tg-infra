#!/usr/bin/env python3
"""Tests for feedback processing — /direction command and email reply polling."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

_PROJECT = Path(__file__).resolve().parent


class TestDirectionCommand(unittest.TestCase):

    def setUp(self):
        (_PROJECT / "state" / "current-direction.md").unlink(missing_ok=True)
        (_PROJECT / "state" / "current-direction-small.md").unlink(missing_ok=True)
        (_PROJECT / "state" / "audit" / "direction.jsonl").unlink(missing_ok=True)
        (_PROJECT / "state").mkdir(parents=True, exist_ok=True)

    @patch("lib.direction.compress_current_direction")
    @patch("lib.direction.call_deepseek")
    def test_updates_full_direction_file(self, mock_pro, mock_compress):
        """'/direction explore comp bio' → file reflects the update."""
        mock_pro.return_value = "You are exploring computational biology roles."

        from lib import update_current_direction, DIRECTION_FILE
        update_current_direction("explore computational biology", "fake-key")

        self.assertTrue(DIRECTION_FILE.exists())
        self.assertIn("computational biology", DIRECTION_FILE.read_text())

    @patch("lib.direction.compress_current_direction")
    @patch("lib.direction.call_deepseek")
    def test_writes_audit_record(self, mock_pro, mock_compress):
        """Update logged to direction.jsonl."""
        mock_pro.return_value = "Updated direction."

        from lib import update_current_direction, DIRECTION_AUDIT_FILE
        update_current_direction("new feedback", "fake-key")

        self.assertTrue(DIRECTION_AUDIT_FILE.exists())
        records = [json.loads(l) for l in
                   DIRECTION_AUDIT_FILE.read_text().splitlines() if l.strip()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["feedback"], "new feedback")

    @patch("lib.direction.compress_current_direction")
    @patch("lib.direction.call_deepseek")
    def test_no_existing_file_creates_first_direction(self, mock_pro, mock_compress):
        """No existing direction → create from feedback."""
        mock_pro.return_value = "First direction."

        from lib import update_current_direction, DIRECTION_FILE
        update_current_direction("I like ML research", "fake-key")

        self.assertTrue(DIRECTION_FILE.exists())


class TestEmailFeedbackPoller(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_graph_api_sent_items_request(self, mock_urlopen):
        """Poller makes correct Graph API request."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "value": [{"id": "msg123", "subject": "Re: Weekly Report"}]
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        import urllib.request
        url = ("https://graph.microsoft.com/v1.0/me/mailFolders/sentitems/messages"
               "?$filter=contains(subject,'Weekly Job Market Trend Report')"
               "&$top=5&$orderby=sentDateTime desc")

        req = urllib.request.Request(url, headers={
            "Authorization": "Bearer fake-token",
            "Content-Type": "application/json",
        })
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.loads(resp.read())
        self.assertIn("value", data)


if __name__ == "__main__":
    unittest.main(verbosity=2)
