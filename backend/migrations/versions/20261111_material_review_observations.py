"""Persist optional structured material-review observations."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_material_review_obs"
down_revision = "20261111_chat_blind_review"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "business_material_reviews",
        sa.Column("observations", sa.JSON(none_as_null=True), nullable=True),
    )


def downgrade():
    op.drop_column("business_material_reviews", "observations")
