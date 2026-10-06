"""Pengujian jadwal kerja per hari (PRD 8.4, BR-09, BR-10)."""
from datetime import time

import pytest

from extensions import db
from models import AuditLog, Employee, WorkSchedule, WorkScheduleDay
from services import audit_service as audit
from services import schedule_service


def _logs(action):
    return db.session.execute(
        db.select(AuditLog).filter_by(action=action)
    ).scalars().all()


def _data(name="Jam Kerja Kantor", tolerance="15", days=None):
    """Membuat isian formulir. days = {weekday: ("JJ:MM", "JJ:MM")}."""
    if days is None:
        days = {w: ("07:30", "16:00") for w in range(4)}
        days[4] = ("07:30", "11:30")
    data = {"name": name, "late_tolerance_minutes": tolerance}
    for w, (start, end) in days.items():
        data[f"day_{w}_on"] = "1"
        data[f"day_{w}_start"] = start
        data[f"day_{w}_end"] = end
    return data


def _get(name):
    return db.session.execute(
        db.select(WorkSchedule).filter_by(name=name)
    ).scalar_one_or_none()


def _day_count():
    return db.session.execute(db.select(db.func.count(WorkScheduleDay.id))).scalar_one()


def test_halaman_jadwal_wajib_login(client):
    assert client.get("/admin/schedules/").status_code == 302
    assert client.post("/admin/schedules/new", data=_data()).status_code == 302
    assert _get("Jam Kerja Kantor") is None


def test_tambah_jadwal_senin_jumat_dengan_jumat_berbeda(auth_client):
    res = auth_client.post("/admin/schedules/new", data=_data(), follow_redirects=True)
    assert res.status_code == 200

    s = _get("Jam Kerja Kantor")
    assert [d.weekday for d in s.days] == [0, 1, 2, 3, 4]
    assert s.day_for(0).end_time == time(16, 0)
    assert s.day_for(4).end_time == time(11, 30)
    assert s.day_for(5) is None  # Sabtu libur
    assert s.day_for(6) is None  # Minggu libur

    logs = _logs(audit.SCHEDULE_CREATED)
    assert len(logs) == 1
    assert "Senin–Kamis 07:30–16:00" in logs[0].description
    assert "Jumat 07:30–11:30" in logs[0].description


def test_ringkasan_jam_menggabungkan_hari_berurutan(schedule):
    assert schedule_service.summarize_days(schedule) == [
        "Senin–Kamis 07:30–16:00",
        "Jumat 07:30–11:30",
    ]


def test_ringkasan_tidak_menggabungkan_hari_yang_tidak_berurutan(app):
    s = WorkSchedule(name="Selang", late_tolerance_minutes=0)
    for w in (0, 2):
        s.days.append(WorkScheduleDay(weekday=w, start_time=time(8), end_time=time(12)))
    db.session.add(s)
    db.session.commit()
    assert schedule_service.summarize_days(s) == ["Senin 08:00–12:00", "Rabu 08:00–12:00"]


def test_daftar_jadwal_menampilkan_ringkasan(auth_client, schedule):
    body = auth_client.get("/admin/schedules/").get_data(as_text=True)
    assert "Senin–Kamis 07:30–16:00" in body
    assert "Jumat 07:30–11:30" in body


def test_formulir_karyawan_menampilkan_ringkasan_jadwal(auth_client, schedule):
    body = auth_client.get("/admin/employees/new").get_data(as_text=True)
    assert "Jam Kerja Kantor (Senin–Kamis 07:30–16:00; Jumat 07:30–11:30)" in body


@pytest.mark.parametrize(
    "overrides,message",
    [
        ({"name": ""}, "Nama jadwal wajib diisi"),
        ({"tolerance": "-1"}, "Toleransi harus angka 0 sampai 180"),
        ({"tolerance": "181"}, "Toleransi harus angka 0 sampai 180"),
        ({"tolerance": "lima"}, "Toleransi harus angka 0 sampai 180"),
        ({"days": {}}, "Pilih minimal satu hari kerja"),
        ({"days": {0: ("25:00", "16:00")}}, "Senin: isi jam masuk dan jam pulang"),
        ({"days": {4: ("07:30", "")}}, "Jumat: isi jam masuk dan jam pulang"),
        ({"days": {2: ("08:00", "08:00")}}, "Rabu: jam pulang tidak boleh sama"),
    ],
)
def test_validasi_jadwal(auth_client, overrides, message):
    res = auth_client.post("/admin/schedules/new", data=_data(**overrides))
    assert res.status_code == 400
    assert message in res.get_data(as_text=True)
    assert db.session.execute(db.select(db.func.count(WorkSchedule.id))).scalar_one() == 0
    assert _day_count() == 0


def test_jam_hari_yang_tidak_dicentang_diabaikan(auth_client):
    data = _data(days={0: ("07:30", "16:00")})
    data["day_5_start"] = "08:00"  # Sabtu tidak dicentang
    data["day_5_end"] = "12:00"
    auth_client.post("/admin/schedules/new", data=data)
    s = _get("Jam Kerja Kantor")
    assert [d.weekday for d in s.days] == [0]


def test_nama_jadwal_duplikat_ditolak(auth_client):
    auth_client.post("/admin/schedules/new", data=_data())
    res = auth_client.post("/admin/schedules/new", data=_data(name="jam kerja kantor"))
    assert res.status_code == 400
    assert "sudah dipakai" in res.get_data(as_text=True)


def test_shift_lintas_tengah_malam_diterima_dan_ditandai(auth_client):
    auth_client.post(
        "/admin/schedules/new",
        data=_data(name="Shift Malam", days={w: ("22:00", "06:00") for w in range(7)}),
    )
    s = _get("Shift Malam")
    assert len(s.days) == 7
    assert schedule_service.crosses_midnight(s)
    assert "Lintas tengah malam" in auth_client.get("/admin/schedules/").get_data(as_text=True)


def test_ubah_jam_jumat_tercatat_dengan_rincian(auth_client, schedule):
    days = {w: ("07:30", "16:00") for w in range(4)}
    days[4] = ("07:30", "11:00")
    res = auth_client.post(f"/admin/schedules/{schedule.id}/edit", data=_data(days=days))
    assert res.status_code == 302

    db.session.refresh(schedule)
    assert schedule.day_for(4).end_time == time(11, 0)
    logs = _logs(audit.SCHEDULE_UPDATED)
    assert len(logs) == 1
    assert "Jumat 07:30–11:30" in logs[0].description
    assert "Jumat 07:30–11:00" in logs[0].description


def test_tambah_dan_hapus_hari_kerja(auth_client, schedule):
    """Menambah Sabtu dan menghapus Rabu tanpa bentrok batasan unik."""
    days = {w: ("07:30", "16:00") for w in (0, 1, 3)}
    days[4] = ("07:30", "11:30")
    days[5] = ("08:00", "12:00")
    res = auth_client.post(f"/admin/schedules/{schedule.id}/edit", data=_data(days=days))
    assert res.status_code == 302

    db.session.refresh(schedule)
    assert [d.weekday for d in schedule.days] == [0, 1, 3, 4, 5]
    assert schedule.day_for(2) is None
    assert _day_count() == 5  # baris Rabu benar-benar terhapus, tidak tertinggal


def test_simpan_tanpa_perubahan_tidak_menambah_log(auth_client, schedule):
    auth_client.post(f"/admin/schedules/{schedule.id}/edit", data=_data())
    assert _logs(audit.SCHEDULE_UPDATED) == []


def test_formulir_ubah_terisi_jam_yang_tersimpan(auth_client, schedule):
    body = auth_client.get(f"/admin/schedules/{schedule.id}/edit").get_data(as_text=True)
    assert 'name="day_4_end" aria-label="Jam pulang Jumat"' in body
    assert 'value="11:30"' in body


def test_jadwal_dipakai_karyawan_aktif_tidak_bisa_dinonaktifkan(auth_client, schedule):
    db.session.add(Employee(
        employee_code="K1", full_name="A", department="X", work_schedule_id=schedule.id
    ))
    db.session.commit()

    res = auth_client.post(
        f"/admin/schedules/{schedule.id}/deactivate", follow_redirects=True
    )
    assert "masih dipakai oleh 1 karyawan aktif" in res.get_data(as_text=True)
    db.session.refresh(schedule)
    assert schedule.is_active is True
    assert _logs(audit.SCHEDULE_DEACTIVATED) == []


def test_jadwal_tanpa_karyawan_aktif_bisa_dinonaktifkan_dan_diaktifkan(auth_client, schedule):
    db.session.add(Employee(
        employee_code="K2", full_name="B", department="X",
        work_schedule_id=schedule.id, is_active=False,
    ))
    db.session.commit()

    auth_client.post(f"/admin/schedules/{schedule.id}/deactivate")
    db.session.refresh(schedule)
    assert schedule.is_active is False
    assert len(schedule.days) == 5  # jam per hari tetap tersimpan

    auth_client.post(f"/admin/schedules/{schedule.id}/activate")
    db.session.refresh(schedule)
    assert schedule.is_active is True
    assert len(_logs(audit.SCHEDULE_DEACTIVATED)) == 1
    assert len(_logs(audit.SCHEDULE_ACTIVATED)) == 1
