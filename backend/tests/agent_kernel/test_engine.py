from types import SimpleNamespace
import pytest
from aftercare.agent_kernel.contracts import StepProposal, GuardResult, architectures
from aftercare.agent_kernel.engine import ExecutionKernel, GuardRejected, KernelNotReady


def test_hard_guard_only_one_retry_no_bad_action_escapes():
    calls=[]
    model=SimpleNamespace(generate=lambda **kw: calls.append(kw) or StepProposal(text='bad'))
    guard=SimpleNamespace(check=lambda *args:GuardResult(allowed=False,feedback='Must authenticate'))
    kernel=ExecutionKernel(architectures()['S1'],model,guard=guard)
    with pytest.raises(GuardRejected): kernel.step([],(),'policy')
    assert len(calls)==2 and len(kernel.trace)==4
    assert 'Must authenticate' in calls[1]['messages'][-1]['content']
    calls.clear()
    with pytest.raises(GuardRejected): kernel.step([],(),'policy')
    assert len(calls[0]['messages'])==1


def test_unwired_layers_fail_before_model_call():
    model=SimpleNamespace(generate=lambda **kw:pytest.fail('must not call'))
    with pytest.raises(KernelNotReady): ExecutionKernel(architectures()['M1'],model).step([],(),'')


def test_customer_output_is_plain_and_trace_excludes_private_context():
    model=SimpleNamespace(generate=lambda **kw:StepProposal(text='目前没查到退款记录。',private_context=({'secret_reasoning':'x'},)))
    kernel=ExecutionKernel(architectures()['S0'],model)
    reply=kernel.step([],(),'policy')
    assert reply.text=='目前没查到退款记录。'
    assert 'private_context' not in kernel.trace[0].output
