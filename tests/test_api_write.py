"""Pengujian API tulis (token write) untuk panel admin lain, mis. Laravel."""
from datetime import datetime

import pytest

from extensions import db
from models import ApiToken, AuditLog, Employee, WorkSchedule
from services import ValidationError
from services import api_token_service as tokens
from services import attendance_service as att

DAYS = [{"weekday": w, "start_time": "07:30", "end_time": "16:00"} for w in range(4)] + [
    {"weekday": 4, "start_time": "07:30", "end_time": "11:30"}]


@pytest.fixture
def writer(app, admin_user):
    return tokens.create_token("Laravel - dashboard admin", admin_user.id, days=30, scope="write")[0]


@pytest.fixture
def reader(app, admin_user):
    return tokens.create_token("Laravel - baca", admin_user.id, days=30)[0]


def h(token, actor=None):
    headers = {"Authorization": f"Bearer {token}"}
    if actor:
        headers["X-Actor"] = actor
    return headers


def last_log(action):
    return db.session.execute(
        db.select(AuditLog).filter_by(action=action).order_by(AuditLog.id.desc())).scalars().first()


# ---------------------------------------------------------------- token

def test_cakupan_token(app, admin_user):
    with pytest.raises(ValidationError):
        tokens.create_token("Laravel", admin_user.id, 30, scope="admin")
    with pytest.raises(ValidationError):
        tokens.create_token("Laravel", admin_user.id, 120, scope="write")   # maks 90 hari
    _, row = tokens.create_token("Laravel", admin_user.id, 30, scope="write")
    assert row.scope == "write" and row.allows("write") and row.allows("read")
    _, row = tokens.create_token("Laravel baca", admin_user.id, 30)
    assert row.scope == "read" and not row.allows("write")


def test_token_read_ditolak_untuk_semua_perubahan(client, schedule, reader):
    for method, url in [("post", "/api/employees"), ("patch", "/api/employees/1"),
                        ("post", "/api/employees/1/deactivate"), ("post", "/api/work-schedules"),
                        ("patch", "/api/work-schedules/1"), ("get", "/api/audit-logs")]:
        res = getattr(client, method)(url, headers=h(reader), json={})
        assert res.status_code == 403, (method, url)


# ---------------------------------------------------------------- karyawan

def emp_body(schedule, **kw):
    body = {"employee_code": "199001012020011001", "full_name": "Siti Aminah",
            "department": "Sekretariat", "work_schedule_id": schedule.id}
    body.update(kw)
    return body


def test_tambah_karyawan_lewat_api_tercatat_dengan_asal(client, schedule, writer):
    res = client.post("/api/employees", headers=h(writer, "andi"), json=emp_body(schedule))
    assert res.status_code == 201, res.get_json()
    data = res.get_json()
    assert data["employee_code"] == "199001012020011001" and data["face_registered"] is False
    log = last_log("employee_created")
    assert log.user_id is None
    assert "via API 'Laravel - dashboard admin'" in log.description and "pelaku: andi" in log.description


def test_validasi_sama_dengan_admin_flask(client, schedule, writer):
    client.post("/api/employees", headers=h(writer), json=emp_body(schedule))
    res = client.post("/api/employees", headers=h(writer), json=emp_body(schedule))   # kode sama
    body = res.get_json()
    assert res.status_code == 422 and body["error"] == "validasi_gagal"
    assert "employee_code" in body["fields"]

    res = client.post("/api/employees", headers=h(writer),
                      json=emp_body(schedule, employee_code="X2", full_name="  siti   AMINAH "))
    body = res.get_json()
    assert res.status_code == 422 and body["needs_confirmation"] is True   # nama ganda
    res = client.post("/api/employees", headers=h(writer),
                      json=emp_body(schedule, employee_code="X2", full_name="Siti Aminah",
                                    confirm_duplicate_name=True))
    assert res.status_code == 201


def test_ubah_sebagian_field(client, schedule, writer):
    eid = client.post("/api/employees", headers=h(writer), json=emp_body(schedule)).get_json()["id"]
    res = client.patch(f"/api/employees/{eid}", headers=h(writer), json={"department": "APTIKA"})
    assert res.status_code == 200
    data = res.get_json()
    assert data["department"] == "APTIKA" and data["full_name"] == "Siti Aminah"
    assert "Departemen: 'Sekretariat' -> 'APTIKA'" in last_log("employee_updated").description


def test_field_tidak_dikenal_dan_body_bukan_json(client, schedule, writer):
    res = client.post("/api/employees", headers=h(writer), json=emp_body(schedule, is_admin=True))
    assert res.status_code == 400 and "is_admin" in res.get_json()["message"]
    res = client.post("/api/employees", headers=h(writer), data="bukan json",
                      content_type="text/plain")
    assert res.status_code == 400 and res.get_json()["error"] == "body_tidak_valid"


def test_aktif_nonaktif_karyawan_dan_tidak_ada_hapus(client, schedule, writer):
    eid = client.post("/api/employees", headers=h(writer), json=emp_body(schedule)).get_json()["id"]
    res = client.post(f"/api/employees/{eid}/deactivate", headers=h(writer))
    assert res.status_code == 200 and res.get_json()["is_active"] is False
    res = client.post(f"/api/employees/{eid}/activate", headers=h(writer))
    assert res.get_json()["is_active"] is True
    assert client.delete(f"/api/employees/{eid}", headers=h(writer)).status_code == 405
    assert db.session.get(Employee, eid) is not None


def test_karyawan_tidak_ditemukan(client, writer):
    assert client.patch("/api/employees/999", headers=h(writer), json={}).status_code == 404
    assert client.get("/api/employees/999", headers=h(writer)).status_code == 404


# ---------------------------------------------------------------- jadwal

def test_tambah_jadwal_per_hari(client, writer):
    res = client.post("/api/work-schedules", headers=h(writer, "andi"),
                      json={"name": "Jam Kerja Resmi", "late_tolerance_minutes": 15, "days": DAYS})
    assert res.status_code == 201, res.get_json()
    data = res.get_json()
    assert [d["weekday_name"] for d in data["days"]] == ["Senin", "Selasa", "Rabu", "Kamis", "Jumat"]
    assert data["days"][4]["end_time"] == "11:30"
    assert data["summary"] == ["Senin–Kamis 07:30–16:00", "Jumat 07:30–11:30"]
    assert "pelaku: andi" in last_log("schedule_created").description


@pytest.mark.parametrize("days,message", [
    ("Senin", "harus berupa daftar"),
    ([{"weekday": 7, "start_time": "07:30", "end_time": "16:00"}], "0 (Senin) sampai 6"),
    ([{"weekday": 0, "start_time": "07:30", "end_time": "16:00"}] * 2, "lebih dari sekali"),
])
def test_bentuk_hari_tidak_valid(client, writer, days, message):
    res = client.post("/api/work-schedules", headers=h(writer),
                      json={"name": "Uji", "late_tolerance_minutes": 0, "days": days})
    assert res.status_code == 400 and message in res.get_json()["message"]


def test_aturan_jadwal_dari_layanan(client, writer):
    res = client.post("/api/work-schedules", headers=h(writer),
                      json={"name": "Uji", "late_tolerance_minutes": 999, "days": DAYS})
    assert res.status_code == 422 and "late_tolerance_minutes" in res.get_json()["fields"]
    res = client.post("/api/work-schedules", headers=h(writer),
                      json={"name": "Uji", "late_tolerance_minutes": 0,
                            "days": [{"weekday": 0, "start_time": "08:00", "end_time": "08:00"}]})
    assert res.status_code == 422


def test_ubah_jumat_saja_tanpa_mengirim_semua_hari(client, schedule, writer):
    res = client.patch(f"/api/work-schedules/{schedule.id}", headers=h(writer),
                       json={"late_tolerance_minutes": 10})
    assert res.status_code == 200
    data = res.get_json()
    assert data["late_tolerance_minutes"] == 10 and len(data["days"]) == len(schedule.days)


def test_jadwal_dipakai_tidak_bisa_dinonaktifkan(client, schedule, writer):
    client.post("/api/employees", headers=h(writer), json=emp_body(schedule))
    res = client.post(f"/api/work-schedules/{schedule.id}/deactivate", headers=h(writer))
    assert res.status_code == 422 and "masih dipakai" in res.get_json()["message"]
    assert db.session.get(WorkSchedule, schedule.id).is_active


def test_baca_jadwal_dengan_token_read(client, schedule, reader):
    res = client.get("/api/work-schedules", headers=h(reader))
    assert res.status_code == 200 and res.get_json()["total"] == 1
    res = client.get(f"/api/work-schedules/{schedule.id}", headers=h(reader))
    assert res.get_json()["name"] == schedule.name


# ---------------------------------------------------------------- ringkasan, laporan, audit

def test_ringkasan_hari_ini(client, schedule, writer, reader):
    for code in ("A1", "A2"):
        client.post("/api/employees", headers=h(writer),
                    json=emp_body(schedule, employee_code=code, full_name=f"Orang {code}"))
    a1 = db.session.execute(db.select(Employee).filter_by(employee_code="A1")).scalar_one()
    att.record(a1.id, now=datetime(2026, 10, 5, 7, 50))
    body = client.get("/api/summary/today?date=2026-10-05", headers=h(reader)).get_json()
    assert (body["present"], body["late"], body["scheduled"]) == (1, 1, 2)
    assert [e["employee_code"] for e in body["not_checked_in"]] == ["A2"]


def test_laporan_rekap_dan_detail(client, schedule, writer, reader):
    client.post("/api/employees", headers=h(writer), json=emp_body(schedule))
    e = db.session.execute(db.select(Employee)).scalar_one()
    att.record(e.id, now=datetime(2026, 10, 5, 7, 50))
    body = client.get("/api/reports/summary?start=2026-10-05&end=2026-10-06", headers=h(reader)).get_json()
    row = body["data"][0]
    assert (row["present"], row["late"], row["late_minutes"]) == (1, 1, 5)
    assert body["totals"]["present"] == 1
    body = client.get("/api/reports/detail?start=2026-10-05&end=2026-10-06&per_page=1",
                      headers=h(reader)).get_json()
    assert body["total"] == 2 and body["pages"] == 2 and body["data"][0]["status"] == "Terlambat"
    res = client.get("/api/reports/summary?start=2026-10-06&end=2026-10-01", headers=h(reader))
    assert res.status_code == 400


def test_audit_log_hanya_untuk_token_write(client, schedule, writer, reader):
    client.post("/api/employees", headers=h(writer, "andi"), json=emp_body(schedule))
    assert client.get("/api/audit-logs", headers=h(reader)).status_code == 403
    body = client.get("/api/audit-logs?action=employee_created", headers=h(writer)).get_json()
    assert body["total"] == 1 and "pelaku: andi" in body["data"][0]["description"]


def test_pelaku_dibersihkan(client, schedule, writer):
    client.post("/api/employees", headers=h(writer, "andi] [palsu" + "x" * 100), json=emp_body(schedule))
    desc = last_log("employee_created").description
    assert "pelaku: andi palsu" in desc and "x" * 61 not in desc


def test_perintah_token_write(app, admin_user):
    from conftest import ADMIN_PASSWORD, ADMIN_USERNAME

    res = app.test_cli_runner().invoke(args=[
        "api-token-create", "--name", "Laravel - dashboard admin", "--scope", "write",
        "--admin", ADMIN_USERNAME, "--password", ADMIN_PASSWORD])
    assert res.exit_code == 0, res.output
    assert "Token WRITE" in res.output and "seperti password admin" in res.output
    row = db.session.execute(db.select(ApiToken)).scalar_one()
    assert row.scope == "write" and (row.expires_at - row.created_at).days in (29, 30)
    listing = app.test_cli_runner().invoke(args=["api-token-list"]).output
    assert "write" in listing
