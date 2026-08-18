"""merge audit logs and purchase order approval

Revision ID: b8b1aeaaf475
Revises: 9a1c4e6f2b7d, b6e2f8a4c910
Create Date: 2026-08-18 17:31:34.860703

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'b8b1aeaaf475'
down_revision: Union[str, Sequence[str], None] = ('9a1c4e6f2b7d', 'b6e2f8a4c910')
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    pass


def downgrade() -> None:
    """Downgrade schema."""
    pass
