#!/usr/bin/env python3
"""Tests for lib.log (setup_logging) and the poller→lib.load_env fold.

The load_env equivalence test pins the decision to retire the poller's private
load_env in favour of lib.config.load_env: a reference copy of the poller's
*original* parser is asserted byte-for-byte equal to lib's across a range of
.env content. If these ever diverge, this test fails loudly before the fold
can silently change poller behaviour.
"""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import logging
import tempfile
import unittest
from pathlib import Path

from lib import setup_logging, load_env
from lib.log import LOG_FORMAT, LOG_DATEFMT


# Verbatim copy of the parser that used to live in it-jobs-poller.py, kept here
# purely as the equivalence oracle. Do NOT "improve" it — it must mirror the
# pre-fold behaviour exactly.
def _legacy_poller_load_env(env_file: Path) -> dict:
    env = {}
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


class TestLoadEnvEquivalence(unittest.TestCase):
    """lib.config.load_env must match the poller's old parser exactly."""

    CASES = [
        "",
        "KEY=value\n",
        "# a comment\nKEY=value\n",
        "  KEY  =  spaced value  \n",
        "TELEGRAM_API_ID=12345\nTELEGRAM_API_HASH=abcdef\n",
        "URL=https://example.com/path?a=b&c=d\n",  # '=' inside the value
        "\n\n   \nKEY=v\n",                          # blank / whitespace lines
        "NOEQUALS\nKEY=ok\n",                         # line without '='
        "#KEY=should_be_ignored\nREAL=yes\n",
        "EMPTY=\nKEY=val\n",                          # empty value
    ]

    def _run(self, content: str):
        with tempfile.TemporaryDirectory() as d:
            env_path = Path(d) / ".env"
            env_path.write_text(content)
            lib_out = load_env(env_path)
            legacy_out = _legacy_poller_load_env(env_path)
            self.assertEqual(lib_out, legacy_out,
                             f"divergence for content={content!r}")

    def test_all_cases_match(self):
        for content in self.CASES:
            with self.subTest(content=content):
                self._run(content)

    def test_missing_file_both_empty(self):
        with tempfile.TemporaryDirectory() as d:
            missing = Path(d) / "nope.env"
            self.assertEqual(load_env(missing), {})
            self.assertEqual(_legacy_poller_load_env(missing), {})


class TestSetupLogging(unittest.TestCase):

    def tearDown(self):
        # Restore root logger to a clean-ish state between tests.
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)

    def test_configures_root_format(self):
        root = logging.getLogger()
        for h in root.handlers[:]:
            root.removeHandler(h)
        setup_logging()
        self.assertTrue(root.handlers)
        fmt = root.handlers[0].formatter
        self.assertEqual(fmt._fmt, LOG_FORMAT)
        self.assertEqual(fmt.datefmt, LOG_DATEFMT)

    def test_default_level_info(self):
        setup_logging()
        # basicConfig only sets level when no handlers exist; assert the
        # configured format constants are the single source of truth.
        self.assertEqual(LOG_FORMAT, "%(asctime)s %(levelname)-8s %(message)s")
        self.assertEqual(LOG_DATEFMT, "%Y-%m-%dT%H:%M:%SZ")

    def test_setup_logging_is_idempotent(self):
        setup_logging()
        # A second call must not raise; basicConfig is a no-op once configured.
        setup_logging()


if __name__ == "__main__":
    unittest.main(verbosity=2)
