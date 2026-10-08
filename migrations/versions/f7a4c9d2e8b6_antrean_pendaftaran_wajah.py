"""antrean pendaftaran wajah dari panel admin (lewat API)

Revision ID: f7a4c9d2e8b6
Revises: e6f3a2b8c1d5
Create Date: 2026-10-08 16:30:00

Menambah tabel face_enrollment_requests. Tidak ada data lama yang diubah.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'f7a4c9d2e8b6'
down_revision = 'e6f3a2b8c1d5'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table('face_enrollment_requests',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('employee_id', sa.Integer(), nullable=False),
    sa.Column('status', sa.String(length=20), nullable=False),
    sa.Column('replace_reason', sa.String(length=255), nullable=True),
    sa.Column('requested_via', sa.String(length=255), nullable=True),
    sa.Column('samples_taken', sa.Integer(), nullable=False),
    sa.Column('samples_total', sa.Integer(), nullable=False),
    sa.Column('message', sa.String(length=255), nullable=True),
    sa.Column('expires_at', sa.DateTime(), nullable=False),
    sa.Column('started_at', sa.DateTime(), nullable=True),
    sa.Column('finished_at', sa.DateTime(), nullable=True),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.ForeignKeyConstraint(['employee_id'], ['employees.id'], name=op.f('fk_face_enrollment_requests_employee_id_employees')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_face_enrollment_requests'))
    )
    with op.batch_alter_table('face_enrollment_requests', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_face_enrollment_requests_employee_id'), ['employee_id'], unique=False)
        batch_op.create_index(batch_op.f('ix_face_enrollment_requests_status'), ['status'], unique=False)


def downgrade():
    with op.batch_alter_table('face_enrollment_requests', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_face_enrollment_requests_status'))
        batch_op.drop_index(batch_op.f('ix_face_enrollment_requests_employee_id'))
    op.drop_table('face_enrollment_requests')
