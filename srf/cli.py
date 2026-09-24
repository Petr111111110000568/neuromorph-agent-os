import argparse, json
from .manifest import PluginManifest
from .router import CapabilityRouter

def main():
    parser = argparse.ArgumentParser(prog="srf")
    sub = parser.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("validate"); v.add_argument("manifest")
    r = sub.add_parser("route"); r.add_argument("capability"); r.add_argument("manifests", nargs="+")
    args = parser.parse_args()

    if args.cmd == "validate":
        errors = PluginManifest.load(args.manifest).validate()
        print(json.dumps({"valid": not errors, "errors": errors}, indent=2))
        raise SystemExit(1 if errors else 0)
    if args.cmd == "route":
        router = CapabilityRouter()
        for path in args.manifests: router.add(PluginManifest.load(path))
        print(json.dumps([x.__dict__ for x in router.resolve(args.capability)], indent=2))

if __name__ == "__main__":
    main()
