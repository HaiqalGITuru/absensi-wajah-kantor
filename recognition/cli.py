"""
Perintah terminal untuk model AI dan pengenalan wajah (Tahap 3B dan 3C).

    flask --app app models-download   Unduh model YuNet dan SFace
    flask --app app models-check      Periksa keaslian file model
    flask --app app face-check        Uji deteksi dan kualitas wajah
    flask --app app face-enroll       Daftarkan wajah karyawan
    flask --app app face-identify     Uji pengenalan identitas (tanpa absensi)

Tidak ada gambar yang disimpan oleh perintah-perintah ini.
"""
import datetime as _dt

import click

from extensions import db
from models import Employee, User
from services import ValidationError
from services import audit_service as audit
from services import biometric_service as biometric


def _load_models(app):
    from recognition import model_files as mf
    from recognition.detector import QualitySettings, YuNetDetector
    from recognition.embedder import SFaceEmbedder

    try:
        yunet = mf.verify(app.config["MODEL_DIR"], mf.YUNET)
        sface = mf.verify(app.config["MODEL_DIR"], mf.SFACE)
    except mf.ModelFileError as exc:
        raise click.ClickException(str(exc))
    return (
        YuNetDetector(yunet, detect_width=app.config["FACE_DETECT_WIDTH"]),
        SFaceEmbedder(sface),
        QualitySettings.from_config(app.config),
    )


def _open_camera(app):
    from camera import webcam

    settings = webcam.CameraSettings.from_config(app.config)
    click.echo("Membuka kamera...")
    try:
        cam = webcam.Webcam(settings).open()
    except webcam.CameraError as exc:
        raise click.ClickException(str(exc))
    a = cam.actual
    click.echo(f"Kamera: {a.get('device_name') or 'indeks ' + str(a['index'])}, {a['width']}x{a['height']}")
    return cam


def _build_matcher(app, embedder):
    from recognition.matcher import Matcher

    try:
        templates, failed = biometric.load_active_templates(embedder.model_name)
    except biometric.BiometricKeyError as exc:
        raise click.ClickException(str(exc))
    if failed:
        click.secho(f"Peringatan: {failed} templat gagal diverifikasi dan dilewati.", fg="yellow")
    return Matcher(
        templates,
        threshold=app.config["FACE_MATCH_THRESHOLD"],
        margin=app.config["FACE_MATCH_MARGIN"],
    )


def _verify_admin(username, password):
    user = db.session.execute(db.select(User).filter_by(username=username)).scalar_one_or_none()
    if user is None or not user.is_active or not user.check_password(password):
        raise click.ClickException("Username atau password admin salah.")
    return user


def register_recognition_commands(app):
    @app.cli.command("models-download")
    def models_download():
        """Mengunduh model YuNet dan SFace lalu memverifikasi sidik jarinya."""
        from recognition import model_files as mf

        model_dir = app.config["MODEL_DIR"]
        for model in mf.ALL_MODELS:
            try:
                mf.verify(model_dir, model)
                click.echo(f"{model.filename}: sudah ada dan asli, dilewati.")
                continue
            except mf.ModelFileError:
                pass
            click.echo(f"Mengunduh {model.filename} ({model.size / 1e6:.1f} MB, lisensi {model.license})...")
            try:
                path = mf.download(model_dir, model)
            except mf.ModelFileError as exc:
                raise click.ClickException(str(exc))
            click.echo(f"  Tersimpan dan terverifikasi: {path}")
        click.secho("Semua model siap.", fg="green")

    @app.cli.command("models-check")
    def models_check():
        """Memeriksa bahwa file model ada dan sidik jarinya cocok dengan versi resmi."""
        from recognition import model_files as mf

        failed = False
        for model in mf.ALL_MODELS:
            try:
                path = mf.verify(app.config["MODEL_DIR"], model)
                click.echo(f"OK     {model.filename} ({model.license}) {path}")
            except mf.ModelFileError as exc:
                failed = True
                click.echo(f"GAGAL  {exc}")
        if failed:
            raise click.ClickException("Ada model yang belum siap.")

    @app.cli.command("face-check")
    @click.option("--seconds", default=20, show_default=True, help="Lama pengujian.")
    @click.option("--preview/--no-preview", default=True, show_default=True)
    def face_check(seconds, preview):
        """Uji deteksi wajah dan kualitas wajah langsung dari kamera.

        Tidak mencatat absensi dan tidak menyimpan gambar.
        """
        import cv2

        from camera import webcam
        from recognition import face_check as fc
        from recognition import model_files as mf
        from recognition.detector import QualitySettings, YuNetDetector
        from recognition.embedder import SFaceEmbedder

        try:
            yunet = mf.verify(app.config["MODEL_DIR"], mf.YUNET)
            sface = mf.verify(app.config["MODEL_DIR"], mf.SFACE)
        except mf.ModelFileError as exc:
            raise click.ClickException(str(exc))

        detector = YuNetDetector(yunet, detect_width=app.config["FACE_DETECT_WIDTH"])
        embedder = SFaceEmbedder(sface)
        quality = QualitySettings.from_config(app.config)

        settings = webcam.CameraSettings.from_config(app.config)
        click.echo("Membuka kamera...")
        try:
            cam = webcam.Webcam(settings).open()
        except webcam.CameraError as exc:
            raise click.ClickException(str(exc))
        a = cam.actual
        click.echo(f"Kamera: {a.get('device_name') or 'indeks ' + str(a['index'])}, {a['width']}x{a['height']}")
        click.echo(
            f"Uji deteksi selama {seconds} detik. Coba berbagai jarak (0,5-2 m), "
            "menoleh, dan berjalan melintas. Tekan Q untuk berhenti."
        )

        window = "Uji deteksi wajah - tekan Q untuk berhenti"

        def show(frame, live):
            header = [
                f"{live['fps']:.1f} FPS  deteksi {live['detect_ms']:.0f} ms",
                f"Wajah: {len(live['faces'])}",
            ]
            cv2.imshow(window, fc.annotate(frame, live["faces"], header))
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                return False
            return cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) >= 1

        try:
            stats = fc.run(cam, detector, embedder, quality, seconds=seconds,
                           on_frame=show if preview else None)
        except webcam.CameraError as exc:
            raise click.ClickException(str(exc))
        finally:
            cam.release()
            if preview:
                cv2.destroyAllWindows()

        pct = lambda n: f"{(100 * n / stats['frames']):.0f}%" if stats["frames"] else "0%"
        click.echo("")
        click.echo("Hasil uji deteksi wajah")
        click.echo(f"  Frame diproses        : {stats['frames']} ({stats['fps']} FPS)")
        click.echo(f"  Frame berisi wajah    : {stats['with_face']} ({pct(stats['with_face'])})")
        click.echo(f"  Wajah layak dipakai   : {stats['good']} ({pct(stats['good'])})")
        click.echo(f"  Waktu deteksi rata2   : {stats['detect_ms']} ms per frame")
        click.echo(f"  Waktu embedding rata2 : {stats['embed_ms']} ms per wajah")
        click.echo(f"  Ukuran wajah rata2    : {stats['face_size']} px (minimal {quality.min_size})")
        click.echo(f"  Ketajaman wajah rata2 : {stats['face_sharpness']} (ambang {quality.min_sharpness}, "
                   f"terendah {stats['min_sharpness_seen']})")
        total_ms = stats["detect_ms"] + stats["embed_ms"]
        click.echo(f"  Perkiraan waktu proses satu wajah: {total_ms:.0f} ms")
        if stats["with_face"] == 0:
            click.secho("Tidak ada wajah terdeteksi. Pastikan wajah menghadap kamera dan cukup terang.",
                        fg="yellow")

    @app.cli.command("face-enroll")
    @click.option("--employee", "employee_code", required=True, help="Kode karyawan yang didaftarkan.")
    @click.option("--admin", "admin_username", prompt="Username admin", help="Admin yang bertanggung jawab.")
    @click.password_option("--password", prompt="Password admin", confirmation_prompt=False)
    @click.option("--samples", default=5, show_default=True, help="Jumlah sampel wajah.")
    @click.option("--replace", "replace_reason", default=None,
                  help="Ganti templat lama. Isi dengan alasan, mis. \"wajah berubah\".")
    @click.option("--allow-similar", is_flag=True,
                  help="Tetap simpan walau mirip karyawan lain (mis. kembar). Tercatat di audit.")
    @click.option("--timeout", default=60, show_default=True, help="Batas waktu pengambilan sampel (detik).")
    @click.option("--preview/--no-preview", default=True, show_default=True)
    def face_enroll(employee_code, admin_username, password, samples, replace_reason,
                    allow_similar, timeout, preview):
        """Mendaftarkan wajah karyawan dari kamera (FR-03).

        Yang disimpan hanya embedding terenkripsi, bukan foto.
        """
        import cv2

        from camera import webcam
        from recognition import enrollment
        from recognition import face_check as fc

        admin = _verify_admin(admin_username, password)
        code = employee_code.strip().upper()
        employee = db.session.execute(
            db.select(Employee).filter_by(employee_code=code)
        ).scalar_one_or_none()
        if employee is None:
            raise click.ClickException(f"Karyawan dengan kode {code} tidak ditemukan.")
        if not employee.is_active:
            raise click.ClickException(f"Karyawan {code} nonaktif dan tidak dapat didaftarkan.")
        if not 3 <= samples <= biometric.MAX_TEMPLATES_PER_EMPLOYEE:
            raise click.ClickException(
                f"Jumlah sampel harus 3 sampai {biometric.MAX_TEMPLATES_PER_EMPLOYEE}."
            )
        existing = biometric.templates_for(employee)
        if existing and not replace_reason:
            raise click.ClickException(
                f"{code} sudah memiliki {len(existing)} templat wajah. Untuk mendaftarkan "
                "ulang, tambahkan --replace \"alasan\"."
            )
        if replace_reason is not None and len(replace_reason.strip()) < 5:
            raise click.ClickException("Alasan --replace minimal 5 karakter.")
        try:
            biometric.load_keyring()
        except biometric.BiometricKeyError as exc:
            raise click.ClickException(str(exc))

        detector, embedder, quality = _load_models(app)
        matcher = _build_matcher(app, embedder)

        click.echo(f"Mendaftarkan wajah: {employee.employee_code} - {employee.full_name}")
        click.echo(
            f"Berdiri sendirian sekitar 0,7-1 m di depan kamera. Akan diambil {samples} sampel; "
            "gerakkan kepala sedikit di antara sampel."
        )
        cam = _open_camera(app)
        window = "Pendaftaran wajah - tekan Q untuk batal"

        def show(frame, status):
            header = [
                f"{employee.employee_code}  sampel {status['taken']}/{status['samples']}",
                status["message"],
            ]
            cv2.imshow(window, fc.annotate(frame, status["faces"], header))
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                return False
            return cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) >= 1

        try:
            result = enrollment.collect(
                cam, detector, embedder, quality, samples=samples, timeout=timeout,
                on_frame=show if preview else None,
            )
            consistency = enrollment.check_consistency(result.embeddings)
        except (enrollment.EnrollmentError, webcam.CameraError) as exc:
            raise click.ClickException(str(exc))
        finally:
            cam.release()
            if preview:
                cv2.destroyAllWindows()

        conflict = enrollment.find_conflict(result.embeddings, matcher, employee.id)
        if conflict is not None:
            other = db.session.get(Employee, conflict.employee_id)
            other_label = f"{other.employee_code} - {other.full_name}" if other else "karyawan lain"
            message = (
                f"Wajah ini mirip dengan wajah terdaftar milik {other_label} "
                f"(skor {conflict.score:.2f}, ambang {matcher.threshold})."
            )
            if not allow_similar:
                raise click.ClickException(
                    message + " Pendaftaran dibatalkan. Periksa apakah orang yang didaftarkan "
                    "sudah benar. Gunakan --allow-similar hanya untuk kasus khusus seperti kembar."
                )
            click.secho("Peringatan: " + message, fg="yellow")
            audit.log_event(
                audit.FACE_ENROLL_SIMILAR_OVERRIDE,
                user_id=admin.id,
                entity_type="employee",
                entity_id=employee.id,
                description=(
                    f"Pendaftaran {employee.employee_code} tetap disimpan walau mirip "
                    f"{other.employee_code if other else '?'} (skor {conflict.score:.2f})"
                ),
                commit=False,
            )

        try:
            saved = biometric.save_enrollment(
                employee, result.embeddings, embedder.model_name, admin.id,
                replace_reason=replace_reason.strip() if replace_reason else None,
            )
        except ValidationError as exc:
            db.session.rollback()
            raise click.ClickException(next(iter(exc.errors.values())))

        click.echo("")
        click.secho(f"{len(saved)} sampel wajah {employee.employee_code} tersimpan terenkripsi.", fg="green")
        click.echo(f"  Waktu pengambilan      : {result.seconds} detik")
        click.echo(f"  Kemiripan antar-sampel : {min(consistency)} - {max(consistency)} "
                   f"(minimal {enrollment.MIN_CONSISTENCY})")
        if result.rejected:
            click.echo(f"  Frame ditolak          : {enrollment._top_reasons(result.rejected)}")

    @app.cli.command("face-identify")
    @click.option("--seconds", default=60, show_default=True, help="Lama pengujian.")
    @click.option("--preview/--no-preview", default=True, show_default=True)
    def face_identify(seconds, preview):
        """Uji pengenalan identitas langsung dari kamera.

        TIDAK mencatat absensi. Dipakai untuk menguji akurasi (PRD 13.2).
        """
        import cv2

        from camera import webcam
        from recognition import identify
        from tracking.face_tracker import FaceTracker, TrackerSettings

        detector, embedder, quality = _load_models(app)
        matcher = _build_matcher(app, embedder)
        if matcher.template_count == 0:
            raise click.ClickException(
                "Belum ada wajah terdaftar. Daftarkan dulu dengan: "
                "flask --app app face-enroll --employee KODE"
            )
        people = {
            e.id: (e.employee_code, e.full_name)
            for e in db.session.execute(db.select(Employee)).scalars()
        }
        tracker = FaceTracker(TrackerSettings.from_config(app.config))

        click.echo(
            f"{matcher.employee_count} karyawan ({matcher.template_count} templat) dimuat. "
            f"Ambang {matcher.threshold}, selisih minimal {matcher.margin}."
        )
        click.secho("Mode uji: absensi TIDAK dicatat.", fg="yellow")
        cam = _open_camera(app)
        window = "Uji pengenalan - tekan Q untuk berhenti"

        def show(frame, tracks):
            header = [f"Terdaftar: {matcher.employee_count} karyawan", "Mode uji, tanpa absensi"]
            cv2.imshow(window, identify.annotate(frame, tracks, people, header))
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                return False
            return cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) >= 1

        def on_event(event):
            code, name = people.get(event.employee_id, ("?", "?"))
            stamp = _dt.datetime.now().strftime("%H:%M:%S")
            note = " (dalam jeda, tidak dihitung ulang)" if event.in_cooldown else ""
            click.echo(f"[{stamp}] Dikenali: {code} {name} dalam {event.seconds_to_confirm} dtk{note}")

        try:
            stats = identify.run(
                cam, detector, embedder, matcher, quality, tracker, seconds=seconds,
                on_frame=show if preview else None, on_event=on_event,
            )
        except webcam.CameraError as exc:
            raise click.ClickException(str(exc))
        finally:
            cam.release()
            if preview:
                cv2.destroyAllWindows()

        click.echo("")
        click.echo("Hasil uji pengenalan")
        click.echo(f"  Frame diproses             : {stats['frames']} ({stats['process_ms']} ms per frame)")
        click.echo(f"  Orang dikenali             : {stats['confirmations']} kali "
                   f"(+{stats['repeats_in_cooldown']} ulangan dalam jeda)")
        click.echo(f"  Rata-rata waktu dikenali   : "
                   f"{stats['avg_seconds_to_confirm'] if stats['avg_seconds_to_confirm'] is not None else '-'} dtk")
        click.echo(f"  Jejak tidak dikenal        : {stats['unknown_tracks']}")
        click.echo(f"  Jejak ragu-ragu            : {stats['ambiguous_tracks']}")
        click.echo("  Data kalibrasi ambang:")
        click.echo(f"    Skor cocok terendah      : {stats['min_match_score'] if stats['min_match_score'] is not None else '-'}")
        click.echo(f"    Skor tidak dikenal tertinggi: "
                   f"{stats['max_unknown_score'] if stats['max_unknown_score'] is not None else '-'}")
        click.echo(f"    Ambang saat ini          : {matcher.threshold}")
