"""Interactive matching setup. All model output is reviewed before publication."""

import difflib
import json
import logging
import urllib.error
from pathlib import Path

from .api import TransportError
from .direction import (direction_lock, direction_updates, load_current_direction,
                        prepare_direction)
from .matching import (PROFILE_FILES, ROLE_FIELDS, matching_gaps, missing_role_fields,
                       source_configs)
from .state import apply_text_updates


def _ask(prompt: str, choices: dict[str, str], default: str) -> str:
    while True:
        try:
            answer = input(prompt).strip().lower() or default
        except EOFError:
            return "later"
        if answer in choices:
            return choices[answer]
        print("Choose " + ", ".join(choices) + ".")


def _error_message(exc: Exception) -> str:
    if isinstance(exc, urllib.error.HTTPError):
        if exc.code in (401, 403):
            return "Model authentication failed. Check DEEPSEEK_API_KEY in state/.env."
        return f"Model request failed (HTTP {exc.code}). Check the API configuration."
    if isinstance(exc, TransportError):
        return "The model service is unavailable. Check your connection and retry later."
    return str(exc)


def _preview(updates, expected):
    print("\nProposed matching settings:")
    for path, content in updates.items():
        if path.name in ("channels.json", "feeds.json"):
            collection = "channels" if path.name == "channels.json" else "feeds"
            for entry in json.loads(content)[collection]:
                print(f"\n{entry.get('username', entry.get('id'))}:")
                for field in ROLE_FIELDS:
                    print(f"  {field}: {entry.get(field, '(unset)')}")
        elif path.name.endswith("criteria.md"):
            print("\n" + "".join(difflib.unified_diff(
                (expected.get(path) or "").splitlines(keepends=True),
                content.splitlines(keepends=True), fromfile=f"current/{path.name}",
                tofile=f"proposed/{path.name}")))
        elif path.name == "current-direction.md":
            print("\nUpdated preferences:\n" + content)
    print("\nFiles to save: " + ", ".join(str(path) for path in updates))


def run_setup(generator) -> int:
    """Return zero only when matching is ready; keep does not call any model."""
    root = generator.PROJECT_DIR
    log_path = root / "state" / "setup.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.FileHandler(log_path)
    logging.getLogger().addHandler(handler)
    try:
        return _run_setup(generator)
    except (EOFError, KeyboardInterrupt):
        print("\nMatching setup deferred. Existing settings remain in use. Re-run ./setup.sh to continue.")
        return 1
    finally:
        logging.getLogger().removeHandler(handler)
        handler.close()


def _run_setup(generator) -> int:
    root = generator.PROJECT_DIR
    env = generator.load_env()
    gaps = matching_gaps(root)
    existing = any((root / "source" / name).exists() for name in PROFILE_FILES)
    existing = existing or any((root / "state" / name).exists() for name in
                               ("it-jobs-criteria.md", "email-triage-criteria.md"))
    try:
        for _, _, entries, defaults, identity, _ in source_configs(root):
            existing = existing or any(len(missing_role_fields(e, defaults.get(e[identity], {}))) < 2
                                       for e in entries)
    except (ValueError, KeyError, TypeError) as exc:
        logging.error("Invalid source configuration: %s. Fix it and re-run ./setup.sh.", exc)
        return 1
    if existing:
        print("Matching setup found. " + ("Incomplete: " + "; ".join(gaps) if gaps else "Required settings are present."))
        choice = _ask("[K]eep existing / [C]omplete missing setup / [R]egenerate from updated information [K]: ",
                      {"k": "keep", "c": "complete", "r": "regenerate"}, "k")
        if choice in ("keep", "later"):
            print("Existing settings kept. " + ("Re-run ./setup.sh and choose Complete to finish matching setup." if gaps else "No model calls made."))
            return int(bool(gaps)) if choice == "keep" else 1
    else:
        if not env.get("DEEPSEEK_API_KEY", "").strip():
            print("Set DEEPSEEK_API_KEY in state/.env, then re-run ./setup.sh to generate matching preferences.")
            return 1
        choice = _ask("Generate job-matching instructions from your profile for Flash and Pro? [Y/n]: ",
                      {"y": "complete", "n": "later"}, "y")
        if choice == "later":
            print("Matching setup deferred. Re-run ./setup.sh to continue.")
            return 1
    api_key = env.get("DEEPSEEK_API_KEY", "").strip()
    if not api_key:
        print("Set DEEPSEEK_API_KEY in state/.env, then re-run ./setup.sh. No settings changed.")
        return 1
    force = choice == "regenerate"
    profile_complete = all((root / "source" / name).exists() and (root / "source" / name).read_text().strip()
                           for name in PROFILE_FILES)
    documents = None
    if not profile_complete or force:
        prompt = "Documents directory (Enter to reuse current profile): " if profile_complete else "Documents directory (CV, letters, or text notes): "
        docs = input(prompt).strip()
        if docs:
            try:
                documents = generator.extract_text(Path(docs).expanduser())
            except (OSError, ValueError) as exc:
                logging.error("Could not read documents: %s. No settings changed.", exc)
                return 1
        elif not profile_complete:
            print("Documents are needed to complete your profile. Re-run ./setup.sh when ready.")
            return 1
    user_name = env.get("USER_NAME", "").strip() or input("Your full name: ").strip() or "the user"
    feedback = input("Additional preferences (roles, alternatives, exclusions; Enter to keep current preferences): ").strip()
    expected = generator.generation_snapshot()
    source_updates = None
    prepared_direction = None
    while True:
        try:
            if source_updates is None:
                source_updates = generator.build_source_updates(documents, api_key) if documents else {}
            profile = "\n\n".join(source_updates.values()) if source_updates else generator.read_profile()
            if feedback and prepared_direction is None:
                prepared_direction = prepare_direction(feedback, api_key, load_current_direction())
            preferences = prepared_direction[0] if prepared_direction else load_current_direction()
            updates = {**source_updates, **generator.build_stage2_updates(
                user_name, api_key, force, profile_context=profile, preferences=preferences)}
            if prepared_direction:
                updates.update(direction_updates(*prepared_direction))
            if not updates:
                print("All matching settings are already complete. No changes needed.")
                return int(bool(matching_gaps(root)))
            _preview(updates, expected)
            action = _ask("[A]pply / [R]evise preferences / [L]ater [L]: ",
                          {"a": "apply", "r": "revise", "l": "later"}, "l")
            if action == "later":
                print("No settings changed. Re-run ./setup.sh to continue.")
                return 1
            if action == "revise":
                revision = input("What should change? ").strip()
                if revision:
                    feedback = feedback + "\nLatest correction (overrides earlier preferences): " + revision
                    prepared_direction = None
                continue
            with direction_lock():
                apply_text_updates(updates, expected)
            logging.info("Matching preferences saved after review.")
            return int(bool(matching_gaps(root)))
        except (ValueError, OSError, TransportError, KeyError, TypeError) as exc:
            logging.error("%s No matching settings changed. Details: state/setup.log", _error_message(exc))
            action = _ask("[R]etry now / Finish [L]ater [L]: ", {"r": "retry", "l": "later"}, "l")
            if action == "later":
                print("Setup incomplete. Existing settings remain in use. Re-run ./setup.sh to continue.")
                return 1
            # Re-read inputs: a stale proposal must never overwrite a newer preference update.
            expected = generator.generation_snapshot()
            prepared_direction = None
