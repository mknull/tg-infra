"""Shared validation and ingestion for the existing RSS/Atom website channel.

Extractors return job dictionaries; this module owns identity, deduplication,
queueing and collection evidence. RSS state paths and IDs remain compatible.
"""

import hashlib
import json
import logging
import os
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit

from .audit import write_audit
from .sources import feed_subscriptions

MAX_DETAIL_FETCHES = 20


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _digest(value: str | bytes) -> str:
    return hashlib.sha256(value.encode() if isinstance(value, str) else value).hexdigest()


def atomic_write(path: Path, data: bytes) -> None:
    """Publish complete files; the triage worker never sees a partial JSON item."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix=".rss-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        Path(name).unlink(missing_ok=True)


def _write_json(path: Path, value: dict) -> None:
    atomic_write(path, json.dumps(value, ensure_ascii=False).encode())


def normalize_job(item: dict) -> dict:
    """Validate extracted fields without rewriting text or inventing dates.

    Required: source_id (the website's job ID), HTTP(S) url, title, content.
    Optional: posted_at (timezone-aware ISO timestamp or None), categories
    (list of nonempty strings). The content hash is always computed here.
    """
    if not isinstance(item, dict):
        raise ValueError("extracted job must be an object")
    for field in ("source_id", "url", "title", "content"):
        if not isinstance(item.get(field), str) or not item[field].strip():
            raise ValueError(f"extracted job needs a nonempty string: {field}")
    url = urlsplit(item["url"])
    if url.scheme not in ("http", "https") or not url.hostname:
        raise ValueError("extracted job needs an HTTP(S) url")
    posted_at = item.get("posted_at")
    if posted_at is not None:
        if not isinstance(posted_at, str):
            raise ValueError("posted_at must be an ISO timestamp or null")
        try:
            date = datetime.fromisoformat(posted_at.replace("Z", "+00:00"))
        except ValueError as error:
            raise ValueError("posted_at must be an ISO timestamp or null") from error
        if date.tzinfo is None:
            raise ValueError("posted_at needs a timezone")
    categories = item.get("categories", [])
    if not isinstance(categories, list) or any(not isinstance(c, str) or not c.strip() for c in categories):
        raise ValueError("categories must be a list of nonempty strings")
    return {**item, "posted_at": posted_at, "categories": list(categories),
            "content_hash": _digest(item["content"])}


def validate_jobs(items: list[dict], *, allow_empty: bool = False) -> list[dict]:
    """Validate a whole extraction before publishing any part of it."""
    if not isinstance(items, list):
        raise ValueError("extractor must return a list of jobs")
    if not items and not allow_empty:
        raise ValueError("empty extraction: not a successful job inventory")
    jobs = [normalize_job(item) for item in items]
    if len({item["source_id"] for item in jobs}) != len(jobs):
        raise ValueError("extractor returned duplicate job identities")
    return jobs


def _selected(item: dict, feed: dict) -> bool:
    prefixes = feed.get("include_title_prefixes", [])
    categories = {value.casefold() for value in feed.get("include_categories", [])}
    excluded_titles = tuple(p.casefold() for p in feed.get("exclude_title_prefixes", []))
    excluded_urls = tuple(p.replace("http://", "https://") for p in feed.get("exclude_url_prefixes", []))
    return ((not prefixes or item["title"].casefold().startswith(tuple(p.casefold() for p in prefixes)))
            and (not categories or not item["categories"] or bool(categories.intersection(c.casefold() for c in item["categories"])))
            and not (excluded_titles and item["title"].casefold().startswith(excluded_titles))
            and not (excluded_urls and item["url"].replace("http://", "https://").startswith(excluded_urls)))


def _read_json(path: Path, default: dict) -> dict:
    return json.loads(path.read_text()) if path.exists() else default


def _subscription_dir(directory: Path, feed: dict, subscription: dict) -> Path:
    return directory / "subscriptions" / subscription["id"] if "subscriptions" in feed else directory


def _backfill_reason(item: dict, subscription: dict, initial: bool, now: str) -> str | None:
    if not initial:
        return None
    policy = subscription.get("backfill", {"mode": "all"})
    if policy["mode"] == "future":
        return "future-only baseline"
    if policy["mode"] == "recent" and item.get("posted_at"):
        cutoff = datetime.fromisoformat(now.replace("Z", "+00:00")) - timedelta(days=policy.get("days", 30))
        if datetime.fromisoformat(item["posted_at"].replace("Z", "+00:00")) < cutoff:
            return "older than initial backfill window"
    return None  # Unknown dates are kept in recent mode.


def _queue_item(item: dict, previous: dict, feed: dict, state: Path, now: str,
                subscription_ids: list[str], revision: str) -> tuple[str, bool]:
    key = _digest(item["source_id"])
    job_id = f"rss-{feed['id']}-{key}"
    mid = f"{job_id}-{item['content_hash']}"
    queue_path = state / "message_queue" / f"{mid}.json"
    archive_path = state / "messages" / f"{mid}.json"
    written = not queue_path.exists() and not archive_path.exists()
    if written:
        meta = {"source": "rss", "source_name": feed["name"], "feed_id": feed["id"],
                "source_id": item["source_id"], "job_id": job_id, "message_id": mid,
                "url": item["url"], "ts": now, "posted_at": item["posted_at"],
                "content_hash": item["content_hash"], "change": "changed" if previous else "new",
                "categories": item["categories"], "subscription_ids": subscription_ids,
                "config_revision": revision}
        atomic_write(queue_path, json.dumps({"content": item["content"], "meta": meta}).encode())
        write_audit({"msg_id": mid, "source": "rss", "feed_id": feed["id"],
                     "sender": feed["name"], "preview": item["title"], "content": item["content"],
                     "url": item["url"], "arrived_at": now, "posted_at": item["posted_at"],
                     "stage": "arrival", "job_id": job_id, "subscription_ids": subscription_ids,
                     "config_revision": revision}, state / "audit" / "rss.jsonl")
    return mid, written


def ingest_feed(feed: dict, state_dir: Path, *, fetch, extract, enrich,
                respect_cadence: bool = False) -> dict:
    """Collect subscriptions, then reconcile once per publisher before queueing.

    An unfamiliar representation is baselined without resending an existing job.
    A later change to a known representation can create a new revision. Keeping
    hashes of accepted revisions also prevents replay when feeds are switched back.

    extract(raw, url, subscription) returns a list of job dictionaries.
    enrich(job, parser, cache, fetch) returns a job with complete text when the
    subscription requires details. Neither callback writes queue/audit state.
    """
    source = feed["id"]
    directory = state_dir / "rss" / source
    directory.mkdir(parents=True, exist_ok=True)
    now = _now()
    revision = _digest(json.dumps(feed, sort_keys=True))[:16]
    index_path = directory / "index.json"
    counts = {"new": 0, "changed": 0, "unchanged": 0, "queued": 0, "filtered": 0, "deferred": 0}
    detail_requests = 0
    def detail_fetch(url, previous):
        nonlocal detail_requests
        if detail_requests >= MAX_DETAIL_FETCHES:
            raise OSError("detail-fetch allowance exhausted; retained for next poll")
        detail_requests += 1
        index["detail_attempt_sequence"] = index.get("detail_attempt_sequence", 0) + 1
        return fetch(url, previous)

    try:
        index = _read_json(index_path, {"items": {}})
        records = []
        for position, sub in enumerate(feed_subscriptions(feed)):
            subdir = _subscription_dir(directory, feed, sub)
            checkpoint = _read_json(subdir / "index.json", {})
            if not checkpoint and index.get("url") == sub["url"] and index.get("retrieved_at"):
                checkpoint = {k: v for k, v in index.items() if k not in ("items", "deferred")}
            selection = {k: v for k, v in sub.items() if k not in ("id", "url")}
            same = checkpoint.get("selection") == selection and checkpoint.get("url") == sub["url"]
            last_status = _read_json(subdir / "status.json", {})
            due = True
            if respect_cadence and same and last_status.get("ok") is True:
                age = datetime.fromisoformat(now.replace("Z", "+00:00")) - datetime.fromisoformat(last_status["at"].replace("Z", "+00:00"))
                due = age >= timedelta(hours=sub["poll_interval_hours"])
            record = {"sub": sub, "dir": subdir, "checkpoint": checkpoint,
                      "items": checkpoint.get("entries", []),
                      "initial": not checkpoint.get("retrieved_at") or checkpoint.get("url") != sub["url"],
                      "policy_changed": checkpoint.get("selection", {}).get("backfill") != sub.get("backfill"),
                      "migrated": bool(checkpoint) and not (subdir / "index.json").exists(),
                      "due": due, "fresh": False, "fetched_bytes": 0,
                      "observations": [], "retained": 0, "error": None}
            records.append(record)
            if not due:
                continue
            if position:
                time.sleep(1)
            try:
                raw, response = fetch(sub["url"], checkpoint if same else {})
                if raw is not None:
                    record["fetched_bytes"] = len(raw)
                    raw_path = subdir / "raw" / f"{_digest(raw)}.xml"
                    if not raw_path.exists():
                        atomic_write(raw_path, raw)
                    items = validate_jobs(extract(raw, response.get("response_url", sub["url"]), sub),
                                          allow_empty=sub.get("allow_empty", False))
                    record.update(items=items, fresh=True)
                    record["checkpoint"] = {**checkpoint, **response, "url": sub["url"],
                        "selection": selection, "raw_file": str(raw_path.relative_to(state_dir)),
                        "retrieved_at": now, "entries": items}
                elif not checkpoint.get("retrieved_at"):
                    raise ValueError("304 received without a previously ingested feed")
            except Exception as error:
                record["error"] = str(error)
                record["items"] = []

        if not any(r["due"] for r in records):
            return {"ok": True, "skipped": True, **counts}

        groups = {}
        for record in records:
            if record["error"]:
                continue
            for item in record["items"]:
                observation = {"source_id": item["source_id"], "job_id": f"rss-{source}-{_digest(item['source_id'])}"}
                record["observations"].append(observation)
                if not _selected(item, record["sub"]):
                    observation.update(disposition="excluded", reason="configured local exclusion")
                    if record["due"]:
                        counts["filtered"] += 1
                    continue
                groups.setdefault(_digest(item["source_id"]), []).append((record, item, observation))

        # Retry an already observed job even if it fell out of a short feed window.
        for key, pending in list(index.get("deferred", {}).items()):
            if key in groups:
                continue
            # A different category's filters cannot decide the fate of this job.
            # Removed subscriptions leave their pending work intact; restoring
            # the subscription (or observing the job in another) resumes it.
            record = next((r for r in records if r["due"] and
                           r["sub"]["id"] == pending["subscription_id"]), None)
            if record is not None:
                item = pending["item"]
                observation = {"source_id": item["source_id"], "job_id": f"rss-{source}-{key}",
                               "retained_from": pending["subscription_id"]}
                record["observations"].append(observation)
                record["retained"] += 1
                if not _selected(item, record["sub"]):
                    observation.update(disposition="excluded", reason="current local exclusion")
                    index["deferred"].pop(key)
                    continue
                groups[key] = [(record, item, observation)]

        # Never-attempted jobs go first, then the least recently attempted.
        # Persisted sequence numbers rotate retries even after a process restart
        # or while the same failed pages remain at the front of a feed.
        pending_items = index.get("deferred", {})
        ordered_keys = sorted(groups, key=lambda key: pending_items.get(key, {}).get("last_attempt", 0))
        for key in ordered_keys:
            candidates = groups[key]
            previous = index["items"].get(key, {})
            representations = dict(previous.get("representations", {}))
            # Seed the old broad-feed representation without changing historical IDs.
            if not representations and previous and index.get("url"):
                representations[index["url"]] = previous["content_hash"]
            changed = [(r, i, o) for r, i, o in candidates
                       if r["sub"]["url"] in representations and
                       representations[r["sub"]["url"]] != i["content_hash"]]
            chosen = sorted(changed or candidates,
                            key=lambda c: (-len(c[1]["content"]), c[1]["content_hash"], c[0]["sub"]["id"]))[0]
            record, item, _ = chosen
            incoming = {r["sub"]["url"]: i["content_hash"] for r, i, _ in candidates}
            subscriptions = sorted({r["sub"]["id"] for r, _, _ in candidates})
            should_queue = (not previous or bool(changed) or key in index.get("deferred", {}))
            exclusion = None
            if not previous and key not in index.get("deferred", {}):
                reasons = [_backfill_reason(i, r["sub"], r["initial"], now) for r, i, _ in candidates]
                if all(reasons):
                    exclusion = reasons[0]
                    should_queue = False
            elif previous and not previous.get("msg_id") and not changed and key not in index.get("deferred", {}):
                if any((r["initial"] or r["policy_changed"]) and
                       _backfill_reason(i, r["sub"], True, now) is None for r, i, _ in candidates):
                    should_queue = True
                else:
                    exclusion = previous.get("exclusion", "initial baseline")

            accepted = dict(previous) if previous else {**item, "first_seen": now}
            disposition, reason = "unchanged", "known job representation"
            if should_queue:
                requests_before = detail_requests
                try:
                    parser = index.get("deferred", {}).get(key, {}).get("enrich", record["sub"].get("enrich"))
                    if parser:
                        enriched = normalize_job(enrich(dict(item), parser, directory / "details", detail_fetch))
                        if any(enriched[field] != item[field] for field in ("source_id", "url")):
                            raise ValueError("detail extractor changed the job identity")
                        item = enriched
                except (OSError, ValueError) as error:
                    counts["deferred"] += 1
                    pending = index.get("deferred", {}).get(key, {})
                    index.setdefault("deferred", {})[key] = {"item": item,
                        "subscription_id": record["sub"]["id"], "reason": str(error),
                        "enrich": parser,
                        "first_seen": pending.get("first_seen", now),
                        "last_attempt": (index["detail_attempt_sequence"] if detail_requests > requests_before
                                         else pending.get("last_attempt", 0))}
                    for r, _, observation in candidates:
                        observation.update(disposition="deferred", reason=str(error))
                        r["error"] = f"required detail fetch failed: {error}"
                    continue
                index.get("deferred", {}).pop(key, None)
                seen_hashes = set(previous.get("revision_hashes", []))
                if previous.get("msg_id"):
                    seen_hashes.add(previous["content_hash"])
                if item["content_hash"] not in seen_hashes:
                    mid, written = _queue_item(item, previous, feed, state_dir, now, subscriptions, revision)
                    counts["queued"] += int(written)
                    counts["changed" if previous else "new"] += 1
                    accepted = {**item, "job_id": f"rss-{source}-{key}", "msg_id": mid,
                                "first_seen": previous.get("first_seen", now),
                                "revision_hashes": sorted(seen_hashes | {item["content_hash"]})}
                    disposition, reason = ("queued" if written else "duplicate"), ("changed representation" if previous else "new job")
                else:
                    disposition, reason = "duplicate", "revision already observed"
            else:
                counts["unchanged"] += int(bool(previous))
            if exclusion:
                accepted["exclusion"] = exclusion
                disposition, reason = "excluded", exclusion
            accepted.update(representations={**representations, **incoming}, subscription_ids=subscriptions)
            if any(r["fresh"] for r, _, _ in candidates):
                accepted["last_seen"] = now
            # A 304-only poll leaves an already complete checkpoint untouched.
            if accepted != previous:
                index["items"][key] = accepted
            for r, _, observation in candidates:
                observation.update(disposition=disposition if r is record else "duplicate",
                                   reason=reason if r is record else "overlapping subscription")
                if accepted.get("msg_id"):
                    observation["msg_id"] = accepted["msg_id"]
                    observation["arrived_at"] = accepted["first_seen"]

        # Queue files precede checkpoints. Stable IDs recover a failed publication.
        for record in records:
            if not record["due"]:
                continue
            subdir = record["dir"]
            if record["fresh"]:
                if subdir == directory:
                    index.update({k: v for k, v in record["checkpoint"].items() if k in
                                  ("url", "etag", "last_modified", "response_url", "selection",
                                   "raw_file", "retrieved_at", "entries")})
        before = _read_json(index_path, None)
        if index != before:
            _write_json(index_path, index)
        for record in records:
            if record["due"] and (record["fresh"] or record["migrated"]) and record["dir"] != directory:
                _write_json(record["dir"] / "index.json", record["checkpoint"])
        failed = []
        for record in records:
            if not record["due"]:
                continue
            subdir = record["dir"]
            evidence = {"at": now, "feed_id": source, "subscription_id": record["sub"]["id"],
                        "config_revision": revision, "parsed_count": len(record["items"]),
                        "fetched_bytes": record["fetched_bytes"],
                        "retained_count": record["retained"],
                        "items": record["observations"]}
            _write_json(subdir / "observations.json", evidence)
            dispositions = [item["disposition"] for item in record["observations"]]
            sub_counts = {key: dispositions.count(key) for key in
                          ("queued", "unchanged", "duplicate", "deferred")}
            sub_counts["filtered"] = dispositions.count("excluded")
            for key, reason in (("new", "new job"), ("changed", "changed representation")):
                sub_counts[key] = sum(item["disposition"] == "queued" and item["reason"] == reason
                                      for item in record["observations"])
            status = {"ok": not bool(record["error"]), "at": now, "config_revision": revision,
                      "parsed_count": len(record["items"]), "retained_count": record["retained"],
                      "fetched_bytes": record["fetched_bytes"],
                      "not_modified": not record["fresh"],
                      "observations_file": str((subdir / "observations.json").relative_to(state_dir)),
                      **sub_counts}
            if record["error"]:
                status["error"] = record["error"]
                failed.append(f"{record['sub']['id']}: {record['error']}")
            _write_json(subdir / "status.json", status)
        status = {"ok": not failed, "at": now, "not_modified": not any(r["fresh"] for r in records), **counts}
        if "subscriptions" in feed:
            status["subscriptions"] = [r["sub"]["id"] for r in records]
            if failed:
                status["error"] = "; ".join(failed)
            _write_json(directory / "status.json", status)
        if failed:
            raise ValueError("; ".join(failed))
        logging.info("[rss/%s] %s", source, counts)
        return status
    except Exception as error:
        _write_json(directory / "status.json", {"ok": False, "at": now, "error": str(error)})
        raise
