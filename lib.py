#!/usr/bin/env python3
"""Shared utilities for the telegram_MCP job pipeline — no external dependencies."""

import json
import logging
import os
import urllib.request
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = PROJECT_DIR / "state"

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
FLASH_MODEL = "deepseek-v4-flash"
PRO_MODEL = "deepseek-v4-pro"
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "")


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


def write_audit(record: dict, audit_file: Path) -> None:
    """Append one complete audit record for a processed message."""
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a") as f:
        f.write(json.dumps(record) + "\n")
