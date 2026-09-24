"""python -m workbench.autonomy --config config/autonomy.json --output-dir runtime/autonomy"""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

from .engine import run_cycle
from .providers import MAX_RESPONSE_BYTES, json_load


def main(argv=None):
    parser = argparse.ArgumentParser(description="One bounded, reviewable Meta-Harness development cycle")
    parser.add_argument("--config", default="config/autonomy.json")
    parser.add_argument("--output-dir", default="runtime/autonomy")
    parser.add_argument("--online", action="store_true")
    parser.add_argument("--provider", choices=("auto", "none", "openai", "anthropic"))
    args = parser.parse_args(argv)
    try:
        with Path(args.config).open("rb") as stream:
            config = json_load(stream.read(MAX_RESPONSE_BYTES + 1))
        result = run_cycle(config, args.output_dir, online=args.online, provider=args.provider)
    except (OSError, ValueError, TypeError, UnicodeError, RecursionError):
        print("Autonomy cycle failed: invalid configuration, unsafe output path, or unavailable local data.", file=sys.stderr)
        return 2
    print(json.dumps({"cycle_id": result["cycle_id"], "status": result["status"], "counts": result["counts"]}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
