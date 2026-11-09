"""Advance payments against a Purchase Order.

An advance is paid to the vendor while the PO is still ordered / partly received. It is stored as a
PurchaseOrderAdvance only once the money has really gone out (bank confirmation of a PO_ADVANCE payment line, or a
cheque/cash advance recorded by hand).

When a bill is booked against the PO, apply_po_advances() turns whatever is still unused into BillPayment rows on that
bill (po_advance_id set). Vendors often supply a PO in parts, so one advance can be spread over several bills: each bill
takes only as much as it needs and the rest stays available for the next one. Removing such a payment from a bill (or
cancelling the bill) frees the advance again.
"""
from datetime import datetime
from decimal import Decimal

from app.audit import log_action
from app.models import Bill, BillPayment, PaymentLine, PurchaseOrderAdvance

ADVANCE_STATUSES = ("ordered", "partially_received")  # PO states in which an advance can still be paid
TOLERANCE = Decimal("0.005")


def po_total(order):
    return Decimal(str(order.total_amount or 0)) + Decimal(str(order.gst_amount or 0))


def advance_used(db, advance):
    return sum((Decimal(str(p.amount)) for p in
                db.query(BillPayment).filter(BillPayment.po_advance_id == advance.id).all()), Decimal(0))


def advance_remaining(db, advance):
    return Decimal(str(advance.amount)) - advance_used(db, advance)


def po_advance_summary(db, order):
    """Position of the PO's advances: paid (money out), in_progress (sent to bank, not confirmed yet), used (applied to
    bills), unused (paid but not applied yet), room (how much more may still be sent), lines (bank payment lines)."""
    paid = sum((Decimal(str(a.amount)) for a in order.advances), Decimal(0))
    used = sum((advance_used(db, a) for a in order.advances), Decimal(0))
    lines = (db.query(PaymentLine).filter(PaymentLine.source_type == "PO_ADVANCE", PaymentLine.source_id == order.id,
                                          PaymentLine.status != "Withdrawn").order_by(PaymentLine.id).all())
    in_progress = sum((Decimal(str(ln.amount)) for ln in lines if ln.status != "Confirmed"), Decimal(0))
    return dict(paid=paid, used=used, unused=paid - used, in_progress=in_progress, lines=lines,
                room=po_total(order) - paid - in_progress)


def record_advance(db, user, order, amount, paid_date, mode, reference_no, notes, payment_line_id=None):
    """Store a paid advance and immediately apply it to any open bill of the PO. Caller commits."""
    adv = PurchaseOrderAdvance(
        purchase_order_id=order.id, amount=amount, paid_date=paid_date, mode=mode, reference_no=reference_no,
        notes=notes, payment_line_id=payment_line_id, created_by=user.id if user else None)
    db.add(adv)
    db.flush()
    log_action(db, user, "purchase_order", order.id, order.order_no, "advance",
               summary=f"Advance of {amount} paid ({mode or 'n/a'}{', ' + reference_no if reference_no else ''})")
    apply_po_advances(db, order, user)
    return adv


def apply_po_advances(db, order, user=None):
    """Spread the PO's unused advance over its open (non-cancelled, not fully paid) bills, oldest first. Safe to call
    any time - it only moves what is unused into bills that still owe something."""
    from app.routers.bills import recompute_bill_status  # lazy: bills.py imports this module
    from app.payments_service import bill_summary

    advances = (db.query(PurchaseOrderAdvance).filter(PurchaseOrderAdvance.purchase_order_id == order.id)
                .order_by(PurchaseOrderAdvance.id).all())
    if not advances:
        return
    bills = (db.query(Bill).filter(Bill.purchase_order_id == order.id, Bill.status != "cancelled")
             .order_by(Bill.id).all())
    for bill in bills:
        recompute_bill_status(bill)
        # a bank payment already on its way to the vendor counts as spoken for, so leave room for it
        owed = bill_summary(db, bill)["balance"]
        for adv in advances:
            if owed <= TOLERANCE:
                break
            take = min(advance_remaining(db, adv), owed)
            if take <= TOLERANCE:
                continue
            take = take.quantize(Decimal("0.01"))
            bill.payments.append(BillPayment(
                payment_date=datetime.utcnow(), amount=take, mode="Advance", reference_no=adv.reference_no,
                notes=f"Advance paid against {order.order_no}", po_advance_id=adv.id,
                created_by=user.id if user else None))
            db.flush()
            owed -= take
            recompute_bill_status(bill)
            log_action(db, user, "bill", bill.id, bill.bill_ref, "payment",
                       summary=f"Advance of {take} from {order.order_no} applied - status now {bill.status}")
