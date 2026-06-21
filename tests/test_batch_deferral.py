#!/usr/bin/env python3
"""Batch processing on an unreliable host: confirm the link once, then make
exactly one evaluation per message. A message is never evaluated twice — if the
link drops it is left un-evaluated for the next run, not retried into a second
call. A reachable-but-unusable reply still dead-letters."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import json
import socket
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from lib import TransportError, SeenLedger

_PROJECT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "triage", _PROJECT / "it_jobs_triage.py")
triage = importlib.util.module_from_spec(_spec)
sys.modules["triage"] = triage
_spec.loader.exec_module(triage)

import lib.api as api


class TestCallDeepseekClassification(unittest.TestCase):
    """call_deepseek maps transport-class failures to TransportError and lets
    non-retryable 4xx surface as themselves."""

    def _raise(self, exc):
        with patch.object(api.urllib.request, "urlopen", side_effect=exc):
            api.call_deepseek("m", "p", "k")

    def test_dns_failure_is_transport(self):
        with self.assertRaises(TransportError):
            self._raise(urllib.error.URLError("Temporary failure in name resolution"))

    def test_read_timeout_is_transport(self):
        with self.assertRaises(TransportError):
            self._raise(socket.timeout("The read operation timed out"))

    def test_http_503_is_transport(self):
        with self.assertRaises(TransportError):
            self._raise(urllib.error.HTTPError("u", 503, "Service Unavailable", {}, None))

    def test_http_429_is_transport(self):
        with self.assertRaises(TransportError):
            self._raise(urllib.error.HTTPError("u", 429, "Too Many Requests", {}, None))

    def test_http_400_is_not_transport(self):
        # Our request is malformed/unauthorized — not retryable, must not be
        # swallowed as a transient transport failure.
        with self.assertRaises(urllib.error.HTTPError):
            self._raise(urllib.error.HTTPError("u", 400, "Bad Request", {}, None))


class TestEndpointReachable(unittest.TestCase):

    def test_true_when_socket_connects(self):
        with patch.object(api.socket, "create_connection") as cc:
            cc.return_value.__enter__.return_value = object()
            self.assertTrue(api.endpoint_reachable())

    def test_false_when_socket_errors(self):
        with patch.object(api.socket, "create_connection",
                          side_effect=OSError("name resolution")):
            self.assertFalse(api.endpoint_reachable())


class TestEvaluateMessage(unittest.TestCase):
    CH = {"desired_roles": "ML", "acceptable_roles": "data"}

    def test_one_call_per_message(self):
        """Exactly one evaluation — no retry that would call the model twice."""
        with patch.object(triage, "_evaluate",
                          return_value={"flash": {"decision": "skip"}}) as ev:
            rec = triage.evaluate_message("body", self.CH, "k")
        self.assertEqual(ev.call_count, 1)
        self.assertEqual(rec["flash"]["decision"], "skip")

    def test_unusable_reply_deadletters(self):
        """Reachable endpoint, unusable reply -> dead_letter in a single call."""
        with patch.object(triage, "_evaluate",
                          return_value={"flash": {"decision": "error", "reason": "bad json"}}) as ev:
            rec = triage.evaluate_message("body", self.CH, "k")
        self.assertEqual(ev.call_count, 1)
        self.assertEqual(rec["flash"]["decision"], "dead_letter")

    def test_transport_error_propagates(self):
        """A dropped link propagates so the caller defers — no second call, no
        dead-letter."""
        with patch.object(triage, "_evaluate",
                          side_effect=TransportError("dns")) as ev:
            with self.assertRaises(TransportError):
                triage.evaluate_message("body", self.CH, "k")
        self.assertEqual(ev.call_count, 1)


class TestBatchProcessing(unittest.TestCase):
    """End-to-end: preflight gates the run; a reachable batch makes one call per
    message and advances; a link that drops seals nothing."""

    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp()
        self.queue = Path(self.tmp) / "queue"
        self.messages = Path(self.tmp) / "messages"
        self.seen_dir = Path(self.tmp) / "seen"
        self.audit = Path(self.tmp) / "audit.jsonl"
        self.queue.mkdir()
        (self.queue / "m1.json").write_text(json.dumps({
            "content": "a job posting\nline two\nline three",
            "meta": {"source": "telegram_group", "chat_id": "it_jobs_cyprus",
                     "user": "u", "ts": "2026-06-21T00:00:00Z", "message_id": "1"},
        }))
        self._patches = [
            patch.object(triage, "QUEUE_DIR", self.queue),
            patch.object(triage, "MESSAGES_DIR", self.messages),
            patch.object(triage, "AUDIT_FILE", self.audit),
            patch.object(triage, "SeenLedger",
                         lambda name: SeenLedger(name, seen_dir=self.seen_dir)),
            patch.object(triage, "load_env",
                         return_value={"DEEPSEEK_API_KEY": "k", "TELEGRAM_BOT_TOKEN": ""}),
            patch.object(triage, "_load_channel_config",
                         return_value={"it_jobs_cyprus": {"desired_roles": "ML",
                                                          "acceptable_roles": "data"}}),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _seen(self):
        return SeenLedger("telegram", seen_dir=self.seen_dir)

    def test_preflight_unreachable_processes_nothing(self):
        with patch.object(triage, "endpoint_reachable", return_value=False):
            with patch.object(triage, "flash_incremental") as fi:
                with self.assertRaises(SystemExit) as cm:
                    triage.main()
        self.assertNotEqual(cm.exception.code, 0)
        fi.assert_not_called()                       # never touched a message
        self.assertTrue((self.queue / "m1.json").exists())
        self.assertEqual(self._seen()._ids, {})
        self.assertFalse(self.audit.exists())

    def test_link_drop_mid_batch_seals_nothing(self):
        with patch.object(triage, "endpoint_reachable", return_value=True):
            with patch.object(triage, "flash_incremental",
                              side_effect=TransportError("name resolution")):
                with self.assertRaises(SystemExit) as cm:
                    triage.main()
        self.assertNotEqual(cm.exception.code, 0)
        self.assertTrue((self.queue / "m1.json").exists())   # left for next run
        self.assertEqual(self._seen()._ids, {})

    def test_reachable_batch_one_call_and_advances(self):
        err_windows = [{"window_start": 1, "window_end": 3, "total_lines": 3,
                        "decision": "error", "reason": "bad json"}]
        with patch.object(triage, "endpoint_reachable", return_value=True):
            with patch.object(triage, "flash_incremental",
                              return_value=(False, "bad json", err_windows)) as fi:
                triage.main()
        self.assertEqual(fi.call_count, 1)                   # one call, no retry
        self.assertFalse((self.queue / "m1.json").exists())  # advanced out
        rec = json.loads(self.audit.read_text().strip())
        self.assertEqual(rec["flash"]["decision"], "dead_letter")
        self.assertIn(rec["msg_id"], self._seen())


if __name__ == "__main__":
    unittest.main(verbosity=2)
