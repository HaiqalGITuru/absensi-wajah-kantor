"""
Titik masuk aplikasi Flask (application factory).

Menjalankan:
    flask --app app run
atau:
    python app.py

Membuat akun admin pertama:
    flask --app app create-admin

Kunci enkripsi templat wajah:
    flask --app app biometric-keygen      (sekali, saat instalasi)
    flask --app app biometric-status      (memeriksa kunci dan templat)
    flask --app app biometric-rotate-key  (mengganti kunci)
"""
import re

import click
from flask import Flask, flash, jsonify, redirect, render_template, url_for
from flask_wtf.csrf import CSRFError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import models  # noqa: F401  (mendaftarkan model untuk Flask-Migrate)
from config import BASE_DIR, get_config
from extensions import csrf, db, login_manager, migrate
from models import User, office_now
from routes.auth import bp as auth_bp
from routes.employees import bp as employees_bp
from routes.schedules import bp as schedules_bp
from services import audit_service as audit
from services import biometric_service as biometric

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

    @app.cli.command("biometric-keygen")
    def biometric_keygen():
        """Membuat file kunci enkripsi templat wajah (sekali saat instalasi)."""
        try:
            path = biometric.generate_keyring()
        except biometric.BiometricKeyError as exc:
            raise click.ClickException(str(exc))

        audit.log_event(
            audit.BIOMETRIC_KEY_CREATED,
            entity_type="biometric_key",
            entity_id=1,
            description="File kunci biometrik dibuat melalui CLI (v1)",
        )
        click.echo(f"File kunci dibuat: {path}")
        if BASE_DIR in path.resolve().parents:
            click.secho(
                "PERINGATAN: file kunci berada di dalam folder proyek. "
                "Pindahkan ke luar folder proyek dan perbarui BIOMETRIC_KEY_FILE.",
                fg="yellow",
            )
        click.echo(
            "Simpan salinan file ini di tempat aman yang terpisah. "
            "Jika file ini hilang, semua wajah harus didaftarkan ulang."
        )

    @app.cli.command("biometric-status")
    def biometric_status():
        """Memeriksa file kunci dan memastikan semua templat dapat dibaca."""
        try:
            active, keys = biometric.load_keyring()
        except biometric.BiometricKeyError as exc:
            raise click.ClickException(str(exc))

        templates = db.session.execute(db.select(models.FaceTemplate)).scalars().all()
        failed = 0
        for template in templates:
            try:
                biometric.decrypt_template(template, (active, keys))
            except biometric.TemplateDecryptionError:
                failed += 1

        click.echo(f"Kunci aktif     : v{active} ({len(keys)} versi tersimpan)")
        click.echo(f"Templat wajah   : {len(templates)}")
        click.echo(f"Gagal diverifikasi: {failed}")
        if failed:
            raise click.ClickException(
                "Ada templat yang tidak bisa dibaca. Periksa file kunci atau "
                "kemungkinan perubahan data di database."
            )

    @app.cli.command("biometric-rotate-key")
    @click.confirmation_option(
        prompt="Buat kunci baru dan enkripsi ulang semua templat wajah?"
    )
    def biometric_rotate_key():
        """Membuat kunci baru dan mengenkripsi ulang semua templat."""
        try:
            version, count = biometric.rotate_key()
        except biometric.BiometricKeyError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Kunci aktif sekarang v{version}. {count} templat dienkripsi ulang.")
        click.echo("Perbarui salinan cadangan file kunci Anda.")


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
    app.register_blueprint(employees_bp)
    app.register_blueprint(schedules_bp)
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
