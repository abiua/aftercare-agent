"""Only explicitly configured local aftercare clusters are migration targets."""
import os

from alembic import context
from sqlalchemy import text

import aftercare.business.models
import aftercare.conversation_analysis.tables
import aftercare.conversations.tables
import aftercare.dispatch
import aftercare.evaluation.chat.tables
import aftercare.evaluation.tables
import aftercare.intake_audit
import aftercare.intelligence.model_limits
import aftercare.intelligence.policies
import aftercare.intelligence.tables  # noqa: F401
from aftercare.config import load_settings
from aftercare.db import Base, guarded_engine

if context.is_offline_mode():
    raise RuntimeError('Offline migration disabled: target identity must be verified')

engine = guarded_engine(load_settings(os.environ.get('AFTERCARE_PROFILE', 'app')))
try:
    with engine.connect() as connection:
        connection.execute(text('CREATE EXTENSION IF NOT EXISTS vector'))
        connection.commit()
        context.configure(
            connection=connection, target_metadata=Base.metadata, compare_type=True,
            include_object=lambda obj, name, kind, reflected, compare_to:
                not (kind == 'table' and name in {
                    'checkpoints', 'checkpoint_blobs', 'checkpoint_writes', 'checkpoint_migrations'
                }),
        )
        with context.begin_transaction():
            context.run_migrations()
finally:
    engine.dispose()
