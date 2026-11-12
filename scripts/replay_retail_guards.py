"""Audit policy guard findings against every frozen public step; no provider calls."""
import json
from collections import Counter
from refactor_baseline import ROOT, baseline_rows
from aftercare.agent_kernel.contracts import StepProposal, ToolCall, ToolDefinition, ToolKind
from aftercare.agent_kernel.retail_guards import RetailGuards, AUTH
from refactor_baseline import WRITES, TRANSFER


def main():
    findings=[];counts=Counter();total=0
    for row,path,trace in baseline_rows():
        history=[]
        for i,m in enumerate(trace['messages']):
            if m['role']=='assistant' and history:
                total+=1
                calls=tuple(ToolCall(id=c['id'],name=c['name'],arguments=c['arguments']) for c in m.get('tool_calls') or [])
                defs=tuple(ToolDefinition(name=c.name,kind=ToolKind.AUTH if c.name in AUTH else ToolKind.WRITE if c.name in WRITES else ToolKind.HANDOFF if c.name==TRANSFER else ToolKind.READ) for c in calls)
                p=StepProposal(text=m.get('content') or '',calls=calls)
                guard=RetailGuards();r=guard.check(history,guard.normalize(p),defs)
                if not r.allowed:
                    counts[r.rule_ids[0]]+=1
                    findings.append({'task_id':row['task_id'],'architecture':row['architecture'],'official_success':row['official_reward']==1,
                        'source':str(path.relative_to(ROOT)),'message_index':i,'rule':r.rule_ids[0],'feedback':r.feedback,'proposal':p.model_dump(mode='json')})
            # Preserve public error fields and original call IDs.
            history.append(m)
    explanations={
        'authentication_reply':'旧回复未同时列出邮箱和姓名加邮编两条路线；这是新计划的严格交互要求，并非官方判定违规。保留规则，不把这次拦截说成已提升成功率。',
        'single_user':'此前没有读取用户资料建立订单清单；旧流程直接使用客户提供的订单号。需增加一次资料读取以证明归属，不否认旧流程最终成功。',
        'payment_owner':'此前没有用户支付方式清单；原订单支付记录可支持原方式，但当前规则要求已读取用户资料。与归属拦截同属缺资料读取，不是已证实用了外人账户。',
        'authentication':'未完成认证就转人工；按本计划新状态机应先提供两种认证方式。客户仍不能认证时不得绕过身份验证。',
    }
    for finding in findings: finding['explanation']=explanations[finding['rule']]
    report={'steps':total,'by_rule':dict(counts),'findings':findings,'success_findings':sum(f['official_success'] for f in findings)}
    (ROOT/'evidence/architecture_refactor/guard_replay.json').write_text(json.dumps(report,indent=2,ensure_ascii=False)+'\n')
    print(json.dumps({k:v for k,v in report.items() if k!='findings'}))

if __name__=='__main__':main()
