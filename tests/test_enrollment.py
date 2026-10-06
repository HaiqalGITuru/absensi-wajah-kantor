"""Pengujian pendaftaran wajah (FR-03) dan perintah face-enroll / face-identify."""
import pytest

from conftest import ADMIN_PASSWORD, ADMIN_USERNAME
from extensions import db
from fake_faces import (FakeDetector, FakeEmbedder, SequenceCam, StepClock,
                        person_frame)
from models import AuditLog, Employee
from recognition import enrollment
from recognition.detector import QualitySettings
from recognition.matcher import Matcher
from services import ValidationError
from services import audit_service as audit
from services import biometric_service as bio


def _logs(action):
    return db.session.execute(db.select(AuditLog).filter_by(action=action)).scalars().all()


def _frames(person, n=60):
    return [person_frame(person, seed=i) for i in range(n)]


@pytest.fixture
def budi(app, schedule):
    e = Employee(employee_code="K001", full_name="Budi", department="TI",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    return e


@pytest.fixture
def sari(app, schedule):
    e = Employee(employee_code="K002", full_name="Sari", department="TI",
                 work_schedule_id=schedule.id)
    db.session.add(e)
    db.session.commit()
    return e


# ---------------------------------------------------------------- pengambilan sampel

def test_mengumpulkan_sampel_berkualitas():
    r = enrollment.collect(SequenceCam(_frames("budi")), FakeDetector(), FakeEmbedder(),
                           QualitySettings(), samples=5, min_interval=0.3, clock=StepClock(0.1))
    assert len(r.embeddings) == 5
    assert min(enrollment.check_consistency(r.embeddings)) > 0.8


def test_dua_wajah_dalam_gambar_tidak_diambil():
    """Mencegah salah mendaftarkan orang lain yang ikut terlihat."""
    two = FakeDetector([(50, 100, 150, 180), (400, 100, 150, 180)])
    with pytest.raises(enrollment.EnrollmentError, match="Hanya satu orang"):
        enrollment.collect(SequenceCam(_frames("budi")), two, FakeEmbedder(),
                           QualitySettings(), samples=3, timeout=3, clock=StepClock(0.1))


def test_wajah_tidak_layak_tidak_diambil():
    """FR-03: sampel buruk tidak dipakai (di sini wajah terlalu kecil)."""
    small = FakeDetector([(200, 150, 40, 50)])
    with pytest.raises(enrollment.EnrollmentError, match="Mendekat ke kamera"):
        enrollment.collect(SequenceCam(_frames("budi")), small, FakeEmbedder(),
                           QualitySettings(), samples=3, timeout=3, clock=StepClock(0.1))


def test_tanpa_wajah_waktu_habis():
    """TC-08: wajah tidak terdeteksi -> pendaftaran ditolak."""
    with pytest.raises(enrollment.EnrollmentError, match="tidak ada wajah terdeteksi"):
        enrollment.collect(SequenceCam(_frames("budi")), FakeDetector([]), FakeEmbedder(),
                           QualitySettings(), samples=3, timeout=2, clock=StepClock(0.1))


def test_sampel_diberi_jeda_waktu():
    r = enrollment.collect(SequenceCam(_frames("budi")), FakeDetector(), FakeEmbedder(),
                           QualitySettings(), samples=3, min_interval=0.5, clock=StepClock(0.1))
    assert r.seconds >= 1.0


def test_sampel_dari_dua_orang_berbeda_ditolak():
    e = FakeEmbedder()
    det = FakeDetector().detect(None)[0]
    mixed = [e.embed(person_frame("budi", i), det) for i in range(3)] + \
            [e.embed(person_frame("sari", i), det) for i in range(3)]
    with pytest.raises(enrollment.EnrollmentError, match="tidak konsisten"):
        enrollment.check_consistency(mixed)


def test_satu_sampel_orang_lain_di_antara_sampel_juga_ditolak():
    e = FakeEmbedder()
    det = FakeDetector().detect(None)[0]
    samples = [e.embed(person_frame("budi", i), det) for i in range(4)]
    samples.append(e.embed(person_frame("sari", 9), det))
    with pytest.raises(enrollment.EnrollmentError, match="tidak konsisten"):
        enrollment.check_consistency(samples)


def test_konflik_dengan_karyawan_lain_terdeteksi():
    e = FakeEmbedder()
    det = FakeDetector().detect(None)[0]
    budi_emb = [e.embed(person_frame("budi", i), det) for i in range(3)]
    sari_emb = [e.embed(person_frame("sari", i), det) for i in range(3)]
    matcher = Matcher([(1, v) for v in budi_emb] + [(2, v) for v in sari_emb])

    # Wajah Budi didaftarkan lagi atas nama karyawan 3 -> konflik dengan 1.
    conflict = enrollment.find_conflict(budi_emb, matcher, employee_id=3)
    assert conflict is not None and conflict.employee_id == 1
    # Mendaftarkan ulang Budi atas namanya sendiri bukan konflik.
    assert enrollment.find_conflict(budi_emb, matcher, employee_id=1) is None


# ---------------------------------------------------------------- penyimpanan atomik

def _embs(person, n=5):
    e = FakeEmbedder()
    det = FakeDetector().detect(None)[0]
    return [e.embed(person_frame(person, i), det) for i in range(n)]


def test_simpan_pendaftaran_sekaligus(biometric_keys, budi, admin_user):
    saved = bio.save_enrollment(budi, _embs("budi"), "fake-model", admin_user.id)
    assert len(saved) == 5
    assert len(bio.templates_for(budi)) == 5
    logs = _logs(audit.FACE_TEMPLATE_CREATED)
    assert len(logs) == 1 and "5 templat" in logs[0].description


def test_daftar_ulang_tanpa_alasan_ditolak(biometric_keys, budi, admin_user):
    bio.save_enrollment(budi, _embs("budi"), "fake-model", admin_user.id)
    with pytest.raises(ValidationError) as exc:
        bio.save_enrollment(budi, _embs("budi"), "fake-model", admin_user.id)
    assert "--replace" in exc.value.errors["_"]
    assert len(bio.templates_for(budi)) == 5


def test_daftar_ulang_mengganti_templat_lama(biometric_keys, budi, admin_user):
    old = bio.save_enrollment(budi, _embs("budi"), "fake-model", admin_user.id)
    old_ids = {t.id for t in old}
    bio.save_enrollment(budi, _embs("budi", 3), "fake-model", admin_user.id,
                        replace_reason="Wajah berubah setelah operasi")
    new = bio.templates_for(budi)
    assert len(new) == 3
    assert not old_ids & {t.id for t in new}
    assert "Wajah berubah" in _logs(audit.FACE_TEMPLATE_REVOKED)[0].description


def test_gagal_di_tengah_tidak_meninggalkan_data_setengah_jadi(biometric_keys, budi, admin_user, monkeypatch):
    bio.save_enrollment(budi, _embs("budi"), "fake-model", admin_user.id)
    calls = {"n": 0}
    real = bio.encrypt_embedding

    def broken(*args, **kwargs):
        calls["n"] += 1
        if calls["n"] == 2:
            raise RuntimeError("galat simulasi")
        return real(*args, **kwargs)

    monkeypatch.setattr(bio, "encrypt_embedding", broken)
    with pytest.raises(RuntimeError):
        bio.save_enrollment(budi, _embs("budi", 3), "fake-model", admin_user.id,
                            replace_reason="uji kegagalan")
    # Templat lama masih utuh, tidak ada templat baru setengah jadi.
    assert len(bio.templates_for(budi)) == 5
    assert _logs(audit.FACE_TEMPLATE_REVOKED) == []


# ---------------------------------------------------------------- perintah CLI

@pytest.fixture
def fake_runtime(monkeypatch):
    """Mengganti model dan kamera asli dengan versi tiruan untuk perintah CLI."""
    from recognition import cli, identify

    state = {"frames": _frames("budi")}
    monkeypatch.setattr(cli, "_load_models", lambda app: (FakeDetector(), FakeEmbedder(), QualitySettings()))
    # Anti-foto diuji terpisah (test_liveness.py); di sini dimatikan.
    from recognition.liveness import LivenessSettings
    monkeypatch.setattr(cli, "_load_liveness", lambda app: (None, LivenessSettings(mode="off")))

    class _Cam(SequenceCam):
        def release(self):
            pass

    monkeypatch.setattr(cli, "_open_camera", lambda app: _Cam(state["frames"]))
    clock = StepClock(0.1)
    monkeypatch.setattr(enrollment.time, "perf_counter", clock)
    monkeypatch.setattr(identify.time, "perf_counter", clock)
    return state


def _enroll(app, code, *extra, password=ADMIN_PASSWORD):
    return app.test_cli_runner().invoke(args=[
        "face-enroll", "--employee", code, "--admin", ADMIN_USERNAME,
        "--password", password, "--no-preview", *extra,
    ])


def test_cli_enroll_berhasil(app, biometric_keys, admin_user, budi, fake_runtime):
    res = _enroll(app, "k001")
    assert res.exit_code == 0, res.output
    assert "5 sampel wajah K001 tersimpan terenkripsi" in res.output
    assert len(bio.templates_for(budi)) == 5
    assert _logs(audit.FACE_TEMPLATE_CREATED)[0].user_id == admin_user.id


def test_cli_enroll_password_admin_salah(app, biometric_keys, admin_user, budi, fake_runtime):
    res = _enroll(app, "K001", password="salah")
    assert res.exit_code != 0 and "password admin salah" in res.output
    assert bio.templates_for(budi) == []


def test_cli_enroll_karyawan_nonaktif(app, biometric_keys, admin_user, budi, fake_runtime):
    budi.is_active = False
    db.session.commit()
    res = _enroll(app, "K001")
    assert res.exit_code != 0 and "nonaktif" in res.output


def test_cli_enroll_ulang_butuh_replace(app, biometric_keys, admin_user, budi, fake_runtime):
    assert _enroll(app, "K001").exit_code == 0
    res = _enroll(app, "K001")
    assert res.exit_code != 0 and "--replace" in res.output
    res = _enroll(app, "K001", "--replace", "Kacamata baru")
    assert res.exit_code == 0, res.output


def test_cli_enroll_wajah_orang_lain_ditolak(app, biometric_keys, admin_user, budi, sari, fake_runtime):
    """Wajah Budi tidak boleh didaftarkan sebagai Sari."""
    assert _enroll(app, "K001").exit_code == 0
    res = _enroll(app, "K002")  # kamera tiruan masih menampilkan Budi
    assert res.exit_code != 0
    assert "mirip dengan wajah terdaftar milik K001 - Budi" in res.output
    assert bio.templates_for(sari) == []


def test_cli_enroll_allow_similar_tercatat(app, biometric_keys, admin_user, budi, sari, fake_runtime):
    assert _enroll(app, "K001").exit_code == 0
    res = _enroll(app, "K002", "--allow-similar")
    assert res.exit_code == 0, res.output
    assert len(_logs(audit.FACE_ENROLL_SIMILAR_OVERRIDE)) == 1


def test_cli_identify_tanpa_templat(app, biometric_keys, fake_runtime):
    res = app.test_cli_runner().invoke(args=["face-identify", "--no-preview", "--seconds", "1"])
    assert res.exit_code != 0 and "Belum ada wajah terdaftar" in res.output


def test_cli_identify_mengenali_karyawan_terdaftar(app, biometric_keys, admin_user, budi, fake_runtime):
    """TC-09: karyawan terdaftar dikenali, dan absensi TIDAK dicatat."""
    assert _enroll(app, "K001").exit_code == 0
    fake_runtime["frames"] = _frames("budi")
    res = app.test_cli_runner().invoke(args=["face-identify", "--no-preview", "--seconds", "3"])
    assert res.exit_code == 0, res.output
    assert "Dikenali: K001 Budi" in res.output
    assert "absensi TIDAK dicatat" in res.output
    assert "Orang dikenali             : 1 kali" in res.output


def test_cli_identify_menolak_wajah_tidak_terdaftar(app, biometric_keys, admin_user, budi, fake_runtime):
    """TC-10: wajah yang tidak terdaftar tidak menghasilkan identitas."""
    assert _enroll(app, "K001").exit_code == 0
    fake_runtime["frames"] = _frames("tamu")
    res = app.test_cli_runner().invoke(args=["face-identify", "--no-preview", "--seconds", "3"])
    assert res.exit_code == 0, res.output
    assert "Dikenali:" not in res.output
    assert "Jejak tidak dikenal        : 1" in res.output


def test_cli_identify_tidak_mengenali_karyawan_nonaktif(app, biometric_keys, admin_user, budi, fake_runtime):
    """TC-15 (sebagian): templat karyawan nonaktif tidak dimuat."""
    assert _enroll(app, "K001").exit_code == 0
    budi.is_active = False
    db.session.commit()
    res = app.test_cli_runner().invoke(args=["face-identify", "--no-preview", "--seconds", "1"])
    assert res.exit_code != 0 and "Belum ada wajah terdaftar" in res.output
