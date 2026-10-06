"""Pengujian Tahap 3B: model, deteksi, kualitas wajah, dan embedding.

- Tes kualitas dan unduhan tidak butuh file model.
- Tes model dilewati jika model belum diunduh (flask --app app models-download).
- Tes dengan foto wajah asli hanya berjalan jika FACE_TEST_IMAGE diisi path foto.
  Proyek ini tidak menyertakan foto wajah apa pun.
"""
import hashlib
import io
import os
from pathlib import Path

import cv2
import numpy as np
import pytest

from recognition import face_check
from recognition import model_files as mf
from recognition.detector import FaceDetection, QualitySettings, assess
from recognition.embedder import EMBEDDING_DIM, cosine_similarity

MODEL_DIR = Path(os.getenv("TEST_MODEL_DIR", Path(__file__).resolve().parents[1] / "model_files"))
HAVE_MODELS = all((MODEL_DIR / m.filename).exists() for m in mf.ALL_MODELS)
needs_models = pytest.mark.skipif(not HAVE_MODELS, reason="model belum diunduh")
FACE_IMAGE = os.getenv("FACE_TEST_IMAGE")
needs_face_image = pytest.mark.skipif(
    not (FACE_IMAGE and Path(FACE_IMAGE).exists()), reason="FACE_TEST_IMAGE tidak diisi"
)


def make_detection(x=200, y=150, w=160, h=200, score=0.92, roll=0.0, nose_shift=0.0):
    """Wajah buatan dalam format YuNet (tanpa model)."""
    cx, cy = x + w / 2, y + h * 0.4
    half = w * 0.2
    t = np.radians(roll)
    re = (cx - half * np.cos(t), cy - half * np.sin(t))
    le = (cx + half * np.cos(t), cy + half * np.sin(t))
    nose = (cx + nose_shift * 2 * half, cy + h * 0.15)
    mr = (cx - half * 0.8, cy + h * 0.3)
    ml = (cx + half * 0.8, cy + h * 0.3)
    return FaceDetection(raw=np.array(
        [x, y, w, h, *re, *le, *nose, *mr, *ml, score], dtype=np.float32))


def textured_frame(value=120, w=640, h=480, seed=0):
    rng = np.random.default_rng(seed)
    noise = rng.integers(-60, 60, (h, w, 3))
    return np.clip(noise + value, 0, 255).astype(np.uint8)


# ---------------------------------------------------------------- kualitas

def test_wajah_baik_dinyatakan_layak():
    q = assess(textured_frame(), make_detection())
    assert q.ok, q.reasons


@pytest.mark.parametrize(
    "kwargs,frame_kwargs,reason",
    [
        ({"w": 50, "h": 60}, {}, "Mendekat ke kamera"),
        ({"x": 0}, {}, "Wajah terpotong tepi gambar"),
        ({"score": 0.6}, {}, "Wajah kurang jelas"),
        ({"roll": 35}, {}, "Tegakkan kepala"),
        ({"nose_shift": 0.6}, {}, "Hadap lurus ke kamera"),
        ({}, {"value": 15}, "Terlalu gelap"),
        ({}, {"value": 245}, "Terlalu terang"),
    ],
)
def test_alasan_wajah_ditolak(kwargs, frame_kwargs, reason):
    frame = textured_frame(**frame_kwargs)
    q = assess(frame, make_detection(**kwargs))
    assert not q.ok
    assert reason in q.reasons


def test_wajah_buram_ditolak():
    sharp = textured_frame()
    blurry = cv2.GaussianBlur(sharp, (31, 31), 0)
    assert assess(sharp, make_detection()).ok
    q = assess(blurry, make_detection())
    assert "Gambar buram, jangan bergerak" in q.reasons


def test_ambang_kualitas_dibaca_dari_config(app):
    s = QualitySettings.from_config(app.config)
    assert (s.min_score, s.min_size, s.min_sharpness) == (0.85, 80, 40.0)


def test_kemiripan_kosinus():
    a = np.array([1.0, 0.0])
    assert cosine_similarity(a, a) == pytest.approx(1.0)
    assert cosine_similarity(a, np.array([0.0, 1.0])) == pytest.approx(0.0)
    assert cosine_similarity(a, np.zeros(2)) == 0.0


# ---------------------------------------------------------------- unduh & verifikasi

def _fake_model(content, urls=("u1", "u2")):
    return mf.ModelFile(
        key="uji", filename="uji.onnx", sha256=hashlib.sha256(content).hexdigest(),
        size=len(content), license="MIT", source="-", urls=urls,
    )


class _Resp(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


def test_verifikasi_model_belum_ada(tmp_path):
    with pytest.raises(mf.ModelFileError, match="models-download"):
        mf.verify(tmp_path, _fake_model(b"isi"))


def test_verifikasi_model_rusak_ditolak(tmp_path):
    model = _fake_model(b"isi asli")
    (tmp_path / model.filename).write_bytes(b"isi palsu")
    with pytest.raises(mf.ModelFileError, match="tidak cocok"):
        mf.verify(tmp_path, model)


def test_unduh_memakai_cermin_jika_sumber_pertama_palsu(tmp_path):
    content = b"model asli"
    model = _fake_model(content)
    responses = {"u1": b"model palsu", "u2": content}
    path = mf.download(tmp_path, model, opener=lambda url, timeout: _Resp(responses[url]))
    assert path.read_bytes() == content
    assert mf.verify(tmp_path, model) == path
    assert not list(tmp_path.glob("*.part"))


def test_unduh_gagal_semua_sumber(tmp_path):
    model = _fake_model(b"asli")

    def opener(url, timeout):
        if url == "u1":
            raise OSError("jaringan putus")
        return _Resp(b"palsu")

    with pytest.raises(mf.ModelFileError) as exc:
        mf.download(tmp_path, model, opener=opener)
    assert "u1: OSError" in str(exc.value)
    assert "u2: sidik jari tidak cocok" in str(exc.value)
    assert not (tmp_path / model.filename).exists()
    assert not list(tmp_path.glob("*.part"))


def test_daftar_model_resmi_lengkap():
    for model in mf.ALL_MODELS:
        assert len(model.sha256) == 64
        assert model.license in ("MIT", "Apache-2.0")
        assert any("opencv_zoo" in url for url in model.urls)


# ---------------------------------------------------------------- uji langsung (tanpa model)

class _FakeDetector:
    def __init__(self, faces_per_frame):
        self.faces = faces_per_frame

    def detect(self, frame):
        return self.faces


class _FakeEmbedder:
    def align(self, frame, det):
        return cv2.resize(frame[:112, :112], (112, 112))

    def embed(self, frame, det):
        return np.ones(EMBEDDING_DIM, np.float32) / np.sqrt(EMBEDDING_DIM)


class _Cam:
    def __init__(self, frame):
        self.frame = frame

    def read(self):
        return self.frame


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.01
        return self.t


def test_face_check_menghitung_statistik():
    frame = textured_frame()
    stats = face_check.run(_Cam(frame), _FakeDetector([make_detection()]), _FakeEmbedder(),
                           QualitySettings(), seconds=1.0, clock=_Clock())
    assert stats["frames"] > 0
    assert stats["with_face"] == stats["frames"]
    assert stats["good"] == stats["frames"]
    assert stats["face_size"] == 160.0


def test_face_check_tanpa_wajah():
    stats = face_check.run(_Cam(textured_frame()), _FakeDetector([]), _FakeEmbedder(),
                           QualitySettings(), seconds=0.5, clock=_Clock())
    assert stats["with_face"] == 0 and stats["good"] == 0


def test_annotate_tidak_mengubah_frame_asli():
    frame = textured_frame()
    before = frame.copy()
    det = make_detection()
    view = face_check.annotate(frame, [(det, assess(frame, det))], ["uji"])
    assert np.array_equal(frame, before)
    assert not np.array_equal(view, frame)


def test_cli_face_check_tanpa_model(app, tmp_path):
    app.config["MODEL_DIR"] = str(tmp_path)
    res = app.test_cli_runner().invoke(args=["face-check", "--no-preview"])
    assert res.exit_code != 0
    assert "models-download" in res.output


def test_cli_models_check_tanpa_model(app, tmp_path):
    app.config["MODEL_DIR"] = str(tmp_path)
    res = app.test_cli_runner().invoke(args=["models-check"])
    assert res.exit_code != 0
    assert "GAGAL" in res.output


# ---------------------------------------------------------------- dengan model asli

@pytest.fixture(scope="module")
def real_models():
    from recognition.detector import YuNetDetector
    from recognition.embedder import SFaceEmbedder

    return (
        YuNetDetector(mf.verify(MODEL_DIR, mf.YUNET)),
        SFaceEmbedder(mf.verify(MODEL_DIR, mf.SFACE)),
    )


@needs_models
def test_model_asli_terverifikasi():
    for model in mf.ALL_MODELS:
        mf.verify(MODEL_DIR, model)


@needs_models
def test_tidak_ada_wajah_pada_gambar_polos(real_models):
    detector, _ = real_models
    assert detector.detect(np.full((720, 1280, 3), 128, np.uint8)) == []


@needs_models
def test_embedding_berukuran_128_dan_ternormalisasi(real_models):
    _, embedder = real_models
    emb = embedder.embed(textured_frame(), make_detection())
    assert emb.shape == (EMBEDDING_DIM,)
    assert float(np.linalg.norm(emb)) == pytest.approx(1.0, abs=1e-5)


@needs_models
@needs_face_image
def test_foto_wajah_asli_terdeteksi_dan_konsisten(real_models):
    detector, embedder = real_models
    img = cv2.imread(FACE_IMAGE)
    dets = detector.detect(img)
    assert dets, "wajah tidak terdeteksi"
    big = cv2.resize(img, None, fx=2.0, fy=2.0)
    brighter = cv2.convertScaleAbs(img, alpha=0.9, beta=25)
    a = embedder.embed(img, dets[0])
    b = embedder.embed(big, detector.detect(big)[0])
    c = embedder.embed(brighter, detector.detect(brighter)[0])
    # Foto yang sama dengan ukuran/cahaya berbeda harus tetap sangat mirip.
    assert cosine_similarity(a, b) > 0.8
    assert cosine_similarity(a, c) > 0.8
