"""
Layanan kamera kios (PRD 6.2, FR-04, FR-07, FR-08, FR-09).

Alur setiap frame:
  kamera -> deteksi -> kualitas -> embedding -> pencocokan -> pelacakan
  -> (identitas dipastikan) -> layanan absensi -> database -> pesan kios

Prinsip:
- Wajah terdeteksi BUKAN absensi. Absensi hanya lewat attendance_service.record().
- Pesan berhasil hanya tampil jika Decision.recorded (transaksi tersimpan).
- Kamera atau database bermasalah: status berubah, tidak ada absensi palsu.
- Tidak ada gambar yang disimpan.
"""
import logging
import time

from sqlalchemy.exc import SQLAlchemyError

from camera.webcam import CameraError
from extensions import db
from kiosk import state as ks
from recognition.identify import process_frame
from recognition.matcher import AMBIGUOUS, UNKNOWN
from services import attendance_service as att
from services import biometric_service as biometric
from tracking.face_tracker import FaceTracker

log = logging.getLogger("kiosk")

KIND_BY_ACTION = {
    att.CHECKED_IN: "success",
    att.CHECKED_OUT: "success",
    att.CHECKOUT_UPDATED: "success",
    att.ALREADY_IN: "info",
    att.ALREADY_OUT: "info",
    att.ERROR: "error",
}

UNKNOWN_MESSAGE = "Wajah tidak dikenali. Coba lagi, atau hubungi admin."
AMBIGUOUS_MESSAGE = "Identitas belum dapat dipastikan. Silakan coba lagi."


class KioskWorker:
    def __init__(self, app, camera, detector, embedder, quality, matcher_factory,
                 tracker_settings, attendance_settings, state_store,
                 template_reload_seconds=60.0, reopen_seconds=5.0,
                 clock=time.monotonic, wall_clock=time.time, on_frame=None):
        self.app = app
        self.camera = camera
        self.detector = detector
        self.embedder = embedder
        self.quality = quality
        self.matcher_factory = matcher_factory
        self.tracker = FaceTracker(tracker_settings)
        self.attendance_settings = attendance_settings
        self.state = state_store
        self.template_reload_seconds = template_reload_seconds
        self.reopen_seconds = reopen_seconds
        self.clock = clock
        self.wall_clock = wall_clock
        self.on_frame = on_frame

        self.matcher = None
        self._loaded_at = -1e9
        self._last_publish = -1e9
        self._last_reopen = -1e9
        self._announced = set()  # jejak yang sudah diberi pesan tidak dikenal/ragu
        self.camera_status = ks.CAMERA_STARTING
        self.detail = ""
        self.message = None
        self.hint = None
        self.recorded = 0
        self.publish_skipped = 0
        self.unexpected_errors = 0
        self.decisions = []  # riwayat singkat untuk pengujian dan log

    # ------------------------------------------------------------ pembantu

    def _end_read_transaction(self):
        # Penting untuk MySQL: transaksi baca yang terbuka lama melihat data basi.
        db.session.rollback()

    def reload_templates(self, force=False):
        now = self.clock()
        if not force and now - self._loaded_at < self.template_reload_seconds:
            return
        try:
            self.matcher = self.matcher_factory()
            self.detail = ""
            self._loaded_at = now
        except biometric.BiometricKeyError as exc:
            self.matcher = None
            self.detail = str(exc)
            self._loaded_at = now
            log.error("Templat tidak dapat dimuat: %s", exc)
        except SQLAlchemyError:
            # Database terputus sesaat: tetap pakai daftar wajah terakhir yang
            # berhasil dimuat, lalu coba lagi beberapa detik kemudian.
            self.detail = "Database tidak dapat dihubungi; mencoba lagi."
            self._loaded_at = now - self.template_reload_seconds + self.reopen_seconds
            log.error("Database tidak dapat dihubungi saat memuat templat wajah.")
        finally:
            try:
                self._end_read_transaction()
            except SQLAlchemyError:
                pass

    def _say(self, kind, text):
        self.message = {"kind": kind, "text": text, "at": self.wall_clock()}

    def publish(self, force=False):
        now = self.clock()
        if not force and now - self._last_publish < 0.5:
            return
        self._last_publish = now
        ok = self.state.publish({
            "camera": self.camera_status,
            "detail": self.detail,
            "message": self.message,
            "hint": self.hint,
            "employees": self.matcher.employee_count if self.matcher else 0,
            "recorded": self.recorded,
            "liveness": False,  # FR-11 belum aktif (Tahap 4D)
        }, clock=self.wall_clock)
        if not ok:
            self.publish_skipped += 1
            log.warning("Pembaruan status kios dilewati (file sedang dipakai proses lain).")

    # ------------------------------------------------------------ satu langkah

    def handle_event(self, event):
        """Identitas dipastikan pelacak -> minta keputusan absensi."""
        if event.in_cooldown:
            return None
        decision = att.record(event.employee_id, score=event.score,
                              settings=self.attendance_settings)
        self.decisions.append(decision)
        if decision.recorded:
            self.recorded += 1
        kind = KIND_BY_ACTION.get(decision.action, "warning")
        self._say(kind, decision.message)
        log.info("Absensi karyawan id=%s: %s", decision.employee_id, decision.action)
        return decision

    def step(self, frame):
        """Memproses satu frame. Mengembalikan daftar jejak (untuk pratinjau)."""
        if self.matcher is None or self.matcher.template_count == 0:
            self.hint = None
            if not self.detail:
                self.detail = "Belum ada wajah karyawan yang terdaftar."
            return []

        tracks, events, _ = process_frame(
            frame, self.detector, self.embedder, self.matcher, self.quality,
            self.tracker, self.clock(),
        )
        for event in events:
            self.handle_event(event)

        self.hint = None
        for track in tracks:
            state = track.state
            if state in (UNKNOWN, AMBIGUOUS) and track.id not in self._announced:
                self._announced.add(track.id)
                self._say("warning", UNKNOWN_MESSAGE if state == UNKNOWN else AMBIGUOUS_MESSAGE)
            elif state == "pending" and track.last_reason:
                self.hint = track.last_reason
        return tracks

    # ------------------------------------------------------------ kamera

    def read_frame(self):
        try:
            frame = self.camera.read()
            if self.camera_status != ks.CAMERA_OK:
                self.camera_status = ks.CAMERA_OK
                self.detail = ""
            return frame
        except CameraError as exc:
            if self.camera_status != ks.CAMERA_ERROR:
                log.error("Kamera bermasalah: %s", exc)
            self.camera_status = ks.CAMERA_ERROR
            self.detail = str(exc)
            self.hint = None
            now = self.clock()
            if now - self._last_reopen >= self.reopen_seconds:
                self._last_reopen = now
                try:
                    self.camera.reopen(attempts=1, delay=0)
                    self.camera_status = ks.CAMERA_OK
                    self.detail = ""
                    log.info("Kamera tersambung kembali.")
                except CameraError:
                    pass
            return None

    def run_once(self):
        self.reload_templates()
        frame = self.read_frame()
        tracks = self.step(frame) if frame is not None else []
        self.publish()
        if self.on_frame is not None and frame is not None:
            return self.on_frame(frame, tracks)
        return True

    def run(self, should_stop=lambda: False, idle_sleep=time.sleep):
        """Perulangan utama. Menyiapkan konteks aplikasinya sendiri, sehingga
        aman dijalankan dari thread mana pun."""
        with self.app.app_context():
            try:
                self.reload_templates(force=True)
                self.publish(force=True)
                while not should_stop():
                    try:
                        if self.run_once() is False:
                            break
                    except Exception:
                        # Layanan absensi tidak boleh mati karena satu galat tak
                        # terduga. Setiap pencatatan absensi berjalan dalam
                        # transaksinya sendiri, jadi melanjutkan tetap aman.
                        self.unexpected_errors += 1
                        log.exception("Galat tak terduga di layanan kamera; dilanjutkan.")
                        db.session.rollback()
                        idle_sleep(1.0)
                        continue
                    if self.camera_status == ks.CAMERA_ERROR:
                        idle_sleep(0.5)
            finally:
                self.camera_status = "stopped"
                try:
                    self.publish(force=True)
                except Exception:
                    log.exception("Status akhir kios tidak dapat ditulis.")
                db.session.remove()
