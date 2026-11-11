"""Add immutable-by-service formal condition identity to evaluation experiments."""

import sqlalchemy as sa
from alembic import op

revision = "20261110_eval_formal"
down_revision = "20261110_eval_progress"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "eval_experiments", sa.Column("formal_freeze_sha256", sa.String(64), nullable=True)
    )
    op.add_column(
        "eval_experiments", sa.Column("formal_condition_key", sa.String(32), nullable=True)
    )
    op.create_check_constraint(
        "ck_eval_experiments_formal_pair",
        "eval_experiments",
        "(formal_freeze_sha256 IS NULL) = (formal_condition_key IS NULL)",
    )
    op.create_unique_constraint(
        "uq_eval_experiments_formal_condition",
        "eval_experiments",
        ["formal_freeze_sha256", "formal_condition_key"],
    )


def downgrade():
    op.execute("LOCK TABLE eval_experiments IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN "
        "IF EXISTS (SELECT 1 FROM eval_experiments WHERE formal_freeze_sha256 IS NOT NULL "
        "OR formal_condition_key IS NOT NULL) THEN "
        "RAISE EXCEPTION 'FORMAL_EVALUATION_DOWNGRADE_BLOCKED'; END IF; END; $$"
    )
    op.drop_constraint("uq_eval_experiments_formal_condition", "eval_experiments", type_="unique")
    op.drop_constraint("ck_eval_experiments_formal_pair", "eval_experiments", type_="check")
    op.drop_column("eval_experiments", "formal_condition_key")
    op.drop_column("eval_experiments", "formal_freeze_sha256")
