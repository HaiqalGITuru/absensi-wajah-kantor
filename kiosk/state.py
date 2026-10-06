"""
Status kios yang dibagikan dari layanan kamera ke server web.

Layanan kamera menulis file JSON kecil (atomik) setiap setengah detik.
Server web hanya MEMBACA file ini, sehingga kamera tetap dipegang satu proses
saja (FR-04).

Isi file sengaja minimal: tidak ada gambar, embedding, NIP, atau skor.
Pesan untuk karyawan hanya memuat nama depan (PRD 10.3).
"""
import json
import os
import time
from pathlib import Path

STALE_SECONDS = 5.0       # layanan dianggap mati jika tidak memperbarui status
MESSAGE_SECONDS = 6.0     # lama pesan tampil di kios

CAMERA_OK = "ok"
CAMERA_ERROR = "error"
CAMERA_STARTING = "starting"


class KioskState:
    def __init__(self, path):
        self.path = Path(path)

    def publish(self, data, clock=time.time):
        data = dict(data, heartbeat=clock())
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(data, fh, ensure_ascii=False)
        os.replace(tmp, self.path)

    def read(self):
        try:
            with open(self.path, encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return None


def public_view(state, now=None, stale_seconds=STALE_SECONDS, message_seconds=MESSAGE_SECONDS):
    """Ringkasan aman untuk halaman kios (tanpa data internal)."""
    now = time.time() if now is None else now
    if not state or now - float(state.get("heartbeat", 0)) > stale_seconds:
        return {"online": False, "camera": "offline", "message": None, "hint": None}

    message = state.get("message")
    if message and now - float(message.get("at", 0)) > message_seconds:
        message = None
    return {
        "online": True,
        "camera": state.get("camera", CAMERA_STARTING),
        "message": {"kind": message["kind"], "text": message["text"]} if message else None,
        "hint": state.get("hint"),
    }


def admin_view(state, now=None, stale_seconds=STALE_SECONDS):
    """Ringkasan untuk dashboard admin."""
    now = time.time() if now is None else now
    if not state:
        return {"online": False, "detail": "Layanan kamera belum pernah dijalankan."}
    age = now - float(state.get("heartbeat", 0))
    if age > stale_seconds:
        return {"online": False, "detail": f"Tidak ada kabar dari layanan kamera selama {int(age)} detik."}
    return {
        "online": True,
        "camera": state.get("camera"),
        "detail": state.get("detail") or "",
        "employees": state.get("employees", 0),
        "recorded_today": state.get("recorded", 0),
        "liveness": bool(state.get("liveness", False)),
    }
