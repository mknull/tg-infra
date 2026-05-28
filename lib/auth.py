"""Outlook OAuth token management — load, save, refresh."""

import json
import logging
import os
import time
import urllib.parse
import urllib.request

from .config import TOKEN_ENDPOINT, TOKEN_FILE, TOKEN_REFRESH_BUFFER_S


def load_token() -> dict:
    return json.loads(TOKEN_FILE.read_text())


def save_token(token: dict) -> None:
    """Atomic write so a crash never leaves a partial token file."""
    tmp = TOKEN_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(token))
    tmp.chmod(0o600)
    os.replace(tmp, TOKEN_FILE)


def ensure_valid_token(env: dict) -> str:
    """Return a valid access token, refreshing if within BUFFER seconds of expiry."""
    token = load_token()
    expires_at_s = token["expires_at"] / 1000
    if time.time() < expires_at_s - TOKEN_REFRESH_BUFFER_S:
        return token["access_token"]

    logging.info("Token expiring soon — refreshing.")
    client_id = env.get("OUTLOOK_CLIENT_ID", "")
    if not client_id:
        raise RuntimeError("OUTLOOK_CLIENT_ID not set in .env")

    body = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": client_id,
        "refresh_token": token["refresh_token"],
        "scope": "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send",
    }).encode()
    req = urllib.request.Request(
        TOKEN_ENDPOINT,
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())

    if "error" in data:
        raise RuntimeError(f"Token refresh failed: {data}")

    new_token = {
        "access_token": data["access_token"],
        "refresh_token": data.get("refresh_token", token["refresh_token"]),
        "expires_at": int((time.time() + data["expires_in"]) * 1000),
    }
    save_token(new_token)
    logging.info("Token refreshed.")
    return new_token["access_token"]
