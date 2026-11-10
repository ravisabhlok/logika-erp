"""Bank Payments: turn sent-for-payment items into a bank upload file, then settle them from the bank's report.

Flow (see app/payments_service.py):
  other pages (a Bill, an Expense, a Payee) SEND a payment  ->  it waits here as 'Queued'
  -> a BATCH is made for a pay date from the waiting payments  ->  the bank upload file is downloaded
  -> pay at the bank  ->  load the bank's confirmation report (ICICI BizPay360 Reverse MIS).
A payment is only complete when its bank confirmation is loaded - 'Downloaded' is not 'paid'.

Permissions use the "payments" module: view = see the pages; edit = process (batch, download, confirm, send);
delete = delete payees. Bank details (payee/vendor account + IFSC, one-time edits, the company debit account) and
"mark as failed" are admin-only regardless, same as CRM2.

Ported from CRM2's payments blueprint.
"""
import os
import re
import secrets
import tempfile
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, UploadFile, File
from fastapi.responses import RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.auth import require_module_permission, get_user_module_permissions
from app.database import get_db
from app.formatting import format_inr, format_dt
from app.models import Company, Payee, PaymentBatch, PaymentConfirmation, PaymentLine, User
from app.payments_confirm import apply_plan, build_plan, parse_report
from app.payments_export import build_bank_file, mode_for
from app.payments_service import BADGE, STATUS_TEXT, audit, lines_for_payee, mask, queue_payee, sync_source

router = APIRouter(prefix="/payments", tags=["payments"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt

PAYEE_CATEGORIES = ("Rent", "Owner", "Utility", "Expense", "Other")
IFSC_RE = re.compile(r"[A-Z]{4}0[A-Z0-9]{6}")
CONFIRM_DIR = os.path.join(tempfile.gettempdir(), "erp_bank_confirm")


# ---------------------------------------------------------------- helpers

def go(url, msg=None, kind="success"):
    if msg:
        url += ("&" if "?" in url else "?") + f"{kind}={quote(msg)}"
    return RedirectResponse(url=url, status_code=303)


def is_admin(user):
    return user.role == "admin"


def parse_date(text):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def parse_amount(text):
    try:
        v = Decimal((text or "").strip().replace(",", ""))
    except InvalidOperation:
        raise ValueError("Amount must be a number")
    if v <= 0:
        raise ValueError("Amount must be more than 0")
    return v.quantize(Decimal("0.01"))


def total_of(lines):
    return sum((Decimal(ln.amount) for ln in lines), Decimal(0))


def get_company(db):
    return db.query(Company).first()


def file_name_for(company, batch):
    """Bank file name for a batch. ALWAYS the same for the same batch (no date/time): the bank refuses a file name it
    already has, which protects against uploading a batch twice. Fixed on the batch at its first download."""
    if batch.file_name:
        return batch.file_name
    return (re.sub(r"[^A-Za-z0-9]+", "", (company.name if company else "") or "Bank") + "_"
            + re.sub(r"[^A-Za-z0-9-]+", "", batch.batch_no) + ".xlsx")


def next_batch_no(db, pay_date):
    stem = f"PAY-{pay_date:%Y%m%d}"
    n = db.query(PaymentBatch).filter(PaymentBatch.batch_no.like(stem + "-%")).count() + 1
    while db.query(PaymentBatch).filter_by(batch_no=f"{stem}-{n:02d}").first():
        n += 1
    return f"{stem}-{n:02d}"


def line_problems(lines, company):
    out = []
    if not lines:
        out.append("There are no payments in this batch.")
    if not (company and (company.bank_debit_acc or "").strip()):
        out.append("The company has no bank debit account - an admin adds it at the top of the Bank Payments page.")
    for ln in lines:
        if not (ln.account_no or "").strip():
            out.append(f"{ln.beneficiary_name}: no bank account number.")
        if not (ln.ifsc or "").strip():
            out.append(f"{ln.beneficiary_name}: no IFSC code.")
        if Decimal(ln.amount) <= 0:
            out.append(f"{ln.beneficiary_name}: amount must be more than 0.")
    return out


def render(request, name, user, **ctx):
    return templates.TemplateResponse(name, {"request": request, "user": user, "mask": mask, "badge": BADGE,
                                             "status_text": STATUS_TEXT, "mode_for": mode_for, **ctx})


def batch_or_none(db, batch_id):
    return db.get(PaymentBatch, batch_id)


def finish_batch_status(batch):
    live = [x for x in batch.lines if x.status != "Withdrawn"]
    if not live:
        batch.status, batch.cancel_reason = "Cancelled", "every payment failed"
    elif all(x.status == "Confirmed" for x in live):
        batch.status = "Confirmed"


# ---------------------------------------------------------------- the Bank Payments page

@router.get("")
def index(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view"))):
    perms = get_user_module_permissions(user, db, "payments")
    waiting = (db.query(PaymentLine).filter_by(status="Queued").order_by(PaymentLine.pay_date, PaymentLine.id).all())
    sender_ids = {ln.requested_by for ln in waiting if ln.requested_by}
    names = {u.id: u.username for u in db.query(User).filter(User.id.in_(sender_ids)).all()} if sender_ids else {}

    # Batches in play (Open / Downloaded-but-not-fully-confirmed) are listed by default. Finished ones (Confirmed,
    # Cancelled) are history: shown only when a From and To date (on the batch's pay date) are chosen.
    d_from, d_to = parse_date(request.query_params.get("from")), parse_date(request.query_params.get("to"))
    history = bool(request.query_params.get("from") or request.query_params.get("to"))
    history_error = None
    q = db.query(PaymentBatch)
    if history and (d_from is None or d_to is None):
        history_error = "Choose both a From date and a To date."
    elif history and d_from > d_to:
        history_error = "The From date is after the To date."
    if history and history_error is None:
        q = q.filter(PaymentBatch.pay_date >= d_from, PaymentBatch.pay_date <= d_to)
    else:
        q = q.filter(PaymentBatch.status.in_(("Open", "Downloaded")))
    rows = []
    for b in q.order_by(PaymentBatch.pay_date.desc(), PaymentBatch.id.desc()).all():
        live = [ln for ln in b.lines if ln.status != "Withdrawn"]
        rows.append({"b": b, "n": len(live), "total": total_of(live),
                     "confirmed": sum(1 for ln in live if ln.status == "Confirmed")})
    return render(request, "payments/index.html", user, perms=perms, waiting=waiting,
                  rows=rows, names=names, company=get_company(db), today=date.today(), is_admin=is_admin(user),
                  showing_history=history and history_error is None, history_error=history_error, d_from=d_from, d_to=d_to,
                  success=request.query_params.get("success"), error=request.query_params.get("error"))


@router.post("/settings")
async def save_settings(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    if not is_admin(user):
        return go("/payments", "Only an admin can change the bank debit account.", "error")
    form = await request.form()
    acct = re.sub(r"[^0-9A-Za-z]", "", form.get("bank_debit_acc") or "")
    company = get_company(db)
    if company is None:
        return go("/payments", "No company record found.", "error")
    old = company.bank_debit_acc
    company.bank_debit_acc = acct or None
    audit(db, user, "company", company.id, company.name, "update", f"Bank debit account changed from {mask(old)} to {mask(acct)}")
    db.commit()
    return go("/payments", "Bank debit account saved.")


@router.post("/make_batch")
async def make_batch(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    form = await request.form()
    pay_date = parse_date(form.get("pay_date"))
    ids = [int(x) for x in form.getlist("line_id") if str(x).isdigit()]
    if pay_date is None or not ids:
        return go("/payments", "Tick the payments to put in the batch.", "error")
    # Waiting payments are not grouped by date: tick any of them and the batch gets the pay date chosen here.
    lines = db.query(PaymentLine).filter(PaymentLine.id.in_(ids), PaymentLine.status == "Queued").all()
    if len(lines) != len(set(ids)):
        return go("/payments", "Some of those payments are no longer waiting - please try again.", "error")
    batch = PaymentBatch(pay_date=pay_date, status="Open", title=(form.get("title") or "").strip()[:100] or None,
                         created_by=user.id, batch_no=next_batch_no(db, pay_date))
    db.add(batch)
    db.flush()
    for ln in lines:
        ln.pay_date = pay_date
        ln.batch_id, ln.status = batch.id, "Batched"
        sync_source(db, ln, user)
    audit(db, user, "payment_batch", batch.id, batch.batch_no, "create",
          f"{len(lines)} payment(s), total {total_of(lines)}, pay date {pay_date:%d-%m-%Y}")
    db.commit()
    return go(f"/payments/batch/{batch.id}", f"Batch {batch.batch_no} made with {len(lines)} payment(s) for {pay_date:%d-%m-%Y}.")


@router.post("/change_dates")
async def change_dates(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    """The pay date of waiting payments may be changed (the sender's date is only the request). A payment moves to the
    group of its new date, so batches can still be made per date."""
    form = await request.form()
    n = 0
    for key in form:
        if not key.startswith("date_") or not key[5:].isdigit():
            continue
        new = parse_date(form.get(key))
        ln = db.query(PaymentLine).filter_by(id=int(key[5:]), status="Queued").first()
        if ln is None or new is None or new == ln.pay_date:
            continue
        old = ln.pay_date
        ln.pay_date = new
        audit(db, user, "payment_line", ln.id, ln.beneficiary_name, "update", f"Pay date {old} -> {new}")
        n += 1
    db.commit()
    return go("/payments", f"Pay date changed for {n} payment(s)." if n else "No pay date was changed.",
              "success" if n else "error")


@router.post("/pay_part/{line_id}")
async def pay_part(line_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    """Pay only part of a waiting payment: this payment is cut down to the amount to pay now and the rest stays waiting
    as a new payment to the same beneficiary. Tick the part-payment and make the batch as usual; the rest can go in a
    later batch (or be deleted, which leaves it unpaid on the bill / order it came from)."""
    form = await request.form()
    ln = db.get(PaymentLine, line_id)
    if ln is None or ln.status != "Queued":
        return go("/payments", "That payment is no longer waiting for a batch.", "error")
    if ln.source_type == "EXPENSE":
        return go("/payments", "An expense entry has a fixed amount and cannot be part-paid.", "error")
    try:
        part = parse_amount(form.get(f"part_{line_id}"))
    except ValueError as ex:
        return go("/payments", f"{ln.beneficiary_name}: {ex}. Type the amount to pay now in the Pay part box.", "error")
    full = Decimal(ln.amount)
    if part >= full:
        return go("/payments", f"{ln.beneficiary_name}: pay-now amount must be less than {full:,.2f}. "
                  "To pay all of it just tick it and make the batch.", "error")
    rest = full - part
    rest_line = PaymentLine(
        source_type=ln.source_type, source_id=ln.source_id, payee_id=ln.payee_id, vendor_id=ln.vendor_id,
        beneficiary_name=ln.beneficiary_name, account_no=ln.account_no, ifsc=ln.ifsc, bank_name=ln.bank_name,
        amount=rest, mode=mode_for(ln.ifsc, rest), remark=ln.remark, pay_date=ln.pay_date,
        requested_pay_date=ln.requested_pay_date, status="Queued", requested_by=ln.requested_by,
        requested_at=ln.requested_at, bank_note=ln.bank_note)
    ln.amount, ln.mode = part, mode_for(ln.ifsc, part)
    db.add(rest_line)
    db.flush()
    audit(db, user, "payment_line", ln.id, ln.beneficiary_name, "split",
          f"Part payment: {full} cut to {part} to pay now; remaining {rest} stays waiting (payment #{rest_line.id})")
    db.commit()
    return go("/payments", f"{ln.beneficiary_name}: {part:,.2f} is ready to pay now (tick it and make the batch); "
              f"the remaining {rest:,.2f} is still waiting.")


@router.post("/withdraw")
async def withdraw(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    """Take waiting payments out of the queue (they go back to 'not sent' on the page they came from)."""
    form = await request.form()
    reason = (form.get("reason") or "").strip() or "deleted from the bank queue"
    ids = [int(x) for x in form.getlist("line_id") if str(x).isdigit()]
    if not ids:
        return go("/payments", "Tick the payments to delete.", "error")
    n = 0
    for ln in db.query(PaymentLine).filter(PaymentLine.id.in_(ids), PaymentLine.status == "Queued").all():
        ln.status, ln.withdraw_reason = "Withdrawn", reason[:200]
        sync_source(db, ln, user)
        audit(db, user, "payment_line", ln.id, ln.beneficiary_name, "withdraw", reason)
        n += 1
    db.commit()
    return go("/payments", f"{n} payment(s) deleted from the queue. They show as not sent again on the page they came from.")


@router.get("/edit_bank/{line_id}")
def edit_bank_form(line_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    ln = db.get(PaymentLine, line_id)
    if ln is None or not is_admin(user):
        return go("/payments", "Only an admin can change bank details." if ln else "Payment not found", "error")
    if ln.status != "Queued":
        return go("/payments", "Bank details can be changed only while the payment is waiting (not yet in a batch).", "error")
    return render(request, "payments/edit_bank.html", user, ln=ln, error=request.query_params.get("error"))


@router.post("/edit_bank/{line_id}")
async def edit_bank(line_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    """One-time bank for ONE waiting payment. Only this payment line changes - the vendor's / payee's own bank details are
    untouched. Only while Queued (once in a batch / downloaded the bank file already has the old details). Audited and
    noted on the line."""
    ln = db.get(PaymentLine, line_id)
    if ln is None:
        return go("/payments", "Payment not found", "error")
    if not is_admin(user):
        return go("/payments", "Only an admin can change bank details.", "error")
    if ln.status != "Queued":
        return go("/payments", "Bank details can be changed only while the payment is waiting (not yet in a batch).", "error")
    form = await request.form()
    name = (form.get("beneficiary_name") or "").strip()
    account = re.sub(r"[^0-9A-Za-z]", "", form.get("account_no") or "")
    ifsc = re.sub(r"[^0-9A-Za-z]", "", form.get("ifsc") or "").upper()
    bank = (form.get("bank_name") or "").strip()[:60]
    reason = (form.get("reason") or "").strip()[:120]
    back = f"/payments/edit_bank/{line_id}"
    if not name:
        return go(back, "Name is required", "error")
    if not re.fullmatch(r"[0-9A-Za-z]{5,30}", account):
        return go(back, "Enter the account number (5 to 30 letters/digits)", "error")
    if not IFSC_RE.fullmatch(ifsc):
        return go(back, "IFSC must be 11 characters, like SBIN0001234", "error")
    if not reason:
        return go(back, "Give a reason for the change", "error")
    if account == (ln.account_no or "") and ifsc == (ln.ifsc or "") and name == ln.beneficiary_name:
        return go(back, "Nothing is different from what is already on this payment", "error")
    note = (f"Bank changed {datetime.now():%d-%m-%Y %H:%M} by {user.username}: was {mask(ln.account_no)} {ln.ifsc or ''}. {reason}")
    if ln.bank_note:  # edited more than once: keep the earliest account in the note
        note = ln.bank_note.split(" | ")[0] + " | " + note
    audit(db, user, "payment_line", ln.id, name, "edit_bank",
          f"was {ln.beneficiary_name} {mask(ln.account_no)} {ln.ifsc}; now {name} {mask(account)} {ifsc}. {reason}")
    ln.beneficiary_name, ln.account_no, ln.ifsc, ln.bank_name = name, account, ifsc, bank or None
    ln.mode = mode_for(ifsc, ln.amount)
    ln.bank_note = note[:255]
    db.commit()
    return go("/payments", f"Bank details changed for this payment only ({name}, a/c {mask(account)}). The saved vendor/payee details were not changed.")


# ---------------------------------------------------------------- one batch

@router.get("/batch/{batch_id}")
def batch_page(batch_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view"))):
    b = batch_or_none(db, batch_id)
    if b is None:
        return go("/payments", "Payment batch not found", "error")
    perms = get_user_module_permissions(user, db, "payments")
    company = get_company(db)
    lines = [ln for ln in b.lines if ln.status != "Withdrawn"]
    return render(request, "payments/batch.html", user, b=b, lines=lines, total=total_of(lines), company=company,
                  file_name=file_name_for(company, b), perms=perms, is_admin=is_admin(user),
                  problems=line_problems(lines, company) if b.status == "Open" else [],
                  confirmation=db.query(PaymentConfirmation).filter_by(batch_id=b.id).first(),
                  n_confirmed=sum(1 for ln in lines if ln.status == "Confirmed"),
                  success=request.query_params.get("success"), error=request.query_params.get("error"))


@router.post("/batch/{batch_id}/fail/{line_id}")
async def mark_failed(batch_id: int, line_id: int, request: Request, db: Session = Depends(get_db),
                      user: User = Depends(require_module_permission("payments", "edit"))):
    """Admin only: the bank did not pay one payment of a downloaded batch and no bank report is at hand. Same effect as a
    rejected row in the bank's report: the payment leaves the batch and waits again. Reason required; audited."""
    back = f"/payments/batch/{batch_id}"
    if not is_admin(user):
        return go(back, "Only an admin can mark a payment as failed.", "error")
    b = batch_or_none(db, batch_id)
    ln = db.query(PaymentLine).filter_by(id=line_id, batch_id=batch_id).first()
    if b is None or ln is None:
        return go("/payments", "Payment not found", "error")
    form = await request.form()
    reason = (form.get("reason") or "").strip()[:150]
    if ln.status != "Downloaded":
        return go(back, "Only a payment that is downloaded and not yet confirmed can be marked as failed.", "error")
    if len(reason) < 5:
        return go(back, "Give a reason (at least 5 letters).", "error")
    ln.batch_id, ln.status, ln.exported_at = None, "Queued", None
    sync_source(db, ln, user)
    audit(db, user, "payment_line", ln.id, ln.beneficiary_name, "mark_failed", f"batch {b.batch_no}: {reason}")
    db.flush()
    db.refresh(b)
    finish_batch_status(b)
    audit(db, user, "payment_batch", b.id, b.batch_no, "update", f"{ln.beneficiary_name} marked failed ({reason}); batch {b.status}")
    db.commit()
    return go("/payments", f"{ln.beneficiary_name}: marked as failed and moved back to the waiting list. Fix the bank details "
                           "with Edit bank if needed, then make a new batch.", "error")


@router.post("/batch/{batch_id}/remove/{line_id}")
def remove_line(batch_id: int, line_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    """Take a payment out of an Open batch, back to the waiting list."""
    back = f"/payments/batch/{batch_id}"
    b = batch_or_none(db, batch_id)
    ln = db.query(PaymentLine).filter_by(id=line_id, batch_id=batch_id).first()
    if b is None or ln is None:
        return go("/payments", "Payment not found", "error")
    if b.status != "Open" or ln.status != "Batched":
        return go(back, "Only a payment in a batch that has not been downloaded can be taken out.", "error")
    ln.batch_id, ln.status = None, "Queued"
    sync_source(db, ln, user)
    audit(db, user, "payment_line", ln.id, ln.beneficiary_name, "update", f"taken out of batch {b.batch_no}")
    db.commit()
    return go(back, f"{ln.beneficiary_name} is back in the waiting list.")


@router.post("/batch/{batch_id}/download")
async def download(batch_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    back = f"/payments/batch/{batch_id}"
    b = batch_or_none(db, batch_id)
    if b is None:
        return go("/payments", "Payment batch not found", "error")
    if b.status in ("Cancelled", "Confirmed"):
        return go(back, f"This batch is {b.status.lower()}.", "error")
    form = await request.form()
    company = get_company(db)
    # The ERP cannot delete the earlier copy on the user's PC, and the bank refuses a file name it already has. So a batch
    # that was already downloaded is NOT downloaded again until the user confirms they deleted the old file.
    if b.status == "Downloaded" and form.get("old_file_deleted") != "1":
        when = b.exported_at.strftime(" on %d-%m-%Y at %H:%M") if b.exported_at else ""
        return go(back, f'This batch was already downloaded{when}. Delete the old file "{file_name_for(company, b)}" from your '
                        'computer first, then tick "I have deleted the old file" and download again. (The file name is always '
                        'the same, and the bank does not accept the same name twice.)', "error")
    lines = [ln for ln in b.lines if ln.status in ("Batched", "Downloaded", "Confirmed")]
    problems = line_problems(lines, company)
    if problems:
        return go(back, " ".join(problems[:6]), "error")

    class _L:
        def __init__(self, ln):
            self.beneficiary_name, self.account_no, self.ifsc, self.amount, self.remark = (
                ln.beneficiary_name, ln.account_no, ln.ifsc, ln.amount, ln.remark)
            self.pay_date = b.pay_date

    data = build_bank_file(company.bank_debit_acc, [_L(ln) for ln in lines])
    now, first = datetime.now(), b.status == "Open"
    if not b.file_name:
        b.file_name = file_name_for(company, b)  # fixed from now on
    for ln in lines:
        ln.mode = mode_for(ln.ifsc, ln.amount)
        if ln.status == "Batched":
            ln.status, ln.exported_at = "Downloaded", now
            sync_source(db, ln, user)
    if first:
        b.status, b.exported_by, b.exported_at = "Downloaded", user.id, now
    audit(db, user, "payment_batch", b.id, b.batch_no, "export",
          f"{len(lines)} payment(s), total {total_of(lines)}, {'first download' if first else 're-download (old file deleted confirmed)'}")
    db.commit()
    return Response(content=data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="{b.file_name}"'})


@router.post("/batch/{batch_id}/cancel")
async def cancel_batch(batch_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    """Cancel a batch that is not confirmed: its payments go back to the waiting list."""
    back = f"/payments/batch/{batch_id}"
    b = batch_or_none(db, batch_id)
    if b is None:
        return go("/payments", "Payment batch not found", "error")
    form = await request.form()
    reason = (form.get("reason") or "").strip()
    if b.status in ("Cancelled", "Confirmed") or any(ln.status == "Confirmed" for ln in b.lines):
        return go(back, "A batch with confirmed payments cannot be cancelled.", "error")
    if b.status == "Downloaded" and len(reason) < 5:
        return go(back, "Give a reason (at least 5 characters) - the file has already been downloaded.", "error")
    n = 0
    for ln in b.lines:
        if ln.status in ("Batched", "Downloaded"):
            ln.batch_id, ln.status, ln.exported_at = None, "Queued", None
            sync_source(db, ln, user)
            n += 1
    b.status, b.cancel_reason = "Cancelled", (reason or "cancelled before download")[:200]
    audit(db, user, "payment_batch", b.id, b.batch_no, "cancel", f"{b.cancel_reason}; {n} payment(s) back in the queue")
    db.commit()
    return go("/payments", f"Batch {b.batch_no} cancelled. Its {n} payment(s) are back in the waiting list.")


# ---------------------------------------------------------------- bank confirmation (ICICI Reverse MIS report)

def plan_for(db, data):
    rows, err = parse_report(data)
    if err:
        return None, err
    company = get_company(db)
    return build_plan(db, rows, company.bank_debit_acc if company else None), None


@router.post("/confirm")
async def confirm_review(request: Request, report: UploadFile = File(None), db: Session = Depends(get_db),
                         user: User = Depends(require_module_permission("payments", "edit"))):
    """Upload the bank's report; show what it would settle. Nothing is written yet."""
    if report is None or not (report.filename or "").lower().endswith(".xlsx"):
        return go("/payments", "Choose the bank's report (.xlsx) from the e-mail.", "error")
    data = await report.read(5 * 1024 * 1024 + 1)
    if len(data) > 5 * 1024 * 1024:
        return go("/payments", "That file is too big for a bank report.", "error")
    plan, err = plan_for(db, data)
    if err:
        return go("/payments", err, "error")
    os.makedirs(CONFIRM_DIR, exist_ok=True)
    token = secrets.token_hex(16)
    with open(os.path.join(CONFIRM_DIR, token + ".xlsx"), "wb") as fh:
        fh.write(data)
    counts = {k: sum(1 for i in plan["items"] if i["outcome"] == k)
              for k in ("paid", "rejected", "pending", "already", "conflict", "unmatched")}
    return render(request, "payments/confirm_review.html", user, plan=plan, token=token,
                  file_name=os.path.basename(report.filename), counts=counts)


@router.post("/confirm/apply")
async def confirm_apply(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    """Apply the reviewed report. The file is read and matched again, so what is written is what is true now."""
    form = await request.form()
    token = form.get("token", "")
    path = os.path.join(CONFIRM_DIR, str(token) + ".xlsx")
    if not re.fullmatch(r"[0-9a-f]{32}", str(token)) or not os.path.exists(path):
        return go("/payments", "The uploaded report is no longer available - please upload it again.", "error")
    with open(path, "rb") as fh:
        data = fh.read()
    plan, err = plan_for(db, data)
    if err or plan["errors"]:
        return go("/payments", err or plan["errors"][0], "error")
    counts = apply_plan(db, user, plan, (form.get("file_name") or "bank report.xlsx")[:200], data)
    db.commit()
    os.remove(path)
    return go("/payments", f"Bank report loaded: {counts['paid']} payment(s) confirmed, {counts['rejected']} rejected "
                           f"(back in the waiting list), {counts['skipped']} row(s) left unchanged.",
              "success" if counts["paid"] else "error")


# ---------------------------------------------------------------- payees (owners, landlords, utilities ...)

def read_payee_form(form):
    name = (form.get("name") or "").strip()
    if not name:
        raise ValueError("Name is required")
    category = form.get("category") if form.get("category") in PAYEE_CATEGORIES else "Other"
    account = re.sub(r"[^0-9A-Za-z]", "", form.get("account_no") or "")
    ifsc = re.sub(r"[^0-9A-Za-z]", "", form.get("ifsc") or "").upper()
    if ifsc and not IFSC_RE.fullmatch(ifsc):
        raise ValueError("IFSC must be 11 characters, like SBIN0001234")
    return dict(name=name, nickname=(form.get("nickname") or "").strip()[:60] or None, category=category,
                bank_name=(form.get("bank_name") or "").strip()[:60] or None, account_no=account or None, ifsc=ifsc or None,
                mobile=(form.get("mobile") or "").strip()[:15] or None, email=(form.get("email") or "").strip()[:100] or None,
                default_remark=(form.get("default_remark") or "").strip()[:100] or None,
                is_active=form.get("is_active") == "on")


@router.get("/payees")
def payees(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view"))):
    perms = get_user_module_permissions(user, db, "payments")
    rows = db.query(Payee).filter_by(is_deleted=False).order_by(Payee.name).all()
    return render(request, "payments/payees.html", user, payees=rows, perms=perms,
                  success=request.query_params.get("success"), error=request.query_params.get("error"))


@router.get("/payees/add")
def payee_add_form(request: Request, user: User = Depends(require_module_permission("payments", "edit")), db: Session = Depends(get_db)):
    return render(request, "payments/payee_form.html", user, payee=None, categories=PAYEE_CATEGORIES, is_admin=is_admin(user),
                  error=request.query_params.get("error"))


@router.post("/payees/add")
async def payee_add(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    form = await request.form()
    try:
        data = read_payee_form(form)
    except ValueError as ex:
        return go("/payments/payees/add", str(ex), "error")
    note = ""
    if not is_admin(user):  # bank details: admin only
        data.update(bank_name=None, account_no=None, ifsc=None)
        note = " Bank details can be added only by an admin - the payee was saved without them."
    p = Payee(**data)
    db.add(p)
    db.flush()
    audit(db, user, "payee", p.id, p.name, "create", f"{p.category}, a/c {mask(p.account_no)}")
    db.commit()
    return go("/payments/payees", f"Payee {p.name} added.{note}")


@router.get("/payees/{payee_id}")
def payee_view(payee_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view"))):
    p = db.get(Payee, payee_id)
    if p is None:
        return go("/payments/payees", "Payee not found", "error")
    perms = get_user_module_permissions(user, db, "payments")
    return render(request, "payments/payee_view.html", user, p=p, lines=lines_for_payee(db, p.id), perms=perms,
                  today=date.today(), success=request.query_params.get("success"), error=request.query_params.get("error"))


@router.post("/payees/{payee_id}/send")
async def payee_send(payee_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    p = db.get(Payee, payee_id)
    back = f"/payments/payees/{payee_id}"
    if p is None or p.is_deleted:
        return go("/payments/payees", "Payee not found", "error")
    form = await request.form()
    try:
        pay_date = parse_date(form.get("pay_date"))
        if pay_date is None:
            raise ValueError("Choose the pay date")
        amount = parse_amount(form.get("amount"))
        queue_payee(db, user, p, amount, pay_date, (form.get("remark") or "").strip()[:100] or None)
    except ValueError as ex:
        return go(back, str(ex), "error")
    db.commit()
    return go(back, f"Payment of {amount} to {p.name} sent for payment ({pay_date:%d-%m-%Y}). It now waits on the Bank Payments page.")


@router.get("/payees/{payee_id}/edit")
def payee_edit_form(payee_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    p = db.get(Payee, payee_id)
    if p is None:
        return go("/payments/payees", "Payee not found", "error")
    return render(request, "payments/payee_form.html", user, payee=p, categories=PAYEE_CATEGORIES, is_admin=is_admin(user),
                  error=request.query_params.get("error"))


@router.post("/payees/{payee_id}/edit")
async def payee_edit(payee_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    p = db.get(Payee, payee_id)
    if p is None:
        return go("/payments/payees", "Payee not found", "error")
    form = await request.form()
    try:
        data = read_payee_form(form)
    except ValueError as ex:
        return go(f"/payments/payees/{payee_id}/edit", str(ex), "error")
    note = ""
    if not is_admin(user):  # bank details: admin only - keep what is saved
        if (data["bank_name"], data["account_no"], data["ifsc"]) != (p.bank_name, p.account_no, p.ifsc):
            note = " Bank details can be changed only by an admin - they were left as they were."
        data.update(bank_name=p.bank_name, account_no=p.account_no, ifsc=p.ifsc)
    audit(db, user, "payee", p.id, p.name, "update",
          f"{data['name']} / {data['category']} / a/c {mask(p.account_no)} -> {mask(data['account_no'])} / active={data['is_active']}")
    for k, v in data.items():
        setattr(p, k, v)
    db.commit()
    return go("/payments/payees", f"Payee {p.name} updated.{note}")


@router.post("/payees/{payee_id}/delete")
def payee_delete(payee_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "delete"))):
    from app.models import ExpenseEntry, ExpenseTemplate
    p = db.get(Payee, payee_id)
    if p is None:
        return go("/payments/payees", "Payee not found", "error")
    blockers = []
    if db.query(PaymentLine).filter(PaymentLine.payee_id == p.id, PaymentLine.status.in_(("Queued", "Batched", "Downloaded"))).count():
        blockers.append("payments not yet bank-confirmed")
    if db.query(ExpenseEntry).filter(ExpenseEntry.payee_id == p.id, ExpenseEntry.is_cancelled == False, ExpenseEntry.status != "Paid").count():  # noqa: E712
        blockers.append("unpaid expenses")
    if db.query(ExpenseTemplate).filter(ExpenseTemplate.payee_id == p.id, ExpenseTemplate.is_active == True).count():  # noqa: E712
        blockers.append("active recurring expenses")
    if blockers:
        return go("/payments/payees", f'"{p.name}" cannot be deleted: it has ' + ", ".join(blockers) + ". Finish or remove those first.", "error")
    audit(db, user, "payee", p.id, p.name, "delete", f"a/c {mask(p.account_no)}")
    p.is_deleted = True  # soft delete: payments, expenses and ledgers keep their history
    db.commit()
    return go("/payments/payees", f"Payee {p.name} deleted. Its past payments and expenses are kept.")
