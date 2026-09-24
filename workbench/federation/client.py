"""Opt-in participant for the Meta-Harness exchange, using only the stdlib.

This client does not discover strangers, send invitations, run downloaded code,
or invent scientific results. The participant's operator chooses an offer and
submits a bounded JSON result. Bearer credentials never appear in CLI output.
"""
import argparse
from datetime import datetime, timezone
import http.client
import ipaddress
import json
import math
import os
from pathlib import Path
import ssl
import stat
import sys
import urllib.error
import urllib.request
from urllib.parse import quote, urlsplit

MAX_BODY = 1024 * 1024
MAX_CREDENTIALS = 16 * 1024
SECRET_KEYS = {"member_token", "invite_token", "token", "authorization", "api_key", "password"}


class ClientError(Exception):
    def __init__(self, message, status=None):
        super().__init__(message)
        self.status = status


def validate_base_url(value):
    if (not isinstance(value, str) or len(value) > 2048 or "\\" in value
            or any(c.isspace() or ord(c) < 32 for c in value)):
        raise ValueError("Invalid exchange URL")
    try:
        parsed = urlsplit(value)
        if (parsed.scheme not in ("http", "https") or not parsed.hostname
                or parsed.username is not None or parsed.password is not None
                or parsed.path not in ("", "/") or parsed.query or parsed.fragment):
            raise ValueError
        parsed.port
        try:
            loopback = ipaddress.ip_address(parsed.hostname).is_loopback
        except ValueError:
            loopback = parsed.hostname == "localhost"
        if parsed.scheme != "https" and not loopback:
            raise ValueError
    except (ValueError, UnicodeError):
        raise ValueError("Use HTTPS, or HTTP on literal loopback/localhost; no URL credentials, path or query") from None
    return value.rstrip("/")


def validate_token(value):
    if not isinstance(value, str) or not 32 <= len(value) <= 4096 or any(not 33 <= ord(c) <= 126 for c in value):
        raise ValueError("A credential must contain 32–4096 printable non-space ASCII characters")
    return value


def _check_json(value, depth=0):
    if depth > 24:
        raise ValueError("JSON nesting exceeds limit")
    if isinstance(value, float) and not math.isfinite(value):
        raise ValueError("JSON numbers must be finite")
    if isinstance(value, dict):
        if any(not isinstance(k, str) for k in value):
            raise ValueError("JSON keys must be strings")
        for item in value.values():
            _check_json(item, depth + 1)
    elif isinstance(value, list):
        for item in value:
            _check_json(item, depth + 1)
    elif not isinstance(value, (str, int, float, bool, type(None))):
        raise ValueError("Unsupported JSON type")


def _encode(value):
    _check_json(value)
    data = json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(",", ":")).encode("utf-8")
    if len(data) > MAX_BODY:
        raise ValueError("JSON exceeds size limit")
    return data


def _decode(data):
    if len(data) > MAX_BODY:
        raise ValueError("JSON exceeds size limit")
    def pairs(items):
        obj = {}
        for key, value in items:
            if key in obj:
                raise ValueError("Duplicate JSON key")
            obj[key] = value
        return obj
    try:
        value = json.loads(data.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(ValueError("Non-finite JSON")))
        _check_json(value)
        return value
    except (ValueError, UnicodeError, RecursionError):
        raise ValueError("Invalid finite JSON document") from None


def _public(value):
    """Defense in depth for CLI output; never echo known credential fields."""
    if isinstance(value, dict):
        return {k: _public(v) for k, v in value.items() if k.casefold() not in SECRET_KEYS}
    if isinstance(value, list):
        return [_public(v) for v in value]
    return value


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        fp.close()
        raise ClientError("Exchange redirects are forbidden", status=code)


class ParticipantClient:
    def __init__(self, base_url="http://127.0.0.1:8767", token=None, timeout=10, ca_file=None):
        self.base_url = validate_base_url(base_url)
        self._token = None if token is None else validate_token(token)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout) or not .5 <= timeout <= 60:
            raise ValueError("Timeout must be 0.5–60 seconds")
        self.timeout = timeout
        context = ssl.create_default_context(cafile=ca_file)
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        # Do not leak member credentials through environment-defined proxies.
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}),
            _NoRedirect(), urllib.request.HTTPSHandler(context=context))

    def _request(self, path, body=None, authenticated=True):
        if authenticated and self._token is None:
            raise ClientError("Set META_MEMBER_TOKEN or join using a protected credentials file")
        data = None if body is None else _encode(body)
        headers = {"Accept": "application/json", "Content-Type": "application/json"}
        if authenticated:
            headers["Authorization"] = "Bearer " + self._token
        request = urllib.request.Request(self.base_url + path, data=data, headers=headers,
                                         method="GET" if body is None else "POST")
        try:
            with self._opener.open(request, timeout=self.timeout) as response:
                if response.headers.get_content_type() != "application/json":
                    raise ClientError("Exchange returned an unsupported content type")
                raw = response.read(MAX_BODY + 1)
                if len(raw) > MAX_BODY:
                    raise ClientError("Exchange response exceeds size limit")
                try:
                    value = _decode(raw)
                except ValueError:
                    raise ClientError("Exchange returned invalid JSON") from None
                if not isinstance(value, dict):
                    raise ClientError("Exchange returned an invalid response envelope")
                return value
        except urllib.error.HTTPError as exc:
            status = exc.code
            exc.close()
            raise ClientError(f"Exchange rejected request (HTTP {status})", status=status) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise ClientError("Exchange connection failed") from None

    def join(self, invite_token, name, capabilities, terms_hash, accepted_terms=False):
        if accepted_terms is not True:
            raise ValueError("Explicit acceptance of the published offer terms is required")
        return self._request("/v1/join", {"invite_token": validate_token(invite_token), "name": name,
            "capabilities": capabilities, "accepted_terms": True, "terms_hash": terms_hash}, authenticated=False)

    def offers(self):
        return self._request("/v1/offers", authenticated=False)

    def resources(self):
        return self._request("/v1/resources", authenticated=False)

    def member(self):
        return self._request("/v1/member")

    def assignments(self):
        return self._request("/v1/assignments")

    def claim(self, offer_id, idempotency_key):
        return self._request("/v1/claim", {"offer_id": offer_id, "idempotency_key": idempotency_key})

    def submit(self, assignment_id, result, source_refs, limitations):
        return self._request("/v1/submit", {"assignment_id": assignment_id, "result": result,
                                             "source_refs": source_refs, "limitations": limitations})

    def redeem(self, resource_id):
        return self._request("/v1/redeem", {"resource_id": resource_id})

    def access(self, resource_id):
        if not isinstance(resource_id, str) or not resource_id or len(resource_id) > 200:
            raise ValueError("Invalid resource ID")
        return self._request("/v1/access?resource_id=" + quote(resource_id, safe=""))


Client = ParticipantClient


def save_credentials(path, base_url, member_id, member_token):
    """Create once, mode 0600. Refuse replacing an existing file or symlink."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    value = {"base_url": validate_base_url(base_url), "member_id": member_id,
             "member_token": validate_token(member_token), "created_at": datetime.now(timezone.utc).isoformat()}
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, 0o600)
    try:
        if os.name == "posix":
            os.fchmod(fd, 0o600)
        with os.fdopen(fd, "wb") as stream:
            fd = None
            stream.write(_encode(value))
    finally:
        if fd is not None:
            os.close(fd)


def _load_private_document(path):
    path = Path(path)
    if path.is_symlink():
        raise ValueError("Credential symlinks are not accepted")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    fd = os.open(path, flags)
    try:
        metadata = os.fstat(fd)
        if not stat.S_ISREG(metadata.st_mode) or metadata.st_size > MAX_CREDENTIALS:
            raise ValueError("Credentials must be a bounded regular file")
        if os.name == "posix" and (metadata.st_uid != os.getuid() or metadata.st_mode & 0o077):
            raise ValueError("Credentials must be owned by this user with mode 0600")
        with os.fdopen(fd, "rb") as stream:
            fd = None
            raw = stream.read(MAX_CREDENTIALS + 1)
        if len(raw) > MAX_CREDENTIALS:
            raise ValueError("Credentials exceed size limit")
        value = _decode(raw)
        if not isinstance(value, dict):
            raise ValueError("Credential file must contain a JSON object")
        return value
    finally:
        if fd is not None:
            os.close(fd)


def load_credentials(path, base_url):
    value = _load_private_document(path)
    if value.get("base_url") != validate_base_url(base_url):
        raise ValueError("Credentials belong to a different exchange")
    return validate_token(value.get("member_token"))


def parser():
    result = argparse.ArgumentParser(description=__doc__)
    result.add_argument("--url", default=os.environ.get("META_FEDERATION_URL", "http://127.0.0.1:8767"))
    result.add_argument("--credentials", default="state/federation-member.json",
                        help="Protected member file; join creates it once (default: state/federation-member.json)")
    result.add_argument("--ca-file", help="Optional trusted CA file; certificate verification remains enabled")
    result.add_argument("--timeout", type=float, default=10)
    sub = result.add_subparsers(dest="command", required=True)
    join = sub.add_parser("join", help="Accept terms and use META_INVITE_TOKEN; never print the member credential")
    join.add_argument("--name", required=True)
    join.add_argument("--capabilities", required=True, help="Comma-separated capability identifiers")
    join.add_argument("--terms-hash", help="Frozen offer hash (or read it from --invitation-file)")
    join.add_argument("--invitation-file", type=Path,
                      help="Protected local invitation JSON; alternative to META_INVITE_TOKEN")
    join.add_argument("--accept-terms", action="store_true", required=True)
    for name in ("offers", "resources", "member", "assignments"):
        sub.add_parser(name)
    claim = sub.add_parser("claim")
    claim.add_argument("offer_id")
    claim.add_argument("--idempotency-key", required=True)
    submit = sub.add_parser("submit", help="Submit JSON with result, source_refs and limitations fields")
    submit.add_argument("assignment_id")
    submit.add_argument("file", type=Path)
    for name in ("redeem", "access"):
        sub.add_parser(name).add_argument("resource_id")
    return result


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        token = None
        if args.command not in ("join", "offers", "resources"):
            token = os.environ.get("META_MEMBER_TOKEN") or load_credentials(args.credentials, args.url)
        client = ParticipantClient(args.url, token=token, timeout=args.timeout, ca_file=args.ca_file)
        if args.command == "join":
            path = Path(args.credentials)
            if path.exists() or path.is_symlink():
                raise ValueError("Credentials file already exists; choose another --credentials path")
            # Check the output location before consuming the single-use invitation.
            path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            if not os.access(path.parent, os.W_OK):
                raise ValueError("Credentials directory is not writable")
            capabilities = [item.strip() for item in args.capabilities.split(",") if item.strip()]
            invitation = _load_private_document(args.invitation_file) if args.invitation_file else {}
            terms_hash = args.terms_hash or invitation.get("terms_hash")
            if args.terms_hash and invitation and args.terms_hash != invitation.get("terms_hash"):
                raise ValueError("Invitation and requested terms do not match")
            if not terms_hash:
                raise ValueError("A frozen terms hash is required")
            invite_token = invitation.get("invite_token") if invitation else os.environ.get("META_INVITE_TOKEN")
            output = client.join(invite_token, args.name, capabilities,
                                 terms_hash, accepted_terms=args.accept_terms)
            save_credentials(path, args.url, output["member_id"], output["member_token"])
            output = {**_public(output), "credentials_saved": True}
        elif args.command in ("offers", "resources", "member", "assignments"):
            output = getattr(client, args.command)()
        elif args.command == "claim":
            output = client.claim(args.offer_id, args.idempotency_key)
        elif args.command == "submit":
            with args.file.open("rb") as stream:
                payload = _decode(stream.read(MAX_BODY + 1))
            if not isinstance(payload, dict) or set(payload) != {"result", "source_refs", "limitations"}:
                raise ValueError("Submission JSON must contain exactly result, source_refs and limitations")
            output = client.submit(args.assignment_id, **payload)
        else:
            output = getattr(client, args.command)(args.resource_id)
        print(json.dumps(_public(output), ensure_ascii=False, indent=2, allow_nan=False))
        return 0
    except ClientError as exc:
        print(json.dumps({"error": str(exc), "status": exc.status}), file=sys.stderr)
    except (ValueError, KeyError, TypeError):
        print(json.dumps({"error": "Invalid client configuration, credential, submission or server response"}), file=sys.stderr)
    except OSError:
        print(json.dumps({"error": "Cannot read or create the protected local file"}), file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
