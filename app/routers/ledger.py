"""Party ledgers — one running account per Customer, Vendor and Payee.

Nothing is stored: every ledger is derived on the fly from documents that
already exist, so it can never disagree with them.

  Customer (receivable, balance shown Dr = they owe us)
      Dr  issued Invoices (taxable + GST)
      Cr  sales-order payment terms marked received
  Vendor (payable, balance shown Cr = we owe them)
      Cr  non-cancelled Bills
      Dr  bill payments (manual or bank-confirmed) + PO advances paid
          (an advance later applied to a bill is a BillPayment with
          po_advance_id — skipped, or it would count twice)
  Payee (payable, Cr = we owe them)
      Cr  non-cancelled expense entries
      Dr  bank-confirmed payment lines to that payee
"""
from datetime import date, datetime
from decimal import Decimal

from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_module_permission
from app.formatting import format_inr
from app.models import (
    Customer, Vendor, Payee, SalesOrder, SalesOrderPaymentTerm, Invoice, Bill, BillPayment,
    PurchaseOrder, PurchaseOrderAdvance, ExpenseEntry, PaymentLine, User,
)

router = APIRouter(prefix="/ledger", tags=["ledger"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["inr"] = format_inr

KINDS = {
    "customer": "Customers",
    "vendor": "Vendors",
    "payee": "Payees",
}
ZERO = Decimal("0")


def _d(value) -> date:
    if isinstance(value, datetime):
        return value.date()
    return value


def _dec(value) -> Decimal:
    return Decimal(str(value or 0))


def _customer_entries(db: Session, cid: int) -> list[dict]:
    rows = []
    invoices = (
        db.query(Invoice).join(SalesOrder, Invoice.sales_order_id == SalesOrder.id)
        .filter(SalesOrder.customer_id == cid, Invoice.status == "issued").all()
    )
    for inv in invoices:
        rows.append({
            "date": _d(inv.invoice_date), "ref": inv.invoice_no, "kind": "Invoice",
            "desc": f"Against {inv.sales_order.order_no}",
            "debit": _dec(inv.total_amount) + _dec(inv.gst_amount), "credit": ZERO,
            "link": f"/invoices/{inv.id}",
        })
    terms = (
        db.query(SalesOrderPaymentTerm).join(SalesOrder, SalesOrderPaymentTerm.sales_order_id == SalesOrder.id)
        .filter(SalesOrder.customer_id == cid, SalesOrderPaymentTerm.status == "received").all()
    )
    for t in terms:
        rows.append({
            "date": _d(t.received_date or t.created_at), "ref": t.sales_order.order_no, "kind": "Receipt",
            "desc": t.description, "debit": ZERO, "credit": _dec(t.amount),
            "link": f"/sales/{t.sales_order_id}",
        })
    return rows


def _vendor_entries(db: Session, vid: int) -> list[dict]:
    rows = []
    bills = db.query(Bill).filter(Bill.vendor_id == vid, Bill.status != "cancelled").all()
    for b in bills:
        rows.append({
            "date": _d(b.bill_date), "ref": b.vendor_bill_no, "kind": "Bill",
            "desc": b.bill_ref, "debit": ZERO, "credit": _dec(b.total_amount), "link": f"/bills/{b.id}",
        })
        for p in b.payments:
            if p.po_advance_id:
                continue
            rows.append({
                "date": _d(p.payment_date), "ref": p.reference_no or "", "kind": "Payment",
                "desc": f"{p.mode or 'Payment'} against {b.vendor_bill_no}",
                "debit": _dec(p.amount), "credit": ZERO, "link": f"/bills/{b.id}",
            })
    advances = (
        db.query(PurchaseOrderAdvance).join(PurchaseOrder, PurchaseOrderAdvance.purchase_order_id == PurchaseOrder.id)
        .filter(PurchaseOrder.vendor_id == vid).all()
    )
    for a in advances:
        rows.append({
            "date": _d(a.paid_date), "ref": a.reference_no or "", "kind": "Advance",
            "desc": f"Advance on {a.purchase_order.order_no}",
            "debit": _dec(a.amount), "credit": ZERO, "link": f"/purchase/{a.purchase_order_id}",
        })
    return rows


def _payee_entries(db: Session, pid: int) -> list[dict]:
    rows = []
    for e in db.query(ExpenseEntry).filter(ExpenseEntry.payee_id == pid, ExpenseEntry.is_cancelled == False).all():  # noqa: E712
        rows.append({
            "date": e.due_date, "ref": e.bill_no or "", "kind": "Expense",
            "desc": " - ".join(x for x in (e.category, e.description) if x),
            "debit": ZERO, "credit": _dec(e.amount), "link": None,
        })
    for ln in db.query(PaymentLine).filter(PaymentLine.payee_id == pid, PaymentLine.status == "Confirmed").all():
        rows.append({
            "date": ln.confirmed_on or _d(ln.created_at), "ref": ln.bank_ref or "", "kind": "Payment",
            "desc": f"{ln.mode} bank payment", "debit": _dec(ln.amount), "credit": ZERO, "link": None,
        })
    return rows


def _entries(db: Session, kind: str, pid: int) -> list[dict]:
    return {"customer": _customer_entries, "vendor": _vendor_entries, "payee": _payee_entries}[kind](db, pid)


def _sign(kind: str) -> int:
    """+1 when balance = debit - credit (customer), -1 when credit - debit."""
    return 1 if kind == "customer" else -1


def _party(db: Session, kind: str, pid: int):
    model = {"customer": Customer, "vendor": Vendor, "payee": Payee}[kind]
    return db.get(model, pid)


@router.get("")
def ledger_index(
    request: Request, kind: str = "customer", show_zero: int = 0,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view")),
):
    if kind not in KINDS:
        kind = "customer"
    if kind == "customer":
        parties = db.query(Customer).order_by(Customer.name).all()
    elif kind == "vendor":
        parties = db.query(Vendor).order_by(Vendor.name).all()
    else:
        parties = db.query(Payee).filter(Payee.is_deleted == False).order_by(Payee.name).all()  # noqa: E712

    sign = _sign(kind)
    rows = []
    total_dr = total_cr = ZERO
    for p in parties:
        ents = _entries(db, kind, p.id)
        dr = sum((e["debit"] for e in ents), ZERO)
        cr = sum((e["credit"] for e in ents), ZERO)
        bal = (dr - cr) * sign
        if not show_zero and not ents:
            continue
        rows.append({"id": p.id, "name": p.name, "debit": dr, "credit": cr, "balance": bal})
        total_dr += dr
        total_cr += cr
    return templates.TemplateResponse("ledger/index.html", {
        "request": request, "user": user, "kind": kind, "kinds": KINDS, "rows": rows,
        "total_dr": total_dr, "total_cr": total_cr, "total_bal": (total_dr - total_cr) * sign,
        "show_zero": show_zero,
        "bal_label": "Receivable" if kind == "customer" else "Payable",
    })


def _parse(s: str):
    try:
        return datetime.strptime(s, "%Y-%m-%d").date() if s else None
    except ValueError:
        return None


@router.get("/{kind}/{party_id}")
def ledger_detail(
    request: Request, kind: str, party_id: int, date_from: str = "", date_to: str = "",
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view")),
):
    if kind not in KINDS:
        return templates.TemplateResponse("403.html", {"request": request, "user": user}, status_code=404)
    party = _party(db, kind, party_id)
    if not party:
        return templates.TemplateResponse("403.html", {"request": request, "user": user}, status_code=404)

    sign = _sign(kind)
    d_from, d_to = _parse(date_from), _parse(date_to)
    ents = sorted(_entries(db, kind, party_id), key=lambda e: (e["date"] or date.min, e["kind"] != "Invoice" and e["kind"] != "Bill" and e["kind"] != "Expense"))

    opening = ZERO
    if d_from:
        opening = sum((e["debit"] - e["credit"] for e in ents if e["date"] and e["date"] < d_from), ZERO) * sign
    shown = [e for e in ents if (not d_from or (e["date"] and e["date"] >= d_from)) and (not d_to or (e["date"] and e["date"] <= d_to))]

    bal = opening
    for e in shown:
        bal += (e["debit"] - e["credit"]) * sign
        e["balance"] = bal
    return templates.TemplateResponse("ledger/detail.html", {
        "request": request, "user": user, "kind": kind, "kinds": KINDS, "party": party, "entries": shown,
        "opening": opening, "closing": bal,
        "total_dr": sum((e["debit"] for e in shown), ZERO), "total_cr": sum((e["credit"] for e in shown), ZERO),
        "date_from": date_from, "date_to": date_to,
        "bal_label": "Receivable (they owe us)" if kind == "customer" else "Payable (we owe them)",
        "party_url": {"customer": "/customers/", "vendor": "/vendors/"}.get(kind),
    })
