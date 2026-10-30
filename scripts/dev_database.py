"""
Create the local development role and database if they do not exist (idempotent).

    uv run python -m scripts.dev_database --admin-password <postgres superuser password>

Reads the target role, password and database name from DATABASE_URL (so `.env` stays the single
source of truth), connects as a PostgreSQL superuser, and:
  1. creates the login role if missing,
  2. creates the database owned by that role if missing,
  3. runs CREATE EXTENSION IF NOT EXISTS vector in it (pgvector must be INSTALLED on the server;
     this script cannot install it and says so clearly if it is missing).

Development only: it refuses to run unless ENVIRONMENT=development. It never prints passwords.
"""

from __future__ import annotations

import argparse
import getpass
import os
import sys
from urllib.parse import unquote, urlsplit

import psycopg
from packages.config.settings import Environment, SettingsLoadError, get_settings
from psycopg import sql


def _target() -> tuple[str, str, str]:
    settings = get_settings()
    if settings.environment is not Environment.development:
        raise SettingsLoadError("scripts.dev_database only runs with ENVIRONMENT=development.")
    parts = urlsplit(str(settings.database.url))
    if not (parts.username and parts.password and parts.path.strip("/")):
        raise SettingsLoadError("DATABASE_URL must include user, password and database name.")
    return unquote(parts.username), unquote(parts.password), parts.path.strip("/")


def _admin_conninfo(args: argparse.Namespace, dbname: str) -> dict[str, object]:
    password = (
        args.admin_password
        or os.environ.get("EKC_ADMIN_PASSWORD")
        or getpass.getpass(f"Password for PostgreSQL user '{args.admin_user}': ")
    )
    return {
        "host": args.host,
        "port": args.port,
        "user": args.admin_user,
        "password": password,
        "dbname": dbname,
        "autocommit": True,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--host", default=urlsplit(str(get_settings().database.url)).hostname)
    parser.add_argument("--port", type=int, default=urlsplit(str(get_settings().database.url)).port)
    parser.add_argument("--admin-user", default="postgres")
    parser.add_argument("--admin-password", help="Or set EKC_ADMIN_PASSWORD, or be prompted.")
    args = parser.parse_args(argv)

    role, role_password, database = _target()
    with psycopg.connect(**_admin_conninfo(args, "postgres")) as admin:  # type: ignore[arg-type]
        if admin.execute("SELECT 1 FROM pg_roles WHERE rolname = %s", (role,)).fetchone() is None:
            admin.execute(
                sql.SQL("CREATE ROLE {} LOGIN PASSWORD {}").format(
                    sql.Identifier(role), sql.Literal(role_password)
                )
            )
            print(f"Created role '{role}'.")
        else:
            print(f"Role '{role}' already exists (password left unchanged).")
        if (
            admin.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,)).fetchone()
            is None
        ):
            admin.execute(
                sql.SQL("CREATE DATABASE {} OWNER {}").format(
                    sql.Identifier(database), sql.Identifier(role)
                )
            )
            print(f"Created database '{database}'.")
        else:
            print(f"Database '{database}' already exists.")

    with psycopg.connect(**_admin_conninfo(args, database)) as db:  # type: ignore[arg-type]
        try:
            db.execute("CREATE EXTENSION IF NOT EXISTS vector")
        except psycopg.errors.FeatureNotSupported:
            print(
                "pgvector is not installed on this PostgreSQL server. Install it first "
                "(see HOW_TO_RUN.md, 'pgvector'), then re-run this command.",
                file=sys.stderr,
            )
            return 2
        print("Extension 'vector' is available.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
