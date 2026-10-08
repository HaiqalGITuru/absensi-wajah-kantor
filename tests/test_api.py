"""Pengujian API baca-saja."""
import json
from datetime import datetime, timedelta

import numpy as np
import pytest

from conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from extensions import db
from models import ApiToken, AuditLog, Employee, office_now
from routes import api as api_module
from services import ValidationError
from services import api_token_service as tokens
from services import attendance_service as att
from services import biometric_service as bio


@pytest.fixture
def token(app, admin_user):
    plain, _ = tokens.create_token("Andi - aplikasi rekap", admin_user.id, days=30)
    return plain


def auth(t):
    return {"Authorization": f"Bearer {t}"}


@pytest.fixture
def data(app, schedule, biometric_keys, admin_user):
    a = Employee(employee_code="2310010164", full_name="Haiqal Ramadhan", department="APTIKA",
                 work_schedule_id=schedule.id)
    b = Employee(employee_code="0001", full_name="Fuad", department="APTIKA",
                 work_schedule_id=schedule.id)
    c = Employee(employee_code="0099", full_name="Lama", department="Umum",
                 work_schedule_id=schedule.id, is_active=False)
    db.session.add_all([a, b, c])
    db.session.commit()
    vec = np.random.default_rng(0).normal(size=(3, 128)).astype(np.float32)
    vec /= np.linalg.norm(vec, axis=1, keepdims=True)
    bio.save_enrollment(a, list(vec), "sface-2021dec", admin_user.id)
    att.record(a.id, now=datetime(2026, 10, 5, 7, 50), score=0.91)
    att.record(b.id, now=datetime(2026, 10, 5, 7, 20), score=0.88)
    att.record(a.id, now=datetime(2026, 10, 6, 7, 25), score=0.9)
    return a, b, c


# ---------------------------------------------------------------- token

def test_token_hanya_disimpan_sebagai_hash(app, admin_user):
    plain, row = tokens.create_token("Uji token", admin_user.id)
    assert plain.startswith("aw_") and len(plain) > 40
    assert row.token_hash == tokens.hash_token(plain) and plain not in row.token_hash
    assert row.prefix == plain[:10]
    log = db.session.execute(db.select(AuditLog).filter_by(action="api_token_created")).scalar_one()
    assert plain not in log.description


@pytest.mark.parametrize("name,days", [("ab", 30), ("Nama sah", 0), ("Nama sah", 400)])
def test_validasi_pembuatan_token(app, admin_user, name, days):
    with pytest.raises(ValidationError):
        tokens.create_token(name, admin_user.id, days)


def test_tanpa_token_ditolak(client, data):
    res = client.get("/api/employees")
    assert res.status_code == 401 and res.get_json()["error"] == "token_diperlukan"


def test_token_salah_ditolak(client, data):
    res = client.get("/api/employees", headers=auth("aw_salah"))
    assert res.status_code == 401 and res.get_json()["error"] == "token_tidak_valid"


def test_token_dicabut_ditolak(client, data, token, admin_user):
    assert client.get("/api/employees", headers=auth(token)).status_code == 200
    row = db.session.execute(db.select(ApiToken)).scalar_one()
    tokens.revoke(row.id, "Magang selesai", admin_user.id)
    assert client.get("/api/employees", headers=auth(token)).status_code == 401


def test_token_kedaluwarsa_ditolak(client, data, token):
    row = db.session.execute(db.select(ApiToken)).scalar_one()
    row.expires_at = office_now() - timedelta(minutes=1)
    db.session.commit()
    assert client.get("/api/employees", headers=auth(token)).status_code == 401


def test_pemakaian_terakhir_dicatat(client, data, token):
    client.get("/api/employees", headers=auth(token))
    assert db.session.execute(db.select(ApiToken)).scalar_one().last_used_at is not None


def test_batas_permintaan_per_menit(app, client, data, token, monkeypatch):
    app.config["API_RATE_LIMIT"] = 3
    monkeypatch.setattr(api_module, "limiter", api_module._RateLimiter())
    codes = [client.get("/api/employees", headers=auth(token)).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    res = client.get("/api/employees", headers=auth(token))
    assert res.get_json()["error"] == "terlalu_banyak_permintaan" and int(res.headers["Retry-After"]) >= 1


# ---------------------------------------------------------------- endpoint

def test_health_tanpa_token(client):
    res = client.get("/api/health")
    body = res.get_json()
    assert res.status_code == 200 and body["status"] == "ok" and body["database"] == "ok"
    assert body["time"].endswith("+08:00")


def test_daftar_karyawan(client, data, token):
    body = client.get("/api/employees", headers=auth(token)).get_json()
    assert body["total"] == 2 and [e["employee_code"] for e in body["data"]] == ["0001", "2310010164"]
    haiqal = body["data"][1]
    assert haiqal["face_registered"] is True and body["data"][0]["face_registered"] is False
    assert haiqal["work_schedule"]["name"]
    body = client.get("/api/employees?status=all&department=Umum", headers=auth(token)).get_json()
    assert [e["employee_code"] for e in body["data"]] == ["0099"]
    body = client.get("/api/employees?q=haiq", headers=auth(token)).get_json()
    assert body["total"] == 1


def test_halaman_dan_batas_per_page(client, data, token):
    body = client.get("/api/employees?per_page=1&page=2", headers=auth(token)).get_json()
    assert body["page"] == 2 and body["pages"] == 2 and len(body["data"]) == 1
    res = client.get("/api/employees?per_page=1000", headers=auth(token))
    assert res.status_code == 400 and res.get_json()["error"] == "parameter_tidak_valid"


def test_daftar_absensi_dengan_filter(client, data, token):
    body = client.get("/api/attendance?date=2026-10-05", headers=auth(token)).get_json()
    assert body["total"] == 2 and body["start"] == body["end"] == "2026-10-05"
    item = next(d for d in body["data"] if d["employee"]["employee_code"] == "2310010164")
    assert item["check_in_at"] == "2026-10-05T07:50:00+08:00"
    assert item["status"] == "terlambat" and item["late_minutes"] == 5
    assert item["check_in_method"] == "kamera"

    body = client.get("/api/attendance?start=2026-10-05&end=2026-10-06&employee_code=2310010164",
                      headers=auth(token)).get_json()
    assert [d["work_date"] for d in body["data"]] == ["2026-10-06", "2026-10-05"]
    body = client.get("/api/attendance?start=2026-10-05&end=2026-10-06&status=hadir",
                      headers=auth(token)).get_json()
    assert body["total"] == 2


@pytest.mark.parametrize("query", [
    "date=05-10-2026", "start=2026-10-06&end=2026-10-01", "start=2026-09-01&end=2026-10-06",
    "date=2026-10-05&start=2026-10-01", "status=libur", "employee_id=abc",
])
def test_parameter_absensi_tidak_valid(client, data, token, query):
    res = client.get(f"/api/attendance?{query}", headers=auth(token))
    assert res.status_code == 400 and res.get_json()["error"] == "parameter_tidak_valid"


def test_detail_absensi(client, data, token):
    first = client.get("/api/attendance?date=2026-10-05", headers=auth(token)).get_json()["data"][0]
    res = client.get(f"/api/attendance/{first['id']}", headers=auth(token))
    assert res.status_code == 200 and res.get_json() == first
    res = client.get("/api/attendance/99999", headers=auth(token))
    assert res.status_code == 404 and res.get_json()["error"] == "tidak_ditemukan"


def test_data_wajah_dan_skor_tidak_pernah_keluar(client, data, token):
    """Templat wajah dan skor kecocokan tidak boleh muncul di API mana pun."""
    texts = []
    for url in ("/api/employees?status=all", "/api/attendance?start=2026-10-05&end=2026-10-06"):
        texts.append(client.get(url, headers=auth(token)).get_data(as_text=True))
    blob = " ".join(texts).lower()
    for forbidden in ("embedding", "encrypted", "template", "score", "0.91", "token_hash"):
        assert forbidden not in blob


def test_token_read_tidak_bisa_mengubah_dan_galat_json(client, data, token):
    res = client.post("/api/employees", headers=auth(token), json={})
    assert res.status_code == 403 and res.get_json()["error"] == "cakupan_tidak_cukup"
    res = client.delete("/api/employees/1", headers=auth(token))
    assert res.status_code == 405 and res.get_json()["error"] == "metode_tidak_diizinkan"
    res = client.get("/api/tidak-ada", headers=auth(token))
    assert res.status_code == 404 and res.get_json()["error"] == "tidak_ditemukan"
    assert client.get("/halaman-tidak-ada").status_code == 404   # halaman biasa tetap HTML


# ---------------------------------------------------------------- perintah

def test_cli_buat_daftar_cabut_token(app, admin_user):
    runner = app.test_cli_runner()
    res = runner.invoke(args=["api-token-create", "--name", "Andi - rekap", "--days", "30",
                              "--admin", ADMIN_USERNAME, "--password", ADMIN_PASSWORD])
    assert res.exit_code == 0, res.output
    plain = next(line.strip() for line in res.output.splitlines() if line.strip().startswith("aw_"))
    assert tokens.authenticate(plain) is not None

    res = runner.invoke(args=["api-token-list"])
    assert "Andi - rekap" in res.output and "aktif" in res.output and plain not in res.output

    row = db.session.execute(db.select(ApiToken)).scalar_one()
    res = runner.invoke(args=["api-token-revoke", str(row.id), "--reason", "Magang selesai",
                              "--admin", ADMIN_USERNAME, "--password", ADMIN_PASSWORD])
    assert res.exit_code == 0 and tokens.authenticate(plain) is None


def test_cli_token_butuh_password_admin(app, admin_user):
    res = app.test_cli_runner().invoke(args=["api-token-create", "--name", "Andi", "--admin", ADMIN_USERNAME,
                                             "--password", "salah"])
    assert res.exit_code != 0
    assert db.session.execute(db.select(ApiToken)).first() is None


def test_security_check_melaporkan_token_aktif(app, admin_user, token):
    from maintenance import security_check as sc

    item = sc.check_api_tokens()[0]
    assert item.status == sc.INFO and "Andi - aplikasi rekap" in item.detail
    assert token not in json.dumps(item.__dict__)


def test_cari_nama_di_absensi(client, data, token):
    body = client.get("/api/attendance?start=2026-10-05&end=2026-10-06&q=fua", headers=auth(token)).get_json()
    assert {d["employee"]["employee_code"] for d in body["data"]} == {"0001"}
