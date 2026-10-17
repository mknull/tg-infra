#!/usr/bin/env python3
"""System-level setup tests — run setup.sh in an isolated temp project."""

import os, sys
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import json
import os
import shutil
import shlex
import subprocess
import tempfile
import unittest
from pathlib import Path

_PROJECT = Path(__file__).resolve().parents[1]


class TestSetupScript(unittest.TestCase):
    """Run setup.sh in a temp directory and verify outputs."""

    @classmethod
    def setUpClass(cls):
        cls.tmpdir = Path(tempfile.mkdtemp(prefix="setup_test_"))
        cls.project = cls.tmpdir / "tg-infra"
        cls.project.mkdir(parents=True)
        cls.state = cls.project / "state"
        cls.home = cls.tmpdir / "home"
        cls.systemd = cls.home / ".config" / "systemd" / "user"
        cls.state.mkdir(parents=True)

        # Copy project files
        for name in ("setup.sh", "requirements.txt", ".gitignore",
                     "email-ingest-wrap", "outlook_auth.py",
                     "generate_profile.py",
                     "bot_commands.py", "it_jobs_poller.py", "rss_poller.py", "feeds.json.example",
                     "it_jobs_triage.py", "email_triage.py",
                     "weekly_trend.py", "weekly_recovery.py", "feedback_poller.py",
                     "setup_verify.py", "delivery_canary.py",
                     "agent.py", "tools.py", "guardrails.py",
                     "audit"):
            src = _PROJECT / name
            if src.exists():
                if src.is_dir():
                    shutil.copytree(src, cls.project / name)
                else:
                    shutil.copy2(src, cls.project / name)
        shutil.copytree(_PROJECT / "lib", cls.project / "lib")
        for name in ("it-jobs-criteria.md.example",
                      "email-triage-criteria.md.example"):
            src = _PROJECT / name
            if src.exists():
                shutil.copy2(src, cls.project / name)

        # Exercise generated config/units without installing packages, downloading
        # browsers, or touching real Docker/systemd services.
        commands = cls.tmpdir / "bin"
        commands.mkdir()
        for name, code in (("docker", 1), ("systemctl", 0)):
            stub = commands / name
            stub.write_text(f"#!/bin/sh\nexit {code}\n")
            stub.chmod(0o755)
        python = cls.project / "jobsmcp" / "bin" / "python3"
        python.parent.mkdir(parents=True)
        python.write_text(
            '#!/bin/bash\n'
            'if [[ "$1" == "-m" && ( "$2" == "pip" || "$2" == "playwright" ) ]]; then exit 0; fi\n'
            f'exec {shlex.quote(sys.executable)} "$@"\n')
        python.chmod(0o755)
        cls.env = {
            "HOME": str(cls.home),
            "XDG_CONFIG_HOME": str(cls.home / ".config"),
            "PATH": str(commands) + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        }
        result = subprocess.run(
            ["bash", str(cls.project / "setup.sh")],
            cwd=str(cls.project), capture_output=True, text=True,
            timeout=30, env=cls.env, input="n\nn\nn\n"
        )
        cls.exit_code = result.returncode
        cls.output = result.stdout + result.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmpdir, ignore_errors=True)

    def test_setup_exits_clean(self):
        """Setup completes without error."""
        self.assertEqual(self.exit_code, 0,
                         f"setup.sh failed:\n{self.output[-1000:]}")

    def test_creates_env_template(self):
        """.env contains all required keys."""
        env = (self.state / ".env").read_text()
        for key in ("TELEGRAM_BOT_TOKEN", "TELEGRAM_API_ID",
                     "DEEPSEEK_API_KEY", "USER_NAME",
                     "OUTLOOK_CLIENT_ID", "OUTLOOK_EMAIL", "SEARXNG_URL"):
            self.assertIn(key, env, f"missing {key} in .env template")

    def test_creates_channels_template(self):
        """channels.json is created with all required fields."""
        channels = json.loads((self.state / "channels.json").read_text())
        self.assertIn("channels", channels)
        ch = channels["channels"][0]
        for field in ("username", "queue_prefix", "description",
                       "message_format", "desired_roles", "acceptable_roles"):
            self.assertIn(field, ch)

    def test_creates_state_directories(self):
        """State subdirectories are created."""
        for subdir in ("message_queue", "messages", "audit"):
            self.assertTrue((self.state / subdir).is_dir(),
                            f"missing state/{subdir}")
        self.assertTrue((self.project / "workspace" / "briefs").is_dir())

    def test_idempotent_second_run(self):
        """Running setup twice produces no errors."""
        result = subprocess.run(
            ["bash", str(self.project / "setup.sh")],
            cwd=str(self.project), capture_output=True, text=True,
            timeout=30,
            env=self.env,
            input="n\nn\nn\n")
        self.assertEqual(result.returncode, 0)
        self.assertIn("SKIP", result.stdout)

    def test_creates_systemd_units(self):
        """Input workers and downstream service+timer pairs are created."""
        for name in ("bot-commands", "telegram-poll", "job-triage",
                      "rss-poll", "email-ingest", "weekly-trend"):
            self.assertTrue((self.systemd / f"{name}.service").exists(),
                            f"missing {name}.service")
            self.assertTrue((self.systemd / f"{name}.timer").exists(),
                            f"missing {name}.timer")

    def test_rss_preset_and_daily_timer(self):
        feeds = json.loads((self.state / "feeds.json").read_text())["feeds"]
        self.assertEqual(feeds[0]["id"], "fossjobs")
        self.assertEqual(feeds[0]["url"], "https://www.fossjobs.net/rss/all/")
        service = (self.systemd / "rss-poll.service").read_text()
        self.assertIn(str(self.project / "rss_poller.py"), service)
        self.assertIn("TimeoutStartSec=15min", service)
        self.assertIn("OnCalendar=*-*-* 06:10:00", (self.systemd / "rss-poll.timer").read_text())

    def test_email_ingest_service_uses_wrapper(self):
        """email-ingest.service runs the shell wrapper, not email_triage.py."""
        svc = (self.systemd / "email-ingest.service").read_text()
        self.assertIn("email-ingest-wrap", svc)
        self.assertNotIn("email_triage.py", svc)

    def test_systemd_units_have_no_hardcoded_paths(self):
        """Systemd units use PROJECT_DIR, not /home/filippos."""
        svc = (self.systemd / "bot-commands.service").read_text()
        self.assertNotIn("/home/filippos", svc)
        self.assertIn(str(self.project), svc)


if __name__ == "__main__":
    unittest.main(verbosity=2)
