"""DeepSeek API calls and JSON extraction."""

import json
import socket
import urllib.error
import urllib.request
from urllib.parse import urlparse

from .config import DEEPSEEK_API_URL


class TransportError(Exception):
    """The model endpoint could not be reached, or returned a transient server
    error — the request was never genuinely evaluated. Retryable, never terminal:
    callers must defer rather than seal a message on this. Distinct from a model
    reply that is merely unusable (bad JSON), which is an application fault."""


def endpoint_reachable(timeout: float = 5.0) -> bool:
    """True if the model host accepts a TCP connection — DNS resolution, routing,
    and connect, which is the exact chain that flakes on an unreliable host. Spends
    no tokens. A caller confirms this once per run, then makes a single call per
    message rather than retrying into double-evaluations."""
    u = urlparse(DEEPSEEK_API_URL)
    port = u.port or (443 if u.scheme == "https" else 80)
    try:
        with socket.create_connection((u.hostname, port), timeout=timeout):
            return True
    except OSError:
        return False


def call_deepseek(model: str, prompt: str, api_key: str,
                  timeout: int = 90) -> str:
    payload = json.dumps({
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
    }).encode()
    req = urllib.request.Request(
        DEEPSEEK_API_URL, data=payload,
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = json.loads(resp.read())
    except urllib.error.HTTPError as e:
        # 5xx/429 is the endpoint failing transiently; 4xx is our request and is
        # not retryable, so let it surface as itself.
        if e.code >= 500 or e.code == 429:
            raise TransportError(f"HTTP {e.code}: {e.reason}") from e
        raise
    except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
        # DNS failure, connection refused/reset, read timeout.
        raise TransportError(str(e)) from e
    return body["choices"][0]["message"]["content"].strip()


def extract_json(text: str) -> dict:
    start = text.find("{")
    end = text.rfind("}") + 1
    if start == -1 or end == 0:
        raise ValueError(f"No JSON in: {text[:200]}")
    return json.loads(text[start:end])
