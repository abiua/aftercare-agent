"""Only explicitly configured local aftercare clusters are migration targets."""
import os
from alembic import context
from sqlalchemy import text
from aftercare.config import load_settings
from aftercare.db import Base, guarded_engine
import aftercare.business.models  # noqa: F401
import aftercare.intelligence.tables  # noqa: F401
import aftercare.intelligence.policies  # noqa: F401
import aftercare.evaluation.tables  # noqa: F401
import aftercare.dispatch  # noqa: F401
import aftercare.intelligence.model_limits  # noqa: F401
import aftercare.intake_audit  # noqa: F401

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
