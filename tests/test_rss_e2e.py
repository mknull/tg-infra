"""Process-level ingestion/triage tests with real HTTP and temporary on-disk state.

Only external endpoints are substituted: a loopback server supplies feeds, model
replies and Telegram acknowledgements. No application processing is mocked.
"""

import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT / "tests" / "fixtures" / "feeds"

# Run each real entry point as __main__ in a fresh interpreter. Redirect the two
# fixed external API endpoints at the transport boundary; reject all other hosts.
RUNNER = """
import runpy, sys, urllib.request
from urllib.parse import urlsplit
import lib.api
base, script, *args = sys.argv[1:]
lib.api.DEEPSEEK_API_URL = base + "/v1/chat/completions"
urllib.request.install_opener(urllib.request.build_opener(urllib.request.ProxyHandler({})))
original = urllib.request.urlopen
def local_only(request, *args, **kwargs):
    url = request.full_url if isinstance(request, urllib.request.Request) else request
    if url.startswith("https://nodesk.co/remote-jobs/"):
        request = urllib.request.Request(base + "/detail")
        response = original(request, *args, **kwargs)
        response.geturl = lambda: url
        return response
    if url == "https://api.telegram.org/botfixture/sendMessage":
        request.full_url = base + "/botfixture/sendMessage"
        url = request.full_url
    if urlsplit(url).netloc != urlsplit(base).netloc:
        raise AssertionError("Unexpected external request: " + url)
    return original(request, *args, **kwargs)
urllib.request.urlopen = local_only
sys.argv = [script, *args]
runpy.run_path(script, run_name="__main__")
"""


class TestRssCLI(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="rss-e2e-")
        self.addCleanup(tmp.cleanup)
        self.project = Path(tmp.name)
        self.state = self.project / "state"
        self.state.mkdir()
        shutil.copytree(PROJECT / "lib", self.project / "lib", ignore=shutil.ignore_patterns("__pycache__"))
        for name in ("rss_poller.py", "it_jobs_triage.py", "audit"):
            shutil.copy2(PROJECT / name, self.project / name)
        (self.project / "runner.py").write_text(RUNNER)
        (self.state / ".env").write_text("DEEPSEEK_API_KEY=fixture\nTELEGRAM_BOT_TOKEN=fixture\n")
        (self.state / "it-jobs-criteria.md").write_text("Match paid engineering jobs.")
        (self.state / "channels.json").write_text('{"channels":[]}')
        (self.state / "delivery.json").write_text(json.dumps({
            "routes": {"job_match": "telegram"}, "telegram": {"chat_id": "fixture-chat"}}))
        self.feeds = {"/feed": (FIXTURES / "atom.xml").read_bytes()}
        self.requests = []
        self.deliveries = []
        self.model_down = False
        self.flash_answer = {"decision": "pass_to_pro", "reason": "Engineering vacancy"}
        self.pro_answer = {"decision": "send", "reason": "Matches criteria",
                           "message": "A suitable engineering role.",
                           "tags": {"role_type": "engineering"}}
        self.conditional = True
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, status, body=b"", **headers):
                self.send_response(status)
                for name, value in headers.items():
                    self.send_header(name, value)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                owner.requests.append(("GET", self.path, dict(self.headers)))
                raw = owner.feeds.get(self.path)
                if raw is None:
                    self.respond(403, b"Forbidden")
                    return
                etag = '"' + hashlib.sha256(raw).hexdigest() + '"'
                if owner.conditional and self.headers.get("If-None-Match") == etag:
                    self.respond(304, ETag=etag)
                else:
                    self.respond(200, raw, ETag=etag)

            def do_POST(self):
                payload = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                owner.requests.append(("POST", self.path, payload))
                if self.path == "/v1/chat/completions":
                    if owner.model_down:
                        self.respond(503, b"Try later")
                        return
                    if payload["model"] == "deepseek-v4-flash":
                        answer = owner.flash_answer
                    elif payload["model"] == "deepseek-v4-pro":
                        answer = owner.pro_answer
                    else:
                        self.respond(400, b"Unexpected model")
                        return
                    reply = {"choices": [{"message": {"content": json.dumps(answer)}}]}
                elif self.path == "/botfixture/sendMessage":
                    owner.deliveries.append(payload)
                    reply = {"ok": True, "result": {"message_id": len(owner.deliveries)}}
                else:
                    self.respond(404)
                    return
                self.respond(200, json.dumps(reply).encode())

        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.base = f"http://127.0.0.1:{server.server_port}"
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()

        def stop_server():
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

        self.addCleanup(stop_server)
        self.configure([{"id": "fixture", "url": self.base + "/feed"}])

    def configure(self, feeds):
        (self.state / "feeds.json").write_text(json.dumps({"feeds": feeds}))

    def run_cli(self, script, *args, code=0):
        result = subprocess.run(
            [sys.executable, str(self.project / "runner.py"), self.base, script, *args],
            cwd=self.project, capture_output=True, text=True, timeout=30,
            env={"PATH": os.defpath, "HOME": str(self.project), "PYTHONIOENCODING": "utf-8"})
        self.assertEqual(result.returncode, code, result.stdout + result.stderr)
        return result.stdout + result.stderr

    def files(self, directory):
        return sorted((self.state / directory).glob("*.json"))

    def records(self, name):
        return [json.loads(line) for line in (self.state / name).read_text().splitlines()]

    def test_feed_to_delivery_rerun_change_and_failure_via_real_processes(self):
        self.run_cli("rss_poller.py")
        first, = self.files("message_queue")
        queued = json.loads(first.read_text())
        self.assertIn("Full description.", queued["content"])
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(self.files("message_queue"), [])
        self.assertEqual(len(self.files("messages")), 1)
        self.assertEqual(self.deliveries[0]["chat_id"], "fixture-chat")
        self.assertIn("https://jobs.example/jobs/44", self.deliveries[0]["text"])
        self.assertEqual(self.records("ref-map.jsonl")[0]["ref"], queued["meta"]["message_id"])
        self.assertEqual(self.records("audit/delivery.jsonl")[0]["success"], True)
        prompts = [body["messages"][0]["content"] for method, path, body in self.requests
                   if method == "POST" and path == "/v1/chat/completions"]
        self.assertEqual(len(prompts), 2)
        self.assertIn("Match paid engineering jobs.", prompts[1])
        self.assertIn("https://jobs.example/apply/44", prompts[1])

        # Each invocation starts fresh: deduplication must survive process restarts
        # and both HTTP 304 and an unchanged HTTP 200 body.
        for conditional in (True, False):
            self.conditional = conditional
            self.run_cli("rss_poller.py")
            self.run_cli("it_jobs_triage.py")
            self.assertEqual(len(self.deliveries), 1)
        gets = [body for method, _, body in self.requests if method == "GET"]
        self.assertIn("If-None-Match", gets[1])
        self.assertEqual(len(self.records("audit/rss.jsonl")), 2)

        self.feeds["/feed"] = self.feeds["/feed"].replace(b"Full description.", b"Full description. Salary increased.")
        self.run_cli("rss_poller.py")
        revised, = self.files("message_queue")
        meta = json.loads(revised.read_text())["meta"]
        self.assertEqual(meta["job_id"], queued["meta"]["job_id"])
        self.assertEqual(meta["change"], "changed")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(len(self.deliveries), 2)
        self.assertEqual(len(self.files("messages")), 2)
        self.run_cli("audit", "--health")

        index = self.state / "rss" / "fixture" / "index.json"
        previous = index.read_bytes()
        self.feeds["/feed"] = b"<html>Verify you are human</html>"
        self.run_cli("rss_poller.py", code=1)
        self.assertEqual(index.read_bytes(), previous)
        self.assertEqual(self.files("message_queue"), [])
        self.assertIn("RSS fixture", self.run_cli("audit", "--health", code=1))

    def test_large_feed_item_reaches_delivery_with_bounded_flash_calls(self):
        from tests.test_rss_subscriptions import xml
        self.feeds["/feed"] = xml("&lt;br/&gt;".join(
            ["Ambiguous vacancy details; responsibilities still to follow."] * 3000 +
            ["LAST REQUIREMENT: fluent Greek"]))
        self.flash_answer = {"decision": "read_more", "reason": "Need more context"}
        self.run_cli("rss_poller.py")
        queued, = self.files("message_queue")
        posting = json.loads(queued.read_text())["content"]
        self.run_cli("it_jobs_triage.py")
        requests = [body for method, path, body in self.requests
                    if method == "POST" and path == "/v1/chat/completions"]
        self.assertEqual([body["model"] for body in requests], ["deepseek-v4-flash"] * 4 + ["deepseek-v4-pro"])
        self.assertIn(posting, requests[-1]["messages"][0]["content"])
        evaluation = self.records("audit/rss.jsonl")[-1]
        self.assertTrue(evaluation["flash"]["windows"][-1]["budget_exhausted"])
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(self.files("message_queue"), [])
        self.run_cli("audit", "--health")
        self.run_cli("rss_poller.py")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(len([1 for method, path, _ in self.requests
                              if method == "POST" and path == "/v1/chat/completions"]), 5)
        self.assertEqual(len(self.deliveries), 1)

    def test_legacy_migration_overlapping_categories_and_future_backfill_via_processes(self):
        self.run_cli("rss_poller.py")
        self.run_cli("it_jobs_triage.py")
        first_ref = self.records("audit/delivery.jsonl")[0]["ref"]
        self.feeds["/summary"] = self.feeds["/feed"].replace(b"Full description.", b"A different category summary.")
        subscriptions = [{"id": "all", "url": self.base + "/feed"},
                         {"id": "category", "url": self.base + "/summary"}]
        self.configure([{"id": "fixture", "subscriptions": subscriptions}])
        self.run_cli("rss_poller.py")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(len(self.deliveries), 1)
        self.assertEqual(self.records("audit/delivery.jsonl")[0]["ref"], first_ref)
        self.assertEqual(len([r for r in self.requests if r[0] == "POST" and r[1] == "/v1/chat/completions"]), 2)
        self.feeds["/summary"] = self.feeds["/summary"].replace(b"category summary.", b"category summary. Salary increased.")
        self.run_cli("rss_poller.py")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(len(self.deliveries), 2)

        # A newly selected subscription's existing inventory is explicitly baselined.
        self.feeds["/new"] = self.feeds["/feed"].replace(b"job:44", b"job:45")
        subscriptions.append({"id": "new", "url": self.base + "/new", "backfill": {"mode": "future"}})
        self.configure([{"id": "fixture", "subscriptions": subscriptions}])
        self.run_cli("rss_poller.py")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(len(self.deliveries), 2)
        self.feeds["/new"] = self.feeds["/new"].replace(b"job:45", b"job:46")
        self.run_cli("rss_poller.py")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(len(self.deliveries), 3)
        self.run_cli("audit", "--health")

    def test_required_detail_outage_recovers_after_feed_window_moves(self):
        from tests.test_rss_subscriptions import xml, EMPTY, DETAIL
        self.feeds["/feed"] = xml(link="https://nodesk.co/remote-jobs/example/")
        self.configure([{"id": "fixture", "subscriptions": [{"id": "design", "url": self.base + "/feed",
                        "allow_empty": True, "enrich": "nodesk"}]}])
        self.run_cli("rss_poller.py", code=1)
        self.assertEqual(self.files("message_queue"), [])
        self.assertIn("DEFERRED", self.run_cli("audit", "--health", code=1))
        self.feeds["/feed"] = EMPTY
        self.feeds["/detail"] = DETAIL
        self.run_cli("rss_poller.py")
        queued, = self.files("message_queue")
        self.assertIn("Cyprus", queued.read_text())
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(len(self.deliveries), 1)
        prompts = [body["messages"][0]["content"] for method, path, body in self.requests
                   if method == "POST" and path == "/v1/chat/completions"]
        self.assertIn("Cyprus", prompts[-1])
        self.run_cli("audit", "--health")
        self.run_cli("rss_poller.py")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(len(self.deliveries), 1)

    def test_failed_source_does_not_block_other_feed_and_model_outage_defers(self):
        self.feeds["/rdf"] = (FIXTURES / "rdf.xml").read_bytes()
        self.configure([{"id": "broken", "url": self.base + "/forbidden"},
                        {"id": "working", "url": self.base + "/rdf"}])
        self.run_cli("rss_poller.py", code=1)
        self.assertEqual(len(self.files("message_queue")), 1)
        self.model_down = True
        self.run_cli("it_jobs_triage.py", code=1)
        self.assertEqual(len(self.files("message_queue")), 1)
        self.assertEqual(self.files("messages"), [])
        self.assertEqual(self.deliveries, [])
        self.model_down = False
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(self.files("message_queue"), [])
        self.assertEqual(len(self.deliveries), 1)

    def test_audit_follows_pending_stalled_and_recovered_message(self):
        self.run_cli("rss_poller.py")
        self.assertIn("1 pending within 1h", self.run_cli("audit", "--health"))
        # Simulate a worker outage older than audit's default one-day window.
        old = (datetime.now(timezone.utc) - timedelta(days=3)).isoformat()
        arrivals = self.records("audit/rss.jsonl")
        arrivals[0]["arrived_at"] = old
        (self.state / "audit/rss.jsonl").write_text(json.dumps(arrivals[0]) + "\n")
        path, = self.files("message_queue")
        entry = json.loads(path.read_text())
        entry["meta"]["ts"] = old
        path.write_text(json.dumps(entry))
        self.assertIn("RSS STALLED", self.run_cli("audit", "--health", code=1))
        self.run_cli("it_jobs_triage.py")
        self.assertIn("1 evaluated", self.run_cli("audit", "--health"))

    def test_audit_catches_send_that_never_attempted_delivery(self):
        self.run_cli("rss_poller.py")
        (self.state / ".env").write_text("DEEPSEEK_API_KEY=fixture\n")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(self.records("audit/rss.jsonl")[-1]["pro"]["decision"], "send")
        self.assertEqual(self.deliveries, [])
        self.assertIn("RSS DELIVERY MISSING", self.run_cli("audit", "--health", code=1))

    def test_audit_catches_pro_dead_letter(self):
        self.pro_answer = "not a decision object"
        self.run_cli("rss_poller.py")
        self.run_cli("it_jobs_triage.py")
        self.assertEqual(self.records("audit/rss.jsonl")[-1]["pro"]["decision"], "dead_letter")
        self.assertIn("Pro dead_letter", self.run_cli("audit", "--health", code=1))


if __name__ == "__main__":
    unittest.main()
