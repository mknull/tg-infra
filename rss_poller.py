#!/usr/bin/env python3
"""Fetch configured RSS/Atom feeds and enqueue new or changed job descriptions."""

import calendar
import fcntl
import hashlib
import json
import logging
import os
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import feedparser

from lib import STATE_DIR, setup_logging, write_audit
from lib.sources import load_feeds

MAX_FEED_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 30


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
        value = "".join(parser.parts)
    return "\n".join(" ".join(line.split()) for line in value.splitlines() if line.strip())


def parse_feed(raw: bytes, feed_url: str) -> list[dict]:
    """Normalize RSS 1/2 and Atom without fetching linked pages or inventing dates."""
    parsed = feedparser.parse(raw, response_headers={"content-location": feed_url,
                                                   "content-type": "application/xml"})
    if parsed.bozo or not parsed.version.startswith(("rss", "atom")):
        raise ValueError(f"invalid RSS/Atom feed: {parsed.get('bozo_exception', parsed.version)}")
    if not parsed.entries:
        raise ValueError("empty feed: not a successful job inventory")
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
        content = f"{title}\n{url}\n\n{description}".strip()
        published = entry.get("published_parsed")
        posted_at = (datetime.fromtimestamp(calendar.timegm(published), timezone.utc)
                     .strftime("%Y-%m-%dT%H:%M:%SZ")) if published else None
        item = {"source_id": str(external_id), "url": url, "title": title,
                "content": content, "posted_at": posted_at, "content_hash": _digest(content)}
        # Repeated identical entries are harmless; conflicting identities need inspection.
        if external_id in items and items[external_id]["content_hash"] != item["content_hash"]:
            raise ValueError(f"conflicting entries for {external_id}")
        items[external_id] = item
    return list(items.values())


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
                if not previous.get("items"):
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


def poll_feed(feed: dict, state_dir: Path) -> dict:
    """Queue before checkpointing. Stable revision IDs also recover interrupted runs."""
    source = feed["id"]
    directory = state_dir / "rss" / source
    directory.mkdir(parents=True, exist_ok=True)
    status_path = directory / "status.json"
    now = _now()
    try:
        index_path = directory / "index.json"
        index = json.loads(index_path.read_text()) if index_path.exists() else {"items": {}}
        raw, response = fetch_feed(feed["url"], index)
        counts = {"new": 0, "changed": 0, "unchanged": 0, "queued": 0}
        if raw is not None:
            raw_path = directory / "raw" / f"{_digest(raw)}.xml"
            if not raw_path.exists():
                atomic_write(raw_path, raw)
            items = parse_feed(raw, response.get("response_url", feed["url"]))
            for item in items:
                key = _digest(item["source_id"])
                previous = index["items"].get(key, {})
                changed = previous.get("content_hash") != item["content_hash"]
                counts["new" if not previous else "changed" if changed else "unchanged"] += 1
                job_id = f"rss-{source}-{key}"
                msg_id = f"{job_id}-{item['content_hash']}"
                if changed:
                    queue_path = state_dir / "message_queue" / f"{msg_id}.json"
                    archive_path = state_dir / "messages" / f"{msg_id}.json"
                    if not queue_path.exists() and not archive_path.exists():
                        meta = {"source": "rss", "source_name": feed["name"], "feed_id": source,
                                "source_id": item["source_id"], "job_id": job_id,
                                "message_id": msg_id, "url": item["url"],
                                "ts": now, "posted_at": item["posted_at"],
                                "content_hash": item["content_hash"], "change": "changed" if previous else "new"}
                        atomic_write(queue_path, json.dumps({"content": item["content"], "meta": meta}).encode())
                        write_audit({"msg_id": msg_id, "source": "rss", "feed_id": source,
                                     "sender": feed["name"], "preview": item["title"],
                                     "content": item["content"], "url": item["url"],
                                     "arrived_at": now, "posted_at": item["posted_at"],
                                     "stage": "arrival", "job_id": job_id},
                                    state_dir / "audit" / "rss.jsonl")
                        counts["queued"] += 1
                index["items"][key] = {**item, "job_id": job_id, "msg_id": msg_id,
                                       "first_seen": previous.get("first_seen", now), "last_seen": now}
            index.update(response)
            index.update(url=feed["url"], raw_file=str(raw_path.relative_to(state_dir)), retrieved_at=now)
            # Missing entries are retained: a feed is a window, not a complete inventory.
            _write_json(index_path, index)
        status = {"ok": True, "at": now, "not_modified": raw is None, **counts}
        _write_json(status_path, status)
        logging.info("[rss/%s] %s", source, counts)
        return status
    except Exception as error:
        _write_json(status_path, {"ok": False, "at": now, "error": str(error)})
        raise


def main() -> None:
    setup_logging()
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
                poll_feed(feed, STATE_DIR)
            except Exception:
                logging.exception("[rss/%s] source failed", feed["id"])
                failed = True
        if failed:
            sys.exit(1)


if __name__ == "__main__":
    main()
