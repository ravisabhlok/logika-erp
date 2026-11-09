"""add freight_amount to sales and purchase orders

Revision ID: d7b3f19a4c82
Revises: c9a4e7b1d256
Create Date: 2026-11-09 16:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'd7b3f19a4c82'
down_revision: Union[str, Sequence[str], None] = 'c9a4e7b1d256'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('sales_orders', sa.Column('freight_amount', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False))
    op.add_column('purchase_orders', sa.Column('freight_amount', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('purchase_orders', 'freight_amount')
    op.drop_column('sales_orders', 'freight_amount')
