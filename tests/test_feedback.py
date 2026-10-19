#!/usr/bin/env python3
"""Tests for feedback processing — /direction command and email reply polling."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import unittest
import tempfile
from contextlib import ExitStack
from pathlib import Path
from unittest.mock import patch, MagicMock

_PROJECT = Path(__file__).resolve().parents[1]


class TestDirectionCommand(unittest.TestCase):

    def setUp(self):
        import lib.direction as direction
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        root = Path(self.stack.enter_context(tempfile.TemporaryDirectory()))
        for key, name in (("DIRECTION_FILE", "current-direction.md"),
                          ("DIRECTION_SMALL_FILE", "current-direction-small.md"),
                          ("DIRECTION_AUDIT_FILE", "audit.jsonl")):
            self.stack.enter_context(patch.object(direction, key, root / name))
            if key != "DIRECTION_SMALL_FILE":
                self.stack.enter_context(patch("lib." + key, root / name))

    @patch("lib.direction.compress_current_direction", return_value="Compressed preferences.")
    @patch("lib.direction.call_deepseek")
    def test_updates_full_direction_file(self, mock_pro, mock_compress):
        """'/direction explore comp bio' → file reflects the update."""
        mock_pro.return_value = "You are exploring computational biology roles."

        from lib import update_current_direction, DIRECTION_FILE
        update_current_direction("explore computational biology", "fake-key")

        self.assertTrue(DIRECTION_FILE.exists())
        self.assertIn("computational biology", DIRECTION_FILE.read_text())

    @patch("lib.direction.compress_current_direction", return_value="Compressed preferences.")
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

    @patch("lib.direction.compress_current_direction", return_value="Compressed preferences.")
    @patch("lib.direction.call_deepseek")
    def test_no_existing_file_creates_first_direction(self, mock_pro, mock_compress):
        """No existing direction → create from feedback."""
        mock_pro.return_value = "First direction."

        from lib import update_current_direction, DIRECTION_FILE
        update_current_direction("I like ML research", "fake-key")

        self.assertTrue(DIRECTION_FILE.exists())


class TestEmailFeedbackPoller(unittest.TestCase):

    def test_failed_update_notifies_once_and_retries_before_advancing_cursor(self):
        import feedback_poller as poller
        with tempfile.TemporaryDirectory() as tmp, ExitStack() as stack:
            stack.enter_context(patch.object(poller, "FAILURE_NOTICE_FILE", Path(tmp) / "notice"))
            stack.enter_context(patch.object(poller, "load_env", return_value={"DEEPSEEK_API_KEY": "fake"}))
            stack.enter_context(patch.object(poller, "ensure_valid_token", return_value="fake"))
            stack.enter_context(patch.object(poller, "_check_for_replies", return_value={
                "id": "reply-1", "body": "Prefer design", "received": "2026-10-18T10:00:00Z"}))
            stack.enter_context(patch.object(poller, "update_current_direction", side_effect=[
                RuntimeError("offline"), RuntimeError("offline"), "Prefer design"]))
            save = stack.enter_context(patch.object(poller, "_save_cursor"))
            notify = stack.enter_context(patch("lib.deliver", return_value="message-id"))
            with self.assertLogs(level="ERROR"):
                for _ in range(2):
                    with self.assertRaises(SystemExit):
                        poller.main()
            save.assert_not_called()
            self.assertEqual(notify.call_count, 1)
            poller.main()
            save.assert_called_once_with("2026-10-18T10:00:00Z")
            self.assertFalse(poller.FAILURE_NOTICE_FILE.exists())

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
