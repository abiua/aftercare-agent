"""Persist independent initial judgments, exposure and post-exposure labels."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_chat_blind_review"
down_revision = "20261111_chat_material_source"
branch_labels = None
depends_on = None

_TASKS = "eval_chat_blind_tasks"
_INITIALS = "eval_chat_blind_initials"
_EXPOSURES = "eval_chat_blind_exposures"
_LABELS = "eval_chat_blind_labels"


def _hash(name):
    return sa.Column(name, sa.String(64), nullable=False)


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def _task():
    return sa.Column("task_id", sa.String(36), sa.ForeignKey(f"{_TASKS}.id"), primary_key=True)


def upgrade():
    op.create_table(
        _TASKS,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("reviewer_id", sa.String(36), sa.ForeignKey("users.id"), nullable=False),
        sa.Column("case_label", sa.String(80), nullable=False),
        _hash("input_sha256"),
        _hash("source_sha256"),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("image_objects", sa.JSON(), nullable=False),
        sa.Column("candidates", sa.JSON(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        _created(),
        sa.UniqueConstraint("reviewer_id", "input_sha256"),
    )
    op.create_table(
        _INITIALS,
        _task(),
        sa.Column("payload", sa.JSON(), nullable=False),
        _hash("sha256"),
        _created(),
    )
    op.create_table(
        _EXPOSURES,
        _task(),
        _hash("initial_sha256"),
        _hash("candidates_sha256"),
        _hash("sha256"),
        _created(),
    )
    op.create_table(
        _LABELS,
        _task(),
        sa.Column("payload", sa.JSON(), nullable=False),
        _hash("exposure_sha256"),
        _hash("sha256"),
        _created(),
    )


def downgrade():
    op.execute(f"LOCK TABLE {_TASKS} IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM eval_chat_blind_tasks) THEN "
        "RAISE EXCEPTION 'CHAT_BLIND_REVIEW_DOWNGRADE_REQUIRES_EMPTY_TASKS'; END IF; END $$"
    )
    for table in (_LABELS, _EXPOSURES, _INITIALS, _TASKS):
        op.drop_table(table)
