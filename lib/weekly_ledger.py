"""Weekly-report delivery ledger — the single source of truth for "was this
week's report sent?".

Why this exists
---------------
The weekly report is sent by email via Microsoft Graph on Sunday. A boot-time
DNS race (or any transient outage) can make that one shot fail silently, and
``Persistent=true`` only re-runs a *missed* timer, never a *failed* one. A
recovery cycle retries every 2h — but a naive retry could send up to twelve
duplicate emails a day.

The ledger makes the retry safe and observable:

* **One file per ISO report-week** under ``state/weekly/<week_key>.json`` holding
  the *current* lifecycle state, not an append trail. Success supersedes
  failure, so the audit stays clean while still recording that recovery happened
  (``recovered`` / ``attempts`` breadcrumb).
* **Fail closed.** A send only happens on a *positive* "not sent" determination
  (Tier-1 ledger says not-sent AND Tier-2 Graph Sent Items confirms absent).
  Any uncertainty — network down, ``429``, ``5xx``, unexpected shape — is
  recorded as ``unknown`` with a reason and *defers* rather than sends. So a
  network outage can never cause a duplicate; it can only delay.
* **Stable week key.** The report week is anchored to the most-recent Sunday,
  not ``now``, so the scheduled Sunday run and a Wednesday recovery run compute
  the *same* key and the *same* subject — true idempotency.

Terminal states (``sent``, ``empty``, ``missed``) are never reopened.
"""

import json
import os
from datetime import datetime, timedelta, timezone

from .config import STATE_DIR
from .graph import graph_get

LEDGER_DIR = STATE_DIR / "weekly"

# Recovery is attempted Sun..Wed (days_since_sunday <= RECOVERY_WINDOW_DAYS).
# After that the week is declared `missed` — a loud, bounded alarm.
RECOVERY_WINDOW_DAYS = 3

# Lifecycle states that are never reopened.
TERMINAL = ("sent", "empty", "missed")

_SUBJECT_PREFIX = "Weekly Job Market Trend Report"


# ---------------------------------------------------------------------------
# Week anchoring — stable across the scheduled run and every recovery tick
# ---------------------------------------------------------------------------

def _days_since_sunday(now: datetime) -> int:
    # Python weekday(): Mon=0 .. Sun=6. Sunday → 0 days since Sunday.
    return (now.weekday() + 1) % 7


def subject_for(start: str, end: str) -> str:
    """Canonical subject line for a report week. Deterministic dedup key."""
    return f"{_SUBJECT_PREFIX} ({start} – {end})"


def report_week(now: datetime | None = None) -> dict:
    """Resolve the report week for ``now`` (defaults to UTC now).

    The week *ends* on the most recent Sunday on or before ``now`` and spans the
    seven preceding days. Returns the stable key, the human date range, the
    canonical subject, the content-selection window, and the Sent-Items floor.
    """
    now = now or datetime.now(timezone.utc)
    end_date = (now - timedelta(days=_days_since_sunday(now))).date()
    start_date = end_date - timedelta(days=7)
    start = start_date.isoformat()
    end = end_date.isoformat()
    return {
        "week_key": end,                       # the Sunday date, e.g. 2026-06-14
        "start": start,
        "end": end,
        "subject": subject_for(start, end),
        # Content window: records that arrived during the report week.
        "start_iso": f"{start}T00:00:00+00:00",
        "end_iso": f"{(start_date + timedelta(days=8)).isoformat()}T00:00:00+00:00",
        # The report cannot have been sent before its own Sunday; bound the
        # Sent-Items lookback there to keep the query small and exact.
        "sent_floor": f"{end}T00:00:00Z",
    }


def window_open(now: datetime | None = None) -> bool:
    """True while recovery should still attempt (Sun..Wed)."""
    now = now or datetime.now(timezone.utc)
    return _days_since_sunday(now) <= RECOVERY_WINDOW_DAYS


# ---------------------------------------------------------------------------
# Ledger — one mutable file per week, atomic, success supersedes failure
# ---------------------------------------------------------------------------

def _ledger_path(week_key: str):
    return LEDGER_DIR / f"{week_key}.json"


def load_ledger(week_key: str) -> dict | None:
    """Return the current ledger for a week, or None if untouched."""
    path = _ledger_path(week_key)
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except (json.JSONDecodeError, OSError):
        return None


def is_terminal(week_key: str) -> bool:
    led = load_ledger(week_key)
    return bool(led) and led.get("status") in TERMINAL


def _atomic_write(record: dict, path) -> None:
    """Write the ledger so a crash never leaves a partial file (cf. save_token)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(record, indent=2))
    os.replace(tmp, path)


def update_ledger(week_key: str, *, status: str, period=None,
                  reason: str | None = None) -> dict:
    """Record one recovery tick's outcome.

    ``status`` is the observed lifecycle state: ``sent``, ``unknown``,
    ``pending``, ``empty``, or ``missed``. Each call counts as an attempt and
    refreshes ``last_attempt``/``last_reason``. ``sent`` is sticky — once a week
    is sent it is never downgraded, and ``recovered`` records whether it took
    more than the first attempt. Terminal weeks are returned unchanged.
    """
    now_iso = datetime.now(timezone.utc).isoformat()
    led = load_ledger(week_key)
    if led and led.get("status") in TERMINAL:
        return led
    if led is None:
        led = {
            "week_key": week_key,
            "period": period,
            "status": "pending",
            "attempts": 0,
            "first_attempt": now_iso,
            "last_attempt": now_iso,
            "last_reason": None,
            "sent_at": None,
            "recovered": False,
        }
    led["attempts"] += 1
    led["last_attempt"] = now_iso
    led["last_reason"] = reason
    if period:
        led["period"] = period
    if status == "sent":
        led["status"] = "sent"
        led["sent_at"] = now_iso
        led["recovered"] = led["attempts"] > 1
    else:
        led["status"] = status
    _atomic_write(led, _ledger_path(week_key))
    return led


def all_ledgers(since_iso: str | None = None) -> list[dict]:
    """Load every ledger (optionally only those touched at/after ``since_iso``),
    most recent week first. Used by ``audit --health``."""
    if not LEDGER_DIR.exists():
        return []
    out = []
    for path in LEDGER_DIR.glob("*.json"):
        try:
            led = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        if since_iso and led.get("last_attempt", "") < since_iso:
            continue
        out.append(led)
    out.sort(key=lambda r: r.get("week_key", ""), reverse=True)
    return out


# ---------------------------------------------------------------------------
# Tier-2: authoritative "did we already send it?" check against Graph
# ---------------------------------------------------------------------------

def report_in_sent_items(access_token: str, subject: str,
                         since_iso: str) -> bool | None:
    """Has a message with this exact subject already been sent?

    Returns ``True`` (found — definitively sent), ``False`` (confirmed absent),
    or ``None`` (could not determine — fail closed, do NOT send).

    The subject match is done client-side on the recent Sent Items rather than
    via a server-side ``subject eq`` filter, so the non-ASCII en-dash in the
    subject can never cause a false "absent" (which would trigger a duplicate).
    Only a clean response with the subject missing counts as ``False``; every
    error, timeout, throttle, or unexpected shape returns ``None``.
    """
    try:
        resp = graph_get(
            "/me/mailFolders/sentitems/messages", access_token,
            params={
                "$filter": f"sentDateTime ge {since_iso}",
                "$select": "subject,sentDateTime",
                "$orderby": "sentDateTime desc",
                "$top": "100",
            },
        )
    except Exception:
        return None  # fail closed: unknown
    if not isinstance(resp, dict) or not isinstance(resp.get("value"), list):
        return None  # unexpected shape → unknown
    return subject in (m.get("subject", "") for m in resp["value"])
