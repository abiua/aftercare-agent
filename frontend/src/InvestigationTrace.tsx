import { Card } from 'antd';
import { investigationReason } from './investigationMessages';
import type { RunEvent } from './contracts';
import { type claims, investigationTrace } from './investigationTraceData';
import { Evidence, money, time } from './ui';

const routes: Record<string, string> = {PROGRAM: '程序核对', SINGLE: '单助手调查', STAGED: '单助手分阶段调查', MULTI: '多角色调查'};
const tools: Record<string, string> = {read_order_lines: '读取订单商品', read_material_evidence: '读取材料记录',
  read_refund_execution: '读取退款执行记录', search_applicable_policy: '查找适用条款', read_policy_clause: '读取条款原文',
  request_targeted_check: '请求指定复查'};
const roles: Record<string, string> = {strong_single: '单助手', fact: '事实核对', policy: '政策核对', verifier: '交叉核验'};
const states: Record<string, string> = {SUPPORTED: '有支持证据', CONFIRMED: '核验角色认为有支持证据',
  CONTRADICTED: '有反证', UNRESOLVED: '仍待核对'};
const reasons: Record<string, string> = {SCOPED_STANDARD: '事项范围清楚', COMPLEX_EVIDENCE: '证据需要交叉核对',
  FACT_CONFLICT: '事实有分歧', POLICY_CONFLICT: '政策依据有分歧', SCOPE_AMBIGUOUS: '处理范围待澄清',
  CUSTOMER_DISPUTES_RECORDED_DELIVERY: '客户描述与签收记录有分歧',
  MULTIPLE_CLAIMED_LINES: '涉及多个售后商品',
  MULTIPLE_APPLICABLE_POLICY_VERSIONS: '发现多个可能适用的条款版本'};
const named = (values: Record<string, string>, key: string) => Object.hasOwn(values, key) ? values[key] : key || '未记录';
function Refs({title, values}: {title: string; values: string[]}) {
  return values.length > 0 && <p>{title} · {values.join('、')}</p>;
}

function Claims({rows}: {rows: ReturnType<typeof claims>}) {
  return rows.map((claim, index) => <div key={`${claim.id}-${index}`}>
    <p><strong>{claim.role && `${named(roles, claim.role)} · `}{claim.value || '判断内容未记录'}</strong> · {named(states, claim.state)}</p>
    <Refs title="支持记录" values={claim.supporting} />
    <Refs title="反证记录" values={claim.conflicting} />
    <Evidence title="判断编号和依赖"><p>{claim.id} · {claim.subject} · {claim.predicate}</p>
      <Refs title="依赖的判断" values={claim.dependencies} /></Evidence>
  </div>);
}

export function InvestigationTrace({events}: {events: RunEvent[]}) {
  const steps = investigationTrace(events);
  if (!steps.length) return null;
  return <Card size="small" title="调查过程" className="stack">
    <p>这里展示调查使用的记录及角色提出的判断。候选仍须经业务校验和人工审批。</p>
    <p><strong>过程概览：</strong>{steps.some(step => step.kind === 'recheck') ? (steps.some(step => step.kind === 'revision')
      ? '已完成局部修订，具体结果见下方判断变更与核验记录。' : '已读取补查记录，但尚未完成判断修订；请查看运行状态和待处理原因。') :
      '本次记录尚无局部补查，不代表已经发生纠错。'}</p>
    {steps.map((step) => <section key={step.seq}>
      {step.kind === 'route' && <>
        <h4>调查方式 · {named(routes, step.selected || step.proposed)}</h4>
        {step.proposed && step.selected && step.proposed !== step.selected && <p>程序推荐 · {named(routes, step.proposed)}</p>}
        <p>{investigationReason(step.reason) ?? named(reasons, step.reason)}</p>
        <Refs title="复杂情况" values={step.features.map((feature) => named(reasons, feature))} />
        {step.amount !== undefined && <p>记录的可退款上限 · {money(step.amount)}</p>}
        {step.unsettled !== undefined && <p>{step.unsettled ? '存在尚待核对的退款记录' : '本次输入未记录待核对退款'}</p>}
        {step.businessControls && <p>后续办理须经过既有业务校验和审批。</p>}
        <Refs title="路由依据编号" values={step.refs} />
      </>}
      {step.kind === 'phase' && <>
        <h4>{named(roles, step.role)} · 开始核对</h4>
        <Refs title="本阶段接收的判断编号" values={step.upstream} />
        <Evidence title="阶段与可用查询"><p>{step.phase || '阶段未记录'}</p>
          <Refs title="可用查询" values={step.permittedTools} /></Evidence>
      </>}
      {step.kind === 'tool' && <>
        <h4>{named(roles, step.role)} · 已读取记录</h4>
        <p>查询 · {named(tools, step.tool)}{step.origin === 'PROGRAM_REQUIRED_READ' && ' · 程序必查，交给本角色核对'}</p>
        <Refs title="返回的记录编号" values={step.refs} />
        {!!step.evidence.length && <Evidence title="读取版本与来源">
          {step.evidence.map((ref, index) => <p key={index}>{ref.id} · {ref.kind || '来源类型未记录'} · {ref.version || '版本未记录'}</p>)}
        </Evidence>}
      </>}
      {step.kind === 'escalation' && <>
        <h4>调整调查方式</h4>
        <p>{named(routes, step.from)} → {named(routes, step.to)} · {investigationReason(step.reason) ?? named(reasons, step.reason)}</p>
      </>}
      {step.kind === 'recheck' && <>
        <h4>{named(roles, step.role)} · 局部复查</h4>
        {step.executor && <p>记录查询：{named(roles, step.executor)} · 判断修订：{named(roles, step.role)}</p>}
        {step.reason && <p>补查原因 · {step.reason}</p>}
        {step.counterexamples.map((item, index) => <p key={index}>触发核验 · {item.claim} · {item.field} · {named(states, item.verdict)} · 证据 {item.refs.join('、') || '未记录'}</p>)}
        <Refs title="复查目标" values={step.targets} />
        <Refs title="复查启动时标为待重新确认" values={step.invalidated} />
        <p>查询 · {named(tools, step.tool)}</p>
        <Refs title="复查读取的记录编号" values={step.refs} />
      </>}
      {step.kind === 'revision' && <>
        <h4>局部补查后的判断变更</h4>
        {step.reason && <p>{step.reason}</p>}
        {step.retired.map(old => {
          const replacement = step.replacements.find(item => item.id === old.id);
          return <div key={old.id}>
            <p><strong>{named(roles, old.role)} · {old.subject}</strong></p>
            <p>原判断（已撤销）：{old.value || '未记录'}</p>
            <p>补查后：{replacement ? `${replacement.value} · ${named(states, replacement.state)}` : '仍待重新核对，不能继续沿用原判断'}</p>
            <Refs title="补查后的支持记录" values={replacement?.supporting ?? []} />
          </div>;
        })}
        <Refs title="仍未解决的依赖判断" values={step.stale} />
      </>}
      {step.kind === 'unread' && <>
        <h4>已有材料尚未核对</h4>
        <p>{investigationReason(step.reason)}</p>
        <Refs title="待读取的材料与审核记录" values={step.refs} />
      </>}
      {step.kind === 'blocked'  && <>
        <h4>候选未通过业务检查</h4>
        <p>{step.option} · {investigationReason(step.reason) ?? named(reasons, step.reason)}</p>
      </>}
      {step.kind === 'claims' && <>
        <h4>{named(roles, step.role)} · {step.phase === 'recheck' ? '复查阶段提出的判断' : '提出的判断'}</h4>
        <Claims rows={step.claims} />
      </>}
      {(step.kind === 'assessment' || step.kind === 'result') && <>
        <h4>{step.kind === 'result' ? '调查结束时记录的候选' : step.recheckCount ? '复查后的交叉核验' : '交叉核验'}</h4>
        {step.kind === 'result' && <>
          <Refs title="助手提出的候选" values={step.proposed.map((option) => `${option.id} · ${option.action}`)} />
          <Refs title="保留供客服核对" values={step.options} />
          <Refs title="未保留的候选" values={step.proposed.filter((option) => !step.options.includes(option.id)).map((option) => option.id)} />
          <Refs title="程序阻断记录" values={step.checks.map((check) => `${check.id} · ${investigationReason(check.code) ?? named(reasons, check.code)}`)} />
        </>}
        {step.kind === 'assessment' && <>
          {'kept' in step && <Refs title="与复查前一致的判断" values={step.kept} />}
          {'revised' in step && <Refs title="复查后有修改的判断" values={step.revised} />}
          <Refs title="失效且未重新核对的判断" values={step.stale} />
        </>}
        <Claims rows={step.claims} />
        {step.assessments.map((assessment, index) => <div key={`${assessment.claim}-${index}`}>
          <p>交叉核验意见 · {assessment.claim} · {assessment.field} · {named(states, assessment.verdict)}</p>
          <Refs title="核验引用记录" values={assessment.refs} />
        </div>)}
      </>}
      <p className="muted">{time(step.timestamp)}</p>
    </section>)}
  </Card>;
}
