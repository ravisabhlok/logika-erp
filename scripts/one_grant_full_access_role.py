"""
One-time safety net for the permissions rollout.

The Role/RolePermission system has existed for a while but was only
enforced on the Items module (see CLAUDE.md's "Rollout status" note) --
every staff login has had free rein over Sales, Purchase, Inventory,
Customers, Vendors, and Production by default, regardless of Role. Those
modules are now switching to the same enforced gating Items already has --
deny by default, same as get_user_module_permissions() has always
documented. Any staff user with no Role assigned (or a Role missing a row
for a given module) would suddenly lose access to that module the moment
this ships.

This script creates one broad "Full Access (All Modules)" Role -- full
view/add/edit/delete on every module in app.auth.MODULES -- and assigns it
to every existing staff user who doesn't already have a Role. Run it once,
before or right alongside the router changes that turn on enforcement, so
nobody starts from zero. Afterwards, go to /roles at your own pace and
split staff into narrower, per-person roles -- this script only guarantees
a safe starting point, not a final permission design.

Safe to re-run: reuses the "Full Access (All Modules)" Role if it already
exists (won't create a duplicate), and only touches staff users who don't
already have a Role assigned -- anyone already assigned a Role (broad or
narrow) is left exactly as they are.

Run from the project root with:
    python scripts\\one_grant_full_access_role.py
"""
import os
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from app.database import SessionLocal
from app.auth import MODULES
from app.models import Role, RolePermission, User

ROLE_NAME = "Full Access (All Modules)"


def get_or_create_full_access_role(db) -> Role:
    role = db.query(Role).filter(Role.name == ROLE_NAME).first()
    if role:
        print(f"Role '{ROLE_NAME}' already exists (id={role.id}) -- reusing it.")
        return role

    role = Role(name=ROLE_NAME)
    role.permissions = [
        RolePermission(module=key, can_view=True, can_add=True, can_edit=True, can_delete=True)
        for key, _ in MODULES
    ]
    db.add(role)
    db.flush()
    print(f"Created role '{ROLE_NAME}' (id={role.id}) with full access to: {', '.join(k for k, _ in MODULES)}")
    return role


def assign_to_unassigned_staff(db, role: Role) -> None:
    staff_without_role = (
        db.query(User)
        .filter(User.role == "staff", User.permission_role_id.is_(None))
        .all()
    )
    if not staff_without_role:
        print("No staff users without a Role -- nothing to assign.")
        return
    for u in staff_without_role:
        u.permission_role_id = role.id
        print(f"Assigned '{ROLE_NAME}' to staff user: {u.username}")


if __name__ == "__main__":
    db = SessionLocal()
    try:
        full_access_role = get_or_create_full_access_role(db)
        assign_to_unassigned_staff(db, full_access_role)
        db.commit()
        print("Done. Any staff user who already had a Role assigned was left untouched.")
    finally:
        db.close()
