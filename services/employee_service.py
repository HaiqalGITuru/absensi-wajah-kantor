"""
Layanan data karyawan (FR-02).

Karyawan tidak pernah dihapus permanen, hanya dinonaktifkan, agar riwayat
absensinya tetap utuh. Karyawan nonaktif tidak boleh melakukan absensi baru
(BR-02); aturan itu ditegakkan di layanan absensi pada Tahap 4.
"""
import re

from sqlalchemy import func, or_
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import selectinload

from extensions import db
from models import Employee, WorkSchedule
from services import ValidationError
from services import audit_service as audit

# Huruf besar, angka, titik, garis bawah, minus. Cukup untuk NIP 18 digit.
CODE_PATTERN = re.compile(r"^[A-Z0-9._-]{1,30}$")
PER_PAGE = 25

FIELD_LABELS = {
    "employee_code": "Kode",
    "full_name": "Nama",
    "department": "Departemen",
    "work_schedule": "Jadwal",
}


def _snapshot(employee):
    return {
        "employee_code": employee.employee_code,
        "full_name": employee.full_name,
        "department": employee.department,
        "work_schedule": employee.work_schedule.name if employee.work_schedule else None,
    }


def _clean(form, current=None):
    errors = {}

    # Kode disimpan dalam huruf besar agar "k001" dan "K001" dianggap sama,
    # baik di MySQL maupun di database pengujian.
    code = (form.get("employee_code") or "").strip().upper()
    if not code:
        errors["employee_code"] = "Kode karyawan wajib diisi."
    elif not CODE_PATTERN.match(code):
        errors["employee_code"] = (
            "Kode maksimal 30 karakter: huruf, angka, titik, garis bawah, atau minus."
        )
    else:
        existing = db.session.execute(
            db.select(Employee).filter_by(employee_code=code)
        ).scalar_one_or_none()
        if existing and (current is None or existing.id != current.id):
            errors["employee_code"] = f"Kode '{code}' sudah dipakai karyawan lain."

    full_name = " ".join((form.get("full_name") or "").split())
    if not full_name:
        errors["full_name"] = "Nama lengkap wajib diisi."
    elif len(full_name) > 150:
        errors["full_name"] = "Nama lengkap maksimal 150 karakter."
    else:
        same_name = find_same_name(full_name, exclude_id=current.id if current else None)
        if same_name and not form.get("confirm_duplicate_name"):
            listed = ", ".join(
                f"{e.employee_code}{'' if e.is_active else ' (nonaktif)'}" for e in same_name
            )
            errors["full_name"] = (
                f"Nama ini sudah dipakai karyawan lain: {listed}. Jika orangnya sama, jangan "
                "buat data baru; aktifkan kembali data lama. Jika memang orang berbeda, "
                "centang konfirmasi di bawah."
            )
            errors["confirm_duplicate_name"] = "1"

    department = " ".join((form.get("department") or "").split())
    if not department:
        errors["department"] = "Departemen wajib diisi."
    elif len(department) > 100:
        errors["department"] = "Departemen maksimal 100 karakter."

    schedule = None
    raw_schedule = (form.get("work_schedule_id") or "").strip()
    if not raw_schedule:
        errors["work_schedule_id"] = "Jadwal kerja wajib dipilih."
    else:
        try:
            schedule = db.session.get(WorkSchedule, int(raw_schedule))
        except ValueError:
            schedule = None
        if schedule is None or not schedule.is_active:
            errors["work_schedule_id"] = "Pilih jadwal kerja yang masih aktif."

    if errors:
        raise ValidationError(errors)

    return {
        "employee_code": code,
        "full_name": full_name,
        "department": department,
        "work_schedule": schedule,
    }


def find_same_name(full_name, exclude_id=None):
    """Karyawan lain (aktif maupun nonaktif) dengan nama yang sama persis,
    tanpa membedakan huruf besar/kecil."""
    stmt = db.select(Employee).where(func.lower(Employee.full_name) == full_name.lower())
    if exclude_id is not None:
        stmt = stmt.where(Employee.id != exclude_id)
    return db.session.execute(stmt.order_by(Employee.employee_code)).scalars().all()


def _commit_or_conflict():
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise ValidationError(
            {"employee_code": "Kode karyawan sudah dipakai karyawan lain."}
        )


def list_employees(q="", department="", status="active", page=1, per_page=PER_PAGE):
    stmt = db.select(Employee).options(selectinload(Employee.work_schedule))

    q = (q or "").strip()
    if q:
        like = f"%{q}%"
        stmt = stmt.where(
            or_(Employee.employee_code.ilike(like), Employee.full_name.ilike(like))
        )
    if department:
        stmt = stmt.where(Employee.department == department)
    if status == "active":
        stmt = stmt.where(Employee.is_active.is_(True))
    elif status == "inactive":
        stmt = stmt.where(Employee.is_active.is_(False))

    stmt = stmt.order_by(Employee.full_name, Employee.id)
    return db.paginate(stmt, page=page, per_page=per_page, error_out=False)


def list_departments():
    return db.session.execute(
        db.select(Employee.department).distinct().order_by(Employee.department)
    ).scalars().all()


def count_employees(active=True):
    return db.session.execute(
        db.select(func.count(Employee.id)).where(Employee.is_active.is_(active))
    ).scalar_one()


def create_employee(form, actor_id):
    data = _clean(form)
    employee = Employee(
        employee_code=data["employee_code"],
        full_name=data["full_name"],
        department=data["department"],
        work_schedule=data["work_schedule"],
        is_active=True,
    )
    db.session.add(employee)
    db.session.flush()

    audit.log_event(
        audit.EMPLOYEE_CREATED,
        user_id=actor_id,
        entity_type="employee",
        entity_id=employee.id,
        description=(
            f"Karyawan ditambahkan: {employee.employee_code} - {employee.full_name} "
            f"({employee.department}, jadwal {employee.work_schedule.name})"
            + (" [nama sama dengan karyawan lain, dikonfirmasi admin sebagai orang berbeda]"
               if form.get("confirm_duplicate_name") and find_same_name(
                   employee.full_name, exclude_id=employee.id) else "")
        ),
        commit=False,
    )
    _commit_or_conflict()
    return employee


def update_employee(employee, form, actor_id):
    data = _clean(form, current=employee)
    before = _snapshot(employee)

    employee.employee_code = data["employee_code"]
    employee.full_name = data["full_name"]
    employee.department = data["department"]
    employee.work_schedule = data["work_schedule"]

    changes = audit.describe_changes(before, _snapshot(employee), FIELD_LABELS)
    if changes:
        audit.log_event(
            audit.EMPLOYEE_UPDATED,
            user_id=actor_id,
            entity_type="employee",
            entity_id=employee.id,
            description=f"Data karyawan {employee.employee_code} diubah. {changes}",
            commit=False,
        )
    _commit_or_conflict()
    return employee


def set_employee_active(employee, active, actor_id):
    if employee.is_active == active:
        return employee

    if active and (employee.work_schedule is None or not employee.work_schedule.is_active):
        raise ValidationError({
            "_": (
                f"Karyawan {employee.employee_code} belum memiliki jadwal aktif. "
                "Ubah datanya dan pilih jadwal aktif sebelum mengaktifkan kembali."
            )
        })

    employee.is_active = active
    audit.log_event(
        audit.EMPLOYEE_ACTIVATED if active else audit.EMPLOYEE_DEACTIVATED,
        user_id=actor_id,
        entity_type="employee",
        entity_id=employee.id,
        description=(
            f"Karyawan {employee.employee_code} - {employee.full_name} "
            f"{'diaktifkan kembali' if active else 'dinonaktifkan'}"
        ),
        commit=False,
    )
    db.session.commit()
    return employee
