"""Fixed E3 design shared by planning, freezing and offline analysis."""
from hashlib import sha256
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ARMS = ('S1c', 'M2', 'S1')
KERNEL_VERSION = 'aftercare-kernel-v3.1'
N = 67
EXCLUDED = ('16', '22', '29', '41', '44', '54', '96')
LIMITS = {
    'max_usd': 20, 'max_calls': 201 * (160 + 60 + 1),
    'agent_calls': 160, 'user_calls': 60, 'max_output_tokens': 1500,
    'concurrency': 6, 'run_timeout': 900, 'request_timeout': 120,
}
RULES = {
    'H_lift': 'M2 成功题数 > S1c 成功题数，且 McNemar 精确检验双侧 p < 0.05。',
    'H_cost': 'M2 成功题数 >= S1 成功题数 - 3，且 M2 每成功一题的总费用 < S1；三组中任一组有未结算调用或费用不完整时为 undetermined。零成功时每成功费用未定义，也为 undetermined。',
    'conclusion': '两条均为 false 时为 no_advantage_shown；任一为 true 时报告该条成立；没有 true 且存在 undetermined 时结论为 undetermined。',
    'denominator': '每组分母始终为 67；未运行、未完成、无得分、预算耗尽均保留，未获成功分数不计成功。',
    'repair': '仅 error/unknown 且无官方得分可在同级 .repair 目录补跑一次；沿用原题、组和种子，不补跑已有 0 分、超时或预算耗尽项；合并所有尝试费用与事件，原始加补跑受原 $20 余额约束。',
}


def population(source=None, development=None):
    source = source or ROOT / '.runtime/tau-official-source'
    development = development or ROOT / 'evidence/architecture_refactor/kernel_v3_dev_tasks.json'
    frozen = json.loads(development.read_text())
    retail = source / 'data/tau2/domains/retail'
    for name in ('tasks.json', 'split_tasks.json'):
        if frozen.get('dataset_sha256', {}).get(name) != sha256((retail / name).read_bytes()).hexdigest():
            raise SystemExit(f'E3 development dataset hash mismatch: {name}')
    train = json.loads((retail / 'split_tasks.json').read_text())['train']
    excluded = frozen.get('development_task_ids', [])
    if sorted(excluded, key=int) != list(EXCLUDED):
        raise SystemExit('E3 development exclusions differ from the seven frozen tasks')
    if len(train) != 74 or len(set(train)) != 74 or not set(excluded) <= set(train):
        raise SystemExit('Unexpected official retail train population')
    tasks = sorted(set(train) - set(excluded), key=int)
    if len(tasks) != N or set(tasks) & set(excluded):
        raise SystemExit('E3 requires exactly 67 non-development train tasks')
    return tasks, sorted(excluded, key=int)


def validate_options(args):
    if args.task_ids is not None:
        raise SystemExit('confirm does not accept --task-ids')
    if args.architectures != ','.join(ARMS):
        raise SystemExit('confirm requires --architectures S1c,M2,S1 in that order')
    if any(getattr(args, key) != value for key, value in LIMITS.items()):
        raise SystemExit('confirm requires the fixed E3 budget and runtime limits')


def validate_manifest(manifest, tasks, excluded):
    from aftercare.agent_kernel.contracts import VERSION
    expected = [(task, ARMS[(i + offset) % 3], 42 + i)
                for i, task in enumerate(tasks) for offset in range(3)]
    actual = [(r['task_id'], r['architecture'], r['seed']) for r in manifest['schedule']]
    budget = {
        'max_usd': 20, 'max_calls': LIMITS['max_calls'], 'max_agent_calls_per_run': 160,
        'max_user_calls_per_run': 60, 'max_review_calls_per_run': 1,
        'max_output_tokens_per_call': 1500, 'max_steps': 100,
        'timeout_seconds_per_run': 900, 'timeout_seconds_per_request': 120,
    }
    if (manifest.get('scope') != 'confirm' or manifest.get('split') != 'train'
            or manifest.get('architectures') != list(ARMS) or manifest.get('task_ids') != tasks
            or manifest.get('excluded_development_task_ids') != excluded
            or manifest.get('kernel_version') != KERNEL_VERSION or VERSION != KERNEL_VERSION
            or manifest.get('budget') != budget or manifest.get('concurrency') != 6
            or actual != expected):
        raise ValueError('Manifest does not match the frozen E3 population and schedule')
