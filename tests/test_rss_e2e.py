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
                        answer = {"decision": "pass_to_pro", "reason": "Engineering vacancy"}
                    elif payload["model"] == "deepseek-v4-pro":
                        answer = {"decision": "send", "reason": "Matches criteria",
                                  "message": "A suitable engineering role.",
                                  "tags": {"role_type": "engineering"}}
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


if __name__ == "__main__":
    unittest.main()
