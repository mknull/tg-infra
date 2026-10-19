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


def feed_subscriptions(feed: dict) -> list[dict]:
    """Legacy URLs remain one subscription; publisher IDs remain job identities."""
    defaults = {k: feed[k] for k in (
        "allow_empty", "repair_html_entities", "merge_variants", "poll_interval_hours", "backfill",
        "include_title_prefixes", "include_categories", "exclude_title_prefixes",
        "exclude_url_prefixes", "enrich") if k in feed}
    entries = feed.get("subscriptions")
    if entries is None:
        entries = [{"id": "default", "url": feed.get("url", "")}]
    return [{"poll_interval_hours": 24, **defaults, **entry} for entry in entries]


def _validate_options(entry: dict, label: str) -> None:
    for key in ("enabled", "allow_empty", "repair_html_entities", "merge_variants"):
        if key in entry and not isinstance(entry[key], bool):
            raise ValueError(f"{label}: {key} must be a boolean")
    for key in ("include_title_prefixes", "include_categories", "exclude_title_prefixes", "exclude_url_prefixes"):
        values = entry.get(key, [])
        if not isinstance(values, list) or any(not isinstance(v, str) or not v.strip() for v in values):
            raise ValueError(f"{label}: {key} must be a list of nonempty strings")
    hours = entry.get("poll_interval_hours", 24)
    if type(hours) is not int or not 1 <= hours <= 168:
        raise ValueError(f"{label}: poll_interval_hours must be an integer between 1 and 168")
    backfill = entry.get("backfill", {"mode": "all"})
    if not isinstance(backfill, dict) or backfill.get("mode") not in ("all", "recent", "future"):
        raise ValueError(f"{label}: backfill mode must be all, recent or future")
    days = backfill.get("days", 30)
    if type(days) is not int or not 1 <= days <= 3650:
        raise ValueError(f"{label}: backfill days must be between 1 and 3650")
    if entry.get("enrich") not in (None, "fsf", "nodesk"):
        raise ValueError(f"{label}: unknown detail-page parser")


def validate_feeds(config: dict) -> dict[str, dict]:
    """Missing configuration disables feeds. Invalid configuration fails loudly."""
    entries = config["feeds"]
    if not isinstance(entries, list):
        raise ValueError("feeds must be a list")
    feeds = {}
    for entry in entries:
        if not isinstance(entry, dict):
            raise ValueError("feeds must contain objects")
        source_id = entry.get("id", "")
        if not isinstance(source_id, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", source_id):
            raise ValueError("feed id must contain only lowercase letters, digits, _ or -")
        if source_id in feeds:
            raise ValueError(f"duplicate feed id: {source_id}")
        _validate_options(entry, source_id)
        subscriptions = entry.get("subscriptions")
        if subscriptions is not None and (not isinstance(subscriptions, list) or
                (not subscriptions and entry.get("enabled", True)) or
                any(not isinstance(s, dict) for s in subscriptions)):
            raise ValueError(f"{source_id}: subscriptions must be a nonempty list of objects for enabled feeds")
        seen = set()
        for subscription in feed_subscriptions(entry):
            sid = subscription.get("id", "")
            if not isinstance(sid, str) or not re.fullmatch(r"[a-z0-9][a-z0-9_-]{0,63}", sid) or sid in seen:
                raise ValueError(f"{source_id}: invalid or duplicate subscription id")
            seen.add(sid)
            url_value = subscription.get("url", "")
            url = urlsplit(url_value) if isinstance(url_value, str) else None
            if url is None or url.scheme not in ("http", "https") or not url.netloc:
                raise ValueError(f"{source_id}/{sid}: an HTTP(S) feed URL is required")
            _validate_options(subscription, f"{source_id}/{sid}")
        feeds[source_id] = {
            "name": source_id,
            "desired_roles": "Paid job vacancies; Pro evaluates fit against the candidate profile",
            "acceptable_roles": "Other paid technical roles",
            **entry,
        }
    return feeds


def load_feeds(path: Path | None = None) -> dict[str, dict]:
    """Missing configuration disables feeds. Invalid configuration fails loudly."""
    path = path or STATE_DIR / "feeds.json"
    return validate_feeds(json.loads(path.read_text())) if path.exists() else {}
