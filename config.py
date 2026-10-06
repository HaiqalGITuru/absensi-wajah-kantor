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

    # Lokasi file kunci enkripsi templat wajah. Simpan DI LUAR folder proyek.
    BIOMETRIC_KEY_FILE = os.getenv("BIOMETRIC_KEY_FILE")

    # Kamera (Tahap 3). Backend "dshow" disarankan untuk Windows.
    CAMERA_INDEX = int(os.getenv("CAMERA_INDEX", "0"))
    # Jika diisi (mis. C270), kamera dicari berdasarkan nama dan CAMERA_INDEX
    # hanya dipakai sebagai cadangan.
    CAMERA_NAME = os.getenv("CAMERA_NAME", "")
    CAMERA_BACKEND = os.getenv("CAMERA_BACKEND", "dshow" if os.name == "nt" else "any")
    CAMERA_WIDTH = int(os.getenv("CAMERA_WIDTH", "1280"))
    CAMERA_HEIGHT = int(os.getenv("CAMERA_HEIGHT", "720"))
    CAMERA_FPS = int(os.getenv("CAMERA_FPS", "30"))

    # Model AI (Tahap 3B). Folder berisi file .onnx; tidak ikut ke Git.
    MODEL_DIR = os.getenv("MODEL_DIR", str(BASE_DIR / "model_files"))
    # Lebar gambar saat deteksi; frame lebih besar diperkecil agar cepat.
    FACE_DETECT_WIDTH = int(os.getenv("FACE_DETECT_WIDTH", "640"))
    # Ambang kualitas wajah. FACE_MIN_SHARPNESS masih SEMENTARA dan perlu
    # dikalibrasi dengan kamera dan lokasi sebenarnya (lihat face-check).
    FACE_MIN_SCORE = float(os.getenv("FACE_MIN_SCORE", "0.85"))
    FACE_MIN_SIZE = int(os.getenv("FACE_MIN_SIZE", "80"))
    FACE_MIN_SHARPNESS = float(os.getenv("FACE_MIN_SHARPNESS", "40"))

    # Pencocokan identitas (Tahap 3C). Ambang 0.363 berasal dari kode contoh
    # resmi OpenCV untuk SFace; SEMENTARA, wajib dikalibrasi (PRD 13.2).
    FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.363"))
    FACE_MATCH_MARGIN = float(os.getenv("FACE_MATCH_MARGIN", "0.05"))
    # Pelacakan: identitas dipastikan jika cocok TRACK_CONFIRM_VOTES kali dari
    # TRACK_WINDOW hasil terakhir pada jejak yang sama.
    TRACK_WINDOW = int(os.getenv("TRACK_WINDOW", "5"))
    TRACK_CONFIRM_VOTES = int(os.getenv("TRACK_CONFIRM_VOTES", "3"))
    IDENTITY_COOLDOWN_SECONDS = float(os.getenv("IDENTITY_COOLDOWN_SECONDS", "30"))

    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"

    # Token CSRF berlaku 1 jam (detik)
    WTF_CSRF_TIME_LIMIT = 3600

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
    # CSRF dimatikan agar tes formulir sederhana; ada tes khusus yang
    # menyalakannya kembali untuk memastikan perlindungan CSRF bekerja.
    WTF_CSRF_ENABLED = False
    # Diisi oleh fixture pengujian dengan folder sementara.
    BIOMETRIC_KEY_FILE = None


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
