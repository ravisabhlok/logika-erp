"""add purchase order item delivery date

Revision ID: 86cff42704c3
Revises: 7af45da8ee24
Create Date: 2026-09-08 12:57:40.424203

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '86cff42704c3'
down_revision: Union[str, Sequence[str], None] = '7af45da8ee24'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('purchase_order_items', sa.Column('expected_delivery_date', sa.DateTime(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('purchase_order_items', 'expected_delivery_date')
