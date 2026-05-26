#!/usr/bin/env python3
"""Agent loop for /briefme — tool-calling with DeepSeek."""

import json
import logging
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from lib import DEEPSEEK_API_URL, PRO_MODEL, STATE_DIR, PROJECT_DIR, USER_NAME, write_audit
from tools import read_file, web_search, web_fetch, md_to_pdf
from guardrails import RateLimiter, BRIEFS_DIR

AGENT_AUDIT_FILE = STATE_DIR / "audit" / "agent.jsonl"

# --- Tool definitions (DeepSeek function-calling schema) ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the project directory. Use to load " + USER_NAME + "'s profile (interests, skills, tech stack) or the briefing references.",
            "parameters": {
                "type": "object",
                "properties": {
                    "path": {"type": "string", "description": "Absolute path to the file"}
                },
                "required": ["path"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the web for funding news, team info, Glassdoor reviews, salary data, alumni trajectories, or company reputation. Returns up to 5 results with snippets and URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "Search query string"}
                },
                "required": ["query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "web_fetch",
            "description": "Fetch and read a specific web page. Use for job listings, company career pages, or articles. NOT for search — use web_search first to find URLs.",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "Full URL to fetch"}
                },
                "required": ["url"]
            }
        }
    }
]

# --- System prompt ---

def build_system_prompt() -> str:
    skill_dir = PROJECT_DIR / "skills" / "job-brief"
    refs_dir = skill_dir / "references"

    try:
        skill = (skill_dir / "SKILL.md").read_text()
        discipline = (refs_dir / "briefing-discipline.md").read_text()
        relevance = (refs_dir / "profile-for-relevance.md").read_text()
    except FileNotFoundError:
        skill = _FALLBACK_SKILL
        discipline = _FALLBACK_DISCIPLINE
        relevance = _FALLBACK_PROFILE

    return f"""You are a job briefing agent. Produce a decision-grade brief for {USER_NAME}.

You have tools: read_file (load {USER_NAME}'s profile), web_search (find funding, team, news, salary data), web_fetch (read specific pages like job listings).

Workflow:
1. Load {USER_NAME}'s profile with read_file (interests, skills, tech_stack from source/)
2. Search for the company/role — funding, Glassdoor, news, alumni trajectories
3. Fetch the job listing page if a URL was provided
4. Write the brief following the structure below
5. The brief is saved as markdown — do NOT include tool calls or thinking in the brief text itself

## Brief structure
{skill}

## Briefing discipline
{discipline}

## {USER_NAME}'s career-strategic profile
{relevance}"""


_FALLBACK_SKILL = "Produce a decision-grade brief with role, environment, and relevance sections."
_FALLBACK_DISCIPLINE = "Base every claim on fetched or read data. Do not invent."
_FALLBACK_PROFILE = "Load profile from source/ directory using read_file."


# --- Agent loop ---

MAX_TURNS = 15


def run_agent(job_text: str, api_key: str,
              audit_meta: dict | None = None) -> str:
    """Run the /briefme agent. Returns the brief as markdown, or raises.

    If audit_meta is provided, writes an audit record on completion or failure.
    audit_meta should have: sender, preview.
    """

    system_prompt = build_system_prompt()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Brief me on this job:\n\n{job_text[:12000]}"}
    ]

    rl = RateLimiter()
    tool_log: list[dict] = []
    started_at = time.monotonic()
    error = None

    try:
        for turn in range(MAX_TURNS):
            payload = json.dumps({
                "model": PRO_MODEL,
                "messages": messages,
                "tools": TOOLS,
                "tool_choice": "auto",
            }).encode()

            req = urllib.request.Request(
                DEEPSEEK_API_URL, data=payload,
                headers={
                    "Content-Type": "application/json",
                    "Authorization": f"Bearer {api_key}",
                },
                method="POST",
            )
            with urllib.request.urlopen(req, timeout=180) as resp:
                body = json.loads(resp.read())

            choice = body["choices"][0]
            msg = choice["message"]
            finish = choice.get("finish_reason", "")

            assistant_msg = {"role": "assistant"}
            if msg.get("content"):
                assistant_msg["content"] = msg["content"]
            if msg.get("tool_calls"):
                assistant_msg["tool_calls"] = msg["tool_calls"]

            messages.append(assistant_msg)

            if finish == "stop" and not msg.get("tool_calls"):
                brief = msg.get("content", "")
                _write_agent_audit(audit_meta, brief, tool_log, turn + 1,
                                   started_at, error=None)
                return brief

            if msg.get("tool_calls"):
                for tc in msg["tool_calls"]:
                    fn = tc["function"]
                    name = fn["name"]
                    args = json.loads(fn["arguments"])

                    t0 = time.monotonic()
                    try:
                        if name == "read_file":
                            result = read_file(args["path"])
                            tool_ok = True
                        elif name == "web_search":
                            result = web_search(args["query"])
                            tool_ok = "search error" not in result
                        elif name == "web_fetch":
                            result = web_fetch(args["url"], rate_limiter=rl)
                            tool_ok = "fetch blocked" not in result and "fetch error" not in result
                        else:
                            result = f"unknown tool: {name}"
                            tool_ok = False
                    except Exception as e:
                        result = f"tool error: {e}"
                        tool_ok = False

                    elapsed_ms = int((time.monotonic() - t0) * 1000)
                    tool_log.append({
                        "tool": name,
                        "args": {k: v for k, v in args.items() if k != "path"},
                        "ok": tool_ok,
                        "result_bytes": len(result),
                        "duration_ms": elapsed_ms,
                    })

                    logging.info("agent tool call: %s(%s) → %s (%dms)",
                                name, json.dumps(args)[:80],
                                "ok" if tool_ok else "FAIL", elapsed_ms)

                    messages.append({
                        "role": "tool",
                        "tool_call_id": tc["id"],
                        "content": result,
                    })

        error = f"exceeded {MAX_TURNS} turns without finishing"
        raise RuntimeError(error)

    except Exception as e:
        error = str(e)
        _write_agent_audit(audit_meta, "", tool_log, 0, started_at, error=error)
        raise


def _write_agent_audit(audit_meta: dict | None, brief: str,
                       tool_log: list[dict], turns: int,
                       started_at: float, error: str | None) -> None:
    """Write one audit record for a /briefme agent run."""
    if audit_meta is None:
        return

    record = {
        "msg_id": datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-briefme",
        "source": "briefme",
        "sender": audit_meta.get("sender", "?"),
        "preview": audit_meta.get("preview", "?")[:120],
        "arrived_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "success": error is None and len(brief) > 0,
        "turns": turns,
        "tool_calls": tool_log,
        "tool_errors": sum(1 for t in tool_log if not t["ok"]),
        "brief_bytes": len(brief),
        "duration_s": round(time.monotonic() - started_at, 1),
        "error": error,
    }
    write_audit(record, AGENT_AUDIT_FILE)
