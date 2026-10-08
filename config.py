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
    # Urutan pengaturan MJPG yang berhasil (disarankan oleh camera-check).
    CAMERA_FORMAT_STRATEGY = os.getenv("CAMERA_FORMAT_STRATEGY", "")

    # Model AI (Tahap 3B). Folder berisi file .onnx; tidak ikut ke Git.
    MODEL_DIR = os.getenv("MODEL_DIR", str(BASE_DIR / "model_files"))
    # Lebar gambar saat deteksi; frame lebih besar diperkecil agar cepat.
    FACE_DETECT_WIDTH = int(os.getenv("FACE_DETECT_WIDTH", "640"))
    # Ambang kualitas wajah. FACE_MIN_SHARPNESS masih SEMENTARA dan perlu
    # dikalibrasi dengan kamera dan lokasi sebenarnya (lihat face-check).
    FACE_MIN_SCORE = float(os.getenv("FACE_MIN_SCORE", "0.85"))
    FACE_MIN_SIZE = int(os.getenv("FACE_MIN_SIZE", "80"))
    FACE_MIN_SHARPNESS = float(os.getenv("FACE_MIN_SHARPNESS", "40"))

    # Pencocokan identitas (Tahap 3C). Ambang awal 0.363 (contoh resmi OpenCV)
    # terbukti terlalu rendah pada uji kantor 7 Okt 2026: skor orang BERBEDA
    # mencapai 0.39-0.49, skor orang SAMA 0.76-0.92 (C270). Ambang 0.60 memberi
    # jarak 0.11 di atas skor orang berbeda tertinggi. Periksa ulang dengan
    # `flask face-similarity-report` setiap ada karyawan baru (PRD 13.2).
    FACE_MATCH_THRESHOLD = float(os.getenv("FACE_MATCH_THRESHOLD", "0.60"))
    FACE_MATCH_MARGIN = float(os.getenv("FACE_MATCH_MARGIN", "0.05"))
    # Pelacakan: identitas dipastikan jika cocok TRACK_CONFIRM_VOTES kali dari
    # TRACK_WINDOW hasil terakhir pada jejak yang sama.
    TRACK_WINDOW = int(os.getenv("TRACK_WINDOW", "5"))
    TRACK_CONFIRM_VOTES = int(os.getenv("TRACK_CONFIRM_VOTES", "3"))
    IDENTITY_COOLDOWN_SECONDS = float(os.getenv("IDENTITY_COOLDOWN_SECONDS", "30"))

    # Aturan absensi (Tahap 4A). Lihat services/attendance_service.py.
    CHECKIN_EARLY_MINUTES = int(os.getenv("CHECKIN_EARLY_MINUTES", "120"))
    CHECKOUT_MIN_MINUTES = int(os.getenv("CHECKOUT_MIN_MINUTES", "240"))
    CHECKOUT_GRACE_HOURS = int(os.getenv("CHECKOUT_GRACE_HOURS", "6"))
    CHECKOUT_UPDATE_LATER = os.getenv("CHECKOUT_UPDATE_LATER", "true")

    # Kios (Tahap 4B). File status dan kunci layanan kamera (folder instance).
    KIOSK_STATE_FILE = os.getenv("KIOSK_STATE_FILE", str(BASE_DIR / "instance" / "kiosk_state.json"))
    KIOSK_LOCK_FILE = os.getenv("KIOSK_LOCK_FILE", str(BASE_DIR / "instance" / "kiosk.lock"))
    TEMPLATE_RELOAD_SECONDS = float(os.getenv("TEMPLATE_RELOAD_SECONDS", "60"))

    # Backup (Tahap 5D). Sebaiknya di luar folder proyek, mis. D:\absensi-backup.
    BACKUP_DIR = os.getenv("BACKUP_DIR", str(BASE_DIR / "backups"))
    BACKUP_KEEP = int(os.getenv("BACKUP_KEEP", "30"))

    # API baca-saja: batas permintaan per token per menit.
    API_RATE_LIMIT = int(os.getenv("API_RATE_LIMIT", "120"))

    # Anti-foto (Tahap 4D). Mode: off | passive | challenge | passive+challenge.
    # Ambang masih SEMENTARA dan perlu dikalibrasi di lokasi (PRD 13.2).
    LIVENESS_MODE = os.getenv("LIVENESS_MODE", "passive+challenge")
    LIVENESS_REAL_THRESHOLD = float(os.getenv("LIVENESS_REAL_THRESHOLD", "0.7"))
    LIVENESS_SPOOF_THRESHOLD = float(os.getenv("LIVENESS_SPOOF_THRESHOLD", "0.3"))
    LIVENESS_MIN_SAMPLES = int(os.getenv("LIVENESS_MIN_SAMPLES", "3"))
    LIVENESS_DECIDE_SECONDS = float(os.getenv("LIVENESS_DECIDE_SECONDS", "3"))
    LIVENESS_CHALLENGE_SECONDS = float(os.getenv("LIVENESS_CHALLENGE_SECONDS", "6"))
    LIVENESS_CHALLENGE_DELTA = float(os.getenv("LIVENESS_CHALLENGE_DELTA", "0.18"))
    LIVENESS_MIRRORED = os.getenv("LIVENESS_MIRRORED", "false")

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
