"""
Simple session-based authentication (cookie session via Starlette's
SessionMiddleware, not JWT) — appropriate for a LAN-only internal app.
"""
import bcrypt
from fastapi import Request, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import User, RolePermission

# Every module this app currently knows how to gate. A plain Python list
# (not a DB enum) so adding a new one is a one-line change here, not a
# migration — see Role/RolePermission in models.py. Only "items" is actually
# wired into its router's routes so far (see app/routers/items.py); the
# rest are listed here so they show up in the Roles admin UI ready to be
# switched on module-by-module as each one gets the same treatment.
MODULES = [
    ("customers", "Customers"),
    ("vendors", "Vendors"),
    ("items", "Items"),
    ("sales", "Sales"),
    ("purchase", "Purchase"),
    ("inventory", "Inventory"),
    ("production", "Production"),
]
MODULE_KEYS = [key for key, _ in MODULES]

NO_ACCESS = {"view": False, "add": False, "edit": False, "delete": False}
FULL_ACCESS = {"view": True, "add": True, "edit": True, "delete": True}


class NotAuthenticatedException(Exception):
    """Raised by require_login when there is no active session; main.py
    registers a handler that turns this into a redirect to /login."""


def hash_password(password: str) -> str:
    return bcrypt.hashpw(password.encode("utf-8"), bcrypt.gensalt()).decode("utf-8")


def verify_password(password: str, password_hash: str) -> bool:
    return bcrypt.checkpw(password.encode("utf-8"), password_hash.encode("utf-8"))


def get_current_user(request: Request, db: Session = Depends(get_db)):
    user_id = request.session.get("user_id")
    if not user_id:
        return None
    return db.query(User).filter(User.id == user_id, User.is_active == True).first()  # noqa: E712


def require_login(request: Request, db: Session = Depends(get_db)):
    """Dependency for protected routes: redirects to /login if not authenticated."""
    user = get_current_user(request, db)
    if not user:
        raise NotAuthenticatedException()
    return user


def require_admin(user: User = Depends(require_login)):
    if user.role != "admin":
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Admins only")
    return user


class PermissionDeniedException(Exception):
    """Raised by require_module_permission when a logged-in staff user lacks
    the needed permission. main.py registers a handler that renders a plain
    403 page instead of FastAPI's default JSON error."""

    def __init__(self, module_label: str, action: str):
        self.module_label = module_label
        self.action = action


def get_user_module_permissions(user: User, db: Session, module: str) -> dict:
    """What `user` can do in `module`: {"view": bool, "add": bool, "edit": bool,
    "delete": bool}. Admins always get full access. A staff user with no role
    assigned, or a role with no row for this module, gets no access — denied
    by default, rather than accidentally open."""
    if user.role == "admin":
        return dict(FULL_ACCESS)
    if not user.permission_role_id:
        return dict(NO_ACCESS)
    perm = (
        db.query(RolePermission)
        .filter(RolePermission.role_id == user.permission_role_id, RolePermission.module == module)
        .first()
    )
    if not perm:
        return dict(NO_ACCESS)
    return {"view": perm.can_view, "add": perm.can_add, "edit": perm.can_edit, "delete": perm.can_delete}


def require_module_permission(module: str, action: str):
    """Dependency factory: `Depends(require_module_permission("items", "edit"))`.
    Use in place of `Depends(require_login)` on a route once that module has
    been wired up for permission gating (see MODULES above)."""
    module_label = dict(MODULES).get(module, module)

    def dependency(user: User = Depends(require_login), db: Session = Depends(get_db)) -> User:
        perms = get_user_module_permissions(user, db, module)
        if not perms.get(action):
            raise PermissionDeniedException(module_label, action)
        return user

    return dependency
