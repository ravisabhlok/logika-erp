"""add zoho_po_no to purchase orders

Revision ID: a7d3c5e91f42
Revises: ffab990bf59e
Create Date: 2026-11-09 12:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a7d3c5e91f42'
down_revision: Union[str, Sequence[str], None] = 'ffab990bf59e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.add_column('purchase_orders', sa.Column('zoho_po_no', sa.String(length=255), nullable=True))


def downgrade() -> None:
    op.drop_column('purchase_orders', 'zoho_po_no')
