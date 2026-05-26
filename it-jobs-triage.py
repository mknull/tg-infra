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


# ---------------------------------------------------------------------------
# it_jobs_cyprus: batch first-18-lines Flash pass
# ---------------------------------------------------------------------------

def flash_first_pass(content: str, message_id: str, channel: str, api_key: str) -> tuple[bool, str]:
    """Return (flag, reason). flag=True if posting should be escalated to Pro."""
    first_lines = "\n".join(content.splitlines()[:18])
    prompt = (
        "You are a job posting filter. Examine the following first lines of a posting "
        "from a tech-jobs Telegram group.\n\n"
        'Respond with ONLY a JSON object, no other text: '
        '{"is_vacancy": true/false, "flag": true/false, "reason": "one sentence"}\n\n'
        "is_vacancy: true if this is a job offer; false if someone is seeking work, "
        "posting their CV, or it's off-topic.\n"
        "flag: true if the role involves Python, ML, AI, LLMs, RAG, embeddings, "
        "data science, backend engineering, research engineering, or similar.\n"
        "flag must be false if is_vacancy is false.\n\n"
        f"Posting (first lines):\n{first_lines}"
    )
    try:
        raw = call_deepseek(FLASH_MODEL, prompt, api_key)
        result = extract_json(raw)
        is_vacancy = bool(result.get("is_vacancy"))
        flag = bool(result.get("flag")) and is_vacancy
        reason = result.get("reason", "")
        decision = "flag" if flag else "skip"
        logging.info("[%s/%s] flash → %s | is_vacancy=%s | %s", channel, message_id, decision, is_vacancy, reason)
        return flag, reason
    except Exception as e:
        logging.error("[%s/%s] flash error: %s", channel, message_id, e)
        return False, str(e)


# ---------------------------------------------------------------------------
# cyithr: line-by-line Flash pass
# ---------------------------------------------------------------------------

def flash_line_by_line(content: str, message_id: str, channel: str, api_key: str) -> tuple[bool, str]:
    """Read one line at a time until Flash decides. Returns (escalate, reason)."""
    criteria = CRITERIA_FILE.read_text()
    lines = [l for l in content.splitlines() if l.strip()]
    if not lines:
        return False, "empty message"

    final_reason = ""
    for i, line in enumerate(lines):
        is_last = (i == len(lines) - 1)
        seen = "\n".join(lines[: i + 1])

        prompt = (
            "You are a job posting filter reading one line at a time.\n\n"
            f"CRITERIA:\n{criteria}\n\n"
            f"Lines read so far:\n{seen}\n\n"
            + ("This is the last line of the post. " if is_last else "")
            + "Respond with ONLY a JSON object: "
            '{"decision": "irrelevant" | "relevant" | "next", "reason": "one sentence"}\n\n'
            "- irrelevant: clearly not a relevant job vacancy — stop here\n"
            "- relevant: looks like a relevant vacancy — escalate to full evaluation\n"
            "- next: cannot decide yet — read the next line\n"
            + ("(On this last line, 'next' will escalate to full evaluation.)\n" if is_last else "")
        )

        try:
            raw = call_deepseek(FLASH_MODEL, prompt, api_key)
            result = extract_json(raw)
            decision = result.get("decision", "irrelevant")
            reason = result.get("reason", "")
            final_reason = reason

            logging.info("[%s/%s] flash line %d/%d → %s | %s",
                         channel, message_id, i + 1, len(lines), decision, reason)

            if decision == "irrelevant":
                return False, reason
            if decision == "relevant" or (is_last and decision == "next"):
                return True, reason
            # "next" with more lines remaining — continue

        except Exception as e:
            logging.error("[%s/%s] flash line %d error: %s", channel, message_id, i + 1, e)
            return False, str(e)

    return False, final_reason


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
        if channel == "it_jobs_cyprus":
            flag, flash_reason = flash_first_pass(content, message_id, channel, api_key)
        elif channel == "cyithr":
            flag, flash_reason = flash_line_by_line(content, message_id, channel, api_key)
        else:
            logging.warning("[%s/%s] unknown channel, skipping", channel, message_id)
            path.unlink(missing_ok=True)
            continue

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
