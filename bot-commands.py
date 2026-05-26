#!/usr/bin/env python3
"""Oneshot bot-command processor. Polls getUpdates once, handles /briefme, exits."""

import json
import logging
import re
import urllib.request
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib import (DEEPSEEK_API_URL, PRO_MODEL, load_env, call_deepseek)
from agent import run_agent

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

PROJECT_DIR = Path(__file__).resolve().parent
STATE_DIR = PROJECT_DIR / "state"
MESSAGES_DIR = STATE_DIR / "messages"
REF_MAP_FILE = STATE_DIR / "ref-map.jsonl"
AUDIT_DIR = STATE_DIR / "audit"
CURSOR_FILE = STATE_DIR / "bot-cursor"
FEEDBACK_FILE = STATE_DIR / "feedback.jsonl"

TELEGRAM_API = "https://api.telegram.org/bot{token}/{method}"


# ---------------------------------------------------------------------------
# env / cursor
# ---------------------------------------------------------------------------


def load_cursor() -> int:
    try:
        return int(CURSOR_FILE.read_text().strip())
    except (FileNotFoundError, ValueError):
        return 0


def save_cursor(update_id: int) -> None:
    CURSOR_FILE.write_text(str(update_id))


# ---------------------------------------------------------------------------
# original content lookup
# ---------------------------------------------------------------------------

def lookup_original_content(quoted_msg_id: str) -> tuple[str | None, str | None]:
    """Find the original full job posting for a Telegram message.

    Looks up the ref-map to get the internal msg_id, then fetches content.
    Returns (content, role_label) or (None, None) if not found.
    """
    ref = None
    if REF_MAP_FILE.exists():
        try:
            for line in REF_MAP_FILE.read_text().splitlines():
                record = json.loads(line)
                if record.get("tg_msg_id") == quoted_msg_id:
                    ref = record.get("ref")
                    break
        except Exception:
            pass

    if not ref:
        return None, None

    # Try messages/ directory first
    msg_file = MESSAGES_DIR / f"{ref}.json"
    if msg_file.exists():
        try:
            entry = json.loads(msg_file.read_text())
            content = entry.get("content", "")
            if content:
                preview = content.split("\n")[0][:100]
                return content, preview
        except Exception:
            pass

    # Fall back to audit files
    for audit_name in ["telegram.jsonl", "email.jsonl"]:
        audit_file = AUDIT_DIR / audit_name
        if not audit_file.exists():
            continue
        try:
            for line in audit_file.read_text().splitlines():
                record = json.loads(line)
                if record.get("msg_id") == ref:
                    content = record.get("content", "")
                    if content:
                        preview = record.get("preview", "")
                        return content, preview or content.split("\n")[0][:100]
        except Exception:
            pass

    return None, None


# ---------------------------------------------------------------------------
# Telegram API
# ---------------------------------------------------------------------------

def telegram_call(token: str, method: str, params: dict) -> dict:
    url = TELEGRAM_API.format(token=token, method=method)
    payload = json.dumps(params).encode()
    req = urllib.request.Request(
        url, data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=15) as resp:
        body = json.loads(resp.read())
    if not body.get("ok"):
        raise RuntimeError(f"Telegram {method} error: {body}")
    return body["result"]


def send_message(token: str, chat_id: str, text: str, reply_to: str | None = None) -> None:
    params = {"chat_id": chat_id, "text": text}
    if reply_to:
        params["reply_to_message_id"] = reply_to
    telegram_call(token, "sendMessage", params)


def send_document(token: str, chat_id: str, file_bytes: bytes, filename: str,
                  caption: str, reply_to: str | None = None) -> None:
    """Upload a file as a Telegram document with a caption."""
    import email.mime.multipart
    import email.mime.nonmultipart

    boundary = "----FormBoundary" + datetime.now(timezone.utc).strftime("%Y%m%d%H%M%S%f")
    body = f"--{boundary}\r\n"
    body += 'Content-Disposition: form-data; name="chat_id"\r\n\r\n'
    body += f"{chat_id}\r\n"
    if reply_to:
        body += f"--{boundary}\r\n"
        body += 'Content-Disposition: form-data; name="reply_to_message_id"\r\n\r\n'
        body += f"{reply_to}\r\n"
    body += f"--{boundary}\r\n"
    body += 'Content-Disposition: form-data; name="caption"\r\n\r\n'
    body += f"{caption}\r\n"
    body += f"--{boundary}\r\n"
    body += f'Content-Disposition: form-data; name="document"; filename="{filename}"\r\n'
    body += "Content-Type: application/octet-stream\r\n\r\n"

    body_bytes = body.encode("utf-8")
    tail = f"\r\n--{boundary}--\r\n".encode("utf-8")

    data = body_bytes + file_bytes + tail

    url = TELEGRAM_API.format(token=token, method="sendDocument")
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": f"multipart/form-data; boundary={boundary}"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        result = json.loads(resp.read())
    if not result.get("ok"):
        raise RuntimeError(f"sendDocument error: {result}")


# ---------------------------------------------------------------------------
# command handlers
# ---------------------------------------------------------------------------

def handle_briefme(token: str, chat_id: str, reply_to_msg_id: str | None,
                   quoted_msg_id: str | None, quoted_text: str | None,
                   api_key: str) -> None:
    if not quoted_text:
        send_message(
            token, chat_id,
            "Quote a job posting message and reply with /briefme.\n\n"
            "Long-press a message → Reply → type /briefme.",
            reply_to=reply_to_msg_id,
        )
        return

    # Try to find the original full ad via ref-map
    original = None
    role_label = None
    if quoted_msg_id:
        original, role_label = lookup_original_content(quoted_msg_id)

    if original:
        job_text = original
        source_note = " (from original ad)"
    else:
        job_text = quoted_text
        source_note = " (from quoted text — original ad not found)"
        role_label = quoted_text.split("\n")[0][:100]

    logging.info("briefme: original=%s, label=%s", bool(original), role_label)

    send_message(token, chat_id, "Agent is out gathering details…",
                 reply_to=reply_to_msg_id)

    try:
        brief = run_agent(job_text, api_key,
                          audit_meta={"sender": chat_id,
                                      "preview": role_label or job_text[:120]})
    except Exception as e:
        logging.error("Agent failed: %s", e)
        send_message(token, chat_id, "Briefing failed — try again later.",
                     reply_to=reply_to_msg_id)
        return

    # Save brief to file, convert to PDF
    briefs_dir = PROJECT_DIR / "workspace" / "briefs"
    briefs_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    slug = re.sub(r"[^a-z0-9]+", "-", (role_label or "brief")[:60].lower()).strip("-")
    brief_path = briefs_dir / f"{today}-{slug}.md"
    brief_path.write_text(brief)

    from tools import md_to_pdf
    try:
        pdf_bytes, pdf_filename = md_to_pdf(brief)
    except Exception as e:
        logging.error("PDF conversion failed, falling back to markdown: %s", e)
        pdf_bytes = brief_path.read_bytes()
        pdf_filename = brief_path.name

    caption = f"Here is your briefing for: {role_label or 'this role'}"

    try:
        send_document(token, chat_id, pdf_bytes, pdf_filename, caption,
                      reply_to=reply_to_msg_id)
        logging.info("brief delivered as %s", pdf_filename)
    except Exception as e:
        logging.error("sendDocument failed, falling back to message: %s", e)
        send_message(token, chat_id, brief[:4000], reply_to=reply_to_msg_id)


def handle_reaction(chat_id: str, msg_id: str, user_id: str,
                    new_reactions: list[str], date: int) -> None:
    """Record a message reaction as feedback."""
    record = {
        "tg_msg_id": str(msg_id),
        "chat_id": str(chat_id),
        "user_id": str(user_id),
        "reactions": new_reactions,
        "at": datetime.fromtimestamp(date, tz=timezone.utc).isoformat(),
    }
    with FEEDBACK_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")
    emoji_str = " ".join(new_reactions) if new_reactions else "none"
    logging.info("feedback msg=%s user=%s reactions=[%s]", msg_id, user_id, emoji_str)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    env = load_env()
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not token:
        logging.error("TELEGRAM_BOT_TOKEN not set in .env")
        sys.exit(1)
    if not api_key:
        logging.error("DEEPSEEK_API_KEY not set in .env")
        sys.exit(1)

    cursor = load_cursor()
    try:
        updates = telegram_call(token, "getUpdates", {
            "offset": cursor + 1,
            "timeout": 5,
            "allowed_updates": ["message", "message_reaction"],
        })
    except urllib.error.HTTPError as e:
        if e.code == 409:
            logging.info("409 conflict — MCP plugin has the token, skipping")
            return
        logging.error("getUpdates HTTP error: %s", e)
        return
    except Exception as e:
        logging.error("getUpdates error: %s", e)
        return

    if not updates:
        logging.info("No pending commands.")
        return

    logging.info("Processing %d update(s).", len(updates))
    last_id = cursor

    for upd in updates:
        update_id = upd["update_id"]
        last_id = max(last_id, update_id)

        reaction = upd.get("message_reaction")
        if reaction:
            chat_id = str(reaction["chat"]["id"])
            msg_id = str(reaction["message_id"])
            user_id = str(reaction.get("user", {}).get("id", "0"))
            new_rxns = [r["emoji"] for r in reaction.get("new_reaction", [])
                        if r.get("type") == "emoji"]
            handle_reaction(chat_id, msg_id, user_id, new_rxns, reaction["date"])
            continue

        msg = upd.get("message", {})
        if not msg:
            continue

        chat_id = str(msg["chat"]["id"])
        text = msg.get("text", "") or msg.get("caption", "")
        msg_tg_id = str(msg["message_id"])

        if not text.startswith("/"):
            continue

        command = text.split()[0].lower().split("@")[0]
        reply_to = msg.get("reply_to_message")
        quoted_text = None
        quoted_msg_id = None
        if reply_to:
            quoted_text = reply_to.get("text") or reply_to.get("caption")
            quoted_msg_id = str(reply_to.get("message_id"))
        reply_to_msg_id = str(msg_tg_id) if reply_to else None

        if command == "/briefme":
            logging.info("[%s] /briefme quoted=%s", chat_id, bool(quoted_text))
            try:
                handle_briefme(token, chat_id, reply_to_msg_id, quoted_msg_id,
                               quoted_text, api_key)
            except Exception as e:
                logging.error("[%s] /briefme failed: %s", chat_id, e)
                try:
                    send_message(token, chat_id, "Briefing failed. Try again.")
                except Exception:
                    pass
        elif command == "/start":
            send_message(token, chat_id,
                         "Job bot. Reply to a job posting with /briefme for a decision-grade brief.")
        elif command == "/help":
            send_message(token, chat_id,
                         "/briefme — Reply to a job posting with this command to get a brief.\n"
                         "/status — Check bot status.")
        elif command == "/direction":
            feedback = text.split(" ", 1)[1] if " " in text else ""
            if not feedback:
                from lib import load_current_direction
                current = load_current_direction()
                if current:
                    send_message(token, chat_id,
                                 f"Current direction:\n\n{current[:3000]}")
                else:
                    send_message(token, chat_id,
                                 "No direction set. Reply to the weekly email or send "
                                 "/direction followed by your preferences.")
            else:
                send_message(token, chat_id, "Got it — updating your direction...")
                try:
                    from lib import update_current_direction
                    update_current_direction(feedback, api_key)
                    from lib import load_current_direction
                    updated = load_current_direction()
                    preview = updated[:500] + ("..." if len(updated) > 500 else "")
                    send_message(token, chat_id, f"Updated:\n\n{preview}")
                except Exception as e:
                    logging.error("[%s] /direction update failed: %s", chat_id, e)
                    send_message(token, chat_id, "Failed to update direction. Try again.")
        elif command == "/status":
            send_message(token, chat_id,
                         f"Bot is running. Last update: {cursor}.")

    save_cursor(last_id)
    logging.info("Done. Cursor → %d", last_id)


if __name__ == "__main__":
    main()
