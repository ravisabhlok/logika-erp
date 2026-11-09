"""add track_inventory to items

Revision ID: b2e8f4a6c013
Revises: a7d3c5e91f42
Create Date: 2026-11-09 14:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b2e8f4a6c013'
down_revision: Union[str, Sequence[str], None] = 'a7d3c5e91f42'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    # server_default true: every existing item stays an inventory item.
    op.add_column('items', sa.Column('track_inventory', sa.Boolean(), server_default=sa.true(), nullable=False))


def downgrade() -> None:
    op.drop_column('items', 'track_inventory')
