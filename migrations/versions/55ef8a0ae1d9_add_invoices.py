"""add invoices

Revision ID: 55ef8a0ae1d9
Revises: c8f4d2a6e731
Create Date: 2026-08-19 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '55ef8a0ae1d9'
down_revision: Union[str, Sequence[str], None] = 'c8f4d2a6e731'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'invoices',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('invoice_no', sa.String(length=40), nullable=False),
        sa.Column('sales_order_id', sa.Integer(), nullable=False),
        sa.Column('invoice_date', sa.DateTime(), nullable=False),
        sa.Column('status', sa.Enum('draft', 'issued', 'cancelled', name='invoice_status'), nullable=False),
        sa.Column('bill_to_name', sa.String(length=200), nullable=True),
        sa.Column('bill_to_address', sa.Text(), nullable=True),
        sa.Column('bill_to_city', sa.String(length=100), nullable=True),
        sa.Column('bill_to_state', sa.String(length=100), nullable=True),
        sa.Column('bill_to_country', sa.String(length=100), nullable=True),
        sa.Column('bill_to_gstin', sa.String(length=20), nullable=True),
        sa.Column('ship_to_name', sa.String(length=200), nullable=True),
        sa.Column('ship_to_address', sa.Text(), nullable=True),
        sa.Column('ship_to_city', sa.String(length=100), nullable=True),
        sa.Column('ship_to_state', sa.String(length=100), nullable=True),
        sa.Column('ship_to_country', sa.String(length=100), nullable=True),
        sa.Column('ship_to_gstin', sa.String(length=20), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('total_amount', sa.Numeric(14, 2), nullable=True),
        sa.Column('gst_amount', sa.Numeric(14, 2), nullable=True),
        sa.Column('stock_deducted', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('issued_by', sa.Integer(), nullable=True),
        sa.Column('issued_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by', sa.Integer(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['sales_order_id'], ['sales_orders.id'], name='fk_invoices_sales_order_id'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name='fk_invoices_created_by'),
        sa.ForeignKeyConstraint(['issued_by'], ['users.id'], name='fk_invoices_issued_by'),
        sa.ForeignKeyConstraint(['cancelled_by'], ['users.id'], name='fk_invoices_cancelled_by'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('invoice_no', name='uq_invoices_invoice_no'),
    )
    with op.batch_alter_table('invoices') as batch_op:
        batch_op.create_index('ix_invoices_invoice_no', ['invoice_no'])

    op.create_table(
        'invoice_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('invoice_id', sa.Integer(), nullable=False),
        sa.Column('sales_order_item_id', sa.Integer(), nullable=False),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Numeric(14, 4), nullable=False),
        sa.Column('unit_price', sa.Numeric(12, 2), nullable=False),
        sa.Column('gst_percentage', sa.Numeric(5, 2), nullable=True),
        sa.Column('hsn_code', sa.String(length=10), nullable=True),
        sa.Column('total', sa.Numeric(14, 2), nullable=False),
        sa.ForeignKeyConstraint(['invoice_id'], ['invoices.id'], name='fk_invoice_items_invoice_id'),
        sa.ForeignKeyConstraint(['sales_order_item_id'], ['sales_order_items.id'], name='fk_invoice_items_sales_order_item_id'),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], name='fk_invoice_items_item_id'),
        sa.PrimaryKeyConstraint('id'),
    )

    # Outbound serial capture — see ItemSerial's docstring. Nullable: only a
    # has_serial line's serials get linked here when its invoice is issued;
    # every existing row (and every non-serialized line) leaves this null.
    op.add_column('item_serials', sa.Column('invoice_item_id', sa.Integer(), nullable=True))
    op.add_column('item_serials', sa.Column('shipped_at', sa.DateTime(), nullable=True))
    with op.batch_alter_table('item_serials') as batch_op:
        batch_op.create_foreign_key('fk_item_serials_invoice_item_id', 'invoice_items', ['invoice_item_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('item_serials') as batch_op:
        batch_op.drop_constraint('fk_item_serials_invoice_item_id', type_='foreignkey')
        batch_op.drop_column('shipped_at')
        batch_op.drop_column('invoice_item_id')

    op.drop_table('invoice_items')

    with op.batch_alter_table('invoices') as batch_op:
        batch_op.drop_index('ix_invoices_invoice_no')
    op.drop_table('invoices')
