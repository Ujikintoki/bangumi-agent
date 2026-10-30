"""Bounded Render-only contrast using frozen C5 messages and the formal recorder.

No graph, tools, session writes or production prompt changes. The default prepares
the four conditions only; --allow-paid executes exactly three blocks, without retries.
"""
import argparse
import asyncio
import json
import os
from pathlib import Path
import random
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
SOURCE = ROOT / '.local-data/acceptance/c1-c5-20261030-formal-run1/C5/trace.json'
OLD_FRAME = '这是一段闲聊。自然地用你的角色性格回复。不要列数据、不要提搜索、就像朋友聊天一样。'
TASK_FRAME = '本轮任务是整理用户观点，输出用户要求的短 comment。'
GROUPS = {'A': (False, False), 'B': (True, False), 'C': (False, True), 'D': (True, True)}


def save(path, value):
    with open(path, 'x', encoding='utf-8', opener=lambda name, flags: os.open(name, flags, 0o600)) as stream:
        json.dump(value, stream, ensure_ascii=False, indent=2)


def prepare():
    source = json.loads(SOURCE.read_text())
    model = next(r for r in source['records'] if r['name'] == 'model.invoke' and r['attributes']['stage'] == 'render')
    artifacts = {a['role']: a['value'] for a in model['artifacts']}
    messages = [{'role': m['role'], 'content': m['content']} for m in artifacts['model_input']]
    assert [m['role'] for m in messages] == ['system', 'human']
    assert messages[0]['content'].count(OLD_FRAME) == 1
    assert messages[0]['content'].count('\n\n## 必须遵守\n') == 1
    variants = {}
    for group, (neutral, task) in GROUPS.items():
        items = [dict(m) for m in messages]
        if neutral:
            _, rest = items[0]['content'].split('\n\n## 必须遵守\n', 1)
            items[0]['content'] = '# 你是谁 + 你怎么说话\n你是一个用简体中文回复用户的助手。\n\n## 必须遵守\n' + rest
        if task:
            items[0]['content'] = items[0]['content'].replace(OLD_FRAME, TASK_FRAME)
        assert items[1] == messages[1]
        variants[group] = items
    assert variants['A'] == messages
    params = artifacts['model_parameters']
    assert params['temperature'] == .4 and params['model'] == 'deepseek-v4-flash'
    return model['trace'], variants, params


async def execute(output, variants, params, manifest):
    from core.config import get_settings
    from core.observability import Runtime, invoke
    from core.observability.maintenance import export
    from agent.llm import create_llm
    from langchain_core.messages import SystemMessage, HumanMessage
    settings = get_settings()
    runtime = Runtime(output / 'observability', secrets=[settings.LLM_API_KEY, settings.BANGUMI_ACCESS_TOKEN,
                                                       settings.BANGUMI_APP_SECRET, settings.ZHIPU_API_KEY])
    runtime.start()
    rows = []
    try:
        runtime.diagnostic('experiment', 'c5-render', seconds=1800)
        model = create_llm(model=params['model'], temperature=params['temperature'],
                           max_tokens=params['max_completion_tokens'], request_timeout=60,
                           max_retries=0, _telemetry_label='render')
        for index, item in enumerate(manifest['order'], 1):
            group, block = item['group'], item['block']
            sample = f'sample-{index:02}'
            messages = [(SystemMessage if m['role'] == 'system' else HumanMessage)(content=m['content'])
                        for m in variants[group]]
            with runtime.turn('experiment', 'c5-render', sample) as scope:
                scope.event('experiment.condition', schema_version='c5-render-contrast-v1',
                            selected={'group': group, 'block': block, 'sample': sample})
                scope.capture('experiment_input', lambda: variants[group])
                response = await invoke(model.ainvoke, messages)
                text = response.content
                assert isinstance(text, str)
                scope.capture('experiment_output', lambda: text)
            row = {'sample': sample, 'group': group, 'block': block, 'reply': text,
                   'usage': response.usage_metadata, 'response_model': response.response_metadata.get('model_name')}
            rows.append(row)
            save(output / f'{sample}.json', row)
            print(f'{sample}: completed', flush=True)
    finally:
        runtime.close()
        save(output / 'trace.json', export(runtime, 'experiment', 'c5-render', content=True))
        save(output / 'results.json', rows)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--allow-paid', action='store_true')
    args = parser.parse_args()
    output = args.output.resolve()
    if (ROOT / '.local-data').resolve() not in output.parents:
        parser.error('output must be below .local-data')
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    trace, variants, params = prepare()
    order = []
    rng = random.Random(20261030)
    for block in range(1, 4):
        groups = list(GROUPS)
        rng.shuffle(groups)
        order.extend({'group': group, 'block': block} for group in groups)
    manifest = {'source_trace': trace, 'source': str(SOURCE.relative_to(ROOT)), 'groups': GROUPS,
                'parameters': params, 'order': order, 'max_calls': 12, 'sdk_retries': 0,
                'comparison': 'raw model outputs before Render postprocessing',
                'production_code_changed': False, 'paid_execution': args.allow_paid}
    save(output / 'manifest.json', manifest)
    save(output / 'conditions.json', variants)
    if args.allow_paid:
        asyncio.run(execute(output, variants, params, manifest))
    else:
        print('Prepared four conditions; no model requests made.')


if __name__ == '__main__':
    try:
        main()
    except Exception as error:
        print(f'Contrast stopped: {type(error).__name__}; no automatic rerun.', file=sys.stderr)
        raise SystemExit(1) from None
