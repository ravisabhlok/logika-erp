"""add purchase order currency and exchange rate

Revision ID: d4f7a9c21b3e
Revises: 8239db419b0d
Create Date: 2026-08-14 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd4f7a9c21b3e'
down_revision: Union[str, Sequence[str], None] = '8239db419b0d'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # server_default so every existing purchase order (all of them placed
    # with Indian vendors so far) is backfilled as INR / rate 1 without
    # needing a data migration step.
    op.add_column('purchase_orders', sa.Column('currency', sa.String(length=3), nullable=False, server_default='INR'))
    op.add_column('purchase_orders', sa.Column('exchange_rate', sa.Numeric(10, 4), nullable=False, server_default='1'))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('purchase_orders', 'exchange_rate')
    op.drop_column('purchase_orders', 'currency')
