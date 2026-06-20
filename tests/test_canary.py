#!/usr/bin/env python3
"""Tests for the delivery canary — i.e. tests of the detector itself.

These verify the canary calls a success a success and a failure a failure (and
records it). The canary's value is exercising the REAL egress at runtime; these
tests cover its decision logic, not the egress (that is the canary's job, live).
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

_PROJECT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location(
    "delivery_canary", _PROJECT / "delivery_canary.py")
canary = importlib.util.module_from_spec(_spec)
sys.modules["delivery_canary"] = canary
_spec.loader.exec_module(canary)


class TestCanaryDetector(unittest.TestCase):

    def test_success_when_delivery_accepted(self):
        ok, detail = canary.run_canary(deliver_fn=lambda mt, content: "42")
        self.assertTrue(ok)
        self.assertIn("42", detail)

    def test_failure_when_delivery_returns_none(self):
        """deliver() returns None on any egress failure — canary must catch it."""
        ok, detail = canary.run_canary(deliver_fn=lambda mt, content: None)
        self.assertFalse(ok)
        self.assertIn("broken", detail.lower())

    def test_routes_through_canary_message_type(self):
        seen = {}

        def fake(mt, content):
            seen["mt"] = mt
            seen["content"] = content
            return "1"

        canary.run_canary(deliver_fn=fake)
        self.assertEqual(seen["mt"], "canary")
        self.assertIn("canary", seen["content"].lower())

    def test_status_file_records_failure(self):
        with tempfile.TemporaryDirectory() as d:
            sf = Path(d) / "canary-status.json"
            with patch.object(canary, "CANARY_STATUS_FILE", sf):
                canary.write_status(False, "boom")
            rec = json.loads(sf.read_text())
            self.assertFalse(rec["ok"])
            self.assertEqual(rec["detail"], "boom")
            self.assertIn("at", rec)


if __name__ == "__main__":
    unittest.main(verbosity=2)
