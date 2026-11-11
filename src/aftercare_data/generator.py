"""Reproducible synthetic after-sales business records."""

from datetime import datetime, timedelta
from random import Random

OBSERVED_AT = datetime.fromisoformat("2026-11-10T12:00:00+08:00")
POLICIES_EFFECTIVE_AT = "2026-01-01T00:00:00+08:00"

# Name, category, list price in cents, warranty days, eligible for standard return.
CATALOG = (
    ("蓝牙耳机", "数码", 19900, 365, True),
    ("机械键盘", "数码", 45900, 365, True),
    ("无线鼠标", "数码", 12900, 365, True),
    ("移动电源", "数码", 15900, 365, True),
    ("降噪耳机", "数码", 89900, 365, True),
    ("便携投影仪", "数码", 169900, 365, True),
    ("电热水壶", "小家电", 21900, 365, True),
    ("空气炸锅", "小家电", 49900, 365, True),
    ("手持吸尘器", "小家电", 72900, 365, True),
    ("台式循环扇", "小家电", 26900, 365, True),
    ("便携榨汁杯", "小家电", 14900, 365, True),
    ("手冲咖啡壶", "家居", 11900, 0, True),
    ("玻璃保鲜盒", "家居", 7900, 0, True),
    ("陶瓷马克杯", "家居", 3900, 0, True),
    ("防滑衣架套装", "家居", 2590, 0, True),
    ("折叠收纳箱", "家居", 5390, 0, True),
    ("棉质床单", "家居", 16900, 0, True),
    ("训练背包", "运动", 29900, 0, True),
    ("瑜伽垫", "运动", 8900, 0, True),
    ("运动水杯", "运动", 6500, 0, True),
    ("速干短袖", "服饰", 9900, 0, True),
    ("连帽卫衣", "服饰", 23900, 0, True),
    ("防晒外套", "服饰", 35900, 0, True),
    ("棉质长裤", "服饰", 18900, 0, True),
    ("中性笔套装", "文具", 1990, 0, True),
    ("线圈笔记本", "文具", 1290, 0, True),
    ("定制刻字钢笔", "文具", 11900, 365, False),
    ("定制印花抱枕", "家居", 8900, 0, False),
    ("密封拆封型唇膏", "个护", 4900, 0, False),
    ("冷藏酸奶礼盒", "食品", 8990, 0, False),
)

ORDER_STATUS_COUNTS = (
    ("UNPAID", 24),
    ("CANCELLED", 21),
    ("PAID", 45),
    ("SHIPPED", 60),
    ("DELIVERED", 150),
)


def _policies():
    rules = (
        (
            "本人订单",
            "只允许已核验的订单所属客户查询订单详情或提交售后。",
            {"owner_only": True},
        ),
        (
            "实付核验",
            "退款须有已确认的支付状态、付款时间和大于零的实际付款金额。",
            {"required_payment_status": "CONFIRMED", "requires_paid_at": True},
        ),
        (
            "未发货取消",
            "已付款且未发货的订单，执行前复核履约状态。确认未交运后可取消，并按已核验的剩余实付办理退款。",
            {
                "eligible_order_status": "PAID",
                "requires_fulfillment_recheck": True,
                "requires_unshipped": True,
            },
        ),
        (
            "已发货拦截",
            "已发货订单可申请拦截。物流确认拦截成功后才可取消并进入退款审核。待确认或失败时保留履约状态。",
            {
                "eligible_order_status": "SHIPPED",
                "requires_interception_confirmation": True,
            },
        ),
        (
            "签收退货时限",
            "支持普通退货的商品可在签收后168小时内提出申请，含恰好168小时的时刻。",
            {
                "window_hours": 168,
                "inclusive": True,
                "anchor": "delivered_at",
                "requires_returnable": True,
            },
        ),
        (
            "损坏补证",
            "损坏售后需提供商品损坏照和外包裹照。资料未齐时记录缺项并请求补充。",
            {"required_photo_kinds": ["ITEM", "PARCEL"]},
        ),
        (
            "签收后退货退款",
            "普通退货获准后安排寄回。收到退件并验货通过后才可提交退款审核。",
            {"requires_return_received": True, "requires_inspection_passed": True},
        ),
        (
            "重复退款",
            "同一退款请求已有处理记录时返回该记录，复用原有流程。",
            {"reuse_existing": True, "execute_again": False},
        ),
        (
            "部分退款金额",
            (
                "部分退款须先有独立核准的售后处置或商家补偿决定。"
                "每行退款金额以该行实付减历史已退款为上限，合计还须在订单剩余实付以内。"
                "部分退款只计商品金额。明确请求超出上限时说明可退金额，并请客户确认更正金额。"
            ),
            {
                "requires_approved_resolution": True,
                "cap": "ITEM_REMAINING_PAID",
                "include_fees": False,
            },
        ),
        (
            "高额退款审批",
            "单笔退款拟退金额超过50000分时，提交前须获得人工审批。",
            {"approval_threshold_cents": 50000, "operator": "gt"},
        ),
        (
            "商品质保",
            "有质保期限的商品自签收起计时。期限内含届满时刻，可提交质保服务审核。",
            {"anchor": "delivered_at", "duration_field": "warranty_days", "inclusive": True},
        ),
        (
            "退款执行结果未知",
            "退款执行超时或结果未知时，用原幂等键核对外部结果。确认结果前保持待核对状态。",
            {"reconcile_by": "idempotency_key", "allow_blind_retry": False},
        ),
    )
    return [
        {
            "id": f"POL{index:02d}",
            "version": "1.0",
            "effective_at": POLICIES_EFFECTIVE_AT,
            "title": title,
            "text": text,
            "parameters": parameters,
        }
        for index, (title, text, parameters) in enumerate(rules, 1)
    ]


def _allocate_discount(line_subtotals, discount):
    """Allocate by gross line value, assigning leftover cents by remainder."""
    subtotal = sum(line_subtotals)
    quotients = [divmod(discount * amount, subtotal) for amount in line_subtotals]
    shares = [share for share, _ in quotients]
    leftover = discount - sum(shares)
    rank = sorted(range(len(shares)), key=lambda index: (-quotients[index][1], index))
    for index in rank[:leftover]:
        shares[index] += 1
    return shares


def _iso(timestamp):
    return timestamp.isoformat(timespec="seconds") if timestamp is not None else None


def _order_times(status, rng):
    paid_at = shipped_at = delivered_at = cancelled_at = None
    if status == "DELIVERED":
        delivered_at = OBSERVED_AT - timedelta(hours=rng.randint(1, 24 * 180))
        shipped_at = delivered_at - timedelta(hours=rng.randint(8, 96))
    elif status == "SHIPPED":
        shipped_at = OBSERVED_AT - timedelta(hours=rng.randint(1, 72))

    if shipped_at is not None:
        paid_at = shipped_at - timedelta(hours=rng.randint(1, 48))
    elif status == "PAID":
        paid_at = OBSERVED_AT - timedelta(minutes=rng.randint(1, 24 * 60))
    elif status == "CANCELLED":
        cancelled_at = OBSERVED_AT - timedelta(hours=rng.randint(1, 24 * 45))

    anchor = paid_at or cancelled_at or OBSERVED_AT
    created_at = anchor - timedelta(minutes=rng.randint(1, 120))
    return {
        "created_at": _iso(created_at),
        "paid_at": _iso(paid_at),
        "shipped_at": _iso(shipped_at),
        "delivered_at": _iso(delivered_at),
        "cancelled_at": _iso(cancelled_at),
    }


def _generate_order(index, status, customer_id, products, rng):
    times = _order_times(status, rng)
    paid = times["paid_at"] is not None
    line_count = rng.choice((1, 1, 1, 2, 2, 3))
    selected_products = rng.sample(products, line_count)
    quantities = [rng.randint(1, 3) for _ in selected_products]
    line_subtotals = [
        product["unit_price_cents"] * quantity
        for product, quantity in zip(selected_products, quantities, strict=True)
    ]
    subtotal = sum(line_subtotals)
    discount_rate = rng.choice((0, 0, 5, 10, 15, 23))
    discount = subtotal * discount_rate // 100
    line_discounts = _allocate_discount(line_subtotals, discount)

    refund_kind = rng.choice(("NONE",) * 6 + ("PARTIAL",) * 3 + ("FULL",))
    items = []
    for line_index, (product, quantity, gross, line_discount) in enumerate(
        zip(selected_products, quantities, line_subtotals, line_discounts, strict=True), 1
    ):
        line_payable = gross - line_discount
        line_paid = line_payable if paid else 0
        line_refunded = 0
        if status == "DELIVERED" and refund_kind == "FULL":
            line_refunded = line_paid
        elif status == "DELIVERED" and refund_kind == "PARTIAL" and line_index == 1:
            line_refunded = rng.randint(1, line_paid - 1)
        items.append(
            {
                "line_id": f"L{line_index:02d}",
                "product_id": product["id"],
                "quantity": quantity,
                "unit_price_cents": product["unit_price_cents"],
                "discount_cents": line_discount,
                "payable_cents": line_payable,
                "paid_cents": line_paid,
                "refunded_cents": line_refunded,
            }
        )

    return {
        "id": f"O{index:04d}",
        "customer_id": customer_id,
        "status": status,
        "payment_status": "CONFIRMED" if paid else "UNPAID",
        **times,
        "items": items,
        "subtotal_cents": subtotal,
        "discount_cents": discount,
        "payable_cents": subtotal - discount,
        "paid_cents": sum(item["paid_cents"] for item in items),
        "refunded_cents": sum(item["refunded_cents"] for item in items),
    }


def generate(seed=42) -> dict:
    """Return independent, JSON-serializable records for a fixed observation time."""
    rng = Random(seed)
    products = [
        {
            "id": f"P{index:03d}",
            "name": name,
            "category": category,
            "unit_price_cents": price,
            "warranty_days": warranty_days,
            "returnable": returnable,
        }
        for index, (name, category, price, warranty_days, returnable) in enumerate(CATALOG, 1)
    ]
    customers = [
        {
            "id": f"C{index:03d}",
            "display_name": f"模拟客户{index:03d}",
            "tier": rng.choice(("STANDARD", "STANDARD", "PLUS")),
        }
        for index in range(1, 101)
    ]

    statuses = [status for status, count in ORDER_STATUS_COUNTS for _ in range(count)]
    rng.shuffle(statuses)
    customer_ids = [customer["id"] for customer in customers] * 3
    rng.shuffle(customer_ids)
    orders = [
        _generate_order(index, status, customer_id, products, rng)
        for index, (status, customer_id) in enumerate(zip(statuses, customer_ids, strict=True), 1)
    ]
    shipments = [
        {
            "id": f"S{order['id'][1:]}",
            "order_id": order["id"],
            "status": "DELIVERED" if order["delivered_at"] else "IN_TRANSIT",
            "carrier": rng.choice(("模拟快递甲", "模拟快递乙", "模拟快递丙")),
            "tracking_number": f"MOCK-{order['id'][1:]}",
            "shipped_at": order["shipped_at"],
            "delivered_at": order["delivered_at"],
        }
        for order in orders
        if order["shipped_at"] is not None
    ]
    return {
        "products": products,
        "customers": customers,
        "orders": orders,
        "shipments": shipments,
        "policies": _policies(),
    }
