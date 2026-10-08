"""cakupan token API (read/write)

Revision ID: e6f3a2b8c1d5
Revises: d5e2f9a1b7c4
Create Date: 2026-10-08 10:00:00

Menambah kolom scope pada api_tokens. Token yang sudah ada otomatis "read"
(hanya baca), sehingga tidak ada token lama yang tiba-tiba bisa mengubah data.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'e6f3a2b8c1d5'
down_revision = 'd5e2f9a1b7c4'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scope', sa.String(length=10), server_default='read', nullable=False))


def downgrade():
    with op.batch_alter_table('api_tokens', schema=None) as batch_op:
        batch_op.drop_column('scope')
