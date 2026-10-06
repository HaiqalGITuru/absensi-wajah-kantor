"""
Halaman kios untuk karyawan (PRD 7.6, 10.3). Tidak memerlukan login.

Halaman ini hanya MEMBACA status dari layanan kamera; tidak membuka kamera
dan tidak bisa mengubah data absensi (PRD 3.2).
"""
from flask import Blueprint, current_app, jsonify, render_template

from kiosk.state import KioskState, public_view
from models import WEEKDAY_NAMES, office_now

bp = Blueprint("kiosk", __name__, url_prefix="/kiosk")

MONTHS = ("Januari", "Februari", "Maret", "April", "Mei", "Juni", "Juli",
          "Agustus", "September", "Oktober", "November", "Desember")


def _date_text(now):
    return f"{WEEKDAY_NAMES[now.weekday()]}, {now.day} {MONTHS[now.month - 1]} {now.year}"


@bp.get("/")
def page():
    return render_template("kiosk.html")


@bp.get("/status")
def status():
    view = public_view(KioskState(current_app.config["KIOSK_STATE_FILE"]).read())
    now = office_now()
    view["time"] = now.strftime("%H:%M:%S")
    view["date"] = _date_text(now)
    response = jsonify(view)
    response.headers["Cache-Control"] = "no-store"
    return response
