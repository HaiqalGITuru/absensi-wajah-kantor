"""Pesan yang jelas untuk perintah terminal ketika database bermasalah."""
import functools

import click
from sqlalchemy.exc import OperationalError

DB_DOWN_MESSAGE = (
    "Database tidak dapat dihubungi. Pastikan MySQL di XAMPP/Laragon sudah "
    "dinyalakan, lalu jalankan perintah ini lagi."
)
DB_ACCESS_MESSAGE = (
    "Akses ke database ditolak. Periksa DB_USER dan DB_PASSWORD di .env, dan pastikan "
    "pengguna itu punya hak akses ke database DB_NAME (atur di phpMyAdmin > User accounts)."
)
DB_MISSING_MESSAGE = (
    "Database yang ditulis di DB_NAME belum dibuat. Buat dulu di phpMyAdmin, lalu "
    "jalankan: flask --app app db upgrade"
)

# Kode galat MySQL/MariaDB
_ACCESS = {1044, 1045, 1698}
_MISSING = {1049}


def explain(exc):
    code = None
    orig = getattr(exc, "orig", None)
    if orig is not None and getattr(orig, "args", None):
        code = orig.args[0]
    if code in _ACCESS:
        return DB_ACCESS_MESSAGE
    if code in _MISSING:
        return DB_MISSING_MESSAGE
    return DB_DOWN_MESSAGE


def friendly_db_errors(fn):
    """Mengganti traceback panjang akibat masalah database dengan pesan singkat."""
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except OperationalError as exc:
            raise click.ClickException(explain(exc)) from exc
    return wrapper
