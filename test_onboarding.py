#!/usr/bin/env python3
"""Tests for lib.onboarding — the pure setup-validation logic.

Notably the Inbox guard: monitoring the whole Inbox would triage every email
the user gets, so it must be refused unless explicitly confirmed.
"""

import unittest

from lib import (resolve_monitored_folder, missing_required,
                 channels_are_placeholder, INBOX_OVERRIDE)


class TestFolderGuard(unittest.TestCase):

    def test_normal_folder_ok(self):
        self.assertEqual(resolve_monitored_folder("mailing lists"), "mailing lists")

    def test_empty_rejected(self):
        with self.assertRaises(ValueError):
            resolve_monitored_folder("")

    def test_inbox_rejected_without_override(self):
        with self.assertRaises(ValueError):
            resolve_monitored_folder("Inbox")
        with self.assertRaises(ValueError):
            resolve_monitored_folder("inbox", "wrong-string")

    def test_inbox_allowed_with_exact_override(self):
        self.assertEqual(resolve_monitored_folder("inbox", INBOX_OVERRIDE), "inbox")

    def test_inbox_guard_is_case_insensitive(self):
        with self.assertRaises(ValueError):
            resolve_monitored_folder("INBOX")
        with self.assertRaises(ValueError):
            resolve_monitored_folder("  Inbox  ")


class TestRequiredConfig(unittest.TestCase):

    def test_all_present(self):
        env = {"TELEGRAM_BOT_TOKEN": "x", "TELEGRAM_API_ID": "1",
               "TELEGRAM_API_HASH": "h", "DEEPSEEK_API_KEY": "k"}
        self.assertEqual(missing_required(env), [])

    def test_blank_and_placeholder_flagged(self):
        env = {"TELEGRAM_BOT_TOKEN": "", "TELEGRAM_API_ID": "CHANGE_ME",
               "TELEGRAM_API_HASH": "h", "DEEPSEEK_API_KEY": "k"}
        m = missing_required(env)
        self.assertIn("TELEGRAM_BOT_TOKEN", m)
        self.assertIn("TELEGRAM_API_ID", m)
        self.assertNotIn("DEEPSEEK_API_KEY", m)

    def test_empty_env_flags_all(self):
        self.assertEqual(len(missing_required({})), 4)


class TestChannelsPlaceholder(unittest.TestCase):

    def test_change_me_is_placeholder(self):
        self.assertTrue(channels_are_placeholder([{"username": "CHANGE_ME"}]))

    def test_empty_is_placeholder(self):
        self.assertTrue(channels_are_placeholder([]))

    def test_real_channel_not_placeholder(self):
        self.assertFalse(channels_are_placeholder([{"username": "it_jobs_cyprus"}]))


if __name__ == "__main__":
    unittest.main(verbosity=2)
