#!/usr/bin/env python3
"""Poll Telegram job groups and write new posts to the message queue."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

from telethon import TelegramClient
from telethon.tl.types import Message

STATE_DIR = Path(__file__).resolve().parent / "state"
QUEUE_DIR = STATE_DIR / "message_queue"
AUDIT_DIR = STATE_DIR / "audit"
AUDIT_FILE = AUDIT_DIR / "telegram.jsonl"
SESSION_FILE = STATE_DIR / "it-jobs-session"
ENV_FILE = STATE_DIR / ".env"

# Each entry: username, cursor file (preserves existing name for it_jobs_cyprus),
# queue filename prefix used by triage to route messages.
# Channel config lives in state/channels.json — one entry per monitored channel.

def load_channels() -> list[dict]:
    with (STATE_DIR / "channels.json").open() as f:
        return json.loads(f.read())["channels"]


def load_env() -> dict:
    env = {}
    try:
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if line and "=" in line and not line.startswith("#"):
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except FileNotFoundError:
        pass
    return env


def load_cursor(cursor_file: Path) -> int | None:
    try:
        val = cursor_file.read_text().strip()
        return int(val) if val else None
    except (FileNotFoundError, ValueError):
        return None


def save_cursor(cursor_file: Path, msg_id: int) -> None:
    cursor_file.write_text(str(msg_id))
    cursor_file.chmod(0o600)


async def poll_channel(client: TelegramClient, channel: dict) -> None:
    username = channel["username"]
    cursor_file = STATE_DIR / f"{username}-cursor"
    queue_prefix = channel["queue_prefix"]

    cursor = load_cursor(cursor_file)
    kwargs: dict = {"limit": 50, "reverse": True}
    if cursor:
        kwargs["min_id"] = cursor
    else:
        kwargs["offset_date"] = datetime.now(timezone.utc) - timedelta(hours=24)

    messages: list[Message] = []
    async for msg in client.iter_messages(username, **kwargs):
        if isinstance(msg, Message) and msg.text and msg.text.strip():
            messages.append(msg)

    if not messages:
        logging.info("No new messages from @%s", username)
        return

    QUEUE_DIR.mkdir(parents=True, exist_ok=True)

    for msg in messages:
        ts = msg.date.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        sender = str(msg.sender_id or "unknown")

        queue_entry = {
            "content": msg.text,
            "meta": {
                "source": "telegram_group",
                "chat_id": username,
                "user": sender,
                "ts": ts,
                "message_id": str(msg.id),
            },
        }

        fname = QUEUE_DIR / f"{int(msg.date.timestamp())}-{queue_prefix}-{msg.id}.json"
        fname.write_text(json.dumps(queue_entry))

        preview = msg.text.split("\n")[0][:120]
        arrival = {
            "msg_id": f"{ts}-{queue_prefix}-{msg.id}",
            "source": username,
            "sender": sender,
            "preview": preview,
            "content": msg.text,
            "arrived_at": ts,
            "stage": "arrival",
        }
        AUDIT_DIR.mkdir(parents=True, exist_ok=True)
        with AUDIT_FILE.open("a") as f:
            f.write(json.dumps(arrival) + "\n")
        logging.info("[%s/%s] arrived=%s | \"%s\"", username, msg.id, ts, preview)

    save_cursor(cursor_file, messages[-1].id)
    logging.info("Queued %d message(s) from @%s (cursor → %d)", len(messages), username, messages[-1].id)


async def poll() -> None:
    env = load_env()
    api_id = int(env.get("TELEGRAM_API_ID") or os.environ.get("TELEGRAM_API_ID", ""))
    api_hash = env.get("TELEGRAM_API_HASH") or os.environ.get("TELEGRAM_API_HASH", "")

    client = TelegramClient(str(SESSION_FILE), api_id, api_hash)
    await client.start()

    for channel in load_channels():
        await poll_channel(client, channel)

    await client.disconnect()

    # Heartbeat: audit --health uses this to detect poller stalls
    (STATE_DIR / "poller-heartbeat").write_text(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")


if __name__ == "__main__":
    asyncio.run(poll())
