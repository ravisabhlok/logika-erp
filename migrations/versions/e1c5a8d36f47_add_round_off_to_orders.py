"""add manual round_off amount to sales and purchase orders

Revision ID: e1c5a8d36f47
Revises: d7b3f19a4c82
Create Date: 2026-11-09 17:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'e1c5a8d36f47'
down_revision: Union[str, Sequence[str], None] = 'd7b3f19a4c82'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    for table in ('sales_orders', 'purchase_orders'):
        op.add_column(table, sa.Column('round_off_amount', sa.Numeric(precision=14, scale=2), server_default='0', nullable=False))


def downgrade() -> None:
    for table in ('purchase_orders', 'sales_orders'):
        op.drop_column(table, 'round_off_amount')
