#!/usr/bin/env python3
"""Tools for the /briefme agent — web_fetch, web_search, read_file, pdf_writer."""

import json
import re
import time
import urllib.request
import urllib.parse
from pathlib import Path

from guardrails import (validate_fetch_url, validate_search_query,
                         validate_read_path, sanitise_fetched_content,
                         RateLimiter, BRIEFS_DIR, MAX_CONTENT_LENGTH)

# ---------------------------------------------------------------------------
# read_file
# ---------------------------------------------------------------------------

def read_file(path: Path | str) -> str:
    """Read a file from the project directory. Guardrailed to project/tmp only."""
    path = Path(path)
    ok, reason = validate_read_path(path)
    if not ok:
        raise PermissionError(f"read blocked: {reason}")
    if not path.exists():
        raise FileNotFoundError(f"not found: {path}")
    text = path.read_text()
    if len(text) > MAX_CONTENT_LENGTH:
        text = text[:MAX_CONTENT_LENGTH] + "\n\n[truncated]"
    return text


# ---------------------------------------------------------------------------
# web_search — SearXNG (self-hosted)
# ---------------------------------------------------------------------------

SEARXNG_URL = "http://localhost:8080/search"


def web_search(query: str, max_results: int = 5) -> str:
    """Search the web via self-hosted SearXNG. Returns JSON string of results."""
    ok, reason = validate_search_query(query)
    if not ok:
        return f"search blocked: {reason}"

    params = {"q": query, "format": "json", "categories": "general",
              "language": "en", "safesearch": "1"}
    url = SEARXNG_URL + "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "briefme/1.0"})
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read())
    except Exception as e:
        return f"search error: {e}"

    results = data.get("results", [])[:max_results]
    if not results:
        return "no results found"

    lines = []
    for r in results:
        title = r.get("title", "?")
        snippet = r.get("snippet", "") or r.get("content", "")
        url_r = r.get("url", "")
        lines.append(f"- {title}\n  {snippet[:300]}\n  {url_r}")
    return "\n\n".join(lines)


# ---------------------------------------------------------------------------
# web_fetch — Playwright (browser-grade TLS)
# ---------------------------------------------------------------------------

def web_fetch(url: str, flash_fn=None, rate_limiter: RateLimiter | None = None) -> str:
    """Fetch a URL with guardrails: validate, rate-limit, then fetch via Playwright.

    flash_fn(model, prompt, api_key) → str is the stage-2 classifier.
    rate_limiter tracks per-brief fetch limits.
    """
    # Guardrails
    ok, reason = validate_fetch_url(url, flash_fn=flash_fn)
    if not ok:
        return f"fetch blocked: {reason}"

    if rate_limiter:
        ok, reason = rate_limiter.check(url)
        if not ok:
            return f"fetch blocked: {reason}"
        ok, reason = rate_limiter.check_consecutive(url, time.monotonic())
        if not ok:
            return f"fetch blocked: {reason}"

    # Fetch via Playwright
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            page = browser.new_page()
            page.goto(url, timeout=15000, wait_until="domcontentloaded")
            text = page.inner_text("body")
            # Strip excessive whitespace
            text = re.sub(r"\n\s*\n", "\n\n", text)
            text = re.sub(r"[ \t]{3,}", "  ", text)
            browser.close()
    except ImportError:
        return "fetch error: playwright not installed"
    except Exception as e:
        return f"fetch error: {e}"

    return sanitise_fetched_content(text, url)


# ---------------------------------------------------------------------------
# pdf_writer — converts markdown brief to PDF
# ---------------------------------------------------------------------------

def md_to_pdf(md_path: Path) -> Path:
    """Convert a markdown brief to PDF. Returns path to the PDF file."""
    import markdown
    from weasyprint import HTML

    md_text = md_path.read_text()
    html_body = markdown.markdown(md_text, extensions=["extra", "sane_lists"])

    html = f"""<!DOCTYPE html>
<html lang="en">
<head><meta charset="utf-8"><title>Brief</title>
<style>
  body {{ font-family: Georgia, serif; max-width: 42em; margin: 2em auto;
         line-height: 1.6; color: #222; }}
  h2 {{ border-bottom: 1px solid #ccc; padding-bottom: 0.3em; }}
  h3 {{ margin-top: 1.5em; }}
  code {{ background: #f4f4f4; padding: 0.1em 0.3em; font-size: 0.92em; }}
  blockquote {{ border-left: 3px solid #999; margin-left: 0; padding-left: 1em;
                color: #555; }}
</style></head>
<body>{html_body}</body>
</html>"""

    pdf_path = md_path.with_suffix(".pdf")
    HTML(string=html).write_pdf(str(pdf_path))
    return pdf_path
