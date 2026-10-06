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
from services import ValidationError
from services import audit_service as audit

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


# =====================================================================
# Daftar harian dan koreksi oleh admin (Tahap 4C, BR-08)
# =====================================================================

METHOD_ADMIN = "admin"
MIN_REASON = 10
MAX_REASON = 300
MAX_SHIFT_HOURS = 20

ROSTER_STATUSES = {
    "all": "Semua",
    "present": "Sudah masuk",
    "late": "Terlambat",
    "absent": "Belum masuk",
    "cancelled": "Dibatalkan",
}


def status_label(session, rule):
    """Label status yang mudah dibaca admin."""
    if session is None or session.check_in_at is None:
        if session is not None and session.status == AttendanceSession.STATUS_CANCELLED:
            return "Dibatalkan"
        return "Libur" if rule is None else "Belum masuk"
    if session.status == AttendanceSession.STATUS_LATE:
        return f"Terlambat {session.late_minutes or 0} mnt"
    return "Hadir"


def daily_roster(work_date, q="", department="", status="all"):
    """Semua karyawan aktif (dan karyawan nonaktif yang punya catatan) untuk satu
    tanggal kerja, beserta sesinya. Dipakai halaman daftar absensi admin."""
    from sqlalchemy import or_
    from sqlalchemy.orm import selectinload

    from models import WorkSchedule

    sessions = {
        s.employee_id: s
        for s in db.session.execute(
            db.select(AttendanceSession).filter_by(work_date=work_date)
        ).scalars()
    }
    stmt = (
        db.select(Employee)
        .options(selectinload(Employee.work_schedule).selectinload(WorkSchedule.days))
        .where(or_(Employee.is_active.is_(True), Employee.id.in_(list(sessions) or [0])))
        .order_by(Employee.full_name, Employee.id)
    )
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Employee.employee_code.ilike(like), Employee.full_name.ilike(like)))
    if department:
        stmt = stmt.where(Employee.department == department)

    rows = []
    for employee in db.session.execute(stmt).scalars():
        session = sessions.get(employee.id)
        schedule = employee.work_schedule
        rule = schedule.day_for(work_date.weekday()) if schedule else None
        present = session is not None and session.check_in_at is not None
        cancelled = session is not None and session.status == AttendanceSession.STATUS_CANCELLED
        late = present and session.status == AttendanceSession.STATUS_LATE

        if status == "present" and not present:
            continue
        if status == "late" and not late:
            continue
        # Absensi yang dibatalkan berarti karyawan belum tercatat masuk.
        if status == "absent" and (present or rule is None):
            continue
        if status == "cancelled" and not cancelled:
            continue

        rows.append({
            "employee": employee,
            "session": session,
            "rule": rule,
            "label": status_label(session, rule),
            "present": present,
            "late": late,
            "cancelled": cancelled,
        })
    return rows


def _parse_hm(text):
    text = (text or "").strip()
    if not text:
        return None
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).time().replace(second=0)
        except ValueError:
            continue
    raise ValueError(text)


def _clean_reason(reason, errors):
    reason = " ".join((reason or "").split())
    if len(reason) < MIN_REASON:
        errors["reason"] = f"Alasan koreksi wajib diisi (minimal {MIN_REASON} karakter)."
    elif len(reason) > MAX_REASON:
        errors["reason"] = f"Alasan maksimal {MAX_REASON} karakter."
    return reason


def _snapshot(session):
    def fmt(dt):
        return dt.strftime("%d-%m %H:%M") if dt else "-"

    if session is None:
        return {"masuk": "-", "pulang": "-", "status": "-"}
    return {
        "masuk": fmt(session.check_in_at),
        "pulang": fmt(session.check_out_at),
        "status": session.status,
    }


def correct(employee_id, work_date, check_in_text, check_out_text, reason, actor_id, now=None):
    """Koreksi absensi oleh admin. Status dan keterlambatan dihitung ulang dari
    jadwal; admin tidak menentukan status secara manual (BR-09).

    Mengembalikan (session, berubah). Melempar ValidationError jika tidak valid.
    """
    now = now or office_now()
    errors = {}
    reason = _clean_reason(reason, errors)

    employee = db.session.get(Employee, employee_id)
    if employee is None:
        raise ValidationError({"_": "Karyawan tidak ditemukan."})

    try:
        t_in = _parse_hm(check_in_text)
    except ValueError:
        t_in, errors["check_in"] = None, "Jam masuk tidak valid (JJ:MM)."
    try:
        t_out = _parse_hm(check_out_text)
    except ValueError:
        t_out, errors["check_out"] = None, "Jam pulang tidak valid (JJ:MM)."
    if t_in is None and "check_in" not in errors:
        errors["check_in"] = "Jam masuk wajib diisi."
    if errors:
        raise ValidationError(errors)

    schedule = employee.work_schedule
    rule = schedule.day_for(work_date.weekday()) if schedule else None
    start = end = None
    if rule is not None:
        start, end = shift_window(work_date, rule)

    check_in_at = datetime.combine(work_date, t_in)
    # Shift lintas tengah malam: jam masuk setelah tengah malam milik hari berikutnya.
    if end is not None and end.date() > work_date and t_in < rule.start_time:
        nxt = datetime.combine(work_date + timedelta(days=1), t_in)
        if nxt <= end:
            check_in_at = nxt

    check_out_at = None
    if t_out is not None:
        check_out_at = datetime.combine(check_in_at.date(), t_out)
        if check_out_at <= check_in_at:
            check_out_at += timedelta(days=1)
        if check_out_at - check_in_at > timedelta(hours=MAX_SHIFT_HOURS):
            errors["check_out"] = (
                f"Jam pulang terlalu jauh dari jam masuk (lebih dari {MAX_SHIFT_HOURS} jam)."
            )

    if check_in_at > now:
        errors["check_in"] = "Jam masuk tidak boleh di masa depan."
    if check_out_at is not None and check_out_at > now:
        errors["check_out"] = "Jam pulang tidak boleh di masa depan."
    if errors:
        raise ValidationError(errors)

    session = db.session.execute(
        db.select(AttendanceSession)
        .filter_by(employee_id=employee.id, work_date=work_date)
        .with_for_update()
    ).scalar_one_or_none()
    before = _snapshot(session)
    if session is None:
        session = AttendanceSession(employee_id=employee.id, work_date=work_date)
        db.session.add(session)

    if session.check_in_at != check_in_at:
        session.check_in_at = check_in_at
        session.check_in_method = METHOD_ADMIN
        session.check_in_score = None
    if session.check_out_at != check_out_at:
        session.check_out_at = check_out_at
        session.check_out_method = METHOD_ADMIN if check_out_at else None
        session.check_out_score = None

    late = 0
    if start is not None:
        deadline = start + timedelta(minutes=schedule.late_tolerance_minutes)
        if check_in_at > deadline:
            late = math.ceil((check_in_at - deadline).total_seconds() / 60)
    session.work_schedule_id = schedule.id if schedule else None
    session.scheduled_start = start
    session.scheduled_end = end
    session.late_minutes = late
    session.status = AttendanceSession.STATUS_LATE if late else AttendanceSession.STATUS_ON_TIME

    after = _snapshot(session)
    changes = audit.describe_changes(before, after)
    if not changes:
        db.session.rollback()
        return session, False

    db.session.flush()
    audit.log_event(
        audit.ATTENDANCE_CORRECTED,
        user_id=actor_id,
        entity_type="attendance_session",
        entity_id=session.id,
        description=(
            f"Koreksi absensi {employee.employee_code} tanggal {work_date:%d-%m-%Y}. "
            f"{changes}. Alasan: {reason}"
        ),
        commit=False,
    )
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValidationError({"_": "Catatan absensi sedang diubah proses lain. Muat ulang halaman."})
    return session, True


def cancel(session_id, reason, actor_id):
    """Membatalkan absensi (mis. kamera salah mengenali orang).

    Jam masuk/pulang dikosongkan agar karyawan yang benar tetap bisa absen;
    nilai lamanya tersimpan di audit log.
    """
    errors = {}
    reason = _clean_reason(reason, errors)
    if errors:
        raise ValidationError(errors)

    session = db.session.execute(
        db.select(AttendanceSession).filter_by(id=session_id).with_for_update()
    ).scalar_one_or_none()
    if session is None or session.check_in_at is None:
        raise ValidationError({"_": "Tidak ada absensi aktif yang dapat dibatalkan."})

    before = _snapshot(session)
    session.check_in_at = None
    session.check_out_at = None
    session.check_in_method = session.check_out_method = None
    session.check_in_score = session.check_out_score = None
    session.late_minutes = None
    session.status = AttendanceSession.STATUS_CANCELLED
    audit.log_event(
        audit.ATTENDANCE_CANCELLED,
        user_id=actor_id,
        entity_type="attendance_session",
        entity_id=session.id,
        description=(
            f"Absensi {session.employee.employee_code} tanggal {session.work_date:%d-%m-%Y} "
            f"dibatalkan (sebelumnya masuk {before['masuk']}, pulang {before['pulang']}). "
            f"Alasan: {reason}"
        ),
        commit=False,
    )
    db.session.commit()
    return session
