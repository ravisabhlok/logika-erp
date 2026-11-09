"""add gst_amount to purchase orders

Revision ID: c9a4e7b1d256
Revises: b2e8f4a6c013
Create Date: 2026-11-09 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c9a4e7b1d256'
down_revision: Union[str, Sequence[str], None] = 'b2e8f4a6c013'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('purchase_orders', sa.Column('gst_amount', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False))
    # Backfill existing INR orders from each line's item's current GST %.
    op.execute(
        """
        UPDATE purchase_orders po
        SET gst_amount = COALESCE((
            SELECT ROUND(SUM(poi.total * COALESCE(i.gst_percentage, 0) / 100), 2)
            FROM purchase_order_items poi
            JOIN items i ON i.id = poi.item_id
            WHERE poi.purchase_order_id = po.id
        ), 0)
        WHERE po.currency = 'INR'
        """
    )


def downgrade() -> None:
    op.drop_column('purchase_orders', 'gst_amount')
