import re

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_module_permission, get_user_module_permissions
from app.formatting import format_qty, format_dt
from app.models import Item, StockTransaction, ItemSerial, User
from app.audit import log_action

router = APIRouter(prefix="/inventory", tags=["inventory"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["qty"] = format_qty
templates.env.filters["dt"] = format_dt


def _split_serials(raw: str) -> list[str]:
    """Split a textarea of serial numbers on commas and/or newlines — same
    helper as purchase.py's receive flow, the other place serials get
    entered."""
    parts = re.split(r"[\n,]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


@router.get("")
def list_inventory(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "view"))):
    items = db.query(Item).order_by(Item.name).all()
    perms = get_user_module_permissions(user, db, "inventory")
    return templates.TemplateResponse("inventory/list.html", {"request": request, "user": user, "items": items, "perms": perms})


@router.get("/adjust")
def adjust_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "add"))):
    items = db.query(Item).filter(Item.is_active == True).order_by(Item.name).all()  # noqa: E712
    return templates.TemplateResponse("inventory/adjust.html", {"request": request, "user": user, "items": items})


@router.post("/adjust")
def submit_adjustment(
    item_id: int = Form(...),
    direction: str = Form(...),  # "increase" | "decrease"
    quantity: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("inventory", "add")),
):
    item = db.query(Item).get(item_id)
    if quantity > 0:
        old_stock = float(item.current_stock or 0)
        # current_stock comes back from SQLAlchemy as Decimal (Numeric(14,4)
        # column) — Python refuses to mix Decimal and float in arithmetic
        # (raises TypeError), so cast to float first, same as the Decimal
        # item fields are handled in sales.py/purchase.py.
        if direction == "decrease":
            item.current_stock = float(item.current_stock or 0) - quantity
        else:
            item.current_stock = float(item.current_stock or 0) + quantity
        db.add(StockTransaction(
            item_id=item.id,
            transaction_type="ADJUST",
            quantity=quantity,
            reference_type="manual",
            notes=(f"{'Increase' if direction != 'decrease' else 'Decrease'} by {user.username}: {notes}").strip(),
        ))
        log_action(
            db, user, "item", item.id, item.name, "adjust",
            summary=f"Manual {'decrease' if direction == 'decrease' else 'increase'} of {quantity:g}: {old_stock:g} -> {float(item.current_stock):g}"
            + (f" ({notes})" if notes else ""),
        )
        db.commit()
    return RedirectResponse(url="/inventory?success=Stock adjusted", status_code=303)


@router.get("/{item_id}/ledger")
def item_ledger(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "view"))):
    item = db.query(Item).get(item_id)
    transactions = (
        db.query(StockTransaction)
        .filter(StockTransaction.item_id == item_id)
        .order_by(StockTransaction.transaction_date.desc())
        .all()
    )
    # Serial numbers live on their own page now (/inventory/{id}/serials,
    # like Zoho's "click the stock number" pattern) — the ledger here is
    # just the stock-movement (IN/OUT/ADJUST) history.
    return templates.TemplateResponse(
        "inventory/ledger.html",
        {"request": request, "user": user, "item": item, "transactions": transactions},
    )


@router.get("/{item_id}/serials")
def serials_list(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "view"))):
    item = db.query(Item).get(item_id)
    serials = (
        db.query(ItemSerial)
        .filter(ItemSerial.item_id == item_id)
        .order_by(ItemSerial.received_at.desc())
        .all()
        if item.has_serial else []
    )
    # How many of this item's current units still have no serial number on
    # file — e.g. stock brought in some way other than the Purchase receive
    # flow (a Zoho import, an opening-balance item, ...), where nothing ever
    # prompted for serials. Only meaningful for a has_serial item; capped at
    # 0 rather than going negative if the count ever ran ahead of stock.
    remaining_to_serialize = max(0, float(item.current_stock or 0) - len(serials)) if item.has_serial else 0
    # perms is computed here purely so the template can hide the add/edit/
    # delete buttons from a user who doesn't have the right to use them,
    # same pattern as items/list.html and items/detail.html.
    perms = get_user_module_permissions(user, db, "inventory")
    return templates.TemplateResponse(
        "inventory/serials.html",
        {
            "request": request, "user": user, "item": item, "serials": serials,
            "remaining_to_serialize": remaining_to_serialize, "perms": perms,
        },
    )


@router.get("/{item_id}/serials/add")
def add_serials_form(
    item_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "add")),
):
    item = db.query(Item).get(item_id)
    if not item or not item.has_serial:
        return RedirectResponse(
            url=f"/inventory/{item_id}/serials?error=Turn on \"This item has serial numbers\" on this item's Edit page first",
            status_code=303,
        )
    recorded_count = db.query(ItemSerial).filter(ItemSerial.item_id == item_id).count()
    remaining = max(0, float(item.current_stock or 0) - recorded_count)
    return templates.TemplateResponse(
        "inventory/serials_add.html",
        {
            "request": request, "user": user, "item": item,
            "recorded_count": recorded_count, "remaining": remaining, "error": None, "submitted": "",
        },
    )


@router.post("/{item_id}/serials/add")
async def add_serials(
    item_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "add")),
):
    item = db.query(Item).get(item_id)
    if not item or not item.has_serial:
        return RedirectResponse(
            url=f"/inventory/{item_id}/serials?error=Turn on \"This item has serial numbers\" on this item's Edit page first",
            status_code=303,
        )

    form = await request.form()
    raw = form.get("serial_numbers", "")
    new_serials = _split_serials(raw)
    recorded_count = db.query(ItemSerial).filter(ItemSerial.item_id == item_id).count()
    remaining = max(0, float(item.current_stock or 0) - recorded_count)

    error = None
    if not new_serials:
        error = "Enter at least one serial number."
    elif len(new_serials) != len(set(new_serials)):
        error = "Duplicate serial numbers entered."
    elif len(new_serials) > remaining:
        error = f"Only {remaining:g} unit(s) still need a serial number — you entered {len(new_serials)}."
    else:
        existing = {
            s for (s,) in db.query(ItemSerial.serial_number).filter(ItemSerial.item_id == item_id).all()
        }
        clashes = existing.intersection(new_serials)
        if clashes:
            error = f"Already recorded for this item: {', '.join(sorted(clashes))}"

    if error:
        return templates.TemplateResponse(
            "inventory/serials_add.html",
            {
                "request": request, "user": user, "item": item, "recorded_count": recorded_count,
                "remaining": remaining, "error": error, "submitted": raw,
            },
            status_code=400,
        )

    # Deliberately no StockTransaction here and no purchase_order_id on
    # these rows (both nullable — see ItemSerial in models.py) — this isn't
    # new stock arriving, just backfilling serial numbers for units already
    # counted in current_stock. Stock quantity stays governed solely by the
    # ledger (Purchase receive / Sales delivery / Manual Adjustment).
    for serial in new_serials:
        db.add(ItemSerial(item_id=item.id, serial_number=serial))
    log_action(
        db, user, "item", item.id, item.name, "add_serials",
        summary=f"Added {len(new_serials)} serial number(s): {', '.join(new_serials)}",
    )
    db.commit()
    return RedirectResponse(url=f"/inventory/{item_id}/serials?success=Added {len(new_serials)} serial number(s)", status_code=303)


@router.get("/{item_id}/serials/{serial_id}/edit")
def edit_serial_form(
    item_id: int, serial_id: int, request: Request,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "edit")),
):
    serial = db.query(ItemSerial).get(serial_id)
    if not serial or serial.item_id != item_id:
        return RedirectResponse(url=f"/inventory/{item_id}/serials?error=Serial number not found", status_code=303)
    item = db.query(Item).get(item_id)
    return templates.TemplateResponse(
        "inventory/serial_edit.html",
        {"request": request, "user": user, "item": item, "serial": serial, "error": None},
    )


@router.post("/{item_id}/serials/{serial_id}/edit")
async def update_serial(
    item_id: int, serial_id: int, request: Request,
    serial_number: str = Form(...),
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "edit")),
):
    serial = db.query(ItemSerial).get(serial_id)
    if not serial or serial.item_id != item_id:
        return RedirectResponse(url=f"/inventory/{item_id}/serials?error=Serial number not found", status_code=303)

    new_value = serial_number.strip()
    item = db.query(Item).get(item_id)
    if not new_value:
        return templates.TemplateResponse(
            "inventory/serial_edit.html",
            {"request": request, "user": user, "item": item, "serial": serial, "error": "Serial number can't be blank."},
            status_code=400,
        )
    clash = (
        db.query(ItemSerial)
        .filter(ItemSerial.item_id == item_id, ItemSerial.serial_number == new_value, ItemSerial.id != serial_id)
        .first()
    )
    if clash:
        return templates.TemplateResponse(
            "inventory/serial_edit.html",
            {
                "request": request, "user": user, "item": item, "serial": serial,
                "error": f"'{new_value}' is already recorded for this item.",
            },
            status_code=400,
        )

    old_value = serial.serial_number
    serial.serial_number = new_value
    if new_value != old_value:
        log_action(db, user, "item_serial", serial.id, item.name, "update", summary=f"{old_value} -> {new_value}")
    db.commit()
    return RedirectResponse(url=f"/inventory/{item_id}/serials?success=Serial number updated", status_code=303)


@router.post("/{item_id}/serials/{serial_id}/delete")
def delete_serial(
    item_id: int, serial_id: int,
    db: Session = Depends(get_db), user: User = Depends(require_module_permission("inventory", "delete")),
):
    serial = db.query(ItemSerial).get(serial_id)
    if serial and serial.item_id == item_id:
        log_action(db, user, "item_serial", serial.id, serial.item.name, "delete", summary=f"Deleted serial '{serial.serial_number}'")
        db.delete(serial)
        db.commit()
    return RedirectResponse(url=f"/inventory/{item_id}/serials?success=Serial number deleted", status_code=303)
