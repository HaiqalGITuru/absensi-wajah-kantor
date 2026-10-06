"""
Daftar nama kamera di Windows dan pemilihan kamera berdasarkan nama.

Nomor indeks kamera bisa berubah (kamera virtual dari ponsel, port USB
berbeda, komputer berbeda). Dengan CAMERA_NAME=C270, aplikasi mencari kamera
yang namanya mengandung "C270" di daftar perangkat DirectShow, yang urutannya
sama dengan nomor indeks OpenCV pada backend dshow.

Daftar nama dibaca dengan pustaka pygrabber, yang hanya tersedia di Windows.
Di sistem lain, list_cameras() mengembalikan None.
"""
import platform


class CameraLookupError(Exception):
    """Kamera dengan nama yang diminta tidak ditemukan atau tidak unik."""


def _enumerate():
    from pygrabber.dshow_graph import FilterGraph

    return list(FilterGraph().get_input_devices())


def list_cameras():
    """Daftar nama kamera sesuai urutan indeks DirectShow, atau None jika tidak bisa dibaca."""
    if platform.system() != "Windows":
        return None
    try:
        return _enumerate()
    except Exception:  # pustaka tidak terpasang atau COM gagal
        return None


def find_index(name, cameras):
    """Mencari indeks kamera yang namanya mengandung `name` (tidak peka huruf besar/kecil)."""
    wanted = name.strip().lower()
    matches = [i for i, cam in enumerate(cameras) if wanted in cam.lower()]
    if not matches:
        available = "; ".join(f"{i}: {cam}" for i, cam in enumerate(cameras)) or "tidak ada"
        raise CameraLookupError(
            f"Tidak ada kamera dengan nama yang mengandung '{name}'. "
            f"Kamera yang terdeteksi: {available}."
        )
    if len(matches) > 1:
        found = "; ".join(f"{i}: {cameras[i]}" for i in matches)
        raise CameraLookupError(
            f"Ada lebih dari satu kamera yang cocok dengan '{name}' ({found}). "
            "Tulis nama yang lebih lengkap di CAMERA_NAME."
        )
    return matches[0]
