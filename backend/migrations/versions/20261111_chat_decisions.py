"""Persist chat decision snapshots, customer consent and business bindings."""

import sqlalchemy as sa
from alembic import op

revision = "20261111_chat_decisions"
down_revision = "20261111_chat_analysis"
branch_labels = None
depends_on = None


def _id(name="id", **kwargs):
    return sa.Column(name, sa.String(36), **kwargs)


def _required_id(name):
    return _id(name, nullable=False)


def _created():
    return sa.Column("created_at", sa.DateTime(timezone=True), nullable=False)


def _expires():
    return sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False)


def _fk(name, target, *, nullable=False, unique=False):
    return sa.Column(name, sa.String(36), sa.ForeignKey(target), nullable=nullable, unique=unique)


def upgrade():
    op.add_column("business_cases", _id("decision_conversation_id"))
    op.add_column("business_cases", _id("decision_issue_id"))
    op.add_column("business_cases", sa.Column("decision_legacy_through_version", sa.Integer()))
    op.create_check_constraint(
        "ck_business_cases_decision_binding",
        "business_cases",
        "(decision_conversation_id IS NULL) = (decision_issue_id IS NULL) "
        "AND (decision_conversation_id IS NULL) = (decision_legacy_through_version IS NULL)",
    )
    op.add_column("business_proposals", sa.Column("decision_binding", sa.JSON()))

    op.create_table(
        "chat_decision_heads",
        _id("conversation_id", primary_key=True),
        _id("issue_id", primary_key=True),
        sa.Column("version", sa.Integer(), nullable=False),
        _id("snapshot_id"),
        _id("offer_id"),
        _id("review_id"),
    )
    op.create_table(
        "chat_decision_snapshots",
        _id(primary_key=True),
        _required_id("conversation_id"),
        _required_id("issue_id"),
        sa.Column("version", sa.Integer(), nullable=False),
        _fk("parent_id", "chat_decision_snapshots.id", nullable=True),
        sa.Column("run_id", sa.String(80)),
        sa.Column("origin", sa.String(24), nullable=False),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("input", sa.JSON(), nullable=False),
        sa.Column("candidate", sa.JSON(), nullable=False),
        sa.Column("checks", sa.JSON(), nullable=False),
        _expires(),
        _created(),
        sa.UniqueConstraint("conversation_id", "issue_id", "version"),
        sa.UniqueConstraint("conversation_id", "issue_id", "run_id"),
    )
    op.create_table(
        "chat_support_decisions",
        _id(primary_key=True),
        _required_id("conversation_id"),
        _required_id("issue_id"),
        _fk("snapshot_id", "chat_decision_snapshots.id"),
        _fk("resulting_snapshot_id", "chat_decision_snapshots.id", nullable=True),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("review", sa.JSON(), nullable=False),
        _created(),
        sa.UniqueConstraint("conversation_id", "issue_id", "actor_id", "request_key"),
    )
    op.create_table(
        "chat_decision_offers",
        _id(primary_key=True),
        _fk("snapshot_id", "chat_decision_snapshots.id"),
        _fk("support_decision_id", "chat_support_decisions.id", unique=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(64), nullable=False),
        sa.Column("option", sa.JSON(), nullable=False),
        _expires(),
        _created(),
    )
    op.create_table(
        "chat_customer_decisions",
        _id(primary_key=True),
        _required_id("conversation_id"),
        _required_id("issue_id"),
        _fk("offer_id", "chat_decision_offers.id"),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.UniqueConstraint("offer_id", "version"),
        sa.Column("actor_id", sa.String(128), nullable=False),
        sa.Column("request_key", sa.String(80), nullable=False),
        sa.Column("request_sha256", sa.String(64), nullable=False),
        sa.Column("response", sa.JSON(), nullable=False),
        _created(),
        sa.UniqueConstraint("conversation_id", "issue_id", "actor_id", "request_key"),
    )
    op.create_table(
        "chat_decision_projections",
        _id("run_id", primary_key=True),
        _id("snapshot_id"),
        sa.Column("state", sa.String(24), nullable=False),
        sa.Column("reason", sa.String(64)),
        _created(),
    )


def downgrade():
    op.execute(
        "LOCK TABLE business_cases, business_proposals, chat_decision_heads, "
        "chat_decision_snapshots, chat_support_decisions, chat_decision_offers, "
        "chat_customer_decisions, chat_decision_projections IN ACCESS EXCLUSIVE MODE"
    )
    op.execute(
        "DO $$ BEGIN IF EXISTS (SELECT 1 FROM business_cases WHERE decision_conversation_id IS NOT NULL) "
        "OR EXISTS (SELECT 1 FROM chat_decision_heads) OR EXISTS (SELECT 1 FROM chat_decision_projections) THEN "
        "RAISE EXCEPTION 'CHAT_DECISIONS_DOWNGRADE_BLOCKED'; END IF; END; $$"
    )
    for name in (
        "chat_decision_projections",
        "chat_customer_decisions",
        "chat_decision_offers",
        "chat_support_decisions",
        "chat_decision_snapshots",
        "chat_decision_heads",
    ):
        op.drop_table(name)
    op.drop_column("business_proposals", "decision_binding")
    op.drop_constraint("ck_business_cases_decision_binding", "business_cases", type_="check")
    for name in (
        "decision_legacy_through_version",
        "decision_issue_id",
        "decision_conversation_id",
    ):
        op.drop_column("business_cases", name)
