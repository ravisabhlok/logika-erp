"""
Shared "what needs to be purchased" computation.

Used by the company-wide requirements page (app/routers/purchase.py) — the
single source of truth for "what do I need to order" across every open
sales order plus each item's minimum-stock (reorder_level) buffer. Kept
here rather than inside a router module because it doesn't belong to one
module's data any more than the other: it reads Sales demand, Item BOM/
reorder data, and Purchase order-in-progress data all at once.

See CLAUDE.md for the underlying data model (BOM components, reorder_level,
PurchaseOrder statuses) this walks.
"""
from sqlalchemy.orm import Session, joinedload
from sqlalchemy import func

from app.models import Item, PurchaseOrder, PurchaseOrderItem, SalesOrder, SalesOrderItem


def compute_demand_map(db: Session):
    """Builds the same {item_id: qty} demand map the company-wide "What to
    Order" page computes (app/routers/purchase.py:compute_global_requirements)
    — every open ('draft' or 'confirmed') sales order line, plus every
    item's own reorder_level seeded in so pure minimum-stock replenishment
    is included too — then explodes it down through BOMs via
    `explode_demand`, so a raw component's number includes demand pushed
    down from a parent assembly's open sales orders, not just sales order
    lines against that component directly.

    Pulled out into its own function (rather than living only inside
    compute_global_requirements) so any other place that needs "how much
    of this item is actually demanded right now" — e.g. the item detail
    page's Sales Demand figure — computes it the same way and always
    agrees with this page's numbers, instead of a narrower recomputation
    that only looks at direct sales order lines for one item and quietly
    misses BOM-driven demand (see CLAUDE.md's note on requirements being a
    single shared computation, not a per-view one that can disagree).

    Returns (demand, sources, exploded): `demand`/`sources` are the same
    dicts `explode_demand` mutates in place; `exploded` is the set of
    assembly item_ids whose own shortfall got pushed down into components
    instead of staying on the assembly's own row.
    """
    open_lines = (
        db.query(SalesOrderItem)
        .join(SalesOrder, SalesOrder.id == SalesOrderItem.sales_order_id)
        .options(joinedload(SalesOrderItem.sales_order))
        .filter(SalesOrder.status.in_(["draft", "confirmed"]))
        .all()
    )

    demand = {}
    sources = {}  # item_id -> set of sales order numbers driving its demand
    for line in open_lines:
        demand[line.item_id] = demand.get(line.item_id, 0.0) + float(line.quantity)
        sources.setdefault(line.item_id, set()).add(line.sales_order.order_no)

    reorder_items = db.query(Item).filter(Item.reorder_level > 0, Item.is_active == True).all()  # noqa: E712
    for item in reorder_items:
        demand.setdefault(item.id, 0.0)
        sources.setdefault(item.id, set())

    exploded = explode_demand(db, demand, sources, include_reorder_buffer=True)
    return demand, sources, exploded


def item_sales_demand(db: Session, item_id: int) -> float:
    """Single-item convenience wrapper around compute_demand_map — the same
    number that item would show in its "Sales Demand" row on the
    company-wide requirements page (0 if it isn't on that page at all,
    e.g. nothing currently demands it). Includes demand flowing down from
    a parent assembly's open sales orders through the BOM, not just sales
    order lines against this item directly."""
    demand, _sources, _exploded = compute_demand_map(db)
    return demand.get(item_id, 0.0)


def on_order_qty(db: Session, item_id: int) -> float:
    """Quantity of `item_id` already placed with a vendor (PurchaseOrder
    status 'ordered') but not yet received — counts toward covering a
    shortfall without needing to be ordered again."""
    total = (
        db.query(func.sum(PurchaseOrderItem.quantity))
        .join(PurchaseOrder, PurchaseOrder.id == PurchaseOrderItem.purchase_order_id)
        .filter(PurchaseOrderItem.item_id == item_id, PurchaseOrder.status == "ordered")
        .scalar()
    )
    return float(total or 0)


def last_vendor(db: Session, item_id: int):
    """Vendor from the most recent purchase order (any status) that included
    this item — a purchasing hint, not a commitment."""
    row = (
        db.query(PurchaseOrder)
        .join(PurchaseOrderItem, PurchaseOrderItem.purchase_order_id == PurchaseOrder.id)
        .filter(PurchaseOrderItem.item_id == item_id)
        .order_by(PurchaseOrder.order_date.desc())
        .first()
    )
    return row.vendor if row else None


def explode_demand(db: Session, demand: dict, sources: dict, include_reorder_buffer: bool) -> set:
    """Walks a starting {item_id: qty} demand map down through BOMs.

    For every `is_assembly` item with a defined BOM, checks whether its own
    current stock covers what's demanded of it (plus its reorder_level
    buffer, if `include_reorder_buffer`); any shortfall is pushed down as
    demand on its components instead of leaving the assembly itself on the
    "to buy" list — components can themselves be assemblies, so this
    repeats until only non-assembly (or BOM-less) items are left. A
    component shared by more than one assembly, or that's also on a sales
    order directly, accumulates all of that demand in `demand` before it's
    ever netted against stock, so it isn't under- or double-counted.

    `sources` is a parallel {item_id: set(...)} map of provenance labels
    (e.g. sales order numbers) — when demand flows from an assembly down
    into its components, the assembly's labels are copied onto each
    component too, so a raw part's row can still show which order(s) are
    ultimately driving it. Both dicts are mutated in place.

    Returns the set of item_ids that got exploded — these should be
    excluded from the final "buy this" list, since their demand was pushed
    down into components instead.

    Not guarded against a genuine circular BOM (A needs B needs A) beyond
    not re-exploding the same item twice — matches the rest of this app's
    BOM support (see production.py / CLAUDE.md), which doesn't detect
    deeper cycles either.
    """
    exploded = set()
    to_process = list(demand.keys())
    while to_process:
        item_id = to_process.pop(0)
        if item_id in exploded:
            continue
        item = db.query(Item).get(item_id)
        if not item:
            continue
        buffer = float(item.reorder_level or 0) if include_reorder_buffer else 0.0
        required = demand.get(item_id, 0.0) + buffer
        if item.is_assembly and item.bom_components:
            exploded.add(item_id)
            in_stock = float(item.current_stock or 0)
            shortfall = max(0.0, required - in_stock)
            if shortfall > 0:
                for comp in item.bom_components:
                    add_qty = float(comp.quantity) * shortfall
                    demand[comp.component_item_id] = demand.get(comp.component_item_id, 0.0) + add_qty
                    sources.setdefault(comp.component_item_id, set()).update(sources.get(item_id, set()))
                    to_process.append(comp.component_item_id)
    return exploded
