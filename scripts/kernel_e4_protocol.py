"""Fixed, descriptive E4 design; E3 population and frozen inputs are read-only."""
from hashlib import sha256
import json

from kernel_e3_protocol import (
    EXCLUDED, KERNEL_VERSION, LIMITS as E3_LIMITS, N, ROOT, RULES as E3_RULES,
    population, validate_manifest as validate_e3_manifest,
)

ARMS = ('M2x', 'M2r')
CELLS = ('S1c', 'M2r', 'M2x', 'M2')
LIMITS = {**E3_LIMITS, 'max_usd': 8, 'max_calls': 134 * (160 + 60 + 1)}
E3_MANIFEST = ROOT / '.runtime/kernel-e3/run-v1/manifest.json'
E3_RESULTS = ROOT / 'evidence/architecture_refactor/kernel_e3_results.json'
PREREGISTRATION = ROOT / 'evidence/architecture_refactor/preregistration_e4.json'
DESCRIPTIVE_NOTE = (
    '本实验为描述性实验，不做假设判定。每组分母固定为 67，每题每组单次运行，功效不足。'
    '“约 8 题以内的差距达不到显著”只能作为粗略功效提醒，不能作为固定阈值；'
    'McNemar 精确双侧 p 取决于独对题的双向分布，例如 6 对 0 时 p=0.03125。'
    '新旧两批运行日期不同，模型服务和时间变化可能混入组间差异。'
)
RULES = {
    'denominator': E3_RULES['denominator'],
    'repair': E3_RULES['repair'].replace('$20', '$8'),
    'reuse': 'S1c、M2 直接复用 E3 汇总（含其补跑），不重跑；读取前核对预注册记录的 SHA256。',
    'report': '报告四格统计、四个配对比较的独对题数和题号及 McNemar 精确双侧 p、成功题数交互差与机制计数；不输出假设判定。',
    'descriptive': DESCRIPTIVE_NOTE,
}
MATCH_FIELDS = (
    'kernel_version', 'kernel_sha256', 'agent_adapter_sha256', 'dataset_sha256',
    'revision', 'agent_model', 'user_model', 'review_model', 'pricing_usd_per_million',
)


def schedule(tasks):
    return [{'task_id': task, 'architecture': ARMS[(i + offset) % 2], 'seed': 42 + i}
            for i, task in enumerate(tasks) for offset in range(2)]


def schedule_sha256(items):
    """Hash ordered identities, excluding optional public task summaries."""
    identities = [{key: row[key] for key in ('task_id', 'architecture', 'seed')} for row in items]
    return sha256(json.dumps(identities, sort_keys=True, separators=(',', ':')).encode()).hexdigest()


def validate_options(args):
    if args.task_ids is not None:
        raise SystemExit('ablate does not accept --task-ids')
    if args.architectures != ','.join(ARMS):
        raise SystemExit('ablate requires --architectures M2x,M2r in that order')
    if any(getattr(args, key) != value for key, value in LIMITS.items()):
        raise SystemExit('ablate requires the fixed E4 budget and runtime limits')


def current_configuration():
    from aftercare.agent_kernel.contracts import VERSION
    from run_tau_official_campaign import AGENT, PRICES, REVIEW, REVISION, USER, load_official_source
    hashes, _ = load_official_source()
    kernel = ROOT / 'backend/src/aftercare/agent_kernel'
    return {
        'kernel_version': VERSION,
        'kernel_sha256': {str(p.relative_to(kernel)): sha256(p.read_bytes()).hexdigest()
                          for p in sorted(kernel.rglob('*.py'))},
        'agent_adapter_sha256': sha256((ROOT / 'scripts/tau_official_agents.py').read_bytes()).hexdigest(),
        'dataset_sha256': hashes, 'revision': REVISION,
        'agent_model': AGENT, 'user_model': USER, 'review_model': REVIEW,
        'pricing_usd_per_million': {model: list(prices) for model, prices in PRICES.items()},
    }


def validate_e3_consistency(manifest, e3_manifest_path=None):
    """Compare both recorded E4 inputs and current code/config with the original E3 run."""
    path = e3_manifest_path or E3_MANIFEST
    original = json.loads(path.read_bytes())
    tasks, excluded = population()
    validate_e3_manifest(original, tasks, excluded)
    current = current_configuration()
    # Pricing uses tuples in the runner before JSON serialization.
    recorded = json.loads(json.dumps(manifest))
    for key in MATCH_FIELDS:
        if key not in original or recorded.get(key) != original[key] or current[key] != original[key]:
            raise ValueError(f'E4 differs from E3 manifest: {key}')
    return original


def validate_manifest(manifest, tasks, excluded, *, e3_manifest_path=None):
    expected_budget = {
        'max_usd': 8, 'max_calls': LIMITS['max_calls'], 'max_agent_calls_per_run': 160,
        'max_user_calls_per_run': 60, 'max_review_calls_per_run': 1,
        'max_output_tokens_per_call': 1500, 'max_steps': 100,
        'timeout_seconds_per_run': 900, 'timeout_seconds_per_request': 120,
    }
    actual = [{key: row.get(key) for key in ('task_id', 'architecture', 'seed')}
              for row in manifest.get('schedule', [])]
    if (manifest.get('scope') != 'ablate' or manifest.get('split') != 'train'
            or manifest.get('architectures') != list(ARMS) or manifest.get('task_ids') != tasks
            or manifest.get('excluded_development_task_ids') != excluded
            or manifest.get('kernel_version') != KERNEL_VERSION
            or manifest.get('budget') != expected_budget or manifest.get('concurrency') != 6
            or manifest.get('trials_per_task') != 1 or actual != schedule(tasks)):
        raise ValueError('Manifest does not match the fixed E4 population and schedule')
    original = validate_e3_consistency(manifest, e3_manifest_path)
    for key in ('schema_version', 'benchmark', 'domain', 'evaluation_type', 'reasoning_effort',
                'price_order', 'tool_names', 'cache_mode', 'seed_scope'):
        if manifest.get(key) != original.get(key):
            raise ValueError(f'E4 differs from E3 manifest: {key}')
