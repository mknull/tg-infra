#!/usr/bin/env python3
"""Two-model triage for Telegram job group postings."""

import json
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib import (DEEPSEEK_API_URL, FLASH_MODEL, PRO_MODEL, TELEGRAM_CHAT_ID,
                 load_env, call_deepseek, extract_json, send_telegram, write_audit)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)

STATE_DIR = Path(__file__).resolve().parent / "state"
QUEUE_DIR = STATE_DIR / "message_queue"
MESSAGES_DIR = STATE_DIR / "messages"
REF_MAP_FILE = STATE_DIR / "ref-map.jsonl"
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
                      api_key: str) -> tuple[bool, str]:
    """Read 3 lines at a time until Flash decides. Returns (flag, reason)."""
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return False, "empty message"

    i = 0
    final_reason = ""
    while i < len(lines):
        window = lines[i:i + 3]
        seen = "\n".join(lines[:i + len(window)])
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
            final_reason = reason

            logging.info("flash window %d-%d/%d → %s | %s",
                         i + 1, min(i + 3, len(lines)), len(lines), decision, reason)

            if decision == "disqualified":
                return False, reason
            if decision == "pass_to_pro":
                return True, reason
            # "read_more" — advance by 3 lines
            i += 3

        except Exception as e:
            logging.error("flash window %d error: %s", i + 1, e)
            return False, str(e)

    # Exhausted content while reading more — escalate (conservative)
    return True, final_reason


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
    except Exception as e:
        return "error", str(e), "", {}


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
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

    logging.info("Processing %d message(s).", len(files))
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

        logging.info("[%s/%s] arrived=%s | \"%s\"", channel, message_id, arrived_at, preview)

        record = {
            "msg_id": f"{arrived_at}-{channel}-{message_id}",
            "source": channel,
            "sender": user,
            "preview": preview,
            "content": content,
            "arrived_at": arrived_at,
        }

        # --- Flash ---
        flash_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        ch_config = _load_channel_config().get(channel)
        if not ch_config:
            logging.warning("[%s/%s] channel not in channels.json, skipping", channel, message_id)
            path.unlink(missing_ok=True)
            continue

        desired_roles = ch_config.get("desired_roles", "")
        acceptable_roles = ch_config.get("acceptable_roles", "")
        flag, flash_reason = flash_incremental(content, desired_roles, acceptable_roles, api_key)

        record["flash"] = {
            "model": FLASH_MODEL,
            "decision": "flag" if flag else "skip",
            "reason": flash_reason,
            "at": flash_at,
        }

        # --- Pro ---
        if flag:
            pro_decision, pro_reason, pro_message, pro_tags = pro_full_eval(content, api_key)
            pro_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
            record["pro"] = {
                "model": PRO_MODEL,
                "decision": pro_decision,
                "reason": pro_reason,
                "at": pro_at,
                "tags": pro_tags,
            }
            logging.info("[%s/%s] pro → %s | %s", channel, message_id, pro_decision, pro_reason)

            if pro_decision == "send" and pro_message and bot_token:
                record["pro"]["message"] = pro_message
                origin = f"@{channel} · @{user}"
                tg_msg_id = send_telegram(bot_token, f"{origin}\n\n{pro_message}")
                with REF_MAP_FILE.open("a") as f:
                    f.write(json.dumps({"tg_msg_id": tg_msg_id,
                                        "ref": record["msg_id"]}) + "\n")
                logging.info("[%s/%s] sent to Telegram as msg %s", channel, message_id, tg_msg_id)
        else:
            logging.info("[%s/%s] pro skipped (flash: skip)", channel, message_id)

        write_audit(record, AUDIT_FILE)
        MESSAGES_DIR.mkdir(parents=True, exist_ok=True)
        msg_id = record["msg_id"]
        path.rename(MESSAGES_DIR / f"{msg_id}.json")

    logging.info("Done.")


if __name__ == "__main__":
    main()
