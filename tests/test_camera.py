"""Pengujian lapisan kamera (FR-04, TC-16) memakai kamera tiruan.

Tes ini tidak membutuhkan webcam. Pengujian dengan webcam asli dilakukan
melalui perintah: flask --app app camera-check
"""
import cv2
import numpy as np
import pytest

from camera import webcam
from camera.webcam import CameraError, CameraSettings, Webcam


def _frame(value=120, w=1280, h=720):
    return np.full((h, w, 3), value, dtype=np.uint8)


class FakeCapture:
    """Meniru cv2.VideoCapture. `script` berisi frame atau None (gagal baca)."""

    def __init__(self, opened=True, script=None, default=None):
        self.opened = opened
        self.script = list(script or [])
        self.default = _frame() if default is None else default
        self.props = {}
        self.released = False

    def isOpened(self):
        return self.opened and not self.released

    def set(self, prop, value):
        self.props[prop] = value
        return True

    def get(self, prop):
        return self.props.get(prop, 0)

    def read(self):
        item = self.script.pop(0) if self.script else self.default
        if item is None:
            return False, None
        return True, item

    def release(self):
        self.released = True


class PickyCapture(FakeCapture):
    """Driver yang hanya menerima MJPG jika `accepts(urutan_set)` bernilai True.

    Jika ditolak, FOURCC yang dilaporkan tetap YUY2 seperti C270 di DirectShow.
    """

    MJPG = cv2.VideoWriter_fourcc(*"MJPG")
    YUY2 = cv2.VideoWriter_fourcc(*"YUY2")

    def __init__(self, accepts, **kw):
        super().__init__(**kw)
        self.accepts = accepts
        self.order = []

    def set(self, prop, value):
        self.order.append(prop)
        return super().set(prop, value)

    def get(self, prop):
        if prop == cv2.CAP_PROP_FOURCC:
            return self.MJPG if self.accepts(self.order) else self.YUY2
        return super().get(prop)


def size_before_fourcc(order):
    return (
        cv2.CAP_PROP_FOURCC in order
        and order.index(cv2.CAP_PROP_FRAME_WIDTH) < order.index(cv2.CAP_PROP_FOURCC)
    )


def fourcc_after_fps(order):
    return (
        cv2.CAP_PROP_FPS in order
        and len(order) - 1 - order[::-1].index(cv2.CAP_PROP_FOURCC)
        > order.index(cv2.CAP_PROP_FPS)
    )


def factory_returning(*captures):
    queue = list(captures)
    calls = []

    def factory(index, backend):
        calls.append((index, backend))
        return queue.pop(0)

    factory.calls = calls
    return factory


class FakeClock:
    def __init__(self, step=1 / 30):
        self.t = 0.0
        self.step = step

    def __call__(self):
        self.t += self.step
        return self.t


def test_buka_kamera_mengatur_mjpg_dan_resolusi():
    cap = FakeCapture()
    cam = Webcam(CameraSettings(backend="dshow"), capture_factory=factory_returning(cap)).open()
    assert cam.is_open
    assert cap.props[cv2.CAP_PROP_FOURCC] == cv2.VideoWriter_fourcc(*"MJPG")
    assert cap.props[cv2.CAP_PROP_FRAME_WIDTH] == 1280
    assert cap.props[cv2.CAP_PROP_FRAME_HEIGHT] == 720
    assert cam.actual["width"] == 1280 and cam.actual["height"] == 720


def test_mjpg_diterima_dengan_urutan_pertama():
    cap = PickyCapture(accepts=lambda order: True)
    factory = factory_returning(cap)
    cam = Webcam(CameraSettings(warmup_frames=1), capture_factory=factory).open()
    assert cam.actual["format_ok"] is True
    assert cam.actual["strategy"] == "format-dulu"
    assert len(factory.calls) == 1


def test_driver_yang_butuh_resolusi_dulu():
    """Seperti C270 di beberapa komputer: MJPG hanya diterima setelah resolusi."""
    caps = [PickyCapture(size_before_fourcc) for _ in range(3)]
    factory = factory_returning(*caps)
    cam = Webcam(CameraSettings(warmup_frames=1), capture_factory=factory).open()
    assert cam.actual["fourcc"] == "MJPG"
    assert cam.actual["strategy"] == "resolusi-dulu"
    assert caps[0].released            # percobaan pertama dilepas
    assert not caps[1].released        # yang berhasil tetap dipakai
    assert len(factory.calls) == 2


def test_driver_yang_butuh_format_diatur_ulang():
    caps = [PickyCapture(fourcc_after_fps) for _ in range(3)]
    cam = Webcam(CameraSettings(warmup_frames=1), capture_factory=factory_returning(*caps)).open()
    assert cam.actual["format_ok"] is True
    assert cam.actual["strategy"] == "format-dua-kali"


def test_driver_menolak_mjpg_tetap_bisa_dipakai():
    caps = [PickyCapture(lambda order: False) for _ in range(3)]
    cam = Webcam(CameraSettings(warmup_frames=1), capture_factory=factory_returning(*caps)).open()
    assert cam.is_open
    assert cam.actual["format_ok"] is False
    assert cam.actual["fourcc"] == "YUY2"
    assert caps[0].released and caps[1].released and not caps[2].released


def test_memakai_backend_dshow():
    factory = factory_returning(FakeCapture())
    Webcam(CameraSettings(index=1, backend="dshow"), capture_factory=factory).open()
    assert factory.calls == [(1, cv2.CAP_DSHOW)]


def test_backend_tidak_dikenal_ditolak():
    with pytest.raises(CameraError, match="tidak dikenal"):
        Webcam(CameraSettings(backend="xyz"))


def test_kamera_tidak_ditemukan_memberi_pesan_jelas():
    """TC-16: kamera tidak ada atau dipakai aplikasi lain."""
    cap = FakeCapture(opened=False)
    with pytest.raises(CameraError, match="tidak sedang dipakai aplikasi lain"):
        Webcam(capture_factory=factory_returning(cap)).open()
    assert cap.released


def test_kamera_terbuka_tapi_tidak_mengirim_gambar():
    cap = FakeCapture(script=[None] * 10, default=None)
    cap.default = None
    cam = Webcam(CameraSettings(warmup_frames=3), capture_factory=factory_returning(cap))
    with pytest.raises(CameraError, match="tidak mengirim gambar"):
        cam.open()
    assert not cam.is_open


def test_frame_awal_gagal_masih_bisa_dibuka():
    cap = FakeCapture(script=[None, None, _frame()])
    cam = Webcam(CameraSettings(warmup_frames=3), capture_factory=factory_returning(cap)).open()
    assert cam.is_open


def test_gagal_sesekali_mengembalikan_none_tanpa_error():
    cap = FakeCapture(script=[_frame()] * 5 + [None, _frame()])
    cam = Webcam(CameraSettings(warmup_frames=5), capture_factory=factory_returning(cap)).open()
    assert cam.read() is None
    assert cam.read() is not None
    assert cam.consecutive_failures == 0
    assert cam.total_failures == 1


def test_kabel_tercabut_terdeteksi_setelah_gagal_berturut_turut():
    """TC-16: kamera terputus di tengah jalan."""
    cap = FakeCapture(script=[_frame()] + [None] * 10)
    cam = Webcam(
        CameraSettings(warmup_frames=1, max_consecutive_failures=5),
        capture_factory=factory_returning(cap),
    ).open()
    for _ in range(4):
        assert cam.read() is None
    with pytest.raises(CameraError, match="berhenti mengirim gambar"):
        cam.read()


def test_buka_ulang_setelah_gangguan():
    broken = FakeCapture()
    later_ok = FakeCapture()
    sleeps = []
    cam = Webcam(
        CameraSettings(warmup_frames=1),
        capture_factory=factory_returning(broken, FakeCapture(opened=False), later_ok),
        sleep=sleeps.append,
    ).open()
    cam.reopen(attempts=3, delay=2.0)
    assert broken.released
    assert cam.is_open
    assert sleeps == [2.0, 2.0]


def test_buka_ulang_menyerah_setelah_batas_percobaan():
    cam = Webcam(
        CameraSettings(warmup_frames=1),
        capture_factory=factory_returning(FakeCapture(), *[FakeCapture(opened=False)] * 3),
        sleep=lambda s: None,
    ).open()
    with pytest.raises(CameraError, match="gagal dibuka ulang"):
        cam.reopen(attempts=3)
    assert not cam.is_open


def test_context_manager_selalu_melepas_kamera():
    cap = FakeCapture()
    with pytest.raises(RuntimeError):
        with Webcam(capture_factory=factory_returning(cap)):
            raise RuntimeError("galat di tengah proses")
    assert cap.released


def test_baca_sebelum_dibuka_ditolak():
    with pytest.raises(CameraError, match="belum dibuka"):
        Webcam().read()


@pytest.mark.parametrize(
    "value,advice",
    [(2, "hampir hitam"), (20, "Terlalu gelap"), (120, "cukup"), (240, "Terlalu terang")],
)
def test_saran_pencahayaan(value, advice):
    brightness, _ = webcam.frame_quality(_frame(value, 64, 48))
    assert advice in webcam.lighting_advice(brightness)


def test_ketajaman_gambar_buram_lebih_rendah():
    rng = np.random.default_rng(0)
    sharp = rng.integers(0, 255, (120, 160, 3), dtype=np.uint8)
    blurry = cv2.GaussianBlur(sharp, (21, 21), 0)
    assert webcam.frame_quality(sharp)[1] > webcam.frame_quality(blurry)[1] * 5


def test_measure_menghitung_fps_dan_kegagalan():
    cap = FakeCapture(script=[_frame()] + [_frame(), None] * 50)
    cam = Webcam(CameraSettings(warmup_frames=1), capture_factory=factory_returning(cap)).open()
    # 30 bacaan per detik, separuhnya gagal -> sekitar 15 frame per detik.
    stats = webcam.measure(cam, seconds=2.0, clock=FakeClock(step=1 / 60))
    assert stats["frames"] > 0
    assert stats["failures"] > 0
    assert 12 <= stats["fps"] <= 18
    assert stats["lighting"] == "Pencahayaan cukup."


def test_measure_bisa_dihentikan_lebih_awal():
    cam = Webcam(CameraSettings(warmup_frames=1), capture_factory=factory_returning(FakeCapture())).open()
    seen = []

    def stop_after_three(frame, live):
        seen.append(live)
        return len(seen) < 3

    stats = webcam.measure(cam, seconds=100, on_frame=stop_after_three, clock=FakeClock())
    assert stats["frames"] == 3


def test_scan_menemukan_kamera_yang_tersedia():
    caps = [FakeCapture(opened=False), FakeCapture(), FakeCapture(opened=False)]
    found = webcam.scan(max_index=2, backend="any", capture_factory=factory_returning(*caps))
    assert [c["index"] for c in found] == [1]
    assert all(c.released for c in caps)


def test_pengaturan_dibaca_dari_config(app):
    s = CameraSettings.from_config(app.config)
    assert (s.width, s.height, s.fps) == (1280, 720, 30)


def test_cli_camera_check_tanpa_kamera_memberi_pesan(app, monkeypatch):
    monkeypatch.setattr(webcam.cv2, "VideoCapture", lambda i, b: FakeCapture(opened=False))
    res = app.test_cli_runner().invoke(args=["camera-check", "--no-preview", "--seconds", "1"])
    assert res.exit_code != 0
    assert "tidak dapat dibuka" in res.output


def test_cli_camera_check_dengan_kamera_tiruan(app, monkeypatch):
    monkeypatch.setattr(webcam.cv2, "VideoCapture", lambda i, b: FakeCapture())
    res = app.test_cli_runner().invoke(args=["camera-check", "--no-preview", "--seconds", "1"])
    assert res.exit_code == 0, res.output
    assert "1280x720" in res.output
    assert "Hasil pengujian kamera" in res.output


def test_cli_camera_scan(app, monkeypatch):
    caps = iter([FakeCapture(opened=False), FakeCapture()] + [FakeCapture(opened=False)] * 3)
    monkeypatch.setattr(webcam.cv2, "VideoCapture", lambda i, b: next(caps))
    res = app.test_cli_runner().invoke(args=["camera-scan", "--backend", "any"])
    assert res.exit_code == 0, res.output
    assert "Indeks 1: gambar awal 1280x720" in res.output


def test_cli_gambar_hitam_tidak_dinyatakan_siap(app, monkeypatch):
    """Kecerahan hampir nol tidak boleh menghasilkan pesan 'siap'."""
    monkeypatch.setattr(webcam.cv2, "VideoCapture", lambda i, b: FakeCapture(default=_frame(1)))
    res = app.test_cli_runner().invoke(args=["camera-check", "--no-preview", "--seconds", "1"])
    assert "Perlu perhatian" in res.output
    assert "hampir hitam" in res.output
    assert "siap untuk tahap deteksi wajah" not in res.output


def test_cli_gambar_normal_dinyatakan_siap(app, monkeypatch):
    monkeypatch.setattr(webcam.cv2, "VideoCapture", lambda i, b: FakeCapture(default=_frame(120)))
    res = app.test_cli_runner().invoke(args=["camera-check", "--no-preview", "--seconds", "1"])
    assert "siap untuk tahap deteksi wajah" in res.output


def test_cli_melaporkan_format_ditolak(app, monkeypatch):
    monkeypatch.setattr(webcam.cv2, "VideoCapture",
                        lambda i, b: PickyCapture(lambda order: False))
    res = app.test_cli_runner().invoke(args=["camera-check", "--no-preview", "--seconds", "1"])
    assert "Driver menolak format MJPG" in res.output
    assert "Laporan format dari DirectShow tidak selalu akurat" not in res.output


def test_cli_opsi_resolusi_sementara(app, monkeypatch):
    sizes = []

    def factory(i, b):
        cap = FakeCapture(default=_frame(120, 640, 480))
        sizes.append(cap)
        return cap

    monkeypatch.setattr(webcam.cv2, "VideoCapture", factory)
    res = app.test_cli_runner().invoke(
        args=["camera-check", "--no-preview", "--seconds", "1", "--width", "640", "--height", "480"]
    )
    assert "target 640x480" in res.output
    assert sizes[0].props[cv2.CAP_PROP_FRAME_WIDTH] == 640
    assert "Resolusi yang didapat" not in res.output


# ---------------------------------------------------------------- pilih berdasarkan nama

from camera import devices  # noqa: E402

# Daftar kamera seperti di komputer pengembang (urutan indeks DirectShow).
DEV_CAMERAS = ["Hy FHD5350 Camera", "V2317 (Windows Virtual Camera)", "Logi C270 HD WebCam"]


def test_cari_indeks_berdasarkan_sebagian_nama():
    assert devices.find_index("C270", DEV_CAMERAS) == 2
    assert devices.find_index("c270", DEV_CAMERAS) == 2
    assert devices.find_index("FHD5350", DEV_CAMERAS) == 0


def test_nama_tidak_ditemukan_menampilkan_daftar_kamera():
    with pytest.raises(devices.CameraLookupError) as exc:
        devices.find_index("C920", DEV_CAMERAS)
    assert "2: Logi C270 HD WebCam" in str(exc.value)


def test_nama_yang_tidak_unik_ditolak():
    with pytest.raises(devices.CameraLookupError, match="lebih dari satu"):
        devices.find_index("Camera", DEV_CAMERAS)


def test_list_cameras_bukan_windows_mengembalikan_none(monkeypatch):
    monkeypatch.setattr(devices.platform, "system", lambda: "Linux")
    assert devices.list_cameras() is None


def test_list_cameras_windows_tanpa_pustaka_mengembalikan_none(monkeypatch):
    monkeypatch.setattr(devices.platform, "system", lambda: "Windows")

    def broken():
        raise ImportError("pygrabber tidak terpasang")

    monkeypatch.setattr(devices, "_enumerate", broken)
    assert devices.list_cameras() is None


def test_webcam_membuka_c270_berdasarkan_nama():
    factory = factory_returning(FakeCapture())
    cam = Webcam(
        CameraSettings(name="C270", backend="dshow", warmup_frames=1),
        capture_factory=factory,
        list_cameras=lambda: DEV_CAMERAS,
    ).open()
    assert factory.calls == [(2, cv2.CAP_DSHOW)]
    assert cam.actual["device_name"] == "Logi C270 HD WebCam"
    assert cam.actual["index"] == 2


def test_indeks_dicari_ulang_saat_buka_ulang():
    """Jika C270 dicabut lalu dicolok lagi dan urutannya berubah, indeks ikut berubah."""
    lists = iter([DEV_CAMERAS, ["Logi C270 HD WebCam", "Hy FHD5350 Camera"]])
    factory = factory_returning(FakeCapture(), FakeCapture())
    cam = Webcam(
        CameraSettings(name="C270", backend="dshow", warmup_frames=1),
        capture_factory=factory,
        list_cameras=lambda: next(lists),
        sleep=lambda s: None,
    ).open()
    cam.reopen(attempts=1)
    assert [c[0] for c in factory.calls] == [2, 0]


def test_nama_tidak_bisa_dibaca_memakai_indeks_cadangan():
    factory = factory_returning(FakeCapture())
    cam = Webcam(
        CameraSettings(name="C270", index=1, backend="dshow", warmup_frames=1),
        capture_factory=factory,
        list_cameras=lambda: None,
    ).open()
    assert factory.calls == [(1, cv2.CAP_DSHOW)]
    assert "memakai CAMERA_INDEX=1" in cam.name_note


def test_nama_tidak_ditemukan_menjadi_camera_error():
    cam = Webcam(
        CameraSettings(name="C920", backend="dshow"),
        capture_factory=factory_returning(FakeCapture()),
        list_cameras=lambda: DEV_CAMERAS,
    )
    with pytest.raises(CameraError, match="Kamera yang terdeteksi"):
        cam.open()


def test_nama_hanya_untuk_backend_dshow():
    cam = Webcam(CameraSettings(name="C270", backend="msmf"),
                 capture_factory=factory_returning(FakeCapture()))
    with pytest.raises(CameraError, match="CAMERA_BACKEND=dshow"):
        cam.open()


def test_cli_scan_menampilkan_nama_dan_menandai_kamera_virtual(app, monkeypatch):
    monkeypatch.setattr(devices, "list_cameras", lambda: DEV_CAMERAS)
    res = app.test_cli_runner().invoke(args=["camera-scan", "--backend", "dshow"])
    assert res.exit_code == 0, res.output
    assert "Indeks 2: Logi C270 HD WebCam" in res.output
    assert "V2317 (Windows Virtual Camera)  <- kamera virtual" in res.output
    assert "CAMERA_NAME=C270" in res.output


def test_cli_check_dengan_nama(app, monkeypatch):
    opened = []
    monkeypatch.setattr(devices, "list_cameras", lambda: DEV_CAMERAS)
    monkeypatch.setattr(webcam.cv2, "VideoCapture",
                        lambda i, b: opened.append(i) or FakeCapture())
    res = app.test_cli_runner().invoke(
        args=["camera-check", "--no-preview", "--seconds", "1", "--backend", "dshow", "--name", "C270"]
    )
    assert res.exit_code == 0, res.output
    assert "Kamera: Logi C270 HD WebCam (indeks 2)" in res.output
    assert opened[0] == 2
