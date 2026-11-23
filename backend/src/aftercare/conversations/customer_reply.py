"""Answer the customer's current text using only public history and owned order facts."""

import json
import re
from datetime import timedelta
from decimal import Decimal

from langchain_core.messages import HumanMessage, SystemMessage
from sqlalchemy import select
from sqlalchemy.orm import Session

from aftercare.auth import UserContext
from aftercare.business.models import Case, Order
from aftercare.business.rules import digest, parse_time, utc
from aftercare.conversation_analysis.tables import ChatAnalysisTurn
from aftercare.intake import rule_intents
from aftercare.intake_audit import IntakeAuditFactory
from aftercare.intelligence.models import InvalidOutput, ModelFailure

from .tables import Conversation, Message

INSTRUCTION = """你是商店的中文售后助手。用一到三句自然的中文回答 latest_message。
先判断客户本轮是否还在咨询售后。问候、闲聊、转向别的话题时只回答当前话题，
给出对新问题的具体判断和理由，不用追问是否还有售后需求，也不用提醒自己是售后助手。
不要带回之前的售后问题、材料、订单号或操作提醒。历史用于理解指代，客户陈述不能作为核实结果。
若本轮涉及退换货，先根据 order 的商品、签收状态和分商品分原因的 policies 回答；
order 为空则请客户选择订单，未确认商品且不同商品会影响判断时请客户选商品。
只对确实适用的条款作条件说明。within_return_window 只代表普通退货期限，不能据此同意或拒绝换货。
换货原因不清或责任不明时，先问一个能区分处理方式的具体问题，例如在拆封时已发现，还是使用或存放后才发生。
不要把卫生、异物或使用中的情况推定为破损、错发或客户责任。原因未厘清时不要索要照片或安排退换。
只有本轮在询问或继续办理有对应材料条款的明确事项时才说明材料。缺少适用规则时说明需核实。
不得承诺受理、替客户同意、编造操作或财务状态。用 JSON 返回 {"text":"回复","needs_selection":false}；
仅当本轮回复确实需要客户选择订单或商品时 needs_selection 为 true。"""


# These are the customer-facing clauses for each possible reason, not a decision on the claim.
_CONSULTATION_CLAUSES = {
    "return": {"POL05", "POL07"},
    "wrong_item": {"POL13"},
    "damaged": {"POL06", "POL14"},
}


def _order_context(business, db, conversation, case, at):
    if not conversation.active_order_id:
        return None
    order = db.scalar(
        select(Order).where(
            Order.id == conversation.active_order_id, Order.customer_id == conversation.customer_id
        )
    )
    if order is None:
        return None
    data = business._order_json(db, order)
    products = {p["id"]: p for p in data["products"]}
    selected = set(conversation.line_ids)
    items = [item for item in data["items"] if not selected or item["line_id"] in selected]
    line_ids = [item["line_id"] for item in items]
    lines = []
    for item in items:
        product = products.get(item["product_id"], {})
        lines.append(
            {
                "line_id": item["line_id"],
                "product_id": item["product_id"],
                "name": product.get("name"),
                "quantity": item["quantity"],
                "paid_cents": item["paid_cents"],
                "returnable": product.get("returnable"),
                "within_return_window": None,
                "policies": {},
            }
        )
    catalogue = business._all_policy_versions(db)
    delivered = parse_time(data.get("delivered_at"))
    for intent, clause_ids in _CONSULTATION_CLAUSES.items():
        requested = utc(case.created_at) if case and case.intent == intent else at
        resolution = business.resolve_order_policies(
            data, line_ids, intent, catalogue, decision_at=at, case_created_at=requested
        )
        for line in lines:
            docs = {p["id"]: p for p in resolution.by_product.get(line["product_id"], [])}
            if resolution.blockers or not clause_ids <= docs.keys():
                continue
            line["policies"][intent] = [
                {key: docs[pid][key] for key in ("id", "version", "text")}
                for pid in sorted(clause_ids)
            ]
            if intent == "return" and data["status"] == "DELIVERED" and delivered:
                hours = docs["POL05"].get("parameters", {}).get("window_hours")
                if type(hours) is int and hours >= 0 and requested >= delivered:
                    line["within_return_window"] = (requested - delivered) / timedelta(
                        hours=1
                    ) <= hours
    return {
        "id": order.id,
        "status": data["status"],
        "delivered_at": data.get("delivered_at"),
        "item_selected": bool(selected),
        "lines": lines,
    }


# Payment and approval assertions belong to the business ledger or role-specific UI.
_TRANSACTION_LANGUAGE = re.compile(
    r"到账|到[您你]的.{0,8}(账户|银行卡)|原路退回|已退回|已退还|已退款|"
    r"(?:退款|款项|审批|审核).{0,10}(?:成功|已通过|已完成|已处理|已批准)|"
    r"(?:已|已经).{0,8}(?:批准|退回|退还|发放退款|执行退款|完成退款|通过审批)"
)


def conversational_text(text):
    sentences = re.split(r"(?<=[。！？!?\n])", text)
    return "".join(
        sentence for sentence in sentences if not _TRANSACTION_LANGUAGE.search(sentence)
    ).strip()


_PHOTO_REQUEST = re.compile(
    r"(?:上传|补交|补充|补拍|拍摄|拍一张|发一张|发来|提供|提交|发送).{0,22}(?:照片|图片|相片|实物照|包裹照|标签照)"
    r"|(?:照片|图片|相片).{0,12}(?:上传|发来|发给|提交)"
)


def _ground_photo_request(text, context):
    if not _PHOTO_REQUEST.search(text):
        return text
    latest = context["latest_message"]
    reasons = set(rule_intents(latest)) & {"wrong_item", "damaged"}
    case = context["case"]
    if (
        case
        and case["intent"] in {"wrong_item", "damaged"}
        and re.search(r"照片|图片|材料|凭证|怎么拍", latest)
    ):
        reasons.add(case["intent"])
    order = context["order"]
    if (
        order
        and order["item_selected"]
        and order["lines"]
        and any(all(reason in line["policies"] for line in order["lines"]) for reason in reasons)
    ):
        return text
    # Unverified reasons do not start evidence collection, even if the model suggests it.
    clean = "".join(
        sentence
        for sentence in re.split(r"(?<=[。！？!?\n])", text)
        if all(
            re.search(
                r"(?:无需|不必|不用|不需要|先别|暂缓)(?:您|你|再|额外|另外)*$",
                sentence[: m.start()],
            )
            for m in _PHOTO_REQUEST.finditer(sentence)
        )
    ).strip()
    return clean or "请问具体是什么时候发现这个情况的？"


def respond_to_text(chat, lease, binding, model):
    with Session(chat.engine) as db:
        turn = db.get_one(ChatAnalysisTurn, lease.turn_id)
        current = db.get_one(Conversation, turn.conversation_id)
        if turn.reason == "MATERIAL_REVIEW_CHANGED":
            return False
        if any(item["sequence"] == turn.input_through_seq for item in turn.attachments):
            return False
        latest = db.scalar(
            select(Message).where(
                Message.conversation_id == current.id,
                Message.sequence == turn.input_through_seq,
                Message.role == "customer",
                Message.actor_id == turn.actor_id,
            )
        )
        if latest is None or not latest.text.strip():
            return False
        rows = db.scalars(
            select(Message)
            .where(
                Message.conversation_id == current.id,
                Message.sequence.between(turn.input_from_seq, turn.input_through_seq),
            )
            .order_by(Message.sequence.desc())
            .limit(16)
        ).all()
        history = [
            {"role": m.role, "text": m.text[:1400]}
            for m in reversed(rows)
            if m.text and m.id != latest.id
        ]
        case = db.get(Case, current.active_case_id) if current.active_case_id else None
        order_context = _order_context(chat.business, db, current, case, utc(latest.created_at))
        context = {
            "latest_message": latest.text,
            "history": history,
            "order": order_context,
            "case": {"intent": case.intent, "status": case.status} if case else None,
            "photos_received": len(turn.attachments),
        }
        actor_id, customer_id, request_id = turn.actor_id, current.customer_id, latest.id

    def validate(calls, content):
        try:
            value = json.loads(content)
        except (ValueError, TypeError):
            value = None
        reply = value.get("text") if isinstance(value, dict) else None
        if calls or not isinstance(reply, str) or not 1 <= len(reply.strip()) <= 1500:
            raise InvalidOutput("INVALID_CHAT_REPLY", "text")
        return {
            "result": {
                "text": reply.strip(),
                "needs_selection": value.get("needs_selection") is True,
            }
        }

    logical_id = "chat:reply"
    audit = IntakeAuditFactory(chat.engine, logical_id=logical_id)(
        actor_id, customer_id, f"chat-reply:{request_id}", digest([binding["revision"], request_id])
    )
    if order_context:
        audit.event(
            "chat_order_policy_lookup",
            {
                "order_id": order_context["id"],
                "lines": [
                    {
                        "line_id": line["line_id"],
                        "within_return_window": line["within_return_window"],
                        "policies": [
                            f"{p['id']}:{p['version']}"
                            for docs in line["policies"].values()
                            for p in docs
                        ],
                    }
                    for line in order_context["lines"]
                ],
            },
        )
    needs_selection = False
    try:
        result = model.with_evaluation_limits(calls=1, budget=Decimal("0.02")).call(
            audit,
            logical_id,
            preferred="economy",
            fixed=True,
            tools=[],
            messages=[
                SystemMessage(content=INSTRUCTION),
                HumanMessage(content=json.dumps(context, ensure_ascii=False)),
            ],
            data_scope="customer_conversation",
            validate=validate,
        )
        needs_selection = result["result"].get("needs_selection", False)
        reply, code = result["result"]["text"], "AGENT_CHAT_REPLY"
    except ModelFailure:
        needs_selection = True
        reply, code = (
            "本轮助手暂时未能回复。你的消息已保存，可以继续补充或联系人工客服。",
            "AGENT_CHAT_UNAVAILABLE",
        )

    def publish(db, row):
        text = _ground_photo_request(conversational_text(reply), context)
        if not text and row.active_order_id:
            order = db.get_one(Order, row.active_order_id)
            summary, cards = chat._refund(db, order, "")
            return chat._reply(db, row, summary, cards, reply_to=request_id)
        if not text:
            return chat._reply(
                db,
                row,
                "请先选择要查询的订单，我可以帮你核对模拟退款记录。",
                [],
                reply_to=request_id,
            )
        cards = [{"type": "status", "data": {"code": code}}]
        if needs_selection and not row.line_ids:
            if row.active_order_id:
                owned = chat.business._order_json(db, db.get_one(Order, row.active_order_id))
                cards.append(
                    {
                        "type": "order_candidates",
                        "data": {"orders": [chat._order_card(owned)], "new_issue": False},
                    }
                )
            else:
                user = UserContext(user_id=actor_id, role="customer", customer_id=customer_id)
                card, _ = chat._candidates(db, user, context["latest_message"])
                cards.append(card)
        return chat._reply(db, row, text, cards, reply_to=request_id)

    chat.analysis.publish_status(lease, publish)
    return True
