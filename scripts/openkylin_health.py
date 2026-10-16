#!/usr/bin/env python3
"""Read-only, bounded Linux host diagnostics for an explicitly private runner.

Does not change system state, read credentials, send data, or execute task input.
Keep its JSON output out of public repositories. Standard library only.
"""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import platform
import re
import shutil
import subprocess
import sys
import tempfile

SERVICES = ("tailscaled", "ssh", "sshd", "ollama", "docker", "tor")
TOOLS = ("python3", "git", "node", "docker", "podman", "nvidia-smi", "rocm-smi", "lspci", "tailscale", "ollama")
PROCESS_NAMES = frozenset(("ollama", "llama-server", "tailscaled", "tor", "dockerd", "containerd", "python3", "node"))


def _command(arguments, timeout=3):
    """Return bounded output from a fixed, non-shell executable invocation."""
    try:
        result = subprocess.run(
            arguments, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL, timeout=timeout, text=True,
            encoding="utf-8", errors="replace", check=False,
            env={"PATH": "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C"},
        )
        if result.returncode == 0:
            return result.stdout[:4096]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return None


def _os_release():
    info = {}
    try:
        for line in Path("/etc/os-release").read_text(encoding="utf-8").splitlines():
            if "=" in line:
                key, val = line.split("=", 1)
                if key in ("ID", "VERSION_ID"):
                    info[key.lower()] = val.strip().strip('"')[:80]
    except OSError:
        pass
    return info


def _memory():
    values = {}
    try:
        for line in Path("/proc/meminfo").read_text(encoding="ascii").splitlines():
            match = re.fullmatch(r"(MemTotal|MemAvailable|SwapTotal|SwapFree):\s+(\d+) kB", line.strip())
            if match:
                values[match.group(1).lower() + "_bytes"] = int(match.group(2)) * 1024
    except OSError:
        pass
    return values


def _process_counts():
    """Count fixed executable basenames; never read command-line args/env."""
    totals = {name: 0 for name in sorted(PROCESS_NAMES)}
    proc = Path("/proc")
    try:
        for item in proc.iterdir():
            if not item.name.isdigit():
                continue
            try:
                name = (item / "comm").read_text(encoding="utf-8").strip()
            except (OSError, UnicodeError):
                continue
            if name in totals:
                totals[name] += 1
    except OSError:
        pass
    return totals


def _gpu():
    data = {"nvidia": [], "pci_vendors": [], "nvidia_probe_ok": False}
    if shutil.which("nvidia-smi"):
        raw = _command(["nvidia-smi", "--query-gpu=name,memory.total,utilization.gpu", "--format=csv,noheader,nounits"])
        if raw is not None:
            data["nvidia_probe_ok"] = True
            for line in raw.splitlines()[:8]:
                parts = [x.strip() for x in line.split(",")]
                if len(parts) == 3 and len(parts[0]) <= 120:
                    data["nvidia"].append({"name": parts[0], "memory_mib": parts[1], "utilization_pct": parts[2]})
    if shutil.which("lspci"):
        raw = _command(["lspci"])
        if raw is not None:
            vendors = set()
            for line in raw.splitlines():
                if "VGA compatible controller" in line or "3D controller" in line or "Display controller" in line:
                    for vendor in ("NVIDIA", "AMD", "Advanced Micro Devices", "Intel"):
                        if vendor.lower() in line.lower():
                            vendors.add("AMD" if vendor == "Advanced Micro Devices" else vendor)
            data["pci_vendors"] = sorted(vendors)
    return data


def make_report():
    disk = shutil.disk_usage("/")
    try:
        loads = [round(float(x), 2) for x in os.getloadavg()]
    except (OSError, AttributeError):
        loads = []
    services = {}
    if shutil.which("systemctl"):
        for service in SERVICES:
            result = _command(["systemctl", "is-active", service], timeout=2)
            services[service] = "active" if result is not None and result.strip() == "active" else "not_confirmed_active"
    return {
        "schema_version": 1,
        "checked_at_utc": datetime.now(timezone.utc).isoformat(),
        "scope": "read_only_local_host_diagnostics",
        "os": {"kernel": platform.system(), "architecture": platform.machine(), **_os_release()},
        "cpu": {"logical_cpus": os.cpu_count(), "load_1_5_15": loads},
        "memory": _memory(),
        "root_disk": {"total_bytes": disk.total, "free_bytes": disk.free},
        "tools_present": {name: bool(shutil.which(name)) for name in TOOLS},
        "service_status": services,
        "process_counts": _process_counts(),
        "gpu": _gpu(),
        "limitations": [
            "No remote-PC connection is implied by this report.",
            "Command presence and process counts do not verify AI model inference or GPU compute.",
            "Never upload host diagnostics to a public repository.",
        ],
    }


def save_report(path, report):
    path = Path(path).expanduser().absolute()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".openkylin-health-", suffix=".tmp", dir=path.parent)
    try:
        os.chmod(temporary, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(report, stream, ensure_ascii=False, indent=2, allow_nan=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return path


def main(argv=None):
    parser = argparse.ArgumentParser(description="Read-only openKylin/WSL Linux preflight; no networking")
    parser.add_argument("--output", required=True, help="Private JSON output path")
    args = parser.parse_args(argv)
    if os.name != "posix" or platform.system() != "Linux":
        parser.error("This tool is for Linux/openKylin/WSL only")
    if os.geteuid() == 0:
        parser.error("Refusing privileged execution; run as an unprivileged user")
    path = save_report(args.output, make_report())
    print("Read-only health report saved to", path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
