#!/usr/bin/env python3
"""Opt-in live website evaluation; invoked by website_issue.py after human approval.

Imports the checkout under test, uses real model and delivery calls, and keeps
all runtime state and exact prompts in a private evaluation directory.
"""

import argparse
import hashlib
import json
import os
from pathlib import Path
import sys


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, indent=2, ensure_ascii=False) + "\n")


def records(path):
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


def score(reference, outcomes, errors=()):
    """Errors are not negative decisions. Missing relevant source items are FNs."""
    result = dict(tp=0, fp=0, tn=0, fn=0, errors=list(errors), disagreements=[], cases=[])
    for case in reference["cases"]:
        actual = outcomes.get(case["url"], {"decision": "skip", "stage": "source", "reason": "Not extracted from selected feeds"})
        row = {**case, **actual, "expected": "send" if case["relevant"] else "skip"}
        if actual["decision"] not in ("send", "skip"):
            result["errors"].append(f"{case['url']}: {actual.get('reason', 'unfinished evaluation')}")
        else:
            positive = actual["decision"] == "send"
            result[("t" if positive == case["relevant"] else "f") + ("p" if positive else "n")] += 1
            if positive != case["relevant"]:
                result["disagreements"].append(case["url"])
        result["cases"].append(row)
    result["precision"] = result["tp"] / (result["tp"] + result["fp"]) if result["tp"] + result["fp"] else 0.0
    result["recall"] = result["tp"] / (result["tp"] + result["fn"]) if result["tp"] + result["fn"] else 0.0
    result["passed"] = (not result["errors"] and result["precision"] >= reference["min_precision"]
                        and result["recall"] >= reference["min_recall"])
    return result


def evaluate(checkout, reference, output, credentials, test_chat):
    os.environ["TG_INFRA_DATA_DIR"] = str(output)
    sys.path.insert(0, str(checkout))
    from lib.config import FLASH_MODEL, PRO_MODEL
    from lib.features import DEFAULTS
    from lib.rss_catalog import catalog_choices
    from lib.rss_selection import apply_choices
    from lib.sources import validate_feeds
    import rss_poller
    import it_jobs_triage as triage

    state = output / "state"
    if state.exists():
        raise ValueError("Use a fresh evaluation output directory; old ledgers must not affect results")
    state.mkdir(parents=True, exist_ok=True)
    if not credentials.get("DEEPSEEK_API_KEY") or not credentials.get("TELEGRAM_BOT_TOKEN") or not test_chat:
        raise ValueError("Real evaluation requires a DeepSeek key, Telegram bot token and designated test chat")
    choices = reference["choices"]
    if any(choice not in catalog_choices() for choice in choices):
        raise ValueError("Reference selects a catalogue option not provided by this implementation")
    config = apply_choices({"feeds": [], "rss_selection_reviewed": False},
                           [{"id": choice, "reason": "Human-approved evaluation"} for choice in choices])
    profile = reference["profile"]
    for feed in config["feeds"]:
        feed.update({key: profile[key] for key in ("desired_roles", "acceptable_roles")})
    # Verify the actual setup operations keep the selection and can disable it.
    if apply_choices(config, [{"id": c, "reason": "Repeat setup"} for c in choices])["feeds"] != config["feeds"]:
        raise ValueError("Setup reconfiguration changes the existing website selection")
    if any(f.get("enabled", True) for f in apply_choices(config, [], disable=True)["feeds"]):
        raise ValueError("Setup could not disable the website")
    save(state / "feeds.json", config)
    save(state / "channels.json", {"channels": []})
    save(state / "installation.json", {"version": 1, "features": DEFAULTS, "schedules": {}})
    save(state / "delivery.json", {"routes": {"job_match": "telegram"}, "telegram": {"chat_id": test_chat}})
    (state / "it-jobs-criteria.md").write_text(profile["criteria"])
    save(state / "current-direction.json", {"revision": "website-evaluation",
         "full": profile.get("direction_full", ""), "small": profile.get("direction_small", "")})
    save(output / "configuration.json", {"reference": reference, "feeds": config,
         "models": {"flash": FLASH_MODEL, "pro": PRO_MODEL}, "test_chat": test_chat})
    env_path = state / ".env"
    env_path.write_text("\n".join(f"{k}={credentials[k]}" for k in ("DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN")) + "\n")
    errors, outcomes = [], {}
    original_call = triage.call_deepseek
    original_evaluate = triage.evaluate_message
    request_number = 0
    posting_hash = None

    def traced_evaluation(content, *args, **kwargs):
        nonlocal posting_hash
        posting_hash = hashlib.sha256(content.encode()).hexdigest()
        return original_evaluate(content, *args, **kwargs)

    def traced_call(model, prompt, key, **kwargs):
        nonlocal request_number
        request_number += 1
        # Production Flash is bounded to four calls and Pro to one per posting.
        if request_number > 5 * len(reference["cases"]):
            raise RuntimeError("Evaluation call allowance exhausted")
        entry = {"request": request_number, "posting_sha256": posting_hash, "model": model, "prompt": prompt}
        try:
            entry["response"] = original_call(model, prompt, key, **kwargs)
            return entry["response"]
        except Exception as exc:
            entry["error"] = type(exc).__name__
            raise
        finally:
            with (output / "requests.jsonl").open("a") as stream:
                stream.write(json.dumps(entry, ensure_ascii=False) + "\n")

    triage.call_deepseek = traced_call
    triage.evaluate_message = traced_evaluation
    try:
        for feed in validate_feeds(config).values():
            try:
                rss_poller.poll_feed(feed, state)
            except Exception as exc:
                errors.append(f"Source fetch failed: {type(exc).__name__}: {exc}")
        cases = {case["url"]: case for case in reference["cases"]}
        messages = {}
        outside = output / "outside-sample"
        outside.mkdir()
        for path in sorted((state / "message_queue").glob("*.json")):
            entry = json.loads(path.read_text())
            url = entry["meta"]["url"]
            if url not in cases:
                # Retain inventory evidence without spending on unlabelled jobs.
                path.rename(outside / path.name)
                continue
            if url in messages:
                errors.append(f"Duplicate extraction for sampled URL: {url}")
                path.rename(outside / path.name)
                continue
            messages[url] = entry
        try:
            triage.main()  # Actual queue processing, prompts, ledgers, and delivery.
        except (Exception, SystemExit) as exc:
            errors.append(f"Triage did not complete: {type(exc).__name__}")
        evaluations = {row["msg_id"]: row for row in records(state / "audit/rss.jsonl") if "flash" in row}
        deliveries = records(state / "audit/delivery.jsonl")
        # Delivery audit does not carry the source reference; ref-map does.
        acknowledgements = records(state / "ref-map.jsonl")
        for url, entry in messages.items():
            mid = entry["meta"]["message_id"]
            rec = evaluations.get(mid)
            content = entry["content"]
            extra = {"extracted_text": content, "content_sha256": hashlib.sha256(content.encode()).hexdigest(),
                     "msg_id": mid, "trace": rec}
            if not rec:
                outcomes[url] = {**extra, "decision": "error", "stage": "triage", "reason": "No terminal evaluation"}
                continue
            flash, pro = rec["flash"], rec.get("pro")
            invalid = any(w["decision"] not in ("disqualified", "read_more", "pass_to_pro") for w in flash["windows"])
            if invalid or flash["decision"] not in ("skip", "flag") or (pro and pro["decision"] not in ("send", "skip")):
                outcomes[url] = {**extra, "decision": "error", "stage": "triage", "reason": "Invalid model decision"}
                continue
            decision = pro["decision"] if pro else "skip"
            outcomes[url] = {**extra, "decision": decision, "stage": "pro" if pro else "flash",
                             "reason": (pro or flash)["reason"]}
            if decision == "send" and len([a for a in acknowledgements if a["ref"] == mid]) != 1:
                errors.append(f"Expected exactly one delivery acknowledgement: {url}")
        if any(not row.get("success") or not row.get("platform_id") for row in deliveries):
            errors.append("Delivery failed; inspect state/audit/delivery.jsonl")
        # Run the unchanged processor again: no repeat model calls or messages.
        calls_before, acks_before = request_number, len(acknowledgements)
        if not errors and all(row["decision"] in ("send", "skip") for row in outcomes.values()):
            try:
                triage.main()
            except (Exception, SystemExit):
                errors.append("Repeat triage did not complete")
        if request_number != calls_before or len(records(state / "ref-map.jsonl")) != acks_before:
            errors.append("Repeat triage caused duplicate evaluation or delivery")
        result = score(reference, outcomes, errors)
        result["model_calls"] = request_number
        result["inventory_scope"] = reference["inventory"]
        result["tool_usage"] = "Job triage uses direct model requests; it has no model-selected tools. See requests.jsonl and source observations."
        save(output / "report.json", result)
        return result
    finally:
        triage.call_deepseek = original_call
        triage.evaluate_message = original_evaluate
        env_path.unlink(missing_ok=True)


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--checkout", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--env-file", type=Path, required=True)
    parser.add_argument("--test-chat", required=True)
    parser.add_argument("--approve-live", action="store_true")
    args = parser.parse_args()
    if not args.approve_live:
        parser.error("--approve-live is required: this executes code, spends API credit and sends test messages")
    credentials = {}
    for line in args.env_file.read_text().splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            key, value = line.split("=", 1)
            if key.strip() in ("DEEPSEEK_API_KEY", "TELEGRAM_BOT_TOKEN"):
                credentials[key.strip()] = value.strip()
    evaluate(args.checkout.resolve(), json.loads(args.reference.read_text()), args.output.resolve(), credentials, args.test_chat)


if __name__ == "__main__":
    main()
