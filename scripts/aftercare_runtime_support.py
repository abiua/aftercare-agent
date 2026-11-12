"""Shared demo model configuration and exclusive evidence writes.

These helpers do not load evaluation cases or define success criteria.
"""
from decimal import Decimal
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def write_new(path, value):
    from aftercare.evaluation.chat.manifest import Manifest
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    Manifest(path.parent)._write(path.name, value)


def registry():
    from aftercare.intelligence.models import Registry
    base = Registry.read(ROOT / '.runtime/models.json').model_dump(mode='json')
    for model in base['models']:
        model.update(data_scopes=['customer_conversation'], output_tokens=4096,
                     fallback_aliases=['analysis'] if model['alias'] == 'economy' else [])
    base.update(version='p6-p9-task-routing-v1', native_streaming=False,
                timeout_seconds=90, deadline_seconds=240, max_attempts=12,
                max_run_attempts=12, max_usd=str(Decimal('0.70')),
                max_run_usd=str(Decimal('0.70')))
    return Registry.model_validate(base)
