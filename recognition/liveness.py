"""
Pemeriksaan keaktifan wajah / anti-foto (FR-11, Tahap 4D).

Dua lapisan:
A. PASIF  - model MiniFASNet (V2 skala 2.7 + V1SE skala 4.0) menilai apakah
            wajah adalah orang asli atau hasil pemotretan ulang (foto cetak,
            layar HP/monitor). Tidak butuh tindakan apa pun dari karyawan.
B. AKTIF  - (cadangan) jika model ragu, kios meminta karyawan menoleh ke arah
            acak. Kepala asli yang menoleh menggeser posisi hidung terhadap
            mata; foto datar tidak.

Prinsip:
- Pemeriksaan ini TERPISAH dari deteksi dan pengenalan (FR-11).
- Hasil yang meragukan TIDAK menghasilkan absensi otomatis.
- Tidak ada anti-foto yang sempurna; metode alternatif dan pengawasan tetap
  diperlukan (PRD 9.3).
"""
import random

from dataclasses import dataclass, field

import cv2
import numpy as np

LIVE = "live"
SPOOF = "spoof"
UNCERTAIN = "uncertain"

MODE_OFF = "off"
MODES = ("off", "passive", "challenge", "passive+challenge")

REASON_SPOOF = "spoof"
REASON_UNCERTAIN = "uncertain"
REASON_CHALLENGE = "challenge_failed"

REASON_TEXT = {
    REASON_SPOOF: "terdeteksi sebagai foto atau layar",
    REASON_UNCERTAIN: "keaktifan wajah tidak dapat dipastikan",
    REASON_CHALLENGE: "tantangan menoleh tidak terpenuhi",
}


# --------------------------------------------------------------------- pasif

def _crop(frame, box, scale, size=(80, 80)):
    """Potongan wajah yang diperluas `scale` kali, sama seperti implementasi
    resmi Silent-Face-Anti-Spoofing."""
    h_img, w_img = frame.shape[:2]
    x, y, w, h = box
    w, h = max(w, 1), max(h, 1)
    s = min((h_img - 1) / h, (w_img - 1) / w, scale)
    nw, nh = w * s, h * s
    cx, cy = x + w / 2, y + h / 2
    x1, y1 = max(0, int(cx - nw / 2)), max(0, int(cy - nh / 2))
    x2, y2 = min(w_img - 1, int(cx + nw / 2)), min(h_img - 1, int(cy + nh / 2))
    return cv2.resize(frame[y1:y2 + 1, x1:x2 + 1], size)


def _softmax(x):
    e = np.exp(x - np.max(x, axis=1, keepdims=True))
    return e / e.sum(axis=1, keepdims=True)


class PassiveLiveness:
    """Ensemble MiniFASNet. score() = peluang wajah asli (0..1)."""

    name = "minifasnet-v2+v1se"

    def __init__(self, model_scales):
        """model_scales: daftar (path_onnx, skala_potongan)."""
        self._nets = [(cv2.dnn.readNetFromONNX(str(path)), float(scale))
                      for path, scale in model_scales]

    def score(self, frame, detection):
        probs = []
        for net, scale in self._nets:
            face = _crop(frame, detection.box, scale).astype(np.float32)
            net.setInput(np.transpose(face, (2, 0, 1))[np.newaxis])
            probs.append(float(_softmax(net.forward())[0, 1]))  # kelas 1 = asli
        return float(np.mean(probs))


# --------------------------------------------------------------------- aktif

def signed_yaw(detection):
    """Posisi hidung relatif terhadap titik tengah kedua mata, dibagi jarak
    mata. Positif = hidung bergeser ke kanan GAMBAR."""
    lm = detection.landmarks
    right_eye, left_eye, nose = lm[0], lm[1], lm[2]
    eye_dist = float(np.hypot(*(left_eye - right_eye))) or 1.0
    return float((nose[0] - (right_eye[0] + left_eye[0]) / 2.0) / eye_dist)


@dataclass
class Challenge:
    direction: str      # "kiri" atau "kanan" (sisi tubuh karyawan)
    sign: int           # arah pergeseran hidung di gambar
    baseline: float
    started_at: float

    @property
    def instruction(self):
        return f"Tolehkan kepala ke {self.direction.upper()} Anda, lalu kembali menghadap kamera."


# --------------------------------------------------------------------- gerbang

@dataclass
class LivenessSettings:
    mode: str = "passive+challenge"
    real_threshold: float = 0.7
    spoof_threshold: float = 0.3
    min_samples: int = 3
    decide_seconds: float = 3.0
    challenge_seconds: float = 6.0
    challenge_delta: float = 0.18
    mirrored: bool = False

    @property
    def passive(self):
        return "passive" in self.mode

    @property
    def challenge(self):
        return "challenge" in self.mode

    @classmethod
    def from_config(cls, config):
        mode = str(config.get("LIVENESS_MODE", cls.mode)).strip().lower()
        if mode not in MODES:
            raise ValueError(f"LIVENESS_MODE '{mode}' tidak dikenal. Pilihan: {', '.join(MODES)}")
        return cls(
            mode=mode,
            real_threshold=float(config.get("LIVENESS_REAL_THRESHOLD", cls.real_threshold)),
            spoof_threshold=float(config.get("LIVENESS_SPOOF_THRESHOLD", cls.spoof_threshold)),
            min_samples=int(config.get("LIVENESS_MIN_SAMPLES", cls.min_samples)),
            decide_seconds=float(config.get("LIVENESS_DECIDE_SECONDS", cls.decide_seconds)),
            challenge_seconds=float(config.get("LIVENESS_CHALLENGE_SECONDS", cls.challenge_seconds)),
            challenge_delta=float(config.get("LIVENESS_CHALLENGE_DELTA", cls.challenge_delta)),
            mirrored=str(config.get("LIVENESS_MIRRORED", "false")).lower() in ("1", "true", "ya"),
        )


def passive_verdict(track, s):
    """LIVE / SPOOF / UNCERTAIN dari rata-rata skor jejak, atau None jika sampel kurang."""
    scores = list(track.live_scores)
    if len(scores) < s.min_samples:
        return None
    mean = sum(scores) / len(scores)
    if mean >= s.real_threshold:
        return LIVE
    if mean <= s.spoof_threshold:
        return SPOOF
    return UNCERTAIN


@dataclass
class _Pending:
    event: object
    since: float
    challenge: Challenge = None


@dataclass
class GateResult:
    accepted: list = field(default_factory=list)
    rejected: list = field(default_factory=list)   # (event, reason)
    prompts: dict = field(default_factory=dict)    # track_id -> instruksi


class LivenessGate:
    """Menahan peristiwa "identitas dipastikan" sampai keaktifan wajah lolos.

    Pengenalan menjawab "siapa"; gerbang ini menjawab "apakah orang asli".
    Absensi hanya boleh dicatat untuk peristiwa yang ada di GateResult.accepted.
    """

    def __init__(self, settings=None, rng=None):
        self.s = settings or LivenessSettings()
        self.rng = rng or random.Random()
        self.pending = {}
        self.rejected_tracks = set()
        self.passed = []   # skor keaktifan peristiwa yang diterima (untuk statistik)

    def _start_challenge(self, track, now):
        direction = self.rng.choice(("kiri", "kanan"))
        # Kamera tidak dicerminkan: menoleh ke kiri karyawan menggeser hidung ke
        # kanan gambar (+). Jika kamera dicerminkan, arahnya terbalik.
        sign = 1 if direction == "kiri" else -1
        if self.s.mirrored:
            sign = -sign
        recent = [y for _, y in list(track.yaw_history)[-5:]] or [0.0]
        return Challenge(direction, sign, float(np.median(recent)), now)

    def _challenge_passed(self, track, ch):
        return any(
            (yaw - ch.baseline) * ch.sign >= self.s.challenge_delta
            for t, yaw in track.yaw_history if t >= ch.started_at
        )

    def update(self, tracks_by_id, events, now):
        result = GateResult()
        for event in events:
            # Semua peristiwa diperiksa, termasuk yang dalam masa jeda: wajah
            # yang ditampilkan ke kamera harus selalu dinilai asli/palsu.
            if self.s.mode == MODE_OFF:
                result.accepted.append(event)
            else:
                self.pending[event.track_id] = _Pending(event, now)

        for track_id, p in list(self.pending.items()):
            track = tracks_by_id.get(track_id)
            if track is None:
                # Orangnya sudah pergi sebelum keaktifan dipastikan: tidak dicatat.
                if now - p.since > 1.0:
                    del self.pending[track_id]
                continue

            verdict = passive_verdict(track, self.s) if self.s.passive else UNCERTAIN
            waited = now - p.since

            if verdict == SPOOF:
                self._reject(result, track_id, p, REASON_SPOOF)
                continue
            if verdict == LIVE and p.challenge is None:
                self._accept(result, track_id, p, track)
                continue
            if verdict is None and waited < self.s.decide_seconds:
                continue  # tunggu sampel cukup

            # Ragu (atau sampel tak kunjung cukup).
            if not self.s.challenge:
                if waited >= self.s.decide_seconds:
                    self._reject(result, track_id, p, REASON_UNCERTAIN)
                continue
            if p.challenge is None:
                p.challenge = self._start_challenge(track, now)
            if self._challenge_passed(track, p.challenge):
                self._accept(result, track_id, p, track)
            elif now - p.challenge.started_at >= self.s.challenge_seconds:
                self._reject(result, track_id, p, REASON_CHALLENGE)
            else:
                result.prompts[track_id] = p.challenge.instruction
        return result

    def _accept(self, result, track_id, p, track):
        scores = list(track.live_scores)
        if scores:
            self.passed.append(sum(scores) / len(scores))
        result.accepted.append(p.event)
        del self.pending[track_id]

    def _reject(self, result, track_id, p, reason):
        result.rejected.append((p.event, reason))
        self.rejected_tracks.add(track_id)
        del self.pending[track_id]
