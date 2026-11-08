"""The one way any page sends a payment to the bank queue.

A page that owes someone money (a vendor Bill, an Expense entry, a Payee) calls one of the queue_* functions here.
That creates a PaymentLine with status 'Queued'. From then on it is handled on the Bank Payments page (/payments):
batch it by pay date, download the bank file, and finally load the bank's confirmation file.

Line status:   Queued -> Batched -> Downloaded -> Confirmed        (or Withdrawn: taken back out of the queue)
  Downloaded is NOT complete - a payment only counts as paid once its confirmation is loaded (Confirmed).

The source row keeps a matching status so the page it came from can show it:
  - ExpenseEntry.status: Pending / Queued / In batch / Downloaded / Paid (mirrored by sync_source()).
  - Bill: when a BILL line becomes Confirmed, sync_source() creates a BillPayment (mode = bank mode, reference = UTR,
    date = bank debit date) and recomputes the bill's amount_paid/status. A confirmed line can never be undone, so
    nothing needs reversing.

Ported from CRM2's app/payments_service.py (single company, so no entity handling).
"""
from datetime import datetime
from decimal import Decimal

from app.audit import log_action
from app.models import PaymentLine, ExpenseEntry, Bill, BillPayment
from app.payments_export import mode_for

ACTIVE = ("Queued", "Batched", "Downloaded", "Confirmed")  # a source can have at most one of these in progress
STATUS_TEXT = {
    "Queued": "Sent for payment", "Batched": "In a bank batch", "Downloaded": "Downloaded - awaiting bank confirmation",
    "Confirmed": "Paid (bank confirmed)", "Withdrawn": "Withdrawn",
}
BADGE = {"Queued": "bg-info text-dark", "Batched": "bg-primary", "Downloaded": "bg-warning text-dark",
         "Confirmed": "bg-success", "Withdrawn": "bg-secondary"}
SOURCE_STATUS = {"Queued": "Queued", "Batched": "In batch", "Downloaded": "Downloaded", "Confirmed": "Paid"}
TOLERANCE = Decimal("0.005")


def audit(db, user, entity_type, entity_id, label, action, summary=None):
    log_action(db, user, entity_type, entity_id, str(label), action, summary=summary)


def mask(acct):
    s = str(acct or "")
    return ("*" * max(len(s) - 4, 0) + s[-4:]) if s else ""


def active_line(db, source_type, source_id):
    return (db.query(PaymentLine)
            .filter(PaymentLine.source_type == source_type, PaymentLine.source_id == source_id,
                    PaymentLine.status.in_(ACTIVE)).first())


def sync_source(db, line, user=None):
    """Mirror the line's status on the row it came from. Call after every status change of a line."""
    if line.source_type == "EXPENSE" and line.source_id:
        ent = db.get(ExpenseEntry, line.source_id)
        if ent is not None:
            ent.status = SOURCE_STATUS.get(line.status, "Pending")  # Withdrawn -> back to not sent
        return
    if line.source_type == "BILL" and line.source_id and line.status == "Confirmed":
        _record_bill_payment(db, line, user)


def _record_bill_payment(db, line, user):
    """A bank-confirmed BILL line becomes a BillPayment (once)."""
    from app.routers.bills import recompute_bill_status  # lazy: bills.py imports this module

    bill = db.get(Bill, line.source_id)
    if bill is None:
        return
    if db.query(BillPayment).filter(BillPayment.payment_line_id == line.id).first():
        return
    paid_on = line.confirmed_on or line.pay_date
    bill.payments.append(BillPayment(
        payment_date=datetime(paid_on.year, paid_on.month, paid_on.day) if paid_on else datetime.utcnow(),
        amount=line.amount, mode=line.mode, reference_no=line.bank_ref, payment_line_id=line.id,
        notes=f"Bank payment {line.batch.batch_no if line.batch_id and line.batch else ''}".strip(),
        created_by=user.id if user else None,
    ))
    db.flush()
    recompute_bill_status(bill)
    audit(db, user, "bill", bill.id, bill.bill_ref, "payment",
          f"Bank payment of {line.amount} confirmed (UTR {line.bank_ref}) - status now {bill.status}")


def _new_line(db, user, *, source_type, source_id=None, payee=None, vendor=None, name, acct, ifsc, bank, amount, remark, pay_date):
    ln = PaymentLine(
        source_type=source_type, source_id=source_id, payee_id=payee.id if payee else None,
        vendor_id=vendor.id if vendor else None, beneficiary_name=name.strip(), account_no=acct, ifsc=ifsc.upper(),
        bank_name=bank, amount=amount, mode=mode_for(ifsc, amount), remark=(remark or "")[:100] or None,
        pay_date=pay_date, requested_pay_date=pay_date, status="Queued",
        requested_by=user.id if user else None, requested_at=datetime.now())
    db.add(ln)
    db.flush()
    audit(db, user, "payment_line", ln.id, name, "queue",
          f"Sent for payment: {source_type} {amount} on {pay_date:%d-%m-%Y}, a/c {mask(acct)}")
    return ln


def bill_summary(db, bill):
    """Payment position of one bill. `in_progress` = bank payments sent but not yet bank-confirmed;
    `balance` = what can still be sent (total - already paid - in progress)."""
    lines = (db.query(PaymentLine).filter(PaymentLine.source_type == "BILL", PaymentLine.source_id == bill.id,
                                          PaymentLine.status != "Withdrawn").order_by(PaymentLine.id).all())
    in_progress = sum((Decimal(ln.amount) for ln in lines if ln.status != "Confirmed"), Decimal(0))
    balance = Decimal(str(bill.total_amount or 0)) - Decimal(str(bill.amount_paid or 0)) - in_progress
    return dict(lines=lines, in_progress=in_progress, balance=balance)


def queue_bill(db, user, bill, amount, pay_date):
    """Send (part of) one Bill to the bank queue; pays the vendor's saved bank account. Raises ValueError with a user
    message. Only one bank payment of a bill may be in progress at a time and, together with what is already paid,
    payments may not exceed the bill total."""
    vendor = bill.vendor
    acct, ifsc = (vendor.bank_account_no or "").strip(), (vendor.bank_ifsc or "").strip()
    if bill.status == "cancelled":
        raise ValueError("this bill is cancelled")
    if not (acct and ifsc):
        raise ValueError(f"{vendor.name} has no bank account / IFSC - an admin must add them on the vendor page")
    amount = Decimal(amount)
    if amount <= 0:
        raise ValueError("amount must be more than 0")
    pos = bill_summary(db, bill)
    if pos["in_progress"] > 0:
        raise ValueError("a bank payment of this bill is already waiting for the bank - finish or withdraw it first")
    if amount > pos["balance"] + TOLERANCE:
        raise ValueError(f"amount {amount:,.2f} is more than the unpaid balance {pos['balance']:,.2f}")
    return _new_line(db, user, source_type="BILL", source_id=bill.id, vendor=vendor, name=vendor.name, acct=acct,
                     ifsc=ifsc, bank=vendor.bank_name, amount=amount, remark=f"Bill {bill.bill_ref}", pay_date=pay_date)


def queue_payee(db, user, payee, amount, pay_date, remark):
    """Send a payment to a payee (owner, landlord ...). Raises ValueError with a user message."""
    if payee.is_deleted or not payee.is_active:
        raise ValueError(f"{payee.name} is inactive or deleted")
    if not ((payee.account_no or "").strip() and (payee.ifsc or "").strip()):
        raise ValueError(f"{payee.name} has no bank account / IFSC - an admin must add them first")
    amount = Decimal(amount)
    if amount <= 0:
        raise ValueError("amount must be more than 0")
    return _new_line(db, user, source_type="PAYEE", payee=payee, name=payee.name, acct=payee.account_no.strip(),
                     ifsc=payee.ifsc.strip(), bank=payee.bank_name, amount=amount,
                     remark=remark or payee.default_remark, pay_date=pay_date)


def queue_expense(db, user, entry, pay_date):
    """Send one Expense entry to the bank queue (pays the payee's saved bank account). Raises ValueError."""
    payee = entry.payee
    if entry.is_cancelled or entry.status != "Pending" or active_line(db, "EXPENSE", entry.id):
        raise ValueError("already sent for payment, or cancelled")
    if payee.is_deleted or not payee.is_active:
        raise ValueError(f"{payee.name} is inactive or deleted")
    acct, ifsc = (payee.account_no or "").strip(), (payee.ifsc or "").strip()
    if not (acct and ifsc):
        raise ValueError(f"{payee.name} has no bank account / IFSC - an admin must add them on the payee first")
    amount = Decimal(entry.amount)
    if amount <= 0:
        raise ValueError("amount must be more than 0")
    label = (entry.description or entry.category)[:60]
    remark = f"{label} {datetime(entry.period_year, entry.period_month, 1):%b}{str(entry.period_year)[2:]}"
    ln = _new_line(db, user, source_type="EXPENSE", source_id=entry.id, payee=payee, name=payee.name, acct=acct,
                   ifsc=ifsc, bank=payee.bank_name, amount=amount, remark=remark, pay_date=pay_date)
    sync_source(db, ln, user)
    return ln


def lines_for_payee(db, payee_id):
    return (db.query(PaymentLine).filter(PaymentLine.payee_id == payee_id, PaymentLine.status != "Withdrawn")
            .order_by(PaymentLine.pay_date.desc(), PaymentLine.id.desc()).all())


def lines_for_bill(db, bill_id):
    return (db.query(PaymentLine).filter(PaymentLine.source_type == "BILL", PaymentLine.source_id == bill_id,
                                         PaymentLine.status != "Withdrawn").order_by(PaymentLine.id).all())
