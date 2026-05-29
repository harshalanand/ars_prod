"""
Alembic migration environment for the ARS System DB (Claude).

This env.py:
  1. Loads the same Settings the app uses (so DEV/STAGING/PROD just work via .env).
  2. Imports `app.models` so every model class registers itself on Base.metadata.
  3. Targets Base.metadata for autogeneration.
  4. Uses pyodbc's `fast_executemany` for batch DDL on Azure SQL.

Only the System DB (Claude) is managed here — the Data DB (Rep_data) holds
dynamically-created tables (uploads, MSA output) that are not SQLAlchemy
models and therefore are not managed by Alembic.
"""
from __future__ import annotations

import os
import sys
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# ---------------------------------------------------------------------------
# Make `app.*` importable when alembic is launched from backend/
# ---------------------------------------------------------------------------
BACKEND_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)

# Import settings + Base + all models. Importing the package triggers each
# model file's `class X(Base):` registration into Base.metadata.
from app.core.config import get_settings  # noqa: E402
from app.database.session import Base  # noqa: E402
import app.models  # noqa: E402, F401 — registers all model classes

# ---------------------------------------------------------------------------
# Alembic Config
# ---------------------------------------------------------------------------
config = context.config

# Inject the live DB URL into the config so alembic can build an engine.
# Allow CLI override via `-x url=...` for ad-hoc targeting of a different DB
# (handy when you want to stamp a staging DB without touching .env).
x_args = context.get_x_argument(as_dictionary=True)
db_url = x_args.get("url") or get_settings().DATABASE_URL
config.set_main_option("sqlalchemy.url", db_url)

if config.config_file_name is not None:
    fileConfig(config.config_file_name)

target_metadata = Base.metadata


# ---------------------------------------------------------------------------
# Autogenerate filters
# ---------------------------------------------------------------------------
# Tables that exist in Claude DB but are NOT SQLAlchemy models (created by
# raw SQL, e.g. PresetManager's ARS_CONT_PRESETS, alembic_version itself).
# Listing them here keeps autogenerate from proposing to DROP them.
IGNORED_TABLES = {
    "alembic_version",
    # Add any other raw-SQL-managed tables here as you discover them.
    # e.g. "ARS_CONT_PRESETS",
}


def include_object(object_, name, type_, reflected, compare_to):
    """Return False to exclude an object from autogenerate diffs."""
    if type_ == "table" and name in IGNORED_TABLES:
        return False
    return True


# ---------------------------------------------------------------------------
# Migration runners
# ---------------------------------------------------------------------------
def run_migrations_offline() -> None:
    """Generate SQL scripts without connecting to a DB.

    Use `alembic upgrade head --sql > migration.sql` to produce a script the
    DBA can review before running against prod.
    """
    context.configure(
        url=db_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        include_object=include_object,
        compare_type=True,           # detect column type changes
        compare_server_default=True, # detect default value changes
    )
    with context.begin_transaction():
        context.run_migrations()


def run_migrations_online() -> None:
    """Connect to the DB and apply migrations."""
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )
    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
            include_object=include_object,
            compare_type=True,
            compare_server_default=True,
            # MSSQL transactional DDL: each migration in its own batch.
            transaction_per_migration=True,
        )
        with context.begin_transaction():
            context.run_migrations()


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
