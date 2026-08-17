"""add customer category

Revision ID: a1b7c9d3e5f2
Revises: e92bc1274286
Create Date: 2026-07-29 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'a1b7c9d3e5f2'
down_revision: Union[str, Sequence[str], None] = 'e92bc1274286'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column(
        'customers',
        sa.Column(
            'category',
            sa.Enum('OEM', 'Enduser', 'Dealer', name='customer_category'),
            nullable=True,
        ),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column('customers', 'category')
