"""
Lapisan kamera (PRD 7.1, FR-04).

Tanggung jawab modul ini hanya urusan kamera: membuka perangkat, mengatur
resolusi dan format, membaca frame, menghitung kegagalan, membuka ulang setelah
gangguan, dan melepas perangkat. Modul ini TIDAK menyimpan gambar ke disk
(PRD 9.2: gambar mentah tidak disimpan tanpa kebutuhan yang jelas).

Catatan untuk Logitech C270 di Windows:
- Backend DirectShow ("dshow") membuka kamera jauh lebih cepat daripada MSMF.
  Pada pengujian di komputer pengembang, backend MSMF berhenti tanpa pesan.
- Format MJPG dibutuhkan agar 1280x720 dapat mendekati 30 FPS. Dengan YUY2,
  C270 hanya sekitar 10 FPS di 1280x720 (terukur 10,0 FPS), tetapi bisa
  30 FPS di 640x480.
"""
import platform
import time
from dataclasses import dataclass

import cv2
import numpy as np

from camera import devices

BACKENDS = {
    "dshow": cv2.CAP_DSHOW,
    "msmf": cv2.CAP_MSMF,
    "any": cv2.CAP_ANY,
}

DEFAULT_BACKEND = "dshow" if platform.system() == "Windows" else "any"

# Batas kualitas cahaya untuk pemeriksaan awal (rata-rata skala abu 0-255).
BLACK_THRESHOLD = 10
DARK_THRESHOLD = 60
BRIGHT_THRESHOLD = 200


class CameraError(Exception):
    """Kamera tidak ditemukan, sedang dipakai, atau berhenti mengirim gambar."""


@dataclass
class CameraSettings:
    index: int = 0
    name: str = ""  # jika diisi, indeks dicari dari nama (khusus backend dshow)
    backend: str = DEFAULT_BACKEND
    width: int = 1280
    height: int = 720
    fps: int = 30
    fourcc: str = "MJPG"
    warmup_frames: int = 5
    max_consecutive_failures: int = 30

    @classmethod
    def from_config(cls, config):
        return cls(
            index=int(config.get("CAMERA_INDEX", 0)),
            name=str(config.get("CAMERA_NAME") or "").strip(),
            backend=str(config.get("CAMERA_BACKEND", DEFAULT_BACKEND)).lower(),
            width=int(config.get("CAMERA_WIDTH", 1280)),
            height=int(config.get("CAMERA_HEIGHT", 720)),
            fps=int(config.get("CAMERA_FPS", 30)),
        )


def _decode_fourcc(value):
    code = int(value)
    if code <= 0:
        return "?"
    return "".join(chr((code >> 8 * i) & 0xFF) for i in range(4)).strip() or "?"


class Webcam:
    """Pembungkus cv2.VideoCapture dengan penanganan gangguan.

    capture_factory dapat diganti saat pengujian agar tidak butuh kamera asli.
    """

    def __init__(self, settings=None, capture_factory=None, sleep=time.sleep,
                 list_cameras=None):
        self.settings = settings or CameraSettings()
        if self.settings.backend not in BACKENDS:
            raise CameraError(
                f"Backend kamera '{self.settings.backend}' tidak dikenal. "
                f"Pilihan: {', '.join(BACKENDS)}."
            )
        self._factory = capture_factory or (lambda index, api: cv2.VideoCapture(index, api))
        self._list_cameras = list_cameras or devices.list_cameras
        self.device_name = None
        self.name_note = None
        self._sleep = sleep
        self._cap = None
        self.consecutive_failures = 0
        self.total_failures = 0
        self.actual = {}

    # -------------------------------------------------------------- siklus

    @property
    def is_open(self):
        return self._cap is not None and self._cap.isOpened()

    # Urutan pengaturan yang dicoba. Driver DirectShow berbeda-beda: ada yang
    # hanya menerima MJPG jika diatur sebelum resolusi, ada yang sesudahnya,
    # dan ada yang harus diatur dua kali.
    FORMAT_STRATEGIES = ("format-dulu", "resolusi-dulu", "format-dua-kali")

    def _resolve_index(self):
        """Menentukan indeks kamera. Jika CAMERA_NAME diisi, indeks dicari ulang
        setiap kali kamera dibuka, sehingga tetap benar walau urutan berubah."""
        s = self.settings
        self.name_note = None
        if not s.name:
            return s.index
        if s.backend != "dshow":
            raise CameraError("CAMERA_NAME hanya dapat dipakai dengan CAMERA_BACKEND=dshow.")
        cameras = self._list_cameras()
        if cameras is None:
            self.name_note = (
                f"Daftar nama kamera tidak dapat dibaca; memakai CAMERA_INDEX={s.index}."
            )
            return s.index
        try:
            index = devices.find_index(s.name, cameras)
        except devices.CameraLookupError as exc:
            raise CameraError(str(exc)) from exc
        self.device_name = cameras[index]
        return index

    def _connect(self):
        s = self.settings
        index = self._resolve_index()
        s.index = index
        cap = self._factory(index, BACKENDS[s.backend])
        if cap is None or not cap.isOpened():
            if cap is not None:
                cap.release()
            raise CameraError(
                f"Kamera indeks {s.index} tidak dapat dibuka. Pastikan webcam "
                "terpasang dan tidak sedang dipakai aplikasi lain (Zoom, Teams, "
                "aplikasi Kamera Windows, Windows Settings, atau jendela aplikasi "
                "ini yang lain)."
            )
        return cap

    def _configure(self, cap, strategy):
        s = self.settings
        fourcc = cv2.VideoWriter_fourcc(*s.fourcc)

        def size():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, s.width)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, s.height)

        if strategy == "resolusi-dulu":
            size()
            cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        else:
            cap.set(cv2.CAP_PROP_FOURCC, fourcc)
            size()
        cap.set(cv2.CAP_PROP_FPS, s.fps)
        if strategy == "format-dua-kali":
            cap.set(cv2.CAP_PROP_FOURCC, fourcc)
        cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)  # kurangi jeda: ambil frame terbaru

    def open(self):
        """Membuka kamera dan berusaha mendapatkan format yang diminta (MJPG).

        Jika driver menolak format dengan satu urutan pengaturan, kamera
        dibuka ulang dengan urutan lain. Jika semua urutan gagal, kamera tetap
        dipakai dengan format bawaan driver dan hal itu dilaporkan di
        self.actual["format_ok"].
        """
        s = self.settings
        strategy_used = None
        for strategy in self.FORMAT_STRATEGIES:
            cap = self._connect()
            self._configure(cap, strategy)
            got = _decode_fourcc(cap.get(cv2.CAP_PROP_FOURCC))
            if got == s.fourcc or strategy == self.FORMAT_STRATEGIES[-1]:
                strategy_used = strategy
                break
            cap.release()
        self._cap = cap

        # Beberapa frame pertama sering gelap/kosong saat kamera menyesuaikan cahaya.
        frame = None
        for _ in range(max(1, s.warmup_frames)):
            ok, frame = cap.read()
            if not ok:
                frame = None
        if frame is None:
            self.release()
            raise CameraError(
                f"Kamera indeks {s.index} terbuka tetapi tidak mengirim gambar."
            )

        fourcc = _decode_fourcc(cap.get(cv2.CAP_PROP_FOURCC))
        self.actual = {
            "width": int(frame.shape[1]),
            "height": int(frame.shape[0]),
            "fps_reported": float(cap.get(cv2.CAP_PROP_FPS) or 0),
            "fourcc": fourcc,
            "format_ok": fourcc == s.fourcc,
            "strategy": strategy_used,
            "backend": s.backend,
            "index": s.index,
            "device_name": self.device_name,
        }
        self.consecutive_failures = 0
        return self

    def read(self):
        """Mengembalikan frame (array BGR) atau None jika gagal.

        Melempar CameraError jika gagal berturut-turut melewati batas, agar
        pemanggil tahu kamera kemungkinan terputus.
        """
        if not self.is_open:
            raise CameraError("Kamera belum dibuka.")
        ok, frame = self._cap.read()
        if not ok or frame is None:
            self.consecutive_failures += 1
            self.total_failures += 1
            if self.consecutive_failures >= self.settings.max_consecutive_failures:
                raise CameraError(
                    f"Kamera berhenti mengirim gambar ({self.consecutive_failures} "
                    "kali gagal berturut-turut). Periksa kabel USB."
                )
            return None
        self.consecutive_failures = 0
        return frame

    def reopen(self, attempts=3, delay=2.0):
        """Membuka ulang kamera setelah gangguan (FR-04)."""
        last_error = None
        for _ in range(attempts):
            self.release()
            self._sleep(delay)
            try:
                return self.open()
            except CameraError as exc:
                last_error = exc
        raise CameraError(f"Kamera gagal dibuka ulang setelah {attempts} percobaan.") from last_error

    def release(self):
        if self._cap is not None:
            self._cap.release()
        self._cap = None

    def __enter__(self):
        return self.open()

    def __exit__(self, *exc):
        self.release()
        return False


# ------------------------------------------------------------------ kualitas

def frame_quality(frame):
    """Kecerahan rata-rata (0-255) dan ketajaman (varian Laplacian)."""
    gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
    brightness = float(np.mean(gray))
    sharpness = float(cv2.Laplacian(gray, cv2.CV_64F).var())
    return brightness, sharpness


def lighting_advice(brightness):
    if brightness < BLACK_THRESHOLD:
        return (
            "Gambar hampir hitam. Lensa mungkin tertutup, penutup privasi kamera "
            "masih terpasang, atau ini kamera inframerah (Windows Hello), bukan C270."
        )
    if brightness < DARK_THRESHOLD:
        return "Terlalu gelap. Tambah cahaya dari arah depan wajah."
    if brightness > BRIGHT_THRESHOLD:
        return (
            "Terlalu terang. Hindari cahaya langsung atau jendela di belakang "
            "orang yang berdiri di depan kamera."
        )
    return "Pencahayaan cukup."


def measure(webcam, seconds=10.0, on_frame=None, clock=time.perf_counter):
    """Membaca frame selama `seconds` detik dan mengembalikan statistik.

    on_frame(frame, stats_sementara) dipanggil setiap frame; jika mengembalikan
    False, pengukuran dihentikan lebih awal (mis. pengguna menekan Q).
    """
    start = clock()
    frames = failures = 0
    brightness_sum = sharpness_sum = 0.0
    max_gap = 0.0
    last = start

    while clock() - start < seconds:
        frame = webcam.read()
        now = clock()
        if frame is None:
            failures += 1
            continue
        frames += 1
        max_gap = max(max_gap, now - last)
        last = now
        b, sh = frame_quality(frame)
        brightness_sum += b
        sharpness_sum += sh
        if on_frame is not None:
            elapsed = max(now - start, 1e-6)
            if on_frame(frame, {"fps": frames / elapsed, "brightness": b}) is False:
                break

    elapsed = max(clock() - start, 1e-6)
    brightness = brightness_sum / frames if frames else 0.0
    return {
        "seconds": round(elapsed, 1),
        "frames": frames,
        "failures": failures,
        "fps": round(frames / elapsed, 1),
        "max_gap_ms": round(max_gap * 1000),
        "brightness": round(brightness, 1),
        "sharpness": round(sharpness_sum / frames, 1) if frames else 0.0,
        "lighting": lighting_advice(brightness) if frames else "Tidak ada frame.",
    }


def scan(max_index=4, backend=DEFAULT_BACKEND, capture_factory=None):
    """Mencari indeks kamera yang dapat dibuka (0 sampai max_index)."""
    capture_factory = capture_factory or (lambda index, api: cv2.VideoCapture(index, api))
    found = []
    for index in range(max_index + 1):
        cap = capture_factory(index, BACKENDS[backend])
        try:
            if cap is not None and cap.isOpened():
                ok, frame = cap.read()
                if ok and frame is not None:
                    found.append({"index": index, "width": frame.shape[1], "height": frame.shape[0]})
        finally:
            if cap is not None:
                cap.release()
    return found
