"""Pesan yang jelas untuk perintah terminal ketika database tidak dapat dihubungi."""
import functools

import click
from sqlalchemy.exc import OperationalError

DB_DOWN_MESSAGE = (
    "Database tidak dapat dihubungi. Pastikan MySQL di XAMPP/Laragon sudah "
    "dinyalakan, lalu jalankan perintah ini lagi."
)


def friendly_db_errors(fn):
    """Mengganti traceback panjang akibat MySQL mati dengan pesan singkat."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OperationalError as exc:
            raise click.ClickException(DB_DOWN_MESSAGE) from exc
    return wrapper
