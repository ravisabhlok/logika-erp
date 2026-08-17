"""add sales order gst amount

Revision ID: 5d643439b150
Revises: 05deb56b8fcd
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '5d643439b150'
down_revision: Union[str, Sequence[str], None] = '05deb56b8fcd'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sales_orders', sa.Column('gst_amount', sa.Numeric(precision=14, scale=2), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sales_orders', 'gst_amount')
