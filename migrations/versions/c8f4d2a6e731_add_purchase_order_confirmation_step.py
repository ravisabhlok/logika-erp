"""add purchase order confirmation step

Revision ID: c8f4d2a6e731
Revises: b8b1aeaaf475
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'c8f4d2a6e731'
down_revision: Union[str, Sequence[str], None] = 'b8b1aeaaf475'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Widen the status enum again to add 'pending_confirmation', inserted
    # between 'pending_approval' and 'ordered' — an admin's approval no
    # longer jumps straight to 'ordered'; it now lands here first, and a
    # second, independently-permissioned person (see role_permissions.
    # can_confirm below) confirms it on to 'ordered'. Existing rows are
    # untouched — every current status value is still valid in the widened
    # enum.
    op.alter_column(
        'purchase_orders', 'status',
        existing_type=sa.Enum('draft', 'pending_approval', 'ordered', 'received', 'cancelled', name='purchase_status'),
        type_=sa.Enum('draft', 'pending_approval', 'pending_confirmation', 'ordered', 'received', 'cancelled', name='purchase_status'),
        existing_nullable=False,
    )
    op.add_column('purchase_orders', sa.Column('confirmed_by', sa.Integer(), nullable=True))
    op.add_column('purchase_orders', sa.Column('confirmed_at', sa.DateTime(), nullable=True))
    op.create_foreign_key(
        'fk_purchase_orders_confirmed_by_users',
        'purchase_orders', 'users', ['confirmed_by'], ['id'],
    )

    # New 5th permission action, alongside view/add/edit/delete, on every
    # existing (role, module) row — see app.auth.ACTIONS. server_default so
    # every role's existing rows backfill to "not granted" (nobody gets a
    # new capability just because this column now exists) rather than
    # needing a data migration step.
    op.add_column('role_permissions', sa.Column('can_confirm', sa.Boolean(), nullable=False, server_default=sa.false()))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('role_permissions', 'can_confirm')
    op.drop_constraint('fk_purchase_orders_confirmed_by_users', 'purchase_orders', type_='foreignkey')
    op.drop_column('purchase_orders', 'confirmed_at')
    op.drop_column('purchase_orders', 'confirmed_by')
    # Any orders currently pending confirmation have to be resolved to a
    # value the narrower enum still allows before it can shrink back — sent
    # to 'draft' rather than silently promoted to 'ordered'.
    op.execute("UPDATE purchase_orders SET status = 'draft' WHERE status = 'pending_confirmation'")
    op.alter_column(
        'purchase_orders', 'status',
        existing_type=sa.Enum('draft', 'pending_approval', 'pending_confirmation', 'ordered', 'received', 'cancelled', name='purchase_status'),
        type_=sa.Enum('draft', 'pending_approval', 'ordered', 'received', 'cancelled', name='purchase_status'),
        existing_nullable=False,
    )
