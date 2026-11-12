"""Two separate processes resume the production multi-agent graph from SQLite."""
import argparse
import sys
from pathlib import Path

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT/'backend/tests/intelligence'))
from aftercare.investigation.contracts import RouteDecision
from aftercare.investigation.graph import build_investigation_graph
from aftercare.investigation.orchestration import InvestigationContext
from aftercare.investigation.prompt_evidence import PROMPT_VERSION
from aftercare.investigation.tools import InvestigationTools
from langgraph.checkpoint.sqlite import SqliteSaver
from aftercare_runtime_support import write_new
from test_chat_investigation_graph import Audit, Script, _call, _claim, _review, investigation


def main():
    parser=argparse.ArgumentParser();parser.add_argument('phase',choices=['before','after']);parser.add_argument('directory',type=Path);args=parser.parse_args()
    args.directory.mkdir(parents=True,exist_ok=True)
    value=investigation.__wrapped__()
    responses={
        f'p4:{PROMPT_VERSION}:fact:0':[_call('read_order_lines',line_ids=['line'])],
        f'p4:{PROMPT_VERSION}:fact:1':{'claims':[_claim('f','line')]},
        f'p4:{PROMPT_VERSION}:policy:0':[_call('read_policy_clause',policy_id='POL01',version='v1',product_id='product')],
        f'p4:{PROMPT_VERSION}:policy:1':{'claims':[_claim('p','POL01:v1')]},
        f'p4:{PROMPT_VERSION}:verify-0:0':{**_review(('fact/f','line'),('policy/p','POL01:v1')),
            'options':[{'id':'help','action':'HUMAN_HANDOFF','target_line_ids':['line'],'rationale':'请客服核对签收记录和客户描述。','evidence_ids':['line','POL01:v1']}]},
    }
    responses={k:v for k,v in responses.items() if ('verify' in k)==(args.phase=='after')}
    model=Script(responses);audit=Audit()
    ctx=InvestigationContext(value,RouteDecision(path='MULTI',reason='CONTROLLED',financial_risk={},output={}),
        model,audit,InvestigationTools(value),lambda **_:None,'analysis',frozenset({'line'}),frozenset({'line','POL01:v1'}),arm='multi')
    cfg={'configurable':{'thread_id':'persistent-p6'}}
    with SqliteSaver.from_conn_string(str(args.directory/'checkpoint.sqlite')) as saver:
        graph=build_investigation_graph(saver)
        state=graph.invoke({'cycle':0} if args.phase=='before' else None,cfg,context=ctx,
                           interrupt_after=['fact','policy'] if args.phase=='before' else None)
        snapshot=graph.get_state(cfg)
        if args.phase=='before': assert snapshot.next==('verify',) and len(model.calls)==4
        else:
            assert not snapshot.next and len(model.calls)==1
            assert state['output']['investigation_options'][0]['action']=='HUMAN_HANDOFF'
    write_new(args.directory/(args.phase+'.json'),{'calls':model.calls,'events':audit.events,'state':state,'next':snapshot.next})
    print(args.phase,'passed',len(model.calls))


if __name__=='__main__':main()
