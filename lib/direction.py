"""CurrentDirection — user preferences updated by feedback."""

import logging
import time

from .api import call_deepseek
from .audit import write_audit
from .config import STATE_DIR, FLASH_MODEL, PRO_MODEL

DIRECTION_FILE = STATE_DIR / "current-direction.md"
DIRECTION_SMALL_FILE = STATE_DIR / "current-direction-small.md"
DIRECTION_AUDIT_FILE = STATE_DIR / "audit" / "direction.jsonl"

_COMPRESS_PROMPT = (
    "You are compressing a candidate's career direction update for a first-pass "
    "job filter. The filter sees 3 lines at a time and must decide quickly: "
    "disqualified, read_more, or pass_to_pro.\n\n"
    "Full direction:\n{full}\n\n"
    "Compress into exactly 1-2 sentences that tell the filter:\n"
    "- What to immediately disqualify (changed interests, rejected domains)\n"
    "- What to pay extra attention to (new interests, exploration areas)\n\n"
    "The filter already knows the candidate's base profile. Only include deltas — "
    "preferences that differ from or go beyond the base criteria. If nothing "
    "changed, respond with \"no change\".\n\n"
    "Output only the compressed text."
)

_UPDATE_PROMPT = (
    "You manage a candidate's career direction document. Given the current "
    "direction and their feedback, write an updated version.\n\n"
    "Current direction:\n{current}\n\n"
    "Feedback:\n{feedback}\n\n"
    "Rules:\n"
    "- Preserve preferences the feedback doesn't contradict\n"
    "- Add new interests the user mentions\n"
    "- Remove or deprioritize anything the user explicitly rejects\n"
    "- Keep it concise (under 200 words). Write in the second person ('You are…').\n\n"
    "Output only the updated direction text."
)


def load_current_direction() -> str:
    """Read the full CurrentDirection file. Returns empty string if missing."""
    try:
        return DIRECTION_FILE.read_text().strip()
    except FileNotFoundError:
        return ""


def load_current_direction_small() -> str:
    """Read the compressed CurrentDirection file."""
    try:
        return DIRECTION_SMALL_FILE.read_text().strip()
    except FileNotFoundError:
        return ""


def compress_current_direction(full_text: str, api_key: str) -> str:
    """Compress full direction into 1-2 sentences for Flash. Saves to disk."""
    if not full_text.strip():
        compressed = "no change"
    else:
        raw = call_deepseek(FLASH_MODEL,
                            _COMPRESS_PROMPT.format(full=full_text), api_key,
                            timeout=30)
        compressed = raw.strip()

    DIRECTION_SMALL_FILE.parent.mkdir(parents=True, exist_ok=True)
    DIRECTION_SMALL_FILE.write_text(compressed + "\n")
    return compressed


def update_current_direction(feedback: str, api_key: str) -> str:
    """Update CurrentDirection from user feedback. Returns new full text."""
    current = load_current_direction()
    if current:
        prompt = _UPDATE_PROMPT.format(current=current, feedback=feedback)
    else:
        prompt = (
            "Create a career direction document from this feedback:\n\n"
            f"{feedback}\n\n"
            "Write concisely (under 150 words). Use second person.\n"
            "Output only the direction text."
        )

    raw = call_deepseek(PRO_MODEL, prompt, api_key, timeout=60)
    new_direction = raw.strip()

    DIRECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    DIRECTION_FILE.write_text(new_direction + "\n")
    logging.info("CurrentDirection updated (%d chars)", len(new_direction))

    compress_current_direction(new_direction, api_key)

    DIRECTION_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
    write_audit({
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "feedback": feedback,
        "direction_chars": len(new_direction),
    }, DIRECTION_AUDIT_FILE)

    return new_direction
