"""Fail-closed policy for zero-spend external inference; no budget metering."""
from __future__ import annotations

import json
from pathlib import Path

_ZERO_SPEND = {
    "schema_version": 1,
    "daily_spend_limit_usd": 0,
    "paid_model_calls_allowed": False,
    "public_catalog_reads_allowed": True,
}
_MAX_POLICY_BYTES = 4096


def _unique_fields(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate resource policy field")
        result[key] = value
    return result


def load_policy(root):
    """Return validated policy; missing configuration defaults to zero spend.

    Nonzero budgets are unsupported until a separately verified metering and
    enforcement mechanism exists. Invalid configuration raises rather than
    permitting a model call.
    """
    path = Path(root) / "config" / "resource_policy.json"
    if path.is_symlink():
        raise ValueError("Resource policy symlink rejected")
    try:
        with path.open("rb") as stream:
            raw = stream.read(_MAX_POLICY_BYTES + 1)
    except FileNotFoundError:
        return dict(_ZERO_SPEND)
    if len(raw) > _MAX_POLICY_BYTES:
        raise ValueError("Resource policy exceeds size limit")
    try:
        policy = json.loads(raw.decode("utf-8"), object_pairs_hook=_unique_fields)
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise ValueError("Invalid resource policy JSON") from exc
    if not isinstance(policy, dict) or set(policy) != set(_ZERO_SPEND):
        raise ValueError("Unknown or missing resource policy fields")
    if type(policy["schema_version"]) is not int or policy["schema_version"] != 1:
        raise ValueError("Unsupported resource policy schema")
    limit = policy["daily_spend_limit_usd"]
    if type(limit) not in (int, float) or limit != 0:
        raise ValueError("Only a zero-spend limit is supported")
    if policy["paid_model_calls_allowed"] is not False:
        raise ValueError("Paid model calls must remain disabled")
    if policy["public_catalog_reads_allowed"] is not True:
        raise ValueError("Policy requires public catalog reads to remain allowed")
    return dict(_ZERO_SPEND)


def live_inference_block_reason(root):
    """Block every external live inference call, even a claimed free provider."""
    load_policy(root)
    return "blocked_zero_spend_policy"

