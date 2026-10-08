"""
API baca-saja (JSON) untuk aplikasi lain.

Baca (token read atau write):
    GET  /api/health, /api/employees[/<id>], /api/work-schedules[/<id>],
         /api/attendance[/<id>], /api/summary/today, /api/reports/summary,
         /api/reports/detail
Tulis (token write saja):
    POST/PATCH /api/employees..., /api/work-schedules..., activate/deactivate
    GET  /api/audit-logs

Autentikasi: header  Authorization: Bearer <token>
Token dibuat admin dengan: flask --app app api-token-create [--scope write]

Prinsip:
- Perubahan data memakai layanan yang sama dengan admin Flask (validasi dan
  audit log sama); asal perubahan dicatat. Tidak ada endpoint hapus.
- Data wajah (templat) dan skor kecocokan TIDAK PERNAH dikirim.
- Waktu dalam ISO 8601 dengan zona waktu kantor (mis. 2026-10-08T07:50:40+08:00).
"""
import math
import threading
import time
from collections import defaultdict, deque
from datetime import datetime
from functools import wraps
from zoneinfo import ZoneInfo

from flask import Blueprint, current_app, g, jsonify, request
from sqlalchemy import or_, text
from sqlalchemy.orm import selectinload

from extensions import db
from models import (WEEKDAY_NAMES, AttendanceSession, AuditLog, Employee, FaceEnrollmentRequest,
                    FaceTemplate, WorkSchedule, office_now)
from services import ValidationError
from services import api_token_service as tokens
from services import attendance_service, employee_service, report_service, schedule_service
from services import face_enrollment_service as face_enroll

bp = Blueprint("api", __name__, url_prefix="/api")

API_VERSION = "1"
MAX_PER_PAGE = 200
DEFAULT_PER_PAGE = 50
MAX_RANGE_DAYS = 31


# --------------------------------------------------------------------- pembantu

def error(status, code, message):
    resp = jsonify({"error": code, "message": message})
    resp.status_code = status
    return resp


class _RateLimiter:
    """Batas permintaan per token per menit (cukup untuk satu proses Waitress)."""

    def __init__(self):
        self._hits = defaultdict(deque)
        self._lock = threading.Lock()

    def allow(self, key, limit, now=None):
        now = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits[key]
            while hits and now - hits[0] > 60:
                hits.popleft()
            if len(hits) >= limit:
                return False, max(1, int(60 - (now - hits[0])) + 1)
            hits.append(now)
            return True, 0


limiter = _RateLimiter()


def _actor():
    """Nama pelaku yang DILAPORKAN aplikasi pemanggil (header X-Actor), mis.
    username admin Laravel. Hanya keterangan di audit log, bukan autentikasi."""
    raw = request.headers.get("X-Actor", "")
    clean = "".join(ch for ch in raw if ch.isprintable() and ch not in "[]").strip()
    return clean[:60]


def require_token(scope="read"):
    """Memeriksa token. scope="write" hanya untuk token write."""
    def decorator(fn):
        @wraps(fn)
        def wrapper(*args, **kwargs):
            header = request.headers.get("Authorization", "")
            if not header.startswith("Bearer "):
                return error(401, "token_diperlukan",
                             "Sertakan header 'Authorization: Bearer <token>'.")
            token = tokens.authenticate(header[7:].strip())
            if token is None:
                return error(401, "token_tidak_valid",
                             "Token tidak dikenal, sudah dicabut, atau kedaluwarsa.")
            if not token.allows(scope):
                return error(403, "cakupan_tidak_cukup",
                             "Token ini hanya boleh membaca. Perubahan data butuh token write.")
            ok, retry = limiter.allow(token.id, current_app.config.get("API_RATE_LIMIT", 120))
            if not ok:
                resp = error(429, "terlalu_banyak_permintaan",
                             "Batas permintaan per menit terlampaui. Coba lagi sebentar.")
                resp.headers["Retry-After"] = str(retry)
                return resp
            g.api_token = token
            if scope == "write":
                actor = _actor()
                g.audit_via = (f"[via API '{token.name}' ({token.prefix}…)"
                               + (f", pelaku: {actor}" if actor else "") + "]")
            return fn(*args, **kwargs)
        return wrapper
    return decorator


def _iso(dt):
    if dt is None:
        return None
    tz = ZoneInfo(current_app.config.get("OFFICE_TIMEZONE", "Asia/Makassar"))
    return dt.replace(tzinfo=tz).isoformat()


def _int_arg(name, default, minimum=1, maximum=None):
    raw = request.args.get(name)
    if raw in (None, ""):
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ValueError(f"Parameter '{name}' harus berupa angka.")
    if value < minimum or (maximum is not None and value > maximum):
        raise ValueError(f"Parameter '{name}' harus antara {minimum} dan {maximum}.")
    return value


def _date_arg(name):
    raw = request.args.get(name)
    if raw in (None, ""):
        return None
    try:
        return datetime.strptime(raw, "%Y-%m-%d").date()
    except ValueError:
        raise ValueError(f"Parameter '{name}' harus berformat YYYY-MM-DD.")


def _paginate(stmt, count_stmt, serialize):
    page = _int_arg("page", 1)
    per_page = _int_arg("per_page", DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)
    total = db.session.execute(count_stmt).scalar_one()
    rows = db.session.execute(stmt.limit(per_page).offset((page - 1) * per_page)).scalars().all()
    return {
        "data": [serialize(r) for r in rows],
        "page": page, "per_page": per_page, "total": total,
        "pages": max(1, math.ceil(total / per_page)),
    }


def _json(payload, status=200):
    resp = jsonify(payload)
    resp.status_code = status
    resp.headers["Cache-Control"] = "no-store"
    return resp


# --------------------------------------------------------------------- data

def employee_json(e, with_face=None):
    return {
        "id": e.id,
        "employee_code": e.employee_code,
        "full_name": e.full_name,
        "department": e.department,
        "is_active": e.is_active,
        "work_schedule": ({"id": e.work_schedule.id, "name": e.work_schedule.name}
                          if e.work_schedule else None),
        "face_registered": with_face,
    }


def attendance_json(s):
    e = s.employee
    return {
        "id": s.id,
        "work_date": s.work_date.isoformat(),
        "employee": {"id": e.id, "employee_code": e.employee_code, "full_name": e.full_name,
                     "department": e.department},
        "status": s.status,
        "check_in_at": _iso(s.check_in_at),
        "check_out_at": _iso(s.check_out_at),
        "late_minutes": s.late_minutes,
        "left_early": s.left_early,
        "scheduled_start": _iso(s.scheduled_start),
        "scheduled_end": _iso(s.scheduled_end),
        "check_in_method": s.check_in_method,
        "check_out_method": s.check_out_method,
    }


# --------------------------------------------------------------------- endpoint

@bp.get("/health")
def health():
    """Tanpa token: hanya menyatakan layanan dan database hidup."""
    try:
        db.session.execute(text("SELECT 1"))
        database = "ok"
    except Exception:
        db.session.rollback()
        database = "error"
    status = "ok" if database == "ok" else "error"
    return _json({"status": status, "database": database, "api_version": API_VERSION,
                  "time": _iso(office_now())}, 200 if status == "ok" else 503)


@bp.get("/employees")
@require_token()
def employees():
    try:
        status = request.args.get("status", "active")
        if status not in ("active", "inactive", "all"):
            raise ValueError("Parameter 'status' harus active, inactive, atau all.")
        filters = []
        if status != "all":
            filters.append(Employee.is_active.is_(status == "active"))
        q = request.args.get("q", "").strip()
        if q:
            like = f"%{q}%"
            filters.append(or_(Employee.employee_code.ilike(like), Employee.full_name.ilike(like)))
        department = request.args.get("department", "").strip()
        if department:
            filters.append(Employee.department == department)

        stmt = (db.select(Employee).options(selectinload(Employee.work_schedule))
                .where(*filters).order_by(Employee.full_name, Employee.id))
        count = db.select(db.func.count(Employee.id)).where(*filters)
        with_face = set(db.session.execute(db.select(FaceTemplate.employee_id).distinct()).scalars())
        return _json(_paginate(stmt, count, lambda e: employee_json(e, e.id in with_face)))
    except ValueError as exc:
        return error(400, "parameter_tidak_valid", str(exc))


@bp.get("/attendance")
@require_token()
def attendance():
    try:
        day = _date_arg("date")
        start, end = _date_arg("start"), _date_arg("end")
        if day and (start or end):
            raise ValueError("Pakai 'date' ATAU 'start'/'end', bukan keduanya.")
        if day:
            start = end = day
        elif start or end:
            start, end = start or end, end or start
        else:
            start = end = office_now().date()
        if start > end:
            raise ValueError("'start' harus sama atau sebelum 'end'.")
        if (end - start).days + 1 > MAX_RANGE_DAYS:
            raise ValueError(f"Rentang tanggal maksimal {MAX_RANGE_DAYS} hari.")

        filters = [AttendanceSession.work_date >= start, AttendanceSession.work_date <= end]
        employee_id = request.args.get("employee_id")
        if employee_id:
            filters.append(AttendanceSession.employee_id == _int_arg("employee_id", None))
        code = request.args.get("employee_code", "").strip().upper()
        if code:
            filters.append(Employee.employee_code == code)
        department = request.args.get("department", "").strip()
        if department:
            filters.append(Employee.department == department)
        q = request.args.get("q", "").strip()
        if q:
            like = f"%{q}%"
            filters.append(or_(Employee.employee_code.ilike(like), Employee.full_name.ilike(like)))
        status = request.args.get("status", "").strip().lower()
        if status:
            if status not in ("hadir", "terlambat", "dibatalkan"):
                raise ValueError("Parameter 'status' harus hadir, terlambat, atau dibatalkan.")
            filters.append(AttendanceSession.status == status)

        base = db.select(AttendanceSession).join(Employee).where(*filters)
        stmt = (base.options(selectinload(AttendanceSession.employee))
                .order_by(AttendanceSession.work_date.desc(), Employee.full_name, AttendanceSession.id))
        count = db.select(db.func.count(AttendanceSession.id)).join(Employee).where(*filters)
        payload = _paginate(stmt, count, attendance_json)
        payload["start"], payload["end"] = start.isoformat(), end.isoformat()
        return _json(payload)
    except ValueError as exc:
        return error(400, "parameter_tidak_valid", str(exc))


@bp.get("/attendance/<int:session_id>")
@require_token()
def attendance_detail(session_id):
    s = db.session.get(AttendanceSession, session_id)
    if s is None:
        return error(404, "tidak_ditemukan", f"Catatan absensi {session_id} tidak ditemukan.")
    return _json(attendance_json(s))


# ===================================================================== tambahan
# Endpoint di bawah ini dipakai panel admin lain (mis. Laravel). Semua perubahan
# data memanggil layanan yang SAMA dengan admin Flask, sehingga validasi, aturan,
# dan audit log tetap berlaku. Tidak ada endpoint hapus.

def _body():
    data = request.get_json(silent=True)
    if not isinstance(data, dict):
        raise ValueError("Body harus JSON objek (Content-Type: application/json).")
    return data


def _validation(exc):
    errors = dict(exc.errors)
    needs_confirmation = bool(errors.pop("confirm_duplicate_name", None))
    general = errors.pop("_", None)
    resp = jsonify({
        "error": "validasi_gagal",
        "message": general or "Periksa kembali data yang dikirim.",
        "fields": errors,
        "needs_confirmation": needs_confirmation,
    })
    resp.status_code = 422
    return resp


def _text(value):
    return "" if value is None else str(value).strip()


def _employee_payload(e):
    face = db.session.execute(
        db.select(FaceTemplate.id).filter_by(employee_id=e.id).limit(1)).first() is not None
    return employee_json(e, face)


def _employee_form(body, current=None):
    keys = ("employee_code", "full_name", "department", "work_schedule_id")
    form = {k: (getattr(current, k) if current is not None else "") for k in keys}
    unknown = set(body) - set(keys) - {"confirm_duplicate_name"}
    if unknown:
        raise ValueError("Field tidak dikenal: " + ", ".join(sorted(unknown)) + ".")
    for k in keys:
        if k in body:
            form[k] = body[k]
    form = {k: _text(v) for k, v in form.items()}
    form["confirm_duplicate_name"] = "1" if body.get("confirm_duplicate_name") else ""
    return form


@bp.get("/employees/<int:employee_id>")
@require_token()
def employee_detail(employee_id):
    e = db.session.get(Employee, employee_id)
    if e is None:
        return error(404, "tidak_ditemukan", f"Karyawan {employee_id} tidak ditemukan.")
    return _json(_employee_payload(e))


@bp.post("/employees")
@require_token("write")
def employee_create():
    try:
        e = employee_service.create_employee(_employee_form(_body()), None)
    except ValueError as exc:
        return error(400, "body_tidak_valid", str(exc))
    except ValidationError as exc:
        db.session.rollback()
        return _validation(exc)
    return _json(_employee_payload(e), 201)


@bp.patch("/employees/<int:employee_id>")
@require_token("write")
def employee_update(employee_id):
    e = db.session.get(Employee, employee_id)
    if e is None:
        return error(404, "tidak_ditemukan", f"Karyawan {employee_id} tidak ditemukan.")
    try:
        e = employee_service.update_employee(e, _employee_form(_body(), e), None)
    except ValueError as exc:
        return error(400, "body_tidak_valid", str(exc))
    except ValidationError as exc:
        db.session.rollback()
        return _validation(exc)
    return _json(_employee_payload(e))


def _set_employee_active(employee_id, active):
    e = db.session.get(Employee, employee_id)
    if e is None:
        return error(404, "tidak_ditemukan", f"Karyawan {employee_id} tidak ditemukan.")
    try:
        e = employee_service.set_employee_active(e, active, None)
    except ValidationError as exc:
        db.session.rollback()
        return _validation(exc)
    return _json(_employee_payload(e))


@bp.post("/employees/<int:employee_id>/activate")
@require_token("write")
def employee_activate(employee_id):
    return _set_employee_active(employee_id, True)


@bp.post("/employees/<int:employee_id>/deactivate")
@require_token("write")
def employee_deactivate(employee_id):
    return _set_employee_active(employee_id, False)


# --------------------------------------------------------------------- jadwal

def schedule_json(s):
    return {
        "id": s.id,
        "name": s.name,
        "late_tolerance_minutes": s.late_tolerance_minutes,
        "is_active": s.is_active,
        "days": [{"weekday": d.weekday, "weekday_name": WEEKDAY_NAMES[d.weekday],
                  "start_time": d.start_time.strftime("%H:%M"),
                  "end_time": d.end_time.strftime("%H:%M"),
                  "crosses_midnight": d.crosses_midnight} for d in s.days],
        "summary": schedule_service.summarize_days(s),
        "active_employees": schedule_service.count_active_employees(s),
    }


def _schedule_form(body, current=None):
    unknown = set(body) - {"name", "late_tolerance_minutes", "days"}
    if unknown:
        raise ValueError("Field tidak dikenal: " + ", ".join(sorted(unknown)) + ".")
    form = schedule_service.form_values(current) if current is not None else {}
    if "name" in body:
        form["name"] = _text(body["name"])
    if "late_tolerance_minutes" in body:
        form["late_tolerance_minutes"] = _text(body["late_tolerance_minutes"])
    if "days" in body or current is None:
        days = body.get("days")
        if not isinstance(days, list):
            raise ValueError("'days' harus berupa daftar, mis. "
                             '[{"weekday": 0, "start_time": "07:30", "end_time": "16:00"}].')
        for w in range(7):
            for suffix in ("on", "start", "end"):
                form.pop(f"day_{w}_{suffix}", None)
        seen = set()
        for item in days:
            if not isinstance(item, dict):
                raise ValueError("Setiap isi 'days' harus objek.")
            weekday = item.get("weekday")
            if not isinstance(weekday, int) or isinstance(weekday, bool) or not 0 <= weekday <= 6:
                raise ValueError("'weekday' harus angka 0 (Senin) sampai 6 (Minggu).")
            if weekday in seen:
                raise ValueError(f"Hari {WEEKDAY_NAMES[weekday]} disebut lebih dari sekali.")
            seen.add(weekday)
            form[f"day_{weekday}_on"] = "1"
            form[f"day_{weekday}_start"] = _text(item.get("start_time"))
            form[f"day_{weekday}_end"] = _text(item.get("end_time"))
    return form


@bp.get("/work-schedules")
@require_token()
def schedules():
    status = request.args.get("status", "all")
    if status not in ("active", "inactive", "all"):
        return error(400, "parameter_tidak_valid", "Parameter 'status' harus active, inactive, atau all.")
    rows = schedule_service.list_schedules()
    if status != "all":
        rows = [r for r in rows if r.is_active == (status == "active")]
    return _json({"data": [schedule_json(r) for r in rows], "total": len(rows)})


@bp.get("/work-schedules/<int:schedule_id>")
@require_token()
def schedule_detail(schedule_id):
    s = db.session.get(WorkSchedule, schedule_id)
    if s is None:
        return error(404, "tidak_ditemukan", f"Jadwal {schedule_id} tidak ditemukan.")
    return _json(schedule_json(s))


@bp.post("/work-schedules")
@require_token("write")
def schedule_create():
    try:
        s = schedule_service.create_schedule(_schedule_form(_body()), None)
    except ValueError as exc:
        return error(400, "body_tidak_valid", str(exc))
    except ValidationError as exc:
        db.session.rollback()
        return _validation(exc)
    return _json(schedule_json(s), 201)


@bp.patch("/work-schedules/<int:schedule_id>")
@require_token("write")
def schedule_update(schedule_id):
    s = db.session.get(WorkSchedule, schedule_id)
    if s is None:
        return error(404, "tidak_ditemukan", f"Jadwal {schedule_id} tidak ditemukan.")
    try:
        s = schedule_service.update_schedule(s, _schedule_form(_body(), s), None)
    except ValueError as exc:
        return error(400, "body_tidak_valid", str(exc))
    except ValidationError as exc:
        db.session.rollback()
        return _validation(exc)
    return _json(schedule_json(s))


def _set_schedule_active(schedule_id, active):
    s = db.session.get(WorkSchedule, schedule_id)
    if s is None:
        return error(404, "tidak_ditemukan", f"Jadwal {schedule_id} tidak ditemukan.")
    try:
        s = schedule_service.set_schedule_active(s, active, None)
    except ValidationError as exc:
        db.session.rollback()
        return _validation(exc)
    return _json(schedule_json(s))


@bp.post("/work-schedules/<int:schedule_id>/activate")
@require_token("write")
def schedule_activate(schedule_id):
    return _set_schedule_active(schedule_id, True)


@bp.post("/work-schedules/<int:schedule_id>/deactivate")
@require_token("write")
def schedule_deactivate(schedule_id):
    return _set_schedule_active(schedule_id, False)


# --------------------------------------------------------------------- ringkasan & laporan

def _brief(e):
    return {"id": e.id, "employee_code": e.employee_code, "full_name": e.full_name,
            "department": e.department}


@bp.get("/summary/today")
@require_token()
def summary_today():
    try:
        day = _date_arg("date") or office_now().date()
    except ValueError as exc:
        return error(400, "parameter_tidak_valid", str(exc))
    roster = attendance_service.daily_roster(day)
    return _json({
        "date": day.isoformat(),
        "active_employees": sum(1 for r in roster if r["employee"].is_active),
        "scheduled": sum(1 for r in roster if r["rule"] is not None),
        "present": sum(1 for r in roster if r["present"]),
        "late": sum(1 for r in roster if r["late"]),
        "checked_out": sum(1 for r in roster if r["present"] and r["session"].check_out_at),
        "not_checked_in": [_brief(r["employee"]) for r in roster
                           if not r["present"] and r["rule"] is not None],
    })


def _report_period():
    start = _date_arg("start") or office_now().date().replace(day=1)
    end = _date_arg("end") or office_now().date()
    try:
        return report_service.validate_period(start, end)
    except ValidationError as exc:
        raise ValueError(" ".join(exc.errors.values()))


@bp.get("/reports/summary")
@require_token()
def report_summary():
    try:
        start, end = _report_period()
    except ValueError as exc:
        return error(400, "parameter_tidak_valid", str(exc))
    details = report_service.detail_rows(start, end, request.args.get("q", ""),
                                         request.args.get("department", ""))
    rows = [{
        "employee": _brief(i["employee"]) | {"is_active": i["employee"].is_active},
        "workdays": i["workdays"], "present": i["present"], "late": i["late"],
        "late_minutes": i["late_minutes"], "left_early": i["left_early"],
        "no_checkout": i["no_checkout"], "not_checked_in": i["absent"],
    } for i in report_service.summary_rows(details)]
    totals = {k: sum(r[k] for r in rows) for k in
              ("workdays", "present", "late", "late_minutes", "left_early", "no_checkout", "not_checked_in")}
    return _json({"start": start.isoformat(), "end": end.isoformat(), "data": rows, "totals": totals})


@bp.get("/reports/detail")
@require_token()
def report_detail():
    try:
        start, end = _report_period()
        page = _int_arg("page", 1)
        per_page = _int_arg("per_page", DEFAULT_PER_PAGE, 1, MAX_PER_PAGE)
    except ValueError as exc:
        return error(400, "parameter_tidak_valid", str(exc))
    details = report_service.detail_rows(start, end, request.args.get("q", ""),
                                         request.args.get("department", ""))
    chunk = details[(page - 1) * per_page: page * per_page]
    data = [{
        "date": r["date"].isoformat(), "weekday": r["weekday"], "employee": _brief(r["employee"]),
        "status": r["status"], "scheduled_start": r["scheduled_start"] or None,
        "scheduled_end": r["scheduled_end"] or None,
        "check_in_at": _iso(r["session"].check_in_at) if r["session"] else None,
        "check_out_at": _iso(r["session"].check_out_at) if r["session"] else None,
        "late_minutes": r["late_minutes"], "left_early": r["left_early"],
        "check_in_method": r["session"].check_in_method if r["session"] else None,
        "check_out_method": r["session"].check_out_method if r["session"] else None,
    } for r in chunk]
    return _json({"start": start.isoformat(), "end": end.isoformat(), "data": data, "page": page,
                  "per_page": per_page, "total": len(details),
                  "pages": max(1, math.ceil(len(details) / per_page))})


@bp.get("/audit-logs")
@require_token("write")
def audit_logs():
    """Audit log hanya untuk token write (setara admin)."""
    filters = []
    action = request.args.get("action", "").strip()
    if action:
        filters.append(AuditLog.action == action)
    q = request.args.get("q", "").strip()
    if q:
        filters.append(AuditLog.description.ilike(f"%{q}%"))
    try:
        stmt = (db.select(AuditLog).options(selectinload(AuditLog.user)).where(*filters)
                .order_by(AuditLog.id.desc()))
        count = db.select(db.func.count(AuditLog.id)).where(*filters)
        return _json(_paginate(stmt, count, lambda a: {
            "id": a.id, "created_at": _iso(a.created_at), "action": a.action,
            "user": ({"id": a.user.id, "name": a.user.name, "username": a.user.username}
                     if a.user else None),
            "entity_type": a.entity_type, "entity_id": a.entity_id, "description": a.description,
        }))
    except ValueError as exc:
        return error(400, "parameter_tidak_valid", str(exc))


# --------------------------------------------------------------------- pendaftaran wajah

def _kiosk_online():
    from kiosk.state import KioskState, public_view

    path = current_app.config.get("KIOSK_STATE_FILE")
    return bool(path) and public_view(KioskState(path).read())["online"]


def enrollment_json(r):
    return {
        "id": r.id,
        "status": r.status,
        "employee": _brief(r.employee),
        "is_replacement": bool(r.replace_reason),
        "samples_taken": r.samples_taken,
        "samples_total": r.samples_total,
        "message": r.message,
        "created_at": _iso(r.created_at),
        "expires_at": _iso(r.expires_at),
        "started_at": _iso(r.started_at),
        "finished_at": _iso(r.finished_at),
        "finished": r.status in face_enroll.FINAL,
    }


@bp.post("/employees/<int:employee_id>/face-enrollment")
@require_token("write")
def face_enrollment_start(employee_id):
    """Meminta layanan kamera kios mendaftarkan wajah karyawan ini."""
    e = db.session.get(Employee, employee_id)
    if e is None:
        return error(404, "tidak_ditemukan", f"Karyawan {employee_id} tidak ditemukan.")
    body = request.get_json(silent=True) or {}
    if not isinstance(body, dict) or set(body) - {"replace_reason"}:
        return error(400, "body_tidak_valid", "Body hanya boleh berisi 'replace_reason'.")
    try:
        r = face_enroll.create(e, _text(body.get("replace_reason")) or None, via=g.get("audit_via"))
    except ValidationError as exc:
        db.session.rollback()
        return _validation(exc)
    payload = enrollment_json(r)
    payload["kiosk_online"] = _kiosk_online()
    return _json(payload, 202)


@bp.get("/face-enrollments/<int:request_id>")
@require_token("write")
def face_enrollment_status(request_id):
    r = face_enroll.get(request_id)
    if r is None:
        return error(404, "tidak_ditemukan", f"Permintaan pendaftaran {request_id} tidak ditemukan.")
    payload = enrollment_json(r)
    payload["kiosk_online"] = _kiosk_online()
    return _json(payload)


@bp.post("/face-enrollments/<int:request_id>/cancel")
@require_token("write")
def face_enrollment_cancel(request_id):
    r = db.session.get(FaceEnrollmentRequest, request_id)
    if r is None:
        return error(404, "tidak_ditemukan", f"Permintaan pendaftaran {request_id} tidak ditemukan.")
    try:
        r = face_enroll.cancel(r)
    except ValidationError as exc:
        db.session.rollback()
        return _validation(exc)
    return _json(enrollment_json(r))


def register_api_error_handlers(app):
    """Galat di bawah /api/ dijawab JSON, bukan halaman HTML."""
    def _wants_json():
        return request.path.startswith("/api/")

    @app.errorhandler(404)
    def _not_found(exc):
        if _wants_json():
            return error(404, "tidak_ditemukan", "Endpoint tidak ada. Lihat docs/API.md.")
        return exc

    @app.errorhandler(405)
    def _not_allowed(exc):
        if _wants_json():
            return error(405, "metode_tidak_diizinkan", "API ini hanya-baca: gunakan GET.")
        return exc

    @app.errorhandler(500)
    def _server_error(exc):
        if _wants_json():
            return error(500, "galat_server", "Terjadi galat di server. Hubungi admin.")
        return exc
