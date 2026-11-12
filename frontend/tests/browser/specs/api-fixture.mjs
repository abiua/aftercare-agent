import { expect, test as base } from '@playwright/test';

export const chatId = '00000000-0000-4000-8000-000000000001';
const chatPath = `/api/conversations/${chatId}`;
const chatPhoto = Buffer.from('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAwMCAO+a3FYAAAAASUVORK5CYII=', 'base64');
const createdAt = '2026-11-10T04:00:00Z';
export const originalCaseId = 'fixture-case-own';
export const newCaseId = 'fixture-case-created';
export const financialCaseId = 'fixture-case-financial';
export const refundLookupCaseId = 'fixture-case-refund-lookup';
export const refundLookupIds = ['1b17965e-8ecd-44f2-a983-482e334af445', 'cc6c8009-2f82-4ba7-835f-7bd02a83bc07'];
export const financialExecutionId = 'fixture-execution-pending';
export const tokens = {
  customer: 'fixture-customer-token', support: 'fixture-support-token', tester: 'fixture-tester-token',
  approver: 'fixture-approver-token',
};
const users = {
  customer: {user_id: 'fixture-customer', role: 'customer', customer_id: 'C-FIXTURE', display_name: '测试客户'},
  support: {user_id: 'fixture-support', role: 'support', display_name: '测试客服'},
  tester: {user_id: 'fixture-tester', role: 'tester', display_name: '测试评测员'},
  approver: {user_id: 'fixture-approver', role: 'approver', display_name: '测试审批人'},
};
const orders = [
  {
    id: 'A1001', customer_id: 'C-FIXTURE', version: 3, status: 'DELIVERED',
    items: [{line_id: 'L01', product_id: 'P01', quantity: 1, paid_cents: 35900, refunded_cents: 0}],
    products: [{id: 'P01', name: '示例外套'}],
  },
  {
    id: 'A1002', customer_id: 'C-FIXTURE', version: 2, status: 'DELIVERED',
    items: [{line_id: 'L02', product_id: 'P02', quantity: 1, paid_cents: 15800, refunded_cents: 0}],
    products: [{id: 'P02', name: '示例背包'}],
  },
];
const intakeOrders = orders.map(({id, version, status}) => ({id, version, status}));
const originalCase = {
  id: originalCaseId, ticket_id: 'FIXTURE-EXISTING', customer_id: 'C-FIXTURE', order_id: 'A1001',
  intent: 'damaged', status: 'OPEN', version: 1, materials_version: 0, line_ids: ['L01'],
  description: '包装破损，请处理。', created_at: createdAt,
};
const material = {id: 'fixture-photo', kind: 'ITEM', filename: 'fixture-proof.png', content_type: 'image/png', created_at: createdAt};
const financialProposal = {
  id: 'fixture-approved-proposal', version: 5, action: 'REFUND', lines: [{line_id: 'L01', amount_cents: 35900}],
  notes: '受控退款方案', policy_ids: ['POL14'], amount_cents: 35900, created_at: createdAt,
};

function financialDetail({executionStatus, reconciled, approvalExpired}) {
  return {
    case: {...originalCase, id: financialCaseId, ticket_id: 'FIXTURE-FINANCIAL',
      status: reconciled ? 'RESOLVED' : executionStatus ? 'EXECUTION_PENDING' : 'APPROVED',
      version: reconciled ? 8 : executionStatus ? 7 : 6},
    order: orders[0], policies: [], materials: [], reviews: [], versions: {}, facts: {},
    eligibility: {status: 'INELIGIBLE', actions: [], lines: [], policy_ids: []},
    proposals: [{...financialProposal, status: reconciled ? 'EXECUTED' : 'APPROVED'}],
    approvals: [{id: 'fixture-approval', proposal_id: financialProposal.id, proposal_version: 5,
      decision: 'APPROVED', effective: !reconciled && !approvalExpired,
      expires_at: approvalExpired ? '2020-01-01T00:00:00Z' : '2099-01-01T00:00:00Z', created_at: createdAt}],
    ledger: reconciled || executionStatus ? [{...financialProposal, id: financialExecutionId, proposal_id: financialProposal.id,
      case_id: financialCaseId, status: reconciled ? 'SUCCEEDED' : executionStatus,
      receipt: reconciled ? {id: 'sim-pay-fixture'} : null}] : [],
  };
}
const refundExecutions = refundLookupIds.map((id, index) => ({
  id, amount_cents: index ? 2000 : 39800, created_at: createdAt, action: 'REFUND',
  status: index ? 'UNKNOWN' : 'SUCCEEDED', receipt: index ? null : {id: 'fixture-refund-receipt'}, lines: [],
}));

function refundLookupDetail(state) {
  return {
    ...financialDetail({}),
    case: {...originalCase, id: refundLookupCaseId, ticket_id: 'FIXTURE-REFUND-LOOKUP', intent: 'refund_status'},
    order: {...orders[0], items: [{...orders[0].items[0], refunded_cents: 35900}]},
    ledger: state.lookupSingle ? refundExecutions.slice(0, 1) : refundExecutions,
    proposals: [], approvals: [], ...(!state.lookupLegacy && {refund_lookup_available: true}),
  };
}
const architectures = ['strong_single', 'staged_single', 'multi_agent'];
const usage = {agent_calls: 2, user_calls: 1, review_calls: 0,
  agent_cost_usd: 0.01, user_cost_usd: 0.02, review_cost_usd: 0, duration_seconds: 5};

function groups(rows) {
  return architectures.map(architecture => {
    const group = rows.filter(row => row.architecture === architecture);
    const passed = group.filter(row => row.official_reward === 1).length;
    const unscored = group.filter(row => row.official_reward == null).length;
    return {...usage, architecture, scheduled: group.length,
      completed: group.filter(row => row.status === 'completed').length,
      passed, unscored,
      agent_cost_usd: group.some(row => row.agent_cost_usd == null) ? null : usage.agent_cost_usd};
  });
}

function officialReport(kind) {
  const rows = architectures.flatMap(architecture => ['demo-a', 'demo-b'].map((task_id, index) => ({
    ...usage, architecture, task_id, seed: 42 + index, status: 'completed',
    official_reward: index === 0 ? 1 : 0, task_summary: 'Customer asked to check an order.',
  })));
  const repair = rows[1];
  rows[1] = {...repair, status: 'unknown', official_reward: null, agent_cost_usd: null};
  const primary = {scope: 'smoke', revision: 'official-fixture-revision', complete: true, all_scored: false,
    agent_model: 'fixture-agent', user_model: 'fixture-customer', review_model: 'fixture-review',
    task_ids: ['demo-a', 'demo-b'], trials_per_task: 1, rows, architectures: groups(rows),
    tool_names: ['get_order_details'], budget: {max_agent_calls_per_run: 8, max_user_calls_per_run: 8,
      max_review_calls_per_run: 1, max_steps: 25, timeout_seconds_per_run: 60,
      max_output_tokens_per_call: 512, max_calls: 100, max_usd: 2},
    billing: {known_usd: 0.42, unsettled_calls: 1, unsettled_reserved_usd: 0.07}};
  if (kind === 'primary') return primary;
  if (kind === 'repair') return {...primary, scope: 'repair', all_scored: true, task_ids: ['demo-b'],
    rows: [repair], architectures: groups([repair])};
  if (kind === 'combined') {
    const effective = rows.map((row, index) => index !== 1 ? row : {
      ...repair, attempt_count: 2, score_source: 'technical_retry', initial_status: row.status,
      original_attempt: row, repair_attempt: repair, agent_cost_usd: null,
    });
    return {...primary, rows: effective, architectures: groups(effective), all_scored: true,
      original_architectures: primary.architectures, original_directory: 'full', repair_directory: 'full.repair'};
  }
  rows[2] = {...rows[2], status: 'budget_exhausted', official_reward: null};
  rows[3] = {...rows[3], status: 'not_run', official_reward: null};
  return {...primary, complete: false, rows, architectures: groups(rows)};
}

const json = (route, value, status = 200) => route.fulfill({status, json: value});
const denied = (route, status, code, message) => json(route, {detail: {code, message}}, status);

export const test = base.extend({
  fixtureApi: async ({context, baseURL}, use) => {
    const {promise: heldExecution, resolve: releaseExecution} = Promise.withResolvers();
    const {promise: heldRefundLookup, resolve: releaseRefundLookup} = Promise.withResolvers();
    const state = {releaseRefundLookup, refundLookupCalls: [], calls: [], confirmed: [], uploads: [], executions: [], created: undefined, releaseExecution,
      chatPosts: [], chatMessages: [], chatCreated: false,
      chatContext: [], chatAbortFirst: false, chatDetails: [], chatDecisionPosts: []};
    const conversation = {id: chatId, customer_id: 'C-FIXTURE', status: 'OPEN', revision: 1,
      created_at: createdAt, updated_at: createdAt, active_order_id: null, active_case_id: null};
    const replies = new Map();
    function message(role, text, attachments = [], cards = []) {
      const sequence = state.chatMessages.length + 1;
      const row = {id: `message-${sequence}`, conversation_id: chatId, sequence, role, text, created_at: createdAt, attachments, cards};
      state.chatMessages.push(row);
      return row;
    }
    state.appendChatMessage = message;
    const unexpected = [];
    await context.route('**/*', async (route) => {
      const request = route.request();
      const url = new URL(request.url());
      if (url.origin !== baseURL) {
        unexpected.push(`unexpected origin ${url.origin}`);
        return route.abort('blockedbyclient');
      }
      if (!url.pathname.startsWith('/api/')) return route.continue();

      const path = url.pathname;
      const method = request.method();
      const headers = request.headers();
      const role = Object.keys(tokens).find((key) => headers.authorization === `Bearer ${tokens[key]}`);
      const body = headers['content-type']?.includes('application/json') ? request.postDataJSON() : undefined;
      state.calls.push({path, method, role, body, key: headers['idempotency-key']});

      if (method === 'POST' && path === '/api/auth/demo-role') {
        const token = tokens[body?.role];
        if (!token) return denied(route, 400, 'DEMO_ROLE_INVALID', '身份无效。');
        if (state.rejectNextLogin) {
          state.rejectNextLogin = false;
          return json(route, {token: 'invalid-fixture-token'});
        }
        return json(route, {token});
      }
      if (!role) return denied(route, 401, 'AUTH_REQUIRED', '凭据失效。');
      if (method === 'GET' && path === '/api/me') return json(route, users[role]);
      if (method === 'GET' && path === '/api/runtime') return json(route, {mode: 'offline', models: [], features: {}});
      if (path.startsWith('/api/conversations')) {
        if (!['customer', 'support'].includes(role)) return denied(route, 403, 'FORBIDDEN', '无权查看。');
        if (path === '/api/conversations') {
          if (method === 'GET') return json(route, {items: state.chatCreated ? [conversation] : []});
          if (role !== 'customer') return denied(route, 403, 'FORBIDDEN', '无权创建。');
          state.chatCreated = true;
          return json(route, conversation, 201);
        }
        if (path === chatPath && method === 'GET' && state.chatCreated) {
          if (state.chatDecision) Object.assign(conversation, {active_order_id: 'A1001'}, state.chatDecision.conversation);
          const after = Number(url.searchParams.get('after') ?? 0);
          const limit = Number(url.searchParams.get('limit') ?? 50);
          state.chatDetails.push(after);
          const messages = state.chatMessages.filter((row) => row.sequence > after).slice(0, limit);
          const next_after = messages.at(-1)?.sequence ?? after;
          return json(route, {conversation, messages, next_after, has_more: next_after < state.chatMessages.length, analysis: state.chatAnalysis,
            features: state.chatDecision ? {chat_decisions: true, chat_material_bridge: !!state.materialBridge} : undefined, execution_progress: state.chatDecision?.executionProgress, investigation_progress: state.chatInvestigationProgress});
        }
        if (path === `${chatPath}/decision-materials` && role === 'support' && state.materialBridge) {
          const bridge = state.materialBridge;
          if (method === 'GET') return json(route, {issue_id: 'bridge-issue', case_id: 'bridge-case',
            materials_version: bridge.version, items: bridge.items, next_after: null});
          if (method === 'POST') {
            bridge.posts.push({body, key: headers['idempotency-key']});
            bridge.version++;
            bridge.items = bridge.items.map((item) => item.attachment_id === body.attachment_id ?
              {...item, material_id: 'material-1', confirmed_kind: body.kind} : item);
            return json(route, {material: {id: 'material-1'}, materials_version: bridge.version,
              source: {attachment_id: body.attachment_id}});
          }
        }
        if (state.chatDecision && path.startsWith(`${chatPath}/decisions`)) {
          const statePath = path.slice(`${chatPath}/decisions`.length);
          const decision = state.chatDecision;
          const issue = decision.issueId ?? 'decision-issue';
          const inIssue = (snapshot) => snapshot && ({...snapshot, input: {...snapshot.input, issue_id: issue}});
          if (role === 'support' && method === 'GET' && !statePath)
            return json(route, {issue_id: issue, snapshot: inIssue(decision.current), expected_review_id: decision.latest?.id ?? null, latest_review: decision.latest ?? null, customer_decision: decision.customerDecision ?? null});
          if (role === 'support' && method === 'GET' && statePath.startsWith('/history')) {
            const snapshots = decision.history ?? [...new Map([decision.original, decision.current].filter(Boolean)
              .map((snapshot) => [snapshot.id, snapshot])).values()];
            if (statePath === '/history') {
              const before = Number(url.searchParams.get('before_version') ?? Infinity);
              const limit = decision.historyPageSize ?? Number(url.searchParams.get('limit') ?? 20);
              const matching = snapshots.filter(({version}) => version < before).sort((a, b) => b.version - a.version);
              const items = matching.slice(0, limit).map(({id, version, origin}) => ({id, version, origin, created_at: createdAt}));
              return json(route, {issue_id: issue, items, next_before_version: matching.length > limit ? items.at(-1).version : null});
            }
            const snapshot = inIssue(snapshots.find(({id}) => statePath === `/history/${id}`));
            if (snapshot) {
              await decision.holdHistory?.[snapshot.id];
              return json(route, snapshot);
            }
          }
          if (role === 'support' && method === 'POST' && statePath === '/review') {
            state.chatDecisionPosts.push({role, body, key: headers['idempotency-key']});
            if (decision.blockedReview) {
              decision.latest = {id: 'review-blocked', action: body.review.action, reason: body.review.reason,
                created_at: createdAt, program_check: {status: 'BLOCKED', code: 'DECISION_EVIDENCE_OUT_OF_SCOPE'}};
              return json(route, {...decision.latest, offer: null});
            }
            if (body.review.action === 'REQUEST_EVIDENCE') {
              decision.latest = {id: 'review-gap', action: body.review.action, reason: body.review.reason, created_at: createdAt};
              decision.offer = null;
              decision.customerDecision = null;
              return json(route, {...decision.latest, program_check: null, offer: null});
            }
            const replacement = body.review.replacement;
            decision.latest = {id: 'review-1', action: body.review.action, reason: body.review.reason, created_at: createdAt};
            decision.current = {...decision.current, id: 'candidate-2', version: 2, sha256: 'b'.repeat(64),
              parent_id: decision.original.id, origin: 'SUPPORT_MODIFICATION', candidate: {options: [replacement], unresolved: []}};
            decision.offer = {id: 'offer-1', version: 1, sha256: 'c'.repeat(64), action: replacement.action,
              lines: replacement.lines, policy_ids: replacement.policy_ids, expires_at: decision.current.expires_at, customer_decision: null};
            return json(route, {id: decision.latest.id, action: decision.latest.action, offer: decision.offer});
          }
          if (role === 'customer' && method === 'GET' && statePath === '/offer') {
            const value = {issue_id: decision.issueId ?? 'decision-issue', offer: decision.offer ?? null};
            await decision.holdOffer;
            return json(route, value);
          }
          if (role === 'customer' && method === 'POST' && statePath === '/offer/respond' && decision.offer) {
            state.chatDecisionPosts.push({role, body, key: headers['idempotency-key']});
            decision.offer.customer_decision = {id: 'reply-1', action: body.response.action};
            decision.customerDecision = {...decision.offer.customer_decision, reason: body.response.reason ?? null,
              offer: body.ref, created_at: createdAt};
            return json(route, decision.offer.customer_decision);
          }
          return denied(route, 404, 'NOT_FOUND', '无权查看方案。');
        }
        if (path === `${chatPath}/messages` && method === 'POST' && state.chatCreated) {
          const raw = request.postDataBuffer().toString('utf8');
          const boundary = headers['content-type'].split('boundary=')[1];
          const parts = raw.split(`--${boundary}`);
          const text = parts.find((part) => part.includes('name="text"')).split('\r\n\r\n')[1].trim();
          const id = parts.find((part) => part.includes('name="client_message_id"')).split('\r\n\r\n')[1].trim();
          const names = [...raw.matchAll(/name="files"; filename="([^"]+)"/g)].map((match) => match[1]);
          state.chatPosts.push({id, key: headers['idempotency-key'], role, text, names});
          if (!replies.has(id)) {
            const attachments = names.map((filename, index) => ({id: `photo-${index}`, filename, size: chatPhoto.length,
              sha256: 'a'.repeat(64), content_type: 'image/png', content_path: `${chatPath}/attachments/photo-${index}/content`}));
            const messages = [message(role, text, attachments)];
            conversation.revision += 1;
            if (role === 'support') conversation.status = 'HUMAN_HANDOFF';
            else if (state.chatAnalysis && text === '暂时无法提供') {
              conversation.status = 'HUMAN_HANDOFF';
              state.chatAnalysis = {...state.chatAnalysis, state: 'HUMAN_HANDOFF',
                generation: state.chatAnalysis.generation + 1, state_version: 0, pending_uploads: 0};
              messages.push(message('assistant', '此前提交的文字和照片会保留。'));
            }
            else if (attachments.length) messages.push(message('assistant', '已收到照片。'));
            else messages.push(message('assistant', '请先确认要处理的订单。', [],
              [{type: 'order_candidates', data: {orders, new_issue: !!conversation.active_order_id}}]));
            replies.set(id, messages);
          }
          if (state.chatAbortFirst && state.chatPosts.length === 1) return route.abort();
          return json(route, {conversation, messages: replies.get(id), analysis: state.chatAnalysis}, 201);
        }
        if (path === `${chatPath}/context` && method === 'POST' && role === 'customer') {
          state.chatContext.push(body);
          conversation.revision += 1;
          conversation.active_order_id = body.order_id;
          return json(route, {conversation, messages: [message('assistant', '这笔订单的退款账本中未查到执行记录。', [],
            [{type: 'status', data: {code: 'REFUND_NOT_FOUND', refunds: []}}, {type: 'status', data: {code: 'ORDER_CONFIRMED'}}])]});
        }
        if (path === `${chatPath}/handoff` && method === 'POST') {
          conversation.revision += 1;
          conversation.status = 'HUMAN_HANDOFF';
          return json(route, {conversation, messages: [message('assistant', '已转客服处理。')]});
        }
        if (path.startsWith(`${chatPath}/attachments/`) && method === 'GET') {
          return route.fulfill({status: 200, contentType: 'image/png', body: chatPhoto});
        }
        return denied(route, 404, 'NOT_FOUND', '对话不存在。');
      }
      if (method === 'GET' && path === '/api/orders') return json(route, {items: orders});
      if (method === 'GET' && state.chatDecision && path.startsWith('/api/orders/')) {
        const order = orders.find(({id}) => path === `/api/orders/${id}`);
        if (order) return json(route, order);
      }
      if (method === 'GET' && path === '/api/cases') return json(route, {items: state.created ? [state.created, originalCase] : [originalCase]});
      if (method === 'GET' && path === '/api/cases/fixture-case-other') return denied(route, 404, 'NOT_FOUND', '工单不存在。');
      if (method === 'GET' && path === `/api/cases/${refundLookupCaseId}` && role === 'customer') {
        return json(route, refundLookupDetail(state));
      }
      if (method === 'GET' && path === `/api/cases/${refundLookupCaseId}/refund-lookup` && role === 'customer') {
        const id = url.searchParams.get('execution_id');
        state.refundLookupCalls.push(id);
        const row = refundLookupDetail(state).ledger.find((item) => item.id === id);
        if (!row) return denied(route, 404, 'NOT_FOUND', '未找到退款执行记录。');
        const {status, amount_cents, created_at, receipt} = row;
        const response = {schema_version: 'ac-v1', status: 'FOUND',
          execution: {id, status, amount_cents, created_at, receipt_confirmed: !!receipt}};
        if (state.holdRefundLookup && id === refundLookupIds[0]) {
          await heldRefundLookup;
          await json(route, response).catch(() => undefined);
          state.refundLookupReleased = true;
          return;
        }
        return json(route, response);
      }
      if (method === 'GET' && path === `/api/cases/${financialCaseId}` && ['support', 'approver'].includes(role)) {
        return json(route, financialDetail(state));
      }
      if (method === 'POST' && path === `/api/cases/${financialCaseId}/execute` && role === 'support') {
        const key = headers['idempotency-key'];
        state.executions.push({key, body});
        if (!key || body?.expected_version !== 6 || body.proposal_id !== financialProposal.id) {
          return denied(route, 409, 'VERSION_CONFLICT', '工单版本已更新。');
        }
        if (state.executions.length === 1) {
          await heldExecution;
          state.executionStatus = 'UNKNOWN';
          return route.abort('failed');
        }
        if (key !== state.executions[0].key) {
          return denied(route, 409, 'IDEMPOTENCY_CONFLICT', '请求编号发生变化。');
        }
        if (state.executions.length === 2) return denied(route, 429, 'FIXTURE_RATE_LIMIT', '受控限流，请稍后重试。');
        state.reconciled = true;
        return json(route, financialDetail(state).ledger[0]);
      }
      if (method === 'POST' && path === `/api/cases/${financialCaseId}/executions/${financialExecutionId}/reconcile` && role === 'support') {
        if (!headers['idempotency-key'] || body?.expected_version !== 7) {
          return denied(route, 409, 'VERSION_CONFLICT', '工单版本已更新。');
        }
        state.reconciled = true;
        return json(route, financialDetail(state).ledger[0]);
      }
      if (method === 'GET' && state.investigation && path.startsWith(`/api/cases/${originalCaseId}/runs`)) {
        const runPath = `/api/cases/${originalCaseId}/runs`;
        const run = {id: 'fixture-investigation', case_id: originalCaseId, thread_id: 'fixture-thread',
          status: 'COMPLETED', mode: 'single', strategy: 'routed', case_version: 1, output: null,
          created_at: createdAt, updated_at: createdAt};
        if (path === runPath) return json(route, [run]);
        if (path === `${runPath}/${run.id}`) return json(route, run);
        if (path === `${runPath}/${run.id}/events`) return json(route, state.investigation);
      }
      if (state.materialReview && path.startsWith(`/api/cases/${originalCaseId}`)) {
        const casePath = `/api/cases/${originalCaseId}`;
        const photos = [material, {...material, id: 'fixture-parcel', kind: 'PARCEL', filename: 'fixture-parcel.png'}];
        const reviews = state.materialReview.saved ? [state.materialReview.saved] : [];
        if (method === 'GET' && path === casePath) {
          return json(route, {
            case: {...originalCase, version: reviews.length ? 6 : 5, materials_version: 2},
            order: {...orders[0], items: [{...orders[0].items[0], quantity: 2}, ...orders[1].items],
              products: [...orders[0].products, ...orders[1].products]},
            policies: [], materials: photos, reviews: role === 'support' ? reviews : reviews.map((row) => ({...row, observations: undefined})),
            ledger: [], versions: {}, facts: {}, proposals: [], approvals: [],
            eligibility: {status: 'HUMAN_REVIEW', actions: [], lines: [], policy_ids: []},
          });
        }
        if (method === 'GET' && photos.some(({id}) => path === `${casePath}/materials/${id}/content`)) {
          return route.fulfill({status: 200, contentType: 'image/png', body: chatPhoto});
        }
        if (method === 'POST' && path === `${casePath}/material-reviews` && role === 'support') {
          state.materialReview.saved = {id: 'fixture-material-review', materials_version: 2,
            decision: body.decision, notes: body.notes, reviewer_id: users.support.user_id, created_at: createdAt,
            ...(body.observations && {observations: body.observations.map((item) => ({...item, source: 'HUMAN_MATERIAL_REVIEW'}))})};
          return json(route, state.materialReview.saved, 201);
        }
      }
      if (state.proposalHandoff && role === 'support' && path === `/api/cases/${originalCaseId}` && method === 'GET') {
        return json(route, {
          case: originalCase, order: orders[0], chat_proposal: state.proposalHandoff,
          policies: [], materials: [], reviews: [], ledger: [], versions: {}, facts: {}, proposals: [], approvals: [],
          eligibility: {status: 'ELIGIBLE', actions: ['REFUND'], lines: [], policy_ids: ['POL01']},
        });
      }
      if (state.proposalHandoff && role === 'support' && path === `/api/cases/${originalCaseId}/proposals` && method === 'POST') {
        return json(route, {id: 'new-proposal'}, 201);
      }
      if (method === 'GET' && (path === `/api/cases/${originalCaseId}` || (state.created && path === `/api/cases/${newCaseId}`))) {
        const current = path.endsWith(newCaseId) ? state.created : originalCase;
        const uploaded = state.uploads.length > 0 && current.id === originalCaseId;
        return json(route, {
          case: {...current, version: uploaded ? 2 : current.version, materials_version: uploaded ? 1 : 0},
          order: orders[0], policies: [], materials: uploaded ? [material] : [], reviews: [],
          ledger: [], versions: {}, facts: {}, proposals: [], approvals: [],
          eligibility: {status: 'WAITING_INPUT', actions: [], reason: '请补充证明照片。', lines: [],
            missing_materials: uploaded ? ['PARCEL'] : ['ITEM', 'PARCEL'], policy_ids: []},
        });
      }
      if (method === 'GET' && /^\/api\/cases\/(fixture-case-own|fixture-case-created|fixture-case-financial|fixture-case-refund-lookup)\/events$/.test(path)) {
        return route.fulfill({status: 200, contentType: 'text/event-stream', body: 'event: idle\ndata: {}\n\n'});
      }
      if (method === 'POST' && path === `/api/cases/${originalCaseId}/materials` && role === 'customer') {
        state.uploads.push({contentType: headers['content-type'], body: request.postDataBuffer()});
        return json(route, material, 201);
      }

      if (path.startsWith('/api/intake/') && role !== 'customer') {
        return denied(route, 403, 'INTAKE_CUSTOMER_REQUIRED', '请使用本人客户账号发起售后。');
      }
      if (method === 'GET' && path === '/api/intake/capabilities') return json(route, {rules: true, model: false});
      if (method === 'POST' && path === '/api/intake/preview') {
        const order = intakeOrders.find((item) => item.id === body?.order_id) ?? null;
        const intent = body?.intent ?? null;
        const required = [...(order ? [] : ['order']), ...(intent ? [] : ['intent'])];
        return json(route, {
          status: required.length ? 'CLARIFICATION_REQUIRED' : 'READY', required, order,
          order_candidates: intakeOrders, intent, intent_candidates: [],
          classifier_source: intent ? 'confirmed' : 'rules',
          reason: !order ? 'ORDER_REQUIRED' : !intent ? 'INTENT_REQUIRED' : null,
        });
      }
      if (method === 'POST' && path === '/api/intake/confirm') {
        const key = headers['idempotency-key'];
        state.confirmed.push({body, key});
        if (!key || body?.confirmed !== true) return denied(route, 422, 'CONFIRMATION_REQUIRED', '请确认工单信息。');
        if (!state.created) {
          state.created = {...originalCase, id: newCaseId, ticket_id: 'FIXTURE-NEW-CASE', description: body.message};
          return route.abort('failed');
        }
        if (key !== state.confirmed[0].key) return denied(route, 409, 'IDEMPOTENCY_CONFLICT', '请求编号发生变化。');
        return json(route, state.created, 201);
      }

      if (path.startsWith('/api/evaluation/') && role !== 'tester') {
        return denied(route, 403, 'EVALUATION_FORBIDDEN', '评测工作台仅供评测人员使用。');
      }
      if (method === 'GET' && path === '/api/evaluation/capabilities') {
        if (state.blindCapabilityError) return denied(route, 503, 'CAPABILITY_UNAVAILABLE', '评测服务暂时不可用。');
        return json(route, {eval_import: false, eval_retry: false, eval_gates: false,
          chat_blind_review: !!state.blindReview});
      }
      if (method === 'GET' && path === '/api/evaluation/campaigns') {
        return json(route, {items: state.officialReport ? [officialReport(state.officialReport)] : []});
      }
      if (method === 'GET' && path === '/api/evaluation/campaigns/tau-official-primary') {
        return json(route, officialReport('primary'));
      }
      if (method === 'GET' && path === '/api/evaluation/campaigns/tau-official-repair') {
        return json(route, officialReport('repair'));
      }
      if (state.blindReview && path.startsWith('/api/evaluation/chat-blind-reviews')) {
        const {task, candidates, posts} = state.blindReview;
        const base = '/api/evaluation/chat-blind-reviews';
        const detail = `${base}/${task.id}`;
        if (method === 'GET' && path === base) {
          const {id, version, input_sha256, case_label, status} = task;
          return json(route, {items: [{id, version, input_sha256, case_label, status}], next_after: null});
        }
        if (method === 'GET' && path === detail) return json(route, task);
        if (method === 'GET' && path === `${detail}/images/image-1`)
          return route.fulfill({status: 200, contentType: 'image/png', body: chatPhoto});
        if (method === 'POST') {
          const stage = path.slice(detail.length + 1);
          posts.push({stage, body, key: headers['idempotency-key']});
          if (body.expected_version !== task.version || state.blindReview.conflict)
            return denied(route, 409, 'BLIND_REVIEW_STAGE_CHANGED', '阶段已变化。');
          if (stage === 'initial' && task.status === 'BLIND') {
            const payload = {...body};
            delete payload.expected_version;
            task.initial = {sha256: 'i'.repeat(64), created_at: createdAt, payload};
            task.status = 'SEALED';
          } else if (stage === 'reveal' && task.status === 'SEALED') {
            task.exposure = {sha256: 'e'.repeat(64), created_at: createdAt, candidates};
            task.status = 'REVEALED';
          } else if (stage === 'labels' && task.status === 'REVEALED') {
            if (body.label_schema_version === 'chat-blind-labels-v2') {
              const expected = candidates.flatMap((candidate) => candidate.status === 'AVAILABLE' ?
                candidate.options.map((option) => JSON.stringify([candidate.id, option.id])) : []).sort();
              const supplied = body.option_labels?.map(({candidate_id, option_id}) =>
                JSON.stringify([candidate_id, option_id])).sort();
              if (JSON.stringify(supplied) !== JSON.stringify(expected))
                return denied(route, 422, 'BLIND_OPTION_LABELS_INVALID', '请逐项评价方案。');
            }
            task.labels = {sha256: 'l'.repeat(64), created_at: createdAt, payload: {
              items: body.items, ...(body.label_schema_version === 'chat-blind-labels-v2' ?
                {label_schema_version: body.label_schema_version, option_labels: body.option_labels} : {}),
            }};
            task.status = 'LABELED';
          } else return denied(route, 409, 'BLIND_REVIEW_STAGE_CHANGED', '阶段已变化。');
          task.version++;
          return json(route, task);
        }
        return denied(route, 404, 'BLIND_REVIEW_NOT_FOUND', '任务不存在。');
      }
      if (/^\/api\/evaluation\/(?:datasets|cases|experiments|imports|compare)(?:\/|$)/.test(path)) {
        return denied(route, 410, 'EVALUATION_RETIRED', '项目用例评测已退役，请查看官方评测。');
      }
      unexpected.push(`${method} ${path}`);
      return denied(route, 500, 'FIXTURE_MISSING', '该请求没有测试替身。');
    });
    await use(state);
    expect(unexpected).toEqual([]);
  },
});

export async function signIn(page, role, location = '/') {
  await page.goto(location);
  const labels = {customer: '客户', support: '客服', approver: '财务审批人', tester: '测试员'};
  await page.getByRole('button', {name: `以${labels[role]}身份进入`}).click();
  await expect(page.getByText(users[role].display_name, {exact: true})).toBeVisible();
}
