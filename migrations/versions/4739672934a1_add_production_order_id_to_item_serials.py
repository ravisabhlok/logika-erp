"""add production_order_id to item_serials

Revision ID: 4739672934a1
Revises: d4f7a9c21b3e
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '4739672934a1'
down_revision: Union[str, Sequence[str], None] = 'd4f7a9c21b3e'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    # A serial can now originate from a Production Order (built in-house)
    # as well as a Purchase Order (bought in) — see ItemSerial's docstring.
    # Nullable: existing rows (all purchase-origin so far) and any serial
    # still captured via Purchase receiving leave this null.
    op.add_column('item_serials', sa.Column('production_order_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('item_serials') as batch_op:
        batch_op.create_foreign_key('fk_item_serials_production_order_id', 'production_orders', ['production_order_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('item_serials') as batch_op:
        batch_op.drop_constraint('fk_item_serials_production_order_id', type_='foreignkey')
        batch_op.drop_column('production_order_id')
