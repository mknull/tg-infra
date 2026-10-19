"""Read-only RSS lifecycle checks: poll -> arrival/queue -> decision -> delivery.

Unfinished work is checked across all retained history, independently of the
reporting window. No network calls, model judgments, or repairs belong here.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .sources import feed_subscriptions, load_feeds

POLL_MAX_AGE = timedelta(hours=48)
PROCESSING_GRACE = timedelta(hours=1)


def _time(value: str) -> datetime:
    result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timestamp needs a timezone")
    return result


def _object(path: Path) -> dict:
    value = json.loads(path.read_text())
    if not isinstance(value, dict):
        raise ValueError("expected a JSON object")
    return value


def _records(path: Path, problems: list[str]) -> list[dict]:
    try:
        lines = path.read_text().splitlines()
    except FileNotFoundError:
        return []
    except OSError as exc:
        problems.append(f"RSS EVIDENCE INVALID: {path.name}: {exc}")
        return []
    records = []
    for number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            record = json.loads(line)
            if not isinstance(record, dict):
                raise ValueError("expected a JSON object")
            records.append(record)
        except ValueError as exc:
            problems.append(f"RSS EVIDENCE INVALID: {path.name}:{number}: {exc}")
    return records


def _evaluation_problem(record: dict) -> str | None:
    flash, pro = record.get("flash", {}), record.get("pro", {})
    if not isinstance(flash, dict) or not isinstance(pro, dict):
        return "invalid Flash/Pro record"
    for stage, result in (("Flash", flash), ("Pro", pro)):
        if result.get("decision") in ("error", "dead_letter"):
            return f"{stage} {result['decision']}: {result.get('reason', '')}"
    windows = flash.get("windows", [])
    if not isinstance(windows, list) or any(
        not isinstance(w, dict) or w.get("decision") not in
        ("read_more", "pass_to_pro", "disqualified") for w in windows
    ):
        return "invalid or failed Flash window"
    decision = flash.get("decision")
    if decision == "skip" and not pro:
        if windows and windows[-1]["decision"] != "disqualified":
            return "Flash skip contradicts its last window"
        return None
    if decision != "flag" or pro.get("decision") not in ("skip", "send"):
        return f"invalid cascade: Flash {decision!r} -> Pro {pro.get('decision')!r}"
    if windows:
        last = windows[-1]
        if last["decision"] == "disqualified" or (
            last["decision"] == "read_more" and
            (not last.get("total_lines") or last.get("window_end") != last["total_lines"])
        ):
            return "Pro called before Flash passed or exhausted the posting"
    if pro["decision"] == "send" and (not isinstance(pro.get("message"), str)
                                      or not pro["message"].strip()):
        return "Pro send has no message"
    return None


def check_rss_health(state: Path, *, since: datetime,
                     now: datetime | None = None) -> tuple[list[str], list[str]]:
    """Return (information, problems); callers fail health on any problem.

    The window bounds historical evaluation/duplicate checks. Pending arrivals,
    latest failed evaluations, and sends without acknowledgements never age
    out. Intentionally disabled feeds may retain a paused queue.
    """
    now = now or datetime.now(timezone.utc)
    info, problems = [], []
    observed_messages = {}
    try:
        feeds = load_feeds(state / "feeds.json")
    except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
        feeds = {}
        problems.append(f"RSS configuration invalid: {exc}")

    for feed_id, feed in feeds.items():
        if not feed.get("enabled", True):
            continue
        publisher_dir = state / "rss" / feed_id
        for subscription in feed_subscriptions(feed):
            label = f"{feed_id}/{subscription['id']}" if "subscriptions" in feed else feed_id
            directory = publisher_dir / "subscriptions" / subscription["id"] if "subscriptions" in feed else publisher_dir
            try:
                status = _object(directory / "status.json")
                age = now - _time(status["at"])
                max_hours = subscription["poll_interval_hours"] * 2
                if status["ok"] is not True:
                    problems.append(f"RSS {label}: {status.get('error', 'poll failed')}")
                elif age > timedelta(hours=max_hours):
                    problems.append(f"RSS {label}: last successful poll over {max_hours} hours ago")
                else:
                    counts = ", ".join(f"{key}={status[key]}" for key in
                                       ("new", "changed", "unchanged", "filtered", "queued") if key in status)
                    detail = "not modified (304)" if status.get("not_modified") else counts
                    info.append(f"RSS {label}: fetched {age.total_seconds() / 3600:.1f}h ago"
                                + (f" ({detail})" if detail else ""))
                    if not (publisher_dir / "index.json").exists() or not (directory / "index.json").exists():
                        problems.append(f"RSS {label}: successful poll has no ingestion index")
                    if "subscriptions" in feed or status.get("observations_file"):
                        evidence = _object(directory / "observations.json")
                        if evidence.get("config_revision") != status.get("config_revision") or evidence.get("at") != status["at"]:
                            raise ValueError("observation evidence does not match the poll")
                        observed = evidence["items"]
                        count = evidence["parsed_count"]
                        retained = evidence.get("retained_count", 0)
                        if (not isinstance(observed, list) or type(count) is not int or count < 0 or
                            type(retained) is not int or retained < 0 or len(observed) != count + retained or
                            status.get("parsed_count") != count or status.get("retained_count", 0) != retained):
                            raise ValueError("parsed count does not match accounted items")
                        for observation in observed:
                            if (not isinstance(observation, dict) or
                                observation.get("disposition") not in ("queued", "unchanged", "excluded", "duplicate", "deferred") or
                                not isinstance(observation.get("reason"), str) or not observation["reason"] or
                                not isinstance(observation.get("source_id"), str) or not observation["source_id"]):
                                raise ValueError("invalid item disposition")
                            if observation["disposition"] == "deferred":
                                problems.append(f"RSS DEFERRED [{label}]: {observation['reason']}")
                            mid = observation.get("msg_id")
                            if observation["disposition"] == "queued" and (not isinstance(mid, str) or not mid):
                                raise ValueError("queued observation has no message ID")
                            if mid:
                                if not isinstance(mid, str):
                                    raise ValueError("invalid observed message ID")
                                _time(observation["arrived_at"])
                                observed_messages[mid] = {"feed_id": feed_id, "arrived_at": observation["arrived_at"]}
            except FileNotFoundError:
                problems.append(f"RSS {label}: never polled or missing poll evidence")
            except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
                problems.append(f"RSS {label}: invalid poll status: {exc}")

    # The index supplies evidence even if an arrival log or queue file was lost.
    indexed = dict(observed_messages)
    for path in sorted((state / "rss").glob("*/index.json")):
        try:
            checkpoint = _object(path)
            items = checkpoint["items"]
            for pending in checkpoint.get("deferred", {}).values():
                feed_id = path.parent.name
                if feed_id in feeds and not feeds[feed_id].get("enabled", True):
                    info.append(f"RSS PAUSED [{feed_id}]: retained detail fetch")
                else:
                    problems.append(f"RSS DEFERRED [{feed_id}]: {pending['item']['source_id']}: {pending['reason']}")
            if not isinstance(items, dict):
                raise ValueError("items must be an object")
            for item in items.values():
                if not item.get("msg_id") and item.get("exclusion") in ("future-only baseline", "older than initial backfill window"):
                    continue
                mid = item["msg_id"]
                if not isinstance(mid, str) or not mid:
                    raise ValueError("missing message ID")
                indexed[mid] = {"feed_id": path.parent.name, "arrived_at": item["first_seen"]}
        except (ValueError, KeyError, TypeError, OSError) as exc:
            problems.append(f"RSS {path.parent.name}: invalid ingestion index: {exc}")

    queued = {}
    for path in sorted((state / "message_queue").glob("*.json")):
        try:
            entry = _object(path)
            meta = entry["meta"]
            if meta.get("source") != "rss":
                continue
            mid = meta["message_id"]
            if not isinstance(mid, str) or not mid:
                raise ValueError("missing message ID")
            queued[mid] = {"feed_id": meta["feed_id"], "arrived_at": meta["ts"]}
        except FileNotFoundError:
            continue  # The worker may have moved it since glob().
        except (ValueError, KeyError, TypeError, AttributeError, OSError) as exc:
            if path.name.startswith("rss-"):
                problems.append(f"RSS QUEUE INVALID: {path.name}: {exc}")

    grouped = defaultdict(list)
    for record in _records(state / "audit" / "rss.jsonl", problems):
        mid = record.get("msg_id")
        if not isinstance(mid, str) or not mid:
            problems.append("RSS EVIDENCE INVALID: arrival/evaluation without a message ID")
            continue
        grouped[mid].append(record)
    deliveries = defaultdict(list)
    if grouped or indexed or queued:
        for record in _records(state / "audit" / "delivery.jsonl", problems):
            if record.get("message_type") == "job_match" and isinstance(record.get("ref"), str):
                deliveries[record["ref"]].append(record)

    pending, paused, completed = 0, 0, 0
    for mid in sorted(grouped.keys() | indexed.keys() | queued.keys()):
        records = grouped[mid]
        evidence = records[0] if records else queued.get(mid, indexed.get(mid))
        feed_id = evidence.get("feed_id", "unknown")
        label = f"[{feed_id}] {mid}"
        arrivals = [r for r in records if r.get("stage") == "arrival"]
        evaluations = [r for r in records if "flash" in r or "pro" in r]
        try:
            age = now - _time(evidence["arrived_at"])
            recent = any(_time(r[stage]["at"]) >= since for r in evaluations
                         for stage in ("flash", "pro") if isinstance(r.get(stage), dict))
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            problems.append(f"RSS EVIDENCE INVALID {label}: {exc}")
            continue

        if not evaluations:
            if mid in queued and feed_id in feeds and not feeds[feed_id].get("enabled", True):
                paused += 1
                info.append(f"RSS PAUSED {label}: feed disabled; queued item retained")
            elif age <= PROCESSING_GRACE:
                pending += 1
            elif mid in queued:
                problems.append(f"RSS STALLED {label}: queued for {age.total_seconds() / 3600:.1f}h without evaluation")
            else:
                problems.append(f"RSS UNACCOUNTED {label}: observed item has neither a queued item nor an evaluation")
            continue

        completed += 1
        if not arrivals:
            problems.append(f"RSS ARRIVAL MISSING {label}: evaluation has no arrival record")
        if len(evaluations) > 1 and recent:
            problems.append(f"RSS DUPLICATE EVALS {label}: {len(evaluations)} evaluations of one revision")
        # The latest outcome still matters when the original attempt is old.
        # A missing Pro stage or dead letter must not become healthy with age.
        for record in evaluations if recent else evaluations[-1:]:
            problem = _evaluation_problem(record)
            if problem:
                problems.append(f"RSS PROCESSING FAILED {label}: {problem}")

        # Delivery is written before the completed evaluation. A recorded send
        # therefore needs a matching acknowledgement immediately, even if old.
        sends = [r for r in evaluations if isinstance(r.get("pro"), dict)
                 and r["pro"].get("decision") == "send"]
        attempts = deliveries[mid]
        accepted = [r for r in attempts if r.get("success") is True
                    and r.get("platform_id") and not r.get("error")]
        if sends and not accepted:
            if attempts:
                problems.append(f"RSS DELIVERY FAILED {label}: {attempts[-1].get('error') or 'no platform acknowledgement'}")
            else:
                problems.append(f"RSS DELIVERY MISSING {label}: Pro send has no job_match delivery result")
        if accepted and not sends:
            problems.append(f"RSS UNEXPECTED DELIVERY {label}: acknowledgement without a send decision")
        if len(accepted) > 1 and recent:
            problems.append(f"RSS DUPLICATE DELIVERY {label}: {len(accepted)} acknowledgements for one revision")

    if grouped or indexed or queued:
        info.append(f"rss lifecycle: {completed} evaluated, {pending} pending within 1h, {paused} paused")
    else:
        info.append("rss: no observed items")
    return info, problems
