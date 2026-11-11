import { useRef, useState } from 'react';
import { Alert, Button, Card, Checkbox, Descriptions, Form, Input, InputNumber, Modal, Select, Space, Table } from 'antd';
import { segment, type Client } from './api';
import type { Action, Approval, CaseDetail, Execution, Proposal, ProposalLine, RunOutput, User } from './contracts';
import { useActions } from './hooks';
import { decisionRef, type ChatProposalHandoff, type ProposalOffer } from './chatDecisionData';
import { RefundLookupPanel, useRefundLookup } from './RefundLookupPanel';
import { refundExecutionId } from './refundLookup';
import { productName } from './OrderFacts';
import { canActBeforeExecution, confirmedRefundReceiptId, orderRefundPresentation, receiptId } from './casePresentation';
import { Evidence, Feedback, Json, label, money, Placeholder, Status, time } from './ui';

type Props = {api: Client; detail: CaseDetail; user: User; base: string; refresh: () => void; canOperate: boolean; readFailed: boolean; draft?: RunOutput; handoff?: ChatProposalHandoff};
type Editor = {version: number} & (
  {type: 'proposal'; draft?: RunOutput; offer?: ProposalOffer; handoff?: ChatProposalHandoff} |
  {type: 'approval'; proposal: Proposal} |
  {type: 'execute'; proposal: Proposal} |
  {type: 'reconcile'; execution: Execution}
);
type ProcessValues = {
  action?: Action; notes?: string; policy_ids?: string[]; line_ids?: string[];
  values?: Record<string, number | null>; decision?: string;
};

const proposalStatuses = {
  NO_OFFER: '客服还没有发送处理建议，请先在聊天中复核候选。',
  WAITING_CUSTOMER: '等待客户确认聊天中的处理建议。',
  WITHDRAWN: '客户已拒绝、撤回同意或要求修改，请先在聊天中处理。',
  STALE: '聊天中的处理建议已失效，请核对当前资料后重新发送建议。',
  READY: '客户已确认聊天中的处理建议，提交后仍需独立审批。',
};

function proposalOffer(detail: CaseDetail, handoff?: ChatProposalHandoff) {
  const proposal = detail.chat_proposal;
  if (proposal?.status !== 'READY' || proposal.case_id !== detail.case.id ||
    !(Date.parse(proposal.offer.expires_at) > Date.now())) return undefined;
  if (handoff && (handoff.caseId !== proposal.case_id || handoff.conversationId !== proposal.conversation_id ||
    handoff.issueId !== proposal.issue_id || handoff.offer.id !== proposal.offer.id ||
    handoff.offer.version !== proposal.offer.version || handoff.offer.sha256 !== proposal.offer.sha256)) return undefined;
  return proposal.offer;
}

export function CaseProcessing({api, detail, user, base, refresh, canOperate, readFailed, draft, handoff}: Props) {
  const [editor, setEditor] = useState<Editor>();
  const [notice, setNotice] = useState('');
  const support = user.role === 'support';
  const approver = user.role === 'approver';
  const internal = user.role !== 'customer';
  const version = detail.case.version;
  const refund = detail.case.intent === 'refund_status' ? orderRefundPresentation(detail, readFailed) : undefined;
  const lookupAvailable = !!refund && detail.refund_lookup_available === true;
  const lookupSection = useRef<HTMLDivElement>(null);
  const lookup = useRefundLookup(api, lookupAvailable ? base : undefined);
  const actions = detail.eligibility.actions;
  const latest = detail.proposals.at(-1);
  const chatProposal = support ? detail.chat_proposal : undefined;
  const managed = !!chatProposal || !!handoff;
  const offer = support ? proposalOffer(detail, handoff) : undefined;
  const canPropose = support && (!managed || (!!offer && actions.includes(offer.action))) &&
    detail.eligibility.status === 'ELIGIBLE' && actions.length > 0 &&
    !['PROPOSED', 'APPROVED', 'EXECUTION_PENDING', 'EXECUTION_FAILED', 'RESOLVED'].includes(detail.case.status);

  return <div className="stack">
    <div className="section-row">
      <div><h2>{refund ? '退款进度' : '处理方案'}</h2><p className="muted">{refund ? '按订单查询退款执行记录，包括该订单其他工单登记的退款。' : '审批通过后，客服可按批准内容发起模拟执行。'}</p></div>
      {canPropose && <Button type="primary" disabled={!canOperate} onClick={() => setEditor({
        type: 'proposal', version, draft: managed ? undefined : draft, offer,
        handoff: offer && chatProposal ? {
          caseId: chatProposal.case_id, conversationId: chatProposal.conversation_id,
          issueId: chatProposal.issue_id, offer: decisionRef(offer),
        } : undefined,
      })}>{offer ? '核对已确认方案并提交' : draft?.action ? '核对草案并提交' : '新建处理方案'}</Button>}
    </div>
    {managed && support && <Alert showIcon type={offer ? 'info' : 'warning'}
      title={offer ? proposalStatuses.READY :
        handoff && (!chatProposal || chatProposal.status === 'READY') ? '聊天中的方案已变化或无法核实，请返回聊天重新打开当前方案。' :
        proposalStatuses[chatProposal?.status === 'READY' ? 'STALE' : chatProposal?.status ?? 'NO_OFFER']} />}
    {!managed && draft?.action && internal && <Alert type="info" showIcon title={`助手草案待客服核对 · ${label(draft.action)}`} description={draft.notes || draft.reason} />}
    <Feedback notice={notice} />
    {refund && <>
      <Alert showIcon {...refund.notice} />
      {!readFailed && <div><strong>订单历史累计登记退款 {money(refund.historicalRefundedCents)}</strong>
        <div className="muted">这是订单汇总。单笔处理结果请按执行记录核对，实际到账需另行核对。</div>
      </div>}
    </>}
    {!internal && latest && <Card size="small" title="最近的处理方案">
      <Space wrap><Status value={latest.action} /><Status value={latest.status} />{latest.action === 'REFUND' && money(latest.amount_cents)}</Space>
    </Card>}
    {internal && <Table rowKey="id" dataSource={detail.proposals} pagination={false} size="small" scroll={{x: 550}}
      locale={{emptyText: <Placeholder text="暂无处理方案" />}} columns={[
        {title: '方案', render: (_, row) => <Space orientation="vertical" size={0}><strong>{label(row.action)}</strong><span className="muted">版本 {row.version}</span></Space>},
        {title: '金额 / 数量', render: (_, row) => row.action === 'REFUND' ? money(row.amount_cents) : `${row.lines.reduce((total, line) => total + (line.quantity ?? 0), 0)} 件`},
        {title: '状态', render: (_, row) => <Status value={row.status} />},
        {title: '时间', render: (_, row) => time(row.created_at)},
        {title: '办理', render: (_, row) => {
          const approval = detail.approvals.find((item) => item.proposal_id === row.id && item.effective);
          const current = latest?.id === row.id;
          const approvalValid = !!approval && (!approval.expires_at || new Date(approval.expires_at).getTime() > Date.now());
          const expired = row.status === 'APPROVED' && !approvalValid;
          const beforeExecution = canActBeforeExecution(detail, row.id);
          const mayApprove = approver && current && beforeExecution && (row.status === 'PROPOSED' || expired);
          const mayExecute = support && current && beforeExecution && approvalValid && row.status === 'APPROVED';
          return <Space wrap>
            {mayApprove && <Button type="link" disabled={!canOperate} onClick={() => setEditor({type: 'approval', version, proposal: row})}>{expired ? '重新审批' : '审批'}</Button>}
            {mayExecute && <Button type="link" disabled={!canOperate} onClick={() => setEditor({type: 'execute', version, proposal: row})}>模拟执行</Button>}
            {!mayApprove && !mayExecute && (expired ? <Status value="EXPIRED" /> : '—')}
          </Space>;
        }},
      ]}
      expandable={{expandedRowRender: (row) => <div className="stack"><div>{row.notes}</div><Evidence title="方案明细与版本凭据"><Json value={{lines: row.lines, policy_ids: row.policy_ids, payload_hash: row.payload_hash}} /></Evidence></div>}}
    />}
    {internal && <Approvals approvals={detail.approvals} />}
    {lookupAvailable && <div ref={lookupSection}><RefundLookupPanel lookup={lookup} disabled={readFailed} /></div>}
    <h3>{refund ? '该订单的退款执行记录' : '模拟执行回执'}</h3>
    <Table rowKey="id" dataSource={refund?.rows ?? detail.ledger} pagination={false} size="small" scroll={{x: lookupAvailable ? 900 : refund ? 630 : 460}}
      locale={{emptyText: <Placeholder text={refund && readFailed ? '当前无法读取退款执行记录' : '暂无模拟执行记录'} />}} columns={[
        {title: '操作', render: (_, row) => label(row.action)},
        {title: refund ? '模拟执行结果' : '结果', render: (_, row) => <Status value={row.status} />},
        {title: refund ? '登记金额' : '退款金额', render: (_, row) => row.action === 'REFUND' ? money(row.amount_cents) : '—'},
        ...(refund ? [{title: '模拟回执', render: (_: unknown, row: Execution) =>
          confirmedRefundReceiptId(row) ?? '待核对'}] : []),
        {title: '时间', render: (_, row) => time(row.created_at)},
        ...(lookupAvailable ? [{title: '退款执行编号', render: (_: unknown, row: Execution) =>
          <Space orientation="vertical" size={0}>
            <span>{row.id}</span>
            {refundExecutionId(row.id) && <Button type="link" className="cell-link"
              aria-label={`查询退款执行编号 ${row.id}`} disabled={readFailed}
              onClick={() => {
                lookup.submit(row.id);
                lookupSection.current?.scrollIntoView({block: 'nearest'});
              }}>查询这笔退款</Button>}
          </Space>}] : []),
        ...(support ? [{title: '办理', render: (_: unknown, row: Execution) => row.case_id === detail.case.id && (row.status === 'UNKNOWN' || row.status === 'SUBMITTING')
          ? <Button type="link" disabled={!canOperate} onClick={() => setEditor({type: 'reconcile', version, execution: row})}>核对执行结果</Button>
          : '—'}] : []),
      ]}
      expandable={{expandedRowRender: (row) => <div className="stack">
        {row.status === 'UNKNOWN' && <p>款项处理结果尚未确认。客服需先核对，再决定后续操作。</p>}
        {row.status === 'SUBMITTING' && <p>已有提交记录，模拟执行结果和回执尚待确认。</p>}
        {!refund && receiptId(row.receipt) && <div>模拟回执编号 {receiptId(row.receipt)}</div>}
        {internal && <Evidence title="执行记录"><Json value={{id: row.id, proposal_id: row.proposal_id, business_key: row.business_key, receipt: row.receipt}} /></Evidence>}
      </div>}}
    />
    {editor && <ProcessingEditor api={api} base={base} editor={editor} detail={detail} canOperate={canOperate} refresh={refresh}
      close={() => setEditor(undefined)} done={(message) => {setNotice(message); setEditor(undefined);}} />}
  </div>;
}

function Approvals({approvals}: {approvals: Approval[]}) {
  return <><h3>审批历史</h3>
    <Table rowKey="id" dataSource={approvals} pagination={false} size="small" scroll={{x: 520}}
      locale={{emptyText: <Placeholder text="暂无审批记录" />}} columns={[
        {title: '结论', render: (_, row) => <Status value={row.decision} />},
        {title: '版本', dataIndex: 'proposal_version'},
        {title: '意见', dataIndex: 'notes'},
        {title: '有效期至', render: (_, row) => time(row.expires_at)},
        {title: '审批时间', render: (_, row) => time(row.created_at)},
      ]} />
  </>;
}

function ProcessingEditor({api, base, editor, detail, canOperate, refresh, close, done}: {
  api: Client; base: string; editor: Editor; detail: CaseDetail; canOperate: boolean; refresh: () => void;
  close: () => void; done: (message: string) => void;
}) {
  const actions = useActions(api);
  const offer = editor.type === 'proposal' ? editor.offer : undefined;
  const offerChanged = editor.type === 'proposal' && (offer ?
    !proposalOffer(detail, editor.handoff) : !!detail.chat_proposal);
  const stale = offerChanged || editor.version !== detail.case.version
    || ((editor.type === 'approval' || editor.type === 'execute') && !canActBeforeExecution(detail, editor.proposal.id));
  const [form] = Form.useForm<ProcessValues>();
  const eligibility = detail.eligibility;
  const draft = editor.type === 'proposal' ? editor.draft : undefined;
  const initialAction = draft?.action && eligibility.actions.includes(draft.action) ? draft.action : eligibility.actions[0];
  const initialLines = draft?.action === initialAction ? draft?.lines ?? [] : [];
  const action = Form.useWatch('action', form) ?? initialAction;
  const selected = Form.useWatch('line_ids', form) ?? initialLines.map((item) => item.line_id);
  const eligiblePolicies = (action === 'REPLACE' ? eligibility.policy_ids.filter((id) => ['POL01', 'POL13'].includes(id)) : eligibility.policy_ids) ?? [];
  const titles = {proposal: '提交处理方案', approval: '审批处理方案', execute: '确认模拟执行', reconcile: '核对执行结果'};
  const actionTitle = titles[editor.type];

  async function submit(values: ProcessValues) {
    if (stale || (offer && (!canOperate || !(Date.parse(offer.expires_at) > Date.now())))) return;
    const body: Record<string, unknown> = {expected_version: editor.version};
    const target = editor.type;
    let path = base;
    if (target === 'proposal') {
      const lines: ProposalLine[] = offer?.lines ?? (values.line_ids ?? []).map((line_id) => values.action === 'REFUND'
        ? {line_id, amount_cents: Math.round((values.values?.[line_id] ?? 0) * 100)}
        : {line_id, quantity: values.values?.[line_id] ?? 0});
      path += '/proposals';
      Object.assign(body, {action: offer?.action ?? values.action, lines, notes: values.notes?.trim(),
        policy_ids: offer?.policy_ids ?? values.policy_ids ?? [],
        ...(offer && {expected_offer: decisionRef(offer)}),
      });
    } else if (target === 'approval') {
      path += '/approvals';
      Object.assign(body, {proposal_id: editor.proposal.id, decision: values.decision, notes: values.notes?.trim()});
    } else if (target === 'execute') {
      path += '/execute';
      body.proposal_id = editor.proposal.id;
    } else {
      path += `/executions/${segment(editor.execution.id)}/reconcile`;
    }
    const messages = {
      proposal: '处理方案已提交，当前审批状态请查看方案记录。',
      approval: '已记录审批结论。',
      execute: '已收到模拟执行响应，请以回执状态为准。',
      reconcile: '核对请求已完成，请查看最新执行状态。',
    };
    const result = await actions.submit(path, body, {success: messages[target], refresh});
    if (result) done(messages[target]);
  }

  return <Modal title={actionTitle} open footer={null} onCancel={actions.busy ? undefined : close} width={560}>
    <Form form={form} layout="vertical" onFinish={submit} initialValues={{
      action: initialAction, notes: draft?.notes,
      policy_ids: draft?.policy_ids?.filter((id) => eligiblePolicies.includes(id)),
      line_ids: initialLines.map((line) => line.line_id),
      values: Object.fromEntries(initialLines.map((line) => [line.line_id, initialAction === 'REFUND' ? (line.amount_cents ?? 0) / 100 : line.quantity])),
    }}>
      {editor.type === 'proposal' && <>
        {offer ? <Card size="small" title="已确认的聊天方案">
          <p>{label(offer.action)}</p>
          <ul>{offer.lines.map((line) => <li key={line.line_id}>
            商品行 {line.line_id} · {offer.action === 'REFUND' ? money(line.amount_cents ?? 0) : `${line.quantity} 件`}
          </li>)}</ul>
          <p>政策依据 · {offer.policy_ids.join('、')}</p>
          <p>提交后仍需独立审批。</p>
        </Card> : <>
        <Form.Item label="处理方式" name="action" rules={[{required: true}]}>
          <Select options={eligibility.actions.map((value) => ({value, label: label(value)}))} onChange={() => {form.setFieldValue('values', {}); form.setFieldValue('line_ids', []); form.setFieldValue('policy_ids', []);}} />
        </Form.Item>
        <Form.Item label="处理商品" name="line_ids" rules={[{required: true, type: 'array', min: 1, message: '请选择至少一个商品'}]}>
          <Select mode="multiple" options={eligibility.lines.filter((line) => action === 'REFUND' ? line.available_cents > 0 : (line.replace_available_quantity ?? 0) > 0)
            .map((line) => ({value: line.line_id, label: productName(detail.order, line.product_id)}))} />
        </Form.Item>
        {eligibility.lines.filter((line) => selected.includes(line.line_id)).map((line) => {
          const refund = action === 'REFUND';
          const max = refund ? line.available_cents / 100 : line.replace_available_quantity ?? 0;
          const min = refund ? 0.01 : 1;
          return <Form.Item key={line.line_id} name={['values', line.line_id]}
            label={`${productName(detail.order, line.product_id)} · ${refund ? '退款金额（元）' : '换货数量'}`}
            rules={[{required: true, type: 'number', min, max, message: `请输入 ${min} 至 ${max} 的数值`}]}>
            <InputNumber min={min} max={max} precision={refund ? 2 : 0} className="full" />
          </Form.Item>;
        })}
        <Form.Item label="政策依据" name="policy_ids">
          <Select mode="multiple" options={detail.policies.filter((policy) => eligiblePolicies.includes(policy.id)).map((policy) => ({value: policy.id, label: policy.title ?? policy.id}))} />
        </Form.Item>
        </>}
        <Form.Item label="处理说明" name="notes" rules={[{required: true, whitespace: true, message: '请填写处理说明'}]}><Input.TextArea rows={3} maxLength={1000} /></Form.Item>
      </>}
      {'proposal' in editor && <Card size="small" className="modal-card">
        <Descriptions size="small" column={1} items={[
          {key: 'action', label: '方案', children: label(editor.proposal.action)},
          {key: 'version', label: '版本', children: editor.proposal.version},
          {key: 'amount', label: '金额 / 数量', children: editor.proposal.action === 'REFUND' ? money(editor.proposal.amount_cents) : `${editor.proposal.lines.reduce((sum, item) => sum + (item.quantity ?? 0), 0)} 件`},
          {key: 'notes', label: '说明', children: editor.proposal.notes},
        ]} />
      </Card>}
      {editor.type === 'approval' && <>
        <Form.Item label="审批结论" name="decision" rules={[{required: true, message: '请选择审批结论'}]}>
          <Select options={['APPROVED', 'REJECTED'].map((value) => ({value, label: label(value)}))} />
        </Form.Item>
        <Form.Item label="审批意见" name="notes" rules={[{required: true, whitespace: true, message: '请填写审批意见'}]}><Input.TextArea rows={3} maxLength={1000} /></Form.Item>
      </>}
      {editor.type === 'execute' && <Form.Item name="confirmed" valuePropName="checked" rules={[{validator: (_, value) => value ? Promise.resolve() : Promise.reject(new Error('请确认执行内容'))}]}>
        <Checkbox>我已核对已审批的方案，确认发起模拟执行</Checkbox>
      </Form.Item>}
      {editor.type === 'reconcile' && <p>核对已有执行记录，查询最新模拟回执。</p>}
      <Space orientation="vertical" className="full">
        {stale && <Alert type="warning" showIcon title="工单状态已更新，请关闭后核对最新处理记录。" />}
        <Feedback error={actions.error} />
        <Button type="primary" htmlType="submit" loading={actions.busy} disabled={stale || (!!offer && !canOperate)}>{actionTitle}</Button>
      </Space>
    </Form>
  </Modal>;
}
