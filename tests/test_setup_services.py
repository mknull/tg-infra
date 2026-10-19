"""Unit preservation, rollback, and opt-in checks against a real user manager."""

import os
import sys
import tempfile
import unittest
import uuid
from pathlib import Path
from unittest.mock import patch

from lib.features import DEFAULTS, SERVICES, selected_services
from lib.setup_services import Systemd


def manifest(**features):
    return {"version": 1, "features": {**DEFAULTS, "canary": False, **features}, "schedules": {}}


class ServiceFiles(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.backend = Systemd(self.root / "code", self.root / "data")
        self.backend.directory = self.root / "units"
        self.backend.directory.mkdir()

    def test_old_daily_rss_units_migrate_and_custom_units_survive(self):
        backend = self.backend
        old_service, old_timer = backend.render("rss-poll", manifest(), legacy=True)
        old_service = old_service.replace("rss_poller.py --due", "rss_poller.py")
        old_timer = old_timer.replace("*:10:00", "06:10:00")
        service, timer = [backend.directory / ("rss-poll." + suffix) for suffix in ("service", "timer")]
        service.write_text(old_service)
        timer.write_text(old_timer)
        with patch.object(backend, "ctl"):
            backend.install(manifest())
        self.assertIn(" --due", service.read_text())
        self.assertIn("OnCalendar=*-*-* *:10:00", timer.read_text())
        self.assertEqual(service.with_suffix(".service.pre-setup").read_text(), old_service)
        self.assertEqual(timer.with_suffix(".timer.pre-setup").read_text(), old_timer)
        custom = service.read_text() + "Nice=10\n"
        service.write_text(custom)
        with patch.object(backend, "ctl"):
            backend.install(manifest())
        self.assertEqual(service.read_text(), custom)

    def test_custom_schedule_conflict_is_explicit(self):
        timer = self.backend.directory / "rss-poll.timer"
        timer.write_text("[Timer]\nOnCalendar=daily\n# My timer\n")
        proposed = manifest()
        proposed["schedules"]["rss-poll"] = "hourly"
        with patch.object(self.backend, "ctl"), self.assertRaisesRegex(ValueError, "conflicts"):
            self.backend.install(proposed)
        self.assertIn("OnCalendar=daily", timer.read_text())


@unittest.skipUnless(os.environ.get("RUN_SYSTEMD_SETUP_TESTS") == "1", "opt-in: real user systemd manager")
class RealSystemd(unittest.TestCase):
    def test_activation_worker_execution_and_rollback(self):
        with tempfile.TemporaryDirectory(prefix="setup systemd ") as temporary:
            root = Path(temporary) / "percent% dollar$"
            code, data = root / "code", root / "data"
            (code / "jobsmcp/bin").mkdir(parents=True)
            (code / "jobsmcp/bin/python3").symlink_to(sys.executable)
            script = "import os\nfrom pathlib import Path\nPath(os.environ['TG_INFRA_DATA_DIR']).joinpath('executed').write_text('ok')\nprint('probe completed')\n"
            from lib.setup_services import SCRIPTS
            for name in SCRIPTS.values():
                (code / name).write_text(script)
            backend = Systemd(code, data, prefix="tg-setup-test-" + uuid.uuid4().hex[:12] + "-")
            proposed = manifest()
            proposed["schedules"] = {name: "2099-01-01 00:00:00" for name in SERVICES}
            backend.preflight(proposed)
            backend.capture()
            try:
                backend.install(proposed)
                backend.activate(proposed)
                for name in SERVICES:
                    active = backend.ctl("is-active", backend.unit(name), check=False).stdout.strip() == "active"
                    self.assertEqual(active, name in selected_services(proposed))
                backend.ctl("start", backend.unit("rss-poll", "service"))
                self.assertEqual((data / "executed").read_text(), "ok")
                self.assertIn("probe completed", (data / "state/rss-poll.log").read_text())
                reinstall = Systemd(code, data, prefix=backend.prefix)
                reinstall.capture()
                changed = {**proposed, "features": {**proposed["features"], "rss": False, "email": True}}
                try:
                    reinstall.quiesce()
                    reinstall.install(changed)
                    reinstall.activate(changed)
                    self.assertEqual(reinstall.ctl("is-active", reinstall.unit("email-ingest")).stdout.strip(), "active")
                    self.assertNotEqual(reinstall.ctl("is-active", reinstall.unit("rss-poll"), check=False).stdout.strip(), "active")
                finally:
                    self.assertEqual(reinstall.restore(), [])
                self.assertEqual(backend.ctl("is-active", backend.unit("rss-poll")).stdout.strip(), "active")
            except Exception:
                log = data / "state/rss-poll.log"
                if log.exists():
                    print(log.read_text())
                raise
            finally:
                errors = backend.restore()
                self.assertEqual(errors, [])
            for name in SERVICES:
                self.assertNotEqual(backend.ctl("is-enabled", backend.unit(name), check=False).stdout.strip(), "enabled")
                self.assertFalse((backend.directory / backend.unit(name)).exists())


if __name__ == "__main__":
    unittest.main()
