"""4-decimal purchase unit price, bill round off

Revision ID: a7c3e91d4b52
Revises: f2d8b6a1c935
Create Date: 2026-11-11 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7c3e91d4b52'
down_revision: Union[str, Sequence[str], None] = 'f2d8b6a1c935'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.alter_column('purchase_order_items', 'unit_price', existing_type=sa.Numeric(12, 2),
                    type_=sa.Numeric(14, 4), existing_nullable=False)
    op.alter_column('bill_items', 'unit_price', existing_type=sa.Numeric(12, 2),
                    type_=sa.Numeric(14, 4), existing_nullable=False)
    op.add_column('bills', sa.Column('round_off_amount', sa.Numeric(14, 2), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('bills', 'round_off_amount')
    op.alter_column('bill_items', 'unit_price', existing_type=sa.Numeric(14, 4),
                    type_=sa.Numeric(12, 2), existing_nullable=False)
    op.alter_column('purchase_order_items', 'unit_price', existing_type=sa.Numeric(14, 4),
                    type_=sa.Numeric(12, 2), existing_nullable=False)
