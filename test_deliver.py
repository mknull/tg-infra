#!/usr/bin/env python3
"""Tests for unified delivery abstraction — deliver() routing and backends."""

import json
import unittest
from pathlib import Path
from unittest.mock import patch, MagicMock

PROJECT = Path(__file__).resolve().parent
STATE_DIR = PROJECT / "state"
DELIVERY_CONFIG = STATE_DIR / "delivery.json"


class TestDeliveryConfig(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        # Ensure no stale test config
        DELIVERY_CONFIG.unlink(missing_ok=True)

    def tearDown(self):
        DELIVERY_CONFIG.unlink(missing_ok=True)

    def _write_config(self, data: dict) -> None:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        DELIVERY_CONFIG.write_text(json.dumps(data))

    def test_loads_routes_from_config(self):
        self._write_config({
            "routes": {"job_match": "telegram", "weekly_report": "email"},
            "telegram": {"chat_id": "123"},
            "email": {"to": "test@example.com"},
        })
        from lib import load_delivery_config
        cfg = load_delivery_config()
        self.assertEqual(cfg["routes"]["job_match"], "telegram")
        self.assertEqual(cfg["routes"]["weekly_report"], "email")

    def test_missing_config_uses_defaults(self):
        # No config file at all
        from lib import load_delivery_config
        cfg = load_delivery_config()
        self.assertEqual(cfg["routes"]["job_match"], "telegram")
        self.assertEqual(cfg["routes"]["weekly_report"], "email")

    def test_default_telegram_chat_id(self):
        # Defaults to TELEGRAM_CHAT_ID from lib
        from lib import load_delivery_config, TELEGRAM_CHAT_ID
        cfg = load_delivery_config()
        self.assertEqual(cfg["telegram"]["chat_id"], TELEGRAM_CHAT_ID)


class TestDeliverRouting(unittest.TestCase):

    def setUp(self):
        self._config_path = DELIVERY_CONFIG
        self._config_path.unlink(missing_ok=True)
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        self._config_path.write_text(json.dumps({
            "routes": {
                "job_match": "telegram",
                "weekly_report": "email",
                "brief": "telegram",
                "alert": "telegram",
            },
            "telegram": {"chat_id": "test_chat_123", "bot_token_env": "TELEGRAM_BOT_TOKEN"},
            "email": {"to": "test@example.com"},
        }))

    def tearDown(self):
        self._config_path.unlink(missing_ok=True)

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram")
    def test_routes_job_match_to_telegram(self, mock_send, mock_audit):
        mock_send.return_value = "msg_42"
        from lib import deliver
        result = deliver("job_match", "Test job match content")
        mock_send.assert_called_once()
        self.assertEqual(result, "msg_42")

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_email")
    @patch("lib.delivery.ensure_valid_token", return_value="fake-token")
    @patch("lib.delivery.load_env", return_value={"OUTLOOK_CLIENT_ID": "x", "OUTLOOK_EMAIL": "test@example.com"})
    def test_routes_weekly_report_to_email(self, mock_env, mock_token, mock_send, mock_audit):
        from lib import deliver
        result = deliver("weekly_report", "Weekly trends...")
        mock_send.assert_called_once()
        self.assertIsNotNone(result)
        self.assertTrue(result.startswith("email:"))

    @patch("lib.delivery.send_telegram")
    def test_returns_none_on_unknown_message_type(self, mock_send):
        from lib import deliver
        result = deliver("unknown_type", "content")
        self.assertIsNone(result)
        mock_send.assert_not_called()

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram", side_effect=Exception("API down"))
    def test_returns_none_on_delivery_failure(self, mock_send, mock_audit):
        from lib import deliver
        result = deliver("job_match", "content")
        self.assertIsNone(result)

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram", return_value="msg_42")
    def test_writes_ref_map_on_telegram_success(self, mock_send, mock_audit):
        ref_map = STATE_DIR / "ref-map.jsonl"
        ref_map.unlink(missing_ok=True)
        from lib import deliver
        deliver("job_match", "content", ref="test-ref-123")
        self.assertTrue(ref_map.exists())
        entries = [json.loads(l) for l in ref_map.read_text().splitlines() if l.strip()]
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["tg_msg_id"], "msg_42")
        self.assertEqual(entries[0]["ref"], "test-ref-123")
        ref_map.unlink()

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram", side_effect=Exception("fail"))
    def test_no_ref_map_on_failure(self, mock_send, mock_audit):
        ref_map = STATE_DIR / "ref-map.jsonl"
        ref_map.unlink(missing_ok=True)
        from lib import deliver
        result = deliver("job_match", "content", ref="test-ref")
        self.assertIsNone(result)
        self.assertFalse(ref_map.exists())

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram", return_value="msg_1")
    def test_deliver_passes_configured_chat_id(self, mock_send, mock_audit):
        """deliver() routes the configured chat_id to send_telegram (bug 1.1)."""
        from lib import deliver
        deliver("job_match", "hi")
        _, kwargs = mock_send.call_args
        self.assertEqual(kwargs.get("chat_id"), "test_chat_123")

    @patch("lib.delivery.write_audit")
    @patch("lib.delivery.send_telegram", return_value="msg_1")
    def test_delivery_audit_records_ref_and_channel(self, mock_send, mock_audit):
        """Egress audit carries provenance (ref → source) and the channel."""
        from lib import deliver
        deliver("job_match", "content", ref="email:abc")
        record = mock_audit.call_args[0][0]
        self.assertEqual(record["ref"], "email:abc")
        self.assertEqual(record["channel"], "telegram")
        self.assertEqual(record["message_type"], "job_match")
        self.assertTrue(record["success"])


class TestDeliverTelegramBackend(unittest.TestCase):

    @patch("urllib.request.urlopen")
    def test_sends_text_message(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True, "result": {"message_id": 42}
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from lib import send_telegram
        result = send_telegram("fake-token", "hello world")
        self.assertEqual(result, "42")

    @patch("urllib.request.urlopen")
    def test_explicit_chat_id_overrides_default(self, mock_urlopen):
        """An explicit chat_id lands in the request payload (bug 1.1 backend)."""
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": True, "result": {"message_id": 7}
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from lib import send_telegram
        send_telegram("fake-token", "hi", chat_id="explicit_chat")
        req = mock_urlopen.call_args[0][0]
        self.assertEqual(json.loads(req.data)["chat_id"], "explicit_chat")

    @patch("urllib.request.urlopen")
    def test_raises_on_api_error(self, mock_urlopen):
        mock_resp = MagicMock()
        mock_resp.read.return_value = json.dumps({
            "ok": False, "description": "Forbidden"
        }).encode()
        mock_urlopen.return_value.__enter__.return_value = mock_resp

        from lib import send_telegram
        with self.assertRaises(RuntimeError):
            send_telegram("fake-token", "hello")


if __name__ == "__main__":
    unittest.main(verbosity=2)
