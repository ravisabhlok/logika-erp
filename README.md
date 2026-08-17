# Logika Systems ERP

Internal ERP for **Logika Systems India Pvt Ltd** — Sales, Purchase, and
Inventory management, built with FastAPI + MySQL. Designed to run on one
office PC and be reached by other PCs over the LAN via a browser.

## Modules (v1)

- Master data: Customers, Vendors, Items (with multi-category tags, pricing, reorder level)
- Sales: Sales orders (draft -> confirmed -> delivered), auto stock deduction on delivery
- Purchase: Purchase orders (draft -> ordered -> received), auto stock increase on receipt, per-unit serial number capture for serialized items
- Production: Bill of Materials on any item ("assembled from other items"), Production Orders (draft -> completed) that consume component stock and add finished stock
- Inventory: Live stock levels, low-stock dashboard alerts, per-item stock ledger, manual adjustments
- Auth: Username/password login (sessions), admin/staff roles, password change

## 1. Prerequisites

- Python 3.10+ (check with `python --version`)
- MySQL Server running locally (you already have this installed)

## 2. Setup

Open a terminal in this folder (`C:\python\ERP`) and run:

```
pip install -r requirements.txt
copy .env.example .env
```

Edit `.env` and set your MySQL credentials (`DB_USER`, `DB_PASSWORD`), and
change `SECRET_KEY` to any random string. `DB_NAME` defaults to
`logika_erp` — the setup script below will create it for you if it
doesn't exist yet.

## 3. Initialize the database

```
python scripts\init_db.py
```

This creates the `logika_erp` database, all tables, the company record,
and a default login:

```
username: admin
password: admin123
```

**Log in and change this password immediately** (top-right menu -> My Account).

## 4. Run the server

```
python run.py
```

By default it listens on `0.0.0.0:8000`, so:
- On this PC: open `http://localhost:8000`
- From another PC on the same office network: open `http://<this-PC's-IP>:8000`
  (find this PC's IP with `ipconfig`, look for "IPv4 Address")

To restrict access to this PC only, set `HOST=127.0.0.1` in `.env`.

## Keeping the database up to date (Alembic)

Schema changes are tracked as versioned migration files under `migrations/versions/`
(same tool/idea as `flask db upgrade` in the CRM2 project, called directly here
since this app isn't Flask).

- **After pulling new code that includes a schema change** (on this PC, or when
  you copy the project to the office server): run

  ```
  alembic upgrade head
  ```

  Safe to run anytime, on any machine — it only applies whatever that specific
  database is missing, and does nothing if it's already current.

- You won't normally need to touch `migrations/versions/` yourself — those files
  come from me whenever a change requires one. Never delete them; they're the
  permanent history every database relies on to know what it still needs.

## Project structure

```
app/
  main.py           FastAPI app + route registration
  config.py         Settings loaded from .env
  database.py       SQLAlchemy engine/session
  models.py         All database tables
  auth.py           Login/session/password helpers
  routers/          One file per module (customers, vendors, items, sales, purchase, inventory, production, auth)
  templates/         Jinja2 + Bootstrap 5 pages
  static/            Custom CSS
migrations/
  versions/          Alembic migration history (permanent — never delete)
  env.py             Wires Alembic to app.models + .env credentials
scripts/
  init_db.py        First-time setup: creates DB, brings schema up to date, seeds data
  reset_db.py       Wipes and rebuilds the database from scratch (destroys data)
run.py               Starts the server
alembic.ini
requirements.txt
.env.example
```

## Notes / next steps

- This is v1: Sales, Purchase, Inventory, and the master data they depend on.
  Accounting/finance and HR/payroll were intentionally left out of scope for
  now — let me know when you want those added.
- Stock quantities only change through Purchase receipt, Sales delivery, or
  a manual Inventory adjustment — never by editing an Item directly — so the
  stock ledger stays a reliable audit trail.
- Passwords are hashed with bcrypt; sessions are signed cookies (not JWT),
  which is the simpler, appropriate choice for a LAN-only internal tool.
