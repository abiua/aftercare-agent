import { Alert, Card, Descriptions, Space, Tag } from 'antd';
import type { EvalResult, Experiment, ReleasePolicy } from './contracts';
import { describeThresholds, summarizeEvaluation } from './evaluationPresentation';
import { evaluationReason } from './evaluationMessages';
import { reviewRecord } from './reviewReading';
import { Evidence, Json, Status } from './ui';

export function ResultSummary({result}: {result: EvalResult}) {
  const value = summarizeEvaluation(result);
  const highlighted = value.keyChecks.slice(0, 4);
  const reason = evaluationReason(result.failure_reason);
  const actual = reviewRecord(result.actual);
  const details = actual ? Object.fromEntries(Object.entries(actual).filter(([key]) => key !== 'final_business')) : result.actual;
  return <>
    {reason && <Alert type={result.status === 'NEEDS_REVIEW' ? 'info' : 'warning'} showIcon title={reason} />}
    <div className="compare-grid">
      <Card size="small" title="预期行为">
        <div className="result-summary">
          {value.expectedPath.length ? <ul>{value.expectedPath.map((step) => <li key={step}>{step}</li>)}</ul> : <p className="muted">暂无可摘要的预期步骤。</p>}
          {!!value.expectedMaterials.length && <p>应核对材料 {value.expectedMaterials.join('、')}</p>}
          {!!value.expectedActions.length && <p>允许拟案 {value.expectedActions.join('、')}</p>}
          {value.expectedAmount && <p>参考退款金额 {value.expectedAmount}</p>}
          {!!value.referencePolicies.length && <p className="muted">参考条款 {value.referencePolicies.join('、')}</p>}
        </div>
      </Card>
      <Card size="small" title="实际动作">
        <div className="result-summary">
          {value.actualPath.length ? <ul>{value.actualPath.map((step) => <li key={step}>{step}</li>)}</ul> : <p className="muted">暂无可摘要的执行动作。</p>}
          <p className="muted">{value.source}</p>
        </div>
      </Card>
      <Card size="small" title="关键检查">
        {value.checkCount ? <div className="result-summary">
          <Space wrap><Tag color="green">通过 {value.passed}</Tag><Tag color="red">失败 {value.failed}</Tag><Tag color="gold">未知 {value.unknown}</Tag></Space>
          {!!highlighted.length && <ul>{highlighted.map((check, index) => <li key={`${check.event}-${index}`}>
            <Status value={check.status} />{check.label}
          </li>)}</ul>}
          {value.keyChecks.length > highlighted.length && <p className="muted">其余检查见下方原始证据。</p>}
        </div> : <p className="muted">暂无检查记录。</p>}
      </Card>
      <Card size="small" title="模型调用与费用">
        <Descriptions size="small" column={1} items={[
          {key: 'calls', label: '已记录调用', children: value.calls === undefined ? '未知' : `${value.calls} 次`},
          {key: 'cost', label: '估算费用', children: value.estimate === undefined ? '未知' : `$${value.estimate.toLocaleString('en-US', {minimumFractionDigits: 2, maximumFractionDigits: 8})}`},
          {key: 'models', label: '模型', children: value.models.join('、') || '—'},
        ]} />
        {!value.auditComplete && <p className="muted">调用记录仍待核对。</p>}
      </Card>
    </div>
    <Card size="small" title="实际引用条款">
      {value.citations.length ? <>
        <Space wrap>{value.citations.map((item) => <Tag key={`${item.id}:${item.version}`}>{item.title}{item.version ? ` · ${item.version}` : ''}</Tag>)}</Space>
        <Evidence title="展开引用原文"><ul>{value.citations.map((item) => <li key={`${item.id}:${item.version}`}><strong>{item.title}</strong><p>{item.quote}</p></li>)}</ul></Evidence>
      </> : <p className="muted">暂无可核验的引用记录。</p>}
    </Card>
    <Evidence title="展开原始证据">
      <div className="stack">
        {result.failure_reason && <p>原始状态码 <code>{result.failure_reason}</code></p>}
        <div><h3>预期记录</h3><Json value={result.expected} /></div>
        <div><h3>实际记录</h3><Json value={details} /></div>
        <div><h3>逐项评分</h3><Json value={result.checks} /></div>
        <div><h3>脱敏工具与证据轨迹</h3><Json value={result.trace} /></div>
        {result.retry_of_result_id && <div><h3>关联原案例结果</h3><Json value={result.retry_of_result_id} /></div>}
      </div>
    </Evidence>
  </>;
}

const requiredChecks: Record<string, string> = {
  coverage: '执行覆盖', task_success: '任务成功率', deterministic: '确定性规则检查',
  human_gold: '独立人工核验', live: '真实模型证据', cost: '估算费用',
  cost_per_success: '每个成功案例的估算费用', final_violation_rate: '最终违规率',
};

export function GateCriteria({policy}: {policy: ReleasePolicy}) {
  const thresholds = describeThresholds(policy.thresholds);
  return <div className="result-summary">
    <p><strong>{policy.name}</strong>{policy.version ? ` · 版本 ${policy.version}` : ''}</p>
    {!!thresholds.length && <Descriptions size="small" column={{xs: 1, sm: 2}}
      items={thresholds.map((item) => ({key: item.label, label: item.label, children: item.value}))} />}
    {!!policy.required_checks?.length && <p>必核项目 {policy.required_checks.map((check) => requiredChecks[check] ?? (/[\u4e00-\u9fff]/u.test(check) ? check : '其他规则')).join('、')}</p>}
    <Evidence title="查看阈值原始配置"><Json value={policy} /></Evidence>
  </div>;
}

export function ReleaseGate({experiment, catalogue}: {experiment: Experiment; catalogue: ReleasePolicy[]}) {
  const gate = experiment.gate;
  const policyId = gate?.policy_id ?? experiment.config.gate_policy_id;
  const current = catalogue.find((policy) => policy.id === policyId);
  return <Card size="small" title="发布门槛">
    <div className="stack">
      <Space wrap><span>技术检查</span><Status value={gate?.status} /><span>发布审批</span>
        <Status value={gate?.release_status ?? (gate?.status === 'FAILED' ? 'BLOCKED' : undefined)} /></Space>
      <p className="muted">发布需由独立人员审核并记录决定。</p>
      {gate?.policy ? <GateCriteria policy={gate.policy} /> : current ? <>
        <p className="muted">以下为当前目录中的阈值配置。</p><GateCriteria policy={current} />
      </> : <p className="muted">{policyId ? '这次使用的阈值详情暂不可读取。' : '这次实验暂无已选的发布阈值。'}</p>}
      {gate?.checks?.length ? <ul className="gate-list">{gate.checks.map((check, index) => <li key={index}>
        <Status value={check.status} /><strong>{check.name}</strong><span>{check.reason}</span>
      </li>)}</ul> : <p className="muted">暂无完整门槛记录。</p>}
    </div>
  </Card>;
}
