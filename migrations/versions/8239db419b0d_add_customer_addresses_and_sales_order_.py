"""add customer addresses and sales order bill/ship to

Revision ID: 8239db419b0d
Revises: 5d643439b150
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '8239db419b0d'
down_revision: Union[str, Sequence[str], None] = '5d643439b150'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'customer_addresses',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('customer_id', sa.Integer(), nullable=False),
        sa.Column('label', sa.String(length=100), nullable=True),
        sa.Column('address', sa.Text(), nullable=False),
        sa.Column('city', sa.String(length=100), nullable=True),
        sa.Column('state', sa.String(length=100), nullable=True),
        sa.Column('country', sa.String(length=100), nullable=True),
        sa.Column('gstin', sa.String(length=20), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['customer_id'], ['customers.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )

    op.add_column('sales_orders', sa.Column('billing_address', sa.Text(), nullable=True))
    op.add_column('sales_orders', sa.Column('shipping_address', sa.Text(), nullable=True))
    op.add_column('sales_orders', sa.Column('ship_to_customer_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('sales_orders') as batch_op:
        batch_op.create_foreign_key('fk_sales_orders_ship_to_customer_id', 'customers', ['ship_to_customer_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('sales_orders') as batch_op:
        batch_op.drop_constraint('fk_sales_orders_ship_to_customer_id', type_='foreignkey')
        batch_op.drop_column('ship_to_customer_id')
    op.drop_column('sales_orders', 'shipping_address')
    op.drop_column('sales_orders', 'billing_address')
    op.drop_table('customer_addresses')
