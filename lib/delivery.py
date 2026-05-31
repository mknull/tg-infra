"""Unified message delivery — Telegram, email, routing via config."""

import json
import logging
import time
import urllib.request

from .audit import write_audit
from .auth import ensure_valid_token
from .config import STATE_DIR, GRAPH_BASE, TELEGRAM_CHAT_ID, load_env


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
        for section in ("routes", "telegram", "email"):
            if section in user:
                defaults[section].update(user[section])
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return defaults


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


def send_telegram_document(bot_token: str, chat_id: str, file_bytes: bytes,
                           filename: str, caption: str,
                           reply_to: str | None = None) -> str:
    """Upload a file to Telegram. Returns the Telegram message_id."""
    import email.mime.multipart
    import email.mime.nonmultipart
    from datetime import datetime, timezone

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

    url = f"https://api.telegram.org/bot{bot_token}/sendDocument"
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
        body = resp.read()
        if not body:
            return {}
        return json.loads(body)


def send_email(access_token: str, to: str, subject: str,
               body_text: str,
               attachments: list[dict] | None = None) -> None:
    """Send an email via Microsoft Graph API.

    attachments: list of {"filename": str, "content": str}
    """
    msg = {
        "subject": subject,
        "body": {"contentType": "Text", "content": body_text},
        "toRecipients": [{"emailAddress": {"address": to}}],
    }
    if attachments:
        import base64
        msg["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": att["filename"],
                "contentType": "text/plain",
                "contentBytes": base64.b64encode(
                    att["content"].encode("utf-8")).decode("ascii"),
            }
            for att in attachments
        ]
    graph_post("/me/sendMail", access_token,
               {"message": msg, "saveToSentItems": True})


def _get_bot_token() -> str:
    return load_env().get("TELEGRAM_BOT_TOKEN", "")


def deliver(message_type: str, content: str, *,
            file_bytes: bytes | None = None,
            file_name: str | None = None,
            subject: str = "",
            attachments: list[dict] | None = None,
            ref: str | None = None) -> str | None:
    """Deliver a message through the configured channel.

    Returns the platform message ID on success, None on failure.
    """
    cfg = load_delivery_config()
    route = cfg["routes"].get(message_type)
    if not route:
        logging.warning("deliver: unknown message_type %s", message_type)
        return None

    DELIVERY_AUDIT = STATE_DIR / "audit" / "delivery.jsonl"
    msg_id = None
    error = None
    try:
        if route == "telegram":
            chat_id = cfg["telegram"]["chat_id"]
            if file_bytes and file_name:
                msg_id = send_telegram_document(
                    _get_bot_token(), chat_id, file_bytes, file_name, content)
            else:
                msg_id = send_telegram(_get_bot_token(), content)
        elif route == "email":
            token = ensure_valid_token(load_env())
            to = cfg["email"]["to"]
            if not to:
                to = load_env().get("OUTLOOK_EMAIL", "")
            if to:
                send_email(token, to, subject or "Weekly Job Market Trend Report",
                           content, attachments=attachments)
                msg_id = f"email:{time.strftime('%Y%m%dT%H%M%SZ', time.gmtime())}"
            else:
                error = "no recipient configured"
    except Exception as e:
        logging.error("deliver (%s) failed: %s", message_type, e)
        error = str(e)

    write_audit({
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "message_type": message_type,
        "channel": route,
        "success": error is None,
        "error": error,
        "platform_id": msg_id,
    }, DELIVERY_AUDIT)

    if error:
        return None

    if ref and msg_id and route == "telegram":
        ref_map = STATE_DIR / "ref-map.jsonl"
        ref_map.parent.mkdir(parents=True, exist_ok=True)
        with ref_map.open("a") as f:
            f.write(json.dumps({"tg_msg_id": msg_id, "ref": ref}) + "\n")

    return msg_id
