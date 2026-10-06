"""
Daftar absensi harian dan koreksi oleh admin (Tahap 4C, PRD 3.1 dan BR-08).
Semua halaman wajib login admin.
"""
from datetime import datetime

from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db
from models import WEEKDAY_NAMES, AttendanceSession, Employee, office_now
from services import ValidationError
from services import attendance_service as att
from services import employee_service

bp = Blueprint("attendance", __name__, url_prefix="/admin/attendance")

PER_PAGE = 50


def _parse_date(text, default=None):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return default


def _day_label(d):
    return f"{WEEKDAY_NAMES[d.weekday()]}, {d:%d-%m-%Y}"


@bp.get("/")
@login_required
def list_attendance():
    today = office_now().date()
    work_date = _parse_date(request.args.get("date"), today)
    q = request.args.get("q", "").strip()
    department = request.args.get("department", "").strip()
    status = request.args.get("status", "all")
    if status not in att.ROSTER_STATUSES:
        status = "all"
    page = max(1, request.args.get("page", 1, type=int))

    rows = att.daily_roster(work_date, q=q, department=department, status=status)
    total = len(rows)
    pages = max(1, (total + PER_PAGE - 1) // PER_PAGE)
    page = min(page, pages)
    shown = rows[(page - 1) * PER_PAGE: page * PER_PAGE]

    all_rows = att.daily_roster(work_date) if (q or department or status != "all") else rows
    counts = {
        "present": sum(1 for r in all_rows if r["present"]),
        "late": sum(1 for r in all_rows if r["late"]),
        "absent": sum(1 for r in all_rows if not r["present"] and r["rule"]),
    }
    return render_template(
        "attendance.html",
        rows=shown, total=total, page=page, pages=pages,
        work_date=work_date, day_label=_day_label(work_date), today=today,
        q=q, department=department, status=status,
        statuses=att.ROSTER_STATUSES, departments=employee_service.list_departments(),
        counts=counts, is_future=work_date > today,
    )


def _load(employee_id, work_date):
    employee = db.get_or_404(Employee, employee_id)
    session = db.session.execute(
        db.select(AttendanceSession).filter_by(employee_id=employee.id, work_date=work_date)
    ).scalar_one_or_none()
    return employee, session


def _form_values(session):
    if session is None or session.check_in_at is None:
        return {"check_in": "", "check_out": "", "reason": ""}
    return {
        "check_in": session.check_in_at.strftime("%H:%M"),
        "check_out": session.check_out_at.strftime("%H:%M") if session.check_out_at else "",
        "reason": "",
    }


def _render_form(employee, session, work_date, values, errors, status=200, cancel_errors=None):
    schedule = employee.work_schedule
    rule = schedule.day_for(work_date.weekday()) if schedule else None
    return render_template(
        "attendance_form.html",
        employee=employee, session=session, work_date=work_date,
        day_label=_day_label(work_date), rule=rule, schedule=schedule,
        values=values, errors=errors, cancel_errors=cancel_errors or {},
        label=att.status_label(session, rule),
    ), status


@bp.route("/correct", methods=["GET", "POST"])
@login_required
def correct():
    work_date = _parse_date(request.values.get("date"))
    employee_id = request.values.get("employee_id", type=int)
    if work_date is None or employee_id is None:
        flash("Pilih karyawan dan tanggal dari daftar absensi.", "error")
        return redirect(url_for("attendance.list_attendance"))
    if work_date > office_now().date():
        flash("Absensi untuk tanggal yang akan datang tidak dapat dikoreksi.", "error")
        return redirect(url_for("attendance.list_attendance"))

    employee, session = _load(employee_id, work_date)
    if request.method == "POST":
        try:
            _, changed = att.correct(
                employee.id, work_date, request.form.get("check_in"),
                request.form.get("check_out"), request.form.get("reason"), current_user.id,
            )
        except ValidationError as exc:
            db.session.rollback()
            employee, session = _load(employee_id, work_date)
            return _render_form(employee, session, work_date, request.form, exc.errors, 400)
        if changed:
            flash(f"Absensi {employee.employee_code} tanggal {work_date:%d-%m-%Y} dikoreksi.", "success")
        else:
            flash("Tidak ada perubahan yang disimpan.", "info")
        return redirect(url_for("attendance.list_attendance", date=work_date.isoformat()))

    return _render_form(employee, session, work_date, _form_values(session), {})


@bp.post("/cancel")
@login_required
def cancel():
    session_id = request.form.get("session_id", type=int)
    session = db.get_or_404(AttendanceSession, session_id)
    employee, work_date = session.employee, session.work_date
    try:
        att.cancel(session.id, request.form.get("reason"), current_user.id)
    except ValidationError as exc:
        db.session.rollback()
        employee, session = _load(employee.id, work_date)
        return _render_form(employee, session, work_date, _form_values(session), {}, 400,
                            cancel_errors=exc.errors)
    flash(f"Absensi {employee.employee_code} tanggal {work_date:%d-%m-%Y} dibatalkan.", "success")
    return redirect(url_for("attendance.list_attendance", date=work_date.isoformat()))
