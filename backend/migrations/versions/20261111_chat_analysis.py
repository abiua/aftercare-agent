"""Add durable conversation analysis and private observation records."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_chat_analysis"
down_revision = "20261111_conversations"
branch_labels = None
depends_on = None


def upgrade():
    for name, kind in (
        ("analysis_issue_id", sa.String(36)),
        ("analysis_from_seq", sa.Integer()),
        ("analysis_requests", sa.Integer()),
    ):
        op.add_column("chat_conversations", sa.Column(name, kind))
    op.add_column("chat_messages", sa.Column("analysis_issue_id", sa.String(36)))
    op.create_table(
        "chat_analysis_turns",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("chat_conversations.id"), nullable=False
        ),
        sa.Column("customer_id", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("input_from_seq", sa.Integer(), nullable=False),
        sa.Column("input_through_seq", sa.Integer(), nullable=False),
        sa.Column("scope_sha256", sa.String(64), nullable=False),
        sa.Column("attachment_sha256", sa.String(64), nullable=False),
        sa.Column("attachments", sa.JSON(), nullable=False),
        sa.Column("vision_input", sa.JSON()),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(64)),
        sa.Column("quiet_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("wait_until", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deadline", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_until", sa.DateTime(timezone=True)),
        sa.Column("lease_token", sa.String(36)),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "generation"),
        sa.UniqueConstraint("conversation_id", "revision"),
    )
    op.create_index("ix_chat_analysis_turns_state", "chat_analysis_turns", ["state"])
    op.create_table(
        "chat_model_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "turn_id",
            sa.String(36),
            sa.ForeignKey("chat_analysis_turns.id"),
            nullable=False,
            unique=True,
        ),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("chat_conversations.id"), nullable=False
        ),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("error_code", sa.String(64)),
        sa.Column("reserved_microusd", sa.BigInteger(), nullable=False),
        sa.Column("estimated_cost_microusd", sa.BigInteger()),
        sa.Column("budget_violation", sa.Boolean(), nullable=False),
        sa.Column("usage", sa.JSON()),
        sa.Column("model_evidence", sa.JSON(), nullable=False),
        sa.Column("response_evidence", sa.JSON()),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("finished_at", sa.DateTime(timezone=True)),
    )
    op.create_index(
        "ix_chat_model_attempts_conversation_id", "chat_model_attempts", ["conversation_id"]
    )
    op.create_table(
        "chat_observations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "turn_id", sa.String(36), sa.ForeignKey("chat_analysis_turns.id"), nullable=False
        ),
        sa.Column(
            "attempt_id", sa.String(36), sa.ForeignKey("chat_model_attempts.id"), nullable=False
        ),
        sa.Column(
            "attachment_id", sa.String(36), sa.ForeignKey("chat_attachments.id"), nullable=False
        ),
        sa.Column("original_sha256", sa.String(64), nullable=False),
        sa.Column("sent_sha256", sa.String(64), nullable=False),
        sa.Column("transform_version", sa.String(64), nullable=False),
        sa.Column("certainty", sa.String(24), nullable=False),
        sa.Column("candidate_kind", sa.String(24), nullable=False),
        sa.Column("findings", sa.JSON(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("turn_id", "attachment_id"),
    )


def downgrade():
    op.execute("LOCK TABLE chat_conversations, chat_analysis_turns IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM chat_analysis_turns) THEN "
        "RAISE EXCEPTION 'CHAT_ANALYSIS_DOWNGRADE_BLOCKED'; END IF; END; $$"
    )
    for name in ("chat_observations", "chat_model_attempts", "chat_analysis_turns"):
        op.drop_table(name)
    op.drop_column("chat_messages", "analysis_issue_id")
    for column in ("analysis_requests", "analysis_from_seq", "analysis_issue_id"):
        op.drop_column("chat_conversations", column)
