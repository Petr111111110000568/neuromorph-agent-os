"""Bounded research services. No remote execution or laboratory integration."""
import hashlib
import copy
import importlib
import json
import math
import os
import signal
import subprocess
import sys
import tempfile
import threading
import uuid
from pathlib import Path
from urllib.parse import urlsplit
from . import __version__
from . import council as council_rules
from .store import Store, canonical, now

MAX_TEXT = 12000
MAX_OUTPUT = 1024 * 1024


class ServiceError(Exception):
    def __init__(self, message, code="invalid_request", status=400):
        super().__init__(message)
        self.message, self.code, self.status = message, code, status


def validate_json(value, depth=0):
    if depth > 30:
        raise ServiceError("JSON nesting exceeds 30 levels")
    if isinstance(value, float) and not math.isfinite(value):
        raise ServiceError("JSON numbers must be finite")
    if isinstance(value, dict):
        if any(not isinstance(key, str) for key in value):
            raise ServiceError("JSON object keys must be strings")
        for item in value.values():
            validate_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            validate_json(item, depth + 1)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ServiceError("Unsupported JSON value")


def parse_json(text):
    try:
        value = json.loads(text, parse_constant=lambda v: (_ for _ in ()).throw(ValueError(v)))
        validate_json(value)
        return value
    except (ValueError, RecursionError, UnicodeError) as exc:
        raise ServiceError("Invalid JSON; numbers must be finite", "invalid_json") from exc


def text_field(value, label, max_length=MAX_TEXT, optional=False):
    if optional and value is None:
        return ""
    if not isinstance(value, str) or not value.strip() or len(value) > max_length:
        raise ServiceError(f"{label} must be a nonempty string of at most {max_length} characters")
    if "\x00" in value:
        raise ServiceError(f"{label} contains a null character")
    return value.strip()


def object_field(value, label="body"):
    if not isinstance(value, dict):
        raise ServiceError(f"{label} must be a JSON object")
    validate_json(value)
    return value


def validate_parameters(value, schema):
    object_field(value, "parameters")
    properties = schema.get("properties", {})
    for key in schema.get("required", []):
        if key not in value:
            raise ServiceError(f"Missing parameter: {key}")
    if schema.get("additionalProperties") is False:
        for key in value:
            if key not in properties:
                raise ServiceError(f"Unknown parameter: {key}")
    for key, item in value.items():
        spec = properties.get(key, {})
        expected = spec.get("type")
        tests = {"number": lambda v: isinstance(v, (int, float)) and not isinstance(v, bool),
                 "integer": lambda v: isinstance(v, int) and not isinstance(v, bool),
                 "string": lambda v: isinstance(v, str), "boolean": lambda v: isinstance(v, bool),
                 "object": lambda v: isinstance(v, dict), "array": lambda v: isinstance(v, list)}
        if expected in tests and not tests[expected](item):
            raise ServiceError(f"Parameter {key} must be {expected}")
        if "enum" in spec and item not in spec["enum"]:
            raise ServiceError(f"Parameter {key} has an unsupported value")
        if isinstance(item, (int, float)) and not isinstance(item, bool):
            if "minimum" in spec and item < spec["minimum"] or "maximum" in spec and item > spec["maximum"]:
                raise ServiceError(f"Parameter {key} is outside its permitted range")
        if isinstance(item, str) and not spec.get("minLength", 0) <= len(item) <= spec.get("maxLength", MAX_TEXT):
            raise ServiceError(f"Parameter {key} has an invalid length")
    if len(canonical(value).encode()) > 65536:
        raise ServiceError("Parameters exceed 64 KiB")


class Service:
    def __init__(self, root=None, db_path=None, timeout=10):
        self.root = Path(root or Path(__file__).resolve().parent.parent).resolve()
        self.store = Store(db_path or self.root / "state" / "workbench.sqlite3")
        self.timeout = timeout
        self.slots = threading.BoundedSemaphore(2)
        self._network = None
        self._network_lock = threading.Lock()
        self._society = None
        self._society_lock = threading.Lock()
        self._federation = None
        self._federation_lock = threading.Lock()
        self._brain = None
        self._brain_lock = threading.Lock()
        self._harness_slot = threading.BoundedSemaphore(1)

    def harnesses_status(self):
        from .harnesses import HarnessRegistry
        result = HarnessRegistry(self.root).status()
        result['model_calls_enabled'] = os.environ.get('AUTONOMY_ALLOW_MODEL_CALLS', '').lower() == 'true'
        return result

    def harnesses_run(self, body):
        """Only a text task: callers cannot choose executables, environments or URLs."""
        from .harnesses import HarnessRegistry
        body = object_field(body)
        allowed = {'harness_id', 'prompt', 'provider', 'model', 'timeout_seconds'}
        if set(body) - allowed:
            raise ServiceError('Unknown harness request field')
        harness_id = text_field(body.get('harness_id'), 'harness_id', 40)
        prompt = text_field(body.get('prompt'), 'prompt', 12000)
        provider = text_field(body.get('provider', 'openai'), 'provider', 40)
        model = text_field(body.get('model'), 'model', 100, optional=True) or None
        timeout = body.get('timeout_seconds', 30)
        if isinstance(timeout, bool) or not isinstance(timeout, int) or not 1 <= timeout <= 60:
            raise ServiceError('timeout_seconds must be an integer in 1..60')
        if not self._harness_slot.acquire(blocking=False):
            raise ServiceError('A harness task is already running', 'busy', 429)
        try:
            result = HarnessRegistry(self.root).run(harness_id, prompt, provider=provider, model=model,
                timeout_seconds=timeout,
                allow_model_calls=os.environ.get('AUTONOMY_ALLOW_MODEL_CALLS', '').lower() == 'true')
            return self.store.record('harness_run', dict(result, id=str(uuid.uuid4()), created_at=now(),
                prompt_sha256=hashlib.sha256(prompt.encode()).hexdigest()))
        except ValueError as exc:
            raise ServiceError(str(exc)) from exc
        finally:
            self._harness_slot.release()

    def harnesses_runs(self):
        return {'items': self.store.list('harness_run')}

    @property
    def brain(self):
        with self._brain_lock:
            if self._brain is None:
                from .brain import Brain
                self._brain = Brain(self)
            return self._brain

    def brain_call(self, method, body=None):
        body = object_field({} if body is None else body)
        try:
            brain = self.brain
            routes = {'status': brain.status, 'sessions': brain.sessions,
                      'session': lambda: brain.get(text_field(body.get('id'), 'id', 100)),
                      'start': lambda: brain.start(body), 'tick': lambda: brain.tick(body),
                      'cancel': lambda: brain.cancel(body), 'export': brain.export}
            if method not in routes:
                raise ServiceError('Unknown brain operation', 'not_found', 404)
            return routes[method]()
        except KeyError as exc:
            raise ServiceError('Brain session not found', 'not_found', 404) from exc
        except ValueError as exc:
            raise ServiceError(str(exc), 'invalid_request', 400) from exc

    @property
    def federation(self):
        with self._federation_lock:
            if self._federation is None:
                from .federation.control import Federation
                self._federation = Federation(self.root, self.store.path.parent)
            return self._federation

    def federation_call(self, method, body=None):
        body = object_field(body or {})
        try:
            f = self.federation
            routes = {'status': f.status, 'candidates': f.candidates, 'export': f.export,
              'offers': lambda: f.exchange.list_offers(public=True),
              'resources': lambda: f.exchange.list_resources(public=False),
              'reviews': f.exchange.review_queue,
              'discover': lambda: f.discover(body), 'card': lambda: f.card(body),
              'proposal': lambda: f.proposal(body),
              'offer': lambda: f.exchange.create_offer(body),
              'resource': lambda: f.exchange.add_resource(body),
              'review': lambda: f.exchange.review(body),
              'cancel': lambda: f.exchange.cancel_offer(body),
              'revoke': lambda: f.exchange.revoke_member(body)}
            return routes[method]()
        except PermissionError as exc:
            raise ServiceError(str(exc), 'forbidden', 403) from exc
        except KeyError as exc:
            raise ServiceError(str(exc), 'not_found', 404) from exc
        except ValueError as exc:
            raise ServiceError(str(exc), 'invalid_request', 400) from exc

    @property
    def network(self):
        with self._network_lock:
            if self._network is None:
                from .network.control import NetworkControl
                self._network = NetworkControl(self.root, self.store.path.parent)
            return self._network

    def network_call(self, method, body=None):
        try:
            routes = {"status": self.network.status, "resources": lambda: self.network.resources((body or {}).get("q", "")),
              "campaigns": self.network.campaigns, "accounts": self.network.accounts.list,
              "discover": lambda: self.network.discover(body), "plan": lambda: self.network.plan(body),
              "campaign": lambda: self.network.create_campaign(body), "tick": lambda: self.network.tick(body),
              "cancel_campaign": lambda: self.network.cancel_campaign(body),
              "cancel_job": lambda: self.network.queue.cancel(text_field((body or {}).get("id"), "id", 100)),
              "account_save": lambda: self.network.accounts.save(body), "account_request": lambda: self.network.accounts.request(body),
              "inspect_local": lambda: self.network.inspect_local(body), "export": self.network.export}
            return routes[method]()
        except KeyError as exc:
            raise ServiceError(str(exc), "not_found", 404) from exc
        except ValueError as exc:
            raise ServiceError(str(exc), "invalid_request", 400) from exc

    @property
    def society(self):
        with self._society_lock:
            if self._society is None:
                from .society.control import Society
                self._society = Society(self.root, self.store.path.parent, self.network)
            return self._society

    def society_call(self, method, body=None):
        try:
            routes = {"status": self.society.status, "agents": lambda: self.society.agents((body or {}).get("q", "")),
                      "route": lambda: self.society.route(body), "directory": lambda: self.society.directory(body), "mission": lambda: self.society.create_mission(body),
                      "tick": lambda: self.society.tick(body), "cancel": lambda: self.society.cancel(body),
                      "claim": lambda: self.society.add_claim(body), "snapshot": lambda: self.society.snapshots.inspect(body),
                      "export": self.society.export}
            return routes[method]()
        except KeyError as exc:
            raise ServiceError(str(exc), "not_found", 404) from exc
        except ValueError as exc:
            raise ServiceError(str(exc), "invalid_request", 400) from exc

    def close(self):
        if self._brain is not None:
            self._brain.close()
        if self._federation is not None:
            self._federation.close()
        if self._society is not None:
            self._society.close()
        if self._network is not None:
            self._network.close()
        self.store.close()

    def read_data(self, name, default):
        path = self.root / "data" / name
        if not path.exists():
            return default
        return parse_json(path.read_text(encoding="utf-8"))

    def _pins(self):
        pins = self.read_data("builtin_pins.json", {}).get("files", {})
        required = ("plugin_worker.py", "workbench/plugins.py", "workbench/__init__.py", "workbench/morphogenesis.py", "workbench/kan.py", "workbench/cortical.py")
        if any(name not in pins for name in required):
            raise ServiceError("Built-in trust pins are absent; execution is disabled", "pins_missing", 503)
        verified = {}
        for name, expected in pins.items():
            path = (self.root / name).resolve()
            if not path.is_relative_to(self.root) or not path.is_file() or not isinstance(expected, str):
                raise ServiceError("Invalid built-in pin entry", "pins_invalid", 503)
            actual = hashlib.sha256(path.read_bytes()).hexdigest()
            if actual != expected:
                raise ServiceError(f"Built-in integrity check failed: {name}", "integrity_failure", 503)
            verified[name] = actual
        return verified

    def sources(self, q=""):
        if not isinstance(q, str) or len(q) > 500:
            raise ServiceError("Search query exceeds 500 characters")
        indexed = {}
        for path in sorted((self.root / "data").glob("*_sources.json")):
            data = parse_json(path.read_text(encoding="utf-8"))
            items = data if isinstance(data, list) else data.get("items", data.get("sources", []))
            for raw in items:
                if not isinstance(raw, dict):
                    continue
                source = dict(raw)
                source.setdefault("id", "src_" + hashlib.sha256((str(source.get("url", "")) + str(source.get("title", ""))).encode()).hexdigest()[:16])
                for key, default in (("title", "Untitled"), ("url", ""), ("category", "research"),
                    ("maturity", "not_assessed"), ("integration", "reference_only"), ("limitations", []), ("status", "unverified")):
                    source.setdefault(key, default)
                indexed[str(source["id"])] = source
        for source in reversed(self.store.list("source")):
            indexed[source["id"]] = source
        result = list(indexed.values())
        if q.strip():
            needle = q.casefold().strip()
            result = [item for item in result if needle in canonical(item).casefold()]
        return {"items": result}

    def add_source(self, body):
        object_field(body)
        title = text_field(body.get("title"), "title", 500)
        url = text_field(body.get("url"), "url", 3000)
        try:
            parsed = urlsplit(url)
            valid = parsed.scheme in ("http", "https") and bool(parsed.hostname) and not parsed.username and not parsed.password
            parsed.port
        except ValueError:
            valid = False
        if not valid or any(ch.isspace() or ord(ch) < 32 for ch in url) or "\\" in url:
            raise ServiceError("Source URL must be an http(s) URL without credentials", "invalid_url")
        category = text_field(body.get("category", "research"), "category", 100)
        summary = body.get("summary", "")
        if not isinstance(summary, str) or len(summary) > MAX_TEXT:
            raise ServiceError("summary must be a string of at most 12000 characters")
        source = {"id": "user_" + str(uuid.uuid4()), "title": title, "url": url,
                  "category": category, "summary": summary, "maturity": "not_assessed",
                  "integration": "reference_only", "limitations": ["Пользовательская ссылка; содержание автоматически не проверено."],
                  "status": "unverified", "created_at": now()}
        return self.store.record("source", source)

    def plugins(self):
        self._pins()
        module = importlib.import_module("workbench.plugins")
        return {"items": module.list_plugins()}

    def run(self, body):
        object_field(body)
        plugin_id = text_field(body.get("plugin_id"), "plugin_id", 100)
        pins = self._pins()
        plugins = {item["id"]: item for item in self.plugins()["items"]}
        if plugin_id not in plugins:
            raise ServiceError("Unknown built-in plugin", "unknown_plugin", 404)
        submitted_parameters = body.get("parameters", {})
        schema = plugins[plugin_id].get("parameters", {})
        validate_parameters(submitted_parameters, schema)
        parameters = {key: copy.deepcopy(spec["default"]) for key, spec in schema.get("properties", {}).items() if "default" in spec}
        parameters.update(submitted_parameters)
        if not self.slots.acquire(blocking=False):
            raise ServiceError("Two computation slots are already occupied", "busy", 429)
        record = {"id": str(uuid.uuid4()), "plugin_id": plugin_id, "parameters": parameters,
                  "created_at": now(), "status": "running", "result": None,
                  "provenance": {"version": __version__, "python": sys.version.split()[0],
                      "builtin_hashes": pins, "execution": "isolated_python_subprocess",
                      "submitted_parameters": submitted_parameters,
                      "input_sha256": hashlib.sha256(canonical(parameters).encode()).hexdigest(),
                      "isolation": "Отдельный процесс и лимиты ресурсов; не контейнерная песочница.",
                      "timeout_seconds": self.timeout}}
        try:
            with tempfile.TemporaryDirectory(prefix="meta-harness-run-") as cwd:
                # Temporary files keep child output out of RAM; the worker sets RLIMIT_FSIZE.
                with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
                    env = {"PATH": os.defpath, "LANG": "C.UTF-8", "PYTHONIOENCODING": "utf-8"}
                    proc = subprocess.Popen([sys.executable, "-I", str(self.root / "plugin_worker.py"), plugin_id],
                        stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, cwd=cwd, env=env,
                        start_new_session=(os.name == "posix"))
                    try:
                        proc.communicate(canonical(parameters).encode(), timeout=self.timeout)
                    except subprocess.TimeoutExpired:
                        if os.name == "posix":
                            try:
                                os.killpg(proc.pid, signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                        else:
                            proc.kill()
                        proc.communicate()
                        raise ServiceError("Plugin exceeded its time limit", "plugin_timeout", 504)
                    stdout.seek(0)
                    raw = stdout.read(MAX_OUTPUT + 1)
                    if len(raw) > MAX_OUTPUT:
                        raise ServiceError("Plugin output exceeds 1 MiB", "output_limit", 502)
                    if proc.returncode:
                        raise ServiceError(f"Plugin process exited with code {proc.returncode}", "plugin_failed", 502)
                    payload = parse_json(raw.decode("utf-8"))
                    if not isinstance(payload, dict) or not isinstance(payload.get("result"), dict):
                        raise ServiceError("Invalid plugin response envelope", "invalid_plugin_output", 502)
                    record["result"] = payload["result"]
                    record["provenance"]["output_sha256"] = hashlib.sha256(canonical(payload["result"]).encode()).hexdigest()
                    record["status"] = "completed"
        except (ServiceError, OSError, UnicodeError) as exc:
            record["status"] = "failed"
            record["error"] = {"message": str(exc), "code": getattr(exc, "code", "execution_error")}
        finally:
            self.slots.release()
        record["completed_at"] = now()
        return self.store.record("run", record, "run." + record["status"])

    def runs(self):
        return {"items": self.store.list("run")}

    def advisors(self):
        return {"items": council_rules.advisors()}

    def council(self, body):
        object_field(body)
        question = text_field(body.get("question"), "question")
        ids = body.get("source_ids", [])
        if not isinstance(ids, list) or len(ids) > 100 or any(not isinstance(i, str) for i in ids):
            raise ServiceError("source_ids must be a list of at most 100 strings")
        sources = {item["id"]: item for item in self.sources()["items"]}
        if any(id not in sources for id in ids):
            raise ServiceError("Unknown source ID", "unknown_source", 404)
        selected = [sources[id] for id in dict.fromkeys(ids)]
        return self.store.record("council", council_rules.review(question, selected))

    def councils(self):
        return {"items": self.store.list("council")}

    def workflow(self, body):
        object_field(body)
        question = text_field(body.get("question"), "question")
        # Validate computational inputs before creating an otherwise orphaned council.
        plugin_id = text_field(body.get("plugin_id"), "plugin_id", 100)
        items = {item["id"]: item for item in self.plugins()["items"]}
        if plugin_id not in items:
            raise ServiceError("Unknown built-in plugin", "unknown_plugin", 404)
        validate_parameters(body.get("parameters", {}), items[plugin_id].get("parameters", {}))
        review = self.council({"question": question, "source_ids": body.get("source_ids", [])})
        run = self.run({"plugin_id": plugin_id, "parameters": body.get("parameters", {})})
        summary = ("Выполнены проверка по правилам и вычислительная демонстрация. Результат требует научной валидации."
                   if run["status"] == "completed" else "Совет завершён; вычислительный этап завершился ошибкой.")
        return self.store.record("workflow", {"id": str(uuid.uuid4()), "status": run["status"],
             "question": question, "council": review, "run": run, "summary": summary, "created_at": now()})

    def workflows(self):
        return {"items": self.store.list("workflow")}

    def environment(self):
        return self.read_data("environment_status.json", {"status": "not_configured"})

    def roadmap(self):
        return self.read_data("roadmap.json", {"items": []})

    def audit(self):
        return self.store.audit()

    def status(self):
        integrity = True
        try:
            plugin_count = len(self.plugins()["items"])
        except ServiceError:
            integrity, plugin_count = False, 0
        return {"name": "Meta-Harness Research Workbench", "version": __version__,
          "mode": "research_simulation", "counts": {"sources": len(self.sources()["items"]),
              "runs": len(self.store.list("run")), "plugins": plugin_count},
          "environment": {"python": sys.version.split()[0], "platform": sys.platform,
              "database": "SQLite", "builtin_integrity": integrity, "llm": "not_configured",
              "qpu": "not_configured", "advisors": "rule_based", "bind": "127.0.0.1"},
          "limitations": ["Исследовательская платформа; способность изменять взрослый организм не реализована и не доказана.",
              "Советники используют фиксированные правила, а не независимые языковые модели.",
              "Процессные лимиты не являются полноценной песочницей."]}

    def export(self):
        with self.store.lock:
            return {"version": __version__, "created_at": now(), "status": self.status(),
              "collections": {kind: self.store.list(kind) for kind in ("run", "council", "workflow")},
              "sources": self.sources()["items"], "audit": self.audit()}

    def report(self):
        snapshot = self.export()
        lines = ["# Meta-Harness — исследовательский отчёт", "", f"Версия: {__version__}. Дата: {snapshot['created_at']}", "",
          "Советники работают по фиксированным правилам. Численные демонстрации не прогнозируют изменения целого организма.", "",
          "## Источники", ""]
        for source in snapshot["sources"]:
            title = source["title"].replace("\n", " ").replace("[", "(").replace("]", ")")
            lines.append(f"- {title}: {source.get('url', '')}")
        lines += ["", "## Вычисления", ""]
        for run in reversed(snapshot["collections"]["run"]):
            lines += [f"### {run['plugin_id']} — {run['status']}", "", f"ID: {run['id']}", "",
                "```json", json.dumps(run, ensure_ascii=False, indent=2, allow_nan=False), "```", ""]
        lines += ["## Исследовательские процессы", ""]
        for workflow in reversed(snapshot["collections"]["workflow"]):
            lines += [f"- {workflow['question']}: {workflow['summary']}"]
        lines += ["", f"Целостность локального журнала: {snapshot['audit']['valid']}.",
                  "Журнал не подписан внешним ключом и не защищает от полного переписывания базы.", ""]
        return "\n".join(lines)
