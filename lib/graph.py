"""Microsoft Graph API client — shared auth header and error handling.

GET and POST against Graph used to live in three places (``email_triage.py``'s
``graph_get``, ``lib/delivery.py``'s ``graph_post``, and raw inline requests in
``feedback_poller.py``), each re-deriving the bearer header and the
``GRAPH_BASE + path`` URL. This consolidates the *mechanism* — header, URL
assembly, JSON (de)serialisation, empty-body handling — in one place. Callers
still own which endpoints they hit and what they do with the result.
"""

import json
import urllib.parse
import urllib.request

from .config import GRAPH_BASE


def _auth_header(access_token: str) -> dict:
    return {"Authorization": f"Bearer {access_token}"}


def graph_get(path: str, access_token: str, params: dict | None = None,
              timeout: int = 30) -> dict:
    """GET a Graph resource. ``path`` is appended to GRAPH_BASE.

    ``params`` are url-encoded onto the query string when provided.
    """
    url = GRAPH_BASE + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers=_auth_header(access_token),
                                 method="GET")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}


def graph_post(path: str, access_token: str, data: dict,
               timeout: int = 30) -> dict:
    """POST JSON to a Graph resource. Returns the parsed response (or {})."""
    url = GRAPH_BASE + path
    payload = json.dumps(data).encode()
    headers = _auth_header(access_token)
    headers["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=payload, headers=headers,
                                 method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        body = resp.read()
    return json.loads(body) if body else {}
