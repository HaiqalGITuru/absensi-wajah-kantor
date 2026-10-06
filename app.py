"""
Titik masuk aplikasi Flask (application factory).

Menjalankan:
    flask --app app run
atau:
    python app.py
"""
from flask import Flask, jsonify, render_template
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import models  # noqa: F401  (mendaftarkan model untuk Flask-Migrate)
from config import get_config
from extensions import db, migrate
from models import office_now


def check_database():
    """Mengembalikan True jika database dapat menjalankan query sederhana."""
    try:
        db.session.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        db.session.rollback()
        return False


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
