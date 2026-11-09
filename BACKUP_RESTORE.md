# Backup and Restore (MySQL `logika_erp`)

Backups are compressed `mysqldump` files named `logika_erp_YYYYMMDD_HHMMSS.sql.gz`,
made by `scripts/backup_db.py` and uploaded to Google Drive with rclone.

- Google Drive folder ID: `16qIO0_ZJlRKti_Jp9RkD-Iioc4ipBjag` (account: technologieslogika@gmail.com)
- rclone remote name: `gdrive`

## Server (Linux, `/opt/logika-erp`)

### How the daily backup is set up

Script `/opt/logika-erp/backup_daily.sh`, run by the `erp` user's cron at 2 AM:

```bash
#!/bin/bash
set -e
cd /opt/logika-erp
FOLDER=16qIO0_ZJlRKti_Jp9RkD-Iioc4ipBjag
./venv/bin/python scripts/backup_db.py --gzip --out-dir /opt/logika-erp/backups
rclone copy /opt/logika-erp/backups gdrive: --drive-root-folder-id $FOLDER --include "*.sql.gz"
# keep 14 days locally, 60 days on Drive
find /opt/logika-erp/backups -name "*.sql.gz" -mtime +14 -delete
rclone delete gdrive: --drive-root-folder-id $FOLDER --min-age 60d --include "*.sql.gz"
```

Cron line (`sudo -u erp crontab -e`):

```
0 2 * * * /opt/logika-erp/backup_daily.sh >> /opt/logika-erp/backups/backup.log 2>&1
```

Do not use the `/run/user/1001/gvfs/...` path for scheduled jobs: it is a desktop
mount that only exists while that user is logged in graphically.

### Check it is working

```bash
sudo -u erp crontab -l
tail /opt/logika-erp/backups/backup.log
sudo -u erp rclone ls gdrive: --drive-root-folder-id 16qIO0_ZJlRKti_Jp9RkD-Iioc4ipBjag
```

### If Drive upload fails with `invalid_grant`

The rclone login token expired or was revoked. Re-link it (answer `n` to the
team drive question):

```bash
sudo -u erp rclone config reconnect gdrive:
```

If it keeps expiring weekly, the Google Cloud OAuth consent screen is in
"Testing" mode: switch it to "In production", or use rclone's built-in client ID.

### Restore

Restoring into the live database overwrites its current data. Test first.

1. Test restore into a scratch database:

```bash
mysql -u root -p -e "CREATE DATABASE logika_erp_test CHARACTER SET utf8mb4"
gzip -dc /opt/logika-erp/backups/logika_erp_YYYYMMDD_HHMMSS.sql.gz | mysql -u root -p logika_erp_test
mysql -u root -p -e "SELECT COUNT(*) FROM logika_erp_test.purchase_orders"
mysql -u root -p -e "DROP DATABASE logika_erp_test"
```

2. Real restore:

```bash
sudo systemctl stop logika-erp
gzip -dc /opt/logika-erp/backups/logika_erp_YYYYMMDD_HHMMSS.sql.gz | mysql -u root -p logika_erp
sudo systemctl start logika-erp
```

3. If the backup is older than the current code, bring the schema up to date:

```bash
cd /opt/logika-erp
sudo -u erp ./venv/bin/alembic upgrade head
```

To fetch a backup from Drive onto the server:

```bash
sudo -u erp rclone copy gdrive:FILENAME.sql.gz /opt/logika-erp/backups --drive-root-folder-id 16qIO0_ZJlRKti_Jp9RkD-Iioc4ipBjag
```

Use the `DB_USER` / `DB_PASSWORD` from `/opt/logika-erp/.env` if they are not `root`.

## Windows (local PC)

`python scripts\backup_db.py --gzip` works on Windows as is (finds `mysqldump.exe`
in the usual MySQL install folders; use `--mysqldump-path` if not).

### Daily run with Task Scheduler

`C:\Python\ERP\backup_daily.bat`:

```bat
@echo off
cd /d C:\Python\ERP
python scripts\backup_db.py --gzip --out-dir "C:\Python\ERP\backups"
rclone copy "C:\Python\ERP\backups" gdrive: --drive-root-folder-id 16qIO0_ZJlRKti_Jp9RkD-Iioc4ipBjag --include "*.sql.gz"
forfiles /p "C:\Python\ERP\backups" /m *.sql.gz /d -14 /c "cmd /c del @path"
```

(With Google Drive for Desktop instead of rclone, drop the `rclone` line and
point `--out-dir` at the Drive folder.)

```powershell
schtasks /Create /TN "ERP DB Backup" /TR "C:\Python\ERP\backup_daily.bat" /SC DAILY /ST 02:00
```

### Restore on Windows

There is no `gzip` command: unpack with Python, then import.

```powershell
python -c "import gzip,shutil; shutil.copyfileobj(gzip.open(r'C:\path\file.sql.gz','rb'), open(r'C:\path\file.sql','wb'))"
cmd /c "mysql -u root -p logika_erp < C:\path\file.sql"
```

Then run `alembic upgrade head` if the backup is older than the current code.
