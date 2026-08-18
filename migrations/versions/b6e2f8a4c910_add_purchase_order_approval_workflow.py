"""add purchase order approval workflow

Revision ID: b6e2f8a4c910
Revises: d4f7a9c21b3e
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b6e2f8a4c910'
down_revision: Union[str, Sequence[str], None] = 'd4f7a9c21b3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Widen the status enum to add 'pending_approval', inserted between
    # 'draft' and 'ordered' — a purchase order now goes
    # draft -> pending_approval -> ordered -> received, with staff sending
    # it for approval and only an admin account able to move it on to
    # 'ordered' (see approve_purchase_order / reject_purchase_order in
    # app/routers/purchase.py). Existing rows are untouched — every current
    # order's status ('draft', 'ordered', 'received', or 'cancelled') is
    # still a valid value in the widened enum, so nothing needs backfilling.
    op.alter_column(
        'purchase_orders', 'status',
        existing_type=sa.Enum('draft', 'ordered', 'received', 'cancelled', name='purchase_status'),
        type_=sa.Enum('draft', 'pending_approval', 'ordered', 'received', 'cancelled', name='purchase_status'),
        existing_nullable=False,
    )
    op.add_column('purchase_orders', sa.Column('approved_by', sa.Integer(), nullable=True))
    op.add_column('purchase_orders', sa.Column('approved_at', sa.DateTime(), nullable=True))
    op.create_foreign_key(
        'fk_purchase_orders_approved_by_users',
        'purchase_orders', 'users', ['approved_by'], ['id'],
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_constraint('fk_purchase_orders_approved_by_users', 'purchase_orders', type_='foreignkey')
    op.drop_column('purchase_orders', 'approved_at')
    op.drop_column('purchase_orders', 'approved_by')
    # Any orders currently pending approval have to be resolved to a value
    # the narrower enum still allows before it can shrink back — sent to
    # 'draft' rather than silently promoted to 'ordered'.
    op.execute("UPDATE purchase_orders SET status = 'draft' WHERE status = 'pending_approval'")
    op.alter_column(
        'purchase_orders', 'status',
        existing_type=sa.Enum('draft', 'pending_approval', 'ordered', 'received', 'cancelled', name='purchase_status'),
        type_=sa.Enum('draft', 'ordered', 'received', 'cancelled', name='purchase_status'),
        existing_nullable=False,
    )
