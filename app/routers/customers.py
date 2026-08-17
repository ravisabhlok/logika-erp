from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.auth import require_login, require_module_permission, get_user_module_permissions
from app.formatting import format_inr, format_dt
from app.models import Customer, CustomerAddress, SalesOrder, User

router = APIRouter(prefix="/customers", tags=["customers"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt


@router.get("")
def list_customers(request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("customers", "view"))):
    customers = db.query(Customer).order_by(Customer.name).all()
    perms = get_user_module_permissions(user, db, "customers")
    return templates.TemplateResponse("customers/list.html", {"request": request, "user": user, "customers": customers, "perms": perms})


@router.get("/new")
def new_customer_form(request: Request, user: User = Depends(require_module_permission("customers", "add"))):
    return templates.TemplateResponse("customers/form.html", {"request": request, "user": user, "customer": None})


@router.post("/new")
def create_customer(
    request: Request,
    name: str = Form(...),
    nickname: str = Form(""),
    contact_person: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    country: str = Form(""),
    gstin: str = Form(""),
    category: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("customers", "add")),
):
    customer = Customer(
        name=name, nickname=nickname or None, contact_person=contact_person, email=email,
        phone=phone, address=address, city=city or None, state=state or None,
        country=country or None, gstin=gstin, category=category or None,
    )
    db.add(customer)
    db.commit()
    return RedirectResponse(url=f"/customers/{customer.id}?success=Customer created", status_code=303)


@router.get("/{customer_id}")
def view_customer(customer_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("customers", "view"))):
    customer = db.query(Customer).options(joinedload(Customer.addresses)).get(customer_id)
    recent_orders = (
        db.query(SalesOrder)
        .filter(SalesOrder.customer_id == customer_id)
        .order_by(SalesOrder.order_date.desc())
        .limit(10)
        .all()
    )
    perms = get_user_module_permissions(user, db, "customers")
    return templates.TemplateResponse(
        "customers/detail.html",
        {"request": request, "user": user, "customer": customer, "recent_orders": recent_orders, "perms": perms},
    )


@router.get("/{customer_id}/edit")
def edit_customer_form(customer_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("customers", "edit"))):
    customer = db.query(Customer).get(customer_id)
    return templates.TemplateResponse("customers/form.html", {"request": request, "user": user, "customer": customer})


@router.post("/{customer_id}/edit")
def update_customer(
    customer_id: int,
    name: str = Form(...),
    nickname: str = Form(""),
    contact_person: str = Form(""),
    email: str = Form(""),
    phone: str = Form(""),
    address: str = Form(""),
    city: str = Form(""),
    state: str = Form(""),
    country: str = Form(""),
    gstin: str = Form(""),
    category: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("customers", "edit")),
):
    customer = db.query(Customer).get(customer_id)
    customer.name = name
    customer.nickname = nickname or None
    customer.contact_person = contact_person
    customer.email = email
    customer.phone = phone
    customer.address = address
    customer.city = city or None
    customer.state = state or None
    customer.country = country or None
    customer.gstin = gstin
    customer.category = category or None
    db.commit()
    return RedirectResponse(url=f"/customers/{customer_id}?success=Customer updated", status_code=303)


@router.post("/{customer_id}/delete")
def delete_customer(customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("customers", "delete"))):
    customer = db.query(Customer).get(customer_id)
    if customer:
        db.delete(customer)
        db.commit()
    return RedirectResponse(url="/customers?success=Customer deleted", status_code=303)


# ---------------------------------------------------------------------------
# Saved addresses — see CustomerAddress's docstring. Managed as their own
# small CRUD off the customer detail page, separate from the main
# customer edit form since they're a list of records, not fields on one.
# ---------------------------------------------------------------------------

@router.get("/{customer_id}/addresses")
def customer_addresses_json(customer_id: int, db: Session = Depends(get_db), user: User = Depends(require_login)):
    """Plain JSON list of a customer's saved addresses — fetched by the
    Sales Order form's JS whenever the Bill To or Ship To customer
    selection changes, to populate that picker without preloading every
    customer's addresses onto the page up front. Deliberately stays on plain
    require_login rather than require_module_permission("customers", "view")
    like the rest of this router — a staff user gated into Sales but not
    Customers still needs this to fill in Bill To/Ship To while creating a
    Sales Order; it's a Sales Order form dependency more than "browsing the
    customer list", so it isn't locked behind the customers permission."""
    addresses = (
        db.query(CustomerAddress)
        .filter(CustomerAddress.customer_id == customer_id)
        .order_by(CustomerAddress.id)
        .all()
    )
    return [
        {
            "id": a.id, "label": a.label, "address": a.address,
            "city": a.city, "state": a.state, "country": a.country, "gstin": a.gstin,
        }
        for a in addresses
    ]


@router.get("/{customer_id}/addresses/new")
def new_customer_address_form(customer_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("customers", "add"))):
    customer = db.query(Customer).get(customer_id)
    return templates.TemplateResponse(
        "customers/address_form.html",
        {"request": request, "user": user, "customer": customer, "address": None},
    )


@router.post("/{customer_id}/addresses/new")
def create_customer_address(
    customer_id: int,
    label: str = Form(""),
    address: str = Form(...),
    city: str = Form(""),
    state: str = Form(""),
    country: str = Form(""),
    gstin: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("customers", "add")),
):
    db.add(CustomerAddress(
        customer_id=customer_id, label=label or None, address=address,
        city=city or None, state=state or None, country=country or None, gstin=gstin or None,
    ))
    db.commit()
    return RedirectResponse(url=f"/customers/{customer_id}?success=Address added", status_code=303)


@router.get("/{customer_id}/addresses/{address_id}/edit")
def edit_customer_address_form(customer_id: int, address_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_module_permission("customers", "edit"))):
    customer = db.query(Customer).get(customer_id)
    address = db.query(CustomerAddress).get(address_id)
    return templates.TemplateResponse(
        "customers/address_form.html",
        {"request": request, "user": user, "customer": customer, "address": address},
    )


@router.post("/{customer_id}/addresses/{address_id}/edit")
def update_customer_address(
    customer_id: int,
    address_id: int,
    label: str = Form(""),
    address: str = Form(...),
    city: str = Form(""),
    state: str = Form(""),
    country: str = Form(""),
    gstin: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_module_permission("customers", "edit")),
):
    row = db.query(CustomerAddress).get(address_id)
    row.label = label or None
    row.address = address
    row.city = city or None
    row.state = state or None
    row.country = country or None
    row.gstin = gstin or None
    db.commit()
    return RedirectResponse(url=f"/customers/{customer_id}?success=Address updated", status_code=303)


@router.post("/{customer_id}/addresses/{address_id}/delete")
def delete_customer_address(customer_id: int, address_id: int, db: Session = Depends(get_db), user: User = Depends(require_module_permission("customers", "delete"))):
    row = db.query(CustomerAddress).get(address_id)
    if row:
        db.delete(row)
        db.commit()
    return RedirectResponse(url=f"/customers/{customer_id}?success=Address deleted", status_code=303)
