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
