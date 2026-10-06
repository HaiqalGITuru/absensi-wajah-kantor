"""
Autentikasi admin (FR-01): login, logout, dan halaman utama admin.
"""
from urllib.parse import urlsplit

from flask import Blueprint, current_app, flash, redirect, render_template, request, url_for
from flask_login import current_user, login_required, login_user, logout_user
from werkzeug.security import check_password_hash, generate_password_hash

from extensions import db, login_manager
from models import AttendanceSession, AuditLog, User, office_now
from kiosk.state import KioskState, admin_view
from services import audit_service as audit
from services import biometric_service, employee_service, schedule_service

bp = Blueprint("auth", __name__)

# Hash palsu untuk menyamakan waktu respons ketika username tidak ditemukan,
# sehingga penyerang tidak bisa menebak username dari lamanya proses login.
_DUMMY_HASH = generate_password_hash("penyamaan-waktu-respons-login")


@login_manager.user_loader
def load_user(user_id):
    try:
        return db.session.get(User, int(user_id))
    except (TypeError, ValueError):
        return None


def _safe_next_url(target):
    """Hanya izinkan redirect ke path internal, cegah open redirect."""
    if not target or not target.startswith("/") or target.startswith("//"):
        return None
    if "\\" in target:
        return None
    parts = urlsplit(target)
    if parts.scheme or parts.netloc:
        return None
    return target


@bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("auth.dashboard"))

    if request.method == "POST":
        username = (request.form.get("username") or "").strip()
        password = request.form.get("password") or ""

        user = None
        if username:
            user = db.session.execute(
                db.select(User).filter_by(username=username)
            ).scalar_one_or_none()

        if user is None:
            check_password_hash(_DUMMY_HASH, password)
            password_ok = False
        else:
            password_ok = user.check_password(password)

        if user is not None and password_ok and user.is_active:
            login_user(user)
            audit.log_event(
                audit.LOGIN_SUCCESS,
                user_id=user.id,
                entity_type="user",
                entity_id=user.id,
                description=f"Login berhasil: {user.username}",
            )
            next_url = _safe_next_url(request.args.get("next"))
            return redirect(next_url or url_for("auth.dashboard"))

        # Pesan ke pengguna sengaja dibuat umum (tidak menyebut username atau
        # password mana yang salah). Detail alasan hanya ada di audit log.
        if user is None:
            reason = "username tidak terdaftar"
        elif not password_ok:
            reason = "password salah"
        else:
            reason = "akun nonaktif"
        audit.log_event(
            audit.LOGIN_FAILED,
            user_id=user.id if user else None,
            entity_type="user",
            entity_id=user.id if user else None,
            description=f"Login gagal untuk '{username[:50]}': {reason}",
        )
        flash("Username atau password salah.", "error")
        return render_template("login.html", username=username), 401

    return render_template("login.html")


@bp.post("/logout")
@login_required
def logout():
    user_id = current_user.id
    username = current_user.username
    logout_user()
    audit.log_event(
        audit.LOGOUT,
        user_id=user_id,
        entity_type="user",
        entity_id=user_id,
        description=f"Logout: {username}",
    )
    flash("Anda telah keluar.", "info")
    return redirect(url_for("auth.login"))


@bp.get("/admin")
@login_required
def dashboard():
    """Halaman utama admin. Statistik kehadiran lengkap dibuat di Tahap 5."""
    recent_logs = db.session.execute(
        db.select(AuditLog).order_by(AuditLog.id.desc()).limit(10)
    ).scalars().all()
    summary = {
        "active_employees": employee_service.count_employees(active=True),
        "inactive_employees": employee_service.count_employees(active=False),
        "active_schedules": len(schedule_service.active_schedules()),
        "employees_with_face": biometric_service.count_employees_with_templates(),
    }
    key_status = biometric_service.keyring_status()
    today = office_now().date()
    summary["checked_in_today"] = db.session.execute(
        db.select(db.func.count(AttendanceSession.id)).where(
            AttendanceSession.work_date == today, AttendanceSession.check_in_at.is_not(None))
    ).scalar_one()
    summary["late_today"] = db.session.execute(
        db.select(db.func.count(AttendanceSession.id)).where(
            AttendanceSession.work_date == today,
            AttendanceSession.status == AttendanceSession.STATUS_LATE)
    ).scalar_one()
    summary["checked_out_today"] = db.session.execute(
        db.select(db.func.count(AttendanceSession.id)).where(
            AttendanceSession.work_date == today, AttendanceSession.check_out_at.is_not(None))
    ).scalar_one()
    kiosk_status = admin_view(KioskState(current_app.config["KIOSK_STATE_FILE"]).read())
    return render_template(
        "dashboard.html",
        recent_logs=recent_logs,
        summary=summary,
        key_status=key_status,
        kiosk_status=kiosk_status,
        liveness_mode=current_app.config.get("LIVENESS_MODE", "off"),
    )
