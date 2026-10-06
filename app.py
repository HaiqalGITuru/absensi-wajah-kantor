"""
Titik masuk aplikasi Flask (application factory).

Menjalankan:
    flask --app app run
atau:
    python app.py

Membuat akun admin pertama:
    flask --app app create-admin
"""
import re

import click
from flask import Flask, flash, jsonify, redirect, render_template, url_for
from flask_wtf.csrf import CSRFError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import models  # noqa: F401  (mendaftarkan model untuk Flask-Migrate)
from config import get_config
from extensions import csrf, db, login_manager, migrate
from models import User, office_now
from routes.auth import bp as auth_bp
from services import audit_service as audit

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
MIN_PASSWORD_LENGTH = 10


def check_database():
    """Mengembalikan True jika database dapat menjalankan query sederhana."""
    try:
        db.session.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        db.session.rollback()
        return False


def register_commands(app):
    @app.cli.command("create-admin")
    @click.option("--username", prompt="Username admin")
    @click.option("--name", prompt="Nama lengkap")
    @click.password_option(
        "--password",
        prompt="Password (minimal 10 karakter)",
        confirmation_prompt="Ulangi password",
    )
    def create_admin(username, name, password):
        """Membuat akun admin baru tanpa menulis password di kode."""
        username = username.strip()
        name = name.strip()

        if not USERNAME_PATTERN.match(username):
            raise click.ClickException(
                "Username harus 3-50 karakter: huruf, angka, titik, "
                "garis bawah, atau tanda minus."
            )
        if not name:
            raise click.ClickException("Nama lengkap tidak boleh kosong.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(
                f"Password minimal {MIN_PASSWORD_LENGTH} karakter."
            )

        exists = db.session.execute(
            db.select(User).filter_by(username=username)
        ).scalar_one_or_none()
        if exists:
            raise click.ClickException(f"Username '{username}' sudah dipakai.")

        user = User(name=name, username=username, role="admin", is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        audit.log_event(
            audit.ADMIN_CREATED,
            entity_type="user",
            entity_id=user.id,
            description=f"Admin dibuat melalui CLI: {username}",
            commit=False,
        )
        db.session.commit()
        click.echo(f"Admin '{username}' berhasil dibuat.")


def create_app(config_name=None):
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError(
            "SECRET_KEY belum diatur. Salin .env.example menjadi .env "
            "lalu isi SECRET_KEY."
        )

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    app.register_blueprint(auth_bp)
    register_commands(app)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        flash("Formulir kedaluwarsa atau tidak valid. Silakan coba lagi.", "error")
        return redirect(url_for("auth.login")), 302

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        db_ok = check_database()
        if not db_ok:
            # Detail error hanya dicatat di log server, tidak dikirim ke browser.
            app.logger.error("Health check: koneksi database gagal.")

        payload = {
            "status": "ok" if db_ok else "degraded",
            "app": "ok",
            "database": "ok" if db_ok else "error",
            "server_time": office_now().isoformat(timespec="seconds"),
            "timezone": app.config["OFFICE_TIMEZONE"],
        }
        return jsonify(payload), (200 if db_ok else 503)

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="127.0.0.1", port=5000)
