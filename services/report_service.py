"""
Laporan kehadiran per periode dan ekspor CSV (Tahap 5B, FR-13).

Dua bentuk laporan dari data yang sama:
- detail harian  : satu baris per karyawan per hari kerja terjadwal (atau hari
                   yang punya catatan absensi);
- rekap karyawan : satu baris per karyawan untuk seluruh periode.

Aturan penghitungan:
- "Tidak tercatat masuk" hanya dihitung untuk hari yang SUDAH LEWAT; hari ini
  ditampilkan "Belum masuk" dan tidak dihitung sebagai tidak hadir.
- Absensi yang dibatalkan admin dihitung tidak tercatat masuk.
- Hari tanpa jam kerja (libur) tanpa catatan tidak dimasukkan.
- Status dan menit terlambat diambil dari catatan (salinan jadwal saat
  check-in), bukan dihitung ulang dari jadwal yang berlaku sekarang.
"""
import csv
import io
from collections import OrderedDict
from datetime import timedelta

from sqlalchemy import or_
from sqlalchemy.orm import selectinload

from extensions import db
from models import WEEKDAY_NAMES, AttendanceSession, Employee, WorkSchedule, office_now
from services import ValidationError

MAX_DAYS = 366

ST_PRESENT = "Hadir"
ST_LATE = "Terlambat"
ST_ABSENT = "Tidak tercatat masuk"
ST_TODAY_PENDING = "Belum masuk"
ST_HOLIDAY_WORK = "Hadir (hari libur)"

DETAIL_HEADER = [
    "Tanggal", "Hari", "Kode", "Nama", "Departemen", "Jadwal masuk", "Jadwal pulang",
    "Jam masuk", "Jam pulang", "Status", "Menit terlambat", "Pulang cepat",
    "Sumber masuk", "Sumber pulang",
]
SUMMARY_HEADER = [
    "Kode", "Nama", "Departemen", "Hari kerja", "Hadir", "Terlambat",
    "Total menit terlambat", "Pulang cepat", "Tanpa jam pulang", "Tidak tercatat masuk",
]


def validate_period(start, end, today=None):
    today = today or office_now().date()
    errors = {}
    if start is None:
        errors["start"] = "Tanggal awal tidak valid."
    if end is None:
        errors["end"] = "Tanggal akhir tidak valid."
    if not errors:
        if start > end:
            errors["end"] = "Tanggal akhir harus sama atau setelah tanggal awal."
        elif start > today:
            errors["start"] = "Tanggal awal tidak boleh di masa depan."
        elif (min(end, today) - start).days + 1 > MAX_DAYS:
            errors["end"] = f"Periode maksimal {MAX_DAYS} hari."
    if errors:
        raise ValidationError(errors)
    return start, min(end, today)


def _employees(start, end, q, department, session_employee_ids):
    stmt = (
        db.select(Employee)
        .options(selectinload(Employee.work_schedule).selectinload(WorkSchedule.days))
        .where(or_(Employee.is_active.is_(True), Employee.id.in_(session_employee_ids or [0])))
        .order_by(Employee.full_name, Employee.id)
    )
    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(or_(Employee.employee_code.ilike(like), Employee.full_name.ilike(like)))
    if department:
        stmt = stmt.where(Employee.department == department)
    return db.session.execute(stmt).scalars().all()


def detail_rows(start, end, q="", department="", today=None):
    """Baris detail harian (dict) untuk periode [start, end]."""
    today = today or office_now().date()
    sessions = {}
    for s in db.session.execute(
        db.select(AttendanceSession).where(
            AttendanceSession.work_date >= start, AttendanceSession.work_date <= end)
    ).scalars():
        sessions[(s.employee_id, s.work_date)] = s
    employees = _employees(start, end, q, department, list({k[0] for k in sessions}))

    rows = []
    day = start
    while day <= end:
        for emp in employees:
            s = sessions.get((emp.id, day))
            rule = emp.work_schedule.day_for(day.weekday()) if emp.work_schedule else None
            present = s is not None and s.check_in_at is not None
            # Karyawan nonaktif hanya muncul pada hari yang punya catatan.
            if not emp.is_active and s is None:
                continue
            if rule is None and not present:
                continue  # hari libur tanpa catatan

            if present:
                if rule is None:
                    status = ST_HOLIDAY_WORK
                elif s.status == AttendanceSession.STATUS_LATE:
                    status = ST_LATE
                else:
                    status = ST_PRESENT
            else:
                status = ST_TODAY_PENDING if day >= today else ST_ABSENT

            sched_start = s.scheduled_start if present and s.scheduled_start else None
            sched_end = s.scheduled_end if present and s.scheduled_end else None
            rows.append({
                "date": day,
                "weekday": WEEKDAY_NAMES[day.weekday()],
                "employee": emp,
                "rule": rule,
                "session": s if present else None,
                "status": status,
                "late_minutes": (s.late_minutes or 0) if present else 0,
                "left_early": bool(present and s.left_early),
                "no_checkout": bool(present and s.check_out_at is None and day < today),
                "scheduled_start": sched_start.strftime("%H:%M") if sched_start
                else (rule.start_time.strftime("%H:%M") if rule else ""),
                "scheduled_end": sched_end.strftime("%H:%M") if sched_end
                else (rule.end_time.strftime("%H:%M") if rule else ""),
            })
        day += timedelta(days=1)
    return rows


def summary_rows(details):
    """Rekap per karyawan dari baris detail."""
    summary = OrderedDict()
    for r in sorted(details, key=lambda r: (r["employee"].full_name, r["employee"].id)):
        emp = r["employee"]
        item = summary.setdefault(emp.id, {
            "employee": emp, "workdays": 0, "present": 0, "late": 0, "late_minutes": 0,
            "left_early": 0, "no_checkout": 0, "absent": 0,
        })
        if r["rule"] is not None and r["status"] != ST_TODAY_PENDING:
            item["workdays"] += 1
        if r["session"] is not None:
            item["present"] += 1
        if r["status"] == ST_LATE:
            item["late"] += 1
            item["late_minutes"] += r["late_minutes"]
        item["left_early"] += int(r["left_early"])
        item["no_checkout"] += int(r["no_checkout"])
        item["absent"] += int(r["status"] == ST_ABSENT)
    return list(summary.values())


# --------------------------------------------------------------------- CSV

_FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def _safe(value):
    """Mencegah CSV injection: sel yang diawali karakter rumus diberi tanda kutip."""
    text = "" if value is None else str(value)
    return "'" + text if text.startswith(_FORMULA_START) else text


class _ExcelText(str):
    """Penanda nilai yang sudah aman dan harus dibaca Excel sebagai TEKS."""


def _code(value):
    """Kode karyawan sebagai teks Excel: ="2310010164".

    Tanpa ini Excel mengubah kode angka panjang menjadi notasi ilmiah
    (2,31E+09) dan memotong NIP 18 digit menjadi 15 digit secara permanen.
    Aman karena kode karyawan hanya boleh berisi A-Z, 0-9, titik, garis bawah,
    dan tanda hubung (employee_service.CODE_PATTERN), jadi tidak bisa memuat
    tanda kutip atau rumus lain.
    """
    return _ExcelText('="' + str(value).replace('"', '""') + '"')


def _hm(dt):
    return dt.strftime("%H:%M") if dt else ""


def _to_csv(header, rows):
    # Pemisah ";" dan BOM UTF-8 agar langsung rapi di Excel berpengaturan Indonesia.
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=";", lineterminator="\r\n")
    writer.writerow(header)
    for row in rows:
        writer.writerow([v if isinstance(v, _ExcelText) else _safe(v) for v in row])
    return "\ufeff" + buf.getvalue()


def detail_csv(details):
    rows = []
    for r in details:
        s, e = r["session"], r["employee"]
        rows.append([
            r["date"].strftime("%d-%m-%Y"), r["weekday"], _code(e.employee_code), e.full_name,
            e.department, r["scheduled_start"], r["scheduled_end"],
            _hm(s.check_in_at) if s else "",
            (_hm(s.check_out_at) + (" (+1)" if s and s.check_out_at
                                    and s.check_out_at.date() != s.work_date else "")) if s else "",
            r["status"], r["late_minutes"] or "", "Ya" if r["left_early"] else "",
            (s.check_in_method or "") if s else "", (s.check_out_method or "") if s else "",
        ])
    return _to_csv(DETAIL_HEADER, rows)


def summary_csv(summary):
    rows = [[
        _code(i["employee"].employee_code), i["employee"].full_name, i["employee"].department,
        i["workdays"], i["present"], i["late"], i["late_minutes"], i["left_early"],
        i["no_checkout"], i["absent"],
    ] for i in summary]
    return _to_csv(SUMMARY_HEADER, rows)
