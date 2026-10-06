"""Pengujian layanan absensi Tahap 4A (FR-08, FR-09, FR-10, BR-01 s.d. BR-10).

Jadwal uji (fixture `schedule`): Senin-Kamis 07:30-16:00, Jumat 07:30-11:30,
toleransi 15 menit, Sabtu-Minggu libur. Kalender: 5 Okt 2026 = Senin.
"""
from datetime import date, datetime, time, timedelta

import pytest
from sqlalchemy.exc import IntegrityError, OperationalError

from extensions import db
from models import AttendanceSession, Employee, WorkSchedule, WorkScheduleDay
from services import attendance_service as att

MON = date(2026, 10, 5)
FRI = date(2026, 10, 9)
SAT = date(2026, 10, 10)


def at(day, hh, mm=0):
    return datetime.combine(day, time(hh, mm))


@pytest.fixture
def emp(app, schedule):
    e = Employee(employee_code="K001", full_name="Haiqal Ramadhan", department="APTIKA",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    return e


def sessions(employee_id=None):
    stmt = db.select(AttendanceSession)
    if employee_id:
        stmt = stmt.filter_by(employee_id=employee_id)
    return db.session.execute(stmt).scalars().all()


S = att.AttendanceSettings()  # nilai bawaan: 120 / 240 / 6 jam / update aktif


# ---------------------------------------------------------------- check-in

def test_check_in_tepat_waktu(emp):
    """TC-11: check-in otomatis tersimpan dengan waktu server."""
    d = att.record(emp.id, now=at(MON, 7, 20), score=0.91, settings=S)
    assert d.action == att.CHECKED_IN and d.recorded
    assert "Selamat datang, Haiqal" in d.message
    s = sessions(emp.id)[0]
    assert s.work_date == MON and s.check_in_at == at(MON, 7, 20)
    assert s.status == AttendanceSession.STATUS_ON_TIME and s.late_minutes == 0
    assert s.check_in_method == "kamera" and s.check_in_score == pytest.approx(0.91)


@pytest.mark.parametrize("hh,mm,late", [(7, 45, 0), (7, 46, 1), (8, 15, 30), (15, 59, 494)])
def test_batas_toleransi_terlambat(emp, hh, mm, late):
    """BR-09: terlambat ditentukan jadwal + toleransi, bukan oleh model AI."""
    d = att.record(emp.id, now=at(MON, hh, mm), settings=S)
    assert d.action == att.CHECKED_IN
    assert d.late_minutes == late
    expected = AttendanceSession.STATUS_LATE if late else AttendanceSession.STATUS_ON_TIME
    assert sessions(emp.id)[0].status == expected


def test_terlalu_pagi_ditolak(emp):
    d = att.record(emp.id, now=at(MON, 5, 20), settings=S)
    assert d.action == att.REJECT_TOO_EARLY and not d.recorded
    assert "05:30" in d.message
    assert sessions() == []


def test_lewat_jam_pulang_tanpa_check_in_ditolak(emp):
    """Kebijakan: tidak ada check-in lalu lewat saat jam pulang -> ke admin."""
    d = att.record(emp.id, now=at(MON, 16, 30), settings=S)
    assert d.action == att.REJECT_NO_CHECKIN and not d.recorded
    assert "Hubungi admin" in d.message
    assert sessions() == []


def test_hari_libur_ditolak(emp):
    d = att.record(emp.id, now=at(SAT, 8, 0), settings=S)
    assert d.action == att.REJECT_NOT_WORKDAY
    assert sessions() == []


def test_karyawan_nonaktif_ditolak(emp):
    """TC-15 / BR-02."""
    emp.is_active = False
    db.session.commit()
    d = att.record(emp.id, now=at(MON, 7, 20), settings=S)
    assert d.action == att.REJECT_INACTIVE and not d.recorded
    assert sessions() == []


def test_jadwal_nonaktif_ditolak(emp, schedule):
    schedule.is_active = False
    db.session.commit()
    assert att.record(emp.id, now=at(MON, 7, 20), settings=S).action == att.REJECT_NO_SCHEDULE


def test_karyawan_tidak_ada(app):
    assert att.record(9999, now=at(MON, 7, 20), settings=S).action == att.REJECT_UNKNOWN_EMPLOYEE


def test_salinan_jadwal_tersimpan_saat_check_in(emp):
    att.record(emp.id, now=at(FRI, 7, 30), settings=S)
    s = sessions(emp.id)[0]
    assert s.scheduled_start == at(FRI, 7, 30)
    assert s.scheduled_end == at(FRI, 11, 30)  # Jumat pulang lebih awal


def test_perubahan_jadwal_tidak_mengubah_catatan_lama(emp, schedule):
    att.record(emp.id, now=at(MON, 7, 50), settings=S)  # terlambat 5 menit
    schedule.day_for(0).start_time = time(8, 0)
    db.session.commit()
    s = sessions(emp.id)[0]
    assert s.scheduled_start == at(MON, 7, 30)
    assert s.late_minutes == 5


# ---------------------------------------------------------------- pengulangan

def test_lewat_lagi_sebelum_batas_minimal_tidak_dicatat(emp):
    """TC-12: check-in berulang tidak membuat catatan ganda."""
    att.record(emp.id, now=at(MON, 7, 20), settings=S)
    for minutes in (1, 30, 239):
        d = att.record(emp.id, now=at(MON, 7, 20) + timedelta(minutes=minutes), settings=S)
        assert d.action == att.ALREADY_IN and not d.recorded
        assert "sudah tercatat masuk pukul 07:20" in d.message
    s = sessions(emp.id)
    assert len(s) == 1 and s[0].check_out_at is None


# ---------------------------------------------------------------- check-out

def test_check_out_setelah_batas_minimal(emp):
    """TC-13: check-out valid tersimpan dan terkait sesi check-in."""
    att.record(emp.id, now=at(MON, 7, 20), settings=S)
    d = att.record(emp.id, now=at(MON, 16, 5), score=0.88, settings=S)
    assert d.action == att.CHECKED_OUT and d.recorded and not d.left_early
    s = sessions(emp.id)
    assert len(s) == 1
    assert s[0].check_out_at == at(MON, 16, 5) and s[0].check_out_method == "kamera"


def test_pulang_cepat_ditandai(emp):
    att.record(emp.id, now=at(MON, 7, 20), settings=S)
    d = att.record(emp.id, now=at(MON, 12, 0), settings=S)
    assert d.action == att.CHECKED_OUT and d.left_early
    assert "sebelum jam pulang" in d.message
    assert sessions(emp.id)[0].left_early


def test_jam_pulang_mengikuti_lintasan_terakhir(emp):
    """Keluar makan siang lalu pulang sore: jam pulang = lintasan terakhir."""
    att.record(emp.id, now=at(MON, 7, 20), settings=S)
    att.record(emp.id, now=at(MON, 12, 0), settings=S)
    d = att.record(emp.id, now=at(MON, 16, 10), settings=S)
    assert d.action == att.CHECKOUT_UPDATED and d.recorded and not d.left_early
    s = sessions(emp.id)
    assert len(s) == 1 and s[0].check_out_at == at(MON, 16, 10)


def test_mode_jam_pulang_pertama(emp):
    """TC-14: dengan pembaruan dimatikan, check-out berulang tidak mengubah apa pun."""
    first = att.AttendanceSettings(update_checkout=False)
    att.record(emp.id, now=at(MON, 7, 20), settings=first)
    att.record(emp.id, now=at(MON, 16, 0), settings=first)
    d = att.record(emp.id, now=at(MON, 17, 0), settings=first)
    assert d.action == att.ALREADY_OUT and not d.recorded
    assert sessions(emp.id)[0].check_out_at == at(MON, 16, 0)


def test_lewat_setelah_masa_check_out_tidak_salah_pesan(emp):
    att.record(emp.id, now=at(MON, 7, 20), settings=S)
    d = att.record(emp.id, now=at(MON, 23, 0), settings=S)
    assert d.action == att.ALREADY_OUT
    assert "tidak ada catatan masuk" not in d.message
    assert len(sessions(emp.id)) == 1


def test_hari_berikutnya_sesi_baru(emp):
    att.record(emp.id, now=at(MON, 7, 20), settings=S)
    att.record(emp.id, now=at(MON, 16, 5), settings=S)
    d = att.record(emp.id, now=at(MON + timedelta(days=1), 7, 25), settings=S)
    assert d.action == att.CHECKED_IN
    assert [s.work_date for s in sessions(emp.id)] == [MON, MON + timedelta(days=1)]


def test_lupa_check_out_kemarin_tidak_menghalangi_check_in_hari_ini(emp):
    att.record(emp.id, now=at(MON, 7, 20), settings=S)
    d = att.record(emp.id, now=at(MON + timedelta(days=1), 7, 25), settings=S)
    assert d.action == att.CHECKED_IN
    assert sessions(emp.id)[0].check_out_at is None


# ---------------------------------------------------------------- shift malam

@pytest.fixture
def night_emp(app):
    s = WorkSchedule(name="Shift Malam", late_tolerance_minutes=10)
    for w in range(7):
        s.days.append(WorkScheduleDay(weekday=w, start_time=time(22, 0), end_time=time(6, 0)))
    e = Employee(employee_code="N001", full_name="Satpam Malam", department="Umum", work_schedule=s)
    db.session.add_all([s, e])
    db.session.commit()
    return e


def test_shift_malam_check_out_setelah_tengah_malam(night_emp):
    """BR-10: check-out setelah tengah malam masuk ke sesi tanggal mulai shift."""
    att.record(night_emp.id, now=at(MON, 21, 55), settings=S)
    d = att.record(night_emp.id, now=at(MON + timedelta(days=1), 6, 5), settings=S)
    assert d.action == att.CHECKED_OUT
    s = sessions(night_emp.id)
    assert len(s) == 1 and s[0].work_date == MON


def test_shift_malam_terlambat_setelah_tengah_malam(night_emp):
    d = att.record(night_emp.id, now=at(MON + timedelta(days=1), 0, 30), settings=S)
    assert d.action == att.CHECKED_IN and d.late_minutes == 140
    assert sessions(night_emp.id)[0].work_date == MON


# ---------------------------------------------------------------- konsistensi & gangguan

def test_tidak_ada_yang_tersimpan_saat_dry_run(emp):
    d = att.record(emp.id, now=at(MON, 7, 20), settings=S, dry_run=True)
    assert d.action == att.CHECKED_IN
    assert sessions() == []


def test_database_gagal_tidak_dinyatakan_berhasil(emp, monkeypatch):
    """TC-17 / BR-07: kegagalan penyimpanan tidak boleh tampil sebagai berhasil."""
    def broken():
        raise OperationalError("COMMIT", {}, Exception("database terputus"))

    monkeypatch.setattr(db.session, "commit", broken)
    d = att.record(emp.id, now=at(MON, 7, 20), settings=S)
    assert d.action == att.ERROR and not d.recorded
    assert "BELUM tercatat" in d.message
    monkeypatch.undo()
    assert sessions() == []


def test_dua_proses_bersamaan_tidak_membuat_sesi_ganda(emp, monkeypatch):
    """TC-20: jika proses lain lebih dulu menyimpan sesi yang sama, batasan unik
    database menolak sesi kedua dan hasilnya bukan kesalahan fatal."""
    att.record(emp.id, now=at(MON, 7, 20), settings=S)

    real_commit = db.session.commit

    def racing_commit():
        raise IntegrityError("INSERT", {}, Exception("Duplicate entry"))

    # Simulasikan proses kedua yang tidak melihat sesi pertama.
    monkeypatch.setattr(att, "_active_session", lambda *a: None)
    monkeypatch.setattr(db.session, "commit", racing_commit)
    d = att.record(emp.id, now=at(MON, 7, 21), settings=S)
    monkeypatch.setattr(db.session, "commit", real_commit)

    assert not d.recorded
    assert len(sessions(emp.id)) == 1


def test_batasan_unik_database_menolak_sesi_ganda(emp):
    """BR-05: pencegahan duplikasi juga ada di tingkat database."""
    db.session.add(AttendanceSession(employee_id=emp.id, work_date=MON, status="hadir"))
    db.session.commit()
    db.session.add(AttendanceSession(employee_id=emp.id, work_date=MON, status="hadir"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_pengaturan_dari_config(app):
    s = att.AttendanceSettings.from_config(app.config)
    assert (s.checkin_early_minutes, s.checkout_min_minutes, s.checkout_grace_hours) == (120, 240, 6)
    assert s.update_checkout is True


# ---------------------------------------------------------------- perintah simulasi

def test_cli_simulasi_tidak_menyimpan(app, emp):
    res = app.test_cli_runner().invoke(
        args=["attendance-simulate", "--employee", "k001", "--at", "2026-10-05 07:50"])
    assert res.exit_code == 0, res.output
    assert "checked_in" in res.output and "Terlambat : 5 menit" in res.output
    assert "Senin 05-10-2026 07:50 (tidak disimpan)" in res.output
    assert sessions() == []


def test_cli_simulasi_format_waktu_salah(app, emp):
    res = app.test_cli_runner().invoke(
        args=["attendance-simulate", "--employee", "K001", "--at", "besok pagi"])
    assert res.exit_code != 0 and "Format --at" in res.output
