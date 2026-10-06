"""Pengujian Tahap 4D: anti-foto / keaktifan wajah (FR-11, TC-19)."""
import os
import random
from collections import deque
from datetime import datetime
from pathlib import Path

import cv2
import numpy as np
import pytest

from extensions import db
from fake_faces import (FakeDetector, FakeEmbedder, FakeLiveness, SequenceCam, StepClock,
                        make_detection, person_frame)
from kiosk import state as ks
from kiosk.worker import LIVENESS_REJECT_MESSAGE, KioskWorker
from models import AttendanceSession, AuditLog, Employee
from recognition import liveness as lv
from recognition import model_files as mf
from recognition.detector import QualitySettings
from recognition.matcher import Matcher
from services import attendance_service as att
from services import audit_service as audit
from services import biometric_service as bio
from tracking.face_tracker import TrackEvent, TrackerSettings

MODEL_DIR = Path(os.getenv("TEST_MODEL_DIR", Path(__file__).resolve().parents[1] / "model_files"))
HAVE_LIVENESS = all((MODEL_DIR / m.filename).exists() for m in mf.LIVENESS_MODELS)
SAMPLES = os.getenv("LIVENESS_TEST_DIR")  # berisi contoh real.jpg / fake.jpg (opsional)


class Track:
    """Jejak tiruan dengan data yang dipakai gerbang keaktifan."""

    def __init__(self, track_id=1, scores=(), yaws=()):
        self.id = track_id
        self.live_scores = deque(scores, maxlen=10)
        self.yaw_history = deque(yaws, maxlen=150)


def ev(track_id=1, employee_id=7, cooldown=False):
    return TrackEvent(track_id, employee_id, 0.3, cooldown, 0.9)


S = lv.LivenessSettings()  # passive+challenge, 0.7 / 0.3, 3 sampel, 3 dtk, 6 dtk, 0.18


# ---------------------------------------------------------------- dasar

def test_arah_kepala_dari_titik_wajah():
    assert lv.signed_yaw(make_detection()) == pytest.approx(0.0, abs=1e-6)
    assert lv.signed_yaw(make_detection(nose_shift=0.3)) == pytest.approx(0.3, abs=1e-3)
    assert lv.signed_yaw(make_detection(nose_shift=-0.25)) == pytest.approx(-0.25, abs=1e-3)


@pytest.mark.parametrize(
    "scores,verdict",
    [((0.9, 0.95, 0.8), lv.LIVE), ((0.05, 0.1, 0.2), lv.SPOOF),
     ((0.5, 0.6, 0.4), lv.UNCERTAIN), ((0.9, 0.9), None)],
)
def test_putusan_pasif(scores, verdict):
    assert lv.passive_verdict(Track(scores=scores), S) == verdict


def test_pengaturan_dari_config(app):
    s = lv.LivenessSettings.from_config(app.config)
    assert s.mode == "passive+challenge" and s.passive and s.challenge
    app.config["LIVENESS_MODE"] = "sembarang"
    with pytest.raises(ValueError):
        lv.LivenessSettings.from_config(app.config)


# ---------------------------------------------------------------- gerbang

def test_mode_off_langsung_diterima():
    gate = lv.LivenessGate(lv.LivenessSettings(mode="off"))
    r = gate.update({}, [ev()], 0.0)
    assert len(r.accepted) == 1 and not gate.pending


def test_wajah_asli_diterima():
    gate = lv.LivenessGate(S)
    t = Track(scores=(0.95, 0.9, 0.97))
    r = gate.update({1: t}, [ev()], 0.0)
    assert len(r.accepted) == 1 and r.rejected == []


def test_foto_ditolak():
    """TC-19: foto/layar terdeteksi -> tidak ada peristiwa yang diterima."""
    gate = lv.LivenessGate(S)
    r = gate.update({1: Track(scores=(0.05, 0.1, 0.02))}, [ev()], 0.0)
    assert r.accepted == []
    assert r.rejected == [(r.rejected[0][0], lv.REASON_SPOOF)]
    assert 1 in gate.rejected_tracks


def test_menunggu_sampel_cukup_sebelum_memutuskan():
    gate = lv.LivenessGate(S)
    t = Track(scores=(0.95,))
    r = gate.update({1: t}, [ev()], 0.0)
    assert r.accepted == [] and r.rejected == [] and 1 in gate.pending
    t.live_scores.extend([0.9, 0.92])
    r = gate.update({1: t}, [], 0.5)
    assert len(r.accepted) == 1


def test_ragu_memicu_tantangan_lalu_lolos_saat_menoleh():
    gate = lv.LivenessGate(S, rng=random.Random(1))
    t = Track(scores=(0.5, 0.55, 0.6), yaws=[(0.0, 0.0)])
    r = gate.update({1: t}, [ev()], 0.1)
    assert r.accepted == [] and 1 in r.prompts
    challenge = gate.pending[1].challenge
    assert "Tolehkan kepala" in r.prompts[1]

    # Menoleh ke arah yang diminta.
    t.yaw_history.append((0.5, challenge.sign * 0.3))
    r = gate.update({1: t}, [], 0.6)
    assert len(r.accepted) == 1


def test_tantangan_menoleh_ke_arah_salah_tidak_lolos():
    gate = lv.LivenessGate(S, rng=random.Random(1))
    t = Track(scores=(0.5, 0.5, 0.5), yaws=[(0.0, 0.0)])
    gate.update({1: t}, [ev()], 0.1)
    sign = gate.pending[1].challenge.sign
    t.yaw_history.append((0.5, -sign * 0.4))
    r = gate.update({1: t}, [], 0.6)
    assert r.accepted == []
    r = gate.update({1: t}, [], 0.1 + S.challenge_seconds + 0.1)
    assert r.rejected[0][1] == lv.REASON_CHALLENGE


def test_foto_yang_dimiringkan_tidak_lolos_tantangan():
    """Foto datar yang dimiringkan tidak menggeser hidung relatif terhadap mata."""
    gate = lv.LivenessGate(S, rng=random.Random(3))
    t = Track(scores=(0.5, 0.5, 0.5), yaws=[(0.0, 0.02)])
    gate.update({1: t}, [ev()], 0.0)
    for i in range(1, 90):
        t.yaw_history.append((i * 0.1, 0.02 + 0.03 * np.sin(i)))   # goyang kecil
        r = gate.update({1: t}, [], i * 0.1)
        if r.rejected or r.accepted:
            break
    assert r.accepted == [] and r.rejected[0][1] == lv.REASON_CHALLENGE


def test_arah_tantangan_acak():
    dirs = set()
    for seed in range(20):
        gate = lv.LivenessGate(S, rng=random.Random(seed))
        gate.update({1: Track(scores=(0.5, 0.5, 0.5))}, [ev()], 0.0)
        dirs.add(gate.pending[1].challenge.direction)
    assert dirs == {"kiri", "kanan"}


def test_kamera_dicerminkan_membalik_arah():
    normal = lv.LivenessGate(S, rng=random.Random(5))
    mirror = lv.LivenessGate(lv.LivenessSettings(mirrored=True), rng=random.Random(5))
    for g in (normal, mirror):
        g.update({1: Track(scores=(0.5, 0.5, 0.5))}, [ev()], 0.0)
    assert normal.pending[1].challenge.sign == -mirror.pending[1].challenge.sign


def test_mode_pasif_saja_ragu_ditolak_setelah_waktu_habis():
    gate = lv.LivenessGate(lv.LivenessSettings(mode="passive"))
    t = Track(scores=(0.5, 0.5, 0.5))
    assert gate.update({1: t}, [ev()], 0.0).rejected == []
    r = gate.update({1: t}, [], S.decide_seconds + 0.1)
    assert r.rejected[0][1] == lv.REASON_UNCERTAIN


def test_foto_terdeteksi_saat_tantangan_tetap_ditolak():
    gate = lv.LivenessGate(S, rng=random.Random(2))
    t = Track(scores=(0.5, 0.5, 0.5))
    gate.update({1: t}, [ev()], 0.0)
    t.live_scores.extend([0.05] * 10)
    r = gate.update({1: t}, [], 0.5)
    assert r.rejected[0][1] == lv.REASON_SPOOF


def test_orang_pergi_sebelum_dipastikan_tidak_dicatat():
    gate = lv.LivenessGate(S)
    gate.update({1: Track(scores=(0.9,))}, [ev()], 0.0)
    r = gate.update({}, [], 2.0)
    assert r.accepted == [] and not gate.pending


def test_peristiwa_dalam_jeda_tidak_ditahan():
    gate = lv.LivenessGate(S)
    r = gate.update({}, [ev(cooldown=True)], 0.0)
    assert len(r.accepted) == 1 and not gate.pending


# ---------------------------------------------------------------- model asli

@pytest.mark.skipif(not HAVE_LIVENESS, reason="model anti-foto belum diunduh")
def test_model_asli_menghasilkan_peluang():
    model = lv.PassiveLiveness([(MODEL_DIR / mf.MINIFAS_V2.filename, 2.7),
                                (MODEL_DIR / mf.MINIFAS_V1SE.filename, 4.0)])
    frame = np.random.default_rng(0).integers(0, 255, (480, 640, 3), dtype=np.uint8)
    p = model.score(frame, make_detection())
    assert 0.0 <= p <= 1.0


@pytest.mark.skipif(not (HAVE_LIVENESS and SAMPLES), reason="LIVENESS_TEST_DIR tidak diisi")
def test_model_asli_membedakan_contoh_asli_dan_palsu():
    from recognition.detector import YuNetDetector

    detector = YuNetDetector(mf.verify(MODEL_DIR, mf.YUNET))
    model = lv.PassiveLiveness([(MODEL_DIR / mf.MINIFAS_V2.filename, 2.7),
                                (MODEL_DIR / mf.MINIFAS_V1SE.filename, 4.0)])
    for name, expect_real in (("real.jpg", True), ("fake.jpg", False)):
        img = cv2.imread(str(Path(SAMPLES) / name))
        p = model.score(img, detector.detect(img)[0])
        assert (p >= 0.7) if expect_real else (p <= 0.3), (name, p)


# ---------------------------------------------------------------- kios

@pytest.fixture
def budi(app, schedule, biometric_keys, admin_user):
    e = Employee(employee_code="K001", full_name="Budi Santoso", department="TI",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    emb = FakeEmbedder(seed=1)
    det = FakeDetector().detect(None)[0]
    bio.save_enrollment(e, [emb.embed(person_frame("budi", i), det) for i in range(5)],
                        FakeEmbedder.model_name, admin_user.id)
    return e


class Cam:
    def __init__(self, frame):
        self.frame = frame

    def read(self):
        return self.frame

    def reopen(self, **kw):
        pass


def kiosk(app, tmp_path, monkeypatch, live_score, mode="passive+challenge", detector=None):
    real = att.record
    monkeypatch.setattr(att, "record", lambda emp_id, **kw: real(emp_id, now=datetime(2026, 10, 5, 7, 20), **kw))

    def factory():
        templates, _ = bio.load_active_templates(FakeEmbedder.model_name)
        return Matcher(templates)

    clock = StepClock(0.1)
    liveness = FakeLiveness(live_score)
    worker = KioskWorker(
        app, Cam(person_frame("budi", 4)), detector or FakeDetector(), FakeEmbedder(seed=2),
        QualitySettings(), factory, TrackerSettings(), att.AttendanceSettings(),
        ks.KioskState(tmp_path / "s.json"), clock=clock, wall_clock=lambda: clock.t,
        liveness=liveness, liveness_settings=lv.LivenessSettings(mode=mode),
    )
    worker.gate.rng = random.Random(4)
    worker.reload_templates(force=True)
    return worker, liveness


def sessions():
    return db.session.execute(db.select(AttendanceSession)).scalars().all()


def test_kios_wajah_asli_tercatat(app, tmp_path, budi, monkeypatch):
    worker, _ = kiosk(app, tmp_path, monkeypatch, live_score=0.95)
    for _ in range(10):
        worker.run_once()
    assert len(sessions()) == 1
    assert worker.message["kind"] == "success"


def test_kios_foto_tidak_tercatat_dan_masuk_audit(app, tmp_path, budi, monkeypatch):
    """TC-19 ujung ke ujung: wajah cocok, tetapi foto -> TIDAK ada absensi."""
    worker, _ = kiosk(app, tmp_path, monkeypatch, live_score=0.05)
    for _ in range(30):
        worker.run_once()
    assert sessions() == []
    assert worker.liveness_rejected == 1
    assert worker.message["text"] == LIVENESS_REJECT_MESSAGE
    log = db.session.execute(db.select(AuditLog).filter_by(action=audit.LIVENESS_REJECTED)).scalar_one()
    assert "K001" in log.description and "foto atau layar" in log.description


def test_kios_ragu_meminta_menoleh_lalu_tercatat(app, tmp_path, budi, monkeypatch):
    detector = FakeDetector()
    worker, _ = kiosk(app, tmp_path, monkeypatch, live_score=0.5, detector=detector)
    for _ in range(8):
        worker.run_once()
    assert sessions() == []
    assert worker.message["kind"] == "info" and "Tolehkan kepala" in worker.message["text"]

    challenge = next(iter(worker.gate.pending.values())).challenge
    detector.nose_shift = challenge.sign * 0.3   # karyawan benar-benar menoleh
    for _ in range(3):
        worker.run_once()
    assert len(sessions()) == 1


def test_kios_mode_off_tetap_berfungsi(app, tmp_path, budi, monkeypatch):
    worker, _ = kiosk(app, tmp_path, monkeypatch, live_score=0.0, mode="off")
    for _ in range(10):
        worker.run_once()
    assert len(sessions()) == 1  # perilaku lama tanpa anti-foto


def test_status_kios_melaporkan_mode_anti_foto(app, tmp_path, budi, monkeypatch):
    worker, _ = kiosk(app, tmp_path, monkeypatch, live_score=0.9)
    worker.publish(force=True)
    assert ks.admin_view(worker.state.read(), now=worker.wall_clock())["liveness"] == "passive+challenge"


def test_dashboard_mode_off_memberi_peringatan(app, auth_client):
    app.config["LIVENESS_MODE"] = "off"
    body = auth_client.get("/admin").get_data(as_text=True)
    assert "<strong>dimatikan</strong>" in body


# ---------------------------------------------------------------- face-identify

@pytest.fixture
def identify_runtime(monkeypatch):
    from recognition import cli, identify
    from recognition import enrollment

    state = {"live": FakeLiveness(0.05)}
    monkeypatch.setattr(cli, "_load_models", lambda app: (FakeDetector(), FakeEmbedder(), QualitySettings()))
    monkeypatch.setattr(cli, "_load_liveness", lambda app: (state["live"], lv.LivenessSettings()))

    class _Cam(SequenceCam):
        def release(self):
            pass

    monkeypatch.setattr(cli, "_open_camera", lambda app: _Cam([person_frame("budi", i) for i in range(60)]))
    clock = StepClock(0.1)
    monkeypatch.setattr(enrollment.time, "perf_counter", clock)
    monkeypatch.setattr(identify.time, "perf_counter", clock)
    return state


def test_cli_identify_foto_tidak_dikenali(app, budi, identify_runtime):
    """TC-19 lewat face-identify: hasil yang diharapkan 'Orang dikenali: 0 kali'."""
    res = app.test_cli_runner().invoke(args=["face-identify", "--no-preview", "--seconds", "3"])
    assert res.exit_code == 0, res.output
    assert "Orang dikenali             : 0 kali" in res.output
    assert "Ditolak anti-foto          : 1 kali" in res.output
    assert "DITOLAK anti-foto: K001" in res.output


def test_cli_identify_wajah_asli_dikenali(app, budi, identify_runtime):
    identify_runtime["live"].value = 0.95
    res = app.test_cli_runner().invoke(args=["face-identify", "--no-preview", "--seconds", "3"])
    assert "Orang dikenali             : 1 kali" in res.output
    assert "Keaktifan terendah lolos : 0.95" in res.output
