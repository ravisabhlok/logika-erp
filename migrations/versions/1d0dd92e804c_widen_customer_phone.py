"""widen customer phone

Revision ID: 1d0dd92e804c
Revises: 53a0c258e40f
Create Date: 2026-07-31 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '1d0dd92e804c'
down_revision: Union[str, Sequence[str], None] = '53a0c258e40f'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # Some real customer phone fields (imported from Zoho) hold more than one
    # number concatenated together, e.g. "07721-264379-84, 98936-94255/59"
    # (32 chars) -- 30 was too tight and rejected a legitimate value outright.
    op.alter_column(
        'customers', 'phone',
        existing_type=sa.String(length=30),
        type_=sa.String(length=50),
        existing_nullable=True,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.alter_column(
        'customers', 'phone',
        existing_type=sa.String(length=50),
        type_=sa.String(length=30),
        existing_nullable=True,
    )
