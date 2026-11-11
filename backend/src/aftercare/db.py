from sqlalchemy import Boolean, String, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from aftercare.config import TARGETS


class Base(DeclarativeBase):
    pass


class User(Base):
    __tablename__ = 'users'
    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(20))
    customer_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    display_name: Mapped[str] = mapped_column(String(100))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


def guarded_engine(settings, *, test_only=False):
    try:
        url = make_url(settings.database_url)
        port, database, username, cluster = TARGETS[settings.profile]
        host = '127.0.0.1'
        if settings.container:
            host, port = f'pg-{settings.profile}', 5432
        valid = (
            (not test_only or settings.profile == 'test')
            and url.drivername == 'postgresql+psycopg'
            and url.host == host
            and url.port == port and url.database == database
            and url.username == username and bool(url.password) and not url.query
        )
    except (ValueError, TypeError, KeyError):
        valid = False
    if not valid:
        raise ValueError('DATABASE_TARGET_REJECTED')
    engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args={'connect_timeout':5})
    with engine.connect() as conn:
        if conn.scalar(text("SELECT current_setting('cluster_name')")) != cluster:
            engine.dispose()
            raise ValueError('DATABASE_IDENTITY_REJECTED')
    return engine
