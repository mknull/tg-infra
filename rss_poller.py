#!/usr/bin/env python3
"""Fetch configured RSS/Atom feeds and enqueue new or changed job descriptions."""

import calendar
import fcntl
import hashlib
import json
import logging
import os
import re
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta, timezone
from html.parser import HTMLParser
from html.entities import name2codepoint
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import feedparser

from lib import STATE_DIR, setup_logging, write_audit
from lib.sources import feed_subscriptions, load_feeds

MAX_FEED_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 30
MAX_DETAIL_FETCHES = 20
_HOST_REQUESTS = {}


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


def _web_url(value: str, base: str) -> str:
    url = urljoin(base, value.strip()) if value else ""
    parsed = urlsplit(url)
    return url if parsed.scheme in ("http", "https") and parsed.netloc else ""


class _FeedText(HTMLParser):
    """Keep paragraph boundaries and application links in feed descriptions."""

    def __init__(self, base: str):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.parts = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if self.hidden:
            return
        if tag in ("p", "div", "br", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")
        if tag == "a":
            link = _web_url(dict(attrs).get("href", ""), self.base)
            if link:
                self.parts.append(f" {link} ")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)
        if not self.hidden and tag in ("p", "div", "li", "h1", "h2", "h3", "tr"):
            self.parts.append("\n")

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def plain_text(value: str, base: str, *, html: bool = True) -> str:
    if html:
        parser = _FeedText(base)
        parser.feed(value)
        parser.close()
        value = "".join(parser.parts)
    return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


def _repair_html_entities(raw: bytes) -> bytes:
    """Translate known HTML entities to XML character references, outside CDATA.

    Some publishers emit &rsquo; in XML text. Unknown entities and structural
    XML errors still fail parsing; the original response is always retained.
    """
    def replace(match):
        name = match[1].decode("ascii")
        if name in ("amp", "lt", "gt", "quot", "apos") or name not in name2codepoint:
            return match[0]
        return f"&#{name2codepoint[name]};".encode()

    parts = re.split(rb"(<!\[CDATA\[.*?\]\]>|<!--.*?-->)", raw, flags=re.DOTALL)
    return b"".join(part if i % 2 else re.sub(rb"&([A-Za-z][A-Za-z0-9]+);", replace, part)
                    for i, part in enumerate(parts))


def parse_feed(raw: bytes, feed_url: str, *, allow_empty: bool = False,
               repair_html_entities: bool = False, merge_variants: bool = False) -> list[dict]:
    """Normalize RSS 1/2 and Atom without fetching linked pages or inventing dates."""
    if repair_html_entities:
        raw = _repair_html_entities(raw)
    parsed = feedparser.parse(raw, response_headers={"content-location": feed_url,
                                                   "content-type": "application/xml"})
    if parsed.bozo or not parsed.version.startswith(("rss", "atom")):
        raise ValueError(f"invalid RSS/Atom feed: {parsed.get('bozo_exception', parsed.version)}")
    if not parsed.entries and not allow_empty:
        raise ValueError("empty feed: not a successful job inventory")
    if not parsed.entries and (not parsed.feed.get("title") or
                               not _web_url(parsed.feed.get("link", ""), feed_url)):
        raise ValueError("empty feed needs channel metadata")
    items = {}
    for entry in parsed.entries:
        url = _web_url(entry.get("link", ""), feed_url)
        external_id = entry.get("id") or url
        title = plain_text(entry.get("title", ""), url or feed_url)
        if not external_id or not url or not title:
            raise ValueError("feed entry needs a stable ID or URL, a title, and an HTTP(S) link")
        body = next((c for c in entry.get("content", [])
                     if c.get("type", "") in ("text/plain", "text/html", "application/xhtml+xml")),
                    entry.get("summary_detail", {}))
        description = plain_text(body.get("value", ""), url,
                                 html=body.get("type") != "text/plain")
        categories = sorted({plain_text(tag.get("term", ""), url)
                             for tag in entry.get("tags", []) if tag.get("term")})
        category_line = f"\nCategories: {', '.join(categories)}" if categories else ""
        content = f"{title}\n{url}{category_line}\n\n{description}".strip()
        published = entry.get("published_parsed")
        posted_at = (datetime.fromtimestamp(calendar.timegm(published), timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ")) if published else None
        item = {"source_id": str(external_id), "url": url, "title": title,
                "content": content, "posted_at": posted_at, "content_hash": _digest(content),
                "categories": categories}
        # Repeated identical entries are harmless; conflicting identities need inspection.
        if external_id in items and items[external_id]["content_hash"] != item["content_hash"]:
            if not merge_variants:
                raise ValueError(f"conflicting entries for {external_id}")
            previous = items[external_id]
            # WWR publishes regional variants under one GUID. Preserve every
            # description, including geography/pay, in a deterministic revision.
            variants = sorted(set(previous.get("variants", [previous["content"]]) + [content]))
            item["variants"] = variants
            item["content"] = "\n\nPosting variant:\n".join(variants)
            item["content_hash"] = _digest(item["content"])
            item["categories"] = sorted(set(previous["categories"] + categories))
            dates = [date for date in (previous["posted_at"], posted_at) if date]
            item["posted_at"] = max(dates) if dates else None
        items[external_id] = item
    return list(items.values())


def _selected(item: dict, feed: dict) -> bool:
    prefixes = feed.get("include_title_prefixes", [])
    categories = {value.casefold() for value in feed.get("include_categories", [])}
    excluded_titles = tuple(p.casefold() for p in feed.get("exclude_title_prefixes", []))
    excluded_urls = tuple(p.replace("http://", "https://") for p in feed.get("exclude_url_prefixes", []))
    return ((not prefixes or item["title"].casefold().startswith(tuple(p.casefold() for p in prefixes)))
            and (not categories or not item["categories"] or bool(categories.intersection(c.casefold() for c in item["categories"])))
            and not (excluded_titles and item["title"].casefold().startswith(excluded_titles))
            and not (excluded_urls and item["url"].replace("http://", "https://").startswith(excluded_urls)))


def fetch_feed(url: str, previous: dict) -> tuple[bytes | None, dict]:
    """Bounded HTTP fetch with conditional requests and transient-only retries."""
    headers = {"User-Agent": "telegram-MCP-rss/1.0", "Accept": "application/atom+xml, application/rss+xml, application/xml, text/xml"}
    if previous.get("url") == url:
        for field, header in (("etag", "If-None-Match"), ("last_modified", "If-Modified-Since")):
            if previous.get(field):
                headers[header] = previous[field]
    for attempt in range(3):
        delay = 2 ** attempt
        try:
            host = urlsplit(url).netloc
            pause = 1 - (time.monotonic() - _HOST_REQUESTS.get(host, 0))
            if pause > 0:
                time.sleep(pause)
            _HOST_REQUESTS[host] = time.monotonic()
            with urllib.request.urlopen(urllib.request.Request(url, headers=headers),
                                        timeout=FETCH_TIMEOUT) as response:
                raw = response.read(MAX_FEED_BYTES + 1)
                if len(raw) > MAX_FEED_BYTES:
                    raise ValueError("feed exceeds 2 MiB limit")
                return raw, {"etag": response.headers.get("ETag"),
                             "last_modified": response.headers.get("Last-Modified"),
                             "response_url": response.geturl()}
        except urllib.error.HTTPError as error:
            if error.code == 304:
                if not previous.get("retrieved_at"):
                    raise ValueError("304 received without a previously ingested feed") from error
                return None, {}
            if error.code != 429 and not 500 <= error.code < 600:
                raise
            if attempt == 2:
                raise
            retry_after = error.headers.get("Retry-After", "") if error.headers else ""
            if retry_after.isdigit():
                delay = min(30, int(retry_after))
        except (urllib.error.URLError, TimeoutError):
            if attempt == 2:
                raise
        time.sleep(delay)
    raise RuntimeError("feed fetch exhausted retries")


def _read_json(path: Path, default: dict) -> dict:
    return json.loads(path.read_text()) if path.exists() else default


def _parse_options(subscription: dict) -> dict:
    return {key: subscription.get(key, False) for key in
            ("allow_empty", "repair_html_entities", "merge_variants")}


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


def poll_feed(feed: dict, state_dir: Path, *, respect_cadence: bool = False) -> dict:
    """Collect subscriptions, then reconcile once per publisher before queueing.

    An unfamiliar representation is baselined without resending an existing job.
    A later change to a known representation can create a new revision. Keeping
    hashes of accepted revisions also prevents replay when feeds are switched back.
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
        return fetch_feed(url, previous)

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
                raw, response = fetch_feed(sub["url"], checkpoint if same else {})
                if raw is not None:
                    record["fetched_bytes"] = len(raw)
                    raw_path = subdir / "raw" / f"{_digest(raw)}.xml"
                    if not raw_path.exists():
                        atomic_write(raw_path, raw)
                    items = parse_feed(raw, response.get("response_url", sub["url"]), **_parse_options(sub))
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
                        from lib.rss_details import enrich_item
                        item = enrich_item(item, parser, directory / "details", detail_fetch, plain_text)
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


def main() -> None:
    setup_logging()
    from lib.config import STATE_DIR
    from lib.features import enabled
    if not enabled("rss", STATE_DIR):
        logging.info("rss disabled by configuration")
        return
    feeds = [feed for feed in load_feeds().values() if feed.get("enabled", True)]
    if not feeds:
        logging.info("No enabled RSS/Atom feeds in state/feeds.json.")
        return
    directory = STATE_DIR / "rss"
    directory.mkdir(parents=True, exist_ok=True)
    # Manual invocations and the timer must not race the persistent ingestion index.
    with (directory / "poll.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            logging.info("RSS poll already running.")
            return
        failed = False
        for position, feed in enumerate(feeds):
            if position:
                time.sleep(1)
            try:
                poll_feed(feed, STATE_DIR, respect_cadence="--due" in sys.argv)
            except Exception:
                logging.exception("[rss/%s] source failed", feed["id"])
                failed = True
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
