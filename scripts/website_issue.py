#!/usr/bin/env python3
"""Human-gated website development. See docs/website-workflow.md."""

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import subprocess
import sys
import tempfile
import time

ROOT = Path(__file__).resolve().parents[1]
CATALOG = "lib/rss_catalog.json"
SETUP_FILES = {"lib/rss_selection.py", "lib/setup_matching.py", "lib/setup_wizard.py"}
READ_TOOLS = {"Read", "Glob", "Grep", "WebFetch", "WebSearch"}


class ScopeViolation(ValueError):
    pass


def command(args, cwd=None, **kwargs):
    return subprocess.run([str(a) for a in args], cwd=cwd, check=True,
                          text=True, capture_output=True, **kwargs).stdout.strip()


def git(repo, *args):
    return command(["git", *args], repo)


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True).encode()).hexdigest()


def read(path):
    return json.loads(Path(path).read_text())


def save(path, value):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")
    temporary.replace(path)


def issue_id(url):
    match = re.fullmatch(r"https://github\.com/([\w.-]+/[\w.-]+)/issues/([1-9]\d*)", url)
    if not match:
        raise ValueError("Use a GitHub issue URL: https://github.com/owner/repo/issues/123")
    return match.groups()


def sync(run):
    """Keep unsent comments on disk; retry explicitly without rerunning development."""
    state = read(run / "run.json")
    for path in sorted((run / "comments").glob("*.md")):
        receipt = path.with_suffix(".sent")
        if receipt.exists():
            continue
        # A previous request may have succeeded despite a lost response.
        comments = json.loads(command(["gh", "issue", "view", state["issue"], "--json", "comments"]))
        marker = path.read_text().splitlines()[0]
        if not any(marker in c["body"] for c in comments["comments"]):
            command(["gh", "issue", "comment", state["issue"], "--body-file", path])
        receipt.write_text("sent\n")


def update(run, status, message):
    state = read(run / "run.json")
    state["status"] = status
    save(run / "run.json", state)
    folder = run / "comments"
    folder.mkdir(exist_ok=True)
    ident = f"{time.time_ns()}-{state['site']}"
    (folder / f"{ident}.md").write_text(
        f"<!-- website-workflow:{ident} -->\n**Website workflow: {status}**\n\n{message}\n")
    sync(run)


def allowed_path(path, site, setup_files):
    p = PurePosixPath(path)
    if p.is_absolute() or str(p) != path or ".." in p.parts or "\\" in path:
        return False
    return (path in setup_files or path == f"tests/test_website_{site}.py"
            or path == f"tests/fixtures/publishers/{site}.xml"
            or path == f"docs/websites/{site}.md"
            or (path.startswith(f"tests/fixtures/websites/{site}/")
                and path != f"tests/fixtures/websites/{site}/reference.json"))


def check_changes(repo, state, changes):
    """Validate the entire proposal before writing even its first file."""
    seen = set()
    for change in changes:
        path = change["path"]
        if path in seen or not allowed_path(path, state["site"], state["setup_files"]):
            raise ScopeViolation(f"Forbidden or duplicate proposed edit: {path}")
        seen.add(path)
        target = repo / path
        if target.is_symlink() or not target.resolve().is_relative_to(repo.resolve()):
            raise ScopeViolation(f"Symlink/path escape: {path}")
        if not isinstance(change.get("content"), str) or not change.get("rationale"):
            raise ValueError(f"{path}: content and stated rationale are required")
        if path not in state["setup_files"] and git(repo, "ls-tree", state["base"], "--", path):
            raise ScopeViolation(f"Website addition would replace an existing file: {path}")


def check_tree(repo, state, before, after):
    """Check each commit, including an edit later reverted by another commit."""
    reference = f"tests/fixtures/websites/{state['site']}/reference.json"
    paths = git(repo, "diff", "--name-only", "--no-renames", before, after).splitlines()
    for path in paths:
        if path not in (CATALOG, reference) and not allowed_path(path, state["site"], state["setup_files"]):
            raise ScopeViolation(f"Commit changes protected file: {path}")
        if path not in (*state["setup_files"], CATALOG) and git(repo, "ls-tree", state["base"], "--", path):
            raise ScopeViolation(f"Addition replaces a pre-existing file: {path}")
        entry = git(repo, "ls-tree", after, "--", path)
        if not entry.startswith("100644 blob "):
            raise ScopeViolation(f"Deletion, executable or symlink is not allowed: {path}")
    original = json.loads(git(repo, "show", f"{state['base']}:{CATALOG}"))
    current = json.loads(git(repo, "show", f"{after}:{CATALOG}"))
    additions = [p for p in current["publishers"] if p["id"] == state["site"]]
    remaining = {**current, "publishers": [p for p in current["publishers"] if p["id"] != state["site"]]}
    if remaining != original or len(additions) != 1:
        raise ScopeViolation("Catalogue must preserve all existing publishers and add exactly this website")
    actual = json.loads(git(repo, "show", f"{after}:{reference}"))
    if digest(actual) != state["reference_sha256"]:
        raise ScopeViolation("Human-reviewed reference labels changed")
    return paths


def check_history(run):
    state = read(run / "run.json")
    repo = run / "checkout"
    if git(repo, "status", "--porcelain"):
        raise ScopeViolation("Checkout has uncommitted changes; refusing to bypass commit checks")
    commits = git(repo, "rev-list", "--reverse", f"{state['base']}..HEAD").splitlines()
    if not commits:
        raise ValueError("No website implementation commit")
    previous = state["base"]
    checked = []
    for commit in commits:
        parents = git(repo, "rev-list", "--parents", "-n", "1", commit).split()[1:]
        if parents != [previous]:
            raise ScopeViolation("Only a linear website branch is supported")
        paths = check_tree(repo, state, previous, commit)
        checked.append({"commit": commit, "paths": paths})
        previous = commit
    save(run / "commits.json", checked)
    return previous


def agent(run, phase, prompt, budget):
    """Read-only agent: proposed changes are data, never executable tool actions."""
    help_text = command(["claude", "--help"])
    for flag in ("--restricted", "--tools", "--strict-mcp-config", "--output-format"):
        if flag not in help_text:
            raise RuntimeError(f"Installed Claude runner lacks required control {flag}")
    tools = ",".join(sorted(READ_TOOLS))
    args = ["claude", "-p", "--restricted", "--tools", tools, "--allowedTools", tools,
            "--permission-mode", "dontAsk", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--setting-sources", "", "--settings", '{"disableAllHooks":true}',
            "--disable-slash-commands", "--no-session-persistence", "--output-format", "stream-json",
            "--verbose", "--max-budget-usd", str(budget),
            "--system-prompt", (ROOT / "docs/website-agent.md").read_text()]
    stamp = str(time.time_ns())
    trace = run / f"{phase}-{stamp}.jsonl"
    error = run / f"{phase}-{stamp}.stderr"
    result = None
    with tempfile.TemporaryFile(mode="w+") as request, trace.open("w") as out, error.open("w") as err:
        request.write(prompt)
        request.seek(0)
        with subprocess.Popen(args, cwd=run / "checkout", stdin=request, stdout=out, stderr=err) as process:
            try:
                deadline, pending = time.monotonic() + 900, ""
                with trace.open() as stream:
                    while True:
                        finished = process.poll() is not None
                        pending += stream.read()
                        lines = pending.split("\n")
                        pending = lines.pop()
                        for line in lines:
                            event = json.loads(line)
                            for block in event.get("message", {}).get("content", []):
                                if isinstance(block, dict) and block.get("type") == "tool_use" and block["name"] not in READ_TOOLS:
                                    save(run / "violation.json", {"event": event, "transcript": trace.name})
                                    raise ScopeViolation(f"Agent attempted forbidden tool: {block['name']}; see {trace.name}")
                            if event.get("type") == "result":
                                result = event
                        if finished:
                            break
                        if time.monotonic() > deadline:
                            raise RuntimeError("Development agent timed out; transcript retained")
                        time.sleep(0.1)
            except BaseException:
                process.kill()
                process.wait()
                raise
    if process.returncode or not result or result.get("is_error"):
        raise RuntimeError(f"Agent did not finish successfully; inspect {trace.name} and {error.name}")
    value = json.loads(result["result"])
    save(run / f"{phase}.json", value)
    return value


def assess(args):
    run = args.run.resolve()
    owner_repo, number = issue_id(args.issue)
    if not re.fullmatch(r"[a-z][a-z0-9_-]{0,40}", args.site):
        raise ValueError("site must be a short lowercase identifier")
    if run.exists():
        raise ValueError("Choose a new run directory; existing evidence is never overwritten")
    remote = git(ROOT, "remote", "get-url", "origin")
    if not remote.removesuffix(".git").endswith(("/" + owner_repo, ":" + owner_repo)):
        raise ValueError("The issue must belong to this checkout's origin repository")
    issue = json.loads(command(["gh", "issue", "view", args.issue, "--json", "url,title,body,comments"]))
    run.mkdir(parents=True, mode=0o700)
    repo = run / "checkout"
    command(["git", "clone", "--no-hardlinks", "--quiet", ROOT, repo])
    base = git(repo, "rev-parse", "HEAD")
    if any(p["id"] == args.site for p in read(repo / CATALOG)["publishers"]):
        raise ValueError("This website identifier is already in the catalogue")
    branch = f"website/{number}-{args.site}"
    git(repo, "switch", "-c", branch)
    git(repo, "remote", "set-url", "origin", remote)
    save(run / "issue.json", issue)
    save(run / "run.json", {"issue": args.issue, "repository": owner_repo, "site": args.site,
                            "branch": branch, "base": base, "status": "assessing", "setup_files": []})
    update(run, "assessing", f"Investigating `{args.site}` and the proposed use. No implementation is approved yet.")
    report = agent(run, "assessment", "ASSESS this issue and all its comments:\n" + json.dumps(issue), args.budget)
    if report["policy"] not in ("permitted", "prohibited", "unclear") or not report.get("evidence"):
        raise ValueError("Assessment must include policy status and linked evidence")
    if report["recommendation"] not in ("accept", "reject", "unresolved"):
        raise ValueError("Invalid assessment recommendation")
    status = "rejected" if report["policy"] == "prohibited" or report["recommendation"] == "reject" else "awaiting assessment approval"
    if status != "rejected" and (report["policy"] == "unclear" or report["recommendation"] == "unresolved"):
        status = "policy unresolved"
    update(run, status, report["summary"] + "\n\nPolicy evidence:\n" +
           "\n".join(f"- {e['url']}: {e['finding']}" for e in report["evidence"]) +
           f"\n\nAssessment SHA256: `{digest(report)}`. A maintainer must review the assessment and reference cases before development.")


def validate_reference(ref, site):
    if not ref.get("inventory") or not ref.get("profile", {}).get("criteria"):
        raise ValueError("Reference needs an independently defined inventory and profile criteria")
    for key in ("desired_roles", "acceptable_roles"):
        if not isinstance(ref["profile"].get(key), str) or not ref["profile"][key].strip():
            raise ValueError(f"Reference profile needs {key}")
    cases = ref["cases"]
    if not 2 <= len(cases) <= 60 or {c["relevant"] for c in cases} != {True, False}:
        raise ValueError("Use 2–60 human-labelled cases including positive and negative examples")
    if len({c["url"] for c in cases}) != len(cases):
        raise ValueError("Reference cases need unique URLs")
    for case in cases:
        if type(case["relevant"]) is not bool or not case.get("reason") or not case.get("reference_text"):
            raise ValueError("Each case needs a boolean label, human reason and actual posting text")
        if not re.match(r"https?://", case["url"]):
            raise ValueError("Each case needs a posting URL")
    for name in ("min_precision", "min_recall"):
        if not isinstance(ref.get(name), (int, float)) or not 0 < ref[name] <= 1:
            raise ValueError(f"Choose {name} between 0 (exclusive) and 1")
    if not ref.get("choices") or any(not c.startswith(site + ":") for c in ref["choices"]):
        raise ValueError("Choose catalogue IDs belonging to this website")


def develop(args):
    run = args.run.resolve()
    state = read(run / "run.json")
    report = read(run / "assessment.json")
    approval = read(args.approval)
    if state["status"] in ("rejected", "accepted and built"):
        raise ValueError("This run is terminal; start a new issue/run")
    if report["policy"] != "permitted" or report["recommendation"] != "accept":
        raise ValueError("Development requires an assessment recommending permitted use")
    if approval.get("assessment_sha256") != digest(report) or approval.get("approve") is not True:
        raise ValueError("Approval must explicitly accept this exact assessment")
    setup_files = approval.get("setup_files", [])
    if not set(setup_files) <= SETUP_FILES:
        raise ValueError("Approval names a protected file, not a supported setup integration file")
    ref = approval["evaluation"]
    validate_reference(ref, state["site"])
    if (run / "approval.json").exists() and read(run / "approval.json") != approval:
        raise ValueError("Approval and reference labels are frozen for this run; changes need a new reviewed run")
    save(run / "approval.json", approval)
    state.update(setup_files=setup_files, reference_sha256=digest(ref))
    save(run / "run.json", state)
    repo = run / "checkout"
    if git(repo, "status", "--porcelain"):
        raise ScopeViolation("Uncommitted changes existed before development")
    if git(repo, "rev-parse", "HEAD") != state["base"]:
        check_history(run)
    update(run, "developing", "Human approval recorded for assessment `" + digest(report) +
           "`. Each proposed commit is checked before publication. Shared pipeline code remains protected.")
    feedback = (run / "verification-summary.md").read_text() if (run / "verification-summary.md").exists() else ""
    if (run / "verification.json").exists():
        evidence = read(run / read(run / "verification.json")["directory"] / "report.json")
        # The developer can diagnose a failure but cannot alter its reference or evaluator.
        feedback += "\n" + json.dumps({"errors": evidence["errors"], "cases": evidence["cases"]})
    proposal = agent(run, "proposal", "DEVELOP one complete commit.\n" + json.dumps({
        "issue": read(run / "issue.json"), "assessment": report, "site": state["site"],
        "setup_files": setup_files, "verification_feedback": feedback}), args.budget)
    if proposal["status"] == "blocked":
        update(run, "rejected", "Cannot implement within the approved boundary: " + proposal["reason"])
        return
    if proposal["status"] != "ready":
        raise ValueError("Proposal status must be ready or blocked")
    check_changes(repo, state, proposal["changes"])
    publisher = proposal["publisher"]
    if publisher["id"] != state["site"]:
        raise ScopeViolation("Proposed catalogue entry belongs to another website")
    catalog = json.loads(git(repo, "show", f"{state['base']}:{CATALOG}"))
    catalog["publishers"].append(publisher)
    required = [f"tests/test_website_{state['site']}.py", f"tests/fixtures/publishers/{state['site']}.xml",
                f"docs/websites/{state['site']}.md"]
    proposed_paths = {c["path"] for c in proposal["changes"]}
    if not all(p in proposed_paths or (repo / p).is_file() for p in required):
        raise ValueError("Addition must include its regression test, publisher fixture and operating instructions")
    # All writes are performed by this driver, never by the development agent.
    for change in proposal["changes"]:
        target = repo / change["path"]
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(change["content"])
    save(repo / CATALOG, catalog)
    reference = f"tests/fixtures/websites/{state['site']}/reference.json"
    save(repo / reference, ref)
    git(repo, "add", "--", CATALOG, reference, *[c["path"] for c in proposal["changes"]])
    tree = git(repo, "write-tree")
    before = git(repo, "rev-parse", "HEAD")
    paths = check_tree(repo, state, before, tree)
    git(repo, "-c", "user.name=Filippos Panagiotou", "-c", "user.email=panagiotou.filippos@outlook.com",
        "commit", "-m", f"Add {state['site']} job source (issue #{issue_id(state['issue'])[1]})")
    sha = check_history(run)
    update(run, "awaiting live verification", f"Commit `{sha}` passed scope checks.\n\nChanged files: " +
           ", ".join(f"`{p}`" for p in paths) + "\n\nReview the patch, then explicitly approve the local tests and paid live evaluation.")


def verify(args):
    run = args.run.resolve()
    state = read(run / "run.json")
    if state["status"] in ("rejected", "accepted and built"):
        raise ValueError("Terminal run")
    sha = check_history(run)
    if not args.approve_live or args.commit != sha:
        raise ValueError("Live verification requires --approve-live and the exact --commit being approved")
    (run / "verification.json").unlink(missing_ok=True)
    destination = run / f"evaluation-{time.time_ns()}"
    destination.mkdir(mode=0o700)
    update(run, "verifying", f"Human approved regression tests and paid live evaluation of `{sha}` on the credentialed machine.")
    repo = run / "checkout"
    # Existing tests sometimes modify project state. Execute only in a disposable copy.
    with tempfile.TemporaryDirectory(prefix="website-tests-") as temporary:
        copy = Path(temporary) / "checkout"
        command(["git", "clone", "--quiet", "--no-hardlinks", repo, copy])
        (copy / "state").mkdir(exist_ok=True)
        (copy / "state/channels.json").write_bytes((copy / "channels.json.example").read_bytes())
        env = {k: v for k, v in os.environ.items() if k in ("PATH", "HOME", "LANG", "LC_ALL", "TZ")}
        result = subprocess.run([sys.executable, "-m", "unittest", "tests.test_rss", "tests.test_rss_e2e",
                                 "tests.test_rss_publishers", "tests.test_rss_selection", "tests.test_system_setup",
                                 f"tests.test_website_{state['site']}"], cwd=copy, env=env,
                                text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=600)
        (destination / "tests.txt").write_text(result.stdout)
        if result.returncode:
            update(run, "verification failed", f"Regression tests failed for `{sha}`. Local evidence: `{destination.name}/tests.txt`. No paid triage run started.")
            (run / "verification-summary.md").write_text(result.stdout)
            return
    command([sys.executable, ROOT / "scripts/website_evaluate.py", "--checkout", repo,
             "--reference", repo / f"tests/fixtures/websites/{state['site']}/reference.json",
             "--output", destination, "--env-file", args.env_file.resolve(), "--test-chat", args.test_chat,
             "--approve-live"])
    if check_history(run) != sha:
        raise ScopeViolation("Checkout changed during verification")
    report = read(destination / "report.json")
    report.update(commit=sha, reference_sha256=state["reference_sha256"], tests_passed=True)
    save(destination / "report.json", report)
    save(run / "verification.json", {"directory": destination.name, "commit": sha})
    summary = (f"Commit `{sha}`: TP={report['tp']}, FP={report['fp']}, TN={report['tn']}, FN={report['fn']}; "
               f"precision={report['precision']:.3f}, recall={report['recall']:.3f}. "
               f"Errors={len(report['errors'])}; disagreements={len(report['disagreements'])}.\n\n"
               f"Local evidence: `{destination.name}/report.json`, `requests.jsonl`, `state/audit/`, and `tests.txt`. "
               "Review disagreements and current posting text before acceptance. Personal prompts remain local.")
    (run / "verification-summary.md").write_text(summary)
    update(run, "awaiting result review" if report["passed"] else "verification failed", summary)


def finish(args):
    run = args.run.resolve()
    if args.decision == "reject":
        update(run, "rejected", args.reason)
        return
    state = read(run / "run.json")
    if state["status"] != "awaiting result review":
        raise ValueError("Acceptance requires a completed, passing verification awaiting review")
    sha = check_history(run)
    verification = read(run / "verification.json")
    report = read(run / verification["directory"] / "report.json")
    if report["commit"] != sha or not report["passed"] or report["reference_sha256"] != state["reference_sha256"]:
        raise ValueError("Acceptance needs passing evidence for this exact commit and reference")
    reviews = read(args.review) if args.review else {}
    if any(not isinstance(reviews.get(url), str) or not reviews[url].strip() for url in report["disagreements"]):
        raise ValueError("Provide --review JSON with a human explanation for every disagreement URL")
    save(run / "human-review.json", {"reason": args.reason, "disagreements": reviews, "commit": sha})
    body = run / "pr.md"
    body.write_text(f"Adds `{state['site']}` through setup and the existing RSS/triage pipeline.\n\n"
                    f"Issue: {state['issue']}\n\n" + (run / "verification-summary.md").read_text() +
                    "\n\nHuman acceptance: " + args.reason + "\n")
    git(run / "checkout", "push", "-u", "origin", state["branch"])
    existing = json.loads(command(["gh", "pr", "list", "--repo", state["repository"], "--head", state["branch"], "--json", "url"]))
    url = existing[0]["url"] if existing else command([
        "gh", "pr", "create", "--repo", state["repository"], "--head", state["branch"],
        "--title", f"Add {state['site']} job source", "--body-file", body])
    update(run, "accepted and built", f"Verified commit `{sha}`: {url}\n\n{args.reason}\n\nReady for repository review and merge; no installation was changed.")


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="action", required=True)
    p = commands.add_parser("assess")
    p.add_argument("issue")
    p.add_argument("--site", required=True)
    p.add_argument("--run", required=True, type=Path)
    p.add_argument("--budget", type=float, default=3)
    for name in ("develop", "verify", "finish", "check", "sync"):
        p = commands.add_parser(name)
        p.add_argument("run", type=Path)
        if name == "develop":
            p.add_argument("--approval", type=Path, required=True)
            p.add_argument("--budget", type=float, default=5)
        if name == "verify":
            p.add_argument("--approve-live", action="store_true")
            p.add_argument("--commit", required=True)
            p.add_argument("--env-file", type=Path, required=True)
            p.add_argument("--test-chat", required=True)
        if name == "finish":
            p.add_argument("decision", choices=("accept", "reject"))
            p.add_argument("--reason", required=True)
            p.add_argument("--review", type=Path)
    args = parser.parse_args()
    if hasattr(args, "budget") and not 0 < args.budget < float("inf"):
        parser.error("--budget must be a positive finite dollar amount")
    try:
        if args.action == "check":
            print(check_history(args.run.resolve()))
        elif args.action == "sync":
            sync(args.run.resolve())
        else:
            globals()[args.action](args)
    except ScopeViolation as exc:
        run = args.run.resolve()
        evidence = read(run / "violation.json") if (run / "violation.json").exists() else {}
        save(run / "violation.json", {**evidence, "reason": str(exc), "proposal": "proposal.json", "transcripts": "*-*.jsonl"})
        update(run, "rejected", f"Scope violation: {exc}. Proposed changes and stated rationale retained in the local run evidence.")
        print(str(exc), file=sys.stderr)
        return 1
    except (ValueError, KeyError, TypeError, OSError, RuntimeError, subprocess.SubprocessError) as exc:
        # An infrastructure/JSON/credential failure is not a verdict on the website.
        if ((args.run / "run.json").exists() and args.action not in ("sync", "check")
                and read(args.run / "run.json")["status"] in ("assessing", "developing", "verifying")):
            try:
                update(args.run.resolve(), "blocked", f"The `{args.action}` phase stopped ({type(exc).__name__}). Inspect retained local evidence; this is not a website rejection or acceptance.")
            except (OSError, subprocess.SubprocessError):
                pass  # update saved the comment before attempting network delivery
        print(f"Workflow stopped without acceptance: {exc}. Evidence retained; use sync to retry issue updates.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
