"""Expenses: rent and other bills (electricity, statutory, reimbursements ...), one-off or recurring.

  * An EXPENSE ENTRY is one amount to pay one payee for one month.
  * A RECURRING TEMPLATE (optional, a tick box when adding) remembers payee, amount and day of the month;
    'Prepare recurring' turns every active template into that month's entry, once per month.
  * 'Send for payment' puts the ticked entries on the Bank Payments queue (payments_service.queue_expense); from there
    they are batched, downloaded and bank-confirmed like any other payment.

Reuses the "payments" permission module (view = see; edit = add/change/send). Ported from CRM2's expenses blueprint
(without employee/supplier/subcontractor-ticket payees: ERP pays vendors from Bills).
"""
import calendar
from datetime import date
from decimal import Decimal

from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.auth import require_module_permission, get_user_module_permissions
from app.database import get_db
from app.formatting import format_inr, format_dt
from app.models import ExpenseEntry, ExpenseTemplate, Payee, PaymentLine, User
from app.payments_service import BADGE, STATUS_TEXT, audit, mask, queue_expense
from app.routers.payments import go, parse_amount, parse_date

router = APIRouter(prefix="/expenses", tags=["expenses"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt

RECENT_LIMIT = 25  # the list shows the latest 25 unless a From/To date range is chosen
CATEGORIES = ("Rent", "Utility", "Reimbursement", "Statutory", "Vendor bill", "Other")
STATUS_BADGE = {"Pending": "bg-secondary", "Queued": "bg-info text-dark", "In batch": "bg-primary",
                "Downloaded": "bg-warning text-dark", "Paid": "bg-success"}


def render(request, name, user, **ctx):
    return templates.TemplateResponse(name, {"request": request, "user": user, "mask": mask, **ctx})


def period(request):
    today = date.today()
    try:
        y = int(request.query_params.get("year") or today.year)
        m = int(request.query_params.get("month") or today.month)
    except ValueError:
        return today.year, today.month
    return (y, m) if 1 <= m <= 12 and 2000 <= y <= 2100 else (today.year, today.month)


def idx(year, month):
    return f"/expenses?year={year}&month={month}"


def due_date(year, month, day):
    return date(year, month, min(day, calendar.monthrange(year, month)[1]))


def active_payees(db, include_id=None):
    return [p for p in db.query(Payee).filter(Payee.is_deleted == False).order_by(Payee.name).all()  # noqa: E712
            if p.is_active or p.id == include_id]


def read_common(db, form):
    """Validated fields shared by the entry form and the recurring form. Raises ValueError with a user message."""
    pid = str(form.get("payee_id") or "")
    payee = db.get(Payee, int(pid)) if pid.isdigit() else None
    if payee is None or payee.is_deleted:
        raise ValueError("Choose who is to be paid")
    category = form.get("category") if form.get("category") in CATEGORIES else "Other"
    return dict(payee=payee, category=category, description=(form.get("description") or "").strip()[:100] or None,
                amount=parse_amount(form.get("amount")))


def template_day(text, fallback):
    try:
        d = int(text)
    except (TypeError, ValueError):
        d = fallback
    return max(1, min(d, 28))


# ---------------------------------------------------------------- the month's list

@router.get("")
def index(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view"))):
    perms = get_user_module_permissions(user, db, "payments")
    year, month = period(request)
    d_from, d_to = parse_date(request.query_params.get("from")), parse_date(request.query_params.get("to"))
    q = db.query(ExpenseEntry).options(joinedload(ExpenseEntry.payee)).filter(ExpenseEntry.is_cancelled == False)  # noqa: E712
    if d_from or d_to:
        if d_from:
            q = q.filter(ExpenseEntry.due_date >= d_from)
        if d_to:
            q = q.filter(ExpenseEntry.due_date <= d_to)
        entries = q.order_by(ExpenseEntry.due_date.desc(), ExpenseEntry.id.desc()).limit(500).all()
    else:
        entries = q.order_by(ExpenseEntry.due_date.desc(), ExpenseEntry.id.desc()).limit(RECENT_LIMIT).all()
    prepared = {e.template_id for e in db.query(ExpenseEntry).filter_by(period_year=year, period_month=month, is_cancelled=False).all()
                if e.template_id}
    unprepared = [t for t in db.query(ExpenseTemplate).options(joinedload(ExpenseTemplate.payee)).filter_by(is_active=True).all()
                  if t.id not in prepared]
    total = sum((Decimal(e.amount) for e in entries), Decimal(0))
    unpaid = sum((Decimal(e.amount) for e in entries if e.status != "Paid"), Decimal(0))
    return render(request, "expenses/index.html", user, entries=entries, year=year, month=month, month_name=calendar.month_name[month],
                  total=total, unpaid=unpaid, unprepared=unprepared, d_from=d_from, d_to=d_to, limit=RECENT_LIMIT, perms=perms,
                  badge=STATUS_BADGE, today=date.today())


# ---------------------------------------------------------------- add / edit / cancel one entry

@router.get("/add")
def add_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    return render(request, "expenses/entry_form.html", user, e=None, payees=active_payees(db), categories=CATEGORIES, today=date.today())


@router.post("/add")
async def add(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    form = await request.form()
    try:
        f = read_common(db, form)
        due = parse_date(form.get("due_date"))
        if due is None:
            raise ValueError("Choose the due date")
        recurring = form.get("recurring") == "on"
        tpl = None
        if recurring:
            tpl = ExpenseTemplate(payee_id=f["payee"].id, category=f["category"], description=f["description"], amount=f["amount"],
                                  day_of_month=template_day(form.get("recur_day"), due.day), is_active=True, created_by=user.id)
            db.add(tpl)
            db.flush()
            audit(db, user, "expense_template", tpl.id, f["payee"].name, "create", f"{tpl.category} {tpl.amount} on day {tpl.day_of_month}")
        e = ExpenseEntry(payee_id=f["payee"].id, template_id=tpl.id if tpl else None, category=f["category"], description=f["description"],
                         bill_no=(form.get("bill_no") or "").strip()[:40] or None, amount=f["amount"], due_date=due,
                         period_year=due.year, period_month=due.month, status="Pending", created_by=user.id)
        db.add(e)
        db.flush()
        audit(db, user, "expense_entry", e.id, f["payee"].name, "create", f"{e.category} {e.amount}, due {due:%d-%m-%Y}, recurring={recurring}")
        db.commit()
    except ValueError as ex:
        return go("/expenses/add", str(ex), "error")
    return go(idx(due.year, due.month), "Expense added and it will repeat every month (see Recurring)." if recurring else "Expense added.")


@router.get("/{entry_id:int}/edit")
def edit_form(entry_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    e = db.get(ExpenseEntry, entry_id)
    if e is None:
        return go("/expenses", "Expense not found", "error")
    if e.is_cancelled or e.status != "Pending":
        return go(idx(e.period_year, e.period_month), "Only an expense that is not yet sent for payment can be changed.", "error")
    return render(request, "expenses/entry_form.html", user, e=e, payees=active_payees(db, e.payee_id), categories=CATEGORIES, today=date.today())


@router.post("/{entry_id:int}/edit")
async def edit(entry_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    e = db.get(ExpenseEntry, entry_id)
    if e is None:
        return go("/expenses", "Expense not found", "error")
    if e.is_cancelled or e.status != "Pending":
        return go(idx(e.period_year, e.period_month), "Only an expense that is not yet sent for payment can be changed.", "error")
    form = await request.form()
    try:
        f = read_common(db, form)
        due = parse_date(form.get("due_date"))
        if due is None:
            raise ValueError("Choose the due date")
    except ValueError as ex:
        return go(f"/expenses/{entry_id}/edit", str(ex), "error")
    audit(db, user, "expense_entry", e.id, f["payee"].name, "update",
          f"payee {e.payee_id}->{f['payee'].id}, {e.category} {e.amount} due {e.due_date} -> {f['category']} {f['amount']} due {due}")
    e.payee_id, e.category, e.description, e.amount, e.due_date = f["payee"].id, f["category"], f["description"], f["amount"], due
    e.bill_no = (form.get("bill_no") or "").strip()[:40] or None
    db.commit()
    return go(idx(e.period_year, e.period_month), "Expense updated. (A recurring template, if any, is not changed - edit it under Recurring.)")


@router.post("/{entry_id:int}/cancel")
def cancel(entry_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    e = db.get(ExpenseEntry, entry_id)
    if e is None:
        return go("/expenses", "Expense not found", "error")
    if e.status != "Pending" or e.is_cancelled:
        return go(idx(e.period_year, e.period_month),
                  "Only an expense that is not yet sent for payment can be cancelled. Delete its payment from the Bank Payments waiting list first.", "error")
    e.is_cancelled = True
    audit(db, user, "expense_entry", e.id, e.payee.name, "cancel", f"{e.category} {e.amount}")
    db.commit()
    return go(idx(e.period_year, e.period_month), "Expense cancelled.")


# ---------------------------------------------------------------- recurring

@router.get("/recurring")
def recurring(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view"))):
    perms = get_user_module_permissions(user, db, "payments")
    tpls = (db.query(ExpenseTemplate).options(joinedload(ExpenseTemplate.payee))
            .order_by(ExpenseTemplate.is_active.desc(), ExpenseTemplate.id).all())
    last = {}
    for e in db.query(ExpenseEntry).filter(ExpenseEntry.template_id.isnot(None), ExpenseEntry.is_cancelled == False).all():  # noqa: E712
        key = (e.period_year, e.period_month)
        if e.template_id not in last or key > last[e.template_id]:
            last[e.template_id] = key
    return render(request, "expenses/recurring.html", user, templates_=tpls, last=last, perms=perms)


@router.get("/recurring/{tpl_id}/edit")
def recurring_edit_form(tpl_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    t = db.get(ExpenseTemplate, tpl_id)
    if t is None:
        return go("/expenses/recurring", "Recurring expense not found", "error")
    return render(request, "expenses/template_form.html", user, t=t, payees=active_payees(db, t.payee_id), categories=CATEGORIES)


@router.post("/recurring/{tpl_id}/edit")
async def recurring_edit(tpl_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    t = db.get(ExpenseTemplate, tpl_id)
    if t is None:
        return go("/expenses/recurring", "Recurring expense not found", "error")
    form = await request.form()
    try:
        f = read_common(db, form)
    except ValueError as ex:
        return go(f"/expenses/recurring/{tpl_id}/edit", str(ex), "error")
    audit(db, user, "expense_template", t.id, f["payee"].name, "update",
          f"payee {t.payee_id}->{f['payee'].id}, {t.category} {t.amount} day {t.day_of_month} -> {f['category']} {f['amount']}")
    t.payee_id, t.category, t.description, t.amount = f["payee"].id, f["category"], f["description"], f["amount"]
    t.day_of_month = template_day(form.get("recur_day"), t.day_of_month)
    t.is_active = form.get("is_active") == "on"
    db.commit()
    return go("/expenses/recurring", "Recurring expense saved. It applies to the months prepared from now on; months already prepared are not changed.")


@router.post("/recurring/{tpl_id}/toggle")
def recurring_toggle(tpl_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    t = db.get(ExpenseTemplate, tpl_id)
    if t is None:
        return go("/expenses/recurring", "Recurring expense not found", "error")
    t.is_active = not t.is_active
    audit(db, user, "expense_template", t.id, t.payee.name, "update", "resumed" if t.is_active else "stopped")
    db.commit()
    return go("/expenses/recurring", "Recurring expense resumed." if t.is_active else "Recurring expense stopped. Months already prepared are not removed.")


@router.post("/prepare")
async def prepare(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    """Create the month's entry for every active recurring template that has none yet (safe to press twice)."""
    form = await request.form()
    try:
        year, month = int(form.get("year")), int(form.get("month"))
        assert 1 <= month <= 12 and 2000 <= year <= 2100
    except (TypeError, ValueError, AssertionError):
        year, month = date.today().year, date.today().month
    have = {e.template_id for e in db.query(ExpenseEntry).filter_by(period_year=year, period_month=month).all() if e.template_id}
    n = 0
    for t in db.query(ExpenseTemplate).filter_by(is_active=True).order_by(ExpenseTemplate.id).all():
        if t.id in have:
            continue
        e = ExpenseEntry(payee_id=t.payee_id, template_id=t.id, category=t.category, description=t.description, amount=t.amount,
                         due_date=due_date(year, month, t.day_of_month), period_year=year, period_month=month,
                         status="Pending", created_by=user.id)
        db.add(e)
        db.flush()
        audit(db, user, "expense_entry", e.id, t.payee.name, "create", f"from recurring {t.id}: {e.category} {e.amount}")
        n += 1
    db.commit()
    return go(idx(year, month), f"{n} recurring expense(s) prepared for {calendar.month_name[month]} {year}." if n
              else "Nothing to prepare - every active recurring expense already has this month.", "success" if n else "error")


# ---------------------------------------------------------------- send to the bank queue

@router.post("/send")
async def send(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "edit"))):
    form = await request.form()
    try:
        year, month = int(form.get("year")), int(form.get("month"))
    except (TypeError, ValueError):
        year, month = date.today().year, date.today().month
    back = idx(year, month)
    ids = [int(x) for x in form.getlist("entry_id") if str(x).isdigit()]
    pay_date = parse_date(form.get("pay_date"))
    if not ids:
        return go(back, "Tick the expenses to send.", "error")
    if pay_date is None:
        return go(back, "Choose the pay date.", "error")
    sent, skipped = 0, []
    for e in db.query(ExpenseEntry).filter(ExpenseEntry.id.in_(ids)).all():
        try:
            queue_expense(db, user, e, pay_date)
            sent += 1
        except ValueError as ex:
            skipped.append(f"{e.payee.name}: {ex}")
    db.commit()
    if skipped:
        msg = ((f"{sent} sent for payment. " if sent else "") + "Not sent - " + "; ".join(skipped))
        return go(back, msg, "error")
    return go(back, f"{sent} expense(s) sent for payment ({pay_date:%d-%m-%Y}). They now wait on the Bank Payments page.")


# ---------------------------------------------------------------- a payee's ledger

@router.get("/ledger/{payee_id}")
def ledger(payee_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("payments", "view"))):
    """Everything due to and paid to one payee: each expense with its payment status, bank reference and the totals."""
    p = db.get(Payee, payee_id)
    if p is None:
        return go("/payments/payees", "Payee not found", "error")
    entries = (db.query(ExpenseEntry).filter_by(payee_id=p.id, is_cancelled=False)
               .order_by(ExpenseEntry.period_year.desc(), ExpenseEntry.period_month.desc(), ExpenseEntry.id.desc()).all())
    lines = {ln.source_id: ln for ln in db.query(PaymentLine).filter(
        PaymentLine.source_type == "EXPENSE", PaymentLine.source_id.in_({e.id for e in entries} or {0}), PaymentLine.status != "Withdrawn").all()}
    due = sum((Decimal(e.amount) for e in entries), Decimal(0))
    paid = sum((Decimal(e.amount) for e in entries if e.status == "Paid"), Decimal(0))
    return render(request, "expenses/ledger.html", user, p=p, entries=entries, lines=lines, due=due, paid=paid, balance=due - paid,
                  badge=STATUS_BADGE)
