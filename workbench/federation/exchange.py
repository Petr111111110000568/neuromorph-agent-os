"""Opt-in exchange of bounded computational research and authorized summaries.

No network access, execution of submitted code, monetary transfers, organization
verification or genomic payload sharing. Administrative methods are an in-process
API: the transport MUST restrict them to its local administrator.
"""
from contextlib import contextmanager
import hashlib
import hmac
import json
from pathlib import Path
import re
import secrets
import sqlite3
import threading
import time


TASK_TYPES = {"literature_review", "dataset_qc", "method_reproduction"}
GRANT_SCOPES = ["offers:read", "assignments:claim_invited", "results:submit_own", "summaries:redeem"]
MAX_JSON_BYTES = 65536
MAX_ROWS = 10000


def _json(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _hash(value):
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _text(value, field, limit=2000):
    if not isinstance(value, str) or not value.strip() or len(value) > limit or "\x00" in value:
        raise ValueError(f"{field}: expected nonempty text, at most {limit} characters")
    return value.strip()


def _integer(value, field, minimum, maximum):
    if type(value) is not int or not minimum <= value <= maximum:
        raise ValueError(f"{field}: expected integer in [{minimum}, {maximum}]")
    return value


def _list(value, field, *, maximum=30, item_limit=2000, minimum=0):
    if not isinstance(value, list) or not minimum <= len(value) <= maximum:
        raise ValueError(f"{field}: expected list with {minimum}..{maximum} items")
    return [_text(item, field, item_limit) for item in value]


def _body(value, allowed, required=()):
    if not isinstance(value, dict) or set(value) - set(allowed) or set(required) - set(value):
        raise ValueError("Unexpected or missing request fields")
    try:
        if len(_json(value).encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("Request exceeds size limit")
    except (TypeError, RecursionError, OverflowError, UnicodeError) as exc:
        raise ValueError("Request is not bounded JSON") from exc
    return value


def _id(prefix):
    return prefix + "_" + secrets.token_hex(12)


def _key(value, field):
    value = _text(value, field, 160)
    if not re.fullmatch(r"[A-Za-z0-9_.:-]+", value):
        raise ValueError(f"{field}: invalid identifier")
    return value


class Exchange:
    """Durable SQLite exchange. Every mutating call has one serialized transaction."""

    def __init__(self, db_path):
        self.db_path = Path(db_path)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.db_path), timeout=15, isolation_level=None, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA foreign_keys=ON")
        self._conn.execute("PRAGMA busy_timeout=15000")
        self._conn.executescript("""
            CREATE TABLE IF NOT EXISTS offers (
              id TEXT PRIMARY KEY, terms TEXT NOT NULL, terms_hash TEXT NOT NULL,
              reward INTEGER NOT NULL, max_assignments INTEGER NOT NULL,
              lease_seconds INTEGER NOT NULL, created REAL NOT NULL, expires REAL NOT NULL,
              status TEXT NOT NULL DEFAULT 'open', cancellation_reason TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS invitations (
              id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL,
              offer_id TEXT NOT NULL REFERENCES offers(id), created REAL NOT NULL,
              expires REAL NOT NULL, used REAL);
            CREATE TABLE IF NOT EXISTS members (
              id TEXT PRIMARY KEY, token_hash TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
              capabilities TEXT NOT NULL, offer_id TEXT NOT NULL REFERENCES offers(id),
              terms_hash TEXT NOT NULL, created REAL NOT NULL, revoked REAL,
              revocation_reason TEXT NOT NULL DEFAULT '');
            CREATE TABLE IF NOT EXISTS assignments (
              id TEXT PRIMARY KEY, offer_id TEXT NOT NULL REFERENCES offers(id),
              member_id TEXT NOT NULL REFERENCES members(id), idempotency_key TEXT NOT NULL,
              status TEXT NOT NULL, created REAL NOT NULL, lease_expires REAL NOT NULL,
              submitted REAL, result TEXT, result_hash TEXT, source_refs TEXT, limitations TEXT,
              reviewed REAL, rationale TEXT, UNIQUE(member_id, idempotency_key));
            CREATE INDEX IF NOT EXISTS assignments_offer_status ON assignments(offer_id,status);
            CREATE TABLE IF NOT EXISTS resources (
              id TEXT PRIMARY KEY, metadata TEXT NOT NULL, content TEXT NOT NULL,
              content_hash TEXT NOT NULL, cost INTEGER NOT NULL, created REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS ledger (
              id TEXT PRIMARY KEY, member_id TEXT NOT NULL REFERENCES members(id),
              kind TEXT NOT NULL, ref_id TEXT NOT NULL, amount INTEGER NOT NULL, created REAL NOT NULL,
              UNIQUE(kind, ref_id));
            CREATE TABLE IF NOT EXISTS grants (
              member_id TEXT NOT NULL REFERENCES members(id),
              resource_id TEXT NOT NULL REFERENCES resources(id), created REAL NOT NULL,
              PRIMARY KEY(member_id,resource_id));
        """)

    def close(self):
        with self._lock:
            self._conn.close()

    @contextmanager
    def _tx(self):
        with self._lock:
            self._conn.execute("BEGIN IMMEDIATE")
            try:
                yield self._conn
            except BaseException:
                self._conn.rollback()
                raise
            else:
                self._conn.commit()

    def _capacity(self, conn, table):
        # Table identifiers only come from code constants, never from callers.
        if conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0] >= MAX_ROWS:
            raise ValueError("Local exchange record limit reached")

    def _expire(self, conn):
        now = time.time()
        conn.execute("UPDATE assignments SET status='expired' WHERE status='claimed' AND lease_expires<=?", (now,))
        conn.execute("UPDATE offers SET status='expired' WHERE status='open' AND expires<=?", (now,))

    def _authenticate(self, conn, token):
        if not isinstance(token, str) or re.fullmatch(r"[A-Za-z0-9_-]{43}", token) is None:
            raise PermissionError("Invalid admitted-member credential")
        row = conn.execute("SELECT * FROM members WHERE token_hash=?", (_hash(token),)).fetchone()
        if row is None or row["revoked"] is not None:
            raise PermissionError("Invalid or revoked admitted-member credential")
        return row

    @staticmethod
    def _balance(conn, member_id):
        return conn.execute("SELECT COALESCE(sum(amount),0) FROM ledger WHERE member_id=?", (member_id,)).fetchone()[0]

    def _member_view(self, conn, row):
        return {"member_id": row["id"], "name": row["name"], "capabilities": json.loads(row["capabilities"]),
                "offer_id": row["offer_id"], "terms_hash": row["terms_hash"], "created_at": row["created"],
                "revoked": row["revoked"] is not None, "credits": self._balance(conn, row["id"]),
                "identity_assurance": "admitted_credential_only", "organization_verified": False}

    def member(self, token):
        with self._tx() as conn:
            return self._member_view(conn, self._authenticate(conn, token))

    def _offer_view(self, conn, row):
        statuses = {x["status"]: x["n"] for x in conn.execute(
            "SELECT status,count(*) n FROM assignments WHERE offer_id=? GROUP BY status", (row["id"],))}
        reserved = statuses.get("claimed", 0) + statuses.get("submitted", 0)
        accepted = statuses.get("accepted", 0)
        return {"offer_id": row["id"], **json.loads(row["terms"]), "terms_hash": row["terms_hash"],
                "status": row["status"], "created_at": row["created"], "expires_at": row["expires"],
                "accepted_count": accepted, "reserved_count": reserved,
                "available_slots": max(0, row["max_assignments"] - accepted - reserved) if row["status"] == "open" else 0,
                "budget_credits": row["reward"] * row["max_assignments"],
                "rewarded_credits": accepted * row["reward"], "reserved_credits": reserved * row["reward"]}

    def create_offer(self, body):
        body = _body(body, {"title", "description", "task_type", "requirements", "reward_credits", "max_assignments",
                            "data_class", "lease_seconds", "expires_in_seconds"},
                     {"title", "description", "task_type", "requirements", "reward_credits", "max_assignments", "data_class"})
        if body["data_class"] != "public" or not isinstance(body["task_type"], str) or body["task_type"] not in TASK_TYPES:
            raise ValueError("Only public computational research tasks are admitted")
        terms = {"title": _text(body["title"], "title", 200), "description": _text(body["description"], "description", 8000),
                 "task_type": body["task_type"], "requirements": _list(body["requirements"], "requirements", minimum=1),
                 "reward_credits": _integer(body["reward_credits"], "reward_credits", 0, 10000),
                 "max_assignments": _integer(body["max_assignments"], "max_assignments", 1, 1000),
                 "data_class": "public", "grant_scopes": GRANT_SCOPES,
                 "lease_seconds": _integer(body.get("lease_seconds", 3600), "lease_seconds", 10, 604800),
                 "review_policy": "local_admin_manual", "reward_unit": "nonmonetary_research_credit",
                 "result_publication": "no_automatic_publication", "terms_version": "1"}
        ttl = _integer(body.get("expires_in_seconds", 604800), "expires_in_seconds", 30, 2592000)
        now, offer_id = time.time(), _id("offer")
        encoded = _json(terms)
        with self._tx() as conn:
            self._capacity(conn, "offers")
            conn.execute("INSERT INTO offers(id,terms,terms_hash,reward,max_assignments,lease_seconds,created,expires) VALUES(?,?,?,?,?,?,?,?)",
                         (offer_id, encoded, _hash(encoded), terms["reward_credits"], terms["max_assignments"], terms["lease_seconds"], now, now + ttl))
            return self._offer_view(conn, conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone())

    def list_offers(self, public=True):
        with self._tx() as conn:
            self._expire(conn)
            return {"items": [self._offer_view(conn, row) for row in conn.execute("SELECT * FROM offers ORDER BY created,id")]}

    def create_invitation(self, body):
        body = _body(body, {"offer_id", "expires_in_seconds"}, {"offer_id"})
        offer_id = _key(body["offer_id"], "offer_id")
        ttl = _integer(body.get("expires_in_seconds", 86400), "expires_in_seconds", 10, 604800)
        token, invitation_id, now = secrets.token_urlsafe(32), _id("invite"), time.time()
        with self._tx() as conn:
            self._expire(conn)
            offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
            if offer is None:
                raise KeyError("Unknown offer")
            if offer["status"] != "open":
                raise ValueError("Offer is not open")
            self._capacity(conn, "invitations")
            expires = min(now + ttl, offer["expires"])
            conn.execute("INSERT INTO invitations(id,token_hash,offer_id,created,expires) VALUES(?,?,?,?,?)",
                         (invitation_id, _hash(token), offer_id, now, expires))
            return {"invitation_id": invitation_id, "offer_id": offer_id, "invite_token": token,
                    "terms_hash": offer["terms_hash"], "expires_at": expires, "single_use": True}

    def join(self, body):
        body = _body(body, {"invite_token", "terms_hash", "name", "capabilities", "accepted_terms"},
                     {"invite_token", "terms_hash", "name", "capabilities", "accepted_terms"})
        if body["accepted_terms"] is not True:
            raise ValueError("Explicit acceptance of frozen offer terms is required")
        invite = _text(body["invite_token"], "invite_token", 200)
        terms_hash = _text(body["terms_hash"], "terms_hash", 64)
        if re.fullmatch(r"[A-Za-z0-9_-]{43}", invite) is None:
            raise PermissionError("Invalid invitation credential")
        if re.fullmatch(r"[a-f0-9]{64}", terms_hash) is None:
            raise PermissionError("Frozen terms hash must match the invitation")
        name = _text(body["name"], "name", 200)
        capabilities = _list(body["capabilities"], "capabilities", item_limit=120, maximum=20)
        token, member_id = secrets.token_urlsafe(32), _id("member")
        with self._tx() as conn:
            self._expire(conn)
            # Admission time must be sampled after obtaining the writer lock.
            now = time.time()
            invitation = conn.execute("SELECT * FROM invitations WHERE token_hash=?", (_hash(invite),)).fetchone()
            if invitation is None or invitation["used"] is not None or invitation["expires"] <= now:
                raise PermissionError("Invalid, expired or consumed invitation")
            offer = conn.execute("SELECT * FROM offers WHERE id=?", (invitation["offer_id"],)).fetchone()
            if offer["status"] != "open" or not hmac.compare_digest(offer["terms_hash"], terms_hash):
                raise PermissionError("Offer unavailable or frozen terms do not match")
            self._capacity(conn, "members")
            conn.execute("INSERT INTO members(id,token_hash,name,capabilities,offer_id,terms_hash,created) VALUES(?,?,?,?,?,?,?)",
                         (member_id, _hash(token), name, _json(capabilities), offer["id"], terms_hash, now))
            conn.execute("UPDATE invitations SET used=? WHERE id=?", (now, invitation["id"]))
            view = self._member_view(conn, conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone())
            return {**view, "member_token": token, "grant_scopes": list(GRANT_SCOPES)}

    @staticmethod
    def _assignment_view(row, include_result=False):
        result = {"assignment_id": row["id"], "offer_id": row["offer_id"], "member_id": row["member_id"],
                  "status": row["status"], "created_at": row["created"], "lease_expires_at": row["lease_expires"],
                  "submitted_at": row["submitted"], "reviewed_at": row["reviewed"], "result_hash": row["result_hash"],
                  "rationale": row["rationale"], "publication_status": "not_published"}
        if include_result and row["result"] is not None:
            result.update(result=json.loads(row["result"]), source_refs=json.loads(row["source_refs"]), limitations=json.loads(row["limitations"]))
        return result

    def claim(self, token, body):
        body = _body(body, {"offer_id", "idempotency_key"}, {"offer_id"})
        offer_id = _key(body["offer_id"], "offer_id")
        idem = _key(body.get("idempotency_key", offer_id), "idempotency_key")
        with self._tx() as conn:
            self._expire(conn)
            member = self._authenticate(conn, token)
            if member["offer_id"] != offer_id:
                raise PermissionError("Credential is admitted only to its invited offer")
            previous = conn.execute("SELECT * FROM assignments WHERE member_id=? AND idempotency_key=?", (member["id"], idem)).fetchone()
            if previous is not None:
                if previous["offer_id"] != offer_id:
                    raise ValueError("Idempotency key belongs to another offer")
                return self._assignment_view(previous)
            previous = conn.execute("SELECT * FROM assignments WHERE member_id=? AND offer_id=? AND status IN ('claimed','submitted','accepted') ORDER BY created DESC LIMIT 1",
                                    (member["id"], offer_id)).fetchone()
            if previous is not None:
                return self._assignment_view(previous)
            offer = conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone()
            if offer is None:
                raise KeyError("Unknown offer")
            if offer["status"] != "open" or self._offer_view(conn, offer)["available_slots"] <= 0:
                raise ValueError("Offer is closed or all slots are reserved")
            self._capacity(conn, "assignments")
            now, assignment_id = time.time(), _id("assignment")
            conn.execute("INSERT INTO assignments(id,offer_id,member_id,idempotency_key,status,created,lease_expires) VALUES(?,?,?,?,'claimed',?,?)",
                         (assignment_id, offer_id, member["id"], idem, now, min(now + offer["lease_seconds"], offer["expires"])))
            return self._assignment_view(conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone())

    def submit(self, token, body):
        body = _body(body, {"assignment_id", "result", "source_refs", "limitations"}, {"assignment_id", "result", "source_refs", "limitations"})
        assignment_id = _key(body["assignment_id"], "assignment_id")
        if not isinstance(body["result"], dict) or not body["result"]:
            raise ValueError("result must be a nonempty JSON object")
        refs = _list(body["source_refs"], "source_refs", maximum=50, item_limit=2000, minimum=1)
        limits = _list(body["limitations"], "limitations", maximum=30, item_limit=2000, minimum=1)
        encoded = _json(body["result"])
        digest = _hash(_json({"result": body["result"], "source_refs": refs, "limitations": limits}))
        with self._tx() as conn:
            self._expire(conn)
            member = self._authenticate(conn, token)
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown assignment")
            if row["member_id"] != member["id"]:
                raise PermissionError("Assignment belongs to another admitted credential")
            if row["result_hash"] is not None:
                if row["result_hash"] != digest:
                    raise ValueError("A frozen submission cannot be replaced")
                return self._assignment_view(row, True)
            if row["status"] != "claimed":
                raise ValueError("Assignment lease expired, cancelled or unavailable")
            conn.execute("UPDATE assignments SET status='submitted',submitted=?,result=?,result_hash=?,source_refs=?,limitations=? WHERE id=?",
                         (time.time(), encoded, digest, _json(refs), _json(limits), assignment_id))
            return self._assignment_view(conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone(), True)

    def assignments(self, token):
        with self._tx() as conn:
            self._expire(conn)
            member = self._authenticate(conn, token)
            return {"items": [self._assignment_view(row, True) for row in conn.execute("SELECT * FROM assignments WHERE member_id=? ORDER BY created,id", (member["id"],))]}

    def review_queue(self):
        """Local-administrator API; results are untrusted submitted data."""
        with self._tx() as conn:
            return {"items": [self._assignment_view(row, True) for row in conn.execute("SELECT * FROM assignments WHERE status='submitted' ORDER BY submitted,id")]}

    def review(self, body):
        """Local-administrator decision only. Member transports must not expose it."""
        body = _body(body, {"assignment_id", "decision", "rationale"}, {"assignment_id", "decision", "rationale"})
        assignment_id = _key(body["assignment_id"], "assignment_id")
        if not isinstance(body["decision"], str) or body["decision"] not in {"accepted", "rejected"}:
            raise ValueError("decision must be accepted or rejected")
        rationale = _text(body["rationale"], "rationale", 4000)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown assignment")
            if row["status"] in {"accepted", "rejected"}:
                if row["status"] != body["decision"] or row["rationale"] != rationale:
                    raise ValueError("Review is already final")
                return self._assignment_view(row)
            if row["status"] != "submitted":
                raise ValueError("Only submitted results can be manually reviewed")
            now = time.time()
            if body["decision"] == "accepted":
                offer = conn.execute("SELECT * FROM offers WHERE id=?", (row["offer_id"],)).fetchone()
                accepted = conn.execute("SELECT count(*) FROM assignments WHERE offer_id=? AND status='accepted'", (row["offer_id"],)).fetchone()[0]
                if accepted >= offer["max_assignments"]:
                    raise ValueError("Offer reward budget exhausted")
                conn.execute("INSERT INTO ledger(id,member_id,kind,ref_id,amount,created) VALUES(?,?,'reward',?,?,?)",
                             (_id("credit"), row["member_id"], assignment_id, offer["reward"], now))
            conn.execute("UPDATE assignments SET status=?,reviewed=?,rationale=? WHERE id=?", (body["decision"], now, rationale, assignment_id))
            return self._assignment_view(conn.execute("SELECT * FROM assignments WHERE id=?", (assignment_id,)).fetchone())

    def add_resource(self, body):
        body = _body(body, {"title", "summary", "content", "license", "data_class", "rights_confirmed", "redistribution_allowed", "kind", "cost_credits"},
                     {"title", "summary", "content", "license", "data_class", "rights_confirmed", "redistribution_allowed", "kind", "cost_credits"})
        if body["rights_confirmed"] is not True or body["redistribution_allowed"] is not True:
            raise ValueError("Explicit resource ownership and redistribution permission are required")
        if not isinstance(body["data_class"], str) or body["data_class"] not in {"public", "internal"} or body["kind"] != "research_summary":
            raise ValueError("Exchange resources are authorized research summaries only; no genomic or medical payloads")
        content = _text(body["content"], "content", 30000)
        # Reject obvious sequence payloads; this is not a comprehensive DLP classifier.
        if re.search(r"[ACGTNacgtn]{80,}", content) or content.lstrip().startswith(("##fileformat=VCF", ">")):
            raise ValueError("Sequence/variant payloads are not exchange summaries")
        metadata = {"title": _text(body["title"], "title", 200), "summary": _text(body["summary"], "summary", 3000),
                    "license": _text(body["license"], "license", 500), "data_class": body["data_class"],
                    "kind": "research_summary", "rights_confirmed": True, "redistribution_allowed": True,
                    "cost_credits": _integer(body["cost_credits"], "cost_credits", 0, 100000),
                    "rights_assurance": "local_admin_attestation"}
        resource_id, now = _id("resource"), time.time()
        with self._tx() as conn:
            self._capacity(conn, "resources")
            conn.execute("INSERT INTO resources(id,metadata,content,content_hash,cost,created) VALUES(?,?,?,?,?,?)",
                         (resource_id, _json(metadata), content, _hash(content), metadata["cost_credits"], now))
            return {"resource_id": resource_id, **metadata, "content_hash": _hash(content), "created_at": now}

    @staticmethod
    def _resource_view(row, public=True):
        metadata = json.loads(row["metadata"])
        if public and metadata["data_class"] == "internal":
            # Content digests can reveal short conclusions through dictionary attacks.
            # Neither descriptions nor digests belong in an internal item's public view.
            metadata = {"kind": metadata["kind"], "data_class": "internal", "cost_credits": row["cost"],
                        "title": "Authorized internal research summary"}
            return {"resource_id": row["id"], **metadata, "created_at": row["created"]}
        return {"resource_id": row["id"], **metadata, "content_hash": row["content_hash"], "created_at": row["created"]}

    def list_resources(self, public=True):
        with self._tx() as conn:
            return {"items": [self._resource_view(row, public) for row in conn.execute("SELECT * FROM resources ORDER BY created,id")]}

    def redeem(self, token, body):
        body = _body(body, {"resource_id"}, {"resource_id"})
        resource_id = _key(body["resource_id"], "resource_id")
        with self._tx() as conn:
            member = self._authenticate(conn, token)
            row = conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown resource")
            grant = conn.execute("SELECT * FROM grants WHERE member_id=? AND resource_id=?", (member["id"], resource_id)).fetchone()
            if grant is not None:
                return {"resource_id": resource_id, "granted": True, "credits": self._balance(conn, member["id"]), "charged_credits": 0, "idempotent_replay": True}
            if self._balance(conn, member["id"]) < row["cost"]:
                raise ValueError("Insufficient nonmonetary research credits")
            self._capacity(conn, "grants")
            now = time.time()
            conn.execute("INSERT INTO grants(member_id,resource_id,created) VALUES(?,?,?)", (member["id"], resource_id, now))
            conn.execute("INSERT INTO ledger(id,member_id,kind,ref_id,amount,created) VALUES(?,?,'redeem',?,?,?)",
                         (_id("credit"), member["id"], member["id"] + ":" + resource_id, -row["cost"], now))
            return {"resource_id": resource_id, "granted": True, "credits": self._balance(conn, member["id"]), "charged_credits": row["cost"], "idempotent_replay": False}

    def access(self, token, resource_id):
        resource_id = _key(resource_id, "resource_id")
        with self._tx() as conn:
            member = self._authenticate(conn, token)
            if conn.execute("SELECT 1 FROM grants WHERE member_id=? AND resource_id=?", (member["id"], resource_id)).fetchone() is None:
                raise PermissionError("Redeem this resource before accessing its content")
            row = conn.execute("SELECT * FROM resources WHERE id=?", (resource_id,)).fetchone()
            return {**self._resource_view(row, False), "content": row["content"]}

    def revoke_member(self, body):
        body = _body(body, {"member_id", "reason"}, {"member_id", "reason"})
        member_id = _key(body["member_id"], "member_id")
        reason = _text(body["reason"], "reason", 2000)
        with self._tx() as conn:
            row = conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone()
            if row is None:
                raise KeyError("Unknown admitted member")
            if row["revoked"] is None:
                conn.execute("UPDATE members SET revoked=?,revocation_reason=? WHERE id=?", (time.time(), reason, member_id))
                conn.execute("UPDATE assignments SET status='cancelled' WHERE member_id=? AND status='claimed'", (member_id,))
            return self._member_view(conn, conn.execute("SELECT * FROM members WHERE id=?", (member_id,)).fetchone())

    def cancel_offer(self, body):
        body = _body(body, {"offer_id", "reason"}, {"offer_id", "reason"})
        offer_id = _key(body["offer_id"], "offer_id")
        reason = _text(body["reason"], "reason", 2000)
        with self._tx() as conn:
            if conn.execute("SELECT 1 FROM offers WHERE id=?", (offer_id,)).fetchone() is None:
                raise KeyError("Unknown offer")
            conn.execute("UPDATE offers SET status='cancelled',cancellation_reason=? WHERE id=?", (reason, offer_id))
            conn.execute("UPDATE assignments SET status='cancelled' WHERE offer_id=? AND status='claimed'", (offer_id,))
            return self._offer_view(conn, conn.execute("SELECT * FROM offers WHERE id=?", (offer_id,)).fetchone())

    def status(self):
        with self._tx() as conn:
            self._expire(conn)
            counts = {table: conn.execute(f"SELECT count(*) FROM {table}").fetchone()[0]
                      for table in ("offers", "invitations", "members", "assignments", "resources", "grants")}
            return {"counts": counts, "rewarded_credits": conn.execute("SELECT COALESCE(sum(amount),0) FROM ledger WHERE kind='reward'").fetchone()[0],
                    "redeemed_credits": -conn.execute("SELECT COALESCE(sum(amount),0) FROM ledger WHERE kind='redeem'").fetchone()[0],
                    "review_policy": "local_admin_manual", "organization_verification": False, "automatic_result_publication": False,
                    "network_actions": False, "reward_unit": "nonmonetary_research_credit"}

    def export(self):
        """Metadata audit only: no token/hash fields, results or resource bodies."""
        with self._tx() as conn:
            self._expire(conn)
            return {"schema_version": "1", "exported_at": time.time(), "contains_result_content": False,
                    "offers": [self._offer_view(conn, row) for row in conn.execute("SELECT * FROM offers ORDER BY created,id")],
                    "members": [self._member_view(conn, row) for row in conn.execute("SELECT * FROM members ORDER BY created,id")],
                    "assignments": [self._assignment_view(row) for row in conn.execute("SELECT * FROM assignments ORDER BY created,id")],
                    "resources": [self._resource_view(row, True) for row in conn.execute("SELECT * FROM resources ORDER BY created,id")],
                    "ledger": [{"entry_id": row["id"], "member_id": row["member_id"], "kind": row["kind"], "ref_id": row["ref_id"],
                                "amount": row["amount"], "created_at": row["created"]} for row in conn.execute("SELECT * FROM ledger ORDER BY created,id")],
                    "limits": {"max_records_per_table": MAX_ROWS, "max_request_bytes": MAX_JSON_BYTES},
                    "identity_assurance": "admitted_credentials_not_verified_organizations"}
