#!/usr/bin/env python3
"""Poll Outlook sent items for replies to the weekly report, process feedback."""

import logging
import sys
from datetime import datetime, timezone, timedelta

from lib import (STATE_DIR, PROJECT_DIR, load_env, ensure_valid_token,
                 update_current_direction, setup_logging, read_cursor,
                 write_cursor, graph_get)

CURSOR_FILE = STATE_DIR / "feedback-cursor"


def _load_cursor() -> str | None:
    return read_cursor(CURSOR_FILE)


def _save_cursor(val: str) -> None:
    write_cursor(CURSOR_FILE, val)


def _check_for_replies(access_token: str) -> dict | None:
    """Find replies to the most recent weekly report. Returns the reply message or None."""
    # Get the most recent sent weekly report
    params = (
        "$filter=contains(subject,'Weekly Job Market Trend Report')"
        "&$top=1&$orderby=sentDateTime desc"
    )
    sent = graph_get(f"/me/mailFolders/sentitems/messages?{params}",
                     access_token, timeout=15)

    if not sent.get("value"):
        logging.info("no weekly report found in sent items")
        return None

    report_msg = sent["value"][0]
    # Look for replies to this conversation
    # Graph API conversation threading:
    conversation_id = report_msg.get("conversationId")
    if not conversation_id:
        logging.info("no conversation ID on report message")
        return None

    # Check for replies that arrived after our cursor
    params = (
        f"$filter=conversationId eq '{conversation_id}'"
        "&$top=10&$orderby=receivedDateTime desc"
    )
    thread = graph_get(f"/me/messages?{params}", access_token, timeout=15)

    cursor = _load_cursor()
    for msg in thread.get("value", []):
        received = msg.get("receivedDateTime", "")
        if cursor and received <= cursor:
            continue
        # Only process replies (not the original report)
        if msg.get("id") == report_msg.get("id"):
            continue
        body = msg.get("body", {}).get("content", "")
        if body:
            return {
                "id": msg["id"],
                "received": received,
                "body": _strip_html(body),
            }

    return None


def _strip_html(text: str) -> str:
    """Basic HTML tag stripping for email bodies."""
    import re
    text = re.sub(r"<[^>]+>", "", text)
    text = text.replace("&amp;", "&").replace("&lt;", "<").replace("&gt;", ">")
    text = text.replace("&quot;", '"').replace("&#39;", "'")
    # Collapse whitespace
    text = "\n".join(l.strip() for l in text.splitlines() if l.strip())
    return text


def main() -> None:
    setup_logging()
    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logging.error("DEEPSEEK_API_KEY not set")
        sys.exit(1)

    try:
        token = ensure_valid_token(env)
    except Exception as e:
        logging.error("token refresh failed: %s", e)
        sys.exit(1)

    try:
        reply = _check_for_replies(token)
    except Exception as e:
        logging.error("reply check failed: %s", e)
        sys.exit(1)

    if reply:
        logging.info("processing reply %s", reply["id"])
        update_current_direction(reply["body"], api_key)
        _save_cursor(reply["received"])
    else:
        logging.info("no new replies")


if __name__ == "__main__":
    main()
