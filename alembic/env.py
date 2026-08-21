"""Alembic environment configured from the same runtime database URL."""

from logging.config import fileConfig
import os

from alembic import context
from sqlalchemy import engine_from_config, pool

from ting_ting.database import enable_sqlite_runtime_pragmas
from ting_ting.models import Base


config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

database_url = os.environ.get("TING_DATABASE_URL")
if database_url:
    config.set_main_option("sqlalchemy.url", database_url.replace("%", "%%"))

target_metadata = Base.metadata


def run_migrations_offline() -> None:
    context.configure(
        url=config.get_main_option("sqlalchemy.url"),
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    if connectable.dialect.name == "sqlite":
        # Same runtime pragmas as the app engine (T-022), EXCEPT
        # foreign_keys: with it ON during a migration, ``DROP TABLE`` in a
        # table rebuild cascades to child tables and wipes their rows.
        # busy_timeout keeps migrations retrying on a busy file; WAL avoids
        # blocking readers during DDL.
        enable_sqlite_runtime_pragmas(connectable, enforce_foreign_keys=False)

    with connectable.connect() as connection:
        # SQLite supports transactional DDL.  Let Alembic wrap each migration
        # file in one real transaction so DDL (e.g. the posts rebuild in
        # 20260819_0007) is atomic and the version stamp commits together.
        # Without this, SQLite's default non-transactional DDL leaves a
        # data-loss window between a DROP TABLE and its RENAME.
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            compare_type=True,
            transactional_ddl=True,
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
