#!/usr/bin/env python3
"""Tests for email-triage batch orchestration — the zero-dead-loss invariant.

The cursor must never advance past an unresolved email (no loss), an already
resolved email must never be reprocessed (no duplicate delivery), and a
permanently-failing email must eventually dead-letter (bounded) and do so
loudly (recorded + alerted).
"""

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "email_triage", _PROJECT / "email-triage.py")
et = importlib.util.module_from_spec(_spec)
sys.modules["email_triage"] = et
_spec.loader.exec_module(et)


def _email(eid: str, received: str) -> dict:
    return {
        "id": eid,
        "receivedDateTime": received,
        "from": {"emailAddress": {"address": f"{eid}@example.com"}},
        "subject": f"subject-{eid}",
    }


def _make_triage(behaviour):
    """Build a stub triage_one driven by {id: 'ok'|'fail'}."""
    def triage_one(msg):
        eid = msg["id"]
        record = {"msg_id": eid, "source": "email",
                  "sender": f"{eid}@example.com", "preview": f"subject-{eid}"}
        if behaviour.get(eid, "ok") == "ok":
            record["flash"] = {"decision": "skip", "reason": "ok"}
            return record, True
        record["flash"] = {"decision": "error", "reason": "boom"}
        return record, False
    return triage_one


class TestAdvanceBatch(unittest.TestCase):

    def test_all_resolve_advances_to_last(self):
        emails = [_email("e1", "2026-06-06T10:00:00Z"),
                  _email("e2", "2026-06-06T10:01:00Z"),
                  _email("e3", "2026-06-06T10:02:00Z")]
        res = et.advance_batch(emails, _make_triage({}), {}, now_iso="2026-06-06T11:00:00Z")
        self.assertEqual(res["cursor"], "2026-06-06T10:02:00Z")
        self.assertEqual(res["deadletters"], [])
        self.assertEqual(res["failures"], {})
        self.assertEqual(len(res["records"]), 3)

    def test_cursor_is_verbatim_no_plus_one_second(self):
        """Cursor must be the exact receivedDateTime, not a +1s rounding."""
        emails = [_email("e1", "2026-06-06T10:00:00.512Z")]
        res = et.advance_batch(emails, _make_triage({}), {}, now_iso="2026-06-06T11:00:00Z")
        self.assertEqual(res["cursor"], "2026-06-06T10:00:00.512Z")

    def test_failure_does_not_advance_past_and_stops_batch(self):
        """e2 fails transiently: cursor pins at e1, e3 is NOT processed (no loss)."""
        emails = [_email("e1", "2026-06-06T10:00:00Z"),
                  _email("e2", "2026-06-06T10:01:00Z"),
                  _email("e3", "2026-06-06T10:02:00Z")]
        failures = {}
        res = et.advance_batch(emails, _make_triage({"e2": "fail"}), failures,
                               now_iso="2026-06-06T11:00:00Z")
        # cursor stays at the last resolved-or-dead email below the failure
        self.assertEqual(res["cursor"], "2026-06-06T10:00:00Z")
        # batch stopped at e2 — e3 was never touched, so it can't be lost
        self.assertEqual([r["msg_id"] for r in res["records"]], ["e1", "e2"])
        self.assertEqual(res["failures"]["e2"]["attempts"], 1)
        self.assertEqual(res["deadletters"], [])

    def test_first_email_fails_leaves_cursor_unmoved(self):
        emails = [_email("e1", "2026-06-06T10:00:00Z"),
                  _email("e2", "2026-06-06T10:01:00Z")]
        res = et.advance_batch(emails, _make_triage({"e1": "fail"}), {},
                               now_iso="2026-06-06T11:00:00Z")
        self.assertIsNone(res["cursor"])
        self.assertEqual([r["msg_id"] for r in res["records"]], ["e1"])

    def test_transient_failure_recovers_without_deadletter(self):
        """Fail once, then succeed next run — never dead-letters, never lost."""
        emails = [_email("e1", "2026-06-06T10:00:00Z")]
        failures = {}
        r1 = et.advance_batch(emails, _make_triage({"e1": "fail"}), failures,
                              now_iso="2026-06-06T11:00:00Z")
        self.assertEqual(r1["failures"]["e1"]["attempts"], 1)
        self.assertEqual(r1["deadletters"], [])
        # next run: same email re-fetched (cursor never advanced), now succeeds
        r2 = et.advance_batch(emails, _make_triage({"e1": "ok"}), r1["failures"],
                              now_iso="2026-06-06T13:00:00Z")
        self.assertEqual(r2["cursor"], "2026-06-06T10:00:00Z")
        self.assertNotIn("e1", r2["failures"])
        self.assertEqual(r2["deadletters"], [])

    def test_permanent_failure_deadletters_after_max_and_unblocks(self):
        """A poison email dead-letters after MAX, then the pipeline moves on."""
        poison = _email("e2", "2026-06-06T10:01:00Z")
        good = _email("e3", "2026-06-06T10:02:00Z")
        failures = {}
        # runs 1..MAX-1: e2 keeps failing, batch stops before e3 (no loss)
        for run in range(et.MAX_DEADLETTER_ATTEMPTS - 1):
            res = et.advance_batch([poison, good], _make_triage({"e2": "fail"}),
                                   failures, now_iso="2026-06-06T11:00:00Z")
            self.assertEqual(res["deadletters"], [])
            self.assertEqual([r["msg_id"] for r in res["records"]], ["e2"])
            failures = res["failures"]
        # final run: e2 exhausts budget → dead-lettered, stepped over, e3 processed
        res = et.advance_batch([poison, good], _make_triage({"e2": "fail"}),
                               failures, now_iso="2026-06-06T15:00:00Z")
        self.assertEqual(len(res["deadletters"]), 1)
        self.assertEqual(res["deadletters"][0]["msg_id"], "e2")
        self.assertEqual(res["deadletters"][0]["attempts"], et.MAX_DEADLETTER_ATTEMPTS)
        self.assertEqual([r["msg_id"] for r in res["records"]], ["e2", "e3"])
        self.assertEqual(res["cursor"], "2026-06-06T10:02:00Z")  # advanced past poison + e3
        self.assertNotIn("e2", res["failures"])  # cleared after dead-lettering


class TestDeadLetterAlarm(unittest.TestCase):

    def test_dead_letter_is_recorded_and_alerted(self):
        """A death must be durable (written) and loud (Telegram alert)."""
        dl = {"msg_id": "e9", "preview": "subject-e9", "sender": "e9@example.com",
              "attempts": 3, "first_seen": "2026-06-06T09:00:00Z",
              "error": "boom", "at": "2026-06-06T11:00:00Z"}
        with tempfile.TemporaryDirectory() as d:
            dlf = Path(d) / "dead-letter.jsonl"
            with patch.object(et, "send_telegram") as mock_send:
                et.report_dead_letters("bot-token", [dl], deadletter_file=dlf)
            # durable
            lines = [json.loads(l) for l in dlf.read_text().splitlines() if l.strip()]
            self.assertEqual(len(lines), 1)
            self.assertEqual(lines[0]["msg_id"], "e9")
            # loud
            mock_send.assert_called_once()
            alert_text = mock_send.call_args[0][1]
            self.assertIn("dead-letter", alert_text.lower())
            self.assertIn("investigate", alert_text.lower())

    def test_no_alert_when_no_deaths(self):
        with tempfile.TemporaryDirectory() as d:
            dlf = Path(d) / "dead-letter.jsonl"
            with patch.object(et, "send_telegram") as mock_send:
                et.report_dead_letters("bot-token", [], deadletter_file=dlf)
            mock_send.assert_not_called()


class TestTriageEmailResolution(unittest.TestCase):

    def test_flash_skip_resolves(self):
        with patch.object(et, "flash_header_triage", return_value=("skip", "not relevant")):
            record, ok = et.triage_email(_email("e1", "2026-06-06T10:00:00Z"),
                                         "tok", "key", "bot")
        self.assertTrue(ok)
        self.assertEqual(record["flash"]["decision"], "skip")

    def test_flash_persistent_error_is_unresolved(self):
        with patch.object(et, "flash_header_triage", side_effect=Exception("API down")), \
             patch.object(et.time, "sleep"):
            record, ok = et.triage_email(_email("e1", "2026-06-06T10:00:00Z"),
                                         "tok", "key", "bot")
        self.assertFalse(ok)
        self.assertEqual(record["flash"]["decision"], "error")

    def test_pro_error_is_unresolved(self):
        with patch.object(et, "flash_header_triage", return_value=("read", "looks relevant")), \
             patch.object(et, "fetch_body", return_value="full body text"), \
             patch.object(et, "pro_body_triage", side_effect=Exception("pro down")), \
             patch.object(et.time, "sleep"):
            record, ok = et.triage_email(_email("e1", "2026-06-06T10:00:00Z"),
                                         "tok", "key", "bot")
        self.assertFalse(ok)
        self.assertEqual(record["pro"]["decision"], "error")


if __name__ == "__main__":
    unittest.main(verbosity=2)
