"""Pure recovery and public handoff decisions; no I/O, browser actions or execution.

The controller persists retry counters and delivery receipts. A decision never
solves a CAPTCHA, logs in, changes accounts, grants access or dispatches a model.
"""
from __future__ import annotations

import hashlib
import json
import re

MAX_RETRIES = 2
MAX_HANDOFF_HOPS = 4
BACKOFF_SECONDS = (30, 120)
_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,127}\Z")
_SHA = re.compile(r"[a-f0-9]{64}\Z")
_EVENTS = {"healthy", "timeout", "transient", "captcha", "auth_lost",
           "region_blocked", "quota", "payment", "unknown"}
_CLOUD = {"google_colab", "explicitly_selected_cloud_runtime"}
_TASK_FIELDS = {"task_id", "input_packet_sha256", "data_class", "phase",
                "required_capabilities", "handoff_hops"}
_PHASES = {"propose", "review", "revise", "validate"}


def _identifier(value):
    if type(value) is not str or not _ID.fullmatch(value):
        raise ValueError("Invalid contributor or task identifier")
    return value


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError("Recovery count or delay exceeds limits")
    return value


def _event(value):
    if type(value) is not str or value not in _EVENTS:
        raise ValueError("Unknown recovery event")
    return value


def classify_event(event, retry_count=0, *, retry_after_seconds=None):
    """Return a bounded recommendation; retry_count counts retries already used.

    CAPTCHA/auth/region/quota/payment events never cause an automatic model retry.
    A quota reset hint only informs cooldown; it does not authorize another call.
    """
    event = _event(event)
    _integer(retry_count, 0, MAX_RETRIES)
    if retry_after_seconds is not None:
        _integer(retry_after_seconds, 1, 7 * 24 * 3600)
    decision = {"event": event, "retry_allowed": False, "delay_seconds": None,
                "retry_count": retry_count, "next_retry_count": retry_count,
                "max_retries": MAX_RETRIES, "checkpoint_required": event != "healthy",
                "requires_user": False, "actions_executed": False}
    if event == "healthy":
        decision.update(status="healthy", action="none")
    elif event in {"timeout", "transient"}:
        if retry_count < MAX_RETRIES:
            decision.update(status="backoff", action="retry_after_backoff",
                            retry_allowed=True, delay_seconds=BACKOFF_SECONDS[retry_count],
                            next_retry_count=retry_count + 1)
        else:
            decision.update(status="retry_exhausted", action="checkpoint_and_stop")
    elif event == "captcha":
        decision.update(status="needs_user_confirmation", action="request_user_confirmation",
                        requires_user=True, automated_challenge_solving=False)
    elif event == "auth_lost":
        decision.update(status="needs_user_login", action="request_user_login",
                        requires_user=True, credential_transfer_allowed=False)
    elif event == "region_blocked":
        decision.update(status="disabled_no_bypass", action="disable_route",
                        bypass_allowed=False)
    elif event == "quota":
        decision.update(status="cooldown", action="checkpoint_and_wait",
                        delay_seconds=retry_after_seconds, anonymous_rotation_allowed=False)
    elif event == "payment":
        decision.update(status="blocked_budget", action="checkpoint_and_stop",
                        paid_fallback_allowed=False)
    else:
        decision.update(status="needs_review", action="checkpoint_and_stop", requires_user=True)
    return decision


def _capabilities(value):
    if type(value) is not list or not 1 <= len(value) <= 16:
        raise ValueError("Expected one to sixteen capabilities")
    items = [_identifier(item) for item in value]
    if len(set(items)) != len(items):
        raise ValueError("Duplicate capabilities")
    return set(items)


def _participants(values):
    if type(values) is not list or not 1 <= len(values) <= 64:
        raise ValueError("Invalid bounded participant health snapshot")
    result = {}
    for entry in values:
        if type(entry) is not dict:
            raise ValueError("Invalid participant health entry")
        ident = _identifier(entry.get("id"))
        if ident in result:
            raise ValueError("Duplicate participant identifier")
        result[ident] = entry
    return result


def plan_handoff(task, source_id, target_id, contributors, delivered_ids=(), *, event="unknown"):
    """Plan one public reference handoff; no payload bytes, credentials or side effects.

    contributors is a controller-owned health snapshot, separate from the static
    registry. A target needs allowlisted=True, dispatch_enabled=True, healthy,
    zero_spend_verified=True,
    explicit cloud location, provider_group and matching capabilities. The caller
    must verify the referenced public artifact and enforce its deadline before
    dispatch; the SHA here identifies it but does not inspect its contents.
    """
    event = _event(event)
    source_id, target_id = _identifier(source_id), _identifier(target_id)
    if type(task) is not dict or set(task) != _TASK_FIELDS:
        raise ValueError("Handoff accepts only bounded public task metadata")
    task_id = _identifier(task["task_id"])
    packet_sha = task["input_packet_sha256"]
    if type(packet_sha) is not str or not _SHA.fullmatch(packet_sha):
        raise ValueError("Invalid public packet SHA256")
    if task["data_class"] != "public_only":
        raise ValueError("Only public tasks may be handed off")
    if type(task["phase"]) is not str or task["phase"] not in _PHASES:
        raise ValueError("Invalid handoff phase")
    required = _capabilities(task["required_capabilities"])
    hops = _integer(task["handoff_hops"], 0, MAX_HANDOFF_HOPS)
    entries = _participants(contributors)
    if type(delivered_ids) not in (list, tuple, set, frozenset) or len(delivered_ids) > 10000:
        raise ValueError("Invalid bounded delivery receipt set")
    if any(type(value) is not str or not _SHA.fullmatch(value) for value in delivered_ids):
        raise ValueError("Invalid delivery receipt identifier")

    def denied(reason):
        return {"allowed": False, "reason": reason, "task_id": task_id,
                "actions_executed": False}

    if hops == MAX_HANDOFF_HOPS:
        return denied("handoff_limit_reached")
    if source_id == target_id:
        return denied("same_participant")
    source, target = entries.get(source_id), entries.get(target_id)
    if source is None or source.get("allowlisted") is not True:
        return denied("source_not_allowlisted")
    if target is None or target.get("allowlisted") is not True:
        return denied("target_not_allowlisted")
    if target.get("dispatch_enabled") is not True:
        return denied("target_dispatch_disabled")
    if target.get("zero_spend_verified") is not True:
        return denied("target_zero_spend_unverified")
    if target.get("health") != "healthy":
        return denied("target_not_healthy")
    if target.get("execution_location") not in _CLOUD:
        return denied("target_not_cloud")
    if not required.issubset(_capabilities(target.get("capabilities"))):
        return denied("target_missing_capability")
    source_group = _identifier(source.get("provider_group"))
    target_group = _identifier(target.get("provider_group"))
    if event in {"quota", "captcha", "auth_lost", "region_blocked"} and source_group == target_group:
        return denied("same_provider_rotation_disallowed")
    identity = {"task_id": task_id, "input_packet_sha256": packet_sha,
                "source_id": source_id, "target_id": target_id,
                "phase": task["phase"], "handoff_hops": hops + 1}
    handoff_id = hashlib.sha256(json.dumps(identity, sort_keys=True,
                              separators=(",", ":")).encode("utf-8")).hexdigest()
    if handoff_id in delivered_ids:
        return {**denied("already_delivered"), "handoff_id": handoff_id}
    return {"allowed": True, "reason": "public_handoff_proposal", "task_id": task_id,
            "handoff_id": handoff_id, "actions_executed": False,
            "controller_dispatch_required": True, "referenced_payload_validation_required": True,
            "outcome_status": "unverified", "envelope": {**identity, "handoff_id": handoff_id,
                "data_class": "public_only", "required_capabilities": sorted(required)}}


