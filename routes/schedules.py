"""
Halaman pengelolaan jadwal kerja (PRD 8.4). Semua halaman wajib login admin.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db
from models import WEEKDAY_NAMES, WorkSchedule
from services import ValidationError
from services import schedule_service

bp = Blueprint("schedules", __name__, url_prefix="/admin/schedules")


def _render_form(schedule, values, errors, status=200):
    return render_template(
        "schedule_form.html",
        schedule=schedule,
        values=values,
        errors=errors,
        max_tolerance=schedule_service.MAX_LATE_TOLERANCE_MINUTES,
        weekday_names=WEEKDAY_NAMES,
    ), status


@bp.get("/")
@login_required
def list_schedules():
    schedules = schedule_service.list_schedules()
    usage = {s.id: schedule_service.count_active_employees(s) for s in schedules}
    return render_template(
        "schedules.html",
        schedules=schedules,
        usage=usage,
        crosses_midnight=schedule_service.crosses_midnight,
        summarize_days=schedule_service.summarize_days,
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "POST":
        try:
            schedule = schedule_service.create_schedule(request.form, current_user.id)
        except ValidationError as exc:
            return _render_form(None, request.form, exc.errors, 400)
        flash(f"Jadwal '{schedule.name}' berhasil ditambahkan.", "success")
        return redirect(url_for("schedules.list_schedules"))
    return _render_form(None, schedule_service.form_values(), {})


@bp.route("/<int:schedule_id>/edit", methods=["GET", "POST"])
@login_required
def edit(schedule_id):
    schedule = db.get_or_404(WorkSchedule, schedule_id)
    if request.method == "POST":
        try:
            schedule_service.update_schedule(schedule, request.form, current_user.id)
        except ValidationError as exc:
            db.session.rollback()
            return _render_form(schedule, request.form, exc.errors, 400)
        flash(f"Jadwal '{schedule.name}' berhasil diperbarui.", "success")
        return redirect(url_for("schedules.list_schedules"))
    return _render_form(schedule, schedule_service.form_values(schedule), {})


def _set_active(schedule_id, active):
    schedule = db.get_or_404(WorkSchedule, schedule_id)
    try:
        schedule_service.set_schedule_active(schedule, active, current_user.id)
    except ValidationError as exc:
        db.session.rollback()
        flash(exc.errors.get("_", "Perubahan status gagal."), "error")
    else:
        word = "diaktifkan kembali" if active else "dinonaktifkan"
        flash(f"Jadwal '{schedule.name}' {word}.", "success")
    return redirect(url_for("schedules.list_schedules"))


@bp.post("/<int:schedule_id>/deactivate")
@login_required
def deactivate(schedule_id):
    return _set_active(schedule_id, False)


@bp.post("/<int:schedule_id>/activate")
@login_required
def activate(schedule_id):
    return _set_active(schedule_id, True)
