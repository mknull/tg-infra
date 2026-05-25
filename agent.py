#!/usr/bin/env python3
"""Agent loop for /briefme — tool-calling with DeepSeek."""

import json
import logging
import urllib.request
from pathlib import Path

from lib import DEEPSEEK_API_URL, PRO_MODEL, PROJECT_DIR
from tools import read_file, web_search, web_fetch, md_to_pdf
from guardrails import RateLimiter, BRIEFS_DIR

# --- Tool definitions (DeepSeek function-calling schema) ---

TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "read_file",
            "description": "Read a file from the project directory. Use to load Filippos's profile (interests, skills, tech stack) or the briefing references.",
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
    source_dir = PROJECT_DIR / "source"

    skill = (skill_dir / "SKILL.md").read_text()
    discipline = (refs_dir / "briefing-discipline.md").read_text()
    relevance = (refs_dir / "profile-for-relevance.md").read_text()

    return f"""You are a job briefing agent. Produce a decision-grade brief for Filippos Panagiotou.

You have tools: read_file (load Filippos's profile), web_search (find funding, team, news, salary data), web_fetch (read specific pages like job listings).

Workflow:
1. Load Filippos's profile with read_file (interests, skills, tech_stack from source/)
2. Search for the company/role — funding, Glassdoor, news, alumni trajectories
3. Fetch the job listing page if a URL was provided
4. Write the brief following the structure below
5. The brief is saved as markdown — do NOT include tool calls or thinking in the brief text itself

## Brief structure
{skill}

## Briefing discipline
{discipline}

## Filippos's career-strategic profile
{relevance}"""


# --- Agent loop ---

MAX_TURNS = 15


def run_agent(job_text: str, api_key: str) -> str:
    """Run the /briefme agent. Returns the brief as markdown, or raises."""

    system_prompt = build_system_prompt()
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": f"Brief me on this job:\n\n{job_text[:12000]}"}
    ]

    rl = RateLimiter()

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

        # --- assistant message may have content, tool_calls, or both ---
        assistant_msg = {"role": "assistant"}
        if msg.get("content"):
            assistant_msg["content"] = msg["content"]
        if msg.get("tool_calls"):
            assistant_msg["tool_calls"] = msg["tool_calls"]

        messages.append(assistant_msg)

        # --- if the model is done, return the content ---
        if finish == "stop" and not msg.get("tool_calls"):
            return msg.get("content", "")

        # --- execute tool calls ---
        if msg.get("tool_calls"):
            for tc in msg["tool_calls"]:
                fn = tc["function"]
                name = fn["name"]
                args = json.loads(fn["arguments"])

                logging.info("agent tool call: %s(%s)", name,
                            json.dumps(args)[:120])

                try:
                    if name == "read_file":
                        result = read_file(args["path"])
                    elif name == "web_search":
                        result = web_search(args["query"])
                    elif name == "web_fetch":
                        result = web_fetch(args["url"], rate_limiter=rl)
                    else:
                        result = f"unknown tool: {name}"
                except Exception as e:
                    result = f"tool error: {e}"

                messages.append({
                    "role": "tool",
                    "tool_call_id": tc["id"],
                    "content": result,
                })

    raise RuntimeError(f"Agent exceeded {MAX_TURNS} turns without finishing")
