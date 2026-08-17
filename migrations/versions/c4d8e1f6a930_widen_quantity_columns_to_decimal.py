"""widen quantity columns to decimal

Revision ID: c4d8e1f6a930
Revises: a1b7c9d3e5f2
Create Date: 2026-07-29 00:00:00.000000

Every quantity-bearing column across the app was Integer, which can't
represent a fractional BOM/recipe requirement (e.g. 0.2459 of a 305m cable
reel, discovered while importing a Zoho composite item's bill of
materials). This widens the whole quantity chain to Numeric(14, 4) so
fractional amounts can be stored and consumed consistently: item stock
levels, BOM recipe quantities, sales/purchase order line quantities,
production order quantities, and stock transaction quantities.

See app/formatting.py:format_qty for the display-side counterpart — it
strips trailing zeros so a plain whole-number quantity still renders as
"3" instead of "3.0000".
"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c4d8e1f6a930'
down_revision: Union[str, Sequence[str], None] = 'a1b7c9d3e5f2'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'items', 'current_stock',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=False,
    )
    op.alter_column(
        'items', 'reorder_level',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=True,
    )
    op.alter_column(
        'bom_components', 'quantity',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=False,
    )
    op.alter_column(
        'sales_order_items', 'quantity',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=False,
    )
    op.alter_column(
        'purchase_order_items', 'quantity',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=False,
    )
    op.alter_column(
        'production_orders', 'quantity',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=False,
    )
    op.alter_column(
        'production_order_components', 'quantity_per_unit',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=False,
    )
    op.alter_column(
        'production_order_components', 'quantity_required',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=False,
    )
    op.alter_column(
        'stock_transactions', 'quantity',
        existing_type=sa.Integer(), type_=sa.Numeric(14, 4),
        existing_nullable=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    # Note: downgrading truncates any fractional quantities back to whole numbers.
    op.alter_column(
        'stock_transactions', 'quantity',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        'production_order_components', 'quantity_required',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        'production_order_components', 'quantity_per_unit',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        'production_orders', 'quantity',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        'purchase_order_items', 'quantity',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        'sales_order_items', 'quantity',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        'bom_components', 'quantity',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=False,
    )
    op.alter_column(
        'items', 'reorder_level',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=True,
    )
    op.alter_column(
        'items', 'current_stock',
        existing_type=sa.Numeric(14, 4), type_=sa.Integer(),
        existing_nullable=False,
    )
