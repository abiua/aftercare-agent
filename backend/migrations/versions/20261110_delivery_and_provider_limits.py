"""Durable delivery leases and shared provider limits."""
from alembic import op
import sqlalchemy as sa

revision = '20261110_delivery_limits'
down_revision = '1ee8d7eb0d87'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('dispatch_jobs',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('kind', sa.String(16), nullable=False),
        sa.Column('target_id', sa.String(36), nullable=False),
        sa.Column('generation', sa.Integer(), nullable=False),
        sa.Column('state', sa.String(20), nullable=False),
        sa.Column('publications', sa.Integer(), nullable=False),
        sa.Column('failures', sa.Integer(), nullable=False),
        sa.Column('claims', sa.Integer(), nullable=False),
        sa.Column('next_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('owner', sa.String(36)),
        sa.Column('lease_until', sa.DateTime(timezone=True)),
        sa.Column('error_code', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('updated_at', sa.DateTime(timezone=True), nullable=False),
        sa.UniqueConstraint('kind', 'target_id', 'generation'))
    op.create_index('ix_dispatch_jobs_state', 'dispatch_jobs', ['state'])
    op.create_table('dispatch_events',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('job_id', sa.String(36), nullable=False),
        sa.Column('state', sa.String(24), nullable=False),
        sa.Column('owner', sa.String(36)),
        sa.Column('code', sa.String(64)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_dispatch_events_job_id', 'dispatch_events', ['job_id'])
    op.create_table('provider_rate_scopes',
        sa.Column('scope', sa.String(96), primary_key=True),
        sa.Column('max_in_flight', sa.Integer(), nullable=False),
        sa.Column('requests_per_minute', sa.Integer(), nullable=False),
        sa.Column('cooldown_until', sa.DateTime(timezone=True)))
    op.create_table('provider_rate_leases',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('scope', sa.String(96), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('active_until', sa.DateTime(timezone=True), nullable=False))
    op.create_index('ix_provider_rate_leases_scope', 'provider_rate_leases', ['scope'])


def downgrade():
    for name in ['provider_rate_leases', 'provider_rate_scopes', 'dispatch_events', 'dispatch_jobs']:
        op.drop_table(name)
