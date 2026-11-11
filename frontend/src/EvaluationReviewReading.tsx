import { Card, Descriptions, Timeline } from 'antd';
import type { EvalResult } from './contracts';
import { materialLabels } from './casePresentation';
import { evaluationReason } from './evaluationMessages';
import { RefundLookupOutcome } from './RefundLookupPanel';
import { readRefundLookup } from './refundLookup';
import { ledgerReceiptText, reviewFacts, reviewFactTitles } from './reviewAnnotations';
import { readReviewEvidence, reviewRecord } from './reviewReading';
import { Evidence, Json, label, money, Status } from './ui';

type RunStep = ReturnType<typeof readReviewEvidence>['runs'][number];

function PublishedOpinion({run, intent}: {run: RunStep; intent: unknown}) {
  const output = run.output;
  const lookup = readRefundLookup(output?.refund_lookup);
  const reasonCode = run.result.reason;
  const reason = typeof reasonCode === 'string' && !['WAITING_INPUT', 'MATERIAL_REVIEW_REQUIRED', 'ELIGIBLE', 'STATUS_ONLY'].includes(reasonCode)
    ? evaluationReason(reasonCode) : '';
  return <div className="result-summary">
    {reasonCode === 'MATERIAL_REVIEW_REQUIRED' ? <p>等待客服审核照片</p> :
      typeof run.result.status === 'string' && <Status value={run.result.status} />}
    {reason && <p>{reason}</p>}
    {run.state === 'empty' && <p>这一步没有发布处理意见。</p>}
    {run.state === 'missing' && <p>报告未保存这一步的处理意见。</p>}
    {output && <>
      {typeof output.notes === 'string' && output.notes.trim()
        ? <p>原文：{output.notes}</p> : <p className="muted">报告未保存意见正文。</p>}
      {Array.isArray(output.missing_materials) && output.missing_materials.length > 0 &&
        <p>需补材料：{output.missing_materials.map((kind) => typeof kind === 'string' && Object.hasOwn(materialLabels, kind)
          ? materialLabels[kind] : '材料类型待核对').join('、')}</p>}
      {typeof output.action === 'string' && <p>处理草案：{label(output.action)}，待客服核对。</p>}
      {typeof output.action === 'string' && Array.isArray(output.lines) && <ul>{output.lines.flatMap((item, index) => {
        const line = reviewRecord(item);
        if (!line) return [];
        const detail = typeof line.amount_cents === 'number' ? `退款金额 ${money(line.amount_cents)}`
          : typeof line.quantity === 'number' ? `换发 ${line.quantity} 件` : '';
        return detail ? [<li key={index}>第 {index + 1} 项商品：{detail}</li>] : [];
      })}</ul>}
      {intent === 'refund_status' && <>
        {lookup && <RefundLookupOutcome result={lookup} />}
        {(!lookup || lookup.status === 'REFERENCE_REQUIRED') &&
          <p>这次意见附带的订单退款执行记录：{ledgerReceiptText(output.ledger) || '报告未保存可核对的记录。'}</p>}
        {typeof output.historical_refunded_cents === 'number' &&
          <p>订单累计登记退款 {money(output.historical_refunded_cents)}</p>}
      </>}
      {Array.isArray(output.citations) && output.citations.length > 0 && <Evidence title="查看这一步引用的条款原文">
        <ul>{output.citations.flatMap((item, index) => {
          const citation = reviewRecord(item);
          return typeof citation?.quote === 'string' && citation.quote.trim() ? [<li key={index}>{citation.quote}</li>] : [];
        })}</ul>
      </Evidence>}
      <Evidence title="查看这一步的完整处理意见"><Json value={output} /></Evidence>
    </>}
  </div>;
}

function BusinessRecords({snapshot, closeout}: {snapshot: Record<string, unknown>; closeout?: boolean}) {
  const fields = Object.entries(reviewFacts(snapshot)).flatMap(([key, value]) =>
    value ? [{key, label: reviewFactTitles[key as keyof typeof reviewFactTitles], children: value}] : []);
  if (closeout) {
    for (const [key, label] of [['proposals', '正式方案'], ['approvals', '方案审批']] as const) {
      const items = snapshot[key];
      if (Array.isArray(items)) fields.push({key, label, children: `${items.length} 条记录`});
    }
  }
  return <>
    {fields.length ? <Descriptions size="small" column={{xs: 1, sm: 2}} items={fields} /> :
      <p className="muted">报告未保存可直接核对的业务事实。</p>}
    <Evidence title="查看业务原始记录"><Json value={snapshot} /></Evidence>
  </>;
}

export function EvaluationReviewReading({result}: {result: EvalResult}) {
  const reading = readReviewEvidence(result);
  const stages = [
    ...reading.actions.map(({position, id, summary, snapshot, record}) => ({
      position, id, children: <><h4>{summary}</h4>
        {snapshot ? <BusinessRecords snapshot={snapshot} /> : <Evidence title="查看本步记录"><Json value={record} /></Evidence>}</>,
    })),
    ...reading.runs.flatMap((run) => run.position == null ? [] : [{
      position: run.position, id: run.id, children: <><h4>{run.title}</h4><PublishedOpinion run={run} intent={reading.intent} /></>,
    }]),
  ].sort((a, b) => a.position - b.position);
  const unordered = reading.runs.filter(({position}) => position == null);
  return <Card size="small" title="先读这次售后">
    <div className="stack">
      <section aria-label="客户原话">
        <h3>客户原话</h3>
        {reading.customerQuestion ? <blockquote>{reading.customerQuestion}</blockquote> : <p>本次报告未保存可确认的客户原话。</p>}
      </section>
      <section aria-label="业务步骤与处理意见">
        <h3>业务步骤与应用记录的处理意见</h3>
        <p className="muted">这里只列报告能确认的动作和处理意见。这些记录无法证明客户实际看到过意见。</p>
        {stages.length ? <Timeline items={stages.map(({id, children}) => ({key: id, children}))} /> :
          <p>本次报告缺少可按顺序核对的业务步骤。</p>}
        {!!unordered.length && <div>
          <p>以下处理记录无法确认先后，无法据此选出末次意见。</p>
          {unordered.map((run) => <PublishedOpinion key={run.id} run={run} intent={reading.intent} />)}
        </div>}
      </section>
      <section aria-label="报告结束时的业务记录">
        <h3>报告结束时的业务记录</h3>
        <p className="muted">这些记录只说明报告结束时的情况。较早步骤当时的事实以各自记录为准。</p>
        {reading.closeout ? <BusinessRecords snapshot={reading.closeout} closeout /> : <p>本次报告未保存结束时的业务记录。</p>}
      </section>
      {!!reading.internalNotes.length && <Evidence title="查看内部草稿和笔记">
        <p>以下是角色内部工作记录，未经核验。应用记录的处理意见见上方。</p>
        {reading.internalNotes.map(({index, role, notes}) => <p key={index}><strong>{role}：</strong>{notes}</p>)}
      </Evidence>}
    </div>
  </Card>;
}
