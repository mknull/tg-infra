"""Reviewed setup choices from a fixed catalogue; no selection during polling."""

import copy
import json
import logging
from pathlib import Path

from .api import TransportError
from .config import PRO_MODEL
from .rss_catalog import catalog_choices, load_catalog
from .sources import feed_subscriptions, validate_feeds


class SelectionDeferred(Exception):
    pass


def read_config(root: Path) -> dict:
    path = root / "state/feeds.json"
    return json.loads(path.read_text()) if path.exists() else {"feeds": [], "rss_selection_reviewed": False}


def choose_mode(root, ask) -> str:
    reviewed = read_config(root).get("rss_selection_reviewed", True)
    choices = {"r": "recommend", "m": "manual", "d": "disable", "l": "later"}
    if reviewed:
        choices["k"] = "keep"
    prefix = "[K]eep RSS choices / " if reviewed else "RSS feeds: "
    mode = ask(prefix + "[R]ecommend from my preferences / Choose [M]yself / [D]isable RSS / [L]ater "
               + ("[K]: " if reviewed else "[R]: "), choices, "k" if reviewed else "r")
    if mode == "later":
        raise SelectionDeferred()
    return mode


def recommend(profile: str, preferences: str, config: dict, api_key: str, call_model) -> list[dict]:
    choices = catalog_choices()
    disabled = {entry["id"] for entry in config["feeds"] if not entry.get("enabled", True)} if config.get("rss_selection_reviewed", True) else set()
    available = {ident: value for ident, value in choices.items() if value["publisher_id"] not in disabled}
    prompt = (
        "Select RSS job feeds from the supplied catalogue for this user's current interests. "
        "Explicit preferences override past work. Include plausible alternative roles; do not assume engineering. "
        "Use native categories when they cover the user's interests. Consider each feed's coverage caveats: "
        "broad and category feeds can have different retained windows. Do not infer location eligibility "
        "from the word remote. Keep ambiguity for the job-matching models. "
        "Previously disabled publishers are unavailable; custom URLs are preserved separately. "
        "An empty selection is valid. Choose only supplied IDs; never invent URLs or filters.\n\n"
        f"PROFILE:\n{profile}\n\nCURRENT PREFERENCES:\n{preferences}\n\nCATALOGUE:\n" +
        json.dumps([{k: value[k] for k in ("publisher_name", "label", "coverage", "publisher_coverage")} | {"id": ident}
                    for ident, value in available.items()], ensure_ascii=False) +
        '\n\nReturn ONLY JSON: {"choices": [{"id": "exact catalogue ID", "reason": "short explanation"}]}. '
        "No additional fields or duplicate IDs."
    )
    for attempt in (1, 2):
        raw = call_model(PRO_MODEL, prompt, api_key)
        try:
            value = json.loads(raw)
            if not isinstance(value, dict) or set(value) != {"choices"} or not isinstance(value["choices"], list):
                raise ValueError("expected a choices list")
            selected, urls = set(), set()
            for choice in value["choices"]:
                if not isinstance(choice, dict) or set(choice) != {"id", "reason"}:
                    raise ValueError("each choice requires only id and reason")
                ident = choice["id"]
                if not isinstance(ident, str) or ident not in available or ident in selected:
                    raise ValueError("unknown, disabled or duplicate catalogue ID")
                reason = choice["reason"]
                if not isinstance(reason, str) or not reason.strip() or len(reason) > 500:
                    raise ValueError("each choice needs a reason of 1–500 characters")
                url = (available[ident]["publisher_id"], available[ident]["url"])
                if url in urls:
                    raise ValueError("choices resolve to the same subscription")
                selected.add(ident); urls.add(url)
            return value["choices"]
        except (ValueError, TypeError) as exc:
            detail = "invalid JSON" if isinstance(exc, json.JSONDecodeError) else str(exc)
            logging.warning("RSS recommendation failed validation (attempt %d/2): %s", attempt, detail)
            prompt += f"\nPrevious response failed validation: {detail}. Return a corrected JSON object."
    raise ValueError(f"RSS recommendation invalid after two attempts: {detail}")


def manual_choices() -> list[dict]:
    choices = catalog_choices()
    for pid, publisher in load_catalog().items():
        print(f"\n{publisher['name']}: {publisher['coverage']}")
        print("  " + ", ".join(f"{pid}:{o['id']} ({o['label']})" for o in publisher["options"]))
    print("Custom URLs are preserved. Disabled publishers can be re-enabled by selecting their IDs here.")
    while True:
        ids = input("Feed IDs, separated by commas (none for no catalogue feeds): ").strip()
        selected = [] if ids.lower() == "none" else [part.strip() for part in ids.split(",")]
        if len(selected) == len(set(selected)) and all(ident in choices for ident in selected):
            return [{"id": ident, "reason": "Selected manually"} for ident in selected]
        print("Choose only listed IDs, without duplicates, or enter none.")


def apply_choices(config: dict, selected: list[dict], *, disable: bool = False) -> dict:
    result = copy.deepcopy(config)
    if disable:
        for entry in result["feeds"]:
            entry["enabled"] = False
    else:
        catalog, choices = load_catalog(), catalog_choices()
        requested = {choice["id"]: choice["reason"] for choice in selected}
        entries = {entry["id"]: entry for entry in result["feeds"]}
        for pid, publisher in catalog.items():
            wanted = [choices[ident] for ident in requested if choices[ident]["publisher_id"] == pid]
            existing = entries.get(pid)
            if not wanted and not existing:
                continue
            known_urls = {option["url"] for option in publisher["options"]}
            if pid == "fsf":
                known_urls.add("https://www.fsf.org/resources/jobs/RSS/")
            custom = [sub for sub in feed_subscriptions(existing) if sub["url"] not in known_urls] if existing else []
            if not wanted:
                if custom:
                    existing["subscriptions"] = custom
                else:
                    existing["enabled"] = False
                continue
            entry = existing if existing is not None else {"id": pid, "name": publisher["name"]}
            if existing is None:
                result["feeds"].append(entry)
            # Remove only the old shipped assumptions; retain custom selectors.
            if pid == "wwr" and entry.get("include_categories") == ["Back-End Programming", "Front-End Programming", "Full-Stack Programming", "DevOps and Sysadmin"]:
                entry.pop("include_categories")
            if pid == "fsf" and entry.get("include_title_prefixes") == ["Job opportunity:"]:
                entry.pop("include_title_prefixes")
            old = {s["url"]: s for s in feed_subscriptions(existing)} if existing else {}
            explicit = existing.get("subscriptions", [existing]) if existing else []
            for url, sub in old.items():
                if not any(s.get("url") == url and "poll_interval_hours" in s for s in explicit):
                    sub.pop("poll_interval_hours", None)
            subs = custom[:]
            for option in wanted:
                sid = old.get(option["url"], {}).get("id", option["id"])
                if sid in {s["id"] for s in subs}:
                    sid = "catalog_" + sid
                sub = {**publisher["defaults"], **old.get(option["url"], {}),
                       "id": sid, "url": option["url"]}
                subs.append(sub)
            entry.update(enabled=True, subscriptions=subs)
        result["rss_selection_reasons"] = requested
    result["rss_selection_reviewed"] = True
    validate_feeds(result)
    return result


def preview_backfill(root: Path, previous: dict, proposed: dict, ask) -> None:
    """Fetch new choices only for a read-only counts/date preview, before Apply."""
    from rss_poller import fetch_feed, parse_feed, _parse_options
    before = {entry["id"]: entry for entry in previous["feeds"]}
    known_urls = {pid: {option["url"] for option in publisher["options"]}
                  for pid, publisher in load_catalog().items()}
    added = []
    for feed in proposed["feeds"]:
        if not feed.get("enabled", True):
            continue
        old = before.get(feed["id"], {})
        old_urls = {s["url"] for s in feed_subscriptions(old)} if old.get("enabled", True) and old else set()
        observed = (root / "state/rss" / feed["id"] / "index.json").exists()
        for sub in feed_subscriptions(feed):
            if sub["url"] in old_urls and (observed or sub["url"] not in known_urls.get(feed["id"], set())):
                continue
            raw, response = fetch_feed(sub["url"], {})
            if raw is None:
                raise ValueError("Backfill preview needs a complete feed response")
            items = parse_feed(raw, response.get("response_url", sub["url"]), **_parse_options(sub))
            dates = sorted(i["posted_at"] for i in items if i["posted_at"])
            unknown = len(items) - len(dates)
            print(f"{feed.get('name', feed['id'])} / {sub['id']}: {len(items)} available entries; "
                  + (f"dates {dates[0][:10]} to {dates[-1][:10]}" if dates else "no publication dates")
                  + f"; {unknown} unknown dates. Overlapping jobs are deduplicated.")
            # Update the actual proposed object, not the normalized copy.
            actual = next(s for s in feed["subscriptions"] if s["id"] == sub["id"])
            added.append(actual)
    if added:
        print("Backfill applies to new subscriptions. Recent keeps unknown dates; future-only records today's entries without sending them.")
        print("Processing existing entries uses paid matching calls. All available can create a large initial batch; Future arrivals only avoids evaluating today's inventory.")
        mode = ask("[R]ecent 30 days / [F]uture arrivals only / [A]ll available / [L]ater [R]: ",
                   {"r": "recent", "f": "future", "a": "all", "l": "later"}, "r")
        if mode == "later":
            raise SelectionDeferred()
        for sub in added:
            sub["backfill"] = {"mode": mode, "days": 30}


def prepare_selection(root: Path, profile: str, preferences: str, api_key: str, call_model,
                      mode: str, ask) -> dict[Path, str]:
    if mode == "keep":
        return {}
    config = read_config(root)
    while True:
        try:
            selected = (recommend(profile, preferences, config, api_key, call_model) if mode == "recommend"
                        else manual_choices() if mode == "manual" else [])
            proposed = apply_choices(config, selected, disable=mode == "disable")
            fsf = next((f for f in proposed["feeds"] if f["id"] == "fsf" and f.get("enabled", True)), None)
            if fsf:
                policy = ask("FSF postings: [P]aid roles only / [I]nclude volunteering / [L]ater [P]: ",
                             {"p": "paid", "i": "include", "l": "later"}, "p")
                if policy == "later":
                    raise SelectionDeferred()
                for sub in fsf.get("subscriptions", []):
                    excluded = [p for p in sub.get("exclude_title_prefixes", []) if p != "Volunteer opportunity:"]
                    if policy == "paid":
                        excluded.append("Volunteer opportunity:")
                    sub["exclude_title_prefixes"] = excluded
            preview_backfill(root, config, proposed, ask)
            return {root / "state/feeds.json": json.dumps(proposed, ensure_ascii=False, indent=2) + "\n"}
        except (ValueError, OSError, TransportError) as exc:
            message = f"RSS selection failed: {exc}. No settings changed. Details: state/setup.log"
            logging.error(message)
            print(message)
            action = ask("Choose [M]anually / [R]etry / Finish [L]ater [L]: ",
                         {"m": "manual", "r": "retry", "l": "later"}, "l")
            if action == "later":
                raise SelectionDeferred()
            if action == "manual":
                mode = "manual"
