"""Inspect or explicitly run a pinned external harness with all tools disabled."""
import argparse
import json
from pathlib import Path
import sys

from . import HarnessRegistry


def main(argv=None):
    parser = argparse.ArgumentParser(description="Pinned, tool-disabled agent harness adapters")
    parser.add_argument("--registry", default=None)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("status")
    run = sub.add_parser("run")
    run.add_argument("harness", choices=("unreal", "pi", "openhands"))
    run.add_argument("--prompt", required=True)
    run.add_argument("--allow-model-calls", action="store_true")
    run.add_argument("--provider", choices=("openai", "anthropic"), default="openai")
    run.add_argument("--model")
    run.add_argument("--timeout", type=int, default=30)
    args = parser.parse_args(argv)
    registry = HarnessRegistry(Path(__file__).resolve().parents[2], args.registry)
    try:
        result = registry.status() if args.command == "status" else registry.run(
            args.harness, args.prompt, allow_model_calls=args.allow_model_calls,
            provider=args.provider, model=args.model, timeout_seconds=args.timeout)
    except (OSError, ValueError, TypeError, UnicodeError):
        print("Harness operation failed: invalid local configuration or unavailable resource.", file=sys.stderr)
        return 2
    print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
