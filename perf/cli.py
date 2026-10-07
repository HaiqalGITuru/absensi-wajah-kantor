"""
Perintah uji performa:

    flask --app app perf-check                       (150 karyawan, tanpa kamera)
    flask --app app perf-check --employees 300
    flask --app app perf-check --camera-seconds 30   (ikut mengukur kamera + model)

Data uji dibuat di database SQLite sementara dan dihapus setelah selesai.
Database MySQL sungguhan TIDAK disentuh.
"""
import click

from perf import benchmark as bm

ROWS = [
    ("match_ms", "Pencocokan 1 wajah", "ms"),
    ("load_s", "Muat + dekripsi semua templat", "dtk"),
    ("attendance_page_s", "Halaman Absensi harian", "dtk"),
    ("dashboard_s", "Dashboard", "dtk"),
    ("report_page_s", "Laporan 31 hari", "dtk"),
    ("export_csv_s", "Ekspor CSV detail 31 hari", "dtk"),
    ("frame_ms", "Proses 1 frame kamera (ada wajah)", "ms"),
]


def register_perf_commands(app):
    @app.cli.command("perf-check")
    @click.option("--employees", default=150, show_default=True, type=click.IntRange(10, 2000))
    @click.option("--templates", default=5, show_default=True, type=click.IntRange(1, 10))
    @click.option("--days", default=31, show_default=True, type=click.IntRange(1, 92))
    @click.option("--camera-seconds", default=0, show_default=True, type=click.IntRange(0, 120),
                  help="Jika > 0, ikut mengukur kamera dan model asli selama sekian detik.")
    def perf_check(employees, templates, days, camera_seconds):
        """Uji performa dengan data sintetis berskala kantor (database sementara)."""
        click.secho("Uji performa memakai database SEMENTARA; data asli tidak disentuh.", fg="yellow")
        try:
            r = bm.run(app, employees, templates, days, camera_seconds, echo=click.echo)
        except Exception as exc:  # pragma: no cover - laporan untuk pengguna
            raise click.ClickException(f"Uji performa gagal: {exc}")

        click.echo("")
        click.echo(f"Hasil ({r['templates']} templat, {r['sessions']} catatan absensi)")
        click.echo(f"  {'Bagian':<36}{'Hasil':>12}{'Sasaran':>12}   Status")
        slow = 0
        for key, label, unit in ROWS:
            value = r.get(key)
            target = bm.TARGETS[key]
            if value is None:
                click.echo(f"  {label:<36}{'-':>12}{'< ' + format(target, 'g') + ' ' + unit:>12}   tidak diukur")
                continue
            ok = value < target
            slow += not ok
            shown = f"{value:.2f} {unit}" if unit == "ms" else f"{value:.3f} {unit}"
            click.echo(f"  {label:<36}{shown:>12}{'< ' + format(target, 'g') + ' ' + unit:>12}   "
                       + click.style("OK" if ok else "LAMBAT", fg="green" if ok else "red"))
        if r.get("frame_ms_p95") is not None:
            empty = (f"{r['empty_frame_ms']:.1f} ms" if r.get("empty_frame_ms") is not None
                     else "- (tidak ada frame tanpa wajah)")
            click.echo(f"    frame dengan wajah: {r['frames_with_face']}, p95 {r['frame_ms_p95']:.1f} ms; "
                       f"frame tanpa wajah rata-rata {empty}")
        elif camera_seconds:
            click.secho("    Tidak ada wajah terdeteksi selama pengukuran kamera.", fg="yellow")
        if r["failed"]:
            click.secho(f"  {r['failed']} templat gagal didekripsi (seharusnya 0).", fg="red")
        click.echo(f"  Ukuran CSV detail: {r['sizes']['export_csv_s'] / 1024:.0f} KB")
        click.echo("")
        if slow:
            click.secho(f"{slow} bagian melewati sasaran waktu.", fg="red")
        else:
            click.secho("Semua bagian yang diukur memenuhi sasaran waktu.", fg="green")
