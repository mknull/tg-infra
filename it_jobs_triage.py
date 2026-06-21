#!/usr/bin/env python3
"""Two-model triage for Telegram job group postings."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib import (FLASH_MODEL, PRO_MODEL, STATE_DIR,
                 load_env, call_deepseek, extract_json, deliver, write_audit,
                 setup_logging, SeenLedger, TransportError, endpoint_reachable)

QUEUE_DIR = STATE_DIR / "message_queue"
MESSAGES_DIR = STATE_DIR / "messages"
AUDIT_FILE = STATE_DIR / "audit" / "telegram.jsonl"
CRITERIA_FILE = STATE_DIR / "it-jobs-criteria.md"


def _load_channel_config() -> dict[str, dict]:
    """Load per-channel config from channels.json, keyed by username."""
    with (STATE_DIR / "channels.json").open() as f:
        channels = json.loads(f.read())["channels"]
    return {ch["username"]: ch for ch in channels}


# ---------------------------------------------------------------------------
# Unified incremental flash — read 3 lines at a time
# ---------------------------------------------------------------------------

FLASH_PROMPT = (
    "You are a job posting filter reading a posting incrementally.\n\n"
    "The candidate wants roles like: {desired_roles}\n"
    "They will also accept: {acceptable_roles}\n\n"
    "You are seeing a 3-line window. Respond with ONLY a JSON object:\n"
    '{{"decision": "disqualified" | "read_more" | "pass_to_pro", '
    '"reason": "one sentence"}}\n\n'
    "- disqualified: clearly not a relevant job vacancy — stop here\n"
    "- read_more: cannot decide from these 3 lines — show me the next 3\n"
    "- pass_to_pro: looks like a relevant vacancy — escalate\n\n"
    "Posting so far:\n{seen}"
)


def flash_incremental(content: str, desired_roles: str, acceptable_roles: str,
                      api_key: str) -> tuple[bool, str, list[dict]]:
    """Read 3 lines at a time until Flash decides. Returns (flag, reason, windows)."""
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return False, "empty message", []

    windows: list[dict] = []
    i = 0
    total = len(lines)
    while i < total:
        window_end = min(i + 3, total)
        seen = "\n".join(lines[:window_end])
        direction = _load_direction_small()
        prompt = FLASH_PROMPT.format(
            desired_roles=desired_roles,
            acceptable_roles=acceptable_roles,
            seen=seen,
        )
        if direction and direction != "no change":
            prompt += f"\n\nCurrent preference deltas:\n{direction}"

        try:
            raw = call_deepseek(FLASH_MODEL, prompt, api_key)
            result = extract_json(raw)
            decision = result.get("decision", "disqualified")
            reason = result.get("reason", "")

            windows.append({
                "window_start": i + 1,
                "window_end": window_end,
                "total_lines": total,
                "decision": decision,
                "reason": reason,
            })

            logging.info("flash window %d-%d/%d → %s | %s",
                         i + 1, window_end, total, decision, reason)

            if decision == "disqualified":
                return False, reason, windows
            if decision == "pass_to_pro":
                return True, reason, windows
            i += 3

        except TransportError:
            # Endpoint unreachable: the model never saw this window. Not a
            # decision — propagate so the caller defers the whole batch.
            raise
        except Exception as e:
            # The model replied but the reply was unusable (bad JSON, etc.): an
            # application fault, recorded as an error window. The caller retries
            # it within the batch and only dead-letters if it persists.
            logging.error("flash window %d error: %s", i + 1, e)
            windows.append({
                "window_start": i + 1,
                "window_end": window_end,
                "total_lines": total,
                "decision": "error",
                "reason": str(e),
            })
            return False, str(e), windows

    # Exhausted content while reading more — escalate (conservative)
    return True, windows[-1]["reason"] if windows else "", windows


def _load_direction_small() -> str:
    """Load CurrentDirectionSmall for Flash context."""
    from lib import load_current_direction_small
    return load_current_direction_small()


# ---------------------------------------------------------------------------
# shared Pro evaluation
# ---------------------------------------------------------------------------

def pro_full_eval(content: str, api_key: str) -> tuple[str, str, str, dict]:
    """Return (decision, reason, message, tags). decision is 'send' or 'skip'."""
    criteria = CRITERIA_FILE.read_text()
    prompt = (
        "You are evaluating a job posting against a candidate profile.\n\n"
        f"CRITERIA:\n{criteria}\n\n"
        f"FULL POSTING:\n{content}\n\n"
        "Based on the Stage 2 criteria above, output ONLY a JSON object:\n"
        '{"decision": "send" or "skip", "reason": "one sentence", '
        '"message": "3-5 sentence Telegram message (only if decision is send, else empty string)", '
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
        "The Telegram message should cover: role + company, key tech stack, why it matches "
        "the profile, location/remote status, salary if stated, how to apply."
    )
    try:
        raw = call_deepseek(PRO_MODEL, prompt, api_key)
        result = extract_json(raw)
        decision = result.get("decision", "skip")
        reason = result.get("reason", "")
        message = result.get("message", "")
        tags = result.get("tags", {})
        return decision, reason, message, tags
    except TransportError:
        # Endpoint unreachable — defer the batch, do not seal this message.
        raise
    except Exception as e:
        return "error", str(e), "", {}


# ---------------------------------------------------------------------------
# batch processing
# ---------------------------------------------------------------------------

def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _evaluate(content: str, ch_config: dict, api_key: str) -> dict:
    """One full evaluation pass — Flash, then Pro if Flash flags. Returns the
    {flash, pro?} portion of the record. Raises TransportError if the endpoint is
    unreachable; a non-transport model fault surfaces as decision 'error'."""
    flag, flash_reason, windows = flash_incremental(
        content, ch_config.get("desired_roles", ""),
        ch_config.get("acceptable_roles", ""), api_key)
    flash_errored = any(w.get("decision") == "error" for w in windows)
    rec = {"flash": {
        "model": FLASH_MODEL,
        "decision": "error" if flash_errored else ("flag" if flag else "skip"),
        "reason": flash_reason, "at": _now(), "windows": windows}}
    if flash_errored or not flag:
        return rec
    decision, reason, message, tags = pro_full_eval(content, api_key)
    rec["pro"] = {"model": PRO_MODEL, "decision": decision,
                  "reason": reason, "at": _now(), "tags": tags}
    if decision == "send" and message:
        rec["pro"]["message"] = message
    return rec


def evaluate_message(content: str, ch_config: dict, api_key: str) -> dict:
    """Evaluate one message exactly once. The batch is entered only after the
    endpoint is confirmed reachable, so a message is never evaluated twice:
      - a clean reply -> its decision;
      - an unusable reply (reachable, but bad output) -> dead_letter, the
        retained terminal signal that tests the assumption that access is the
        only failure mode;
      - TransportError (the link dropped mid-batch) -> propagate so the caller
        defers the remainder, leaving this message un-evaluated for the next
        run's first attempt.
    """
    rec = _evaluate(content, ch_config, api_key)
    if rec["flash"]["decision"] == "error":
        rec["flash"]["decision"] = "dead_letter"
    if rec.get("pro", {}).get("decision") == "error":
        rec["pro"]["decision"] = "dead_letter"
    return rec


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    setup_logging()
    env = load_env()
    bot_token = env.get("TELEGRAM_BOT_TOKEN", "")
    api_key = env.get("DEEPSEEK_API_KEY", "")
    if not api_key:
        logging.error("DEEPSEEK_API_KEY not set in .env")
        sys.exit(1)

    files = sorted(QUEUE_DIR.glob("*.json"))
    if not files:
        logging.info("No messages in queue.")
        return

    # Confirm we have the model before touching the queue. On an unreliable host
    # this is the difference between a clean deferral and a batch of half-made
    # calls: if the link is down, process nothing and let the next run resume.
    if not endpoint_reachable():
        logging.warning("model endpoint unreachable — deferring batch (%d queued)",
                        len(files))
        sys.exit(1)

    logging.info("Processing %d message(s).", len(files))
    seen = SeenLedger("telegram")
    deferred = False
    for path in files:
        try:
            entry = json.loads(path.read_text())
        except Exception as e:
            logging.error("Bad queue file %s: %s", path.name, e)
            path.unlink(missing_ok=True)
            continue

        meta = entry.get("meta", {})
        if meta.get("source") != "telegram_group":
            continue

        content = entry.get("content", "")
        channel = meta.get("chat_id", "unknown")
        user = meta.get("user", "unknown")
        message_id = meta.get("message_id", path.stem)
        arrived_at = meta.get("ts", "")
        preview = content.split("\n")[0][:120]
        msg_id = f"{arrived_at}-{channel}-{message_id}"

        # Idempotency: the poller can re-queue a message (re-fetch at the cursor
        # boundary); skip anything already brought to a terminal state.
        if msg_id in seen:
            logging.info("[%s/%s] already processed, skipping", channel, message_id)
            path.unlink(missing_ok=True)
            continue

        ch_config = _load_channel_config().get(channel)
        if not ch_config:
            logging.warning("[%s/%s] channel not in channels.json, skipping", channel, message_id)
            path.unlink(missing_ok=True)
            continue

        logging.info("[%s/%s] arrived=%s | \"%s\"", channel, message_id, arrived_at, preview)
        record = {
            "msg_id": msg_id,
            "source": channel,
            "sender": user,
            "preview": preview,
            "content": content,
            "arrived_at": arrived_at,
        }

        # An unreachable endpoint means this is not a valid batch: stop, leave
        # this message and every one after it in the queue, seal nothing, and let
        # the next run retry the lot. No message is made terminal un-evaluated.
        try:
            record.update(evaluate_message(content, ch_config, api_key))
        except TransportError as e:
            logging.error("[%s/%s] model unreachable — deferring batch: %s",
                          channel, message_id, e)
            deferred = True
            break

        pro = record.get("pro", {})
        logging.info("[%s/%s] flash=%s pro=%s", channel, message_id,
                     record["flash"]["decision"], pro.get("decision", "-"))

        if pro.get("decision") == "send" and pro.get("message") and bot_token:
            # origin preserves provenance — which channel/user the job came from
            origin = f"@{channel} · @{user}"
            tg_msg_id = deliver("job_match", f"{origin}\n\n{pro['message']}",
                                ref=msg_id)
            if tg_msg_id:
                logging.info("[%s/%s] delivered to Telegram as msg %s",
                             channel, message_id, tg_msg_id)
            else:
                logging.error("[%s/%s] delivery failed", channel, message_id)

        write_audit(record, AUDIT_FILE)
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        path.rename(MESSAGES_DIR / f"{msg_id}.json")
        seen.add(msg_id)

    seen.save()
    logging.info("Done%s.", " (batch deferred — will retry next run)" if deferred else "")
    if deferred:
        sys.exit(1)


if __name__ == "__main__":
    main()
