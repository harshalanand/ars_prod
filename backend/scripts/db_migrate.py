"""
db_migrate.py — thin wrapper around `alembic` for use in deploys and CI.

Why not just call `alembic upgrade head` directly? Two reasons:
  1. We want a single command that works the same on Windows, Linux, and
     inside the Azure App Service SCM container (where `alembic` may not be
     on PATH but `python -m alembic` always is).
  2. We print a banner with the resolved DB target before running, so a
     misconfigured .env is obvious in deploy logs.

Usage (run from backend/):
    python -m scripts.db_migrate upgrade head
    python -m scripts.db_migrate current
    python -m scripts.db_migrate history
    python -m scripts.db_migrate downgrade -1
    python -m scripts.db_migrate stamp head        # baseline an existing DB
"""
from __future__ import annotations

import os
import sys

# Make `app.*` importable when launched from backend/
HERE = os.path.dirname(os.path.abspath(__file__))
BACKEND_DIR = os.path.dirname(HERE)
if BACKEND_DIR not in sys.path:
    sys.path.insert(0, BACKEND_DIR)


def _resolve_target() -> str:
    """Return a one-line description of WHICH database we're about to touch.
    Reads the same Settings the app uses, so what you see here is what
    Alembic will hit."""
    from app.core.config import get_settings

    s = get_settings()
    c = s._db()
    return (
        f"server={c['server']} "
        f"database={c['system_database']} "
        f"user={c['username']} "
        f"env={s.APP_ENV}"
    )


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2

    print("=" * 78)
    print("ARS Alembic migration")
    print("  target:", _resolve_target())
    print("  cmd:   ", "alembic", *argv)
    print("=" * 78, flush=True)

    # Defer the import — fails clearly if alembic isn't installed.
    try:
        from alembic.config import main as alembic_main
    except ImportError:
        print(
            "ERROR: alembic is not installed. Run `pip install -r requirements.txt`.",
            file=sys.stderr,
        )
        return 1

    # alembic looks for alembic.ini in cwd. Always run from backend/.
    cwd = os.getcwd()
    if os.path.abspath(cwd) != os.path.abspath(BACKEND_DIR):
        os.chdir(BACKEND_DIR)

    return alembic_main(argv=argv) or 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
