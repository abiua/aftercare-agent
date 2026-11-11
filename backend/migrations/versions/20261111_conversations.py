"""Add conversation, message, attachment and case-link records."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_conversations"
down_revision = "20261110_eval_formal"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "chat_conversations",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "customer_id", sa.String(64), sa.ForeignKey("business_customers.id"), nullable=False
        ),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("revision", sa.Integer(), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("active_case_id", sa.String(36), sa.ForeignKey("business_cases.id")),
        sa.Column("active_order_id", sa.String(64), sa.ForeignKey("business_orders.id")),
        sa.Column("line_ids", sa.JSON(), nullable=False),
        sa.Column("input_floor", sa.Integer(), nullable=False),
        sa.Column("require_new_case", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
    )
    for column in ("customer_id", "updated_at"):
        op.create_index(f"ix_chat_conversations_{column}", "chat_conversations", [column])
    op.create_table(
        "chat_messages",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("chat_conversations.id"), nullable=False
        ),
        sa.Column("client_message_id", sa.String(36)),
        sa.Column("request_hash", sa.String(64)),
        sa.Column("actor_id", sa.String(128)),
        sa.Column("sequence", sa.Integer()),
        sa.Column("discarded_at", sa.DateTime(timezone=True)),
        sa.Column("role", sa.String(12), nullable=False),
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("cards", sa.JSON(), nullable=False),
        sa.Column("reply_to_id", sa.String(36), sa.ForeignKey("chat_messages.id")),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "sequence"),
        sa.UniqueConstraint("conversation_id", "client_message_id"),
    )
    op.create_table(
        "chat_attachments",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("message_id", sa.String(36), sa.ForeignKey("chat_messages.id"), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("filename", sa.String(160), nullable=False),
        sa.Column("object_key", sa.String(255), nullable=False, unique=True),
        sa.Column("content_type", sa.String(32), nullable=False),
        sa.Column("size", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.UniqueConstraint("message_id", "position"),
    )
    op.create_table(
        "chat_case_links",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "conversation_id", sa.String(36), sa.ForeignKey("chat_conversations.id"), nullable=False
        ),
        sa.Column("case_id", sa.String(36), sa.ForeignKey("business_cases.id"), nullable=False),
        sa.Column(
            "source_message_id", sa.String(36), sa.ForeignKey("chat_messages.id"), nullable=False
        ),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint("conversation_id", "case_id"),
    )


def downgrade():
    op.execute("LOCK TABLE chat_conversations IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM chat_conversations) THEN "
        "RAISE EXCEPTION 'CHAT_DATA_DOWNGRADE_BLOCKED'; END IF; END; $$"
    )
    for table in ("chat_case_links", "chat_attachments", "chat_messages", "chat_conversations"):
        op.drop_table(table)
