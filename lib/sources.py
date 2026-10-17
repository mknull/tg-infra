"""Job-input audit files and RSS/Atom source configuration."""

import json
import re
from pathlib import Path
from urllib.parse import urlsplit

from .config import STATE_DIR

JOB_AUDIT_FILES = {
    "telegram": "telegram.jsonl",
    "email": "email.jsonl",
    "rss": "rss.jsonl",
}


def load_feeds(path: Path | None = None) -> dict[str, dict]:
    """Missing configuration disables feeds. Invalid configuration fails loudly."""
    path = path or STATE_DIR / "feeds.json"
    if not path.exists():
        return {}
    entries = json.loads(path.read_text())["feeds"]
    if not isinstance(entries, list):
        raise ValueError("feeds must be a list")
    feeds = {}
    for entry in entries:
        source_id = entry.get("id", "")
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source_id):
            raise ValueError("feed id must contain only lowercase letters, digits, _ or -")
        if source_id in feeds:
            raise ValueError(f"duplicate feed id: {source_id}")
        url = urlsplit(entry.get("url", ""))
        if url.scheme not in ("http", "https") or not url.netloc:
            raise ValueError(f"{source_id}: an HTTP(S) feed URL is required")
        if not isinstance(entry.get("enabled", True), bool):
            raise ValueError(f"{source_id}: enabled must be a boolean")
        feeds[source_id] = {
            "name": source_id,
            "desired_roles": "Paid job vacancies; Pro evaluates fit against the candidate profile",
            "acceptable_roles": "Other paid technical roles",
            **entry,
        }
    return feeds
