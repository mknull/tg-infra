"""Behavioral checks of the audit CLI using isolated, on-disk RSS evidence."""

import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone

PROJECT = Path(__file__).resolve().parents[1]


class TestRssAudit(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="rss-audit-")
        self.addCleanup(temporary.cleanup)
        self.project = Path(temporary.name)
        self.state = self.project / "state"
        shutil.copy2(PROJECT / "audit", self.project / "audit")
        shutil.copytree(PROJECT / "lib", self.project / "lib",
                        ignore=shutil.ignore_patterns("__pycache__"))
        self.now = datetime.now(timezone.utc)
        self.mid = "rss-fixture-revision-one"
        self.configure()
        self.put("rss/fixture/status.json", {"ok": True, "at": self.now.isoformat()})
        self.put("rss/fixture/index.json", {"items": {}})

    def put(self, name, value):
        path = self.state / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(value))
        return path

    def configure(self, enabled=True):
        self.put("feeds.json", {"feeds": [{"id": "fixture", "url": "https://example.invalid/feed",
                                          "enabled": enabled}]})

    def records(self, name, values):
        path = self.state / "audit" / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("".join(json.dumps(r) + "\n" for r in values))
        return path

    def arrival(self, hours=0, mid=None):
        return {"msg_id": mid or self.mid, "source": "rss", "feed_id": "fixture",
                "sender": "Fixture Jobs", "preview": "Engineer", "stage": "arrival",
                "arrived_at": (self.now - timedelta(hours=hours)).isoformat()}

    def evaluation(self, flash="flag", pro="send", hours=0, mid=None):
        record = self.arrival(hours, mid)
        record.pop("stage")
        record["flash"] = {"decision": flash, "at": record["arrived_at"], "reason": "fixture"}
        if pro is not None:
            record["pro"] = {"decision": pro, "at": record["arrived_at"],
                             "message": "A suitable role.", "reason": "fixture"}
        return record

    def queue(self, hours=0):
        self.put("message_queue/" + self.mid + ".json",
                 {"content": "Engineer", "meta": {"source": "rss", "feed_id": "fixture",
                  "message_id": self.mid, "ts": self.arrival(hours)["arrived_at"]}})

    def delivery(self, mid=None, **changes):
        return {"at": self.now.isoformat(), "ref": mid or self.mid, "message_type": "job_match",
                "channel": "telegram", "success": True, "platform_id": "42", "error": None, **changes}

    def cli(self, *args, code=0):
        result = subprocess.run([sys.executable, str(self.project / "audit"), *args],
                                cwd=self.project, text=True, capture_output=True, timeout=15)
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        return result.stdout

    def test_successful_send_and_skips_are_healthy_and_read_only(self):
        records = [self.arrival(), self.evaluation()]
        for mid, flash, pro in (("rss-skip-flash", "skip", None), ("rss-skip-pro", "flag", "skip")):
            records.extend([self.arrival(mid=mid), self.evaluation(flash, pro, mid=mid)])
        self.records("rss.jsonl", records)
        self.records("delivery.jsonl", [self.delivery()])
        before = {p.relative_to(self.state): p.read_bytes() for p in self.state.rglob("*") if p.is_file()}
        self.assertIn("3 evaluated", self.cli("--health"))
        self.assertEqual(before, {p.relative_to(self.state): p.read_bytes()
                                 for p in self.state.rglob("*") if p.is_file()})
        self.assertNotIn("re-evaluated", self.cli("rss", "--last", "10"))

    def test_recent_arrival_is_pending_then_old_queue_fails_outside_window(self):
        self.records("rss.jsonl", [self.arrival()])
        self.queue()
        self.assertIn("1 pending within 1h", self.cli("--health"))
        self.records("rss.jsonl", [self.arrival(hours=72)])
        self.queue(hours=72)
        self.assertIn("RSS STALLED [fixture]", self.cli("--health", "--days", "1", code=1))

    def test_lost_queue_does_not_hide_an_old_arrival(self):
        self.records("rss.jsonl", [self.arrival(hours=72)])
        self.assertIn("RSS UNACCOUNTED [fixture]", self.cli("--health", code=1))

    def test_index_catches_lost_arrival_and_queue(self):
        self.put("rss/fixture/index.json", {"items": {"key": {
            "msg_id": self.mid, "first_seen": self.arrival(hours=72)["arrived_at"]}}})
        self.assertIn("RSS UNACCOUNTED [fixture]", self.cli("--health", code=1))

    def test_queue_without_arrival_or_index_cannot_be_invisible(self):
        self.queue(hours=4)
        self.assertIn("RSS STALLED [fixture]", self.cli("--health", code=1))

    def test_disabled_feed_is_paused_but_removed_feed_is_not_silently_ignored(self):
        self.configure(enabled=False)
        (self.state / "rss/fixture/status.json").unlink()
        self.records("rss.jsonl", [self.arrival(hours=72)])
        self.queue(hours=72)
        self.assertIn("RSS PAUSED [fixture]", self.cli("--health"))
        self.put("feeds.json", {"feeds": []})
        self.assertIn("RSS STALLED [fixture]", self.cli("--health", code=1))

    def test_evaluation_without_arrival_fails(self):
        self.records("rss.jsonl", [self.evaluation("skip", None)])
        self.assertIn("RSS ARRIVAL MISSING", self.cli("--health", code=1))

    def test_failed_and_incomplete_cascades_fail(self):
        for flash, pro in (("dead_letter", None), ("error", None), ("flag", "dead_letter"),
                           ("flag", "error"), ("flag", None), ("skip", "skip"), ("unknown", None)):
            with self.subTest(flash=flash, pro=pro):
                self.records("rss.jsonl", [self.arrival(), self.evaluation(flash, pro)])
                self.assertIn("RSS PROCESSING FAILED", self.cli("--health", code=1))

    def test_failed_flash_window_cannot_masquerade_as_skip(self):
        evaluation = self.evaluation("skip", None)
        evaluation["flash"]["windows"] = [{"decision": "error"}]
        self.records("rss.jsonl", [self.arrival(), evaluation])
        self.assertIn("invalid or failed Flash window", self.cli("--health", code=1))

    def test_latest_failed_or_incomplete_evaluation_does_not_age_out(self):
        for pro in (None, "dead_letter", "error"):
            with self.subTest(pro=pro):
                self.records("rss.jsonl", [self.arrival(hours=72), self.evaluation(pro=pro, hours=72)])
                self.assertIn("RSS PROCESSING FAILED", self.cli("--health", "--days", "1", code=1))

    def test_recovered_historical_failure_is_not_permanently_unhealthy(self):
        self.records("rss.jsonl", [self.arrival(hours=74), self.evaluation(pro="dead_letter", hours=73),
                                   self.evaluation(pro="skip", hours=72)])
        self.cli("--health", "--days", "1")

    def test_flash_exhaustion_escalates_but_partial_read_does_not(self):
        evaluation = self.evaluation(pro="skip")
        window = {"decision": "read_more", "window_end": 3, "total_lines": 3}
        evaluation["flash"]["windows"] = [window]
        self.records("rss.jsonl", [self.arrival(), evaluation])
        self.cli("--health")
        window["total_lines"] = 6
        self.records("rss.jsonl", [self.arrival(), evaluation])
        self.assertIn("before Flash passed or exhausted", self.cli("--health", code=1))

    def test_explicit_flash_budget_handoff_requires_completed_pro_evaluation(self):
        evaluation = self.evaluation(pro="skip")
        window = {"decision": "read_more", "window_end": 12, "total_lines": 3003,
                  "budget_exhausted": True}
        evaluation["flash"]["windows"] = [window]
        self.records("rss.jsonl", [self.arrival(), evaluation])
        self.cli("--health")
        pro = evaluation.pop("pro")
        self.records("rss.jsonl", [self.arrival(), evaluation])
        self.assertIn("invalid cascade", self.cli("--health", code=1))
        evaluation["pro"] = pro
        window["decision"] = "disqualified"
        self.records("rss.jsonl", [self.arrival(), evaluation])
        self.assertIn("before Flash passed or exhausted", self.cli("--health", code=1))

    def test_send_requires_its_own_job_match_acknowledgement_even_when_old(self):
        self.records("rss.jsonl", [self.arrival(hours=72), self.evaluation(hours=72)])
        for deliveries in ([], [self.delivery(mid="some-other-revision")],
                           [self.delivery(message_type="canary")]):
            with self.subTest(deliveries=deliveries):
                self.records("delivery.jsonl", deliveries)
                self.assertIn("RSS DELIVERY MISSING", self.cli("--health", code=1))

    def test_delivery_failure_or_missing_acknowledgement_fails(self):
        self.records("rss.jsonl", [self.arrival(), self.evaluation()])
        for changes in ({"success": False, "error": "HTTP 403", "platform_id": None},
                        {"platform_id": None}, {"error": "timeout"}):
            with self.subTest(changes=changes):
                self.records("delivery.jsonl", [self.delivery(**changes)])
                self.assertIn("RSS DELIVERY FAILED", self.cli("--health", code=1))

    def test_empty_send_message_fails_even_with_acknowledgement(self):
        evaluation = self.evaluation()
        evaluation["pro"]["message"] = ""
        self.records("rss.jsonl", [self.arrival(), evaluation])
        self.records("delivery.jsonl", [self.delivery()])
        self.assertIn("Pro send has no message", self.cli("--health", code=1))

    def test_distinct_revisions_pass_and_same_minute_duplicate_evaluations_fail(self):
        evaluations = [self.evaluation("skip", None), self.evaluation("skip", None, mid="rss-revision-two")]
        self.records("rss.jsonl", [self.arrival(), self.arrival(mid="rss-revision-two"), *evaluations])
        self.cli("--health")
        self.records("rss.jsonl", [self.arrival(), evaluations[0], evaluations[0]])
        self.assertIn("RSS DUPLICATE EVALS", self.cli("--health", code=1))

    def test_duplicate_or_unexpected_delivery_fails(self):
        self.records("rss.jsonl", [self.arrival(), self.evaluation()])
        self.records("delivery.jsonl", [self.delivery(), self.delivery(platform_id="43")])
        self.assertIn("RSS DUPLICATE DELIVERY", self.cli("--health", code=1))
        self.records("rss.jsonl", [self.arrival(), self.evaluation("skip", None)])
        self.records("delivery.jsonl", [self.delivery()])
        self.assertIn("RSS UNEXPECTED DELIVERY", self.cli("--health", code=1))

    def test_missing_failed_stale_and_malformed_poll_status(self):
        path = self.state / "rss/fixture/status.json"
        path.unlink()
        self.assertIn("never polled", self.cli("--health", code=1))
        for status, expected in (({"ok": False, "at": self.now.isoformat(), "error": "HTTP 403"}, "HTTP 403"),
                                 ({"ok": True, "at": self.arrival(hours=49)["arrived_at"]}, "over 48 hours"),
                                 ([], "invalid poll status")):
            self.put("rss/fixture/status.json", status)
            self.assertIn(expected, self.cli("--health", code=1))

    def test_poll_counters_explain_filtering_and_304_is_healthy(self):
        self.put("rss/fixture/status.json", {"ok": True, "at": self.now.isoformat(),
                                            "new": 0, "changed": 0, "unchanged": 0, "filtered": 12, "queued": 0})
        self.assertIn("filtered=12", self.cli("--health"))
        self.put("rss/fixture/status.json", {"ok": True, "at": self.now.isoformat(), "not_modified": True})
        self.assertIn("not modified (304)", self.cli("--health"))

    def test_successful_poll_requires_index(self):
        (self.state / "rss/fixture/index.json").unlink()
        self.assertIn("successful poll has no ingestion index", self.cli("--health", code=1))

    def test_corrupt_rss_evidence_reports_failure_without_crashing(self):
        for name, value in (("rss/fixture/index.json", []),
                            ("message_queue/rss-broken.json", [])):
            with self.subTest(name=name):
                path = self.put(name, value)
                self.cli("--health", code=1)
                path.unlink()
                self.put("rss/fixture/index.json", {"items": {}})
        self.records("rss.jsonl", []).write_text("not json\n")
        self.assertIn("RSS EVIDENCE INVALID", self.cli("--health", code=1))


if __name__ == "__main__":
    unittest.main()
