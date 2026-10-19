"""Interactive installation using private proposals and an optional OS adapter."""

import getpass
import json
import os
import shlex
import subprocess
import sys
from pathlib import Path

from .config import DATA_DIR, PROJECT_DIR, load_env
from .features import FEATURES, load, proposed, required_credentials, validate
from .direction import direction_lock
from .matching import matching_gaps
from .state import write_cursor
from . import setup_session as session
from .setup_session import write_env

LABELS = {
    "rss": "RSS/Atom job feeds", "telegram": "Telegram groups as an INPUT (uses your user account)",
    "email": "Outlook job-email input", "briefings": "Researched /briefme reports (browser, PDF and search)",
    "weekly": "Weekly reports and email feedback (uses Outlook)",
    "canary": "Hourly Telegram delivery test messages",
}
FEATURE_HELP = {
    "rss": "Public RSS feeds need no source account. You will review feed choices and how much existing inventory to process.",
    "telegram": "If enabled: needs your Telegram user API ID/hash, phone/code/2FA login and access to source usernames; separate from bot delivery.",
    "email": "If enabled: needs an Outlook mailbox, an Entra app and a prepared job-alert folder. Email text is sent to DeepSeek for matching.",
    "briefings": "If enabled: needs PDF system libraries and SearXNG search. Local search needs Docker working without sudo and localhost:8080 available; alternatively set SEARXNG_URL in Customize > Edit advanced files. Start downloads Chromium and checks browser libraries.",
    "weekly": "If enabled: needs an Outlook mailbox and Entra app even without email input; sends paid model-generated reports to your email and reads replies.",
}
HINTS = {
    "TELEGRAM_BOT_TOKEN": "Create a dedicated bot with @BotFather /newbot.",
    "DEEPSEEK_API_KEY": "Create an API key and add credit at https://platform.deepseek.com.",
    "TELEGRAM_API_ID": "Obtain API ID and hash from https://my.telegram.org/apps.",
    "TELEGRAM_API_HASH": "The user API hash is separate from your bot token.",
    "OUTLOOK_CLIENT_ID": "Register an Entra app supporting your account; allow public client flows and delegated Mail.Read and Mail.Send (both requested even for input only). No client secret. Instructions: docs/installation.md, Optional features.",
    "OUTLOOK_EMAIL": "Your Outlook mailbox address; also the weekly-report recipient.",
    "OUTLOOK_FOLDER": "A dedicated mail folder's exact name. Your entire Inbox requires explicit confirmation in advanced configuration.",
}
SECRET_KEYS = {"TELEGRAM_BOT_TOKEN", "DEEPSEEK_API_KEY", "TELEGRAM_API_HASH"}


class Cancelled(Exception):
    pass


def ask(prompt: str, choices: str, default: str) -> str:
    while True:
        answer = input(prompt).strip().lower() or default
        if answer in choices and len(answer) == 1:
            return answer
        print("Choose: " + ", ".join(choices))


class Wizard:
    def __init__(self, root: Path = DATA_DIR, code: Path = PROJECT_DIR):
        self.root, self.code, self.proposal = root, code, None

    def child(self, script: str, *args: str) -> bool:
        result = subprocess.run([sys.executable, str(self.code / script), *args], cwd=self.code,
                                env={**os.environ, "TG_INFRA_DATA_DIR": str(self.proposal), "TG_SETUP_GUIDED": "1"})
        return result.returncode == 0

    def initialize(self) -> bool:
        saved = session.pending(self.root)
        if saved:
            action = ask("A saved proposal is waiting. [R]esume / [N]ew proposal / [C]ancel [R]: ", "rnc", "r")
            if action == "c":
                raise Cancelled()
            if action == "r":
                session.assert_current(self.root, saved)
                self.proposal = saved
                return True
            session.discard(self.root, saved)
        self.proposal = session.create(self.root)
        (self.proposal / "state/message_queue").mkdir()
        (self.proposal / "state/audit").mkdir()
        (self.proposal / "workspace/briefs").mkdir(parents=True)
        for name, default in (("channels.json", '{"channels": []}'), ("feeds.json", (self.code / "feeds.json.example").read_text())):
            path = self.proposal / "state" / name
            if not path.exists():
                write_cursor(path, default)
        manifest = proposed(self.root / "state", load_env(self.root / "state/.env"))
        write_cursor(self.proposal / "state/installation.json", json.dumps(manifest, indent=2))
        existing = (self.root / "state/installation.json").exists() or (self.root / "source/interests.txt").exists()
        if existing:
            print("Existing settings found. Keep makes no model calls. A legacy installation's inferred feature choices are reviewed before application.")
            self.show_features(manifest)
            action = ask("[K]eep existing / [C]hange features and credentials / [Q]uit [K]: ", "kcq", "k")
            if action == "q":
                raise Cancelled()
            if action == "k":
                return False
        self.configure()
        return False

    def show_features(self, manifest: dict) -> None:
        for key in FEATURES:
            print(f"  {'ON ' if manifest['features'][key] else 'OFF'} {LABELS[key]}")

    def configure(self) -> None:
        path = self.proposal / "state/installation.json"
        manifest = json.loads(path.read_text())
        print("\nChoose your inputs and optional features. Matched jobs are delivered to your Telegram bot chat.")
        while True:
            for key in FEATURES:
                if key in FEATURE_HELP:
                    print(FEATURE_HELP[key])
                default = "y" if manifest["features"][key] else "n"
                manifest["features"][key] = ask(f"{LABELS[key]}? [y/n] [{default.upper()}]: ", "yn", default) == "y"
            try:
                validate(manifest)
                break
            except ValueError as exc:
                print(exc)
        write_cursor(path, json.dumps(manifest, indent=2))
        self.credentials(manifest)
        if manifest["features"]["telegram"]:
            self.channels()

    def credentials(self, manifest: dict) -> None:
        path = self.proposal / "state/.env"
        before = load_env(path)
        values = dict(before)
        print("\nCredentials stay in private local files and are not printed in the review.")
        for key in (*required_credentials(manifest), "USER_NAME"):
            print(HINTS.get(key, "Your name, used in generated profiles."))
            prompt = key + (" [set; Enter to keep]: " if values.get(key) else ": ")
            reader = getpass.getpass if key in SECRET_KEYS and sys.stdin.isatty() else input
            answer = reader(prompt).strip()
            if answer:
                if any(c in answer for c in "\n\r\0"):
                    raise ValueError("Credential values must occupy one line")
                values[key] = answer
        values.setdefault("SEARXNG_URL", "http://localhost:8080/search")
        write_env(path, values)
        session.invalidate_auth(self.proposal, before, values)

    def channels(self) -> None:
        path = self.proposal / "state/channels.json"
        config = json.loads(path.read_text())
        if config["channels"]:
            print("Existing Telegram sources: " + ", ".join(c["username"] for c in config["channels"]))
            if ask("[K]eep / [E]dit the source list [K]: ", "ke", "k") == "k":
                return
        names = input("Telegram usernames, separated by commas (without @ or URLs): ").strip()
        old = {c["username"]: c for c in config["channels"]}
        channels = []
        for name in (part.strip().removeprefix("@") for part in names.split(",")):
            if name in old:
                channels.append(old[name])
                continue
            channels.append({"username": name, "queue_prefix": "jobs_" + name,
                             "description": input(f"What does @{name} publish? ").strip(),
                             "message_format": input("Typical language, layout and level of detail: ").strip(),
                             "desired_roles": "", "acceptable_roles": ""})
        write_cursor(path, json.dumps({**config, "channels": channels}, indent=2))

    def errors(self, *, ready=False) -> list[str]:
        try:
            return session.validate_configuration(self.proposal, ready=ready)
        except (OSError, ValueError, KeyError, TypeError) as exc:
            return ["Configuration is invalid: " + str(exc)]

    def matching(self) -> bool:
        from .setup_services import Systemd, check_briefing_dependencies
        try:
            manifest = load(self.proposal / "state")
            Systemd(self.code, self.root).preflight(manifest)
            if manifest["features"]["briefings"]:
                check_briefing_dependencies(load_env(self.proposal / "state/.env"))
        except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
            print("Prerequisites need attention before paid generation: " + str(exc))
            print("Use Customize to change features or edit advanced files, then Customize > Matching to retry; or Save for Later. No generation started.")
            return False
        print("\nPrepare matching in this private proposal. Applying the matching review here does not change the running installation.")
        return self.child("generate_profile.py", "--setup")

    def review(self) -> None:
        try:
            manifest = load(self.proposal / "state")
        except (ValueError, OSError):
            print("The proposal needs configuration corrections.")
            return
        print("\nReview installation")
        self.show_features(manifest)
        if manifest["features"]["rss"]:
            from .sources import load_feeds, feed_subscriptions
            for feed in load_feeds(self.proposal / "state/feeds.json").values():
                if feed.get("enabled", True):
                    print(feed.get("name", feed["id"]) + ": " + ", ".join(sub["url"] for sub in feed_subscriptions(feed)))
        if manifest["schedules"]:
            print("Custom schedules: " + json.dumps(manifest["schedules"]))
        if manifest["features"]["briefings"]:
            url = load_env(self.proposal / "state/.env").get("SEARXNG_URL", "http://localhost:8080/search")
            print("Start downloads Chromium and verifies browser, search and PDF support; missing browser system libraries may require OS package installation.")
            if url in ("http://localhost:8080/search", "http://127.0.0.1:8080/search"):
                print("It also starts/reuses or creates the searxng Docker container on localhost:8080. Downloads and the container remain if verification fails or briefings are later disabled.")
            else:
                print("Search uses your configured SearXNG endpoint; no local search container will be created.")
        print("Start verifies selected inputs and sends a real Telegram delivery test, then enables scheduled paid processing.")
        print("It installs user services/timers for the selected features. Keep this host awake/online; setup prints how to continue after logout.")
        print("Ongoing matching sends job text and profile/preferences to DeepSeek. There is no application-wide spending cap; source volume and backfill affect cost.")
        print("Save keeps this proposal separate; active settings and services continue unchanged.")
        print("Save or Quit cannot undo paid generation already performed. Saved credentials/profiles and processing history are local files; protect them and monitor disk usage.")

    def customize(self) -> None:
        action = ask("Customize [F]eatures/credentials / [M]atching / [E]dit advanced files / [B]ack [B]: ", "fmeb", "b")
        if action == "f":
            self.configure()
        elif action == "m":
            self.matching()
        elif action == "e":
            editable = [name for name in session.CONFIG_FILES if (self.proposal / name).exists()]
            print("Advanced controls: feeds.json includes custom URLs, selectors, cadence and backfill; installation.json includes schedules and canary settings.")
            for number, name in enumerate(editable, 1):
                print(f"  {number}. {name}")
            choice = input("File number (Enter to return): ").strip()
            if not choice:
                return
            try:
                index = int(choice) - 1
                if not 0 <= index < len(editable):
                    raise ValueError()
            except ValueError:
                print("Choose a listed file number.")
                return
            path = self.proposal / editable[index]
            before = path.read_text()
            old_env = load_env(path) if path.name == ".env" else None
            editor = os.environ.get("VISUAL") or os.environ.get("EDITOR") or "nano"
            try:
                subprocess.run([*shlex.split(editor), str(path)], check=True)
                if path.suffix == ".json":
                    json.loads(path.read_text())
                errors = self.errors()
                if errors:
                    raise ValueError("; ".join(errors))
            except (OSError, ValueError, subprocess.CalledProcessError) as exc:
                write_cursor(path, before)
                print("Edit rejected and previous proposal restored: " + str(exc))
            else:
                if path.name == ".env":
                    session.invalidate_auth(self.proposal, old_env, load_env(path))

    def start(self) -> bool:
        from .setup_services import Systemd, prepare_briefings
        manifest = load(self.proposal / "state")
        backend = Systemd(self.code, self.root)
        backend.preflight(manifest)
        selected = manifest["features"]
        env = load_env(self.proposal / "state/.env")
        if selected["briefings"]:
            prepare_briefings(self.code, self.root, env)
        session.assert_current(self.root, self.proposal)
        backend.capture()
        backup, activated, idle = None, False, False
        try:
            print("Pausing existing schedules for verification; failures restore their previous state.")
            backend.quiesce()
            idle = True
            # Authentication must not race a worker's session/token refresh or
            # compete with the bot command worker for pairing updates.
            session.sync_auth(self.root, self.proposal)
            if (selected["email"] or selected["weekly"]) and not (self.proposal / "state/outlook-token.json").exists():
                if not self.child("outlook_auth.py"):
                    return False
            if not env.get("TELEGRAM_CHAT_ID"):
                if not self.child("setup_verify.py", "pair"):
                    return False
            if not self.child("setup_verify.py", "inputs") or not self.child("setup_verify.py", "verify"):
                return False
            with direction_lock():
                session.assert_current(self.root, self.proposal)
                backend.install(manifest)
                backup = session.apply(self.root, self.proposal)
                backend.activate(manifest)
                activated = True
        finally:
            if not activated:
                def restore_config():
                    with direction_lock():
                        if backup is not None:
                            session.restore(backup)
                        if idle:
                            session.sync_auth(self.root, self.proposal, to_active=True)
                errors = backend.restore(restore_config)
                if errors:
                    raise RuntimeError("Service rollback needs attention:\n" + "\n".join(errors))
        (self.root / "state/setup/pending.json").unlink(missing_ok=True)
        session.discard(self.root, self.proposal)
        self.proposal = None
        print("Setup complete — selected timers are active. Input access and Telegram delivery verified; inspect audit after the first job is processed.")
        print("Status: jobsmcp/bin/python3 setup_verify.py status")
        print("For operation after logout/at boot: sudo loginctl enable-linger " + getpass.getuser())
        return True

    def run(self) -> int:
        print("Job search setup: your documents and current preferences configure the matching models and feed choices.")
        resumed = self.initialize()
        while self.errors():
            print("\n".join(self.errors()))
            if ask("[C]orrect configuration / [Q]uit [C]: ", "cq", "c") == "q":
                raise Cancelled()
            self.configure()
        if not resumed and matching_gaps(self.proposal):
            self.matching()
        while True:
            self.review()
            errors = self.errors(ready=True)
            if errors:
                print("Still needed: " + "; ".join(errors))
            action = ask("[S]tart / [C]ustomize / Save for [L]ater / [Q]uit [L]: ", "sclq", "l")
            if action == "q":
                raise Cancelled()
            if action == "c":
                self.customize()
            elif action == "l":
                session.save_pending(self.root, self.proposal)
                print(f"Proposal saved privately at {self.proposal}. Nothing activated or applied. Re-run ./setup.sh to resume.")
                return 0
            elif not errors:
                try:
                    if self.start():
                        return 0
                    print("Verification incomplete. Active settings were not changed; correct credentials or retry Start.")
                except (OSError, ValueError, RuntimeError, subprocess.SubprocessError) as exc:
                    print("Start failed: " + str(exc))


def run() -> int:
    wizard = Wizard()
    try:
        return wizard.run()
    except (Cancelled, EOFError, KeyboardInterrupt):
        print("\nSetup cancelled. Active configuration and services were not changed.")
        return 1
    except (OSError, ValueError, KeyError, TypeError) as exc:
        print("Setup could not continue: " + str(exc))
        return 1
    finally:
        if wizard.proposal is not None and session.pending(wizard.root) != wizard.proposal:
            session.discard(wizard.root, wizard.proposal)
