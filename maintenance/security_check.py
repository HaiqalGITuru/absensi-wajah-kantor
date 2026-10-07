"""
Daftar periksa keamanan sebelum sistem dipakai resmi (Tahap 5E).

Setiap butir menghasilkan status:
  OK     - memenuhi syarat
  PERLU  - WAJIB dibereskan sebelum dipakai resmi
  INFO   - perlu diketahui / diputuskan admin, tidak menghalangi

Nilai rahasia tidak pernah dicetak. Rahasia yang pernah terlihat selama
pengembangan (mis. di tangkapan layar) dikenali dari sidik jari SHA-256-nya
saja, sehingga kode ini tidak ikut membocorkan nilainya.
"""
import hashlib
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.exc import OperationalError

from extensions import db
from services.cli_errors import explain

OK, NEED, INFO = "OK", "PERLU", "INFO"

PROJECT = Path(__file__).resolve().parents[1]

# Sidik jari nilai yang sudah tidak rahasia: contoh di .env.example dan nilai
# yang pernah tampil di tangkapan layar selama pengembangan (7 Okt 2026).
EXPOSED = {
    "3021006a1609feb5ac0108adc92cbbbe3c758986ce38839dcb77f81bf41226b5",  # SECRET_KEY pengembangan
    "acbf51fc647a2fa8553c8a529406c3c1cd233c08b2e18f8c8ab3f5035b4bf7e6",  # password DB pengembangan
    "23f0418ab9cfcc0749f12a30c3b5478e99948ed5ba3a3991e7fa21ca6677c44b",  # contoh SECRET_KEY
    "992bb00afbef942f74e52060040007c6ae7e7719d522d18c76fc8f3069b821ef",  # contoh password DB
    "978ade24ee2485d0adf824241f496f96cd4bc2a26bb8557f8b7cbb81749299c4",  # SECRET_KEY pengganti, terlihat di layar 7 Okt
    "cb9156793e02ee199575d9a6613ee3f94b80e802473d10a67b97d5f5ef10bc3d",  # password DB pengganti, terlihat di layar 7 Okt
}

WEEKDAY_WORDS = {"senin", "selasa", "rabu", "kamis", "jumat", "sabtu", "minggu"}


@dataclass
class Item:
    status: str
    title: str
    detail: str = ""


def _exposed(value):
    return hashlib.sha256((value or "").encode()).hexdigest() in EXPOSED


def _inside(path, parent):
    try:
        Path(path).resolve().relative_to(Path(parent).resolve())
        return True
    except ValueError:
        return False


def check_app(cfg, env):
    items = []
    # APP_ENV di .env adalah sumber kebenaran: perintah `flask` menimpa nilai
    # DEBUG di konfigurasi, jadi app.config["DEBUG"] tidak bisa dipercaya di sini.
    app_env = (env.get("APP_ENV") or "production").strip().lower()
    items.append(Item(OK if app_env == "production" else NEED, "Mode produksi (debug mati)",
                      "" if app_env == "production" else f"APP_ENV={app_env}. Atur APP_ENV=production di .env."))

    key = cfg.get("SECRET_KEY") or ""
    if _exposed(key):
        items.append(Item(NEED, "SECRET_KEY baru",
                          "SECRET_KEY masih nilai contoh atau pernah terlihat di tangkapan layar. "
                          "Buat baru: python -c \"import secrets; print(secrets.token_hex(32))\""))
    elif len(key) < 32:
        items.append(Item(NEED, "SECRET_KEY baru", "SECRET_KEY terlalu pendek (minimal 32 karakter acak)."))
    else:
        items.append(Item(OK, "SECRET_KEY baru"))
    return items


def check_database(cfg, env):
    items = []
    user = env.get("DB_USER", "")
    pwd = env.get("DB_PASSWORD", "")
    if user.lower() == "root":
        items.append(Item(NEED, "Pengguna database khusus",
                          "Jangan memakai root. Pakai pengguna khusus (absensi_app) yang hanya punya "
                          "hak ke database absensi."))
    else:
        items.append(Item(OK, "Pengguna database khusus"))
    if _exposed(pwd):
        items.append(Item(NEED, "Password database baru",
                          "Password database masih contoh atau pernah terlihat di tangkapan layar. "
                          "Ganti di phpMyAdmin > User accounts, lalu ubah DB_PASSWORD di .env."))
    elif len(pwd) < 12:
        items.append(Item(NEED, "Password database baru", "Password database minimal 12 karakter."))
    else:
        items.append(Item(OK, "Password database baru"))
    return items


def check_secrets_location(cfg):
    items = []
    key_file = cfg.get("BIOMETRIC_KEY_FILE")
    if not key_file:
        items.append(Item(NEED, "Lokasi kunci biometrik", "BIOMETRIC_KEY_FILE belum diatur."))
    elif _inside(key_file, PROJECT):
        items.append(Item(NEED, "Lokasi kunci biometrik",
                          "File kunci berada di dalam folder proyek (bisa ikut ke Git). Pindahkan ke luar."))
    else:
        items.append(Item(OK, "Lokasi kunci biometrik"))

    tracked = None
    try:
        res = subprocess.run(["git", "ls-files", "--error-unmatch", ".env"], cwd=PROJECT,
                             capture_output=True, text=True, timeout=10)
        tracked = res.returncode == 0
    except (OSError, subprocess.SubprocessError):
        pass
    if tracked:
        items.append(Item(NEED, ".env tidak ada di Git",
                          ".env tercatat di Git. Hapus dari Git (git rm --cached .env) dan ganti semua "
                          "rahasia di dalamnya, karena riwayat Git menyimpannya."))
    elif tracked is False:
        items.append(Item(OK, ".env tidak ada di Git"))
    else:
        items.append(Item(INFO, ".env tidak ada di Git", "Git tidak tersedia; periksa manual."))
    return items


def check_biometrics(cfg):
    from recognition import model_files as mf
    from services import biometric_service as bio

    items = []
    status = bio.keyring_status()
    if not status["ok"]:
        items.append(Item(NEED, "Kunci biometrik siap", status["message"]))
        return items
    try:
        from recognition.embedder import MODEL_NAME

        _, failed = bio.load_active_templates(MODEL_NAME)
        items.append(Item(NEED if failed else OK, "Semua templat wajah terbaca",
                          f"{failed} templat gagal diverifikasi." if failed else ""))
    finally:
        db.session.rollback()

    missing = []
    for model in mf.ALL_MODELS:
        try:
            mf.verify(cfg["MODEL_DIR"], model)
        except mf.ModelFileError:
            missing.append(model.filename)
    items.append(Item(NEED if missing else OK, "File model lengkap dan asli",
                      ("Bermasalah: " + ", ".join(missing) + ". Jalankan models-download.") if missing else ""))

    mode = str(cfg.get("LIVENESS_MODE", "off")).lower()
    items.append(Item(NEED if mode == "off" else OK, "Anti-foto aktif",
                      "LIVENESS_MODE=off: foto wajah bisa dipakai untuk absen." if mode == "off" else f"mode {mode}"))

    threshold = float(cfg.get("FACE_MATCH_THRESHOLD", 0))
    if threshold < 0.5:
        items.append(Item(NEED, "Ambang kecocokan terkalibrasi",
                          f"FACE_MATCH_THRESHOLD={threshold} terlalu rendah; uji kantor 7 Okt menunjukkan "
                          "orang berbeda mencapai 0,49. Pakai 0.60 dan periksa face-similarity-report."))
    else:
        items.append(Item(OK, "Ambang kecocokan terkalibrasi", f"{threshold}"))
    return items


def check_backup(cfg, now=None):
    items = []
    backup_dir = Path(cfg.get("BACKUP_DIR", ""))
    if _inside(backup_dir, PROJECT):
        items.append(Item(INFO, "Folder backup di luar proyek",
                          "BACKUP_DIR berada di dalam folder proyek. Sebaiknya di luar, mis. D:\\absensi-backup."))
    files = sorted(backup_dir.glob("absensi_backup_*.zip")) if backup_dir.exists() else []
    if not files:
        items.append(Item(NEED, "Backup terbaru", "Belum ada backup. Jalankan backup-create dan jadwalkan harian."))
    else:
        age_h = ((now or time.time()) - files[-1].stat().st_mtime) / 3600
        if age_h > 48:
            items.append(Item(NEED, "Backup terbaru",
                              f"Backup terakhir {age_h / 24:.0f} hari lalu. Periksa jadwal backup harian."))
        else:
            items.append(Item(OK, "Backup terbaru", files[-1].name))
    return items


def check_data():
    from models import Employee, FaceTemplate, User, WorkSchedule

    items = []
    admins = db.session.execute(db.select(User).filter_by(is_active=True)).scalars().all()
    if not admins:
        items.append(Item(NEED, "Akun admin", "Belum ada admin aktif. Jalankan create-admin."))
    else:
        names = ", ".join(a.username for a in admins)
        generic = [a.username for a in admins if a.username.lower() in ("admin", "administrator", "root")]
        items.append(Item(INFO if generic else OK, "Akun admin",
                          f"{len(admins)} admin aktif ({names})."
                          + (" Nama pengguna umum seperti 'admin' mudah ditebak; pertimbangkan nama "
                             "pengguna perorangan." if generic else "")))

    odd = [s.name for s in db.session.execute(db.select(WorkSchedule).filter_by(is_active=True)).scalars()
           if s.name.strip().lower() in WEEKDAY_WORDS]
    items.append(Item(NEED if odd else OK, "Nama jadwal kerja jelas",
                      ("Jadwal bernama hari (" + ", ".join(odd) + ") kemungkinan salah isi. Ganti nama "
                       "dan periksa jam masuk/pulang, termasuk Jumat.") if odd else ""))

    with_face = db.select(FaceTemplate.employee_id).distinct()
    no_face = db.session.execute(
        db.select(db.func.count(Employee.id)).where(Employee.is_active.is_(True),
                                                    Employee.id.not_in(with_face))).scalar_one()
    if no_face:
        items.append(Item(INFO, "Wajah karyawan aktif terdaftar",
                          f"{no_face} karyawan aktif belum punya wajah terdaftar."))
    else:
        items.append(Item(OK, "Wajah karyawan aktif terdaftar"))
    db.session.rollback()
    return items


def run_all(app, env):
    cfg = app.config
    groups = [
        ("Aplikasi", lambda: check_app(cfg, env)),
        ("Database", lambda: check_database(cfg, env)),
        ("Rahasia", lambda: check_secrets_location(cfg)),
        ("Pengenalan wajah", lambda: check_biometrics(cfg)),
        ("Backup", lambda: check_backup(cfg)),
        ("Data", check_data),
    ]
    results = []
    for name, fn in groups:
        try:
            results.append((name, fn()))
        except OperationalError as exc:
            db.session.rollback()
            results.append((name, [Item(NEED, "Database tidak bisa diperiksa", explain(exc))]))
        except Exception as exc:  # satu kelompok gagal tidak menghentikan pemeriksaan lain
            db.session.rollback()
            results.append((name, [Item(NEED, "Pemeriksaan gagal dijalankan", str(exc))]))
    return results


MANUAL = [
    "Password root MySQL di XAMPP sudah diisi (bawaan XAMPP: kosong).",
    "phpMyAdmin hanya bisa dibuka dari komputer ini (cek di XAMPP: Require local).",
    "Akun Windows komputer kios berpassword dan layar terkunci otomatis.",
    "Disk komputer terenkripsi (BitLocker), atau komputer di ruangan terkunci.",
    "Salinan kunci biometrik di media terpisah (flashdisk/hard disk eksternal) yang disimpan terkunci.",
    "Data uji sudah dikosongkan sesuai docs/BACKUP.md (bagian pengosongan data uji).",
    "Pembimbing meninjau docs/MODEL_DECISION.md, termasuk catatan lisensi dataset WIDER FACE.",
    "Karyawan sudah diberi tahu tentang pemrosesan data wajah dan tujuannya; persetujuan dan dasar "
    "pemrosesan dikonfirmasi dengan bagian hukum/kepegawaian instansi (UU No. 27 Tahun 2022 tentang PDP).",
    "Metode absensi alternatif (mis. buku hadir + koreksi admin) disepakati untuk saat kamera gangguan.",
    "Kios terpasang di area yang terlihat petugas.",
]
