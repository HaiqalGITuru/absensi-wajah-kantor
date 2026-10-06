"""Pengujian Tahap 1: halaman awal, health check, dan struktur database."""
from datetime import date, datetime

import pytest
from sqlalchemy import inspect
from sqlalchemy.exc import IntegrityError, OperationalError

import config
from app import create_app
from extensions import db
from models import AttendanceSession, Employee


def test_halaman_awal_tampil(client):
    """TC-01: membuka aplikasi."""
    res = client.get("/")
    assert res.status_code == 200
    assert "Sistem Absensi Wajah" in res.get_data(as_text=True)


def test_health_database_ok(client):
    """TC-02: koneksi database berhasil."""
    res = client.get("/health")
    data = res.get_json()
    assert res.status_code == 200
    assert data["status"] == "ok"
    assert data["database"] == "ok"
    assert data["timezone"] == "Asia/Makassar"
    datetime.fromisoformat(data["server_time"])


def test_health_database_gagal(client, monkeypatch):
    """Database terputus menghasilkan status 503 tanpa membocorkan detail error."""

    def gagal(*args, **kwargs):
        raise OperationalError("SELECT 1", {}, Exception("rahasia-internal"))

    monkeypatch.setattr(db.session, "execute", gagal)
    res = client.get("/health")
    data = res.get_json()
    assert res.status_code == 503
    assert data["database"] == "error"
    assert "rahasia-internal" not in res.get_data(as_text=True)


def test_semua_tabel_terbentuk(app):
    tables = set(inspect(db.engine).get_table_names())
    expected = {
        "users",
        "employees",
        "face_templates",
        "work_schedules",
        "work_schedule_days",
        "attendance_sessions",
        "audit_logs",
    }
    assert expected <= tables


def test_kode_karyawan_harus_unik(app):
    """TC-06 di level database: kode karyawan duplikat ditolak."""
    db.session.add(Employee(employee_code="K001", full_name="A", department="TI"))
    db.session.commit()
    db.session.add(Employee(employee_code="K001", full_name="B", department="TI"))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_sesi_absensi_ganda_ditolak_database(app):
    """BR-05: satu karyawan tidak boleh punya dua sesi pada tanggal yang sama."""
    emp = Employee(employee_code="K002", full_name="C", department="TI")
    db.session.add(emp)
    db.session.commit()
    tanggal = date(2026, 10, 6)
    db.session.add(AttendanceSession(employee_id=emp.id, work_date=tanggal))
    db.session.commit()
    db.session.add(AttendanceSession(employee_id=emp.id, work_date=tanggal))
    with pytest.raises(IntegrityError):
        db.session.commit()
    db.session.rollback()


def test_tanpa_secret_key_aplikasi_menolak_jalan(monkeypatch):
    monkeypatch.setattr(config.ProductionConfig, "SECRET_KEY", None)
    with pytest.raises(RuntimeError):
        create_app("production")
