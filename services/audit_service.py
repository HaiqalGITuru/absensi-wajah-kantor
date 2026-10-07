"""
Layanan audit log (FR-14).

Aturan penting: JANGAN pernah mengirim password, kunci enkripsi, atau data
biometrik ke fungsi log_event.
"""
from flask import current_app, has_request_context, request
from sqlalchemy.exc import SQLAlchemyError

from extensions import db
from models import AuditLog

# Nama aksi dibuat konstan agar konsisten dan mudah difilter di laporan.
LOGIN_SUCCESS = "login_success"
LOGIN_FAILED = "login_failed"
LOGOUT = "logout"
ADMIN_CREATED = "admin_created"

EMPLOYEE_CREATED = "employee_created"
EMPLOYEE_UPDATED = "employee_updated"
EMPLOYEE_DEACTIVATED = "employee_deactivated"
EMPLOYEE_ACTIVATED = "employee_activated"

SCHEDULE_CREATED = "schedule_created"
SCHEDULE_UPDATED = "schedule_updated"
SCHEDULE_DEACTIVATED = "schedule_deactivated"
SCHEDULE_ACTIVATED = "schedule_activated"

FACE_TEMPLATE_CREATED = "face_template_created"
FACE_TEMPLATE_REVOKED = "face_template_revoked"
FACE_ENROLL_SIMILAR_OVERRIDE = "face_enroll_similar_override"

ATTENDANCE_CORRECTED = "attendance_corrected"
ATTENDANCE_CANCELLED = "attendance_cancelled"

LIVENESS_REJECTED = "liveness_rejected"

REPORT_EXPORTED = "report_exported"

BACKUP_CREATED = "backup_created"
BACKUP_RESTORED = "backup_restored"
BIOMETRIC_KEY_CREATED = "biometric_key_created"
BIOMETRIC_KEY_ROTATED = "biometric_key_rotated"

MAX_DESCRIPTION_LENGTH = 1000


def describe_changes(before, after, labels=None):
    """Menyusun teks perubahan, mis. "Nama: 'A' -> 'B'; Departemen: 'X' -> 'Y'".

    Mengembalikan string kosong jika tidak ada yang berubah.
    """
    labels = labels or {}
    parts = []
    for key, old in before.items():
        new = after.get(key)
        if old != new:
            label = labels.get(key, key)
            parts.append(f"{label}: '{old}' -> '{new}'")
    return "; ".join(parts)


def log_event(
    action,
    *,
    user_id=None,
    entity_type=None,
    entity_id=None,
    description=None,
    commit=True,
):
    """Mencatat satu kejadian ke tabel audit_logs.

    commit=True  : langsung disimpan (untuk kejadian berdiri sendiri, mis. login).
    commit=False : hanya ditambahkan ke sesi, disimpan bersama perubahan data
                   lain dalam satu transaksi, sehingga data dan catatan auditnya
                   selalu tersimpan bersamaan atau gagal bersamaan.
    """
    if has_request_context() and request.remote_addr:
        ip_note = f"[IP {request.remote_addr}]"
        description = f"{description} {ip_note}" if description else ip_note

    entry = AuditLog(
        action=action,
        user_id=user_id,
        entity_type=entity_type,
        entity_id=entity_id,
        description=(description or "")[:MAX_DESCRIPTION_LENGTH] or None,
    )
    db.session.add(entry)

    if commit:
        try:
            db.session.commit()
        except SQLAlchemyError:
            db.session.rollback()
            current_app.logger.exception("Gagal menyimpan audit log: %s", action)
            return None
    return entry
