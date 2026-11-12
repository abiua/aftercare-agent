import { Alert, Card, Space, Tag } from 'antd';
import type { CaseDetail } from './contracts';
import { decisionActions, isExecutionOption, orderLineName } from './chatDecisionData';
import { decisionCheck } from './decisionLanguage';
import { Evidence, Json, money, time } from './ui';

export function AgentAdvice({detail}: {detail: CaseDetail}) {
  const advice = detail.agent_advice;
  const issues = advice?.candidate.unresolved ?? [];
  if (!advice) return <Alert type="info" showIcon title="助手正在等待资料或调查结果" description="请查看材料和调查进度。" />;
  return <Card size="small" title="助手建议" extra={<Tag>第 {advice.version} 版</Tag>}>
    <p className="muted">{time(advice.created_at)} 更新</p>
    {advice.candidate.options.map(option => <section key={option.id} className="agent-advice-option">
      <Space wrap><h3>{decisionActions[option.action]}</h3>
        {isExecutionOption(option) && <strong>{option.action === 'REFUND' ? money(option.lines.reduce((sum, line) => sum + (line.amount_cents ?? 0), 0)) : `${option.lines.reduce((sum, line) => sum + (line.quantity ?? 0), 0)} 件`}</strong>}
        <Tag>{decisionCheck(advice.checks[option.id])}</Tag>
      </Space>
      <p>{isExecutionOption(option) ? option.lines.map(line => orderLineName(detail.order, line.line_id)).join('、') : option.target_line_ids.map(id => orderLineName(detail.order, id)).join('、')}</p>
      <p><strong>建议理由：</strong>{option.rationale}</p>
      {isExecutionOption(option) && <Evidence title="查看店铺规则">{option.policy_ids.map(id => {
        const policy = advice.policies.find(p => p.id === id || `${p.id}:${p.version}` === id);
        return <p key={id}><strong>依据：{policy?.title ?? '店铺规则'}</strong>{policy?.text && <> — {policy.text}</>}</p>;
      })}</Evidence>}
      {!isExecutionOption(option) && <ul>{[...option.missing_evidence, ...option.alternatives].map((item, i) => <li key={i}>{item}</li>)}</ul>}
    </section>)}
    {!!issues.length && <Evidence title={`需要客服核对（${issues.length}项）`}>
      <ul>{issues.map(item => <li key={item}>{item}</li>)}</ul>
    </Evidence>}
    <Evidence title="技术记录：助手原文与政策版本"><Json value={advice} /></Evidence>
  </Card>;
}
