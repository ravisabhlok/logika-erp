"""Confidential parties and items.

A Customer / Vendor / Payee / Item can be marked `is_confidential` (admin only). Admins and staff whose Role has
`can_view_confidential` (e.g. the Auditor / CA role) see everything; every other user must not see a confidential record
or anything that belongs to it (orders, bills, payments, ledger ...).

Keep every "can this user see it?" decision in this module so future refinements (e.g. party groups) change one place.
"""
from fastapi import Depends, Request
from sqlalchemy.orm import Session

from app.auth import PermissionDeniedException, require_login
from app.database import get_db
from app.models import Role, User


def can_see_confidential(user: User, db: Session) -> bool:
    if user.role == "admin":
        return True
    if not user.permission_role_id:
        return False
    role = db.get(Role, user.permission_role_id)
    return bool(role and role.can_view_confidential)


def visible(query, model, user: User, db: Session):
    """Filter a query on a model with `is_confidential` so users without access never get confidential rows."""
    if can_see_confidential(user, db):
        return query
    return query.filter(model.is_confidential == False)  # noqa: E712


def is_visible(obj, user: User, db: Session) -> bool:
    return obj is None or not getattr(obj, "is_confidential", False) or can_see_confidential(user, db)


def require_visible(obj, user: User, db: Session) -> None:
    if not is_visible(obj, user, db):
        raise PermissionDeniedException("confidential records", "view")


def guard_path_param(param: str, model):
    """Router-level dependency: any route whose path has `{param}` (e.g. customer_id) refuses a confidential record
    for a user who can't see confidential data. Covers every current and future route on that router."""
    def dependency(request: Request, user: User = Depends(require_login), db: Session = Depends(get_db)) -> None:
        raw = request.path_params.get(param)
        if raw is None:
            return
        try:
            obj = db.get(model, int(raw))
        except (TypeError, ValueError):
            return
        require_visible(obj, user, db)
    return dependency


# ---------------------------------------------------------------------------
# Documents (orders, invoices, bills ...) follow their party and their items:
# a document is confidential when its customer / vendor is, or when any of its
# line items is. Each clause below is a SQL condition that is TRUE for a
# confidential document, so lists use `~clause` and guards use `clause`.
# ---------------------------------------------------------------------------
from sqlalchemy import and_, or_, select  # noqa: E402

from app.models import (  # noqa: E402
    Bill, BillItem, Customer, Invoice, InvoiceItem, Item, Payee, ProductionOrder, ProductionOrderComponent,
    ExpenseEntry, ExpenseTemplate, PaymentBatch, PaymentLine,
    PurchaseOrder, PurchaseOrderItem, SalesOrder, SalesOrderItem, Vendor,
)


def _conf_sales_order():
    return or_(
        SalesOrder.customer.has(Customer.is_confidential == True),  # noqa: E712
        SalesOrder.ship_to_customer.has(Customer.is_confidential == True),  # noqa: E712
        SalesOrder.items.any(SalesOrderItem.item.has(Item.is_confidential == True)),  # noqa: E712
    )


def _conf_invoice():
    return or_(
        Invoice.sales_order.has(_conf_sales_order()),
        Invoice.items.any(InvoiceItem.item.has(Item.is_confidential == True)),  # noqa: E712
    )


def _conf_purchase_order():
    return or_(
        PurchaseOrder.vendor.has(Vendor.is_confidential == True),  # noqa: E712
        PurchaseOrder.items.any(PurchaseOrderItem.item.has(Item.is_confidential == True)),  # noqa: E712
    )


def _conf_bill():
    return or_(
        Bill.vendor.has(Vendor.is_confidential == True),  # noqa: E712
        Bill.items.any(BillItem.item.has(Item.is_confidential == True)),  # noqa: E712
        Bill.purchase_order.has(_conf_purchase_order()),
    )


def _conf_production():
    return or_(
        ProductionOrder.item.has(Item.is_confidential == True),  # noqa: E712
        ProductionOrder.components.any(ProductionOrderComponent.component_item.has(Item.is_confidential == True)),  # noqa: E712
    )


def _conf_payment_line():
    """A bank payment is confidential when its payee / vendor is, or when the bill / PO it pays is."""
    return or_(
        PaymentLine.payee_id.in_(select(Payee.id).where(Payee.is_confidential == True)),  # noqa: E712
        PaymentLine.vendor_id.in_(select(Vendor.id).where(Vendor.is_confidential == True)),  # noqa: E712
        and_(PaymentLine.source_type == "BILL", PaymentLine.source_id.in_(select(Bill.id).where(_conf_bill()))),
        and_(PaymentLine.source_type == "PO_ADVANCE",
             PaymentLine.source_id.in_(select(PurchaseOrder.id).where(_conf_purchase_order()))),
    )


def _conf_payment_batch():
    """A batch holding any confidential payment is hidden as a whole (its bank file would show them)."""
    return PaymentBatch.lines.any(_conf_payment_line())


CONFIDENTIAL_CLAUSE = {
    PaymentLine: _conf_payment_line,
    PaymentBatch: _conf_payment_batch,
    ExpenseEntry: lambda: ExpenseEntry.payee.has(Payee.is_confidential == True),  # noqa: E712
    ExpenseTemplate: lambda: ExpenseTemplate.payee.has(Payee.is_confidential == True),  # noqa: E712
    SalesOrder: _conf_sales_order,
    Invoice: _conf_invoice,
    PurchaseOrder: _conf_purchase_order,
    Bill: _conf_bill,
    ProductionOrder: _conf_production,
    Customer: lambda: Customer.is_confidential == True,  # noqa: E712
    Vendor: lambda: Vendor.is_confidential == True,  # noqa: E712
    Item: lambda: Item.is_confidential == True,  # noqa: E712
    Payee: lambda: Payee.is_confidential == True,  # noqa: E712
}


def visible_docs(query, model, user: User, db: Session):
    """Like `visible`, but for documents that are confidential through their party or items."""
    if can_see_confidential(user, db):
        return query
    return query.filter(~CONFIDENTIAL_CLAUSE[model]())


def guard_doc(model, param: str):
    """Router-level dependency: a route with `{param}` in its path (or ?param=) is refused when that record is
    confidential and the user can't see confidential data."""
    def dependency(request: Request, user: User = Depends(require_login), db: Session = Depends(get_db)) -> None:
        raw = request.path_params.get(param) or request.query_params.get(param)
        if not raw:
            return
        try:
            record_id = int(raw)
        except (TypeError, ValueError):
            return
        if can_see_confidential(user, db):
            return
        if db.query(model.id).filter(model.id == record_id, CONFIDENTIAL_CLAUSE[model]()).first():
            raise PermissionDeniedException("confidential records", "view")
    return dependency
