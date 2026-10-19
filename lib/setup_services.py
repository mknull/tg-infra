"""Linux activation adapter. Configuration generation does not use systemd."""

import hashlib
import os
import secrets
import subprocess
import time
from pathlib import Path

from .features import SERVICES, SCHEDULES, selected_services

DESCRIPTIONS = {
    "bot-commands": "Process Telegram bot commands", "telegram-poll": "Poll Telegram groups",
    "rss-poll": "Poll RSS and Atom job feeds", "job-triage": "Run triage on queued messages",
    "email-ingest": "Fetch and triage Outlook emails", "weekly-trend": "Weekly market trend report",
    "weekly-recovery": "Retry the weekly report until confirmed sent (runs 5 min before email-ingest)",
    "feedback-poller": "Poll Outlook for replies to weekly reports",
    "delivery-canary": "Synthetic delivery check (audit --health alerts if it breaks)",
}
SCRIPTS = dict(zip(SERVICES, ("bot_commands.py", "it_jobs_poller.py", "rss_poller.py",
                            "it_jobs_triage.py", "email-ingest-wrap", "weekly_trend.py",
                            "weekly_recovery.py", "feedback_poller.py", "delivery_canary.py")))
MARKER = "# Managed by tg-infra setup v1 sha256="


def managed(content: str) -> str:
    return MARKER + hashlib.sha256(content.encode()).hexdigest() + "\n" + content


def unmodified(content: str) -> bool:
    header, _, body = content.partition("\n")
    return header == MARKER + hashlib.sha256(body.encode()).hexdigest()


def quote(value: str | Path, *, command: bool = False) -> str:
    value = str(value).replace('\\', '\\\\').replace('"', '\\"').replace('%', '%%')
    return '"' + (value.replace('$', '$$') if command else value) + '"'


class Systemd:
    def __init__(self, code: Path, data: Path, *, prefix: str = ""):
        if any(c in str(path) for path in (code, data) for c in "\n\r\0"):
            raise ValueError("Installation paths must not contain line breaks")
        self.code, self.data, self.prefix = code, data, prefix
        self.directory = Path(os.environ.get("XDG_CONFIG_HOME", Path.home() / ".config")) / "systemd/user"
        self.files, self.before = {}, {}

    def ctl(self, *args: str, check: bool = True):
        result = subprocess.run(["systemctl", "--user", *args], capture_output=True, text=True, timeout=35)
        if check and result.returncode:
            raise RuntimeError("systemctl " + " ".join(args) + " failed: " + result.stderr.strip())
        return result

    def unit(self, name: str, suffix: str = "timer") -> str:
        return self.prefix + name + "." + suffix

    def preflight(self, manifest: dict) -> None:
        try:
            self.ctl("show-environment")
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Scheduling needs Linux with a working user systemd manager. Run setup as your normal user; check `systemctl --user show-environment` in that login session.") from exc
        for schedule in {**SCHEDULES, **manifest["schedules"]}.values():
            try:
                result = subprocess.run(["systemd-analyze", "calendar", schedule], capture_output=True, text=True, timeout=10)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise ValueError("Schedule validation needs a working `systemd-analyze` command; check your systemd installation.") from exc
            if result.returncode:
                raise ValueError("Invalid schedule: " + schedule)

    def capture(self) -> None:
        for name in SERVICES:
            unit = self.unit(name)
            self.before[name] = {
                "enabled": self.ctl("is-enabled", unit, check=False).stdout.strip() == "enabled",
                "active": self.ctl("is-active", unit, check=False).stdout.strip() == "active",
            }
            for suffix in ("timer", "service"):
                path = self.directory / self.unit(name, suffix)
                if path.is_symlink():
                    raise ValueError(f"{path} is masked or linked; review it before running setup")
                self.files[path] = path.read_bytes() if path.exists() else None

    def quiesce(self) -> None:
        for name in SERVICES:
            if self.ctl("is-active", self.unit(name), check=False).stdout.strip() in ("active", "activating"):
                self.ctl("stop", self.unit(name))
        # Do not kill a worker halfway through evaluation/delivery. A busy
        # installation may defer setup, retaining its original configuration.
        deadline = time.monotonic() + 30
        while True:
            busy = [name for name in SERVICES if self.ctl("is-active", self.unit(name, "service"), check=False).stdout.strip()
                    in ("active", "activating", "deactivating")]
            if not busy:
                return
            if time.monotonic() >= deadline:
                raise RuntimeError("Workers are still busy; retry later: " + ", ".join(busy))
            time.sleep(1)

    def render(self, name: str, manifest: dict, *, legacy: bool = False) -> tuple[str, str]:
        script = self.code / SCRIPTS[name]
        python = self.code / "jobsmcp/bin/python3"
        command = str(script) if name == "email-ingest" else f"{python} {script}"
        if not legacy:
            # systemd expands dollars in arguments, but not the executable path.
            command = quote(script) if name == "email-ingest" else f"{quote(python)} {quote(script, command=True)}"
        if name == "rss-poll":
            command += " --due"
        timeout = "TimeoutStartSec=15min" if name in ("telegram-poll", "rss-poll") else ""
        working = str(self.code) if legacy else str(self.data).replace('%', '%%')
        environment = "" if legacy else "Environment=" + quote("TG_INFRA_DATA_DIR=" + str(self.data)) + "\n"
        log = self.data / "state" / (name + ".log")
        output = "append:" + (str(log) if legacy else str(log).replace('%', '%%'))
        service = (f"[Unit]\nDescription={DESCRIPTIONS[name]}\nAfter=network-online.target\n\n"
                   f"[Service]\nType=oneshot\nWorkingDirectory={working}\n{environment}ExecStart={command}\n"
                   f"{timeout}\nStandardOutput={output}\nStandardError={output}\n")
        schedule = {**SCHEDULES, **manifest["schedules"]}[name]
        timer = (f"[Unit]\nDescription=Run {name} every interval\n\n[Timer]\nOnCalendar={schedule}\n"
                 "Persistent=true\n\n[Install]\nWantedBy=timers.target\n")
        return service, timer

    def install(self, manifest: dict) -> None:
        self.directory.mkdir(parents=True, exist_ok=True)
        (self.data / "state").mkdir(parents=True, exist_ok=True)
        for name in SERVICES:
            rendered = self.render(name, manifest)
            default_manifest = {**manifest, "schedules": {}}
            old = self.render(name, default_manifest, legacy=True)
            for suffix, content, legacy in zip(("service", "timer"), rendered, old):
                path = self.directory / self.unit(name, suffix)
                existing = path.read_text() if path.exists() else None
                compatible = {legacy}
                if name == "rss-poll":
                    compatible.add(legacy.replace("rss_poller.py --due", "rss_poller.py").replace("OnCalendar=*-*-* *:10:00", "OnCalendar=*-*-* 06:10:00"))
                if existing and suffix == "service":
                    script = str(self.code / SCRIPTS[name])
                    environment = "Environment=" + quote("TG_INFRA_DATA_DIR=" + str(self.data))
                    if (script not in existing and quote(script) not in existing and quote(script, command=True) not in existing) or (
                            "TG_INFRA_DATA_DIR=" in existing and environment not in existing):
                        raise ValueError(f"{path} belongs to another installation; use a separate Linux user")
                if existing and existing not in compatible and not unmodified(existing):
                    print(f"Custom unit preserved: {path}")
                    if name == "rss-poll":
                        print("  Check that ExecStart runs rss_poller.py --due and OnCalendar polls at least hourly.")
                    if suffix == "timer" and name in manifest["schedules"] and f"OnCalendar={manifest['schedules'][name]}\n" not in existing:
                        raise ValueError(f"Custom {path.name} conflicts with the proposed schedule; edit it or remove the override")
                    continue
                if existing and existing != managed(content):
                    backup = path.with_suffix(path.suffix + ".pre-setup")
                    if not backup.exists():
                        backup.write_text(existing)
                path.write_text(managed(content))
        self.ctl("daemon-reload")

    def activate(self, manifest: dict) -> None:
        wanted = selected_services(manifest)
        for name in SERVICES:
            self.ctl("enable" if name in wanted else "disable", self.unit(name))
        for name in SERVICES:
            if name in wanted:
                self.ctl("start", self.unit(name))
                if self.ctl("is-active", self.unit(name), check=False).stdout.strip() != "active":
                    raise RuntimeError(self.unit(name) + " did not become active")

    def restore(self, restore_config=None) -> list[str]:
        errors = []
        try:
            self.quiesce()
        except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
            return ["Could not safely roll back while workers are running: " + str(exc)]
        if restore_config is not None:
            restore_config()
        for name in self.before:
            try:
                # Remove enablement links while newly created units still exist.
                if (self.directory / self.unit(name)).exists():
                    self.ctl("disable", self.unit(name))
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
        for path, content in self.files.items():
            try:
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_bytes(content)
            except OSError as exc:
                errors.append(str(exc))
        self.ctl("daemon-reload", check=False)
        for name, status in self.before.items():
            if self.files.get(self.directory / self.unit(name)) is None:
                continue
            try:
                self.ctl("enable" if status["enabled"] else "disable", self.unit(name))
                if status["active"]:
                    self.ctl("start", self.unit(name))
            except (RuntimeError, OSError, subprocess.TimeoutExpired) as exc:
                errors.append(str(exc))
        return errors


def check_briefing_dependencies(env: dict) -> None:
    """Read-only local checks before generation or optional downloads."""
    url = env.get("SEARXNG_URL", "http://localhost:8080/search")
    if url in ("http://localhost:8080/search", "http://127.0.0.1:8080/search"):
        try:
            subprocess.run(["docker", "ps"], check=True, capture_output=True, timeout=10)
        except (OSError, subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
            raise ValueError("Briefings need accessible Docker or a custom SEARXNG_URL. Install/start Docker and make `docker ps` work without sudo, or set a JSON-enabled /search endpoint in Customize > Edit advanced files > state/.env. You can also disable briefings.") from exc
    try:
        from weasyprint import HTML
    except (ImportError, OSError) as exc:
        raise ValueError("Briefing PDF libraries are missing. Ubuntu/Debian: sudo apt-get install libpango-1.0-0 libpangoft2-1.0-0. See docs/installation.md, or disable briefings.") from exc


def prepare_briefings(code: Path, data: Path, env: dict) -> None:
    """Optional local dependencies, run only after Start is selected."""
    check_briefing_dependencies(env)
    subprocess.run([str(code / "jobsmcp/bin/python3"), "-m", "playwright", "install", "chromium"], check=True)
    url = env.get("SEARXNG_URL", "http://localhost:8080/search")
    if url not in ("http://localhost:8080/search", "http://127.0.0.1:8080/search"):
        return
    exists = subprocess.run(["docker", "container", "inspect", "searxng"], capture_output=True)
    if exists.returncode == 0:
        subprocess.run(["docker", "start", "searxng"], check=True, capture_output=True)
        return
    directory = data / "state/searxng"
    directory.mkdir(parents=True, exist_ok=True)
    settings = directory / "settings.yml"
    settings.write_text("use_default_settings: true\nserver:\n  secret_key: " + secrets.token_hex(32) +
                        "\n  bind_address: '0.0.0.0'\n  limiter: false\n  public_instance: false\nsearch:\n  formats: [html, json]\n")
    subprocess.run(["docker", "run", "-d", "--name", "searxng", "--restart", "unless-stopped",
                    "-p", "127.0.0.1:8080:8080", "-v", f"{settings}:/etc/searxng/settings.yml:ro", "searxng/searxng"], check=True)
