"""
Layanan perlindungan templat wajah (FR-03, PRD 8.3 dan 9.2).

Ringkasan cara kerja:
- Templat (embedding) dienkripsi dengan AES-256-GCM sebelum disimpan.
- Kunci disimpan di file terpisah di LUAR folder proyek dan di luar database.
  File .env hanya berisi lokasi file kunci (BIOMETRIC_KEY_FILE).
- Setiap templat diikat ke employee_id, nama model, dan versi kunci melalui
  "associated data". Templat yang dipindahkan ke baris karyawan lain akan
  gagal didekripsi, sehingga manipulasi langsung di database terdeteksi.
- Kunci dapat dirotasi: kunci lama tetap disimpan untuk membaca data lama,
  lalu semua templat dienkripsi ulang dengan kunci baru.

Aturan: nilai embedding, isi kunci, dan isi file kunci TIDAK BOLEH ditulis ke
log, audit log, pesan error, atau halaman web.
"""
import base64
import json
import math
import os
import secrets
import struct
from pathlib import Path

from cryptography.exceptions import InvalidTag
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from flask import current_app
from sqlalchemy import func

from extensions import db
from models import Employee, FaceTemplate
from services import ValidationError
from services import audit_service as audit

FORMAT_VERSION = 1
NONCE_SIZE = 12
KEY_SIZE = 32  # 256 bit
MIN_EMBEDDING_DIM = 64
MAX_EMBEDDING_DIM = 4096
MAX_TEMPLATES_PER_EMPLOYEE = 10


class BiometricKeyError(Exception):
    """File kunci tidak diatur, tidak ditemukan, atau rusak."""


class TemplateDecryptionError(Exception):
    """Templat tidak bisa didekripsi: kunci salah, data diubah, atau dipindah."""


# ---------------------------------------------------------------------------
# Pengelolaan kunci
# ---------------------------------------------------------------------------

def key_file_path():
    path = current_app.config.get("BIOMETRIC_KEY_FILE")
    if not path:
        raise BiometricKeyError(
            "BIOMETRIC_KEY_FILE belum diatur di file .env."
        )
    return Path(path)


def load_keyring():
    """Membaca file kunci. Mengembalikan (versi_aktif, {versi: kunci_bytes})."""
    path = key_file_path()
    if not path.exists():
        raise BiometricKeyError(
            "File kunci biometrik belum dibuat. "
            "Jalankan: flask --app app biometric-keygen"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        active = int(data["active_version"])
        keys = {
            int(version): base64.b64decode(value, validate=True)
            for version, value in data["keys"].items()
        }
    except (OSError, ValueError, KeyError, TypeError) as exc:
        raise BiometricKeyError("File kunci biometrik rusak atau formatnya salah.") from exc

    if active not in keys or any(len(k) != KEY_SIZE for k in keys.values()):
        raise BiometricKeyError("File kunci biometrik rusak atau formatnya salah.")
    return active, keys


def _write_keyring(path, active, keys):
    """Menulis file kunci secara atomik (tulis ke file sementara lalu ganti)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "format": "absensi-wajah-keyring",
        "active_version": active,
        "keys": {str(v): base64.b64encode(k).decode("ascii") for v, k in sorted(keys.items())},
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, indent=2)
    os.replace(tmp, path)


def keyring_status():
    """Status kunci untuk dashboard. Tidak pernah mengembalikan isi kunci."""
    try:
        active, keys = load_keyring()
    except BiometricKeyError as exc:
        return {"ok": False, "message": str(exc)}
    return {"ok": True, "active_version": active, "key_count": len(keys)}


def generate_keyring():
    """Membuat file kunci pertama. Menolak jika file sudah ada."""
    path = key_file_path()
    if path.exists():
        raise BiometricKeyError(
            "File kunci sudah ada dan tidak akan ditimpa. "
            "Menimpa kunci membuat semua templat wajah tidak bisa dibaca lagi."
        )
    _write_keyring(path, 1, {1: secrets.token_bytes(KEY_SIZE)})
    return path


# ---------------------------------------------------------------------------
# Enkripsi dan dekripsi
# ---------------------------------------------------------------------------

def validate_embedding(embedding):
    """Memeriksa embedding tanpa menyebut nilainya di pesan error."""
    try:
        values = [float(v) for v in embedding]
    except (TypeError, ValueError):
        raise ValidationError({"embedding": "Embedding harus berupa deretan angka."})

    if not MIN_EMBEDDING_DIM <= len(values) <= MAX_EMBEDDING_DIM:
        raise ValidationError({
            "embedding": (
                f"Panjang embedding harus {MIN_EMBEDDING_DIM}-{MAX_EMBEDDING_DIM} angka."
            )
        })
    if not all(math.isfinite(v) for v in values):
        raise ValidationError({"embedding": "Embedding berisi nilai yang tidak valid."})
    if math.fsum(v * v for v in values) == 0.0:
        raise ValidationError({"embedding": "Embedding tidak boleh bernilai nol semua."})
    return values


def _associated_data(employee_id, model_name, key_version):
    return (
        f"absensi-wajah|f{FORMAT_VERSION}|employee:{employee_id}"
        f"|model:{model_name}|key:{key_version}"
    ).encode("utf-8")


def _pack(values):
    return struct.pack(f"<I{len(values)}f", len(values), *values)


def _unpack(raw):
    (count,) = struct.unpack_from("<I", raw, 0)
    if len(raw) != 4 + 4 * count:
        raise TemplateDecryptionError("Isi templat tidak utuh.")
    return list(struct.unpack_from(f"<{count}f", raw, 4))


def encrypt_embedding(embedding, employee_id, model_name, keyring=None):
    """Mengembalikan (blob_terenkripsi, versi_kunci)."""
    values = validate_embedding(embedding)
    active, keys = keyring or load_keyring()
    nonce = secrets.token_bytes(NONCE_SIZE)
    ciphertext = AESGCM(keys[active]).encrypt(
        nonce, _pack(values), _associated_data(employee_id, model_name, active)
    )
    return bytes([FORMAT_VERSION]) + nonce + ciphertext, active


def decrypt_embedding(blob, employee_id, model_name, key_version, keyring=None):
    _, keys = keyring or load_keyring()
    if not blob or blob[0] != FORMAT_VERSION or len(blob) <= 1 + NONCE_SIZE:
        raise TemplateDecryptionError("Format templat tidak dikenali.")
    key = keys.get(key_version)
    if key is None:
        raise TemplateDecryptionError("Versi kunci templat tidak tersedia.")

    nonce = blob[1:1 + NONCE_SIZE]
    try:
        raw = AESGCM(key).decrypt(
            nonce,
            blob[1 + NONCE_SIZE:],
            _associated_data(employee_id, model_name, key_version),
        )
    except InvalidTag as exc:
        raise TemplateDecryptionError(
            "Templat gagal diverifikasi (data diubah, dipindah, atau kunci salah)."
        ) from exc
    return _unpack(raw)


def decrypt_template(template, keyring=None):
    return decrypt_embedding(
        template.encrypted_embedding,
        template.employee_id,
        template.model_name,
        template.encryption_key_version,
        keyring=keyring,
    )


# ---------------------------------------------------------------------------
# Operasi templat
# ---------------------------------------------------------------------------

def template_counts(employee_ids=None):
    """{employee_id: jumlah_templat}. Untuk menampilkan status di daftar karyawan."""
    stmt = db.select(FaceTemplate.employee_id, func.count(FaceTemplate.id)).group_by(
        FaceTemplate.employee_id
    )
    if employee_ids is not None:
        if not employee_ids:
            return {}
        stmt = stmt.where(FaceTemplate.employee_id.in_(employee_ids))
    return dict(db.session.execute(stmt).all())


def templates_for(employee):
    """Daftar templat seorang karyawan (hanya metadata yang ditampilkan)."""
    return db.session.execute(
        db.select(FaceTemplate)
        .filter_by(employee_id=employee.id)
        .order_by(FaceTemplate.created_at)
    ).scalars().all()


def count_employees_with_templates():
    return db.session.execute(
        db.select(func.count(func.distinct(FaceTemplate.employee_id)))
        .join(Employee, Employee.id == FaceTemplate.employee_id)
        .where(Employee.is_active.is_(True))
    ).scalar_one()


def save_template(employee, embedding, model_name, actor_id):
    """Menyimpan satu templat wajah terenkripsi untuk karyawan aktif.

    Dipanggil oleh proses pendaftaran wajah di Tahap 3.
    """
    model_name = (model_name or "").strip()
    if not model_name or len(model_name) > 100:
        raise ValidationError({"model_name": "Nama model wajib diisi (maks. 100 karakter)."})
    if not employee.is_active:
        raise ValidationError({"_": "Templat hanya dapat didaftarkan untuk karyawan aktif."})
    if template_counts([employee.id]).get(employee.id, 0) >= MAX_TEMPLATES_PER_EMPLOYEE:
        raise ValidationError({
            "_": (
                f"Karyawan sudah memiliki {MAX_TEMPLATES_PER_EMPLOYEE} templat. "
                "Cabut templat lama terlebih dahulu."
            )
        })

    blob, key_version = encrypt_embedding(embedding, employee.id, model_name)
    template = FaceTemplate(
        employee_id=employee.id,
        encrypted_embedding=blob,
        encryption_key_version=key_version,
        model_name=model_name,
    )
    db.session.add(template)
    db.session.flush()
    audit.log_event(
        audit.FACE_TEMPLATE_CREATED,
        user_id=actor_id,
        entity_type="employee",
        entity_id=employee.id,
        description=(
            f"Templat wajah ditambahkan untuk {employee.employee_code} "
            f"(model {model_name}, kunci v{key_version})"
        ),
        commit=False,
    )
    db.session.commit()
    return template


def save_enrollment(employee, embeddings, model_name, actor_id, replace_reason=None):
    """Menyimpan beberapa sampel wajah sekaligus dalam SATU transaksi (FR-03).

    Jika replace_reason diisi, templat lama dicabut di transaksi yang sama,
    sehingga tidak pernah ada keadaan setengah jadi (lama sudah hilang tetapi
    yang baru gagal tersimpan).
    """
    model_name = (model_name or "").strip()
    if not model_name or len(model_name) > 100:
        raise ValidationError({"model_name": "Nama model wajib diisi (maks. 100 karakter)."})
    if not employee.is_active:
        raise ValidationError({"_": "Templat hanya dapat didaftarkan untuk karyawan aktif."})
    if not embeddings:
        raise ValidationError({"_": "Tidak ada sampel wajah untuk disimpan."})

    existing = templates_for(employee)
    if existing and not replace_reason:
        raise ValidationError({
            "_": (
                f"Karyawan {employee.employee_code} sudah memiliki {len(existing)} templat. "
                "Gunakan opsi ganti (--replace) dengan alasan untuk mendaftarkan ulang."
            )
        })
    if len(embeddings) > MAX_TEMPLATES_PER_EMPLOYEE:
        raise ValidationError({
            "_": f"Maksimal {MAX_TEMPLATES_PER_EMPLOYEE} sampel per karyawan."
        })

    keyring = load_keyring()
    try:
        if existing:
            for template in existing:
                db.session.delete(template)
            audit.log_event(
                audit.FACE_TEMPLATE_REVOKED,
                user_id=actor_id,
                entity_type="employee",
                entity_id=employee.id,
                description=(
                    f"{len(existing)} templat wajah {employee.employee_code} diganti "
                    f"melalui pendaftaran ulang. Alasan: {replace_reason}"
                ),
                commit=False,
            )
        saved = []
        for embedding in embeddings:
            blob, key_version = encrypt_embedding(embedding, employee.id, model_name, keyring)
            template = FaceTemplate(
                employee_id=employee.id,
                encrypted_embedding=blob,
                encryption_key_version=key_version,
                model_name=model_name,
            )
            db.session.add(template)
            saved.append(template)
        audit.log_event(
            audit.FACE_TEMPLATE_CREATED,
            user_id=actor_id,
            entity_type="employee",
            entity_id=employee.id,
            description=(
                f"{len(saved)} templat wajah didaftarkan untuk {employee.employee_code} "
                f"(model {model_name}, kunci v{keyring[0]})"
            ),
            commit=False,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return saved


def revoke_templates(employee, actor_id, reason):
    """Menghapus permanen SEMUA templat wajah seorang karyawan (FR-03, PRD 9.2).

    Data kehadiran tidak terpengaruh. Karyawan perlu didaftarkan ulang agar
    bisa dikenali kamera lagi.
    """
    reason = " ".join((reason or "").split())
    if len(reason) < 5:
        raise ValidationError({"reason": "Tuliskan alasan pencabutan (minimal 5 karakter)."})
    if len(reason) > 300:
        raise ValidationError({"reason": "Alasan maksimal 300 karakter."})

    templates = templates_for(employee)
    if not templates:
        raise ValidationError({"_": "Karyawan ini belum memiliki templat wajah."})

    for template in templates:
        db.session.delete(template)
    audit.log_event(
        audit.FACE_TEMPLATE_REVOKED,
        user_id=actor_id,
        entity_type="employee",
        entity_id=employee.id,
        description=(
            f"{len(templates)} templat wajah {employee.employee_code} dicabut. "
            f"Alasan: {reason}"
        ),
        commit=False,
    )
    db.session.commit()
    return len(templates)


def load_active_templates(model_name):
    """Templat terdekripsi milik karyawan AKTIF untuk proses pencocokan (Tahap 3).

    Mengembalikan (daftar (employee_id, embedding), jumlah_gagal). Templat yang
    gagal diverifikasi dilewati dan hanya dicatat jumlahnya, tanpa isi datanya.
    """
    return _load_templates(model_name, include_inactive=False)


def load_all_templates(model_name):
    """Templat SEMUA karyawan, termasuk nonaktif.

    Dipakai HANYA untuk mencegah pendaftaran ganda: wajah yang sudah tersimpan
    atas nama karyawan nonaktif tidak boleh didaftarkan ulang dengan kode baru.
    Tidak pernah dipakai untuk mencatat absensi.
    """
    return _load_templates(model_name, include_inactive=True)


def _load_templates(model_name, include_inactive):
    keyring = load_keyring()
    stmt = (
        db.select(FaceTemplate)
        .join(Employee, Employee.id == FaceTemplate.employee_id)
        .where(FaceTemplate.model_name == model_name)
        .order_by(FaceTemplate.employee_id, FaceTemplate.id)
    )
    if not include_inactive:
        stmt = stmt.where(Employee.is_active.is_(True))
    rows = db.session.execute(stmt).scalars().all()

    result, failed = [], 0
    for template in rows:
        try:
            result.append((template.employee_id, decrypt_template(template, keyring)))
        except TemplateDecryptionError:
            failed += 1
            current_app.logger.error(
                "Templat id=%s gagal diverifikasi dan dilewati.", template.id
            )
    return result, failed


def rotate_key():
    """Membuat kunci baru lalu mengenkripsi ulang semua templat.

    Urutan aman: kunci baru ditambahkan ke file lebih dulu (kunci lama tetap
    ada), baru templat dienkripsi ulang dalam satu transaksi. Jika transaksi
    gagal, templat tetap bisa dibaca dengan kunci lama.
    """
    path = key_file_path()
    active, keys = load_keyring()
    new_version = max(keys) + 1
    keys[new_version] = secrets.token_bytes(KEY_SIZE)
    _write_keyring(path, new_version, keys)

    keyring = (new_version, keys)
    templates = db.session.execute(db.select(FaceTemplate)).scalars().all()
    try:
        for template in templates:
            values = decrypt_template(template, keyring)
            blob, version = encrypt_embedding(
                values, template.employee_id, template.model_name, keyring
            )
            template.encrypted_embedding = blob
            template.encryption_key_version = version
        audit.log_event(
            audit.BIOMETRIC_KEY_ROTATED,
            entity_type="biometric_key",
            entity_id=new_version,
            description=(
                f"Kunci biometrik dirotasi ke v{new_version}; "
                f"{len(templates)} templat dienkripsi ulang"
            ),
            commit=False,
        )
        db.session.commit()
    except Exception:
        db.session.rollback()
        raise
    return new_version, len(templates)
