"""
Pembuat representasi wajah / embedding (PRD 7.3) dengan SFace.

Embedding adalah 128 angka yang mewakili ciri wajah. Embedding dua foto orang
yang sama akan mirip (kemiripan kosinus tinggi). Embedding inilah yang
dienkripsi dan disimpan sebagai templat (Tahap 2C), BUKAN foto wajahnya.
"""
import cv2
import numpy as np

MODEL_NAME = "sface-2021dec"
EMBEDDING_DIM = 128


class SFaceEmbedder:
    model_name = MODEL_NAME

    def __init__(self, model_path):
        self._net = cv2.FaceRecognizerSF.create(str(model_path), "")

    def align(self, frame, detection):
        """Wajah 112x112 yang sudah diluruskan berdasarkan 5 titik wajah."""
        return self._net.alignCrop(frame, detection.raw)

    def embed_aligned(self, aligned):
        feature = self._net.feature(aligned).reshape(-1).astype(np.float32)
        norm = float(np.linalg.norm(feature))
        if norm == 0.0:
            raise ValueError("Embedding bernilai nol.")
        return feature / norm

    def embed(self, frame, detection):
        return self.embed_aligned(self.align(frame, detection))


def cosine_similarity(a, b):
    a = np.asarray(a, dtype=np.float32)
    b = np.asarray(b, dtype=np.float32)
    denom = float(np.linalg.norm(a) * np.linalg.norm(b))
    return float(np.dot(a, b) / denom) if denom else 0.0
