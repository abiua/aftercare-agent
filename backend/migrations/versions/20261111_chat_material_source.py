"""Record staff-confirmed provenance for typed copies of chat originals."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_chat_material_source"
down_revision = "20261111_chat_investigation"
branch_labels = None
depends_on = None

_TABLE = "chat_material_sources"


def _fk(name, target, *, primary=False):
    return sa.Column(
        name, sa.String(36), sa.ForeignKey(target), nullable=False, primary_key=primary
    )


def upgrade():
    op.create_table(
        _TABLE,
        _fk("case_id", "business_cases.id", primary=True),
        _fk("attachment_id", "chat_attachments.id", primary=True),
        _fk("material_id", "business_materials.id"),
        _fk("conversation_id", "chat_conversations.id"),
        _fk("message_id", "chat_messages.id"),
        sa.Column("issue_id", sa.String(36), nullable=False),
        sa.Column("kind", sa.String(24), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("actor_id", sa.String(36), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
    )
    op.create_index("ix_chat_material_sources_material_id", _TABLE, ["material_id"])


def downgrade():
    op.execute(f"LOCK TABLE {_TABLE} IN ACCESS EXCLUSIVE MODE")
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM chat_material_sources) THEN "
        "RAISE EXCEPTION 'CHAT_MATERIAL_SOURCE_DOWNGRADE_REQUIRES_EMPTY_TABLE'; END IF; END $$"
    )
    op.drop_table(_TABLE)
