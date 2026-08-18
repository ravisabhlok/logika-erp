"""Explicit, field-level audit logging — see AuditLog's docstring in
app/models.py for why this is called explicitly from each mutating route
rather than hooked in generically.

Usage in a route, editing an existing record::

    changes = diff_fields(item, {
        "name": name, "purchase_price": purchase_price, "sales_price": sales_price,
        ...
    })
    item.name = name
    item.purchase_price = purchase_price
    item.sales_price = sales_price
    ...
    log_field_changes(db, user, "item", item.id, item.name, changes)
    db.commit()

`diff_fields` must be called BEFORE the new values are assigned onto the
object (it reads the current/old value off the object itself). Both
`log_field_changes` and `log_action` just `db.add()` — they don't call
`db.commit()`, so they compose with whatever the route was already doing
and land in the same transaction as the actual change.

For an event that isn't a field-by-field diff (creating a record, deleting
one, a status transition, receiving a purchase order, completing a
production order, granting/revoking a permission, ...), use `log_action`
instead with a short human-readable summary.
"""
from app.models import AuditLog


def _stringify(value):
    if value is None:
        return None
    return str(value)


def diff_fields(obj, updates: dict) -> dict:
    """Compare `obj`'s current attribute values against a dict of
    prospective new values ({attr_name: new_value}) and return only the
    ones that actually changed, as {attr_name: (old_value, new_value)} —
    the shape `log_field_changes` wants. Call this BEFORE assigning the new
    values onto `obj`."""
    changes = {}
    for field_name, new_value in updates.items():
        old_value = getattr(obj, field_name)
        if old_value != new_value:
            changes[field_name] = (old_value, new_value)
    return changes


def log_field_changes(db, user, entity_type: str, entity_id: int, entity_label: str, changes: dict):
    """`changes` is {field_name: (old_value, new_value)} — typically the
    output of `diff_fields`. One AuditLog row per field. No-op if `changes`
    is empty, so callers don't need to guard an all-blank edit themselves."""
    for field_name, (old_value, new_value) in changes.items():
        db.add(AuditLog(
            user_id=user.id if user else None,
            username=user.username if user else None,
            entity_type=entity_type,
            entity_id=entity_id,
            entity_label=entity_label,
            action="update",
            field_name=field_name,
            old_value=_stringify(old_value),
            new_value=_stringify(new_value),
        ))


def log_action(db, user, entity_type: str, entity_id: int, entity_label: str, action: str, summary: str = None):
    """A single-row audit entry for an event that isn't a field-by-field
    diff. `summary` is a short human-readable note (e.g. "draft -> ordered",
    "2 line item(s), total 24,430.00") stored in `new_value`; `field_name`
    stays null so these are easy to tell apart from log_field_changes rows
    when listing a record's history."""
    db.add(AuditLog(
        user_id=user.id if user else None,
        username=user.username if user else None,
        entity_type=entity_type,
        entity_id=entity_id,
        entity_label=entity_label,
        action=action,
        field_name=None,
        old_value=None,
        new_value=summary,
    ))
