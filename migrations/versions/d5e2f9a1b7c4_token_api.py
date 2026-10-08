"""token API baca-saja

Revision ID: d5e2f9a1b7c4
Revises: c4d1e8f2a9b3
Create Date: 2026-10-08 09:00:00

Menambah tabel api_tokens untuk API baca-saja. Hanya SHA-256 token yang
disimpan; token asli ditampilkan sekali saat dibuat. Tidak ada data lama yang
diubah atau dihapus.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'd5e2f9a1b7c4'
down_revision = 'c4d1e8f2a9b3'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('api_tokens',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('name', sa.String(length=100), nullable=False),
    sa.Column('token_hash', sa.String(length=64), nullable=False),
    sa.Column('prefix', sa.String(length=12), nullable=False),
    sa.Column('created_by_id', sa.Integer(), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=True),
    sa.Column('last_used_at', sa.DateTime(), nullable=True),
    sa.Column('revoked_at', sa.DateTime(), nullable=True),
    sa.Column('revoke_reason', sa.String(length=255), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['created_by_id'], ['users.id'], name=op.f('fk_api_tokens_created_by_id_users')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_api_tokens')),
    sa.UniqueConstraint('token_hash', name=op.f('uq_api_tokens_token_hash'))
    )


def downgrade():
    op.drop_table('api_tokens')
