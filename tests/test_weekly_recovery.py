#!/usr/bin/env python3
"""Tests for the weekly-report recovery cycle: stable week anchoring, the
fail-closed ledger, and idempotent send."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import sys
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from lib import weekly_ledger as wl

# Load under a unique module name so we don't clobber sys.modules["weekly_trend"],
# which test_weekly.py's string-based @patch("weekly_trend.call_deepseek") resolves
# against. We patch via patch.object(wt, ...), so the registered name is irrelevant.
_PROJECT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "weekly_trend_recovery", _PROJECT / "weekly_trend.py")
wt = importlib.util.module_from_spec(_spec)
sys.modules["weekly_trend_recovery"] = wt
_spec.loader.exec_module(wt)


def _dt(s):
    return datetime.fromisoformat(s).replace(tzinfo=timezone.utc)


class TestWeekAnchor(unittest.TestCase):

    def test_sunday_and_following_days_share_week(self):
        """The scheduled Sunday run and any recovery run Mon..Wed must resolve
        to the same week key and the same subject — the dedup invariant."""
        sunday = wt.report_week(_dt("2026-06-14T10:00:00"))
        monday = wt.report_week(_dt("2026-06-15T12:40:00"))
        wednesday = wt.report_week(_dt("2026-06-17T22:40:00"))
        self.assertEqual(sunday["week_key"], "2026-06-14")
        self.assertEqual(monday["week_key"], "2026-06-14")
        self.assertEqual(wednesday["week_key"], "2026-06-14")
        self.assertEqual(sunday["subject"], wednesday["subject"])
        self.assertEqual(sunday["start"], "2026-06-07")
        self.assertEqual(sunday["end"], "2026-06-14")

    def test_next_sunday_rolls_to_new_week(self):
        nxt = wt.report_week(_dt("2026-06-21T10:00:00"))
        self.assertEqual(nxt["week_key"], "2026-06-21")

    def test_window_open_sun_to_wed_only(self):
        self.assertTrue(wl.window_open(_dt("2026-06-14T10:00:00")))   # Sun
        self.assertTrue(wl.window_open(_dt("2026-06-17T23:00:00")))   # Wed
        self.assertFalse(wl.window_open(_dt("2026-06-18T00:40:00")))  # Thu
        self.assertFalse(wl.window_open(_dt("2026-06-20T12:00:00")))  # Sat


class TestLedger(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(wl, "LEDGER_DIR", Path(self._tmp.name))
        self._patch.start()

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_unknown_is_recorded_with_reason(self):
        led = wl.update_ledger("2026-06-14", status="unknown",
                               period=["2026-06-07", "2026-06-14"],
                               reason="sent_items_unreachable")
        self.assertEqual(led["status"], "unknown")
        self.assertEqual(led["attempts"], 1)
        self.assertEqual(led["last_reason"], "sent_items_unreachable")
        self.assertIsNone(led["sent_at"])
        # Survives a reload (persisted).
        self.assertEqual(wl.load_ledger("2026-06-14")["last_reason"],
                         "sent_items_unreachable")

    def test_success_supersedes_failure_and_marks_recovered(self):
        wl.update_ledger("2026-06-14", status="unknown", reason="network_down")
        led = wl.update_ledger("2026-06-14", status="sent")
        self.assertEqual(led["status"], "sent")
        self.assertEqual(led["attempts"], 2)
        self.assertTrue(led["recovered"])     # took more than one attempt
        self.assertIsNotNone(led["sent_at"])

    def test_clean_first_try_is_not_recovered(self):
        led = wl.update_ledger("2026-06-14", status="sent")
        self.assertEqual(led["attempts"], 1)
        self.assertFalse(led["recovered"])

    def test_sent_is_terminal_never_downgraded(self):
        wl.update_ledger("2026-06-14", status="sent")
        led = wl.update_ledger("2026-06-14", status="unknown", reason="late tick")
        self.assertEqual(led["status"], "sent")
        self.assertEqual(led["attempts"], 1)   # terminal: not incremented


class TestSentItemsCheck(unittest.TestCase):

    def test_present_returns_true(self):
        resp = {"value": [{"subject": "Weekly Job Market Trend Report (a – b)"}]}
        with patch.object(wl, "graph_get", return_value=resp):
            self.assertIs(wl.report_in_sent_items(
                "tok", "Weekly Job Market Trend Report (a – b)", "f"), True)

    def test_absent_returns_false(self):
        resp = {"value": [{"subject": "Some other email"}]}
        with patch.object(wl, "graph_get", return_value=resp):
            self.assertIs(wl.report_in_sent_items(
                "tok", "Weekly Job Market Trend Report (a – b)", "f"), False)

    def test_network_error_returns_none(self):
        with patch.object(wl, "graph_get", side_effect=OSError("name resolution")):
            self.assertIsNone(wl.report_in_sent_items("tok", "subj", "f"))

    def test_unexpected_shape_returns_none(self):
        with patch.object(wl, "graph_get", return_value={"error": "throttled"}):
            self.assertIsNone(wl.report_in_sent_items("tok", "subj", "f"))


class TestEnsureWeekReport(unittest.TestCase):

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self._patch = patch.object(wl, "LEDGER_DIR", Path(self._tmp.name))
        self._patch.start()
        self.env = {"DEEPSEEK_API_KEY": "k", "OUTLOOK_EMAIL": "u@example.com"}
        self.now = _dt("2026-06-14T10:00:00")

    def tearDown(self):
        self._patch.stop()
        self._tmp.cleanup()

    def test_fail_closed_when_sent_items_unknown(self):
        """Graph unreachable → unknown, and crucially NO send."""
        with patch.object(wt, "ensure_valid_token", return_value="tok"), \
             patch.object(wt, "report_in_sent_items", return_value=None), \
             patch.object(wt, "send_email") as send:
            status = wt.ensure_week_report(self.env, now=self.now)
        self.assertEqual(status, "unknown")
        send.assert_not_called()
        self.assertEqual(wl.load_ledger("2026-06-14")["status"], "unknown")

    def test_already_in_sent_items_marks_sent_without_sending(self):
        with patch.object(wt, "ensure_valid_token", return_value="tok"), \
             patch.object(wt, "report_in_sent_items", return_value=True), \
             patch.object(wt, "send_email") as send:
            status = wt.ensure_week_report(self.env, now=self.now)
        self.assertEqual(status, "sent")
        send.assert_not_called()

    def test_confirmed_not_sent_with_records_sends(self):
        email = {"empty": False, "subject": "S", "body": "B", "attachments": [],
                 "records": [1], "agg": {"total_tagged": 1}, "sends": []}
        with patch.object(wt, "ensure_valid_token", return_value="tok"), \
             patch.object(wt, "report_in_sent_items", return_value=False), \
             patch.object(wt, "_build_email", return_value=email), \
             patch.object(wt, "send_email") as send:
            status = wt.ensure_week_report(self.env, now=self.now)
        self.assertEqual(status, "sent")
        send.assert_called_once()
        self.assertEqual(wl.load_ledger("2026-06-14")["status"], "sent")

    def test_confirmed_not_sent_but_empty_does_not_send(self):
        email = {"empty": True, "records": [], "agg": {"total_tagged": 0}, "sends": []}
        with patch.object(wt, "ensure_valid_token", return_value="tok"), \
             patch.object(wt, "report_in_sent_items", return_value=False), \
             patch.object(wt, "_build_email", return_value=email), \
             patch.object(wt, "send_email") as send:
            status = wt.ensure_week_report(self.env, now=self.now)
        self.assertEqual(status, "empty")
        send.assert_not_called()

    def test_send_failure_records_unknown(self):
        email = {"empty": False, "subject": "S", "body": "B", "attachments": [],
                 "records": [1], "agg": {"total_tagged": 1}, "sends": []}
        with patch.object(wt, "ensure_valid_token", return_value="tok"), \
             patch.object(wt, "report_in_sent_items", return_value=False), \
             patch.object(wt, "_build_email", return_value=email), \
             patch.object(wt, "send_email", side_effect=OSError("name resolution")):
            status = wt.ensure_week_report(self.env, now=self.now)
        self.assertEqual(status, "unknown")
        self.assertIn("send:", wl.load_ledger("2026-06-14")["last_reason"])

    def test_token_failure_fails_closed(self):
        with patch.object(wt, "ensure_valid_token", side_effect=OSError("dns")), \
             patch.object(wt, "send_email") as send:
            status = wt.ensure_week_report(self.env, now=self.now)
        self.assertEqual(status, "unknown")
        send.assert_not_called()

    def test_terminal_week_is_noop(self):
        wl.update_ledger("2026-06-14", status="sent")
        with patch.object(wt, "ensure_valid_token") as tok, \
             patch.object(wt, "send_email") as send:
            status = wt.ensure_week_report(self.env, now=self.now)
        self.assertEqual(status, "sent")
        tok.assert_not_called()
        send.assert_not_called()


if __name__ == "__main__":
    unittest.main(verbosity=2)
