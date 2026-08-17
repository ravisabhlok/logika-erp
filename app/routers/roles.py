from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_admin, MODULES
from app.models import Role, RolePermission, User

router = APIRouter(prefix="/roles", tags=["roles"])
templates = Jinja2Templates(directory="app/templates")


async def build_permissions(request: Request) -> list[RolePermission]:
    """Parse the module x action checkbox matrix (fields named
    perm_<module>_<action>) into RolePermission rows. Modules with every
    box unchecked are skipped entirely — no row means no access anyway,
    same as get_user_module_permissions' default-deny behaviour."""
    form = await request.form()
    permissions = []
    for module_key, _ in MODULES:
        flags = {
            action: form.get(f"perm_{module_key}_{action}") == "on"
            for action in ("view", "add", "edit", "delete")
        }
        if any(flags.values()):
            permissions.append(RolePermission(
                module=module_key,
                can_view=flags["view"], can_add=flags["add"],
                can_edit=flags["edit"], can_delete=flags["delete"],
            ))
    return permissions


@router.get("")
def list_roles(request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    roles = db.query(Role).order_by(Role.name).all()
    return templates.TemplateResponse("roles/list.html", {"request": request, "user": user, "roles": roles})


@router.get("/new")
def new_role_form(request: Request, user: User = Depends(require_admin)):
    return templates.TemplateResponse(
        "roles/form.html",
        {"request": request, "user": user, "role": None, "modules": MODULES, "perm_map": {}},
    )


@router.post("/new")
async def create_role(
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    role = Role(name=name.strip())
    role.permissions = await build_permissions(request)
    db.add(role)
    db.commit()
    return RedirectResponse(url="/roles?success=Role created", status_code=303)


@router.get("/{role_id}/edit")
def edit_role_form(role_id: int, request: Request, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    role = db.query(Role).get(role_id)
    perm_map = {p.module: p for p in role.permissions} if role else {}
    return templates.TemplateResponse(
        "roles/form.html",
        {"request": request, "user": user, "role": role, "modules": MODULES, "perm_map": perm_map},
    )


@router.post("/{role_id}/edit")
async def update_role(
    role_id: int,
    request: Request,
    name: str = Form(...),
    db: Session = Depends(get_db),
    user: User = Depends(require_admin),
):
    role = db.query(Role).get(role_id)
    role.name = name.strip()
    new_permissions = await build_permissions(request)
    # Delete the old rows and flush before inserting the new ones — replacing
    # `role.permissions` in one shot can make SQLAlchemy try to insert the
    # replacements before the delete-orphans flush, tripping the
    # (role_id, module) unique constraint since both rows briefly coexist.
    db.query(RolePermission).filter(RolePermission.role_id == role_id).delete()
    db.flush()
    role.permissions = new_permissions
    db.commit()
    return RedirectResponse(url="/roles?success=Role updated", status_code=303)


@router.post("/{role_id}/delete")
def delete_role(role_id: int, db: Session = Depends(get_db), user: User = Depends(require_admin)):
    role = db.query(Role).get(role_id)
    if role:
        assigned = db.query(User).filter(User.permission_role_id == role_id).all()
        if assigned:
            names = ", ".join(u.username for u in assigned)
            return RedirectResponse(
                url=f"/roles?error=Can't delete '{role.name}' — still assigned to: {names}. Reassign them first.",
                status_code=303,
            )
        db.delete(role)
        db.commit()
    return RedirectResponse(url="/roles?success=Role deleted", status_code=303)
