"""One execution per case; LangGraph owns its checkpoint schema."""
from alembic import op
revision = '8afc73e77e59'
down_revision = 'dc326a9a4621'
branch_labels = None
depends_on = None

def upgrade():
    op.drop_index('ix_business_executions_case_id', table_name='business_executions')
    op.create_unique_constraint('uq_business_executions_case_id', 'business_executions', ['case_id'])

def downgrade():
    op.drop_constraint('uq_business_executions_case_id', 'business_executions', type_='unique')
    op.create_index('ix_business_executions_case_id', 'business_executions', ['case_id'])
