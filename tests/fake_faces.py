"""Wajah tiruan untuk pengujian pengenalan tanpa foto wajah asli.

Setiap "orang" adalah gambar bertekstur dengan tingkat kecerahan tertentu.
FakeEmbedder mengubah kecerahan itu menjadi embedding unik per orang, dengan
sedikit derau agar mirip kondisi nyata (sampel orang yang sama tidak identik).
"""
import cv2
import numpy as np

from recognition.detector import FaceDetection
from recognition.embedder import EMBEDDING_DIM

PERSON_BRIGHTNESS = {"budi": 100, "sari": 130, "tamu": 160, "budi_kembar": 101}


def make_detection(x=200, y=150, w=160, h=200, score=0.92, nose_shift=0.0):
    """Wajah buatan format YuNet. nose_shift = arah kepala (signed yaw)."""
    cx, cy, half = x + w / 2, y + h * 0.4, w * 0.2
    nose_x = cx + nose_shift * 2 * half
    return FaceDetection(raw=np.array(
        [x, y, w, h, cx - half, cy, cx + half, cy, nose_x, cy + h * 0.15,
         cx - half * 0.8, cy + h * 0.3, cx + half * 0.8, cy + h * 0.3, score],
        dtype=np.float32))


def person_frame(person, seed=0, w=640, h=480):
    rng = np.random.default_rng(seed)
    value = PERSON_BRIGHTNESS[person]
    return np.clip(rng.integers(-50, 50, (h, w, 3)) + value, 0, 255).astype(np.uint8)


def _person_vector(value):
    # Orang "kembar" (kecerahan selisih 1) mendapat vektor yang hampir sama.
    base = np.random.default_rng(int(round(value / 10.0))).normal(size=EMBEDDING_DIM)
    return base / np.linalg.norm(base)


class FakeDetector:
    def __init__(self, boxes=None, nose_shift=0.0):
        self.boxes = boxes if boxes is not None else [(200, 150, 160, 200)]
        self.nose_shift = nose_shift  # bisa diubah selama tes untuk meniru menoleh

    def detect(self, frame):
        return [make_detection(*b, nose_shift=self.nose_shift) for b in self.boxes]


class FakeLiveness:
    """Model anti-foto tiruan: skor tetap yang bisa diubah selama tes."""

    name = "fake-liveness"

    def __init__(self, score):
        self.value = score

    def score(self, frame, detection):
        return self.value


class FakeEmbedder:
    model_name = "fake-model"

    def __init__(self, noise=0.15, seed=123):
        self.noise = noise
        self.rng = np.random.default_rng(seed)

    def align(self, frame, det):
        x, y, w, h = det.box
        return cv2.resize(frame[y:y + h, x:x + w], (112, 112))

    def embed_aligned(self, aligned):
        v = _person_vector(float(aligned.mean())) + self.rng.normal(scale=self.noise / np.sqrt(EMBEDDING_DIM), size=EMBEDDING_DIM)
        return (v / np.linalg.norm(v)).astype(np.float32)

    def embed(self, frame, det):
        return self.embed_aligned(self.align(frame, det))


class SequenceCam:
    """Kamera tiruan yang mengirim daftar frame berurutan, lalu frame terakhir terus."""

    def __init__(self, frames):
        self.frames = list(frames)
        self.i = 0

    def read(self):
        frame = self.frames[min(self.i, len(self.frames) - 1)]
        self.i += 1
        return frame


class StepClock:
    def __init__(self, step=0.1):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t
