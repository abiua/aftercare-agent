import { useEffect, useState } from 'react';
import { Button, Card, Form, Input, Modal, Select, Space, Table, Tag } from 'antd';
import { InboxOutlined, PlusOutlined, ProfileOutlined } from '@ant-design/icons';
import { segment, type Client } from './api';
import type { Case, Intent, Items, Order, Runtime, User } from './contracts';
import { useActions, useResource } from './hooks';
import { CaseWorkspace } from './CaseWorkspace';
import { OrderFacts, productName } from './OrderFacts';
import { Feedback, intents, label, money, Placeholder, Reload, Status, time } from './ui';

export function Workbench({api, user, runtime}: {api: Client; user: User; runtime?: Runtime}) {
  const [selected, setSelected] = useState(() => caseInUrl());
  useEffect(() => {
    const update = () => setSelected(caseInUrl());
    window.addEventListener('popstate', update);
    return () => window.removeEventListener('popstate', update);
  }, []);
  function navigate(id?: string) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set('case', id);
    else url.searchParams.delete('case');
    history.pushState(null, '', url);
    setSelected(id);
  }
  const [view, setView] = useState<'cases' | 'orders'>('cases');
  const [order, setOrder] = useState<Order>();
  const [create, setCreate] = useState<string>();
  const [search, setSearch] = useState('');
  const cases = useResource<Items<Case>>(api, '/cases?limit=300');
  const orders = useResource<Items<Order>>(api, '/orders?limit=300');
  const canCreate = user.role === 'support';
  const caseRows = cases.value?.items ?? [];
  const orderRows = orders.value?.items ?? [];

  if (selected) {
    return <CaseWorkspace key={selected} api={api} user={user} runtime={runtime} id={selected} onBack={() => {navigate(); cases.refresh();}} />;
  }

  const needle = search.trim().toLowerCase();
  return <div className="stack">
    <section className="hero">
      <div><div className="eyebrow">售后服务 / 工作台</div><h1>订单与工单</h1><p>查看订单、跟进处理进度并处理待办事项。</p></div>
      {canCreate && <Space wrap>
        <Button type="primary" size="large" icon={<PlusOutlined />} onClick={() => setCreate('')}>新建售后工单</Button>
      </Space>}
    </section>
    <div className="stat-grid">
      <div className="stat"><ProfileOutlined /><span>可见工单</span><strong>{cases.value ? caseRows.length : '—'}</strong></div>
      <div className="stat"><InboxOutlined /><span>可见订单</span><strong>{orders.value ? orderRows.length : '—'}</strong></div>
      <div className="stat"><span className="stat-dot" /><span>已办结工单</span><strong>{cases.value ? caseRows.filter((row) => row.status === 'RESOLVED').length : '—'}</strong></div>
    </div>
    <Card className="data-card">
      <div className="table-toolbar">
        <Space>
          <Button type={view === 'cases' ? 'primary' : 'text'} onClick={() => {setView('cases'); setSearch('');}}>售后工单</Button>
          <Button type={view === 'orders' ? 'primary' : 'text'} onClick={() => {setView('orders'); setSearch('');}}>订单列表</Button>
        </Space>
        <Space wrap><Input allowClear aria-label="搜索记录" placeholder="搜索工单号或订单号" value={search} onChange={(e) => setSearch(e.target.value)} />
          <Reload refresh={view === 'cases' ? cases.refresh : orders.refresh} loading={view === 'cases' ? cases.loading : orders.loading} />
        </Space>
      </div>
      <Feedback error={view === 'cases' ? cases.error : orders.error} />
      {view === 'cases' ? (
        <Table rowKey="id" dataSource={caseRows.filter((row) => [row.id, row.ticket_id, row.order_id].some((part) => part.toLowerCase().includes(needle)))} loading={cases.loading}
          scroll={{x: 700}} locale={{emptyText: <Placeholder text="暂无匹配工单" />}} columns={[
            {title: '工单编号', render: (_, row) => <Button type="link" className="cell-link" onClick={() => navigate(row.id)}>{row.ticket_id}</Button>},
            {title: '订单编号', dataIndex: 'order_id'},
            {title: '售后类型', render: (_, row) => intents[row.intent] ?? row.intent},
            {title: '处理状态', render: (_, row) => <Status value={row.status} />},
            {title: '提交时间', render: (_, row) => time(row.created_at)},
          ]} />
      ) : (
        <Table rowKey="id" dataSource={orderRows.filter((row) => row.id.toLowerCase().includes(needle))} loading={orders.loading}
          scroll={{x: 580}} locale={{emptyText: <Placeholder text="暂无匹配订单" />}} columns={[
            {title: '订单编号', render: (_, row) => <Button type="link" className="cell-link" onClick={() => setOrder(row)}>{row.id}</Button>},
            {title: '商品', render: (_, row) => row.items.map((item) => productName(row, item.product_id)).join('、')},
            {title: '状态', render: (_, row) => <Tag>{label(row.status)}</Tag>},
            {title: '实付', render: (_, row) => money(row.items.reduce((sum, item) => sum + item.paid_cents, 0))},
          ]} />
      )}
    </Card>
    <Modal title="订单详情" open={!!order} footer={null} onCancel={() => setOrder(undefined)}>
      {order && <OrderDetail api={api} fallback={order} />}
    </Modal>
    {create !== undefined && <CreateCase api={api} orders={orderRows} description={create} close={() => setCreate(undefined)}
      onCreated={(id) => {setCreate(undefined); navigate(id);}} />}
  </div>;
}

function caseInUrl() { return new URLSearchParams(window.location.search).get('case') || undefined; }

function OrderDetail({api, fallback}: {api: Client; fallback: Order}) {
  const order = useResource<Order>(api, `/orders/${segment(fallback.id)}`);
  return <><Feedback error={order.error} />{!order.error && <OrderFacts order={order.value ?? fallback} />}</>;
}

function CreateCase({api, orders, description, close, onCreated}: {api: Client; orders: Order[]; description: string; close: () => void; onCreated: (id: string) => void}) {
  const [form] = Form.useForm<{order_id: string; intent: Intent; description: string; line_ids?: string[]}>();
  const orderId = Form.useWatch('order_id', form);
  const order = orders.find((item) => item.id === orderId);
  const actions = useActions(api);
  return <Modal title="新建售后工单" open footer={null} onCancel={actions.busy ? undefined : close}>
    <Form form={form} layout="vertical" initialValues={{description}} onFinish={async (values) => {
      if (!order) return;
      const result = await actions.submit<Case>('/cases', {...values, line_ids: values.line_ids?.length ? values.line_ids : undefined, description: values.description.trim(), expected_version: order.version}, {success: '已提交工单'});
      if (result) onCreated(result.id);
    }}>
      <Form.Item label="订单" name="order_id" rules={[{required: true, message: '请选择订单'}]}>
        <Select placeholder="选择需要售后的订单" options={orders.map((item) => ({value: item.id, label: item.id}))} onChange={() => form.setFieldValue('line_ids', undefined)} />
      </Form.Item>
      <Form.Item label="涉及商品" name="line_ids" extra="如留空，系统将根据订单判断。">
        <Select mode="multiple" disabled={!order} options={order?.items.map((item) => ({value: item.line_id, label: productName(order, item.product_id)}))} />
      </Form.Item>
      <Form.Item label="售后类型" name="intent" rules={[{required: true, message: '请选择售后类型'}]}>
        <Select options={Object.entries(intents).map(([value, label]) => ({value, label}))} />
      </Form.Item>
      <Form.Item label="问题描述" name="description" rules={[{required: true, whitespace: true, min: 5, transform: (value: string) => value?.trim(), message: '请至少输入 5 个字描述问题'}]}>
        <Input.TextArea rows={4} maxLength={2000} showCount placeholder="请说明收到的商品状况，或要查询的退款问题" />
      </Form.Item>
      <Space orientation="vertical" className="full"><Feedback error={actions.error} />
        <Button type="primary" htmlType="submit" loading={actions.busy} disabled={!orders.length}>提交工单</Button>
      </Space>
    </Form>
  </Modal>;
}
