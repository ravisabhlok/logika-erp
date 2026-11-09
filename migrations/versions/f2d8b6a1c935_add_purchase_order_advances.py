"""add purchase order advances

Revision ID: f2d8b6a1c935
Revises: e1c5a8d36f47
Create Date: 2026-11-09 18:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = 'f2d8b6a1c935'
down_revision: Union[str, Sequence[str], None] = 'e1c5a8d36f47'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    op.create_table(
        'purchase_order_advances',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('purchase_order_id', sa.Integer(), nullable=False),
        sa.Column('amount', sa.Numeric(precision=14, scale=2), nullable=False),
        sa.Column('paid_date', sa.DateTime(), nullable=False),
        sa.Column('mode', sa.String(length=30), nullable=True),
        sa.Column('reference_no', sa.String(length=80), nullable=True),
        sa.Column('notes', sa.Text(), nullable=True),
        sa.Column('payment_line_id', sa.Integer(), nullable=True),
        sa.Column('created_by', sa.Integer(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=True),
        sa.ForeignKeyConstraint(['created_by'], ['users.id']),
        sa.ForeignKeyConstraint(['payment_line_id'], ['payment_lines.id']),
        sa.ForeignKeyConstraint(['purchase_order_id'], ['purchase_orders.id']),
        sa.PrimaryKeyConstraint('id'),
    )
    op.create_index(op.f('ix_purchase_order_advances_purchase_order_id'), 'purchase_order_advances', ['purchase_order_id'])
    op.create_index(op.f('ix_purchase_order_advances_payment_line_id'), 'purchase_order_advances', ['payment_line_id'])
    op.add_column('bill_payments', sa.Column('po_advance_id', sa.Integer(), nullable=True))
    op.create_index(op.f('ix_bill_payments_po_advance_id'), 'bill_payments', ['po_advance_id'])
    op.create_foreign_key('fk_bill_payments_po_advance_id', 'bill_payments', 'purchase_order_advances', ['po_advance_id'], ['id'])


def downgrade() -> None:
    op.drop_constraint('fk_bill_payments_po_advance_id', 'bill_payments', type_='foreignkey')
    op.drop_index(op.f('ix_bill_payments_po_advance_id'), table_name='bill_payments')
    op.drop_column('bill_payments', 'po_advance_id')
    op.drop_index(op.f('ix_purchase_order_advances_payment_line_id'), table_name='purchase_order_advances')
    op.drop_index(op.f('ix_purchase_order_advances_purchase_order_id'), table_name='purchase_order_advances')
    op.drop_table('purchase_order_advances')
