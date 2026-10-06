"""
Konfigurasi aplikasi.

Semua nilai rahasia (SECRET_KEY, password database) dibaca dari file .env,
tidak pernah ditulis langsung di kode sumber.
"""
import os
from pathlib import Path

from dotenv import load_dotenv
from sqlalchemy.engine import URL

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")


def _build_database_uri():
    """Menyusun URI MySQL dari variabel lingkungan.

    URL.create dipakai agar karakter khusus pada password (misalnya @ atau #)
    di-escape dengan benar.
    """
    override = os.getenv("DATABASE_URL")
    if override:
        return override

    url = URL.create(
        drivername="mysql+pymysql",
        username=os.getenv("DB_USER", "absensi_app"),
        password=os.getenv("DB_PASSWORD") or None,
        host=os.getenv("DB_HOST", "127.0.0.1"),
        port=int(os.getenv("DB_PORT", "3306")),
        database=os.getenv("DB_NAME", "absensi_wajah"),
        query={"charset": "utf8mb4"},
    )
    return url.render_as_string(hide_password=False)


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY")

    SQLALCHEMY_DATABASE_URI = _build_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        # Memeriksa koneksi sebelum dipakai, berguna jika MySQL sempat restart.
        "pool_pre_ping": True,
        "pool_recycle": 280,
    }

    OFFICE_TIMEZONE = os.getenv("OFFICE_TIMEZONE", "Asia/Makassar")

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    DEBUG = False
    TESTING = False


class DevelopmentConfig(Config):
    DEBUG = True


class ProductionConfig(Config):
    DEBUG = False


class TestingConfig(Config):
    TESTING = True
    SECRET_KEY = "kunci-khusus-pengujian-bukan-untuk-produksi"
    # Pengujian memakai SQLite di memori agar tidak menyentuh data MySQL.
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS = {}


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
}


def get_config(name=None):
    """Mengembalikan kelas konfigurasi. Default-nya production (debug mati)."""
    name = (name or os.getenv("APP_ENV", "production")).lower()
    if name not in CONFIGS:
        raise ValueError(
            f"APP_ENV '{name}' tidak dikenal. Pilihan: {', '.join(CONFIGS)}"
        )
    return CONFIGS[name]
