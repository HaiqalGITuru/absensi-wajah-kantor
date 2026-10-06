"""
Deteksi wajah (PRD 7.2, FR-05) dan pemeriksaan kualitas wajah (FR-03).

Deteksi hanya menjawab "di mana ada wajah", BUKAN "wajah siapa". Identitas
ditentukan di lapisan pengenalan (embedder + matcher).

Detektor dibuat dengan antarmuka sederhana: detect(frame) -> list[FaceDetection].
Jika suatu saat diganti (misalnya YOLO berlisensi), cukup buat kelas baru dengan
method yang sama tanpa mengubah bagian sistem lain.
"""
from dataclasses import dataclass, field

import cv2
import numpy as np


@dataclass
class FaceDetection:
    """Satu wajah dalam koordinat frame ASLI.

    raw: 15 angka format YuNet [x, y, w, h, 5 titik (x, y), skor], dibutuhkan
    SFace untuk meluruskan wajah.
    """

    raw: np.ndarray

    @property
    def box(self):
        x, y, w, h = self.raw[:4]
        return int(round(x)), int(round(y)), int(round(w)), int(round(h))

    @property
    def score(self):
        return float(self.raw[14])

    @property
    def landmarks(self):
        """Titik: mata kanan, mata kiri, hidung, mulut kanan, mulut kiri."""
        return self.raw[4:14].reshape(5, 2)

    @property
    def size(self):
        return float(min(self.raw[2], self.raw[3]))


class YuNetDetector:
    name = "yunet-2023mar"

    def __init__(self, model_path, score_threshold=0.8, nms_threshold=0.3,
                 top_k=20, detect_width=640):
        self.detect_width = int(detect_width)
        self._net = cv2.FaceDetectorYN.create(
            str(model_path), "", (320, 320), float(score_threshold),
            float(nms_threshold), int(top_k),
        )
        self._input_size = None

    def detect(self, frame):
        """Mendeteksi wajah. Frame besar diperkecil dulu agar cepat, lalu
        koordinat dikembalikan ke ukuran asli.

        YuNet paling baik untuk wajah sekitar 10-300 piksel, sehingga
        memperkecil 1280x720 ke 640 lebar juga membantu wajah yang dekat kamera.
        """
        h, w = frame.shape[:2]
        scale = min(1.0, self.detect_width / float(w)) if self.detect_width else 1.0
        small = frame if scale == 1.0 else cv2.resize(
            frame, (int(round(w * scale)), int(round(h * scale))), interpolation=cv2.INTER_AREA
        )
        size = (small.shape[1], small.shape[0])
        if size != self._input_size:
            self._net.setInputSize(size)
            self._input_size = size

        _, faces = self._net.detect(small)
        if faces is None:
            return []
        result = []
        for row in faces:
            row = row.astype(np.float32).copy()
            if scale != 1.0:
                row[:14] /= scale
            result.append(FaceDetection(raw=row))
        result.sort(key=lambda d: d.size, reverse=True)
        return result


# --------------------------------------------------------------------- kualitas

@dataclass
class QualitySettings:
    min_score: float = 0.85
    min_size: int = 80            # piksel, sisi terpendek kotak wajah di frame asli
    min_brightness: float = 50.0
    max_brightness: float = 210.0
    min_sharpness: float = 40.0   # varian Laplacian pada wajah 112x112 (SEMENTARA)
    max_roll_deg: float = 20.0    # kemiringan kepala ke samping
    max_yaw_ratio: float = 0.35   # seberapa jauh hidung bergeser dari tengah mata
    edge_margin: int = 4          # wajah tidak boleh terpotong tepi frame

    @classmethod
    def from_config(cls, config):
        return cls(
            min_score=float(config.get("FACE_MIN_SCORE", cls.min_score)),
            min_size=int(config.get("FACE_MIN_SIZE", cls.min_size)),
            min_sharpness=float(config.get("FACE_MIN_SHARPNESS", cls.min_sharpness)),
        )


@dataclass
class QualityResult:
    ok: bool
    reasons: list = field(default_factory=list)
    metrics: dict = field(default_factory=dict)


def face_metrics(frame, det, crop=None):
    """Ukuran kualitas wajah. `crop` adalah wajah 112x112 hasil pelurusan (opsional)."""
    x, y, w, h = det.box
    lm = det.landmarks
    right_eye, left_eye, nose = lm[0], lm[1], lm[2]
    dx, dy = left_eye[0] - right_eye[0], left_eye[1] - right_eye[1]
    eye_dist = float(np.hypot(dx, dy)) or 1.0
    roll = float(np.degrees(np.arctan2(dy, dx)))
    eye_mid = (right_eye + left_eye) / 2.0
    yaw_ratio = float(abs(nose[0] - eye_mid[0]) / eye_dist)

    if crop is None:
        fh, fw = frame.shape[:2]
        x0, y0 = max(0, x), max(0, y)
        x1, y1 = min(fw, x + w), min(fh, y + h)
        crop = frame[y0:y1, x0:x1]
        if crop.size:
            crop = cv2.resize(crop, (112, 112))
    gray = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY) if crop.size else np.zeros((1, 1), np.uint8)
    return {
        "score": round(det.score, 3),
        "size": round(det.size, 1),
        "brightness": round(float(gray.mean()), 1),
        "sharpness": round(float(cv2.Laplacian(gray, cv2.CV_64F).var()), 1),
        "roll": round(roll, 1),
        "yaw_ratio": round(yaw_ratio, 2),
    }


def assess(frame, det, settings=None, crop=None):
    """Menilai apakah wajah layak dipakai untuk pendaftaran atau pengenalan.

    Alasan penolakan ditulis dalam bahasa yang bisa ditampilkan ke karyawan.
    """
    s = settings or QualitySettings()
    m = face_metrics(frame, det, crop)
    reasons = []

    fh, fw = frame.shape[:2]
    x, y, w, h = det.box
    if x < s.edge_margin or y < s.edge_margin or x + w > fw - s.edge_margin or y + h > fh - s.edge_margin:
        reasons.append("Wajah terpotong tepi gambar")
    if m["score"] < s.min_score:
        reasons.append("Wajah kurang jelas")
    if m["size"] < s.min_size:
        reasons.append("Mendekat ke kamera")
    if m["brightness"] < s.min_brightness:
        reasons.append("Terlalu gelap")
    elif m["brightness"] > s.max_brightness:
        reasons.append("Terlalu terang")
    if m["sharpness"] < s.min_sharpness:
        reasons.append("Gambar buram, jangan bergerak")
    if abs(m["roll"]) > s.max_roll_deg:
        reasons.append("Tegakkan kepala")
    if m["yaw_ratio"] > s.max_yaw_ratio:
        reasons.append("Hadap lurus ke kamera")

    return QualityResult(ok=not reasons, reasons=reasons, metrics=m)
