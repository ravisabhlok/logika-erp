from fastapi import APIRouter, Request, Depends, Form
from fastapi.responses import RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from app.database import get_db
from app.auth import require_admin, MODULES, ACTIONS
from app.models import Role, RolePermission, User
from app.audit import log_action, log_field_changes

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
            for action in ACTIONS
        }
        if any(flags.values()):
            permissions.append(RolePermission(
                module=module_key,
                can_view=flags["view"], can_add=flags["add"],
                can_edit=flags["edit"], can_delete=flags["delete"],
                can_confirm=flags["confirm"],
            ))
    return permissions


def _perm_flags(perm: RolePermission) -> str:
    """'view,add,edit,delete,confirm' -> just the ones that are actually on,
    e.g. 'view,edit', or 'none' if the module has no access at all."""
    flags = [
        name for name, on in (
            ("view", perm.can_view), ("add", perm.can_add), ("edit", perm.can_edit),
            ("delete", perm.can_delete), ("confirm", perm.can_confirm),
        ) if on
    ]
    return ",".join(flags) if flags else "none"


def _perm_map(permissions: list[RolePermission]) -> dict:
    return {p.module: p for p in permissions}


def _diff_permissions(old_permissions: list[RolePermission], new_permissions: list[RolePermission]) -> dict:
    """{module: (old_flags_str, new_flags_str)} for every module whose
    access actually changed between the two permission sets — a module
    missing from a list means no access at all for that module, same as
    get_user_module_permissions' default-deny behaviour."""
    old_map = _perm_map(old_permissions)
    new_map = _perm_map(new_permissions)
    changes = {}
    for module_key, module_label in MODULES:
        old_str = _perm_flags(old_map[module_key]) if module_key in old_map else "none"
        new_str = _perm_flags(new_map[module_key]) if module_key in new_map else "none"
        if old_str != new_str:
            changes[f"permissions.{module_key}"] = (old_str, new_str)
    return changes


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
    db.flush()  # need role.id before logging
    log_action(db, user, "role", role.id, role.name, "create", summary=f"Created with {len(role.permissions)} module permission(s) set")
    log_field_changes(db, user, "role", role.id, role.name, _diff_permissions([], role.permissions))
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
    old_name = role.name
    old_permissions = [
        RolePermission(
            module=p.module, can_view=p.can_view, can_add=p.can_add, can_edit=p.can_edit,
            can_delete=p.can_delete, can_confirm=p.can_confirm,
        )
        for p in role.permissions
    ]  # detached copies — role.permissions itself is about to be deleted below
    role.name = name.strip()
    new_permissions = await build_permissions(request)
    # Delete the old rows and flush before inserting the new ones — replacing
    # `role.permissions` in one shot can make SQLAlchemy try to insert the
    # replacements before the delete-orphans flush, tripping the
    # (role_id, module) unique constraint since both rows briefly coexist.
    db.query(RolePermission).filter(RolePermission.role_id == role_id).delete()
    db.flush()
    role.permissions = new_permissions
    if role.name != old_name:
        log_field_changes(db, user, "role", role.id, role.name, {"name": (old_name, role.name)})
    log_field_changes(db, user, "role", role.id, role.name, _diff_permissions(old_permissions, new_permissions))
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
        log_action(db, user, "role", role.id, role.name, "delete", summary=f"Deleted '{role.name}'")
        db.delete(role)
        db.commit()
    return RedirectResponse(url="/roles?success=Role deleted", status_code=303)
