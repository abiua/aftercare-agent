"""Deterministic offline selection; never emit scenario text or reference arguments."""
from __future__ import annotations

import argparse
from hashlib import sha256
import json
from pathlib import Path
import subprocess

from aftercare.agent_kernel.experts import CONSTRAINT
from tau_official_summary import read_json, write_json

ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / '.runtime/tau-official-source'
OUTPUT = ROOT / 'evidence/architecture_refactor/kernel_v3_dev_tasks.json'
WRITES = frozenset({
    'cancel_pending_order', 'modify_pending_order_items', 'modify_pending_order_address',
    'modify_pending_order_payment', 'return_delivered_order_items',
    'exchange_delivered_order_items', 'modify_user_address',
})
RULES = {
    'candidates': 'split.train excluding task 44; numeric ascending task ID order',
    'condition': 'CONSTRAINT.finditer(json.dumps(user_scenario, ensure_ascii=False))',
    'A': 'price condition matched and >=2 write actions (duplicates count); first 4',
    'B': 'no price condition matched and >=2 distinct write action names; first 2',
    'write_names': sorted(WRITES),
}


def select(tasks, split, hashes):
    train = split['train']
    if not isinstance(train, list) or len(set(train)) != len(train):
        raise ValueError('Expected unique train task IDs')
    if any(not isinstance(t, str) or not t.isdecimal() for t in train):
        raise ValueError('Expected numeric string task IDs')
    by_id = {task['id']: task for task in tasks}
    if len(by_id) != len(tasks) or not set(train) <= by_id.keys():
        raise ValueError('Duplicate or missing task IDs')
    candidates = sorted(set(train) - {'44'}, key=int)
    groups = {'A': [], 'B': []}
    for task_id in candidates:
        task = by_id[task_id]
        names = [a['name'] for a in task['evaluation_criteria']['actions'] if a['name'] in WRITES]
        matches = [m.group(0) for m in CONSTRAINT.finditer(json.dumps(task['user_scenario'], ensure_ascii=False))]
        group = 'A' if matches and len(names) >= 2 else 'B' if not matches and len(set(names)) >= 2 else None
        if group:
            groups[group].append({'task_id': task_id, 'group': group, 'write_actions': names,
                                  'matched_conditions': matches})
    if len(groups['A']) < 4 or len(groups['B']) < 2:
        raise ValueError('Insufficient candidates for exactly 4 A + 2 B tasks')
    selected = groups['A'][:4] + groups['B'][:2]
    ids = [task['task_id'] for task in selected]
    development = sorted(set(ids) | {'44'}, key=int)
    return {'schema_version': 1, 'task_ids': ids, 'selected': selected, 'rules': RULES,
            'constraint_pattern': CONSTRAINT.pattern, 'dataset_sha256': hashes,
            'candidate_count': len(candidates), 'group_candidate_counts': {k: len(v) for k, v in groups.items()},
            'development_task_ids': development,
            'confirmation_exclusion': 'These six tasks and task 44 are development-only and must never enter confirmation experiments.',
            'remaining_available_train_tasks': len(set(train) - set(development))}


def load_selection(source=SOURCE, manifest=ROOT / 'data/tau/manifest.json'):
    provenance = read_json(manifest)
    revision = subprocess.check_output(['git', '--no-optional-locks', '-C', str(source), 'rev-parse', 'HEAD'], text=True).strip()
    if revision != provenance['revision']:
        raise ValueError('Official checkout revision mismatch')
    if subprocess.check_output(['git', '--no-optional-locks', '-C', str(source), 'status', '--porcelain', '--untracked-files=no'], text=True):
        raise ValueError('Official checkout has tracked changes')
    retail = source / 'data/tau2/domains/retail'
    hashes, data = {}, {}
    # Verify the exact bytes BEFORE parsing either task data file.
    for name in ('tasks.json', 'split_tasks.json'):
        raw = (retail / name).read_bytes()
        hashes[name] = sha256(raw).hexdigest()
        if hashes[name] != provenance['files'][name]['sha256']:
            raise ValueError(f'Official retail hash mismatch: {name}')
        data[name] = raw
    return {**select(json.loads(data['tasks.json']), json.loads(data['split_tasks.json']), hashes),
            'revision': revision}


def main():
    argparse.ArgumentParser(description=__doc__).parse_args()
    report = load_selection()
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    write_json(OUTPUT, report)
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
