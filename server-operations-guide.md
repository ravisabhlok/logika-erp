# Logika ERP — Server Operations Guide (logika, 192.168.1.235)

Quick reference for day-to-day work on the office server now that the app
is live there. Keep this updated as things change — see the changelog at
the bottom.

---

## How to pull an update and restart the app

Whenever you've committed and pushed a change from the Windows dev PC
(`C:\Python\ERP`) to GitHub, do this on the server to bring it live:

```
ssh <your-ssh-user>@192.168.1.235
cd /opt/logika-erp

sudo -u erp git pull origin main

# only if requirements.txt changed:
sudo -u erp ./venv/bin/pip install -r requirements.txt

# only if there's a new migration (safe to run every time — no-ops if current):
sudo -u erp ./venv/bin/alembic upgrade head

# restart so the new code actually takes effect:
sudo systemctl restart logika-erp

# confirm it came back up clean:
sudo systemctl status logika-erp
```

If something looks wrong after a restart, watch the live logs:

```
journalctl -u logika-erp -f
```

(`Ctrl+C` to stop watching — this doesn't stop the app.)

That's the whole loop: **pull → (pip install / alembic, if needed) → restart → check status/logs.**

---

## Useful commands cheat sheet

| What | Command |
|---|---|
| Check if it's running | `sudo systemctl status logika-erp` |
| Start it | `sudo systemctl start logika-erp` |
| Stop it | `sudo systemctl stop logika-erp` |
| Restart it (after an update) | `sudo systemctl restart logika-erp` |
| Watch live logs | `journalctl -u logika-erp -f` |
| See recent logs (no follow) | `journalctl -u logika-erp -n 100` |
| Re-enable auto-start (if ever disabled) | `sudo systemctl enable logika-erp` |
| Disable auto-start | `sudo systemctl disable logika-erp` |
| Edit the service definition | `sudo nano /etc/systemd/system/logika-erp.service` then `sudo systemctl daemon-reload` |

---

## Where things live

| | Path |
|---|---|
| App code (server) | `/opt/logika-erp` |
| App code (Windows dev PC) | `C:\Python\ERP` |
| systemd service file | `/etc/systemd/system/logika-erp.service` |
| Runs as user | `erp` (no login shell) |
| Database | MySQL, `logika_erp`, local to the server |
| GitHub repo (private) | `https://github.com/ravisabhlok/logika-erp` |
| App URL on LAN | `http://192.168.1.235:8000` |

---

## Changelog

### 2026-08-28 — Auto-start on boot configured

The app was running fine manually but had to be started by hand after
every reboot. Set it up to start automatically and to auto-restart if it
ever crashes.

Created `/etc/systemd/system/logika-erp.service`:

```ini
[Unit]
Description=Logika Systems ERP
After=network.target mysql.service

[Service]
Type=simple
User=erp
WorkingDirectory=/opt/logika-erp
Environment=PATH=/opt/logika-erp/venv/bin
ExecStart=/opt/logika-erp/venv/bin/python run.py
Restart=on-failure
RestartSec=5

[Install]
WantedBy=multi-user.target
```

Then:

```
sudo systemctl daemon-reload
sudo systemctl enable --now logika-erp
```

Verified with `sudo systemctl status logika-erp` and by browsing to
`http://192.168.1.235:8000` from another LAN PC — confirmed working.

**If you ever need to change how the app starts** (different port, a
different entry point, extra environment variables, etc.), this is the
file to edit — `sudo nano /etc/systemd/system/logika-erp.service`, then
`sudo systemctl daemon-reload && sudo systemctl restart logika-erp`.

**Reminder — still pending as of today** (per `deployment-log.md`): two
code fixes (the 2026-08-19 order-numbering fix and the 2026-08-27
negative-stock guards) were made on the dev PC but may not yet be pushed
to GitHub and pulled onto this server. Since the app now restarts
automatically, don't forget that a `git pull` + `systemctl restart` is
still required to actually get a fix live — auto-start does not mean
auto-update.
