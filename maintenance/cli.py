"""
Perintah backup dan pemulihan (Tahap 5D):

    flask --app app backup-create                       buat backup database
    flask --app app backup-key E:\\kunci-absensi         salin kunci ke tempat terpisah
    flask --app app backup-verify FILE [--key KUNCI]    periksa keutuhan backup
    flask --app app backup-test FILE --key KUNCI        uji pulihkan ke database sementara
    flask --app app backup-restore FILE --key KUNCI     pulihkan ke database KOSONG
"""
from pathlib import Path

import click

from extensions import db
from services import audit_service as audit
from services import backup_service as bk
from services.cli_errors import friendly_db_errors


def _fmt_size(path):
    size = Path(path).stat().st_size
    return f"{size / 1024:.0f} KB" if size < 1024 * 1024 else f"{size / 1024 / 1024:.1f} MB"


def _print_tables(tables):
    for name, info in tables.items():
        rows = info["rows"] if isinstance(info, dict) else info
        click.echo(f"    {name:<22}{rows:>8} baris")


def register_maintenance_commands(app):
    @app.cli.command("backup-create")
    @click.option("--dir", "dest_dir", default=None, help="Folder tujuan (bawaan: BACKUP_DIR di .env).")
    @click.option("--keep", default=None, type=int, help="Jumlah backup terbaru yang disimpan.")
    @friendly_db_errors
    def backup_create(dest_dir, keep):
        """Membuat backup seluruh database (tanpa kunci biometrik)."""
        dest_dir = dest_dir or app.config["BACKUP_DIR"]
        keep = app.config["BACKUP_KEEP"] if keep is None else keep
        try:
            path, manifest, removed = bk.create_backup(dest_dir, keep=keep)
        except (bk.BackupError, OSError) as exc:
            raise click.ClickException(f"Backup gagal: {exc}")
        audit.log_event(audit.BACKUP_CREATED, entity_type="backup",
                        description=f"Backup database dibuat: {path.name} ({_fmt_size(path)})")
        click.secho(f"Backup tersimpan: {path} ({_fmt_size(path)})", fg="green")
        click.echo(f"  Revisi skema: {manifest['schema_revision']}")
        _print_tables(manifest["tables"])
        if removed:
            click.echo(f"  {len(removed)} backup lama dihapus (menyimpan {keep} terbaru).")
        if manifest["key_versions"]:
            click.secho(
                f"  Templat wajah memakai kunci versi {manifest['key_versions']}. Pastikan salinan "
                "kunci tersimpan TERPISAH (flask --app app backup-key ...).", fg="yellow")

    @app.cli.command("backup-key")
    @click.argument("dest")
    def backup_key(dest):
        """Menyalin kunci biometrik ke lokasi terpisah (mis. flashdisk)."""
        from services.biometric_service import BiometricKeyError

        try:
            path = bk.copy_key(dest, app.config["BACKUP_DIR"])
        except (bk.BackupError, BiometricKeyError, OSError) as exc:
            raise click.ClickException(str(exc))
        click.secho(f"Kunci biometrik disalin ke: {path}", fg="green")
        click.echo("  Simpan di tempat aman yang BERBEDA dari backup database. Ulangi setelah "
                   "biometric-rotate-key.")

    @app.cli.command("backup-verify")
    @click.argument("file")
    @click.option("--key", "key_path", default=None, help="File kunci untuk menguji dekripsi templat.")
    def backup_verify(file, key_path):
        """Memeriksa keutuhan file backup tanpa memulihkannya."""
        from services.biometric_service import BiometricKeyError, load_keyring

        try:
            manifest, tables = bk.read_backup(file)
        except bk.BackupError as exc:
            raise click.ClickException(str(exc))
        click.secho(f"Backup utuh: {Path(file).name} (dibuat {manifest['created_at']})", fg="green")
        click.echo(f"  Revisi skema: {manifest['schema_revision']}")
        _print_tables(manifest["tables"])
        if key_path:
            try:
                ok, failed = bk.check_templates(tables, load_keyring(key_path))
            except BiometricKeyError as exc:
                raise click.ClickException(str(exc))
            if failed:
                raise click.ClickException(
                    f"{failed} dari {ok + failed} templat wajah TIDAK bisa didekripsi dengan kunci ini.")
            click.secho(f"  Semua {ok} templat wajah bisa didekripsi dengan kunci ini.", fg="green")
        elif manifest["key_versions"]:
            click.secho("  Dekripsi templat belum diuji; tambahkan --key FILE_KUNCI.", fg="yellow")

    @app.cli.command("backup-test")
    @click.argument("file")
    @click.option("--key", "key_path", default=None, help="File kunci untuk menguji dekripsi templat.")
    def backup_test(file, key_path):
        """Uji pemulihan ke database SEMENTARA (database asli tidak disentuh)."""
        from services.biometric_service import BiometricKeyError

        click.secho("Uji pemulihan ke database SEMENTARA; data asli tidak disentuh.", fg="yellow")
        try:
            r = bk.test_restore(file, key_path)
        except (bk.BackupError, BiometricKeyError) as exc:
            raise click.ClickException(str(exc))
        problems = []
        if r["mismatch"]:
            problems.append(f"jumlah baris tidak cocok: {r['mismatch']}")
        if r["templates_failed"]:
            problems.append(f"{r['templates_failed']} templat wajah gagal didekripsi")
        click.echo("  Tabel dipulihkan:")
        _print_tables(r["restored"])
        click.echo(f"  Laporan bulan ini dapat dibuat: {r['report_employees']} karyawan")
        if r["templates_ok"] is not None:
            click.echo(f"  Templat wajah terbaca: {r['templates_ok']}")
        else:
            click.secho("  Dekripsi templat belum diuji; tambahkan --key FILE_KUNCI.", fg="yellow")
        if problems:
            raise click.ClickException("Uji pemulihan GAGAL: " + "; ".join(problems))
        click.secho("Uji pemulihan BERHASIL: backup ini dapat dipakai.", fg="green")

    @app.cli.command("backup-restore")
    @click.argument("file")
    @click.option("--key", "key_path", required=True,
                  help="Salinan file kunci biometrik yang cocok dengan backup.")
    @friendly_db_errors
    def backup_restore(file, key_path):
        """Memulihkan backup ke database yang terhubung (HARUS kosong)."""
        import shutil

        from services.biometric_service import BiometricKeyError, key_file_path, load_keyring

        try:
            manifest, tables = bk.read_backup(file)
            keyring = load_keyring(key_path)
        except (bk.BackupError, BiometricKeyError) as exc:
            raise click.ClickException(str(exc))
        ok, failed = bk.check_templates(tables, keyring)
        if failed:
            raise click.ClickException(
                f"{failed} templat wajah tidak cocok dengan kunci {key_path}. Pakai salinan kunci yang benar.")

        current_key = key_file_path()
        if current_key.exists():
            _, current_keys = load_keyring()
            if any(current_keys.get(v) != keyring[1].get(v) for v in manifest["key_versions"]):
                raise click.ClickException(
                    f"File kunci yang aktif ({current_key}) berbeda dengan kunci backup. Pindahkan "
                    "dulu file kunci yang aktif ke tempat lain, lalu ulangi agar kunci backup dipasang.")

        click.secho(f"Akan memulihkan {Path(file).name} (dibuat {manifest['created_at']}) ke database "
                    f"{db.engine.url.render_as_string(hide_password=True)}.", fg="yellow")
        _print_tables(manifest["tables"])
        answer = click.prompt('Ketik PULIHKAN untuk melanjutkan', default="", show_default=False)
        if answer.strip() != "PULIHKAN":
            raise click.ClickException("Dibatalkan.")
        try:
            bk.restore_into_current_db(manifest, tables)
        except bk.BackupError as exc:
            raise click.ClickException(str(exc))
        if not current_key.exists():
            current_key.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(key_path, current_key)
            click.echo(f"  Kunci biometrik dipasang di {current_key}")
        audit.log_event(audit.BACKUP_RESTORED, entity_type="backup",
                        description=f"Database dipulihkan dari {Path(file).name} "
                                    f"(dibuat {manifest['created_at']}); {ok} templat wajah terbaca.")
        click.secho("Pemulihan selesai. Jalankan biometric-status untuk memastikan semuanya terbaca.",
                    fg="green")
