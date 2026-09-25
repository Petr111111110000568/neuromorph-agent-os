"""Trusted profile controller for existing pinned builtins, never an installer."""
from __future__ import annotations

import argparse
import ast
import copy
import hashlib
import json
import os
from pathlib import Path
import re
import sys
import uuid

from .daemon import _job_lock, _safe_path
from .providers import json_load
from ..resource_policy import load_policy

ROOT = Path(__file__).resolve().parents[2]
AGENTS = ('author', 'reviewer', 'reviser')
PINNED = ('plugin_worker.py', 'workbench/plugins.py', 'workbench/__init__.py',
          'workbench/morphogenesis.py', 'workbench/kan.py', 'workbench/cortical.py')
LIMIT = 128 * 1024


def digest(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False,
        separators=(',', ':'), allow_nan=False).encode('utf-8')).hexdigest()


def _read(path, limit=LIMIT):
    with _safe_path(path).open('rb') as stream:
        raw = stream.read(limit + 1)
    if len(raw) > limit:
        raise ValueError('coordination_file_too_large')
    return json_load(raw)


def _integer(value, low, high):
    if type(value) is not int or not low <= value <= high:
        raise ValueError('invalid_coordination_limit')


def _ids(value, available):
    if (type(value) is not list or len(value) > 7 or any(type(x) is not str for x in value)
            or len(set(value)) != len(value) or not set(value) <= set(available)):
        raise ValueError('unknown_or_duplicate_plugin')
    return sorted(value)


def _specs(raw, variable):
    """Read only literal id/version fields, without importing plugin code."""
    tree = ast.parse(raw.decode('utf-8'))
    values = [node.value for node in tree.body if isinstance(node, ast.Assign)
              and any(isinstance(target, ast.Name) and target.id == variable for target in node.targets)]
    if len(values) != 1:
        raise ValueError('registry_shape_changed')
    items = values[0].elts if variable == 'SPECS' and isinstance(values[0], ast.List) else [values[0]]
    result = {}
    for item in items:
        if not isinstance(item, ast.Dict):
            raise ValueError('registry_shape_changed')
        fields = {key.value: value for key, value in zip(item.keys, item.values)
                  if isinstance(key, ast.Constant) and key.value in {'id', 'version'}}
        plugin_id = ast.literal_eval(fields['id'])
        version = ast.literal_eval(fields['version']) if 'version' in fields else '0.4.0'
        if not isinstance(plugin_id, str) or plugin_id in result or not isinstance(version, str):
            raise ValueError('registry_shape_changed')
        result[plugin_id] = version
    return result


def catalogue(root=ROOT):
    root = _safe_path(root, directory=True)
    load_policy(root)
    pins = _read(root / 'data/builtin_pins.json', 16384)
    if type(pins) is not dict or set(pins) != {'files'} or set(pins['files']) != set(PINNED):
        raise ValueError('builtin_pins_changed')
    source = {}
    for name in PINNED:
        with _safe_path(root / name).open('rb') as stream:
            raw = stream.read(1024 * 1024 + 1)
        expected = pins['files'][name]
        if (len(raw) > 1024 * 1024 or not isinstance(expected, str)
                or hashlib.sha256(raw).hexdigest() != expected):
            raise ValueError('builtin_integrity_failure')
        source[name] = raw
    registry = _specs(source['workbench/plugins.py'], 'SPECS')
    for name in ('workbench/kan.py', 'workbench/cortical.py'):
        extra = _specs(source[name], 'SPEC')
        if set(extra) & set(registry):
            raise ValueError('duplicate_registry_plugin')
        registry.update(extra)
    config = _read(root / 'config/plugin_coordination.json', 32768)
    keys = {'schema_version', 'network_allowed', 'credentials_allowed', 'paid_calls_allowed',
            'resources', 'initial_profiles', 'catalogue'}
    if (type(config) is not dict or set(config) != keys or type(config['schema_version']) is not int
            or config['schema_version'] != 1 or any(config[k] is not False
            for k in ('network_allowed', 'credentials_allowed', 'paid_calls_allowed'))):
        raise ValueError('unsupported_coordination_policy')
    limits = config['resources']
    if type(limits) is not dict or set(limits) != {'max_enabled', 'cpu_seconds', 'memory_mb'}:
        raise ValueError('invalid_coordination_resources')
    for key, high in (('max_enabled', 7), ('cpu_seconds', 42), ('memory_mb', 3584)):
        _integer(limits[key], 1, high)
    entries = config['catalogue']
    if type(entries) is not dict or set(entries) != set(registry) or len(entries) != 7:
        raise ValueError('catalogue_must_match_existing_builtins')
    for plugin_id, entry in entries.items():
        if (type(entry) is not dict or set(entry) != {'version', 'roles', 'requires', 'conflicts',
                'cpu_seconds', 'memory_mb'} or entry['version'] != registry[plugin_id]
                or type(entry['roles']) is not list or not entry['roles']
                or len(set(entry['roles'])) != len(entry['roles']) or not set(entry['roles']) <= set(AGENTS)):
            raise ValueError('incompatible_catalogue_entry')
        _ids(entry['requires'], registry)
        _ids(entry['conflicts'], registry)
        if plugin_id in entry['requires'] or plugin_id in entry['conflicts']:
            raise ValueError('self_dependency_or_conflict')
        # Admission reservations match the reviewed worker's existing POSIX limits.
        if type(entry['cpu_seconds']) is not int or entry['cpu_seconds'] != 6:
            raise ValueError('unsupported_cpu_reservation')
        if type(entry['memory_mb']) is not int or entry['memory_mb'] != 512:
            raise ValueError('unsupported_memory_reservation')
    if type(config['initial_profiles']) is not dict or set(config['initial_profiles']) != set(AGENTS):
        raise ValueError('invalid_initial_profiles')
    value = {'schema_version': 1, 'config': config, 'pins': pins['files']}
    value['catalogue_sha256'] = digest(value)
    for agent, enabled in config['initial_profiles'].items():
        _profile(agent, enabled, value)
    return value


def _profile(agent, enabled, catalog):
    entries, limits = catalog['config']['catalogue'], catalog['config']['resources']
    if agent not in AGENTS:
        raise ValueError('unknown_agent')
    enabled = _ids(enabled, entries)
    if len(enabled) > limits['max_enabled']:
        raise ValueError('profile_resource_limit')
    for plugin_id in enabled:
        entry = entries[plugin_id]
        if (agent not in entry['roles'] or not set(entry['requires']) <= set(enabled)
                or set(entry['conflicts']) & set(enabled)):
            raise ValueError('profile_incompatible')
    for resource in ('cpu_seconds', 'memory_mb'):
        if sum(entries[x][resource] for x in enabled) > limits[resource]:
            raise ValueError('profile_resource_limit')
    return enabled


def initial_state(catalog):
    return {'schema_version': 1, 'catalogue_sha256': catalog['catalogue_sha256'], 'revision': 0,
            'profiles': {agent: _profile(agent, enabled, catalog)
                         for agent, enabled in catalog['config']['initial_profiles'].items()}, 'journal': []}


def validate_state(state, catalog, *, journal_limit=64):
    if (type(state) is not dict or set(state) != {'schema_version', 'catalogue_sha256', 'revision', 'profiles', 'journal'}
            or type(state['schema_version']) is not int or state['schema_version'] != 1
            or state['catalogue_sha256'] != catalog['catalogue_sha256']):
        raise ValueError('profile_catalogue_changed')
    _integer(state['revision'], 0, 10**9)
    if type(state['profiles']) is not dict or set(state['profiles']) != set(AGENTS):
        raise ValueError('invalid_profiles')
    for agent, enabled in state['profiles'].items():
        if _profile(agent, enabled, catalog) != enabled:
            raise ValueError('profiles_must_be_canonical')
    _integer(journal_limit, 1, 64)
    journal = state['journal']
    if type(journal) is not list or len(journal) != min(state['revision'], journal_limit):
        raise ValueError('invalid_profile_journal')
    prior = None
    for entry in journal:
        if (type(entry) is not dict or set(entry) != {'revision', 'operation', 'actor', 'target', 'before',
                'after', 'proposal_sha256', 'previous_sha256', 'entry_sha256'}
                or entry['operation'] not in {'apply', 'rollback'} or entry['actor'] not in (*AGENTS, 'controller')
                or entry['target'] not in AGENTS):
            raise ValueError('invalid_profile_journal')
        _integer(entry['revision'], 1, 10**9)
        for key in ('proposal_sha256', 'previous_sha256', 'entry_sha256'):
            if not isinstance(entry[key], str) or not re.fullmatch('[a-f0-9]{64}', entry[key]):
                raise ValueError('invalid_profile_journal')
        if digest({k: v for k, v in entry.items() if k != 'entry_sha256'}) != entry['entry_sha256']:
            raise ValueError('profile_journal_integrity')
        _profile(entry['target'], entry['before'], catalog)
        _profile(entry['target'], entry['after'], catalog)
        if prior and (entry['revision'] != prior['revision'] + 1 or entry['previous_sha256'] != prior['entry_sha256']):
            raise ValueError('profile_journal_integrity')
        prior = entry
    if journal and (journal[0]['revision'] != state['revision'] - len(journal) + 1
                    or (journal[0]['revision'] == 1 and journal[0]['previous_sha256'] != '0' * 64)
                    or journal[-1]['revision'] != state['revision']
                    or journal[-1]['after'] != state['profiles'][journal[-1]['target']]):
        raise ValueError('profile_journal_integrity')
    return state


def proposal(state, catalog, actor, target, enable, disable, reason, *, journal_limit=64):
    value = {'schema_version': 1, 'catalogue_sha256': catalog['catalogue_sha256'],
             'expected_revision': state['revision'], 'actor': actor, 'target': target,
             'enable': enable, 'disable': disable, 'reason': reason}
    evaluate(state, catalog, value, journal_limit=journal_limit)
    return value


def evaluate(state, catalog, request, *, journal_limit=64):
    validate_state(state, catalog, journal_limit=journal_limit)
    if (type(request) is not dict or set(request) != {'schema_version', 'catalogue_sha256', 'expected_revision',
            'actor', 'target', 'enable', 'disable', 'reason'} or type(request['schema_version']) is not int
            or request['schema_version'] != 1 or request['catalogue_sha256'] != catalog['catalogue_sha256']
            or type(request['expected_revision']) is not int or request['expected_revision'] != state['revision']):
        raise ValueError('stale_or_invalid_proposal')
    if request['actor'] not in AGENTS or request['target'] not in AGENTS or request['actor'] == request['target']:
        raise ValueError('peer_proposal_required')
    if not isinstance(request['reason'], str) or not 1 <= len(request['reason']) <= 500 or '\0' in request['reason']:
        raise ValueError('invalid_proposal_reason')
    entries = catalog['config']['catalogue']
    enable, disable = _ids(request['enable'], entries), _ids(request['disable'], entries)
    if set(enable) & set(disable) or not (enable or disable):
        raise ValueError('ambiguous_proposal')
    before = state['profiles'][request['target']]
    after = _profile(request['target'], sorted((set(before) | set(enable)) - set(disable)), catalog)
    if after == before:
        raise ValueError('proposal_has_no_change')
    plan = {'revision': state['revision'], 'catalogue_sha256': catalog['catalogue_sha256'],
            'proposal_sha256': digest(request), 'actor': request['actor'], 'target': request['target'],
            'before': list(before), 'after': after, 'configuration_only': True, 'execution_allowed': False}
    return dict(plan, plan_sha256=digest(plan))


def _change(state, plan, operation, *, journal_limit=64):
    updated = copy.deepcopy(state)
    updated['revision'] += 1
    updated['profiles'][plan['target']] = list(plan['after'])
    entry = {k: plan[k] for k in ('actor', 'target', 'before', 'after', 'proposal_sha256')}
    entry.update(revision=updated['revision'], operation=operation,
                 previous_sha256=state['journal'][-1]['entry_sha256'] if state['journal'] else '0' * 64)
    entry['entry_sha256'] = digest(entry)
    updated['journal'] = (updated['journal'] + [entry])[-journal_limit:]
    return updated


def apply_plan(state, catalog, request, expected_plan_sha256, *, journal_limit=64):
    plan = evaluate(state, catalog, request, journal_limit=journal_limit)
    if expected_plan_sha256 != plan['plan_sha256']:
        raise ValueError('reviewed_plan_changed')
    return validate_state(_change(state, plan, 'apply', journal_limit=journal_limit), catalog, journal_limit=journal_limit)


def rollback_state(state, catalog, expected_revision):
    validate_state(state, catalog)
    if type(expected_revision) is not int or expected_revision != state['revision'] or not state['journal']:
        raise ValueError('stale_or_missing_rollback')
    last = state['journal'][-1]
    plan = {'actor': 'controller', 'target': last['target'], 'before': last['after'], 'after': last['before'],
            'proposal_sha256': digest({'rollback_revision': expected_revision, 'entry': last['entry_sha256']})}
    return validate_state(_change(state, plan, 'rollback'), catalog)


def _storage(root, create=False):
    root = _safe_path(root, directory=True)
    folder = root
    for name in ('runtime', 'plugin-coordination'):
        folder = _safe_path(folder / name, directory=True)
        if create:
            folder.mkdir(exist_ok=True)
    return _safe_path(folder / 'state.json')


def _atomic(path, state):
    raw = (json.dumps(state, sort_keys=True, ensure_ascii=False, allow_nan=False) + '\n').encode('utf-8')
    if len(raw) > LIMIT:
        raise ValueError('profile_state_too_large')
    temporary = path.with_name(path.name + '.tmp-' + uuid.uuid4().hex)
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(fd, 'wb') as stream:
            stream.write(raw)
            stream.flush()
            os.fsync(stream.fileno())
        _safe_path(path)
        os.replace(temporary, path)
        if os.name != 'nt':
            parent_fd = os.open(path.parent, os.O_RDONLY)
            try:
                os.fsync(parent_fd)
            finally:
                os.close(parent_fd)
    finally:
        if temporary.exists():
            temporary.unlink()


def transact(root, operation, request=None, expected=None):
    path = _storage(root, create=True)
    with _job_lock(path.with_suffix('.lock')):
        catalog = catalogue(root)
        if operation == 'init':
            if path.exists():
                raise ValueError('profiles_already_initialized')
            state = initial_state(catalog)
        else:
            state = validate_state(_read(path), catalog)  # Missing state never resets implicitly.
            if operation == 'apply':
                state = apply_plan(state, catalog, request, expected)
            elif operation == 'rollback':
                state = rollback_state(state, catalog, expected)
            else:
                raise ValueError('unsupported_profile_operation')
        _atomic(path, state)
    return state


def require_enabled(agent, plugin_id, root=ROOT):
    """Admission hook before an existing runner; this function never runs code."""
    catalog = catalogue(root)
    state = validate_state(_read(_storage(root)), catalog)
    if agent not in AGENTS or plugin_id not in state['profiles'][agent]:
        raise ValueError('plugin_disabled_for_agent')
    return {'admitted': True, 'agent': agent, 'plugin_id': plugin_id, 'revision': state['revision'],
            'catalogue_sha256': catalog['catalogue_sha256'], 'configuration_only': True,
            'execution_allowed': False, 'required_runner': 'existing_pinned_builtin_worker'}


def bootstrap(root=ROOT):
    """Explicit trusted bootstrap: configure all reviewed builtins for each role."""
    path = _storage(root, create=True)
    if not path.exists():
        transact(root, 'init')
    receipts = []
    for actor, target in (('author', 'reviewer'), ('reviewer', 'author'), ('author', 'reviser')):
        catalog = catalogue(root)
        state = validate_state(_read(path), catalog)
        missing = sorted(set(catalog['config']['catalogue']) - set(state['profiles'][target]))
        if not missing:
            continue
        request = proposal(state, catalog, actor, target, missing, [], 'Trusted catalogue bootstrap; no model output.')
        plan = evaluate(state, catalog, request)
        changed = transact(root, 'apply', request, plan['plan_sha256'])
        receipts.append({'actor': actor, 'target': target, 'revision': changed['revision'],
                         'plan_sha256': plan['plan_sha256']})
    return {'configuration_only': True, 'execution_allowed': False, 'model_calls': 0,
            'installed_remote_code': False, 'profiles': validate_state(_read(path), catalogue(root))['profiles'],
            'changes': receipts}


def main(argv=None):
    parser = argparse.ArgumentParser(description='Pinned project plugin profile coordination; no code installation')
    parser.add_argument('operation', choices=('catalogue', 'init', 'status', 'propose', 'evaluate', 'apply', 'rollback', 'bootstrap'))
    parser.add_argument('--root', type=Path, default=ROOT)
    parser.add_argument('--proposal', type=Path)
    parser.add_argument('--actor', choices=AGENTS)
    parser.add_argument('--target', choices=AGENTS)
    parser.add_argument('--enable', nargs='*', default=[])
    parser.add_argument('--disable', nargs='*', default=[])
    parser.add_argument('--reason', default='Proposed peer profile change')
    parser.add_argument('--plan-sha256')
    parser.add_argument('--expected-revision', type=int)
    args = parser.parse_args(argv)
    try:
        if args.operation == 'bootstrap':
            result = bootstrap(args.root)
        elif args.operation in {'init', 'rollback'}:
            result = transact(args.root, args.operation, expected=args.expected_revision)
        else:
            catalog = catalogue(args.root)
            if args.operation == 'catalogue':
                result = catalog
            else:
                state = validate_state(_read(_storage(args.root)), catalog)
                if args.operation == 'status':
                    result = state
                elif args.operation == 'propose':
                    result = proposal(state, catalog, args.actor, args.target, args.enable, args.disable, args.reason)
                else:
                    request = _read(args.proposal, 8192)
                    result = evaluate(state, catalog, request) if args.operation == 'evaluate' else transact(
                        args.root, 'apply', request, args.plan_sha256)
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
        return 0
    except (OSError, ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        print('Plugin coordination stopped: invalid policy, pins, profile, proposal or concurrent state.', file=sys.stderr)
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
