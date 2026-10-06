"""
Layanan jadwal kerja (PRD 8.4, BR-09, BR-10).

Satu jadwal berisi nama, toleransi terlambat, dan jam masuk/pulang per hari.
Hari tanpa jam dianggap libur. Jadwal tidak pernah dihapus permanen, hanya
dinonaktifkan, karena sesi absensi lama tetap merujuk ke jadwal tersebut.
"""
from datetime import datetime

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError

from extensions import db
from models import WEEKDAY_NAMES, Employee, WorkSchedule, WorkScheduleDay
from services import ValidationError
from services import audit_service as audit

MAX_LATE_TOLERANCE_MINUTES = 180
WEEKDAYS = range(7)

FIELD_LABELS = {
    "name": "Nama",
    "late_tolerance_minutes": "Toleransi terlambat (menit)",
    "days": "Hari dan jam kerja",
}


def _parse_time(value):
    value = (value or "").strip()
    for fmt in ("%H:%M", "%H:%M:%S"):
        try:
            return datetime.strptime(value, fmt).time().replace(second=0)
        except ValueError:
            continue
    return None


def _fmt(t):
    return t.strftime("%H:%M")


def summarize_days(schedule):
    """Ringkasan jam kerja, mis. ["Senin–Kamis 07:30–16:00", "Jumat 07:30–11:30"].

    Hari berurutan dengan jam yang sama digabung menjadi satu baris.
    """
    groups = []
    for day in schedule.days:
        hours = (day.start_time, day.end_time)
        last = groups[-1] if groups else None
        if last and last["hours"] == hours and last["end"] == day.weekday - 1:
            last["end"] = day.weekday
        else:
            groups.append({"start": day.weekday, "end": day.weekday, "hours": hours})

    lines = []
    for g in groups:
        if g["start"] == g["end"]:
            days = WEEKDAY_NAMES[g["start"]]
        else:
            days = f"{WEEKDAY_NAMES[g['start']]}–{WEEKDAY_NAMES[g['end']]}"
        lines.append(f"{days} {_fmt(g['hours'][0])}–{_fmt(g['hours'][1])}")
    return lines


def crosses_midnight(schedule):
    """True jika ada hari dengan jam pulang lebih awal dari jam masuk."""
    return any(day.crosses_midnight for day in schedule.days)


def form_values(schedule=None):
    """Nilai awal formulir. Jadwal baru: Senin–Jumat dicentang, jam dikosongkan."""
    values = {"late_tolerance_minutes": "0"}
    if schedule is None:
        for w in range(5):
            values[f"day_{w}_on"] = "1"
        return values

    values["name"] = schedule.name
    values["late_tolerance_minutes"] = str(schedule.late_tolerance_minutes)
    for day in schedule.days:
        values[f"day_{day.weekday}_on"] = "1"
        values[f"day_{day.weekday}_start"] = _fmt(day.start_time)
        values[f"day_{day.weekday}_end"] = _fmt(day.end_time)
    return values


def _snapshot(schedule):
    return {
        "name": schedule.name,
        "late_tolerance_minutes": schedule.late_tolerance_minutes,
        "days": "; ".join(summarize_days(schedule)) or "tidak ada hari kerja",
    }


def _clean(form, current=None):
    errors = {}

    name = (form.get("name") or "").strip()
    if not name:
        errors["name"] = "Nama jadwal wajib diisi."
    elif len(name) > 100:
        errors["name"] = "Nama jadwal maksimal 100 karakter."
    else:
        existing = db.session.execute(
            db.select(WorkSchedule).where(
                func.lower(WorkSchedule.name) == name.lower()
            )
        ).scalar_one_or_none()
        if existing and (current is None or existing.id != current.id):
            errors["name"] = "Nama jadwal sudah dipakai."

    raw_tolerance = (form.get("late_tolerance_minutes") or "0").strip()
    try:
        tolerance = int(raw_tolerance)
    except ValueError:
        tolerance = None
    if tolerance is None or not 0 <= tolerance <= MAX_LATE_TOLERANCE_MINUTES:
        errors["late_tolerance_minutes"] = (
            f"Toleransi harus angka 0 sampai {MAX_LATE_TOLERANCE_MINUTES} menit."
        )

    days = {}
    for w in WEEKDAYS:
        if not form.get(f"day_{w}_on"):
            continue
        start = _parse_time(form.get(f"day_{w}_start"))
        end = _parse_time(form.get(f"day_{w}_end"))
        label = WEEKDAY_NAMES[w]
        if start is None or end is None:
            errors[f"day_{w}"] = f"{label}: isi jam masuk dan jam pulang (format JJ:MM)."
        elif start == end:
            errors[f"day_{w}"] = f"{label}: jam pulang tidak boleh sama dengan jam masuk."
        else:
            days[w] = (start, end)

    if not days and not any(k.startswith("day_") for k in errors):
        errors["days"] = "Pilih minimal satu hari kerja."

    if errors:
        raise ValidationError(errors)

    return {"name": name, "late_tolerance_minutes": tolerance, "days": days}


def _apply_days(schedule, days):
    """Menyamakan baris hari dengan isian formulir.

    Baris yang ada diubah di tempat (tidak dihapus lalu dibuat ulang) agar
    tidak bentrok dengan batasan unik (jadwal, hari).
    """
    for w in WEEKDAYS:
        existing = schedule.day_for(w)
        wanted = days.get(w)
        if wanted and existing:
            existing.start_time, existing.end_time = wanted
        elif wanted:
            schedule.days.append(
                WorkScheduleDay(weekday=w, start_time=wanted[0], end_time=wanted[1])
            )
        elif existing:
            schedule.days.remove(existing)
    schedule.days.sort(key=lambda d: d.weekday)


def _commit_or_conflict():
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValidationError({"name": "Nama jadwal sudah dipakai."})


def list_schedules():
    return db.session.execute(
        db.select(WorkSchedule).order_by(
            WorkSchedule.is_active.desc(), WorkSchedule.name
        )
    ).scalars().all()


def active_schedules():
    return db.session.execute(
        db.select(WorkSchedule)
        .filter_by(is_active=True)
        .order_by(WorkSchedule.name)
    ).scalars().all()


def count_active_employees(schedule):
    return db.session.execute(
        db.select(func.count(Employee.id)).where(
            Employee.work_schedule_id == schedule.id,
            Employee.is_active.is_(True),
        )
    ).scalar_one()


def create_schedule(form, actor_id):
    data = _clean(form)
    schedule = WorkSchedule(
        name=data["name"],
        late_tolerance_minutes=data["late_tolerance_minutes"],
        is_active=True,
    )
    _apply_days(schedule, data["days"])
    db.session.add(schedule)
    db.session.flush()

    snap = _snapshot(schedule)
    audit.log_event(
        audit.SCHEDULE_CREATED,
        user_id=actor_id,
        entity_type="work_schedule",
        entity_id=schedule.id,
        description=(
            f"Jadwal dibuat: {snap['name']} ({snap['days']}), "
            f"toleransi {snap['late_tolerance_minutes']} menit"
        ),
        commit=False,
    )
    _commit_or_conflict()
    return schedule


def update_schedule(schedule, form, actor_id):
    data = _clean(form, current=schedule)
    before = _snapshot(schedule)

    schedule.name = data["name"]
    schedule.late_tolerance_minutes = data["late_tolerance_minutes"]
    _apply_days(schedule, data["days"])

    changes = audit.describe_changes(before, _snapshot(schedule), FIELD_LABELS)
    if changes:
        audit.log_event(
            audit.SCHEDULE_UPDATED,
            user_id=actor_id,
            entity_type="work_schedule",
            entity_id=schedule.id,
            description=f"Jadwal '{schedule.name}' diubah. {changes}",
            commit=False,
        )
    _commit_or_conflict()
    return schedule


def set_schedule_active(schedule, active, actor_id):
    if schedule.is_active == active:
        return schedule

    if not active:
        used_by = count_active_employees(schedule)
        if used_by:
            raise ValidationError({
                "_": (
                    f"Jadwal '{schedule.name}' masih dipakai oleh {used_by} "
                    "karyawan aktif. Pindahkan karyawan tersebut ke jadwal lain "
                    "terlebih dahulu."
                )
            })

    schedule.is_active = active
    audit.log_event(
        audit.SCHEDULE_ACTIVATED if active else audit.SCHEDULE_DEACTIVATED,
        user_id=actor_id,
        entity_type="work_schedule",
        entity_id=schedule.id,
        description=(
            f"Jadwal '{schedule.name}' "
            f"{'diaktifkan kembali' if active else 'dinonaktifkan'}"
        ),
        commit=False,
    )
    db.session.commit()
    return schedule
