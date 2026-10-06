"""salinan jadwal dan metode di absensi

Revision ID: c4d1e8f2a9b3
Revises: b7e3a91c4d20
Create Date: 2026-10-06 17:30:00

Menambah kolom pada attendance_sessions (Tahap 4A):
- scheduled_start, scheduled_end : salinan jam jadwal saat check-in, agar
  perubahan jadwal di kemudian hari tidak mengubah penilaian catatan lama.
- late_minutes                   : menit keterlambatan saat check-in.
- check_in_method / check_out_method : sumber pencatatan (kamera / admin).
- check_in_score / check_out_score   : skor kecocokan wajah (bukan data biometrik).

Semua kolom baru boleh kosong, jadi data lama tidak berubah dan tidak ada
data yang dihapus.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'c4d1e8f2a9b3'
down_revision = 'b7e3a91c4d20'
branch_labels = None
depends_on = None


def upgrade():
    with op.batch_alter_table('attendance_sessions', schema=None) as batch_op:
        batch_op.add_column(sa.Column('scheduled_start', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('scheduled_end', sa.DateTime(), nullable=True))
        batch_op.add_column(sa.Column('late_minutes', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('check_in_method', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('check_out_method', sa.String(length=20), nullable=True))
        batch_op.add_column(sa.Column('check_in_score', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('check_out_score', sa.Float(), nullable=True))


def downgrade():
    with op.batch_alter_table('attendance_sessions', schema=None) as batch_op:
        batch_op.drop_column('check_out_score')
        batch_op.drop_column('check_in_score')
        batch_op.drop_column('check_out_method')
        batch_op.drop_column('check_in_method')
        batch_op.drop_column('late_minutes')
        batch_op.drop_column('scheduled_end')
        batch_op.drop_column('scheduled_start')
