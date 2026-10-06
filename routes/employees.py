"""
Halaman pengelolaan karyawan (FR-02). Semua halaman wajib login admin.
"""
from flask import Blueprint, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from extensions import db
from models import Employee
from routes.auth import _safe_next_url
from services import ValidationError
from services import biometric_service, employee_service, schedule_service

bp = Blueprint("employees", __name__, url_prefix="/admin/employees")

STATUS_OPTIONS = {"active": "Aktif", "inactive": "Nonaktif", "all": "Semua"}


def _form_values(employee=None):
    if employee is None:
        return {}
    return {
        "employee_code": employee.employee_code,
        "full_name": employee.full_name,
        "department": employee.department,
        "work_schedule_id": str(employee.work_schedule_id or ""),
    }


def _render_form(employee, values, errors, status=200, revoke_errors=None):
    return render_template(
        "employee_form.html",
        employee=employee,
        values=values,
        errors=errors,
        templates=biometric_service.templates_for(employee) if employee else [],
        revoke_errors=revoke_errors or {},
        revoke_reason=request.form.get("reason", "") if revoke_errors else "",
        schedules=schedule_service.active_schedules(),
        summarize_days=schedule_service.summarize_days,
        departments=employee_service.list_departments(),
    ), status


@bp.get("/")
@login_required
def list_employees():
    q = request.args.get("q", "").strip()
    department = request.args.get("department", "").strip()
    status = request.args.get("status", "active")
    if status not in STATUS_OPTIONS:
        status = "active"
    page = request.args.get("page", 1, type=int)

    pagination = employee_service.list_employees(
        q=q, department=department, status=status, page=page
    )
    face_counts = biometric_service.template_counts([e.id for e in pagination.items])
    return render_template(
        "employees.html",
        pagination=pagination,
        face_counts=face_counts,
        q=q,
        department=department,
        status=status,
        status_options=STATUS_OPTIONS,
        departments=employee_service.list_departments(),
    )


@bp.route("/new", methods=["GET", "POST"])
@login_required
def create():
    if request.method == "POST":
        try:
            employee = employee_service.create_employee(request.form, current_user.id)
        except ValidationError as exc:
            return _render_form(None, request.form, exc.errors, 400)
        flash(
            f"Karyawan {employee.employee_code} - {employee.full_name} berhasil ditambahkan.",
            "success",
        )
        return redirect(url_for("employees.list_employees"))
    return _render_form(None, {}, {})


@bp.route("/<int:employee_id>/edit", methods=["GET", "POST"])
@login_required
def edit(employee_id):
    employee = db.get_or_404(Employee, employee_id)
    if request.method == "POST":
        try:
            employee_service.update_employee(employee, request.form, current_user.id)
        except ValidationError as exc:
            db.session.rollback()
            return _render_form(employee, request.form, exc.errors, 400)
        flash(f"Data karyawan {employee.employee_code} berhasil diperbarui.", "success")
        return redirect(url_for("employees.list_employees"))
    return _render_form(employee, _form_values(employee), {})


def _set_active(employee_id, active):
    employee = db.get_or_404(Employee, employee_id)
    try:
        employee_service.set_employee_active(employee, active, current_user.id)
    except ValidationError as exc:
        db.session.rollback()
        flash(exc.errors.get("_", "Perubahan status gagal."), "error")
    else:
        word = "diaktifkan kembali" if active else "dinonaktifkan"
        flash(f"Karyawan {employee.employee_code} {word}.", "success")
    return_to = _safe_next_url(request.form.get("return_to"))
    return redirect(return_to or url_for("employees.list_employees"))


@bp.post("/<int:employee_id>/deactivate")
@login_required
def deactivate(employee_id):
    return _set_active(employee_id, False)


@bp.post("/<int:employee_id>/activate")
@login_required
def activate(employee_id):
    return _set_active(employee_id, True)


@bp.post("/<int:employee_id>/face-templates/revoke")
@login_required
def revoke_face_templates(employee_id):
    """Mencabut (menghapus permanen) semua templat wajah karyawan."""
    employee = db.get_or_404(Employee, employee_id)
    try:
        count = biometric_service.revoke_templates(
            employee, current_user.id, request.form.get("reason")
        )
    except ValidationError as exc:
        db.session.rollback()
        return _render_form(
            employee, _form_values(employee), {}, 400, revoke_errors=exc.errors
        )
    flash(
        f"{count} templat wajah {employee.employee_code} telah dicabut. "
        "Karyawan perlu didaftarkan ulang agar dikenali kamera.",
        "success",
    )
    return redirect(url_for("employees.edit", employee_id=employee.id))
