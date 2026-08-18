"""add audit_logs

Revision ID: 9a1c4e6f2b7d
Revises: 4739672934a1
Create Date: 2026-08-18 00:00:00.000000

"""
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = '9a1c4e6f2b7d'
down_revision: Union[str, Sequence[str], None] = '4739672934a1'
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        'audit_logs',
        sa.Column('id', sa.Integer(), nullable=False),
        sa.Column('user_id', sa.Integer(), nullable=True),
        sa.Column('username', sa.String(length=80), nullable=True),
        sa.Column('entity_type', sa.String(length=50), nullable=False),
        sa.Column('entity_id', sa.Integer(), nullable=False),
        sa.Column('entity_label', sa.String(length=200), nullable=True),
        sa.Column('action', sa.String(length=30), nullable=False),
        sa.Column('field_name', sa.String(length=80), nullable=True),
        sa.Column('old_value', sa.Text(), nullable=True),
        sa.Column('new_value', sa.Text(), nullable=True),
        sa.Column('created_at', sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(['user_id'], ['users.id'], name='fk_audit_logs_user_id'),
        sa.PrimaryKeyConstraint('id'),
    )
    with op.batch_alter_table('audit_logs') as batch_op:
        batch_op.create_index('ix_audit_logs_entity_type', ['entity_type'])
        batch_op.create_index('ix_audit_logs_entity_id', ['entity_id'])
        batch_op.create_index('ix_audit_logs_created_at', ['created_at'])


def downgrade() -> None:
    """Downgrade schema."""
    with op.batch_alter_table('audit_logs') as batch_op:
        batch_op.drop_index('ix_audit_logs_created_at')
        batch_op.drop_index('ix_audit_logs_entity_id')
        batch_op.drop_index('ix_audit_logs_entity_type')
    op.drop_table('audit_logs')
