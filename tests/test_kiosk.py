"""Pengujian Tahap 4B: layanan kamera kios dan halaman kios."""
from datetime import datetime

import pytest

from camera.webcam import CameraError
from extensions import db
from fake_faces import FakeDetector, FakeEmbedder, StepClock, person_frame
from kiosk import state as ks
from kiosk.lock import AlreadyRunning, SingleInstanceLock
from kiosk.worker import UNKNOWN_MESSAGE, KioskWorker
from models import AttendanceSession, Employee
from recognition.detector import QualitySettings
from recognition.matcher import Matcher
from services import attendance_service as att
from services import biometric_service as bio
from tracking.face_tracker import TrackerSettings

MONDAY_0720 = datetime(2026, 10, 5, 7, 20)


# ---------------------------------------------------------------- status kios

def test_status_kosong_berarti_offline():
    assert ks.public_view(None)["online"] is False
    assert ks.admin_view(None)["online"] is False


def test_status_basi_berarti_offline(tmp_path):
    store = ks.KioskState(tmp_path / "s.json")
    store.publish({"camera": "ok"}, clock=lambda: 1000.0)
    assert ks.public_view(store.read(), now=1003.0)["online"] is True
    assert ks.public_view(store.read(), now=1006.0)["online"] is False


def test_pesan_kedaluwarsa(tmp_path):
    store = ks.KioskState(tmp_path / "s.json")
    store.publish({"camera": "ok", "message": {"kind": "success", "text": "Hai", "at": 1000.0}},
                  clock=lambda: 1004.0)
    assert ks.public_view(store.read(), now=1004.0)["message"]["text"] == "Hai"
    assert ks.public_view(store.read(), now=1004.0, message_seconds=3)["message"] is None


def test_tampilan_publik_tidak_membocorkan_data_internal(tmp_path):
    store = ks.KioskState(tmp_path / "s.json")
    store.publish({"camera": "ok", "detail": "rahasia", "employees": 5, "recorded": 3,
                   "message": {"kind": "success", "text": "Hai", "at": 1000.0, "employee_id": 7}},
                  clock=lambda: 1000.0)
    view = ks.public_view(store.read(), now=1000.0)
    assert set(view) == {"online", "camera", "message", "hint"}
    assert set(view["message"]) == {"kind", "text"}


def test_file_status_rusak_tidak_membuat_galat(tmp_path):
    path = tmp_path / "s.json"
    path.write_text("{rusak")
    assert ks.KioskState(path).read() is None


# ---------------------------------------------------------------- kunci satu salinan

def test_layanan_kamera_hanya_satu_salinan(tmp_path):
    """FR-04: kamera tidak boleh dipegang dua proses."""
    path = tmp_path / "kiosk.lock"
    first = SingleInstanceLock(path).acquire()
    with pytest.raises(AlreadyRunning):
        SingleInstanceLock(path).acquire()
    first.release()
    SingleInstanceLock(path).acquire().release()


# ---------------------------------------------------------------- layanan kamera

class Cam:
    """Kamera tiruan; frame diatur lewat atribut `frame`, bisa dibuat gagal."""

    def __init__(self, frame=None):
        self.frame = frame
        self.fail = False
        self.reopened = 0
        self.can_reopen = True

    def read(self):
        if self.fail:
            raise CameraError("Kamera berhenti mengirim gambar.")
        return self.frame

    def reopen(self, attempts=1, delay=0):
        self.reopened += 1
        if not self.can_reopen:
            raise CameraError("gagal")
        self.fail = False

    def release(self):
        pass


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


def make_worker(app, tmp_path, cam, now=MONDAY_0720):
    def factory():
        templates, _ = bio.load_active_templates(FakeEmbedder.model_name)
        return Matcher(templates)

    clock = StepClock(0.1)
    worker = KioskWorker(
        app, cam, FakeDetector(), FakeEmbedder(seed=2), QualitySettings(), factory,
        TrackerSettings(confirm_votes=3), att.AttendanceSettings(),
        ks.KioskState(tmp_path / "state.json"), clock=clock, wall_clock=lambda: clock.t,
    )
    # Waktu absensi dikunci agar tes tidak bergantung pada jam sebenarnya.
    real_record = att.record
    worker_record = lambda employee_id, **kw: real_record(employee_id, now=now, **kw)
    return worker, worker_record


@pytest.fixture
def frozen_record(monkeypatch):
    def install(record):
        monkeypatch.setattr(att, "record", record)
    return install


def sessions():
    return db.session.execute(db.select(AttendanceSession)).scalars().all()


def test_alur_penuh_wajah_dikenali_lalu_absensi_tercatat(app, tmp_path, budi, frozen_record):
    """TC-11 ujung ke ujung: kamera -> pengenalan -> layanan absensi -> database -> kios."""
    cam = Cam(person_frame("budi", 99))
    worker, record = make_worker(app, tmp_path, cam)
    frozen_record(record)
    worker.reload_templates(force=True)

    for _ in range(5):
        worker.run_once()

    s = sessions()
    assert len(s) == 1 and s[0].employee_id == budi.id
    assert s[0].check_in_method == "kamera" and s[0].check_in_score > 0.8
    view = ks.public_view(worker.state.read(), now=worker.wall_clock())
    assert view["message"]["kind"] == "success"
    assert view["message"]["text"].startswith("Selamat datang, Budi.")
    assert "K001" not in view["message"]["text"]


def test_berdiri_lama_di_depan_kamera_tidak_membuat_absensi_ganda(app, tmp_path, budi, frozen_record):
    """FR-07 / TC-12."""
    worker, record = make_worker(app, tmp_path, Cam(person_frame("budi", 3)))
    frozen_record(record)
    worker.reload_templates(force=True)
    for _ in range(200):
        worker.run_once()
    assert len(sessions()) == 1
    assert sum(1 for d in worker.decisions if d.recorded) == 1


def test_wajah_tidak_dikenal_tidak_mencatat_dan_memberi_pesan(app, tmp_path, budi, frozen_record):
    """6.3 / TC-10."""
    worker, record = make_worker(app, tmp_path, Cam(person_frame("tamu", 5)))
    frozen_record(record)
    worker.reload_templates(force=True)
    for _ in range(10):
        worker.run_once()
    assert sessions() == []
    assert worker.message["text"] == UNKNOWN_MESSAGE
    assert worker.message["kind"] == "warning"


def test_tanpa_templat_tidak_ada_absensi(app, tmp_path, biometric_keys):
    worker, _ = make_worker(app, tmp_path, Cam(person_frame("budi", 1)))
    worker.reload_templates(force=True)
    worker.run_once()
    assert sessions() == []
    assert "Belum ada wajah" in worker.detail


def test_karyawan_baru_dikenali_setelah_muat_ulang(app, tmp_path, schedule, biometric_keys, admin_user, frozen_record):
    worker, record = make_worker(app, tmp_path, Cam(person_frame("budi", 1)))
    frozen_record(record)
    worker.template_reload_seconds = 1.0
    worker.reload_templates(force=True)
    assert worker.matcher.template_count == 0

    e = Employee(employee_code="K009", full_name="Budi Baru", department="TI", work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    emb = FakeEmbedder(seed=7)
    det = FakeDetector().detect(None)[0]
    bio.save_enrollment(e, [emb.embed(person_frame("budi", i), det) for i in range(5)],
                        FakeEmbedder.model_name, admin_user.id)

    for _ in range(20):
        worker.run_once()
    assert worker.matcher.employee_count == 1
    assert len(sessions()) == 1


def test_database_gagal_tidak_tampil_berhasil(app, tmp_path, budi, frozen_record):
    """TC-17 / BR-07: gangguan database tampil sebagai gangguan."""
    worker, _ = make_worker(app, tmp_path, Cam(person_frame("budi", 4)))
    frozen_record(lambda employee_id, **kw: att.Decision(att.ERROR, "Gangguan sistem. Absensi BELUM tercatat.", employee_id))
    worker.reload_templates(force=True)
    for _ in range(5):
        worker.run_once()
    assert worker.message["kind"] == "error"
    assert "BELUM tercatat" in worker.message["text"]
    assert worker.recorded == 0


def test_kamera_terputus_lalu_pulih(app, tmp_path, budi, frozen_record):
    """TC-16 / FR-04."""
    cam = Cam(person_frame("budi", 1))
    worker, record = make_worker(app, tmp_path, cam)
    frozen_record(record)
    worker.reopen_seconds = 0.0
    worker.reload_templates(force=True)

    cam.fail = True
    cam.can_reopen = False
    worker.run_once()
    assert worker.camera_status == ks.CAMERA_ERROR
    assert ks.public_view(worker.state.read(), now=worker.wall_clock())["camera"] == "error"
    assert sessions() == []

    cam.can_reopen = True
    worker.run_once()
    assert worker.camera_status == ks.CAMERA_OK
    assert cam.reopened >= 2


def test_peristiwa_dalam_jeda_tidak_memanggil_layanan_absensi(app, tmp_path, budi, frozen_record):
    from tracking.face_tracker import TrackEvent

    calls = []
    worker, _ = make_worker(app, tmp_path, Cam())
    frozen_record(lambda employee_id, **kw: calls.append(employee_id))
    assert worker.handle_event(TrackEvent(1, budi.id, 0.3, True, 0.9)) is None
    assert calls == []


def test_status_kios_menyatakan_anti_foto_belum_aktif(app, tmp_path, budi):
    worker, _ = make_worker(app, tmp_path, Cam(person_frame("budi", 1)))
    worker.reload_templates(force=True)
    worker.publish(force=True)
    assert ks.admin_view(worker.state.read(), now=worker.wall_clock())["liveness"] is False


# ---------------------------------------------------------------- halaman web

def test_halaman_kios_tanpa_login(client):
    res = client.get("/kiosk/")
    assert res.status_code == 200
    assert "Absensi Kehadiran" in res.get_data(as_text=True)


def test_status_kios_offline_jika_layanan_tidak_berjalan(app, client, tmp_path):
    app.config["KIOSK_STATE_FILE"] = str(tmp_path / "tidak-ada.json")
    data = client.get("/kiosk/status").get_json()
    assert data["online"] is False
    assert data["time"] and data["date"]


def test_status_kios_online(app, client, tmp_path):
    path = tmp_path / "state.json"
    app.config["KIOSK_STATE_FILE"] = str(path)
    import time
    ks.KioskState(path).publish({"camera": "ok", "message": {"kind": "success", "text": "Selamat datang, Budi.", "at": time.time()}})
    res = client.get("/kiosk/status")
    data = res.get_json()
    assert data["online"] is True and data["camera"] == "ok"
    assert data["message"]["text"] == "Selamat datang, Budi."
    assert res.headers["Cache-Control"] == "no-store"


def test_kios_tidak_bisa_mengubah_data(client):
    """PRD 3.2: halaman kios tidak menerima perubahan data."""
    assert client.post("/kiosk/status").status_code == 405
    assert client.post("/kiosk/").status_code == 405


def test_dashboard_menampilkan_status_layanan_kamera(app, auth_client, tmp_path):
    app.config["KIOSK_STATE_FILE"] = str(tmp_path / "tidak-ada.json")
    body = auth_client.get("/admin").get_data(as_text=True)
    assert "Layanan kamera kios" in body
    assert "Tidak berjalan" in body
    assert "anti-foto (keaktifan wajah) <strong>belum aktif</strong>" in body


def test_cli_kiosk_run_menolak_salinan_kedua(app, tmp_path):
    lock_path = tmp_path / "kiosk.lock"
    app.config["KIOSK_LOCK_FILE"] = str(lock_path)
    held = SingleInstanceLock(lock_path).acquire()
    try:
        res = app.test_cli_runner().invoke(args=["kiosk-run", "--no-preview"])
        assert res.exit_code != 0
        assert "sudah berjalan" in res.output
    finally:
        held.release()


def test_layanan_kamera_bisa_berjalan_di_thread_lain(app, tmp_path, budi, frozen_record):
    """Layanan menyiapkan konteks aplikasinya sendiri."""
    import threading

    worker, record = make_worker(app, tmp_path, Cam(person_frame("budi", 8)))
    frozen_record(record)
    counter = {"n": 0}

    def stop():
        counter["n"] += 1
        return counter["n"] > 10

    errors = []
    t = threading.Thread(target=lambda: _safe(worker.run, stop, errors))
    t.start()
    t.join(timeout=10)
    assert errors == []
    assert worker.recorded == 1
    assert worker.state.read()["camera"] == "stopped"


def _safe(fn, stop, errors):
    try:
        fn(should_stop=stop)
    except Exception as exc:  # pragma: no cover - hanya untuk melaporkan
        errors.append(exc)
