import { useState, type ReactNode } from 'react';
import { Button, Card, Modal, Radio, Space, Tag } from 'antd';
import type { Client } from './api';
import { materialLabels } from './casePresentation';
import { useActions, useResource } from './hooks';
import { Feedback } from './ui';

const kinds = ['ITEM', 'PARCEL', 'WRONG_ITEM', 'LABEL'] as const;
type Kind = typeof kinds[number];
type Attachment = {
  attachment_id: string; message_id: string; sha256: string; content_type: string; size: number;
  suggested_kind: Kind | null; material_id: string | null; confirmed_kind: Kind | null;
};
type Page = {issue_id: string; case_id: string; materials_version: number; items: Attachment[]; next_after: number | null};
type Props = {
  api: Client; base: string; caseId: string; onCase: (id: string) => void;
  photo: (attachment: Attachment, onLoaded?: () => void) => ReactNode;
};

export function ChatMaterialBridge({api, base, caseId, onCase, photo}: Props) {
  const path = `${base}/decision-materials`;
  const [open, setOpen] = useState(false);
  const [cursors, setCursors] = useState([0]);
  const [selection, setSelection] = useState<{attachment: Attachment; page: Page}>();
  const [kind, setKind] = useState<Kind>();
  const [previewed, setPreviewed] = useState(false);
  const resource = useResource<Page>(api, open ? `${path}?after=${cursors.at(-1)}&limit=50` : null);
  const actions = useActions(api);
  const page = !resource.error && resource.value?.case_id === caseId ? resource.value : undefined;
  const stale = !!selection && (!page || selection.page.issue_id !== page.issue_id ||
    selection.page.materials_version !== page.materials_version || !page.items.some((item) =>
      item.attachment_id === selection.attachment.attachment_id && !item.material_id));
  async function confirm() {
    if (!selection || stale || !kind || !previewed) return;
    const associated = await actions.submit(path, {
      attachment_id: selection.attachment.attachment_id, kind,
      expected_issue_id: selection.page.issue_id, expected_case_id: caseId,
      expected_materials_version: selection.page.materials_version,
    }, {success: '已关联工单。请到工单材料页完成审核。', refresh: resource.refresh});
    if (associated) setSelection(undefined);
  }
  return <Card size="small" title="聊天照片与工单材料" extra={<Button onClick={() => setOpen(!open)}>{open ? '收起' : '查看可关联照片'}</Button>}>
    {open && <>
      <p className="muted">核对聊天原图后，可将它关联到当前工单。关联后仍需单独审核。</p>
      <Feedback error={resource.error} notice={actions.notice} />
      {page?.items.length === 0 && <p>当前事项没有可关联的聊天照片。</p>}
      {page?.items.map((attachment) => <Card key={attachment.attachment_id} size="small">
        {photo(attachment)}
        {attachment.material_id ? <Tag>已关联 · {attachment.confirmed_kind ? materialLabels[attachment.confirmed_kind] : '材料类型未记录'}</Tag> :
          <Button disabled={resource.loading} onClick={() => {setKind(undefined); setPreviewed(false); setSelection({attachment, page});}}>核对这张照片</Button>}
      </Card>)}
      <Space wrap>
        <Button disabled={cursors.length <= 1 || resource.loading} onClick={() => setCursors(cursors.slice(0, -1))}>上一批照片</Button>
        <Button disabled={page?.next_after == null || resource.loading}
          onClick={() => {if (page?.next_after != null) setCursors([...cursors, page.next_after]);}}>下一批照片</Button>
        <Button disabled={resource.loading} onClick={resource.refresh}>刷新照片列表</Button>
        <Button onClick={() => onCase(caseId)}>查看工单材料</Button>
      </Space>
    </>}
    {selection && <Modal open title="确认照片关联" okText="关联到工单" cancelText="返回"
      onOk={() => void confirm()} onCancel={() => {if (!actions.busy) setSelection(undefined);}}
      okButtonProps={{disabled: stale || !kind || !previewed || actions.busy}}>
      <Space orientation="vertical" style={{width: '100%'}}>
        {photo(selection.attachment, () => setPreviewed(true))}
        {selection.attachment.suggested_kind && <p>照片观察建议 · {materialLabels[selection.attachment.suggested_kind]}。请查看原图后确认。</p>}
        <fieldset><legend>确认材料类型</legend>
          <Radio.Group value={kind} onChange={(event) => setKind(event.target.value)}
            options={kinds.map((value) => ({value, label: materialLabels[value]}))} />
        </fieldset>
        {stale && <p>当前事项或工单材料已更新。请关闭并重新选择照片。</p>}
        <Feedback error={actions.error} />
      </Space>
    </Modal>}
  </Card>;
}
