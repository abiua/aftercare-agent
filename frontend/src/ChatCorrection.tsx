import { useState, type ReactNode } from 'react';
import { Button, Card, Checkbox, Input } from 'antd';
import type { ChatMemory } from './chat';

export function ChatCorrection({memory, revision, busy, confirm, photo}: {
  memory: ChatMemory; revision: number; busy: boolean;
  confirm: (body: object) => Promise<boolean>;
  photo: (attachment: ChatMemory['recent_attachments'][number]) => ReactNode;
}) {
  const [retained, setRetained] = useState<string[]>([]);
  const [reason, setReason] = useState('');
  if (!memory.correction) return null;
  const previousPhotos = memory.recent_attachments.filter((item) => item.sequence < memory.correction!.sequence);
  const selected = retained.filter((id) => previousPhotos.some((item) => item.attachment_id === id));
  return <Card size="small" title="确认客户更正">
    <p>客户更正：{memory.correction.text}</p>
    <p>请核对更正仍属于订单 {memory.confirmed_context.order_id ?? '待确认'} 的本次售后。确认后沿原工单重新核查。</p>
    {!!previousPhotos.length && <>
      <p>选择本次核查仍适用的已有照片：</p>
      {previousPhotos.map((item) => <div key={item.attachment_id}>
        <Checkbox disabled={busy} checked={selected.includes(item.attachment_id)}
          onChange={(event) => setRetained((old) => event.target.checked ? [...old, item.attachment_id] : old.filter((id) => id !== item.attachment_id))}>
          {item.filename || '聊天照片'}
        </Checkbox>
        {photo(item)}
      </div>)}
    </>}
    {memory.has_more_attachments && <p>这里只列出近期照片，请先核对原会话记录。</p>}
    <Input.TextArea aria-label="照片复用理由" placeholder="如保留已有照片，请说明仍适用的原因"
      value={reason} maxLength={1000} disabled={busy} onChange={(event) => setReason(event.target.value)} />
    <Button disabled={busy || (selected.length > 0 && !reason.trim())} loading={busy}
      onClick={() => void confirm({message_id: memory.correction?.message_id, expected_revision: revision,
        retained_attachment_ids: selected, evidence_reason: reason.trim()})}>确认更正并继续核查</Button>
  </Card>;
}
