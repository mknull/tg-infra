#!/usr/bin/env python3
"""Fetch new Outlook emails via Graph API, two-stage DeepSeek triage, deliver to Telegram."""

import json
import logging
import os
import re
import time
import urllib.request
import urllib.parse
import sys
from datetime import datetime, timezone, timedelta
from pathlib import Path

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

STATE_DIR = Path(__file__).resolve().parent / "state"
CURSOR_FILE = STATE_DIR / "email-cursor"
REF_MAP_FILE = STATE_DIR / "ref-map.jsonl"
AUDIT_DIR = STATE_DIR / "audit"
AUDIT_FILE = AUDIT_DIR / "email.jsonl"
CRITERIA_FILE = STATE_DIR / "email-triage-criteria.md"
TOKEN_FILE = STATE_DIR / "outlook-token.json"
ENV_FILE = STATE_DIR / ".env"

DEEPSEEK_API_URL = "https://api.deepseek.com/v1/chat/completions"
FLASH_MODEL = "deepseek-v4-flash"
PRO_MODEL = "deepseek-v4-pro"
GRAPH_BASE = "https://graph.microsoft.com/v1.0"
TOKEN_ENDPOINT = "https://login.microsoftonline.com/common/oauth2/v2.0/token"
TELEGRAM_CHAT_ID = "CHAT_ID_PLACEHOLDER"
TOKEN_REFRESH_BUFFER_S = 300  # refresh 5 min before expiry


# ---------------------------------------------------------------------------
# env / token
# ---------------------------------------------------------------------------

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
        "scope": "offline_access https://graph.microsoft.com/Mail.Read",
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
        # Microsoft may or may not rotate the refresh token; keep the new one if present
        "refresh_token": data.get("refresh_token", token["refresh_token"]),
        "expires_at": int((time.time() + data["expires_in"]) * 1000),
    }
    save_token(new_token)
    print("  Token refreshed.")
    return new_token["access_token"]


# ---------------------------------------------------------------------------
# Graph API
# ---------------------------------------------------------------------------

def graph_get(path: str, access_token: str, params: dict | None = None) -> dict:
    url = GRAPH_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {access_token}"},
        method="GET",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read())


MAILING_LIST_FOLDER = "mailing lists"
_folder_id_cache: str | None = None


def _resolve_folder_id(access_token: str) -> str:
    """Find the mailing-lists folder ID. Cached for the lifetime of the process."""
    global _folder_id_cache
    if _folder_id_cache:
        return _folder_id_cache
    result = graph_get(
        "/me/mailFolders", access_token,
        {"$filter": f"displayName eq '{MAILING_LIST_FOLDER}'", "$select": "id", "$top": "1"},
    )
    folders = result.get("value", [])
    if not folders:
        raise RuntimeError(f"Outlook folder '{MAILING_LIST_FOLDER}' not found")
    _folder_id_cache = folders[0]["id"]
    return _folder_id_cache


def fetch_emails(access_token: str, cursor: str | None) -> list[dict]:
    if cursor:
        filter_expr = f"receivedDateTime gt {cursor}"
    else:
        since = (datetime.now(timezone.utc) - timedelta(hours=24)).strftime("%Y-%m-%dT%H:%M:%SZ")
        filter_expr = f"receivedDateTime gt {since}"

    params = {
        "$select": "id,from,subject,receivedDateTime",
        "$filter": filter_expr,
        "$orderby": "receivedDateTime asc",
        "$top": "50",
    }
    folder_id = _resolve_folder_id(access_token)
    return graph_get(f"/me/mailFolders/{folder_id}/messages", access_token, params).get("value", [])


def fetch_body(access_token: str, msg_id: str) -> str:
    data = graph_get(f"/me/messages/{msg_id}", access_token, {"$select": "body"})
    body = data.get("body", {})
    text = body.get("content", "")
    if body.get("contentType") == "html":
        text = re.sub(r"<[^>]+>", " ", text)
        text = re.sub(r"\s+", " ", text).strip()
    return text


# ---------------------------------------------------------------------------
# cursor
# ---------------------------------------------------------------------------

def load_cursor() -> str | None:
    try:
        val = CURSOR_FILE.read_text().strip()
        return val if val else None
    except FileNotFoundError:
        return None


def save_cursor(ts: str) -> None:
    CURSOR_FILE.write_text(ts)


# ---------------------------------------------------------------------------
# logging / telegram
# ---------------------------------------------------------------------------

def write_audit(record: dict) -> None:
    """Append one complete audit record for a processed email."""
    AUDIT_DIR.mkdir(parents=True, exist_ok=True)
    with AUDIT_FILE.open("a") as f:
        f.write(json.dumps(record) + "\n")


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


# ---------------------------------------------------------------------------
# DeepSeek
# ---------------------------------------------------------------------------

def call_deepseek(model: str, prompt: str, api_key: str, timeout: int = 90) -> str:
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


def flash_header_triage(from_addr: str, subject: str, api_key: str) -> tuple[str, str]:
    """Return (decision, reason). decision is 'read' or 'skip'."""
    criteria = CRITERIA_FILE.read_text()
    prompt = (
        "You are an email header triage filter. Evaluate this email against the criteria.\n\n"
        f"CRITERIA:\n{criteria}\n\n"
        f"FROM: {from_addr}\nSUBJECT: {subject}\n\n"
        'Respond with ONLY a JSON object: '
        '{"decision": "read" or "skip", "reason": "one sentence"}'
    )
    result = extract_json(call_deepseek(FLASH_MODEL, prompt, api_key))
    decision = result.get("decision", "skip")
    reason = result.get("reason", "")
    return decision, reason


def pro_body_triage(
    from_addr: str, subject: str, body: str, api_key: str
) -> tuple[str, str, str]:
    """Return (decision, reason, message). decision is 'send' or 'skip'."""
    criteria = CRITERIA_FILE.read_text()
    prompt = (
        "You are evaluating an email against a candidate profile.\n\n"
        f"CRITERIA:\n{criteria}\n\n"
        f"FROM: {from_addr}\nSUBJECT: {subject}\n\nBODY:\n{body[:8000]}\n\n"
        "Output ONLY a JSON object:\n"
        '{"decision": "send" or "skip", "reason": "one sentence", '
        '"message": "3-5 sentence Telegram message (only if send, else empty string)"}\n\n'
        "Telegram message: what the role/event is, where, why it matches, link or contact if present."
    )
    result = extract_json(call_deepseek(PRO_MODEL, prompt, api_key))
    decision = result.get("decision", "skip")
    reason = result.get("reason", "")
    message = result.get("message", "")
    return decision, reason, message


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY", "")
    bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not api_key:
        logging.error("DEEPSEEK_API_KEY not set in .env")
        sys.exit(1)

    try:
        access_token = ensure_valid_token(env)
    except Exception as e:
        logging.error("Auth error: %s", e)
        sys.exit(1)

    cursor = load_cursor()
    emails = fetch_emails(access_token, cursor)

    if not emails:
        logging.info("No new emails.")
        return

    logging.info("Processing %d email(s).", len(emails))
    last_received = cursor

    for msg in emails:
        msg_id = msg["id"]
        from_addr = msg.get("from", {}).get("emailAddress", {}).get("address", "unknown")
        subject = msg.get("subject", "(no subject)")
        received = msg.get("receivedDateTime", "")

        logging.info("[email] arrived=%s | \"%s\"", received, subject[:100])

        record = {
            "msg_id": msg_id,
            "source": "email",
            "sender": from_addr,
            "preview": subject,
            "arrived_at": received,
        }
        email_content = ""

        # --- Flash ---
        try:
            decision, reason = flash_header_triage(from_addr, subject, api_key)
        except Exception as e:
            logging.error("[email] flash error: %s", e)
            record["flash"] = {"model": FLASH_MODEL, "decision": "error", "reason": str(e),
                               "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            write_audit(record)
            last_received = received
            continue

        record["flash"] = {"model": FLASH_MODEL, "decision": decision, "reason": reason,
                           "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
        logging.info("[email] flash → %s | %s", decision, reason)

        # --- Pro ---
        if decision == "read":
            try:
                body_text = fetch_body(access_token, msg_id)
            except Exception as e:
                logging.error("[email] body fetch error: %s", e)
                last_received = received
                write_audit(record)
                continue

            try:
                bd, br, bm = pro_body_triage(from_addr, subject, body_text, api_key)
            except Exception as e:
                logging.error("[email] pro error: %s", e)
                record["pro"] = {"model": PRO_MODEL, "decision": "error", "reason": str(e),
                                 "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
                write_audit(record)
                last_received = received
                continue

            record["pro"] = {"model": PRO_MODEL, "decision": bd, "reason": br,
                             "at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")}
            logging.info("[email] pro → %s | %s", bd, br)

            if bd == "send" and bm and bot_token:
                record["pro"]["message"] = bm
                msg_id_ref = record["msg_id"]
                try:
                    origin = f"{subject} · {from_addr}"
                    tg_msg_id = send_telegram(bot_token, f"{origin}\n\n{bm}")
                    with REF_MAP_FILE.open("a") as f:
                        f.write(json.dumps({"tg_msg_id": tg_msg_id,
                                            "ref": msg_id_ref}) + "\n")
                    logging.info("[email] sent to Telegram as msg %s", tg_msg_id)
                except Exception as e:
                    logging.error("[email] Telegram send error: %s", e)

            email_content = body_text

        record["content"] = email_content or subject
        write_audit(record)
        last_received = received

    if last_received and last_received != cursor:
        # Advance 1 s past the last email so Graph API's `gt` filter
        # excludes sub-second timestamps (e.g. 21:16:43.527 > 21:16:43).
        ts = last_received.replace("Z", "+00:00")
        dt = datetime.fromisoformat(ts)
        safe = (dt + timedelta(seconds=1)).strftime("%Y-%m-%dT%H:%M:%SZ")
        save_cursor(safe)
        logging.info("Cursor → %s", safe)

    logging.info("Done.")


if __name__ == "__main__":
    main()
