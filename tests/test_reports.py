"""Pengujian Tahap 5A (dashboard kehadiran) dan 5B (laporan + ekspor CSV)."""
import csv
import io
from datetime import date, datetime, time

import pytest

from extensions import db
from models import AuditLog, Employee
from services import ValidationError
from services import attendance_service as att
from services import audit_service as audit
from services import report_service as rpt

MON, TUE, WED, SAT = date(2026, 10, 5), date(2026, 10, 6), date(2026, 10, 7), date(2026, 10, 10)


def at(d, h, m=0):
    return datetime.combine(d, time(h, m))


@pytest.fixture
def staff(app, schedule):
    people = [Employee(employee_code=c, full_name=n, department=d, work_schedule_id=schedule.id)
              for c, n, d in (("K1", "Ani", "TI"), ("K2", "Budi", "TI"), ("K3", "Citra", "Umum"))]
    db.session.add_all(people)
    db.session.commit()
    return people


def _logs(action):
    return db.session.execute(db.select(AuditLog).filter_by(action=action)).scalars().all()


# ---------------------------------------------------------------- periode

@pytest.mark.parametrize("start,end,field", [
    (None, WED, "start"), (WED, MON, "end"), (date(2026, 10, 9), date(2026, 10, 9), "start"),
    (date(2025, 1, 1), WED, "end"),
])
def test_validasi_periode(app, start, end, field):
    with pytest.raises(ValidationError) as exc:
        rpt.validate_period(start, end, today=WED)
    assert field in exc.value.errors


def test_akhir_periode_dipotong_sampai_hari_ini(app):
    assert rpt.validate_period(MON, date(2026, 10, 31), today=WED) == (MON, WED)


# ---------------------------------------------------------------- detail & rekap

def test_detail_dan_rekap(staff, admin_user):
    ani, budi, citra = staff
    att.record(ani.id, now=at(MON, 7, 20))
    att.record(ani.id, now=at(MON, 16, 5))
    att.record(ani.id, now=at(TUE, 7, 50))            # terlambat 5 menit, tanpa jam pulang
    att.record(budi.id, now=at(MON, 8, 0))            # terlambat 15 menit
    att.record(budi.id, now=at(MON, 12, 0))           # pulang cepat
    att.record(citra.id, now=at(TUE, 7, 25))
    att.cancel(att.daily_roster(TUE)[2]["session"].id, "Kamera salah mengenali orang", admin_user.id)

    details = rpt.detail_rows(MON, WED, today=WED)
    status = {(r["employee"].employee_code, r["date"]): r["status"] for r in details}
    assert status[("K1", MON)] == rpt.ST_PRESENT
    assert status[("K1", TUE)] == rpt.ST_LATE
    assert status[("K2", TUE)] == rpt.ST_ABSENT
    assert status[("K3", TUE)] == rpt.ST_ABSENT          # dibatalkan = tidak tercatat masuk
    assert status[("K1", WED)] == rpt.ST_TODAY_PENDING   # hari ini belum dihitung tidak hadir

    s = {i["employee"].employee_code: i for i in rpt.summary_rows(details)}
    assert (s["K1"]["workdays"], s["K1"]["present"], s["K1"]["late"], s["K1"]["late_minutes"]) == (2, 2, 1, 5)
    assert s["K1"]["no_checkout"] == 1
    assert (s["K2"]["present"], s["K2"]["late_minutes"], s["K2"]["left_early"], s["K2"]["absent"]) == (1, 15, 1, 1)
    assert (s["K3"]["present"], s["K3"]["absent"]) == (0, 2)


def test_hari_libur_tanpa_catatan_tidak_masuk_laporan(staff):
    assert rpt.detail_rows(SAT, SAT, today=date(2026, 10, 12)) == []


def test_karyawan_nonaktif_hanya_muncul_jika_punya_catatan(staff):
    ani = staff[0]
    att.record(ani.id, now=at(MON, 7, 20))
    ani.is_active = False
    db.session.commit()
    codes = {(r["employee"].employee_code, r["date"]) for r in rpt.detail_rows(MON, TUE, today=WED)}
    assert ("K1", MON) in codes and ("K1", TUE) not in codes


def test_filter_departemen_dan_nama(staff):
    rows = rpt.detail_rows(MON, MON, department="Umum", today=WED)
    assert {r["employee"].employee_code for r in rows} == {"K3"}
    rows = rpt.detail_rows(MON, MON, q="bud", today=WED)
    assert {r["employee"].employee_code for r in rows} == {"K2"}


# ---------------------------------------------------------------- CSV

def _parse(content):
    assert content.startswith("\ufeff")
    return list(csv.reader(io.StringIO(content[1:]), delimiter=";"))


def test_csv_rekap_dan_detail(staff):
    att.record(staff[0].id, now=at(MON, 7, 50))
    details = rpt.detail_rows(MON, MON, today=WED)
    detail = _parse(rpt.detail_csv(details))
    assert detail[0] == rpt.DETAIL_HEADER
    row = next(r for r in detail if r[2] == "K1")
    assert row[:2] == ["05-10-2026", "Senin"] and row[7] == "07:50" and row[9] == "Terlambat"
    rekap = _parse(rpt.summary_csv(rpt.summary_rows(details)))
    assert rekap[0] == rpt.SUMMARY_HEADER and len(rekap) == 4


def test_csv_mencegah_rumus_excel(app, schedule):
    """CSV injection: nama yang diawali '=' tidak boleh dijalankan sebagai rumus."""
    db.session.add(Employee(employee_code="X1", full_name='=HYPERLINK("http://jahat","klik")',
                            department="-Umum", work_schedule_id=schedule.id))
    db.session.commit()
    rows = _parse(rpt.summary_csv(rpt.summary_rows(rpt.detail_rows(MON, MON, today=WED))))
    assert rows[1][1].startswith("'=") and rows[1][2] == "'-Umum"


def test_csv_huruf_indonesia_tetap_utuh(app, schedule):
    db.session.add(Employee(employee_code="Y1", full_name="Muhammad Ma'ruf Ñañez", department="Umum",
                            work_schedule_id=schedule.id))
    db.session.commit()
    content = rpt.summary_csv(rpt.summary_rows(rpt.detail_rows(MON, MON, today=WED)))
    assert "Muhammad Ma'ruf Ñañez" in content


# ---------------------------------------------------------------- halaman

def test_halaman_laporan_wajib_login(client):
    assert client.get("/admin/reports/").status_code == 302
    assert client.get("/admin/reports/export/rekap.csv").status_code == 302


def test_halaman_laporan_tampil(auth_client, staff):
    att.record(staff[0].id, now=at(MON, 7, 50))
    body = auth_client.get("/admin/reports/?start=2026-10-05&end=2026-10-05").get_data(as_text=True)
    assert "Laporan kehadiran" in body and "Ani" in body and "Unduh rekap (CSV)" in body


def test_periode_tidak_valid_ditolak(auth_client):
    res = auth_client.get("/admin/reports/?start=2026-10-07&end=2026-10-01")
    assert res.status_code == 400
    assert "Tanggal akhir" in res.get_data(as_text=True)


def test_unduh_csv_tercatat_di_audit(auth_client, staff, admin_user):
    res = auth_client.get("/admin/reports/export/detail.csv?start=2026-10-05&end=2026-10-06&department=TI")
    assert res.status_code == 200
    assert res.mimetype == "text/csv"
    assert 'filename="detail_absensi_2026-10-05_2026-10-06.csv"' in res.headers["Content-Disposition"]
    log = _logs(audit.REPORT_EXPORTED)[0]
    assert log.user_id == admin_user.id
    assert "05-10-2026 s.d. 06-10-2026" in log.description and "departemen TI" in log.description


def test_jenis_ekspor_tidak_dikenal(auth_client):
    assert auth_client.get("/admin/reports/export/gaji.csv").status_code == 404


def test_menu_laporan_dan_tombol_keluar_tidak_berubah(auth_client):
    body = auth_client.get("/admin").get_data(as_text=True)
    assert 'href="/admin/reports/"' in body
    assert "btn-secondary" in body   # gaya tombol Keluar di bilah atas tetap


# ---------------------------------------------------------------- dashboard 5A

def test_dashboard_kehadiran_hari_ini(app, auth_client, staff, monkeypatch):
    import routes.auth as auth_routes
    from models import office_now

    today = office_now().date()
    if today.weekday() >= 5:
        pytest.skip("hari ini libur pada jadwal uji")
    att.record(staff[0].id, now=at(today, 7, 20))
    att.record(staff[1].id, now=at(today, 8, 0))
    body = auth_client.get("/admin").get_data(as_text=True)
    assert "Kehadiran hari ini" in body
    assert "Absensi terbaru hari ini" in body and "Ani" in body
    assert "karyawan aktif belum punya wajah terdaftar" in body
    assert "Layanan kamera kios tidak berjalan" in body


def test_dashboard_peringatan_penolakan_anti_foto(app, auth_client):
    audit.log_event(audit.LIVENESS_REJECTED, description="uji")
    body = auth_client.get("/admin").get_data(as_text=True)
    assert "1 percobaan absensi ditolak pemeriksaan anti-foto hari ini" in body
