from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_module_permission, get_user_module_permissions
from app.formatting import format_inr, format_dt
from app.models import Vendor, PurchaseOrder, User

router = APIRouter(prefix="/vendors", tags=["vendors"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt


@router.get("")
def list_vendors(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("vendors", "view"))):
    vendors = db.query(Vendor).order_by(Vendor.name).all()
    perms = get_user_module_permissions(user, db, "vendors")
    return templates.TemplateResponse("vendors/list.html", {"request": request, "user": user, "vendors": vendors, "perms": perms})


@router.get("/new")
def new_vendor_form(request: Request, user: User = Depends(require_module_permission("vendors", "add"))):
    return templates.TemplateResponse("vendors/form.html", {"request": request, "user": user, "vendor": None})


@router.post("/new")
def create_vendor(
    request: Request,
    name: str = Form(...),
    contact_person: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    gstin: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("vendors", "add")),
):
    vendor = Vendor(
        name=name, contact_person=contact_person, email=email,
        phone=phone, address=address, gstin=gstin,
    )
    db.add(vendor)
    db.commit()
    return RedirectResponse(url=f"/vendors/{vendor.id}?success=Vendor created", status_code=303)


@router.get("/{vendor_id}")
def view_vendor(vendor_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("vendors", "view"))):
    vendor = db.query(Vendor).get(vendor_id)
    recent_orders = (
        db.query(PurchaseOrder)
        .filter(PurchaseOrder.vendor_id == vendor_id)
        .order_by(PurchaseOrder.order_date.desc())
        .limit(10)
        .all()
    )
    perms = get_user_module_permissions(user, db, "vendors")
    return templates.TemplateResponse(
        "vendors/detail.html",
        {"request": request, "user": user, "vendor": vendor, "recent_orders": recent_orders, "perms": perms},
    )


@router.get("/{vendor_id}/edit")
def edit_vendor_form(vendor_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("vendors", "edit"))):
    vendor = db.query(Vendor).get(vendor_id)
    return templates.TemplateResponse("vendors/form.html", {"request": request, "user": user, "vendor": vendor})


@router.post("/{vendor_id}/edit")
def update_vendor(
    vendor_id: int,
    name: str = Form(...),
    contact_person: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    gstin: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("vendors", "edit")),
):
    vendor = db.query(Vendor).get(vendor_id)
    vendor.name = name
    vendor.contact_person = contact_person
    vendor.email = email
    vendor.phone = phone
    vendor.address = address
    vendor.gstin = gstin
    db.commit()
    return RedirectResponse(url=f"/vendors/{vendor_id}?success=Vendor updated", status_code=303)


@router.post("/{vendor_id}/delete")
def delete_vendor(vendor_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("vendors", "delete"))):
    vendor = db.query(Vendor).get(vendor_id)
    if vendor:
        db.delete(vendor)
        db.commit()
    return RedirectResponse(url="/vendors?success=Vendor deleted", status_code=303)
