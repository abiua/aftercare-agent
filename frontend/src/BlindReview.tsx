import { useEffect, useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Input, Modal, Radio, Select, Space, Tag } from 'antd';
import { segment, type Client } from './api';
import { editableMeasure } from './chatDecisionData';
import {
  blindActions, blindLabels, blindStages, latestBlindDetail, policyStatuses, visibleBlindExposure,
  type BlindAction, type BlindCandidate, type BlindDetail, type BlindImage, type BlindInput, type BlindJudgment,
  type BlindLabel, type BlindPage, type BlindPlan, type BlindReference, type InitialPayload, type PolicyStatus,
} from './chatBlindReview';
import { useActions, useResource } from './hooks';
import { Evidence, Feedback, Json, label, money, time } from './ui';

const base = '/evaluation/chat-blind-reviews';
const actions = Object.entries(blindActions).map(([value, label]) => ({value, label}));
const factLabels: Record<string, string> = {
  ORDER_LINE: '订单商品', ORDER: '订单', CONDITION: '订单状态', EXECUTION: '既有执行记录',
  MATERIAL: '已登记材料', REVIEW: '材料审核记录', FACT: '业务记录', OBSERVATION: '图片观察',
};
const rowName = (id: string) => `商品行 ${id}`;

export function BlindReview({api}: {api: Client}) {
  const [selected, setSelected] = useState<string>();
  const [pages, setPages] = useState<string[]>([]);
  const cursor = pages.at(-1);
  const list = useResource<BlindPage>(api, `${base}?limit=25${cursor ? `&after=${segment(cursor)}` : ''}`);
  if (selected) return <BlindTask key={selected} api={api} id={selected} back={() => {setSelected(undefined); list.refresh();}} />;
  return <div className="stack">
    <h2>独立盲审</h2>
    <p>先根据冻结资料封存自己的判断。之后可选择查看助手候选，并单独记录后置复核。</p>
    <Feedback error={list.error} />
    {list.value?.items.length === 0 && <p>当前没有分配给你的盲审任务。</p>}
    {list.value?.items.map((task) => <div className="section-row" key={task.id}>
      <Button type="link" onClick={() => setSelected(task.id)}>{task.case_label}</Button><Tag>{blindStages[task.status]}</Tag>
    </div>)}
    <Space wrap>
      <Button disabled={!pages.length || list.loading} onClick={() => setPages(pages.slice(0, -1))}>上一页任务</Button>
      <Button disabled={!list.value?.next_after || list.loading} onClick={() => {
        if (list.value?.next_after) setPages([...pages, list.value.next_after]);
      }}>下一页任务</Button>
      <Button onClick={list.refresh}>刷新任务</Button>
    </Space>
  </div>;
}

function BlindTask({api, id, back}: {api: Client; id: string; back: () => void}) {
  const path = `${base}/${segment(id)}`;
  const resource = useResource<BlindDetail>(api, path);
  const actions = useActions(api);
  const [written, setWritten] = useState<BlindDetail>();
  const [confirmation, setConfirmation] = useState<{stage: 'reveal' | 'initial'; version: number}>();
  const [pending, setPending] = useState<InitialPayload>();
  const detail = !resource.error ? latestBlindDetail(resource.value, written) : undefined;
  const exposure = detail ? visibleBlindExposure(detail) : null;

  async function save(stage: 'initial' | 'reveal' | 'labels', payload: object) {
    if (!detail) return;
    const result = await actions.submit<BlindDetail>(`${path}/${stage}`, {expected_version: detail.version, ...payload},
      {success: '', refresh: resource.refresh});
    if (result) {setWritten(result); setConfirmation(undefined); setPending(undefined);}
  }
  return <article className="stack">
    <Space wrap><Button onClick={back} disabled={actions.busy}>返回盲审任务</Button><Button onClick={resource.refresh}>刷新任务状态</Button></Space>
    <Feedback error={resource.error || actions.error} />
    {detail && <>
      <div className="section-row"><h2>{detail.case_label}</h2><Tag>{blindStages[detail.status]}</Tag></div>
      <Alert type="info" title="这份记录只说明你在本应用内何时封存初判、何时查看候选。"
        description="是否在其他地方看过该案例，以及评审身份是否独立，需要另行确认。" />
      <FrozenInput api={api} path={path} input={detail.input} sha256={detail.input_sha256} />
      {detail.status === 'BLIND' && !detail.initial && <InitialEditor input={detail.input} disabled={resource.loading || actions.busy}
        onSubmit={(payload) => {setPending(payload); setConfirmation({stage: 'initial', version: detail.version});}} />}
      {detail.initial && <InitialRecord record={detail.initial} />}
      {detail.status === 'SEALED' && detail.initial && <Card title="查看助手候选">
        <p>查看后会记录首次揭示时间。你的初判已经封存。</p>
        <Button type="primary" disabled={actions.busy || resource.loading} onClick={() => setConfirmation({stage: 'reveal', version: detail.version})}>查看候选</Button>
      </Card>}
      {exposure && <Card title="已揭示的候选">
        <p className="muted">揭示时间 {time(exposure.created_at)}。请对照你的独立判断与当前冻结证据。</p>
        <Evidence title="揭示记录摘要"><Json value={exposure.sha256} /></Evidence>
        <PostLabels key={exposure.sha256} candidates={exposure.candidates} recorded={detail.labels}
          editable={detail.status === 'REVEALED'} disabled={resource.loading || actions.busy}
          onSubmit={(items) => void save('labels', {items})} />
      </Card>}
    </>}
    {confirmation && <Modal open title={confirmation.stage === 'initial' ? '封存独立判断' : '揭示助手候选'}
      okText={confirmation.stage === 'initial' ? '确认封存' : '确认揭示'} cancelText="返回"
      okButtonProps={{disabled: actions.busy || resource.loading || confirmation.version !== detail?.version ||
        (confirmation.stage === 'initial' ? detail?.status !== 'BLIND' || !pending : detail?.status !== 'SEALED')}}
      onOk={() => {if (confirmation.version !== detail?.version) return; if (confirmation.stage === 'reveal') void save('reveal', {}); else if (pending) void save('initial', pending);}}
      onCancel={() => {if (!actions.busy) setConfirmation(undefined);}}>
      <p>{confirmation.stage === 'initial' ? '封存后，你仍可回看本次判断。后置复核会另存一份记录。' :
        '确认后，应用会先记录揭示时间，再显示候选。'}</p>
      <Feedback error={actions.error} />
    </Modal>}
  </article>;
}

function FactSummary({fact}: {fact: BlindInput['facts'][number]}) {
  const data = fact.data;
  if (fact.kind === 'ORDER_LINE') return <p>
    {typeof data.title === 'string' && data.title}
    {typeof data.quantity === 'number' && ` · ${data.quantity} 件`}
    {typeof data.paid_cents === 'number' && ` · 实付 ${money(data.paid_cents)}`}
    {typeof data.refunded_cents === 'number' && ` · 已登记退款 ${money(data.refunded_cents)}`}
  </p>;
  if (fact.kind === 'OBSERVATION' && typeof data.summary === 'string') return <p>{data.summary}</p>;
  if (fact.kind === 'MATERIAL' && typeof data.kind === 'string') return <p>{label(data.kind)}</p>;
  if (fact.kind === 'REVIEW') {
    const findings = {
      VISIBLE_DAMAGE: '可见破损', NO_VISIBLE_DAMAGE: '未观察到可见破损', UNCERTAIN: '无法判断',
    };
    const observations = Array.isArray(data.observations) ? data.observations.flatMap((item) => {
      if (item?.source !== 'HUMAN_MATERIAL_REVIEW' || typeof item.material_id !== 'string' ||
        typeof item.finding !== 'string' || !Object.hasOwn(findings, item.finding)) return [];
      return [<li key={item.material_id}>
        材料 {item.material_id} · {findings[item.finding as keyof typeof findings]}
        {typeof item.line_id === 'string' && ` · 商品行 ${item.line_id}`}
        {typeof item.affected_quantity === 'number' && ` · 该商品行受影响总数 ${item.affected_quantity} 件`}
      </li>];
    }) : [];
    return <>
      <p>材料审核{typeof data.decision === 'string' ? label(data.decision) : '结果未登记'}
        {typeof data.effective === 'boolean' && (data.effective ? ' · 当前有效' : ' · 当前已失效')}
        {typeof data.created_at === 'string' && ` · ${time(data.created_at)}`}</p>
      {observations.length > 0 && <><p>人工记录的照片观察</p>
        <ul>{observations}</ul></>}
    </>;
  }
  if (fact.kind === 'EXECUTION' && typeof data.status === 'string') return <p>{label(data.status)}
    {typeof data.amount_cents === 'number' && ` · ${money(data.amount_cents)}`}</p>;
  if (fact.kind === 'CONDITION' && typeof data.value === 'string') return <p>{label(data.value)}</p>;
  return null;
}

function FrozenInput({api, path, input, sha256}: {api: Client; path: string; input: BlindInput; sha256: string}) {
  return <Card title="冻结资料" className="stack">
    <section><h3>客户原话与图片</h3>
      {input.messages.map((message) => <article key={message.id}>
        <p style={{whiteSpace: 'pre-wrap'}}>{message.text || '本条消息只有图片。'}</p>
        {input.images.filter((item) => item.message_id === message.id).map((item, index) =>
          <OriginalImage key={item.id} api={api} path={path} image={item} label={`消息 ${message.sequence} 的原图 ${index + 1}`} />)}
      </article>)}
    </section>
    <section><h3>业务与材料记录</h3>
      {input.facts.map((fact) => <article key={fact.id}>
        <strong>{Object.hasOwn(factLabels, fact.kind) ? factLabels[fact.kind] : '冻结记录'} · {fact.id}</strong>
        <Tag>{fact.trust === 'UNVERIFIED_OBSERVATION' ? '待核实的图片观察' : fact.trust === 'VERIFIED_RECORD' ? '已冻结的业务记录' : '来源未标明'}</Tag>
        <FactSummary fact={fact} />
        <Evidence title="查看原始字段"><Json value={fact.data} /></Evidence>
      </article>)}
    </section>
    <section><h3>条款原文</h3>
      {input.policy_coverage === 'COMPLETE_CATALOGUE' ? <p>包含冻结时的完整条款目录。</p> :
        <Alert type="warning" showIcon title="此快照只保留当时选取的条款，无法核对未归档的条款。" />}
      {input.policy_context && <Evidence title="查看条款判断所用的原始业务背景"><Json value={input.policy_context} /></Evidence>}
      {input.policies.map((policy) => <article key={JSON.stringify([policy.id, policy.version])}>
        <h4>{policy.title || policy.id} · {policy.id} 第 {policy.version} 版</h4>
        <p style={{whiteSpace: 'pre-wrap'}}>{policy.text}</p>
        <Evidence title="查看适用范围与参数"><Json value={{conditions: policy.conditions, parameters: policy.parameters}} /></Evidence>
      </article>)}
    </section>
    <Evidence title="冻结资料摘要"><Json value={{sha256, intent: input.intent}} /></Evidence>
  </Card>;
}

function OriginalImage({api, path, image, label}: {api: Client; path: string; image: BlindImage; label: string}) {
  const [source, setSource] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const current = useRef<{controller?: AbortController; url?: string}>({});
  useEffect(() => {
    const state = current.current;
    return () => {state.controller?.abort(); if (state.url) URL.revokeObjectURL(state.url);};
  }, []);
  async function load() {
    if (current.current.controller || current.current.url) return;
    const controller = new AbortController();
    current.current.controller = controller;
    setError('');
    setLoading(true);
    try {
      const blob = await api.blob(`${path}/images/${segment(image.id)}`, controller.signal);
      if (!controller.signal.aborted) {
        current.current.url = URL.createObjectURL(blob);
        setSource(current.current.url);
      }
    } catch {
      if (!controller.signal.aborted) setError('这张原图暂时无法读取，请稍后重试。');
    } finally {
      if (!controller.signal.aborted) {
        current.current.controller = undefined;
        setLoading(false);
      }
    }
  }
  return <div>
    {source ? <img alt={label} src={source} style={{display: 'block', maxWidth: '100%', maxHeight: 360}} /> :
      <Button loading={loading} onClick={() => void load()}>查看{label}</Button>}
    <Feedback error={error} />
  </div>;
}

type DraftLine = {line_id: string; minimum: string; maximum: string; quantity: string};
type DraftPlan = {key: string; action?: BlindAction; lines: DraftLine[]; evidence: string};

function planPayload(draft: DraftPlan): BlindPlan | undefined {
  const action = draft.action;
  if (!action) return;
  if ((action === 'REFUND' || action === 'REPLACE') && !draft.lines.length) return;
  const lines: BlindPlan['lines'] = [];
  for (const row of draft.lines) {
    if (action === 'REFUND') {
      const minimum = editableMeasure(row.minimum, action);
      const maximum = editableMeasure(row.maximum, action);
      if (minimum === undefined || maximum === undefined || minimum > maximum) return;
      lines.push({line_id: row.line_id, amount_min_cents: minimum, amount_max_cents: maximum});
    } else if (action === 'REPLACE') {
      const quantity = editableMeasure(row.quantity, action);
      if (quantity === undefined) return;
      lines.push({line_id: row.line_id, quantity});
    } else lines.push({line_id: row.line_id});
  }
  const required_evidence = draft.evidence.split('\n').map((item) => item.trim()).filter(Boolean);
  if ((action === 'REQUEST_EVIDENCE' && !required_evidence.length) ||
    required_evidence.length > 20 || required_evidence.some((item) => item.length > 160)) return;
  return {action, lines, required_evidence};
}

function InitialEditor({input, disabled, onSubmit}: {input: BlindInput; disabled: boolean; onSubmit: (payload: InitialPayload) => void}) {
  const [judgment, setJudgment] = useState<BlindJudgment>();
  const [plans, setPlans] = useState<DraftPlan[]>([]);
  const [requiredFacts, setRequiredFacts] = useState<string[]>([]);
  const [policyChoices, setPolicyChoices] = useState<Record<number, PolicyStatus>>({});
  const [notes, setNotes] = useState('');
  const eligibleLines = input.facts.flatMap((fact) => fact.kind === 'ORDER_LINE' && typeof fact.data.line_id === 'string' ?
    [{value: fact.data.line_id, label: typeof fact.data.title === 'string' ? `${fact.data.title} · ${rowName(fact.data.line_id)}` : rowName(fact.data.line_id)}] : []);
  const acceptablePlans = plans.map(planPayload);
  const maySeal = judgment === 'UNDETERMINED' || judgment === 'DECIDABLE' && acceptablePlans.length > 0 && acceptablePlans.every(Boolean);

  function save() {
    if (!judgment || !maySeal) return;
    onSubmit({judgment, acceptable_plans: judgment === 'DECIDABLE' ? acceptablePlans.filter((item): item is BlindPlan => !!item) : [],
      required_fact_ids: requiredFacts, policy_assessments: input.policies.flatMap((policy, index) => policyChoices[index] ?
        [{policy_id: policy.id, version: policy.version, status: policyChoices[index]}] : []), notes});
  }
  function edit(key: string, update: Partial<DraftPlan>) {
    setPlans(plans.map((plan) => plan.key === key ? {...plan, ...update} : plan));
  }
  return <Card title="记录独立判断">
    <div className="stack">
      <fieldset><legend>根据现有资料，可以判断应如何处理吗？</legend>
        <Radio.Group value={judgment} onChange={(event) => setJudgment(event.target.value)}
          options={[{value: 'DECIDABLE', label: '可以判断'}, {value: 'UNDETERMINED', label: '无法判断'}]} />
      </fieldset>
      {judgment === 'DECIDABLE' && <section>
        <h3>你认为可接受的方案</h3>
        <p>可以登记多个方案。金额按人民币元填写，换发按件数填写。</p>
        {plans.map((plan, index) => <Card key={plan.key} size="small" title={`可接受方案 ${index + 1}`}
          extra={<Button onClick={() => setPlans(plans.filter((item) => item.key !== plan.key))}>移除此方案</Button>}>
          <div className="stack">
            <Select virtual={false} aria-label={`方案 ${index + 1} 的动作`} value={plan.action} onChange={(action) => edit(plan.key, {action})}
              placeholder="请选择处理方式" options={actions} />
            <Select virtual={false} mode="multiple" maxCount={30} aria-label={`方案 ${index + 1} 的商品`} value={plan.lines.map((row) => row.line_id)}
              options={eligibleLines} placeholder="请选择本方案涉及的商品"
              onChange={(selected: string[]) => edit(plan.key, {lines: selected.map((line_id) =>
                plan.lines.find((row) => row.line_id === line_id) ?? {line_id, minimum: '', maximum: '', quantity: ''})})} />
            {plan.lines.map((row) => {
              function change(field: 'minimum' | 'maximum' | 'quantity', value: string) {
                edit(plan.key, {lines: plan.lines.map((entry) => entry.line_id === row.line_id ? {...entry, [field]: value} : entry)});
              }
              return <fieldset key={row.line_id}><legend>{rowName(row.line_id)}</legend>
                {plan.action === 'REFUND' && <Space wrap>
                  <label>最低退款金额<Input aria-label={`${rowName(row.line_id)} 最低退款金额`} inputMode="decimal" value={row.minimum}
                    onChange={(event) => change('minimum', event.target.value)} /></label>
                  <label>最高退款金额<Input aria-label={`${rowName(row.line_id)} 最高退款金额`} inputMode="decimal" value={row.maximum}
                    onChange={(event) => change('maximum', event.target.value)} /></label>
                </Space>}
                {plan.action === 'REPLACE' && <label>换发数量<Input aria-label={`${rowName(row.line_id)} 换发数量`} inputMode="numeric"
                  value={row.quantity} onChange={(event) => change('quantity', event.target.value)} /></label>}
              </fieldset>;
            })}
            <label>需要补充或核对的材料（每行一项，每项最多 160 字）
              <Input.TextArea aria-label={`方案 ${index + 1} 需要的材料`} maxLength={3220} value={plan.evidence}
                onChange={(event) => edit(plan.key, {evidence: event.target.value})} />
            </label>
          </div>
        </Card>)}
        <Button disabled={plans.length >= 12} onClick={() => setPlans([...plans, {key: crypto.randomUUID(), lines: [], evidence: ''}])}>添加可接受方案</Button>
      </section>}
      {!!input.facts.length && <fieldset><legend>需要核对哪些事实</legend>
        {input.facts.map((fact) => <div key={fact.id}><Checkbox checked={requiredFacts.includes(fact.id)}
          onChange={(event) => setRequiredFacts(event.target.checked ? [...requiredFacts, fact.id] : requiredFacts.filter((id) => id !== fact.id))}>
          {Object.hasOwn(factLabels, fact.kind) ? factLabels[fact.kind] : '冻结记录'} · {fact.id}
        </Checkbox></div>)}
      </fieldset>}
      {!!input.policies.length && <fieldset><legend>条款是否适用</legend>
        {input.policies.map((policy, index) => <div key={index}>
          <p>{policy.title || policy.id} · {policy.id} 第 {policy.version} 版</p>
          <Select virtual={false} aria-label={`条款 ${policy.id} 第 ${policy.version} 版的适用性`} value={policyChoices[index]} allowClear
            placeholder="暂不评价" options={Object.entries(policyStatuses).map(([value, label]) => ({value, label}))}
            onChange={(value) => setPolicyChoices({...policyChoices, [index]: value})} />
        </div>)}
      </fieldset>}
      <label>判断说明<Input.TextArea aria-label="判断说明" value={notes} maxLength={2000}
        onChange={(event) => setNotes(event.target.value)} /></label>
      <Button type="primary" disabled={disabled || !maySeal} onClick={save}>封存初判</Button>
    </div>
  </Card>;
}

function InitialRecord({record}: {record: NonNullable<BlindDetail['initial']>}) {
  const value = record.payload;
  return <Card title="已封存的独立判断">
    <p>{value.judgment === 'DECIDABLE' ? '可以判断' : '无法判断'} · {time(record.created_at)}</p>
    {value.acceptable_plans.map((plan, index) => <section key={index}>
      <h4>{blindActions[plan.action]}</h4>
      {plan.lines.map((row) => <p key={row.line_id}>{rowName(row.line_id)}
        {row.amount_min_cents != null && ` · ${money(row.amount_min_cents)} 至 ${money(row.amount_max_cents)}`}
        {row.quantity != null && ` · ${row.quantity} 件`}</p>)}
      {!!plan.required_evidence.length && <p>所需材料 · {plan.required_evidence.join('、')}</p>}
    </section>)}
    {!!value.required_fact_ids.length && <p>需核对的事实 · {value.required_fact_ids.join('、')}</p>}
    {value.policy_assessments.map((item) => <p key={JSON.stringify([item.policy_id, item.version])}>
      {item.policy_id} 第 {item.version} 版 · {policyStatuses[item.status]}</p>)}
    {value.notes && <p>{value.notes}</p>}
    <Evidence title="封存记录摘要"><Json value={record.sha256} /></Evidence>
  </Card>;
}

function PostLabels({candidates, recorded, editable, disabled, onSubmit}: {
  candidates: BlindCandidate[]; recorded: BlindDetail['labels']; editable: boolean; disabled: boolean;
  onSubmit: (items: NonNullable<BlindDetail['labels']>['payload']['items']) => void;
}) {
  const [decisions, setDecisions] = useState<Record<number, BlindLabel>>({});
  const [notes, setNotes] = useState<Record<number, string>>({});
  const allChosen = candidates.length > 0 && candidates.every((_candidate, index) => decisions[index]);
  return <div className="stack">
    {candidates.map((candidate, index) => {
      const saved = recorded?.payload.items.find((item) => item.candidate_id === candidate.id);
      const available = candidate.status === 'AVAILABLE';
      return <Card key={candidate.id} size="small" title={`候选 ${index + 1}`}>
        {!available && <p>这一项没有可供核对的方案。请选「无法判断」。</p>}
        {available && candidate.options.map((option) => <section key={option.id}>
          <h4>{blindActions[option.action] ?? '未登记的处理方式'}</h4>
          {option.lines.map((row) => <p key={row.line_id}>{rowName(row.line_id)}
            {row.amount_cents != null && ` · ${money(row.amount_cents)}`}{row.quantity != null && ` · ${row.quantity} 件`}</p>)}
          {!!option.required_evidence.length && <p>补证要求 · {option.required_evidence.join('、')}</p>}
          <p style={{whiteSpace: 'pre-wrap'}}>理由 · {option.rationale}</p>
          <CandidateList title="备选" items={option.alternatives} />
          <CandidateReferences title="引用条款" ids={option.policy_ids} references={option.policy_references} />
          <CandidateReferences title="引用记录" ids={option.evidence_ids} references={option.evidence_references} />
        </section>)}
        <CandidateList title="未解决事项" items={candidate.unresolved} />
        {saved && <p>{blindLabels[saved.decision]}{saved.notes && ` · ${saved.notes}`}</p>}
        {editable && <div className="stack">
          <fieldset><legend>这个候选是否符合你能接受的处理方式？</legend>
            <Radio.Group value={decisions[index]} onChange={(event) => setDecisions({...decisions, [index]: event.target.value})}
              options={Object.entries(blindLabels).map(([value, label]) => ({value, label, disabled: !available && value !== 'UNDETERMINED'}))} />
          </fieldset>
          <Input.TextArea aria-label={`候选 ${index + 1} 的复核说明`} placeholder="可记录理由" maxLength={2000}
            value={notes[index] ?? ''} onChange={(event) => setNotes({...notes, [index]: event.target.value})} />
        </div>}
      </Card>;
    })}
    {recorded && <p>后置复核已记录 · {time(recorded.created_at)}</p>}
    {editable && <Button type="primary" disabled={disabled || !allChosen} onClick={() => onSubmit(
      candidates.map((candidate, index) => ({candidate_id: candidate.id, decision: decisions[index], notes: notes[index] ?? ''}))
    )}>保存后置复核</Button>}
  </div>;
}

function CandidateList({title, items}: {title: string; items: string[]}) {
  return items.length > 0 && <div><p>{title}</p><ul>
    {items.map((item, index) => <li key={index} style={{whiteSpace: 'pre-wrap'}}>{item}</li>)}
  </ul></div>;
}

function CandidateReferences({title, ids, references}: {title: string; ids: string[]; references: BlindReference[]}) {
  const byId = new Map(references.map((item) => [item.id, item]));
  return ids.length > 0 && <div><p>{title}</p><ul>
    {ids.map((id, index) => {
      const reference = byId.get(id);
      const resolved = reference?.status === 'RESOLVED';
      return <li key={index}>{id} <Tag color={resolved ? 'green' : 'orange'}>{resolved ? '已匹配冻结记录' : '未匹配冻结记录'}</Tag>
        {resolved && reference.resolved_ids.join('、')}
      </li>;
    })}
  </ul></div>;
}
