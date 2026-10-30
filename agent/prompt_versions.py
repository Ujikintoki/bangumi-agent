"""Versions of allowlisted public template sources, never of prepared user input.

Bundles include template builders and static scaffolding. A bundle change can
invalidate the version even when a particular rendered prompt stays identical.
Sources are cached per process, matching the application's restart-to-load-code lifecycle.
"""
from functools import lru_cache
from hashlib import sha256
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SHARED = ('agent/helpers.py', 'agent/persona/profiles.py')
_TEMPLATE_SOURCES = {
    'classify': ('agent/nodes/classify.py', *_SHARED),
    'reasoning': ('agent/nodes/reasoning.py', 'agent/config.py',
                  'agent/prompts/aggregator.py', 'agent/prompts/scene_hints.py',
                  'agent/prompts/scene_hints_deep.py', *_SHARED),
    'pipeline': ('agent/nodes/pipeline.py', 'agent/prompts/pipeline.py', *_SHARED),
    'render': ('agent/persona/render.py', 'main.py', *_SHARED),
}


@lru_cache(maxsize=4)
def _bundle_version(family):
    digest = sha256()
    for relative in _TEMPLATE_SOURCES[family]:
        content = (_ROOT / relative).read_bytes()
        digest.update(relative.encode() + b'\0' + len(content).to_bytes(8, 'big') + content)
    return f'public-source-v1:{family}:{digest.hexdigest()}'


def public_prompt_metadata(stage):
    family = stage.partition('#')[0] if isinstance(stage, str) else ''
    if family not in _TEMPLATE_SOURCES:
        return {'prompt_version': None, 'prompt_version_status': 'unavailable',
                'prompt_version_reason': 'unregistered_stage'}
    try:
        return {'prompt_version': _bundle_version(family), 'prompt_version_status': 'available'}
    except Exception:
        # Missing packaged sources must not prevent a model invocation.
        return {'prompt_version': None, 'prompt_version_status': 'unavailable',
                'prompt_version_reason': 'public_source_unavailable'}
