import { useState } from 'react';
import { Alert, Button, Card, Descriptions, Form, Input, Modal, Select, Space } from 'antd';
import type { Client } from './api';
import type { CaseDetail, User } from './contracts';
import { productName } from './OrderFacts';
import { useActions } from './hooks';
import { Feedback, label } from './ui';

type FactField = {key: string; name: string; options: string[]};
const fulfillment: FactField[] = [
  {key: 'fulfillment_status', name: '交运状态', options: ['UNSHIPPED', 'SHIPPED', 'UNVERIFIED']},
  {key: 'interception_status', name: '物流拦截', options: ['PENDING', 'CONFIRMED', 'FAILED', 'UNVERIFIED']},
];
const returns: FactField[] = [
  {key: 'return_receipt_status', name: '退件签收', options: ['NOT_RECEIVED', 'RECEIVED']},
  {key: 'inspection_status', name: '退件验货', options: ['NOT_STARTED', 'PASSED', 'FAILED']},
];
const resolution: FactField[] = [{key: 'resolution_status', name: '部分退款处置决定', options: ['APPROVED', 'REJECTED']}];

type Props = {api: Client; detail: CaseDetail; user: User; base: string; refresh: () => void; canOperate: boolean};
export function CaseFacts({api, detail, user, base, refresh, canOperate}: Props) {
  const [version, setVersion] = useState<number>();
  const [notice, setNotice] = useState('');
  const intent = detail.case.intent;
  const options = user.role === 'support' ? intent === 'cancel' ? fulfillment : intent === 'return' ? returns : []
    : user.role === 'approver' && ['damaged', 'wrong_item', 'return'].includes(intent) ? resolution : [];
  if (!options.length) return null;
  const checkingReturns = user.role === 'support' && intent === 'return';
  const orderLines = detail.order.items.filter((item) => detail.case.line_ids.includes(item.line_id))
    .map((item) => ({value: item.line_id, label: productName(detail.order, item.product_id)}));
  const returnLines = detail.facts.return_lines as Record<string, Record<string, string>> | undefined;
  const current = options.map((item) => ({
    key: item.key, label: item.name, children: typeof detail.facts[item.key] === 'string' ? label(detail.facts[item.key] as string) : '未登记',
  }));
  const editable = !['EXECUTION_PENDING', 'EXECUTION_FAILED', 'RESOLVED'].includes(detail.case.status);
  return <Card title="业务事实核对">
    <div className="stack">
      {checkingReturns ? <Descriptions size="small" column={1} items={orderLines.map((line) => ({
        key: line.value, label: line.label, children: options.map((item) => `${item.name} ${label(returnLines?.[line.value]?.[item.key])}`).join(' · '),
      }))} /> : <Descriptions size="small" column={1} items={current} />}
      <Feedback notice={notice} />
      <div><Button disabled={!canOperate || !editable} onClick={() => setVersion(detail.case.version)}>登记核对结果</Button></div>
    </div>
    {version !== undefined && <FactsEditor api={api} base={base} fields={options} lines={checkingReturns ? orderLines : undefined} version={version} stale={detail.case.version !== version}
      close={() => setVersion(undefined)} refresh={refresh} onSaved={() => {setNotice('已登记核对结果。'); setVersion(undefined);}} />}
  </Card>;
}

function FactsEditor({api, base, fields, lines, version, stale, close, refresh, onSaved}: {
  api: Client; base: string; fields: FactField[]; lines?: {value: string; label: string}[]; version: number; stale: boolean;
  close: () => void; refresh: () => void; onSaved: () => void;
}) {
  const actions = useActions(api);
  const [localError, setLocalError] = useState('');
  return <Modal title="登记业务事实" open footer={null} onCancel={actions.busy ? undefined : close}>
    <Form layout="vertical" onFinish={async (values: {facts?: Record<string, string>; notes: string; line_ids?: string[]}) => {
      if (stale) return;
      const facts = Object.fromEntries(Object.entries(values.facts ?? {}).filter(([, value]) => !!value));
      if (!Object.keys(facts).length) {setLocalError('请至少选择一项已核对的事实。'); return;}
      setLocalError('');
      const result = await actions.submit(`${base}/facts`, {facts, line_ids: values.line_ids?.length ? values.line_ids : undefined, notes: values.notes.trim(), expected_version: version},
        {success: '已登记业务事实', refresh});
      if (result) onSaved();
    }}>
      <p className="muted">只填写本次已核对的事项，并记录依据。</p>
      {lines && <Form.Item name="line_ids" label="涉及退件" extra="留空则适用于工单中的全部商品。"><Select mode="multiple" options={lines} /></Form.Item>}
      {fields.map((field) => <Form.Item key={field.key} label={field.name} name={['facts', field.key]}>
        <Select allowClear options={field.options.map((value) => ({value, label: label(value)}))} />
      </Form.Item>)}
      <Form.Item label="核对依据" name="notes" rules={[{required: true, whitespace: true, message: '请填写核对依据'}]}>
        <Input.TextArea rows={3} maxLength={2000} />
      </Form.Item>
      <Space orientation="vertical" className="full">
        {stale && <Alert type="warning" showIcon title="工单状态已更新，请关闭后核对最新记录。" />}
        <Feedback error={localError || actions.error} />
        <Button type="primary" htmlType="submit" loading={actions.busy} disabled={stale}>保存事实</Button>
      </Space>
    </Form>
  </Modal>;
}
