"""
DANGER: Drops and recreates the entire logika_erp database (every table,
gone), rebuilds the schema fresh via Alembic, then reseeds the admin user
and company record.

Use this when the schema has changed in a way that can't be safely migrated
in place, and you're OK losing existing data. For anything else, prefer
`alembic upgrade head` (see README.md) — it keeps your data.

Run from the project root with:
    python scripts\\reset_db.py
"""
import sys
import os

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import pymysql
from alembic.config import Config
from alembic import command

from app.database import SessionLocal
from app import models  # noqa: F401  (ensures models are registered on Base)
from app.config import settings
from app.auth import hash_password

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))


def confirm():
    print(f"This will DROP the entire '{settings.DB_NAME}' database (every table, all data) and recreate it empty.")
    answer = input("Type 'yes' to continue: ").strip().lower()
    if answer != "yes":
        print("Aborted. Nothing was changed.")
        sys.exit(0)


def drop_and_recreate_database():
    conn = pymysql.connect(
        host=settings.DB_HOST,
        port=int(settings.DB_PORT),
        user=settings.DB_USER,
        password=settings.DB_PASSWORD,
    )
    try:
        with conn.cursor() as cursor:
            cursor.execute(f"DROP DATABASE IF EXISTS `{settings.DB_NAME}`")
            cursor.execute(
                f"CREATE DATABASE `{settings.DB_NAME}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
        print(f"Dropped and recreated database '{settings.DB_NAME}'.")
    finally:
        conn.close()


def rebuild_schema():
    alembic_cfg = Config(os.path.join(PROJECT_ROOT, "alembic.ini"))
    command.upgrade(alembic_cfg, "head")
    print("Rebuilt schema via Alembic.")


def seed_data():
    db = SessionLocal()
    try:
        db.add(models.Company(name=settings.COMPANY_NAME))
        db.add(models.User(
            username="admin",
            password_hash=hash_password("admin123"),
            full_name="Administrator",
            role="admin",
            is_active=True,
        ))
        db.commit()
        print("Seeded company record and default admin user -> username: admin / password: admin123")
        print("IMPORTANT: log in and change this password immediately.")
    finally:
        db.close()


if __name__ == "__main__":
    confirm()
    drop_and_recreate_database()
    rebuild_schema()
    seed_data()
    print("Database reset complete.")
