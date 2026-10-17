#!/usr/bin/env python3
"""Read-only screening of this standalone, explicitly synthetic release package."""
import json
import re
import stat
import sys
from collections import Counter
from html.parser import HTMLParser
from pathlib import Path, PurePosixPath
from urllib.parse import unquote, urlsplit

ROOT = Path(__file__).resolve().parents[1]
MAX_FILE_SIZE = 128 * 1024
PATTERNS = {
    "email-like address": r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}",
    "remote URL": r"(?i)\b(?:https?|wss?)://[^\s<>'\"]+",
    "private host path": r"(?i)/(?:home|users|workspace|tmp)/|[a-z]:\\(?:users|documents)\\",
    "private key block": r"-----BEGIN (?:[A-Z ]+ )?PRIVATE KEY-----",
    "credential-shaped value": r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,}|xox[baprs]-[A-Za-z0-9-]{12,}|sk-[A-Za-z0-9_-]{20,}|AKIA[A-Z0-9]{16})\b",
    "UUID-like identifier": r"\b[0-9a-fA-F]{8}(?:-[0-9a-fA-F]{4}){3}-[0-9a-fA-F]{12}\b",
    "service/internal identifier": r"\b(?:[TCDF][A-Z0-9]{10}|BW-COORD-\d+|(?:runtime|tunnel|environment|dispatch|thread)_[a-zA-Z0-9]{8,})\b",
}


class Markup(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.ids = []
        self.links = []
        self.unsafe = []
        self.csp = ""
        self.inline_script = False
        self._script = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if "id" in attrs:
            self.ids.append(attrs["id"])
        for key, value in attrs.items():
            if key in {"href", "src", "action", "poster", "data"}:
                self.links.append(value or "")
            if key.startswith("on") or key == "style" or key == "srcdoc":
                self.unsafe.append(key)
        if tag in {"iframe", "object", "embed", "base"}:
            self.unsafe.append(tag)
        if tag == "meta" and attrs.get("http-equiv", "").lower() == "content-security-policy":
            self.csp = attrs.get("content", "")
        if tag == "script":
            self._script = True
            if attrs.get("src") != "app.js":
                self.unsafe.append("unexpected script")

    def handle_endtag(self, tag):
        if tag == "script":
            self._script = False

    def handle_data(self, value):
        if self._script and value.strip():
            self.inline_script = True


def validate(root):
    root = Path(root).resolve()
    errors = []
    manifest = root / "allowlist.txt"
    if not manifest.is_file() or manifest.is_symlink():
        return ["Missing regular allowlist.txt"], {}
    try:
        listed = manifest.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeError):
        return ["Unreadable UTF-8 allowlist"], {}
    if listed != sorted(set(listed)) or not listed:
        errors.append("Allowlist must be sorted, unique and nonempty")
    for name in listed:
        path = PurePosixPath(name)
        if not name or path.is_absolute() or ".." in path.parts or any(part.startswith(".") for part in path.parts) or "\\" in name:
            errors.append("Unsafe allowlist path")
    if errors:
        return errors, {}
    expected = set(listed)
    allowed_dirs = {str(parent) for name in listed for parent in PurePosixPath(name).parents if str(parent) != "."}
    actual = set()
    for path in root.rglob("*"):
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            errors.append(f"Symlink forbidden: {relative}")
        elif path.is_dir():
            if relative not in allowed_dirs:
                errors.append(f"Unexpected directory: {relative}")
        elif stat.S_ISREG(path.stat().st_mode):
            actual.add(relative)
        else:
            errors.append(f"Non-regular file: {relative}")
    if actual != expected:
        errors.append(f"Allowlist mismatch: {len(actual - expected)} extra, {len(expected - actual)} missing")
    texts = {}
    for name in sorted(expected & actual):
        path = root / name
        if path.stat().st_size > MAX_FILE_SIZE:
            errors.append(f"File exceeds size limit: {name}")
            continue
        try:
            value = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            errors.append(f"Not readable UTF-8: {name}")
            continue
        texts[name] = value
        if any(ord(c) < 32 and c not in "\n\r\t" for c in value):
            errors.append(f"Control character: {name}")
        for label, pattern in PATTERNS.items():
            if re.search(pattern, value):
                errors.append(f"Potential {label}: {name}")
    if sum(len(value.encode("utf-8")) for value in texts.values()) > 256 * 1024:
        errors.append("Package exceeds expected small-demo size")

    dataset = {}
    try:
        dataset = json.loads(texts.get("demo-data.json", ""))
        if set(dataset) != {"schema_version", "synthetic", "dataset_label", "tasks"}:
            errors.append("Unexpected dataset fields")
        if dataset.get("schema_version") != 1 or dataset.get("synthetic") is not True or dataset.get("dataset_label") != "Entirely fictional Atlas examples":
            errors.append("Dataset is not the declared synthetic fixture")
        tasks = dataset.get("tasks")
        if not isinstance(tasks, list) or len(tasks) != 6:
            errors.append("Expected six synthetic cards")
            tasks = []
        required = {"id", "synthetic", "title", "status", "category", "summary", "acceptance", "evidence"}
        for task in tasks:
            if not isinstance(task, dict) or set(task) != required:
                errors.append("Unexpected task fields")
                continue
            if task["synthetic"] is not True or task["status"] not in {"proposed", "review", "complete"} or task["category"] not in {"interface", "documentation", "quality"}:
                errors.append("Invalid synthetic task values")
            for field in {"id", "title", "summary", "acceptance", "evidence"}:
                if not isinstance(task[field], str) or not 1 <= len(task[field]) <= 400:
                    errors.append(f"Invalid task text field: {field}")
        if {task.get("id") for task in tasks if isinstance(task, dict)} != {f"DEMO-{i:03d}" for i in range(1, 7)}:
            errors.append("Expected unique demo-only identifiers")
        if Counter(task.get("status") for task in tasks if isinstance(task, dict)) != Counter(proposed=2, review=2, complete=2):
            errors.append("Unexpected sample status distribution")
    except (ValueError, TypeError, AttributeError):
        errors.append("Malformed synthetic JSON")

    markup = {}
    for name, value in texts.items():
        if name.endswith(".html"):
            parsed = Markup()
            parsed.feed(value)
            markup[name] = parsed
            if parsed.unsafe or parsed.inline_script:
                errors.append(f"Unsafe or unexpected HTML feature: {name}")
            if len(parsed.ids) != len(set(parsed.ids)):
                errors.append(f"Duplicate HTML IDs: {name}")
            if not all(rule in parsed.csp for rule in ["default-src 'none'", "script-src 'self'", "connect-src 'self'", "base-uri 'none'", "form-action 'none'"]):
                errors.append(f"Missing restrictive demo CSP: {name}")
    link_count = 0
    for name, value in texts.items():
        links = markup[name].links if name in markup else []
        if name.endswith(".md"):
            links += re.findall(r"\[[^\]]+\]\(([^\s)]+)\)", value)
        for link in links:
            link_count += 1
            parsed = urlsplit(link)
            if parsed.scheme or parsed.netloc or parsed.query or not link or "\\" in link or parsed.path.startswith("/"):
                errors.append(f"Nonlocal or unsupported link in {name}")
                continue
            target = (root / name).parent / unquote(parsed.path) if parsed.path else root / name
            target = target.resolve()
            if not target.is_relative_to(root) or target.relative_to(root).as_posix() not in expected:
                errors.append(f"Missing or out-of-package link target in {name}")
                continue
            target_name = target.relative_to(root).as_posix()
            if parsed.fragment and (target_name not in markup or unquote(parsed.fragment) not in markup[target_name].ids):
                errors.append(f"Missing HTML fragment in {name}")
    javascript = texts.get("app.js", "")
    if re.findall(r"\bfetch\(\s*['\"]([^'\"]+)", javascript) != ["demo-data.json"]:
        errors.append("Unexpected browser request target")
    if re.search(r"\b(?:innerHTML|outerHTML|eval|WebSocket|EventSource|sendBeacon|localStorage|sessionStorage|serviceWorker)\b", javascript):
        errors.append("Unexpected browser execution, storage or network API")
    if re.search(r"@import|url\s*\(", texts.get("styles.css", ""), re.I):
        errors.append("CSS resource reference forbidden")
    if "SYNTHETIC DATA" not in texts.get("index.html", ""):
        errors.append("Missing visible synthetic-data notice")
    return errors, {"files": len(texts), "cards": len(dataset.get("tasks", [])) if isinstance(dataset, dict) and isinstance(dataset.get("tasks"), list) else 0, "links": link_count}


def main():
    errors, counts = validate(ROOT)
    if errors:
        for error in errors:
            print(f"FAIL: {error}")
        return 1
    print(f"PASS: {counts['files']} allowlisted UTF-8 files; {counts['cards']} synthetic cards; {counts['links']} local links/fragments")
    print("PASS: no symlinks, external resource references or screened sensitive patterns")
    print("LIMIT: heuristic screening; human privacy and license review still required")
    return 0


if __name__ == "__main__":
    sys.exit(main())
