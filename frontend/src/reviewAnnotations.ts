import type { EvalResult, ReviewAnnotations, ReviewDecision } from './contracts';
import { reviewRunSteps } from './reviewReading.ts';
import { describeCitationCondition, sameCitation } from './runCitations.ts';

export const reviewGroups = ['tools', 'citations', 'facts'] as const;
export const reviewItemLimit = 100;
export const reviewDecisionLabels = {PASS: '正确', FAIL: '不正确', UNKNOWN: '无法判断'};
type Data = Record<string, unknown>;
type ReviewGroup = typeof reviewGroups[number];
type EvidenceRow = {label: string; text: string};
type ReviewItem = {key: string; title: string; evidence: EvidenceRow[]; raw: Data};
type ReviewSection = {group: ReviewGroup; title: string; question: string; empty: string; items: ReviewItem[]};

const object = (value: unknown): Data | undefined =>
  value !== null && typeof value === 'object' && !Array.isArray(value) ? value as Data : undefined;
const record = (value: unknown): Data => object(value) ?? {};
const rows = (value: unknown): Data[] => Array.isArray(value) ? value.map(record) : [];
const string = (value: unknown): string => typeof value === 'string' ? value : '';
const keys = (value: unknown) => Array.isArray(value) ? [...new Set(value.map(string).filter(Boolean))] : [];
const name = (labels: Record<string, string>, value: unknown) =>
  typeof value === 'string' && Object.hasOwn(labels, value) ? labels[value] : '';
const amount = (value: unknown): string =>
  typeof value === 'number' && Number.isSafeInteger(value) ? `¥${(value / 100).toFixed(2)}` : '';
const date = (value: unknown): string => {
  const timestamp = Date.parse(string(value));
  return Number.isFinite(timestamp) ? new Date(timestamp).toLocaleString('zh-CN', {hour12: false}) : '';
};

const roles = {coordinator: '任务协调', policy: '政策查证', verifier: '结果核验', single: '售后助手'};
const toolNames = {read_case_facts: '查询工单与业务记录', read_policy: '查询条款原文'};
const reviewStates = {APPROVED: '照片审核通过', REJECTED: '照片审核未通过', NEEDS_MORE: '审核要求补充材料'};
const payments = {CONFIRMED: '付款已确认', UNKNOWN: '付款情况待核对'};
const executionStates = {SUCCEEDED: '执行成功', FAILED: '执行失败', SUBMITTING: '执行提交中', UNKNOWN: '执行结果待核对'};
export const reviewFactTitles = {
  selected_line: '申请的是哪件商品', signed_at: '订单何时签收', payment: '订单支付情况',
  material_review: '照片审核结果', stock: '商品可换库存', refund_history: '订单历史退款金额',
  receipt: '系统有没有退款执行和回执记录', historical_balance: '商品还可退多少',
};

function describeArguments(args: Data | undefined) {
  if (!args) return '这份报告没有记录参数。';
  const known = [
    ['条款', string(args.policy_id)], ['版本', string(args.version)],
    ['订单', string(args.order_id)], ['工单', string(args.case_id)],
  ].filter(([, value]) => value).map(([label, value]) => `${label} ${value}`);
  const count = Object.keys(args).length;
  if (!count) return '这次查询没有输入参数。';
  if (known.length < count) known.push('其他参数可在原始记录中查看。');
  return known.join('，');
}

export function ledgerReceiptText(ledger: unknown): string {
  if (!Array.isArray(ledger)) return '';
  if (!ledger.length) return '快照里没有执行记录。';
  const refundRows = rows(ledger).filter((entry) => entry.action === 'REFUND');
  if (!refundRows.length) return '';
  return refundRows.map((entry, index) => {
    const status = name(executionStates, entry.status) || '执行状态未记录';
    const receipt = object(entry.receipt);
    const receiptText = receipt && Object.keys(receipt).length ? '已有回执记录' : '未见确认回执';
    return `第 ${index + 1} 笔记录：${status}，${receiptText}`;
  }).join('；');
}

export function reviewFacts(business: Data): Record<string, string> {
  const order = record(business.order);
  const incident = record(business.case);
  const products = new Map(rows(order.products).map((item) => [item.id, string(item.name)]));
  const lines = new Map(rows(order.items).map((item) => [item.line_id, item]));
  const product = (id: unknown) => products.get(id) || '商品名称未记录';
  const currentReviews = rows(business.reviews).filter((review) =>
    review.effective === true && typeof incident.materials_version === 'number' &&
    review.materials_version === incident.materials_version);
  const review = currentReviews.length === 1 ? currentReviews[0] : {};
  const reviewState = name(reviewStates, review.decision);
  const describeLines = (describe: (line: Data) => string) =>
    rows(record(business.eligibility).lines).flatMap((line) => {
      const description = describe(line);
      return description ? [`${product(line.product_id ?? lines.get(line.line_id)?.product_id)} ${description}`] : [];
    }).join('；');
  return {
    selected_line: keys(incident.line_ids).map((id) => product(lines.get(id)?.product_id)).join('、'),
    signed_at: date(order.delivered_at),
    payment: [name(payments, order.payment_status), amount(order.paid_cents) && `实付 ${amount(order.paid_cents)}`,
      date(order.paid_at) && `付款时间 ${date(order.paid_at)}`].filter(Boolean).join('，'),
    material_review: [reviewState, string(review.notes)].filter(Boolean).join('。'),
    stock: describeLines((line) => typeof line.replace_available_quantity === 'number' && Number.isSafeInteger(line.replace_available_quantity)
      ? `可换 ${line.replace_available_quantity} 件` : ''),
    refund_history: amount(order.refunded_cents) && `订单累计已退款 ${amount(order.refunded_cents)}`,
    receipt: ledgerReceiptText(business.ledger),
    historical_balance: describeLines((line) => amount(line.available_cents)),
  };
}

export function reviewSections(result: Pick<EvalResult, 'expected' | 'actual' | 'trace'>): ReviewSection[] {
  const expected = record(result.expected);
  const actual = record(result.actual);
  const steps = reviewRunSteps(result).map(({id, position, result}) => ({
    eventId: id, step: position == null ? '记录中的' : `第 ${position} 步`, output: record(result.output),
  }));
  const rubric = rows(expected.tool_decisions);
  const audit = new Map(rows(actual.citation_audit).flatMap((entry) => {
    const key = string(entry.key);
    return key ? [[key, entry] as const] : [];
  }));
  const quotes = new Map<string, {
    quote: string; notes: string; eventId: string; step: string; citation: Data; assessment?: Data;
  }[]>();
  for (const {output, eventId, step} of steps) {
    const assessments = rows(output.citation_assessments);
    for (const citation of rows(output.citations)) {
      const id = string(citation.id), version = string(citation.version), quote = string(citation.quote);
      if (!id || !version || !quote) continue;
      const key = `${id}@${version}`;
      const fragments = quotes.get(key) ?? [];
      const notes = string(output.notes);
      if (!fragments.some((item) => item.eventId === eventId && item.quote === quote && item.notes === notes)) {
        const assessment = assessments.find((entry) => sameCitation({id, version, quote}, entry));
        fragments.push({quote, notes, eventId, step, citation, assessment});
        quotes.set(key, fragments);
      }
    }
  }
  return [
    {group: 'tools', title: '查询对象和参数', question: '这次查询的对象和参数是否正确？',
      empty: Array.isArray(result.trace) ? '这份报告没有可逐项复核的查询记录。' : '历史报告缺少查询记录。',
      items: rows(result.trace).filter((event) => event.kind === 'tool').map((event, index) => {
        const payload = record(event.payload);
        const args = object(payload.arguments);
        const reference = object(rubric[index]?.required_arguments);
        const role = name(roles, payload.role);
        const tool = name(toolNames, payload.name) || (payload.name ? '其他查询' : '查询内容未记录');
        return {
          key: String(index), title: `第 ${index + 1} 次查询 · ${[role, tool].filter(Boolean).join(' · ')}`,
          evidence: [
            {label: '实际使用的参数', text: describeArguments(args)},
            {label: '预期参数', text: reference ? describeArguments(reference) : '报告未记录参数对照。'},
            {label: '查询返回内容', text: '这份查询记录只用于核对调用，没有保存工具返回的业务内容。'},
            {label: '参数格式检查', text: payload.schema_valid === true ? '格式检查通过'
              : payload.schema_valid === false ? '格式检查未通过' : '报告未记录格式检查。'},
            ...(args && reference ? [{label: '统计说明', text: '选择无法判断或暂不评时，指标仍可使用已有的程序参数比对。'}] : []),
          ],
          raw: {event, reference: rubric[index]},
        };
      }),
    },
    {group: 'citations', title: '条款依据', question: '这条原文能支持这次答复吗？',
      empty: Array.isArray(actual.citation_audit) ? '这份报告没有可逐项复核的已发布引文。' : '历史报告缺少引用核对记录。',
      items: [...audit].map(([key, entry], index) => {
        const fragments = quotes.get(key) ?? [];
        return {
          key, title: `第 ${index + 1} 条引用`,
          evidence: [
            ...(fragments.length ? fragments.flatMap(({quote, notes, step, assessment}) => [
              {label: `${step}发布的说法`, text: notes || '这次引用未记录对应的答复文字。'},
              {label: `${step}引用的条款原文`, text: quote},
              {label: `${step}的业务前提检查`, text: assessment
                ? describeCitationCondition(assessment.condition_status).label : '报告未登记这段引文的前提检查。'},
            ]) : [{label: '助手引用的条款原文', text: '报告未记录该版本的已发布原文。'}]),
            {label: '条款版本适用检查', text: entry.applicable === true ? '条款版本适用'
              : entry.applicable === false ? '条款版本不适用' : '版本适用性待核对'},
          ],
          raw: {entry, citations: fragments.map(({eventId, citation, assessment}) => ({eventId, citation, assessment}))},
        };
      }),
    },
    {group: 'facts', title: '关键事实', question: '答复有没有准确交代这项事实？',
      empty: '这份报告没有逐项列出需要核对的事实。',
      items: keys(expected.required_facts).map((key, index) => ({
        key, title: name(reviewFactTitles, key) || `第 ${index + 1} 项关键事实`,
        evidence: [
          {label: '核对位置', text: '请看上方的各阶段处理意见。结束时的记录仅供对照，无法代表每一步当时的情况。'},
        ],
        raw: {requiredFact: key},
      })),
    },
  ];
}

export function recordedAnnotations(value: unknown, group: ReviewGroup): [string, ReviewDecision][] {
  return Object.entries(record(record(value)[group])).filter((entry): entry is [string, ReviewDecision] =>
    typeof entry[1] === 'string' && Object.hasOwn(reviewDecisionLabels, entry[1]));
}

export type ReviewChoices = Partial<Record<ReviewGroup, Record<number, ReviewDecision | 'UNSELECTED' | undefined>>>;

export function buildReviewAnnotations(sections: ReviewSection[], choices: ReviewChoices) {
  const annotations: ReviewAnnotations = {tools: {}, citations: {}, facts: {}};
  for (const {group, items} of sections) {
    const entries = items.flatMap(({key}, index) => {
      const decision = choices[group]?.[index];
      return decision && decision !== 'UNSELECTED' && Object.hasOwn(reviewDecisionLabels, decision) ? [[key, decision] as const] : [];
    });
    if (entries.length > reviewItemLimit) return {overLimit: group} as const;
    annotations[group] = Object.fromEntries(entries);
  }
  return {annotations: reviewGroups.some((group) => Object.keys(annotations[group]).length) ? annotations : undefined};
}
