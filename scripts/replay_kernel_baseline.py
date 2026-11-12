"""Free full S0 replay: drive the new official adapter with frozen public outputs."""
import json
from pathlib import Path
from unittest.mock import patch
from refactor_baseline import ROOT, baseline_rows
from tau_official_agents import create_agent
from tau2.domains.retail.environment import get_environment
from tau2.data_model.message import AssistantMessage, ToolCall, ToolMessage, UserMessage


def message(raw):
    if raw['role']=='tool':return ToolMessage(role='tool',id=raw['id'],content=raw['content'],error=raw.get('error',False))
    if raw['role']=='user':return UserMessage(role='user',content=raw['content'])
    return AssistantMessage(role='assistant',content=raw['content'],tool_calls=[ToolCall(**c) for c in raw.get('tool_calls') or []] or None)


def main():
    env=get_environment();counts=[]
    for row,path,trace in baseline_rows():
        if row['architecture']!='strong_single':continue
        agent=create_agent('S0',env.get_tools(),env.get_policy(),'gpt-6.1-sol')
        history=[];steps=0;calls=[]
        for raw in trace['messages']:
            current=message(raw)
            if raw['role']=='assistant' and history:
                state=agent.get_init_state(history[:-1])
                def recorded(**kwargs):
                    expected=[(m.role,m.content) for m in history]
                    actual=[(m.role,m.content) for m in kwargs['messages'] if m.role!='system']
                    assert actual==expected, (row['task_id'],'public transcript mismatch')
                    assert kwargs['aftercare_role']=='executor'
                    return current.model_copy(deep=True)
                with patch('tau_official_agents.generate',recorded):
                    output,_=agent.generate_next_message(history[-1],state)
                assert output.content==current.content
                assert output.tool_calls==current.tool_calls
                steps+=1;calls.extend(c.model_dump() for c in output.tool_calls or [])
            history.append(current)
        counts.append({'task_id':row['task_id'],'steps':steps,'tool_calls':len(calls),'matched':True})
    assert len(counts)==40
    report={'mode':'offline recorded outputs, no model-quality claim','tasks':counts,'steps':sum(r['steps'] for r in counts)}
    target=ROOT/'evidence/architecture_refactor/s0_replay.json'
    target.write_text(json.dumps(report,indent=2)+'\n')
    print(json.dumps({'tasks':len(counts),'steps':report['steps'],'all_matched':True}))

if __name__=='__main__':main()
