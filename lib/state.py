"""Cursor file I/O plumbing — atomic writes, consistent permissions.

This module owns only the *mechanism* of persisting a small cursor string to
disk: reading it back, and writing it atomically with restrictive permissions.
It deliberately knows nothing about the cursor's *meaning* — callers parse and
format their own values (integer message-ids, ISO timestamps, ...). The point
is to kill the drift that crept into the four cursor sites (some chmod'd, some
appended a newline, none wrote atomically), not to unify their semantics.
"""

import os
import tempfile
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


def apply_text_updates(updates: dict[Path, str], expected: dict[Path, str | None]) -> None:
    """Apply a reviewed proposal, refusing stale inputs and rolling back I/O failures.

    Each replacement is atomic. This is not a crash-atomic multi-file database
    transaction; callers needing one version must store it in a single file.
    """
    for path, previous in expected.items():
        current = path.read_text() if path.exists() else None
        if current != previous:
            raise ValueError(f"{path.name} changed during generation; review a fresh proposal")
    staged = {}
    originals = {}
    replaced = []
    try:
        for path, content in updates.items():
            path.parent.mkdir(parents=True, exist_ok=True)
            originals[path] = (path.read_bytes(), path.stat().st_mode & 0o777) if path.exists() else None
            fd, name = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
            staged[path] = Path(name)
            with os.fdopen(fd, "w") as stream:
                stream.write(content)
                stream.flush()
                os.fsync(stream.fileno())
            staged[path].chmod(originals[path][1] if originals[path] else 0o600)
        for path, temporary in staged.items():
            os.replace(temporary, path)
            replaced.append(path)
    except OSError:
        for path in reversed(replaced):
            original = originals[path]
            if original is None:
                path.unlink(missing_ok=True)
            else:
                write_cursor(path, original[0].decode(), mode=original[1])
        raise
    finally:
        for temporary in staged.values():
            temporary.unlink(missing_ok=True)
