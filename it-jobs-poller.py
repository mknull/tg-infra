#!/usr/bin/env python3
"""Poll Telegram job groups and write new posts to the message queue."""

import asyncio
import json
import logging
import os
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib import STATE_DIR, load_env, setup_logging, read_cursor, write_cursor

from telethon import TelegramClient
from telethon.tl.types import Message

QUEUE_DIR = STATE_DIR / "message_queue"
AUDIT_DIR = STATE_DIR / "audit"
AUDIT_FILE = AUDIT_DIR / "telegram.jsonl"
SESSION_FILE = STATE_DIR / "it-jobs-session"

# Channel config lives in state/channels.json — one entry per monitored channel.
# Each channel's cursor is stored at state/{username}-cursor.

def load_channels() -> list[dict]:
    with (STATE_DIR / "channels.json").open() as f:
        return json.loads(f.read())["channels"]


def load_cursor(cursor_file: Path) -> int | None:
    val = read_cursor(cursor_file)
    try:
        return int(val) if val is not None else None
    except ValueError:
        return None


def save_cursor(cursor_file: Path, msg_id: int) -> None:
    write_cursor(cursor_file, str(msg_id))


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
    setup_logging()
    env = load_env()
    api_id_raw = env.get("TELEGRAM_API_ID") or os.environ.get("TELEGRAM_API_ID", "")
    api_hash = env.get("TELEGRAM_API_HASH") or os.environ.get("TELEGRAM_API_HASH", "")
    if not api_id_raw or not api_hash:
        logging.error("TELEGRAM_API_ID / TELEGRAM_API_HASH not set in state/.env")
        raise SystemExit(1)
    try:
        api_id = int(api_id_raw)
    except ValueError:
        logging.error("TELEGRAM_API_ID must be an integer, got %r", api_id_raw)
        raise SystemExit(1)

    client = TelegramClient(str(SESSION_FILE), api_id, api_hash,
                            auto_reconnect=False)
    await client.start()

    for channel in load_channels():
        await poll_channel(client, channel)

    await client.disconnect()

    # Heartbeat: audit --health uses this to detect poller stalls
    (STATE_DIR / "poller-heartbeat").write_text(
        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") + "\n")


if __name__ == "__main__":
    asyncio.run(poll())
