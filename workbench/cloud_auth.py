"""Owner-only Yandex ID login; no model calls, token persistence or framework.

The embedding HTTPS server must apply this guard to every private read/write and
must not log callback queries, Cookie, CSRF, Authorization or token responses.
Sessions intentionally die on process restart. One process is supported.
"""
from __future__ import annotations

import base64
from dataclasses import dataclass, field
import hashlib
import hmac
import json
import math
import re
import secrets
import threading
import time
from urllib.parse import urlencode, urlsplit
from urllib.request import HTTPRedirectHandler, ProxyHandler, Request, build_opener


AUTHORIZE_URL = "https://oauth.yandex.ru/authorize"
TOKEN_URL = "https://oauth.yandex.ru/token"
PROFILE_URL = "https://login.yandex.ru/info?format=json"
CALLBACK_PATH = "/auth/yandex/callback"
SESSION_COOKIE = "__Host-neuromorph_session"
FLOW_COOKIE = "__Host-neuromorph_oauth"
FLOW_TTL = 600
SESSION_TTL = 3600
MAX_PENDING = 32
MAX_SESSIONS = 16
MAX_RESPONSE_BYTES = 65536
REQUEST_TIMEOUT = 10
_TOKEN = re.compile(r"[A-Za-z0-9_-]{43}\Z")
_SUBJECT = re.compile(r"[0-9]{1,64}\Z")
_CLIENT = re.compile(r"[A-Za-z0-9_-]{1,128}\Z")
_DOMAIN = re.compile(r"[a-z0-9](?:[a-z0-9.-]{0,251}[a-z0-9])?\Z")
_ERRORS = {
    "auth_unconfigured": 503, "auth_config_invalid": 503,
    "invalid_request": 400, "invalid_state": 401, "login_denied": 403,
    "owner_required": 403, "unauthorized": 401, "csrf_rejected": 403,
    "auth_capacity": 503, "oauth_unavailable": 503, "clock_unavailable": 503,
}


class AuthError(Exception):
    """Fixed public error; never embed an upstream exception or input."""

    def __init__(self, code: str):
        if code not in _ERRORS:
            code = "oauth_unavailable"
        self.code = code
        self.status = _ERRORS[code]
        super().__init__(code)


def _text(value, maximum):
    return (type(value) is str and 0 < len(value) <= maximum
            and all(32 <= ord(c) <= 126 for c in value))


@dataclass(frozen=True)
class Config:
    public_origin: str
    client_id: str
    client_secret: str = field(repr=False)
    owner_id: str = field(repr=False)

    def __post_init__(self):
        try:
            if not _text(self.public_origin, 300):
                raise ValueError()
            parsed = urlsplit(self.public_origin)
            host = parsed.hostname or ""
            if (parsed.scheme != "https" or not _DOMAIN.fullmatch(host)
                    or "." not in host or ".." in host or host.endswith(".")
                    or any(not part or part.startswith("-") or part.endswith("-")
                           or len(part) > 63 for part in host.split("."))
                    or parsed.username is not None or parsed.password is not None
                    or parsed.path or parsed.query or parsed.fragment
                    or parsed.netloc != host + (f":{parsed.port}" if parsed.port is not None else "")
                    or (parsed.port is not None and not 1 <= parsed.port <= 65535)):
                raise ValueError()
            if (type(self.client_id) is not str or not _CLIENT.fullmatch(self.client_id)
                    or not _text(self.client_secret, 512)
                    or type(self.owner_id) is not str or not _SUBJECT.fullmatch(self.owner_id)):
                raise ValueError()
        except (ValueError, TypeError, AttributeError):
            raise AuthError("auth_config_invalid") from None

    @classmethod
    def from_env(cls, env):
        names = ("NEUROMORPH_PUBLIC_ORIGIN", "NEUROMORPH_YANDEX_CLIENT_ID",
                 "NEUROMORPH_YANDEX_CLIENT_SECRET", "NEUROMORPH_YANDEX_OWNER_ID")
        values = [env.get(name) for name in names]
        if any(value is None or value == "" for value in values):
            raise AuthError("auth_unconfigured")
        return cls(*values)

    @property
    def redirect_uri(self):
        return self.public_origin + CALLBACK_PATH


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        raise AuthError("oauth_unavailable")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate JSON key")
        result[key] = value
    return result


def _invalid_constant(value):
    raise ValueError("non-finite JSON")


def request_json(method, url, headers, body):
    """Fixed-origin bounded OAuth transport; injectable in CloudAuth tests.

    No automatic proxy discovery or redirects. Nothing is logged. Callers may
    inject a trusted transport, which must preserve these security properties.
    """
    try:
        if not ((method == "POST" and url == TOKEN_URL and type(body) is bytes
                 and len(body) <= 16384)
                or (method == "GET" and url == PROFILE_URL and body is None)):
            raise ValueError()
        request = Request(url, data=body, headers=headers, method=method)
        opener = build_opener(ProxyHandler({}), _NoRedirect())
        with opener.open(request, timeout=REQUEST_TIMEOUT) as response:
            if (response.status != 200 or response.geturl() != url
                    or response.headers.get("Content-Type", "").split(";", 1)[0].strip().lower()
                    != "application/json"):
                raise ValueError()
            raw = response.read(MAX_RESPONSE_BYTES + 1)
        if type(raw) is not bytes or len(raw) > MAX_RESPONSE_BYTES:
            raise ValueError()
        result = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_object,
                            parse_constant=_invalid_constant)
        if type(result) is not dict:
            raise ValueError()
        return result
    except Exception:
        raise AuthError("oauth_unavailable") from None


def _digest(value):
    return hashlib.sha256(value.encode("ascii")).hexdigest()


def _cookie(name, value, max_age):
    return f"{name}={value}; Path=/; Max-Age={max_age}; Secure; HttpOnly; SameSite=Lax"


def _read_cookie(header, name):
    if (type(header) is not str or len(header) > 8192
            or any(ord(c) < 32 or ord(c) == 127 for c in header)):
        return None
    found = []
    parts = header.split(";")
    if len(parts) > 64:
        return None
    for item in parts:
        key, sep, value = item.strip().partition("=")
        if key == name:
            if not sep or not _TOKEN.fullmatch(value):
                return None
            found.append(value)
    return found[0] if len(found) == 1 else None


class CloudAuth:
    def __init__(self, config: Config, transport=None, clock=time.time):
        if type(config) is not Config:
            raise AuthError("auth_config_invalid")
        self.config = config
        self._transport = transport if transport is not None else request_json
        self._clock = clock
        self._lock = threading.Lock()
        self._pending = {}
        self._sessions = {}

    def _now(self):
        try:
            value = self._clock()
            if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
                raise ValueError()
            return value
        except Exception:
            raise AuthError("clock_unavailable") from None

    def _prune(self, now):
        for mapping in (self._pending, self._sessions):
            for key in list(mapping):
                if mapping[key]["expires_at"] <= now:
                    del mapping[key]

    def begin(self):
        now = self._now()
        state, binding, verifier = (secrets.token_urlsafe(32) for _ in range(3))
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).rstrip(b"=").decode("ascii")
        with self._lock:
            self._prune(now)
            if len(self._pending) >= MAX_PENDING:
                raise AuthError("auth_capacity")
            self._pending[_digest(state)] = {
                "binding": _digest(binding), "verifier": verifier,
                "expires_at": now + FLOW_TTL,
            }
        query = urlencode({"response_type": "code", "client_id": self.config.client_id,
                           "redirect_uri": self.config.redirect_uri, "scope": "login:info",
                           "state": state, "code_challenge": challenge,
                           "code_challenge_method": "S256"})
        return {"authorize_url": AUTHORIZE_URL + "?" + query,
                "set_cookie": _cookie(FLOW_COOKIE, binding, FLOW_TTL)}

    def callback(self, query, cookie_header):
        # HTTP adapter must reject duplicate query keys before making this dict.
        if (type(query) is not dict or not set(query) <=
                {"code", "state", "error", "error_description", "error_uri"}
                or not all(_text(value, 4096) for value in query.values())
                or not _TOKEN.fullmatch(query.get("state", ""))
                or ("code" in query) == ("error" in query)):
            raise AuthError("invalid_request")
        binding = _read_cookie(cookie_header, FLOW_COOKIE)
        now = self._now()
        with self._lock:
            self._prune(now)
            key = _digest(query["state"])
            pending = self._pending.get(key)
            if (binding is None or pending is None
                    or not hmac.compare_digest(pending["binding"], _digest(binding))):
                raise AuthError("invalid_state")
            # Consume before any network operation, including provider failures.
            del self._pending[key]
        if "error" in query:
            raise AuthError("login_denied")
        body = urlencode({"grant_type": "authorization_code", "code": query["code"],
                          "client_id": self.config.client_id, "client_secret": self.config.client_secret,
                          "redirect_uri": self.config.redirect_uri,
                          "code_verifier": pending["verifier"]}).encode("ascii")
        try:
            token = self._transport("POST", TOKEN_URL,
                                    {"Content-Type": "application/x-www-form-urlencoded", "Accept": "application/json"}, body)
            if (type(token) is not dict or "error" in token
                    or not _text(token.get("access_token"), 8192)
                    or token.get("token_type", "").lower() != "bearer"):
                raise ValueError()
            profile = self._transport("GET", PROFILE_URL,
                                      {"Authorization": "OAuth " + token["access_token"], "Accept": "application/json"}, None)
            if (type(profile) is not dict or type(profile.get("id")) is not str
                    or not _SUBJECT.fullmatch(profile["id"])
                    or profile.get("client_id") != self.config.client_id):
                raise ValueError()
        except Exception:
            raise AuthError("oauth_unavailable") from None
        if not hmac.compare_digest(profile["id"], self.config.owner_id):
            raise AuthError("owner_required")
        now = self._now()
        session_id, csrf = secrets.token_urlsafe(32), secrets.token_urlsafe(32)
        session = {"subject": self.config.owner_id, "csrf_token": csrf,
                   "expires_at": now + SESSION_TTL}
        with self._lock:
            self._prune(now)
            if len(self._sessions) >= MAX_SESSIONS:
                raise AuthError("auth_capacity")
            self._sessions[_digest(session_id)] = session
        return dict(session, set_cookie=_cookie(SESSION_COOKIE, session_id, SESSION_TTL),
                    clear_oauth_cookie=_cookie(FLOW_COOKIE, "", 0))

    def session(self, cookie_header):
        session_id = _read_cookie(cookie_header, SESSION_COOKIE)
        now = self._now()
        with self._lock:
            self._prune(now)
            session = self._sessions.get(_digest(session_id)) if session_id else None
            if session is None:
                raise AuthError("unauthorized")
            return dict(session)

    def authorize_write(self, cookie_header, origin, csrf_token):
        session = self.session(cookie_header)
        if (type(origin) is not str or origin != self.config.public_origin
                or type(csrf_token) is not str or not _TOKEN.fullmatch(csrf_token)
                or not hmac.compare_digest(csrf_token, session["csrf_token"])):
            raise AuthError("csrf_rejected")
        return session

    def logout(self, cookie_header, origin, csrf_token):
        self.authorize_write(cookie_header, origin, csrf_token)
        session_id = _read_cookie(cookie_header, SESSION_COOKIE)
        with self._lock:
            self._sessions.pop(_digest(session_id), None)
        return {"set_cookie": _cookie(SESSION_COOKIE, "", 0)}
