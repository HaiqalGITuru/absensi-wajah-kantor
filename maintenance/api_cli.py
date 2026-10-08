"""
Perintah pengelolaan token API:

    flask --app app api-token-create --name "Andi - aplikasi rekap" [--days 90]
    flask --app app api-token-list
    flask --app app api-token-revoke ID --reason "alasan"

Membuat dan mencabut token butuh username + password admin (tercatat di audit log).
"""
import click

from services import ValidationError
from services import api_token_service as tokens
from services.cli_errors import friendly_db_errors


def _admin(username, password):
    from recognition.cli import _verify_admin

    return _verify_admin(username, password)


def register_api_commands(app):
    @app.cli.command("api-token-create")
    @click.option("--name", required=True, help="Pemakai dan keperluan, mis. 'Andi - aplikasi rekap'.")
    @click.option("--scope", type=click.Choice(tokens.SCOPES), default="read", show_default=True,
                  help="read = hanya baca; write = juga mengubah karyawan/jadwal.")
    @click.option("--days", default=None, type=int,
                  help=f"Masa berlaku (read: 1-{tokens.MAX_DAYS}, bawaan {tokens.DEFAULT_DAYS}; "
                       f"write: 1-{tokens.MAX_WRITE_DAYS}, bawaan 30).")
    @click.option("--admin", "admin_username", prompt="Username admin")
    @click.option("--password", prompt="Password admin", hide_input=True)
    @friendly_db_errors
    def api_token_create(name, scope, days, admin_username, password):
        """Membuat token API. Token hanya ditampilkan SEKALI."""
        admin = _admin(admin_username, password)
        if days is None:
            days = 30 if scope == "write" else tokens.DEFAULT_DAYS
        try:
            token, row = tokens.create_token(name, admin.id, days, scope)
        except ValidationError as exc:
            raise click.ClickException(" ".join(exc.errors.values()))
        click.secho(f"Token {row.scope.upper()} untuk '{row.name}' (id {row.id}), berlaku sampai "
                    f"{row.expires_at:%d-%m-%Y %H:%M}:", fg="green")
        if row.scope == "write":
            click.secho("Token WRITE bisa menambah/mengubah/menonaktifkan karyawan dan jadwal "
                        "serta membaca audit log. Perlakukan seperti password admin.", fg="red")
        click.echo("")
        click.echo(f"    {token}")
        click.echo("")
        click.secho("Token ini HANYA ditampilkan sekali dan tidak bisa dilihat lagi. Serahkan langsung "
                    "kepada pemakainya (jangan lewat grup chat atau tangkapan layar). Jika hilang atau "
                    "bocor: cabut dengan api-token-revoke lalu buat yang baru.", fg="yellow")

    @app.cli.command("api-token-list")
    @friendly_db_errors
    def api_token_list():
        """Menampilkan semua token (tanpa isi token)."""
        from models import office_now

        rows = tokens.list_tokens()
        if not rows:
            click.echo("Belum ada token API.")
            return
        now = office_now()
        click.echo(f"{'ID':>3}  {'Awal token':<12}{'Cakupan':<9}{'Pemakai':<34}{'Status':<12}"
                   f"{'Berlaku s.d.':<18}Terakhir dipakai")
        for r in rows:
            if r.revoked_at:
                status = "dicabut"
            elif r.expires_at and now >= r.expires_at:
                status = "kedaluwarsa"
            else:
                status = "aktif"
            used = r.last_used_at.strftime("%d-%m-%Y %H:%M") if r.last_used_at else "-"
            expires = r.expires_at.strftime("%d-%m-%Y %H:%M") if r.expires_at else "-"
            click.echo(f"{r.id:>3}  {r.prefix + '…':<12}{r.scope:<9}{r.name[:32]:<34}{status:<12}"
                       f"{expires:<18}{used}")

    @app.cli.command("api-token-revoke")
    @click.argument("token_id", type=int)
    @click.option("--reason", required=True, help="Alasan pencabutan (tercatat di audit log).")
    @click.option("--admin", "admin_username", prompt="Username admin")
    @click.option("--password", prompt="Password admin", hide_input=True)
    @friendly_db_errors
    def api_token_revoke(token_id, reason, admin_username, password):
        """Mencabut token API; aplikasi yang memakainya langsung ditolak."""
        admin = _admin(admin_username, password)
        try:
            row = tokens.revoke(token_id, reason, admin.id)
        except ValidationError as exc:
            raise click.ClickException(" ".join(exc.errors.values()))
        click.secho(f"Token '{row.name}' ({row.prefix}…) dicabut.", fg="green")
