"""Build an isolated, tool-free DSH 0.1.7-rc.2 profile; never execute it here.

The caller owns the one-request loopback HTTP guard and an external timeout.
An empty tools registry removes executable capabilities, but does not itself
limit the number of HTTP attempts a malformed provider response could induce.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path

DSH_VERSION = '0.1.7-rc.2'
UPSTREAM_COMMIT = '477b4f420553e8a52c2fbccc464d7561b239c443'
PROFILE = 'srf-review'
MODEL = 'mimo-v2.5-free'
PROVIDER = 'srf-loopback'
MAX_TOKENS = 1024
MAX_PROMPT_BYTES = 8192
LOOPBACK_ENV_KEY = 'SRF_DSH_LOOPBACK_KEY'
LOOPBACK_SENTINEL = 'public-loopback-only-not-a-credential'

# No bundle is inherited. In particular, base/headless defaults would add
# shell tools, title inference, retry, web, compaction and telemetry producers.
PLUGIN_NAMES = {
    'timer': '@deepseek-ai/cordis-plugin-timer',
    'llm': '@deepseek-ai/dsh-llm',
    'session': '@deepseek-ai/dsh-session',
    'session-projection': '@deepseek-ai/dsh-session-projection',
    'agent': '@deepseek-ai/dsh-agent',
    'system-prompt': '@deepseek-ai/dsh-system-prompt',
    'tools': '@deepseek-ai/dsh-tools',
    'agent-loop': '@deepseek-ai/dsh-agent-loop',
    'agent-default-model': '@deepseek-ai/dsh-agent-default-model',
    'llm-pi-ai': '@deepseek-ai/dsh-llm-pi-ai',
    'session-persistence-jsonl': '@deepseek-ai/dsh-session-persistence-jsonl',
    'headless-runner': '@deepseek-ai/dsh-headless',
}


def _need(condition):
    if not condition:
        raise ValueError('invalid_dsh_review_profile')


def profile_patch(*, port: int, prompt: str, session_root: str,
                  max_tokens: int = MAX_TOKENS) -> list:
    """Return JSON-compatible Cordis insert syntax, a subset of YAML.

    Only the port, bounded public prompt, absolute persistence path and output
    ceiling are variable. Provider/model/plugin identities are fixed.
    """
    _need(type(port) is int and 1 <= port <= 65535)
    _need(type(max_tokens) is int and 1 <= max_tokens <= MAX_TOKENS)
    _need(type(prompt) is str and prompt.strip() != '' and '\x00' not in prompt)
    try:
        _need(len(prompt.encode('utf-8')) <= MAX_PROMPT_BYTES)
    except UnicodeError as exc:
        raise ValueError('invalid_dsh_review_profile') from exc
    _need(type(session_root) is str and Path(session_root).is_absolute()
          and '\x00' not in session_root)
    configs = {
        'system-prompt': {
            'includeHarnessIdentity': False,
            'includeRuntimeContext': False,
            'personaPrefix': 'Review the supplied public research text. Return a concise plain-text critique.',
            'personaSuffix': '',
        },
        'tools': {'mode': 'native'},
        'agent-loop': {'agents': [], 'maxParallelToolCalls': 1},
        'agent-default-model': {'provider': PROVIDER, 'model': MODEL},
        'llm-pi-ai': {'providers': {PROVIDER: {
            'apiKeyEnv': LOOPBACK_ENV_KEY,
            'api': 'openai-completions',
            'baseURL': f'http://127.0.0.1:{port}/v1',
            'models': [{
                'id': MODEL, 'name': MODEL, 'contextWindow': 16384,
                'maxTokens': max_tokens, 'input': ['text'],
                'reasoningEfforts': False,
            }],
            'compat': {
                'supportsDeveloperRole': False,
                'supportsReasoningEffort': False,
                'maxTokensField': 'max_tokens',
                'supportsStore': False,
            },
            'timeoutMs': 60000,
            'streamIdleTimeoutMs': 60000,
            'retryPolicy': {'mode': 'normal', 'maxRetries': 0},
        }}},
        'session-persistence-jsonl': {'root': session_root, 'compression': 'none'},
        'headless-runner': {'task': prompt, 'json': True},
    }
    entries = []
    for identity, name in PLUGIN_NAMES.items():
        row = {'id': identity, 'name': name}
        if identity in configs:
            row['config'] = configs[identity]
        entries.append(row)
    return [{'insert': entries}]


def validate_patch(patch, *, port, prompt, session_root, max_tokens=MAX_TOKENS):
    """Reject added bundles/plugins/tools, altered endpoints and safety fields."""
    expected = profile_patch(port=port, prompt=prompt, session_root=session_root,
                             max_tokens=max_tokens)
    # Canonical JSON distinguishes true from 1, rejects unsupported objects and
    # checks every field rather than merely scanning plugin-name substrings.
    try:
        actual = json.dumps(patch, sort_keys=True, allow_nan=False)
        canonical = json.dumps(expected, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError, RecursionError) as exc:
        raise ValueError('invalid_dsh_review_profile') from exc
    _need(actual == canonical)


def prepare_profile(home, *, port: int, prompt: str,
                    max_tokens: int = MAX_TOKENS) -> dict:
    """Create a NEW isolated home; return cwd and explicit environment additions.

    The caller must launch with a scrubbed environment and the returned cwd.
    Existing homes are refused so home-level overrides, credentials and .env
    files cannot silently broaden this composition.
    """
    home = Path(home).resolve()
    session_root = str(home / 'sessions')
    patch = profile_patch(port=port, prompt=prompt, session_root=session_root,
                          max_tokens=max_tokens)
    validate_patch(patch, port=port, prompt=prompt, session_root=session_root,
                   max_tokens=max_tokens)
    raw = (json.dumps(patch, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
    # The exclusive create is intentional; no existing directory is overwritten.
    home.mkdir(parents=True, exist_ok=False)
    profile_dir = home / 'profiles' / PROFILE
    profile_dir.mkdir(parents=True)
    workspace = home / 'workspace'
    workspace.mkdir()
    manifest = {'name': 'dsh-profile-srf-review', 'private': True,
                'dependencies': {}, 'dsh': {'profile': {'bundles': []}}}
    (profile_dir / 'package.json').write_text(json.dumps(manifest, indent=2) + '\n', encoding='utf-8')
    # JSON is valid YAML; no Python YAML package or expression evaluation needed.
    (profile_dir / 'cordis.patch.yml').write_bytes(raw)
    return {
        'profile': PROFILE, 'home': str(home), 'cwd': str(workspace),
        'config_sha256': hashlib.sha256(raw).hexdigest(),
        'plugin_ids': list(PLUGIN_NAMES), 'model': MODEL, 'tools': [],
        'environment': {'DSH_HOME': str(home), 'DSH_TELEMETRY_DISABLED': '1',
                        LOOPBACK_ENV_KEY: LOOPBACK_SENTINEL},
    }


def command(node, dsh_bin, *, inspect_only=False) -> list[str]:
    """The task resides in Config; no prompt interpolation through a shell."""
    _need(type(inspect_only) is bool)
    argv = [str(node), str(dsh_bin), '--profile', PROFILE]
    if inspect_only:
        argv.append('--dump-config')
    return argv
