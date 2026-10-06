"""Pengujian Tahap 2C: enkripsi templat wajah (FR-03, PRD 8.3, 9.2)."""
import json
import struct

import pytest

from conftest import make_embedding
from extensions import db
from models import AuditLog, Employee, FaceTemplate
from services import ValidationError
from services import audit_service as audit
from services import biometric_service as bio


def _logs(action):
    return db.session.execute(
        db.select(AuditLog).filter_by(action=action)
    ).scalars().all()


@pytest.fixture
def employee(app, schedule):
    e = Employee(employee_code="K001", full_name="Budi", department="TI",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    return e


@pytest.fixture
def employee2(app, schedule):
    e = Employee(employee_code="K002", full_name="Sari", department="TI",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    return e


def _close(a, b):
    return len(a) == len(b) and all(abs(x - y) < 1e-6 for x, y in zip(a, b))


# ---------------------------------------------------------------- kunci

def test_tanpa_konfigurasi_kunci_memberi_pesan_jelas(app):
    with pytest.raises(bio.BiometricKeyError, match="BIOMETRIC_KEY_FILE"):
        bio.load_keyring()


def test_file_kunci_belum_dibuat(app, tmp_path):
    app.config["BIOMETRIC_KEY_FILE"] = str(tmp_path / "tidak-ada.json")
    with pytest.raises(bio.BiometricKeyError, match="biometric-keygen"):
        bio.load_keyring()


def test_keygen_membuat_kunci_256_bit(biometric_keys):
    active, keys = bio.load_keyring()
    assert active == 1
    assert len(keys[1]) == 32


def test_keygen_menolak_menimpa_kunci(biometric_keys):
    before = open(biometric_keys).read()
    with pytest.raises(bio.BiometricKeyError, match="tidak akan ditimpa"):
        bio.generate_keyring()
    assert open(biometric_keys).read() == before


def test_file_kunci_rusak_ditolak(biometric_keys):
    with open(biometric_keys, "w") as fh:
        fh.write("{bukan json")
    with pytest.raises(bio.BiometricKeyError, match="rusak"):
        bio.load_keyring()


def test_status_kunci_tidak_membocorkan_isi_kunci(biometric_keys):
    status = bio.keyring_status()
    raw_key = json.load(open(biometric_keys))["keys"]["1"]
    assert status["ok"] is True
    assert raw_key not in json.dumps(status)


# ---------------------------------------------------------------- enkripsi

def test_enkripsi_dan_dekripsi_kembali_sama(biometric_keys):
    emb = make_embedding(1)
    blob, version = bio.encrypt_embedding(emb, 7, "arcface-r100")
    assert version == 1
    assert _close(bio.decrypt_embedding(blob, 7, "arcface-r100", 1), emb)


def test_ciphertext_tidak_memuat_embedding_terbuka(biometric_keys):
    emb = make_embedding(2)
    blob, _ = bio.encrypt_embedding(emb, 7, "m")
    plain = struct.pack(f"<{len(emb)}f", *emb)
    assert plain[:64] not in blob


def test_enkripsi_dua_kali_menghasilkan_data_berbeda(biometric_keys):
    emb = make_embedding(3)
    a, _ = bio.encrypt_embedding(emb, 7, "m")
    b, _ = bio.encrypt_embedding(emb, 7, "m")
    assert a != b  # nonce acak


@pytest.mark.parametrize(
    "employee_id,model_name,key_version",
    [(8, "m", 1), (7, "model-lain", 1), (7, "m", 2)],
)
def test_templat_terikat_karyawan_model_dan_kunci(biometric_keys, employee_id, model_name, key_version):
    blob, _ = bio.encrypt_embedding(make_embedding(4), 7, "m")
    with pytest.raises(bio.TemplateDecryptionError):
        bio.decrypt_embedding(blob, employee_id, model_name, key_version)


def test_data_yang_diubah_terdeteksi(biometric_keys):
    blob, _ = bio.encrypt_embedding(make_embedding(5), 7, "m")
    tampered = bytearray(blob)
    tampered[-1] ^= 0x01
    with pytest.raises(bio.TemplateDecryptionError):
        bio.decrypt_embedding(bytes(tampered), 7, "m", 1)


def test_kunci_lain_tidak_bisa_membaca(biometric_keys, tmp_path, app):
    blob, _ = bio.encrypt_embedding(make_embedding(6), 7, "m")
    app.config["BIOMETRIC_KEY_FILE"] = str(tmp_path / "kunci-lain.json")
    bio.generate_keyring()
    with pytest.raises(bio.TemplateDecryptionError):
        bio.decrypt_embedding(blob, 7, "m", 1)


@pytest.mark.parametrize(
    "embedding,message",
    [
        ([0.1] * 10, "Panjang embedding"),
        ([0.1] * 5000, "Panjang embedding"),
        ([0.0] * 128, "nol semua"),
        ([float("nan")] * 128, "tidak valid"),
        (["a"] * 128, "deretan angka"),
    ],
)
def test_embedding_tidak_valid_ditolak_tanpa_menyebut_nilai(biometric_keys, embedding, message):
    with pytest.raises(ValidationError) as exc:
        bio.encrypt_embedding(embedding, 7, "m")
    assert message in exc.value.errors["embedding"]


# ---------------------------------------------------------------- templat

def test_simpan_templat_terenkripsi_dan_tercatat(biometric_keys, employee, admin_user):
    emb = make_embedding(10)
    t = bio.save_template(employee, emb, "arcface-r100", admin_user.id)

    assert t.encryption_key_version == 1
    assert _close(bio.decrypt_template(t), emb)

    logs = _logs(audit.FACE_TEMPLATE_CREATED)
    assert len(logs) == 1
    assert "K001" in logs[0].description
    assert str(round(emb[0], 4)) not in logs[0].description


def test_templat_dipindah_ke_karyawan_lain_terdeteksi(biometric_keys, employee, employee2, admin_user):
    """Simulasi manipulasi database: templat Budi dipindah ke baris Sari."""
    t = bio.save_template(employee, make_embedding(11), "m", admin_user.id)
    db.session.execute(
        db.update(FaceTemplate).where(FaceTemplate.id == t.id).values(employee_id=employee2.id)
    )
    db.session.commit()
    db.session.refresh(t)

    with pytest.raises(bio.TemplateDecryptionError):
        bio.decrypt_template(t)

    loaded, failed = bio.load_active_templates("m")
    assert loaded == []
    assert failed == 1


def test_templat_hanya_untuk_karyawan_aktif(biometric_keys, employee, admin_user):
    employee.is_active = False
    db.session.commit()
    with pytest.raises(ValidationError) as exc:
        bio.save_template(employee, make_embedding(12), "m", admin_user.id)
    assert "karyawan aktif" in exc.value.errors["_"]
    assert bio.templates_for(employee) == []


def test_batas_jumlah_templat_per_karyawan(biometric_keys, employee, admin_user):
    for i in range(bio.MAX_TEMPLATES_PER_EMPLOYEE):
        bio.save_template(employee, make_embedding(100 + i), "m", admin_user.id)
    with pytest.raises(ValidationError) as exc:
        bio.save_template(employee, make_embedding(999), "m", admin_user.id)
    assert "Cabut templat lama" in exc.value.errors["_"]


def test_load_hanya_karyawan_aktif_dan_model_yang_sama(biometric_keys, employee, employee2, admin_user):
    bio.save_template(employee, make_embedding(20), "m", admin_user.id)
    bio.save_template(employee2, make_embedding(21), "m", admin_user.id)
    bio.save_template(employee2, make_embedding(22), "model-lain", admin_user.id)
    employee2.is_active = False
    db.session.commit()

    loaded, failed = bio.load_active_templates("m")
    assert [emp_id for emp_id, _ in loaded] == [employee.id]
    assert failed == 0


def test_load_lebih_dari_100_karyawan(biometric_keys, schedule, admin_user):
    """PRD: pencocokan harus mendukung lebih dari 100 karyawan."""
    emps = [
        Employee(employee_code=f"P{i:03d}", full_name=f"P{i}", department="X",
                 work_schedule_id=schedule.id)
        for i in range(120)
    ]
    db.session.add_all(emps)
    db.session.commit()
    for i, e in enumerate(emps):
        bio.save_template(e, make_embedding(i), "m", admin_user.id)

    loaded, failed = bio.load_active_templates("m")
    assert len(loaded) == 120
    assert failed == 0


def test_cabut_templat_menghapus_permanen_dan_tercatat(biometric_keys, employee, admin_user):
    bio.save_template(employee, make_embedding(30), "m", admin_user.id)
    bio.save_template(employee, make_embedding(31), "m", admin_user.id)

    count = bio.revoke_templates(employee, admin_user.id, "Permintaan karyawan")
    assert count == 2
    assert db.session.execute(db.select(db.func.count(FaceTemplate.id))).scalar_one() == 0
    assert db.session.get(Employee, employee.id) is not None

    logs = _logs(audit.FACE_TEMPLATE_REVOKED)
    assert "Permintaan karyawan" in logs[0].description


def test_cabut_templat_butuh_alasan(biometric_keys, employee, admin_user):
    bio.save_template(employee, make_embedding(32), "m", admin_user.id)
    with pytest.raises(ValidationError) as exc:
        bio.revoke_templates(employee, admin_user.id, "  ")
    assert "alasan" in exc.value.errors["reason"]
    assert len(bio.templates_for(employee)) == 1


# ---------------------------------------------------------------- rotasi

def test_rotasi_kunci_mengenkripsi_ulang_semua_templat(biometric_keys, employee, admin_user):
    emb = make_embedding(40)
    t = bio.save_template(employee, emb, "m", admin_user.id)
    old_blob = t.encrypted_embedding

    version, count = bio.rotate_key()
    db.session.refresh(t)

    assert (version, count) == (2, 1)
    assert t.encryption_key_version == 2
    assert t.encrypted_embedding != old_blob
    assert _close(bio.decrypt_template(t), emb)
    active, keys = bio.load_keyring()
    assert active == 2 and set(keys) == {1, 2}
    assert len(_logs(audit.BIOMETRIC_KEY_ROTATED)) == 1


# ---------------------------------------------------------------- halaman

def test_daftar_karyawan_menampilkan_status_wajah(auth_client, biometric_keys, employee, employee2, admin_user):
    bio.save_template(employee, make_embedding(50), "m", admin_user.id)
    body = auth_client.get("/admin/employees/").get_data(as_text=True)
    assert "Terdaftar (1)" in body
    assert "Belum" in body


def test_halaman_ubah_tidak_menampilkan_data_biometrik(auth_client, biometric_keys, employee, admin_user):
    t = bio.save_template(employee, make_embedding(51), "arcface-r100", admin_user.id)
    body = auth_client.get(f"/admin/employees/{employee.id}/edit").get_data(as_text=True)
    assert "arcface-r100" in body
    assert "Cabut semua templat wajah" in body
    assert t.encrypted_embedding.hex()[:32] not in body


def test_cabut_lewat_halaman(auth_client, biometric_keys, employee, admin_user):
    bio.save_template(employee, make_embedding(52), "m", admin_user.id)
    url = f"/admin/employees/{employee.id}/face-templates/revoke"

    res = auth_client.post(url, data={"reason": ""})
    assert res.status_code == 400
    assert len(bio.templates_for(employee)) == 1

    res = auth_client.post(url, data={"reason": "Karyawan berhenti bekerja"})
    assert res.status_code == 302
    assert bio.templates_for(employee) == []


def test_cabut_wajib_login(client, biometric_keys, employee, admin_user):
    bio.save_template(employee, make_embedding(53), "m", admin_user.id)
    res = client.post(f"/admin/employees/{employee.id}/face-templates/revoke",
                      data={"reason": "coba-coba tanpa login"})
    assert res.status_code == 302 and "/login" in res.headers["Location"]
    assert len(bio.templates_for(employee)) == 1


def test_dashboard_memperingatkan_jika_kunci_belum_ada(auth_client):
    body = auth_client.get("/admin").get_data(as_text=True)
    assert "Kunci enkripsi templat wajah belum siap" in body


def test_dashboard_tanpa_peringatan_jika_kunci_ada(auth_client, biometric_keys):
    body = auth_client.get("/admin").get_data(as_text=True)
    assert "Kunci enkripsi templat wajah belum siap" not in body
    assert "Wajah terdaftar" in body


# ---------------------------------------------------------------- CLI

def test_cli_keygen_dan_status(app, tmp_path):
    app.config["BIOMETRIC_KEY_FILE"] = str(tmp_path / "s" / "k.json")
    runner = app.test_cli_runner()

    res = runner.invoke(args=["biometric-keygen"])
    assert res.exit_code == 0, res.output
    assert "File kunci dibuat" in res.output
    assert len(_logs(audit.BIOMETRIC_KEY_CREATED)) == 1

    res = runner.invoke(args=["biometric-keygen"])
    assert res.exit_code != 0
    assert "tidak akan ditimpa" in res.output

    res = runner.invoke(args=["biometric-status"])
    assert res.exit_code == 0
    assert "Kunci aktif     : v1" in res.output


def test_cli_status_mendeteksi_templat_rusak(app, biometric_keys, employee, admin_user):
    t = bio.save_template(employee, make_embedding(60), "m", admin_user.id)
    t.encrypted_embedding = t.encrypted_embedding[:-1] + bytes([t.encrypted_embedding[-1] ^ 1])
    db.session.commit()
    res = app.test_cli_runner().invoke(args=["biometric-status"])
    assert res.exit_code != 0
    assert "Gagal diverifikasi: 1" in res.output


def test_cli_rotasi_butuh_konfirmasi(app, biometric_keys):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["biometric-rotate-key"], input="n\n")
    assert res.exit_code != 0
    assert bio.load_keyring()[0] == 1

    res = runner.invoke(args=["biometric-rotate-key", "--yes"])
    assert res.exit_code == 0, res.output
    assert bio.load_keyring()[0] == 2
