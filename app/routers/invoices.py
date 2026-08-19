"""Sales Order -> Invoice. See claude/invoice-and-serial-tracking-design.md
for the agreed design and app/models.py's Invoice/InvoiceItem docstrings
for the lifecycle/schema reasoning. This router owns everything under
/invoices; the entry point into it is a "Create Invoice" link on a Sales
Order's own detail page (sales/detail.html), not a bare "new" button here —
an invoice always starts from a specific order.
"""
import io
import re
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, Depends
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import func
from sqlalchemy.orm import Session, joinedload
from xhtml2pdf import pisa

from app.database import get_db
from app.auth import require_module_permission, get_user_module_permissions
from app.formatting import format_qty, format_inr, format_dt
from app.models import Invoice, InvoiceItem, SalesOrder, SalesOrderItem, Item, ItemSerial, StockTransaction, User, Company
from app.audit import diff_fields, log_field_changes, log_action

router = APIRouter(prefix="/invoices", tags=["invoices"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["qty"] = format_qty
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt


def _nl2br(value):
    """Same as sales.py's own _nl2br (used by invoices/pdf.html) — xhtml2pdf
    ignores CSS white-space: pre-line, so stored newlines need real <br/>
    tags to survive into the PDF. Escapes first so raw text can't inject
    markup, then re-marks safe after inserting the <br/> tags."""
    if not value:
        return ""
    lines = [str(escape(line)) for line in str(value).split("\n")]
    return Markup("<br/>".join(lines))


templates.env.filters["nl2br"] = _nl2br

# Only these two Sales Order statuses can ever have an invoice raised
# against them. 'confirmed' is the normal case (billing happens before/at
# delivery). 'delivered' covers an order that went out the door via the old
# one-click Mark Delivered button before this feature existed (or before
# anyone bothered to invoice it) — raising an invoice against it now is
# billing-only, since the stock already moved; see Invoice.stock_deducted's
# docstring and issue_invoice below.
INVOICEABLE_STATUSES = ("confirmed", "delivered")


def _parse_date(value: str):
    """Same as sales.py's own _parse_date — HTML <input type=date> gives
    'YYYY-MM-DD' or ''."""
    value = (value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def _split_serials(raw: str) -> list[str]:
    """Same helper as purchase.py's receive flow / inventory.py's serial
    entry — split a textarea of serial numbers on commas and/or newlines."""
    parts = re.split(r"[\n,]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


def _financial_year_label(dt: datetime) -> str:
    """'25-26' for any date between 1-Apr-2025 and 31-Mar-2026 (Indian
    financial year) — used by next_invoice_no. Two-digit years only, to
    match the INV/25-26/0001 format agreed in the design."""
    start = dt.year if dt.month >= 4 else dt.year - 1
    end = start + 1
    return f"{start % 100:02d}-{end % 100:02d}"


def next_invoice_no(db: Session, invoice_date: datetime) -> str:
    """INV/<FY>/0001, resetting every financial year (1-Apr) — derived from
    the highest existing numeric suffix within this FY's prefix, not a row
    count. A row count breaks the moment a draft invoice (the only kind
    that can be deleted) other than the most recent one is removed, since
    the count drops but the highest number in use doesn't — same class of
    bug fixed in purchase.py/sales.py's next_order_no on 2026-08-19."""
    prefix = f"INV/{_financial_year_label(invoice_date)}/"
    existing = db.query(Invoice.invoice_no).filter(Invoice.invoice_no.like(f"{prefix}%")).all()
    max_n = 0
    for (invoice_no,) in existing:
        suffix = invoice_no[len(prefix):]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{prefix}{max_n + 1:04d}"


def remaining_to_invoice(db: Session, sales_order_item: SalesOrderItem, exclude_invoice_id: int = None) -> float:
    """Order qty minus the sum of quantities already invoiced against this
    line on non-cancelled invoices — always computed fresh, never a stored
    counter (see InvoiceItem's docstring). `exclude_invoice_id` leaves one
    invoice's own lines out of the sum — used when editing a draft invoice,
    so its own (about-to-be-resaved) quantities don't count against
    themselves and cap out the very row they're already on."""
    query = (
        db.query(func.coalesce(func.sum(InvoiceItem.quantity), 0))
        .join(Invoice, Invoice.id == InvoiceItem.invoice_id)
        .filter(InvoiceItem.sales_order_item_id == sales_order_item.id, Invoice.status != "cancelled")
    )
    if exclude_invoice_id:
        query = query.filter(Invoice.id != exclude_invoice_id)
    invoiced = float(query.scalar() or 0)
    return float(sales_order_item.quantity) - invoiced


def invoicing_summary(db: Session, order: SalesOrder, exclude_invoice_id: int = None) -> list[dict]:
    """One row per order line — ordered / already invoiced (by other
    invoices) / remaining — the shared 'what's left to bill' view used by
    the New/Edit Invoice form, the issue step's completeness check, and
    (via order.invoices on the template side) the Sales Order detail page."""
    rows = []
    for line in order.items:
        remaining = remaining_to_invoice(db, line, exclude_invoice_id)
        rows.append({
            "line": line, "ordered": float(line.quantity),
            "invoiced": float(line.quantity) - remaining, "remaining": remaining,
            "default_qty": remaining, "default_price": float(line.unit_price),
        })
    return rows


def _apply_submitted_lines(rows: list[dict], line_ids, quantities, unit_prices) -> list[dict]:
    """Overrides each row's default_qty/default_price with whatever was
    actually submitted, so a validation-error redisplay shows back what the
    user typed rather than silently reverting to the original defaults."""
    submitted = {}
    for i, q, p in zip(line_ids, quantities, unit_prices):
        if i and i.isdigit():
            submitted[int(i)] = (q, p)
    for r in rows:
        if r["line"].id in submitted:
            q, p = submitted[r["line"].id]
            r["default_qty"] = q
            r["default_price"] = p
    return rows


def _bill_to_from_form(form) -> dict:
    return {
        "name": form.get("bill_to_name", "").strip(), "address": form.get("bill_to_address", "").strip(),
        "city": form.get("bill_to_city", "").strip(), "state": form.get("bill_to_state", "").strip(),
        "country": form.get("bill_to_country", "").strip(), "gstin": form.get("bill_to_gstin", "").strip(),
    }


def _ship_to_from_form(form) -> dict:
    return {
        "name": form.get("ship_to_name", "").strip(), "address": form.get("ship_to_address", "").strip(),
        "city": form.get("ship_to_city", "").strip(), "state": form.get("ship_to_state", "").strip(),
        "country": form.get("ship_to_country", "").strip(), "gstin": form.get("ship_to_gstin", "").strip(),
    }


def _default_bill_to(order: SalesOrder) -> dict:
    c = order.customer
    return {
        "name": c.name if c else "",
        "address": order.billing_address or (c.address if c else "") or "",
        "city": (c.city if c else "") or "", "state": (c.state if c else "") or "",
        "country": (c.country if c else "") or "", "gstin": (c.gstin if c else "") or "",
    }


def _default_ship_to(order: SalesOrder) -> dict:
    c = order.ship_to_customer or order.customer
    return {
        "name": c.name if c else "",
        "address": order.shipping_address or (c.address if c else "") or "",
        "city": (c.city if c else "") or "", "state": (c.state if c else "") or "",
        "country": (c.country if c else "") or "", "gstin": (c.gstin if c else "") or "",
    }


def _bill_ship_dict_from_invoice(invoice: Invoice) -> tuple[dict, dict]:
    bill_to = {
        "name": invoice.bill_to_name or "", "address": invoice.bill_to_address or "",
        "city": invoice.bill_to_city or "", "state": invoice.bill_to_state or "",
        "country": invoice.bill_to_country or "", "gstin": invoice.bill_to_gstin or "",
    }
    ship_to = {
        "name": invoice.ship_to_name or "", "address": invoice.ship_to_address or "",
        "city": invoice.ship_to_city or "", "state": invoice.ship_to_state or "",
        "country": invoice.ship_to_country or "", "gstin": invoice.ship_to_gstin or "",
    }
    return bill_to, ship_to


@router.get("")
def list_invoices(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "view"))):
    invoices = (
        db.query(Invoice)
        .options(joinedload(Invoice.sales_order).joinedload(SalesOrder.customer))
        .order_by(Invoice.created_at.desc())
        .all()
    )
    perms = get_user_module_permissions(user, db, "invoices")
    return templates.TemplateResponse("invoices/list.html", {"request": request, "user": user, "invoices": invoices, "perms": perms})


@router.get("/new")
def new_invoice_form(
    sales_order_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "add")),
):
    order = (
        db.query(SalesOrder)
        .options(
            joinedload(SalesOrder.customer), joinedload(SalesOrder.ship_to_customer),
            joinedload(SalesOrder.items).joinedload(SalesOrderItem.item),
        )
        .get(sales_order_id)
    )
    if not order or order.status not in INVOICEABLE_STATUSES:
        return RedirectResponse(url=f"/sales/{sales_order_id}?error=Only confirmed or delivered orders can be invoiced", status_code=303)
    rows = invoicing_summary(db, order)
    if not any(r["remaining"] > 1e-9 for r in rows):
        return RedirectResponse(url=f"/sales/{sales_order_id}?error=Every line on this order has already been fully invoiced", status_code=303)

    return templates.TemplateResponse(
        "invoices/form.html",
        {
            "request": request, "user": user, "order": order, "rows": rows, "invoice": None,
            "invoice_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "bill_to": _default_bill_to(order), "ship_to": _default_ship_to(order),
            "submitted_no": "", "notes": "", "errors": {},
        },
    )


@router.post("/new")
async def create_invoice(
    sales_order_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "add")),
):
    order = (
        db.query(SalesOrder)
        .options(
            joinedload(SalesOrder.customer), joinedload(SalesOrder.ship_to_customer),
            joinedload(SalesOrder.items).joinedload(SalesOrderItem.item),
        )
        .get(sales_order_id)
    )
    if not order or order.status not in INVOICEABLE_STATUSES:
        return RedirectResponse(url=f"/sales/{sales_order_id}?error=Only confirmed or delivered orders can be invoiced", status_code=303)

    form = await request.form()
    invoice_no = form.get("invoice_no", "").strip()
    invoice_date = _parse_date(form.get("invoice_date", "")) or datetime.utcnow()
    notes = form.get("notes", "")
    bill_to = _bill_to_from_form(form)
    ship_to = _ship_to_from_form(form)
    line_ids = form.getlist("sales_order_item_id")
    quantities = form.getlist("quantity")
    unit_prices = form.getlist("unit_price")

    if invoice_no and db.query(Invoice).filter(Invoice.invoice_no == invoice_no).first():
        rows = _apply_submitted_lines(invoicing_summary(db, order), line_ids, quantities, unit_prices)
        return templates.TemplateResponse(
            "invoices/form.html",
            {
                "request": request, "user": user, "order": order, "rows": rows, "invoice": None,
                "invoice_date": invoice_date.strftime("%Y-%m-%d"), "bill_to": bill_to, "ship_to": ship_to,
                "submitted_no": invoice_no, "notes": notes,
                "errors": {"invoice_no": f"Invoice No '{invoice_no}' is already used by another invoice."},
            },
            status_code=400,
        )

    lines_by_id = {line.id: line for line in order.items}
    invoice = Invoice(
        invoice_no=invoice_no or next_invoice_no(db, invoice_date),
        sales_order_id=order.id, invoice_date=invoice_date, notes=notes, status="draft",
        bill_to_name=bill_to["name"] or None, bill_to_address=bill_to["address"] or None,
        bill_to_city=bill_to["city"] or None, bill_to_state=bill_to["state"] or None,
        bill_to_country=bill_to["country"] or None, bill_to_gstin=bill_to["gstin"] or None,
        ship_to_name=ship_to["name"] or None, ship_to_address=ship_to["address"] or None,
        ship_to_city=ship_to["city"] or None, ship_to_state=ship_to["state"] or None,
        ship_to_country=ship_to["country"] or None, ship_to_gstin=ship_to["gstin"] or None,
        created_by=user.id,
    )
    db.add(invoice)

    errors = {}
    total = 0.0
    gst_total = 0.0
    for line_id_str, qty_str, price_str in zip(line_ids, quantities, unit_prices):
        if not line_id_str or not qty_str:
            continue
        try:
            qty = float(qty_str)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        line = lines_by_id.get(int(line_id_str))
        if not line:
            continue
        remaining = remaining_to_invoice(db, line)
        if qty - remaining > 1e-9:
            errors[line.id] = f"Only {remaining:g} remaining to invoice on this line."
            continue
        try:
            unit_price = float(price_str)
        except (TypeError, ValueError):
            unit_price = float(line.unit_price)
        line_total = unit_price * qty
        gst_pct = float(line.item.gst_percentage or 0)
        total += line_total
        gst_total += line_total * gst_pct / 100
        invoice.items.append(InvoiceItem(
            sales_order_item_id=line.id, item_id=line.item_id, quantity=qty, unit_price=unit_price,
            gst_percentage=gst_pct, hsn_code=line.item.hsn_code, total=line_total,
        ))

    if errors or not invoice.items:
        db.rollback()
        rows = _apply_submitted_lines(invoicing_summary(db, order), line_ids, quantities, unit_prices)
        return templates.TemplateResponse(
            "invoices/form.html",
            {
                "request": request, "user": user, "order": order, "rows": rows, "invoice": None,
                "invoice_date": invoice_date.strftime("%Y-%m-%d"), "bill_to": bill_to, "ship_to": ship_to,
                "submitted_no": invoice_no, "notes": notes,
                "errors": errors or {"_general": "Enter a quantity for at least one line."},
            },
            status_code=400,
        )

    invoice.total_amount = total
    invoice.gst_amount = gst_total
    db.flush()  # need invoice.id before logging
    log_action(
        db, user, "invoice", invoice.id, invoice.invoice_no, "create",
        summary=f"Created against {order.order_no} — {len(invoice.items)} line(s), total {invoice.total_amount}",
    )
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice.id}", status_code=303)


@router.get("/{invoice_id}")
def view_invoice(invoice_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "view"))):
    invoice = (
        db.query(Invoice)
        .options(
            joinedload(Invoice.sales_order),
            joinedload(Invoice.items).joinedload(InvoiceItem.item),
            joinedload(Invoice.items).joinedload(InvoiceItem.serials),
        )
        .get(invoice_id)
    )
    perms = get_user_module_permissions(user, db, "invoices")
    return templates.TemplateResponse("invoices/detail.html", {"request": request, "user": user, "invoice": invoice, "perms": perms})


@router.get("/{invoice_id}/edit")
def edit_invoice_form(invoice_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "edit"))):
    invoice = db.query(Invoice).options(joinedload(Invoice.items)).get(invoice_id)
    if invoice.status != "draft":
        return RedirectResponse(url=f"/invoices/{invoice_id}?error=Only draft invoices can be edited", status_code=303)
    order = (
        db.query(SalesOrder)
        .options(joinedload(SalesOrder.customer), joinedload(SalesOrder.items).joinedload(SalesOrderItem.item))
        .get(invoice.sales_order_id)
    )
    rows = invoicing_summary(db, order, exclude_invoice_id=invoice.id)
    qty_by_line = {i.sales_order_item_id: float(i.quantity) for i in invoice.items}
    price_by_line = {i.sales_order_item_id: float(i.unit_price) for i in invoice.items}
    for r in rows:
        if r["line"].id in qty_by_line:
            r["default_qty"] = qty_by_line[r["line"].id]
            r["default_price"] = price_by_line[r["line"].id]
        else:
            r["default_qty"] = 0

    bill_to, ship_to = _bill_ship_dict_from_invoice(invoice)
    return templates.TemplateResponse(
        "invoices/form.html",
        {
            "request": request, "user": user, "order": order, "rows": rows, "invoice": invoice,
            "invoice_date": invoice.invoice_date.strftime("%Y-%m-%d"), "bill_to": bill_to, "ship_to": ship_to,
            "submitted_no": invoice.invoice_no, "notes": invoice.notes or "", "errors": {},
        },
    )


@router.post("/{invoice_id}/edit")
async def update_invoice(invoice_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "edit"))):
    invoice = db.query(Invoice).options(joinedload(Invoice.items)).get(invoice_id)
    if invoice.status != "draft":
        return RedirectResponse(url=f"/invoices/{invoice_id}?error=Only draft invoices can be edited", status_code=303)
    order = (
        db.query(SalesOrder)
        .options(joinedload(SalesOrder.customer), joinedload(SalesOrder.items).joinedload(SalesOrderItem.item))
        .get(invoice.sales_order_id)
    )

    form = await request.form()
    invoice_no = form.get("invoice_no", "").strip()
    invoice_date = _parse_date(form.get("invoice_date", "")) or invoice.invoice_date
    notes = form.get("notes", "")
    bill_to = _bill_to_from_form(form)
    ship_to = _ship_to_from_form(form)
    line_ids = form.getlist("sales_order_item_id")
    quantities = form.getlist("quantity")
    unit_prices = form.getlist("unit_price")

    if invoice_no and db.query(Invoice).filter(Invoice.invoice_no == invoice_no, Invoice.id != invoice_id).first():
        rows = _apply_submitted_lines(invoicing_summary(db, order, exclude_invoice_id=invoice_id), line_ids, quantities, unit_prices)
        return templates.TemplateResponse(
            "invoices/form.html",
            {
                "request": request, "user": user, "order": order, "rows": rows, "invoice": invoice,
                "invoice_date": invoice_date.strftime("%Y-%m-%d"), "bill_to": bill_to, "ship_to": ship_to,
                "submitted_no": invoice_no, "notes": notes,
                "errors": {"invoice_no": f"Invoice No '{invoice_no}' is already used by another invoice."},
            },
            status_code=400,
        )

    lines_by_id = {line.id: line for line in order.items}
    old_total = invoice.total_amount

    changes = diff_fields(invoice, {
        "invoice_no": invoice_no or invoice.invoice_no, "invoice_date": invoice_date, "notes": notes,
        "bill_to_name": bill_to["name"] or None, "bill_to_address": bill_to["address"] or None,
        "bill_to_city": bill_to["city"] or None, "bill_to_state": bill_to["state"] or None,
        "bill_to_country": bill_to["country"] or None, "bill_to_gstin": bill_to["gstin"] or None,
        "ship_to_name": ship_to["name"] or None, "ship_to_address": ship_to["address"] or None,
        "ship_to_city": ship_to["city"] or None, "ship_to_state": ship_to["state"] or None,
        "ship_to_country": ship_to["country"] or None, "ship_to_gstin": ship_to["gstin"] or None,
    })

    invoice.invoice_no = invoice_no or invoice.invoice_no
    invoice.invoice_date = invoice_date
    invoice.notes = notes
    invoice.bill_to_name = bill_to["name"] or None
    invoice.bill_to_address = bill_to["address"] or None
    invoice.bill_to_city = bill_to["city"] or None
    invoice.bill_to_state = bill_to["state"] or None
    invoice.bill_to_country = bill_to["country"] or None
    invoice.bill_to_gstin = bill_to["gstin"] or None
    invoice.ship_to_name = ship_to["name"] or None
    invoice.ship_to_address = ship_to["address"] or None
    invoice.ship_to_city = ship_to["city"] or None
    invoice.ship_to_state = ship_to["state"] or None
    invoice.ship_to_country = ship_to["country"] or None
    invoice.ship_to_gstin = ship_to["gstin"] or None
    invoice.items = []  # cascade="all, delete-orphan" removes the old lines on flush

    errors = {}
    total = 0.0
    gst_total = 0.0
    for line_id_str, qty_str, price_str in zip(line_ids, quantities, unit_prices):
        if not line_id_str or not qty_str:
            continue
        try:
            qty = float(qty_str)
        except (TypeError, ValueError):
            continue
        if qty <= 0:
            continue
        line = lines_by_id.get(int(line_id_str))
        if not line:
            continue
        remaining = remaining_to_invoice(db, line, exclude_invoice_id=invoice_id)
        if qty - remaining > 1e-9:
            errors[line.id] = f"Only {remaining:g} remaining to invoice on this line."
            continue
        try:
            unit_price = float(price_str)
        except (TypeError, ValueError):
            unit_price = float(line.unit_price)
        line_total = unit_price * qty
        gst_pct = float(line.item.gst_percentage or 0)
        total += line_total
        gst_total += line_total * gst_pct / 100
        invoice.items.append(InvoiceItem(
            sales_order_item_id=line.id, item_id=line.item_id, quantity=qty, unit_price=unit_price,
            gst_percentage=gst_pct, hsn_code=line.item.hsn_code, total=line_total,
        ))

    if errors or not invoice.items:
        db.rollback()
        rows = _apply_submitted_lines(invoicing_summary(db, order, exclude_invoice_id=invoice_id), line_ids, quantities, unit_prices)
        return templates.TemplateResponse(
            "invoices/form.html",
            {
                "request": request, "user": user, "order": order, "rows": rows, "invoice": invoice,
                "invoice_date": invoice_date.strftime("%Y-%m-%d"), "bill_to": bill_to, "ship_to": ship_to,
                "submitted_no": invoice_no, "notes": notes,
                "errors": errors or {"_general": "Enter a quantity for at least one line."},
            },
            status_code=400,
        )

    invoice.total_amount = total
    invoice.gst_amount = gst_total
    if invoice.total_amount != old_total:
        changes["line_items"] = (f"total {old_total}", f"total {invoice.total_amount}")
    log_field_changes(db, user, "invoice", invoice.id, invoice.invoice_no, changes)
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}?success=Invoice updated", status_code=303)


@router.get("/{invoice_id}/issue")
def issue_invoice_form(invoice_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "edit"))):
    invoice = (
        db.query(Invoice)
        .options(joinedload(Invoice.items).joinedload(InvoiceItem.item), joinedload(Invoice.sales_order))
        .get(invoice_id)
    )
    if invoice.status != "draft":
        return RedirectResponse(url=f"/invoices/{invoice_id}?error=Only draft invoices can be issued", status_code=303)
    billing_only = invoice.sales_order.status == "delivered"
    return templates.TemplateResponse(
        "invoices/issue.html",
        {"request": request, "user": user, "invoice": invoice, "billing_only": billing_only, "errors": {}, "submitted": {}},
    )


@router.post("/{invoice_id}/issue")
async def issue_invoice(invoice_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "edit"))):
    invoice = (
        db.query(Invoice)
        .options(
            joinedload(Invoice.items).joinedload(InvoiceItem.item),
            joinedload(Invoice.sales_order).joinedload(SalesOrder.items),
        )
        .get(invoice_id)
    )
    if invoice.status != "draft":
        return RedirectResponse(url=f"/invoices/{invoice_id}?error=Only draft invoices can be issued", status_code=303)

    order = invoice.sales_order
    billing_only = order.status == "delivered"

    form = await request.form()
    errors = {}
    submitted = {}
    serials_by_line = {}

    if not billing_only:
        # Validate serial counts for every serialized line before writing
        # anything — same two-pass validate-then-commit shape as
        # purchase.py's receive_purchase_order.
        for line in invoice.items:
            if not line.item.has_serial:
                continue
            field_name = f"serials_{line.id}"
            raw = form.get(field_name, "")
            submitted[line.id] = raw
            serials = _split_serials(raw)
            serials_by_line[line.id] = serials
            if len(serials) != line.quantity:
                errors[line.id] = f"Expected {line.quantity} serial number(s), got {len(serials)}."
            elif len(set(serials)) != len(serials):
                errors[line.id] = "Duplicate serial numbers entered for this line."

        if not errors:
            for line in invoice.items:
                if not line.item.has_serial:
                    continue
                # Must be an existing, currently-unshipped serial for this
                # exact item — i.e. actually in stock, not already shipped
                # on a previous invoice.
                unshipped = {
                    s for (s,) in db.query(ItemSerial.serial_number)
                    .filter(ItemSerial.item_id == line.item_id, ItemSerial.invoice_item_id.is_(None))
                    .all()
                }
                bad = [s for s in serials_by_line[line.id] if s not in unshipped]
                if bad:
                    errors[line.id] = f"Not a currently in-stock, unshipped serial for this item: {', '.join(bad)}"

    if errors:
        return templates.TemplateResponse(
            "invoices/issue.html",
            {"request": request, "user": user, "invoice": invoice, "billing_only": billing_only, "errors": errors, "submitted": submitted},
            status_code=400,
        )

    # Everything validated — write stock, serials, and status together.
    if not billing_only:
        for line in invoice.items:
            item = line.item
            item.current_stock = (item.current_stock or 0) - line.quantity
            db.add(StockTransaction(
                item_id=item.id, transaction_type="OUT", quantity=line.quantity,
                reference_type="invoice", reference_id=invoice.id,
                notes=f"Invoiced against {invoice.invoice_no} ({order.order_no})",
            ))
            for serial_number in serials_by_line.get(line.id, []):
                serial = (
                    db.query(ItemSerial)
                    .filter(ItemSerial.item_id == line.item_id, ItemSerial.serial_number == serial_number)
                    .first()
                )
                serial.invoice_item_id = line.id
                serial.shipped_at = datetime.utcnow()
        invoice.stock_deducted = True

    invoice.status = "issued"
    invoice.issued_by = user.id
    invoice.issued_at = datetime.utcnow()
    log_action(
        db, user, "invoice", invoice.id, invoice.invoice_no, "issue",
        summary=("Issued (billing only — order already delivered)" if billing_only
                 else f"Issued — stock deducted for {len(invoice.items)} line(s)"),
    )

    # Auto-flip the order to 'delivered' once every line is fully invoiced —
    # only meaningful starting from 'confirmed' (an already-'delivered'
    # order just stays 'delivered'; see Invoice.stock_deducted's docstring).
    if order.status == "confirmed":
        rows = invoicing_summary(db, order)
        if rows and all(r["remaining"] <= 1e-9 for r in rows):
            old_status = order.status
            order.status = "delivered"
            log_action(
                db, user, "sales_order", order.id, order.order_no, "status_change",
                summary=f"{old_status} -> delivered (auto — fully invoiced)",
            )

    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}?success=Invoice issued", status_code=303)


@router.post("/{invoice_id}/cancel")
def cancel_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "edit"))):
    invoice = (
        db.query(Invoice)
        .options(
            joinedload(Invoice.items).joinedload(InvoiceItem.item),
            joinedload(Invoice.items).joinedload(InvoiceItem.serials),
            joinedload(Invoice.sales_order),
        )
        .get(invoice_id)
    )
    if invoice.status != "issued":
        return RedirectResponse(url=f"/invoices/{invoice_id}?error=Only issued invoices can be cancelled", status_code=303)

    order = invoice.sales_order
    if invoice.stock_deducted:
        for line in invoice.items:
            item = line.item
            item.current_stock = (item.current_stock or 0) + line.quantity
            db.add(StockTransaction(
                item_id=item.id, transaction_type="IN", quantity=line.quantity,
                reference_type="invoice", reference_id=invoice.id,
                notes=f"{invoice.invoice_no} cancelled — stock reversed",
            ))
            for serial in line.serials:
                serial.invoice_item_id = None
                serial.shipped_at = None
        if order.status == "delivered":
            old_status = order.status
            order.status = "confirmed"
            log_action(
                db, user, "sales_order", order.id, order.order_no, "status_change",
                summary=f"{old_status} -> confirmed (auto — invoice {invoice.invoice_no} cancelled)",
            )

    invoice.status = "cancelled"
    invoice.cancelled_by = user.id
    invoice.cancelled_at = datetime.utcnow()
    log_action(
        db, user, "invoice", invoice.id, invoice.invoice_no, "cancel",
        summary="Cancelled" + (" — stock and serials reversed" if invoice.stock_deducted else " (billing only, no stock movement to reverse)"),
    )
    db.commit()
    return RedirectResponse(url=f"/invoices/{invoice_id}?success=Invoice cancelled", status_code=303)


@router.post("/{invoice_id}/delete")
def delete_invoice(invoice_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "delete"))):
    invoice = db.query(Invoice).get(invoice_id)
    if not invoice:
        return RedirectResponse(url="/invoices?error=Invoice not found", status_code=303)
    if invoice.status != "draft":
        return RedirectResponse(url=f"/invoices/{invoice_id}?error=Only draft invoices can be deleted — cancel an issued one instead", status_code=303)
    order_id = invoice.sales_order_id
    log_action(db, user, "invoice", invoice.id, invoice.invoice_no, "delete", summary=f"Deleted draft '{invoice.invoice_no}'")
    db.delete(invoice)
    db.commit()
    return RedirectResponse(url=f"/sales/{order_id}?success=Draft invoice deleted", status_code=303)


@router.get("/{invoice_id}/pdf")
def invoice_pdf(invoice_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("invoices", "view"))):
    """Same xhtml2pdf approach as sales.py:sales_order_pdf — see that
    route's docstring for why (pure-Python, no system binary dependency)."""
    invoice = (
        db.query(Invoice)
        .options(
            joinedload(Invoice.items).joinedload(InvoiceItem.item),
            joinedload(Invoice.sales_order).joinedload(SalesOrder.customer),
        )
        .get(invoice_id)
    )
    company = db.query(Company).first()
    status_color = {"draft": "#6c757d", "issued": "#198754", "cancelled": "#dc3545"}.get(invoice.status, "#6c757d")
    logo_file = Path(__file__).resolve().parent.parent / "static" / "Logo.jpg"
    logo_path = str(logo_file) if logo_file.exists() else None
    html = templates.get_template("invoices/pdf.html").render(
        invoice=invoice, company=company, generated_at=datetime.utcnow(),
        status_color=status_color, logo_path=logo_path, request=request, user=user,
    )
    buffer = io.BytesIO()
    result = pisa.CreatePDF(io.StringIO(html), dest=buffer)
    if result.err:
        return Response(content="Failed to generate PDF", status_code=500)
    buffer.seek(0)
    return Response(
        content=buffer.read(),
        media_type="application/pdf",
        headers={
            # invoice_no contains "/" (INV/25-26/0001) — not safe in a filename.
            "Content-Disposition": f'inline; filename="{invoice.invoice_no.replace("/", "-")}.pdf"',
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )
