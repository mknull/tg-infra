#!/usr/bin/env python3
"""Delivery canary — outcome verification for the egress path.

Pushes one synthetic message through the *real* deliver() path (the same code
a real job match uses: config resolution, chat_id, send_telegram, the
delivery.jsonl write) and confirms it was actually accepted. It mocks nothing —
that is the whole point. If delivery does not succeed it exits non-zero and
records the failure loudly, so a bot that silently stops delivering cannot keep
looking healthy.

Run it on a timer; pair it with `./audit --health`, which reads the status file
this writes. Tests in test_canary.py exercise the detector logic (does the
canary correctly call success a success and a failure a failure).
"""

import json
import logging
import sys
from datetime import datetime, timezone

from lib import deliver, STATE_DIR

CANARY_STATUS_FILE = STATE_DIR / "canary-status.json"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def run_canary(deliver_fn=deliver) -> tuple[bool, str]:
    """Send one synthetic message through the real delivery path.

    Returns (ok, detail). ok is True only if delivery actually succeeded —
    deliver() returns a platform id on success and None on any failure, so the
    return value is the real outcome, not an intent.
    """
    ts = _now()
    msg = (f"🐤 delivery canary {ts}\n"
           "Automated end-to-end delivery check — please ignore.")
    platform_id = deliver_fn("canary", msg)
    if not platform_id:
        return False, ("deliver() returned no platform id — the egress path is "
                       "broken (check TELEGRAM_CHAT_ID / bot token)")
    return True, f"delivered (platform_id={platform_id})"


def write_status(ok: bool, detail: str) -> None:
    """Persist the last canary result for ./audit --health to read."""
    CANARY_STATUS_FILE.parent.mkdir(parents=True, exist_ok=True)
    CANARY_STATUS_FILE.write_text(json.dumps({
        "at": _now(), "ok": ok, "detail": detail,
    }))


def main() -> None:
    ok, detail = run_canary()
    write_status(ok, detail)
    if ok:
        logging.info("canary OK — %s", detail)
        sys.exit(0)
    logging.error("CANARY FAILED — delivery is broken: %s", detail)
    sys.exit(1)


if __name__ == "__main__":
    main()
