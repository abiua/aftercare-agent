import { ChatDecisions } from './ChatDecisions';
import { useCallback, useEffect, useRef, useState } from 'react';
import { Button, Card, Checkbox, Input, Modal, Space, Tag } from 'antd';
import { ApiError, errorMessage, segment, type Client } from './api';
import { analysisStatus, CHAT_PAGE_SIZE, chatMessageBody, isCurrentEvidenceRequest, mergeChatAnalysis, mergeChatMessages, mergeExecutionProgress, validChatFiles, type ChatAttachment, type ChatCard, type ChatExecutionProgress, type ChatPage, type ChatUpdate, type Conversation, type OrderCandidate } from './chat';
import type { Items, User } from './contracts';
import { useActions, useResource } from './hooks';
import { Feedback, money, time } from './ui';

const statuses = {OPEN: '对话中', HUMAN_HANDOFF: '人工处理', CLOSED: '已结束'};
const listSize = 20;
type Outgoing = {id: string; text: string; files: File[]};
type ComposeDraft = {text: string; files: File[]; error: string; outgoing?: Outgoing};
const emptyDraft: ComposeDraft = {text: '', files: [], error: ''};
const refundStatus: Record<string, string> = {SUCCEEDED: '模拟退款执行成功', UNKNOWN: '状态待核对', SUBMITTING: '处理中', FAILED: '模拟退款执行失败'};
function ExecutionProgress({progress}: {progress: ChatExecutionProgress}) {
  const action = progress.action === 'REFUND' ? '退款' : '换发';
  const status = progress.status === 'SUBMITTING' ? '等待回执' :
    progress.receipt_confirmed && progress.status === 'SUCCEEDED' ? '执行成功' :
    progress.receipt_confirmed && progress.status === 'FAILED' ? '执行失败' : '结果待核对';
  return <Card size="small" title="本次售后执行进度">
    <p>模拟{action} · {status}</p>
    <p>{progress.action === 'REFUND' ? money(progress.amount_cents) :
      `${progress.lines.reduce((total, line) => total + (line.quantity ?? 0), 0)} 件`}</p>
    <p className="muted">执行编号 {progress.id} · 更新于 {time(progress.updated_at)}</p>
  </Card>;
}

function refundLabel(status: string, receiptConfirmed?: boolean) {
  if (['SUCCEEDED', 'FAILED'].includes(status) && receiptConfirmed !== true) return '状态待核对';
  return refundStatus[status] ?? '状态待核对';
}

export function CustomerChat({api, user, onCase}: {api: Client; user: User; onCase: (id: string) => void}) {
  const [id, setId] = useState(() => new URLSearchParams(location.search).get('conversation') || '');
  const [offset, setOffset] = useState(0);
  const [drafts] = useState(() => new Map<string, ComposeDraft>());
  const listing = useResource<Items<Conversation>>(api, `/conversations?limit=${listSize}&offset=${offset}`);
  const {refresh} = listing;
  const create = useActions(api);
  useEffect(() => {
    const timer = setInterval(refresh, 8000);
    return () => clearInterval(timer);
  }, [refresh]);
  function select(next: string) {
    const url = new URL(location.href);
    url.searchParams.delete('case');
    if (next) url.searchParams.set('conversation', next);
    else url.searchParams.delete('conversation');
    history.replaceState(null, '', url);
    setId(next);
  }
  async function start() {
    const created = await create.submit<Conversation>('/conversations', {}, {success: ''});
    if (created) {
      setOffset(0);
      refresh();
      select(created.id);
    }
  }
  const rows = listing.value?.items ?? [];
  return <div className="chat-layout">
    <Card title="售后对话" extra={user.role === 'customer' && <Button type="primary" loading={create.busy} onClick={() => void start()}>新对话</Button>}>
      <Feedback error={listing.error || create.error} />
      {!rows.length && <p className="muted">{listing.loading ? '正在读取对话…' : '暂无对话'}</p>}
      <div className="stack chat-list">{rows.map((row) => <Button key={row.id} type={id === row.id ? 'primary' : 'default'} onClick={() => select(row.id)}>
        {row.active_order_id ? `订单 ${row.active_order_id}` : `对话 · ${time(row.created_at)}`} · {statuses[row.status]}
      </Button>)}</div>
      <Space className="chat-actions">
        <Button disabled={offset === 0 || listing.loading} onClick={() => setOffset(offset - listSize)}>上一页</Button>
        <Button disabled={rows.length < listSize || listing.loading} onClick={() => setOffset(offset + listSize)}>下一页</Button>
        <Button onClick={refresh}>刷新列表</Button>
      </Space>
    </Card>
    {id ? <ChatThread key={id} api={api} id={id} user={user} drafts={drafts} refreshList={refresh} onCase={onCase} />
      : <Card><h1>{user.role === 'customer' ? '你好，需要什么售后帮助？' : '客户售后对话'}</h1><p>{user.role === 'customer' ? '开启对话，描述问题。你可以在同一对话补充文字和照片。' : '选择一条对话查看客户消息，并发送公开回复。'}</p></Card>}
  </div>;
}

function ChatThread({api, id, user, drafts, refreshList, onCase}: {api: Client; id: string; user: User; drafts: Map<string, ComposeDraft>; refreshList: () => void; onCase: (id: string) => void}) {
  const base = `/conversations/${segment(id)}`;
  const [state, setState] = useState<ChatUpdate & {analysisRead: ReturnType<typeof mergeChatAnalysis>; executionRead: ReturnType<typeof mergeExecutionProgress>}>();
  const [more, setMore] = useState(false);
  const [readError, setReadError] = useState('');
  const [reading, setReading] = useState(false);
  const [draft, setDraft] = useState(() => drafts.get(id) ?? emptyDraft);
  const {text, files, error, outgoing} = draft;
  function save(next: ComposeDraft) {
    drafts.set(id, next);
    setDraft(next);
  }
  const retry = !!outgoing && outgoing.text === text &&
    outgoing.files.length === files.length && files.every((file, i) => file === outgoing.files[i]);
  const [sending, setSending] = useState(false);
  const sendLock = useRef(false);
  const denied = useRef(false);
  const read = useRef<AbortController | null>(null);
  const cursor = useRef(0);
  const requestNumber = useRef(0);
  const alive = useRef(false);
  const controls = useActions(api);
  const merge = useCallback((update: ChatUpdate, request: number) => setState((old) => ({
    conversation: !old || update.conversation.revision >= old.conversation.revision ? update.conversation : old.conversation,
    features: !old || update.conversation.revision >= old.conversation.revision ? update.features : old.features,
    messages: mergeChatMessages(old?.messages ?? [], update.messages),
    executionRead: mergeExecutionProgress(old?.executionRead, update, request),
    analysisRead: mergeChatAnalysis(old?.analysisRead, {
      revision: update.conversation.revision, request, value: update.analysis,
    }),
  })), []);
  const load = useCallback(async () => {
    if (read.current) return;
    const controller = new AbortController();
    const request = ++requestNumber.current;
    read.current = controller;
    setReading(true);
    try {
      const page = await api.get<ChatPage>(`${base}?after=${cursor.current}&limit=${CHAT_PAGE_SIZE}`, controller.signal);
      if (controller.signal.aborted) return;
      denied.current = false;
      cursor.current = page.next_after;
      merge(page, request);
      setMore(page.has_more);
      setReadError('');
    } catch (failure) {
      if (controller.signal.aborted) return;
      if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) {
        denied.current = true;
        setState(undefined);
        cursor.current = 0;
      }
      setReadError(errorMessage(failure));
    } finally {
      if (read.current === controller) {
        read.current = null;
        setReading(false);
      }
    }
  }, [api, base, merge]);
  useEffect(() => {
    alive.current = true;
    void load();
    return () => {
      alive.current = false;
      read.current?.abort();
      read.current = null;
    };
  }, [load]);
  useEffect(() => {
    if (more) return;
    const timer = setInterval(() => void load(), 4000);
    return () => clearInterval(timer);
  }, [load, more]);

  async function send() {
    if (sendLock.current || !state || readError || state.conversation.status === 'CLOSED') return;
    const message = text;
    if ((!message.trim() && !files.length) || !validChatFiles(files)) return;
    const item = retry && outgoing ? outgoing : {id: crypto.randomUUID(), text: message, files};
    const request = ++requestNumber.current;
    sendLock.current = true;
    setSending(true);
    save({...draft, outgoing: item, error: ''});
    try {
      const result = await api.post<ChatUpdate>(`${base}/messages`, chatMessageBody(item.id, item.text, item.files), item.id);
      if (drafts.get(id)?.outgoing === item) drafts.delete(id);
      if (!alive.current || denied.current) return;
      merge(result, request);
      setDraft(emptyDraft);
      refreshList();
      void load();
    } catch (failure) {
      const failed = {...draft, outgoing: item, error: errorMessage(failure)};
      if (drafts.get(id)?.outgoing === item) drafts.set(id, failed);
      if (alive.current) {
        setDraft(failed);
        if (failure instanceof ApiError && [401, 403, 404].includes(failure.status)) void load();
      }
    } finally {
      sendLock.current = false;
      if (alive.current) setSending(false);
    }
  }
  async function act(path: string, body: object) {
    if (readError || sendLock.current) return false;
    const request = ++requestNumber.current;
    const result = await controls.submit<ChatUpdate>(`${base}/${path}`, body, {success: '', refresh: () => void load()});
    if (!result || !alive.current || denied.current) return false;
    merge(result, request);
    refreshList();
    return true;
  }

  const conversation = state?.conversation;
  const analysis = state?.analysisRead.value;
  const statusText = analysisStatus(analysis);
  const progress = state?.executionRead.value;
  const attachments = new Map(state?.messages.flatMap((row) => row.attachments.map((attachment) =>
    [attachment.id, attachment.filename] as const)));
  const latestOrderPrompt = state?.messages.slice().reverse().find((row) => row.cards.some((card) => card.type === 'order_candidates'))?.sequence;
  const [resolvedPrompt, setResolvedPrompt] = useState(0);
  const hasLinkedCase = state?.messages.some((row) => row.sequence > (latestOrderPrompt ?? 0) && row.cards.some((card) => card.type === 'case_link' || (card.type === 'status' && card.data.code === 'ORDER_CONFIRMED'))) ?? false;
  const blocked = !conversation || !!readError || conversation.status === 'CLOSED';
  const sendLabel = retry && error ? '重试发送' : user.role === 'support' ? '发送公开回复' : '发送消息';
  return <Card className="chat-thread" title={user.role === 'support' ? '客户对话 · 公开回复' : '售后聊天'} extra={<Space wrap>
    {conversation && <Tag>{statuses[conversation.status]}</Tag>}
    {conversation?.active_order_id && <Tag>订单 {conversation.active_order_id}</Tag>}
    <Button loading={reading} onClick={() => void load()}>{more ? '加载更多消息' : '刷新消息'}</Button>
  </Space>}>
    <Feedback error={readError || controls.error} />
    {statusText && <p role="status" className="chat-analysis-status">{statusText}</p>}
    <div className="chat-messages" aria-label="对话消息">
      {state?.messages.map((row) => <article key={row.id} className={`chat-message chat-${row.role}`}>
        <div className="chat-byline">{row.role === 'customer' ? '客户' : row.role === 'support' ? '客服' : row.cards.some((card) => card.type === 'photo_observation') ? '售后助手（照片观察）' : '售后助手（程序回复）'} · {time(row.created_at)}</div>
        {row.text && <p>{row.text}</p>}
        {!!row.attachments.length && <div className="chat-photos">{row.attachments.map((attachment) =>
          <div key={attachment.id} id={`chat-photo-${attachment.id}`}><ChatPhoto api={api} base={base} attachment={attachment} /></div>)}</div>}
        {row.cards.map((card, index) => <MessageCard key={index} card={card} onCase={onCase}
          messageText={row.text}
          attachments={attachments}
          offerCannotProvide={card.type === 'evidence_request' && user.role === 'customer' && !blocked &&
            conversation?.status === 'OPEN' && isCurrentEvidenceRequest(analysis, card.data) &&
            !text.trim() && !files.length ? () => save({...draft, text: '暂时无法提供', error: ''}) : undefined}
          canSelect={user.role === 'customer' && !blocked && row.sequence === latestOrderPrompt && !hasLinkedCase && row.sequence > resolvedPrompt && !more}
          choose={async (order, lines, newIssue) => {
            const done = await act('context', {order_id: order, line_ids: lines, expected_revision: conversation?.revision, new_issue: newIssue});
            if (done) setResolvedPrompt(row.sequence);
            return done;
          }}
          currentOrder={conversation?.active_order_id} busy={sending || controls.busy} />)}
      </article>)}
      {conversation && !state?.messages.length && <p className="muted">发送一条消息开始对话。</p>}
    </div>
    {conversation && !readError && state?.features?.chat_decisions === true &&
      <ChatDecisions key={JSON.stringify([conversation.revision, conversation.active_order_id, conversation.active_case_id, analysis?.issue_id])} api={api} base={base} user={user} orderId={conversation.active_order_id}
        issueId={analysis?.issue_id} revision={conversation.revision} refreshChat={load} />}
    {conversation && !readError && progress?.case_id === conversation.active_case_id &&
      progress?.simulated === true && <ExecutionProgress progress={progress} />}
    {conversation && <div className="chat-compose">
      <Input.TextArea aria-label={user.role === 'support' ? '公开回复' : '描述售后问题'} autoSize={{minRows: 2, maxRows: 5}}
        value={text} maxLength={2000} showCount disabled={blocked || sending} onChange={(event) => save({...draft, text: event.target.value, error: ''})}
        placeholder={user.role === 'support' ? '输入客户可见的回复' : '描述问题或继续补充'} />
      <label className="chat-file-label">添加照片（最多 6 张，共 8 MiB）
        <input type="file" aria-label="添加照片" accept="image/png,image/jpeg,image/webp" multiple disabled={blocked || sending}
          onChange={(event) => {
            const selected = [...files, ...Array.from(event.target.files ?? [])];
            event.target.value = '';
            if (!validChatFiles(selected)) {save({...draft, error: '最多可添加 6 张照片，总大小不能超过 8 MiB。'}); return;}
            save({...draft, files: selected, error: ''});
          }} />
      </label>
      <Space wrap>{files.map((file, index) => <Tag key={index} closable={!sending && !blocked} onClose={() => save({...draft, files: files.filter((_, i) => i !== index), error: ''})}>{file.name}</Tag>)}</Space>
      <Feedback error={error} />
      <Space wrap>
        <Button type="primary" aria-label={sendLabel} aria-busy={sending} disabled={blocked || sending || controls.busy || (!text.trim() && !files.length)} onClick={() => void send()}>
          {sending ? '发送中…' : sendLabel}
        </Button>
        {conversation.status === 'OPEN' && <Button disabled={blocked || sending || controls.busy} onClick={() => void act('handoff', {expected_revision: conversation.revision})}>{user.role === 'support' ? '接管对话' : '转人工客服'}</Button>}
      </Space>
    </div>}
  </Card>;
}

function MessageCard({card, onCase, messageText, attachments, offerCannotProvide, canSelect, choose, currentOrder, busy}: {
  card: ChatCard; onCase: (id: string) => void; canSelect: boolean;
  messageText: string; offerCannotProvide?: () => void;
  attachments: Map<string, string>;
  choose: (order: string, lines: string[], newIssue: boolean) => Promise<boolean>;
  currentOrder?: string | null; busy: boolean;
}) {
  if (card.type === 'case_link') {
    return <Button onClick={() => onCase(card.data.case_id)}>查看订单 {card.data.order_id} 的工单</Button>;
  }
  if (card.type === 'status') {
    return card.data.refunds?.length ? <ul className="chat-refunds">{card.data.refunds.map((row) =>
      <li key={row.id}>{money(row.amount_cents)} · {refundLabel(row.status, row.receipt_confirmed)}<br />退款执行编号 {row.id}</li>)}</ul> : null;
  }
  if (card.type === 'photo_observation') {
    return <div className="chat-observation">
      <strong>照片观察 · {card.data.certainty === 'OBSERVED' ? '仍需核对' : '暂不能确定'}</strong>
      <p>{card.data.summary}</p>
      <Space wrap>{card.data.attachment_refs.flatMap((id) => {
        const filename = attachments.get(id);
        return filename ? [<a key={id} href={`#chat-photo-${segment(id)}`}>参照照片 · {filename}</a>] : [];
      })}</Space>
    </div>;
  }
  if (card.type === 'evidence_request') {
    return <div className="chat-observation">
      {messageText !== card.data.prompt && <p>{card.data.prompt}</p>}
      {!!card.data.alternatives.length && <p className="muted">可以选择：{card.data.alternatives.join('、')}</p>}
      {offerCannotProvide && card.data.alternatives.includes('暂时无法提供') &&
        <Button disabled={busy} onClick={offerCannotProvide}>暂时无法提供</Button>}
    </div>;
  }
  if (card.type !== 'order_candidates') return null;
  return <div className="chat-orders">{card.data.orders.map((order) => <OrderChoice key={order.id} order={order} disabled={!canSelect || busy}
    newIssue={card.data.new_issue || !!(currentOrder && currentOrder !== order.id)}
    choose={choose} />)}</div>;
}

function OrderChoice({order, disabled, newIssue, choose}: {
  order: OrderCandidate; disabled: boolean; newIssue: boolean;
  choose: (order: string, lines: string[], newIssue: boolean) => Promise<boolean>;
}) {
  const [lines, setLines] = useState<string[]>(() => order.items.length === 1 ? [order.items[0].line_id] : []);
  const [confirm, setConfirm] = useState(false);
  async function submit() {
    if (await choose(order.id, lines, newIssue)) setConfirm(false);
  }
  return <div className="chat-order">
    <strong>订单 {order.id}</strong>
    {order.items.map((item) => <Checkbox key={item.line_id} checked={lines.includes(item.line_id)} disabled={disabled}
      onChange={(e) => setLines((old) => e.target.checked ? [...old, item.line_id] : old.filter((line) => line !== item.line_id))}>
      {order.products?.find((product) => product.id === item.product_id)?.name ?? `商品 ${item.product_id}`} × {item.quantity} · {money(item.paid_cents)}
    </Checkbox>)}
    {(!disabled || confirm) && <Button disabled={disabled || !lines.length} onClick={() => newIssue ? setConfirm(true) : void submit()}>确认订单和商品</Button>}
    {confirm && <Modal open title="确认开始另一项售后" okText="确认开始" cancelText="返回" okButtonProps={{disabled}} onOk={() => void submit()} onCancel={() => setConfirm(false)}>
      <p>本次选择订单 {order.id}。原对话和照片仍可查看，新事项会使用你本次选择的商品。</p>
    </Modal>}
  </div>;
}

function ChatPhoto({api, base, attachment}: {api: Client; base: string; attachment: ChatAttachment}) {
  const [source, setSource] = useState('');
  const [error, setError] = useState('');
  const [loading, setLoading] = useState(false);
  const request = useRef<AbortController | null>(null);
  const image = useRef('');
  useEffect(() => () => {
    request.current?.abort();
    if (image.current) URL.revokeObjectURL(image.current);
  }, []);
  async function open() {
    if (request.current || image.current) return;
    const controller = new AbortController();
    request.current = controller;
    setLoading(true);
    setError('');
    try {
      const blob = await api.blob(`${base}/attachments/${segment(attachment.id)}/content`, controller.signal);
      if (controller.signal.aborted) return;
      image.current = URL.createObjectURL(blob);
      setSource(image.current);
    } catch (failure) {
      if (!controller.signal.aborted) setError(errorMessage(failure));
    } finally {
      if (!controller.signal.aborted) {
        request.current = null;
        setLoading(false);
      }
    }
  }
  return <div>{source ? <img src={source} alt={attachment.filename} /> : <Button loading={loading} onClick={() => void open()}>查看照片 · {attachment.filename}</Button>}
    <Feedback error={error} />
  </div>;
}
