"""Bank confirmation: read ICICI's BizPay360 "Reverse MIS" report and settle the payments it reports on.

The bank e-mails this .xlsx after a bulk payment (columns: Beneficiary Account No, Amount, STATUS, Current Step, UTR NO,
Acct_Debit_date, Rejection Reason, ...). One row = one payment. There is no ERP reference in it, so a row is matched to
a payment we sent by beneficiary account number + amount, among the payments that are 'Downloaded'.

  parse_report(data)               -> rows read from the file (nothing touches the database)
  build_plan(db, rows, debit_acc)  -> what would happen to each row (the review screen shows this)
  apply_plan(db, user, plan, ...)  -> writes it: Success -> Confirmed (UTR as bank_ref), Rejected -> back to Queued

Ported from CRM2's app/payments_confirm.py.
"""
import io
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from app.models import PaymentBatch, PaymentLine, PaymentConfirmation
from app.payments_service import audit, mask, sync_source

REQUIRED = ("beneficiary account no", "amount", "status")
REJECTED_WORDS = ("reject", "fail", "return", "cancel", "declin")


def _text(v):
    if v is None:
        return ""
    if isinstance(v, float) and v == int(v):
        v = int(v)
    return str(v).strip()


def norm_account(v):
    return re.sub(r"[^0-9A-Za-z]", "", _text(v)).upper().lstrip("0")


def _amount(v):
    try:
        return Decimal(_text(v).replace(",", "")).quantize(Decimal("0.01"))
    except InvalidOperation:
        return None


def _date(v):
    if isinstance(v, datetime):
        return v.date()
    if isinstance(v, date):
        return v
    for fmt in ("%d-%m-%Y", "%d/%m/%Y", "%Y-%m-%d", "%d-%b-%Y"):
        try:
            return datetime.strptime(_text(v), fmt).date()
        except ValueError:
            pass
    return None


def parse_report(data):
    """Returns (rows, error). Each row is a dict; error is a message when the file is not a usable report."""
    import warnings
    import openpyxl
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("ignore")
            wb = openpyxl.load_workbook(io.BytesIO(data), data_only=True)
        ws = wb.worksheets[0]
        grid = list(ws.iter_rows(values_only=True))
    except Exception:
        return [], "This is not a readable Excel (.xlsx) file."
    head_at = next((i for i, r in enumerate(grid[:10]) if r and all(
        any(_text(c).lower() == k for c in r) for k in REQUIRED)), None)
    if head_at is None:
        return [], "This does not look like the bank's report (no 'Beneficiary Account No', 'Amount' and 'STATUS' columns)."
    col = {_text(c).lower(): i for i, c in enumerate(grid[head_at]) if _text(c)}

    def get(r, name):
        i = col.get(name)
        return r[i] if i is not None and i < len(r) else None

    rows = []
    for n, r in enumerate(grid[head_at + 1:], start=head_at + 2):
        if not r or not _text(get(r, "beneficiary account no")):
            continue
        status, step, reason = _text(get(r, "status")), _text(get(r, "current step")), _text(get(r, "rejection reason"))
        low = (status + " " + step).lower()
        if status.lower() == "success":
            kind = "paid"
        elif reason or any(w in low for w in REJECTED_WORDS):
            kind = "rejected"
        else:
            kind = "pending"
        rows.append({
            "row": n, "name": _text(get(r, "beneficiary name")), "account": _text(get(r, "beneficiary account no")),
            "amount": _amount(get(r, "amount")), "status": status, "step": step, "reason": reason, "kind": kind,
            "utr": _text(get(r, "utr no")) or _text(get(r, "customer ref no")),
            "on": _date(get(r, "acct_debit_date")) or _date(get(r, "pymt_date")),
            "debit_acc": _text(get(r, "debit_acct_no")), "file_name": _text(get(r, "file name")),
        })
    if not rows:
        return [], "The report has no payment rows."
    return rows, None


def build_plan(db, rows, debit_acc):
    """Match each report row to one of the Downloaded payments. Returns {'items': [...], 'errors': [...]}.
    item = row + 'line' (PaymentLine or None) + 'outcome': paid / rejected / pending / already / conflict / unmatched."""
    errors = []
    files = {r["debit_acc"] for r in rows if r["debit_acc"]}
    if debit_acc and files and any(norm_account(f) != norm_account(debit_acc) for f in files):
        errors.append("This report is for a different debit account than the company's "
                      f"(***{debit_acc[-4:]}).")
    lines = (db.query(PaymentLine).filter(PaymentLine.status.in_(("Downloaded", "Confirmed")))
             .order_by(PaymentLine.exported_at, PaymentLine.id).all())
    used, items = set(), []
    for r in rows:
        r = dict(r, line=None, outcome=r["kind"], note="")
        if r["amount"] is None:
            r["outcome"], r["note"] = "unmatched", "amount is not a number"
            items.append(r)
            continue
        cands = [ln for ln in lines if ln.id not in used and norm_account(ln.account_no) == norm_account(r["account"])
                 and Decimal(ln.amount) == r["amount"]]
        want = re.sub(r"\.xlsx$", "", r["file_name"], flags=re.I)
        cands.sort(key=lambda ln: (ln.status != "Downloaded", not (ln.batch and ln.batch.file_name and want
                                   and ln.batch.file_name.startswith(want))))
        if not cands:
            r["outcome"], r["note"] = "unmatched", "no downloaded payment with this account and amount"
        else:
            ln = cands[0]
            used.add(ln.id)
            r["line"] = ln
            if ln.status == "Confirmed":
                same = r["kind"] == "paid" and ln.bank_ref == r["utr"]
                r["outcome"] = "already" if same else "conflict"
                r["note"] = "already confirmed" + ("" if same else f" with another reference ({ln.bank_ref})")
            elif r["kind"] == "paid" and not r["utr"]:
                r["outcome"], r["note"] = "pending", "paid but the report has no UTR - not changed"
            elif r["kind"] == "pending":
                r["note"] = f"status '{r['status']}' / '{r['step']}' is not final - not changed"
        items.append(r)
    return {"items": items, "errors": errors}


def apply_plan(db, user, plan, file_name, data):
    """Write the plan. Returns counts. The caller commits."""
    counts = {"paid": 0, "rejected": 0, "skipped": 0}
    batches = {}
    for it in plan["items"]:
        ln = it["line"]
        if ln is None or it["outcome"] not in ("paid", "rejected"):
            counts["skipped"] += 1
            continue
        if ln.batch_id:
            batches.setdefault(ln.batch_id, 0)
        if it["outcome"] == "paid":
            ln.status, ln.bank_ref, ln.confirmed_on = "Confirmed", it["utr"][:40], it["on"] or date.today()
            db.flush()
            sync_source(db, ln, user)
            audit(db, user, "payment_line", ln.id, ln.beneficiary_name, "confirm",
                  f"Bank confirmed {ln.amount}, UTR {ln.bank_ref}, a/c {mask(ln.account_no)}")
            if ln.batch_id:
                batches[ln.batch_id] += 1
            counts["paid"] += 1
        else:
            old_batch = ln.batch.batch_no if ln.batch_id else None
            ln.batch_id, ln.status, ln.exported_at = None, "Queued", None
            sync_source(db, ln, user)
            audit(db, user, "payment_line", ln.id, ln.beneficiary_name, "bank_rejected",
                  f"Bank rejected ({it['reason'] or it['status']}); back in the waiting list (was batch {old_batch})")
            counts["rejected"] += 1
    db.flush()
    for bid, n_paid in batches.items():
        b = db.get(PaymentBatch, bid)
        db.refresh(b)
        live = [ln for ln in b.lines if ln.status != "Withdrawn"]
        if not live:
            b.status, b.cancel_reason = "Cancelled", "bank rejected every payment"
        elif all(ln.status == "Confirmed" for ln in live):
            b.status = "Confirmed"
        if n_paid:
            conf = db.query(PaymentConfirmation).filter_by(batch_id=bid).first()
            if conf is None:
                db.add(PaymentConfirmation(batch_id=bid, file_name=file_name[:200], file_data=data,
                                           uploaded_by=user.id if user else None, lines_matched=n_paid))
            else:  # a later file for the same batch: keep the first file
                conf.lines_matched = (conf.lines_matched or 0) + n_paid
                conf.notes = f"more lines confirmed by {file_name}"[:200]
        audit(db, user, "payment_batch", bid, b.batch_no, "confirm",
              f"Bank confirmation {file_name} loaded: {n_paid} confirmed now, batch {b.status}")
    return counts
