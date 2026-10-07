"""Pengujian Tahap 5C: uji performa tidak menyentuh data asli."""
from pathlib import Path

from extensions import db
from fake_faces import FakeDetector, FakeEmbedder, SequenceCam, person_frame
from models import AttendanceSession, AuditLog, Employee
from perf import benchmark as bm
from recognition.detector import QualitySettings
from recognition.liveness import LivenessSettings


def test_uji_performa_tidak_menyentuh_database_utama(app, schedule, monkeypatch):
    db.session.add(Employee(employee_code="ASLI1", full_name="Karyawan Asli", department="X",
                            work_schedule_id=schedule.id))
    db.session.commit()
    before = (db.session.query(Employee).count(), db.session.query(AuditLog).count(),
              db.session.query(AttendanceSession).count())

    created = []
    real_mkdtemp = bm.tempfile.mkdtemp
    monkeypatch.setattr(bm.tempfile, "mkdtemp", lambda **kw: created.append(real_mkdtemp(**kw)) or created[-1])

    r = bm.run(app, employees=12, templates=2, days=3, echo=lambda *a: None)

    after = (db.session.query(Employee).count(), db.session.query(AuditLog).count(),
             db.session.query(AttendanceSession).count())
    assert after == before
    assert db.session.query(Employee).filter(Employee.employee_code.like("PERF%")).count() == 0
    assert created and not Path(created[0]).exists()   # folder sementara terhapus

    assert r["templates"] == 24 and r["failed"] == 0 and r["sessions"] > 0
    for key in ("match_ms", "load_s", "attendance_page_s", "dashboard_s", "report_page_s", "export_csv_s"):
        assert r[key] > 0


def test_cli_perf_check(app):
    res = app.test_cli_runner().invoke(args=["perf-check", "--employees", "10", "--days", "2",
                                             "--templates", "1"])
    assert res.exit_code == 0, res.output
    assert "database SEMENTARA" in res.output
    assert "Pencocokan 1 wajah" in res.output and "OK" in res.output
    assert "tidak diukur" in res.output   # kamera tidak diminta


def test_pengukuran_kamera_dengan_tiruan(app, monkeypatch):
    from recognition import cli
    from recognition.matcher import Matcher

    class Cam(SequenceCam):
        def release(self):
            pass

    monkeypatch.setattr(cli, "_load_models", lambda a: (FakeDetector(), FakeEmbedder(), QualitySettings()))
    monkeypatch.setattr(cli, "_load_liveness", lambda a: (None, LivenessSettings(mode="off")))
    monkeypatch.setattr(cli, "_open_camera", lambda a: Cam([person_frame("budi", i) for i in range(500)]))
    r = bm.measure_camera(app, Matcher([]), 0.3, echo=lambda *a: None)
    assert r["frames_with_face"] > 0 and r["frame_ms"] > 0
