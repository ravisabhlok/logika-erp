from fastapi import APIRouter, Request, Depends
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session
from sqlalchemy import func

from app.database import get_db
from app.auth import require_login
from app.formatting import format_qty, format_inr, format_dt
from app.models import Item, SalesOrder, PurchaseOrder, Customer, Vendor, User
from app.routers.sales import compute_payments_due

router = APIRouter(tags=["dashboard"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["qty"] = format_qty
templates.env.filters["inr"] = format_inr
templates.env.filters["dt"] = format_dt


@router.get("/")
def dashboard(request: Request, db: Session = Depends(get_db), user: User = Depends(require_login)):
    total_customers = db.query(func.count(Customer.id)).scalar()
    total_vendors = db.query(func.count(Vendor.id)).scalar()
    total_items = db.query(func.count(Item.id)).scalar()

    low_stock_items = (
        db.query(Item)
        .filter(Item.is_active == True, Item.current_stock <= Item.reorder_level)  # noqa: E712
        .order_by(Item.current_stock.asc())
        .limit(10)
        .all()
    )

    recent_sales = db.query(SalesOrder).order_by(SalesOrder.created_at.desc()).limit(5).all()
    recent_purchases = db.query(PurchaseOrder).order_by(PurchaseOrder.created_at.desc()).limit(5).all()

    open_sales = db.query(func.count(SalesOrder.id)).filter(SalesOrder.status.in_(["draft", "confirmed"])).scalar()
    open_purchases = db.query(func.count(PurchaseOrder.id)).filter(PurchaseOrder.status.in_(["draft", "ordered"])).scalar()

    payments_due_rows = compute_payments_due(db)[:5]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "user": user,
            "total_customers": total_customers,
            "total_vendors": total_vendors,
            "total_items": total_items,
            "low_stock_items": low_stock_items,
            "recent_sales": recent_sales,
            "recent_purchases": recent_purchases,
            "open_sales": open_sales,
            "open_purchases": open_purchases,
            "payments_due_rows": payments_due_rows,
        },
    )
