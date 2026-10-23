"""Extractor contract failures must stay visible and must not lose pending jobs."""

import hashlib
import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from lib.job_ingestion import ingest_feed, normalize_job, validate_jobs
from lib.rss_audit import check_rss_health

JOB = {"source_id": "job-1", "url": "https://jobs.example/one", "title": "Engineer",
       "content": "Engineer\nA paid role, open to applicants in Cyprus."}
FEED = {"id": "fixture", "name": "Fixture Jobs", "url": "https://jobs.example/feed"}


class TestJobContract(unittest.TestCase):
    def test_content_is_preserved_and_hash_is_owned_by_ingestion(self):
        original = {**JOB, "content": "  Engineer\nRequirements: Python.\n", "content_hash": "untrusted"}
        job = normalize_job(original)
        self.assertEqual(job["content"], original["content"])
        self.assertEqual(job["content_hash"], hashlib.sha256(original["content"].encode()).hexdigest())
        self.assertIsNone(job["posted_at"])
        self.assertEqual(job["categories"], [])
        self.assertEqual(original["content_hash"], "untrusted")

    def test_invalid_fields_fail_explicitly(self):
        invalid = [(field, value) for field in ("source_id", "title", "content", "url")
                   for value in (None, 1, "", " \n")]
        invalid += [("url", "file:///tmp/job"), ("url", "https:///missing-host"),
                    ("posted_at", "yesterday"), ("posted_at", "2026-10-21"),
                    ("posted_at", 123), ("categories", "Engineering"),
                    ("categories", [None]), ("categories", [""])]
        for field, value in invalid:
            with self.subTest(field=field, value=value), self.assertRaises(ValueError):
                normalize_job({**JOB, field: value})
        for field in ("source_id", "title", "content", "url"):
            with self.subTest(missing=field), self.assertRaises(ValueError):
                normalize_job({k: v for k, v in JOB.items() if k != field})

    def test_batch_contract_rejects_false_success_and_duplicate_identities(self):
        for items in (None, JOB, [], [JOB, None], [JOB, JOB]):
            with self.subTest(items=items), self.assertRaises(ValueError):
                validate_jobs(items)
        self.assertEqual(validate_jobs([], allow_empty=True), [])
        posted_at = "2026-10-21T10:00:00+03:00"
        self.assertEqual(normalize_job({**JOB, "posted_at": posted_at})["posted_at"], posted_at)


class TestIngestionBoundary(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="job-contract-")
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.index = self.state / "rss/fixture/index.json"
        self.output = [dict(JOB)]
        self.enriched = dict(JOB)

    def poll(self, **options):
        feed = {**FEED, **options}
        (self.state / "feeds.json").write_text(json.dumps({"feeds": [feed]}))
        def enrich(item, *_):
            # Even an in-place extractor must not corrupt the retained original.
            item.update(self.enriched)
            return item
        return ingest_feed(feed, self.state, fetch=lambda *_: (b"fixture", {}),
                           extract=lambda *_: self.output, enrich=enrich)

    def queue(self):
        return list((self.state / "message_queue").glob("*.json"))

    def problems(self):
        return check_rss_health(self.state, since=datetime.now(timezone.utc)-timedelta(days=1))[1]

    def test_whole_batch_validated_before_queue_and_previous_state_survives(self):
        self.poll()
        previous = self.index.read_bytes()
        original = self.queue()[0].read_bytes()
        self.output = [{**JOB, "source_id": "new-job"}, {**JOB, "content": None}]
        with self.assertRaisesRegex(ValueError, "content"):
            self.poll()
        self.assertEqual(self.index.read_bytes(), previous)
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.queue()[0].read_bytes(), original)
        self.assertTrue(any("content" in problem for problem in self.problems()))
        self.output = self.output[:1]
        self.assertEqual(self.poll()["queued"], 1)
        self.assertEqual(self.problems(), [])

    def test_legacy_identity_and_all_audit_links_are_preserved(self):
        self.poll()
        queued = json.loads(self.queue()[0].read_text())
        key = hashlib.sha256(JOB["source_id"].encode()).hexdigest()
        content_hash = hashlib.sha256(JOB["content"].encode()).hexdigest()
        mid = f"rss-fixture-{key}-{content_hash}"
        self.assertEqual(queued["meta"]["message_id"], mid)
        self.assertEqual(self.queue()[0].stem, mid)
        index = json.loads(self.index.read_text())
        observation = json.loads(self.index.with_name("observations.json").read_text())["items"][0]
        arrival = json.loads((self.state / "audit/rss.jsonl").read_text())
        self.assertEqual([index["items"][key]["msg_id"], observation["msg_id"], arrival["msg_id"]], [mid]*3)
        # Ignore a stale extractor-supplied hash, including after a restart.
        self.output[0]["content_hash"] = "incorrect"
        self.assertEqual(self.poll()["queued"], 0)
        self.assertEqual(len(self.queue()), 1)
        self.output[0]["content"] += " Salary increased."
        self.assertEqual(self.poll()["queued"], 1)
        metas = [json.loads(path.read_text())["meta"] for path in self.queue()]
        self.assertEqual({m["job_id"] for m in metas}, {f"rss-fixture-{key}"})
        self.assertEqual(len({m["message_id"] for m in metas}), 2)
        self.assertEqual(self.problems(), [])

    def test_invalid_enrichment_is_retained_and_recovered_after_feed_rolloff(self):
        for invalid in ({"content": ""}, {"source_id": "wrong-id"}, {"url": "https://jobs.example/wrong"}):
            self.enriched = {**JOB, **invalid}
            with self.subTest(invalid=invalid), self.assertRaises(ValueError):
                self.poll(enrich="nodesk", allow_empty=True)
            pending, = json.loads(self.index.read_text())["deferred"].values()
            self.assertEqual(pending["item"]["source_id"], JOB["source_id"])
            self.assertEqual(pending["item"]["content"], JOB["content"])
            self.assertEqual(self.queue(), [])
            self.assertTrue(any("DEFERRED" in problem for problem in self.problems()))
        self.output = []
        self.enriched = {**JOB, "content": JOB["content"] + "\nFull requirements: Python."}
        self.assertEqual(self.poll(enrich="nodesk", allow_empty=True)["queued"], 1)
        self.assertIn("Full requirements", self.queue()[0].read_text())
        self.assertEqual(self.poll(enrich="nodesk", allow_empty=True)["queued"], 0)
        self.assertEqual(self.problems(), [])

    def test_existing_rss_adapter_uses_the_shared_validation_boundary(self):
        import rss_poller
        self.output = [{**JOB, "content": None}]
        (self.state / "feeds.json").write_text(json.dumps({"feeds": [FEED]}))
        with patch.object(rss_poller, "fetch_feed", return_value=(b"fixture", {})), \
             patch.object(rss_poller, "parse_feed", return_value=self.output), \
             self.assertRaisesRegex(ValueError, "content"):
            rss_poller.poll_feed(FEED, self.state)
        self.assertEqual(self.queue(), [])
        self.assertTrue(any("content" in problem for problem in self.problems()))
