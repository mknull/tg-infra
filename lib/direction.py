"""CurrentDirection — user preferences updated by feedback."""

import logging
import fcntl
import hashlib
import json
import time
from contextlib import contextmanager

from .api import call_deepseek
from .audit import write_audit
from .config import STATE_DIR, FLASH_MODEL, PRO_MODEL
from .state import write_cursor

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


def direction_record_path():
    return DIRECTION_FILE.with_suffix(".json")


@contextmanager
def direction_lock():
    """Serialize feedback updates, including their read/generate/publish sequence."""
    DIRECTION_FILE.parent.mkdir(parents=True, exist_ok=True)
    with (DIRECTION_FILE.parent / ".direction.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        yield


def load_direction_pair() -> tuple[str, str]:
    """The JSON record is authoritative; support existing text-only installs."""
    record = direction_record_path()
    if record.exists():
        data = json.loads(record.read_text())
        return data["full"], data["small"]
    full = DIRECTION_FILE.read_text().strip() if DIRECTION_FILE.exists() else ""
    small = DIRECTION_SMALL_FILE.read_text().strip() if DIRECTION_SMALL_FILE.exists() else ""
    # A legacy failed compression must not silently leave Flash on stale preferences.
    if full and (not small or DIRECTION_FILE.stat().st_mtime > DIRECTION_SMALL_FILE.stat().st_mtime):
        small = full
    return full, small


def load_current_direction() -> str:
    return load_direction_pair()[0]


def load_current_direction_small() -> str:
    """Read the compressed CurrentDirection file."""
    return load_direction_pair()[1]


def direction_context(*, small: bool = False) -> str:
    text = load_current_direction_small() if small else load_current_direction()
    if not text or text == "no change":
        return ""
    return ("\n\nCurrent preference deltas (explicit user preferences):\n"
            "These newer preferences override conflicting base profile, role preferences "
            "and criteria. Retain base preferences that they do not contradict.\n" + text)


def compress_current_direction(full_text: str, api_key: str, *, persist: bool = True) -> str:
    """Compress preferences; generation can be staged without changing live state."""
    if not full_text.strip():
        return "no change"
    else:
        raw = call_deepseek(FLASH_MODEL,
                            _COMPRESS_PROMPT.format(full=full_text), api_key,
                            timeout=30)
        compressed = raw.strip()
        if not compressed or compressed == "no change":
            raise ValueError("Direction compression returned no usable preferences")
    if persist:
        with direction_lock():
            save_direction_pair(full_text, compressed)
    return compressed


def prepare_direction(feedback: str, api_key: str, current: str) -> tuple[str, str]:
    """Prepare both forms before either is made visible to consumers."""
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
    if not new_direction:
        raise ValueError("Direction update returned empty preferences")
    compressed = compress_current_direction(new_direction, api_key, persist=False)
    return new_direction, compressed


def direction_updates(full: str, small: str) -> dict:
    if not isinstance(full, str) or not full.strip() or not isinstance(small, str) or not small.strip():
        raise ValueError("Both direction forms must be nonempty text")
    revision = hashlib.sha256((full + "\0" + small).encode()).hexdigest()[:16]
    return {DIRECTION_FILE: full + "\n", DIRECTION_SMALL_FILE: small + "\n",
            direction_record_path(): json.dumps({"revision": revision, "full": full, "small": small}) + "\n"}


def save_direction_pair(full: str, small: str) -> None:
    updates = direction_updates(full, small)
    # One atomic replacement publishes both forms. Text files are compatibility exports.
    write_cursor(direction_record_path(), updates.pop(direction_record_path()))
    for path, content in updates.items():
        try:
            write_cursor(path, content)
        except OSError:
            logging.exception("Direction saved, but could not refresh export %s", path.name)


def update_current_direction(feedback: str, api_key: str) -> str:
    """Publish a complete preference version; a model failure leaves it unchanged."""
    with direction_lock():
        new_direction, compressed = prepare_direction(feedback, api_key, load_current_direction())
        save_direction_pair(new_direction, compressed)
    try:
        DIRECTION_AUDIT_FILE.parent.mkdir(parents=True, exist_ok=True)
        write_audit({
            "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
            "feedback": feedback,
            "direction_chars": len(new_direction),
        }, DIRECTION_AUDIT_FILE)
    except OSError:
        logging.exception("Direction saved, but audit write failed")
    return new_direction
