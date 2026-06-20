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

from lib import (FLASH_MODEL, PRO_MODEL, STATE_DIR,
                 load_env, ensure_valid_token,
                 call_deepseek, extract_json, send_telegram, deliver, write_audit,
                 setup_logging, read_cursor, write_cursor, graph_get,
                 resolve_monitored_folder, SeenLedger)

CURSOR_FILE = STATE_DIR / "email-cursor"
AUDIT_FILE = STATE_DIR / "audit" / "email.jsonl"
CRITERIA_FILE = STATE_DIR / "email-triage-criteria.md"
DEADLETTER_FILE = STATE_DIR / "audit" / "dead-letter.jsonl"
FAILURES_FILE = STATE_DIR / "email-failures.json"

# Invariant: no email is ever silently lost. A message that fails to process is
# retried across runs; only after MAX_DEADLETTER_ATTEMPTS does it become a
# dead-letter — a loud, recorded anomaly that should never happen in practice.
MAX_DEADLETTER_ATTEMPTS = 3
_folder_id_cache: str | None = None


def _resolve_folder_id(access_token: str, folder_name: str) -> str:
    """Find the monitored folder's ID. Cached for the lifetime of the process."""
    global _folder_id_cache
    if _folder_id_cache:
        return _folder_id_cache
    result = graph_get(
        "/me/mailFolders", access_token,
        {"$filter": f"displayName eq '{folder_name}'", "$select": "id", "$top": "1"},
    )
    folders = result.get("value", [])
    if not folders:
        raise RuntimeError(f"Outlook folder '{folder_name}' not found")
    _folder_id_cache = folders[0]["id"]
    return _folder_id_cache


def fetch_emails(access_token: str, cursor: str | None, folder_name: str) -> list[dict]:
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
    folder_id = _resolve_folder_id(access_token, folder_name)
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
    return read_cursor(CURSOR_FILE)


def save_cursor(ts: str) -> None:
    write_cursor(CURSOR_FILE, ts)


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
) -> tuple[str, str, str, dict]:
    """Return (decision, reason, message, tags). decision is 'send' or 'skip'."""
    criteria = CRITERIA_FILE.read_text()
    prompt = (
        "You are evaluating an email against a candidate profile.\n\n"
        f"CRITERIA:\n{criteria}\n\n"
        f"FROM: {from_addr}\nSUBJECT: {subject}\n\nBODY:\n{body[:8000]}\n\n"
        "Output ONLY a JSON object:\n"
        '{"decision": "send" or "skip", "reason": "one sentence", '
        '"message": "3-5 sentence Telegram message (only if send, else empty string)", '
        '"tags": {'
        '"role_title": "string (e.g. Senior ML Engineer)", '
        '"role_type": "research|engineering|research_engineering|data_science|conference|internship|other", '
        '"seniority": "junior|mid|senior|lead|principal|unknown", '
        '"skills": ["list of required skills mentioned, lowercase"], '
        '"tech_stack": ["list of tools/frameworks/languages, lowercase"], '
        '"domain": "string (e.g. computer_vision, nlp, probabilistic_ml, robotics, general_ml)", '
        '"location": "string or remote or unknown", '
        '"remote": true or false, '
        '"salary_range": "string or empty string"}}\n\n'
        "Telegram message: what the role/event is, where, why it matches, link or contact if present."
    )
    result = extract_json(call_deepseek(PRO_MODEL, prompt, api_key))
    decision = result.get("decision", "skip")
    reason = result.get("reason", "")
    message = result.get("message", "")
    tags = result.get("tags", {})
    return decision, reason, message, tags


# ---------------------------------------------------------------------------
# dead-letter state
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def load_failures() -> dict:
    """Cross-run failure ledger: {msg_id: {attempts, first_seen, error}}."""
    try:
        return json.loads(FAILURES_FILE.read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_failures(failures: dict) -> None:
    FAILURES_FILE.parent.mkdir(parents=True, exist_ok=True)
    FAILURES_FILE.write_text(json.dumps(failures))


def _record_error(record: dict) -> str:
    """Pull the error string out of a record's failed flash/pro stage."""
    for stage in ("pro", "flash"):
        s = record.get(stage, {})
        if s.get("decision") == "error":
            return s.get("reason", "unknown error")
    return "unknown error"


# ---------------------------------------------------------------------------
# per-email triage — returns (record, resolved)
# ---------------------------------------------------------------------------

def triage_email(msg: dict, access_token: str, api_key: str,
                 bot_token: str) -> tuple[dict, bool]:
    """Triage one email. Returns (audit_record, resolved).

    resolved is True iff the email reached a terminal classification this run
    (flash skip, or flash read → pro skip/send). It is False on any stage error
    — the caller then decides retry vs. dead-letter. A delivery failure does not
    mark the email unresolved (the email *was* classified); delivery health is
    tracked separately.
    """
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

    # --- Flash (in-run retries for transient blips) ---
    flash_error = None
    for attempt in range(3):
        try:
            decision, reason = flash_header_triage(from_addr, subject, api_key)
            flash_error = None
            break
        except Exception as e:
            flash_error = e
            if attempt < 2:
                logging.warning("[email] flash error (attempt %d/3): %s", attempt + 1, e)
                time.sleep(2 ** attempt)

    if flash_error:
        logging.error("[email] flash error after 3 attempts: %s", flash_error)
        record["flash"] = {"model": FLASH_MODEL, "decision": "error",
                           "reason": str(flash_error), "at": _now()}
        return record, False

    record["flash"] = {"model": FLASH_MODEL, "decision": decision,
                       "reason": reason, "at": _now()}
    logging.info("[email] flash → %s | %s", decision, reason)

    email_content = ""
    if decision == "read":
        try:
            body_text = fetch_body(access_token, msg_id)
        except Exception as e:
            logging.error("[email] body fetch error: %s", e)
            record["pro"] = {"model": PRO_MODEL, "decision": "error",
                             "reason": f"body fetch: {e}", "at": _now(), "tags": {}}
            return record, False

        pro_error = None
        for attempt in range(3):
            try:
                bd, br, bm, bt = pro_body_triage(from_addr, subject, body_text, api_key)
                pro_error = None
                break
            except Exception as e:
                pro_error = e
                if attempt < 2:
                    logging.warning("[email] pro error (attempt %d/3): %s", attempt + 1, e)
                    time.sleep(2 ** attempt)

        if pro_error:
            logging.error("[email] pro error after 3 attempts: %s", pro_error)
            record["pro"] = {"model": PRO_MODEL, "decision": "error",
                             "reason": str(pro_error), "at": _now(), "tags": {}}
            return record, False

        record["pro"] = {"model": PRO_MODEL, "decision": bd, "reason": br,
                         "at": _now(), "tags": bt}
        logging.info("[email] pro → %s | %s", bd, br)

        if bd == "send" and bm and bot_token:
            record["pro"]["message"] = bm
            # origin preserves provenance — sender/subject of the source email
            origin = f"{subject} · {from_addr}"
            tg_msg_id = deliver("job_match", f"{origin}\n\n{bm}", ref=msg_id)
            if tg_msg_id:
                logging.info("[email] delivered to Telegram as msg %s", tg_msg_id)
            else:
                logging.error("[email] delivery failed for %s", msg_id)

        email_content = body_text

    record["content"] = email_content or subject
    return record, True


# ---------------------------------------------------------------------------
# batch orchestration — the zero-loss invariant lives here (pure, testable)
# ---------------------------------------------------------------------------

def advance_batch(emails: list[dict], triage_one, failures: dict, *,
                  now_iso: str,
                  max_attempts: int = MAX_DEADLETTER_ATTEMPTS) -> dict:
    """Process a fetched batch, never losing an email.

    Emails arrive ordered by receivedDateTime ascending. We advance the cursor
    only over a contiguous prefix of *resolved-or-dead-lettered* emails. The
    first email that fails transiently (still within its retry budget) stops the
    batch — the cursor never moves past it, so it (and everything after) is
    re-fetched next run. This guarantees no email is skipped and none is
    re-delivered. An email that exhausts its budget is dead-lettered: recorded,
    surfaced, and stepped over so it cannot stall the pipeline forever.

    Returns {records, cursor, deadletters, failures}. cursor is the verbatim
    receivedDateTime of the last resolved/dead email, or None to leave it.
    """
    records: list[dict] = []
    deadletters: list[dict] = []
    terminal_ids: list[str] = []
    cursor: str | None = None

    for msg in emails:
        msg_id = msg["id"]
        received = msg.get("receivedDateTime", "")
        record, resolved = triage_one(msg)
        records.append(record)

        if resolved:
            failures.pop(msg_id, None)
            terminal_ids.append(msg_id)
            if received:
                cursor = received
            continue

        info = failures.get(msg_id) or {"attempts": 0, "first_seen": now_iso}
        info["attempts"] = info.get("attempts", 0) + 1
        info["error"] = _record_error(record)
        failures[msg_id] = info

        if info["attempts"] >= max_attempts:
            dl = {
                "msg_id": msg_id,
                "source": "email",
                "sender": record.get("sender", "?"),
                "preview": record.get("preview", "?"),
                "attempts": info["attempts"],
                "first_seen": info["first_seen"],
                "error": info["error"],
                "at": now_iso,
            }
            deadletters.append(dl)
            record["dead_letter"] = dl
            failures.pop(msg_id, None)
            terminal_ids.append(msg_id)
            if received:
                cursor = received  # step over it — must not stall the pipeline
            continue

        # transient failure within budget: stop the batch, retry next run.
        break

    return {"records": records, "cursor": cursor, "deadletters": deadletters,
            "failures": failures, "terminal_ids": terminal_ids}


def _alert_dead_letter(bot_token: str, dl: dict) -> None:
    """Loud, one-shot Telegram alert — a dead-letter should never happen."""
    if not bot_token:
        return
    try:
        send_telegram(
            bot_token,
            f"⚠️ Email could not be processed after {dl['attempts']} attempts "
            f"and was dead-lettered:\n\n{dl['preview']}\nfrom {dl['sender']}\n\n"
            f"Error: {dl['error']}\n\nThis should not happen — please investigate.")
    except Exception as e:
        logging.error("[email] dead-letter alert failed: %s", e)


def report_dead_letters(bot_token: str, deadletters: list[dict],
                        deadletter_file=DEADLETTER_FILE) -> None:
    """Persist each death durably and raise the alarm."""
    for dl in deadletters:
        write_audit(dl, deadletter_file)
        _alert_dead_letter(bot_token, dl)
        logging.error("[email] DEAD-LETTER after %d attempts: \"%s\" from %s — %s",
                      dl["attempts"], dl["preview"], dl["sender"], dl["error"])


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY", "")
    bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not api_key:
        logging.error("DEEPSEEK_API_KEY not set in .env")
        sys.exit(1)

    folder_raw = env.get("OUTLOOK_FOLDER", "")
    if not folder_raw.strip():
        logging.info("OUTLOOK_FOLDER not set — email triage disabled.")
        return
    try:
        folder = resolve_monitored_folder(
            folder_raw, env.get("OUTLOOK_FOLDER_CONFIRM", ""))
    except ValueError as e:
        logging.error("%s", e)
        sys.exit(1)

    try:
        access_token = ensure_valid_token(env)
    except Exception as e:
        logging.error("Auth error: %s", e)
        sys.exit(1)

    cursor = load_cursor()
    emails = fetch_emails(access_token, cursor, folder)
    if not emails:
        logging.info("No new emails.")
        return

    # Idempotency: the cursor only bounds the query — the seen-ledger is the
    # authoritative gate, so a re-fetched email that was already handled is
    # never re-evaluated (the duplicate-eval / wasted-call bug).
    seen = SeenLedger("email")
    emails = [e for e in emails if e["id"] not in seen]
    if not emails:
        logging.info("All fetched emails already processed.")
        return

    logging.info("Processing %d email(s).", len(emails))
    failures = load_failures()

    def triage_one(msg: dict) -> tuple[dict, bool]:
        return triage_email(msg, access_token, api_key, bot_token)

    result = advance_batch(emails, triage_one, failures, now_iso=_now())

    for record in result["records"]:
        write_audit(record, AUDIT_FILE)
    save_failures(result["failures"])
    for mid in result["terminal_ids"]:
        seen.add(mid)
    seen.save()
    if result["cursor"]:
        save_cursor(result["cursor"])
    report_dead_letters(bot_token, result["deadletters"])

    logging.info("Done.")


if __name__ == "__main__":
    main()
