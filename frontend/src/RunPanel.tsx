import { useCallback, useEffect, useState } from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, Modal, Select, Space, Tag, Timeline } from 'antd';
import { ApartmentOutlined } from '@ant-design/icons';
import { segment, type Client } from './api';
import type { CaseDetail, Mode, Run, RunEvent, RunOutput, Runtime, Strategy, User } from './contracts';
import { investigationReason } from './investigationMessages';
import { recoveryReason } from './evaluationMessages';
import { useActions, useEventRefresh, useResource, useStream } from './hooks';
import type { SseFrame } from './sse';
import { InvestigationTrace } from './InvestigationTrace';
import { isInvestigationEvent } from './investigationTraceData';
import { RefundLookupOutcome } from './RefundLookupPanel';
import { readRefundLookup } from './refundLookup';
import { describeRunCitations } from './runCitations';
import { mergeRunEvents, provisionalDrafts } from './runDrafts';
import { Evidence, Feedback, Json, label, money, Placeholder, Reload, Status, StreamBadge, time } from './ui';

type Props = {
  api: Client; detail: CaseDetail; runtime?: Runtime; base: string; user: User;
  refreshCase: () => void; canOperate: boolean; onDraft: (output: RunOutput) => void;
};
type RunConfig = {mode: Mode; strategy: Strategy; message?: string};
type RunCommand = {type: 'start' | 'resume' | 'cancel'; version: number};
const modes: Mode[] = ['multi', 'single'];
const strategies: Strategy[] = ['routed', 'economy', 'analysis'];
const activeStates = ['QUEUED', 'RUNNING', 'WAITING_INPUT'];
const runningStates = ['QUEUED', 'RUNNING'];

export function RunPanel({api, detail, runtime, base, user, refreshCase, canOperate, onDraft}: Props) {
  const route = `${base}/runs`;
  const runs = useResource<Run[]>(api, route);
  const records = runs.value ?? [];
  const [selected, setSelected] = useState<string>();
  const [command, setCommand] = useState<RunCommand>();
  const [notice, setNotice] = useState('');
  const current = records.find((run) => run.id === selected) ?? records[0];
  const canRun = user.role === 'support' || user.role === 'tester';
  const refreshRuns = runs.refresh;
  return <div className="stack">
    <div className="section-row">
      <div><h2>助手协作</h2><p className="muted">查看任务协调、政策查证与方案核验的过程。</p></div>
      <Space wrap>
        <Reload refresh={refreshRuns} loading={runs.loading} />
        {canRun && <Button type="primary" icon={<ApartmentOutlined />} disabled={!canOperate || !runtime || !!runs.error || records.some((run) => activeStates.includes(run.status))}
          onClick={() => setCommand({type: 'start', version: detail.case.version})}>启动助手</Button>}
      </Space>
    </div>
    <Feedback error={runs.error} notice={notice} />
    {current ? <>
      <Select aria-label="选择运行记录" className="full" value={current.id} onChange={setSelected} options={records.map((run) => ({
        value: run.id, label: `${time(run.created_at)} · ${label(run.mode)} · ${label(run.status)}`,
      }))} />
      <RunDetails key={current.id} api={api} route={route} initialRun={current} caseDetail={detail} canRun={canRun && canOperate}
        showInvestigation={canRun} allowDraft={user.role === 'support'} refreshRuns={refreshRuns} refreshCase={refreshCase}
        onDraft={onDraft} onCommand={(type) => setCommand({type, version: detail.case.version})} />
    </> : <Placeholder text={runs.loading ? '正在读取运行记录' : '暂无助手运行记录'} />}
    {command && <RunEditor api={api} route={route} runId={current?.id} command={command} runtime={runtime}
      currentVersion={detail.case.version} close={() => setCommand(undefined)} refresh={() => {refreshRuns(); refreshCase();}}
      done={(result, message) => {setSelected(result.id); setNotice(message); setCommand(undefined);}} />}
  </div>;
}

function RunDetails({api, route, initialRun, caseDetail, canRun, allowDraft, showInvestigation, refreshRuns, refreshCase, onCommand, onDraft}: {
  api: Client; route: string; initialRun: Run; caseDetail: CaseDetail; canRun: boolean; allowDraft: boolean; showInvestigation: boolean;
  refreshRuns: () => void; refreshCase: () => void; onCommand: (type: 'resume' | 'cancel') => void; onDraft: (output: RunOutput) => void;
}) {
  const path = `${route}/${segment(initialRun.id)}`;
  const runResource = useResource<Run>(api, path);
  const history = useResource<RunEvent[]>(api, `${path}/events?after=0`);
  const [incoming, setIncoming] = useState<RunEvent[]>([]);
  const refreshRun = runResource.refresh;
  const eventRefresh = useEventRefresh(refreshRun);
  const eventRefreshCase = useEventRefresh(refreshCase);
  const eventRefreshRuns = useEventRefresh(refreshRuns);
  const listen = useCallback((frame: SseFrame) => {
    if (frame.event === 'idle') {
      eventRefresh();
      eventRefreshRuns();
      eventRefreshCase();
      return;
    }
    const event = JSON.parse(frame.data) as RunEvent;
    if (!event.kind) return;
    setIncoming((current) => [...current, {...event, seq: event.seq ?? Number(frame.id)}]);
    if (event.kind === 'model_draft_delta') return;
    eventRefresh();
    eventRefreshCase();
    eventRefreshRuns();
  }, [eventRefresh, eventRefreshCase, eventRefreshRuns]);

  const snapshot = runResource.value;
  const run = snapshot && snapshot.updated_at >= initialRun.updated_at ? snapshot : initialRun;
  const active = activeStates.includes(run.status);
  const stream = useStream(api, runningStates.includes(run.status) ? `${path}/stream` : null, listen);
  const refreshHistory = history.refresh;
  const loading = runResource.loading || history.loading;

  useEffect(() => {
    if (!runningStates.includes(run.status) || stream === 'connected') return;
    const timer = setInterval(() => {refreshRun(); refreshHistory(); refreshRuns(); refreshCase();}, 5000);
    return () => clearInterval(timer);
  }, [run.status, stream, refreshRun, refreshHistory, refreshRuns, refreshCase]);

  if (runResource.error) return <Feedback error={runResource.error} />;
  const events = mergeRunEvents(history.value ?? [], incoming);
  const timeline = events.filter((event) => !['model_draft_delta', 'decision_input', 'decision_ready'].includes(event.kind) &&
    !isInvestigationEvent(event));
  const drafts = run.status === 'RUNNING' && (stream === 'connected' || stream === 'reconnecting') ? provisionalDrafts(events) : [];
  const output = run.output;
  const lookup = readRefundLookup(output?.refund_lookup);
  const reviewing = run.reason === 'MATERIAL_REVIEW_REQUIRED';
  const recovery = recoveryReason(run.reason);
  const explanation = investigationReason(run.reason) ?? investigationReason(output?.reason);
  return <>
    <Card size="small" className="run-card">
      <div className="section-row">
        <Space wrap><Status value={run.status} /><StreamBadge state={stream} /></Space>
        <Space wrap>
          <Reload refresh={() => {refreshRun(); refreshHistory(); refreshRuns();}} loading={loading} />
          {canRun && run.status === 'WAITING_INPUT' && <Button type="primary" disabled={caseDetail.case.version <= run.case_version} onClick={() => onCommand('resume')}>补充后继续</Button>}
          {canRun && active && <Button danger onClick={() => onCommand('cancel')}>取消运行</Button>}
        </Space>
      </div>
      <Descriptions size="small" column={{xs: 1, sm: 2}} items={[
        {key: 'mode', label: '运行方式', children: label(run.mode)},
        {key: 'strategy', label: '模型策略', children: label(run.strategy)},
        {key: 'created', label: '开始', children: time(run.created_at)},
        {key: 'updated', label: '最近更新', children: time(run.updated_at)},
      ]} />
      {run.status === 'WAITING_INPUT' && <Alert type="warning" showIcon title={reviewing ? '助手已暂停，等待人工审核' : '助手已暂停，等待补充材料'}
        description={reviewing ? '请在「材料与审核」记录人工意见，再返回此处继续同一运行。' : output?.missing_materials?.length ? output.missing_materials.map(label).join('、') : '请查看「材料与审核」中的补充要求，再返回此处继续同一运行。'} />}
      {(recovery || explanation) && <Alert type="warning" showIcon title={recovery ?? explanation} />}
      {run.reason && !reviewing && run.status !== 'WAITING_INPUT' && <Evidence title="运行结果说明"><Json value={run.reason} /></Evidence>}
      <Evidence title="运行与恢复凭据"><Json value={{run_id: run.id, thread_id: run.thread_id, case_version: run.case_version}} /></Evidence>
    </Card>
    {!!drafts.length && <Card size="small" title="助手正在生成草稿">
      <div className="stack">
        <p className="muted">模型生成，未核验。此处为临时预览。</p>
        {drafts.map((draft) => <div key={draft.key}>
          {draft.alias && <div className="muted">{label(draft.alias)}</div>}
          <pre className="json">{draft.text}{draft.truncated ? '…' : ''}</pre>
        </div>)}
      </div>
    </Card>}
    {output && <Card size="small" title={output.action ? '助手方案草案' : '助手处理意见'}>
      <div className="stack">
        {output.action && <div><Status value={output.action} /> <span>草案待客服核对</span></div>}
        {output.notes ? <p>{investigationReason(output.notes) ?? output.notes}</p> : output.reason && !explanation && <p>{investigationReason(output.reason) ?? output.reason}</p>}
        {lookup && <RefundLookupOutcome result={lookup} />}
        {output.historical_refunded_cents != null && <div>订单历史累计登记退款 {money(output.historical_refunded_cents)}</div>}
        {!!output.policy_ids?.length && <p className="muted">{output.action ? '草案参考条款编号' : '候选条款编号'} {output.policy_ids.join('、')}</p>}
        {allowDraft && output.action && (run.case_version === caseDetail.case.version ? (
          <div><Button onClick={() => onDraft(output)}>带入方案编辑</Button></div>
        ) : <Alert type="warning" showIcon title="草案使用了较早的工单版本，请按最新事实核对。" />)}
        {!!output.citations?.length && <RunCitations output={output} />}
        <Evidence title="草案与引用明细"><Json value={output} /></Evidence>
      </div>
    </Card>}
    {showInvestigation && <InvestigationTrace events={events} />}
    <div className="section-row"><h3>协作轨迹</h3><span className="muted">{timeline.length} 条事件</span></div>
    <Feedback error={history.error} />
    {timeline.length ? <Timeline items={timeline.map((event) => ({
      children: <div>
        <strong>{eventName(event)}</strong><div className="muted">{time(event.created_at)}</div>
        {event.kind === 'agent' && typeof event.payload.notes === 'string'
          ? <Evidence title={`事件 #${event.seq} · 角色笔记（未核验）`}>
            <p className="muted">模型生成的内容尚未核验，请以业务记录和公开处理意见为准。</p><Json value={event.payload} />
          </Evidence>
          : <Evidence title={`事件 #${event.seq} · 证据`}><Json value={event.payload} /></Evidence>}
      </div>,
    }))} /> : <Placeholder text="暂无运行事件" />}
  </>;
}

const citationUses = {
  REUSE_EXISTING_REFUND: '核对同一退款请求的既有记录',
  RECONCILE_UNKNOWN_REFUND: '核对结果未知的退款执行',
};
const citationScopes = {
  ORDER_REFUND_EXECUTION_LEDGER: '订单退款执行账本',
  CURRENT_CASE_EXECUTION_LEDGER: '当前工单执行账本',
  MATCHED_REFUND_EXECUTION: '本次指定的退款执行记录',
};

function RunCitations({output}: {output: RunOutput}) {
  const {supported, background} = describeRunCitations(output);
  const groups = [
    {title: '前提有记录支持的引文', entries: supported},
    {title: '参考背景', entries: background},
  ];
  return <div className="stack">
    <p className="muted">条件状态来自本次运行可读取的业务记录。人工复核结论另行记录。</p>
    {groups.filter((group) => group.entries.length).map((group) => <div key={group.title}>
      <h4>{group.title}</h4>
      <ul>{group.entries.map(({citation, assessment, status, label}, index) => <li key={index}>
        <Space wrap><strong>{citation.id} · 版本 {citation.version}</strong><Tag color={status === 'MET' ? 'blue' : undefined}>{label}</Tag></Space>
        {assessment?.use && <p className="muted">核对用途 {citationUses[assessment.use]}
          {assessment.scope.kind && <> · 范围 {citationScopes[assessment.scope.kind]}</>}</p>}
        <Evidence title="查看匹配原文与核对记录">
          <p>原文已匹配</p>
          <blockquote>{citation.quote}</blockquote>
          {assessment && <Descriptions size="small" column={1} items={[
            {key: 'reason', label: '核对原因码', children: assessment.reason_code},
            {key: 'registry', label: '条件表版本', children: assessment.registry_version},
            ...(assessment.scope.execution_ids.length ? [{key: 'executions', label: '涉及的执行记录编号', children: assessment.scope.execution_ids.join('、')}] : []),
          ]} />}
        </Evidence>
      </li>)}</ul>
    </div>)}
  </div>;
}

const agentNames: Record<string, string> = {
  coordinator: '任务协调', supervisor: '任务协调', policy: '政策查证', policy_evidence: '政策查证', evidence: '政策查证',
  verifier: '方案核验', verification: '方案核验', single: '单 Agent', single_agent: '单 Agent',
};
function eventName(event: RunEvent) {
  const names: Record<string, string> = {
    agent: '助手完成步骤', tool: '查询业务证据', route: '选择模型策略', attempt: '模型调用记录',
    status: '更新运行状态', model_draft_committed: '草稿已进入核验', model_draft_discarded: '临时草稿已撤下', retrieval: '检索政策证据', conflict: '检查证据冲突', interrupt: '暂停并等待补充', resume: '继续原运行',
  };
  const value = event.payload.agent ?? event.payload.role ?? event.payload.node;
  const agent = typeof value === 'string' ? agentNames[value.toLowerCase()] : undefined;
  const step = event.kind === 'agent' ? event.payload.stage === 'start' ? '开始处理' : '完成处理' : names[event.kind];
  return agent ? `${agent} · ${step ?? '更新'}` : step ?? '运行记录已更新';
}

function RunEditor({api, route, runId, command, runtime, currentVersion, close, refresh, done}: {
  api: Client; route: string; runId?: string; command: RunCommand; runtime?: Runtime;
  currentVersion: number; close: () => void; refresh: () => void; done: (result: Run, message: string) => void;
}) {
  const actions = useActions(api);
  const stale = command.version !== currentVersion;
  const start = command.type === 'start';
  const cancel = command.type === 'cancel';
  const title = start ? '启动助手' : cancel ? '取消运行' : '继续原运行';
  return <Modal title={title} open footer={null} onCancel={actions.busy ? undefined : close}>
    <Form layout="vertical" initialValues={{mode: 'multi', strategy: 'routed'}} onFinish={async (values: RunConfig) => {
      if (stale || (!start && !runId)) return;
      const path = start ? route : `${route}/${segment(runId!)}/${command.type}`;
      const body = {expected_version: command.version, ...(start ? values : {})};
      const notice = start ? '助手运行请求已提交。' : cancel ? '已提交取消请求，原有运行记录会保留。' : '已提交继续请求，请查看运行记录。';
      const result = await actions.submit<Run>(path, body, {success: notice, refresh});
      if (result) done(result, notice);
    }}>
      {start && <>
        <Form.Item label="运行方式" name="mode" rules={[{required: true}]}><Select options={modes.map((value) => ({value, label: label(value)}))} /></Form.Item>
        <Form.Item label="模型策略" name="strategy" rules={[{required: true}]}><Select options={strategies.map((value) => ({value, label: label(value)}))} /></Form.Item>
        <Form.Item label="补充说明（可选）" name="message"><Input.TextArea rows={3} maxLength={1000} /></Form.Item>
      </>}
      <Space orientation="vertical" className="full">
        {runtime?.mode === 'live' && !cancel && <Alert type="warning" showIcon title="确认后可能发起真实模型调用并产生费用。" />}
        {cancel && <p>取消会请求停止后续处理，已完成的步骤和调用记录会保留。</p>}
        {stale && <Alert type="warning" showIcon title="工单状态已更新，请关闭后核对最新状态。" />}
        <Feedback error={actions.error} />
        <Button type="primary" htmlType="submit" loading={actions.busy} disabled={stale} danger={cancel}>{title}</Button>
      </Space>
    </Form>
  </Modal>;
}
