"""Pengujian Tahap 5D: backup, pemeriksaan, uji pemulihan, pemulihan, salin kunci."""
import json
import zipfile
from datetime import datetime
from pathlib import Path

import numpy as np
import pytest
from sqlalchemy.exc import OperationalError

from extensions import db
from models import AttendanceSession, AuditLog, Employee, FaceTemplate
from services import attendance_service as att
from services import backup_service as bk
from services import biometric_service as bio
from services.cli_errors import DB_ACCESS_MESSAGE, DB_DOWN_MESSAGE, DB_MISSING_MESSAGE, explain


@pytest.fixture
def filled(app, schedule, biometric_keys, admin_user):
    emp = Employee(employee_code="2310010164", full_name="Haiqal Ramadhan", department="APTIKA",
                   work_schedule_id=schedule.id)
    db.session.add(emp)
    db.session.commit()
    rng = np.random.default_rng(1)
    vecs = rng.normal(size=(3, 128)).astype(np.float32)
    vecs /= np.linalg.norm(vecs, axis=1, keepdims=True)
    bio.save_enrollment(emp, list(vecs), "sface-2021dec", admin_user.id)
    att.record(emp.id, now=datetime(2026, 10, 5, 7, 40))
    return emp


def _backup(app, tmp_path, **kw):
    with app.app_context():
        return bk.create_backup(tmp_path / "backups", **kw)


def test_backup_lengkap_dan_konsisten(app, filled, tmp_path):
    path, manifest, removed = _backup(app, tmp_path)
    assert path.name.startswith("absensi_backup_") and path.exists()
    assert manifest["tables"]["employees"]["rows"] == 1
    assert manifest["tables"]["face_templates"]["rows"] == 3
    assert manifest["tables"]["attendance_sessions"]["rows"] == 1
    assert manifest["key_versions"] == [1]
    m, tables = bk.read_backup(path)
    assert tables["employees"][0]["employee_code"] == "2310010164"
    assert isinstance(tables["face_templates"][0]["encrypted_embedding"], bytes)
    assert isinstance(tables["attendance_sessions"][0]["check_in_at"], datetime)


def test_kunci_biometrik_tidak_ikut_backup(app, filled, tmp_path, biometric_keys):
    path, _, _ = _backup(app, tmp_path)
    key_text = Path(app.config["BIOMETRIC_KEY_FILE"]).read_text()
    secret = json.loads(key_text)["keys"]["1"]
    with zipfile.ZipFile(path) as zf:
        everything = b"".join(zf.read(n) for n in zf.namelist())
    assert secret.encode() not in everything


def test_backup_yang_diubah_terdeteksi(app, filled, tmp_path):
    path, _, _ = _backup(app, tmp_path)
    tampered = tmp_path / "ubah.zip"
    with zipfile.ZipFile(path) as zi, zipfile.ZipFile(tampered, "w") as zo:
        for item in zi.infolist():
            data = zi.read(item.filename)
            if item.filename == "data/attendance_sessions.jsonl":
                data = data.replace(b"hadir", b"HADIR")
            zo.writestr(item, data)
    with pytest.raises(bk.BackupError, match="sidik jari"):
        bk.read_backup(tampered)


def test_file_bukan_backup_ditolak(tmp_path):
    bad = tmp_path / "x.zip"
    bad.write_bytes(b"bukan zip")
    with pytest.raises(bk.BackupError):
        bk.read_backup(bad)


def test_kunci_salah_terdeteksi(app, filled, tmp_path):
    path, _, _ = _backup(app, tmp_path)
    _, tables = bk.read_backup(path)
    wrong = (1, {1: b"\x00" * 32})
    ok, failed = bk.check_templates(tables, wrong)
    assert (ok, failed) == (0, 3)
    with app.app_context():
        assert bk.check_templates(tables, bio.load_keyring()) == (3, 0)


def test_uji_pemulihan_ke_database_sementara(app, filled, tmp_path):
    path, _, _ = _backup(app, tmp_path)
    with app.app_context():
        before = db.session.query(Employee).count()
        r = bk.test_restore(path, app.config["BIOMETRIC_KEY_FILE"])
        assert db.session.query(Employee).count() == before   # database asli tidak berubah
    assert r["mismatch"] == {} and r["templates_ok"] == 3 and r["templates_failed"] == 0
    assert r["restored"]["face_templates"] == 3


def test_pemulihan_hanya_ke_database_kosong(app, filled, tmp_path):
    path, manifest, _ = _backup(app, tmp_path)
    _, tables = bk.read_backup(path)
    with app.app_context():
        with pytest.raises(bk.BackupError, match="TIDAK kosong"):
            bk.restore_into_current_db(manifest, tables)


def test_pemulihan_ke_database_kosong_lalu_data_baru_tidak_bentrok(app, filled, tmp_path):
    from app import create_app

    path, manifest, _ = _backup(app, tmp_path)
    _, tables = bk.read_backup(path)
    fresh = create_app("testing", overrides={
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp_path / 'baru.db').as_posix()}",
        "BIOMETRIC_KEY_FILE": app.config["BIOMETRIC_KEY_FILE"],
    })
    with fresh.app_context():
        db.create_all()
        bk.restore_into_current_db(manifest, tables, allow_revision_mismatch=True)
        assert db.session.query(FaceTemplate).count() == 3
        assert db.session.query(AttendanceSession).count() == 1
        templates, failed = bio.load_active_templates("sface-2021dec")
        assert len(templates) == 3 and failed == 0
        e = Employee(employee_code="BARU", full_name="Baru", department="X",
                     work_schedule_id=tables["work_schedules"][0]["id"])
        db.session.add(e)
        db.session.commit()
        assert e.id == max(r["id"] for r in tables["employees"]) + 1
        db.session.remove()
        db.engine.dispose()


def test_revisi_skema_berbeda_ditolak(app, filled, tmp_path):
    path, manifest, _ = _backup(app, tmp_path)
    _, tables = bk.read_backup(path)
    manifest["schema_revision"] = "revisi-lain"
    with app.app_context():
        with pytest.raises(bk.BackupError, match="Revisi skema berbeda"):
            bk.restore_into_current_db(manifest, tables)


def test_backup_lama_dihapus_sesuai_batas(tmp_path):
    folder = tmp_path / "b"
    folder.mkdir()
    for i in range(5):
        (folder / f"absensi_backup_2026010{i}_000000.zip").write_bytes(b"x")
    removed = bk.prune(folder, keep=3)
    assert removed == ["absensi_backup_20260100_000000.zip", "absensi_backup_20260101_000000.zip"]
    assert len(list(folder.glob("*.zip"))) == 3


def test_salin_kunci_ke_tempat_terpisah(app, biometric_keys, tmp_path):
    with app.app_context():
        backups = tmp_path / "backups"
        with pytest.raises(bk.BackupError, match="folder backup"):
            bk.copy_key(backups, backups)
        with pytest.raises(bk.BackupError, match="folder proyek"):
            bk.copy_key(Path(__file__).resolve().parents[1] / "kunci", backups)
        dest = bk.copy_key(tmp_path / "flashdisk", backups)
        assert dest.exists() and bio.load_keyring(dest)[1] == bio.load_keyring()[1]
        with pytest.raises(bk.BackupError, match="sudah ada"):
            bk.copy_key(dest, backups)


# ---------------------------------------------------------------- perintah

def test_cli_backup_create_tercatat_di_audit(app, filled, tmp_path):
    app.config["BACKUP_DIR"] = str(tmp_path / "bk")
    res = app.test_cli_runner().invoke(args=["backup-create"])
    assert res.exit_code == 0, res.output
    assert "Backup tersimpan" in res.output and "backup-key" in res.output
    log = db.session.execute(db.select(AuditLog).filter_by(action="backup_created")).scalar_one()
    assert "absensi_backup_" in log.description


def test_cli_verify_dan_test(app, filled, tmp_path):
    path, _, _ = _backup(app, tmp_path)
    runner = app.test_cli_runner()
    key = app.config["BIOMETRIC_KEY_FILE"]
    res = runner.invoke(args=["backup-verify", str(path), "--key", key])
    assert res.exit_code == 0 and "Semua 3 templat wajah bisa didekripsi" in res.output
    res = runner.invoke(args=["backup-test", str(path), "--key", key])
    assert res.exit_code == 0 and "Uji pemulihan BERHASIL" in res.output


def test_cli_restore_butuh_konfirmasi(app, filled, tmp_path):
    path, _, _ = _backup(app, tmp_path)
    res = app.test_cli_runner().invoke(
        args=["backup-restore", str(path), "--key", app.config["BIOMETRIC_KEY_FILE"]], input="ya\n")
    assert res.exit_code != 0 and "Dibatalkan" in res.output


def test_cli_restore_ke_database_berisi_ditolak(app, filled, tmp_path):
    path, _, _ = _backup(app, tmp_path)
    res = app.test_cli_runner().invoke(
        args=["backup-restore", str(path), "--key", app.config["BIOMETRIC_KEY_FILE"]],
        input="PULIHKAN\n")
    assert res.exit_code != 0 and "TIDAK kosong" in res.output


@pytest.mark.parametrize("code,message", [
    (2003, DB_DOWN_MESSAGE), (1045, DB_ACCESS_MESSAGE), (1044, DB_ACCESS_MESSAGE),
    (1049, DB_MISSING_MESSAGE),
])
def test_pesan_galat_database_dibedakan(code, message):
    exc = OperationalError("SELECT 1", {}, Exception(code, "pesan"))
    assert explain(exc) == message
