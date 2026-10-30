"""
Alembic environment.

Migrations run synchronously (via the `psycopg` sync driver) even though the
application uses `asyncpg` at runtime — this is the standard, supported pattern:
Alembic's autogenerate/compare machinery is built around a sync `Connection`, and DDL
statements don't benefit from async execution anyway. We derive the sync URL from the
same typed `Settings` the application uses (`packages/config/settings.py`), swapping
only the driver component, so there is exactly one source of truth for "which database"
and the localhost-forbidden-in-production check still applies.
"""

from __future__ import annotations

from logging.config import fileConfig

from alembic import context
from packages.config.settings import get_settings, to_sync_database_url
from packages.database.models import Base
from sqlalchemy import engine_from_config, pool

config = context.config

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


def _sync_database_url() -> str:
    settings = get_settings()
    # asyncpg URL -> psycopg (sync) URL for Alembic's own connection (driver and ssl->sslmode).
    return to_sync_database_url(str(settings.database.url))


def run_migrations_offline() -> None:
    url = _sync_database_url()
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    configuration = config.get_section(config.config_ini_section) or {}
    configuration["sqlalchemy.url"] = _sync_database_url()

    connectable = engine_from_config(
        configuration,
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            compare_server_default=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
