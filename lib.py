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
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")
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


def write_audit(record: dict, audit_file: Path) -> None:
    """Append one complete audit record for a processed message."""
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a") as f:
        f.write(json.dumps(record) + "\n")
