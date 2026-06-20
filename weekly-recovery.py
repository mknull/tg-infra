#!/usr/bin/env python3
"""Weekly-report recovery cycle.

Runs every 2 hours (5 minutes before the inbox poll). If the week's report was
not confirmed sent, it retries — safely. The actual work and all the
fail-closed dedup logic live in ``weekly-trend.py``'s ``ensure_week_report``;
this script only decides *whether* to attempt and, once the recovery window
(Sun..Wed) has closed without a confirmed send, promotes the week to ``missed``
so ``audit --health`` raises a loud, bounded alarm.

A network outage can never cause a duplicate here: the same Sent-Items check
that gates the send also shares fate with it (same Graph endpoint), so an
unreachable Graph defers rather than resends.
"""

import importlib.util
import logging
import sys
from datetime import datetime, timezone
from pathlib import Path

from lib import (load_env, setup_logging, report_week, load_ledger,
                 update_ledger, window_open)

# weekly-trend.py is hyphenated — load it the way the tests do.
_PROJECT = Path(__file__).resolve().parent
_spec = importlib.util.spec_from_file_location(
    "weekly_trend", _PROJECT / "weekly-trend.py")
weekly_trend = importlib.util.module_from_spec(_spec)
sys.modules["weekly_trend"] = weekly_trend
_spec.loader.exec_module(weekly_trend)


def main() -> None:
    setup_logging()
    now = datetime.now(timezone.utc)
    week = report_week(now)
    week_key = week["week_key"]

    led = load_ledger(week_key)
    if led and led.get("status") in ("sent", "empty", "missed"):
        logging.info("recovery: week %s already terminal (%s) — nothing to do",
                     week_key, led["status"])
        return

    if window_open(now):
        status = weekly_trend.ensure_week_report(load_env(), now=now)
        if status == "unknown":
            # Deferred (recorded in the ledger with a reason). Exit non-zero so
            # the service run shows as failed in the journal; the next tick
            # retries.
            sys.exit(1)
        return

    # Window closed and still not sent → declare the week missed. Loud, bounded.
    logging.error("recovery: window closed for week %s without a confirmed "
                  "send — marking missed", week_key)
    update_ledger(week_key, status="missed", period=[week["start"], week["end"]],
                  reason="recovery_window_expired")
    sys.exit(1)


if __name__ == "__main__":
    main()
