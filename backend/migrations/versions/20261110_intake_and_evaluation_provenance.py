"""Durable intake audit and evaluation provenance."""
from alembic import op
import sqlalchemy as sa

revision = '20261110_intake_eval'
down_revision = '20261110_delivery_limits'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('eval_experiments', sa.Column('parent_experiment_id', sa.String(36)))
    op.create_foreign_key('fk_eval_parent', 'eval_experiments', 'eval_experiments',
                          ['parent_experiment_id'], ['id'])
    op.create_index('ix_eval_experiments_parent_experiment_id', 'eval_experiments', ['parent_experiment_id'])
    op.add_column('eval_results', sa.Column('retry_of_result_id', sa.String(36)))
    op.create_foreign_key('fk_eval_retry', 'eval_results', 'eval_results', ['retry_of_result_id'], ['id'])
    op.create_index('ix_eval_results_retry_of_result_id', 'eval_results', ['retry_of_result_id'])
    op.add_column('eval_reviews', sa.Column('annotations', sa.JSON()))
    op.create_table('eval_imports',
        sa.Column('id', sa.String(36), primary_key=True),
        sa.Column('sha256', sa.String(64), nullable=False, unique=True),
        sa.Column('origin', sa.String(200), nullable=False),
        sa.Column('content', sa.Text(), nullable=False),
        sa.Column('summary', sa.JSON(), nullable=False),
        sa.Column('created_by', sa.String(36), nullable=False),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False))
    op.create_table('intake_model_audits',
        sa.Column('actor_id', sa.String(36), primary_key=True),
        sa.Column('request_key', sa.String(128), primary_key=True),
        sa.Column('customer_id', sa.String(36), nullable=False),
        sa.Column('message_digest', sa.String(64), nullable=False),
        sa.Column('status', sa.String(16), nullable=False),
        sa.Column('error_code', sa.String(64)),
        sa.Column('metadata_json', sa.JSON(), nullable=False),
        sa.Column('usage', sa.JSON()),
        sa.Column('result', sa.JSON()),
        sa.Column('trace', sa.JSON(), nullable=False),
        sa.Column('reserved_cost_usd', sa.String(30)),
        sa.Column('estimated_cost_usd', sa.String(30)),
        sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
        sa.Column('started_at', sa.DateTime(timezone=True)),
        sa.Column('deadline', sa.DateTime(timezone=True), nullable=False),
        sa.Column('finished_at', sa.DateTime(timezone=True)))


def downgrade():
    op.drop_table('intake_model_audits')
    op.drop_table('eval_imports')
    op.drop_column('eval_reviews', 'annotations')
    op.drop_index('ix_eval_results_retry_of_result_id', table_name='eval_results')
    op.drop_constraint('fk_eval_retry', 'eval_results', type_='foreignkey')
    op.drop_column('eval_results', 'retry_of_result_id')
    op.drop_index('ix_eval_experiments_parent_experiment_id', table_name='eval_experiments')
    op.drop_constraint('fk_eval_parent', 'eval_experiments', type_='foreignkey')
    op.drop_column('eval_experiments', 'parent_experiment_id')
