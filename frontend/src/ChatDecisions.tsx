import { useEffect, useState } from 'react';
import { Alert, Button, Card, Checkbox, Input, Modal, Select, Space, Tag } from 'antd';
import { ApiError, errorMessage, segment, type Client } from './api';
import type { Order, User } from './contracts';
import { canAcceptOption, customerActions, decisionActions, decisionRef, editableMeasure, evidenceGaps, isExecutionOption, orderLineName, reviewActions, type CustomerAction, type CustomerOffer, type CustomerOfferView, type DecisionHistoryPage, type DecisionLine, type DecisionOption, type DecisionSnapshot, type EvidenceGap, type ProgramCheck, type ReviewAction, type SupportDecisionView } from './chatDecisionData';
import { investigationReason } from './investigationMessages';
import { useActions, useResource } from './hooks';
import { Evidence, Feedback, Json, money, time } from './ui';

type Props = {api: Client; base: string; user: User; orderId?: string | null; issueId?: string; revision: number; refreshChat: () => void};

export function ChatDecisions(props: Props) {
  return props.user.role === 'customer' ? <CustomerDecisionPanel {...props} /> : <SupportDecisionPanel {...props} />;
}

function useDecisionRead<T>(api: Client, path: string, revision: number) {
  const view = useResource<T>(api, path);
  const {refresh} = view;
  useEffect(() => {
    refresh();
    const timer = setInterval(refresh, 5000);
    return () => clearInterval(timer);
  }, [refresh, revision]);
  return view;
}

function Lines({lines, order}: {lines: DecisionLine[]; order?: Order}) {
  return <ul>{lines.map((line) => <li key={line.line_id}>{orderLineName(order, line.line_id)}
    {line.amount_cents != null ? ` · ${money(line.amount_cents)}` : line.quantity != null ? ` · ${line.quantity} 件` : ''}
  </li>)}</ul>;
}

function CustomerDecisionPanel({api, base, orderId, issueId, revision, refreshChat}: Props) {
  const path = `${base}/decisions/offer`;
  const resource = useDecisionRead<CustomerOfferView>(api, path, revision);
  const order = useResource<Order>(api, orderId ? `/orders/${segment(orderId)}` : null);
  const view = resource.value;
  const offer = !resource.error && (!issueId || view?.issue_id === issueId) ? view?.offer : undefined;
  if (!offer) return resource.error ? <Feedback error={resource.error} /> : null;
  return <OfferCard key={offer.id} api={api} path={path} offer={offer} order={order.value} orderId={orderId}
    locked={resource.loading} refresh={() => {resource.refresh(); refreshChat();}} />;
}

function OfferCard({api, path, offer, order, orderId, locked, refresh}: {
  api: Client; path: string; offer: CustomerOffer; order?: Order; orderId?: string | null; locked: boolean; refresh: () => void;
}) {
  const actions = useActions(api);
  const [choice, setChoice] = useState<CustomerAction>();
  const [reason, setReason] = useState('');
  const [recorded, setRecorded] = useState<CustomerAction>();
  const persisted = offer.customer_decision?.action;
  const response = persisted && persisted !== 'ACCEPT' ? persisted : recorded ?? persisted;
  const choices: CustomerAction[] = response === 'ACCEPT' ? ['REQUEST_CHANGE', 'REJECT'] :
    response ? [] : ['ACCEPT', 'REJECT', 'REQUEST_CHANGE'];
  const actionLabel = (action: CustomerAction) => response === 'ACCEPT' && action === 'REJECT' ? '撤回同意' : customerActions[action];
  const unavailable = !choice || !choices.includes(choice) || locked || (choice === 'REQUEST_CHANGE' && !reason.trim());
  async function submit() {
    if (!choice || unavailable) return;
    const posted = await actions.submit<{action: CustomerAction}>(`${path}/respond`,
      {ref: decisionRef(offer), response: {action: choice, ...(reason.trim() && {reason: reason.trim()})}},
      {success: '意见已记录。', refresh});
    if (posted) {setRecorded(posted.action); setChoice(undefined);}
  }
  return <Card size="small" title="客服方案" className="chat-decision">
    <p>{orderId && `订单 ${orderId} · `}{decisionActions[offer.action]}</p>
    <Lines lines={offer.lines} order={order} />
    {!!offer.policy_ids.length && <p>核对依据 · {offer.policy_ids.join('、')}</p>}
    <p className="muted">请在 {time(offer.expires_at)} 前确认。确认后，仍由客服和审批人员按流程办理。</p>
    {response && <Tag>已提交 · {customerActions[response]}</Tag>}
    {choices.length > 0 && <Space wrap>
      {choices.map((action) =>
        <Button key={action} type={action === 'ACCEPT' ? 'primary' : 'default'} disabled={locked}
          onClick={() => {setChoice(action); setReason('');}}>{actionLabel(action)}</Button>)}
    </Space>}
    {choice && <Modal open title={actionLabel(choice)} okText="确认提交" cancelText="返回"
      okButtonProps={{disabled: unavailable || actions.busy}}
      onOk={() => void submit()} onCancel={() => {if (!actions.busy) setChoice(undefined);}}>
      <p>{response === 'ACCEPT' ? '提交后会撤回之前的同意。若要重新同意，请等待客服提供新方案。' :
        '你的意见会记录在当前方案上。客服将根据处理进度继续跟进。'}</p>
      {choice !== 'ACCEPT' && <Input.TextArea aria-label="给客服的说明" placeholder={choice === 'REQUEST_CHANGE' ? '请说明你想修改的内容' : '可说明拒绝原因'} value={reason} maxLength={1000} onChange={(event) => setReason(event.target.value)} />}
      <Feedback error={actions.error} />
    </Modal>}
  </Card>;
}

function SupportDecisionPanel({api, base, issueId, revision, refreshChat}: Props) {
  const path = `${base}/decisions`;
  const resource = useDecisionRead<SupportDecisionView>(api, path, revision);
  const actions = useActions(api);
  const [historySelection, setHistorySelection] = useState<{scope: string; id: string}>();
  const [historyBrowse, setHistoryBrowse] = useState<{scope: string; cursors: number[]}>();
  const [savedReview, setSavedReview] = useState<{id: string; program_check: ProgramCheck | null}>();
  const [editor, setEditor] = useState<{snapshot: DecisionSnapshot; reviewId: string | null; action: ReviewAction}>();
  const view = resource.value;
  const usable = !resource.error && (!issueId || view?.issue_id === issueId);
  const current = usable ? view?.snapshot : undefined;
  const currentIssue = usable ? view?.issue_id : undefined;
  const scope = JSON.stringify([base, currentIssue]);
  const historyId = historySelection?.scope === scope ? historySelection.id : undefined;
  const browsing = historyBrowse?.scope === scope;
  const cursors = browsing ? historyBrowse.cursors : [];
  const cursor = cursors.at(-1);
  const historyIndex = useResource<DecisionHistoryPage>(api, currentIssue && browsing ?
    `${path}/history?limit=20${cursor ? `&before_version=${cursor}` : ''}` : null);
  const index = !historyIndex.error && historyIndex.value?.issue_id === currentIssue ? historyIndex.value : undefined;
  const nextCursor = index?.next_before_version;
  const showHistory = (id: string) => {setEditor(undefined); setHistorySelection(id === current?.id ? undefined : {scope, id});};
  const reviewCheck = usable ? view?.latest_review?.program_check ?? (
    savedReview?.id === view?.expected_review_id ? savedReview?.program_check : undefined
  ) : undefined;
  const history = useResource<DecisionSnapshot>(api, currentIssue && historyId ? `${path}/history/${segment(historyId)}` : null);
  const historical = !history.error && history.value?.id === historyId &&
    history.value?.input.issue_id === currentIssue ? history.value : undefined;
  const draft = historyId ? historical : current;
  const order = useResource<Order>(api, draft ? `/orders/${segment(draft.input.order_id)}` : null);
  function refresh() {resource.refresh(); historyIndex.refresh(); refreshChat();}
  const stale = !!editor && (current?.id !== editor.snapshot.id || current.sha256 !== editor.snapshot.sha256 || view?.expected_review_id !== editor.reviewId);
  return <Card size="small" className="chat-decision" title="售后方案复核" extra={!historyId && <Button disabled={actions.busy || !usable || !view?.issue_id} onClick={() =>
    void actions.submit(`${path}/run`, {}, {success: '已提交调查。', refresh})}>启动方案调查</Button>}>
    <Feedback error={resource.error || actions.error || history.error || historyIndex.error} notice={actions.notice} />
    {currentIssue && <Button onClick={() => {
      setHistoryBrowse(browsing ? undefined : {scope, cursors: []});
    }}>{browsing ? '收起候选历史' : '查看候选历史'}</Button>}
    {browsing && <section aria-label="候选历史">
      {historyIndex.loading && !index && <p>正在读取候选历史。</p>}
      {index?.items.length === 0 && <p>本事项还没有候选记录。</p>}
      {!!index?.items.length && <ul>{index.items.map((item) => <li key={item.id}>
        <Button type="link" onClick={() => showHistory(item.id)}>
          第 {item.version} 版 · {item.origin === 'AGENT' ? 'Agent 原始候选' : '客服修改方案'}
        </Button>
        {item.id === current?.id && <Tag>当前版本</Tag>}
        <span>{time(item.created_at)}</span>
      </li>)}</ul>}
      <Space wrap>
        {cursors.length > 0 && <Button disabled={historyIndex.loading} onClick={() =>
          setHistoryBrowse({scope, cursors: cursors.slice(0, -1)})}>更新的版本</Button>}
        {nextCursor != null && <Button disabled={historyIndex.loading} onClick={() =>
          setHistoryBrowse({scope, cursors: [...cursors, nextCursor]})}>更早的版本</Button>}
      </Space>
    </section>}
    {historyId && <p>历史记录仅供查看。</p>}

    {!historyId && reviewCheck?.status === 'BLOCKED' && <Alert type="warning" title="复核已记录，暂未向客户发送方案。"
      description={reviewCheck.code ? errorMessage(new ApiError(422, reviewCheck.code, '本次程序核对未通过，请核对订单、材料和方案。')) :
        '本次程序核对未通过，请核对订单、材料和方案。'} />}
    {!historyId && reviewCheck?.status === 'PASSED' && <Tag>客服复核时程序核对通过</Tag>}
    {!historyId && reviewCheck?.status === 'NON_EXECUTING' && <Tag>客服复核记录为后续处理建议</Tag>}
    {!draft && <p className="muted">{historyId ?
      (history.loading ? '正在读取历史版本。' : '当前无法查看该历史版本。') :
      (resource.loading ? '正在读取方案。' : '当前没有待复核的候选方案。')}</p>}
    {draft && <SnapshotCard snapshot={draft} order={order.value} showParent={showHistory} />}
    {historyId && <Button onClick={() => setHistorySelection(undefined)}>返回当前方案</Button>}
    {!historyId && usable && view?.latest_review && <p>最近客服处理 · {reviewActions[view.latest_review.action]} · {view.latest_review.reason} · {time(view.latest_review.created_at)}</p>}
    {!historyId && usable && view?.customer_decision && <section>
      <h3>客户对关联方案的回复</h3>
      <p>方案第 {view.customer_decision.offer.version} 版 · {customerActions[view.customer_decision.action]} · {time(view.customer_decision.created_at)}</p>
      {view.customer_decision.reason && <p>{view.customer_decision.reason}</p>}
      <p className="muted">后续办理时还会核对方案是否有效。</p>
    </section>}
    {current && !historyId && <Space wrap>
      {(Object.keys(reviewActions) as ReviewAction[]).map((action) => <Button key={action} aria-label={reviewActions[action]} disabled={resource.loading ||
        (action === 'MODIFY' && !current.candidate.options.length) || (action === 'ACCEPT' && !current.candidate.options.some((option) => canAcceptOption(current, option)))}
        onClick={() => setEditor({snapshot: current, reviewId: view?.expected_review_id ?? null, action})}>{reviewActions[action]}</Button>)}
    </Space>}
    {!historyId && editor && <ReviewEditor api={api} path={path} order={order.value} {...editor} stale={stale}
      close={() => setEditor(undefined)} refresh={refresh} onSaved={setSavedReview} />}
  </Card>;
}

function SnapshotCard({snapshot, order, showParent}: {snapshot: DecisionSnapshot; order?: Order; showParent: (id: string) => void}) {
  return <section>
    <h3>{snapshot.origin === 'AGENT' ? 'Agent 原始候选' : '客服修改方案'} · 第 {snapshot.version} 版</h3>
    <p>订单 {snapshot.input.order_id} · 截至第 {snapshot.input.input_through_seq} 条消息</p>
    {snapshot.parent_id && <Button onClick={() => showParent(snapshot.parent_id!)}>查看修改前版本</Button>}
    {snapshot.candidate.unresolved.length > 0 && <Alert type="warning" title="仍需核对" description={
      <ul>{snapshot.candidate.unresolved.map((item) => <li key={item}>{investigationReason(item) ?? item}</li>)}</ul>
    } />}
    {snapshot.candidate.options.map((option) => <Card key={option.id} size="small" title={decisionActions[option.action]}>
      <p>{option.rationale}</p>
      <Lines lines={isExecutionOption(option) ? option.lines : option.target_line_ids.map((line_id) => ({line_id}))} order={order} />
      {!isExecutionOption(option) && <ul>{[...option.missing_evidence, ...option.alternatives].map((item, index) => <li key={index}>{item}</li>)}</ul>}
      <Tag>{snapshot.checks[option.id]?.status === 'PASSED' ? '候选生成时核对通过' :
        snapshot.checks[option.id]?.status === 'NON_EXECUTING' ? '仅后续处理建议' : '候选生成时核对未通过'}</Tag>
      <Evidence title="查看依据和核对记录"><Json value={{evidence: option.evidence_ids, check: snapshot.checks[option.id]}} /></Evidence>
    </Card>)}
    <Evidence title="查看候选证据与版本"><Json value={{input: snapshot.input, unresolved: snapshot.candidate.unresolved}} /></Evidence>
  </section>;
}

function ReviewEditor({api, path, order, snapshot, reviewId, action, stale, close, refresh, onSaved}: {
  api: Client; path: string; order?: Order; snapshot: DecisionSnapshot; reviewId: string | null;
  action: ReviewAction; stale: boolean; close: () => void; refresh: () => void; onSaved: (review: {id: string; program_check: ProgramCheck | null}) => void;
}) {
  const actions = useActions(api);
  const options = action === 'ACCEPT' ? snapshot.candidate.options.filter((option) => canAcceptOption(snapshot, option)) : snapshot.candidate.options;
  const [selected, select] = useState<DecisionOption | undefined>(options[0]);
  const [reason, setReason] = useState('');
  const [rationale, setRationale] = useState(selected?.rationale ?? '');
  const [measures, setMeasures] = useState<Record<string, string>>({});
  const [resolved, setResolved] = useState<string[]>([]);
  const [gaps, setGaps] = useState<EvidenceGap[]>([]);
  const [missing, setMissing] = useState(selected && !isExecutionOption(selected) ? selected.missing_evidence.join('\n') : '');
  const choosesOption = action === 'ACCEPT' || action === 'MODIFY';
  function measure(line: DecisionLine, option: DecisionOption) {
    return measures[line.line_id] ?? (option.action === 'REFUND' ? ((line.amount_cents ?? 0) / 100).toFixed(2) : String(line.quantity ?? ''));
  }
  function choose(id: string) {
    const option = options.find((item) => item.id === id);
    select(option);
    setRationale(option?.rationale ?? '');
    setMeasures({});
    setMissing(option && !isExecutionOption(option) ? option.missing_evidence.join('\n') : '');
  }
  const editedLines: DecisionLine[] = selected && isExecutionOption(selected) ? selected.lines.map((line) => {
    const value = editableMeasure(measure(line, selected), selected.action);
    return {line_id: line.line_id, ...(selected.action === 'REFUND' ? {amount_cents: value} : {quantity: value})};
  }) : [];
  const missingEvidence = missing.split('\n').map((value) => value.trim()).filter(Boolean);
  const invalidModification = action === 'MODIFY' && (!rationale.trim() ||
    editedLines.some((line) => line.amount_cents == null && line.quantity == null) ||
    (selected?.action === 'REQUEST_EVIDENCE' && !missingEvidence.length));
  const disabled = stale || !reason.trim() || (choosesOption && !selected) || invalidModification;
  async function submit() {
    if (disabled) return;
    const replacement = action === 'MODIFY' && selected ? (isExecutionOption(selected) ?
      {...selected, rationale: rationale.trim(), lines: editedLines} :
      {...selected, rationale: rationale.trim(), missing_evidence: missingEvidence}) : undefined;
    const result = await actions.submit<{id: string; program_check: ProgramCheck | null}>(`${path}/review`, {
      ref: decisionRef(snapshot), expected_review_id: reviewId,
      ...(action === 'REQUEST_EVIDENCE' && !!gaps.length && {evidence_gaps: gaps}),
      review: {action, reason: reason.trim(), ...(action === 'ACCEPT' && {option_id: selected?.id}),
        ...(replacement && {replacement, resolved_items: resolved})},
    }, {success: '复核已记录。', refresh});
    if (result) {onSaved(result); close();}
  }
  return <Modal open title={`${reviewActions[action]} · 方案第 ${snapshot.version} 版`} okText="保存复核" cancelText="返回"
    okButtonProps={{disabled: disabled || actions.busy}} onOk={() => void submit()}
    onCancel={() => {if (!actions.busy) close();}}>
    <Space orientation="vertical" style={{width: '100%'}}>
      {stale && <Alert type="warning" title="候选方案或复核记录已更新，请关闭并重新打开。" />}
      {choosesOption && <Select aria-label="选择候选" value={selected?.id} onChange={choose}
        options={options.map((item, index) => ({value: item.id, label: `${index + 1}. ${decisionActions[item.action]}`}))} />}
      {selected && action === 'MODIFY' && <>
        {isExecutionOption(selected) && selected.lines.map((line) => {
          const label = `${orderLineName(order, line.line_id)} ${selected.action === 'REFUND' ? '退款金额（元）' : '换发数量（件）'}`;
          return <label key={line.line_id}>{label}<Input aria-label={label} value={measure(line, selected)}
            onChange={(event) => setMeasures({...measures, [line.line_id]: event.target.value})} /></label>;
        })}
        {selected.action === 'REQUEST_EVIDENCE' && <label>需要补充的材料（每行一项）
          <Input.TextArea aria-label="需要补充的材料" value={missing} maxLength={1000} onChange={(event) => setMissing(event.target.value)} />
        </label>}
        <label>修改后的方案依据<Input.TextArea aria-label="修改后的方案依据" value={rationale} maxLength={1000}
          onChange={(event) => setRationale(event.target.value)} /></label>
        {!!snapshot.candidate.unresolved.length && <fieldset><legend>勾选已核对清楚的事项</legend>
          {snapshot.candidate.unresolved.map((item) => <div key={item}><Checkbox checked={resolved.includes(item)}
            onChange={(event) => setResolved(event.target.checked ? [...resolved, item] : resolved.filter((value) => value !== item))}>{investigationReason(item) ?? item}</Checkbox></div>)}
        </fieldset>}
        <p>保存后将重新核对。核对结果和原始候选都可查看。</p>
      </>}
      {action === 'REQUEST_EVIDENCE' && <fieldset><legend>请客户补充的内容</legend>
        <p>可勾选具体内容。留空时由系统根据本次售后的规则判断缺项。</p>
        {(Object.keys(evidenceGaps) as EvidenceGap[]).map((gap) => <div key={gap}>
          <Checkbox checked={gaps.includes(gap)} onChange={(event) =>
            setGaps(event.target.checked ? [...gaps, gap] : gaps.filter((value) => value !== gap))}>{evidenceGaps[gap]}</Checkbox>
        </div>)}
      </fieldset>}
      <label>客服复核理由（内部可见）<Input.TextArea aria-label="客服复核理由" value={reason} maxLength={1000}
        onChange={(event) => setReason(event.target.value)} /></label>
      <Feedback error={actions.error} />
    </Space>
  </Modal>;
}
