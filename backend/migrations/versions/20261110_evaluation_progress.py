"""Record evaluation execution progress without synthesizing historical events."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_eval_progress"
down_revision = "20261110_intake_eval"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("eval_experiments", sa.Column("progress_seq", sa.Integer(), nullable=True))
    op.create_table(
        "eval_progress_events",
        sa.Column(
            "experiment_id",
            sa.String(36),
            sa.ForeignKey("eval_experiments.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("sequence", sa.Integer(), primary_key=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )


def downgrade():
    op.drop_table("eval_progress_events")
    op.drop_column("eval_experiments", "progress_seq")
