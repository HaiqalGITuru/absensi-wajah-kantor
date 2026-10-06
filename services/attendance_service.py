"""
Layanan absensi (PRD 7.5, FR-08, FR-09, FR-10, BR-01 sampai BR-10).

Satu-satunya pintu untuk membuat atau mengubah catatan kehadiran dari kamera.
Kamera dan model AI TIDAK boleh menulis ke tabel absensi secara langsung
(PRD 7.7); mereka hanya memanggil record() dengan employee_id yang sudah
dipastikan oleh pelacak.

Aturan kantor (disepakati Tahap 4A, bisa diatur di .env):
- Check-in diterima dari CHECKIN_EARLY_MINUTES sebelum jam masuk sampai jam
  pulang jadwal hari itu. Lewat jam masuk + toleransi = terlambat.
- Check-out diterima kapan saja setelah CHECKOUT_MIN_MINUTES sejak check-in.
  Sebelum jam pulang jadwal ditandai pulang cepat.
- Jika CHECKOUT_UPDATE_LATER aktif, lintasan berikutnya memperbarui jam pulang
  (jam pulang = lintasan terakhir). Catatan tetap satu, tidak ganda.
- Lewat kamera setelah jam pulang tanpa check-in: DITOLAK, ke admin.
- Hari tanpa jam kerja di jadwal (libur): ditolak.
- Waktu resmi selalu waktu server zona kantor (BR-06), bukan waktu browser.
"""
import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy.exc import IntegrityError, SQLAlchemyError

from extensions import db
from models import AttendanceSession, Employee, office_now

# Hasil keputusan
CHECKED_IN = "checked_in"
CHECKED_OUT = "checked_out"
CHECKOUT_UPDATED = "checkout_updated"
ALREADY_IN = "already_in"
ALREADY_OUT = "already_out"
REJECT_INACTIVE = "reject_inactive"
REJECT_NO_SCHEDULE = "reject_no_schedule"
REJECT_NOT_WORKDAY = "reject_not_workday"
REJECT_TOO_EARLY = "reject_too_early"
REJECT_NO_CHECKIN = "reject_no_checkin"
REJECT_UNKNOWN_EMPLOYEE = "reject_unknown_employee"
ERROR = "error"

RECORDED = {CHECKED_IN, CHECKED_OUT, CHECKOUT_UPDATED}

METHOD_CAMERA = "kamera"


@dataclass
class AttendanceSettings:
    checkin_early_minutes: int = 120
    checkout_min_minutes: int = 240
    checkout_grace_hours: int = 6
    update_checkout: bool = True

    @classmethod
    def from_config(cls, config):
        return cls(
            checkin_early_minutes=int(config.get("CHECKIN_EARLY_MINUTES", cls.checkin_early_minutes)),
            checkout_min_minutes=int(config.get("CHECKOUT_MIN_MINUTES", cls.checkout_min_minutes)),
            checkout_grace_hours=int(config.get("CHECKOUT_GRACE_HOURS", cls.checkout_grace_hours)),
            update_checkout=str(config.get("CHECKOUT_UPDATE_LATER", "true")).lower()
            in ("1", "true", "ya", "yes"),
        )


@dataclass
class Decision:
    action: str
    message: str
    employee_id: int = None
    session_id: int = None
    late_minutes: int = 0
    left_early: bool = False

    @property
    def recorded(self):
        """True HANYA jika perubahan sudah tersimpan di database (FR-08)."""
        return self.action in RECORDED


def shift_window(work_date, rule):
    """Waktu mulai dan selesai shift. Shift lintas tengah malam berakhir besok."""
    start = datetime.combine(work_date, rule.start_time)
    end = datetime.combine(work_date, rule.end_time)
    if end <= start:
        end += timedelta(days=1)
    return start, end


def _hm(dt):
    return dt.strftime("%H:%M")


def _first_name(employee):
    return employee.full_name.split()[0]


def _active_session(employee, now, s):
    """Sesi yang sudah check-in dan masih dalam masa check-out."""
    today = now.date()
    stmt = (
        db.select(AttendanceSession)
        .where(
            AttendanceSession.employee_id == employee.id,
            AttendanceSession.work_date.in_([today, today - timedelta(days=1)]),
            AttendanceSession.check_in_at.is_not(None),
        )
        .order_by(AttendanceSession.work_date.desc())
        .with_for_update()
    )
    for session in db.session.execute(stmt).scalars():
        end = session.scheduled_end or datetime.combine(session.work_date, datetime.max.time())
        if now <= end + timedelta(hours=s.checkout_grace_hours):
            return session
    return None


def _checkin_window(schedule, now, s):
    """Mencari tanggal kerja yang jendelanya mencakup `now` (hari ini atau shift
    malam kemarin). Mengembalikan (work_date, rule, start, end) atau None."""
    early = timedelta(minutes=s.checkin_early_minutes)
    for work_date in (now.date(), now.date() - timedelta(days=1)):
        rule = schedule.day_for(work_date.weekday())
        if rule is None:
            continue
        start, end = shift_window(work_date, rule)
        if start - early <= now <= end:
            return work_date, rule, start, end
    return None


def _reject_reason(employee, schedule, now, s):
    rule = schedule.day_for(now.weekday())
    name = _first_name(employee)
    if rule is None:
        return Decision(REJECT_NOT_WORKDAY,
                        f"{name}, hari ini bukan hari kerja sesuai jadwal Anda. Hubungi admin.",
                        employee.id)
    start, end = shift_window(now.date(), rule)
    if now < start - timedelta(minutes=s.checkin_early_minutes):
        return Decision(REJECT_TOO_EARLY,
                        f"{name}, absen masuk dibuka pukul "
                        f"{_hm(start - timedelta(minutes=s.checkin_early_minutes))}.",
                        employee.id)
    return Decision(REJECT_NO_CHECKIN,
                    f"{name}, tidak ada catatan masuk hari ini. Hubungi admin untuk koreksi.",
                    employee.id)


def _decide(employee_id, now, score, s):
    employee = db.session.get(Employee, employee_id)
    if employee is None:
        return Decision(REJECT_UNKNOWN_EMPLOYEE, "Data karyawan tidak ditemukan.", employee_id)
    name = _first_name(employee)
    if not employee.is_active:
        return Decision(REJECT_INACTIVE, f"{name}, akun Anda nonaktif. Hubungi admin.", employee.id)
    schedule = employee.work_schedule
    if schedule is None or not schedule.is_active:
        return Decision(REJECT_NO_SCHEDULE,
                        f"{name}, jadwal kerja Anda belum diatur. Hubungi admin.", employee.id)

    session = _active_session(employee, now, s)
    if session is not None:
        if session.check_out_at is None:
            if now - session.check_in_at < timedelta(minutes=s.checkout_min_minutes):
                return Decision(ALREADY_IN,
                                f"{name}, Anda sudah tercatat masuk pukul {_hm(session.check_in_at)}.",
                                employee.id, session.id)
            session.check_out_at = now
            session.check_out_method = METHOD_CAMERA
            session.check_out_score = score
            early = session.left_early
            return Decision(CHECKED_OUT,
                            f"Sampai jumpa, {name}. Pulang tercatat {_hm(now)}"
                            + (" (sebelum jam pulang)." if early else "."),
                            employee.id, session.id, session.late_minutes or 0, early)

        if s.update_checkout and now > session.check_out_at:
            session.check_out_at = now
            session.check_out_score = score
            early = session.left_early
            return Decision(CHECKOUT_UPDATED,
                            f"Sampai jumpa, {name}. Jam pulang diperbarui ke {_hm(now)}.",
                            employee.id, session.id, session.late_minutes or 0, early)
        return Decision(ALREADY_OUT,
                        f"{name}, Anda sudah tercatat pulang pukul {_hm(session.check_out_at)}.",
                        employee.id, session.id)

    window = _checkin_window(schedule, now, s)
    if window is None:
        done = db.session.execute(
            db.select(AttendanceSession).where(
                AttendanceSession.employee_id == employee.id,
                AttendanceSession.work_date == now.date(),
                AttendanceSession.check_in_at.is_not(None),
            )
        ).scalar_one_or_none()
        if done is not None:
            return Decision(ALREADY_OUT,
                            f"{name}, absensi hari ini sudah tercatat. Hubungi admin bila perlu koreksi.",
                            employee.id, done.id)
        return _reject_reason(employee, schedule, now, s)

    work_date, rule, start, end = window
    deadline = start + timedelta(minutes=schedule.late_tolerance_minutes)
    late = max(0, math.ceil((now - deadline).total_seconds() / 60)) if now > deadline else 0

    session = db.session.execute(
        db.select(AttendanceSession).filter_by(employee_id=employee.id, work_date=work_date)
    ).scalar_one_or_none()
    if session is not None and session.check_in_at is not None:
        # Sesi tanggal ini sudah selesai di luar masa check-out.
        return Decision(ALREADY_OUT,
                        f"{name}, absensi hari ini sudah lengkap.", employee.id, session.id)
    if session is None:
        session = AttendanceSession(employee_id=employee.id, work_date=work_date)
        db.session.add(session)

    session.work_schedule_id = schedule.id
    session.check_in_at = now
    session.check_in_method = METHOD_CAMERA
    session.check_in_score = score
    session.scheduled_start = start
    session.scheduled_end = end
    session.late_minutes = late
    session.status = AttendanceSession.STATUS_LATE if late else AttendanceSession.STATUS_ON_TIME
    db.session.flush()

    message = f"Selamat datang, {name}. Masuk tercatat {_hm(now)}"
    message += f" (terlambat {late} menit)." if late else "."
    return Decision(CHECKED_IN, message, employee.id, session.id, late)


def record(employee_id, now=None, score=None, settings=None, dry_run=False):
    """Memproses satu peristiwa "karyawan dipastikan" dari kamera.

    Mengembalikan Decision. Decision.recorded True HANYA jika transaksi
    benar-benar tersimpan. Jika database gagal, hasilnya ERROR dan tidak ada
    yang dinyatakan berhasil (BR-07, FR-08).
    """
    now = now or office_now()
    s = settings or AttendanceSettings()
    try:
        decision = _decide(employee_id, now, score, s)
        if dry_run or not decision.recorded:
            db.session.rollback()
        else:
            db.session.commit()
        return decision
    except IntegrityError:
        # Proses lain mencatat sesi yang sama pada saat bersamaan (TC-20).
        db.session.rollback()
        return Decision(ALREADY_IN, "Absensi Anda sudah tercatat.", employee_id)
    except SQLAlchemyError:
        db.session.rollback()
        return Decision(ERROR, "Gangguan sistem. Absensi BELUM tercatat, silakan hubungi admin.",
                        employee_id)
