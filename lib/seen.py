"""Per-pipeline idempotency — exactly-once processing keyed by message id.

A cursor bounds how far back a pipeline queries; it does NOT guarantee a message
is evaluated only once. A re-fetch at the cursor boundary, a cursor that didn't
advance, or a retry all re-present the same message — and with a cursor as the
only guard, the message gets fully re-evaluated (wasted LLM calls, conflicting
flash decisions, the "duplicate eval" the audit flags). This ledger is the
authoritative gate: an id marked terminal for a pipeline is never evaluated
again. The cursor stays as a fetch bound; this is the dedup. Bounded by pruning
ids older than a retention window so it cannot grow without limit.
"""

import json
import os
import time
from pathlib import Path

from .config import STATE_DIR

SEEN_DIR = STATE_DIR / "seen"
RETENTION_DAYS = 14


class SeenLedger:
    """Message ids a pipeline has brought to a terminal state (skipped / sent /
    dead-lettered). Load once per run, test membership with ``in``, record
    terminal ids with ``add()``, then ``save()`` once at the end.

    Only TERMINAL ids are recorded — an id mid-retry is deliberately absent so
    it is retried, while a fully-handled id is never re-evaluated even if the
    cursor re-presents it.
    """

    def __init__(self, pipeline: str, *, retention_days: int = RETENTION_DAYS,
                 seen_dir: Path = SEEN_DIR):
        self.path = Path(seen_dir) / f"{pipeline}.json"
        self.retention = retention_days * 86400
        self._ids: dict = self._load()

    def _load(self) -> dict:
        try:
            return json.loads(self.path.read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}

    def __contains__(self, msg_id: str) -> bool:
        return msg_id in self._ids

    def add(self, msg_id: str) -> None:
        self._ids[msg_id] = int(time.time())

    def save(self) -> None:
        """Prune stale ids, then write atomically."""
        cutoff = int(time.time()) - self.retention
        self._ids = {k: v for k, v in self._ids.items() if v >= cutoff}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self._ids))
        os.replace(tmp, self.path)
