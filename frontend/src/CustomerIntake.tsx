import { useRef, useState } from 'react';
import { Alert, Button, Descriptions, Form, Input, Modal, Select, Space, Tag } from 'antd';
import { errorMessage, type Client } from './api';
import type { Case, IntakeClassifier, IntakeIntent, IntakePreview, Order } from './contracts';
import {
  intakeConfirmRequest, intakeInputKey, intakeIntents, intakeMessage, intakePreviewRequest,
  intakeSource, type BoundIntakePreview, type IntakeDraft,
} from './intake';
import { useActions } from './hooks';
import { productName } from './OrderFacts';
import { Feedback, intents, label } from './ui';

type Props = {
  api: Client;
  orders: Order[];
  modelAvailable: boolean;
  close: () => void;
  onStructured: (description: string) => void;
  onCreated: (id: string) => void;
};

export function CustomerIntake({api, orders, modelAvailable, close, onStructured, onCreated}: Props) {
  const [draft, setDraft] = useState<IntakeDraft>({message: ''});
  const [preview, setPreview] = useState<BoundIntakePreview>();
  const [reading, setReading] = useState(false);
  const [ruleError, setRuleError] = useState('');
  const [lastAction, setLastAction] = useState<'rules' | 'model' | 'confirm'>('rules');
  const active = useRef(false);
  const model = useActions(api);
  const confirm = useActions(api);
  const busy = reading || model.busy || confirm.busy;
  const current = preview?.inputKey === intakeInputKey(draft) ? preview.result : undefined;
  const response = preview?.result;
  const confirmation = intakeConfirmRequest(draft, preview);
  const candidateOrders = [...new Map([...(response?.order_candidates ?? []), ...(response?.order ? [response.order] : [])]
    .map((order) => [order.id, order])).values()];
  const validMessage = draft.message.trim().length >= 5;
  const mayUseModel = !!current?.order && current.required.includes('intent') && !draft.intent
    && modelAvailable && current.classifier_source !== 'model_unavailable';

  function updateDetails(patch: Partial<IntakeDraft>) {
    setDraft({...draft, ...patch});
    setLastAction('rules');
    setRuleError('');
  }

  async function previewNow(classifier: IntakeClassifier) {
    if (active.current || !validMessage || (classifier === 'model' && !mayUseModel)) return;
    const requestDraft = classifier === 'model' && current?.order ? {...draft, orderId: current.order.id} : draft;
    const request = intakePreviewRequest(requestDraft, classifier);
    active.current = true;
    setLastAction(classifier);
    setRuleError('');
    if (classifier === 'rules') setReading(true);
    try {
      const next = classifier === 'model'
        ? await model.submit<IntakePreview>('/intake/preview', request, {success: ''})
        : await api.post<IntakePreview>('/intake/preview', request);
      if (next) setPreview({inputKey: intakeInputKey(draft), result: next});
    } catch (error) {
      setRuleError(errorMessage(error));
    } finally {
      active.current = false;
      setReading(false);
    }
  }

  async function createCase() {
    if (active.current || !confirmation) return;
    active.current = true;
    setLastAction('confirm');
    try {
      const result = await confirm.submit<Case>('/intake/confirm', confirmation, {
        success: '工单已提交',
        refresh: () => setPreview(undefined),
      });
      if (result) onCreated(result.id);
    } finally {
      active.current = false;
    }
  }

  function candidateLabel(order: (typeof candidateOrders)[number]) {
    const snapshot = orders.find((item) => item.id === order.id);
    const items = snapshot?.items.map((item) => productName(snapshot, item.product_id)).join('、');
    return [order.id, items, label(order.status)].filter(Boolean).join(' · ');
  }

  const error = lastAction === 'confirm' ? confirm.error : lastAction === 'model' ? model.error : ruleError;
  const clarification = intakeMessage(current?.reason);
  return <Modal title="描述问题发起售后" open footer={null} onCancel={busy ? undefined : close} width={620}>
    <div className="stack">
      <Form layout="vertical" onFinish={() => {if (!confirmation) void previewNow('rules');}}>
        <Form.Item label="遇到了什么问题" htmlFor="intake-message" required extra="请至少输入 5 个字。">
          <Input.TextArea id="intake-message" rows={4} showCount maxLength={2000} value={draft.message} disabled={busy}
            placeholder="例如，收到商品有破损，外包装也坏了。"
            onChange={(event) => {
              setDraft({message: event.target.value});
              setPreview(undefined);
              setLastAction('rules');
              setRuleError('');
            }} />
        </Form.Item>
        {response && <div className="form-grid">
          <Form.Item label="本人订单" htmlFor="intake-order">
            <Select id="intake-order" disabled={busy || !candidateOrders.length} placeholder="请选择本人订单"
              value={draft.orderId ?? response.order?.id}
              options={candidateOrders.map((order) => ({value: order.id, label: candidateLabel(order)}))}
              onChange={(orderId) => updateDetails({orderId: orderId ?? undefined})} />
          </Form.Item>
          <Form.Item label="售后类型" htmlFor="intake-intent">
            <Select id="intake-intent" disabled={busy} placeholder="请选择售后类型"
              value={draft.intent ?? response.intent ?? undefined}
              options={intakeIntents.map((value) => ({value, label: intents[value]}))}
              onChange={(intent: IntakeIntent | undefined) => updateDetails({intent})} />
          </Form.Item>
        </div>}
        <div className="stack">
          {current && <div><Tag color={current.classifier_source === 'model_unavailable' ? 'gold' : 'blue'}>{intakeSource(current.classifier_source)}</Tag></div>}
          {clarification && <Alert type="info" showIcon title={clarification} />}
          {!current && response && <p className="muted">选择已更新，请重新核对后提交。</p>}
          {confirmation && current?.order && <CardConfirmation orderId={confirmation.order_id}
            intent={confirmation.intent} status={current.order.status} />}
          <Feedback error={error} />
          <Space wrap>
            {confirmation ? <Button type="primary" onClick={() => void createCase()} loading={confirm.busy} disabled={busy}>确认并建单</Button>
              : <Button type="primary" htmlType="submit" loading={reading} disabled={busy || !validMessage}>{response ? '核对订单和售后类型' : '识别售后需求'}</Button>}
            {mayUseModel && <Button onClick={() => void previewNow('model')} loading={model.busy} disabled={busy}>请模型帮我判断</Button>}
            <Button type="link" onClick={() => onStructured(draft.message)} disabled={busy}>改用售后表单</Button>
          </Space>
        </div>
      </Form>
    </div>
  </Modal>;
}

function CardConfirmation({orderId, intent, status}: {orderId: string; intent: IntakeIntent; status: string}) {
  return <div className="stack">
    <h3>确认提交信息</h3>
    <Descriptions size="small" column={1} items={[
      {key: 'order', label: '订单编号', children: orderId},
      {key: 'intent', label: '售后类型', children: intents[intent]},
      {key: 'status', label: '订单状态', children: label(status)},
    ]} />
    <p className="muted">确认后会按上方描述创建售后工单。</p>
  </div>;
}
