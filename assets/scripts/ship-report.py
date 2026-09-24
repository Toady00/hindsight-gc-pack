#!/usr/bin/env python3
"""Read shared scan health. All persistent records live in the city Beads store."""
import json
import sys

sys.dont_write_bytecode = True
from ingestion import BeadsStore, Error
from ship_report import status

if __name__ == "__main__":
    try:
        if len(sys.argv) not in (4, 5) or sys.argv[1] != "status":
            raise Error("usage: ship-report.py status <api> <bank> [work-id]", 2)
        report = status(BeadsStore(sys.argv[2], sys.argv[3]), sys.argv[4] if len(sys.argv) == 5 else "")
        print(json.dumps(report, indent=2))
        sys.exit(0 if report["healthy"] else 1)
    except (Error, ValueError, OSError) as error:
        print(f"ship report: {error}", file=sys.stderr)
        sys.exit(getattr(error, "code", 5))
