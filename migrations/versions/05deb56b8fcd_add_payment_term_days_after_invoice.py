"""add payment term days after invoice

Revision ID: 05deb56b8fcd
Revises: 7cd576aaee5b
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '05deb56b8fcd'
down_revision: Union[str, Sequence[str], None] = '7cd576aaee5b'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sales_order_payment_terms', sa.Column('days_after_invoice', sa.Integer(), nullable=True))


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('sales_order_payment_terms', 'days_after_invoice')
