import { useCallback, useState } from 'react';
import { Alert, Button, Card, Descriptions, Space, Tabs, Timeline } from 'antd';
import { ArrowLeftOutlined, ClockCircleOutlined } from '@ant-design/icons';
import { segment, type Client } from './api';
import type { BusinessEvent, CaseDetail, RunOutput, Runtime, User } from './contracts';
import { useEventRefresh, useResource, useStream } from './hooks';
import type { SseFrame } from './sse';
import { OrderFacts } from './OrderFacts';
import { CaseMaterials } from './CaseMaterials';
import { CaseFacts } from './CaseFacts';
import { CaseProcessing } from './CaseProcessing';
import { RunPanel } from './RunPanel';
import { Evidence, Feedback, intents, Json, Placeholder, Reload, Status, StreamBadge, time } from './ui';

export function CaseWorkspace({api, user, runtime, id, onBack}: {api: Client; user: User; runtime?: Runtime; id: string; onBack: () => void}) {
  const base = `/cases/${segment(id)}`;
  const resource = useResource<CaseDetail>(api, base);
  const refresh = resource.refresh;
  const eventRefresh = useEventRefresh(refresh);
  const [events, setEvents] = useState<BusinessEvent[]>([]);
  const [tab, setTab] = useState('materials');
  const [draft, setDraft] = useState<{output: RunOutput; version: number}>();
  const listen = useCallback((frame: SseFrame) => {
    const data = JSON.parse(frame.data) as BusinessEvent;
    if (!data.type) return;
    setEvents((current) => [...current.slice(-49), {...data, sequence: data.sequence ?? Number(frame.id)}]);
    eventRefresh();
  }, [eventRefresh]);
  const stream = useStream(api, resource.value ? `${base}/events` : null, listen);
  const detail = resource.value;
  if (!detail) return <div className="stack">
    <div><Button icon={<ArrowLeftOutlined />} onClick={onBack}>返回工单</Button></div>
    <Feedback error={resource.error} />
    <Card loading={resource.loading}><Placeholder text={resource.error ? '当前无法查看该工单' : '正在读取工单'} /></Card>
  </div>;

  const canSeeAgent = user.role !== 'customer';
  const canOperate = !resource.error && !resource.loading;
  return <div className="stack">
    <div className="table-toolbar">
      <Button icon={<ArrowLeftOutlined />} onClick={onBack}>返回工单</Button>
      <Space wrap><StreamBadge state={stream} /><Reload refresh={refresh} loading={resource.loading} /></Space>
    </div>
    <Feedback error={resource.error} />
    <section className="hero case-hero">
      <div><div className="eyebrow">工单 {detail.case.ticket_id}</div><h1>{intents[detail.case.intent] ?? detail.case.intent}</h1><p>{detail.case.description}</p></div>
      <div><Status value={detail.case.status} /></div>
    </section>
    <div className="detail-layout">
      <Card className="workspace-card" title="处理工单">
        <Tabs activeKey={tab} onChange={setTab} items={[
          {key: 'materials', label: '材料与审核', children: <CaseMaterials api={api} detail={detail} user={user} base={base} refresh={refresh} canOperate={canOperate} />},
          ...(canSeeAgent ? [{key: 'agent', label: '助手协作', children: <RunPanel api={api} detail={detail} runtime={runtime} base={base} user={user} refreshCase={refresh} canOperate={canOperate} onDraft={(output) => {setDraft({output, version: detail.case.version}); setTab('processing');}} />}] : []),
          {key: 'processing', label: '方案与回执', children: <CaseProcessing api={api} detail={detail} user={user} base={base} refresh={refresh} canOperate={canOperate} readFailed={!!resource.error} draft={draft?.version === detail.case.version ? draft.output : undefined} />},
        ]} />
      </Card>
      <div className="stack">
        <Card title="订单信息"><OrderFacts order={detail.order} /></Card>
        <Card title="工单摘要">
          <Descriptions size="small" column={1} items={[
            {key: 'submitted', label: '提交时间', children: time(detail.case.created_at)},
            {key: 'conditions', label: '处理条件', children: <Status value={detail.eligibility.status} />},
          ]} />
          {detail.eligibility.reason && <p className="muted">{detail.eligibility.reason}</p>}
          {!!detail.eligibility.reasons?.length && <ul>{detail.eligibility.reasons.map((reason) => <li key={reason}>{reason}</li>)}</ul>}
          {user.role !== 'customer' && <Evidence title="适用政策与事实版本"><Json value={{policies: detail.policies, versions: detail.versions}} /></Evidence>}
        </Card>
        <CaseFacts api={api} detail={detail} user={user} base={base} refresh={refresh} canOperate={canOperate} />
        {user.role !== 'customer' && <Card title="工单更新">
          {!events.length && <p className="muted">连接后会加载工单更新。</p>}
          {!!events.length && <Timeline items={events.slice(-6).reverse().map((event) => ({
            icon: <ClockCircleOutlined />,
            children: <div><span>{businessEventName(event.type)}</span><div className="muted">{time(event.created_at)}</div><Evidence><Json value={event.payload} /></Evidence></div>,
          }))} />}
        </Card>}
      </div>
    </div>
    {!canOperate && resource.error && <Alert type="warning" showIcon title="请刷新并确认当前工单状态后再办理。" />}
  </div>;
}

const eventLabels: Record<string, string> = {
  CASE_CREATED: '已提交工单', MATERIAL_UPLOADED: '已上传材料', MATERIALS_REVIEWED: '已记录人工审核', FACTS_VERIFIED: '已核对业务事实',
  PROPOSAL_CREATED: '已提交处理方案', PROPOSAL_DECIDED: '已记录审批', EXECUTION_RECORDED: '已提交模拟执行',
  EXECUTION_UPDATED: '执行结果已更新', EXECUTION_RECONCILED: '已核对执行结果',
};
function businessEventName(type: string) { return eventLabels[type.toUpperCase()] ?? '工单记录已更新'; }
