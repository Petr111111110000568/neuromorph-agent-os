"""Pure integrity checks against an out-of-band trusted registry and policy.

API: verify_provenance(blob, record, registry, policy, now) -> VerificationResult.
Inputs are exact builtin bytes/dicts/strings/integers, normally decoded JSON.
No URI is fetched or normalized. No filesystem, clock, network or code execution.

Record: record_id, project_id, purpose, source_uri, digest, source_ts; optional legacy required_trust
is bounded integer metadata and NEVER authorizes acceptance. Registry maps IDs to
{project_id, purpose, source_uri, digest, source_ts, valid_from, valid_until, trust, revoked}.
Policy is {project_id, purpose, min_trust, max_blob_bytes}; only this trusted policy
sets scope and threshold. Repeated valid records within this same scope are
intentionally accepted: no nonce, replay cache or signature is claimed.
Trust integers range 0..100. The caller must provide an aware fixed-offset
stdlib datetime (normally datetime.now(timezone.utc)) from a trusted clock.

Timestamps are bounded ISO8601 strings with T, seconds, optional 1..6 fractional
places and Z or a numeric offset. Record source_ts must exactly match the registry
string BEFORE parsing: equivalent spelling is not accepted as a new binding.
Validity is half-open [valid_from, valid_until); both issuance and now must be
inside it, and issuance cannot be later than now. Expiry and revoked are required.

The caller authenticates/authorizes registry, policy and clock out of band. If an
attacker controls these trusted inputs, this verifier supplies no trust anchor.
Freshness is limited to registry-bound timestamps and the caller-supplied clock.
An accepted hash proves agreement with registered bytes, NOT factual truth,
scientific validation, author identity, a digital signature or registry security.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re

MAX_BLOB_BYTES = 16 * 1024 * 1024
MAX_REGISTRY_ENTRIES = 4096
MAX_ID_CHARS = 128
MAX_URI_CHARS = 2048
MAX_TIMESTAMP_CHARS = 32

_RECORD_FIELDS = frozenset({'record_id', 'project_id', 'purpose', 'source_uri', 'digest', 'source_ts'})
_ENTRY_FIELDS = frozenset({'project_id', 'purpose', 'source_uri', 'digest', 'source_ts', 'valid_from',
                           'valid_until', 'trust', 'revoked'})
_POLICY_FIELDS = frozenset({'project_id', 'purpose', 'min_trust', 'max_blob_bytes'})
_SHA256 = re.compile(r'[0-9a-f]{64}')
_TIMESTAMP = re.compile(r'[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}'
                        r'(?:\.[0-9]{1,6})?(?:Z|[+-](?:[01][0-9]|2[0-3]):[0-5][0-9])')


@dataclass(frozen=True)
class VerificationResult:
    accepted: bool
    reason: str


def _shape(value, required, optional=frozenset()):
    if type(value) is not dict or not len(required) <= len(value) <= len(required | optional):
        return False
    if any(type(key) is not str for key in value):
        return False
    return required <= value.keys() <= required | optional


def _text(value, limit):
    return (type(value) is str and 0 < len(value) <= limit
            and value.strip() == value
            and all(ord(char) >= 32 and ord(char) != 127 for char in value))


def _integer(value, minimum, maximum):
    return type(value) is int and minimum <= value <= maximum


def _digest(value):
    return type(value) is str and _SHA256.fullmatch(value) is not None


def _timestamp(value):
    if (type(value) is not str or len(value) > MAX_TIMESTAMP_CHARS
            or _TIMESTAMP.fullmatch(value) is None):
        raise ValueError('invalid_timestamp')
    # Strict syntax first; fromisoformat then verifies calendar and offset values.
    result = datetime.fromisoformat(value[:-1] + '+00:00' if value.endswith('Z') else value)
    if result.tzinfo is None:
        raise ValueError('naive_timestamp')
    return result


def verify_provenance(blob, record, registry, policy, now):
    """Fail closed on malformed data, returning only fixed non-sensitive reasons."""
    try:
        return _verify(blob, record, registry, policy, now)
    except (ValueError, TypeError, KeyError, OverflowError, RecursionError):
        return VerificationResult(False, 'invalid_input')


def _verify(blob, record, registry, policy, now):
    if not _shape(policy, _POLICY_FIELDS):
        return VerificationResult(False, 'invalid_input')
    if (not _text(policy['project_id'], MAX_ID_CHARS) or not _text(policy['purpose'], MAX_ID_CHARS)
            or not _integer(policy['min_trust'], 0, 100)
            or not _integer(policy['max_blob_bytes'], 1, MAX_BLOB_BYTES)):
        return VerificationResult(False, 'invalid_input')
    if type(blob) is not bytes:
        return VerificationResult(False, 'invalid_input')
    # Reject by O(1) length before any hashing or payload traversal.
    if len(blob) > policy['max_blob_bytes']:
        return VerificationResult(False, 'payload_too_large')
    if not _shape(record, _RECORD_FIELDS, frozenset({'required_trust'})):
        return VerificationResult(False, 'invalid_input')
    if (not _text(record['record_id'], MAX_ID_CHARS)
            or not _text(record['source_uri'], MAX_URI_CHARS)
            or not _digest(record['digest'])):
        return VerificationResult(False, 'invalid_input')
    # Accept bounded legacy metadata, including -1, but never use its threshold.
    if 'required_trust' in record and not _integer(record['required_trust'], -100, 100):
        return VerificationResult(False, 'invalid_input')
    if type(registry) is not dict or len(registry) > MAX_REGISTRY_ENTRIES:
        return VerificationResult(False, 'invalid_input')
    if any(not _text(key, MAX_ID_CHARS) for key in registry):
        return VerificationResult(False, 'invalid_input')
    if record['record_id'] not in registry:
        return VerificationResult(False, 'unknown_record')
    entry = registry[record['record_id']]
    if not _shape(entry, _ENTRY_FIELDS):
        return VerificationResult(False, 'invalid_input')
    if (not _text(entry['source_uri'], MAX_URI_CHARS) or not _digest(entry['digest'])
            or not _integer(entry['trust'], 0, 100) or type(entry['revoked']) is not bool):
        return VerificationResult(False, 'invalid_input')
    for key in ('project_id', 'purpose'):
        if not _text(entry[key], MAX_ID_CHARS) or not _text(record[key], MAX_ID_CHARS):
            return VerificationResult(False, 'invalid_input')
        if entry[key] != policy[key] or record[key] != entry[key]:
            return VerificationResult(False, 'scope_mismatch')
    if entry['revoked']:
        return VerificationResult(False, 'revoked')
    if record['source_uri'] != entry['source_uri']:
        return VerificationResult(False, 'source_mismatch')
    if (type(record['source_ts']) is not str or type(entry['source_ts']) is not str
            or len(record['source_ts']) > MAX_TIMESTAMP_CHARS or len(entry['source_ts']) > MAX_TIMESTAMP_CHARS):
        return VerificationResult(False, 'invalid_input')
    if record['source_ts'] != entry['source_ts']:
        return VerificationResult(False, 'timestamp_mismatch')
    issued = _timestamp(entry['source_ts'])
    start = _timestamp(entry['valid_from'])
    end = _timestamp(entry['valid_until'])
    # Reject datetime/tzinfo subclasses and user-defined timezone callbacks.
    if type(now) is not datetime or type(now.tzinfo) is not timezone:
        return VerificationResult(False, 'invalid_input')
    if not start < end or not start <= issued < end:
        return VerificationResult(False, 'invalid_time_window')
    if not start <= now < end or issued > now:
        return VerificationResult(False, 'not_current')
    if entry['trust'] < policy['min_trust']:
        return VerificationResult(False, 'insufficient_trust')
    if record['digest'] != entry['digest']:
        return VerificationResult(False, 'digest_mismatch')
    if hashlib.sha256(blob).hexdigest() != entry['digest']:
        return VerificationResult(False, 'digest_mismatch')
    return VerificationResult(True, 'verified_against_trusted_registry')

