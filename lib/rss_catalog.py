"""Verified choices for setup. Polling uses the reviewed configuration only."""

import json
from pathlib import Path


def load_catalog() -> dict[str, dict]:
    data = json.loads(Path(__file__).with_name("rss_catalog.json").read_text())
    return {publisher["id"]: publisher for publisher in data["publishers"]}


def catalog_choices() -> dict[str, dict]:
    return {f"{pid}:{option['id']}": {**option, "publisher_id": pid,
            "publisher_name": publisher["name"], "publisher_coverage": publisher["coverage"],
            "defaults": publisher["defaults"]}
            for pid, publisher in load_catalog().items() for option in publisher["options"]}
