"""Closed M02 adapter for one pinned CPU algorithm; no model or code router.

The accepted checkout is the trust root. A peer's payload or matching hash is
not authority to execute: ProjectDispatcher must also recognize the immutable
handoff and its reserved Queue identity. Separate processes are not sandboxes.
"""
from __future__ import annotations

import hashlib
import json
import math
import os
from pathlib import Path
import platform
import re
import sys


HANDLER = "m02.cpu.coupled.v1"
KIND = "m02_cpu"
SUPPORTED_PLATFORMS = frozenset({"linux", "win32"})
ACCEPTED_BASE_COMMIT = "898162ee6208be03c5d5cf294c3d264f10f7de1b"
PINS = {
    "plugin_worker.py": "53afbc2fc05ae7649b31f09cd0f28ff3f4e8cb8b4fcbcacd22fb8a6e11f08250",
    "workbench/plugins.py": "bc7fcb59569b522e034e8add77202667e396b2b7ccfa1325e35825535dd5a1c0",
    "workbench/__init__.py": "bc59c00093296c3cb7d8b624af46f248bb28709b54d3cfe1e578a613ee6f19a2",
    "workbench/morphogenesis.py": "8c3f70d1a996bbc6e8785bdcc22ac29c813a54a68a3fbde176fa27869a8065f5",
    "workbench/kan.py": "0c7a7a3694bcfe7235599cbad13e76c1e555573e688087a90117528cf62c19b6",
    "workbench/cortical.py": "a21053b3fedec128f64b63622274fe31d1412d8d385f0d9e328dd5b653fe1b39",
}
BOUNDS = {"steps": (60, 240, int), "replicates": (3, 12, int),
          "recovery": (.1, 2, float), "coupling": (0, 1, float),
          "load": (0, 1, float), "perturbation": (0, 2, float),
          "uncertainty": (0, .4, float), "seed": (0, 2147483647, int)}
INPUT_FIELDS = {"schema_version", "project_id", "task_id", "revision", "handoff_id",
                "parent_handoff", "base_commit", "handler", "parameters", "input_sha256"}
RESULT_FIELDS = {"schema", "input", "project_revision", "project_sha256", "queue_job_id",
                 "execution_attempt", "reserved_attempts", "total_reserved_attempts_at_execution",
                 "scientific_payload", "scientific_sha256", "builtin_hashes", "runtime",
                 "model_calls", "network_calls", "additional_spend_usd"}
SERIES = ("Базовая модель", "Модель с возмущением", "Компонент 1 · базовый",
          "Компонент 1 · возмущённый", "Разность: 5-й процентиль", "Разность: 95-й процентиль")
LIMITATIONS = ["Абстрактные безразмерные переменные; модель не калибрована на организме.",
               "Интервал отражает заданный разброс параметров, а не вероятность клинического исхода."]
SUMMARY = ("Рассчитано воздействие абстрактного импульса на линейную связанную модель. "
           "Изменения параметров и численная модель сохранены.")
MODEL = {"equation": "dx_i/dt = -r*x_i + c*(mean(x)-x_i) + forcing_i", "dt": .05,
         "calibrated": False, "units": "dimensionless",
         "mean_invariant": "Диффузионная связь меняет отдельные компоненты, но её сумма равна нулю: среднее не зависит от coupling."}
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.:-]{0,95}\Z")


class UnsupportedCPUPlatform(ValueError):
    """Execution cannot be admitted here; durable state remains recoverable."""


def require_supported_platform():
    """Admission for execution, not for pure validation or reading old receipts.

    The unchanged pinned worker fails at RLIMIT_AS setup on macOS 15. Do not
    remove that protection, spend a reservation, or advertise worker capability
    on Darwin/unknown systems. Linux and Windows are the accepted runtimes.
    """
    if sys.platform not in SUPPORTED_PLATFORMS:
        raise UnsupportedCPUPlatform("m02.cpu.coupled.v1 execution unsupported on this platform; Linux or Windows required")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode("utf-8")).hexdigest()


def closed(value, keys, label):
    if type(value) is not dict or set(value) != set(keys):
        raise ValueError(label + " has an unsupported schema")


def number(value):
    if type(value) not in (int, float) or (type(value) is float and not math.isfinite(value)):
        raise ValueError("finite non-boolean number required")


def parameters(value):
    closed(value, BOUNDS, "parameters")
    for key, (low, high, kind) in BOUNDS.items():
        number(value[key])
        if kind is int and type(value[key]) is not int:
            raise ValueError(key + " must be an integer")
        if not low <= value[key] <= high:
            raise ValueError(key + " is outside the CPU contract bounds")
    # Preserve the accepted numeric representation: normalizing 1 to 1.0 would
    # change the caller's immutable input hash on replay.
    return json.loads(canonical(value))


def make_input(task, parent_handoff=None):
    value = {key: task[key] for key in ("project_id", "task_id", "revision", "handoff_id", "base_commit", "handler")}
    value.update(schema_version=1, parent_handoff=parent_handoff, parameters=parameters(task["parameters"]))
    value["input_sha256"] = digest(value)
    return validate_input(value)


def validate_input(value):
    closed(value, INPUT_FIELDS, "CPU input")
    if type(value["schema_version"]) is not int or value["schema_version"] != 1 or value["handler"] != HANDLER:
        raise ValueError("unsupported CPU handler")
    for key in ("project_id", "task_id", "handoff_id"):
        if not isinstance(value[key], str) or not _ID.fullmatch(value[key]):
            raise ValueError("invalid CPU identity")
    revision = value["revision"]
    if type(revision) is not int or revision not in (1, 2):
        raise ValueError("invalid CPU revision")
    parent = value["parent_handoff"]
    if (revision == 1 and parent is not None) or (revision == 2 and
            (not isinstance(parent, str) or not _ID.fullmatch(parent) or parent == value["handoff_id"])):
        raise ValueError("invalid CPU parent")
    if value["base_commit"] != ACCEPTED_BASE_COMMIT:
        raise ValueError("CPU base commit is not in the accepted checkout registry")
    parameters(value["parameters"])
    if value["input_sha256"] != digest({key: item for key, item in value.items() if key != "input_sha256"}):
        raise ValueError("CPU input hash mismatch")
    encoded = canonical(value)
    if len(encoded.encode("utf-8")) > 16 * 1024:
        raise ValueError("CPU input exceeds 16 KiB")
    return json.loads(encoded)


def verify_pins(root):
    root = Path(root).resolve()
    registry = json.loads((root / "data/builtin_pins.json").read_text(encoding="utf-8"))
    if registry != {"files": PINS}:
        raise ValueError("CPU pin registry differs from the accepted checkout")
    for name, expected in PINS.items():
        path = (root / name).resolve()
        if not path.is_relative_to(root) or not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
            raise ValueError("CPU builtin pin mismatch")
    return dict(PINS)


def scientific_payload(value, submitted_parameters):
    """Closed, finite scientific shape. This validates data, not scientific truth."""
    closed(value, ("summary", "metrics", "series", "table", "model", "parameters", "limitations"), "CPU result")
    if (value["summary"] != SUMMARY or canonical(value["model"]) != canonical(MODEL)
            or value["limitations"] != LIMITATIONS
            or canonical(parameters(value["parameters"])) != canonical(parameters(submitted_parameters))):
        raise ValueError("CPU result metadata differs from the pinned model")
    series = value["series"]
    if type(series) is not list or len(series) != len(SERIES):
        raise ValueError("CPU result must have six series")
    for row, name in zip(series, SERIES):
        closed(row, ("name", "points"), "series")
        if row["name"] != name or type(row["points"]) is not list or len(row["points"]) != submitted_parameters["steps"] + 1:
            raise ValueError("invalid CPU series")
        for index, point in enumerate(row["points"]):
            closed(point, ("x", "y"), "point")
            number(point["x"])
            number(point["y"])
            if point["x"] != index * .05:
                raise ValueError("CPU time grid mismatch")
    metrics = value["metrics"]
    labels = ("Средний интеграл разности", "Остаточная разность", "Повторы")
    units = ("условные единицы × время", "условные единицы")
    if type(metrics) is not list or len(metrics) != 3:
        raise ValueError("CPU metrics count mismatch")
    for index, row in enumerate(metrics):
        closed(row, ("label", "value", "unit") if index < 2 else ("label", "value"), "metric")
        number(row["value"])
        if row["label"] != labels[index] or (index < 2 and row["unit"] != units[index]):
            raise ValueError("CPU metric metadata mismatch")
    if type(metrics[2]["value"]) is not int or metrics[2]["value"] != submitted_parameters["replicates"]:
        raise ValueError("CPU replicate count mismatch")
    table = value["table"]
    if type(table) is not list or len(table) != 2:
        raise ValueError("CPU table count mismatch")
    for row, name in zip(table, ("integral_delta", "final_delta")):
        closed(row, ("показатель", "p05", "p50", "p95"), "table")
        if row["показатель"] != name:
            raise ValueError("CPU table metadata mismatch")
        for key in ("p05", "p50", "p95"):
            number(row[key])
        if not row["p05"] <= row["p50"] <= row["p95"]:
            raise ValueError("CPU percentiles out of order")
    if len(canonical(value).encode("utf-8")) > 1024 * 1024:
        raise ValueError("CPU scientific result exceeds 1 MiB")
    return json.loads(canonical(value))


def process_limits(os_name):
    return {"timeout_seconds": 10, "max_output_bytes": 1024 * 1024,
            "cpu_seconds": [5, 6] if os_name == "posix" else None,
            "address_space_bytes": 512 * 1024 * 1024 if os_name == "posix" else None,
            "file_size_bytes": 1024 * 1024 if os_name == "posix" else None,
            "file_descriptors": 64 if os_name == "posix" else None,
            "os_sandbox": False}


def validate_result(result, payload, job, authorization):
    closed(result, RESULT_FIELDS, "CPU result envelope")
    if (result["schema"] != "neuromorph.m02.cpu-result.v1" or canonical(result["input"]) != canonical(validate_input(payload))
            or type(result["project_revision"]) is not int or result["project_revision"] != authorization["project_revision"]
            or result["project_sha256"] != authorization["project_sha256"] or result["queue_job_id"] != job["id"]
            or type(result["execution_attempt"]) is not int or not 1 <= result["execution_attempt"] <= job["attempts"] <= 2
            or type(result["reserved_attempts"]) is not int or result["reserved_attempts"] != 2
            or type(result["total_reserved_attempts_at_execution"]) is not int
            or not 2 <= result["total_reserved_attempts_at_execution"] <= authorization["total_reserved_attempts"] <= 8
            or result["builtin_hashes"] != PINS):
        raise ValueError("CPU result identity/provenance mismatch")
    for key in ("model_calls", "network_calls", "additional_spend_usd"):
        if type(result[key]) is not int or result[key] != 0:
            raise ValueError("CPU adapter cost/call fields must be zero")
    scientific = scientific_payload(result["scientific_payload"], payload["parameters"])
    if result["scientific_sha256"] != digest(scientific):
        raise ValueError("CPU scientific hash mismatch")
    runtime = result["runtime"]
    closed(runtime, ("python", "os", "os_name", "process_limits", "service_run_id", "completed_at"), "runtime")
    if ((runtime["os"], runtime["os_name"]) not in (("Linux", "posix"), ("Windows", "nt"))
            or canonical(runtime["process_limits"]) != canonical(process_limits(runtime["os_name"]))):
        raise ValueError("unsupported CPU limits")
    for key, limit in (("python", 32), ("os", 128), ("service_run_id", 64), ("completed_at", 64)):
        if not isinstance(runtime[key], str) or not 0 < len(runtime[key]) <= limit or any(ord(c) < 32 for c in runtime[key]):
            raise ValueError("invalid CPU runtime provenance")
    if len(canonical(result).encode("utf-8")) > 1024 * 1024:
        raise ValueError("CPU result envelope exceeds 1 MiB")
    return result


def run_cpu_once(dispatcher, *, root, worker_id="m02-cpu", failpoint=None):
    """One dedicated claim; restore saved output, then fenced Queue.finish.

    A crash before durable output may cause a second pure computation after the
    lease expires. A saved result is reused. Unresolved completion stays unknown;
    this routine never invents a handoff or resets Queue's attempt counter.
    """
    require_supported_platform()
    from .service import Service, ServiceError

    def hit(stage):
        if failpoint is not None:
            failpoint(stage)

    root = Path(root).resolve()
    if dispatcher.cpu_root is None or root != dispatcher.cpu_root:
        raise ValueError("CPU root differs from the trusted dispatcher configuration")
    verify_pins(root)
    queue = dispatcher.queue
    queue.register_worker(worker_id, [KIND])
    job = queue.claim(worker_id, lease_seconds=60)
    if job is None:
        return {"status": "waiting", "reason": "no_cpu_job", "model_calls": 0}
    try:
        dispatcher.cpu_bind_claim(job)
        authorization = dispatcher.cpu_authorization(job["payload"], job, require_current=True)
        result = dispatcher.cpu_saved_result(job["id"])
        if result is None:
            # Keep the Service record in memory until our own durable boundary;
            # there is no second untracked persistent result journal to recover.
            service = Service(root=root, db_path=":memory:", timeout=10)
            try:
                record = service.run({"plugin_id": "coupled_dynamics", "parameters": job["payload"]["parameters"]})
            finally:
                service.close()
            if record["status"] != "completed" or record["provenance"]["builtin_hashes"] != PINS:
                raise ValueError("pinned CPU calculation did not complete")
            scientific = scientific_payload(record["result"], job["payload"]["parameters"])
            result = {"schema": "neuromorph.m02.cpu-result.v1", "input": job["payload"],
                      "project_revision": authorization["project_revision"], "project_sha256": authorization["project_sha256"],
                      "queue_job_id": job["id"], "execution_attempt": job["attempts"], "reserved_attempts": 2,
                      "total_reserved_attempts_at_execution": authorization["total_reserved_attempts"],
                      "scientific_payload": scientific, "scientific_sha256": digest(scientific), "builtin_hashes": dict(PINS),
                      "runtime": {"python": record["provenance"]["python"], "os": platform.system(), "os_name": os.name,
                                  "process_limits": process_limits(os.name), "service_run_id": record["id"], "completed_at": record["completed_at"]},
                      "model_calls": 0, "network_calls": 0, "additional_spend_usd": 0}
            validate_result(result, job["payload"], job, authorization)
            hit("after_cpu_compute")
            queue.heartbeat(job["id"], worker_id, job["lease_token"])
            result = dispatcher.cpu_save_result(job, result)
            hit("after_cpu_result_saved")
        validate_result(result, job["payload"], job, authorization)
    except (ValueError, TypeError, KeyError, OSError, ServiceError):
        try:
            done = queue.finish(job["id"], worker_id, job["lease_token"], error="M02 CPU contract or execution rejected")
        except (ValueError, OSError):
            return {"status": "outcome_unknown", "queue_job_id": job["id"], "model_calls": 0}
        return {"status": done["status"], "queue_job_id": job["id"], "attempts": done["attempts"], "model_calls": 0}
    try:
        done = queue.finish(job["id"], worker_id, job["lease_token"], result=result)
    except (ValueError, OSError):
        done = queue.get(job["id"])
        if done["status"] != "completed" or canonical(done["result"]) != canonical(result):
            return {"status": "outcome_unknown", "queue_job_id": job["id"], "model_calls": 0}
    hit("after_queue_finish")
    hit("after_finish")
    return {"status": done["status"], "queue_job_id": job["id"], "attempts": done["attempts"],
            "scientific_sha256": result["scientific_sha256"], "model_calls": 0}
