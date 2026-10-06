"""Pengujian pencocokan identitas (FR-06) dan pelacakan (FR-07)."""
import numpy as np
import pytest

from fake_faces import FakeEmbedder, person_frame, make_detection
from recognition.matcher import AMBIGUOUS, MATCH, NO_TEMPLATES, UNKNOWN, Matcher, MatchResult
from tracking.face_tracker import CONFIRMED, FaceTracker, TrackerSettings, iou


def unit(*values, dim=8):
    v = np.zeros(dim, np.float32)
    v[: len(values)] = values
    return v / np.linalg.norm(v)


# ---------------------------------------------------------------- matcher

def test_tanpa_templat():
    assert Matcher([]).match(unit(1)).status == NO_TEMPLATES


def test_cocok_dengan_karyawan_yang_benar():
    m = Matcher([(1, unit(1, 0)), (2, unit(0, 1))], threshold=0.5)
    r = m.match(unit(0.95, 0.1))
    assert r.status == MATCH and r.employee_id == 1
    assert r.score > 0.9


def test_wajah_tidak_terdaftar_ditolak():
    """TC-10: wajah tidak terdaftar tidak menghasilkan identitas."""
    m = Matcher([(1, unit(1, 0)), (2, unit(0, 1))], threshold=0.5)
    r = m.match(unit(0, 0, 1))
    assert r.status == UNKNOWN and r.employee_id is None


def test_kecocokan_ambigu_ditolak():
    """FR-06: dua karyawan sama-sama mirip dengan selisih kecil -> ditolak."""
    m = Matcher([(1, unit(1, 0.05)), (2, unit(1, -0.05))], threshold=0.5, margin=0.05)
    r = m.match(unit(1, 0))
    assert r.status == AMBIGUOUS and r.employee_id is None


def test_banyak_templat_per_karyawan_memakai_yang_terbaik():
    m = Matcher([(1, unit(1, 0)), (1, unit(0, 0, 1)), (2, unit(0, 1))], threshold=0.5)
    assert m.employee_count == 2 and m.template_count == 3
    assert m.match(unit(0, 0, 1)).employee_id == 1


def test_exclude_karyawan_tertentu():
    m = Matcher([(1, unit(1, 0)), (2, unit(0.8, 0.6))], threshold=0.5, margin=0.0)
    assert m.match(unit(1, 0), exclude=1).employee_id == 2


def test_lebih_dari_100_karyawan():
    rng = np.random.default_rng(0)
    vecs = [rng.normal(size=128) for _ in range(150)]
    m = Matcher([(i, v) for i, v in enumerate(vecs)], threshold=0.5)
    noisy = vecs[77] + rng.normal(scale=0.1, size=128)
    r = m.match(noisy)
    assert r.status == MATCH and r.employee_id == 77


# ---------------------------------------------------------------- tracker

BOX = (100, 100, 150, 180)
MOVED = (110, 104, 150, 180)


def match(emp, score=0.8):
    return MatchResult(MATCH, emp, score, 0.1)


def unknown(score=0.2):
    return MatchResult(UNKNOWN, None, score, 0.1)


def test_iou():
    assert iou(BOX, BOX) == pytest.approx(1.0)
    assert iou(BOX, (500, 500, 10, 10)) == 0.0
    assert 0.6 < iou(BOX, MOVED) < 1.0


def test_identitas_dipastikan_setelah_beberapa_frame():
    """FR-07: satu frame cocok belum cukup; perlu konsisten."""
    t = FaceTracker(TrackerSettings(confirm_votes=3))
    _, ev = t.update([(BOX, match(1), "")], 0.0)
    assert ev == []
    _, ev = t.update([(MOVED, match(1), "")], 0.1)
    assert ev == []
    tracks, ev = t.update([(BOX, match(1), "")], 0.2)
    assert len(ev) == 1 and ev[0].employee_id == 1
    assert ev[0].seconds_to_confirm == pytest.approx(0.2)
    assert tracks[0].state == CONFIRMED


def test_jejak_yang_sudah_pasti_tidak_menghasilkan_keputusan_lagi():
    t = FaceTracker(TrackerSettings(confirm_votes=2))
    events = []
    for i in range(20):
        _, ev = t.update([(BOX, match(1), "")], i * 0.1)
        events += ev
    assert len(events) == 1


def test_dua_identitas_dalam_satu_jejak_tidak_dicampur():
    """FR-07: identitas berbeda tidak boleh tercampur dalam satu jejak."""
    t = FaceTracker(TrackerSettings(confirm_votes=3))
    events = []
    for i, emp in enumerate([1, 2, 1, 2, 1, 2]):
        _, ev = t.update([(BOX, match(emp), "")], i * 0.1)
        events += ev
    assert events == []


def test_identitas_lain_di_jejak_pasti_membuat_jejak_baru():
    t = FaceTracker(TrackerSettings(confirm_votes=2))
    t.update([(BOX, match(1), "")], 0.0)
    tracks, ev = t.update([(BOX, match(1), "")], 0.1)
    first_id = tracks[0].id
    assert [e.employee_id for e in ev] == [1]

    # Satu frame berbeda belum cukup untuk memecah jejak.
    tracks, ev = t.update([(BOX, match(2), "")], 0.2)
    assert tracks[0].id == first_id and tracks[0].confirmed_id == 1 and ev == []

    # Frame berbeda kedua: jejak lama ditutup, jejak baru dimulai tanpa identitas.
    tracks, ev = t.update([(BOX, match(2), "")], 0.3)
    assert tracks[0].id != first_id
    assert tracks[0].confirmed_id is None and ev == []

    # Berikutnya identitas 2 dipastikan di jejak baru.
    tracks, ev = t.update([(BOX, match(2), "")], 0.4)
    assert [e.employee_id for e in ev] == [2]
    assert tracks[0].confirmed_id == 2


def test_frame_berkualitas_buruk_tidak_ikut_memilih():
    t = FaceTracker(TrackerSettings(confirm_votes=3))
    for i in range(10):
        tracks, ev = t.update([(BOX, None, "Gambar buram, jangan bergerak")], i * 0.1)
        assert ev == []
    assert tracks[0].last_reason == "Gambar buram, jangan bergerak"
    assert len(tracks[0].votes) == 0


def test_wajah_tidak_dikenal_ditandai():
    t = FaceTracker(TrackerSettings(window=5))
    for i in range(5):
        tracks, ev = t.update([(BOX, unknown(), "")], i * 0.1)
        assert ev == []
    assert tracks[0].state == UNKNOWN


def test_jejak_hilang_lalu_wajah_kembali_menjadi_jejak_baru():
    t = FaceTracker(TrackerSettings(max_missing_seconds=1.0))
    tracks, _ = t.update([(BOX, match(1), "")], 0.0)
    old = tracks[0].id
    t.update([], 0.5)
    tracks, _ = t.update([(BOX, match(1), "")], 2.0)
    assert tracks[0].id != old


def test_jeda_per_karyawan():
    """FR-07: wajah yang kembali dalam masa jeda ditandai, tidak dihitung ulang."""
    s = TrackerSettings(confirm_votes=2, cooldown_seconds=30, max_missing_seconds=0.5)
    t = FaceTracker(s)
    t.update([(BOX, match(1), "")], 0.0)
    _, ev = t.update([(BOX, match(1), "")], 0.1)
    assert ev[0].in_cooldown is False

    t.update([(BOX, match(1), "")], 5.0)       # jejak baru setelah hilang
    _, ev = t.update([(BOX, match(1), "")], 5.1)
    assert ev[0].in_cooldown is True

    t.update([(BOX, match(1), "")], 40.0)
    _, ev = t.update([(BOX, match(1), "")], 40.1)
    assert ev[0].in_cooldown is False


def test_dua_orang_bersamaan_dilacak_terpisah():
    left, right = (50, 100, 120, 150), (400, 100, 120, 150)
    t = FaceTracker(TrackerSettings(confirm_votes=2))
    t.update([(left, match(1), ""), (right, match(2), "")], 0.0)
    tracks, ev = t.update([(left, match(1), ""), (right, match(2), "")], 0.1)
    assert {e.employee_id for e in ev} == {1, 2}
    assert tracks[0].id != tracks[1].id


def test_pengaturan_dari_config(app):
    s = TrackerSettings.from_config(app.config)
    assert (s.window, s.confirm_votes, s.cooldown_seconds) == (5, 3, 30.0)


def test_embedder_tiruan_konsisten_per_orang():
    e = FakeEmbedder()
    det = make_detection()
    a = e.embed(person_frame("budi", 1), det)
    b = e.embed(person_frame("budi", 2), det)
    c = e.embed(person_frame("sari", 3), det)
    assert float(a @ b) > 0.8
    assert float(a @ c) < 0.5
