"""add customer po fields and payment terms

Revision ID: 7cd576aaee5b
Revises: 1d0dd92e804c
Create Date: 2026-08-11 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7cd576aaee5b'
down_revision: Union[str, Sequence[str], None] = '1d0dd92e804c'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column('sales_orders', sa.Column('customer_po_no', sa.String(length=80), nullable=True))
    op.add_column('sales_orders', sa.Column('customer_po_date', sa.DateTime(), nullable=True))
    op.add_column('sales_orders', sa.Column('expected_shipment_date', sa.DateTime(), nullable=True))

    op.create_table(
        'sales_order_payment_terms',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('sales_order_id', sa.Integer(), nullable=False),
        sa.Column('description', sa.String(length=255), nullable=False),
        sa.Column('percentage', sa.Numeric(precision=5, scale=2), nullable=True),
        sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('due_date', sa.DateTime(), nullable=True),
        sa.Column('secured_by', sa.Enum('cash', 'bank_guarantee', name='payment_term_security'), nullable=False),
        sa.Column('bg_expiry_date', sa.DateTime(), nullable=True),
        sa.Column('status', sa.Enum('pending', 'received', name='payment_term_status'), nullable=False),
        sa.Column('received_date', sa.DateTime(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['sales_order_id'], ['sales_orders.id'], ),
        sa.PrimaryKeyConstraint('id'),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_table('sales_order_payment_terms')
    op.drop_column('sales_orders', 'expected_shipment_date')
    op.drop_column('sales_orders', 'customer_po_date')
    op.drop_column('sales_orders', 'customer_po_no')
