"""Profile-derived Flash preferences and read-only setup readiness checks."""

import json
import logging
from pathlib import Path

from .config import PRO_MODEL

ROLE_FIELDS = ("desired_roles", "acceptable_roles")
PROFILE_FILES = ("interests.txt", "skills.txt", "tech_stack.txt")
PLACEHOLDERS = {
    "desired_roles": {"Roles you want to find here", "Roles you most want to find here",
                      "Paid job vacancies; Pro evaluates fit against the candidate profile"},
    "acceptable_roles": {"Roles you will also consider", "Other paid technical roles"},
}


def source_configs(root: Path, overrides: dict[Path, str] | None = None):
    overrides = overrides or {}
    for filename, collection, identity, kind in (
        ("channels.json", "channels", "username", "telegram"),
        ("feeds.json", "feeds", "id", "rss"),
    ):
        path = root / "state" / filename
        if not path.exists() and path not in overrides:
            continue
        config = json.loads(overrides[path] if path in overrides else path.read_text())
        entries = config[collection]
        if not isinstance(entries, list) or any(not isinstance(e, dict) for e in entries):
            raise ValueError(f"{filename}: {collection} must be a list of objects")
        example = root / (filename + ".example")
        defaults = json.loads(example.read_text())[collection] if example.exists() else []
        seen = set()
        for entry in entries:
            ident = entry.get(identity)
            if not isinstance(ident, str) or not ident.strip() or ident in seen:
                raise ValueError(f"{filename}: missing or duplicate {identity}")
            seen.add(ident)
        yield path, config, entries, {e[identity]: e for e in defaults}, identity, kind


def missing_role_fields(entry: dict, default: dict) -> list[str]:
    return [field for field in ROLE_FIELDS
            if not isinstance(entry.get(field), str) or not entry[field].strip()
            or entry[field].strip() in PLACEHOLDERS[field]
            or entry[field] == default.get(field)]


def matching_gaps(root: Path) -> list[str]:
    """Missing/template preferences prevent a new installation from going live."""
    gaps = []
    for name in PROFILE_FILES:
        path = root / "source" / name
        if not path.exists() or not path.read_text().strip():
            gaps.append(f"source/{name}")
    for name in ("it-jobs-criteria.md", "email-triage-criteria.md"):
        path = root / "state" / name
        if not path.exists() or not path.read_text().strip():
            gaps.append(f"state/{name}")
    try:
        feeds_path = root / "state/feeds.json"
        if feeds_path.exists() and json.loads(feeds_path.read_text()).get("rss_selection_reviewed") is False:
            gaps.append("RSS feed selection needs review")
        for path, _, entries, defaults, identity, _ in source_configs(root):
            for entry in entries:
                if entry.get("enabled", True):
                    fields = missing_role_fields(entry, defaults.get(entry[identity], {}))
                    if fields:
                        gaps.append(f"{path.name}/{entry[identity]}: {', '.join(fields)}")
    except (ValueError, KeyError, TypeError) as exc:
        gaps.append(f"source configuration invalid: {exc}")
    return gaps


def build_flash_updates(root: Path, profile: str, api_key: str, call_model,
                        force: bool = False, config_overrides: dict[Path, str] | None = None) -> dict[Path, str]:
    """One batched Pro request; one correction attempt; no writes until validated."""
    configs = list(source_configs(root, config_overrides))
    targets = {}
    for path, config, entries, defaults, identity, kind in configs:
        for entry in entries:
            if not entry.get("enabled", True):
                continue
            fields = list(ROLE_FIELDS) if force else missing_role_fields(entry, defaults.get(entry[identity], {}))
            if fields:
                targets[f"{kind}:{entry[identity]}"] = (path, entry, fields)
    if not targets:
        return {}
    sources = [{"id": ident, "context": {k: entry[k] for k in
                ("name", "description", "message_format") if k in entry},
                "existing_preferences": {k: entry[k] for k in ROLE_FIELDS
                                         if k not in fields and k in entry}}
               for ident, (_, entry, fields) in targets.items()]
    prompt = (
        "Generate Flash job-filter preferences from the user's evidence and explicit preferences.\n"
        "desired_roles: concise roles directly aligned with their interests.\n"
        "acceptable_roles: plausible alternatives supported by their skills and stated flexibility.\n"
        "Respect explicit exclusions and preferences over inferences from past work. "
        "Do not invent geography, salary, degrees or willingness to change careers. "
        "Source descriptions describe coverage, not what the user wants. "
        "Keep Flash permissive for ambiguous relevant vacancies; Pro evaluates detailed fit.\n\n"
        f"CANDIDATE PROFILE AND PREFERENCES:\n{profile}\n\n"
        f"SOURCES:\n{json.dumps(sources, ensure_ascii=False)}\n\n"
        'Return ONLY JSON: {"sources": [{"id": "exact source id", '
        '"desired_roles": "nonempty text", "acceptable_roles": "nonempty text"}]}. '
        "Return exactly one entry for every supplied ID, no additional IDs or fields."
    )
    for attempt in (1, 2):
        logging.info("Generating Flash preferences (attempt %d/2)", attempt)
        raw = call_model(PRO_MODEL, prompt, api_key)
        try:
            response = json.loads(raw)
            if not isinstance(response, dict) or set(response) != {"sources"} or not isinstance(response["sources"], list):
                raise ValueError("expected an object containing a sources list")
            selected = {}
            for item in response["sources"]:
                if not isinstance(item, dict) or set(item) != {"id", *ROLE_FIELDS}:
                    raise ValueError("each source needs id, desired_roles and acceptable_roles")
                ident = item["id"]
                if not isinstance(ident, str) or ident not in targets or ident in selected:
                    raise ValueError("unknown or duplicate source ID")
                for field in ROLE_FIELDS:
                    value = item[field]
                    if not isinstance(value, str) or not value.strip() or len(value) > 2000:
                        raise ValueError(f"{ident}: {field} must be nonempty text of at most 2000 characters")
                    if value.strip() in PLACEHOLDERS[field]:
                        raise ValueError(f"{ident}: {field} is still a placeholder")
                selected[ident] = item
            if set(selected) != set(targets):
                raise ValueError("response omitted one or more source IDs")
            break
        except (ValueError, TypeError) as exc:
            reason = "invalid JSON" if isinstance(exc, json.JSONDecodeError) else str(exc)
            if attempt == 2:
                raise ValueError(f"Could not generate matching preferences after two attempts: {reason}") from exc
            logging.warning("Flash preferences rejected: %s; requesting one correction", reason)
            prompt += f"\nYour previous response failed validation: {reason}. Return the complete corrected JSON."
    changed = set()
    for ident, (path, entry, fields) in targets.items():
        for field in fields:
            entry[field] = selected[ident][field].strip()
        changed.add(path)
    return {path: json.dumps(config, indent=2, ensure_ascii=False) + "\n"
            for path, config, *_ in configs if path in changed}
