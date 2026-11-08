import re
from urllib.parse import quote

from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.audit import log_action
from app.auth import require_module_permission, get_user_module_permissions
from app.formatting import format_inr, format_dt
from app.models import Vendor, PurchaseOrder, User
from app.payments_service import mask

router = APIRouter(prefix="/vendors", tags=["vendors"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt
templates.env.globals["mask"] = mask


def _apply_bank_details(vendor: Vendor, bank_name: str, account_no: str, ifsc: str):
    """Validate and set the vendor's bank details (caller has checked the user is an admin).
    Returns an error message, or None when saved onto the object."""
    account = re.sub(r"[^0-9A-Za-z]", "", account_no or "")
    code = re.sub(r"[^0-9A-Za-z]", "", ifsc or "").upper()
    if code and not re.fullmatch(r"[A-Z]{4}0[A-Z0-9]{6}", code):
        return "IFSC must be 11 characters, like SBIN0001234"
    if account and not re.fullmatch(r"[0-9A-Za-z]{5,30}", account):
        return "Account number must be 5 to 30 letters/digits"
    vendor.bank_name = (bank_name or "").strip()[:60] or None
    vendor.bank_account_no = account or None
    vendor.bank_ifsc = code or None
    return None


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
    bank_name: str = Form(""),
    bank_account_no: str = Form(""),
    bank_ifsc: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("vendors", "add")),
):
    vendor = Vendor(
        name=name, contact_person=contact_person, email=email,
        phone=phone, address=address, gstin=gstin,
    )
    if user.role == "admin":  # bank details: admin only
        error = _apply_bank_details(vendor, bank_name, bank_account_no, bank_ifsc)
        if error:
            return RedirectResponse(url=f"/vendors/new?error={quote(error)}", status_code=303)
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
    bank_name: str = Form(""),
    bank_account_no: str = Form(""),
    bank_ifsc: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("vendors", "edit")),
):
    vendor = db.query(Vendor).get(vendor_id)
    if user.role == "admin":  # bank details: admin only, audited (old/new masked)
        old = f"{mask(vendor.bank_account_no)} {vendor.bank_ifsc or ''}".strip()
        error = _apply_bank_details(vendor, bank_name, bank_account_no, bank_ifsc)
        if error:
            return RedirectResponse(url=f"/vendors/{vendor_id}/edit?error={quote(error)}", status_code=303)
        new = f"{mask(vendor.bank_account_no)} {vendor.bank_ifsc or ''}".strip()
        if old != new:
            log_action(db, user, "vendor", vendor.id, vendor.name, "bank_details", summary=f"Bank details changed from '{old}' to '{new}'")
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
