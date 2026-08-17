from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.auth import require_module_permission, get_user_module_permissions
from app.formatting import format_qty, format_dt
from app.models import ProductionOrder, ProductionOrderComponent, Item, StockTransaction, User

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
        )
        .get(order_id)
    )
    perms = get_user_module_permissions(user, db, "production")
    return templates.TemplateResponse("production/detail.html", {"request": request, "user": user, "order": order, "error": None, "perms": perms})


@router.post("/{order_id}/complete")
def complete_production_order(order_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("production", "edit"))):
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
