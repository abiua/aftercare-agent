import { Descriptions, Space, Table } from 'antd';
import type { Order } from './contracts';
import { Evidence, Json, label, money } from './ui';

export function productName(order: Order, productId: string) {
  return order.products?.find((product) => product.id === productId)?.name ?? productId;
}

export function OrderFacts({order}: {order: Order}) {
  return <Space orientation="vertical" size={12} className="full">
    <Descriptions size="small" column={{xs: 1, sm: 2}} items={[
      {key: 'id', label: '订单编号', children: order.id},
      {key: 'status', label: '订单状态', children: label(order.status)},
    ]} />
    <Table rowKey="line_id" dataSource={order.items} pagination={false} size="small" scroll={{x: 440}} columns={[
      {title: '商品', render: (_, row) => productName(order, row.product_id)},
      {title: '数量', dataIndex: 'quantity'},
      {title: '实付', render: (_, row) => money(row.paid_cents)},
      {title: '历史累计登记退款', render: (_, row) => money(row.refunded_cents)},
    ]} />
    {order.shipment != null && <Evidence title="物流记录"><Json value={order.shipment} /></Evidence>}
  </Space>;
}

