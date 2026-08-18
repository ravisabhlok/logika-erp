from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_admin, hash_password
from app.models import User, Role
from app.audit import diff_fields, log_field_changes, log_action

router = APIRouter(prefix="/users", tags=["users"])
templates = Jinja2Templates(directory="app/templates")


@router.get("")
def list_users(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    users = db.query(User).order_by(User.username).all()
    return templates.TemplateResponse("users/list.html", {"request": request, "user": user, "users": users})


@router.get("/new")
def new_user_form(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    roles = db.query(Role).order_by(Role.name).all()
    return templates.TemplateResponse(
        "users/form.html", {"request": request, "user": user, "edit_user": None, "roles": roles},
    )


@router.post("/new")
def create_user(
    username: str = Form(...),
    password: str = Form(...),
    full_name: str = Form(""),
    account_role: str = Form("staff"),
    permission_role_id: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    username = username.strip()
    if db.query(User).filter(User.username == username).first():
        return RedirectResponse(url=f"/users/new?error=Username '{username}' is already taken", status_code=303)
    if len(password) < 6:
        return RedirectResponse(url="/users/new?error=Password must be at least 6 characters", status_code=303)

    new_user = User(
        username=username,
        password_hash=hash_password(password),
        full_name=full_name,
        role=account_role if account_role in ("admin", "staff") else "staff",
        permission_role_id=int(permission_role_id) if permission_role_id else None,
        is_active=True,
    )
    db.add(new_user)
    db.flush()  # need new_user.id before logging
    log_action(
        db, user, "user", new_user.id, new_user.username, "create",
        summary=f"Created ({new_user.role}, permission role: {new_user.permission_role.name if new_user.permission_role else 'none'})",
    )
    db.commit()
    return RedirectResponse(url="/users?success=User created", status_code=303)


@router.get("/{user_id}/edit")
def edit_user_form(user_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    edit_user = db.query(User).get(user_id)
    roles = db.query(Role).order_by(Role.name).all()
    return templates.TemplateResponse(
        "users/form.html", {"request": request, "user": user, "edit_user": edit_user, "roles": roles},
    )


@router.post("/{user_id}/edit")
def update_user(
    user_id: int,
    full_name: str = Form(""),
    account_role: str = Form("staff"),
    permission_role_id: str = Form(""),
    is_active: bool = Form(False),
    new_password: str = Form(""),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    edit_user = db.query(User).get(user_id)
    if user_id == user.id and not is_active:
        return RedirectResponse(url=f"/users/{user_id}/edit?error=You can't deactivate your own account", status_code=303)
    if user_id == user.id and account_role != "admin":
        return RedirectResponse(url=f"/users/{user_id}/edit?error=You can't remove your own admin access", status_code=303)

    changes = diff_fields(edit_user, {
        "full_name": full_name,
        "role": account_role if account_role in ("admin", "staff") else "staff",
        "permission_role_id": int(permission_role_id) if permission_role_id else None,
        "is_active": is_active,
    })

    edit_user.full_name = full_name
    edit_user.role = account_role if account_role in ("admin", "staff") else "staff"
    edit_user.permission_role_id = int(permission_role_id) if permission_role_id else None
    edit_user.is_active = is_active
    if new_password:
        if len(new_password) < 6:
            return RedirectResponse(url=f"/users/{user_id}/edit?error=Password must be at least 6 characters", status_code=303)
        edit_user.password_hash = hash_password(new_password)
        # Never log the actual password — just that a reset happened.
        changes["password"] = ("(unchanged)", "(reset by admin)")
    log_field_changes(db, user, "user", edit_user.id, edit_user.username, changes)
    db.commit()
    return RedirectResponse(url="/users?success=User updated", status_code=303)
