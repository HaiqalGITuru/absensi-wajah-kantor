"""Pengujian Tahap 4C: daftar absensi harian, koreksi, dan pembatalan (BR-08)."""
from datetime import date, datetime, time

import pytest
from sqlalchemy.exc import OperationalError

from extensions import db
from models import AttendanceSession, AuditLog, Employee, WorkSchedule, WorkScheduleDay
from services import ValidationError
from services import attendance_service as att
from services import audit_service as audit

MON = date(2026, 10, 5)
SAT = date(2026, 10, 10)
NOW = datetime(2026, 10, 5, 18, 0)  # "sekarang" untuk pengujian: Senin sore


def _logs(action):
    return db.session.execute(db.select(AuditLog).filter_by(action=action)).scalars().all()


@pytest.fixture
def emp(app, schedule):
    e = Employee(employee_code="K001", full_name="Budi Santoso", department="TI",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    return e


@pytest.fixture
def emp2(app, schedule):
    e = Employee(employee_code="K002", full_name="Sari Dewi", department="Umum",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    return e


def session_of(employee, d=MON):
    return db.session.execute(
        db.select(AttendanceSession).filter_by(employee_id=employee.id, work_date=d)
    ).scalar_one_or_none()


# ---------------------------------------------------------------- koreksi (layanan)

def test_tambah_absensi_lupa_tercatat(emp, admin_user):
    s, changed = att.correct(emp.id, MON, "07:40", "16:05",
                             "Kamera mati pagi hari, absen manual di buku tamu", admin_user.id, now=NOW)
    assert changed
    assert s.check_in_at == datetime(2026, 10, 5, 7, 40)
    assert s.check_out_at == datetime(2026, 10, 5, 16, 5)
    assert s.check_in_method == "admin" and s.check_out_method == "admin"
    assert s.status == AttendanceSession.STATUS_ON_TIME and s.late_minutes == 0
    assert s.scheduled_start == datetime(2026, 10, 5, 7, 30)

    log = _logs(audit.ATTENDANCE_CORRECTED)[0]
    assert log.user_id == admin_user.id
    assert "Kamera mati pagi hari" in log.description
    assert "'-' -> '05-10 07:40'" in log.description


def test_status_terlambat_dihitung_ulang_oleh_sistem(emp, admin_user):
    """BR-09: admin hanya mengisi jam; status dihitung dari jadwal."""
    s, _ = att.correct(emp.id, MON, "08:00", "", "Lupa absen, dikonfirmasi atasan", admin_user.id, now=NOW)
    assert s.status == AttendanceSession.STATUS_LATE and s.late_minutes == 15


def test_koreksi_hanya_jam_pulang_mempertahankan_sumber_kamera(emp, admin_user):
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 25), score=0.9)
    s, _ = att.correct(emp.id, MON, "07:25", "16:10", "Lupa lewat kamera saat pulang", admin_user.id, now=NOW)
    assert s.check_in_method == "kamera" and s.check_in_score == pytest.approx(0.9)
    assert s.check_out_method == "admin" and s.check_out_score is None


@pytest.mark.parametrize(
    "check_in,check_out,reason,field,message",
    [
        ("07:30", "", "pendek", "reason", "minimal 10 karakter"),
        ("", "16:00", "Alasan yang cukup panjang", "check_in", "wajib diisi"),
        ("25:00", "", "Alasan yang cukup panjang", "check_in", "tidak valid"),
        ("07:30", "abc", "Alasan yang cukup panjang", "check_out", "tidak valid"),
        ("19:00", "", "Alasan yang cukup panjang", "check_in", "masa depan"),
    ],
)
def test_validasi_koreksi(emp, admin_user, check_in, check_out, reason, field, message):
    with pytest.raises(ValidationError) as exc:
        att.correct(emp.id, MON, check_in, check_out, reason, admin_user.id, now=NOW)
    assert message in exc.value.errors[field]
    assert session_of(emp) is None
    assert _logs(audit.ATTENDANCE_CORRECTED) == []


def test_jam_pulang_lebih_awal_dianggap_hari_berikutnya(emp, admin_user):
    later = datetime(2026, 10, 7, 12, 0)
    s, _ = att.correct(emp.id, MON, "07:30", "01:30", "Lembur sampai dini hari", admin_user.id, now=later)
    assert s.check_out_at == datetime(2026, 10, 6, 1, 30)


def test_koreksi_hari_libur_tanpa_jadwal(emp, admin_user):
    later = datetime(2026, 10, 10, 18, 0)
    s, _ = att.correct(emp.id, SAT, "08:00", "12:00", "Masuk kerja bakti hari Sabtu", admin_user.id, now=later)
    assert s.scheduled_start is None and s.late_minutes == 0
    assert s.status == AttendanceSession.STATUS_ON_TIME


def test_simpan_tanpa_perubahan_tidak_menambah_log(emp, admin_user):
    att.correct(emp.id, MON, "07:40", "", "Koreksi pertama yang valid", admin_user.id, now=NOW)
    _, changed = att.correct(emp.id, MON, "07:40", "", "Koreksi kedua tanpa perubahan", admin_user.id, now=NOW)
    assert not changed
    assert len(_logs(audit.ATTENDANCE_CORRECTED)) == 1


def test_shift_malam_jam_masuk_setelah_tengah_malam(app, admin_user):
    s = WorkSchedule(name="Malam", late_tolerance_minutes=10)
    for w in range(7):
        s.days.append(WorkScheduleDay(weekday=w, start_time=time(22, 0), end_time=time(6, 0)))
    e = Employee(employee_code="N1", full_name="Satpam", department="Umum", work_schedule=s)
    db.session.add_all([s, e])
    db.session.commit()
    later = datetime(2026, 10, 6, 12, 0)
    sess, _ = att.correct(e.id, MON, "00:30", "06:00", "Terlambat karena ban bocor", admin_user.id, now=later)
    assert sess.check_in_at == datetime(2026, 10, 6, 0, 30)
    assert sess.late_minutes == 140


# ---------------------------------------------------------------- pembatalan

def test_pembatalan_salah_kenal_lalu_orang_benar_bisa_absen(emp, emp2, admin_user):
    """Kamera keliru mencatat Budi; admin membatalkan; Budi asli tetap bisa absen."""
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 20), score=0.41)
    s = session_of(emp)
    att.cancel(s.id, "Kamera salah mengenali Sari sebagai Budi", admin_user.id)

    db.session.refresh(s)
    assert s.check_in_at is None and s.status == AttendanceSession.STATUS_CANCELLED
    log = _logs(audit.ATTENDANCE_CANCELLED)[0]
    assert "sebelumnya masuk 05-10 07:20" in log.description
    assert "salah mengenali" in log.description

    d = att.record(emp.id, now=datetime(2026, 10, 5, 7, 35), score=0.9)
    assert d.action == att.CHECKED_IN
    db.session.refresh(s)
    assert s.check_in_at == datetime(2026, 10, 5, 7, 35)
    assert len(db.session.execute(db.select(AttendanceSession)).scalars().all()) == 1


def test_pembatalan_butuh_alasan(emp, admin_user):
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 20))
    with pytest.raises(ValidationError) as exc:
        att.cancel(session_of(emp).id, "  ", admin_user.id)
    assert "reason" in exc.value.errors
    assert session_of(emp).check_in_at is not None


def test_tidak_bisa_membatalkan_yang_kosong(emp, admin_user):
    with pytest.raises(ValidationError):
        att.cancel(9999, "Alasan cukup panjang untuk uji", admin_user.id)


# ---------------------------------------------------------------- daftar harian

def test_daftar_harian_menampilkan_yang_belum_masuk(emp, emp2):
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 50))
    rows = att.daily_roster(MON)
    by_code = {r["employee"].employee_code: r for r in rows}
    assert by_code["K001"]["late"] and by_code["K001"]["label"] == "Terlambat 5 mnt"
    assert by_code["K002"]["label"] == "Belum masuk"

    assert [r["employee"].employee_code for r in att.daily_roster(MON, status="absent")] == ["K002"]
    assert [r["employee"].employee_code for r in att.daily_roster(MON, status="late")] == ["K001"]
    assert [r["employee"].employee_code for r in att.daily_roster(MON, department="Umum")] == ["K002"]
    assert [r["employee"].employee_code for r in att.daily_roster(MON, q="sari")] == ["K002"]


def test_absensi_dibatalkan_dihitung_belum_masuk(auth_client, emp, emp2, admin_user):
    """Setelah dibatalkan, karyawan memang belum tercatat masuk."""
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 20))
    att.cancel(session_of(emp).id, "Kamera salah mengenali orang lain", admin_user.id)
    codes = [r["employee"].employee_code for r in att.daily_roster(MON, status="absent")]
    assert codes == ["K001", "K002"]
    body = auth_client.get("/admin/attendance/?date=2026-10-05").get_data(as_text=True)
    assert "0 sudah masuk (0 terlambat), 2 belum masuk" in body
    assert "Dibatalkan" in body


def test_hari_libur_tidak_dihitung_belum_masuk(emp):
    rows = att.daily_roster(SAT)
    assert rows[0]["label"] == "Libur"
    assert att.daily_roster(SAT, status="absent") == []


def test_karyawan_nonaktif_tetap_tampil_jika_punya_catatan(emp):
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 20))
    emp.is_active = False
    db.session.commit()
    assert [r["employee"].employee_code for r in att.daily_roster(MON)] == ["K001"]
    assert att.daily_roster(MON + (date(2026, 10, 6) - MON)) == []


# ---------------------------------------------------------------- halaman

def test_halaman_absensi_wajib_login(client):
    assert client.get("/admin/attendance/").status_code == 302
    assert client.post("/admin/attendance/cancel", data={"session_id": 1}).status_code == 302


def test_halaman_daftar_absensi(auth_client, emp, emp2):
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 50))
    body = auth_client.get("/admin/attendance/?date=2026-10-05").get_data(as_text=True)
    assert "Senin, 05-10-2026" in body
    assert "Terlambat 5 mnt" in body and "Belum masuk" in body
    assert "1 sudah masuk (1 terlambat), 1 belum masuk" in body


def test_koreksi_lewat_halaman(auth_client, emp, admin_user):
    url = f"/admin/attendance/correct?employee_id={emp.id}&date=2026-10-05"
    assert auth_client.get(url).status_code == 200

    res = auth_client.post(url, data={"check_in": "07:45", "check_out": "",
                                      "reason": "pendek"})
    assert res.status_code == 400 and "minimal 10 karakter" in res.get_data(as_text=True)

    res = auth_client.post(url, data={"check_in": "07:45", "check_out": "16:00",
                                      "reason": "Kamera mati, dikonfirmasi atasan"})
    assert res.status_code == 302
    s = session_of(emp)
    assert s.check_in_at == datetime(2026, 10, 5, 7, 45) and s.check_in_method == "admin"
    assert _logs(audit.ATTENDANCE_CORRECTED)[0].user_id == admin_user.id


def test_batalkan_lewat_halaman(auth_client, emp):
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 20))
    s = session_of(emp)
    res = auth_client.post("/admin/attendance/cancel",
                           data={"session_id": s.id, "reason": "Salah mengenali orang lain"})
    assert res.status_code == 302
    db.session.refresh(s)
    assert s.status == AttendanceSession.STATUS_CANCELLED


def test_tanggal_masa_depan_tidak_bisa_dikoreksi(auth_client, emp):
    res = auth_client.get(f"/admin/attendance/correct?employee_id={emp.id}&date=2099-01-01")
    assert res.status_code == 302


def test_menu_absensi_ada_di_navigasi(auth_client):
    assert 'href="/admin/attendance/"' in auth_client.get("/admin").get_data(as_text=True)


# ---------------------------------------------------------------- database mati

def test_perintah_terminal_memberi_pesan_jelas_saat_database_mati(app, monkeypatch):
    def down(*a, **k):
        raise OperationalError("SELECT", {}, Exception("Can't connect to MySQL server"))

    monkeypatch.setattr(db.session, "execute", down)
    res = app.test_cli_runner().invoke(
        args=["attendance-simulate", "--employee", "K001", "--at", "2026-10-05 07:30"])
    assert res.exit_code != 0
    assert "MySQL di XAMPP/Laragon sudah dinyalakan" in res.output
    assert "Traceback" not in res.output
