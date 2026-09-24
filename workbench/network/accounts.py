"""Account *configuration* registry; never a credential store or signup bot.

Only environment variable names are stored. Presence is a local observation and
must not be confused with successful provider authentication. This module makes
no outbound requests, grants no scopes, and creates no remote identities.
"""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import threading
from urllib.parse import urlsplit


_PROFILE_FIELDS = frozenset({"provider", "domain", "auth_mode", "credential_env", "scopes", "setup_url"})
_MODES = frozenset({"none", "api_key", "oauth", "manual"})
_PROVIDER = re.compile(r"[a-z][a-z0-9_-]{0,63}\Z")
_ENV_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,127}\Z")
_HOST_LABEL = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\Z")
_SCOPE = re.compile(r"[A-Za-z0-9][A-Za-z0-9:._/-]{0,127}\Z")
_SECRET_PATTERN = re.compile(r"(?:\bBearer\s+\S+|\b(?:sk|ghp|github_pat|hf)-?[A-Za-z0-9_]{16,}|\b(?:api[_ -]?key|access[_ -]?token|password|secret)\s*[:=]\s*\S+)", re.IGNORECASE)

_DEFAULTS = (
    {"provider": "europepmc", "domain": "europepmc.org", "auth_mode": "none",
     "credential_env": "", "scopes": ["metadata:read"],
     "setup_url": "https://europepmc.org/RestfulWebService"},
    {"provider": "crossref", "domain": "www.crossref.org", "auth_mode": "none",
     "credential_env": "", "scopes": ["metadata:read"],
     "setup_url": "https://www.crossref.org/documentation/retrieve-metadata/rest-api/access-and-authentication/"},
)
_PUBLIC_DOMAINS = {item["provider"]: item["domain"] for item in _DEFAULTS}


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


def _text(value, field, max_length, *, allow_empty=False):
    if not isinstance(value, str) or len(value) > max_length:
        raise ValueError(f"{field}: expected a string of at most {max_length} characters")
    if any(ord(char) < 32 or ord(char) == 127 for char in value):
        raise ValueError(f"{field}: control characters are not allowed")
    result = value.strip()
    if not result and not allow_empty:
        raise ValueError(f"{field}: value is required")
    if _SECRET_PATTERN.search(result):
        raise ValueError(f"{field}: credential values are not accepted")
    return result


def _body(body, fields):
    if not isinstance(body, dict) or any(not isinstance(key, str) for key in body):
        raise ValueError("expected a JSON object")
    if set(body) - fields:
        # Do not echo rejected keys or values: these may contain a credential.
        raise ValueError("unknown fields are not accepted; submit profile metadata only")


def _provider(value):
    value = _text(value, "provider", 64)
    if not _PROVIDER.fullmatch(value):
        raise ValueError("provider: use lowercase letters, digits, underscores or hyphens")
    return value


def _validate_profile(body):
    _body(body, _PROFILE_FIELDS)
    provider = _provider(body.get("provider"))
    domain = _text(body.get("domain"), "domain", 253).lower()
    labels = domain.split(".")
    if len(labels) < 2 or not all(_HOST_LABEL.fullmatch(label) for label in labels) or labels[-1].isdigit():
        raise ValueError("domain: expected a DNS hostname, without scheme, path, credentials or port")
    mode = body.get("auth_mode")
    if not isinstance(mode, str) or mode not in _MODES:
        raise ValueError("auth_mode: choose none, api_key, oauth or manual")
    credential_env = _text(body.get("credential_env", ""), "credential_env", 128, allow_empty=True)
    if credential_env and not _ENV_NAME.fullmatch(credential_env):
        raise ValueError("credential_env: expected an uppercase environment variable NAME, never its value")
    if credential_env and mode not in {"api_key", "oauth"}:
        raise ValueError("credential_env: allowed only for api_key or oauth profiles")
    scopes = body.get("scopes", [])
    if not isinstance(scopes, list) or len(scopes) > 32:
        raise ValueError("scopes: expected a list of at most 32 scope names")
    normalized_scopes = []
    for scope in scopes:
        scope = _text(scope, "scopes", 128)
        if not _SCOPE.fullmatch(scope):
            raise ValueError("scopes: invalid scope name")
        if scope not in normalized_scopes:
            normalized_scopes.append(scope)
    url = _text(body.get("setup_url", ""), "setup_url", 2048, allow_empty=True) or f"https://{domain}"
    if any(char.isspace() for char in url) or "\\" in url:
        raise ValueError("setup_url: whitespace and backslashes are not allowed")
    try:
        parsed = urlsplit(url)
        valid = (parsed.scheme == "https" and parsed.hostname == domain and
                 parsed.username is None and parsed.password is None and
                 parsed.port in (None, 443) and not parsed.query and not parsed.fragment)
    except ValueError:
        valid = False
    if not valid:
        raise ValueError("setup_url: expected HTTPS on the exact profile hostname without credentials, query or fragment")
    # Public connector identity cannot silently be redirected to another domain.
    if provider in _PUBLIC_DOMAINS and domain != _PUBLIC_DOMAINS[provider]:
        raise ValueError("domain: reserved public provider hostname cannot be changed")
    return {"provider": provider, "domain": domain, "auth_mode": mode,
            "credential_env": credential_env, "scopes": normalized_scopes, "setup_url": url}


def _is_public(profile):
    return (profile["provider"] in _PUBLIC_DOMAINS and profile["auth_mode"] == "none" and
            profile["domain"] == _PUBLIC_DOMAINS[profile["provider"]] and
            set(profile["scopes"]) <= {"metadata:read"})


class Accounts:
    """Persistent non-secret provider profiles and idempotent setup handoffs."""

    def __init__(self, path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), timeout=15, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA busy_timeout=15000")
        with self._conn:
            self._conn.execute("CREATE TABLE IF NOT EXISTS account_profiles (provider TEXT PRIMARY KEY, body TEXT NOT NULL, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
            self._conn.execute("CREATE TABLE IF NOT EXISTS account_requests (id TEXT PRIMARY KEY, provider TEXT NOT NULL, reason TEXT NOT NULL, body TEXT NOT NULL, UNIQUE(provider, reason))")
            for profile in _DEFAULTS:
                now = _now()
                self._conn.execute("INSERT OR IGNORE INTO account_profiles VALUES (?,?,?,?)",
                                   (profile["provider"], json.dumps(profile), now, now))

    @staticmethod
    def _public(row):
        profile = json.loads(row["body"])
        name = profile["credential_env"]
        present = bool(name and os.environ.get(name))
        if _is_public(profile):
            status = "public_metadata_access"
        elif profile["auth_mode"] == "none":
            status = "access_unverified"
        elif present:
            status = "credential_present_unverified"
        else:
            status = "requires_setup"
        return {**profile, "status": status, "credential_present": present,
                "remote_auth_verified": False, "created_at": row["created_at"],
                "updated_at": row["updated_at"]}

    def list(self):
        with self._lock:
            profiles = self._conn.execute("SELECT * FROM account_profiles ORDER BY provider").fetchall()
            requests = self._conn.execute("SELECT body FROM account_requests ORDER BY rowid DESC").fetchall()
        return {"items": [self._public(row) for row in profiles],
                "requests": [json.loads(row["body"]) for row in requests]}

    def save(self, body):
        profile = _validate_profile(body)
        now = _now()
        with self._lock, self._conn:
            self._conn.execute("INSERT INTO account_profiles VALUES (?,?,?,?) ON CONFLICT(provider) DO UPDATE SET body=excluded.body, updated_at=excluded.updated_at",
                               (profile["provider"], json.dumps(profile, ensure_ascii=False), now, now))
            row = self._conn.execute("SELECT * FROM account_profiles WHERE provider=?", (profile["provider"],)).fetchone()
        return self._public(row)

    def request(self, body):
        _body(body, frozenset({"provider", "reason"}))
        provider = _provider(body.get("provider"))
        reason = _text(body.get("reason"), "reason", 1000)
        with self._lock, self._conn:
            row = self._conn.execute("SELECT * FROM account_profiles WHERE provider=?", (provider,)).fetchone()
            if row is None:
                raise KeyError("unknown provider; save its configuration first")
            existing = self._conn.execute("SELECT body FROM account_requests WHERE provider=? AND reason=?", (provider, reason)).fetchone()
            if existing:
                return json.loads(existing["body"])
            profile = json.loads(row["body"])
            public = _is_public(profile)
            digest = hashlib.sha256(json.dumps([provider, reason], ensure_ascii=False).encode()).hexdigest()[:24]
            now = _now()
            request = {"id": f"setup_{digest}", "provider": provider, "reason": reason,
                       "status": "no_registration_required" if public else "requires_user_action",
                       "setup_url": profile["setup_url"], "auth_mode": profile["auth_mode"],
                       "created_at": now, "updated_at": now, "external_action_performed": False,
                       "next_step": ("Публичный поиск метаданных доступен без регистрации; доступность сети проверяется отдельным заданием."
                                     if public else "Откройте официальный сервис, выберите собственную учётную запись и необходимые права; задайте секрет в окружении сервера. Профиль сам по себе не подключает адаптер.")}
            self._conn.execute("INSERT OR IGNORE INTO account_requests VALUES (?,?,?,?)", (request["id"], provider, reason, json.dumps(request, ensure_ascii=False)))
            # Return the stored record if another coordinator won the unique insert.
            stored = self._conn.execute("SELECT body FROM account_requests WHERE provider=? AND reason=?", (provider, reason)).fetchone()
            return json.loads(stored["body"])

    def close(self):
        with self._lock:
            self._conn.close()
