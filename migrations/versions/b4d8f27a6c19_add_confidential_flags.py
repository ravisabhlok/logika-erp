"""add confidential flags

Revision ID: b4d8f27a6c19
Revises: a7c3e91d4b52
Create Date: 2026-11-11 15:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b4d8f27a6c19'
down_revision: Union[str, Sequence[str], None] = 'a7c3e91d4b52'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None

PARTY_TABLES = ('customers', 'vendors', 'items', 'payees')


def upgrade() -> None:
    for table in PARTY_TABLES:
        op.add_column(table, sa.Column('is_confidential', sa.Boolean(), server_default='0', nullable=False))
    op.add_column('roles', sa.Column('can_view_confidential', sa.Boolean(), server_default='0', nullable=False))


def downgrade() -> None:
    op.drop_column('roles', 'can_view_confidential')
    for table in reversed(PARTY_TABLES):
        op.drop_column(table, 'is_confidential')
