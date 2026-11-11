"""Persist the automatic investigation start outbox."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_chat_investigation"
down_revision = "20261111_chat_decisions"
branch_labels = None
depends_on = None

_TABLE = "chat_investigation_requests"


def upgrade():
    op.create_table(
        _TABLE,
        sa.Column(
            "turn_id", sa.String(36), sa.ForeignKey("chat_analysis_turns.id"), primary_key=True
        ),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("chat_conversations.id"), nullable=False
        ),
        sa.Column("issue_id", sa.String(36), nullable=False),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("business_cases.id"), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("run_id", sa.String(36), sa.ForeignKey("agent_runs.id"), unique=True),
        sa.Column("scope", sa.JSON()),
        sa.Column("previous", sa.JSON()),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_investigation_requests_state", _TABLE, ["state"])


def downgrade():
    op.execute(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM chat_investigation_requests) THEN "
        "RAISE EXCEPTION 'CHAT_INVESTIGATION_DOWNGRADE_REQUIRES_EMPTY_OUTBOX'; END IF; END $$"
    )
    op.drop_table(_TABLE)
