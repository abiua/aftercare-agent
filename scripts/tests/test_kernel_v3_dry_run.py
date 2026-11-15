"""Real CLI parsing/official input loading; no model calls or campaign writes."""
from hashlib import sha256
import json
import subprocess
import sys

import pytest

import run_tau_official_campaign as campaign
from tau_official_agents import create_agent
from tau_official_summary import ARCHITECTURES


@pytest.mark.parametrize('names', [None, 'S1,S2,S1c,M1,M2', 'M1x,M1r,M2x,M2r'])
def test_dry_run_accepts_names_without_changing_default_or_frozen_hashes(names, tmp_path):
    frozen_path = campaign.ROOT / '.runtime/kernel-e2-20261113/manifest.json'
    frozen_hash = sha256(frozen_path.read_bytes()).hexdigest()
    adapter_path = campaign.ROOT / 'scripts/tau_official_agents.py'
    adapter_hash = sha256(adapter_path.read_bytes()).hexdigest()
    argv = [str(campaign.ROOT/'scripts/run_tau_official_campaign.py'), 'run', '--scope', 'smoke', '--directory', str(tmp_path/'dry'),
            '--max-usd', '1', '--max-calls', '1']
    if names:
        argv += ['--architectures', names]
    # Official initialization mutates a process-global registry; each CLI gets its own process.
    code = '''import runpy, socket, sys
def forbidden(*args, **kwargs):
    raise AssertionError('No network permitted in dry run')
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
sys.argv = sys.argv[1:]
runpy.run_path(sys.argv[0], run_name='__main__')
'''
    process = subprocess.run([sys.executable, '-B', '-c', code, *argv], check=True, capture_output=True, text=True)
    manifest = json.loads(process.stdout)
    expected = names.split(',') if names else list(ARCHITECTURES)
    assert manifest['architectures'] == expected
    assert [r['architecture'] for r in manifest['schedule']] == expected
    assert all(r['seed'] == 42 for r in manifest['schedule'])
    assert not (tmp_path/'dry').exists()
    assert sha256(frozen_path.read_bytes()).hexdigest() == frozen_hash
    assert manifest['agent_adapter_sha256'] == adapter_hash == sha256(adapter_path.read_bytes()).hexdigest()


def test_new_architectures_construct_through_official_adapter():
    campaign.load_official_source()
    from tau2.domains.retail.environment import get_environment
    environment = get_environment()
    for name in ('M1x','M1r','M2x','M2r'):
        agent = create_agent(name, environment.get_tools(), environment.get_policy(), 'unused')
        assert agent.kernel.architecture.name == name
