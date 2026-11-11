"""Shared request shapes for material review observations."""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

ReviewReference = Annotated[str, Field(min_length=1, max_length=64)]


class MaterialReviewObservation(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    material_id: ReviewReference
    finding: Literal["VISIBLE_DAMAGE", "NO_VISIBLE_DAMAGE", "UNCERTAIN"]
    line_id: ReviewReference | None = None
    affected_quantity: Annotated[int, Field(gt=0, le=2_147_483_647)] | None = None

    @model_validator(mode="after")
    def quantity_requires_visible_damage_on_a_line(self):
        if self.affected_quantity is not None and (
            self.finding != "VISIBLE_DAMAGE" or self.line_id is None
        ):
            raise ValueError("Affected quantity requires visible damage on an explicit order line")
        return self


MaterialReviewObservations = Annotated[
    list[MaterialReviewObservation], Field(min_length=1, max_length=20)
]
