"""add purchase order partial receipt

Revision ID: f3a91c7d5e20
Revises: 86cff42704c3
Create Date: 2026-10-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f3a91c7d5e20'
down_revision: Union[str, Sequence[str], None] = '86cff42704c3'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


OLD_STATUSES = ('draft', 'pending_approval', 'pending_confirmation', 'ordered', 'received', 'cancelled')
NEW_STATUSES = ('draft', 'pending_approval', 'pending_confirmation', 'ordered', 'partially_received', 'received', 'cancelled')


def upgrade() -> None:
    """Upgrade schema."""
    op.alter_column(
        'purchase_orders', 'status',
        existing_type=sa.Enum(*OLD_STATUSES, name='purchase_status'),
        type_=sa.Enum(*NEW_STATUSES, name='purchase_status'),
        existing_nullable=False,
    )
    op.add_column('purchase_orders', sa.Column('short_closed_at', sa.DateTime(), nullable=True))
    op.add_column('purchase_orders', sa.Column('short_closed_by', sa.Integer(), nullable=True))
    op.add_column('purchase_orders', sa.Column('short_close_reason', sa.Text(), nullable=True))
    op.create_foreign_key(
        'fk_purchase_orders_short_closed_by_users',
        'purchase_orders', 'users', ['short_closed_by'], ['id'],
    )
    op.add_column(
        'purchase_order_items',
        sa.Column('received_quantity', sa.Numeric(14, 4), nullable=False, server_default='0'),
    )
    # Backfill: every order already 'received' was received in full (there
    # was no partial receipt before this), so its lines are fully received.
    op.execute(
        "UPDATE purchase_order_items poi "
        "JOIN purchase_orders po ON po.id = poi.purchase_order_id "
        "SET poi.received_quantity = poi.quantity "
        "WHERE po.status = 'received'"
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('purchase_order_items', 'received_quantity')
    op.drop_constraint('fk_purchase_orders_short_closed_by_users', 'purchase_orders', type_='foreignkey')
    op.drop_column('purchase_orders', 'short_close_reason')
    op.drop_column('purchase_orders', 'short_closed_by')
    op.drop_column('purchase_orders', 'short_closed_at')
    # Partially received orders already moved stock, so the closest value
    # the narrower enum allows is 'received'.
    op.execute("UPDATE purchase_orders SET status = 'received' WHERE status = 'partially_received'")
    op.alter_column(
        'purchase_orders', 'status',
        existing_type=sa.Enum(*NEW_STATUSES, name='purchase_status'),
        type_=sa.Enum(*OLD_STATUSES, name='purchase_status'),
        existing_nullable=False,
    )
