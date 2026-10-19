"""Reviewed installation features shared by setup, workers and health checks.

Old installations without a manifest retain their runtime behavior. Inference is
only used to propose a migration in setup; it never silently disables a worker.
"""

import json
from pathlib import Path

from .config import STATE_DIR

FEATURES = ("rss", "telegram", "email", "briefings", "weekly", "canary")
DEFAULTS = dict(rss=True, telegram=False, email=False, briefings=False, weekly=False, canary=False)
SERVICES = {
    "bot-commands": (), "telegram-poll": ("telegram",), "rss-poll": ("rss",),
    "job-triage": ("telegram", "rss"), "email-ingest": ("email",),
    "weekly-trend": ("weekly",), "weekly-recovery": ("weekly",),
    "feedback-poller": ("weekly",), "delivery-canary": ("canary",),
}
SCHEDULES = {
    "bot-commands": "*:*:00,30", "telegram-poll": "*-*-* *:05,35:00",
    "rss-poll": "*-*-* *:10:00", "job-triage": "*-*-* *:20,50:00",
    "email-ingest": "*-*-* 0/2:45:00", "weekly-trend": "Sun *-*-* 10:00:00",
    "weekly-recovery": "*-*-* 0/2:40:00", "feedback-poller": "*-*-* *:15,45:00",
    "delivery-canary": "*-*-* *:10:00",
}


def validate(value: dict, *, require_input: bool = True) -> dict:
    if not isinstance(value, dict) or set(value) != {"version", "features", "schedules"} or type(value["version"]) is not int or value["version"] != 1:
        raise ValueError("installation.json requires version 1, features and schedules")
    selected = value["features"]
    if not isinstance(selected, dict) or set(selected) != set(FEATURES) or any(type(v) is not bool for v in selected.values()):
        raise ValueError("features must contain a boolean for each of: " + ", ".join(FEATURES))
    if require_input and not any(selected[k] for k in ("rss", "telegram", "email")):
        raise ValueError("Select at least one job input: RSS, Telegram or Outlook")
    schedules = value["schedules"]
    if not isinstance(schedules, dict) or set(schedules) - set(SERVICES):
        raise ValueError("Unknown service in schedules")
    if any(not isinstance(v, str) or not v.strip() or any(c in v for c in "\n\r\0") for v in schedules.values()):
        raise ValueError("Schedules must be nonempty, single-line systemd calendar expressions")
    return value


def load(state: Path = STATE_DIR) -> dict | None:
    path = state / "installation.json"
    return validate(json.loads(path.read_text())) if path.exists() else None


def enabled(name: str, state: Path = STATE_DIR) -> bool:
    manifest = load(state)
    return manifest is None or manifest["features"][name]


def selected_services(manifest: dict) -> set[str]:
    validate(manifest)
    return {name for name, dependencies in SERVICES.items()
            if not dependencies or any(manifest["features"][key] for key in dependencies)}


def required_credentials(manifest: dict | None) -> tuple[str, ...]:
    keys = ["TELEGRAM_BOT_TOKEN", "DEEPSEEK_API_KEY"]
    if manifest is None or manifest["features"]["telegram"]:
        keys += ["TELEGRAM_API_ID", "TELEGRAM_API_HASH"]
    if manifest and (manifest["features"]["email"] or manifest["features"]["weekly"]):
        keys += ["OUTLOOK_CLIENT_ID", "OUTLOOK_EMAIL"]
    if manifest and manifest["features"]["email"]:
        keys += ["OUTLOOK_FOLDER"]
    return tuple(keys)


def proposed(state: Path, env: dict) -> dict:
    existing = load(state)
    if existing:
        return existing
    selected = DEFAULTS.copy()
    if (state / ".env").exists():
        channels = json.loads((state / "channels.json").read_text()).get("channels", []) if (state / "channels.json").exists() else []
        feeds = json.loads((state / "feeds.json").read_text()).get("feeds", []) if (state / "feeds.json").exists() else []
        selected.update(telegram=any(c.get("username") and not c["username"].startswith("CHANGE") for c in channels),
                        rss=any(f.get("enabled", True) for f in feeds),
                        email=bool(env.get("OUTLOOK_FOLDER")),
                        weekly=bool(env.get("OUTLOOK_CLIENT_ID") and env.get("OUTLOOK_EMAIL")),
                        briefings=(state.parent / "skills/job-brief").exists())
        selected["canary"] = True  # Old setup enabled this timer unconditionally.
    return {"version": 1, "features": selected, "schedules": {}}
