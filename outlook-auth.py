#!/usr/bin/env python3
"""One-time Outlook OAuth — device code flow for initial token acquisition."""

import json
import sys
import time
import urllib.parse
import urllib.request

from lib import load_env, save_token, TOKEN_ENDPOINT

SCOPES = "offline_access https://graph.microsoft.com/Mail.Read https://graph.microsoft.com/Mail.Send"


def main() -> None:
    env = load_env()
    client_id = env.get("OUTLOOK_CLIENT_ID", "")
    if not client_id:
        print("Set OUTLOOK_CLIENT_ID in state/.env first.")
        sys.exit(1)

    # Step 1: request device code
    device_req = urllib.request.Request(
        "https://login.microsoftonline.com/common/oauth2/v2.0/devicecode",
        data=urllib.parse.urlencode({
            "client_id": client_id,
            "scope": SCOPES,
        }).encode(),
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urllib.request.urlopen(device_req, timeout=15) as resp:
        device = json.loads(resp.read())

    print(f"\nVisit:  {device['verification_uri']}")
    print(f"Code:   {device['user_code']}")
    print(f"Expires in {device['expires_in']} seconds.\n")
    print("Waiting for you to enter the code...")

    # Step 2: poll for token
    interval = device.get("interval", 5)
    deadline = time.time() + device["expires_in"]
    while time.time() < deadline:
        time.sleep(interval)
        poll_req = urllib.request.Request(
            TOKEN_ENDPOINT,
            data=urllib.parse.urlencode({
                "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                "client_id": client_id,
                "device_code": device["device_code"],
            }).encode(),
            headers={"Content-Type": "application/x-www-form-urlencoded"},
            method="POST",
        )
        with urllib.request.urlopen(poll_req, timeout=15) as resp:
            data = json.loads(resp.read())

        if "access_token" in data:
            save_token({
                "access_token": data["access_token"],
                "refresh_token": data["refresh_token"],
                "expires_at": int((time.time() + data["expires_in"]) * 1000),
            })
            print("Token saved to state/outlook-token.json")
            return
        elif data.get("error") == "authorization_pending":
            continue
        elif data.get("error") == "authorization_declined":
            print("Authorization was declined. Re-run outlook-auth.py to try again.")
            sys.exit(1)
        else:
            print(f"Unexpected response: {data}")
            sys.exit(1)

    print("Timed out waiting for authorization. Re-run outlook-auth.py when ready.")
    sys.exit(1)


if __name__ == "__main__":
    main()
