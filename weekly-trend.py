#!/usr/bin/env python3
"""Weekly job market trend report — aggregate structured tags, generate analysis, email it."""

import json
import logging
import sys
from collections import Counter
from datetime import datetime, timezone, timedelta
from pathlib import Path

from lib import (PROJECT_DIR, STATE_DIR, PRO_MODEL,
                 load_env, call_deepseek, write_audit, send_email,
                 ensure_valid_token)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)-8s %(message)s",
)

AUDIT_DIR = STATE_DIR / "audit"
WEEKLY_AUDIT_FILE = AUDIT_DIR / "weekly.jsonl"
DRY_RUN = "--dry-run" in sys.argv


def load_profile() -> str:
    """Load Filippos's profile files. Returns empty string if missing."""
    source_dir = PROJECT_DIR / "source"
    parts = []
    for name in ("interests.txt", "skills.txt", "tech_stack.txt"):
        path = source_dir / name
        if path.exists():
            parts.append(path.read_text().strip())
    return "\n\n".join(parts)


def load_week_records() -> list[dict]:
    """Load audit records from the past 7 days that have structured tags."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    records = []
    for audit_file in (AUDIT_DIR / "telegram.jsonl", AUDIT_DIR / "email.jsonl"):
        if not audit_file.exists():
            continue
        with audit_file.open() as f:
            for line in f:
                if not line.strip():
                    continue
                r = json.loads(line)
                if r.get("arrived_at", "") >= cutoff:
                    records.append(r)
    return records


def extract_tagged(records: list[dict]) -> tuple[list[dict], list[dict]]:
    """Return (tagged, sends) — records with pro.tags, and the send-decision subset."""
    tagged = []
    sends = []
    for r in records:
        pro = r.get("pro", {})
        tags = pro.get("tags")
        if not isinstance(tags, dict) or not tags:
            continue
        tagged.append(r)
        if pro.get("decision") == "send":
            sends.append(r)
    return tagged, sends


def aggregate(tagged: list[dict]) -> dict:
    """Aggregate structured tags into counts and sets."""
    role_types = Counter()
    all_skills: set[str] = set()
    all_tech: set[str] = set()
    domains = Counter()
    seniority = Counter()
    remote_count = 0
    onsite_count = 0

    for r in tagged:
        tags = r["pro"]["tags"]
        rt = tags.get("role_type", "")
        if rt:
            role_types[rt] += 1
        for s in tags.get("skills", []):
            if s:
                all_skills.add(s.lower().strip())
        for t in tags.get("tech_stack", []):
            if t:
                all_tech.add(t.lower().strip())
        d = tags.get("domain", "")
        if d:
            domains[d] += 1
        sv = tags.get("seniority", "")
        if sv:
            seniority[sv] += 1
        if tags.get("remote"):
            remote_count += 1
        else:
            onsite_count += 1

    return {
        "total_tagged": len(tagged),
        "role_types": dict(role_types),
        "skills": sorted(all_skills),
        "tech_stack": sorted(all_tech),
        "domains": dict(domains),
        "seniority": dict(seniority),
        "remote": remote_count,
        "onsite": onsite_count,
    }


def build_report(profile: str, agg: dict, sends: list[dict]) -> str:
    """Ask DeepSeek Pro to write a trend report from the aggregation."""
    prompt = (
        "You are a job market analyst. Below is a raw aggregation of structured tags "
        f"from {agg['total_tagged']} job postings and emails evaluated over the past 7 days.\n\n"
        f"--- CANDIDATE PROFILE ---\n{profile}\n\n"
        "--- WEEKLY AGGREGATION ---\n"
        f"Tagged postings: {agg['total_tagged']}\n"
        f"Matches delivered: {len(sends)}\n\n"
        f"Role type distribution: {agg['role_types']}\n\n"
        f"Skills seen ({len(agg['skills'])} unique): {', '.join(agg['skills'])}\n\n"
        f"Tech stack items: {', '.join(agg['tech_stack'])}\n\n"
        f"Domains: {agg['domains']}\n\n"
        f"Seniority distribution: {agg['seniority']}\n\n"
        f"Remote: {agg['remote']} | Onsite/Hybrid: {agg['onsite']}\n\n"
        "Write a concise weekly market trend report (3-5 paragraphs) covering:\n"
        "1. Which skills and technologies are appearing most frequently this week\n"
        "2. Which of these align with the candidate's profile (and which are notable gaps)\n"
        "3. Notable patterns — domains, seniority mix, remote ratio\n"
        "4. Any specific signals worth acting on\n\n"
        "Write in the second person ('You'). Be specific and data-driven. "
        "Keep it under 400 words."
    )
    return call_deepseek(PRO_MODEL, prompt, api_key)


def build_raw_section(agg: dict, sends: list[dict]) -> str:
    """Fallback: plain stats section when Pro is unavailable."""
    lines = [
        "RAW STATS",
        f"Role types: {agg['role_types']}",
        f"Skills seen ({len(agg['skills'])}): {', '.join(agg['skills'])}",
        f"Tech stack: {', '.join(agg['tech_stack'])}",
        f"Domains: {agg['domains']}",
        f"Seniority: {agg['seniority']}",
        f"Remote: {agg['remote']} | Onsite: {agg['onsite']}",
        "",
        f"Recent matches ({len(sends)}):",
    ]
    for s in sends[-5:]:
        tags = s["pro"]["tags"]
        lines.append(f"  - {tags.get('role_title', '?')} ({tags.get('location', '?')})")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main() -> None:
    env = load_env()
    api_key = env.get("DEEPSEEK_API_KEY", "")
    email_to = env.get("OUTLOOK_EMAIL", "")

    if not api_key:
        logging.error("DEEPSEEK_API_KEY not set")
        sys.exit(1)

    records = load_week_records()
    tagged, sends = extract_tagged(records)
    agg = aggregate(tagged)
    week_end = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    week_start = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")

    if agg["total_tagged"] == 0:
        logging.info("no tagged records in the past 7 days — nothing to report")
        write_audit({
            "report_at": datetime.now(timezone.utc).isoformat(),
            "period_start": week_start,
            "period_end": week_end,
            "total_records": len(records),
            "tagged_count": 0,
            "send_count": 0,
            "success": True,
            "note": "no tagged records in window",
        }, WEEKLY_AUDIT_FILE)
        return

    profile = load_profile()
    if not profile:
        logging.warning("source/ profile files not found — report will omit profile context")

    # Generate trend report
    trend_error = None
    try:
        trend_text = build_report(profile, agg, sends)
    except Exception as e:
        logging.error("Pro trend analysis failed: %s", e)
        trend_error = e
        trend_text = None

    # Compose email body
    subject = f"Weekly Job Market Trend Report ({week_start} – {week_end})"
    body_parts = [
        f"Weekly Job Market Intelligence",
        f"Period: {week_start} to {week_end}",
        f"Total postings triaged: {len(records)}",
        f"Postings with structured tags: {agg['total_tagged']}",
        f"Matches delivered to Telegram: {len(sends)}",
        "",
    ]
    if trend_text:
        body_parts.append(trend_text)
    else:
        body_parts.append(f"(Trend analysis unavailable: {trend_error})")
    body_parts.append("")
    body_parts.append(build_raw_section(agg, sends))
    body = "\n".join(body_parts)

    if DRY_RUN:
        print(f"Subject: {subject}")
        print("---")
        print(body)
        print("---")
        print(f"(dry run — not sent. {len(records)} records, {agg['total_tagged']} tagged)")
        return

    if not email_to:
        logging.error("OUTLOOK_EMAIL not set in .env — cannot send report")
        sys.exit(1)

    try:
        token = ensure_valid_token(env)
        send_email(token, email_to, subject, body)
        logging.info("weekly report sent to %s", email_to)
        success = True
        error = None
    except Exception as e:
        logging.error("failed to send weekly report: %s", e)
        success = False
        error = str(e)

    write_audit({
        "report_at": datetime.now(timezone.utc).isoformat(),
        "period_start": week_start,
        "period_end": week_end,
        "total_records": len(records),
        "tagged_count": agg["total_tagged"],
        "send_count": len(sends),
        "success": success,
        "error": error,
    }, WEEKLY_AUDIT_FILE)

    if not success:
        sys.exit(1)


if __name__ == "__main__":
    main()
