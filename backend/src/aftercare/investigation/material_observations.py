"""Explicitly versioned evidence for existing structured human material observations."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, TypeAdapter

from aftercare.business.contracts import MaterialReviewObservation, ReviewReference

HUMAN_MATERIAL_PROJECTION_V1 = "human-material-observations-v1"

ORDER_PAYMENT_REF = "recorded-order-payment"


def recorded_order_payment(order):
    """Whitelist the raw order record without asserting a payment policy conclusion."""
    return {
        "order_id": order["id"],
        "order_version": order["version"],
        "payment_status": order.get("payment_status"),
        "paid_at": order.get("paid_at"),
    }


Sha = Annotated[str, Field(pattern=r"^[0-9a-f]{64}$")]


class OriginalAttachmentReference(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    attachment_id: ReviewReference
    message_id: ReviewReference
    sha256: Sha


class FrozenHumanObservation(MaterialReviewObservation):
    source: Literal["HUMAN_MATERIAL_REVIEW"]
    material_sha256: Sha
    original_attachments: Annotated[list[OriginalAttachmentReference], Field(max_length=100)]


_OBSERVATIONS = TypeAdapter(
    Annotated[list[FrozenHumanObservation], Field(min_length=1, max_length=20)]
)


def validate_review_observations(review, materials, attachments, lines, materials_version):
    """Validate the structured payload without upgrading a recorded observation to a decision."""
    observations = _OBSERVATIONS.validate_python(review["observations"])
    material_ids = {item.material_id for item in observations}
    reviewed_ids = review["material_ids"]
    if (
        review["effective"] is not True
        or review["materials_version"] != materials_version
        or len(reviewed_ids) != len(set(reviewed_ids))
        or set(reviewed_ids) != materials.keys()
        or len(material_ids) != len(observations)
        or not material_ids <= set(reviewed_ids)
    ):
        raise ValueError("INVALID_HUMAN_MATERIAL_OBSERVATIONS")

    totals = {}
    for item in observations:
        material = materials[item.material_id]
        if item.material_sha256 != material["sha256"] or (
            item.line_id is not None and item.line_id not in lines
        ):
            raise ValueError("INVALID_HUMAN_MATERIAL_OBSERVATIONS")
        if item.affected_quantity is not None and (
            item.affected_quantity > lines[item.line_id]
            or totals.setdefault(item.line_id, item.affected_quantity) != item.affected_quantity
        ):
            raise ValueError("INVALID_HUMAN_MATERIAL_OBSERVATIONS")
        ids = [ref.attachment_id for ref in item.original_attachments]
        if ids != sorted(set(ids)):
            raise ValueError("INVALID_HUMAN_MATERIAL_OBSERVATIONS")
        for ref in item.original_attachments:
            attachment = attachments.get(ref.attachment_id)
            if (
                attachment is None
                or (attachment["message_id"], attachment["sha256"]) != (ref.message_id, ref.sha256)
                or ref.sha256 != item.material_sha256
                or (attachment["size"], attachment["content_type"])
                != (material["size"], material["content_type"])
            ):
                raise ValueError("INVALID_HUMAN_MATERIAL_OBSERVATIONS")
    return [item.model_dump(mode="json", exclude_none=True) for item in observations]
