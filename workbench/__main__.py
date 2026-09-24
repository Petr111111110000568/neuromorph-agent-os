"""Run with python -m workbench; Python standard library only."""
import argparse
import json
import os
import secrets
import subprocess
import sys
import threading
import time
from pathlib import Path
from . import __version__
from .service import Service, ServiceError, parse_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Meta-Harness local research workbench")
    parser.add_argument("--version", action="version", version=__version__)
    parser.add_argument("--data-dir", type=Path, help="Directory for persistent SQLite state")
    commands = parser.add_subparsers(dest="command", required=True)
    serve = commands.add_parser("serve", help="Launch localhost web console")
    serve.add_argument("--port", type=int, default=8765)
    commands.add_parser("doctor", help="Inspect local runtime capabilities")
    run = commands.add_parser("run", help="Run a registered numerical demonstration")
    run.add_argument("plugin_id")
    run.add_argument("--parameters", default="{}", help="JSON object")
    commands.add_parser("mcp", help="Start bounded MCP stdio subset")
    hub = commands.add_parser("hub", help="Start authenticated worker transport; TLS required outside loopback")
    hub.add_argument("--host", default="127.0.0.1")
    hub.add_argument("--port", type=int, default=8766)
    hub.add_argument("--certfile")
    hub.add_argument("--keyfile")
    daemon = commands.add_parser("daemon", help="Advance persisted bounded research campaigns")
    daemon.add_argument("--once", action="store_true")
    daemon.add_argument("--interval", type=float, default=2)
    local = commands.add_parser("local-network", help="Launch UI, hub, coordinator and independent worker processes")
    local.add_argument("--port", type=int, default=8765)
    local.add_argument("--hub-port", type=int, default=8766)
    local.add_argument("--workers", type=int, default=2)
    local.add_argument("--federation-port", type=int, default=8767)
    gateway = commands.add_parser("federation-gateway", help="Contribution gateway; TLS required outside loopback")
    gateway.add_argument("--host", default="127.0.0.1")
    gateway.add_argument("--port", type=int, default=8767)
    gateway.add_argument("--certfile")
    gateway.add_argument("--keyfile")
    gateway.add_argument("--public-base-url")
    invite = commands.add_parser("invite", help="Issue a scoped single-use invitation to a protected local file")
    invite.add_argument("--offer", required=True)
    invite.add_argument("--out", type=Path, required=True)
    invite.add_argument("--expires", type=int, default=86400)
    scout = commands.add_parser("scout", help="Bounded directory discovery; no outbound invitations")
    scout.add_argument("--query", required=True)
    scout.add_argument("--providers", default="agentverse,mcp_registry")
    scout.add_argument("--online", action="store_true")
    scout.add_argument("--limit", type=int, default=5)
    scout.add_argument("--cycles", type=int, default=1)
    scout.add_argument("--interval", type=float, default=60)
    commands.add_parser("seed-exchange", help="Create a labelled starter research offer and project-owned summary")
    args = parser.parse_args(argv)
    service = Service(db_path=args.data_dir / "workbench.sqlite3" if args.data_dir else None)
    try:
        if args.command == "serve":
            from .server import make_server
            server = make_server(service, port=args.port)
            print(f"Meta-Harness: http://127.0.0.1:{server.server_address[1]} — Ctrl+C to stop", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.command == "doctor":
            print(json.dumps({"status": service.status(), "configuration": service.environment(),
                              "audit": {"valid": service.audit()["valid"]}}, ensure_ascii=False, indent=2))
        elif args.command == "run":
            result = service.run({"plugin_id": args.plugin_id, "parameters": parse_json(args.parameters)})
            print(json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False))
            return 0 if result["status"] == "completed" else 1
        elif args.command == "mcp":
            from .mcp_server import serve_stdio
            serve_stdio(service)
        elif args.command == "federation-gateway":
            from .federation.transport import make_gateway
            server = make_gateway(service.federation.exchange, host=args.host, port=args.port,
              certfile=args.certfile, keyfile=args.keyfile, public_base_url=args.public_base_url)
            print(f"Contribution gateway listening on {server.server_address}; Ctrl+C to stop", flush=True)
            try: server.serve_forever()
            except KeyboardInterrupt: pass
            finally: server.server_close()
        elif args.command == "invite":
            if args.out.exists():
                raise ValueError("Invitation file already exists; choose a new path")
            args.out.parent.mkdir(parents=True, exist_ok=True)
            invitation = service.federation.exchange.create_invitation({"offer_id": args.offer, "expires_in_seconds": args.expires})
            # Atomic exclusive creation; bearer credentials never printed or exported.
            fd = os.open(args.out, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as stream:
                json.dump(invitation, stream, ensure_ascii=False)
                stream.write("\n")
            print(json.dumps({"invitation_file": str(args.out), "offer_id": args.offer,
              "notice": "Contains a single-use credential. Share only by an agreed protected channel; Windows ACLs must be checked separately."}, ensure_ascii=False))
        elif args.command == "scout":
            if not 1 <= args.cycles <= 20 or not 10 <= args.interval <= 3600:
                raise ValueError("cycles must be 1..20; interval 10..3600 seconds")
            try:
                for index in range(args.cycles):
                    result=service.federation.discover({"query": args.query, "providers": args.providers.split(','),
                      "limit": args.limit, "online": args.online, "data_class": "public"})
                    print(json.dumps(result, ensure_ascii=False, allow_nan=False), flush=True)
                    if index+1 < args.cycles: time.sleep(args.interval)
            except KeyboardInterrupt: pass
        elif args.command == "seed-exchange":
            from .federation.seed import seed_exchange
            print(json.dumps(seed_exchange(service.federation.exchange), ensure_ascii=False, indent=2))
        elif args.command == "hub":
            from .network.transport import make_hub
            server = make_hub(service.network.queue, host=args.host, port=args.port,
                              token=os.environ.get("META_HUB_TOKEN"), certfile=args.certfile, keyfile=args.keyfile)
            print(f"Worker hub listening at {server.server_address[0]}:{server.server_address[1]}", flush=True)
            try:
                server.serve_forever()
            except KeyboardInterrupt:
                pass
            finally:
                server.server_close()
        elif args.command == "daemon":
            if not 0.2 <= args.interval <= 60:
                raise ValueError("interval must be 0.2..60 seconds")
            try:
                while True:
                    result = {"network": service.network.tick_all(), "society": service.society.tick_all(), "brain": service.brain.tick_all()}
                    if args.once:
                        print(json.dumps(result, ensure_ascii=False))
                        break
                    time.sleep(args.interval)
            except KeyboardInterrupt:
                pass
        elif args.command == "local-network":
            if not 1 <= args.workers <= 8:
                raise ValueError("workers must be 1..8")
            return local_network(service, args)
        return 0
    except (ServiceError, ValueError, PermissionError, KeyError) as exc:
        print(json.dumps({"error": {"message": str(exc), "code": getattr(exc, "code", "invalid_argument")}}, ensure_ascii=False), file=sys.stderr)
        return 2
    finally:
        service.close()


def local_network(service, args):
    """Convenience launcher, not a replacement for multi-host deployment."""
    from .network.transport import make_hub
    from .server import make_server
    from .federation.transport import make_gateway
    token = secrets.token_urlsafe(48)
    hub = make_hub(service.network.queue, port=args.hub_port, token=token)
    try:
        ui = make_server(service, port=args.port)
        gateway = make_gateway(service.federation.exchange, port=args.federation_port)
    except Exception:
        hub.server_close()
        if 'ui' in locals(): ui.server_close()
        raise
    stopped = threading.Event()
    hub_thread = threading.Thread(target=hub.serve_forever, daemon=True)
    gateway_thread = threading.Thread(target=gateway.serve_forever, daemon=True)
    processes = []

    def advance():
        while not stopped.is_set():
            try:
                service.network.tick_all()
                service.society.tick_all()
                service.brain.tick_all()
            except Exception:
                print("Coordinator tick failed; state retained. Inspect application status.", file=sys.stderr, flush=True)
            stopped.wait(1)

    coordinator = threading.Thread(target=advance, daemon=True)
    try:
        hub_thread.start()
        gateway_thread.start()
        coordinator.start()
        for index in range(args.workers):
            env = dict(os.environ, META_HUB_TOKEN=token)
            worker_id = f"local-{os.getpid()}-{index + 1}"
            command = [sys.executable, "-m", "workbench.network.worker", "--hub", f"http://127.0.0.1:{hub.server_address[1]}",
                "--id", worker_id, "--root", str(service.root), "--data-dir", str(service.store.path.parent / "workers" / worker_id)]
            processes.append(subprocess.Popen(command, cwd=service.root, env=env, stdin=subprocess.DEVNULL))
        print(f"Meta-Harness: http://127.0.0.1:{ui.server_address[1]}/federation.html — {args.workers} local workers; contribution gateway {gateway.server_address[1]}; Ctrl+C to stop", flush=True)
        ui.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stopped.set()
        for process in processes:
            process.terminate()
        for process in processes:
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
        hub.shutdown()
        gateway.shutdown()
        hub_thread.join(timeout=3)
        gateway_thread.join(timeout=3)
        coordinator.join(timeout=5)
        hub.server_close()
        gateway.server_close()
        ui.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
