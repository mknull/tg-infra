#!/usr/bin/env python3
"""Fetch configured RSS/Atom feeds and enqueue new or changed job descriptions."""

import calendar
import fcntl
import logging
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from html.parser import HTMLParser
from html.entities import name2codepoint
from pathlib import Path
from urllib.parse import urljoin, urlsplit

import feedparser

from lib import STATE_DIR, setup_logging
from lib.job_ingestion import ingest_feed, normalize_job
from lib.sources import load_feeds

MAX_FEED_BYTES = 2 * 1024 * 1024
FETCH_TIMEOUT = 30
_HOST_REQUESTS = {}


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
        item = normalize_job({"source_id": str(external_id), "url": url, "title": title,
                              "content": content, "posted_at": posted_at, "categories": categories})
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
            item = normalize_job(item)
            item["categories"] = sorted(set(previous["categories"] + categories))
            dates = [date for date in (previous["posted_at"], posted_at) if date]
            item["posted_at"] = max(dates) if dates else None
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


def _parse_options(subscription: dict) -> dict:
    return {key: subscription.get(key, False) for key in
            ("allow_empty", "repair_html_entities", "merge_variants")}


def _extract(raw: bytes, url: str, subscription: dict) -> list[dict]:
    return parse_feed(raw, url, **_parse_options(subscription))


def _enrich(item: dict, parser: str, cache: Path, fetch) -> dict:
    from lib.rss_details import enrich_item
    return enrich_item(item, parser, cache, fetch, plain_text)


def poll_feed(feed: dict, state_dir: Path, *, respect_cadence: bool = False) -> dict:
    return ingest_feed(feed, state_dir, fetch=fetch_feed, extract=_extract,
                       enrich=_enrich, respect_cadence=respect_cadence)


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
