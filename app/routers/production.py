import re

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.auth import require_module_permission, get_user_module_permissions
from app.formatting import format_qty, format_dt
from app.models import ProductionOrder, ProductionOrderComponent, Item, StockTransaction, ItemSerial, User

router = APIRouter(prefix="/production", tags=["production"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["qty"] = format_qty
templates.env.filters["dt"] = format_dt


def next_order_no(db: Session) -> str:
    count = db.query(ProductionOrder).count()
    return f"PR-{count + 1:05d}"


@router.get("")
def list_production(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("production", "view"))):
    orders = db.query(ProductionOrder).options(joinedload(ProductionOrder.item)).order_by(ProductionOrder.created_at.desc()).all()
    perms = get_user_module_permissions(user, db, "production")
    return templates.TemplateResponse("production/list.html", {"request": request, "user": user, "orders": orders, "perms": perms})


@router.get("/new")
def new_production_form(request: Request, item_id: int = None, db: Session = Depends(get_db), user: User = Depends(require_module_permission("production", "add"))):
    assemblies = (
        db.query(Item)
        .filter(Item.is_assembly == True, Item.is_active == True)  # noqa: E712
        .order_by(Item.name)
        .all()
    )
    return templates.TemplateResponse(
        "production/form.html",
        {"request": request, "user": user, "assemblies": assemblies, "preselect_item_id": item_id},
    )


@router.post("/new")
def create_production_order(
    request: Request,
    item_id: int = Form(...),
    quantity: float = Form(...),
    notes: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("production", "add")),
):
    item = db.query(Item).get(item_id)
    if not item or not item.is_assembly:
        return RedirectResponse(url="/production/new?error=Select an item that is marked as assembled", status_code=303)
    if not item.bom_components:
        return RedirectResponse(
            url=f"/production/new?error=This item has no Bill of Materials defined yet — edit it first",
            status_code=303,
        )
    if quantity <= 0:
        return RedirectResponse(url="/production/new?error=Quantity must be at least 1", status_code=303)

    order = ProductionOrder(
        order_no=next_order_no(db),
        item_id=item.id,
        quantity=quantity,
        notes=notes,
        created_by=user.id,
        status="draft",
    )
    # Snapshot the recipe as it stands right now, so later edits to the BOM
    # don't silently change what an already-created order will consume.
    for component in item.bom_components:
        order.components.append(ProductionOrderComponent(
            component_item_id=component.component_item_id,
            quantity_per_unit=component.quantity,
            quantity_required=float(component.quantity) * quantity,
        ))
    db.add(order)
    db.commit()
    return RedirectResponse(url=f"/production/{order.id}", status_code=303)


@router.get("/{order_id}")
def view_production_order(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("production", "view"))):
    order = (
        db.query(ProductionOrder)
        .options(
            joinedload(ProductionOrder.item),
            joinedload(ProductionOrder.components).joinedload(ProductionOrderComponent.component_item),
            joinedload(ProductionOrder.serials),
        )
        .get(order_id)
    )
    perms = get_user_module_permissions(user, db, "production")
    return templates.TemplateResponse("production/detail.html", {"request": request, "user": user, "order": order, "error": None, "perms": perms})


def _split_serials(raw: str) -> list[str]:
    """Split a textarea of serial numbers on commas and/or newlines. Same
    helper as app/routers/purchase.py's — kept local rather than shared
    since it's five lines and this app doesn't otherwise have a shared
    utils module to put it in."""
    parts = re.split(r"[\n,]+", raw or "")
    return [p.strip() for p in parts if p.strip()]


@router.get("/{order_id}/complete")
def complete_form(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("production", "edit"))):
    """Serial-capture confirmation page — only reached (via a link, not a
    plain form button) when the finished item is has_serial, mirroring how
    Purchase receiving always asks for serials on the way in. A
    non-serialized item has nothing to enter here, so it keeps completing
    in one click straight from the detail page's button; hitting this URL
    directly for one just bounces back rather than showing a pointless
    empty form."""
    order = (
        db.query(ProductionOrder)
        .options(
            joinedload(ProductionOrder.item),
            joinedload(ProductionOrder.components).joinedload(ProductionOrderComponent.component_item),
        )
        .get(order_id)
    )
    if not order:
        return RedirectResponse(url="/production?error=Production order not found", status_code=303)
    if order.status != "draft":
        return RedirectResponse(url=f"/production/{order_id}?error=Only draft orders can be completed", status_code=303)
    if not order.item.has_serial:
        return RedirectResponse(url=f"/production/{order_id}", status_code=303)
    return templates.TemplateResponse(
        "production/complete.html",
        {"request": request, "user": user, "order": order, "error": None, "submitted": ""},
    )


@router.post("/{order_id}/complete")
async def complete_production_order(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("production", "edit"))):
    order = (
        db.query(ProductionOrder)
        .options(
            joinedload(ProductionOrder.item),
            joinedload(ProductionOrder.components).joinedload(ProductionOrderComponent.component_item),
        )
        .get(order_id)
    )
    if order.status != "draft":
        return RedirectResponse(url=f"/production/{order_id}?error=Only draft orders can be completed", status_code=303)

    # Validate every component has enough stock before touching anything.
    shortages = []
    for line in order.components:
        if line.component_item.current_stock < line.quantity_required:
            shortages.append(
                f"{line.component_item.name}: need {line.quantity_required}, have {line.component_item.current_stock}"
            )
    if shortages:
        perms = get_user_module_permissions(user, db, "production")
        return templates.TemplateResponse(
            "production/detail.html",
            {
                "request": request, "user": user, "order": order,
                "error": "Not enough stock: " + "; ".join(shortages), "perms": perms,
            },
            status_code=400,
        )

    # Serialized finished items need one serial number captured per unit
    # being built here, same as a serialized item captures one per unit on
    # the way IN via Purchase receiving — this is that same capture, just
    # for a built (not bought) unit. Validated independently of whether the
    # request came from the GET confirmation page above, so a re-POST of a
    # stale form (or a direct POST) can't skip it.
    serials = []
    raw_serials = ""
    if order.item.has_serial:
        form = await request.form()
        raw_serials = form.get("serials", "")
        serials = _split_serials(raw_serials)
        error = None
        if len(serials) != order.quantity:
            error = f"Expected {order.quantity} serial number(s), got {len(serials)}."
        elif len(set(serials)) != len(serials):
            error = "Duplicate serial numbers entered."
        else:
            existing = {
                s for (s,) in db.query(ItemSerial.serial_number).filter(ItemSerial.item_id == order.item_id).all()
            }
            clashes = existing.intersection(serials)
            if clashes:
                error = f"Already recorded for this item: {', '.join(sorted(clashes))}"
        if error:
            return templates.TemplateResponse(
                "production/complete.html",
                {"request": request, "user": user, "order": order, "error": error, "submitted": raw_serials},
                status_code=400,
            )

    for line in order.components:
        component = db.query(Item).get(line.component_item_id)
        component.current_stock -= line.quantity_required
        db.add(StockTransaction(
            item_id=component.id, transaction_type="OUT", quantity=line.quantity_required,
            reference_type="production_order", reference_id=order.id,
            notes=f"Consumed to build {order.order_no} ({order.item.name})",
        ))

    finished = db.query(Item).get(order.item_id)
    finished.current_stock = (finished.current_stock or 0) + order.quantity
    db.add(StockTransaction(
        item_id=finished.id, transaction_type="IN", quantity=order.quantity,
        reference_type="production_order", reference_id=order.id,
        notes=f"Built via {order.order_no}",
    ))
    for serial in serials:
        db.add(ItemSerial(
            item_id=finished.id, serial_number=serial, production_order_id=order.id,
        ))

    order.status = "completed"
    db.commit()
    return RedirectResponse(url=f"/production/{order_id}?success=Order completed — stock updated", status_code=303)


@router.post("/{order_id}/cancel")
def cancel_production_order(order_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("production", "edit"))):
    order = db.query(ProductionOrder).get(order_id)
    if order.status == "draft":
        order.status = "cancelled"
        db.commit()
    return RedirectResponse(url=f"/production/{order_id}?success=Order cancelled", status_code=303)
