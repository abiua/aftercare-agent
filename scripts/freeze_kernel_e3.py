"""Generate the E3 draft inventory offline; no commit hash is inferred or filled."""
from hashlib import sha256
import argparse
import json
from pathlib import Path

from kernel_e3_protocol import ARMS, KERNEL_VERSION, LIMITS, ROOT, RULES, population
from run_tau_official_campaign import SOURCE, load_official_source
from tau_official_budget import USER, REVIEW


def inventory():
    from aftercare.agent_kernel.contracts import VERSION, architectures
    from aftercare.agent_kernel.engine import EXECUTOR_INSTRUCTION
    from aftercare.agent_kernel.experts import INSTRUCTIONS
    from aftercare.agent_kernel.review import REVIEW_INSTRUCTION
    if VERSION != KERNEL_VERSION:
        raise SystemExit('E3 requires aftercare-kernel-v3.1')
    hashes, _ = load_official_source()
    tasks, excluded = population(SOURCE)
    files = sorted((ROOT / 'backend/src/aftercare/agent_kernel').glob('*.py'))
    files += [ROOT / name for name in (
        'scripts/tau_official_agents.py', 'scripts/tau_official_agents_legacy.py',
        'scripts/run_tau_official_campaign.py', 'scripts/tau_official_budget.py',
        'scripts/tau_official_summary.py', 'scripts/analyze_kernel_campaign.py',
        'scripts/refactor_baseline.py', 'scripts/analyze_kernel_e3.py',
        'scripts/kernel_e3_protocol.py', 'scripts/freeze_kernel_e3.py',
        'scripts/tests/test_kernel_e3.py', 'docs/REFACTOR_PREREGISTRATION_E3.md',
        'evidence/architecture_refactor/kernel_v3_dev_tasks.json',
    )]
    prompts = {'review': REVIEW_INSTRUCTION, 'executor': EXECUTOR_INSTRUCTION, **INSTRUCTIONS}
    return {
        'version': 'E3-draft', 'status': 'offline_preparation_only', 'git_head': '',
        'git_head_note': '源码提交哈希留空，由提交后填写；此草稿不构成真实运行授权。',
        'kernel_version': VERSION, 'split': 'train', 'task_ids': tasks,
        'excluded_development_task_ids': excluded, 'dataset_sha256': hashes,
        'architectures': {n: architectures()[n].model_dump(mode='json') for n in ARMS},
        'run': {**LIMITS, 'tasks': 67, 'runs': 201, 'architectures': list(ARMS),
                'trials_per_task': 1, 'review_calls': 1, 'max_steps': 100,
                'user_model': USER, 'judge_model': REVIEW,
                'schedule': '题号数值升序；第 i 题顺序 ARMS[(i + offset) % 3]；seed=42+i；i 从 0 起。'},
        'rules': RULES,
        'sha256': {str(p.relative_to(ROOT)): sha256(p.read_bytes()).hexdigest() for p in files},
        'prompt_sha256': {n: sha256(p.encode()).hexdigest() for n, p in prompts.items()},
        'prompt_note': '沿用 v2.2 对执行者、审查者、事实专家、政策专家的四段固定指令哈希；动态上下文不进入固定指令哈希。官方客户/裁判实现另存文件哈希，题目和政策由 dataset_sha256 固定。',
        'official_role_source_sha256': {
            str(p.relative_to(SOURCE)): sha256(p.read_bytes()).hexdigest() for p in (
                SOURCE / 'src/tau2/user/user_simulator.py', SOURCE / 'src/tau2/user/guidelines.py',
                SOURCE / 'src/tau2/evaluator/evaluator_nl_assertions.py')},
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', type=Path, default=ROOT / 'evidence/architecture_refactor/preregistration_e3.json')
    args = parser.parse_args()
    data = inventory()
    # Refuse to silently replace a subsequently frozen inventory.
    with args.output.open('x') as stream:
        json.dump(data, stream, indent=2, ensure_ascii=False, allow_nan=False)
        stream.write('\n')
    print(args.output)


if __name__ == '__main__':
    main()
