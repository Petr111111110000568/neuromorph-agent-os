"""SQLite persistence with an append-only, hash-linked event journal.

The chain detects accidental changes; it is not a signature or protection
against an attacker able to rewrite the entire database.
"""
import hashlib
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path


def now():
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


class Store:
    def __init__(self, path):
        path = Path(path)
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.lock = threading.RLock()
        self.db = sqlite3.connect(str(path), check_same_thread=False)
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA busy_timeout=5000")
        self.db.executescript("""
          CREATE TABLE IF NOT EXISTS records (
            kind TEXT NOT NULL, id TEXT NOT NULL, payload TEXT NOT NULL,
            PRIMARY KEY(kind,id));
          CREATE TABLE IF NOT EXISTS audit (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            event TEXT NOT NULL, timestamp TEXT NOT NULL, details TEXT NOT NULL,
            prev_hash TEXT NOT NULL, hash TEXT NOT NULL);
        """)
        self.db.commit()

    def close(self):
        with self.lock:
            self.db.close()

    def _append(self, event, details):
        previous = self.db.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
        prev_hash = previous[0] if previous else "0" * 64
        timestamp = now()
        cursor = self.db.execute("INSERT INTO audit(event,timestamp,details,prev_hash,hash) VALUES(?,?,?,?,?)",
                                 (event, timestamp, canonical(details), prev_hash, ""))
        entry = {"id": cursor.lastrowid, "event": event, "timestamp": timestamp,
                 "details": details, "prev_hash": prev_hash}
        digest = hashlib.sha256(canonical(entry).encode()).hexdigest()
        self.db.execute("UPDATE audit SET hash=? WHERE id=?", (digest, cursor.lastrowid))
        entry["hash"] = digest
        return entry

    def record(self, kind, value, event=None):
        with self.lock, self.db:
            self.db.execute("INSERT INTO records(kind,id,payload) VALUES(?,?,?)",
                            (kind, value["id"], canonical(value)))
            self._append(event or kind + ".created", {"id": value["id"], "kind": kind,
                "payload_hash": hashlib.sha256(canonical(value).encode()).hexdigest()})
        return value

    def list(self, kind):
        with self.lock:
            rows = self.db.execute("SELECT payload FROM records WHERE kind=? ORDER BY rowid DESC", (kind,)).fetchall()
        return [json.loads(row[0]) for row in rows]

    def audit(self):
        with self.lock:
            rows = self.db.execute("SELECT id,event,timestamp,details,prev_hash,hash FROM audit ORDER BY id").fetchall()
            records = self.db.execute("SELECT kind,id,payload FROM records").fetchall()
        items = [{"id": r[0], "event": r[1], "timestamp": r[2], "details": json.loads(r[3]),
                  "prev_hash": r[4], "hash": r[5]} for r in rows]
        previous = "0" * 64
        valid = True
        indexed = {(kind, id): hashlib.sha256(payload.encode()).hexdigest() for kind, id, payload in records}
        for entry in items:
            body = {k: v for k, v in entry.items() if k != "hash"}
            if entry["prev_hash"] != previous or hashlib.sha256(canonical(body).encode()).hexdigest() != entry["hash"]:
                valid = False
            details = entry["details"]
            if "payload_hash" in details and indexed.get((details.get("kind"), details.get("id"))) != details["payload_hash"]:
                valid = False
            previous = entry["hash"]
        audited = {(e["details"].get("kind"), e["details"].get("id")) for e in items}
        if any(key not in audited for key in indexed):
            valid = False
        return {"items": items, "valid": valid,
                "limitation": "Hash chain detects changes but is not externally signed."}
