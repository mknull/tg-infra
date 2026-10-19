#!/usr/bin/env python3
"""One read-only public-feed check; no model calls, runtime state or delivery.

Run: jobsmcp/bin/python3 scripts/verify_rss_catalog.py --output /tmp/rss-verification
"""

import argparse
import hashlib
import json
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from lib.rss_catalog import load_catalog
from lib.rss_details import extract_detail
from rss_poller import fetch_feed, parse_feed, plain_text


def verify(output: Path) -> dict:
    output.mkdir(parents=True, exist_ok=True)
    report = {"checked_at": datetime.now(timezone.utc).isoformat(), "subscriptions": [],
              "comparisons": [], "details": [], "errors": []}
    for pid, publisher in load_catalog().items():
        inventories = {}
        for option in publisher["options"]:
            ident = f"{pid}:{option['id']}"
            try:
                raw, response = fetch_feed(option["url"], {})
                (output / f"{pid}-{option['id']}.xml").write_bytes(raw)
                items = parse_feed(raw, response.get("response_url", option["url"]), **{
                    k: publisher["defaults"].get(k, False) for k in
                    ("allow_empty", "repair_html_entities", "merge_variants")})
                inventories[option["id"]] = {i["source_id"] for i in items}
                dates = [i["posted_at"] for i in items if i["posted_at"]]
                report["subscriptions"].append({"id": ident, "url": option["url"],
                    "count": len(items), "sha256": hashlib.sha256(raw).hexdigest(),
                    "oldest": min(dates) if dates else None, "newest": max(dates) if dates else None})
                if option["id"] == "all" and pid in ("fsf", "nodesk"):
                    jobs = [i for i in items if i["title"].startswith("Job opportunity:")] if pid == "fsf" else items
                    for n, item in enumerate(jobs[:2]):
                        time.sleep(1)
                        url = item["url"].replace("http://www.fsf.org/", "https://www.fsf.org/")
                        detail, _ = fetch_feed(url, {})
                        (output / f"{pid}-detail-{n}.html").write_bytes(detail)
                        body = extract_detail(detail, pid, url, plain_text)
                        report["details"].append({"publisher": pid, "url": url,
                            "feed_characters": len(item["content"]), "detail_characters": len(body)})
                print(f"{ident}: {len(items)} entries", flush=True)
            except Exception as exc:
                report["errors"].append({"id": ident, "error": str(exc)})
                print(f"{ident}: FAILED: {exc}", flush=True)
            time.sleep(1)
        broad = inventories.get("all", set())
        for oid, ids in inventories.items():
            if oid != "all":
                report["comparisons"].append({"id": f"{pid}:{oid}", "overlap_with_all": len(ids & broad),
                                              "absent_from_all": len(ids - broad)})
        if pid == "wwr":
            report["comparisons"].append({"id": "wwr:programming", "back_end_missing":
                len(inventories.get("back-end-programming", set()) - inventories.get("programming", set()))})
        if pid == "opensourcejobhub":
            for query in ("?q=zzzznonexistentfeedprobezzzz", "?categories%5B%5D=System%20Administration"):
                try:
                    url = publisher["options"][0]["url"] + query
                    raw, _ = fetch_feed(url, {})
                    ids = {i["source_id"] for i in parse_feed(raw, url, allow_empty=True)}
                    report["comparisons"].append({"url": url, "membership_unchanged": ids == broad})
                except Exception as exc:
                    report["errors"].append({"url": url, "error": str(exc)})
                time.sleep(1)
        (output / "report.json").write_text(json.dumps(report, indent=2) + "\n")
    return report


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    report = verify(parser.parse_args().output)
    sys.exit(bool(report["errors"]))
