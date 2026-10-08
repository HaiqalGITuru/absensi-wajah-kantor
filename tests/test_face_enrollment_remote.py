"""Pendaftaran wajah dari panel admin (lewat API) yang dijalankan kamera kios."""
from datetime import timedelta

import pytest

from extensions import db
from kiosk import state as ks
from kiosk.worker import KioskWorker
from models import AuditLog, Employee, FaceEnrollmentRequest, office_now
from recognition.detector import QualitySettings
from recognition.liveness import LivenessSettings
from recognition.matcher import Matcher
from services import api_token_service as tokens
from services import attendance_service as att
from services import biometric_service as bio
from services import face_enrollment_service as fe
from tests.fake_faces import (FakeDetector, FakeEmbedder, FakeLiveness, SequenceCam, StepClock,
                              person_frame)
from tracking.face_tracker import TrackerSettings


@pytest.fixture
def writer(app, admin_user):
    return tokens.create_token("Laravel - panel admin", admin_user.id, days=30, scope="write")[0]


def h(token, actor="andi"):
    return {"Authorization": f"Bearer {token}", "X-Actor": actor}


@pytest.fixture
def sari(app, schedule, biometric_keys):
    e = Employee(employee_code="199001012020011001", full_name="Sari Wulandari", department="TI",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    return e


@pytest.fixture
def budi(app, schedule, biometric_keys, admin_user):
    e = Employee(employee_code="K001", full_name="Budi Santoso", department="TI",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    emb, det = FakeEmbedder(seed=1), FakeDetector().detect(None)[0]
    bio.save_enrollment(e, [emb.embed(person_frame("budi", i), det) for i in range(5)],
                        FakeEmbedder.model_name, admin_user.id)
    return e


def make_worker(app, tmp_path, person, liveness_score=0.95, detector=None, timeout=60.0):
    def factory():
        templates, _ = bio.load_active_templates(FakeEmbedder.model_name)
        return Matcher(templates)

    clock = StepClock(0.1)
    frames = [person_frame(person, i) for i in range(400)] if person else [person_frame("budi", 0)]
    worker = KioskWorker(
        app, SequenceCam(frames), detector or FakeDetector(), FakeEmbedder(seed=3), QualitySettings(),
        factory, TrackerSettings(confirm_votes=3), att.AttendanceSettings(),
        ks.KioskState(tmp_path / "state.json"), clock=clock, wall_clock=lambda: clock.t,
        liveness=FakeLiveness(liveness_score), liveness_settings=LivenessSettings(mode="passive+challenge"),
        enroll_poll_seconds=0, enroll_start_delay=0, enroll_timeout=timeout,
    )
    return worker


def last_log(action):
    return db.session.execute(db.select(AuditLog).filter_by(action=action)
                              .order_by(AuditLog.id.desc())).scalars().first()


def template_count(employee):
    return len(bio.templates_for(employee))


# ---------------------------------------------------------------- API

def test_minta_pendaftaran_lewat_api(client, sari, writer):
    res = client.post(f"/api/employees/{sari.id}/face-enrollment", headers=h(writer), json={})
    assert res.status_code == 202, res.get_json()
    body = res.get_json()
    assert body["status"] == "pending" and body["samples_total"] == 5
    assert body["kiosk_online"] is False and body["finished"] is False
    log = last_log("face_enroll_requested")
    assert "pelaku: andi" in log.description and sari.employee_code in log.description

    res = client.get(f"/api/face-enrollments/{body['id']}", headers=h(writer))
    assert res.status_code == 200 and res.get_json()["status"] == "pending"


def test_hanya_satu_permintaan_aktif(client, sari, budi, writer):
    client.post(f"/api/employees/{sari.id}/face-enrollment", headers=h(writer), json={})
    res = client.post(f"/api/employees/{budi.id}/face-enrollment", headers=h(writer),
                      json={"replace_reason": "Wajah berubah"})
    assert res.status_code == 422 and "sedang melayani" in res.get_json()["message"]


def test_daftar_ulang_wajib_alasan(client, budi, writer):
    res = client.post(f"/api/employees/{budi.id}/face-enrollment", headers=h(writer), json={})
    assert res.status_code == 422 and "replace_reason" in res.get_json()["fields"]
    res = client.post(f"/api/employees/{budi.id}/face-enrollment", headers=h(writer),
                      json={"replace_reason": "abc"})
    assert res.status_code == 422
    res = client.post(f"/api/employees/{budi.id}/face-enrollment", headers=h(writer),
                      json={"replace_reason": "Sering tidak dikenali"})
    assert res.status_code == 202 and res.get_json()["is_replacement"] is True


def test_karyawan_nonaktif_ditolak(client, sari, writer):
    sari.is_active = False
    db.session.commit()
    res = client.post(f"/api/employees/{sari.id}/face-enrollment", headers=h(writer), json={})
    assert res.status_code == 422 and "nonaktif" in res.get_json()["message"]


def test_token_read_tidak_bisa(client, sari, admin_user):
    reader = tokens.create_token("baca", admin_user.id, days=30)[0]
    res = client.post(f"/api/employees/{sari.id}/face-enrollment", headers=h(reader), json={})
    assert res.status_code == 403


def test_batalkan_permintaan(client, sari, writer):
    rid = client.post(f"/api/employees/{sari.id}/face-enrollment", headers=h(writer), json={}).get_json()["id"]
    res = client.post(f"/api/face-enrollments/{rid}/cancel", headers=h(writer))
    assert res.status_code == 200 and res.get_json()["status"] == "cancelled"
    assert client.post(f"/api/face-enrollments/{rid}/cancel", headers=h(writer)).status_code == 422
    assert last_log("face_enroll_cancelled") is not None


def test_permintaan_kedaluwarsa_jika_kios_tidak_berjalan(app, sari):
    past = office_now() - timedelta(minutes=5)
    req = fe.create(sari, now=past)
    assert fe.get(req.id).status == "expired"
    assert "jalankan_kios.bat" in fe.get(req.id).message


# ---------------------------------------------------------------- kios

def test_kios_mendaftarkan_wajah_lalu_langsung_mengenali(app, tmp_path, sari):
    req = fe.create(sari, via="[via API 'Laravel - panel admin' (aw_x), pelaku: andi]")
    worker = make_worker(app, tmp_path, "sari")
    with app.app_context():
        worker.reload_templates(force=True)
        assert worker.run_once() is True
    req = db.session.get(FaceEnrollmentRequest, req.id)
    assert req.status == "done" and req.samples_taken == 5, req.message
    assert template_count(sari) == 5
    log = last_log("face_template_created")
    assert f"permintaan #{req.id}" in log.description and "pelaku: andi" in log.description
    assert sari.id in worker.matcher.scores(FakeEmbedder(seed=9).embed(person_frame("sari", 1),
                                                                   FakeDetector().detect(None)[0]))
    state = ks.KioskState(tmp_path / "state.json").read()
    assert "berhasil didaftarkan" in state["message"]["text"]
    assert "Sari" in state["message"]["text"] and "Wulandari" not in state["message"]["text"]


def test_foto_ditolak_saat_pendaftaran(app, tmp_path, sari):
    req = fe.create(sari)
    worker = make_worker(app, tmp_path, "sari", liveness_score=0.1)
    with app.app_context():
        worker.run_once()
    req = db.session.get(FaceEnrollmentRequest, req.id)
    assert req.status == "failed" and "orang asli" in req.message
    assert template_count(sari) == 0
    assert last_log("face_enroll_failed") is not None


def test_wajah_orang_lain_yang_sudah_terdaftar_ditolak(app, tmp_path, sari, budi):
    req = fe.create(sari)
    worker = make_worker(app, tmp_path, "budi")      # yang berdiri ternyata Budi
    with app.app_context():
        worker.run_once()
    req = db.session.get(FaceEnrollmentRequest, req.id)
    assert req.status == "failed" and "K001" in req.message
    assert template_count(sari) == 0


def test_dibatalkan_saat_berjalan(app, tmp_path, sari, monkeypatch):
    req = fe.create(sari)
    monkeypatch.setattr(fe, "is_cancelled", lambda r: True)
    worker = make_worker(app, tmp_path, "sari")
    with app.app_context():
        worker.run_once()
    req = db.session.get(FaceEnrollmentRequest, req.id)
    assert req.status == "cancelled" and template_count(sari) == 0


def test_waktu_habis_tanpa_wajah(app, tmp_path, sari):
    req = fe.create(sari)
    worker = make_worker(app, tmp_path, None, detector=FakeDetector(boxes=[]), timeout=2.0)
    with app.app_context():
        worker.run_once()
    req = db.session.get(FaceEnrollmentRequest, req.id)
    assert req.status == "failed" and "Waktu habis" in req.message


def test_daftar_ulang_mengganti_templat(app, tmp_path, budi):
    req = fe.create(budi, replace_reason="Sering tidak dikenali")
    worker = make_worker(app, tmp_path, "budi")
    with app.app_context():
        worker.run_once()
    assert db.session.get(FaceEnrollmentRequest, req.id).status == "done"
    assert template_count(budi) == 5
    assert "Sering tidak dikenali" in last_log("face_template_revoked").description


def test_tanpa_permintaan_kios_tetap_absensi_biasa(app, tmp_path, sari):
    worker = make_worker(app, tmp_path, "sari")
    with app.app_context():
        worker.reload_templates(force=True)
        assert worker.check_enrollment() is False
        worker.run_once()
    assert worker.enrollments == []
