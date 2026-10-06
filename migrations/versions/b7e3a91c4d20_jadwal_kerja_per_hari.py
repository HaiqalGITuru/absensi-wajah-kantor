"""jadwal kerja per hari

Revision ID: b7e3a91c4d20
Revises: aa16224f959c
Create Date: 2026-10-06 16:00:00

Memindahkan jam masuk/pulang dari tabel work_schedules ke tabel baru
work_schedule_days (satu baris per hari kerja).

Konversi data lama: setiap jadwal yang sudah ada diubah menjadi jadwal
Senin-Jumat dengan jam yang sama seperti sebelumnya. Setelah migrasi,
admin dapat mengubah jam hari tertentu (misalnya Jumat) lewat halaman
Jadwal kerja.

Urutan langkah sengaja dibuat begini agar data tidak hilang:
  1. buat tabel baru,
  2. salin jam dari kolom lama ke tabel baru,
  3. baru hapus kolom lama.
"""
from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision = 'b7e3a91c4d20'
down_revision = 'aa16224f959c'
branch_labels = None
depends_on = None

# 0 = Senin ... 4 = Jumat
WEEKDAYS_FOR_EXISTING_SCHEDULES = (0, 1, 2, 3, 4)


def upgrade():
    # 1. Tabel baru untuk jam per hari
    op.create_table('work_schedule_days',
    sa.Column('id', sa.Integer(), nullable=False),
    sa.Column('work_schedule_id', sa.Integer(), nullable=False),
    sa.Column('weekday', sa.SmallInteger(), nullable=False),
    sa.Column('start_time', sa.Time(), nullable=False),
    sa.Column('end_time', sa.Time(), nullable=False),
    sa.Column('created_at', sa.DateTime(), nullable=False),
    sa.Column('updated_at', sa.DateTime(), nullable=False),
    sa.CheckConstraint('weekday >= 0 AND weekday <= 6', name=op.f('ck_work_schedule_days_weekday_range')),
    sa.ForeignKeyConstraint(['work_schedule_id'], ['work_schedules.id'], name=op.f('fk_work_schedule_days_work_schedule_id_work_schedules')),
    sa.PrimaryKeyConstraint('id', name=op.f('pk_work_schedule_days')),
    sa.UniqueConstraint('work_schedule_id', 'weekday', name=op.f('uq_work_schedule_days_work_schedule_id_weekday'))
    )

    # 2. Salin jam lama ke setiap hari Senin-Jumat.
    #    Memakai INSERT ... SELECT agar nilai jam disalin langsung oleh
    #    database tanpa diubah formatnya oleh Python.
    for weekday in WEEKDAYS_FOR_EXISTING_SCHEDULES:
        op.execute(sa.text(
            "INSERT INTO work_schedule_days "
            "(work_schedule_id, weekday, start_time, end_time, created_at, updated_at) "
            f"SELECT id, {weekday}, start_time, end_time, created_at, updated_at "
            "FROM work_schedules"
        ))

    # 3. Baru setelah data aman, hapus kolom lama
    with op.batch_alter_table('work_schedules', schema=None) as batch_op:
        batch_op.drop_column('end_time')
        batch_op.drop_column('start_time')


def downgrade():
    # Mengembalikan kolom lama. Karena skema lama hanya punya satu jam untuk
    # semua hari, jam yang dipakai adalah jam hari kerja paling awal
    # (biasanya Senin). Perbedaan jam Jumat akan hilang saat downgrade.
    with op.batch_alter_table('work_schedules', schema=None) as batch_op:
        batch_op.add_column(sa.Column('start_time', sa.Time(), nullable=True))
        batch_op.add_column(sa.Column('end_time', sa.Time(), nullable=True))

    op.execute(sa.text(
        "UPDATE work_schedules SET "
        "start_time = COALESCE((SELECT d.start_time FROM work_schedule_days d "
        "  WHERE d.work_schedule_id = work_schedules.id "
        "  ORDER BY d.weekday LIMIT 1), '07:30:00'), "
        "end_time = COALESCE((SELECT d.end_time FROM work_schedule_days d "
        "  WHERE d.work_schedule_id = work_schedules.id "
        "  ORDER BY d.weekday LIMIT 1), '16:00:00')"
    ))

    with op.batch_alter_table('work_schedules', schema=None) as batch_op:
        batch_op.alter_column('start_time', existing_type=sa.Time(), nullable=False)
        batch_op.alter_column('end_time', existing_type=sa.Time(), nullable=False)

    op.drop_table('work_schedule_days')
