"""Pengujian Tahap 5E: daftar periksa keamanan, skrip, dan dokumentasi."""
import os
import re
import time
from pathlib import Path

import pytest

from extensions import db
from maintenance import security_check as sc

ROOT = Path(__file__).resolve().parents[1]
GOOD_KEY = "a" * 8 + "b7c1" * 14
LEAKED_KEY = "2f5d386388c395ee1b98ab0fa03f2f34745556712370f8403bce5871850573f0"


def statuses(items):
    return {i.title: i.status for i in items}


@pytest.mark.parametrize("app_env,key,expected_env,expected_key", [
    ("production", GOOD_KEY, sc.OK, sc.OK),
    ("development", GOOD_KEY, sc.NEED, sc.OK),
    ("production", LEAKED_KEY, sc.OK, sc.NEED),
    ("production", "pendek", sc.OK, sc.NEED),
    ("production", "ganti-dengan-string-acak-hasil-perintah-di-atas", sc.OK, sc.NEED),
])
def test_mode_dan_secret_key(app_env, key, expected_env, expected_key):
    s = statuses(sc.check_app({"SECRET_KEY": key}, {"APP_ENV": app_env}))
    assert s["Mode produksi (debug mati)"] == expected_env
    assert s["SECRET_KEY baru"] == expected_key


def test_rahasia_tidak_dicetak():
    items = sc.check_app({"SECRET_KEY": LEAKED_KEY}, {"APP_ENV": "production"})
    assert all(LEAKED_KEY not in (i.detail or "") for i in items)


@pytest.mark.parametrize("user,pwd,user_status,pwd_status", [
    ("absensi_app", "Xq7!rT9#mK2$vL8p", sc.OK, sc.OK),
    ("root", "Xq7!rT9#mK2$vL8p", sc.NEED, sc.OK),
    ("absensi_app", "PasswordKuatAnda123", sc.OK, sc.NEED),   # pernah terlihat di layar
    ("absensi_app", "pendek", sc.OK, sc.NEED),
])
def test_database(user, pwd, user_status, pwd_status):
    s = statuses(sc.check_database({}, {"DB_USER": user, "DB_PASSWORD": pwd}))
    assert s["Pengguna database khusus"] == user_status
    assert s["Password database baru"] == pwd_status


def test_kunci_di_dalam_proyek_ditolak(tmp_path):
    inside = statuses(sc.check_secrets_location({"BIOMETRIC_KEY_FILE": str(ROOT / "kunci.json")}))
    outside = statuses(sc.check_secrets_location({"BIOMETRIC_KEY_FILE": str(tmp_path / "k.json")}))
    assert inside["Lokasi kunci biometrik"] == sc.NEED
    assert outside["Lokasi kunci biometrik"] == sc.OK


def test_anti_foto_dan_ambang(app, biometric_keys, monkeypatch):
    from recognition import model_files as mf

    monkeypatch.setattr(mf, "verify", lambda d, m: Path(d) / m.filename)
    cfg = dict(app.config, LIVENESS_MODE="off", FACE_MATCH_THRESHOLD=0.363)
    s = statuses(sc.check_biometrics(cfg))
    assert s["Anti-foto aktif"] == sc.NEED
    assert s["Ambang kecocokan terkalibrasi"] == sc.NEED
    cfg.update(LIVENESS_MODE="passive+challenge", FACE_MATCH_THRESHOLD=0.60)
    s = statuses(sc.check_biometrics(cfg))
    assert s["Anti-foto aktif"] == sc.OK and s["Ambang kecocokan terkalibrasi"] == sc.OK
    assert s["Semua templat wajah terbaca"] == sc.OK


def test_backup(tmp_path):
    cfg = {"BACKUP_DIR": str(tmp_path)}
    assert statuses(sc.check_backup(cfg))["Backup terbaru"] == sc.NEED
    f = tmp_path / "absensi_backup_20261007_160000.zip"
    f.write_bytes(b"x")
    assert statuses(sc.check_backup(cfg))["Backup terbaru"] == sc.OK
    old = time.time() - 5 * 86400
    os.utime(f, (old, old))
    assert statuses(sc.check_backup(cfg))["Backup terbaru"] == sc.NEED


def test_jadwal_bernama_hari_ditandai(app, schedule, admin_user):
    schedule.name = "Selasa"
    db.session.commit()
    s = statuses(sc.check_data())
    assert s["Nama jadwal kerja jelas"] == sc.NEED
    schedule.name = "Jam Kerja Kantor"
    db.session.commit()
    assert statuses(sc.check_data())["Nama jadwal kerja jelas"] == sc.OK


def test_akun_admin(app, admin_user):
    item = next(i for i in sc.check_data() if i.title == "Akun admin")
    assert item.status in (sc.OK, sc.INFO)


def test_cli_security_check_gagal_jika_ada_yang_perlu(app, admin_user, monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    res = app.test_cli_runner().invoke(args=["security-check"])
    assert res.exit_code == 1
    assert "[PERLU] Mode produksi" in res.output
    assert "Periksa manual" in res.output and "UU No. 27 Tahun 2022" in res.output


# ---------------------------------------------------------------- skrip & dokumen

@pytest.mark.parametrize("name", ["jalankan_server.bat", "jalankan_kios.bat", "buka_kios.bat",
                                  "backup_harian.bat"])
def test_skrip_windows_ada_dan_berakhir_crlf(name):
    data = (ROOT / "scripts" / name).read_bytes()
    assert b"\r\n" in data and b"\n" not in data.replace(b"\r\n", b"")


def test_server_produksi_hanya_lokal():
    text = (ROOT / "scripts" / "jalankan_server.bat").read_text()
    assert "waitress-serve --listen=127.0.0.1:" in text and "flask run" not in text


def test_waitress_tercantum_di_requirements():
    assert re.search(r"^waitress==\d", (ROOT / "requirements.txt").read_text(), re.M)


def test_tautan_dokumen_di_readme_ada():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    links = set(re.findall(r"\]\(([^)#]+\.md)\)", readme))
    assert links and all((ROOT / link).exists() for link in links), links


def test_akses_database_ditolak_diberi_pesan_jelas(app, monkeypatch):
    from sqlalchemy.exc import OperationalError

    def denied():
        raise OperationalError("SELECT", {}, Exception(1045, "Access denied"))

    monkeypatch.setattr(sc, "check_data", denied)
    results = dict(sc.run_all(app, {"APP_ENV": "production"}))
    item = results["Data"][0]
    assert item.status == sc.NEED and item.title == "Database tidak bisa diperiksa"
    assert "Akses ke database ditolak" in item.detail and "Access denied" not in item.detail


def test_nilai_pengganti_yang_terlihat_juga_ditolak():
    s = statuses(sc.check_database({}, {"DB_USER": "absensi_app",
                                        "DB_PASSWORD": "52_kSzrLX2amNspwHrho_sen"}))
    assert s["Password database baru"] == sc.NEED
