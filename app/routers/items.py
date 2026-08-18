import os
import uuid
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, Form, Query, UploadFile, File
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_module_permission, get_user_module_permissions
from app.formatting import format_qty, format_inr
from app.models import (
    Item, Category, BomComponent, ItemAttachment, User,
    StockTransaction, SalesOrderItem, PurchaseOrderItem,
    ProductionOrderComponent, ItemSerial,
)
from app.requirements import item_sales_demand, on_order_qty
from app.audit import diff_fields, log_field_changes, log_action

router = APIRouter(prefix="/items", tags=["items"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["qty"] = format_qty
templates.env.filters["inr"] = format_inr

# All item attachments (pictures + PDFs) live on disk in this one flat
# folder — only the metadata + generated filename is stored in the DB,
# never the file bytes. Served back out via the existing /static mount.
ATTACHMENT_DIR = "app/static/uploads"
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp"}
PDF_EXTENSIONS = {".pdf"}


def _attachment_kind(filename: str) -> str | None:
    ext = os.path.splitext(filename)[1].lower()
    if ext in IMAGE_EXTENSIONS:
        return "image"
    if ext in PDF_EXTENSIONS:
        return "pdf"
    return None


def build_bom_components(item: Item, component_ids: list[str], quantities: list[str]) -> list[BomComponent]:
    """Turn the submitted component/quantity rows into BomComponent objects,
    skipping blank rows and refusing to let an item list itself as its own
    component (that would make production orders impossible to complete)."""
    components = []
    for comp_id_str, qty_str in zip(component_ids, quantities):
        if not comp_id_str or not qty_str:
            continue
        qty = float(qty_str)
        if qty <= 0:
            continue
        comp_id = int(comp_id_str)
        if item.id is not None and comp_id == item.id:
            continue  # guard against self-reference
        components.append(BomComponent(component_item_id=comp_id, quantity=qty))
    return components


def get_or_create_categories(db: Session, names: list[str]) -> list[Category]:
    """Look up (or create) a Category row for each submitted name. Names come
    from the multi-select on the item form — Select2's `tags: true` mode lets
    the user either pick an existing one or type a brand-new one."""
    seen = []
    categories = []
    for raw_name in names:
        name = (raw_name or "").strip()
        if not name or name in seen:
            continue
        seen.append(name)
        category = db.query(Category).filter(Category.name == name).first()
        if not category:
            category = Category(name=name)
            db.add(category)
            db.flush()
        categories.append(category)
    return categories


def get_item_delete_blockers(db: Session, item: Item) -> list[str]:
    """Reasons an item can't be hard-deleted. Several tables reference
    items.id with a NOT NULL, no-cascade foreign key (stock_transactions,
    sales/purchase order lines, production order components, serials) —
    deleting an item that already has any of this history either violates
    that constraint outright or (for the one relationship SQLAlchemy does
    manage automatically, stock_transactions) tries to null out the FK and
    fails the same way. Rather than lose history or crash with a 500,
    refuse up front and point at the existing 'Active' checkbox on Edit as
    the safe way to retire an item that's actually been used."""
    blockers = []
    if db.query(StockTransaction.id).filter(StockTransaction.item_id == item.id).first():
        blockers.append("it has stock transaction history")
    if db.query(BomComponent.id).filter(BomComponent.component_item_id == item.id).first():
        blockers.append("it's used as a component in another item's Bill of Materials")
    if db.query(SalesOrderItem.id).filter(SalesOrderItem.item_id == item.id).first():
        blockers.append("it appears on one or more Sales Orders")
    if db.query(PurchaseOrderItem.id).filter(PurchaseOrderItem.item_id == item.id).first():
        blockers.append("it appears on one or more Purchase Orders")
    if db.query(ProductionOrderComponent.id).filter(ProductionOrderComponent.component_item_id == item.id).first():
        blockers.append("it appears as a component on one or more Production Orders")
    if db.query(ItemSerial.id).filter(ItemSerial.item_id == item.id).first():
        blockers.append("it has serial numbers recorded against it")
    return blockers


@router.get("")
def list_items(
    request: Request,
    category: list[str] = Query(default=[]),
    exclude_category: list[str] = Query(default=[]),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("items", "view")),
):
    query = db.query(Item)
    # AND filter: item must carry every selected category tag (e.g. Sensor + Spare).
    for cat_name in category:
        query = query.filter(Item.categories.any(Category.name == cat_name))
    # Exclude filter: hide the item if it carries ANY of these tags.
    for cat_name in exclude_category:
        query = query.filter(~Item.categories.any(Category.name == cat_name))
    items = query.order_by(Item.name).all()
    all_categories = db.query(Category).order_by(Category.name).all()
    perms = get_user_module_permissions(user, db, "items")
    return templates.TemplateResponse(
        "items/list.html",
        {
            "request": request, "user": user, "items": items, "all_categories": all_categories,
            "selected_categories": category, "excluded_categories": exclude_category, "perms": perms,
        },
    )


@router.get("/new")
def new_item_form(
    request: Request,
    clone_from: int | None = Query(default=None),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("items", "add")),
):
    categories = db.query(Category).order_by(Category.name).all()
    possible_components = db.query(Item).filter(Item.is_active == True).order_by(Item.name).all()  # noqa: E712
    clone_source = db.query(Item).get(clone_from) if clone_from else None
    return templates.TemplateResponse(
        "items/form.html",
        {
            "request": request, "user": user, "item": None, "categories": categories,
            "possible_components": possible_components, "clone_source": clone_source,
        },
    )


@router.post("/new")
async def create_item(
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    small_description: str = Form(""),
    unit: str = Form("Nos"),
    hsn_code: str = Form(""),
    gst_percentage: float = Form(0),
    purchase_price: float = Form(0),
    sales_price: float = Form(0),
    reorder_level: float = Form(0),
    current_stock: float = Form(0),
    has_serial: bool = Form(False),
    is_assembly: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("items", "add")),
):
    form = await request.form()
    categories = get_or_create_categories(db, form.getlist("categories"))
    item = Item(
        name=name, description=description, small_description=small_description.strip() or None,
        categories=categories, unit=unit,
        hsn_code=hsn_code.strip() or None, gst_percentage=gst_percentage,
        purchase_price=purchase_price, sales_price=sales_price,
        reorder_level=reorder_level, current_stock=current_stock,
        has_serial=has_serial, is_assembly=is_assembly,
    )
    if is_assembly:
        item.bom_components = build_bom_components(
            item, form.getlist("component_item_id"), form.getlist("component_quantity"),
        )
    db.add(item)
    db.flush()  # need item.id before logging
    log_action(db, user, "item", item.id, item.name, "create", summary=f"Created ({item.unit}, sales price {item.sales_price})")
    db.commit()
    return RedirectResponse(url=f"/items/{item.id}?success=Item created", status_code=303)


@router.get("/{item_id}")
def view_item(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("items", "view"))):
    item = db.query(Item).get(item_id)
    perms = get_user_module_permissions(user, db, "items")
    sales_demand = item_sales_demand(db, item_id)
    on_order = on_order_qty(db, item_id)
    return templates.TemplateResponse(
        "items/detail.html",
        {
            "request": request, "user": user, "item": item, "perms": perms,
            "sales_demand": sales_demand, "on_order": on_order,
        },
    )


@router.post("/{item_id}/attachments")
async def upload_attachments(
    item_id: int,
    files: list[UploadFile] = File(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("items", "edit")),
):
    item = db.query(Item).get(item_id)
    if not item:
        return RedirectResponse(url="/items?error=Item not found", status_code=303)

    os.makedirs(ATTACHMENT_DIR, exist_ok=True)
    rejected = []
    saved = 0
    for upload in files:
        if not upload.filename:
            continue
        kind = _attachment_kind(upload.filename)
        if kind is None:
            rejected.append(upload.filename)
            continue
        ext = os.path.splitext(upload.filename)[1].lower()
        stored_filename = f"{uuid.uuid4().hex}{ext}"
        dest_path = os.path.join(ATTACHMENT_DIR, stored_filename)
        contents = await upload.read()
        with open(dest_path, "wb") as f:
            f.write(contents)
        db.add(ItemAttachment(
            item_id=item.id, kind=kind,
            original_filename=upload.filename, stored_filename=stored_filename,
        ))
        saved += 1

    db.commit()

    if rejected and saved:
        msg = f"Uploaded {saved} file(s). Skipped unsupported file(s): {', '.join(rejected)}"
        return RedirectResponse(url=f"/items/{item_id}/edit?error={quote(msg)}", status_code=303)
    if rejected and not saved:
        msg = f"No files uploaded — unsupported type(s): {', '.join(rejected)}. Only images (jpg, png, gif, webp) and PDFs are allowed."
        return RedirectResponse(url=f"/items/{item_id}/edit?error={quote(msg)}", status_code=303)
    return RedirectResponse(url=f"/items/{item_id}/edit?success=Uploaded {saved} file(s)", status_code=303)


@router.post("/{item_id}/attachments/{attachment_id}/delete")
def delete_attachment(item_id: int, attachment_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("items", "edit"))):
    attachment = db.query(ItemAttachment).get(attachment_id)
    if attachment and attachment.item_id == item_id:
        file_path = os.path.join(ATTACHMENT_DIR, attachment.stored_filename)
        if os.path.exists(file_path):
            os.remove(file_path)
        db.delete(attachment)
        db.commit()
    return RedirectResponse(url=f"/items/{item_id}/edit?success=Attachment deleted", status_code=303)


@router.get("/{item_id}/edit")
def edit_item_form(item_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("items", "edit"))):
    item = db.query(Item).get(item_id)
    categories = db.query(Category).order_by(Category.name).all()
    possible_components = (
        db.query(Item).filter(Item.is_active == True, Item.id != item_id).order_by(Item.name).all()  # noqa: E712
    )
    return templates.TemplateResponse(
        "items/form.html",
        {
            "request": request, "user": user, "item": item, "categories": categories,
            "possible_components": possible_components, "clone_source": None,
        },
    )


@router.post("/{item_id}/edit")
async def update_item(
    item_id: int,
    request: Request,
    name: str = Form(...),
    description: str = Form(""),
    small_description: str = Form(""),
    unit: str = Form("Nos"),
    hsn_code: str = Form(""),
    gst_percentage: float = Form(0),
    purchase_price: float = Form(0),
    sales_price: float = Form(0),
    reorder_level: float = Form(0),
    has_serial: bool = Form(False),
    is_assembly: bool = Form(False),
    is_active: bool = Form(False),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("items", "edit")),
):
    form = await request.form()
    item = db.query(Item).get(item_id)

    # Field-level diff captured against the *current* row before anything is
    # overwritten — covers pricing/stock-relevant fields plus the rest of
    # the editable columns; categories and bom_components are relationship
    # collections (not plain scalars) so they're compared separately below.
    old_category_names = sorted(c.name for c in item.categories)
    old_component_count = len(item.bom_components)

    changes = diff_fields(item, {
        "name": name,
        "description": description,
        "small_description": small_description.strip() or None,
        "unit": unit,
        "hsn_code": hsn_code.strip() or None,
        "gst_percentage": gst_percentage,
        "purchase_price": purchase_price,
        "sales_price": sales_price,
        "reorder_level": reorder_level,
        "has_serial": has_serial,
        "is_assembly": is_assembly,
        "is_active": is_active,
    })

    item.name = name
    item.description = description
    item.small_description = small_description.strip() or None
    item.categories = get_or_create_categories(db, form.getlist("categories"))
    item.unit = unit
    item.hsn_code = hsn_code.strip() or None
    item.gst_percentage = gst_percentage
    item.purchase_price = purchase_price
    item.sales_price = sales_price
    item.reorder_level = reorder_level
    item.has_serial = has_serial
    item.is_assembly = is_assembly
    item.bom_components = (
        build_bom_components(item, form.getlist("component_item_id"), form.getlist("component_quantity"))
        if is_assembly else []
    )
    item.is_active = is_active

    new_category_names = sorted(c.name for c in item.categories)
    if new_category_names != old_category_names:
        changes["categories"] = (", ".join(old_category_names) or "(none)", ", ".join(new_category_names) or "(none)")
    new_component_count = len(item.bom_components)
    if new_component_count != old_component_count:
        changes["bom_components"] = (f"{old_component_count} component(s)", f"{new_component_count} component(s)")

    log_field_changes(db, user, "item", item.id, item.name, changes)
    db.commit()
    return RedirectResponse(url=f"/items/{item_id}?success=Item updated", status_code=303)


@router.post("/{item_id}/delete")
def delete_item(item_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("items", "delete"))):
    item = db.query(Item).get(item_id)
    if not item:
        return RedirectResponse(url="/items?error=Item not found", status_code=303)

    blockers = get_item_delete_blockers(db, item)
    if blockers:
        msg = (
            f"Can't delete '{item.name}' — {'; '.join(blockers)}. "
            "Untick 'Active' on Edit instead to retire it without losing that history."
        )
        return RedirectResponse(url=f"/items/{item_id}?error={quote(msg)}", status_code=303)

    for attachment in item.attachments:
        file_path = os.path.join(ATTACHMENT_DIR, attachment.stored_filename)
        if os.path.exists(file_path):
            os.remove(file_path)
    try:
        log_action(db, user, "item", item.id, item.name, "delete", summary=f"Deleted '{item.name}'")
        db.delete(item)
        db.commit()
    except IntegrityError:
        # Belt-and-braces: catch any reference the checks above didn't
        # anticipate rather than surfacing a raw 500 to the user.
        db.rollback()
        msg = f"Can't delete '{item.name}' — it's still referenced elsewhere. Untick 'Active' on Edit instead."
        return RedirectResponse(url=f"/items/{item_id}?error={quote(msg)}", status_code=303)
    return RedirectResponse(url="/items?success=Item deleted", status_code=303)
