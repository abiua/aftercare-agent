"""Generate an E4 draft inventory offline; refuse to replace any existing inventory."""
import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from kernel_e4_protocol import (
    ARMS, CELLS, E3_MANIFEST, E3_RESULTS, KERNEL_VERSION, LIMITS, N, PREREGISTRATION,
    ROOT, RULES, current_configuration, population, schedule, schedule_sha256,
    validate_e3_consistency,
)


def inventory():
    from aftercare.agent_kernel.contracts import architectures
    config = current_configuration()
    validate_e3_consistency(config)
    tasks, excluded = population()
    items = schedule(tasks)
    files = sorted((ROOT / 'backend/src/aftercare/agent_kernel').rglob('*.py'))
    files += [ROOT / name for name in (
        'scripts/tau_official_agents.py', 'scripts/tau_official_agents_legacy.py',
        'scripts/run_tau_official_campaign.py', 'scripts/tau_official_budget.py',
        'scripts/tau_official_summary.py', 'scripts/refactor_baseline.py',
        'scripts/analyze_kernel_campaign.py', 'scripts/analyze_kernel_e3.py',
        'scripts/kernel_e3_protocol.py', 'scripts/kernel_e4_protocol.py',
        'scripts/analyze_kernel_e4.py', 'scripts/freeze_kernel_e4.py',
        'scripts/tests/test_kernel_e4.py', 'docs/REFACTOR_PREREGISTRATION_E4.md',
        'evidence/architecture_refactor/kernel_v3_dev_tasks.json',
    )]
    return {
        'version': 'E4-draft', 'status': 'draft_unfrozen', 'status_note': '草稿，未冻结',
        'git_head': subprocess.check_output(['git', '-C', ROOT, 'rev-parse', 'HEAD'], text=True).strip(),
        'git_head_note': '生成时 HEAD，仅供溯源；未提交改动由文件哈希标识，本草稿不构成真实运行授权。',
        'kernel_version': KERNEL_VERSION, 'split': 'train', 'task_ids': tasks,
        'excluded_development_task_ids': excluded, 'configuration': config,
        'architectures': {name: architectures()[name].model_dump(mode='json') for name in CELLS},
        'design': {'cells': list(CELLS), 'new_arms': list(ARMS), 'reused_E3_arms': ['S1c', 'M2'],
                   'factors': {name: {'review': architectures()[name].review != 'none',
                                     'experts': architectures()[name].experts} for name in CELLS}},
        'run': {**LIMITS, 'tasks': N, 'runs': N * len(ARMS), 'architectures': list(ARMS),
                'trials_per_task': 1, 'review_calls': 1, 'max_steps': 100},
        'rules': RULES, 'descriptive': True, 'schedule': items,
        'schedule_sha256': schedule_sha256(items),
        'schedule_hash_note': '有序 task_id/architecture/seed 对象列表；键排序，紧凑 JSON UTF-8；不含公开题目摘要。',
        'sha256': {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in files},
        'kernel_e3_results_sha256': sha256(E3_RESULTS.read_bytes()).hexdigest(),
        'e3_manifest_sha256': sha256(E3_MANIFEST.read_bytes()).hexdigest(),
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=PREREGISTRATION)
    args = parser.parse_args()
    data = inventory()
    with args.output.open('x') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, allow_nan=False)
        stream.write('\n')
    print(args.output)


if __name__ == '__main__':
    main()
