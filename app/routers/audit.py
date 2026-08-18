from fastapi import APIRouter, Request, Depends, Query
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session, joinedload

from app.database import get_db
from app.auth import require_admin
from app.formatting import format_dt
from app.models import AuditLog, User

router = APIRouter(prefix="/audit", tags=["audit"])
templates = Jinja2Templates(directory="app/templates")
templates.env.filters["dt"] = format_dt

# entity_type -> (label, url template with {id}) for a "View" link back to
# the actual record, where one still exists to link to. Kept here (not on
# AuditLog itself) since it's purely a display concern for this one page —
# a log entry has to remain readable even for a record that's since been
# deleted (see AuditLog's docstring), which a broken link would undermine.
ENTITY_LINKS = {
    "item": ("Item", "/items/{id}"),
    "sales_order": ("Sales Order", "/sales/{id}"),
    "purchase_order": ("Purchase Order", "/purchase/{id}"),
    "user": ("User", "/users/{id}/edit"),
    "role": ("Role", "/roles/{id}/edit"),
}
ENTITY_TYPES = [
    ("item", "Items"),
    ("sales_order", "Sales Orders"),
    ("sales_order_payment_term", "Payment Terms"),
    ("purchase_order", "Purchase Orders"),
    ("item_serial", "Serial Numbers"),
    ("user", "Users"),
    ("role", "Roles"),
]


@router.get("")
def list_audit_log(
    request: Request,
    entity_type: str = Query(default=""),
    q: str = Query(default=""),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    """Who changed what, most recent first. Admin-only — this is the one
    place in the app that shows every user's activity across modules
    (including who granted which permissions), which is more sensitive than
    any single module's own data. Filterable by entity type and by a plain
    text search across the entity label / field name / values, since a
    growing log is only useful if you can actually find the row you're
    looking for."""
    query = db.query(AuditLog).options(joinedload(AuditLog.user))
    if entity_type:
        query = query.filter(AuditLog.entity_type == entity_type)
    if q:
        like = f"%{q}%"
        query = query.filter(
            (AuditLog.entity_label.ilike(like))
            | (AuditLog.field_name.ilike(like))
            | (AuditLog.old_value.ilike(like))
            | (AuditLog.new_value.ilike(like))
            | (AuditLog.username.ilike(like))
        )
    # Capped at the most recent 500 rows — this is a review/lookup screen,
    # not an export; a growing log shouldn't make this page slower to load
    # every month. Narrow with the filters above to find older entries.
    entries = query.order_by(AuditLog.created_at.desc(), AuditLog.id.desc()).limit(500).all()
    return templates.TemplateResponse(
        "audit/list.html",
        {
            "request": request, "user": user, "entries": entries,
            "entity_type": entity_type, "q": q, "entity_types": ENTITY_TYPES,
            "entity_links": ENTITY_LINKS,
        },
    )
