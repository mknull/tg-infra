#!/usr/bin/env python3
"""setup-verify — confirm an install is actually configured and delivering.

Subcommands:
  status   report what is configured / missing / working (read-only)
  require  exit non-zero if required config is missing (setup gate)
  getme    print the bot's identity (catches wrong token / name confusion)
  pair     capture your chat id from the first message you send the bot
  verify   required config + bot + a real canary delivery
  inputs   authenticate/read selected inputs and check briefing dependencies
  telegram authenticate/read Telegram groups without collecting jobs

Pure validation lives in lib.onboarding (unit-tested). The live checks here
(getMe, getUpdates, a real canary delivery) run against provider APIs. Automated
tests substitute the external transports; installation uses the real services.
Verification and scheduled service activation are separate steps.
"""

import json
import sys
import urllib.error
import urllib.request

from lib import (load_env, STATE_DIR, missing_required,
                 channels_are_placeholder, deliver)
from lib.matching import matching_gaps
from lib.features import load as load_installation, enabled

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
        if ln.split("=", 1)[0].strip() == key:
            out.append(f"{key}={value}"); found = True
        else:
            out.append(ln)
    if not found:
        out.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(out) + "\n")


def cmd_require(env: dict) -> int:
    missing = missing_required(env, load_installation(STATE_DIR))
    if missing:
        print("Missing required config in state/.env: " + ", ".join(missing))
        return 1
    if enabled("telegram", STATE_DIR) and channels_are_placeholder(_load_channels()):
        print("state/channels.json still has the CHANGE_ME template — add your channels.")
        return 1
    gaps = matching_gaps(STATE_DIR.parent)
    if gaps:
        print("Matching setup incomplete: " + "; ".join(gaps))
        print("Run ./setup.sh, then Customize > Matching > Complete missing setup.")
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
        print("TELEGRAM_CHAT_ID not set — run: setup_verify.py pair")
        return 1
    print("sending a canary message to confirm delivery...")
    mid = deliver("canary", "\U0001f424 setup verify — if you can read this, delivery works.")
    if not mid:
        print("CANARY FAILED — delivery did not go through "
              "(see state/audit/delivery.jsonl)")
        return 1
    print(f"canary delivered (id {mid}) — delivery verified; service activation is a separate step.")
    return 0


def cmd_status(env: dict) -> int:
    print("== setup status ==")
    missing = missing_required(env, load_installation(STATE_DIR))
    print(f"required config : {'OK' if not missing else 'MISSING ' + ','.join(missing)}")
    ch = _load_channels()
    channel_status = ('placeholder/none' if channels_are_placeholder(ch) else str(len(ch)) + ' configured') if enabled("telegram", STATE_DIR) else 'disabled'
    print(f"channels       : {channel_status}")
    gaps = matching_gaps(STATE_DIR.parent)
    print("matching       : " + ("INCOMPLETE — " + "; ".join(gaps) if gaps else "ready"))
    folder = env.get("OUTLOOK_FOLDER", "").strip()
    print(f"email          : {'disabled' if not enabled('email', STATE_DIR) or not folder else 'folder=' + folder}")
    print(f"chat id        : {'set' if env.get('TELEGRAM_CHAT_ID', '').strip() else 'NOT paired'}")
    manifest = load_installation(STATE_DIR)
    if manifest:
        print("enabled features: " + ", ".join(k for k, v in manifest["features"].items() if v))
    if (STATE_DIR / "setup/pending.json").exists():
        print("setup proposal : saved, not applied — run ./setup.sh to resume")
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


def cmd_telegram(env: dict) -> int:
    """Authenticate/read access without collecting or consuming jobs."""
    if not enabled("telegram", STATE_DIR):
        return 0
    import asyncio
    from telethon import TelegramClient
    async def check():
        client = TelegramClient(str(STATE_DIR / "it-jobs-session"), int(env["TELEGRAM_API_ID"]), env["TELEGRAM_API_HASH"])
        try:
            await client.start()
            for channel in _load_channels():
                if channel.get("enabled", True):
                    await client.get_messages(channel["username"], limit=1)
                    print("Telegram read access verified: @" + channel["username"])
        finally:
            await client.disconnect()
    asyncio.run(check())
    return 0


def cmd_inputs(env: dict) -> int:
    """Read-only probes, owned by setup rather than audit."""
    manifest = load_installation(STATE_DIR)
    selected = manifest["features"] if manifest else {"rss": True, "telegram": True, "email": bool(env.get("OUTLOOK_FOLDER")), "weekly": bool(env.get("OUTLOOK_EMAIL")), "briefings": False}
    if selected["telegram"]:
        cmd_telegram(env)
    if selected["rss"]:
        from lib.sources import load_feeds, feed_subscriptions
        from rss_poller import fetch_feed, parse_feed, _parse_options
        for feed in load_feeds().values():
            if feed.get("enabled", True):
                for sub in feed_subscriptions(feed):
                    raw, response = fetch_feed(sub["url"], {})
                    if raw is None:
                        raise ValueError("RSS verification requires a complete response")
                    items = parse_feed(raw, response.get("response_url", sub["url"]), **_parse_options(sub))
                    print(f"RSS read verified: {feed['id']}/{sub['id']} ({len(items)} entries)")
    if selected["email"] or selected["weekly"]:
        from lib import ensure_valid_token, graph_get
        token = ensure_valid_token(env)
        if selected["email"]:
            from email_triage import _resolve_folder_id
            from lib.onboarding import resolve_monitored_folder
            folder = resolve_monitored_folder(env["OUTLOOK_FOLDER"], env.get("OUTLOOK_FOLDER_CONFIRM", ""))
            folder_id = _resolve_folder_id(token, folder)
            graph_get(f"/me/mailFolders/{folder_id}/messages", token, {"$top": "1", "$select": "id"})
            print("Outlook folder read verified: " + folder)
        if selected["weekly"]:
            graph_get("/me/mailFolders/sentitems/messages", token, {"$top": "1", "$select": "id"})
            print("Weekly-report mailbox read verified; first report delivery remains pending")
    if selected["briefings"]:
        from tools import web_search, md_to_pdf
        from playwright.sync_api import sync_playwright
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                browser.close()
        except Exception:
            print("Browser check failed. Install Chromium's system dependencies: jobsmcp/bin/python3 -m playwright install-deps chromium")
            return 1
        result = web_search("job vacancies", max_results=1)
        if result.startswith("Web search unavailable") or result == "no results found":
            raise ValueError("Search has no usable results; check SearXNG and its upstream engines")
        try:
            md_to_pdf("# Setup check\nPDF generation works.")
        except (ImportError, OSError):
            print("PDF check failed. Install WeasyPrint system dependencies; see docs/installation.md.")
            return 1
        print("Briefing browser, search and PDF verified")
    print("Input access verified. First job arrival, evaluation and delivery remain to be observed in audit.")
    return 0


def main() -> None:
    cmds = {"require": cmd_require, "getme": cmd_getme, "pair": cmd_pair,
            "verify": cmd_verify, "status": cmd_status, "telegram": cmd_telegram, "inputs": cmd_inputs}
    if len(sys.argv) < 2 or sys.argv[1] not in cmds:
        print("usage: setup_verify.py {" + "|".join(cmds) + "}")
        sys.exit(2)
    try:
        sys.exit(cmds[sys.argv[1]](load_env()))
    except Exception as exc:
        # Provider exceptions may contain token-bearing URLs. Never echo them.
        print(f"Verification failed ({type(exc).__name__}). Check the selected integration's credentials and connectivity.")
        sys.exit(1)


if __name__ == "__main__":
    main()
