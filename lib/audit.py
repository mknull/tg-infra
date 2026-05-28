"""Audit trail writer — appends JSON lines to audit files."""

import json
from pathlib import Path


def write_audit(record: dict, audit_file: Path) -> None:
    """Append one complete audit record for a processed message."""
    audit_file.parent.mkdir(parents=True, exist_ok=True)
    with audit_file.open("a") as f:
        f.write(json.dumps(record) + "\n")
