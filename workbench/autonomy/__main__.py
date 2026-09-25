"""python -m workbench.autonomy --config config/autonomy.json --output-dir runtime/autonomy"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys

from .engine import run_cycle
from .history import record_cycle
from .providers import MAX_RESPONSE_BYTES, json_load


def main(argv=None):
    parser = argparse.ArgumentParser(description="One bounded, reviewable Meta-Harness development cycle")
    parser.add_argument("--config", default="config/autonomy.json")
    parser.add_argument("--output-dir", default="runtime/autonomy")
    parser.add_argument("--history-db", help="Optional bounded discovery history; parent directory must exist")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--provider", choices=("auto", "none", "openai", "anthropic"))
    args = parser.parse_args(argv)
    try:
        with Path(args.config).open("rb") as stream:
            config = json_load(stream.read(MAX_RESPONSE_BYTES + 1))
        result = run_cycle(config, args.output_dir, online=args.online, provider=args.provider)
        history = record_cycle(Path(args.history_db), result) if args.history_db else None
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError, sqlite3.Error):
        print("Autonomy cycle failed: invalid configuration, unsafe output path, or unavailable local data.", file=sys.stderr)
        return 2
    receipt = {"cycle_id": result["cycle_id"], "status": result["status"], "counts": result["counts"]}
    if history is not None:
        receipt["history"] = history
    print(json.dumps(receipt, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
