"""Cloud-only bounded diagnosis of the existing pinned macOS CPU worker.

No pin/limit changes, arbitrary payloads, environment dumps, network or model.
This diagnostic cannot convert a failing portability test into a passing one.
"""
from __future__ import annotations

import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from workbench.m02_cpu import verify_pins
from workbench.service import Service

PARAMETERS = {"steps": 60, "replicates": 3, "recovery": .6, "coupling": .25,
              "load": .2, "perturbation": .5, "uncertainty": .15, "seed": 42}
CAPTURE_BYTES = 2048


def emit(value):
    print(json.dumps(value, ensure_ascii=True, sort_keys=True, allow_nan=False), flush=True)


def bounded(value):
    return str(value).encode("utf-8", errors="replace")[:CAPTURE_BYTES].decode("utf-8", errors="replace")


def main():
    if sys.platform != "darwin":
        raise SystemExit("This finite diagnostic is restricted to the macOS cloud fixture")
    verify_pins(ROOT)
    service = Service(root=ROOT, db_path=":memory:", timeout=10)
    try:
        record = service.run({"plugin_id": "coupled_dynamics", "parameters": PARAMETERS})
        error = record.get("error", {})
        emit({"stage": "service", "status": record["status"],
              "error_code": bounded(error.get("code", "")), "error_message": bounded(error.get("message", ""))})
    finally:
        service.close()

    # Recheck the complete accepted pin registry immediately before the one
    # direct invocation. Match Service.run's interpreter, -I, environment, CWD,
    # output files and timeout. The original worker installs original limits.
    verify_pins(ROOT)
    with tempfile.TemporaryDirectory(prefix="m02-cpu-diagnostic-") as cwd:
        with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
            proc = subprocess.Popen([sys.executable, "-I", str(ROOT / "plugin_worker.py"), "coupled_dynamics"],
                stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, cwd=cwd,
                env={"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"},
                start_new_session=True)
            timed_out = False
            try:
                proc.communicate(json.dumps(PARAMETERS, ensure_ascii=False, allow_nan=False).encode("utf-8"), timeout=10)
            except subprocess.TimeoutExpired:
                timed_out = True
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                proc.communicate()
            stdout.seek(0)
            stderr.seek(0)
            captured_out = stdout.read(CAPTURE_BYTES + 1)
            captured_err = stderr.read(CAPTURE_BYTES + 1)
            emit({"stage": "pinned_worker", "returncode": proc.returncode, "timeout": timed_out,
                  "stdout": captured_out[:CAPTURE_BYTES].decode("utf-8", errors="replace"),
                  "stderr": captured_err[:CAPTURE_BYTES].decode("utf-8", errors="replace"),
                  "stdout_truncated": len(captured_out) > CAPTURE_BYTES,
                  "stderr_truncated": len(captured_err) > CAPTURE_BYTES})


if __name__ == "__main__":
    main()
