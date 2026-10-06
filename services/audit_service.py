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

MAX_DESCRIPTION_LENGTH = 1000


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
                   lain dalam satu transaksi (dipakai di Tahap 2B dan seterusnya).
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
