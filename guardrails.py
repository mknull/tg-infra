#!/usr/bin/env python3
"""Guardrails for /briefme agent tool access.

Importable module — the tests at test_guardrails.py import from here.
When the agent loop is built, it calls these before every tool execution.
"""

import json
import re
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parent
SOURCE_DIR = PROJECT / "source"
BRIEFS_DIR = PROJECT / "workspace" / "briefs"
STATE_DIR = PROJECT / "state"
SKILLS_DIR = PROJECT / "skills"

# ---------------------------------------------------------------------------
# Fetch: two-stage guardrail
# ---------------------------------------------------------------------------

TRUSTED_DOMAINS = [
    "linkedin.com",
    "stepstone.de",
    "indeed.com",
    "glassdoor.com",
    "xing.com",
    "levels.fyi",
    "crunchbase.com",
    "wikipedia.org",
    ".github.io",
    "arbeitsagentur.de",
    "stackoverflow.com",
    "medium.com",
]

BLOCKED_SCHEMES = frozenset({"file:", "ftp:", "gopher:", "javascript:", "data:"})
BLOCKED_HOSTS = frozenset({"localhost", "127.0.0.1", "0.0.0.0", "[::1]"})
BLOCKED_NETS = (
    "10.", "172.16.", "172.17.", "172.18.", "172.19.",
    "172.20.", "172.21.", "172.22.", "172.23.", "172.24.",
    "172.25.", "172.26.", "172.27.", "172.28.", "172.29.",
    "172.30.", "172.31.", "192.168.",
)

DOMAIN_CLASSIFY_PROMPT = """You are a URL safety classifier for a research agent
that gathers PUBLIC information about companies, roles, salaries, and people.
Given a URL, respond with ONLY a JSON object:
{{"safe": true/false, "reason": "one sentence"}}

Safe: any legitimate public web page — company sites and career pages, job
boards, recruitment agencies, salary/news/review sites, professional or
academic profiles, encyclopaedias, blogs, and similar public information.
Unsafe: internal services, admin panels, file servers, login/credential or
phishing pages, or anything that is not a public, informational web page.

URL to classify: {url}"""


def url_to_host(url: str) -> str:
    """Strip scheme, path, port, and credentials from a URL."""
    for prefix in ("https://", "http://", "//"):
        if url.startswith(prefix):
            url = url[len(prefix):]
            break
    if "@" in url:
        url = url.split("@", 1)[1]
    return url.split("/")[0].split(":")[0].lower()


def _classify_domain(url: str, flash_fn=None) -> tuple[bool, str]:
    """Stage 2: ask a light model to classify an unknown domain.

    flash_fn(prompt: str) -> str is injected by the caller (the agent binds
    the real model + API key). When absent, fail closed.
    """
    if flash_fn is None:
        return False, "no classifier configured"
    prompt = DOMAIN_CLASSIFY_PROMPT.format(url=url)
    try:
        raw = flash_fn(prompt)
        result = json.loads(raw[raw.find("{"):raw.rfind("}") + 1])
        safe = result.get("safe", False)
        reason = result.get("reason", "no reason")
        return safe, reason
    except Exception as e:
        return False, f"classifier error: {e}"


def validate_fetch_url(url: str, flash_fn=None) -> tuple[bool, str]:
    """Two-stage fetch guardrail.

    Stage 1 (static): block dangerous schemes, internal IPs.
        Fast-path allow if domain is on the trusted list.
    Stage 2 (classifier): for unknown domains, ask a light model whether
        the URL looks like a legitimate job posting site. The classifier is
        injected as flash_fn(prompt: str) -> str (model + key bound by caller).
    """
    url_lower = url.lower().strip()

    # --- stage 1: static blocks ---
    for scheme in BLOCKED_SCHEMES:
        if url_lower.startswith(scheme):
            return False, f"blocked scheme: {scheme}"

    host = url_to_host(url_lower)
    if host in BLOCKED_HOSTS:
        return False, f"blocked host: {host}"
    for net in BLOCKED_NETS:
        if host.startswith(net):
            return False, f"blocked network: {net}*"

    # --- stage 1: fast-path trusted domains ---
    if any(d in host for d in TRUSTED_DOMAINS):
        return True, "ok (trusted)"

    # --- stage 2: Flash classifies the unknown domain ---
    return _classify_domain(url, flash_fn)


# ---------------------------------------------------------------------------
# Search: prompt-injection guard
# ---------------------------------------------------------------------------

INJECTION_MARKERS = [
    "ignore previous instructions",
    "ignore all previous",
    "system prompt:",
    "you are now",
    "new instructions:",
    "forget all previous",
    "your new role",
    "<system>",
    "<|system|>",
    "[system]",
    "do not refuse",
    "you must comply",
]
MAX_QUERY_LENGTH = 500


def validate_search_query(query: str) -> tuple[bool, str]:
    """Block search queries that try to prompt-inject the agent."""
    if len(query) > MAX_QUERY_LENGTH:
        return False, "query too long"
    q_lower = query.lower()
    for marker in INJECTION_MARKERS:
        if marker in q_lower:
            return False, f"injection pattern: {marker}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Filesystem sandbox
# ---------------------------------------------------------------------------

WRITABLE_PATHS = frozenset({BRIEFS_DIR})


def validate_write_path(path: Path) -> tuple[bool, str]:
    """Allow writes only within allowed directories."""
    resolved = path.resolve()
    for writable in WRITABLE_PATHS:
        writable_resolved = writable.resolve()
        try:
            resolved.relative_to(writable_resolved)
            return True, "ok"
        except ValueError:
            continue
    return False, f"path not in writable dirs: {path}"


def validate_read_path(path: Path) -> tuple[bool, str]:
    """Allow reading only from project directories or temp."""
    resolved = path.resolve()
    project_root = PROJECT.resolve()
    try:
        resolved.relative_to(project_root)
    except ValueError:
        tmp = Path(tempfile.gettempdir()).resolve()
        try:
            resolved.relative_to(tmp)
            return True, "ok"
        except ValueError:
            return False, f"path outside project: {path}"
    return True, "ok"


# ---------------------------------------------------------------------------
# Content sanitisation: context isolation
# ---------------------------------------------------------------------------

MAX_CONTENT_LENGTH = 8000


def sanitise_fetched_content(text: str, source_url: str) -> str:
    """Wrap fetched content to isolate it from the system prompt.

    Strips any occurrence of </fetched_content (case-insensitive) from the
    text before wrapping, so a malicious page cannot close the isolation tag.
    """
    text = re.sub(r"</fetched_content", "", text, flags=re.IGNORECASE)
    domain = url_to_host(source_url)
    text = text[:MAX_CONTENT_LENGTH]
    return (
        f'<fetched_content source="{domain}">\n'
        f"{text}\n"
        f"</fetched_content>"
    )


# ---------------------------------------------------------------------------
# Rate limiter
# ---------------------------------------------------------------------------

MAX_FETCHES_PER_BRIEF = 12
MIN_FETCH_DELAY_S = 1.0


class RateLimiter:
    """Per-brief fetch counter and per-domain timers."""

    def __init__(self, max_fetches=MAX_FETCHES_PER_BRIEF,
                 min_delay=MIN_FETCH_DELAY_S):
        self.max_fetches = max_fetches
        self.min_delay = min_delay
        self.count = 0
        self._last_fetch: dict[str, float] = {}

    def check(self, url: str) -> tuple[bool, str]:
        """Return (allowed, reason)."""
        self.count += 1
        if self.count > self.max_fetches:
            return False, f"fetch limit exceeded ({self.max_fetches})"
        return True, "ok"

    def check_consecutive(self, url: str, now: float) -> tuple[bool, str]:
        """Rate-limit to the same domain."""
        host = url_to_host(url)
        last = self._last_fetch.get(host, 0)
        elapsed = now - last
        if elapsed < self.min_delay:
            return False, f"rate limit ({self.min_delay}s) for {host}"
        self._last_fetch[host] = now
        return True, "ok"
