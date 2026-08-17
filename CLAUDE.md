# CLAUDE.md

This file provides guidance to Claude when working in this repository.

## Project overview

Internal ERP for **Logika Systems India Pvt Ltd** — Sales, Purchase, and
Inventory, built with FastAPI + SQLAlchemy + MySQL. Server-rendered
(Jinja2 + Bootstrap 5 + Select2), no separate frontend build. Designed to
run on one office PC and be reached by other PCs over the LAN via a browser.

This is a separate, unrelated codebase from `C:\Python\CRM2` (the LaseTek
Flask CRM) — they only happen to share the same local MySQL server. CRM2's
database is `lasetek_flask`; this project's database is `logika_erp`. Never
touch `lasetek_flask` or files under `C:\Python\CRM2` while working here.

This project is deployed to more than one machine (local dev PC, office
server), same as CRM2 — so schema changes must be applied to each database
independently. See "Database migrations" below.

## Commands

- Install deps: `pip install -r requirements.txt`
- Run the app: `python run.py` (serves on `0.0.0.0:8000` by default, configurable via `.env`)
- First-time DB setup (new machine, database doesn't exist yet): `python scripts\init_db.py` — creates the database if missing, brings the schema up to date via Alembic, seeds the company record and a default `admin`/`admin123` login. Safe to re-run.
- Apply schema changes to an existing database (e.g. after pulling code with new migrations, or on a second machine): `alembic upgrade head`.
- Full reset (drops and recreates every table, destroys all data): `python scripts\reset_db.py` — asks for a typed `yes` confirmation. Only use this deliberately, e.g. when you'd rather lose local test data than write a tricky migration.

## Database migrations

Schema changes go through **Alembic** (`migrations/` folder), the same tool CRM2 uses via `flask db upgrade` — just invoked directly here since this project isn't Flask. `migrations/env.py` is wired to `app.models` and reads the DB connection from `.env` (via `app.config.settings`), so it always targets whichever database your `.env` points at.

Workflow for any schema change:
1. Edit `app/models.py`.
2. Generate a migration file: `alembic revision --autogenerate -m "short description"`. Check the generated file in `migrations/versions/` before running it — autogenerate is usually right but not infallible (e.g. it won't detect a plain column rename, it'll see that as drop+add).
3. Apply it locally: `alembic upgrade head`.
4. Commit the new file under `migrations/versions/` — **never delete migration files**, unlike the old one-off `scripts/one_*.py` scripts. They're permanent history; every database (dev machine, office server) needs the full chain to know what it's missing.
5. On any other machine running this app (e.g. the office server), after pulling the new code: just run `alembic upgrade head`. It only applies what that specific database hasn't seen yet — safe to run repeatedly, and safe even if that machine is several migrations behind.

Before Alembic was added, schema changes were handled by disposable `scripts/one_*.py` scripts (see git history). Those are gone now — don't recreate that pattern for schema changes. A `one_*.py` script is still the right tool for a genuine one-time *data* fix that isn't a schema change (e.g. backfilling/correcting existing rows).

## Architecture

**Stack:** FastAPI, SQLAlchemy (raw ORM, not Flask-SQLAlchemy), PyMySQL driver, Jinja2 templates, session-based auth via Starlette's `SessionMiddleware` (signed cookie, not JWT — appropriate for a LAN-only internal tool), bcrypt for password hashing (used directly, not through passlib — passlib is unmaintained and breaks with modern bcrypt releases).

**Layout:**
- `app/main.py` — creates the FastAPI app, registers `SessionMiddleware`, mounts `/static`, registers all routers, and handles `NotAuthenticatedException` by redirecting to `/login`.
- `app/config.py` — `Settings` loaded from `.env` (see `.env.example`).
- `app/database.py` — SQLAlchemy `engine`/`SessionLocal`/`Base`, and the `get_db()` FastAPI dependency.
- `app/models.py` — every table, in one file.
- `app/auth.py` — `hash_password`/`verify_password` (bcrypt), `require_login`/`require_admin` dependencies, `get_current_user`, plus the permission system (see "User roles / permissions" below): `MODULES`, `get_user_module_permissions()`, `require_module_permission(module, action)`.
- `app/routers/<name>.py` — one file per module: `auth_router`, `dashboard`, `customers`, `vendors`, `items`, `sales`, `purchase`, `inventory`, `production`, `roles`, `users`. Each owns its own prefix and its own templates under `app/templates/<name>/`.
- `app/templates/` — Jinja2, extends `base.html` (top navbar, Bootstrap 5 + Select2 + jQuery loaded from CDN in `base.html`; page-specific JS goes in a `{% block extra_js %}` at the bottom, *after* jQuery/Select2 load — don't put `$(...)` calls directly in `{% block content %}`, they'll run before jQuery is loaded).

**Data model:**
- `Item` has no SKU field (removed by request — was judged not useful). Identification is by name.
- `Item.hsn_code` (free-text, up to 10 chars) and `Item.gst_percentage` (`Numeric(5,2)`) exist to support GST invoicing later — v1 has no invoicing/billing module yet, these are just captured on the item now so they're available when that's built. Both are optional (nullable/defaults to 0) so existing items aren't broken by the new fields. Kept as plain columns directly on `Item` rather than a separate HSN lookup table (unlike CRM2's `HSNTable`, which maps one HSN code to one shared GST rate across items) — simpler for now; revisit as a shared lookup table if the same HSN/rate combo ends up duplicated across many items and needs single-point-of-update.
- `Category` is a free-form tag, not a strict single-value field: `Item.categories` is many-to-many via the `item_categories` association table, so one item can carry several tags (e.g. "Sensor" + "Spare") for cross-cutting reports later. The item form's category picker is a Select2 multi-select with `tags: true` — pick existing categories or type a new one to create it inline. `items` list page supports filtering by one or more categories via repeated `?category=` query params, ANDed together (item must have every selected tag).
- `Item.has_serial` flags items that need per-unit serial tracking. When a `PurchaseOrder` moves from `ordered` to `received`, it always goes through `GET/POST /purchase/{id}/receive` (never a one-click status change) — for any line item with `has_serial`, the receive form requires exactly one serial number per unit (comma/newline separated), rejects short/long counts and duplicates already recorded against that item, and only commits stock + serials + status together if everything validates. Serials are stored in `ItemSerial`, viewable on the purchase order detail page and on that item's Inventory ledger page. There is no equivalent for tracking which serial goes out on a sales delivery yet — only inbound (received → in stock) is tracked.
- Stock (`Item.current_stock`) only ever changes through three paths, each logging a `StockTransaction` row: Purchase receipt (`IN`), Sales delivery (`OUT`), or a manual adjustment on the Inventory page (`ADJUST`). Never edit `current_stock` directly — the ledger is the audit trail.
- Sales orders: `draft -> confirmed -> delivered` (or `cancelled`). Stock deducts on `delivered`.
- Purchase orders: `draft -> ordered -> received` (or `cancelled`). Stock increases on `received`, via the receive flow above.
- `Item.is_assembly` + `BomComponent` model a Bill of Materials directly on the item form (checkbox reveals a component/quantity picker) — "this item is built from these other items". Building stock of an assembled item goes through a `ProductionOrder` (`draft -> completed`, or `cancelled` while still draft), same lifecycle shape as Sales/Purchase orders. Creating the order snapshots the current BOM into `ProductionOrderComponent` rows (so later edits to the recipe don't rewrite the history of past orders); completing it validates every component has enough stock first (rejects with a shortage list if not, same validate-before-committing pattern as the serial receive flow), then atomically deducts each component (`StockTransaction` `OUT`, `reference_type='production_order'`) and adds the finished item (`IN`, same reference). BOM assignment guards against an item listing itself as its own component, but does not detect deeper cycles (A needs B needs A) — components can themselves be assemblies, since building a sub-assembly is just its own separate Production Order that adds to that item's stock first.
- **Clone**: the item detail page has a Clone button (gated on `items`/`add`, since it creates a new item) linking to `GET /items/new?clone_from={id}`. This reuses the plain new-item form — `new_item_form` loads the source item and `form.html` pre-fills every field from it (name gets a " (Copy)" suffix), including categories, HSN/GST, prices, `has_serial`/`is_assembly`, and the BOM component rows. Two things are deliberately **not** copied: attachments (a brand-new item has no rows to attach to yet — the form's attachments section only ever renders for an already-saved `item`, never for `clone_source`) and stock (`current_stock` always starts at the normal "Opening Stock" default of 0, never the source's current stock, since stock represents real physical units, not a template to duplicate). Nothing is written to the database until the user reviews the pre-filled form and hits Save — cloning doesn't silently create a duplicate row. Template note: `form.html` computes `{% set source = item or clone_source %}` separately in **both** `{% block content %}` and `{% block extra_js %}` — Jinja's `{% set %}` is scoped per-block, so a variable set in one block isn't visible in another; this bit us once already (the BOM prefill silently rendered empty) — don't hoist it to just one block if the template gains more blocks later.
- `ItemAttachment` lets an item carry multiple pictures and PDFs. Upload/delete controls live on the item **edit** page ("Photos" / "Documents" sections), not the view page — uploads and deletes both take effect immediately on click (there's no way to stage a file the way text fields are staged behind the form's Save button), so they're grouped with other instant actions rather than the view page's Edit/Delete buttons. The view page shows a read-only thumbnail/PDF-link preview when attachments exist, pointing the user to Edit to manage them. Deliberately **not** stored as DB blobs — the file bytes live on disk in one flat folder, `app/static/uploads/` (served straight back out via the existing `/static` mount, so `<img>`/download links just point at `/static/uploads/<stored_filename>`); the DB (`item_attachments` table) only holds `original_filename`, a generated unique `stored_filename` (`uuid4().hex + extension`, so two items' uploads can never collide even in the shared flat folder), and `kind` (`image` or `pdf`, inferred from the upload's extension — jpg/jpeg/png/gif/webp vs pdf, anything else is rejected). `POST /items/{id}/attachments` accepts multiple files at once (`files: list[UploadFile]`); `POST /items/{id}/attachments/{attachment_id}/delete` removes both the DB row and the file from disk. Deleting the item itself removes its attachment files from disk too (the DB rows cascade via the relationship, but cascade alone wouldn't touch the filesystem). Because these are real files outside the database, `app/static/uploads/` is gitignored (contents, not the folder) and isn't part of any DB backup/restore — there is currently no separate backup step for it.

- `Customer`/`Vendor` each have a detail/view page (`/customers/{id}`, `/vendors/{id}`) mirroring the Items pattern: name is clickable from the list instead of per-row Edit/Delete buttons, Edit/Delete live top-right on the detail page, and the list has a client-side live search box. The detail page also shows that customer's/vendor's 10 most recent Sales/Purchase Orders (queried directly by `customer_id`/`vendor_id`, not via the unordered ORM relationship) as a quick jumping-off point — this is read-only convenience, not new functionality; creating an order from there still goes to the plain `/sales/new` or `/purchase/new` form (no customer/vendor pre-fill yet).

- **Bill To / Ship To**: `Customer.address` is a single field, but a real customer often has more than one location (head office vs a warehouse vs a project site) — `CustomerAddress` is a small saved address book per customer (`label` + address/city/state/country/optional `gstin`, since a customer with multiple state registrations legitimately has a different GSTIN per address), managed from the customer detail page (`/customers/{id}/addresses/...`). `SalesOrder.billing_address`/`shipping_address` are plain text **snapshots**, not FKs to a `CustomerAddress` row — same reasoning as `SalesOrderItem.unit_price` snapshotting `Item.sales_price`: an order keeps whatever address text applied when it was placed even if the saved address is later edited or deleted, and it also has to support a one-off address that was never worth saving to the book at all. On the order form, picking a saved address (or the customer's own "Main Address", synthesized client-side from data attributes on the customer `<select>` options) just pre-fills the Bill To / Ship To textarea — the textarea itself is what's submitted and stays freely editable. Saved addresses are fetched on demand via `GET /customers/{id}/addresses` (JSON) rather than preloaded for every customer up front.

  Ship To can also go to a **different customer entirely** — a dealer/OEM order shipped direct to their end customer's site. `SalesOrder.ship_to_customer_id` (nullable FK to `customers`) captures that; null means "ships to the same customer that's billed". It's metadata for traceability/filtering only — the address that's actually used is still the `shipping_address` snapshot, not derived from this FK at read/render time. The order form's "Different customer" checkbox swaps the Ship To address picker's source between the billed customer's saved addresses and the separately-picked ship-to customer's.

- **Purchase requirements ("What to Order")**: `GET /purchase/requirements` (`app/routers/purchase.py:compute_global_requirements`, walk logic shared via `app/requirements.py:explode_demand`) is the one company-wide answer to "what do I need to buy" — deliberately not computed per-sales-order, since two open orders each wanting the same item would look individually fine but jointly overrun stock if netted separately (an earlier per-order version of this had exactly that bug and was removed). It aggregates demand across every `draft`/`confirmed` sales order at once, adds each item's own `reorder_level` as a minimum-stock buffer (so items with zero open sales demand still surface if they're below minimum), explodes `is_assembly` items down through their BOM when finished stock falls short, and nets the result against current stock plus anything already on a PurchaseOrder with status `ordered`. Linked from the sidebar, the Purchase list, the Dashboard's Low Stock card, and from a sales order's detail page (via `?highlight=<order_no>`, which just highlights that order's rows client-side rather than recomputing anything — there is only ever one number). **Known gap, flagged for future work**: it only knows about rows that exist as an actual `SalesOrder` — it cannot reserve stock against a verbal commitment or a quote that hasn't been entered as an order yet. If that becomes a real problem, the likely fix is a lightweight `Quote`/`Estimate` model feeding the same `demand` dict in `compute_global_requirements`, not a parallel calculation.

- `SalesOrder` also carries `customer_po_no`/`customer_po_date` (the customer's own paperwork reference, separate from our `order_no`) and `expected_shipment_date`, all optional plain fields.

- **Sales Order editing lock**: `GET/POST /sales/{id}/edit` (customer, Order No, Customer PO fields, Notes, line items) allows status `draft`, `confirmed`, or `cancelled` — not `delivered`. Stock only deducts on `delivered` (see `update_sales_status`), so editing anything up to that point — including reducing a line's quantity when a customer only picks up part of what they ordered (there's no partial-delivery flow; you edit the confirmed order down before marking it delivered) — can never desync `StockTransaction` from what's actually on the order. Editing after `delivered` isn't allowed since the line items would no longer match the stock transactions already logged against them; a post-delivery quantity correction goes through the Inventory page's manual `ADJUST` instead. Editing a `cancelled` order revives it back to `draft`.

- **Payment schedule / "Payments Due"**: real orders are rarely paid in one shot (advance now, balance on delivery, a retention held back until warranty ends, sometimes released early against a bank guarantee instead of cash) — `SalesOrderPaymentTerm` models this as a list of milestones on a sales order. `description` is deliberately free text rather than a fixed set of trigger types (advance/delivery/days-after/etc.) — real payment terms are too varied to force into a taxonomy. `percentage` is optional and only pre-fills `amount` client-side from the order's current total at the moment it's typed (same "computed default, editable after" pattern as `SalesOrderItem.unit_price` defaulting from `Item.sales_price`) — `amount` is what's actually stored and nothing re-derives it later if the order total changes. `secured_by` (`cash`/`bank_guarantee`) plus `bg_expiry_date` cover a retention released against a bank guarantee instead of held as cash.

  The percentage prefill isn't always against the same base: a common real pattern (see `payment_terms_form.html`) is an advance and a pre-dispatch payment quoted as % of the **basic** (pre-tax) order value, with GST settled entirely alongside the final installment rather than spread proportionally across each one — e.g. "10% advance, 80% before dispatch, 10% + full GST on delivery" (10%+80%+10% of basic = 100% of basic, plus the full GST amount once, still totaling exactly the Invoice Value). The "% of" selector next to Percentage (Basic Value / Invoice Value incl. GST / Basic Value + Full GST) is a client-side-only calculation aid — like `percentage` itself, the choice isn't persisted anywhere; only the resulting `amount` is saved.

  `due_date` is the real date once known, but most terms besides an advance are actually tied to a future invoice date this app doesn't have yet — no invoicing module exists (see "Known scope"). `days_after_invoice` captures that as a rule instead ("due 45 days after invoice") so it isn't lost while `due_date` sits blank; nothing resolves it into a real date today; the natural place to do that is the future Sales Order → Invoice conversion, which will know the actual invoice date. No `invoice_date` field was added to `SalesOrder` for this — that belongs on the future Invoice model (a sales order could become more than one invoice later), so the rule lives on the payment term instead of guessing at that schema now. If both `due_date` and `days_after_invoice` are set, `due_date` wins for display.

  Unlike the rest of a sales order, **payment terms are not locked by order status at all** — nothing about a term touches stock, and in practice most of these dates/amounts only become known well after the order is confirmed or even delivered. Editing the schedule is its own screen, `GET/POST /sales/{id}/payment-terms/edit` (`app/routers/sales.py:_payment_terms_from_form`/`_build_payment_term`, same repeated-field parsing pattern as line items), separate from the main order edit form and usable at any status. Marking a milestone `received` is a further-separate action still — `POST /sales/{id}/payment-terms/{term_id}/status` — so a routine schedule edit can't revert one that's already been paid: the payment-terms form rebuilds `order.payment_terms` from scratch on every save, same as line items, so `status`/`received_date` are threaded through as hidden fields per row (see `edit_payment_terms_form`'s `submitted_terms` and the hidden `payment_status`/`payment_received_date` inputs in `payment_terms_form.html`) precisely so a resubmit can't silently reset one back to pending.

  Both `payment_terms_form.html` (live, via JS) and `sales/detail.html` (server-rendered) show a non-blocking warning when the schedule's total doesn't add up to the order's Invoice Value (subtotal + GST) — a mismatch is often just an in-progress schedule, not an error, so it's flagged rather than blocked on save.

- **GST**: `Item.hsn_code`/`gst_percentage` (added ahead of any real invoicing module — see "Known scope") are put to their first use on `SalesOrder`, which now shows Subtotal / GST / Invoice Value. Deliberately the simplest version of this: no per-line GST column in the UI and no CGST/SGST/IGST split (the real Indian GST rule — same-state sales split into CGST+SGST, inter-state sales use IGST instead — depends on comparing the customer's state to the company's, and `Company` doesn't even carry a state field today). GST is still computed correctly under the hood per line, since items can carry different rates — see the `gst_total` accumulation in `create_sales_order`/`update_sales_order` and the parallel client-side calc in `sales/form.html`'s `recalcTotal()` — just not broken out visibly per line, only as one blended total.

  `SalesOrder.gst_amount` is a stored aggregate (like `total_amount` itself), computed from each line's *item's current* `gst_percentage` at the moment the order is saved — not snapshotted per line onto `SalesOrderItem`. This means it's frozen at save time (won't drift if you view an old order after an item's rate changes) but will shift if the order itself is later re-saved after that item's rate changed — acceptable for what this is today: a planning/estimate figure ("what will the invoice value be"), not a legal tax record. A real GST tax invoice, when the Invoice model gets built, should snapshot the rate per line at that point instead, the same way `SalesOrderItem.unit_price` already snapshots price rather than trusting `Item.sales_price` to stay put.

  `GET /sales/payments-due` (`compute_payments_due`) is the company-wide rollup — every `pending` term across `confirmed`/`delivered` orders (`draft` isn't a firm commitment yet; `cancelled` never will be), earliest `due_date` first, with rows that have no `due_date` yet (including `days_after_invoice`-only rows) sorted last rather than hidden. Linked from the sidebar, the Sales list, and a Dashboard card (top 5, via the same `compute_payments_due` — no separate query). Same shape as the Purchase requirements page above: one shared computation, not a per-order one that could disagree with the global view.

**Multi-value form fields:** where a form needs a repeated field (order line items, category tags), routes take `request: Request` and parse via `await request.form()` / `form.getlist(...)` rather than declaring a typed `Form(List[...])` parameter — this is the pattern used throughout `sales.py`, `purchase.py`, and `items.py`; follow it for new multi-value fields rather than introducing a different approach.

**Currency formatting:** every currency amount shown anywhere in the app displays Indian-style — lakh/crore comma grouping, not the international thousands grouping (e.g. `1,00,000.00`, not `100,000.00`), always with exactly 2 decimals. Quantities (`|qty`) are a separate, existing convention and unaffected by this.
- Server-rendered: `app/formatting.py:format_inr` is registered as the Jinja filter `inr` (alongside the existing `qty` filter) in every router whose templates show money — `sales.py`, `purchase.py`, `items.py`, `customers.py`, `vendors.py`, `dashboard.py`. `inventory.py`/`production.py` don't register it since their templates have no currency fields. Use `{{ value|inr }}` in templates — never hand-roll `"%.2f"|format(...)` for a money value again. (A plain percentage like `Item.gst_percentage` or a payment term's `percentage` column is not currency — those keep `"%.2f"|format(...)`.)
- Client-side (live totals before a form is saved): `formatINR(value)` is defined once in `base.html`'s bottom script block (loads on every page, before any `{% block extra_js %}` content runs) and mirrors `format_inr` exactly. Used in `sales/form.html`, `purchase/form.html`, `sales/payment_terms_form.html` for the running Subtotal/GST/Invoice Value/Line Total/Total Scheduled displays.
- **Never format an editable `<input type="number">`'s `.value`** — browsers reject comma-formatted strings in a number input, so fields like `.price-input`, `.payment-amount`, `.qty-input` always stay plain (still `.toFixed(2)` where that was already the case). Only display-only elements (`<span>`s like `.line-total`, `#orderTotal`, `#orderSubtotal`, `#orderGst`, `#orderGrandTotal`, `#scheduleTotal`, and warning message text) get `formatINR()`.
- One consequence: `.line-total` spans (`sales/form.html`, `purchase/form.html`) can no longer be summed by re-parsing their formatted `textContent` (`parseFloat` stops at the first comma). Each `.line-total` span carries a `data-value` attribute holding the raw unformatted number alongside the Indian-formatted `textContent`; `recalcTotal()` reads `dataset.value`, not the displayed text.

## One-time scripts

Schema changes go through Alembic now (see "Database migrations" above), not disposable scripts. `scripts/` should stay limited to `init_db.py` and `reset_db.py`, plus, occasionally, a genuine one-time *data* fix (not a schema change) named `one_<description>.py` — e.g. backfilling a value into existing rows. Those still get deleted once run; migration files under `migrations/versions/` never do.

## Known scope

v1 covers Sales, Purchase, Inventory, Production (BOM/assembly), and the
master data they depend on (Customers, Vendors, Items/Categories).
Accounting/finance and HR/payroll are not implemented.

## User roles / permissions

Not every logged-in user should have view/add/edit/delete power on
everything. `User.role` (`admin`/`staff`) is a coarse switch — admins
always have full access to everything, no exceptions. Staff users are
gated by a separate, finer-grained system:

- `Role` (`roles` table) — a named, reusable permission set an admin
  creates (e.g. "Sales Staff", "Warehouse Staff") via `/roles`.
- `RolePermission` (`role_permissions` table) — one row per (role, module),
  with four independent booleans: `can_view`, `can_add`, `can_edit`,
  `can_delete`. A module with no row for a role means no access — deny by
  default, not accidentally open. `module` is a plain string, not a DB
  enum (see `MODULES` in `app/auth.py`), so wiring up a new module later is
  a one-line addition there, not a migration.
- `User.permission_role_id` — nullable FK to `Role`, only consulted when
  `user.role == "staff"`. A staff user with no role assigned has no access
  to any gated module. Assigned via `/users/{id}/edit`.
- `app/auth.py:get_user_module_permissions(user, db, module)` returns
  `{"view": bool, "add": bool, "edit": bool, "delete": bool}` for a given
  module — admins get all-`True` without a query. `require_module_permission(module, action)`
  is a dependency factory: `Depends(require_module_permission("items", "edit"))`
  in place of `Depends(require_login)`. A denied request raises
  `PermissionDeniedException`, handled in `main.py` by rendering
  `app/templates/403.html` (not FastAPI's default JSON error).

**Rollout status: only the Items module is actually gated today.**
`items.py` routes use `require_module_permission("items", ...)` and its
templates (`list.html`, `detail.html`) read a `perms` dict passed from the
route to hide the New/Edit/Delete buttons a user can't use. Every other
router (`customers`, `vendors`, `sales`, `purchase`, `inventory`,
`production`) still only uses plain `require_login` — any logged-in user,
admin or staff, can still do anything there. `MODULES` in `app/auth.py`
already lists all of them so they show up in the Roles admin UI's
permission matrix ready to switch on, but wiring each one up (swap
`require_login` for `require_module_permission`, add a `perms` dict to
its templates, hide its buttons) is separate follow-up work, module by
module, not done automatically by adding the row to `MODULES`.

This was a deliberate factor in earlier design decisions too, not just a
new add-on: mutating actions are kept grouped by the page/route they'll
eventually be gated on, rather than scattered, so a permission check can
be added to one dependency instead of hunting through templates. This is
also why item attachment upload/delete live on the item **edit** page
instead of the view page (see Data model above) — "can edit this item"
and "can manage its photos/documents" are the same permission check
(`items`/`edit`), not two different ones on two different pages.

The sidebar's "Roles & Users" link (`base.html`) is only shown to admins.
There's currently no self-registration or invite flow — admins create
staff logins directly from `/users/new`.

CRM2's `app/utils.py:Permission(user_id)` + per-route
`if 'permission name' not in permissions` pattern (see CRM2's own
CLAUDE.md) was the closest prior art in this codebase family; this
project ended up with `require_module_permission(module, action)` as a
FastAPI dependency factory instead, since route-level `Depends(...)` is
this framework's idiom rather than an in-body `if` check.
