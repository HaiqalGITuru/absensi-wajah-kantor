"""
Model database (PRD bagian 8).

Catatan: semua waktu disimpan sebagai waktu lokal kantor (WITA) tanpa
informasi zona, sesuai PRD BR-06. Gunakan office_now() untuk waktu resmi,
jangan datetime.now() atau waktu dari browser.
"""
from datetime import datetime
from zoneinfo import ZoneInfo

from flask import current_app, has_app_context

from extensions import db

DEFAULT_TIMEZONE = "Asia/Makassar"


def office_now():
    """Waktu server saat ini dalam zona waktu kantor (naive datetime)."""
    tz_name = DEFAULT_TIMEZONE
    if has_app_context():
        tz_name = current_app.config.get("OFFICE_TIMEZONE", DEFAULT_TIMEZONE)
    return datetime.now(ZoneInfo(tz_name)).replace(tzinfo=None)


class TimestampMixin:
    created_at = db.Column(db.DateTime, nullable=False, default=office_now)
    updated_at = db.Column(
        db.DateTime, nullable=False, default=office_now, onupdate=office_now
    )


class User(TimestampMixin, db.Model):
    """Akun admin (PRD 8.1)."""

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False)
    username = db.Column(db.String(50), nullable=False, unique=True)
    password_hash = db.Column(db.String(255), nullable=False)
    role = db.Column(db.String(20), nullable=False, default="admin")
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    def __repr__(self):
        return f"<User {self.username}>"


class WorkSchedule(TimestampMixin, db.Model):
    """Jadwal kerja (PRD 8.4)."""

    __tablename__ = "work_schedules"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)
    start_time = db.Column(db.Time, nullable=False)
    end_time = db.Column(db.Time, nullable=False)
    late_tolerance_minutes = db.Column(db.Integer, nullable=False, default=0)
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    employees = db.relationship("Employee", back_populates="work_schedule")

    def __repr__(self):
        return f"<WorkSchedule {self.name}>"


class Employee(TimestampMixin, db.Model):
    """Data karyawan (PRD 8.2).

    Kolom work_schedule_id ditambahkan karena FR-02 mewajibkan jadwal kerja
    sebagai data minimal karyawan.
    """

    __tablename__ = "employees"

    id = db.Column(db.Integer, primary_key=True)
    employee_code = db.Column(db.String(30), nullable=False, unique=True)
    full_name = db.Column(db.String(150), nullable=False)
    department = db.Column(db.String(100), nullable=False)
    work_schedule_id = db.Column(
        db.Integer, db.ForeignKey("work_schedules.id"), nullable=True
    )
    is_active = db.Column(db.Boolean, nullable=False, default=True)

    work_schedule = db.relationship("WorkSchedule", back_populates="employees")
    face_templates = db.relationship("FaceTemplate", back_populates="employee")
    attendance_sessions = db.relationship(
        "AttendanceSession", back_populates="employee"
    )

    def __repr__(self):
        return f"<Employee {self.employee_code}>"


class FaceTemplate(TimestampMixin, db.Model):
    """Templat wajah terenkripsi (PRD 8.3). Kunci enkripsi TIDAK disimpan di sini."""

    __tablename__ = "face_templates"

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(
        db.Integer, db.ForeignKey("employees.id"), nullable=False, index=True
    )
    encrypted_embedding = db.Column(db.LargeBinary, nullable=False)
    encryption_key_version = db.Column(db.Integer, nullable=False, default=1)
    model_name = db.Column(db.String(100), nullable=False)

    employee = db.relationship("Employee", back_populates="face_templates")

    def __repr__(self):
        return f"<FaceTemplate employee={self.employee_id} model={self.model_name}>"


class AttendanceSession(TimestampMixin, db.Model):
    """Sesi kerja yang menghubungkan check-in dan check-out (PRD 8.5).

    Batasan unik (employee_id, work_date) mencegah sesi ganda di level
    database (BR-05). Jika nanti ada shift malam atau beberapa sesi per hari,
    batasan ini diubah melalui migrasi di Tahap 4.
    """

    __tablename__ = "attendance_sessions"
    __table_args__ = (
        db.UniqueConstraint("employee_id", "work_date"),
    )

    id = db.Column(db.Integer, primary_key=True)
    employee_id = db.Column(
        db.Integer, db.ForeignKey("employees.id"), nullable=False
    )
    work_schedule_id = db.Column(
        db.Integer, db.ForeignKey("work_schedules.id"), nullable=True
    )
    work_date = db.Column(db.Date, nullable=False, index=True)
    check_in_at = db.Column(db.DateTime, nullable=True)
    check_out_at = db.Column(db.DateTime, nullable=True)
    status = db.Column(db.String(20), nullable=False, default="open")

    employee = db.relationship("Employee", back_populates="attendance_sessions")
    work_schedule = db.relationship("WorkSchedule")

    def __repr__(self):
        return f"<AttendanceSession employee={self.employee_id} date={self.work_date}>"


class AuditLog(db.Model):
    """Catatan aktivitas penting (PRD 8.6).

    user_id boleh kosong untuk kejadian sistem (misalnya kamera terputus).
    Jangan pernah menyimpan password, kunci enkripsi, atau data biometrik di sini.
    """

    __tablename__ = "audit_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    action = db.Column(db.String(50), nullable=False)
    entity_type = db.Column(db.String(50), nullable=True)
    entity_id = db.Column(db.Integer, nullable=True)
    description = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime, nullable=False, default=office_now, index=True
    )

    user = db.relationship("User")

    def __repr__(self):
        return f"<AuditLog {self.action}>"
