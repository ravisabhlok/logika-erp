import io
from datetime import datetime
from pathlib import Path

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse, JSONResponse, Response
from fastapi.templating import Jinja2Templates
from markupsafe import Markup, escape
from sqlalchemy import or_, func
from sqlalchemy.orm import Session, joinedload, aliased
from xhtml2pdf import pisa

from app.database import get_db
from app.auth import require_login, require_module_permission, get_user_module_permissions
from app.formatting import format_qty, format_inr, format_dt
from app.models import SalesOrder, SalesOrderItem, SalesOrderPaymentTerm, Customer, Item, StockTransaction, User, Company
from app.audit import diff_fields, log_field_changes, log_action

router = APIRouter(prefix="/sales", tags=["sales"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["qty"] = format_qty
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt


def _nl2br(value):
    """xhtml2pdf (used by sales/pdf.html) silently ignores CSS
    white-space: pre-line — a real browser would honor stored newlines in
    an address/notes field, but xhtml2pdf collapses them into spaces and
    reflows as one line. Real <br/> tags are the only reliable way to force
    a line break there, so multi-line text going into the PDF template goes
    through this instead of a plain white-space:pre-line div. Escapes first
    so the raw text can't inject markup, then re-marks safe after inserting
    the <br/> tags."""
    if not value:
        return ""
    lines = [str(escape(line)) for line in str(value).split("\n")]
    return Markup("<br/>".join(lines))


templates.env.filters["nl2br"] = _nl2br
# Used by sales/detail.html and sales/payments_due.html to flag an overdue
# payment term (due_date in the past, still pending) — a plain global
# rather than threading "today" through every route's context.
templates.env.globals["now"] = datetime.utcnow


def next_order_no(db: Session) -> str:
    count = db.query(SalesOrder).count()
    return f"SO-{count + 1:05d}"


def _parse_date(value: str):
    """HTML <input type=date> gives 'YYYY-MM-DD' or '' — parse to a
    datetime, or None if left blank (payment term dates are frequently
    unknown at entry time, e.g. 'after warranty ends' before delivery has
    even happened)."""
    value = (value or "").strip()
    if not value:
        return None
    return datetime.strptime(value, "%Y-%m-%d")


def _payment_terms_from_form(form) -> list[dict]:
    """Zips the payment-schedule columns from a submitted form into a list
    of plain dicts (still strings, as submitted) — one dict per row, rows
    with no description dropped (an unfilled blank template row). Used both
    to redisplay the schedule if a save is rejected and, converted via
    `_build_payment_term`, to actually save it. `status`/`received_date`
    travel through as hidden fields set by the form's JS from existing data
    — see SalesOrderPaymentTerm's docstring for why that matters on a
    resubmit."""
    rows = zip(
        form.getlist("payment_description"),
        form.getlist("payment_percentage"),
        form.getlist("payment_amount"),
        form.getlist("payment_due_date"),
        form.getlist("payment_days_after_invoice"),
        form.getlist("payment_secured_by"),
        form.getlist("payment_bg_expiry"),
        form.getlist("payment_status"),
        form.getlist("payment_received_date"),
    )
    return [
        {
            "description": description.strip(), "percentage": percentage, "amount": amount,
            "due_date": due_date, "days_after_invoice": days_after_invoice,
            "secured_by": secured_by, "bg_expiry_date": bg_expiry,
            "status": status, "received_date": received_date,
        }
        for description, percentage, amount, due_date, days_after_invoice, secured_by, bg_expiry, status, received_date in rows
        if description.strip()
    ]


def _build_payment_term(row: dict) -> SalesOrderPaymentTerm:
    try:
        percentage = float(row["percentage"]) if (row["percentage"] or "").strip() else None
    except (TypeError, ValueError):
        percentage = None
    try:
        amount = float(row["amount"])
    except (TypeError, ValueError):
        amount = 0.0
    try:
        days_after_invoice = int(row["days_after_invoice"]) if (row["days_after_invoice"] or "").strip() else None
    except (TypeError, ValueError):
        days_after_invoice = None
    return SalesOrderPaymentTerm(
        description=row["description"],
        percentage=percentage,
        amount=amount,
        due_date=_parse_date(row["due_date"]),
        days_after_invoice=days_after_invoice,
        secured_by=row["secured_by"] if row["secured_by"] in ("cash", "bank_guarantee") else "cash",
        bg_expiry_date=_parse_date(row["bg_expiry_date"]),
        status=row["status"] if row["status"] in ("pending", "received") else "pending",
        received_date=_parse_date(row["received_date"]),
    )


@router.get("")
def list_sales(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "view"))):
    orders = db.query(SalesOrder).options(joinedload(SalesOrder.customer)).order_by(SalesOrder.created_at.desc()).all()
    perms = get_user_module_permissions(user, db, "sales")
    return templates.TemplateResponse("sales/list.html", {"request": request, "user": user, "orders": orders, "perms": perms})


@router.get("/new")
def new_sales_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "add"))):
    customers = db.query(Customer).order_by(Customer.name).all()
    items = db.query(Item).filter(Item.is_active == True).order_by(Item.name).all()  # noqa: E712
    return templates.TemplateResponse(
        "sales/form.html",
        {
            "request": request, "user": user, "customers": customers, "items": items,
            "errors": {}, "submitted": {}, "order": None,
        },
    )


@router.post("/new")
async def create_sales_order(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "add"))):
    form = await request.form()
    customer_id = int(form.get("customer_id"))
    order_no = form.get("order_no", "").strip()
    notes = form.get("notes", "")
    customer_po_no = form.get("customer_po_no", "").strip()
    customer_po_date = form.get("customer_po_date", "")
    expected_shipment_date = form.get("expected_shipment_date", "")
    billing_address = form.get("billing_address", "").strip()
    shipping_address = form.get("shipping_address", "").strip()
    ship_to_customer_id_str = form.get("ship_to_customer_id", "").strip()
    item_ids = form.getlist("item_id")
    quantities = form.getlist("quantity")
    unit_prices = form.getlist("unit_price")

    # Order No is optional — leave blank to auto-generate (SO-00001, ...), or
    # paste in the number from Zoho (or wherever) to keep the two in sync.
    # Still has to be unique here since orders are looked up by it elsewhere.
    if order_no and db.query(SalesOrder).filter(SalesOrder.order_no == order_no).first():
        customers = db.query(Customer).order_by(Customer.name).all()
        items = db.query(Item).filter(Item.is_active == True).order_by(Item.name).all()  # noqa: E712
        submitted = {
            "customer_id": customer_id, "order_no": order_no, "notes": notes,
            "customer_po_no": customer_po_no, "customer_po_date": customer_po_date,
            "expected_shipment_date": expected_shipment_date,
            "billing_address": billing_address, "shipping_address": shipping_address,
            "ship_to_customer_id": int(ship_to_customer_id_str) if ship_to_customer_id_str.isdigit() else None,
            "lines": [
                {"item_id": i, "quantity": q, "unit_price": p}
                for i, q, p in zip(item_ids, quantities, unit_prices) if i and q
            ],
        }
        return templates.TemplateResponse(
            "sales/form.html",
            {
                "request": request, "user": user, "customers": customers, "items": items,
                "errors": {"order_no": f"Order No '{order_no}' is already used by another sales order."},
                "submitted": submitted, "order": None,
            },
            status_code=400,
        )

    order = SalesOrder(
        order_no=order_no or next_order_no(db),
        customer_id=customer_id,
        notes=notes,
        customer_po_no=customer_po_no or None,
        customer_po_date=_parse_date(customer_po_date),
        expected_shipment_date=_parse_date(expected_shipment_date),
        billing_address=billing_address or None,
        shipping_address=shipping_address or None,
        ship_to_customer_id=int(ship_to_customer_id_str) if ship_to_customer_id_str.isdigit() else None,
        created_by=user.id,
        status="draft",
    )
    db.add(order)

    total = 0
    gst_total = 0
    for item_id_str, qty_str, price_str in zip(item_ids, quantities, unit_prices):
        if not item_id_str or not qty_str:
            continue
        qty = float(qty_str)
        if qty <= 0:
            continue
        item = db.query(Item).get(int(item_id_str))
        # Unit price defaults to the item's list price but can be overridden
        # per line on the order form (e.g. a negotiated/discounted rate) —
        # fall back to the item's price if the field was left blank/invalid.
        try:
            unit_price = float(price_str)
        except (TypeError, ValueError):
            unit_price = float(item.sales_price)
        line_total = unit_price * qty
        total += line_total
        # GST uses the item's *current* rate — see SalesOrder.gst_amount's
        # comment for why this isn't snapshotted per line.
        gst_total += line_total * float(item.gst_percentage or 0) / 100
        order.items.append(SalesOrderItem(
            item_id=item.id, quantity=qty, unit_price=unit_price, total=line_total,
        ))

    order.total_amount = total
    order.gst_amount = gst_total
    db.flush()  # need order.id before logging
    log_action(
        db, user, "sales_order", order.id, order.order_no, "create",
        summary=f"Created for {order.customer.name if order.customer else order.customer_id} — {len(order.items)} line(s), total {order.total_amount}",
    )
    db.commit()
    return RedirectResponse(url=f"/sales/{order.id}", status_code=303)


@router.get("/item-suggestions")
def item_suggestions(item_ids: str = "", db: Session = Depends(get_db), user: User = Depends(require_login)):
    """"Frequently ordered together" suggestions for the New/Edit Sales Order
    form. Purely derived from past SalesOrderItem history — for the item(s)
    already on this order, finds other items that showed up on the same past
    orders most often, and returns the top few (excluding whatever's already
    selected and anything inactive). Nothing to curate by hand; it improves
    automatically as more orders are placed, and returns nothing for an item
    with no order history yet (e.g. brand new items).

    NOTE: registered before GET /{order_id} — Starlette matches path patterns
    positionally, and "/{order_id}" would otherwise swallow this literal path
    first and fail trying to cast "item-suggestions" to int.

    Deliberately stays on plain require_login rather than
    require_module_permission("sales", ...) — it's a New/Edit Sales Order
    form dependency (populates the suggestion chips while building an
    order), not really "viewing sales data" on its own, and add/view/edit
    are independent booleans on a Role, so a staff user gated into "add"
    but not "view" should still get working suggestions while creating one.
    """
    ids = [int(x) for x in item_ids.split(",") if x.strip().isdigit()]
    if not ids:
        return []

    # Self-join sales_order_items against itself: for every past order line
    # whose item is one of `ids`, find the other lines on that same order,
    # then count how many distinct orders each candidate item co-occurred in.
    this_line = aliased(SalesOrderItem)
    other_line = aliased(SalesOrderItem)
    rows = (
        db.query(other_line.item_id, func.count(func.distinct(this_line.sales_order_id)).label("cnt"))
        .select_from(this_line)
        .join(other_line, this_line.sales_order_id == other_line.sales_order_id)
        .filter(this_line.item_id.in_(ids))
        .filter(other_line.item_id.notin_(ids))
        .group_by(other_line.item_id)
        .order_by(func.count(func.distinct(this_line.sales_order_id)).desc())
        .limit(5)
        .all()
    )
    suggested_ids = [row[0] for row in rows]
    if not suggested_ids:
        return []

    items = db.query(Item).filter(Item.id.in_(suggested_ids), Item.is_active == True).all()  # noqa: E712
    items_by_id = {i.id: i for i in items}
    # Preserve the co-occurrence ranking (the query above), not query result order.
    ordered = [items_by_id[i] for i in suggested_ids if i in items_by_id]
    return [
        {"id": i.id, "name": i.name, "price": float(i.sales_price), "stock": float(i.current_stock)}
        for i in ordered
    ]


def compute_payments_due(db: Session):
    """Every pending payment milestone across confirmed/delivered sales
    orders, earliest due date first — rows with no due date set yet (e.g.
    'after warranty', before the order's even been delivered) sort to the
    end rather than disappearing, since they still represent money owed,
    just not yet dated. Draft orders are excluded (not a firm commitment
    yet); cancelled orders never will be."""
    terms = (
        db.query(SalesOrderPaymentTerm)
        .join(SalesOrder, SalesOrder.id == SalesOrderPaymentTerm.sales_order_id)
        .options(joinedload(SalesOrderPaymentTerm.sales_order).joinedload(SalesOrder.customer))
        .filter(SalesOrder.status.in_(["confirmed", "delivered"]), SalesOrderPaymentTerm.status == "pending")
        .order_by(SalesOrderPaymentTerm.due_date.is_(None), SalesOrderPaymentTerm.due_date.asc())
        .all()
    )
    today = datetime.utcnow().date()
    return [
        {"term": t, "order": t.sales_order, "overdue": bool(t.due_date and t.due_date.date() < today)}
        for t in terms
    ]


@router.get("/payments-due")
def payments_due(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "view"))):
    """NOTE: registered before GET /{order_id} — same reason as
    /item-suggestions above (a literal one-segment path would otherwise be
    swallowed by "/{order_id}" and fail trying to cast "payments-due" to int)."""
    rows = compute_payments_due(db)
    total_due = sum(float(r["term"].amount) for r in rows)
    perms = get_user_module_permissions(user, db, "sales")
    return templates.TemplateResponse(
        "sales/payments_due.html",
        {"request": request, "user": user, "rows": rows, "total_due": total_due, "perms": perms},
    )


@router.get("/{order_id}")
def view_sales_order(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "view"))):
    order = (
        db.query(SalesOrder)
        .options(
            joinedload(SalesOrder.customer),
            joinedload(SalesOrder.ship_to_customer),
            joinedload(SalesOrder.items).joinedload(SalesOrderItem.item),
            joinedload(SalesOrder.payment_terms),
        )
        .get(order_id)
    )
    perms = get_user_module_permissions(user, db, "sales")
    return templates.TemplateResponse("sales/detail.html", {"request": request, "user": user, "order": order, "perms": perms})


@router.get("/{order_id}/pdf")
def sales_order_pdf(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "view"))):
    """Renders sales/pdf.html (a standalone print layout, not base.html —
    xhtml2pdf only understands a small CSS subset, no Bootstrap) to HTML and
    converts it to a PDF via xhtml2pdf, which is pure-Python and needs no
    system binaries (unlike wkhtmltopdf/WeasyPrint) — this app runs off a
    single office PC, so that keeps deployment to just `pip install`.
    Streamed back as an inline-viewable attachment named after the order."""
    order = (
        db.query(SalesOrder)
        .options(
            joinedload(SalesOrder.customer),
            joinedload(SalesOrder.ship_to_customer),
            joinedload(SalesOrder.items).joinedload(SalesOrderItem.item),
            joinedload(SalesOrder.payment_terms),
        )
        .get(order_id)
    )
    company = db.query(Company).first()
    # Same palette as the status badge on sales/detail.html (bg-secondary/
    # primary/success/danger), just as hex since the PDF has no Bootstrap.
    status_color = {
        "draft": "#6c757d", "confirmed": "#0d6efd",
        "delivered": "#198754", "cancelled": "#dc3545",
    }.get(order.status, "#6c757d")
    # xhtml2pdf isn't loading this over HTTP, so the logo has to be a real
    # filesystem path (not a /static/... URL) — resolved absolute so it
    # doesn't depend on the app's working directory at runtime. Omitted
    # from the PDF entirely if the file isn't there rather than erroring.
    logo_file = Path(__file__).resolve().parent.parent / "static" / "Logo.jpg"
    logo_path = str(logo_file) if logo_file.exists() else None
    html = templates.get_template("sales/pdf.html").render(
        order=order, company=company, generated_at=datetime.utcnow(),
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
            "Content-Disposition": f'inline; filename="{order.order_no}.pdf"',
            # This PDF is regenerated fresh from the DB on every request —
            # without this, browsers happily cache it at this same URL and
            # keep showing a stale version (e.g. after the company address
            # is edited) even though the server is returning a 200 with
            # updated content each time.
            "Cache-Control": "no-store, no-cache, must-revalidate",
        },
    )


@router.get("/{order_id}/edit")
def edit_sales_form(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "edit"))):
    order = (
        db.query(SalesOrder)
        .options(joinedload(SalesOrder.items).joinedload(SalesOrderItem.item))
        .get(order_id)
    )
    # Draft, confirmed, and cancelled orders are editable — stock only
    # deducts once an order is marked 'delivered' (see update_sales_status),
    # so editing anything up to that point (including reducing a line's
    # quantity when a customer only picks up part of what they ordered) can
    # never desync the stock ledger from what's actually on the order.
    # Delivered is where that stops being true: line items would no longer
    # match the StockTransaction rows already logged against them. Cancelled
    # is allowed too since it never touched stock either; editing one is
    # treated as reviving it (see POST handler). Note this only covers the
    # order itself (customer, PO fields, notes, line items) — the Payment
    # Schedule is edited separately and isn't locked by order status at all;
    # see GET/POST /sales/{id}/payment-terms/edit and SalesOrderPaymentTerm's
    # docstring for why.
    if order.status not in ("draft", "confirmed", "cancelled"):
        return RedirectResponse(url=f"/sales/{order_id}?error=Delivered orders can't be edited — line items would no longer match the stock already moved", status_code=303)

    customers = db.query(Customer).order_by(Customer.name).all()
    # Include any item already on this order even if it's since been marked
    # inactive, so its line doesn't silently disappear from the picker.
    order_item_ids = [line.item_id for line in order.items]
    items = (
        db.query(Item)
        .filter(or_(Item.is_active == True, Item.id.in_(order_item_ids)))  # noqa: E712
        .order_by(Item.name)
        .all()
    )
    submitted = {
        "customer_id": order.customer_id,
        "order_no": order.order_no,
        "notes": order.notes or "",
        "customer_po_no": order.customer_po_no or "",
        "customer_po_date": order.customer_po_date.strftime("%Y-%m-%d") if order.customer_po_date else "",
        "expected_shipment_date": order.expected_shipment_date.strftime("%Y-%m-%d") if order.expected_shipment_date else "",
        "billing_address": order.billing_address or "",
        "shipping_address": order.shipping_address or "",
        "ship_to_customer_id": order.ship_to_customer_id,
        "lines": [
            {"item_id": line.item_id, "quantity": float(line.quantity), "unit_price": float(line.unit_price)}
            for line in order.items
        ],
    }
    return templates.TemplateResponse(
        "sales/form.html",
        {
            "request": request, "user": user, "customers": customers, "items": items,
            "errors": {}, "submitted": submitted, "order": order,
        },
    )


@router.post("/{order_id}/edit")
async def update_sales_order(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "edit"))):
    order = db.query(SalesOrder).options(joinedload(SalesOrder.items)).get(order_id)
    if order.status not in ("draft", "confirmed", "cancelled"):
        return RedirectResponse(url=f"/sales/{order_id}?error=Delivered orders can't be edited — line items would no longer match the stock already moved", status_code=303)
    was_cancelled = order.status == "cancelled"

    form = await request.form()
    customer_id = int(form.get("customer_id"))
    order_no = form.get("order_no", "").strip()
    notes = form.get("notes", "")
    customer_po_no = form.get("customer_po_no", "").strip()
    customer_po_date = form.get("customer_po_date", "")
    expected_shipment_date = form.get("expected_shipment_date", "")
    billing_address = form.get("billing_address", "").strip()
    shipping_address = form.get("shipping_address", "").strip()
    ship_to_customer_id_str = form.get("ship_to_customer_id", "").strip()
    item_ids = form.getlist("item_id")
    quantities = form.getlist("quantity")
    unit_prices = form.getlist("unit_price")

    if order_no and db.query(SalesOrder).filter(SalesOrder.order_no == order_no, SalesOrder.id != order_id).first():
        customers = db.query(Customer).order_by(Customer.name).all()
        order_item_ids = [line.item_id for line in order.items]
        items = (
            db.query(Item)
            .filter(or_(Item.is_active == True, Item.id.in_(order_item_ids)))  # noqa: E712
            .order_by(Item.name)
            .all()
        )
        submitted = {
            "customer_id": customer_id, "order_no": order_no, "notes": notes,
            "customer_po_no": customer_po_no, "customer_po_date": customer_po_date,
            "expected_shipment_date": expected_shipment_date,
            "billing_address": billing_address, "shipping_address": shipping_address,
            "ship_to_customer_id": int(ship_to_customer_id_str) if ship_to_customer_id_str.isdigit() else None,
            "lines": [
                {"item_id": i, "quantity": q, "unit_price": p}
                for i, q, p in zip(item_ids, quantities, unit_prices) if i and q
            ],
        }
        return templates.TemplateResponse(
            "sales/form.html",
            {
                "request": request, "user": user, "customers": customers, "items": items,
                "errors": {"order_no": f"Order No '{order_no}' is already used by another sales order."},
                "submitted": submitted, "order": order,
            },
            status_code=400,
        )

    changes = diff_fields(order, {
        "customer_id": customer_id,
        "order_no": order_no or order.order_no,
        "notes": notes,
        "customer_po_no": customer_po_no or None,
        "customer_po_date": _parse_date(customer_po_date),
        "expected_shipment_date": _parse_date(expected_shipment_date),
        "billing_address": billing_address or None,
        "shipping_address": shipping_address or None,
        "ship_to_customer_id": int(ship_to_customer_id_str) if ship_to_customer_id_str.isdigit() else None,
    })
    old_line_count = len(order.items)
    old_total = order.total_amount

    order.customer_id = customer_id
    # Order No is a required column — if the field somehow arrives blank,
    # keep the existing number rather than leaving the order without one.
    order.order_no = order_no or order.order_no
    order.notes = notes
    order.customer_po_no = customer_po_no or None
    order.customer_po_date = _parse_date(customer_po_date)
    order.expected_shipment_date = _parse_date(expected_shipment_date)
    order.billing_address = billing_address or None
    order.shipping_address = shipping_address or None
    order.ship_to_customer_id = int(ship_to_customer_id_str) if ship_to_customer_id_str.isdigit() else None
    order.items = []  # cascade="all, delete-orphan" removes the old lines on flush

    total = 0
    gst_total = 0
    for item_id_str, qty_str, price_str in zip(item_ids, quantities, unit_prices):
        if not item_id_str or not qty_str:
            continue
        qty = float(qty_str)
        if qty <= 0:
            continue
        item = db.query(Item).get(int(item_id_str))
        try:
            unit_price = float(price_str)
        except (TypeError, ValueError):
            unit_price = float(item.sales_price)
        line_total = unit_price * qty
        total += line_total
        gst_total += line_total * float(item.gst_percentage or 0) / 100
        order.items.append(SalesOrderItem(
            item_id=item.id, quantity=qty, unit_price=unit_price, total=line_total,
        ))

    order.total_amount = total
    order.gst_amount = gst_total
    if len(order.items) != old_line_count or order.total_amount != old_total:
        changes["line_items"] = (
            f"{old_line_count} line(s), total {old_total}",
            f"{len(order.items)} line(s), total {order.total_amount}",
        )
    if was_cancelled:
        # Editing a cancelled order means it's being revived — send it back
        # through the normal lifecycle rather than leaving it edited but
        # still marked cancelled.
        changes["status"] = ("cancelled", "draft")
        order.status = "draft"
    log_field_changes(db, user, "sales_order", order.id, order.order_no, changes)
    db.commit()
    success_msg = "Sales order updated" + (" and moved back to draft" if was_cancelled else "")
    return RedirectResponse(url=f"/sales/{order_id}?success={success_msg}", status_code=303)


@router.post("/{order_id}/status")
def update_sales_status(
    order_id: int,
    new_status: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("sales", "edit")),
):
    order = db.query(SalesOrder).options(joinedload(SalesOrder.items)).get(order_id)
    old_status = order.status

    if new_status == "delivered" and order.status != "delivered":
        # Deduct stock and log a stock-out transaction for each line item.
        for line in order.items:
            item = db.query(Item).get(line.item_id)
            item.current_stock = (item.current_stock or 0) - line.quantity
            db.add(StockTransaction(
                item_id=item.id, transaction_type="OUT", quantity=line.quantity,
                reference_type="sales_order", reference_id=order.id,
                notes=f"Delivered against {order.order_no}",
            ))

    order.status = new_status
    if new_status != old_status:
        log_action(db, user, "sales_order", order.id, order.order_no, "status_change", summary=f"{old_status} -> {new_status}")
    db.commit()
    return RedirectResponse(url=f"/sales/{order_id}?success=Status updated", status_code=303)


@router.post("/{order_id}/payment-terms/{term_id}/status")
def update_payment_term_status(
    order_id: int,
    term_id: int,
    request: Request,
    new_status: str = Form(...),
    received_date: str = Form(None),
    return_to: str = Form(None),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("sales", "edit")),
):
    """Mark a single payment milestone Received (or back to Pending) —
    deliberately its own action rather than something set via the order's
    Edit form, so flipping it can't be lost by an unrelated order edit (see
    SalesOrderPaymentTerm's docstring). Callable both from the order detail
    page and the company-wide Payments Due page, hence `return_to`.

    The date the payment actually landed is asked for right on the Mark
    Received button (a date input defaulting to today, editable — money
    that arrived yesterday shouldn't have to be logged as today) rather
    than stamped automatically, since it's the real record of when cash
    showed up, not when someone got around to clicking the button. Falls
    back to today only if the field is somehow missing/unparseable (e.g.
    a hand-crafted request), so a Mark Received can never silently no-op.

    Both call sites (sales/detail.html, sales/payments_due.html) submit
    this via `fetch()` with an `X-Requested-With: XMLHttpRequest` header
    rather than a plain form POST, so the row updates in place — no page
    navigation, no lost scroll position on a long order or a long Payments
    Due list. When that header is present this returns JSON instead of a
    redirect; a plain form POST (JS disabled, or any other caller) still
    gets the original redirect-based behavior, so the action keeps working
    without JavaScript too."""
    term = (
        db.query(SalesOrderPaymentTerm)
        .filter(SalesOrderPaymentTerm.id == term_id, SalesOrderPaymentTerm.sales_order_id == order_id)
        .first()
    )
    if term and new_status in ("pending", "received"):
        old_status = term.status
        term.status = new_status
        if new_status == "received":
            term.received_date = _parse_date(received_date) or datetime.utcnow()
        else:
            term.received_date = None
        if new_status != old_status:
            log_action(
                db, user, "sales_order_payment_term", term.id, f"{term.sales_order.order_no}: {term.description}",
                "status_change", summary=f"{old_status} -> {new_status}",
            )
        db.commit()

    if request.headers.get("x-requested-with") == "XMLHttpRequest":
        return JSONResponse({
            "ok": term is not None,
            "term_id": term_id,
            "status": term.status if term else None,
            "received_date": term.received_date.strftime("%d-%b-%Y") if term and term.received_date else None,
        })
    return RedirectResponse(url=return_to or f"/sales/{order_id}", status_code=303)


@router.get("/{order_id}/payment-terms/edit")
def edit_payment_terms_form(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "edit"))):
    """Payment Schedule has its own editing screen, deliberately separate
    from the main Edit Sales Order form and not gated by order status at
    all — see SalesOrderPaymentTerm's docstring. Usable whether the order is
    draft, confirmed, delivered, or cancelled."""
    order = db.query(SalesOrder).options(joinedload(SalesOrder.payment_terms)).get(order_id)
    submitted_terms = [
        {
            "description": t.description,
            "percentage": "" if t.percentage is None else float(t.percentage),
            "amount": float(t.amount),
            "due_date": t.due_date.strftime("%Y-%m-%d") if t.due_date else "",
            "days_after_invoice": t.days_after_invoice if t.days_after_invoice is not None else "",
            "secured_by": t.secured_by,
            "bg_expiry_date": t.bg_expiry_date.strftime("%Y-%m-%d") if t.bg_expiry_date else "",
            "status": t.status,
            "received_date": t.received_date.strftime("%Y-%m-%d") if t.received_date else "",
        }
        for t in order.payment_terms
    ]
    return templates.TemplateResponse(
        "sales/payment_terms_form.html",
        {"request": request, "user": user, "order": order, "submitted_terms": submitted_terms},
    )


@router.post("/{order_id}/payment-terms/edit")
async def update_payment_terms(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("sales", "edit"))):
    order = db.query(SalesOrder).options(joinedload(SalesOrder.payment_terms)).get(order_id)
    form = await request.form()
    payment_term_rows = _payment_terms_from_form(form)
    order.payment_terms = []  # cascade="all, delete-orphan" removes the old rows on flush
    for row in payment_term_rows:
        order.payment_terms.append(_build_payment_term(row))
    db.commit()
    return RedirectResponse(url=f"/sales/{order_id}?success=Payment schedule updated", status_code=303)
