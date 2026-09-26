"""Launch only local council MCP tools from this checkout, independent of cwd."""
import argparse
from pathlib import Path
import sys


def main(argv=None):
    parser = argparse.ArgumentParser(description="NeuroMorf local-only council MCP launcher")
    parser.add_argument("--data-dir", type=Path, required=True, help="Shared local UI/MCP state directory")
    args = parser.parse_args(argv)
    root = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(root))
    from workbench.__main__ import main as workbench_main
    return workbench_main(["--data-dir", str(args.data_dir.resolve()), "mcp", "--offline-only"])


if __name__ == "__main__":
    raise SystemExit(main())
