"""
Titik masuk aplikasi Flask (application factory).

Menjalankan:
    flask --app app run
atau:
    python app.py

Membuat akun admin pertama:
    flask --app app create-admin

Kunci enkripsi templat wajah:
    flask --app app biometric-keygen      (sekali, saat instalasi)
    flask --app app biometric-status      (memeriksa kunci dan templat)
    flask --app app biometric-rotate-key  (mengganti kunci)

Pemeriksaan kamera:
    flask --app app camera-scan           (mencari webcam yang terpasang)
    flask --app app camera-check          (uji stabilitas dan pencahayaan)

Model AI dan deteksi wajah:
    flask --app app models-download       (unduh model YuNet dan SFace)
    flask --app app models-check          (periksa keaslian file model)
    flask --app app face-check            (uji deteksi wajah langsung)
"""
import re

import click
from flask import Flask, flash, jsonify, redirect, render_template, url_for
from flask_wtf.csrf import CSRFError
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

import models  # noqa: F401  (mendaftarkan model untuk Flask-Migrate)
from config import BASE_DIR, get_config
from extensions import csrf, db, login_manager, migrate
from models import User, office_now
from routes.auth import bp as auth_bp
from routes.employees import bp as employees_bp
from routes.schedules import bp as schedules_bp
from services import audit_service as audit
from services import biometric_service as biometric

USERNAME_PATTERN = re.compile(r"^[A-Za-z0-9_.-]{3,50}$")
MIN_PASSWORD_LENGTH = 10


def check_database():
    """Mengembalikan True jika database dapat menjalankan query sederhana."""
    try:
        db.session.execute(text("SELECT 1"))
        return True
    except SQLAlchemyError:
        db.session.rollback()
        return False


def register_commands(app):
    @app.cli.command("create-admin")
    @click.option("--username", prompt="Username admin")
    @click.option("--name", prompt="Nama lengkap")
    @click.password_option(
        "--password",
        prompt="Password (minimal 10 karakter)",
        confirmation_prompt="Ulangi password",
    )
    def create_admin(username, name, password):
        """Membuat akun admin baru tanpa menulis password di kode."""
        username = username.strip()
        name = name.strip()

        if not USERNAME_PATTERN.match(username):
            raise click.ClickException(
                "Username harus 3-50 karakter: huruf, angka, titik, "
                "garis bawah, atau tanda minus."
            )
        if not name:
            raise click.ClickException("Nama lengkap tidak boleh kosong.")
        if len(password) < MIN_PASSWORD_LENGTH:
            raise click.ClickException(
                f"Password minimal {MIN_PASSWORD_LENGTH} karakter."
            )

        exists = db.session.execute(
            db.select(User).filter_by(username=username)
        ).scalar_one_or_none()
        if exists:
            raise click.ClickException(f"Username '{username}' sudah dipakai.")

        user = User(name=name, username=username, role="admin", is_active=True)
        user.set_password(password)
        db.session.add(user)
        db.session.flush()
        audit.log_event(
            audit.ADMIN_CREATED,
            entity_type="user",
            entity_id=user.id,
            description=f"Admin dibuat melalui CLI: {username}",
            commit=False,
        )
        db.session.commit()
        click.echo(f"Admin '{username}' berhasil dibuat.")

    @app.cli.command("biometric-keygen")
    def biometric_keygen():
        """Membuat file kunci enkripsi templat wajah (sekali saat instalasi)."""
        try:
            path = biometric.generate_keyring()
        except biometric.BiometricKeyError as exc:
            raise click.ClickException(str(exc))

        audit.log_event(
            audit.BIOMETRIC_KEY_CREATED,
            entity_type="biometric_key",
            entity_id=1,
            description="File kunci biometrik dibuat melalui CLI (v1)",
        )
        click.echo(f"File kunci dibuat: {path}")
        if BASE_DIR in path.resolve().parents:
            click.secho(
                "PERINGATAN: file kunci berada di dalam folder proyek. "
                "Pindahkan ke luar folder proyek dan perbarui BIOMETRIC_KEY_FILE.",
                fg="yellow",
            )
        click.echo(
            "Simpan salinan file ini di tempat aman yang terpisah. "
            "Jika file ini hilang, semua wajah harus didaftarkan ulang."
        )

    @app.cli.command("biometric-status")
    def biometric_status():
        """Memeriksa file kunci dan memastikan semua templat dapat dibaca."""
        try:
            active, keys = biometric.load_keyring()
        except biometric.BiometricKeyError as exc:
            raise click.ClickException(str(exc))

        templates = db.session.execute(db.select(models.FaceTemplate)).scalars().all()
        failed = 0
        for template in templates:
            try:
                biometric.decrypt_template(template, (active, keys))
            except biometric.TemplateDecryptionError:
                failed += 1

        click.echo(f"Kunci aktif     : v{active} ({len(keys)} versi tersimpan)")
        click.echo(f"Templat wajah   : {len(templates)}")
        click.echo(f"Gagal diverifikasi: {failed}")
        if failed:
            raise click.ClickException(
                "Ada templat yang tidak bisa dibaca. Periksa file kunci atau "
                "kemungkinan perubahan data di database."
            )

    @app.cli.command("biometric-rotate-key")
    @click.confirmation_option(
        prompt="Buat kunci baru dan enkripsi ulang semua templat wajah?"
    )
    def biometric_rotate_key():
        """Membuat kunci baru dan mengenkripsi ulang semua templat."""
        try:
            version, count = biometric.rotate_key()
        except biometric.BiometricKeyError as exc:
            raise click.ClickException(str(exc))
        click.echo(f"Kunci aktif sekarang v{version}. {count} templat dienkripsi ulang.")
        click.echo("Perbarui salinan cadangan file kunci Anda.")

    @app.cli.command("camera-scan")
    @click.option("--max-index", default=4, show_default=True, help="Indeks kamera tertinggi yang dicoba.")
    @click.option("--backend", default=None, help="dshow, msmf, atau any.")
    def camera_scan(max_index, backend):
        """Menampilkan daftar kamera beserta nama dan indeksnya."""
        from camera import devices, webcam

        backend = (backend or app.config["CAMERA_BACKEND"]).lower()
        if backend not in webcam.BACKENDS:
            raise click.ClickException(f"Backend tidak dikenal: {backend}")

        names = devices.list_cameras() if backend == "dshow" else None
        if names is not None:
            if not names:
                raise click.ClickException(
                    "Windows tidak melaporkan kamera apa pun. Periksa kabel USB dan "
                    "izin kamera (Settings > Privacy & security > Camera)."
                )
            click.echo("Kamera yang terdaftar di Windows (urutan indeks DirectShow):")
            for i, name in enumerate(names):
                note = ""
                if "virtual" in name.lower():
                    note = "  <- kamera virtual, bukan kamera fisik"
                click.echo(f"  Indeks {i}: {name}{note}")
            click.echo("")
            click.echo(
                "Disarankan: isi CAMERA_NAME di .env dengan sebagian nama kamera, "
                "misalnya CAMERA_NAME=C270."
            )
            return

        click.echo(
            f"Nama kamera tidak dapat dibaca. Mencoba membuka indeks 0-{max_index} "
            f"(backend {backend})..."
        )
        found = webcam.scan(max_index=max_index, backend=backend)
        if not found:
            raise click.ClickException(
                "Tidak ada kamera yang dapat dibuka. Periksa kabel USB, izin kamera "
                "Windows (Settings > Privacy > Camera), dan tutup aplikasi lain "
                "yang memakai kamera."
            )
        for cam in found:
            click.echo(f"  Indeks {cam['index']}: gambar awal {cam['width']}x{cam['height']}")
        click.echo(
            "Catatan: DirectShow kadang membuka indeks yang tidak ada. Pastikan "
            "dengan camera-check --index N dan perhatikan lampu webcam."
        )

    @app.cli.command("camera-check")
    @click.option("--seconds", default=15, show_default=True, help="Lama pengujian.")
    @click.option("--preview/--no-preview", default=True, show_default=True,
                  help="Tampilkan jendela pratinjau.")
    @click.option("--index", type=int, default=None, help="Ganti CAMERA_INDEX sementara.")
    @click.option("--name", default=None, help="Ganti CAMERA_NAME sementara, mis. C270.")
    @click.option("--backend", default=None, help="Ganti CAMERA_BACKEND sementara.")
    @click.option("--width", type=int, default=None, help="Ganti CAMERA_WIDTH sementara.")
    @click.option("--height", type=int, default=None, help="Ganti CAMERA_HEIGHT sementara.")
    def camera_check(seconds, preview, index, name, backend, width, height):
        """Menguji webcam: resolusi, FPS, kegagalan baca, dan pencahayaan.

        Tidak ada gambar yang disimpan ke disk.
        """
        import time as _time

        import cv2

        from camera import webcam

        settings = webcam.CameraSettings.from_config(app.config)
        if index is not None:
            settings.index = index
            settings.name = ""  # --index berarti pilih langsung berdasarkan nomor
        if name is not None:
            settings.name = name.strip()
        if backend:
            settings.backend = backend.lower()
        if width:
            settings.width = width
        if height:
            settings.height = height

        target = f"nama '{settings.name}'" if settings.name else f"indeks {settings.index}"
        click.echo(
            f"Membuka kamera {target} (backend {settings.backend}, "
            f"target {settings.width}x{settings.height} {settings.fourcc} {settings.fps} FPS)..."
        )
        t0 = _time.perf_counter()
        try:
            cam = webcam.Webcam(settings).open()
        except webcam.CameraError as exc:
            raise click.ClickException(str(exc))
        open_seconds = _time.perf_counter() - t0
        a = cam.actual
        if cam.name_note:
            click.secho(cam.name_note, fg="yellow")
        if a["device_name"]:
            click.echo(f"Kamera: {a['device_name']} (indeks {a['index']})")
        click.echo(
            f"Terbuka dalam {open_seconds:.1f} detik: {a['width']}x{a['height']}, "
            f"format {a['fourcc']}, FPS dilaporkan {a['fps_reported']:.0f}"
        )
        if a["format_ok"]:
            click.echo(f"Format {settings.fourcc} diterima driver (urutan: {a['strategy']}).")
        else:
            click.secho(
                f"Driver menolak format {settings.fourcc} di semua urutan pengaturan; "
                f"kamera memakai {a['fourcc']}.",
                fg="yellow",
            )

        window = "Uji kamera - tekan Q untuk berhenti"

        def show(frame, live):
            view = frame.copy()
            lines = [
                f"{a['width']}x{a['height']}  {live['fps']:.1f} FPS",
                f"Kecerahan {live['brightness']:.0f}/255",
                "Q = berhenti",
            ]
            for i, label in enumerate(lines):
                y = 32 + i * 30
                cv2.putText(view, label, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (0, 0, 0), 4)
                cv2.putText(view, label, (12, y), cv2.FONT_HERSHEY_SIMPLEX, 0.8, (255, 255, 255), 2)
            cv2.imshow(window, view)
            key = cv2.waitKey(1) & 0xFF
            if key in (ord("q"), ord("Q"), 27):
                return False
            if cv2.getWindowProperty(window, cv2.WND_PROP_VISIBLE) < 1:
                return False
            return True

        click.echo(f"Mengukur selama {seconds} detik. Berdirilah di depan kamera seperti karyawan yang lewat.")
        try:
            stats = webcam.measure(cam, seconds=seconds, on_frame=show if preview else None)
        except webcam.CameraError as exc:
            raise click.ClickException(str(exc))
        finally:
            cam.release()
            if preview:
                cv2.destroyAllWindows()

        click.echo("")
        click.echo("Hasil pengujian kamera")
        click.echo(f"  Durasi            : {stats['seconds']} detik")
        click.echo(f"  Frame terbaca     : {stats['frames']}")
        click.echo(f"  Gagal baca        : {stats['failures']}")
        click.echo(f"  FPS rata-rata     : {stats['fps']}")
        click.echo(f"  Jeda terlama      : {stats['max_gap_ms']} ms")
        click.echo(f"  Kecerahan rata2   : {stats['brightness']}/255")
        click.echo(f"  Ketajaman rata2   : {stats['sharpness']}")
        click.echo(f"  Pencahayaan       : {stats['lighting']}")

        problems = []
        if (a["width"], a["height"]) != (settings.width, settings.height):
            problems.append(
                f"Resolusi yang didapat {a['width']}x{a['height']}, bukan "
                f"{settings.width}x{settings.height}."
            )
        if stats["fps"] < 15:
            problems.append(
                "FPS di bawah 15. Turunkan resolusi dengan --width 640 --height 480 "
                "untuk menguji, lalu ubah CAMERA_WIDTH/CAMERA_HEIGHT di .env jika hasilnya baik."
            )
        if stats["failures"]:
            problems.append("Ada frame yang gagal dibaca. Periksa kabel/port USB.")
        if stats["frames"] and not (
            webcam.DARK_THRESHOLD <= stats["brightness"] <= webcam.BRIGHT_THRESHOLD
        ):
            problems.append(f"Kecerahan {stats['brightness']}/255. {stats['lighting']}")

        click.echo("")
        if problems:
            click.secho("Perlu perhatian:", fg="yellow")
            for p in problems:
                click.echo(f"  - {p}")
        else:
            click.secho("Kamera stabil dan siap untuk tahap deteksi wajah.", fg="green")

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


def create_app(config_name=None):
    app = Flask(__name__)
    app.config.from_object(get_config(config_name))

    if not app.config.get("SECRET_KEY"):
        raise RuntimeError(
            "SECRET_KEY belum diatur. Salin .env.example menjadi .env "
            "lalu isi SECRET_KEY."
        )

    db.init_app(app)
    migrate.init_app(app, db)
    login_manager.init_app(app)
    csrf.init_app(app)

    app.register_blueprint(auth_bp)
    app.register_blueprint(employees_bp)
    app.register_blueprint(schedules_bp)
    register_commands(app)

    @app.after_request
    def security_headers(response):
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    @app.errorhandler(CSRFError)
    def handle_csrf_error(error):
        flash("Formulir kedaluwarsa atau tidak valid. Silakan coba lagi.", "error")
        return redirect(url_for("auth.login")), 302

    @app.get("/")
    def index():
        return render_template("index.html")

    @app.get("/health")
    def health():
        db_ok = check_database()
        if not db_ok:
            # Detail error hanya dicatat di log server, tidak dikirim ke browser.
            app.logger.error("Health check: koneksi database gagal.")

        payload = {
            "status": "ok" if db_ok else "degraded",
            "app": "ok",
            "database": "ok" if db_ok else "error",
            "server_time": office_now().isoformat(timespec="seconds"),
            "timezone": app.config["OFFICE_TIMEZONE"],
        }
        return jsonify(payload), (200 if db_ok else 503)

    return app


if __name__ == "__main__":
    application = create_app()
    application.run(host="127.0.0.1", port=5000)
