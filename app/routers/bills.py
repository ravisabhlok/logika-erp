"""Vendor Bills — the accounts-payable counterpart to Invoice. See
app/models.py's Bill/BillItem/BillPayment docstrings and
claude/vendor-bills-design.md for the agreed design.

Two entry points, both landing on this router's /bills/new:
  - From a received Purchase Order's own detail page ("Record Bill" button)
    — po_id is passed, and this bill is a document-level record only
    (vendor's bill number/date/amount/GST/attachment); stock was already
    moved when the PO was received.
  - Standalone, for a purchase made with no PO at all — no po_id, and
    submitting this bill is what moves stock (and captures serials for a
    has_serial item), same effect as a PO receive, in one step.
"""
import os
import re
import uuid
from datetime import datetime

from fastapi import APIRouter, Request, Depends, Query
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.auth import require_module_permission, get_user_module_permissions
from app.formatting import format_qty, format_inr, format_dt
from app.models import (
    Bill, BillItem, BillPayment, PurchaseOrder, PurchaseOrderItem, Vendor,
    Item, ItemSerial, StockTransaction, User,
)
from app.audit import log_action

router = APIRouter(prefix="/bills", tags=["bills"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["qty"] = format_qty
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt

# Same flat-folder-on-disk pattern as ItemAttachment (app/routers/items.py) —
# only the generated filename + original name are stored in the DB, never
# the file bytes. Shares the folder with item attachments; the unique
# uuid-based stored filename keeps the two from ever colliding.
ATTACHMENT_DIR = "app/static/uploads"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
PDF_EXTENSIONS = {".pdf"}


def _attachment_kind(filename: str) -> str | None:
    ext = os.path.splitext(filename)[1].lower()
    if ext in IMAGE_EXTENSIONS or ext in PDF_EXTENSIONS:
        return ext
    return None


def _parse_date(value: str):
    """Same as sales.py/invoices.py's own _parse_date — HTML <input
    type=date> gives 'YYYY-MM-DD' or ''."""
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
    """Same as invoices.py's own _financial_year_label — '25-26' for any
    date between 1-Apr-2025 and 31-Mar-2026."""
    start = dt.year if dt.month >= 4 else dt.year - 1
    end = start + 1
    return f"{start % 100:02d}-{end % 100:02d}"


def next_bill_ref(db: Session, bill_date: datetime) -> str:
    """BILL/<FY>/0001, resetting every financial year (1-Apr) — derived from
    the highest existing numeric suffix within this FY's prefix, not a row
    count, same reasoning as next_invoice_no/next_order_no."""
    prefix = f"BILL/{_financial_year_label(bill_date)}/"
    existing = db.query(Bill.bill_ref).filter(Bill.bill_ref.like(f"{prefix}%")).all()
    max_n = 0
    for (bill_ref,) in existing:
        suffix = bill_ref[len(prefix):]
        if suffix.isdigit():
            max_n = max(max_n, int(suffix))
    return f"{prefix}{max_n + 1:04d}"


def recompute_bill_status(bill: Bill) -> None:
    """Derive amount_paid/status from this bill's own (non-deleted)
    payments — never hand-edited, same 'derive, don't hand-maintain'
    approach as InvoiceItem/remaining_to_invoice. Leaves a 'cancelled' bill
    alone; call this before checking bill.status == 'cancelled' if that
    matters to the caller."""
    if bill.status == "cancelled":
        return
    paid = sum(float(p.amount) for p in bill.payments)
    bill.amount_paid = paid
    total = float(bill.total_amount or 0)
    if paid <= 1e-9:
        bill.status = "unpaid"
    elif paid + 1e-9 >= total:
        bill.status = "paid"
    else:
        bill.status = "partially_paid"


def record_po_bill(
    db: Session, order: PurchaseOrder, user: User,
    vendor_bill_no: str, bill_date: datetime, due_date, notes: str,
    attachment_filename: str = None, attachment_bytes: bytes = None,
    quantities: dict = None,
) -> Bill:
    """Quick-entry path used by Purchase's own Receive step (an optional
    'enter the bill while you're here' section — see
    purchase.py:receive_purchase_order) as well as anywhere else that wants
    a document-level bill at the PO's own quantities/prices with no line
    editing. One BillItem per PO line, at that line's own quantity/price —
    never the fuller, editable-line version that /bills/new's PO-based form
    offers. `quantities` ({po_line_id: qty}) limits the bill to what arrived
    in one shipment — the Receive step passes exactly the qty received
    now, so a partial receipt gets a bill for just that part; lines missing
    from it (or 0) are left off the bill. None = every line at its full
    ordered qty. Caller commits;
    stock/serials are never touched here (purchase_order_item_id is set on
    every line, exactly like a PO-based /bills/new submission)."""
    bill = Bill(
        bill_ref=next_bill_ref(db, bill_date),
        vendor_id=order.vendor_id, purchase_order_id=order.id,
        vendor_bill_no=vendor_bill_no, bill_date=bill_date, due_date=due_date,
        notes=notes, status="unpaid", amount_paid=0, stock_updated=False,
        created_by=user.id,
    )
    taxable_total = 0.0
    gst_total = 0.0
    for po_line in order.items:
        qty = po_line.quantity if quantities is None else quantities.get(po_line.id, 0)
        if not qty or float(qty) <= 0:
            continue
        line_total = float(qty) * float(po_line.unit_price)
        gst_pct = float(po_line.item.gst_percentage or 0)
        taxable_total += line_total
        gst_total += line_total * gst_pct / 100
        bill.items.append(BillItem(
            purchase_order_item_id=po_line.id, item_id=po_line.item_id,
            quantity=qty, unit_price=po_line.unit_price,
            gst_percentage=gst_pct, hsn_code=po_line.item.hsn_code, total=line_total,
        ))
    bill.taxable_amount = taxable_total
    bill.gst_amount = gst_total
    bill.total_amount = taxable_total + gst_total

    if attachment_filename and attachment_bytes is not None:
        kind = _attachment_kind(attachment_filename)
        if kind:
            os.makedirs(ATTACHMENT_DIR, exist_ok=True)
            stored_filename = f"{uuid.uuid4().hex}{kind}"
            with open(os.path.join(ATTACHMENT_DIR, stored_filename), "wb") as f:
                f.write(attachment_bytes)
            bill.attachment_original_filename = attachment_filename
            bill.attachment_stored_filename = stored_filename

    db.add(bill)
    return bill


# A PO can be billed as soon as anything has arrived — vendors usually bill
# each dispatch separately when a PO is delivered in parts.
BILLABLE_PO_STATUSES = ("partially_received", "received")


def unbilled_qty_by_po_line(db: Session, order: PurchaseOrder) -> dict:
    """{po_line_id: received qty not yet covered by a (non-cancelled) bill}
    — the default qty the Record Bill form pre-fills per line, so billing
    shipment-by-shipment never double-counts. Only a default: the form
    stays editable to match whatever the vendor actually invoiced."""
    billed = {}
    for b in order.bills:
        if b.status == "cancelled":
            continue
        for bi in b.items:
            if bi.purchase_order_item_id:
                billed[bi.purchase_order_item_id] = billed.get(bi.purchase_order_item_id, 0.0) + float(bi.quantity or 0)
    return {
        line.id: max(float(line.received_quantity or 0) - billed.get(line.id, 0.0), 0.0)
        for line in order.items
    }


@router.get("")
def list_bills(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("bills", "view"))):
    bills = (
        db.query(Bill)
        .options(joinedload(Bill.vendor), joinedload(Bill.purchase_order))
        .order_by(Bill.bill_date.desc(), Bill.id.desc())
        .all()
    )
    perms = get_user_module_permissions(user, db, "bills")
    return templates.TemplateResponse("bills/list.html", {"request": request, "user": user, "bills": bills, "perms": perms})


@router.get("/new")
def new_bill_form(
    request: Request, po_id: int = Query(None),
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("bills", "add")),
):
    order = None
    unbilled = {}
    if po_id:
        order = (
            db.query(PurchaseOrder)
            .options(joinedload(PurchaseOrder.vendor), joinedload(PurchaseOrder.items).joinedload(PurchaseOrderItem.item))
            .get(po_id)
        )
        if not order:
            return RedirectResponse(url="/purchase?error=Purchase order not found", status_code=303)
        if order.status not in BILLABLE_PO_STATUSES:
            return RedirectResponse(
                url=f"/purchase/{po_id}?error=Only a received (or partially received) order can have a bill recorded against it",
                status_code=303,
            )
        unbilled = unbilled_qty_by_po_line(db, order)

    vendors = db.query(Vendor).order_by(Vendor.name).all()
    items = db.query(Item).filter(Item.is_active == True).order_by(Item.name).all()  # noqa: E712
    return templates.TemplateResponse(
        "bills/new.html",
        {
            "request": request, "user": user, "order": order, "vendors": vendors, "items": items,
            "bill_date": datetime.utcnow().strftime("%Y-%m-%d"),
            "submitted": {}, "errors": {}, "unbilled": unbilled,
        },
    )


@router.post("/new")
async def create_bill(
    request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("bills", "add")),
):
    form = await request.form()
    po_id_str = (form.get("purchase_order_id") or "").strip()
    vendor_bill_no = (form.get("vendor_bill_no") or "").strip()
    bill_date = _parse_date(form.get("bill_date", "")) or datetime.utcnow()
    due_date = _parse_date(form.get("due_date", ""))
    notes = form.get("notes", "")

    order = None
    if po_id_str:
        order = (
            db.query(PurchaseOrder)
            .options(joinedload(PurchaseOrder.vendor), joinedload(PurchaseOrder.items).joinedload(PurchaseOrderItem.item))
            .get(int(po_id_str))
        )
        if not order or order.status not in BILLABLE_PO_STATUSES:
            return RedirectResponse(url="/purchase?error=Only a received (or partially received) order can have a bill recorded against it", status_code=303)
        vendor_id = order.vendor_id
    else:
        vendor_id_str = (form.get("vendor_id") or "").strip()
        if not vendor_id_str:
            return await _rerender_new_form(request, db, user, order, {"vendor_id": "Select a vendor."}, form)
        vendor_id = int(vendor_id_str)

    if not vendor_bill_no:
        return await _rerender_new_form(request, db, user, order, {"vendor_bill_no": "Enter the vendor's bill number."}, form)

    bill = Bill(
        bill_ref=next_bill_ref(db, bill_date),
        vendor_id=vendor_id, purchase_order_id=order.id if order else None,
        vendor_bill_no=vendor_bill_no, bill_date=bill_date, due_date=due_date,
        notes=notes, status="unpaid", amount_paid=0, stock_updated=False,
        created_by=user.id,
    )

    taxable_total = 0.0
    gst_total = 0.0
    errors = {}

    if order:
        # PO-based: document-level only, no stock/serial effect — the PO's
        # own Receive step already moved stock. Lines are keyed by
        # purchase_order_item_id so bill.items still traces back to the
        # exact PO line even if qty/price were touched up to match what the
        # vendor actually billed.
        line_ids = form.getlist("purchase_order_item_id")
        quantities = form.getlist("quantity")
        unit_prices = form.getlist("unit_price")
        lines_by_id = {line.id: line for line in order.items}
        for line_id_str, qty_str, price_str in zip(line_ids, quantities, unit_prices):
            if not line_id_str or not qty_str:
                continue
            po_line = lines_by_id.get(int(line_id_str))
            if not po_line:
                continue
            try:
                qty = float(qty_str)
                unit_price = float(price_str)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            line_total = qty * unit_price
            gst_pct = float(po_line.item.gst_percentage or 0)
            taxable_total += line_total
            gst_total += line_total * gst_pct / 100
            bill.items.append(BillItem(
                purchase_order_item_id=po_line.id, item_id=po_line.item_id,
                quantity=qty, unit_price=unit_price, gst_percentage=gst_pct,
                hsn_code=po_line.item.hsn_code, total=line_total,
            ))
        if not bill.items:
            return await _rerender_new_form(request, db, user, order, {"_general": "Enter a quantity for at least one line."}, form)
    else:
        # No-PO: this bill is what records the purchase at all, so it moves
        # stock itself — same shape as purchase.py:receive_purchase_order's
        # validate-everything-then-write-everything flow, just with fresh
        # item lines instead of existing PO lines.
        item_ids = form.getlist("item_id")
        quantities = form.getlist("quantity")
        unit_prices = form.getlist("unit_price")

        parsed_lines = []
        for idx, (item_id_str, qty_str, price_str) in enumerate(zip(item_ids, quantities, unit_prices)):
            if not item_id_str or not qty_str:
                continue
            item = db.query(Item).get(int(item_id_str))
            if not item:
                continue
            try:
                qty = float(qty_str)
                unit_price = float(price_str)
            except (TypeError, ValueError):
                continue
            if qty <= 0:
                continue
            parsed_lines.append({"idx": idx, "item": item, "qty": qty, "unit_price": unit_price})

        if not parsed_lines:
            return await _rerender_new_form(request, db, user, order, {"_general": "Add at least one item line."}, form)

        # Pass 1: validate serial counts for every serialized line before
        # writing anything.
        serials_by_idx = {}
        for line in parsed_lines:
            if not line["item"].has_serial:
                continue
            raw = form.get(f"serials_{line['idx']}", "")
            serials = _split_serials(raw)
            serials_by_idx[line["idx"]] = serials
            if len(serials) != line["qty"]:
                errors[line["idx"]] = f"Expected {line['qty']:g} serial number(s), got {len(serials)}."
            elif len(set(serials)) != len(serials):
                errors[line["idx"]] = "Duplicate serial numbers entered for this line."

        if not errors:
            for line in parsed_lines:
                if not line["item"].has_serial:
                    continue
                existing = {s for (s,) in db.query(ItemSerial.serial_number).filter(ItemSerial.item_id == line["item"].id).all()}
                clashes = existing.intersection(serials_by_idx.get(line["idx"], []))
                if clashes:
                    errors[line["idx"]] = f"Already recorded for this item: {', '.join(sorted(clashes))}"

        if errors:
            return await _rerender_new_form(request, db, user, order, errors, form)

        for line in parsed_lines:
            item = line["item"]
            line_total = line["qty"] * line["unit_price"]
            gst_pct = float(item.gst_percentage or 0)
            taxable_total += line_total
            gst_total += line_total * gst_pct / 100
            bill_item = BillItem(
                item_id=item.id, quantity=line["qty"], unit_price=line["unit_price"],
                gst_percentage=gst_pct, hsn_code=item.hsn_code, total=line_total,
            )
            bill.items.append(bill_item)
        bill.stock_updated = True

    bill.taxable_amount = taxable_total
    bill.gst_amount = gst_total
    bill.total_amount = taxable_total + gst_total

    # Attachment — optional. Same save-to-disk pattern as items.py's
    # upload_attachments; unsupported types are silently skipped rather
    # than blocking the whole bill, since the attachment is a convenience,
    # not the record itself.
    upload = form.get("attachment")
    if upload is not None and getattr(upload, "filename", ""):
        kind = _attachment_kind(upload.filename)
        if kind:
            os.makedirs(ATTACHMENT_DIR, exist_ok=True)
            stored_filename = f"{uuid.uuid4().hex}{kind}"
            contents = await upload.read()
            with open(os.path.join(ATTACHMENT_DIR, stored_filename), "wb") as f:
                f.write(contents)
            bill.attachment_original_filename = upload.filename
            bill.attachment_stored_filename = stored_filename

    db.add(bill)
    db.flush()  # need bill.id / bill_item.id before writing stock+serials

    if not order:
        # Now that every BillItem has a real id, actually move stock and
        # capture serials — mirrors receive_purchase_order's write phase.
        for bill_item in bill.items:
            item = bill_item.item
            item.current_stock = float(item.current_stock or 0) + float(bill_item.quantity)
            db.add(StockTransaction(
                item_id=item.id, transaction_type="IN", quantity=bill_item.quantity,
                reference_type="bill", reference_id=bill.id,
                notes=f"Received against {bill.bill_ref} (no PO)",
            ))
        # Serials need the original form-index -> BillItem mapping; rebuild
        # it in the same order the lines were appended above (bill.items is
        # still in insertion order at this point, pre-commit).
        for line, bill_item in zip(parsed_lines, bill.items):
            for serial in serials_by_idx.get(line["idx"], []):
                db.add(ItemSerial(item_id=bill_item.item_id, serial_number=serial, bill_item_id=bill_item.id))

    log_action(
        db, user, "bill", bill.id, bill.bill_ref, "create",
        summary=(f"Recorded against {order.order_no}" if order else "Recorded directly (no PO)")
        + f" — {len(bill.items)} line(s), total {bill.total_amount}"
        + (", stock and serials updated" if bill.stock_updated else ""),
    )
    db.commit()
    return RedirectResponse(url=f"/bills/{bill.id}?success=Bill recorded", status_code=303)


async def _rerender_new_form(request, db, user, order, errors, form):
    vendors = db.query(Vendor).order_by(Vendor.name).all()
    items = db.query(Item).filter(Item.is_active == True).order_by(Item.name).all()  # noqa: E712
    submitted = {
        "vendor_id": form.get("vendor_id", ""),
        "vendor_bill_no": form.get("vendor_bill_no", ""),
        "bill_date": form.get("bill_date", ""),
        "due_date": form.get("due_date", ""),
        "notes": form.get("notes", ""),
        "lines": [],
    }
    if order:
        for line_id, qty, price in zip(form.getlist("purchase_order_item_id"), form.getlist("quantity"), form.getlist("unit_price")):
            submitted["lines"].append({"purchase_order_item_id": line_id, "quantity": qty, "unit_price": price})
    else:
        for i, (item_id, qty, price) in enumerate(zip(form.getlist("item_id"), form.getlist("quantity"), form.getlist("unit_price"))):
            submitted["lines"].append({"item_id": item_id, "quantity": qty, "unit_price": price, "serials": form.get(f"serials_{i}", "")})
    return templates.TemplateResponse(
        "bills/new.html",
        {
            "request": request, "user": user, "order": order, "vendors": vendors, "items": items,
            "bill_date": submitted["bill_date"] or datetime.utcnow().strftime("%Y-%m-%d"),
            "submitted": submitted, "errors": errors,
        },
        status_code=400,
    )


@router.get("/{bill_id}")
def bill_detail(bill_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("bills", "view"))):
    bill = (
        db.query(Bill)
        .options(
            joinedload(Bill.vendor), joinedload(Bill.purchase_order),
            joinedload(Bill.items).joinedload(BillItem.item),
            joinedload(Bill.items).joinedload(BillItem.serials),
            joinedload(Bill.payments),
        )
        .get(bill_id)
    )
    if not bill:
        return RedirectResponse(url="/bills?error=Bill not found", status_code=303)
    perms = get_user_module_permissions(user, db, "bills")
    return templates.TemplateResponse(
        "bills/detail.html",
        {"request": request, "user": user, "bill": bill, "perms": perms, "today": datetime.utcnow().strftime("%Y-%m-%d")},
    )


@router.post("/{bill_id}/payments")
async def record_payment(
    bill_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("bills", "edit")),
):
    bill = db.query(Bill).options(joinedload(Bill.payments)).get(bill_id)
    if not bill:
        return RedirectResponse(url="/bills?error=Bill not found", status_code=303)
    if bill.status == "cancelled":
        return RedirectResponse(url=f"/bills/{bill_id}?error=Cannot record a payment against a cancelled bill", status_code=303)

    form = await request.form()
    try:
        amount = float(form.get("amount", ""))
    except (TypeError, ValueError):
        return RedirectResponse(url=f"/bills/{bill_id}?error=Enter a valid payment amount", status_code=303)
    if amount <= 0:
        return RedirectResponse(url=f"/bills/{bill_id}?error=Payment amount must be greater than zero", status_code=303)

    balance = float(bill.total_amount) - float(bill.amount_paid)
    if amount - balance > 1e-9:
        return RedirectResponse(url=f"/bills/{bill_id}?error=Payment of {amount:g} exceeds the outstanding balance of {balance:.2f}", status_code=303)

    payment_date = _parse_date(form.get("payment_date", "")) or datetime.utcnow()
    bill.payments.append(BillPayment(
        payment_date=payment_date, amount=amount,
        mode=form.get("mode", "") or None, reference_no=form.get("reference_no", "") or None,
        notes=form.get("notes", "") or None, created_by=user.id,
    ))
    recompute_bill_status(bill)
    log_action(
        db, user, "bill", bill.id, bill.bill_ref, "payment",
        summary=f"Payment of {amount:.2f} recorded — status now {bill.status}",
    )
    db.commit()
    return RedirectResponse(url=f"/bills/{bill_id}?success=Payment recorded", status_code=303)


@router.post("/{bill_id}/payments/{payment_id}/delete")
def delete_payment(bill_id: int, payment_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("bills", "edit"))):
    bill = db.query(Bill).options(joinedload(Bill.payments)).get(bill_id)
    if not bill:
        return RedirectResponse(url="/bills?error=Bill not found", status_code=303)
    if bill.status == "cancelled":
        return RedirectResponse(url=f"/bills/{bill_id}?error=This bill is cancelled", status_code=303)
    payment = next((p for p in bill.payments if p.id == payment_id), None)
    if not payment:
        return RedirectResponse(url=f"/bills/{bill_id}?error=Payment not found", status_code=303)
    db.delete(payment)
    db.flush()
    bill.payments = [p for p in bill.payments if p.id != payment_id]
    recompute_bill_status(bill)
    log_action(db, user, "bill", bill.id, bill.bill_ref, "payment_delete", summary=f"Removed payment of {payment.amount} — status now {bill.status}")
    db.commit()
    return RedirectResponse(url=f"/bills/{bill_id}?success=Payment removed", status_code=303)


@router.post("/{bill_id}/cancel")
def cancel_bill(bill_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("bills", "edit"))):
    bill = (
        db.query(Bill)
        .options(joinedload(Bill.items).joinedload(BillItem.item), joinedload(Bill.items).joinedload(BillItem.serials), joinedload(Bill.payments))
        .get(bill_id)
    )
    if not bill:
        return RedirectResponse(url="/bills?error=Bill not found", status_code=303)
    if bill.status == "cancelled":
        return RedirectResponse(url=f"/bills/{bill_id}?error=Already cancelled", status_code=303)
    if bill.payments:
        return RedirectResponse(url=f"/bills/{bill_id}?error=Remove the recorded payment(s) before cancelling this bill", status_code=303)

    if bill.stock_updated:
        shortages = []
        for line in bill.items:
            available = float(line.item.current_stock or 0)
            if available < float(line.quantity):
                shortages.append(f"{line.item.name}: only {available:g} left, this bill added {line.quantity:g}")
        if shortages:
            return RedirectResponse(
                url=f"/bills/{bill_id}?error=Cannot cancel — stock already used: " + "; ".join(shortages),
                status_code=303,
            )
        for line in bill.items:
            item = line.item
            item.current_stock = float(item.current_stock or 0) - float(line.quantity)
            db.add(StockTransaction(
                item_id=item.id, transaction_type="OUT", quantity=line.quantity,
                reference_type="bill_cancel", reference_id=bill.id,
                notes=f"{bill.bill_ref} cancelled — stock reversed",
            ))
            for serial in list(line.serials):
                db.delete(serial)

    bill.status = "cancelled"
    bill.cancelled_by = user.id
    bill.cancelled_at = datetime.utcnow()
    log_action(
        db, user, "bill", bill.id, bill.bill_ref, "cancel",
        summary="Cancelled" + (" — stock and serials reversed" if bill.stock_updated else " (document only, no stock movement to reverse)"),
    )
    db.commit()
    return RedirectResponse(url=f"/bills/{bill_id}?success=Bill cancelled", status_code=303)
