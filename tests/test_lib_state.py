#!/usr/bin/env python3
"""Tests for lib.state — cursor file I/O plumbing (atomicity, permissions)."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import os
import stat
import tempfile
import unittest
from pathlib import Path

from lib import read_cursor, write_cursor


class TestReadCursor(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cursor"

    def tearDown(self):
        self.tmp.cleanup()

    def test_missing_file_returns_none(self):
        self.assertIsNone(read_cursor(self.path))

    def test_empty_file_returns_none(self):
        self.path.write_text("")
        self.assertIsNone(read_cursor(self.path))

    def test_whitespace_only_returns_none(self):
        self.path.write_text("   \n")
        self.assertIsNone(read_cursor(self.path))

    def test_reads_and_strips_value(self):
        self.path.write_text("12345\n")
        self.assertEqual(read_cursor(self.path), "12345")

    def test_reads_iso_timestamp_verbatim(self):
        self.path.write_text("2026-06-08T10:00:00Z")
        self.assertEqual(read_cursor(self.path), "2026-06-08T10:00:00Z")


class TestWriteCursor(unittest.TestCase):

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cursor"

    def tearDown(self):
        self.tmp.cleanup()

    def test_round_trips(self):
        write_cursor(self.path, "999")
        self.assertEqual(read_cursor(self.path), "999")

    def test_overwrites_existing(self):
        write_cursor(self.path, "1")
        write_cursor(self.path, "2")
        self.assertEqual(read_cursor(self.path), "2")

    def test_default_mode_is_0600(self):
        write_cursor(self.path, "x")
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o600)

    def test_custom_mode_honoured(self):
        write_cursor(self.path, "x", mode=0o644)
        mode = stat.S_IMODE(os.stat(self.path).st_mode)
        self.assertEqual(mode, 0o644)

    def test_creates_parent_directories(self):
        nested = Path(self.tmp.name) / "a" / "b" / "cursor"
        write_cursor(nested, "deep")
        self.assertEqual(read_cursor(nested), "deep")

    def test_no_temp_file_left_behind(self):
        write_cursor(self.path, "x")
        leftovers = list(self.path.parent.glob("*.tmp"))
        self.assertEqual(leftovers, [])

    def test_write_is_not_appended(self):
        # Distinct from the old append-with-newline drift: a fresh value
        # fully replaces the file, no trailing accumulation.
        write_cursor(self.path, "first")
        write_cursor(self.path, "second")
        self.assertEqual(self.path.read_text(), "second")


if __name__ == "__main__":
    unittest.main(verbosity=2)
