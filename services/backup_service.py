"""
Backup dan pemulihan database (Tahap 5D).

Format backup: satu file ZIP berisi
  manifest.json            - versi format, waktu, revisi skema (Alembic),
                             jumlah baris dan SHA-256 tiap tabel, versi kunci
                             biometrik yang dibutuhkan templat wajah
  data/<tabel>.jsonl       - satu baris JSON per baris tabel

Kunci biometrik TIDAK ikut di dalam backup database. Kunci disalin terpisah
(backup-key) dan disimpan di tempat lain; tanpa kunci, templat wajah di backup
tidak bisa dibaca, dan tanpa backup, kunci tidak berguna.

Pemulihan hanya dilakukan ke database KOSONG dengan revisi skema yang sama,
sehingga tidak pernah menimpa atau mencampur data yang sudah ada.
"""
import base64
import hashlib
import json
import os
import shutil
import tempfile
import zipfile
from datetime import date, datetime, time
from decimal import Decimal
from pathlib import Path

from sqlalchemy import func, inspect, select, text

from extensions import db
from models import office_now

FORMAT = "absensi-wajah-backup"
FORMAT_VERSION = 1
PREFIX = "absensi_backup_"


class BackupError(Exception):
    pass


# --------------------------------------------------------------------- nilai

def _encode(value):
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, datetime):
        return {"$dt": value.isoformat()}
    if isinstance(value, date):
        return {"$d": value.isoformat()}
    if isinstance(value, time):
        return {"$t": value.isoformat()}
    if isinstance(value, (bytes, bytearray, memoryview)):
        return {"$b": base64.b64encode(bytes(value)).decode("ascii")}
    if isinstance(value, Decimal):
        return {"$n": str(value)}
    raise BackupError(f"Tipe data tidak didukung: {type(value).__name__}")


def _decode(value):
    if not isinstance(value, dict):
        return value
    (tag, raw), = value.items()
    return {
        "$dt": datetime.fromisoformat, "$d": date.fromisoformat, "$t": time.fromisoformat,
        "$b": base64.b64decode, "$n": Decimal,
    }[tag](raw)


def _tables():
    """Semua tabel aplikasi dalam urutan aman untuk diisi (induk lebih dulu)."""
    return list(db.metadata.sorted_tables)


def schema_revision(conn=None):
    conn = conn or db.session.connection()
    if not inspect(conn).has_table("alembic_version"):
        return None
    return conn.execute(text("SELECT version_num FROM alembic_version")).scalar()


# --------------------------------------------------------------------- buat

def create_backup(dest_dir, keep=30):
    """Membuat file backup. Mengembalikan (path, manifest).

    Semua tabel dibaca dalam SATU transaksi agar isinya konsisten (InnoDB
    memakai satu snapshot untuk seluruh transaksi baca).
    """
    dest_dir = Path(dest_dir)
    dest_dir.mkdir(parents=True, exist_ok=True)
    stamp = office_now().strftime("%Y%m%d_%H%M%S")
    final = dest_dir / f"{PREFIX}{stamp}.zip"
    tmp = final.with_suffix(".zip.tmp")

    manifest = {
        "format": FORMAT, "format_version": FORMAT_VERSION,
        "created_at": office_now().isoformat(timespec="seconds"),
        "schema_revision": None, "tables": {}, "key_versions": [],
    }
    conn = db.session.connection()
    manifest["schema_revision"] = schema_revision(conn)
    try:
        with zipfile.ZipFile(tmp, "w", compression=zipfile.ZIP_DEFLATED) as zf:
            for table in _tables():
                digest, count = hashlib.sha256(), 0
                lines = []
                pk = list(table.primary_key.columns)
                for row in conn.execute(select(table).order_by(*pk)).mappings():
                    line = json.dumps({k: _encode(v) for k, v in row.items()},
                                      ensure_ascii=False, separators=(",", ":"))
                    lines.append(line)
                    count += 1
                data = ("\n".join(lines) + ("\n" if lines else "")).encode("utf-8")
                digest.update(data)
                zf.writestr(f"data/{table.name}.jsonl", data)
                manifest["tables"][table.name] = {"rows": count, "sha256": digest.hexdigest()}
            if "face_templates" in manifest["tables"]:
                versions = conn.execute(
                    text("SELECT DISTINCT encryption_key_version FROM face_templates")).scalars()
                manifest["key_versions"] = sorted(int(v) for v in versions)
            zf.writestr("manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
        os.replace(tmp, final)
    except Exception:
        tmp.unlink(missing_ok=True)
        raise
    finally:
        db.session.rollback()   # akhiri transaksi baca

    removed = prune(dest_dir, keep)
    return final, manifest, removed


def prune(dest_dir, keep):
    """Menghapus backup tertua sehingga tersisa `keep` file."""
    if keep is None or keep <= 0:
        return []
    files = sorted(Path(dest_dir).glob(f"{PREFIX}*.zip"))
    old = files[:-keep] if len(files) > keep else []
    for f in old:
        f.unlink()
    return [f.name for f in old]


# --------------------------------------------------------------------- baca & periksa

def read_backup(path):
    """Membuka dan memeriksa keutuhan backup. Mengembalikan (manifest, {tabel: [baris]})."""
    path = Path(path)
    if not path.exists():
        raise BackupError(f"File backup tidak ditemukan: {path}")
    try:
        with zipfile.ZipFile(path) as zf:
            bad = zf.testzip()
            if bad:
                raise BackupError(f"File backup rusak (bagian {bad}).")
            manifest = json.loads(zf.read("manifest.json"))
            if manifest.get("format") != FORMAT:
                raise BackupError("Ini bukan file backup sistem absensi wajah.")
            if manifest.get("format_version") != FORMAT_VERSION:
                raise BackupError("Versi format backup tidak didukung.")
            tables = {}
            for name, info in manifest["tables"].items():
                data = zf.read(f"data/{name}.jsonl")
                if hashlib.sha256(data).hexdigest() != info["sha256"]:
                    raise BackupError(f"Isi tabel {name} tidak cocok dengan sidik jarinya (file diubah/rusak).")
                rows = [{k: _decode(v) for k, v in json.loads(line).items()}
                        for line in data.decode("utf-8").splitlines() if line]
                if len(rows) != info["rows"]:
                    raise BackupError(f"Jumlah baris tabel {name} tidak cocok.")
                tables[name] = rows
    except (zipfile.BadZipFile, KeyError, ValueError) as exc:
        raise BackupError(f"File backup rusak atau tidak lengkap: {exc}") from exc
    return manifest, tables


def check_templates(tables, keyring):
    """Mencoba mendekripsi semua templat wajah dengan kunci yang diberikan.
    Mengembalikan (berhasil, gagal)."""
    from services.biometric_service import TemplateDecryptionError, decrypt_embedding

    ok = failed = 0
    for row in tables.get("face_templates", []):
        try:
            decrypt_embedding(row["encrypted_embedding"], row["employee_id"], row["model_name"],
                              row["encryption_key_version"], keyring=keyring)
            ok += 1
        except TemplateDecryptionError:
            failed += 1
    return ok, failed


# --------------------------------------------------------------------- pulihkan

def restore_into_current_db(manifest, tables, allow_revision_mismatch=False):
    """Mengisi database yang terhubung saat ini. Database HARUS kosong."""
    conn = db.session.connection()
    current_rev = schema_revision(conn)
    if (manifest["schema_revision"] and current_rev != manifest["schema_revision"]
            and not allow_revision_mismatch):
        raise BackupError(
            f"Revisi skema berbeda: backup {manifest['schema_revision']}, database {current_rev}. "
            "Samakan dulu dengan 'flask db upgrade' (atau downgrade) sebelum memulihkan."
        )
    known = {t.name for t in _tables()}
    unknown = set(tables) - known
    if unknown:
        raise BackupError(f"Backup berisi tabel yang tidak dikenal aplikasi: {', '.join(sorted(unknown))}.")

    not_empty = [t.name for t in _tables()
                 if conn.execute(select(func.count()).select_from(t)).scalar()]
    if not_empty:
        db.session.rollback()
        raise BackupError(
            "Database tujuan TIDAK kosong (berisi: " + ", ".join(not_empty) + "). "
            "Pemulihan hanya dilakukan ke database kosong agar data yang ada tidak tertimpa."
        )
    try:
        for table in _tables():
            rows = tables.get(table.name, [])
            if rows:
                conn.execute(table.insert(), rows)
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return {name: len(rows) for name, rows in tables.items()}


def test_restore(path, keyring_path=None):
    """Uji pemulihan ke database SQLite SEMENTARA. Database asli tidak disentuh."""
    from app import create_app
    from services import report_service
    from services.biometric_service import load_keyring

    manifest, tables = read_backup(path)
    keyring = load_keyring(keyring_path) if keyring_path else None
    tmp = Path(tempfile.mkdtemp(prefix="absensi-restore-test-"))
    try:
        app = create_app("testing", overrides={
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp / 'restore.db').as_posix()}",
            "SQLALCHEMY_ENGINE_OPTIONS": {},
            "BIOMETRIC_KEY_FILE": str(tmp / "tidak-dipakai.json"),
        })
        with app.app_context():
            db.create_all()
            restored = restore_into_current_db(manifest, tables, allow_revision_mismatch=True)
            counts = {t.name: db.session.execute(select(func.count()).select_from(t)).scalar()
                      for t in _tables()}
            mismatch = {n: (manifest["tables"][n]["rows"], counts.get(n))
                        for n in manifest["tables"] if counts.get(n) != manifest["tables"][n]["rows"]}
            today = office_now().date()
            start = today.replace(day=1)
            details = report_service.detail_rows(start, today, today=today)
            report_rows = len(report_service.summary_rows(details))
            db.session.remove()
            db.engine.dispose()
        templates_ok = templates_failed = None
        if keyring is not None:
            templates_ok, templates_failed = check_templates(tables, keyring)
        return {
            "manifest": manifest, "restored": restored, "mismatch": mismatch,
            "report_employees": report_rows,
            "templates_ok": templates_ok, "templates_failed": templates_failed,
        }
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


# --------------------------------------------------------------------- kunci

def copy_key(dest, backup_dir):
    """Menyalin file kunci biometrik ke lokasi TERPISAH dari backup database."""
    from services.biometric_service import key_file_path, load_keyring

    source = key_file_path()
    load_keyring()  # pastikan kunci sah sebelum disalin
    dest = Path(dest)
    if dest.is_dir() or not dest.suffix:
        dest = dest / f"kunci_biometrik_{office_now():%Y%m%d_%H%M%S}.json"
    backup_dir = Path(backup_dir).resolve()
    if dest.resolve().parent == backup_dir or backup_dir in dest.resolve().parents:
        raise BackupError(
            "Salinan kunci tidak boleh disimpan di folder backup database. Simpan di tempat "
            "terpisah (mis. flashdisk khusus atau brankas), agar backup yang bocor tidak bisa "
            "dipakai membuka data wajah."
        )
    project = Path(__file__).resolve().parents[1]
    if project == dest.resolve().parent or project in dest.resolve().parents:
        raise BackupError("Salinan kunci tidak boleh disimpan di dalam folder proyek (bisa ikut ke Git).")
    if dest.exists():
        raise BackupError(f"File tujuan sudah ada: {dest}")
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, dest)
    return dest
