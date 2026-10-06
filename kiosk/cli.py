"""
Perintah menjalankan layanan kamera kios:

    flask --app app kiosk-run              (dengan jendela pratinjau)
    flask --app app kiosk-run --no-preview

Jalankan di terminal terpisah dari `flask --app app run`. Hentikan dengan Ctrl+C.
"""
import logging
import signal

import click

from extensions import db
from models import Employee
from services.cli_errors import friendly_db_errors


def register_kiosk_commands(app):
    @app.cli.command("kiosk-run")
    @click.option("--preview/--no-preview", default=True, show_default=True,
                  help="Tampilkan jendela kamera di komputer kios.")
    @friendly_db_errors
    def kiosk_run(preview):
        """Menjalankan layanan kamera yang MENCATAT ABSENSI otomatis."""
        import cv2

        from camera import webcam
        from kiosk.lock import AlreadyRunning, SingleInstanceLock
        from kiosk.state import KioskState
        from kiosk.worker import KioskWorker
        from recognition import identify
        from recognition.cli import _load_liveness, _load_models
        from recognition.matcher import Matcher
        from services import attendance_service as att
        from services import biometric_service as biometric
        from tracking.face_tracker import TrackerSettings

        logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
        # Pesan teknis pustaka pembaca nama kamera tidak perlu tampil.
        logging.getLogger("comtypes").setLevel(logging.WARNING)

        lock = SingleInstanceLock(app.config["KIOSK_LOCK_FILE"])
        try:
            lock.acquire()
        except AlreadyRunning as exc:
            raise click.ClickException(str(exc))

        cam = None
        try:
            detector, embedder, quality = _load_models(app)
            liveness, live_settings = _load_liveness(app)
            people = {}

            def matcher_factory():
                templates, failed = biometric.load_active_templates(embedder.model_name)
                if failed:
                    logging.warning("%s templat gagal diverifikasi dan dilewati.", failed)
                people.clear()
                people.update({
                    e.id: (e.employee_code, e.full_name)
                    for e in db.session.execute(db.select(Employee)).scalars()
                })
                return Matcher(templates, threshold=app.config["FACE_MATCH_THRESHOLD"],
                               margin=app.config["FACE_MATCH_MARGIN"])

            cam = webcam.Webcam(webcam.CameraSettings.from_config(app.config))
            try:
                cam.open()
                a = cam.actual
                click.echo(f"Kamera: {a.get('device_name') or 'indeks ' + str(a['index'])}, "
                           f"{a['width']}x{a['height']}")
            except webcam.CameraError as exc:
                click.secho(f"Kamera belum siap: {exc} Akan dicoba lagi otomatis.", fg="yellow")

            window = "Kios absensi - kamera"

            def show(frame, tracks):
                header = ["MENCATAT ABSENSI", "Ctrl+C di terminal untuk berhenti"]
                gate_state = {"prompts": {}, "pending": set(worker.gate.pending),
                              "rejected": worker.gate.rejected_tracks}
                cv2.imshow(window, identify.annotate(frame, tracks, people, header, gate_state))
                cv2.waitKey(1)
                return True

            worker = KioskWorker(
                app, cam, detector, embedder, quality, matcher_factory,
                TrackerSettings.from_config(app.config),
                att.AttendanceSettings.from_config(app.config),
                KioskState(app.config["KIOSK_STATE_FILE"]),
                template_reload_seconds=app.config["TEMPLATE_RELOAD_SECONDS"],
                on_frame=show if preview else None,
                liveness=liveness,
                liveness_settings=live_settings,
            )

            stop = {"flag": False}

            def request_stop(*_):
                stop["flag"] = True

            signal.signal(signal.SIGINT, request_stop)
            click.secho("Layanan kamera berjalan dan MENCATAT ABSENSI. Buka /kiosk di browser.",
                        fg="green")
            if live_settings.mode == "off":
                click.secho("Peringatan: pemeriksaan anti-foto DIMATIKAN (LIVENESS_MODE=off).", fg="yellow")
            else:
                click.echo(f"Anti-foto aktif: {live_settings.mode}")
            worker.run(should_stop=lambda: stop["flag"])
        finally:
            if cam is not None:
                cam.release()
            if preview:
                try:
                    cv2.destroyAllWindows()
                except Exception:
                    pass
            lock.release()
            click.echo("Layanan kamera berhenti.")
