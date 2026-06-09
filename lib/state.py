"""Cursor file I/O plumbing — atomic writes, consistent permissions.

This module owns only the *mechanism* of persisting a small cursor string to
disk: reading it back, and writing it atomically with restrictive permissions.
It deliberately knows nothing about the cursor's *meaning* — callers parse and
format their own values (integer message-ids, ISO timestamps, ...). The point
is to kill the drift that crept into the four cursor sites (some chmod'd, some
appended a newline, none wrote atomically), not to unify their semantics.
"""

import os
from pathlib import Path


def read_cursor(path: Path) -> str | None:
    """Return the stripped cursor value, or None if the file is absent/empty."""
    try:
        val = path.read_text().strip()
    except FileNotFoundError:
        return None
    return val if val else None


def write_cursor(path: Path, value: str, *, mode: int = 0o600) -> None:
    """Atomically write a cursor value with restrictive permissions.

    Writes to a sibling temp file, chmods it, then os.replace()s it into place
    so a crash mid-write can never leave a truncated cursor on disk.
    """
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(value)
    tmp.chmod(mode)
    os.replace(tmp, path)
