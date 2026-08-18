"""
SQLAlchemy ORM models for the Logika Systems ERP.

Modules covered: Auth/Users, Company, Master data (Customers, Vendors,
Categories, Items), Sales, Purchase, Inventory (stock transactions).
"""
from datetime import datetime

from sqlalchemy import (
    Column, Integer, String, Text, DateTime, Numeric, ForeignKey,
    Boolean, Enum, UniqueConstraint, Table,
)
from sqlalchemy.orm import relationship

from app.database import Base


# ---------------------------------------------------------------------------
# Company / Users
# ---------------------------------------------------------------------------

class Company(Base):
    __tablename__ = "company"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False, default="Logika Systems India Pvt Ltd")
    address = Column(Text)
    gstin = Column(String(20))
    phone = Column(String(30))
    email = Column(String(120))


class User(Base):
    __tablename__ = "users"

    id = Column(Integer, primary_key=True)
    username = Column(String(80), unique=True, nullable=False, index=True)
    password_hash = Column(String(255), nullable=False)
    full_name = Column(String(150))
    role = Column(Enum("admin", "staff", name="user_role"), default="staff", nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    # Fine-grained per-module permissions for staff users (see Role/RolePermission
    # below). Admins bypass this entirely and always have full access, so this is
    # only consulted when role == "staff". Deliberately a separate column from
    # the coarse admin/staff `role` enum above rather than repurposing it, so the
    # simple admin/staff switch and the fine-grained permission matrix don't get
    # tangled together.
    permission_role_id = Column(Integer, ForeignKey("roles.id"), nullable=True)
    permission_role = relationship("Role", back_populates="users")


class Role(Base):
    """A named, reusable set of per-module permissions (e.g. 'Sales Staff',
    'Warehouse Staff') that a staff User can be assigned to. Admin users don't
    need one — they always have full access regardless of this table."""
    __tablename__ = "roles"

    id = Column(Integer, primary_key=True)
    name = Column(String(100), unique=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    users = relationship("User", back_populates="permission_role")
    permissions = relationship("RolePermission", back_populates="role", cascade="all, delete-orphan")


class RolePermission(Base):
    """One row per (role, module): what a role can do in that module. `module`
    is a plain string (not a DB enum) — see app.auth.MODULES for the list this
    app currently understands — so adding a new gated module later doesn't
    require an Alembic migration just to widen an enum."""
    __tablename__ = "role_permissions"
    __table_args__ = (UniqueConstraint("role_id", "module", name="uq_role_module"),)

    id = Column(Integer, primary_key=True)
    role_id = Column(Integer, ForeignKey("roles.id"), nullable=False)
    module = Column(String(50), nullable=False)
    can_view = Column(Boolean, default=False, nullable=False)
    can_add = Column(Boolean, default=False, nullable=False)
    can_edit = Column(Boolean, default=False, nullable=False)
    can_delete = Column(Boolean, default=False, nullable=False)

    role = relationship("Role", back_populates="permissions")


# ---------------------------------------------------------------------------
# Master data
# ---------------------------------------------------------------------------

class Customer(Base):
    __tablename__ = "customers"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    nickname = Column(String(100), nullable=True)
    contact_person = Column(String(150))
    email = Column(String(120))
    phone = Column(String(50))
    address = Column(Text)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    gstin = Column(String(20))
    category = Column(Enum("OEM", "Enduser", "Dealer", name="customer_category"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sales_orders = relationship("SalesOrder", back_populates="customer", foreign_keys="SalesOrder.customer_id")
    addresses = relationship(
        "CustomerAddress", back_populates="customer",
        cascade="all, delete-orphan", order_by="CustomerAddress.id",
    )


class CustomerAddress(Base):
    """A saved address for a customer beyond the single `Customer.address`
    field — real customers often have more than one location (head office
    vs a warehouse, or a separate site per project), and a Sales Order's
    Bill To / Ship To should be able to pick from whichever of these
    applies rather than retyping it every time. `label` is a free-text
    identifier (e.g. "Head Office", "Warehouse - Pune") purely to tell
    addresses apart in the picker — not a controlled type like billing vs
    shipping, since either address can be used for either purpose on a
    given order. `gstin` is separate from `Customer.gstin` since a
    customer with multiple state registrations legitimately has a
    different GSTIN per address in India.

    Sales Order's `billing_address`/`shipping_address` are plain text
    snapshots (not a FK to a row here) — same reasoning as
    SalesOrderItem.unit_price snapshotting Item.sales_price: an order
    should keep whatever address text applied when it was placed, not
    silently change if this record is later edited or deleted, and it
    also needs to support a one-off address that was never worth saving
    here at all.
    """
    __tablename__ = "customer_addresses"

    id = Column(Integer, primary_key=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    label = Column(String(100), nullable=True)
    address = Column(Text, nullable=False)
    city = Column(String(100), nullable=True)
    state = Column(String(100), nullable=True)
    country = Column(String(100), nullable=True)
    gstin = Column(String(20), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="addresses")


class Vendor(Base):
    __tablename__ = "vendors"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    contact_person = Column(String(150))
    email = Column(String(120))
    phone = Column(String(30))
    address = Column(Text)
    gstin = Column(String(20))
    created_at = Column(DateTime, default=datetime.utcnow)

    purchase_orders = relationship("PurchaseOrder", back_populates="vendor")


class Category(Base):
    """A tag-like label (e.g. 'Sensor', 'Spare', 'Cable'). An item can carry
    several of these at once so it can show up in more than one report."""
    __tablename__ = "categories"

    id = Column(Integer, primary_key=True)
    name = Column(String(120), unique=True, nullable=False)


item_categories = Table(
    "item_categories",
    Base.metadata,
    Column("item_id", Integer, ForeignKey("items.id"), primary_key=True),
    Column("category_id", Integer, ForeignKey("categories.id"), primary_key=True),
)


class Item(Base):
    __tablename__ = "items"

    id = Column(Integer, primary_key=True)
    name = Column(String(200), nullable=False)
    description = Column(Text, nullable=True)
    small_description = Column(String(255), nullable=True)
    unit = Column(String(20), default="Nos")
    hsn_code = Column(String(10), nullable=True)
    gst_percentage = Column(Numeric(5, 2), nullable=True, default=0)
    purchase_price = Column(Numeric(12, 2), default=0)
    sales_price = Column(Numeric(12, 2), default=0)
    reorder_level = Column(Numeric(14, 4), default=0)
    current_stock = Column(Numeric(14, 4), default=0, nullable=False)
    has_serial = Column(Boolean, default=False, nullable=False)
    is_assembly = Column(Boolean, default=False, nullable=False)
    is_active = Column(Boolean, default=True, nullable=False)
    created_at = Column(DateTime, default=datetime.utcnow)

    categories = relationship("Category", secondary=item_categories, backref="items")
    stock_transactions = relationship("StockTransaction", back_populates="item")
    bom_components = relationship(
        "BomComponent", foreign_keys="BomComponent.item_id",
        back_populates="item", cascade="all, delete-orphan",
        order_by="BomComponent.id",
    )
    # Reverse of bom_components: BOM rows on *other* items that list this
    # item as a component — i.e. "what is this item used to build". Read-only
    # from this side (no cascade) since deleting this item shouldn't delete
    # the assemblies that use it, just their (now-dangling) component rows —
    # same as any other FK; nothing currently blocks deleting a component
    # that's in use elsewhere.
    used_in_boms = relationship(
        "BomComponent", foreign_keys="BomComponent.component_item_id",
        back_populates="component_item", order_by="BomComponent.id",
    )
    attachments = relationship(
        "ItemAttachment", back_populates="item",
        cascade="all, delete-orphan", order_by="ItemAttachment.id",
    )


class ItemAttachment(Base):
    """A picture or PDF attached to an item. The file itself lives on disk
    under app/static/uploads/ (flat folder, unique stored filename) — only
    the metadata and path are kept in the database, never the file bytes."""
    __tablename__ = "item_attachments"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    kind = Column(Enum("image", "pdf", name="attachment_kind"), nullable=False)
    original_filename = Column(String(255), nullable=False)
    stored_filename = Column(String(255), nullable=False, unique=True)
    uploaded_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", back_populates="attachments")


class BomComponent(Base):
    """One row of an assembled item's recipe: 'one unit of `item` requires
    `quantity` units of `component_item`'. Defined on the Item form itself."""
    __tablename__ = "bom_components"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    component_item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    quantity = Column(Numeric(14, 4), nullable=False)

    item = relationship("Item", foreign_keys=[item_id], back_populates="bom_components")
    component_item = relationship("Item", foreign_keys=[component_item_id], back_populates="used_in_boms")


# ---------------------------------------------------------------------------
# Sales
# ---------------------------------------------------------------------------

class SalesOrder(Base):
    __tablename__ = "sales_orders"

    id = Column(Integer, primary_key=True)
    order_no = Column(String(40), unique=True, nullable=False, index=True)
    customer_id = Column(Integer, ForeignKey("customers.id"), nullable=False)
    order_date = Column(DateTime, default=datetime.utcnow)
    status = Column(
        Enum("draft", "confirmed", "delivered", "cancelled", name="sales_status"),
        default="draft", nullable=False,
    )
    total_amount = Column(Numeric(14, 2), default=0)
    # Sum of each line's (Item.gst_percentage * line total), using each
    # item's *current* rate at the moment the order is saved — not stored
    # per line, since this app has no invoicing module yet and this is a
    # planning/estimate figure ("what will the invoice value be"), not a
    # legal tax record. Frozen at save time same as total_amount itself
    # (doesn't silently drift if an item's GST% changes later without the
    # order being re-saved); revisit with a real per-line rate snapshot if
    # a proper GST tax invoice gets built later. Grand total / "Invoice
    # Value" is total_amount + gst_amount, computed at display time rather
    # than stored a third time. No CGST/SGST/IGST split — deliberately one
    # blended figure, not a compliance-grade tax invoice.
    gst_amount = Column(Numeric(14, 2), default=0)
    notes = Column(Text)
    # The customer's own PO reference (their paperwork, not ours) — free text
    # since formats vary wildly by customer, plus the date on their PO,
    # which is often earlier than our order_date (their PO arrives, then we
    # get around to entering it). Both optional: not every order has a
    # formal customer PO behind it.
    customer_po_no = Column(String(80), nullable=True)
    customer_po_date = Column(DateTime, nullable=True)
    expected_shipment_date = Column(DateTime, nullable=True)
    # Bill To / Ship To — plain text snapshots (see CustomerAddress's
    # docstring for why this isn't a FK to a saved address row), pre-filled
    # in the UI from a CustomerAddress pick but always freely editable.
    # `billing_address` always belongs to `customer` above; there's no
    # separate "bill to a different customer" concept, only ship-to.
    billing_address = Column(Text, nullable=True)
    shipping_address = Column(Text, nullable=True)
    # Set only when the delivery goes to a different party than the one
    # being billed (a dealer/OEM order shipped direct to their end
    # customer's site) — null means "ships to the same customer that's
    # billed". This is metadata for traceability/filtering; the address
    # that actually gets printed/used is still `shipping_address` above,
    # not derived from this at read time, same snapshot reasoning.
    ship_to_customer_id = Column(Integer, ForeignKey("customers.id"), nullable=True)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    customer = relationship("Customer", back_populates="sales_orders", foreign_keys=[customer_id])
    ship_to_customer = relationship("Customer", foreign_keys=[ship_to_customer_id])
    items = relationship("SalesOrderItem", back_populates="sales_order", cascade="all, delete-orphan")
    payment_terms = relationship(
        "SalesOrderPaymentTerm", back_populates="sales_order",
        cascade="all, delete-orphan", order_by="SalesOrderPaymentTerm.id",
    )


class SalesOrderItem(Base):
    __tablename__ = "sales_order_items"

    id = Column(Integer, primary_key=True)
    sales_order_id = Column(Integer, ForeignKey("sales_orders.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    quantity = Column(Numeric(14, 4), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    total = Column(Numeric(14, 2), nullable=False)

    sales_order = relationship("SalesOrder", back_populates="items")
    item = relationship("Item")


class SalesOrderPaymentTerm(Base):
    """One milestone of a sales order's payment schedule — e.g. '30% Advance',
    'Balance on Delivery', 'Retention released after warranty'. Deliberately
    a free-text `description` rather than a fixed set of trigger types
    (advance/delivery/etc.): real payment terms are too varied to force into
    a rigid taxonomy, and `due_date` alone already gives the company-wide
    Payments Due view something concrete to sort and total by.

    `percentage` is optional and only used client-side to pre-fill `amount`
    (a % of the order's total at the moment it's typed) — same "computed
    default, editable afterward" pattern as SalesOrderItem.unit_price
    defaulting from Item.sales_price. `amount` is what's actually stored and
    used everywhere; nothing here re-derives it later if the order total
    changes, so a milestone's amount doesn't silently shift out from under
    a term that's already been agreed/invoiced.

    `secured_by` covers the case where a retention isn't literally withheld
    in cash but released against a bank guarantee instead — `bg_expiry_date`
    is only meaningful when `secured_by == 'bank_guarantee'`.

    `due_date` is the real, authoritative date once known, but most terms
    other than an advance aren't fixed dates at booking time — they're tied
    to a future invoice date this app doesn't have yet (no invoicing module
    exists — see "Known scope"). `days_after_invoice` captures that as a
    rule instead ("due 45 days after invoice") so it isn't lost while
    `due_date` sits blank. Deliberately not resolved into a real date by
    anything today; the natural place to do that is the future Sales
    Order → Invoice conversion, which will know the actual invoice date and
    can compute `due_date = invoice_date + days_after_invoice` for any term
    carried across. No `invoice_date` lives on `SalesOrder` for this reason
    either — that belongs on the future Invoice model, not guessed at here
    (a sales order could conceivably become more than one invoice later).
    If both `due_date` and `days_after_invoice` are set, `due_date` wins for
    display — it's the one you actually know for sure.

    `status`/`received_date` track whether the money has actually come in.
    Unlike the rest of a sales order (see SalesOrder.status and the edit
    routes in sales.py), payment terms are **not** locked by order status at
    all — editing them lives entirely in its own route
    (GET/POST /sales/{id}/payment-terms/edit), separate from the main order
    edit form, and works whether the order is draft, confirmed, delivered,
    or cancelled. That's deliberate: unlike line items, nothing about a
    payment term touches stock, so there's no integrity reason to lock it,
    and in practice most of these dates and amounts only become known well
    after the order itself is confirmed or even delivered. Marking a term
    received/pending is a further-separate action still
    (POST /sales/{id}/payment-terms/{term_id}/status) so a routine schedule
    edit can't accidentally revert one that's already been paid.
    `received_date` is asked for right on the Mark Received button (a date
    input defaulting to today, editable) rather than auto-stamped, since
    money that arrived a few days ago shouldn't have to be logged as today.

    **Known future extension**: which bank account the money actually
    landed in isn't tracked yet — there will eventually be a `BankAccount`
    model (the company holds more than one account) and this table will
    gain a nullable `bank_account_id` FK to it, filled in alongside
    `received_date` when a term is marked received. Not built yet since
    there's no bank account data anywhere in the app today; adding the
    column is a small, backward-compatible migration whenever that's
    needed, not something to design around prematurely now.
    """
    __tablename__ = "sales_order_payment_terms"

    id = Column(Integer, primary_key=True)
    sales_order_id = Column(Integer, ForeignKey("sales_orders.id"), nullable=False)
    description = Column(String(255), nullable=False)
    percentage = Column(Numeric(5, 2), nullable=True)
    amount = Column(Numeric(14, 2), nullable=False)
    due_date = Column(DateTime, nullable=True)
    days_after_invoice = Column(Integer, nullable=True)
    secured_by = Column(Enum("cash", "bank_guarantee", name="payment_term_security"), default="cash", nullable=False)
    bg_expiry_date = Column(DateTime, nullable=True)
    status = Column(Enum("pending", "received", name="payment_term_status"), default="pending", nullable=False)
    received_date = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    sales_order = relationship("SalesOrder", back_populates="payment_terms")


# ---------------------------------------------------------------------------
# Purchase
# ---------------------------------------------------------------------------

class PurchaseOrder(Base):
    __tablename__ = "purchase_orders"

    id = Column(Integer, primary_key=True)
    order_no = Column(String(40), unique=True, nullable=False, index=True)
    vendor_id = Column(Integer, ForeignKey("vendors.id"), nullable=False)
    order_date = Column(DateTime, default=datetime.utcnow)
    status = Column(
        Enum("draft", "ordered", "received", "cancelled", name="purchase_status"),
        default="draft", nullable=False,
    )
    # 3-letter ISO code (INR, USD, ...) the vendor invoices this order in,
    # plus the INR-per-unit rate at order time — total_amount and every
    # PurchaseOrderItem.unit_price on this order are in `currency`, not
    # always INR. exchange_rate is captured for reference/audit only
    # (e.g. when an order was imported from an external system that
    # recorded its own rate); nothing in this app converts through it
    # automatically today. Added for vendors like an overseas parent
    # company that invoice in USD — see migration
    # d4f7a9c21b3e_add_purchase_order_currency.
    currency = Column(String(3), default="INR", nullable=False)
    exchange_rate = Column(Numeric(10, 4), default=1, nullable=False)
    total_amount = Column(Numeric(14, 2), default=0)
    notes = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    vendor = relationship("Vendor", back_populates="purchase_orders")
    items = relationship("PurchaseOrderItem", back_populates="purchase_order", cascade="all, delete-orphan")


class PurchaseOrderItem(Base):
    __tablename__ = "purchase_order_items"

    id = Column(Integer, primary_key=True)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=False)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    quantity = Column(Numeric(14, 4), nullable=False)
    unit_price = Column(Numeric(12, 2), nullable=False)
    total = Column(Numeric(14, 2), nullable=False)

    purchase_order = relationship("PurchaseOrder", back_populates="items")
    item = relationship("Item")
    serials = relationship("ItemSerial", back_populates="purchase_order_item", cascade="all, delete-orphan")


# ---------------------------------------------------------------------------
# Production (assembling stock of one item out of others, per its BOM)
# ---------------------------------------------------------------------------

class ProductionOrder(Base):
    __tablename__ = "production_orders"

    id = Column(Integer, primary_key=True)
    order_no = Column(String(40), unique=True, nullable=False, index=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    quantity = Column(Numeric(14, 4), nullable=False)
    order_date = Column(DateTime, default=datetime.utcnow)
    status = Column(
        Enum("draft", "completed", "cancelled", name="production_status"),
        default="draft", nullable=False,
    )
    notes = Column(Text)
    created_by = Column(Integer, ForeignKey("users.id"), nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", foreign_keys=[item_id])
    components = relationship("ProductionOrderComponent", back_populates="production_order", cascade="all, delete-orphan")
    # Serial numbers captured for the finished item when this order is
    # completed (only populated for a has_serial item — see
    # complete_production_order). Not cascade="delete-orphan": there's no
    # delete route for a ProductionOrder today, but even if there were,
    # a captured serial is inventory-audit data, not something that should
    # silently vanish alongside its originating order.
    serials = relationship("ItemSerial", back_populates="production_order")


class ProductionOrderComponent(Base):
    """A snapshot of one BOM line at the time the production order was
    created, so editing an item's recipe later doesn't rewrite history."""
    __tablename__ = "production_order_components"

    id = Column(Integer, primary_key=True)
    production_order_id = Column(Integer, ForeignKey("production_orders.id"), nullable=False)
    component_item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    quantity_per_unit = Column(Numeric(14, 4), nullable=False)
    quantity_required = Column(Numeric(14, 4), nullable=False)  # quantity_per_unit * order.quantity

    production_order = relationship("ProductionOrder", back_populates="components")
    component_item = relationship("Item")


# ---------------------------------------------------------------------------
# Inventory
# ---------------------------------------------------------------------------

class StockTransaction(Base):
    __tablename__ = "stock_transactions"

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    transaction_type = Column(Enum("IN", "OUT", "ADJUST", name="stock_txn_type"), nullable=False)
    quantity = Column(Numeric(14, 4), nullable=False)  # always positive; type determines direction
    reference_type = Column(String(30))  # 'sales_order' | 'purchase_order' | 'manual'
    reference_id = Column(Integer, nullable=True)
    notes = Column(Text)
    transaction_date = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item", back_populates="stock_transactions")


class ItemSerial(Base):
    """One row per physical unit of a serialized item — captured either when
    a Purchase Order is received (goods receipt: `purchase_order_id` /
    `purchase_order_item_id`) or when a Production Order is completed
    (built in-house rather than bought: `production_order_id`, set by
    `complete_production_order`). Exactly one of those two origins is set
    per row in practice — nothing enforces that with a DB constraint (see
    this app's general style of validating in the route, not the schema),
    but the two capture flows each only ever populate their own side.

    Still only inbound: nothing here yet records which serial went *out*
    on a sale — see CLAUDE.md's "Known scope" note; that's a Sales/Invoice
    side gap, not a Production/Purchase one.
    """
    __tablename__ = "item_serials"
    __table_args__ = (UniqueConstraint("item_id", "serial_number", name="uq_item_serial"),)

    id = Column(Integer, primary_key=True)
    item_id = Column(Integer, ForeignKey("items.id"), nullable=False)
    serial_number = Column(String(120), nullable=False)
    purchase_order_id = Column(Integer, ForeignKey("purchase_orders.id"), nullable=True)
    purchase_order_item_id = Column(Integer, ForeignKey("purchase_order_items.id"), nullable=True)
    production_order_id = Column(Integer, ForeignKey("production_orders.id"), nullable=True)
    received_at = Column(DateTime, default=datetime.utcnow)

    item = relationship("Item")
    purchase_order = relationship("PurchaseOrder")
    purchase_order_item = relationship("PurchaseOrderItem", back_populates="serials")
    production_order = relationship("ProductionOrder", back_populates="serials")


# ---------------------------------------------------------------------------
# Audit log
# ---------------------------------------------------------------------------

class AuditLog(Base):
    """One row per changed field on a tracked mutation — editing a Purchase
    Order's vendor and one line's price produces two rows sharing the same
    (entity_type, entity_id, action, created_at) grouping, one per field.
    A create/delete/status-change/receive-style event that isn't really a
    field-by-field diff gets exactly one row instead, with `field_name` left
    null and a short human-readable note in `new_value` (see
    app/audit.py:log_action).

    Deliberately explicit-per-route — every mutating route that's in scope
    calls into app/audit.py itself, right where it makes the change — rather
    than a generic SQLAlchemy `before_flush` hook that would fire for every
    column on every table. Same reasoning as this app's other transactional
    writes (StockTransaction, ItemSerial): a global hook can't easily attach
    a human-readable `entity_label`, can't tell "changed via the Edit form"
    apart from "system recomputed a total", and would need an equally
    explicit allow-list to avoid logging incidental columns nobody asked to
    track anyway. See CLAUDE.md's established pattern.

    Only wired into the high-value areas agreed on first: Items (pricing/
    stock-affecting fields), Sales Orders and Purchase Orders (including
    status transitions and receiving), Inventory manual adjustments, and
    Users/Roles (permission changes). Customers, Vendors, and Production
    aren't covered yet — a deliberate first-pass scope, not an oversight;
    extend the same way (call log_field_changes/log_action from the route)
    if/when those need it too.

    `username` is a point-in-time snapshot (not just a FK join) so a log
    entry still reads correctly even if the user's account is later renamed
    — same "snapshot at the moment of the action" convention as
    SalesOrderItem.unit_price. `entity_label` is the same idea for the
    record itself (e.g. an order's order_no, an item's name at the time),
    so the log stays readable without a join even if the underlying row is
    later deleted (items can be, per get_item_delete_blockers) or renamed.

    Values are always stored as plain strings (str() of whatever the field
    held) — this is a human-readable log for staff to review, not a typed
    replay/undo mechanism.
    """
    __tablename__ = "audit_logs"

    id = Column(Integer, primary_key=True)
    user_id = Column(Integer, ForeignKey("users.id"), nullable=True)
    username = Column(String(80), nullable=True)
    entity_type = Column(String(50), nullable=False, index=True)
    entity_id = Column(Integer, nullable=False, index=True)
    entity_label = Column(String(200), nullable=True)
    action = Column(String(30), nullable=False)
    field_name = Column(String(80), nullable=True)
    old_value = Column(Text, nullable=True)
    new_value = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, nullable=False, index=True)

    user = relationship("User")
