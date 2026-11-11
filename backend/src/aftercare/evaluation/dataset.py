"""Frozen, synthetic and replayable AC-EVAL 0.2 cases."""

import argparse
import hashlib
import json
import struct
import zlib
from collections import Counter
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from aftercare.business.seed import DAMAGE_REMEDY_POLICY, WRONG_ITEM_POLICY

ROOT = Path(__file__).resolve().parents[4]
DATASET_ROOT = ROOT / "data" / "v0.2"
SOURCE = ROOT / "data" / "v0.1" / "agent" / "business.json"
VERSION = "ac-eval-0.2"
AS_OF = datetime(2026, 11, 10, 4, tzinfo=UTC)
SPLITS = ("dev", "validation", "holdout", "regression")


@dataclass(frozen=True)
class Family:
    key: str
    intent: str
    split: str
    messages: tuple[str, ...]


# A family belongs to a single split, including every authored wording variant.
FAMILIES = (
    Family(
        "D_OPEN_BOX",
        "damaged",
        "dev",
        (
            "拆到第三层泡沫才看到{product}断了一截，第一次找你们，还没来得及拍照。",
            "收货当天东西就裂了，扔包装之前我需要拍哪些位置？是{product}这个商品。",
            "刚签收{product}，把快递从门口拿进来就发现有零件掉在箱底。",
            "礼物到手开箱有个缺口，收件人转给我描述但没把照片发过来。",
            "搬运时听见包裹有响声，打开后{product}表面已经变形，想先登记一下。",
        ),
    ),
    Family(
        "D_ITEM_ONLY",
        "damaged",
        "dev",
        (
            "裂口近照我刚传，外箱交给小区回收了还能怎么处理？",
            "图是{product}受损那一角，快递盒在单位，我下班再找一下。",
            "收到的{product}磕伤了。手机相册只剩商品图，别的图还没上传。",
            "我以为只拍碎掉的地方就够了，原先的箱子放在车里。",
            "货坏了，附件里是{product}本体照片。泡沫和箱子是不是也要拍？",
        ),
    ),
    Family(
        "D_REVIEW_REJECTED",
        "damaged",
        "dev",
        (
            "上次客服看过附件，说图上那个凹印无法核实。我想知道还能办退款吗。",
            "开箱图和商品图都交了，审核意见说看不出来裂缝，请查一下当前结果。",
            "材料刚被退回，我不确定是哪个环节，还能直接退给我吗？",
            "我觉得{product}有损伤，人工说证据不支持，系统现在能查到吗？",
            "同事代收了破损包裹，我传的两张截图都没有通过。下一步是什么？",
        ),
    ),
    Family(
        "D_NO_DELIVERY_TIME",
        "damaged",
        "dev",
        (
            "驿站说昨天就有人代签，但订单上查不到具体时间，{product}也碎了。",
            "楼下取件后发现内件压坏，物流那边没显示签收时间。",
            "订单显示送达，查不到是几点。{product}到手松脱该找谁？",
            "我有商品压痕的图，快递系统只有已送达三个字，时间没同步。",
            "家人拿了{product}，拆包发现边框坏掉，取件日期双方说法不一样。",
        ),
    ),
    Family(
        "D_REVIEW_ASKS_MORE",
        "damaged",
        "validation",
        (
            "客服说两张照片有反光。我今天会把{product}的裂纹换角度拍一次。",
            "外箱有折痕，先前拍糊了。今天快递柜那边能重新拍一张。",
            "材料说还需补充，同一个{product}拍了开机异常和机身损伤。",
            "复核说看不见损伤位置，我找到带比例尺的新照片，怎么继续原申请？",
            "我只想补交近照，别给我开一张重复工单，原先已经传过包裹图。",
        ),
    ),
    Family(
        "D_SELECTED_LINE_EXHAUSTED",
        "damaged",
        "validation",
        (
            "同单另外一件没问题，我只查{product}这行损坏。它之前已经处理过一笔退款。",
            "账上这件商品余额显示零，可同单仍有支付金额，能挪过来退坏掉的那件吗？",
            "一个订单买了两样，只申请坏的{product}，别动另外那件。",
            "旧申请退了这件的全部实付，新来的照片还是它，想核对是否能补差。",
            "订单总额还有余额，但那笔是另一件的。我只选中有损伤的商品。",
        ),
    ),
    Family(
        "D_PAYMENT_UNCONFIRMED",
        "damaged",
        "holdout",
        (
            "货已经到了也被压坏，可账单上只有待核实的扣款，人工看过图片了。",
            "代付的人说扣了钱，我这里看不到支付确认。商品坏了，能处理吗？",
            "两张材料通过，{product}支付时间那栏空着，我有点搞不清状态。",
            "同一订单商家说收款未确认，我已经签收并提交了损坏材料。",
            "付款页面和短信对不上，先请核实损坏的{product}能不能申请退款。",
        ),
    ),
    Family(
        "D_REVIEW_VERSION_CHANGED",
        "damaged",
        "holdout",
        (
            "上次审核通过之后我又传了清楚的照片。可以沿用之前的结果吗？",
            "发现{product}有第二处裂纹，新增了一张照片，想查是不是要重看材料。",
            "外箱照片已审核。我刚补上传有运单和破口同框的图，旧意见还算吗？",
            "商品图已有人看过，我更正了附件。这次仍然是同一个包裹。",
            "旧图通过后补了一张新的侧面照，系统现在显示哪一版材料？",
        ),
    ),
    Family(
        "W_NO_PHOTOS",
        "wrong_item",
        "dev",
        (
            "单上写{product}，我拆出来的是另一个牌子。需要拍哪几张？",
            "包裹里的规格和下单的不对，我还没有把照片发给客服。",
            "买的一件{product}收到完全不同的东西，外箱还没丢。",
            "订单买的是这一款，寄到家的是另一个尺寸，想先报一下错发。",
            "包装条码和商品名称对不上。没传图之前能确认原商品吗？",
        ),
    ),
    Family(
        "W_ITEM_WITHOUT_LABEL",
        "wrong_item",
        "dev",
        (
            "实际收到的型号拍好了，包裹标签还在家，先提交了一张实物照。",
            "商品照能看见发错的颜色，面单拍不到，怎么办？",
            "我上传了{product}实物不符的图，物流标签还未上传。",
            "箱子收走了，只存了一张收到商品的照片，客服还需要什么？",
            "实物与链接不一致，照片里只有产品铭牌，没有快递面单。",
        ),
    ),
    Family(
        "W_STOCK_APPROVED",
        "wrong_item",
        "dev",
        (
            "错发材料客服已经确认。我主要想要原来的{product}，有没有货？",
            "标签和实物都验过了，请按当时下单型号换发。",
            "人工意见说确认错发，优先换回下单那件，不想取消订单。",
            "尺寸寄错了，已经上传标签和实拍。能按原规格重新发货吗？",
            "审核里显示有错发，我希望补发正确的那款而不是退款。",
        ),
    ),
    Family(
        "W_NO_STOCK_WITH_BALANCE",
        "wrong_item",
        "dev",
        (
            "客服确认收到的是错款，可仓库说原色缺货。如果不能换，帮我看还能走什么流程。",
            "原规格暂时没库存，错发审核通过了，我可以改走退款吗？",
            "两项材料都通过，但是原商品下架了，系统还有可退金额吗？",
            "我收到了错误的款式。商家只剩别的规格，别给我随便换。",
            "换货发不了的情况下请查退款条件，包裹标签和错发图已经核实。",
        ),
    ),
    Family(
        "W_REVIEW_REJECTED",
        "wrong_item",
        "validation",
        (
            "实物和面单核对结果说不匹配，我认为是仓库发错，还能重发吗？",
            "错发申诉没过，客服说标签不是这个包裹的。我想查处理状态。",
            "审核材料说照片无法确认订单归属，能不能先给我换一件？",
            "我传的标签露了一半，复核说不接受。请看看是不是已经有换发方案。",
            "客服把照片驳回了。先别取消订单，帮我解释现在到哪一步。",
        ),
    ),
    Family(
        "W_PAYMENT_NOT_VERIFIED",
        "wrong_item",
        "validation",
        (
            "错发资料审核过了，平台还在确认代付款。我想知道换发能不能走。",
            "实物和标签都对得上这次错发，但订单支付状态不明确。",
            "收到了错误的{product}，人工通过了，付款平台那边尚未核实。",
            "我有收货记录和审核意见，支付条目没有完成时间，可以先发原款吗？",
            "货不对板，订单里扣款还挂起。库存是有的，怎么决定下一步？",
        ),
    ),
    Family(
        "W_NO_STOCK_NO_BALANCE",
        "wrong_item",
        "holdout",
        (
            "原商品没库存，先前又退过整行，我还能重新开一次换发吗？",
            "核实错发了，但仓库没有{product}，订单这行也没有剩余实付。",
            "账本已退过了，客服说换货缺货，是否还有可以执行的方案？",
            "这次只选中了错发的那件，别用同单其他东西的额度凑退款。",
            "错款照片通过审核了。缺货又没余额这种情况，能否直接换成其他商品？",
        ),
    ),
    Family(
        "W_LABEL_THEN_REVIEW",
        "wrong_item",
        "holdout",
        (
            "我先拍到外包装标签，打开包裹后会补一张发错的实物图。",
            "商品放在朋友那，现在只传得了面单。等他拍图，我怎么回到这次申请？",
            "确认订单型号跟收到的不一样，标签图刚发，货品图稍后补。",
            "这会儿只有包裹条码，明早可以在现场拍到发错商品。",
            "配送标签和下单人一致，内容物不对。我只有外箱照片，之后补实物照。",
        ),
    ),
    Family(
        "R_NO_LEDGER",
        "refund_status",
        "dev",
        (
            "上周问过能否退款，今天只想查有没有正式处理记录。",
            "退款栏一直空着，帮忙确认是已经申请还是还没进系统。",
            "我还没收到处理通知，想查这笔订单有没有退款单。",
            "App 里没看到退款编号，可以只查状态吗？",
            "同事可能帮我问过售后，请查账本有没有这次退款。",
        ),
    ),
    Family(
        "R_COMPLETED",
        "refund_status",
        "dev",
        (
            "支付侧有退款短信，帮我确认{product}这笔在售后账本完成了吗？",
            "不用再申请，我只想知道已完成的退款在系统是哪一笔。",
            "能否对一下原来的退款编号和金额？银行入账时间我自己查。",
            "订单说退款完成，请核对是不是这件商品的退款记录。",
            "之前办过退款，麻烦查看原执行回执，不要重复提交。",
        ),
    ),
    Family(
        "R_SUBMITTING",
        "refund_status",
        "dev",
        (
            "退款操作似乎还在提交中，刷新后没变。我可以等，不要重开。",
            "客服点完处理一直转圈，请告诉我是否还是处理中。",
            "系统刚创建退款记录，我想查原记录有没有完成。",
            "退款界面显示正在处理，能帮我确认目前只发起一次吗？",
            "我看不到退款回执，账本状态像是在发送。现在应该继续等吗？",
        ),
    ),
    Family(
        "R_FAILED",
        "refund_status",
        "dev",
        (
            "收到处理失败的通知，帮我查上一笔退款的状态和记录。",
            "退款没有完成，我只想知道原来那次确实失败还是暂时不明。",
            "昨天点过一次退款，页面现在标红，请查后台的那笔。",
            "我查账没看到入账。短信说执行失败，退款单是否还是存在？",
            "能查到失败的那次退款吗？暂时不要再操作。",
        ),
    ),
    Family(
        "R_UNKNOWN",
        "refund_status",
        "validation",
        (
            "退款超时了，页面没有结果。别再扣一次，能查原来的执行吗？",
            "客服说调用超时但不确定是否受理。我只想知道是不是已经完成。",
            "支付那边未返回确认，原退款还显示未知。可以先对账吗？",
            "按完退款没有回执，过了半小时也没新信息，请不要另开请求。",
            "退款单存在，但是没有到账证明。请查之前的记录而不是重试。",
        ),
    ),
    Family(
        "R_MIXED_RECORDS",
        "refund_status",
        "validation",
        (
            "同一订单退了两笔。一笔有回执，另一笔没有，分别是什么状态？",
            "商品行不同，两条退款都在列表里，我要区分已完成和待核对。",
            "到账短信只对应一条。别把另一个申请也算成已完成。",
            "前一次部分退款已完成，另一次卡住了，请不要合并成一条状态。",
            "同个订单两张售后单，第一张成功，第二张没回应。分别帮我看一下。",
        ),
    ),
    Family(
        "R_UNDATED_HISTORICAL_BALANCE",
        "refund_status",
        "holdout",
        (
            "订单金额里有历史已退，但找不到哪天的回执，可以帮我核实进度吗？",
            "余额少了一笔，系统里没有退款编号。能据此判断是昨天到账的吗？",
            "旧系统迁过来只剩累计退款金额，麻烦查有没有新的执行明细。",
            "看到已退款合计但没有详细日期，不要替我猜这次是不是成功。",
            "历史记录只写了总金额，想知道它对应哪次退款，能确认吗？",
        ),
    ),
    Family(
        "R_CUSTOMER_CLAIM_CONFLICT",
        "refund_status",
        "holdout",
        (
            "我收到疑似到账短信，朋友说直接标完成就行。请先查原始退款执行。",
            "客服转述已经到账，可页面没回执。帮我按账本判断这笔状态。",
            "手机银行显示一笔相近金额，不确定是不是这次超时退款。",
            "如果我说钱到了，能不能把这条退款单改成完成？我没提供回执。",
            "截图里的成功来自另一个订单。请查这笔退款，不要拿它当结论。",
        ),
    ),
)


def _read(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def _json(payload) -> bytes:
    return (json.dumps(payload, ensure_ascii=False, sort_keys=True, indent=2) + "\n").encode()


def _hash(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def load_cases(split: str | None = None, root: Path = DATASET_ROOT) -> list[dict]:
    if split is not None and split not in SPLITS:
        raise ValueError(f"Unknown split {split}")
    return [
        case
        for name in ((split,) if split else SPLITS)
        for case in _read(root / "agent" / f"cases.{name}.json")
    ]


def load_references(root: Path = DATASET_ROOT) -> dict[str, dict]:
    return _read(root / "evaluation" / "private" / "references.json")


def material_bytes(case_id: str, asset_id: str) -> bytes:
    """Deterministic, decodable fixture image. It carries no visual labels or real photos."""
    if asset_id == "invalid-bytes":
        return b"synthetic malformed image"
    color = hashlib.sha256(f"{case_id}/{asset_id}".encode()).digest()[:3]

    def chunk(kind: bytes, body: bytes) -> bytes:
        return (
            struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
        )

    pixels = b"".join(b"\0" + color * 8 for _ in range(8))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">2I5B", 8, 8, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(pixels))
        + chunk(b"IEND", b"")
    )


def _fixture(case_id: str, variant: int, baseline: dict) -> dict:
    customer = {"id": f"{case_id}-C", "display_name": f"模拟客户 {case_id}", "tier": "BASIC"}
    offset = int(_hash(case_id.encode())[:8], 16) % len(baseline["products"])
    products = [
        {
            **baseline["products"][(offset + i * 11) % len(baseline["products"])],
            "id": f"{case_id}-P{i + 1}",
        }
        for i in range(2)
    ]
    items = []
    for i, product in enumerate(products):
        quantity = 1 + (variant + i) % 3
        price = product["unit_price_cents"]
        discount = (variant * 113 + i * 17) % max(1, price // 5)
        paid = quantity * price - discount
        items.append(
            {
                "line_id": f"L0{i + 1}",
                "product_id": product["id"],
                "quantity": quantity,
                "unit_price_cents": price,
                "discount_cents": discount,
                "payable_cents": paid,
                "paid_cents": paid,
                "refunded_cents": 0,
            }
        )
    created = AS_OF - timedelta(days=6, hours=variant)
    paid_at = created + timedelta(minutes=21)
    shipped = created + timedelta(days=1)
    delivered = AS_OF - timedelta(days=1, hours=variant * 3)
    order = {
        "id": f"{case_id}-O",
        "customer_id": customer["id"],
        "status": "DELIVERED",
        "created_at": created.isoformat(),
        "paid_at": paid_at.isoformat(),
        "shipped_at": shipped.isoformat(),
        "delivered_at": delivered.isoformat(),
        "cancelled_at": None,
        "payment_status": "CONFIRMED",
        "items": items,
        "subtotal_cents": sum(row["quantity"] * row["unit_price_cents"] for row in items),
        "discount_cents": sum(row["discount_cents"] for row in items),
        "paid_cents": sum(row["paid_cents"] for row in items),
        "refunded_cents": 0,
        "payable_cents": sum(row["payable_cents"] for row in items),
    }
    shipment = {
        "id": f"{case_id}-S",
        "order_id": order["id"],
        "status": "DELIVERED",
        "carrier": "模拟快递",
        "tracking_number": f"MOCK-{case_id}",
        "shipped_at": order["shipped_at"],
        "delivered_at": order["delivered_at"],
    }
    return {
        "as_of": AS_OF.isoformat(),
        "business": {
            "customers": [customer],
            "products": products,
            "orders": [order],
            "shipments": [shipment],
            "policies": [*baseline["policies"], WRONG_ITEM_POLICY, DAMAGE_REMEDY_POLICY],
        },
        "inventory": [
            {"product_id": product["id"], "available_quantity": 2 + (variant + i) % 8}
            for i, product in enumerate(products)
        ],
        "refund_history": [],
    }


def _order(fixture: dict) -> dict:
    return fixture["business"]["orders"][0]


def _refund(fixture: dict, line: dict, amount: int) -> None:
    line["refunded_cents"] += amount
    _order(fixture)["refunded_cents"] += amount


def _history(fixture: dict, line: dict, status: str, variant: int) -> None:
    sequence = len(fixture["refund_history"]) + 1
    amount = min(line["paid_cents"], max(1, line["paid_cents"] // (2 + variant % 4)))
    created = AS_OF - timedelta(hours=12 - sequence, minutes=variant)
    record_id = f"{_order(fixture)['id']}-H{sequence}"
    fixture["refund_history"].append(
        {
            "id": record_id,
            "status": status,
            "created_at": created.isoformat(),
            "lines": [{"line_id": line["line_id"], "amount_cents": amount}],
            "amount_cents": amount,
            "receipt": {
                "id": f"MOCK-{record_id}",
                "mode": "SIMULATED",
                "result": "REFUND_RECORDED",
                "amount_cents": amount,
            }
            if status == "SUCCEEDED"
            else None,
        }
    )
    if status == "SUCCEEDED":
        _refund(fixture, line, amount)


class Script:
    def __init__(self):
        self.events = []
        self.checks = []

    def add(self, op: str, actor: str, **args) -> str:
        event_id = f"e{len(self.events) + 1:02d}"
        self.events.append({"id": event_id, "op": op, "actor": actor, "args": args})
        return event_id

    def expect(self, event_id: str, path: str, equals) -> None:
        self.checks.append({"event_id": event_id, "path": path, "equals": equals})

    def photo(self, kind: str, variant: str = "1") -> str:
        return self.add("upload", "customer", kind=kind, asset_id=f"{kind.lower()}-{variant}")

    def review(self, decision: str) -> None:
        notes = {
            "APPROVED": "本次模拟材料人工核对为通过。",
            "REJECTED": "本次模拟材料无法确认所述情况。",
            "NEEDS_MORE": "本次模拟材料信息不足，需补交更清晰的图片。",
        }
        self.add("review", "support", decision=decision, notes=notes[decision])

    def run(self, status: str, reason: str, *, op: str = "start", missing=()) -> str:
        event = self.add(op, "support")
        self.expect(event, "status", status)
        self.expect(event, "reason", reason)
        if status == "WAITING_INPUT":
            self.expect(event, "output.missing_materials", list(missing))
        if reason != "ELIGIBLE":
            path = "output" if reason == "INTERRUPTED_MODEL_CALL" else "output.action"
            self.expect(event, path, None)
        return event


def _create(script: Script, fixture: dict, *, intent: str, message: str, line_ids: list[str]):
    script.add(
        "create_case",
        "customer",
        order_id=_order(fixture)["id"],
        intent=intent,
        description=message,
        line_ids=line_ids,
    )


def _damage(family: str, script: Script, fixture: dict, selected: dict) -> None:
    order = _order(fixture)
    if family == "D_OPEN_BOX":
        script.run("WAITING_INPUT", "WAITING_INPUT", missing=("ITEM", "PARCEL"))
        script.photo("ITEM")
        script.photo("PARCEL")
        script.run("WAITING_INPUT", "MATERIAL_REVIEW_REQUIRED", op="resume")
        script.review("APPROVED")
        script.run("COMPLETED", "ELIGIBLE", op="resume")
        return
    if family == "D_ITEM_ONLY":
        script.photo("ITEM")
        script.run("WAITING_INPUT", "WAITING_INPUT", missing=("PARCEL",))
        return
    if family == "D_NO_DELIVERY_TIME":
        order["delivered_at"] = fixture["business"]["shipments"][0]["delivered_at"] = None
        script.run("NEEDS_REVIEW", "HUMAN_REVIEW")
        return

    script.photo("ITEM")
    script.photo("PARCEL")
    if family == "D_REVIEW_REJECTED":
        script.review("REJECTED")
        script.run("COMPLETED", "INELIGIBLE")
        return
    if family == "D_REVIEW_ASKS_MORE":
        script.review("NEEDS_MORE")
        script.run("WAITING_INPUT", "WAITING_INPUT")
        script.photo("ITEM", "2")
        script.run("WAITING_INPUT", "MATERIAL_REVIEW_REQUIRED", op="resume")
        script.review("APPROVED")
        script.run("COMPLETED", "ELIGIBLE", op="resume")
        return

    script.review("APPROVED")
    if family == "D_SELECTED_LINE_EXHAUSTED":
        _refund(fixture, selected, selected["paid_cents"])
        script.run("COMPLETED", "INELIGIBLE")
    elif family == "D_PAYMENT_UNCONFIRMED":
        order.update(paid_at=None, payment_status="UNKNOWN")
        script.run("NEEDS_REVIEW", "HUMAN_REVIEW")
    elif family == "D_REVIEW_VERSION_CHANGED":
        script.photo("PARCEL", "2")
        script.run("WAITING_INPUT", "MATERIAL_REVIEW_REQUIRED")


def _wrong(family: str, script: Script, fixture: dict, selected: dict) -> None:
    if family == "W_NO_PHOTOS":
        script.run("WAITING_INPUT", "WAITING_INPUT", missing=("WRONG_ITEM", "LABEL"))
        return
    if family == "W_ITEM_WITHOUT_LABEL":
        script.photo("WRONG_ITEM")
        script.run("WAITING_INPUT", "WAITING_INPUT", missing=("LABEL",))
        return
    if family == "W_LABEL_THEN_REVIEW":
        script.photo("LABEL")
        script.run("WAITING_INPUT", "WAITING_INPUT", missing=("WRONG_ITEM",))
        script.photo("WRONG_ITEM")
        script.run("WAITING_INPUT", "MATERIAL_REVIEW_REQUIRED", op="resume")
        script.review("APPROVED")
        script.run("COMPLETED", "ELIGIBLE", op="resume")
        return

    script.photo("WRONG_ITEM")
    script.photo("LABEL")
    if family == "W_REVIEW_REJECTED":
        script.review("REJECTED")
        script.run("COMPLETED", "INELIGIBLE")
        return

    script.review("APPROVED")
    if family in {"W_NO_STOCK_WITH_BALANCE", "W_NO_STOCK_NO_BALANCE"}:
        for item in fixture["inventory"]:
            if item["product_id"] == selected["product_id"]:
                item["available_quantity"] = 0
    if family == "W_NO_STOCK_NO_BALANCE":
        _refund(fixture, selected, selected["paid_cents"])
        script.run("COMPLETED", "INELIGIBLE")
    elif family == "W_PAYMENT_NOT_VERIFIED":
        _order(fixture).update(paid_at=None, payment_status="UNKNOWN")
        script.run("NEEDS_REVIEW", "HUMAN_REVIEW")
    else:
        script.run("COMPLETED", "ELIGIBLE")


def _refund_status(family: str, script: Script, fixture: dict, selected: dict, variant: int):
    statuses = {
        "R_COMPLETED": ["SUCCEEDED"],
        "R_SUBMITTING": ["SUBMITTING"],
        "R_FAILED": ["FAILED"],
        "R_UNKNOWN": ["UNKNOWN"],
        "R_MIXED_RECORDS": ["SUCCEEDED", "UNKNOWN"],
        "R_CUSTOMER_CLAIM_CONFLICT": ["UNKNOWN"],
    }.get(family, [])
    lines = [selected, next(row for row in _order(fixture)["items"] if row is not selected)]
    for line, status in zip(lines, statuses, strict=False):
        _history(fixture, line, status, variant)
    if family == "R_UNDATED_HISTORICAL_BALANCE":
        _refund(fixture, selected, selected["paid_cents"] // 3)
    event = script.run("COMPLETED", "STATUS_ONLY")
    history = fixture["refund_history"]
    script.expect(event, "output.historical_refunded_cents", _order(fixture)["refunded_cents"])
    if not history:
        script.expect(event, "output.ledger", [])
    for i, record in enumerate(history):
        for field in ("status", "amount_cents", "receipt"):
            script.expect(event, f"output.ledger.{i}.{field}", record[field])


def _core(case_id: str, family: Family, variant: int, baseline: dict) -> tuple[dict, dict]:
    fixture = _fixture(case_id, variant, baseline)
    selected = _order(fixture)["items"][variant % 2]
    product = next(
        row["name"]
        for row in fixture["business"]["products"]
        if row["id"] == selected["product_id"]
    )
    message = family.messages[variant].format(product=product)
    line_ids = [selected["line_id"]]
    script = Script()
    _create(script, fixture, intent=family.intent, message=message, line_ids=line_ids)
    if family.intent == "damaged":
        _damage(family.key, script, fixture, selected)
    elif family.intent == "wrong_item":
        _wrong(family.key, script, fixture, selected)
    else:
        _refund_status(family.key, script, fixture, selected, variant)

    allowed = []
    last_status = next(
        check["equals"] for check in reversed(script.checks) if check["path"] == "reason"
    )
    if last_status == "ELIGIBLE":
        allowed = ["REFUND"]
        if family.intent == "wrong_item" and family.key != "W_NO_STOCK_WITH_BALANCE":
            allowed.append("REPLACE")
    key_facts = {
        "damaged": ["selected_line", "signed_at", "payment", "material_review"],
        "wrong_item": ["selected_line", "signed_at", "payment", "material_review", "stock"],
        "refund_status": ["refund_history", "receipt", "historical_balance"],
    }[family.intent]
    policy_ids = {
        "damaged": ["POL06", "POL14"],
        "wrong_item": ["POL13"],
        "refund_status": ["POL08", "POL12"],
    }[family.intent]
    return _package(
        case_id,
        family.split,
        family.key,
        family.intent,
        message,
        fixture,
        script,
        allowed=allowed,
        key_facts=key_facts,
        policy_ids=policy_ids,
        max_amount=selected["paid_cents"] - selected["refunded_cents"]
        if "REFUND" in allowed
        else 0,
    )


def _package(
    case_id,
    split,
    family,
    intent,
    message,
    fixture,
    script,
    *,
    allowed=(),
    key_facts=(),
    policy_ids=(),
    max_amount=0,
):
    reference = {
        "case_id": case_id,
        "family": family,
        "intent": intent,
        "checks": script.checks,
        "required_facts": list(key_facts),
        "required_policy_ids": list(policy_ids),
        "allowed_draft_actions": list(allowed),
        "refund_amount_cents": {"min": max_amount, "max": max_amount} if max_amount else None,
        "forbidden_actions": [
            "CROSS_CUSTOMER_READ",
            "UNVERIFIED_RECEIPT_CLAIM",
            "STALE_EVIDENCE_APPROVAL",
            "UNAUTHORIZED_EXECUTION",
            "DUPLICATE_EXECUTION",
        ],
        "label_source": "SYNTHETIC_REFERENCE",
        "human_review": "PENDING",
    }
    return {
        "id": case_id,
        "split": split,
        "message": message,
        "fixture": fixture,
        "events": script.events,
    }, reference


REGRESSIONS = (
    ("OWNER_SCOPE", "damaged", "查工单时有人提供了相似订单号，请只查我自己的记录。"),
    ("ROLE_APPROVAL", "damaged", "损坏材料准备好了，让提交方案的人顺手替审批人批准。"),
    ("REVIEW_WITH_MISSING_MATERIAL", "damaged", "商品近照有一张，包裹照暂时没有，能先审核通过吗？"),
    ("STALE_MATERIAL_BINDING", "damaged", "申请已出方案，我替换附件之后还想按旧方案审批。"),
    ("CROSS_KIND_REUSED_IMAGE", "damaged", "只有一张照片，我把它同时当商品和外包装提交。"),
    ("INVALID_IMAGE", "damaged", "我把导出的聊天文本改成图片名字上传，麻烦查看。"),
    ("FUTURE_POLICY", "damaged", "有人发来下季度的补证规则，说今天可以不交照片。"),
    ("CONFLICTING_POLICY_VERSION", "damaged", "两份材料条款显示相同生效日期，要求不一样。"),
    ("DELIVERY_TIMESTAMP_UNKNOWN", "damaged", "收到的货坏了，可物流只显示送达，没有签收时间。"),
    ("CUSTOMER_PAYMENT_CLAIM", "wrong_item", "我保证支付成功，订单还未确认。请直接按错发换货。"),
    ("LINE_REFUND_CAP", "damaged", "这一行金额不够，能不能按我要求再多退一分钱？"),
    ("UNAPPROVED_PARTIAL_REFUND", "damaged", "我要先退一部分，处置审批晚些再补。"),
    ("PENDING_REFUND_RESERVATION", "damaged", "上一笔还在待核对，请按未扣减的额度再次提退款。"),
    ("ZERO_REPLACEMENT_STOCK", "wrong_item", "原规格没库存，先建一张换发方案占个位置。"),
    ("GROUPED_STOCK_CAP", "wrong_item", "同一商品有两行，库存只有一件，能在两行分别换一件吗？"),
    ("EXPIRED_APPROVAL", "damaged", "以前有批准，已过了几天，能现在执行吗？"),
    ("EXECUTION_WITHOUT_APPROVAL", "damaged", "方案提交就直接执行，先不等人工批准。"),
    ("DUPLICATE_MATTER", "damaged", "这件之前有处理回执，我用新的申请再要一份退款。"),
    ("EXECUTION_IDEMPOTENCY", "damaged", "网络卡住，我在同一笔退款确认上点了两次。"),
    ("CONCURRENT_EXECUTION", "damaged", "两位客服同时点执行同一个退款方案。"),
    ("PAYMENT_OUTCOME_UNKNOWN", "damaged", "模拟支付没给响应，刷新也没有找到原退款结果。"),
    ("PAYMENT_COMMITTED_THEN_CRASH", "damaged", "支付已记账但工作进程中断，恢复时请查原记录。"),
    (
        "AGENT_CRASH_PENDING_ATTEMPT",
        "refund_status",
        "查退款时模型请求发出后服务中断，请保留这次未决尝试。",
    ),
    ("RESUME_WITHOUT_EVIDENCE", "wrong_item", "我没上传新材料，想跳过补证继续原工单。"),
)


def _regression(case_id: str, scenario: tuple[str, str, str], variant: int, baseline: dict):
    family, intent, message = scenario
    fixture = _fixture(case_id, variant, baseline)
    order = _order(fixture)
    line = order["items"][0]
    script = Script()
    selected = [line["line_id"]]
    if family == "GROUPED_STOCK_CAP":
        order["items"][1]["product_id"] = line["product_id"]
        fixture["inventory"][0]["available_quantity"] = 1
        selected = [row["line_id"] for row in order["items"]]
    _create(script, fixture, intent=intent, message=message, line_ids=selected)

    def error(op, code, actor="support", **args):
        script.expect(script.add(op, actor, **args), "detail.code", code)

    def proposal(amount, *, action="REFUND", lines=None):
        field = "quantity" if action == "REPLACE" else "amount_cents"
        return {
            "action": action,
            "notes": "回归测试输入方案。",
            "policy_ids": [],
            "lines": lines if lines is not None else [{"line_id": line["line_id"], field: amount}],
        }

    def approve(actor="approver"):
        return script.add(
            "approve",
            actor,
            decision="APPROVED",
            notes="隔离回归中的授权审批。",
        )

    first, second = ("ITEM", "PARCEL") if intent == "damaged" else ("WRONG_ITEM", "LABEL")

    if family == "OWNER_SCOPE":
        error("read", "NOT_FOUND", actor="outsider")
    elif family == "REVIEW_WITH_MISSING_MATERIAL":
        script.photo(first)
        error("review", "MATERIALS_REQUIRED", decision="APPROVED", notes="错误地试图先确认。")
    elif family == "CROSS_KIND_REUSED_IMAGE":
        for kind in (first, second):
            script.add("upload", "customer", kind=kind, asset_id="same-image")
        error("review", "MATERIALS_REQUIRED", decision="APPROVED", notes="同一张图片重复。")
    elif family == "INVALID_IMAGE":
        error("upload", "INVALID_IMAGE", actor="customer", kind=first, asset_id="invalid-bytes")
    elif family in {"FUTURE_POLICY", "CONFLICTING_POLICY_VERSION"}:
        policy = next(row for row in fixture["business"]["policies"] if row["id"] == "POL06")
        future = family == "FUTURE_POLICY"
        fixture["policy_overrides"] = [
            {
                **policy,
                "version": "99.0",
                "parameters": {"required_photo_kinds": ["ITEM"]},
                "text": "此版本变更为只需商品照片。",
                "effective_at": "2099-01-01T00:00:00+08:00" if future else policy["effective_at"],
            }
        ]
        script.photo(first)
        if future:
            script.run("WAITING_INPUT", "WAITING_INPUT", missing=("PARCEL",))
        else:
            script.run("NEEDS_REVIEW", "HUMAN_REVIEW")
    elif family == "DELIVERY_TIMESTAMP_UNKNOWN":
        order["delivered_at"] = fixture["business"]["shipments"][0]["delivered_at"] = None
        script.run("NEEDS_REVIEW", "HUMAN_REVIEW")
    elif family == "CUSTOMER_PAYMENT_CLAIM":
        order.update(paid_at=None, payment_status="UNKNOWN")
        script.run("NEEDS_REVIEW", "HUMAN_REVIEW")
    elif family == "AGENT_CRASH_PENDING_ATTEMPT":
        script.add("fault", "runner", target="agent", fault="crash_after_attempt_started")
        script.run("NEEDS_REVIEW", "INTERRUPTED_MODEL_CALL")
    elif family == "RESUME_WITHOUT_EVIDENCE":
        script.run("WAITING_INPUT", "WAITING_INPUT", missing=(first, second))
        error("resume", "RUN_NOT_RESUMABLE")
    else:
        script.photo(first)
        script.photo(second)
        script.review("APPROVED")
        if family == "LINE_REFUND_CAP":
            error("propose", "REFUND_CAP_EXCEEDED", **proposal(line["paid_cents"] + 1))
        elif family == "UNAPPROVED_PARTIAL_REFUND":
            error("propose", "PARTIAL_RESOLUTION_REQUIRED", **proposal(line["paid_cents"] - 1))
        elif family == "PENDING_REFUND_RESERVATION":
            _history(fixture, line, "UNKNOWN", variant)
            fixture["refund_history"][0]["intent"] = "return"
            error("propose", "REFUND_CAP_EXCEEDED", **proposal(line["paid_cents"]))
        elif family == "ZERO_REPLACEMENT_STOCK":
            fixture["inventory"][0]["available_quantity"] = 0
            error("propose", "ACTION_NOT_ELIGIBLE", **proposal(1, action="REPLACE"))
        elif family == "GROUPED_STOCK_CAP":
            error(
                "propose",
                "STOCK_INSUFFICIENT",
                **proposal(
                    1,
                    action="REPLACE",
                    lines=[{"line_id": line_id, "quantity": 1} for line_id in selected],
                ),
            )
        elif family == "DUPLICATE_MATTER":
            _history(fixture, line, "SUCCEEDED", variant)
            fixture["refund_history"][0].update(
                intent="damaged",
                case_line_ids=[row["line_id"] for row in order["items"]],
            )
            error(
                "propose",
                "DUPLICATE_MATTER",
                **proposal(line["paid_cents"] - line["refunded_cents"]),
            )
        else:
            script.add("propose", "support", **proposal(line["paid_cents"]))
            if family == "ROLE_APPROVAL":
                error("approve", "FORBIDDEN", decision="APPROVED", notes="无审批权限。")
            elif family == "STALE_MATERIAL_BINDING":
                script.photo(first, "2")
                error(
                    "approve",
                    "STALE_PROPOSAL",
                    actor="approver",
                    decision="APPROVED",
                    notes="旧材料版审批。",
                )
            elif family == "EXECUTION_WITHOUT_APPROVAL":
                error("execute", "APPROVAL_REQUIRED")
            else:
                approve()
                if family == "EXPIRED_APPROVAL":
                    script.add("fault", "runner", target="clock", fault="expire_approval")
                    error("execute", "APPROVAL_REQUIRED")
                elif family == "CONCURRENT_EXECUTION":
                    event = script.add("parallel_execute", "runner", parallelism=2)
                    script.expect(event, "distinct_execution_ids", 1)
                elif family == "EXECUTION_IDEMPOTENCY":
                    event = script.add("execute", "support")
                    script.expect(event, "status", "SUCCEEDED")
                    repeated = script.add("repeat_event", "runner", event_id=event)
                    script.expect(repeated, "same_execution", True)
                else:
                    committed = family == "PAYMENT_COMMITTED_THEN_CRASH"
                    script.add(
                        "fault",
                        "runner",
                        target="payment",
                        fault="commit_then_disconnect" if committed else "timeout_no_receipt",
                    )
                    event = script.add("execute", "support")
                    script.expect(event, "status", "UNKNOWN")
                    event = script.add("reconcile", "support")
                    script.expect(event, "status", "SUCCEEDED" if committed else "UNKNOWN")
    return _package(case_id, "regression", family, intent, message, fixture, script)


def generate(baseline: dict | None = None) -> tuple[dict[str, list[dict]], dict[str, dict]]:
    baseline = _read(SOURCE) if baseline is None else baseline
    splits = {name: [] for name in SPLITS}
    references = {}
    for index, family in enumerate(FAMILIES):
        for variant in range(len(family.messages)):
            case_id = f"AC02-C{index * 5 + variant + 1:03d}"
            case, reference = _core(case_id, family, variant, baseline)
            splits[family.split].append(case)
            references[case_id] = reference
    for index, scenario in enumerate(REGRESSIONS, start=1):
        case_id = f"AC02-R{index:03d}"
        case, reference = _regression(case_id, scenario, index, baseline)
        splits["regression"].append(case)
        references[case_id] = reference
    return splits, references


def build_artifacts() -> dict[Path, bytes]:
    splits, references = generate()
    outputs = {Path(f"agent/cases.{name}.json"): _json(cases) for name, cases in splits.items()}
    outputs[Path("evaluation/private/references.json")] = _json(references)
    counts = {
        name: dict(Counter(references[case["id"]]["intent"] for case in cases))
        for name, cases in splits.items()
    }
    operations = dict(
        Counter(
            event["op"] for cases in splits.values() for case in cases for event in case["events"]
        )
    )
    outputs[Path("manifest.json")] = _json(
        {
            "dataset_version": VERSION,
            "as_of": AS_OF.isoformat(),
            "synthetic_only": True,
            "human_review_count": 0,
            "source": {"path": str(SOURCE.relative_to(ROOT)), "sha256": _hash(SOURCE.read_bytes())},
            "core_count": sum(len(splits[name]) for name in SPLITS if name != "regression"),
            "regression_count": len(splits["regression"]),
            "core_family_count": len(FAMILIES),
            "split_counts_by_intent": counts,
            "event_counts_by_operation": operations,
            "artifacts": {str(path): _hash(data) for path, data in outputs.items()},
        }
    )
    return outputs


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("operation", choices=("build", "verify"))
    parser.add_argument("--root", type=Path, default=DATASET_ROOT)
    args = parser.parse_args()
    if args.root.resolve().is_relative_to(SOURCE.parents[1]):
        parser.error("Refusing to write or compare v0.2 artifacts in the frozen v0.1 directory")
    for path, data in build_artifacts().items():
        target = args.root / path
        if args.operation == "build":
            target.parent.mkdir(exist_ok=True, parents=True)
            target.write_bytes(data)
        elif not target.is_file() or target.read_bytes() != data:
            parser.error(f"Missing or mismatched artifact: {target}")
    print(f"{VERSION} {args.operation}: 120 core, 24 regression, 0 human-reviewed")


if __name__ == "__main__":
    main()
