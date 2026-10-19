"""Behavioral contracts for publisher identity, subscription changes and backfill."""

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import rss_poller as rss
from lib.rss_audit import check_rss_health
from lib.sources import validate_feeds

NOW = "2026-10-18T12:00:00Z"
DATE = datetime.fromisoformat(NOW.replace("Z", "+00:00"))


def xml(body="A full description.", *, guid="job-1", link="https://jobs.example/one", date="", category=""):
    date = f"<pubDate>{date}</pubDate>" if date else ""
    category = f"<category>{category}</category>" if category else ""
    return (f'<rss version="2.0"><channel><title>Jobs</title><link>https://jobs.example/</link>'
            f'<description>Jobs</description><item><guid>{guid}</guid><title>Example job</title>'
            f'<link>{link}</link>{date}{category}<description>{body}</description></item></channel></rss>').encode()


EMPTY = b'<rss version="2.0"><channel><title>Jobs</title><link>https://jobs.example/</link></channel></rss>'
DETAIL = ('<script type="application/ld+json">' + json.dumps({"@type": "JobPosting",
    "description": "<p>We need an accessibility designer to interview users and prototype inclusive services. "
                   "Requirements include user research experience and accessible interaction design.</p>",
    "applicantLocationRequirements": {"@type": "Country", "name": "Cyprus"}}) + '</script>').encode()


class TestSubscriptions(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.state = Path(temporary.name)
        self.feed = {"id": "fixture", "name": "Example", "subscriptions": [
            {"id": "a", "url": "https://jobs.example/a", "allow_empty": True, "poll_interval_hours": 1},
            {"id": "b", "url": "https://jobs.example/b", "allow_empty": True, "poll_interval_hours": 24}]}
        self.responses = {s["url"]: xml() for s in self.feed["subscriptions"]}
        self.calls = []
        self.addCleanup(patch.stopall)
        patch.object(rss, "_now", return_value=NOW).start()
        patch.object(rss.time, "sleep").start()
        patch.object(rss, "fetch_feed", side_effect=self.fetch).start()

    def fetch(self, url, previous):
        self.calls.append((url, previous))
        result = self.responses[url]
        if isinstance(result, Exception):
            raise result
        return result, {"etag": "fixture", "response_url": url}

    def poll(self, feed=None, **kwargs):
        feed = feed or self.feed
        (self.state / "feeds.json").write_text(json.dumps({"feeds": [feed]}))
        return rss.poll_feed(feed, self.state, **kwargs)

    def queue(self):
        return list((self.state / "message_queue").glob("*.json"))

    def index(self):
        return json.loads((self.state / "rss/fixture/index.json").read_text())

    def health(self):
        return check_rss_health(self.state, since=DATE-timedelta(days=1), now=DATE)

    def test_overlapping_feeds_choose_once_and_replay_survives_queue_consumption(self):
        self.responses[self.feed["subscriptions"][1]["url"]] = xml("Longer complete job description with requirements.")
        self.assertEqual(self.poll()["queued"], 1)
        original = json.loads(self.queue()[0].read_text())
        self.assertIn("Longer complete", original["content"])
        self.assertEqual(original["meta"]["subscription_ids"], ["a", "b"])
        statuses = [json.loads((self.state / "rss/fixture/subscriptions" / sid / "status.json").read_text()) for sid in ("a", "b")]
        self.assertEqual(sum(s["queued"] for s in statuses), 1)
        self.assertEqual(sum(s["duplicate"] for s in statuses), 1)
        self.assertTrue(all(s["fetched_bytes"] > 0 and s["parsed_count"] == 1 for s in statuses))
        self.queue()[0].unlink()
        self.assertEqual(self.poll()["queued"], 0)
        self.assertEqual(self.queue(), [])
        self.assertEqual(len((self.state / "audit/rss.jsonl").read_text().splitlines()), 1)

    def test_migrate_legacy_identity_then_switch_representations_without_resends(self):
        legacy = {"id": "fixture", "name": "Example", "url": self.feed["subscriptions"][0]["url"]}
        self.poll(legacy)
        path = self.state / "rss/fixture/index.json"
        old = self.index()
        for item in old["items"].values():
            item.pop("representations"); item.pop("revision_hashes")
        path.write_text(json.dumps(old))
        original = self.queue()[0].name
        self.responses[self.feed["subscriptions"][1]["url"]] = xml("Different summary for the existing vacancy.")
        selected = {**self.feed, "subscriptions": [self.feed["subscriptions"][1]]}
        self.assertEqual(self.poll(selected)["queued"], 0)
        self.assertEqual(self.queue()[0].name, original)
        self.responses[self.feed["subscriptions"][1]["url"]] = xml("Actual subsequent change: salary increased.")
        self.assertEqual(self.poll(selected)["queued"], 1)
        self.assertEqual(len(self.queue()), 2)
        self.assertEqual(self.poll(legacy)["queued"], 0)
        self.assertEqual(self.poll(selected)["queued"], 0)

    def test_future_only_seeds_baseline_and_new_arrivals_queue(self):
        feed = {**self.feed, "backfill": {"mode": "future"}}
        self.assertEqual(self.poll(feed)["queued"], 0)
        self.assertEqual(self.health()[1], [])
        self.responses[self.feed["subscriptions"][0]["url"]] = xml(guid="new-job")
        self.assertEqual(self.poll(feed)["queued"], 1)
        self.assertEqual(len(self.queue()), 1)

    def test_legacy_future_baseline_survives_schema_migration_without_missing_new_jobs(self):
        url = self.feed["subscriptions"][0]["url"]
        legacy = {"id": "fixture", "name": "Example", "url": url, "backfill": {"mode": "future"}}
        self.assertEqual(self.poll(legacy)["queued"], 0)
        self.responses[url] = xml(guid="arrived-after-original-baseline")
        migrated = {**self.feed, "backfill": {"mode": "future"}, "subscriptions": [self.feed["subscriptions"][0]]}
        self.assertEqual(self.poll(migrated)["queued"], 1)
        self.assertEqual(self.health()[1], [])

    def test_explicit_backfill_expansion_can_include_previously_baselined_jobs_once(self):
        self.assertEqual(self.poll({**self.feed, "backfill": {"mode": "future"}})["queued"], 0)
        expanded = {**self.feed, "backfill": {"mode": "all"}}
        self.assertEqual(self.poll(expanded)["queued"], 1)
        self.assertEqual(self.poll(expanded)["queued"], 0)

    def test_recent_backfill_keeps_unknown_dates_and_excludes_old_jobs(self):
        self.responses[self.feed["subscriptions"][0]["url"]] = xml(date="Tue, 01 Jan 2019 12:00:00 GMT")
        self.responses[self.feed["subscriptions"][1]["url"]] = xml(guid="unknown-date")
        self.assertEqual(self.poll({**self.feed, "backfill": {"mode": "recent", "days": 30}})["queued"], 1)
        self.assertIn("unknown-date", self.queue()[0].read_text())
        self.assertEqual(self.health()[1], [])

    def test_missing_category_is_not_an_exclusion(self):
        self.assertEqual(self.poll({**self.feed, "include_categories": ["Design"]})["queued"], 1)

    def test_each_subscription_obeys_its_cadence_and_config_changes_refetch(self):
        self.poll()
        self.calls.clear()
        with patch.object(rss, "_now", return_value="2026-10-18T14:00:00Z"):
            self.poll(respect_cadence=True)
        self.assertEqual([u for u, _ in self.calls], [self.feed["subscriptions"][0]["url"]])
        self.calls.clear()
        self.poll({**self.feed, "include_categories": ["Design"]}, respect_cadence=True)
        self.assertEqual(len(self.calls), 2)
        self.assertTrue(all(not previous for _, previous in self.calls))

    def test_subscription_failure_isolated_and_audited(self):
        self.responses[self.feed["subscriptions"][0]["url"]] = OSError("publisher unavailable")
        with self.assertRaises(ValueError):
            self.poll()
        self.assertEqual(len(self.queue()), 1)
        self.assertTrue(any("fixture/a" in p and "unavailable" in p for p in self.health()[1]))
        self.responses[self.feed["subscriptions"][0]["url"]] = xml()
        self.assertEqual(self.poll()["queued"], 0)
        self.assertEqual(self.health()[1], [])

    def test_audit_checks_subscription_cadence_and_item_accounting(self):
        self.poll()
        _, problems = check_rss_health(self.state, since=DATE, now=DATE+timedelta(hours=3))
        self.assertTrue(any("fixture/a" in p and "over 2 hours" in p for p in problems))
        self.assertFalse(any("fixture/b" in p and "hours" in p for p in problems))
        path = self.state / "rss/fixture/subscriptions/a/observations.json"
        evidence = json.loads(path.read_text()); evidence["items"] = []
        path.write_text(json.dumps(evidence))
        self.assertTrue(any("accounted items" in p for p in self.health()[1]))

    def test_failed_publisher_checkpoint_does_not_commit_future_only_baseline(self):
        feed = {**self.feed, "backfill": {"mode": "future"}}
        original = rss._write_json
        def interrupted(path, value):
            if path == self.state / "rss/fixture/index.json":
                raise OSError("interrupted")
            original(path, value)
        with patch.object(rss, "_write_json", side_effect=interrupted), self.assertRaises(OSError):
            self.poll(feed)
        self.assertFalse((self.state / "rss/fixture/subscriptions/a/index.json").exists())
        self.assertEqual(self.poll(feed)["queued"], 0)

    def test_detail_failure_retried_after_item_leaves_feed_window(self):
        self.feed["subscriptions"] = [self.feed["subscriptions"][0]]
        self.feed["enrich"] = "nodesk"
        job_url = "https://nodesk.co/remote-jobs/example/"
        self.responses[self.feed["subscriptions"][0]["url"]] = xml(link=job_url)
        self.responses[job_url] = OSError("temporary detail outage")
        with self.assertRaises(ValueError):
            self.poll()
        self.assertEqual(self.queue(), [])
        self.assertEqual(len(self.index()["deferred"]), 1)
        self.assertTrue(any("DEFERRED" in p for p in self.health()[1]))
        self.responses[self.feed["subscriptions"][0]["url"]] = EMPTY
        self.responses[job_url] = DETAIL
        self.assertEqual(self.poll()["queued"], 1)
        self.assertIn("Cyprus", self.queue()[0].read_text())
        self.assertEqual(self.index()["deferred"], {})
        self.assertEqual(self.health()[1], [])
        self.calls.clear()
        self.poll()
        self.assertNotIn(job_url, [url for url, _ in self.calls])

    def test_detail_budget_defers_and_recovers_without_requeueing_success(self):
        self.feed["enrich"] = "nodesk"
        for i, sub in enumerate(self.feed["subscriptions"]):
            url = f"https://nodesk.co/remote-jobs/example-{i}/"
            self.responses[sub["url"]] = xml(guid=str(i), link=url)
            self.responses[url] = DETAIL
        with patch.object(rss, "MAX_DETAIL_FETCHES", 1), self.assertRaises(ValueError):
            self.poll()
        self.assertEqual(len(self.queue()), 1)
        self.assertEqual(self.poll()["queued"], 1)
        self.assertEqual(len(self.queue()), 2)

    def test_legacy_url_configuration_clears_successfully_retried_details(self):
        url = self.feed["subscriptions"][0]["url"]
        feed = {"id": "fixture", "name": "Example", "url": url, "enrich": "nodesk", "allow_empty": True}
        job_url = "https://nodesk.co/remote-jobs/example/"
        self.responses[url] = xml(link=job_url)
        self.responses[job_url] = OSError("outage")
        with self.assertRaises(ValueError):
            self.poll(feed)
        self.responses[url] = EMPTY
        self.responses[job_url] = DETAIL
        self.assertEqual(self.poll(feed)["queued"], 1)
        self.assertEqual(self.index()["deferred"], {})
        self.assertEqual(self.poll(feed)["queued"], 0)
        self.assertEqual(self.health()[1], [])

    def test_invalid_subscription_configuration_is_rejected(self):
        for subscriptions in ([], [{"id": "../bad", "url": "https://example.com"}],
                              [self.feed["subscriptions"][0]]*2,
                              [{"id": "x", "url": "file:///tmp/job"}],
                              [{"id": "x", "url": "https://example.com", "poll_interval_hours": 0}]):
            with self.subTest(subscriptions=subscriptions), self.assertRaises(ValueError):
                validate_feeds({"feeds": [{**self.feed, "subscriptions": subscriptions}]})


if __name__ == "__main__":
    unittest.main()
