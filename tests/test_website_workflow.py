"""Workflow behaviour with real Git/processes and local external-service doubles."""

import argparse
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import patch

from scripts import website_issue as workflow
from scripts.website_evaluate import score

ROOT = Path(__file__).resolve().parents[1]


def reference():
    return {"inventory": "Four independently reviewed listing-page postings",
            "choices": ["examplejobs:all"], "min_precision": 0.5, "min_recall": 0.5,
            "profile": {"desired_roles": "Paid software jobs", "acceptable_roles": "Infrastructure jobs",
                        "criteria": "Match paid software roles", "direction_full": "Include research",
                        "direction_small": "Include research"},
            "cases": [{"url": f"https://jobs.example/{name}", "relevant": relevant,
                       "reason": "Reviewed by human", "reference_text": f"Actual {name} posting"}
                      for name, relevant in (("GOOD", True), ("FALSEPOS", False), ("NO", False), ("MISSING", True))]}


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.repo = self.root / "repository"
        (self.repo / "lib").mkdir(parents=True)
        (self.repo / "core.py").write_text("original = True\n")
        workflow.save(self.repo / workflow.CATALOG, {"publishers": [{"id": "existing", "name": "Existing"}]})
        workflow.git(self.repo, "init", "-b", "master")
        workflow.git(self.repo, "config", "user.name", "Test")
        workflow.git(self.repo, "config", "user.email", "test@example.org")
        workflow.git(self.repo, "add", ".")
        workflow.git(self.repo, "commit", "-m", "Base")
        self.base = workflow.git(self.repo, "rev-parse", "HEAD")
        self.state = {"base": self.base, "site": "examplejobs", "setup_files": [],
                      "reference_sha256": workflow.digest(reference()), "status": "developing",
                      "issue": "https://github.com/example/repo/issues/1", "repository": "example/repo", "branch": "website/1-examplejobs"}

    def commit_addition(self):
        workflow.save(self.repo / workflow.CATALOG, {"publishers": [
            {"id": "existing", "name": "Existing"}, {"id": "examplejobs", "name": "Example"}]})
        workflow.save(self.repo / "tests/fixtures/websites/examplejobs/reference.json", reference())
        workflow.git(self.repo, "add", ".")
        workflow.git(self.repo, "commit", "-m", "Website")

    def test_proposal_is_rejected_before_any_write_with_rationale_retained(self):
        changes = [{"path": "tests/test_website_examplejobs.py", "content": "pass\n", "rationale": "Source test"},
                   {"path": "core.py", "content": "bad = True\n", "rationale": "Would make the test pass"}]
        with self.assertRaises(workflow.ScopeViolation):
            workflow.check_changes(self.repo, self.state, changes)
        self.assertEqual((self.repo / "core.py").read_text(), "original = True\n")
        self.assertFalse((self.repo / "tests").exists())

    def test_traversal_symlinks_and_reference_edits_are_forbidden(self):
        for path in ("../core.py", "/tmp/escape.py", "tests/fixtures/websites/examplejobs/../x",
                     "tests/fixtures/websites/examplejobs/reference.json", ".git/config", "lib/api.py"):
            with self.subTest(path=path), self.assertRaises(workflow.ScopeViolation):
                workflow.check_changes(self.repo, self.state, [{"path": path, "content": "x", "rationale": "x"}])
        target = self.repo / "tests/fixtures/websites/examplejobs"
        target.parent.mkdir(parents=True)
        target.symlink_to(self.root, target_is_directory=True)
        with self.assertRaises(workflow.ScopeViolation):
            workflow.check_changes(self.repo, self.state, [{"path": str(target.relative_to(self.repo)) + "/escape", "content": "x", "rationale": "x"}])

    def test_each_commit_is_checked_even_when_forbidden_change_is_reverted(self):
        self.commit_addition()
        (self.repo / "core.py").write_text("changed = True\n")
        workflow.git(self.repo, "commit", "-am", "Unrelated change")
        (self.repo / "core.py").write_text("original = True\n")
        workflow.git(self.repo, "commit", "-am", "Revert unrelated change")
        self.assertNotIn("core.py", workflow.git(self.repo, "diff", "--name-only", self.base, "HEAD"))
        run = self.root / "run"
        run.mkdir()
        self.repo.rename(run / "checkout")
        workflow.save(run / "run.json", self.state)
        with self.assertRaisesRegex(workflow.ScopeViolation, "core.py"):
            workflow.check_history(run)

    def test_catalogue_cannot_change_existing_publishers_or_reference_labels(self):
        self.commit_addition()
        head = workflow.git(self.repo, "rev-parse", "HEAD")
        workflow.check_tree(self.repo, self.state, self.base, head)
        catalog = workflow.read(self.repo / workflow.CATALOG)
        catalog["publishers"][0]["name"] = "Changed"
        workflow.save(self.repo / workflow.CATALOG, catalog)
        workflow.git(self.repo, "commit", "-am", "Change existing publisher")
        with self.assertRaisesRegex(workflow.ScopeViolation, "existing publishers"):
            workflow.check_tree(self.repo, self.state, head, "HEAD")

    def test_scope_check_rejects_a_deleted_fixture(self):
        self.commit_addition()
        fixture = self.repo / "tests/fixtures/websites/examplejobs/post.txt"
        fixture.write_text("Actual text")
        workflow.git(self.repo, "add", ".")
        workflow.git(self.repo, "commit", "-m", "Fixture")
        before = workflow.git(self.repo, "rev-parse", "HEAD")
        fixture.unlink()
        workflow.git(self.repo, "commit", "-am", "Remove fixture")
        with self.assertRaisesRegex(workflow.ScopeViolation, "Deletion"):
            workflow.check_tree(self.repo, self.state, before, "HEAD")

    def test_pending_comment_sync_is_idempotent_after_lost_response(self):
        run = self.root / "run"
        workflow.save(run / "run.json", self.state)
        (run / "comments").mkdir()
        body = "<!-- website-workflow:one -->\nAlready sent"
        (run / "comments/one.md").write_text(body)
        with patch.object(workflow, "command", return_value=json.dumps({"comments": [{"body": body}]})) as command:
            workflow.sync(run)
            workflow.sync(run)
        self.assertEqual(command.call_count, 1)
        self.assertTrue((run / "comments/one.sent").exists())

    def test_live_gate_precedes_credentials_tests_and_paid_calls(self):
        run = self.root / "run"
        workflow.save(run / "run.json", self.state)
        args = argparse.Namespace(run=run, approve_live=False, commit="abc")
        with patch.object(workflow, "check_history", return_value="abc"), patch.object(workflow, "command") as command:
            with self.assertRaisesRegex(ValueError, "approve-live"):
                workflow.verify(args)
            command.assert_not_called()

    def test_error_is_not_counted_as_correct_rejection(self):
        ref = reference()
        outcomes = {c["url"]: {"decision": "send" if c["relevant"] else "skip"} for c in ref["cases"]}
        outcomes[ref["cases"][1]["url"]] = {"decision": "error", "reason": "invalid model JSON"}
        result = score(ref, outcomes)
        self.assertFalse(result["passed"])
        self.assertEqual(result["tn"], 1)
        self.assertEqual(len(result["errors"]), 1)

    def test_reference_requires_positive_and_negative_actual_posts(self):
        ref = reference()
        workflow.validate_reference(ref, "examplejobs")
        for case in ref["cases"]:
            case["relevant"] = True
        with self.assertRaises(ValueError):
            workflow.validate_reference(ref, "examplejobs")

    def test_acceptance_requires_current_passing_evidence_and_disagreement_review(self):
        run = self.root / "run"
        state = {**self.state, "status": "awaiting result review"}
        workflow.save(run / "run.json", state)
        workflow.save(run / "verification.json", {"directory": "evaluation-1"})
        report = {"commit": "old", "passed": True, "reference_sha256": state["reference_sha256"],
                  "disagreements": ["https://jobs.example/FALSEPOS"]}
        workflow.save(run / "evaluation-1/report.json", report)
        args = argparse.Namespace(run=run, decision="accept", reason="Reviewed", review=None)
        with patch.object(workflow, "check_history", return_value="new"), patch.object(workflow, "command") as command:
            with self.assertRaisesRegex(ValueError, "exact commit"):
                workflow.finish(args)
            report["commit"] = "new"
            workflow.save(run / "evaluation-1/report.json", report)
            with self.assertRaisesRegex(ValueError, "every disagreement"):
                workflow.finish(args)
            command.assert_not_called()

    def test_rejection_is_terminal_even_after_a_failed_retry(self):
        run = self.root / "run"
        workflow.save(run / "run.json", {**self.state, "status": "rejected"})
        workflow.save(run / "assessment.json", {})
        approval = self.root / "approval.json"
        workflow.save(approval, {})
        with patch.object(sys, "argv", ["workflow", "develop", str(run), "--approval", str(approval)]), patch.object(workflow, "sync"):
            self.assertEqual(workflow.main(), 1)
        self.assertEqual(workflow.read(run / "run.json")["status"], "rejected")

    def test_acceptance_publishes_only_the_verified_branch_and_links_pr(self):
        run = self.root / "run"
        state = {**self.state, "status": "awaiting result review"}
        workflow.save(run / "run.json", state)
        workflow.save(run / "verification.json", {"directory": "evaluation-1"})
        workflow.save(run / "evaluation-1/report.json", {"commit": "verified", "passed": True,
                      "reference_sha256": state["reference_sha256"], "disagreements": []})
        (run / "verification-summary.md").write_text("Verified source, models and delivery")
        args = argparse.Namespace(run=run, decision="accept", reason="Reviewed actual postings", review=None)
        with patch.object(workflow, "check_history", return_value="verified"), patch.object(workflow, "git") as git, \
             patch.object(workflow, "command", return_value='[{"url":"https://github.com/example/repo/pull/2"}]'), \
             patch.object(workflow, "update") as update:
            workflow.finish(args)
        git.assert_called_once_with(run / "checkout", "push", "-u", "origin", state["branch"])
        self.assertEqual(update.call_args.args[1], "accepted and built")
        self.assertIn("pull/2", update.call_args.args[2])


class WorkflowProcessTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("claude"), "Optional development runner is not installed")
    def test_installed_runner_blocks_and_records_unavailable_write_tool(self):
        # This uses a dummy key and a local fake Anthropic endpoint, never a paid provider.
        result = subprocess.run([sys.executable, ROOT / "tests/fixtures/website_runner_probe.py"],
                                capture_output=True, text=True, timeout=60)
        if "PermissionError" in result.stderr:
            self.skipTest("Loopback binding unavailable")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("forbidden_file_exists: False", result.stdout)
        self.assertIn("recorded_tool_attempts: ['Write']", result.stdout)

    def test_issue_assessment_approval_development_and_commit_checks(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            repo, binaries = root / "repo", root / "bin"
            binaries.mkdir()
            (repo / "scripts").mkdir(parents=True)
            (repo / "docs").mkdir()
            shutil.copy2(ROOT / "scripts/website_issue.py", repo / "scripts/website_issue.py")
            shutil.copy2(ROOT / "docs/website-agent.md", repo / "docs/website-agent.md")
            workflow.save(repo / workflow.CATALOG, {"publishers": []})
            workflow.git(repo, "init", "-b", "master")
            workflow.git(repo, "config", "user.name", "Test")
            workflow.git(repo, "config", "user.email", "test@example.org")
            workflow.git(repo, "remote", "add", "origin", "https://github.com/example/repo.git")
            workflow.git(repo, "add", ".")
            workflow.git(repo, "commit", "-m", "Base")
            fake = '''import json, os, sys
from pathlib import Path
args = sys.argv[1:]
if Path(sys.argv[0]).name == "gh":
    path = Path(os.environ["FAKE_COMMENTS"])
    comments = json.loads(path.read_text()) if path.exists() else []
    if "view" in args:
        print(json.dumps({"title": "Recommend Example", "body": "Use native RSS", "comments": comments}))
    elif "comment" in args:
        comments.append({"body": Path(args[args.index("--body-file") + 1]).read_text()})
        path.write_text(json.dumps(comments))
else:
    if "--help" in args:
        print("--restricted --tools --strict-mcp-config --output-format")
    else:
        assert args[args.index("--tools") + 1] == "Glob,Grep,Read,WebFetch,WebSearch"
        prompt = sys.stdin.read()
        if prompt.startswith("ASSESS"):
            value = {"policy": "permitted", "recommendation": "accept", "evidence": [{"url": "https://example.org/policy", "finding": "Allows feed reuse"}], "summary": "Reviewed policy, volume and filtering"}
            value["policy"] = os.environ.get("FAKE_POLICY", "permitted")
        else:
            value = {"status": "ready", "reason": "Catalogue suffices", "publisher": {"id": "examplejobs", "name": "Example"}, "changes": [
                {"path": "tests/test_website_examplejobs.py", "content": "# source tests\\n", "rationale": "Regression coverage"},
                {"path": "tests/fixtures/publishers/examplejobs.xml", "content": "<rss/>", "rationale": "Feed fixture"},
                {"path": "docs/websites/examplejobs.md", "content": "Use setup catalogue", "rationale": "Instructions"}]}
            if os.environ.get("FAKE_BAD_EDIT"):
                value["changes"].append({"path":"lib/api.py", "content":"unrelated = True", "rationale":"Changing shared API would make my test pass"})
        print(json.dumps({"type": "result", "result": json.dumps(value)}))
'''
            for name in ("gh", "claude"):
                path = binaries / name
                path.write_text(f"#!{sys.executable}\n" + fake)
                path.chmod(0o755)
            env = {**os.environ, "PATH": str(binaries) + os.pathsep + os.environ["PATH"],
                   "FAKE_COMMENTS": str(root / "comments.json")}
            run = root / "run"
            cli = [sys.executable, repo / "scripts/website_issue.py"]
            result = subprocess.run([*cli, "assess", "https://github.com/example/repo/issues/123", "--site", "examplejobs", "--run", run], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(workflow.read(run / "run.json")["status"], "awaiting assessment approval")
            approval = root / "approval.json"
            workflow.save(approval, {"approve": True, "assessment_sha256": workflow.digest(workflow.read(run / "assessment.json")), "setup_files": [], "evaluation": reference()})
            result = subprocess.run([*cli, "develop", run, "--approval", approval], env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(workflow.read(run / "run.json")["status"], "awaiting live verification")
            self.assertEqual(len(workflow.read(run / "commits.json")), 1)
            self.assertGreaterEqual(len(workflow.read(root / "comments.json")), 4)
            self.assertFalse((repo / "tests").exists(), "Source checkout must remain untouched")
            rejected = root / "policy-rejected"
            result = subprocess.run([*cli, "assess", "https://github.com/example/repo/issues/123", "--site", "examplejobs", "--run", rejected],
                                    env={**env, "FAKE_POLICY": "prohibited"}, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(workflow.read(rejected / "run.json")["status"], "rejected")
            # A later proposal cannot hide its attempted shared-code edit.
            result = subprocess.run([*cli, "develop", run, "--approval", approval],
                                    env={**env, "FAKE_BAD_EDIT": "1"}, capture_output=True, text=True)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(workflow.read(run / "run.json")["status"], "rejected")
            self.assertIn("lib/api.py", workflow.read(run / "violation.json")["reason"])
            self.assertIn("make my test pass", (run / "proposal.json").read_text())
            self.assertEqual(len(workflow.read(run / "commits.json")), 1)


class LiveEvaluationProcessTests(unittest.TestCase):
    def test_real_pipeline_reports_confusion_matrix_traces_and_delivery(self):
        requests, deliveries = [], []

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def respond(self, data, kind="application/json"):
                self.send_response(200)
                self.send_header("Content-Type", kind)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                body = '<rss version="2.0"><channel><title>Jobs</title><link>https://jobs.example</link><description>Jobs</description>'
                for name in ("GOOD", "FALSEPOS", "NO"):
                    body += f'<item><guid>{name}</guid><title>{name} vacancy</title><link>https://jobs.example/{name}</link><description>Paid software role open worldwide with detailed requirements.</description></item>'
                self.respond((body + "</channel></rss>").encode(), "application/rss+xml")

            def do_POST(self):
                value = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                if self.path == "/telegram":
                    deliveries.append(value)
                    self.respond(json.dumps({"ok": True, "result": {"message_id": len(deliveries)}}).encode())
                    return
                requests.append(value)
                prompt = value["messages"][0]["content"]
                if "3-line window" in prompt:
                    answer = {"decision": "disqualified" if "NO vacancy" in prompt else "pass_to_pro", "reason": "Screening evidence"}
                else:
                    answer = {"decision": "send", "reason": "Profile fit", "message": "A relevant job", "tags": {}}
                self.respond(json.dumps({"choices": [{"message": {"content": json.dumps(answer)}}]}).encode())

        try:
            server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        except PermissionError:
            self.skipTest("Loopback binding unavailable")
        self.addCleanup(server.server_close)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        self.addCleanup(server.shutdown)
        base = f"http://127.0.0.1:{server.server_port}"
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            workflow.save(root / "reference.json", reference())
            (root / "private.env").write_text("DEEPSEEK_API_KEY=fixture\nTELEGRAM_BOT_TOKEN=fixture\n")
            runner = root / "runner.py"
            runner.write_text('''import os, runpy, sys, urllib.request
from pathlib import Path
repo, base, output, reference, credentials = sys.argv[1:]
os.environ["TG_INFRA_DATA_DIR"] = output
sys.path.insert(0, repo)
import lib.api, lib.rss_catalog
lib.api.DEEPSEEK_API_URL = base + "/model"
publisher = {"id": "examplejobs", "name": "Example", "coverage": "Fixture", "defaults": {"poll_interval_hours": 1}, "options": [{"id": "all", "label": "All", "coverage": "All", "url": base + "/feed"}]}
lib.rss_catalog.load_catalog = lambda: {"examplejobs": publisher}
original = urllib.request.urlopen
def local(request, *args, **kwargs):
    url = request.full_url if hasattr(request, "full_url") else request
    if url == "https://api.telegram.org/botfixture/sendMessage":
        request.full_url = base + "/telegram"
        url = request.full_url
    if not url.startswith(base + "/"):
        raise AssertionError("Unexpected external access: " + url)
    return original(request, *args, **kwargs)
urllib.request.urlopen = local
sys.argv = ["website_evaluate.py", "--checkout", repo, "--reference", reference, "--output", output, "--env-file", credentials, "--test-chat", "fixture-chat", "--approve-live"]
runpy.run_path(str(Path(repo) / "scripts/website_evaluate.py"), run_name="__main__")
''')
            result = subprocess.run([sys.executable, runner, ROOT, base, root / "output", root / "reference.json", root / "private.env"], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            report = workflow.read(root / "output/report.json")
            self.assertEqual([report[k] for k in ("tp", "fp", "tn", "fn")], [1, 1, 1, 1])
            self.assertTrue(report["passed"], report)
            self.assertEqual(len(deliveries), 2)
            self.assertEqual(len(requests), 5)
            self.assertTrue(all(d["chat_id"] == "fixture-chat" for d in deliveries))
            traces = (root / "output/requests.jsonl").read_text()
            self.assertIn("Include research", traces)
            self.assertIn("Screening evidence", traces)
            self.assertFalse((root / "output/state/.env").exists())
            missing = next(c for c in report["cases"] if c["url"].endswith("MISSING"))
            self.assertEqual(missing["stage"], "source")


if __name__ == "__main__":
    unittest.main()
