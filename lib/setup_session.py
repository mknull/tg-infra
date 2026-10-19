"""Private, resumable setup proposals. Runtime files change only on application."""

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
from pathlib import Path

from .config import load_env
from .features import load, required_credentials
from .matching import PROFILE_FILES, matching_gaps
from .onboarding import resolve_monitored_folder
from .sources import validate_feeds
from .state import apply_text_updates, write_cursor

CONFIG_FILES = (
    "state/.env", "state/installation.json", "state/channels.json", "state/feeds.json",
    "state/it-jobs-criteria.md", "state/email-triage-criteria.md",
    "state/current-direction.json", "state/current-direction.md", "state/current-direction-small.md",
    "state/delivery.json",
    *("source/" + name for name in PROFILE_FILES),
    "skills/job-brief/SKILL.md", "skills/job-brief/references/profile-for-relevance.md",
    "skills/job-mine/SKILL.md", "skills/job-mine/references/profile.md",
    "skills/job-pitch/SKILL.md", "skills/job-pitch/references/profile-detailed.md",
)
AUTH_FILES = ("state/it-jobs-session.session", "state/outlook-token.json")
AUTH_KEYS = (("TELEGRAM_API_ID", "TELEGRAM_API_HASH"), ("OUTLOOK_CLIENT_ID", "OUTLOOK_EMAIL"))


def copy_auth(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    if source.suffix == ".session":
        with sqlite3.connect(f"file:{source}?mode=ro", uri=True) as old, sqlite3.connect(target) as new:
            old.backup(new)
        target.chmod(0o600)
    else:
        write_cursor(target, source.read_text())


def sync_auth(root: Path, proposal: Path, *, to_active: bool = False) -> None:
    """Workers must be idle. Keep refresh-token rotation even if setup fails."""
    before, after = load_env(root / "state/.env"), load_env(proposal / "state/.env")
    for name, keys in zip(AUTH_FILES, AUTH_KEYS):
        if all(before.get(k) == after.get(k) for k in keys):
            source, target = (proposal / name, root / name) if to_active else (root / name, proposal / name)
            if source.exists() and (not to_active or target.exists()):
                copy_auth(source, target)


def write_env(path: Path, values: dict) -> None:
    lines, remaining = [], dict(values)
    for line in path.read_text().splitlines() if path.exists() else []:
        key = line.split("=", 1)[0].strip()
        if not line.lstrip().startswith("#") and key in values:
            if key in remaining:
                lines.append(key + "=" + remaining.pop(key))
        else:
            lines.append(line)
    lines.extend(k + "=" + v for k, v in remaining.items())
    write_cursor(path, "\n".join(lines) + "\n")


def digest(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def snapshot(root: Path) -> dict:
    return {name: digest(root / name) for name in CONFIG_FILES}


def create(root: Path) -> Path:
    directory = root / "state/setup"
    directory.mkdir(parents=True, exist_ok=True, mode=0o700)
    directory.chmod(0o700)
    proposal = Path(tempfile.mkdtemp(prefix="proposal-", dir=directory))
    (proposal / "state").mkdir(mode=0o700)
    for name in CONFIG_FILES + AUTH_FILES:
        source, target = root / name, proposal / name
        if source.exists():
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if name.endswith(".session"):
                # A running poller may have an open SQLite session/WAL.
                copy_auth(source, target)
            else:
                shutil.copyfile(source, target)
            target.chmod(0o600)
    write_cursor(proposal / "baseline.json", json.dumps(snapshot(root)))
    if (proposal / "state/.env").exists():
        write_cursor(proposal / "original.env", (proposal / "state/.env").read_text())
    return proposal


def save_pending(root: Path, proposal: Path) -> None:
    write_cursor(root / "state/setup/pending.json", json.dumps({"directory": proposal.name}))


def pending(root: Path) -> Path | None:
    pointer = root / "state/setup/pending.json"
    if not pointer.exists():
        return None
    name = json.loads(pointer.read_text())["directory"]
    if not re.fullmatch(r"proposal-[a-zA-Z0-9_-]+", name):
        raise ValueError("Invalid saved proposal path")
    directory = pointer.parent / name
    if not directory.is_dir():
        raise ValueError("Saved proposal is missing")
    return directory


def discard(root: Path, proposal: Path) -> None:
    if proposal.parent != root / "state/setup" or not proposal.name.startswith("proposal-"):
        raise ValueError("Invalid proposal directory")
    if pending(root) == proposal:
        (root / "state/setup/pending.json").unlink()
    shutil.rmtree(proposal)


def invalidate_auth(proposal: Path, before: dict, after: dict) -> None:
    for name, keys in zip(AUTH_FILES, AUTH_KEYS):
        if any(before.get(k) != after.get(k) for k in keys):
            (proposal / name).unlink(missing_ok=True)
    if before.get("TELEGRAM_BOT_TOKEN") != after.get("TELEGRAM_BOT_TOKEN"):
        write_env(proposal / "state/.env", {"TELEGRAM_CHAT_ID": ""})


def assert_current(root: Path, proposal: Path) -> None:
    before = json.loads((proposal / "baseline.json").read_text())
    if snapshot(root) != before:
        raise ValueError("Active settings changed since this proposal began. Start a new proposal to preserve those changes.")


def validate_configuration(root: Path, *, ready: bool = False) -> list[str]:
    errors = []
    manifest = load(root / "state")
    if manifest is None:
        return ["Feature selection is missing"]
    env = load_env(root / "state/.env")
    missing = [key for key in required_credentials(manifest) if not env.get(key, "").strip() or env[key].upper().startswith("CHANGE")]
    if missing:
        errors.append("Missing credentials: " + ", ".join(missing))
    selected = manifest["features"]
    if selected["telegram"]:
        try:
            if int(env.get("TELEGRAM_API_ID", "0")) <= 0:
                raise ValueError()
        except ValueError:
            errors.append("Telegram API ID must be a positive integer")
        channels = json.loads((root / "state/channels.json").read_text())["channels"]
        active = [c for c in channels if c.get("enabled", True)]
        if not active:
            errors.append("Add at least one Telegram channel")
        users, prefixes = set(), set()
        for ch in active:
            username, prefix = ch.get("username", ""), ch.get("queue_prefix", "")
            if (not isinstance(username, str) or not re.fullmatch(r"[A-Za-z0-9_]+", username)
                    or username.upper().startswith("CHANGE") or username in users):
                errors.append("Telegram usernames must be unique, without @ or URL prefixes")
            if not isinstance(prefix, str) or not re.fullmatch(r"[a-zA-Z0-9_-]+", prefix) or prefix in prefixes:
                errors.append("Telegram queue prefixes must be unique letters/digits/_/-")
            users.add(username); prefixes.add(prefix)
            if not all(isinstance(ch.get(k), str) and ch[k].strip() for k in ("description", "message_format")):
                errors.append("Describe each Telegram channel and its message format")
    if selected["email"]:
        try:
            resolve_monitored_folder(env.get("OUTLOOK_FOLDER", ""), env.get("OUTLOOK_FOLDER_CONFIRM", ""))
        except ValueError as exc:
            errors.append(str(exc))
    feeds_path = root / "state/feeds.json"
    feeds = validate_feeds(json.loads(feeds_path.read_text())) if feeds_path.exists() else {}
    if ready and selected["rss"] and not any(f.get("enabled", True) for f in feeds.values()):
        errors.append("RSS is selected but no publisher is enabled; select feeds or disable the RSS input")
    if ready:
        errors.extend(matching_gaps(root))
        if selected["briefings"]:
            for filename in ("skills/job-brief/SKILL.md", "skills/job-brief/references/profile-for-relevance.md"):
                if not (root / filename).exists() or not (root / filename).read_text().strip():
                    errors.append("Missing briefing profile: " + filename)
    return errors


def apply(root: Path, proposal: Path) -> dict[Path, bytes | None]:
    assert_current(root, proposal)
    updates, expected, backup = {}, {}, {}
    for name in CONFIG_FILES:
        source, target = proposal / name, root / name
        if source.exists() and digest(source) != digest(target):
            backup[target] = target.read_bytes() if target.exists() else None
            expected[target] = target.read_text() if target.exists() else None
            updates[target] = source.read_text()
    apply_text_updates(updates, expected)
    # Existing refresh/session state belongs to running workers, unless the
    # reviewed account credentials changed or this is the first authorization.
    old_env = load_env(proposal / "original.env") if (proposal / "original.env").exists() else {}
    new_env = load_env(proposal / "state/.env")
    try:
        for name, credentials in zip(AUTH_FILES, AUTH_KEYS):
            source, target = proposal / name, root / name
            if source.exists():
                if not target.exists() or any(old_env.get(k) != new_env.get(k) for k in credentials):
                    backup[target] = target.read_bytes() if target.exists() else None
                copy_auth(source, target)
    except Exception:
        restore(backup)
        raise
    return backup


def restore(backup: dict[Path, bytes | None]) -> None:
    for path, content in backup.items():
        if content is None:
            path.unlink(missing_ok=True)
        else:
            fd, temporary = tempfile.mkstemp(dir=path.parent)
            with os.fdopen(fd, "wb") as stream:
                stream.write(content)
            os.replace(temporary, path)
