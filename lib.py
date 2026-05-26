#!/usr/bin/env python3
"""Shared utilities for the telegram_MCP job pipeline — no external dependencies."""

import json
import logging
import os
import time
import urllib.parse
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = PROJECT_DIR / "state"

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
FLASH_MODEL = "deepseek-v4-flash"
PRO_MODEL = "deepseek-v4-pro"


def _get_config(key: str, default: str) -> str:
    """Read config from environment, falling back to .env file."""
    val = os.environ.get(key)
    if val:
        return val
    try:
        return load_env().get(key, default)
    except Exception:
        return default


USER_NAME = _get_config("USER_NAME", "the user")
TELEGRAM_CHAT_ID = _get_config("TELEGRAM_CHAT_ID", "")
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
TOKEN_FILE = STATE_DIR / "outlook-token.json"
TOKEN_REFRESH_BUFFER_S = 300


def load_env(path: Path | None = None) -> dict:
    env = {}
    env_file = path or STATE_DIR / ".env"
    try:
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def call_deepseek(model: str, prompt: str, api_key: str,
                  timeout: int = 90) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        DEEPSEEK_API_URL, data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = json.loads(resp.read())
    return body["choices"][0]["message"]["content"].strip()


def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON in: {text[:200]}")
    return json.loads(text[start:end])


def send_telegram(bot_token: str, text: str) -> str:
    """Send a Telegram message. Returns the Telegram message_id."""
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps({"chat_id": TELEGRAM_CHAT_ID, "text": text}).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if not body.get("ok"):
        raise RuntimeError(f"Telegram API error: {body}")
    return str(body["result"]["message_id"])


def graph_post(path: str, access_token: str, data: dict,
               timeout: int = 30) -> dict:
    """POST JSON to Microsoft Graph API. Returns parsed response."""
    url = GRAPH_BASE + path
    payload = json.dumps(data).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read())


def send_email(access_token: str, to: str, subject: str,
               body_text: str) -> None:
    """Send an email via Microsoft Graph API."""
    message = {
        "message": {
            "subject": subject,
            "body": {"contentType": "Text", "content": body_text},
            "toRecipients": [{"emailAddress": {"address": to}}],
        },
        "saveToSentItems": True,
    }
    graph_post("/me/sendMail", access_token, message)


def load_token() -> dict:
    return json.loads(TOKEN_FILE.read_text())


def save_token(token: dict) -> None:
    """Atomic write so a crash never leaves a partial token file."""
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(token))
    tmp.chmod(0o600)
    os.replace(tmp, TOKEN_FILE)


def ensure_valid_token(env: dict) -> str:
    """Return a valid access token, refreshing if within BUFFER seconds of expiry."""
    token = load_token()
    expires_at_s = token["expires_at"] / 1000  # stored in ms
    if time.time() < expires_at_s - TOKEN_REFRESH_BUFFER_S:
        return token["access_token"]

    logging.info("Token expiring soon — refreshing.")
    client_id = env.get("OUTLOOK_CLIENT_ID", "")
    if not client_id:
        raise RuntimeError("OUTLOOK_CLIENT_ID not set in .env")

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": token["refresh_token"],
        "scope": "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send",
    }).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    if "error" in data:
        raise RuntimeError(f"Token refresh failed: {data}")

    new_token = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", token["refresh_token"]),
        "expires_at": int((time.time() + data["expires_in"]) * 1000),
    }
    save_token(new_token)
    logging.info("Token refreshed.")
    return new_token["access_token"]


def load_delivery_config() -> dict:
    """Load delivery routing config, with sensible defaults if missing."""
    config_path = STATE_DIR / "delivery.json"
    defaults = {
        "routes": {
            "job_match": "telegram",
            "brief": "telegram",
            "weekly_report": "email",
            "alert": "telegram",
        },
        "telegram": {"chat_id": TELEGRAM_CHAT_ID},
        "email": {"to": ""},
    }
    try:
        with config_path.open() as f:
            user = json.loads(f.read())
        # Merge user config into defaults so missing keys get defaults
        for section in ("routes", "telegram", "email"):
            if section in user:
                defaults[section].update(user[section])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


def deliver(message_type: str, content: str, *,
            file_bytes: bytes | None = None,
            file_name: str | None = None,
            ref: str | None = None) -> str | None:
    """Deliver a message through the configured channel.

    Returns the platform message ID on success, None on failure.
    Writes ref-map if ref is provided and delivery succeeds.
    """
    cfg = load_delivery_config()
    route = cfg["routes"].get(message_type)
    if not route:
        logging.warning("deliver: unknown message_type %s", message_type)
        return None

    msg_id = None
    try:
        if route == "telegram":
            chat_id = cfg["telegram"]["chat_id"]
            if file_bytes and file_name:
                # File document delivery
                msg_id = _deliver_telegram_document(
                    chat_id, file_bytes, file_name, content)
            else:
                # Text-only delivery
                bot_token = _get_bot_token()
                msg_id = send_telegram(bot_token, content)
        elif route == "email":
            token = ensure_valid_token(load_env())
            to = cfg["email"]["to"]
            if not to:
                to = load_env().get("OUTLOOK_EMAIL", "")
            if to:
                send_email(token, to, "Weekly Job Market Trend Report", content)
                # send_email doesn't return a message ID
                msg_id = None
    except Exception as e:
        logging.error("deliver (%s) failed: %s", message_type, e)
        return None

    if ref and msg_id:
        ref_map = STATE_DIR / "ref-map.jsonl"
        ref_map.parent.mkdir(parents=True, exist_ok=True)
        with ref_map.open("a") as f:
            f.write(json.dumps({"tg_msg_id": msg_id, "ref": ref}) + "\n")

    return msg_id


def _get_bot_token() -> str:
    return load_env().get("TELEGRAM_BOT_TOKEN", "")


def _deliver_telegram_document(chat_id: str, file_bytes: bytes,
                               file_name: str, caption: str) -> str | None:
    """Upload a file to Telegram. Returns message_id or None.

    Constructs multipart/form-data manually (same approach as bot-commands.py).
    """
    import email.mime.multipart
    import email.mime.nonmultipart
    from datetime import datetime, timezone

    token = _get_bot_token()
    boundary = "----FormBoundary" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    body = f"--{boundary}\r\n"
    body += 'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
    body += f"{chat_id}\r\n"
    body += f"--{boundary}\r\n"
    body += 'Content-Disposition: form-data; name="caption"\r\n\r\n'
    body += f"{caption}\r\n"
    body += f"--{boundary}\r\n"
    body += f'Content-Disposition: form-data; name="document"; filename="{file_name}"\r\n'
    body += "Content-Type: application/octet-stream\r\n\r\n"

    body_bytes = body.encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")
    data = body_bytes + file_bytes + tail

    url = f"https://api.telegram.org/bot{token}/sendDocument"
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"sendDocument error: {result}")
    return str(result["result"]["message_id"])


def write_audit(record: dict, audit_file: Path) -> None:
    """Append one complete audit record for a processed message."""
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a") as f:
        f.write(json.dumps(record) + "\n")
