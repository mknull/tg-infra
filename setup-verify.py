#!/usr/bin/env python3
"""setup-verify — confirm an install is actually configured and delivering.

Subcommands:
  status   report what is configured / missing / working (read-only)
  require  exit non-zero if required config is missing (setup gate)
  getme    print the bot's identity (catches wrong token / name confusion)
  pair     capture your chat id from the first message you send the bot
  verify   the activation gate: required config + bot + a real canary delivery

Pure validation lives in lib.onboarding (unit-tested). The live checks here
(getMe, getUpdates, a real canary delivery) run against real services — they
are deliberately not faked. "Set up" means verify is green, not that files exist.
"""

import json
import sys
import urllib.error
import urllib.request

from lib import (load_env, STATE_DIR, missing_required,
                 channels_are_placeholder, deliver)

ENV_FILE = STATE_DIR / ".env"


def _tg(token: str, method: str, params: dict | None = None) -> dict:
    url = f"https://api.telegram.org/bot{token}/{method}"
    data = json.dumps(params).encode() if params else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"} if data else {},
        method="POST" if data else "GET")
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return json.loads(e.read())


def _load_channels() -> list:
    try:
        return json.loads((STATE_DIR / "channels.json").read_text())["channels"]
    except Exception:
        return []


def _set_env(key: str, value: str) -> None:
    lines = ENV_FILE.read_text().splitlines() if ENV_FILE.exists() else []
    out, found = [], False
    for ln in lines:
        if ln.lstrip().startswith(key):
            out.append(f"{key}={value}"); found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n")


def cmd_require(env: dict) -> int:
    missing = missing_required(env)
    if missing:
        print("Missing required config in state/.env: " + ", ".join(missing))
        return 1
    if channels_are_placeholder(_load_channels()):
        print("state/channels.json still has the CHANGE_ME template — add your channels.")
        return 1
    print("required config: OK")
    return 0


def cmd_getme(env: dict) -> int:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set"); return 1
    me = _tg(token, "getMe")
    if not me.get("ok"):
        print(f"bot token invalid: {me.get('description')}"); return 1
    r = me["result"]
    print(f"bot: @{r['username']} (\"{r.get('first_name')}\", id {r['id']})")
    print("  ^ start a chat with THIS @username — not a similarly-named bot")
    return 0


def cmd_pair(env: dict) -> int:
    token = env.get("TELEGRAM_BOT_TOKEN", "")
    if not token:
        print("TELEGRAM_BOT_TOKEN not set"); return 1
    me = _tg(token, "getMe")
    if not me.get("ok"):
        print(f"bot token invalid: {me.get('description')}"); return 1
    print(f"Open Telegram, find @{me['result']['username']}, and send it any message.")
    input("Press Enter once you've sent it... ")
    updates = _tg(token, "getUpdates", {"offset": -1, "timeout": 5})
    if not updates.get("ok"):
        print(f"getUpdates failed: {updates.get('description')} — stop the "
              "bot-commands timer if it's running, then retry.")
        return 1
    chat = None
    for u in updates.get("result", []):
        m = u.get("message") or u.get("edited_message") or {}
        c = m.get("chat", {})
        if c.get("type") == "private":
            chat = c
    if not chat:
        print("no message seen — another poller may have consumed it. Send the "
              "bot a fresh message and run pair again.")
        return 1
    _set_env("TELEGRAM_CHAT_ID", str(chat["id"]))
    print(f"paired: TELEGRAM_CHAT_ID={chat['id']} (@{chat.get('username')}) written to .env")
    return 0


def cmd_verify(env: dict) -> int:
    if cmd_require(env):
        return 1
    if cmd_getme(env):
        return 1
    if not env.get("TELEGRAM_CHAT_ID", "").strip():
        print("TELEGRAM_CHAT_ID not set — run: setup-verify.py pair")
        return 1
    print("sending a canary message to confirm delivery...")
    mid = deliver("canary", "\U0001f424 setup verify — if you can read this, delivery works.")
    if not mid:
        print("CANARY FAILED — delivery did not go through "
              "(see state/audit/delivery.jsonl)")
        return 1
    print(f"canary delivered (id {mid}) — install is live.")
    return 0


def cmd_status(env: dict) -> int:
    print("== setup status ==")
    missing = missing_required(env)
    print(f"required config : {'OK' if not missing else 'MISSING ' + ','.join(missing)}")
    ch = _load_channels()
    print(f"channels       : {'placeholder/none' if channels_are_placeholder(ch) else str(len(ch)) + ' configured'}")
    folder = env.get("OUTLOOK_FOLDER", "").strip()
    print(f"email          : {'disabled (OUTLOOK_FOLDER unset)' if not folder else 'folder=' + folder}")
    print(f"chat id        : {'set' if env.get('TELEGRAM_CHAT_ID', '').strip() else 'NOT paired'}")
    cs = STATE_DIR / "canary-status.json"
    if cs.exists():
        try:
            c = json.loads(cs.read_text())
            print(f"canary         : {'OK' if c.get('ok') else 'FAILED'} ({c.get('at')})")
        except Exception:
            print("canary         : unreadable")
    else:
        print("canary         : never run")
    return 0


def main() -> None:
    cmds = {"require": cmd_require, "getme": cmd_getme, "pair": cmd_pair,
            "verify": cmd_verify, "status": cmd_status}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print("usage: setup-verify.py {" + "|".join(cmds) + "}")
        sys.exit(2)
    sys.exit(cmds[sys.argv[1]](load_env()))


if __name__ == "__main__":
    main()
