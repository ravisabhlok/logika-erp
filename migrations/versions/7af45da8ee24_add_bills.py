"""add bills

Revision ID: 7af45da8ee24
Revises: 55ef8a0ae1d9
Create Date: 2026-09-08 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '7af45da8ee24'
down_revision: Union[str, Sequence[str], None] = '55ef8a0ae1d9'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'bills',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bill_ref', sa.String(length=40), nullable=False),
        sa.Column('vendor_id', sa.Integer(), nullable=False),
        sa.Column('purchase_order_id', sa.Integer(), nullable=True),
        sa.Column('vendor_bill_no', sa.String(length=60), nullable=False),
        sa.Column('bill_date', sa.DateTime(), nullable=False),
        sa.Column('due_date', sa.DateTime(), nullable=True),
        sa.Column('taxable_amount', sa.Numeric(14, 2), nullable=True),
        sa.Column('gst_amount', sa.Numeric(14, 2), nullable=True),
        sa.Column('total_amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('amount_paid', sa.Numeric(14, 2), nullable=False),
        sa.Column('status', sa.Enum('unpaid', 'partially_paid', 'paid', 'cancelled', name='bill_status'), nullable=False),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('attachment_original_filename', sa.String(length=255), nullable=True),
        sa.Column('attachment_stored_filename', sa.String(length=255), nullable=True),
        sa.Column('stock_updated', sa.Boolean(), nullable=False),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.Column('cancelled_by', sa.Integer(), nullable=True),
        sa.Column('cancelled_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['vendor_id'], ['vendors.id'], name='fk_bills_vendor_id'),
        sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_orders.id'], name='fk_bills_purchase_order_id'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name='fk_bills_created_by'),
        sa.ForeignKeyConstraint(['cancelled_by'], ['users.id'], name='fk_bills_cancelled_by'),
        sa.PrimaryKeyConstraint('id'),
        sa.UniqueConstraint('bill_ref', name='uq_bills_bill_ref'),
    )
    with op.batch_alter_table('bills') as batch_op:
        batch_op.create_index('ix_bills_bill_ref', ['bill_ref'])

    op.create_table(
        'bill_items',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bill_id', sa.Integer(), nullable=False),
        sa.Column('purchase_order_item_id', sa.Integer(), nullable=True),
        sa.Column('item_id', sa.Integer(), nullable=False),
        sa.Column('quantity', sa.Numeric(14, 4), nullable=False),
        sa.Column('unit_price', sa.Numeric(12, 2), nullable=False),
        sa.Column('gst_percentage', sa.Numeric(5, 2), nullable=True),
        sa.Column('hsn_code', sa.String(length=10), nullable=True),
        sa.Column('total', sa.Numeric(14, 2), nullable=False),
        sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], name='fk_bill_items_bill_id'),
        sa.ForeignKeyConstraint(['purchase_order_item_id'], ['purchase_order_items.id'], name='fk_bill_items_purchase_order_item_id'),
        sa.ForeignKeyConstraint(['item_id'], ['items.id'], name='fk_bill_items_item_id'),
        sa.PrimaryKeyConstraint('id'),
    )

    op.create_table(
        'bill_payments',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('bill_id', sa.Integer(), nullable=False),
        sa.Column('payment_date', sa.DateTime(), nullable=False),
        sa.Column('amount', sa.Numeric(14, 2), nullable=False),
        sa.Column('mode', sa.String(length=30), nullable=True),
        sa.Column('reference_no', sa.String(length=80), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['bill_id'], ['bills.id'], name='fk_bill_payments_bill_id'),
        sa.ForeignKeyConstraint(['created_by'], ['users.id'], name='fk_bill_payments_created_by'),
        sa.PrimaryKeyConstraint('id'),
    )

    # Inbound serial capture for a no-PO bill — see ItemSerial.bill_item_id's
    # docstring. Nullable: only set for serials captured this way; every
    # existing row (and every PO-based or production-built serial) leaves
    # this null.
    op.add_column('item_serials', sa.Column('bill_item_id', sa.Integer(), nullable=True))
    with op.batch_alter_table('item_serials') as batch_op:
        batch_op.create_foreign_key('fk_item_serials_bill_item_id', 'bill_items', ['bill_item_id'], ['id'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('item_serials') as batch_op:
        batch_op.drop_constraint('fk_item_serials_bill_item_id', type_='foreignkey')
        batch_op.drop_column('bill_item_id')

    op.drop_table('bill_payments')
    op.drop_table('bill_items')

    with op.batch_alter_table('bills') as batch_op:
        batch_op.drop_index('ix_bills_bill_ref')
    op.drop_table('bills')
