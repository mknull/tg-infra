"""RSS/Atom ingestion, replay, and downstream integration without live services."""

import contextlib
import importlib.machinery
import importlib.util
import io
import json
import tempfile
import unittest
import urllib.error
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

import bot_commands
import it_jobs_triage as triage
import lib.delivery as delivery
import rss_poller as rss
import weekly_trend
from lib import SeenLedger, TransportError
from lib.sources import load_feeds

FIXTURES = Path(__file__).parent / "fixtures" / "feeds"
FEED = {"id": "fixture", "name": "Fixture Jobs", "url": "https://jobs.example/feed", "enabled": True,
        "desired_roles": "Engineering", "acceptable_roles": "Research"}


def fixture(name="rss"):
    return (FIXTURES / f"{name}.xml").read_bytes()


class TestParsing(unittest.TestCase):
    def test_rss_url_identity_html_links_and_dates(self):
        first, second = rss.parse_feed(fixture(), FEED["url"])
        self.assertEqual(first["source_id"], "https://jobs.example/jobs/42")
        self.assertEqual(first["posted_at"], "2026-10-13T09:00:00Z")
        self.assertIn("Build open source tools & services.", first["content"])
        self.assertIn("https://employer.example/apply/42", first["content"])
        self.assertNotIn("<p>", first["content"])
        self.assertEqual(second["source_id"], "vacancy-43")
        self.assertIsNone(second["posted_at"])

    def test_atom_prefers_full_content_and_alternate_link(self):
        item, = rss.parse_feed(fixture("atom"), FEED["url"])
        self.assertEqual(item["source_id"], "urn:fixture:job:44")
        self.assertEqual(item["url"], "https://jobs.example/jobs/44")
        self.assertIn("Full description.", item["content"])
        self.assertNotIn("Short summary", item["content"])
        self.assertIn("https://jobs.example/apply/44", item["content"])
        self.assertEqual(item["posted_at"], "2026-10-13T09:00:00Z")

    def test_rss_one_rdf(self):
        item, = rss.parse_feed(fixture("rdf"), FEED["url"])
        self.assertEqual(item["url"], "https://jobs.example/jobs/45")

    def test_named_entity_repair_is_opt_in_and_keeps_xml_validation(self):
        raw = fixture("rdf").replace(b"Build free software.", b"Build &ldquo;free&rdquo; software.")
        with self.assertRaises(ValueError):
            rss.parse_feed(raw, FEED["url"])
        item, = rss.parse_feed(raw, FEED["url"], repair_html_entities=True)
        self.assertIn('Build “free” software.', item["content"])
        for invalid in (raw.replace(b"&ldquo;", b"&unknown;"), raw.replace(b"</item>", b"</broken>")):
            with self.subTest(raw=invalid), self.assertRaises(ValueError):
                rss.parse_feed(invalid, FEED["url"], repair_html_entities=True)
        cdata = b'<![CDATA[literal &rsquo; &amp;]]><!-- &rsquo; -->'
        self.assertEqual(rss._repair_html_entities(cdata), cdata)

    def test_expected_empty_feed_requires_opt_in_and_channel_metadata(self):
        raw = fixture("empty")
        with self.assertRaises(ValueError):
            rss.parse_feed(raw, FEED["url"])
        self.assertEqual(rss.parse_feed(raw, FEED["url"], allow_empty=True), [])
        for invalid in (b'<rss version="2.0"/>', b'<html>No jobs</html>'):
            with self.subTest(raw=invalid), self.assertRaises(ValueError):
                rss.parse_feed(invalid, FEED["url"], allow_empty=True)

    def test_republication_date_does_not_change_content_identity(self):
        before = rss.parse_feed(fixture(), FEED["url"])[0]
        after = rss.parse_feed(fixture().replace(b"13 Oct", b"14 Oct"), FEED["url"])[0]
        self.assertEqual(before["content_hash"], after["content_hash"])
        self.assertNotEqual(before["posted_at"], after["posted_at"])

    def test_rejects_challenge_malformed_empty_and_incomplete_entries(self):
        cases = [b"<html><title>Please verify</title></html>", b"<rss><broken>",
                 b'<rss version="2.0"><channel><title>Empty</title></channel></rss>',
                 fixture().replace(b"https://jobs.example/jobs/42", b"")]
        for raw in cases:
            with self.subTest(raw=raw[:40]), self.assertRaises(ValueError):
                rss.parse_feed(raw, FEED["url"])


class _FeedState(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state = Path(self.tmp.name)
        self.index = self.state / "rss" / "fixture" / "index.json"
        self.status = self.index.with_name("status.json")

    def poll(self, raw=None):
        with patch.object(rss, "fetch_feed", return_value=(fixture() if raw is None else raw, {"etag": '"v1"'})):
            return rss.poll_feed(FEED, self.state)

    def queue(self):
        return list((self.state / "message_queue").glob("*.json"))


class TestIngestion(_FeedState):
    def test_fsf_filters_volunteer_and_information_entries_before_queueing(self):
        preset = load_feeds(Path(__file__).resolve().parents[1] / "feeds.json.example")["fsf"]
        with patch.object(rss, "fetch_feed", return_value=(fixture("fsf"), {})):
            result = rss.poll_feed(preset, self.state)
        self.assertEqual((result["new"], result["filtered"], result["queued"]), (1, 2, 1))
        entry = json.loads(self.queue()[0].read_text())
        self.assertTrue(entry["content"].startswith("Job opportunity:"))

    def test_category_selection_changes_reconsider_excluded_entries(self):
        preset = load_feeds(Path(__file__).resolve().parents[1] / "feeds.json.example")["wwr"]
        with patch.object(rss, "fetch_feed", return_value=(fixture("categories"), {})) as fetch:
            result = rss.poll_feed(preset, self.state)
            self.assertEqual((result["queued"], result["filtered"]), (1, 1))
            queued = json.loads(self.queue()[0].read_text())
            self.assertEqual(queued["meta"]["categories"], ["Back-End Programming"])
            self.assertIn("Categories: Back-End Programming", queued["content"])
            rss.poll_feed(preset, self.state)
            self.assertIn("retrieved_at", fetch.call_args.args[1])
            result = rss.poll_feed({**preset, "include_categories": []}, self.state)
            self.assertEqual(fetch.call_args.args[1], {})
            self.assertEqual(result["queued"], 1)

    def test_expected_empty_poll_keeps_prior_jobs_and_accepts_304(self):
        self.poll()
        prior = json.loads(self.index.read_text())["items"]
        with patch.object(rss, "fetch_feed", return_value=(fixture("empty"), {"etag": '"empty"'})):
            result = rss.poll_feed({**FEED, "allow_empty": True}, self.state)
        self.assertEqual(result["queued"], 0)
        self.assertEqual(json.loads(self.index.read_text())["items"], prior)
        empty_state = self.state / "first-empty"
        with patch.object(rss, "fetch_feed", return_value=(fixture("empty"), {"etag": '"empty"'})):
            rss.poll_feed({**FEED, "allow_empty": True}, empty_state)
        error = urllib.error.HTTPError(FEED["url"], 304, "Not Modified", {}, None)
        with patch.object(rss.urllib.request, "urlopen", side_effect=error):
            result = rss.poll_feed({**FEED, "allow_empty": True}, empty_state)
        self.assertTrue(result["not_modified"])

    def test_unchanged_rerun_is_idempotent_after_queue_consumption(self):
        self.assertEqual(self.poll()["queued"], 2)
        before = json.loads(self.index.read_text())
        # The persistent index, not a short-lived processing ledger, gates ingestion.
        for path in self.queue():
            path.unlink()
        with patch.object(rss, "_now", return_value="2027-01-01T00:00:00Z"):
            self.assertEqual(self.poll()["queued"], 0)
        self.assertEqual(self.queue(), [])
        after = json.loads(self.index.read_text())
        self.assertEqual(set(before["items"]), set(after["items"]))
        for key in before["items"]:
            self.assertEqual(before["items"][key]["first_seen"], after["items"][key]["first_seen"])
        self.assertEqual(len((self.state / "audit" / "rss.jsonl").read_text().splitlines()), 2)

    def test_change_has_same_job_id_and_new_revision(self):
        self.poll()
        before = json.loads(self.index.read_text())
        result = self.poll(fixture().replace(b"Remote role.", b"Remote role. Salary increased."))
        self.assertEqual((result["new"], result["changed"], result["queued"]), (0, 1, 1))
        after = json.loads(self.index.read_text())
        revisions = 0
        for key, item in before["items"].items():
            self.assertEqual(item["job_id"], after["items"][key]["job_id"])
            revisions += item["msg_id"] != after["items"][key]["msg_id"]
        self.assertEqual(revisions, 1)

    def test_missing_entries_are_retained_and_failed_polls_preserve_index(self):
        self.poll()
        self.poll(fixture("atom"))
        self.assertEqual(len(json.loads(self.index.read_text())["items"]), 3)
        before = self.index.read_bytes()
        for response in (b"<html>Challenge</html>", b'<rss version="2.0"><channel/></rss>'):
            with self.assertRaises(ValueError):
                self.poll(response)
            self.assertEqual(self.index.read_bytes(), before)
            self.assertFalse(json.loads(self.status.read_text())["ok"])

    def test_checkpoint_failure_recovers_without_duplicate_arrivals(self):
        original = rss._write_json
        def fail_index(path, value):
            if path == self.index:
                raise OSError("checkpoint interrupted")
            original(path, value)
        with patch.object(rss, "_write_json", side_effect=fail_index), self.assertRaises(OSError):
            self.poll()
        self.assertEqual(len(self.queue()), 2)
        # Simulate triage completing before the collector can checkpoint.
        archived = self.state / "messages"
        archived.mkdir()
        for path in self.queue():
            path.rename(archived / path.name)
        self.assertEqual(self.poll()["queued"], 0)
        self.assertEqual(len((self.state / "audit" / "rss.jsonl").read_text().splitlines()), 2)

    def test_failed_queue_write_does_not_checkpoint(self):
        original = rss.atomic_write
        def fail_queue(path, data):
            if path.parent.name == "message_queue":
                raise OSError("disk full")
            original(path, data)
        with patch.object(rss, "atomic_write", side_effect=fail_queue), self.assertRaises(OSError):
            self.poll()
        self.assertFalse(self.index.exists())
        self.assertEqual(self.poll()["queued"], 2)

    def test_304_updates_health_without_requeue(self):
        self.poll()
        before = self.index.read_bytes()
        with patch.object(rss, "fetch_feed", return_value=(None, {})):
            result = rss.poll_feed(FEED, self.state)
        self.assertTrue(result["not_modified"])
        self.assertEqual(result["queued"], 0)
        self.assertEqual(self.index.read_bytes(), before)

    def test_source_failure_does_not_stop_other_feeds(self):
        other = {**FEED, "id": "other"}
        with patch.object(rss, "STATE_DIR", self.state), patch.object(rss, "load_feeds", return_value={"fixture": FEED, "other": other}), \
             patch.object(rss, "poll_feed", side_effect=[OSError("down"), {}]) as poll, patch.object(rss.time, "sleep"), \
             self.assertRaises(SystemExit) as stopped:
            rss.main()
        self.assertEqual(stopped.exception.code, 1)
        self.assertEqual(poll.call_count, 2)

    def test_configuration_optional_unique_and_path_safe(self):
        config = self.state / "feeds.json"
        self.assertEqual(load_feeds(config), {})
        for feeds in ([{**FEED, "id": "../escape"}], [FEED, FEED], [{**FEED, "url": "file:///tmp/feed"}]):
            config.write_text(json.dumps({"feeds": feeds}))
            with self.assertRaises(ValueError):
                load_feeds(config)
        for key, value in (("allow_empty", "yes"), ("repair_html_entities", 1),
                           ("include_title_prefixes", "Job"), ("include_categories", [""])):
            config.write_text(json.dumps({"feeds": [{**FEED, key: value}]}))
            with self.subTest(key=key), self.assertRaises(ValueError):
                load_feeds(config)
        config.write_text(json.dumps({"feeds": [FEED]}))
        self.assertEqual(load_feeds(config)["fixture"]["name"], "Fixture Jobs")


class TestFetch(unittest.TestCase):
    def test_conditional_headers_and_not_modified(self):
        previous = {"url": FEED["url"], "etag": '"v1"', "last_modified": "yesterday", "retrieved_at": "2026-10-17T00:00:00Z", "items": {"1": {}}}
        error = urllib.error.HTTPError(FEED["url"], 304, "Not Modified", {}, None)
        with patch.object(rss.urllib.request, "urlopen", side_effect=error) as fetch:
            self.assertEqual(rss.fetch_feed(FEED["url"], previous), (None, {}))
        headers = dict(fetch.call_args.args[0].header_items())
        self.assertEqual(headers["If-none-match"], '"v1"')
        self.assertEqual(headers["If-modified-since"], "yesterday")

    def test_transient_retries_are_bounded_and_403_is_not_retried(self):
        for status, calls in ((429, 3), (503, 3), (403, 1)):
            error = urllib.error.HTTPError(FEED["url"], status, "failure", {}, None)
            with self.subTest(status=status), patch.object(rss.urllib.request, "urlopen", side_effect=error) as fetch, \
                 patch.object(rss.time, "sleep"), self.assertRaises(urllib.error.HTTPError):
                rss.fetch_feed(FEED["url"], {})
            self.assertEqual(fetch.call_count, calls)

    def test_oversized_feed_is_rejected(self):
        response = MagicMock()
        response.__enter__.return_value = response
        response.read.return_value = b"x" * (rss.MAX_FEED_BYTES + 1)
        with patch.object(rss.urllib.request, "urlopen", return_value=response), self.assertRaises(ValueError):
            rss.fetch_feed(FEED["url"], {})


class TestFeedPipeline(_FeedState):
    def _worker(self, stack):
        for name, value in {"QUEUE_DIR": self.state / "message_queue", "MESSAGES_DIR": self.state / "messages",
                            "AUDIT_FILE": self.state / "audit" / "telegram.jsonl", "RSS_AUDIT_FILE": self.state / "audit" / "rss.jsonl",
                            "CRITERIA_FILE": self.state / "criteria.md"}.items():
            stack.enter_context(patch.object(triage, name, value))
        triage.CRITERIA_FILE.write_text("Evaluate engineering jobs.")
        stack.enter_context(patch.object(triage, "load_feeds", return_value={FEED["id"]: FEED}))
        stack.enter_context(patch.object(triage, "SeenLedger", side_effect=lambda name: SeenLedger(name, seen_dir=self.state / "seen")))
        stack.enter_context(patch.object(triage, "load_env", return_value={"DEEPSEEK_API_KEY": "test", "TELEGRAM_BOT_TOKEN": "test"}))
        stack.enter_context(patch.object(triage, "endpoint_reachable", return_value=True))
        stack.enter_context(patch.object(triage, "_load_direction_small", return_value=""))

    def test_feed_to_triage_delivery_briefme_and_weekly(self):
        self.poll(fixture("atom"))
        queued = json.loads(self.queue()[0].read_text())
        msg_id = queued["meta"]["message_id"]
        with contextlib.ExitStack() as stack:
            self._worker(stack)
            stack.enter_context(patch.object(triage, "call_deepseek", side_effect=[
                '{"decision":"pass_to_pro","reason":"engineering"}',
                '{"decision":"send","reason":"match","message":"A suitable role.","tags":{"role_type":"engineering"}}']))
            stack.enter_context(patch.object(delivery, "STATE_DIR", self.state))
            stack.enter_context(patch.object(delivery, "_get_bot_token", return_value="test"))
            stack.enter_context(patch.object(delivery, "load_delivery_config", return_value={"routes": {"job_match":"telegram"}, "telegram":{"chat_id":"test"}}))
            sender = stack.enter_context(patch.object(delivery, "send_telegram", return_value="77"))
            triage.main()
            self.assertEqual(sender.call_count, 1)
            self.assertIn("https://jobs.example/jobs/44", sender.call_args.args[1])
        self.assertEqual(self.queue(), [])
        self.assertTrue((self.state / "messages" / f"{msg_id}.json").exists())
        with patch.object(bot_commands, "MESSAGES_DIR", self.state / "messages"), \
             patch.object(bot_commands, "REF_MAP_FILE", self.state / "ref-map.jsonl"), \
             patch.object(bot_commands, "AUDIT_DIR", self.state / "audit"):
            self.assertEqual(bot_commands.lookup_original_content("77")[0], queued["content"])
            # Audit fallback works if the content archive is unavailable.
            with patch.object(bot_commands, "MESSAGES_DIR", self.state / "missing"):
                self.assertEqual(bot_commands.lookup_original_content("77")[0], queued["content"])
        with patch.object(weekly_trend, "AUDIT_DIR", self.state / "audit"):
            records = weekly_trend.load_week_records("2000", "2100")
        tagged, sends = weekly_trend.extract_tagged(records)
        self.assertEqual((len(tagged), len(sends)), (1, 1))
        self.assertEqual(sends[0]["feed_id"], "fixture")

    def test_model_outage_retains_queue_for_next_run(self):
        self.poll(fixture("atom"))
        with contextlib.ExitStack() as stack:
            self._worker(stack)
            stack.enter_context(patch.object(triage, "evaluate_message", side_effect=TransportError("offline")))
            with self.assertRaises(SystemExit):
                triage.main()
        self.assertEqual(len(self.queue()), 1)
        self.assertFalse((self.state / "messages").exists())

    def test_disabled_feed_retains_pending_items_without_evaluation(self):
        self.poll(fixture("atom"))
        with contextlib.ExitStack() as stack:
            self._worker(stack)
            stack.enter_context(patch.object(triage, "load_feeds", return_value={"fixture": {**FEED, "enabled": False}}))
            evaluate = stack.enter_context(patch.object(triage, "evaluate_message"))
            triage.main()
            evaluate.assert_not_called()
        self.assertEqual(len(self.queue()), 1)


class TestFeedAudit(_FeedState):
    def test_rss_audit_query_and_failure_health(self):
        project = Path(__file__).resolve().parents[1]
        loader = importlib.machinery.SourceFileLoader("rss_test_audit", str(project / "audit"))
        spec = importlib.util.spec_from_loader(loader.name, loader)
        audit = importlib.util.module_from_spec(spec)
        loader.exec_module(audit)
        self.poll()
        (self.state / "feeds.json").write_text(json.dumps({"feeds": [FEED]}))
        with patch.object(audit, "STATE", str(self.state)), patch.object(audit, "AUDIT_DIR", str(self.state / "audit")), \
             patch.object(audit, "FEEDBACK_FILE", str(self.state / "feedback.jsonl")), \
             patch.object(audit, "REF_MAP_FILE", str(self.state / "ref-map.jsonl")), \
             patch.object(audit.os.path, "expanduser", return_value=str(self.state / "units")):
            with patch("sys.argv", ["audit", "rss", "--content"]), contextlib.redirect_stdout(io.StringIO()) as output:
                audit.main()
            self.assertIn("Python Engineer", output.getvalue())
            self.status.write_text(json.dumps({"ok": False, "at": datetime.now(timezone.utc).isoformat(), "error": "HTTP 403"}))
            with patch("sys.argv", ["audit", "--health"]), contextlib.redirect_stdout(io.StringIO()) as output, \
                 self.assertRaises(SystemExit) as stopped:
                audit.main()
            self.assertEqual(stopped.exception.code, 1)
            self.assertIn("RSS fixture: HTTP 403", output.getvalue())


if __name__ == "__main__":
    unittest.main()
