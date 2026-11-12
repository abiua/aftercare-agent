"""New user-authorized two-case rerun; preserve the closed P5 acceptance journals."""
import argparse
import fcntl
import json
from decimal import Decimal
from hashlib import sha256
from pathlib import Path

import httpx
from sqlalchemy import text
from tau_p5_runtime import API_PORT, RUNTIME, configure
from verify_chat_live_business_path import _checkpoint
from verify_tau_p5_live import analyze, audit, prepare_revision, transport_timing

ROOT = Path(__file__).resolve().parents[1]
CASES = {
    'O9961': 'tau-p5-live-o9961-structured-recheck-90s-20261112.json',
    'O9962': 'tau-p5-live-o9962-final-verifier-90s-20261112.json',
}


def configure_runner(**kwargs):
    configure(**kwargs)
    from aftercare import config, runtime
    runtime.load_settings = config.load_settings


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--resume-staging', action='store_true')
    args = parser.parse_args()
    configure_runner()
    from aftercare.conversations.decision_delivery import project
    from aftercare.conversations.investigation_delivery import process
    from aftercare.runtime import open_runtime
    journal = RUNTIME / 'plain-language-rerun-20261112.json'
    with (RUNTIME / 'acceptance.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if not args.resume_staging:
            with journal.open('x') as f:
                json.dump({'authorization': 'User requested rerun of prior cases after prompt change',
                       'cases': list(CASES), 'new_call_limit': 24, 'new_usd_limit': '1.40'}, f)
        configure_runner()
        credentials = json.loads((RUNTIME / '.runtime/users-test.json').read_text())
        token = next(r['token'] for r in credentials if r['role'] == 'customer')
        baseline = None
        for order, prior_name in CASES.items():
            output = ROOT / 'reports' / f'tau-p5-plain-language-{order.lower()}-20261112.json'
            existing = json.loads(output.read_text()) if output.exists() else None
            if existing and (not args.resume_staging or existing['stage'] != 'analysis_pending'):
                raise RuntimeError('REPORT_ALREADY_EXISTS')
            report = {'status': 'STAGING', 'request_ids': {}, 'mutations': {},
                      'rerun_reason': 'User requested new investigation with plain-zh-v1',
                      'prior_report_sha256': sha256((ROOT/'reports'/prior_name).read_bytes()).hexdigest()}
            configure_runner()
            with httpx.Client(base_url=f'http://127.0.0.1:{API_PORT}/api/',
                              headers={'Authorization': f'Bearer {token}'}, timeout=15, trust_env=False) as customer:
                if existing:
                    report = existing
                else:
                    prepare_revision(customer, ROOT/'reports'/prior_name, report, output, continuation=True)
            report['acceptance_round'] = 'plain-language-rerun-20261112'
            analyze(report, output)
            configure_runner(live=True, timeout_seconds=90, max_attempts=12)
            with open_runtime('test', dispatch=lambda _: None) as (_settings, engine, business, agent):
                before = audit(engine, report)
                if baseline is None:
                    baseline = (len(before['attempts']), Decimal(before['conservative_booked_usd']))
                assert before['request'] is None
                assert all(r['status'] != 'STARTED' for r in before['attempts'])
                assert len(before['attempts']) - baseline[0] + 12 <= 24
                assert Decimal(before['conservative_booked_usd']) - baseline[1] + agent.model.registry.max_run_usd <= Decimal('1.40')
                assert agent.model.registry.max_run_attempts <= 12
                assert agent.model.registry.max_run_usd <= Decimal('0.70')
                report['budget'] = {'new_calls': 24, 'new_usd': '1.40',
                                    'baseline_calls': baseline[0], 'baseline_booked_usd': str(baseline[1])}
                with engine.connect() as db:
                    report['revises']['snapshot_sha256'] = db.scalar(text('SELECT sha256 FROM chat_decision_snapshots WHERE id=:id'), {'id':report['revises']['snapshot_id']})
                report['model_scope'] = {'language_version':'plain-zh-v1','timeout_seconds':90,
                                          'deadline_seconds':240,'max_attempts':12}
                _checkpoint(report, output, 'live_dispatch_pending')
                try:
                    process(business, agent, report['paid_turn_id'])
                    usage = audit(engine, report)
                    if not usage['request'] or not usage['request']['run_id']:
                        report['status'] = 'BLOCKED_NO_MODEL_RUN'
                    else:
                        run_id = usage['request']['run_id']
                        with transport_timing(report, 12):
                            agent.process(run_id)
                        project(business, run_id)
                        report['status'] = 'LIVE_CAPTURED'
                except Exception as exc:  # noqa: BLE001 - retain audit, never retry unknown calls
                    report['status'] = 'LIVE_REVIEW_REQUIRED'
                    report['failure_type'] = type(exc).__name__
                finally:
                    usage = audit(engine, report)
                    assert usage['previous_snapshot_sha256'] == report['revises']['snapshot_sha256']
                    assert usage['executions'] == before['executions'] == 0
                    _checkpoint(report, output, 'live_readback')
                print(json.dumps({'order':order,'status':report['status'],'run':usage['run'] and usage['run']['reason'],
                                  'new_calls':len(usage['attempts'])-baseline[0]}, ensure_ascii=False), flush=True)


if __name__ == '__main__':
    main()
