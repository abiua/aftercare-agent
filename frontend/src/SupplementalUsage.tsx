import { useState } from 'react';
import { Alert, Card, Collapse, Descriptions } from 'antd';
import { segment, type Client } from './api';
import type { ProviderTokenUsage, SupplementalUsageReport } from './contracts';
import { useResource } from './hooks';
import { formatProviderTokens, formatSupplementalCost, supplementalUsageMessage } from './supplementalUsagePresentation';
import { Reload, time } from './ui';

export function SupplementalUsage({api, resultId}: {api: Client; resultId: string}) {
  const [expanded, setExpanded] = useState(false);
  return <Collapse size="small" onChange={(keys) => setExpanded(keys.includes('usage'))} items={[{
    key: 'usage', label: '补充用量审计',
    children: expanded ? <UsageAudit api={api} resultId={resultId} /> : null,
  }]} />;
}

function UsageAudit({api, resultId}: {api: Client; resultId: string}) {
  const resource = useResource<SupplementalUsageReport>(api, `/evaluation/results/${segment(resultId)}/supplemental-usage`);
  const report = resource.value;
  return <div className="stack">
    <div className="section-row">
      <span className="muted">{report && !resource.error
        ? <>获取时间 <time dateTime={report.as_of} title={report.as_of}>{time(report.as_of)}</time></>
        : '核对原快照与后续收到的调用记录'}</span>
      <Reload refresh={resource.refresh} loading={resource.loading} />
    </div>
    <p className="muted">晚到的已知费用只对应所列调用。完整实验总费用需另行核对，原报告继续展示冻结快照。</p>
    {resource.error
      ? <Alert type="warning" showIcon title="补充用量审计证据不可用" />
      : report ? <UsageDifferences report={report} /> : <p className="muted">正在读取补充用量审计。</p>}
  </div>;
}

const attemptStatuses: Record<string, string> = {
  STARTED: '已发起', SUCCEEDED: '已完成', FAILED: '调用失败', UNKNOWN: '结果未知',
};

function UsageRecord({status, usage, cost}: {status: string | null; usage: ProviderTokenUsage | null; cost: string | null}) {
  const tokens = formatProviderTokens(usage);
  return <Descriptions size="small" column={1} items={[
    {key: 'status', label: '调用状态', children: status && Object.hasOwn(attemptStatuses, status) ? attemptStatuses[status] : '未知'},
    {key: 'usage', label: '用量（词元）', children: tokens.join('、') || '未知'},
    {key: 'cost', label: '该次调用估算费用（美元）', children: formatSupplementalCost(cost)},
  ]} />;
}

function UsageDifferences({report}: {report: SupplementalUsageReport}) {
  const {title, detail} = supplementalUsageMessage(report);
  if (report.status === 'UNAVAILABLE') return <Alert type="warning" showIcon title={title} description={detail} />;
  return <>
    <Alert type="info" showIcon title={title} description={detail} />
    {report.attempts.map((attempt) => <Card key={JSON.stringify([attempt.run_id, attempt.cycle, attempt.logical_id, attempt.ordinal])}
      size="small" title={`调用 ${attempt.logical_id} · 第 ${attempt.ordinal} 次 · 轮次 ${attempt.cycle}`}>
      {attempt.finished_at && <p className="muted">调用记录结束时间 {time(attempt.finished_at)}</p>}
      <div className="compare-grid">
        <div><h4>原快照</h4>
          <UsageRecord status={attempt.snapshot_status} usage={attempt.snapshot_usage} cost={attempt.snapshot_estimated_cost_usd} />
        </div>
        <div><h4>补充记录</h4>
          <UsageRecord status={attempt.status} usage={attempt.usage} cost={attempt.estimated_cost_usd} />
        </div>
      </div>
    </Card>)}
  </>;
}
