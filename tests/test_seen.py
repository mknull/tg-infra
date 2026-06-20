#!/usr/bin/env python3
"""Tests for lib.seen — the exactly-once idempotency ledger.

These pin the property that was missing and let the duplicate-eval bug recur:
an id brought to a terminal state is never seen as new again, while an unsaved
(mid-retry) id is not yet terminal.
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tempfile
import time
import unittest
from pathlib import Path

from lib.seen import SeenLedger


class TestSeenLedger(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _ledger(self, pipeline="email", **kw):
        return SeenLedger(pipeline, seen_dir=self.d, **kw)

    def test_unseen_then_terminal_persists(self):
        led = self._ledger()
        self.assertNotIn("m1", led)
        led.add("m1")
        led.save()
        self.assertIn("m1", self._ledger())  # a fresh instance sees it

    def test_unsaved_id_is_not_terminal(self):
        led = self._ledger()
        led.add("m1")  # mid-retry: added but not saved
        self.assertNotIn("m1", self._ledger())  # so it will be retried

    def test_retention_prunes_old_ids(self):
        led = self._ledger(retention_days=1)
        led._ids["old"] = int(time.time()) - 2 * 86400
        led.add("fresh")
        led.save()
        reloaded = self._ledger(retention_days=1)
        self.assertNotIn("old", reloaded)
        self.assertIn("fresh", reloaded)

    def test_atomic_write_leaves_no_tmp(self):
        led = self._ledger()
        led.add("m")
        led.save()
        self.assertEqual(list(self.d.glob("*.tmp")), [])

    def test_pipelines_are_isolated(self):
        a = SeenLedger("email", seen_dir=self.d)
        a.add("x")
        a.save()
        b = SeenLedger("telegram", seen_dir=self.d)
        self.assertNotIn("x", b)

    def test_corrupt_file_treated_as_empty(self):
        (self.d / "email.json").write_text("{ not json")
        self.assertNotIn("anything", self._ledger())


if __name__ == "__main__":
    unittest.main(verbosity=2)
