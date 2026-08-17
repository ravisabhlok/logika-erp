"""
First-time / new-machine setup script:
  1. Creates the MySQL database if it doesn't already exist.
  2. Brings the schema up to date via Alembic (creates all tables — or, if
     tables already exist from before Alembic was introduced, just marks
     the database as being up to date without touching them).
  3. Seeds a default admin user and the company record.

Run from the project root with:
    python scripts\\init_db.py

For any schema change after this, use `alembic upgrade head` directly
(see README.md) — you won't need to run this script again for that.
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pymysql
from sqlalchemy import inspect
from alembic.config import Config
from alembic import command

from app.config import settings
from app.database import engine, SessionLocal
from app import models  # noqa: F401  (ensures models are registered on Base)
from app.auth import hash_password

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def create_database_if_missing():
    conn = pymysql.connect(
        host=settings.DB_HOST,
        port=int(settings.DB_PORT),
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.DB_NAME}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
        print(f"Database '{settings.DB_NAME}' is ready.")
    finally:
        conn.close()


def upgrade_schema():
    alembic_cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    inspector = inspect(engine)
    table_names = inspector.get_table_names()

    has_no_tables = "items" not in table_names
    # "production_orders" is the newest table as of the current baseline
    # migration — used as the marker for "this database already has every
    # table the models currently define".
    has_all_tables = "items" in table_names and "production_orders" in table_names

    if has_no_tables:
        command.upgrade(alembic_cfg, "head")
        print("Created all tables via Alembic.")
    elif has_all_tables:
        # Tables already exist (this database predates Alembic being added)
        # — just record it as up to date rather than trying to re-create
        # tables that are already there.
        command.stamp(alembic_cfg, "head")
        print("Existing tables found — marked database as up to date (no changes made).")
    else:
        raise RuntimeError(
            "This database has some tables but not all of them (looks like it predates "
            "the Bill of Materials / Production feature). Ask Claude to bring it up to "
            "date before running this again — don't guess at fixing this by hand."
        )


def seed_data():
    db = SessionLocal()
    try:
        if db.query(models.Company).count() == 0:
            db.add(models.Company(name=settings.COMPANY_NAME))
            print(f"Seeded company record: {settings.COMPANY_NAME}")

        if db.query(models.User).filter(models.User.username == "admin").first() is None:
            db.add(models.User(
                username="admin",
                password_hash=hash_password("admin123"),
                full_name="Administrator",
                role="admin",
                is_active=True,
            ))
            print("Seeded default admin user -> username: admin / password: admin123")
            print("IMPORTANT: log in and change this password immediately.")

        db.commit()
    finally:
        db.close()


if __name__ == "__main__":
    create_database_if_missing()
    upgrade_schema()
    seed_data()
    print("Database setup complete.")
