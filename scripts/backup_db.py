"""
Creates a timestamped mysqldump backup of the logika_erp database.

Run from the project root with:
    python scripts\\backup_db.py
    python scripts\\backup_db.py --out-dir D:\\Backups
    python scripts\\backup_db.py --gzip

Each run writes one file named "<DB_NAME>_<YYYYMMDD>_<HHMMSS>.sql" (or
".sql.gz" with --gzip) into backups\\ under the project root by default —
nothing already there is ever touched or overwritten, so this is safe to
schedule (e.g. Windows Task Scheduler, once a day) without babysitting it.
To keep it from filling the disk over time, periodically delete old files
from that folder by hand, or add your own cleanup pass later.

Reads DB_HOST/DB_PORT/DB_USER/DB_PASSWORD/DB_NAME from the same .env file
the app itself uses (via app.config.settings) — nothing to configure here
separately.

Needs the MySQL client tools' `mysqldump` on this machine — the same
install that ships with MySQL Server. If it isn't on PATH, either add its
folder to PATH or pass --mysqldump-path explicitly.

To restore a plain .sql backup:
    mysql -h HOST -P PORT -u USER -p DB_NAME < backup_file.sql
For a .sql.gz backup, unzip it first, or pipe it straight in:
    gzip -dc backup_file.sql.gz | mysql -h HOST -P PORT -u USER -p DB_NAME
"""
import argparse
import gzip
import os
import shutil
import subprocess
import sys
from datetime import datetime, timedelta

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.config import settings

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
DEFAULT_BACKUP_DIR = os.path.join(PROJECT_ROOT, "backups")

# Same fixed IST offset used for on-screen timestamps elsewhere in this app
# (see app/formatting.py's IST_OFFSET) — so a backup taken late at night IST
# doesn't end up stamped with the next day's date just because the machine's
# clock happens to be UTC.
IST_OFFSET = timedelta(hours=5, minutes=30)

# Common install locations to fall back to if mysqldump isn't already on PATH.
CANDIDATE_PATHS = [
    r"C:\Program Files\MySQL\MySQL Server 8.4\bin\mysqldump.exe",
    r"C:\Program Files\MySQL\MySQL Server 8.0\bin\mysqldump.exe",
    r"C:\Program Files\MySQL\MySQL Server 5.7\bin\mysqldump.exe",
    r"C:\xampp\mysql\bin\mysqldump.exe",
    r"C:\wamp64\bin\mysql\mysql8.0.31\bin\mysqldump.exe",
]


def find_mysqldump(explicit_path: str | None) -> str:
    if explicit_path:
        if os.path.isfile(explicit_path):
            return explicit_path
        raise SystemExit(f"--mysqldump-path was given but no file exists at: {explicit_path}")

    on_path = shutil.which("mysqldump")
    if on_path:
        return on_path

    for candidate in CANDIDATE_PATHS:
        if os.path.isfile(candidate):
            return candidate

    raise SystemExit(
        "Could not find mysqldump.exe. It ships with MySQL Server (usually under "
        "'...\\MySQL Server <version>\\bin') -- either add that folder to your PATH, "
        "or re-run with --mysqldump-path \"C:\\path\\to\\mysqldump.exe\"."
    )


def build_filename(gzip_output: bool) -> str:
    stamp = (datetime.utcnow() + IST_OFFSET).strftime("%Y%m%d_%H%M%S")
    ext = "sql.gz" if gzip_output else "sql"
    return f"{settings.DB_NAME}_{stamp}.{ext}"


def run_backup(out_dir: str, gzip_output: bool, mysqldump_path: str) -> str:
    os.makedirs(out_dir, exist_ok=True)
    dest_path = os.path.join(out_dir, build_filename(gzip_output))

    cmd = [
        mysqldump_path,
        f"--host={settings.DB_HOST}",
        f"--port={settings.DB_PORT}",
        f"--user={settings.DB_USER}",
        "--single-transaction",
        "--routines",
        "--triggers",
        "--events",
        "--default-character-set=utf8mb4",
        settings.DB_NAME,
    ]

    # Pass the password via env var rather than --password=... on the
    # command line, so it doesn't sit in plain text in Task Manager /
    # `tasklist` while mysqldump is running.
    env = os.environ.copy()
    if settings.DB_PASSWORD:
        env["MYSQL_PWD"] = settings.DB_PASSWORD

    print(f"Backing up '{settings.DB_NAME}' from {settings.DB_HOST}:{settings.DB_PORT} -> {dest_path}")

    try:
        if gzip_output:
            with gzip.open(dest_path, "wb") as out_file:
                result = subprocess.run(cmd, stdout=out_file, stderr=subprocess.PIPE, env=env)
        else:
            with open(dest_path, "wb") as out_file:
                result = subprocess.run(cmd, stdout=out_file, stderr=subprocess.PIPE, env=env)
    except FileNotFoundError:
        raise SystemExit(f"Could not run '{mysqldump_path}' -- check the path is correct.")

    if result.returncode != 0:
        # Don't leave a broken/partial backup file lying around looking valid.
        if os.path.exists(dest_path):
            os.remove(dest_path)
        raise SystemExit(
            f"mysqldump failed (exit code {result.returncode}):\n"
            f"{result.stderr.decode(errors='replace')}"
        )

    size_mb = os.path.getsize(dest_path) / (1024 * 1024)
    print(f"Backup complete: {dest_path} ({size_mb:.2f} MB)")
    return dest_path


def parse_args():
    parser = argparse.ArgumentParser(description="Back up the ERP MySQL database to a timestamped .sql file.")
    parser.add_argument(
        "--out-dir", default=DEFAULT_BACKUP_DIR,
        help=f"Folder to write the backup into (default: {DEFAULT_BACKUP_DIR})",
    )
    parser.add_argument("--gzip", action="store_true", help="Compress the backup (.sql.gz instead of .sql).")
    parser.add_argument(
        "--mysqldump-path", default=None,
        help="Full path to mysqldump.exe, if it isn't on PATH and not in one of the usual install locations.",
    )
    return parser.parse_args()


if __name__ == "__main__":
    args = parse_args()
    resolved_mysqldump = find_mysqldump(args.mysqldump_path)
    run_backup(args.out_dir, args.gzip, resolved_mysqldump)
