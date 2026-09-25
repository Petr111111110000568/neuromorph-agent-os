"""Pure, bounded governance of logical roles sharing one approved Qwen adapter.

This module grants no accounts, tools, network, execution or billing authority.
Persist transitions with the caller's existing compare-and-swap transaction.
"""
from __future__ import annotations

import copy
import json
import re

ADAPTER = 'qwen-official-space'
INITIAL_ROLES = ('author', 'reviewer', 'reviser')
DAY = 86400
TTL = 7 * DAY
MAX_BYTES = 32768
SECURITY_KEYS = {'secrets', 'access', 'supply_chain', 'generated_code', 'resources'}
_POLICY = {
    'adapter': ADAPTER,
    'space': 'Qwen/Qwen3-Demo',
    'revision': '60e1db0778067d36b8a2793c350bf85cd461a298',
    'model': 'qwen3-235b-a22b',
    'max_members': 6, 'max_pending': 4, 'admission_interval_seconds': DAY,
    'proposal_ttl_seconds': TTL, 'required_approvals': 2,
    'paid_calls_allowed': False, 'credentials_allowed': False,
    'installation_allowed': False, 'execution_allowed': False,
    'global_resource_limits': 'inherited_unchanged',
}
_STATUSES = {'proposed', 'vote_recorded', 'meeting_approved', 'member_admitted',
             'rejected', 'expired'}
_KINDS = {'meeting', 'create_member', 'admit_member'}
_COMMON = {'id', 'kind', 'expected_revision', 'question', 'reason', 'security'}
_MEMBER_FIELDS = {'member_id', 'focus', 'adapter'}
_STATE_KEYS = {'schema_version', 'revision', 'policy', 'members', 'pending',
               'last_admission_at', 'clock', 'journal'}


def _need(condition, reason):
    if not condition:
        raise ValueError(reason)


def _integer(value, low=0, high=10**12):
    _need(type(value) is int and low <= value < high, 'invalid_council_integer')


def _identifier(value):
    _need(type(value) is str and re.fullmatch(r'[a-z][a-z0-9_]{0,31}', value)
          is not None, 'invalid_council_id')


def _text(value, limit):
    _need(type(value) is str and 0 < len(value) <= limit and value.strip() == value
          and all(ord(char) >= 32 and ord(char) != 127 for char in value),
          'invalid_council_text')
    # Common credential formats must never enter the durable council ledger.
    _need(re.search(r'(?i)(?:hf_[a-z0-9]{16,}|gh[pousr]_[a-z0-9]{16,}|'
                    r'github_pat_[a-z0-9_]{16,}|sk-[a-z0-9_-]{16,}|'
                    r'-----BEGIN [A-Z ]*PRIVATE KEY|authorization\s*:\s*bearer)',
                    value) is None, 'council_secret_rejected')


def _security(value):
    _need(type(value) is dict and set(value) == SECURITY_KEYS,
          'council_security_checklist_required')
    for item in value.values():
        _need(type(item) is dict and set(item) == {'passed', 'evidence'}
              and item['passed'] is True, 'council_security_check_failed')
        _text(item['evidence'], 120)


def _proposal(value):
    _need(type(value) is dict and type(value.get('kind')) is str
          and value['kind'] in _KINDS, 'invalid_council_proposal')
    keys = _COMMON if value['kind'] == 'meeting' else _COMMON | _MEMBER_FIELDS
    _need(set(value) == keys, 'invalid_council_proposal_fields')
    _identifier(value['id'])
    _integer(value['expected_revision'])
    _text(value['question'], 500)
    _text(value['reason'], 500)
    _security(value['security'])
    if value['kind'] != 'meeting':
        _identifier(value['member_id'])
        _text(value['focus'], 240)
        _text(value['adapter'], 120)


def initial_state(now=0):
    _integer(now)
    return {'schema_version': 1, 'revision': 0, 'policy': copy.deepcopy(_POLICY),
            'members': {role: {'adapter': ADAPTER, 'focus': role, 'admitted_at': 0,
                               'proposal_id': None, 'proposer': None, 'approvers': []}
                        for role in INITIAL_ROLES},
            'pending': {}, 'last_admission_at': None, 'clock': now, 'journal': []}


def check_schema(state):
    """Validate stored trusted state. Never accept a whole state from a model."""
    _need(type(state) is dict and set(state) == _STATE_KEYS,
          'invalid_council_state')
    _need(type(state['schema_version']) is int and state['schema_version'] == 1
          and type(state['policy']) is dict and state['policy'] == _POLICY
          and all(type(state['policy'][key]) is type(expected)
                  for key, expected in _POLICY.items()), 'council_policy_changed')
    _integer(state['revision'])
    _integer(state['clock'])
    roster = state['members']
    _need(type(roster) is dict and set(INITIAL_ROLES) <= set(roster)
          and len(roster) <= 6, 'invalid_council_members')
    admissions = []
    for name, member in roster.items():
        _identifier(name)
        _need(name != 'controller', 'reserved_council_member')
        _need(type(member) is dict and set(member) == {'adapter', 'focus', 'admitted_at',
              'proposal_id', 'proposer', 'approvers'} and member['adapter'] == ADAPTER,
              'unsupported_council_member')
        _text(member['focus'], 240)
        _integer(member['admitted_at'])
        _need(member['admitted_at'] <= state['clock'], 'council_future_member')
        if name in INITIAL_ROLES:
            _need(member == {'adapter': ADAPTER, 'focus': name, 'admitted_at': 0,
                  'proposal_id': None, 'proposer': None, 'approvers': []},
                  'initial_council_member_changed')
        else:
            _identifier(member['proposal_id'])
            _need(type(member['proposer']) is str and member['proposer'] in roster
                  and member['proposer'] != name,
                  'invalid_admission_proposer')
            voters = member['approvers']
            _need(type(voters) is list and len(voters) == 2
                  and all(type(actor) is str and actor in roster for actor in voters)
                  and len(set(voters)) == 2 and member['proposer'] not in voters
                  and name not in voters, 'invalid_admission_approvers')
            _need(all(roster[actor]['admitted_at'] <= member['admitted_at']
                      for actor in [member['proposer'], *voters]),
                  'future_admission_approver')
            admissions.append(member['admitted_at'])
    admissions.sort()
    _need(all(right - left >= DAY for left, right in zip(admissions, admissions[1:])),
          'council_admission_rate_exceeded')
    _need(state['last_admission_at'] == (admissions[-1] if admissions else None),
          'invalid_last_admission')
    pending = state['pending']
    _need(type(pending) is dict and len(pending) <= 4, 'invalid_council_pending')
    target_members = set()
    for proposal_id, item in pending.items():
        _identifier(proposal_id)
        _need(type(item) is dict and set(item) == {'proposal', 'proposer', 'created_at',
              'expires_at', 'electorate', 'votes'}, 'invalid_pending_proposal')
        body = item['proposal']
        _proposal(body)
        _need(body['id'] == proposal_id and body['expected_revision'] < state['revision'],
              'invalid_pending_revision')
        _integer(item['created_at'])
        _integer(item['expires_at'])
        _need(item['created_at'] <= state['clock']
              and item['expires_at'] == item['created_at'] + TTL,
              'invalid_proposal_expiry')
        _need(type(item['proposer']) is str and item['proposer'] in roster,
              'unknown_council_actor')
        electorate = item['electorate']
        _need(type(electorate) is list and len(electorate) >= 2
              and all(type(actor) is str and actor in roster for actor in electorate)
              and len(electorate) == len(set(electorate))
              and item['proposer'] not in electorate,
              'invalid_council_electorate')
        _need(all(roster[actor]['admitted_at'] <= item['created_at']
                  for actor in [item['proposer'], *electorate]), 'future_council_voter')
        votes = item['votes']
        _need(type(votes) is dict and set(votes) <= set(electorate)
              and len(votes) < 2 and all(v == 'approve' for v in votes.values()),
              'invalid_council_votes')
        if body['kind'] != 'meeting':
            _need(body['adapter'] == ADAPTER and body['member_id'] not in roster
                  and body['member_id'] not in target_members, 'invalid_member_proposal')
            target_members.add(body['member_id'])
    journal = state['journal']
    _need(type(journal) is list and len(journal) == min(state['revision'], 8),
          'invalid_council_journal')
    previous_at = 0
    for index, item in enumerate(journal):
        _need(type(item) is dict and set(item) == {'revision', 'at', 'actor',
              'proposal_id', 'status'}, 'invalid_council_journal')
        _integer(item['revision'], 1)
        _integer(item['at'])
        _need(type(item['actor']) is str and item['actor'] in (*roster, 'controller')
              and type(item['status']) is str and item['status'] in _STATUSES,
              'invalid_council_journal')
        _identifier(item['proposal_id'])
        _need(item['revision'] == state['revision'] - len(journal) + index + 1
              and previous_at <= item['at'] <= state['clock'], 'invalid_council_revision')
        previous_at = item['at']
    if journal:
        _need(journal[-1]['at'] == state['clock'], 'invalid_council_clock')
    try:
        size = len(json.dumps(state, ensure_ascii=False, allow_nan=False).encode('utf-8'))
    except (TypeError, ValueError, UnicodeError, RecursionError):
        raise ValueError('invalid_council_json') from None
    _need(size <= MAX_BYTES, 'council_state_too_large')
    return state


validate_state = check_schema


def members(state):
    check_schema(state)
    return (*INITIAL_ROLES, *sorted(set(state['members']) - set(INITIAL_ROLES)))


def _receipt(state, actor, proposal_id, status, reason=None):
    result = {'status': status, 'revision': state['revision'],
              'proposal_id': proposal_id, 'actor': actor}
    if reason is not None:
        result['reason'] = reason
    return result


def _record(state, actor, proposal_id, status, now):
    state['revision'] += 1
    state['clock'] = now
    state['journal'] = (state['journal'] + [{'revision': state['revision'], 'at': now,
        'actor': actor, 'proposal_id': proposal_id, 'status': status}])[-8:]
    return _receipt(state, actor, proposal_id, status)


def _prepare(state, actor, now):
    check_schema(state)
    _need(type(actor) is str and actor in state['members'], 'unknown_council_actor')
    _integer(now)
    _need(now >= state['clock'], 'council_clock_reversed')
    return copy.deepcopy(state)


def _expire(state, now):
    expired = [key for key, item in state['pending'].items() if now >= item['expires_at']]
    for key in expired:
        del state['pending'][key]
        _record(state, 'controller', key, 'expired', now)
    return expired


def receive_proposal(state, actor, proposal, now):
    result = _prepare(state, actor, now)
    _proposal(proposal)
    _need(proposal['expected_revision'] == state['revision'], 'stale_council_revision')
    proposal_id = proposal['id']
    seen = set(result['pending']) | {entry['proposal_id'] for entry in result['journal']}
    seen |= {entry['proposal_id'] for entry in result['members'].values()}
    _need(proposal_id not in seen, 'duplicate_council_proposal')
    _expire(result, now)
    if proposal['kind'] != 'meeting' and proposal['adapter'] != ADAPTER:
        return result, _receipt(result, actor, proposal_id, 'unsupported_adapter')
    _need(len(result['pending']) < 4, 'council_pending_limit')
    if proposal['kind'] != 'meeting':
        _need(proposal['member_id'] != 'controller'
              and proposal['member_id'] not in result['members']
              and all(item['proposal'].get('member_id') != proposal['member_id']
                      for item in result['pending'].values()), 'duplicate_council_member')
        if len(result['members']) >= 6:
            return result, _receipt(result, actor, proposal_id, 'member_limit')
    result['pending'][proposal_id] = {'proposal': copy.deepcopy(proposal), 'proposer': actor,
        'created_at': now, 'expires_at': now + TTL,
        'electorate': [member for member in members(state) if member != actor], 'votes': {}}
    receipt = _record(result, actor, proposal_id, 'proposed', now)
    check_schema(result)
    return result, receipt


def vote(state, actor, proposal_id, decision, now, *, expected_revision=None):
    result = _prepare(state, actor, now)
    _identifier(proposal_id)
    _need(type(decision) is str and decision in {'approve', 'reject'}, 'invalid_council_vote')
    if expected_revision is not None:
        _integer(expected_revision)
        _need(expected_revision == state['revision'], 'stale_council_revision')
    expired = _expire(result, now)
    if proposal_id in expired:
        return result, _receipt(result, actor, proposal_id, 'expired')
    _need(proposal_id in result['pending'], 'unknown_council_proposal')
    item = result['pending'][proposal_id]
    _need(actor in item['electorate'] and actor != item['proposer'], 'council_self_vote')
    _need(actor not in item['votes'], 'duplicate_council_vote')
    status = 'vote_recorded'
    if decision == 'reject':
        del result['pending'][proposal_id]
        status = 'rejected'
    elif len(item['votes']) + 1 >= 2:
        body = item['proposal']
        if body['kind'] == 'meeting':
            status = 'meeting_approved'
        else:
            if len(result['members']) >= 6:
                return result, _receipt(result, actor, proposal_id, 'member_limit')
            last = result['last_admission_at']
            if last is not None and now - last < DAY:
                # Do not consume the second vote: it can be resubmitted after cooldown.
                return result, _receipt(result, actor, proposal_id, 'cooldown')
            result['members'][body['member_id']] = {'adapter': ADAPTER, 'focus': body['focus'],
                'admitted_at': now, 'proposal_id': proposal_id, 'proposer': item['proposer'],
                'approvers': sorted([*item['votes'], actor])}
            result['last_admission_at'] = now
            status = 'member_admitted'
        del result['pending'][proposal_id]
    else:
        item['votes'][actor] = decision
    receipt = _record(result, actor, proposal_id, status, now)
    check_schema(result)
    return result, receipt


def apply_action(state, actor, action, now):
    """Safe model-action boundary; malformed durable state still raises."""
    check_schema(state)
    try:
        _need(type(action) is dict and type(action.get('operation')) is str,
              'invalid_council_action')
        if action['operation'] == 'propose':
            _need(set(action) == {'operation', 'proposal'}, 'invalid_council_action')
            return receive_proposal(state, actor, action['proposal'], now)
        _need(action['operation'] == 'vote' and set(action) == {'operation',
              'proposal_id', 'decision', 'expected_revision'}, 'invalid_council_action')
        _integer(action['expected_revision'])
        return vote(state, actor, action['proposal_id'], action['decision'], now,
                    expected_revision=action['expected_revision'])
    except (ValueError, TypeError, KeyError):
        # No raw untrusted payload, exception text, credentials or URLs in receipts.
        return copy.deepcopy(state), _receipt(state, actor if actor in members(state)
            else 'unknown', None, 'rejected', 'invalid_or_unauthorized_action')

