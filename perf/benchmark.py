"""
Uji performa Tahap 5C dengan data sintetis berskala kantor.

KESELAMATAN DATA: semua data uji dibuat di database SQLite SEMENTARA dan kunci
enkripsi SEMENTARA di folder temp, lalu dihapus. Database MySQL sungguhan
tidak pernah disambungkan oleh aplikasi uji ini.

Catatan: SQLite dan MySQL berbeda; angka halaman web adalah perkiraan. Untuk
ukuran data kantor (ratusan karyawan) keduanya berada di orde yang sama.
"""
import shutil
import statistics
import tempfile
import time
from datetime import datetime, time as dtime, timedelta
from pathlib import Path

import numpy as np

TARGETS = {
    "match_ms": 5.0,
    "load_s": 2.0,
    "attendance_page_s": 1.0,
    "dashboard_s": 1.0,
    "report_page_s": 3.0,
    "export_csv_s": 3.0,
    "frame_ms": 150.0,
}

ADMIN_PASSWORD = "PerfTestPassword123"


def _timed(fn, repeat=3):
    times = []
    result = None
    for _ in range(repeat):
        t0 = time.perf_counter()
        result = fn()
        times.append(time.perf_counter() - t0)
    return statistics.median(times), result


def _unit(rng, n):
    v = rng.normal(size=(n, 128)).astype(np.float32)
    return v / np.linalg.norm(v, axis=1, keepdims=True)


def seed(app, employees, templates, days, rng, today):
    """Mengisi database sementara: jadwal, admin, karyawan, templat, absensi."""
    from extensions import db
    from models import AttendanceSession, Employee, User, WorkSchedule, WorkScheduleDay
    from recognition.embedder import MODEL_NAME
    from services import biometric_service as bio

    with app.app_context():
        db.create_all()
        schedule = WorkSchedule(name="Jam Kerja Uji", late_tolerance_minutes=15)
        for w in range(5):
            schedule.days.append(WorkScheduleDay(
                weekday=w, start_time=dtime(7, 30),
                end_time=dtime(16, 0) if w != 4 else dtime(11, 30)))
        admin = User(name="Admin Uji", username="admin", role="admin")
        admin.set_password(ADMIN_PASSWORD)
        db.session.add_all([schedule, admin])
        db.session.commit()

        people = [Employee(employee_code=f"PERF{i:04d}", full_name=f"Karyawan Uji {i:04d}",
                           department=f"Bidang {i % 6 + 1}", work_schedule_id=schedule.id)
                  for i in range(employees)]
        db.session.add_all(people)
        db.session.commit()

        bio.generate_keyring()
        base = _unit(rng, employees)
        for i, emp in enumerate(people):
            noisy = base[i] + rng.normal(scale=0.05, size=(templates, 128)).astype(np.float32)
            noisy /= np.linalg.norm(noisy, axis=1, keepdims=True)
            bio.save_enrollment(emp, list(noisy), MODEL_NAME, admin.id)

        sessions = []
        for d in range(days):
            day = today - timedelta(days=d)
            if day.weekday() >= 5:
                continue
            end = dtime(16, 0) if day.weekday() != 4 else dtime(11, 30)
            for emp in people:
                if rng.random() < 0.08:
                    continue  # tidak masuk
                minute = int(rng.integers(0, 40))
                check_in = datetime.combine(day, dtime(7, 15)) + timedelta(minutes=minute)
                late = max(0, minute - 30)
                sessions.append(AttendanceSession(
                    employee_id=emp.id, work_schedule_id=schedule.id, work_date=day,
                    check_in_at=check_in,
                    check_out_at=None if d == 0 else datetime.combine(day, end) + timedelta(minutes=5),
                    status="terlambat" if late else "hadir", late_minutes=late,
                    scheduled_start=datetime.combine(day, dtime(7, 30)),
                    scheduled_end=datetime.combine(day, end),
                    check_in_method="kamera", check_out_method=None if d == 0 else "kamera"))
        db.session.add_all(sessions)
        db.session.commit()
        return len(sessions)


def measure_matching(app, rng):
    from extensions import db
    from recognition.embedder import MODEL_NAME
    from recognition.matcher import Matcher
    from services import biometric_service as bio

    with app.app_context():
        load_s, (templates, failed) = _timed(lambda: bio.load_active_templates(MODEL_NAME))
        db.session.rollback()
    matcher = Matcher(templates, threshold=0.60, margin=0.05)
    probes = np.stack([e for _, e in templates[:: max(1, len(templates) // 200)]])
    probes = probes + rng.normal(scale=0.05, size=probes.shape).astype(np.float32)
    t0 = time.perf_counter()
    n = 0
    for _ in range(5):
        for p in probes:
            matcher.match(p)
            n += 1
    match_ms = (time.perf_counter() - t0) * 1000 / n
    return {"templates": len(templates), "failed": failed, "load_s": load_s,
            "match_ms": match_ms, "matcher": matcher}


def measure_pages(app, today):
    client = app.test_client()
    res = client.post("/login", data={"username": "admin", "password": ADMIN_PASSWORD})
    if res.status_code not in (302, 303):
        raise RuntimeError("Login uji gagal.")
    start = (today - timedelta(days=30)).isoformat()
    urls = {
        "attendance_page_s": f"/admin/attendance/?date={today.isoformat()}",
        "dashboard_s": "/admin",
        "report_page_s": f"/admin/reports/?start={start}&end={today.isoformat()}",
        "export_csv_s": f"/admin/reports/export/detail.csv?start={start}&end={today.isoformat()}",
    }
    results, sizes = {}, {}
    for key, url in urls.items():
        def get(url=url):
            r = client.get(url)
            if r.status_code != 200:
                raise RuntimeError(f"{url} -> HTTP {r.status_code}")
            return r
        results[key], resp = _timed(get)
        sizes[key] = len(resp.data)
    return results, sizes


def measure_camera(base_app, matcher, seconds, echo):
    """Pemrosesan frame sungguhan (kamera + model asli) dengan matcher berisi
    templat sintetis berskala kantor. Tidak membaca atau menulis database."""
    from recognition.cli import _load_liveness, _load_models, _open_camera
    from recognition.identify import process_frame
    from tracking.face_tracker import FaceTracker, TrackerSettings

    with base_app.app_context():
        detector, embedder, quality = _load_models(base_app)
        liveness, _ = _load_liveness(base_app)
        cam = _open_camera(base_app)
    tracker = FaceTracker(TrackerSettings())
    with_face, without = [], []
    echo(f"Mengukur kamera {seconds:g} detik. Berdirilah di depan kamera sebagian waktu.")
    end = time.perf_counter() + seconds
    try:
        while time.perf_counter() < end:
            frame = cam.read()
            if frame is None:
                continue
            t0 = time.perf_counter()
            tracks, _, _ = process_frame(frame, detector, embedder, matcher, quality, tracker,
                                         t0, liveness=liveness)
            ms = (time.perf_counter() - t0) * 1000
            (with_face if tracks else without).append(ms)
    finally:
        cam.release()
    return {
        "frames_with_face": len(with_face),
        "frames_without_face": len(without),
        "frame_ms": statistics.mean(with_face) if with_face else None,
        "frame_ms_p95": float(np.percentile(with_face, 95)) if with_face else None,
        "empty_frame_ms": statistics.mean(without) if without else None,
    }


def run(base_app, employees=150, templates=5, days=31, camera_seconds=0, echo=print):
    from app import create_app
    from models import office_now

    rng = np.random.default_rng(2026)
    tmp = Path(tempfile.mkdtemp(prefix="absensi-perf-"))
    today = office_now().date()
    try:
        app = create_app("testing", overrides={
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{(tmp / 'perf.db').as_posix()}",
            "SQLALCHEMY_ENGINE_OPTIONS": {},
            "BIOMETRIC_KEY_FILE": str(tmp / "keys.json"),
            "KIOSK_STATE_FILE": str(tmp / "kiosk_state.json"),
            "WTF_CSRF_ENABLED": False,
        })
        assert "sqlite" in app.config["SQLALCHEMY_DATABASE_URI"]  # tidak pernah MySQL asli

        echo(f"Menyiapkan data uji sementara: {employees} karyawan x {templates} templat, "
             f"{days} hari absensi...")
        t0 = time.perf_counter()
        sessions = seed(app, employees, templates, days, rng, today)
        echo(f"  selesai dalam {time.perf_counter() - t0:.1f} detik ({sessions} catatan absensi).")

        results = measure_matching(app, rng)
        pages, sizes = measure_pages(app, today)
        results.update(pages)
        results["sizes"] = sizes
        results["sessions"] = sessions
        if camera_seconds:
            results.update(measure_camera(base_app, results["matcher"], camera_seconds, echo))
        results.pop("matcher", None)
        app_engine = None
        with app.app_context():
            from extensions import db
            app_engine = db.engine
        app_engine.dispose()
        return results
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
