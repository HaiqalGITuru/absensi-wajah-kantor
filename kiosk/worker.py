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
from recognition import enrollment
from recognition.identify import process_frame
from recognition.liveness import REASON_TEXT, LivenessGate, LivenessSettings
from recognition.matcher import AMBIGUOUS, UNKNOWN, Matcher
from services import ValidationError
from services import attendance_service as att
from services import audit_service as audit
from services import biometric_service as biometric
from services import face_enrollment_service as fe
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
LIVENESS_REJECT_MESSAGE = (
    "Wajah tidak dapat dipastikan sebagai orang asli. Absensi BELUM tercatat. "
    "Coba lagi tanpa kacamata gelap atau hubungi admin."
)
AMBIGUOUS_MESSAGE = "Identitas belum dapat dipastikan. Silakan coba lagi."

ENROLL_POLL_SECONDS = 1.5      # seberapa sering antrean pendaftaran diperiksa
ENROLL_TIMEOUT_SECONDS = 60.0  # batas waktu pengambilan sampel
ENROLL_PROGRESS_SECONDS = 1.0  # seberapa sering kemajuan ditulis ke database


class KioskWorker:
    def __init__(self, app, camera, detector, embedder, quality, matcher_factory,
                 tracker_settings, attendance_settings, state_store,
                 template_reload_seconds=60.0, reopen_seconds=5.0,
                 clock=time.monotonic, wall_clock=time.time, on_frame=None,
                 liveness=None, liveness_settings=None, match_threshold=0.6, match_margin=0.05,
                 enroll_poll_seconds=ENROLL_POLL_SECONDS, enroll_timeout=ENROLL_TIMEOUT_SECONDS,
                 enroll_start_delay=enrollment.START_DELAY):
        self.app = app
        self.camera = camera
        self.detector = detector
        self.embedder = embedder
        self.quality = quality
        self.matcher_factory = matcher_factory
        self.tracker_settings = tracker_settings
        self.tracker = FaceTracker(tracker_settings)
        self.attendance_settings = attendance_settings
        self.state = state_store
        self.template_reload_seconds = template_reload_seconds
        self.reopen_seconds = reopen_seconds
        self.clock = clock
        self.wall_clock = wall_clock
        self.on_frame = on_frame
        # Tahap 4D: anti-foto. Tanpa pengaturan, gerbang dalam mode "off".
        self.liveness = liveness
        self.liveness_settings = liveness_settings or LivenessSettings(mode="off")
        self.gate = LivenessGate(self.liveness_settings)
        self.liveness_rejected = 0

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
        # Pendaftaran wajah yang diminta dari panel admin lewat API.
        self.match_threshold = match_threshold
        self.match_margin = match_margin
        self.enroll_poll_seconds = enroll_poll_seconds
        self.enroll_timeout = enroll_timeout
        self.enroll_start_delay = enroll_start_delay
        self._last_enroll_poll = -1e9
        self.enrollments = []  # (id permintaan, status akhir) untuk pengujian dan log

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
            "liveness": self.liveness_settings.mode,
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

    def handle_rejection(self, event, reason):
        """Identitas cocok tetapi keaktifan wajah gagal: TIDAK ada absensi."""
        from models import Employee

        self.liveness_rejected += 1
        self._say("warning", LIVENESS_REJECT_MESSAGE)
        employee = db.session.get(Employee, event.employee_id)
        code = employee.employee_code if employee else f"id={event.employee_id}"
        audit.log_event(
            audit.LIVENESS_REJECTED,
            entity_type="employee",
            entity_id=event.employee_id,
            description=(
                f"Absensi {code} ditolak oleh pemeriksaan anti-foto: {REASON_TEXT[reason]} "
                f"(skor kecocokan wajah {event.score})."
            ),
        )
        log.warning("Anti-foto menolak karyawan id=%s: %s", event.employee_id, reason)

    def step(self, frame):
        """Memproses satu frame. Mengembalikan daftar jejak (untuk pratinjau)."""
        if self.matcher is None or self.matcher.template_count == 0:
            self.hint = None
            if not self.detail:
                self.detail = "Belum ada wajah karyawan yang terdaftar."
            return []

        now = self.clock()
        tracks, events, _ = process_frame(
            frame, self.detector, self.embedder, self.matcher, self.quality,
            self.tracker, now, liveness=self.liveness,
        )
        # Absensi hanya untuk peristiwa yang lolos pemeriksaan keaktifan (FR-11).
        gate = self.gate.update({t.id: t for t in self.tracker.tracks}, events, now)
        for event, reason in gate.rejected:
            self.handle_rejection(event, reason)
        for event in gate.accepted:
            self.handle_event(event)
        for instruction in gate.prompts.values():
            self._say("info", instruction)

        self.hint = None
        for track in tracks:
            state = track.state
            if state in (UNKNOWN, AMBIGUOUS) and track.id not in self._announced:
                self._announced.add(track.id)
                self._say("warning", UNKNOWN_MESSAGE if state == UNKNOWN else AMBIGUOUS_MESSAGE)
            elif state == "pending" and track.last_reason:
                self.hint = track.last_reason
        return tracks

    # ------------------------------------------------------------ pendaftaran wajah

    def check_enrollment(self):
        """Mengambil permintaan pendaftaran dari antrean (jika ada) lalu
        menjalankannya. Mengembalikan True jika satu pendaftaran dijalankan."""
        now = self.clock()
        if now - self._last_enroll_poll < self.enroll_poll_seconds:
            return False
        self._last_enroll_poll = now
        try:
            req = fe.claim_next()
        except SQLAlchemyError:
            db.session.rollback()
            return False
        if req is None:
            self._end_read_transaction()
            return False
        self.run_enrollment(req)
        return True

    def run_enrollment(self, req):
        employee = req.employee
        first = (employee.full_name or "").split()[0] if employee.full_name else "karyawan"
        label = f"{first} (kode ...{employee.employee_code[-4:]})"
        via = req.requested_via or ""
        live_scores = []
        cancelled = [False]
        last_progress = [-1e9]
        log.info("Pendaftaran wajah dimulai: permintaan #%s karyawan id=%s", req.id, employee.id)

        def on_frame(frame, status):
            self.camera_status = ks.CAMERA_OK
            faces = status["faces"]
            if self.liveness is not None and self.liveness_settings.passive and len(faces) == 1:
                det, quality = faces[0]
                if quality.ok:
                    live_scores.append(self.liveness.score(frame, det))
            self._say("info", f"Pendaftaran wajah: {label}. {status['message']}")
            self.hint = f"Sampel {status['taken']}/{status['samples']}"
            self.publish()
            now = self.clock()
            if now - last_progress[0] >= ENROLL_PROGRESS_SECONDS or status["taken"] == status["samples"]:
                last_progress[0] = now
                fe.update_progress(req, status["taken"], status["samples"], status["message"])
                if fe.is_cancelled(req):
                    cancelled[0] = True
                    return False
            return True

        outcome, message = "failed", ""
        try:
            result = enrollment.collect(
                self.camera, self.detector, self.embedder, self.quality,
                samples=req.samples_total, timeout=self.enroll_timeout, on_frame=on_frame,
                clock=self.clock, start_delay=self.enroll_start_delay,
            )
            enrollment.check_consistency(result.embeddings)

            # Anti-foto: pendaftaran dari jarak jauh tidak boleh memakai foto/layar.
            s = self.liveness_settings
            if self.liveness is not None and s.passive:
                mean = sum(live_scores) / len(live_scores) if live_scores else 0.0
                if len(live_scores) < s.min_samples or mean < s.real_threshold:
                    raise enrollment.EnrollmentError(
                        "Wajah tidak dapat dipastikan sebagai orang asli (bukan foto atau layar). "
                        "Ulangi dengan cahaya cukup dan tanpa kacamata gelap.")

            all_templates, _ = biometric.load_all_templates(self.embedder.model_name)
            matcher = Matcher(all_templates, threshold=self.match_threshold, margin=self.match_margin)
            conflict = enrollment.find_conflict(result.embeddings, matcher, employee.id)
            if conflict is not None:
                from models import Employee

                other = db.session.get(Employee, conflict.employee_id)
                who = f"{other.employee_code} - {other.full_name}" if other else "karyawan lain"
                raise enrollment.EnrollmentError(
                    f"Wajah ini mirip wajah {who} yang sudah terdaftar (skor {conflict.score:.2f}). "
                    "Tidak disimpan. Jika memang orang berbeda, daftarkan lewat terminal kios "
                    "(face-enroll --allow-similar).")

            saved = biometric.save_enrollment(
                employee, result.embeddings, self.embedder.model_name, None,
                replace_reason=req.replace_reason,
                note=f"[permintaan #{req.id} dari panel admin: {via.strip('[] ') or '-'}]",
            )
            outcome, message = "done", f"{len(saved)} sampel wajah tersimpan terenkripsi."
            self._say("success", f"Wajah {first} berhasil didaftarkan.")
        except enrollment.EnrollmentError as exc:
            db.session.rollback()
            if cancelled[0]:
                outcome, message = "cancelled", "Dibatalkan dari panel admin."
            else:
                message = str(exc)
        except CameraError as exc:
            db.session.rollback()
            message = f"Kamera bermasalah: {exc}"
        except ValidationError as exc:
            db.session.rollback()
            message = " ".join(exc.errors.values())
        except Exception:
            db.session.rollback()
            log.exception("Galat tak terduga saat pendaftaran wajah #%s", req.id)
            message = "Galat tak terduga di layanan kamera. Lihat log kios."
        finally:
            try:
                fe.finish(req, outcome, message)
                if outcome != "done":
                    action = audit.FACE_ENROLL_CANCELLED if outcome == "cancelled" else audit.FACE_ENROLL_FAILED
                    audit.log_event(
                        action, entity_type="employee", entity_id=employee.id,
                        description=(f"Pendaftaran wajah {employee.employee_code} (permintaan #{req.id}) "
                                     f"{'dibatalkan' if outcome == 'cancelled' else 'gagal'}: {message} {via}").strip(),
                    )
            except SQLAlchemyError:
                db.session.rollback()
                log.error("Hasil pendaftaran #%s tidak dapat dicatat.", req.id)
            if outcome != "done":
                self._say("warning", "Pendaftaran wajah tidak berhasil. " + message)
            self.enrollments.append((req.id, outcome))
            log.info("Pendaftaran wajah #%s selesai: %s", req.id, outcome)
            # Kembali ke mode absensi dengan pelacak bersih dan daftar wajah terbaru.
            self.tracker = FaceTracker(self.tracker_settings)
            self.gate = LivenessGate(self.liveness_settings)
            self.hint = None
            self.reload_templates(force=True)
            self.publish(force=True)

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
        if self.check_enrollment():
            return True
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
