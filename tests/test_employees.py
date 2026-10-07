"""Pengujian Tahap 2B: pengelolaan karyawan (FR-02, TC-05, TC-06)."""
import pytest

from extensions import db
from models import AuditLog, Employee
from services import audit_service as audit


def _logs(action):
    return db.session.execute(
        db.select(AuditLog).filter_by(action=action)
    ).scalars().all()


def _employee_data(schedule, **overrides):
    data = {
        "employee_code": "199001012020011001",
        "full_name": "Budi Santoso",
        "department": "Bidang Aplikasi Informatika",
        "work_schedule_id": str(schedule.id),
    }
    data.update(overrides)
    return data


def _get_employee(code):
    return db.session.execute(
        db.select(Employee).filter_by(employee_code=code)
    ).scalar_one_or_none()


@pytest.mark.parametrize(
    "method,url",
    [
        ("get", "/admin/employees/"),
        ("get", "/admin/employees/new"),
        ("post", "/admin/employees/new"),
        ("post", "/admin/employees/1/deactivate"),
    ],
)
def test_halaman_karyawan_wajib_login(client, method, url):
    res = getattr(client, method)(url)
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]


def test_tambah_karyawan_berhasil(auth_client, schedule):
    """TC-05: data karyawan tersimpan dan tercatat di audit log."""
    res = auth_client.post(
        "/admin/employees/new", data=_employee_data(schedule), follow_redirects=True
    )
    assert res.status_code == 200
    assert "berhasil ditambahkan" in res.get_data(as_text=True)

    emp = _get_employee("199001012020011001")
    assert emp is not None
    assert emp.is_active is True
    assert emp.work_schedule_id == schedule.id

    logs = _logs(audit.EMPLOYEE_CREATED)
    assert len(logs) == 1
    assert logs[0].entity_id == emp.id
    assert logs[0].user_id is not None


def test_kode_disimpan_huruf_besar_dan_spasi_dirapikan(auth_client, schedule):
    auth_client.post(
        "/admin/employees/new",
        data=_employee_data(
            schedule, employee_code="  k-001 ", full_name="  Siti   Aminah  "
        ),
    )
    emp = _get_employee("K-001")
    assert emp is not None
    assert emp.full_name == "Siti Aminah"


def test_kode_duplikat_ditolak(auth_client, schedule):
    """TC-06: kode karyawan duplikat ditolak, termasuk beda huruf besar/kecil."""
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="K001"))
    res = auth_client.post(
        "/admin/employees/new",
        data=_employee_data(schedule, employee_code="k001", full_name="Orang Lain"),
    )
    assert res.status_code == 400
    assert "sudah dipakai" in res.get_data(as_text=True)
    count = db.session.execute(db.select(db.func.count(Employee.id))).scalar_one()
    assert count == 1


@pytest.mark.parametrize(
    "field,value,message",
    [
        ("employee_code", "", "Kode karyawan wajib diisi"),
        ("employee_code", "K 001", "Kode maksimal 30 karakter"),
        ("employee_code", "X" * 31, "Kode maksimal 30 karakter"),
        ("full_name", "", "Nama lengkap wajib diisi"),
        ("department", "", "Departemen wajib diisi"),
        ("work_schedule_id", "", "Jadwal kerja wajib dipilih"),
        ("work_schedule_id", "9999", "Pilih jadwal kerja yang masih aktif"),
        ("work_schedule_id", "abc", "Pilih jadwal kerja yang masih aktif"),
    ],
)
def test_validasi_formulir_karyawan(auth_client, schedule, field, value, message):
    res = auth_client.post(
        "/admin/employees/new", data=_employee_data(schedule, **{field: value})
    )
    assert res.status_code == 400
    assert message in res.get_data(as_text=True)
    assert db.session.execute(db.select(db.func.count(Employee.id))).scalar_one() == 0


def test_jadwal_nonaktif_tidak_bisa_dipilih(auth_client, schedule):
    schedule.is_active = False
    db.session.commit()
    res = auth_client.post("/admin/employees/new", data=_employee_data(schedule))
    assert res.status_code == 400
    assert "Pilih jadwal kerja yang masih aktif" in res.get_data(as_text=True)


def test_ubah_karyawan_tercatat_dengan_rincian(auth_client, schedule):
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="K002"))
    emp = _get_employee("K002")

    res = auth_client.post(
        f"/admin/employees/{emp.id}/edit",
        data=_employee_data(
            schedule, employee_code="K002", department="Sekretariat"
        ),
        follow_redirects=True,
    )
    assert res.status_code == 200
    db.session.refresh(emp)
    assert emp.department == "Sekretariat"

    logs = _logs(audit.EMPLOYEE_UPDATED)
    assert len(logs) == 1
    assert "Departemen" in logs[0].description
    assert "Sekretariat" in logs[0].description


def test_simpan_tanpa_perubahan_tidak_menambah_log(auth_client, schedule):
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="K003"))
    emp = _get_employee("K003")
    auth_client.post(
        f"/admin/employees/{emp.id}/edit", data=_employee_data(schedule, employee_code="K003")
    )
    assert _logs(audit.EMPLOYEE_UPDATED) == []


def test_ubah_kode_ke_kode_milik_orang_lain_ditolak(auth_client, schedule):
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="A1"))
    auth_client.post(
        "/admin/employees/new",
        data=_employee_data(schedule, employee_code="A2", full_name="Kedua"),
    )
    emp2 = _get_employee("A2")
    res = auth_client.post(
        f"/admin/employees/{emp2.id}/edit",
        data=_employee_data(schedule, employee_code="A1", full_name="Kedua"),
    )
    assert res.status_code == 400
    db.session.refresh(emp2)
    assert emp2.employee_code == "A2"


def test_nonaktifkan_tidak_menghapus_data(auth_client, schedule):
    """FR-02: karyawan nonaktif tetap ada di database."""
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="K004"))
    emp = _get_employee("K004")

    res = auth_client.post(f"/admin/employees/{emp.id}/deactivate")
    assert res.status_code == 302
    db.session.refresh(emp)
    assert emp.is_active is False
    assert _get_employee("K004") is not None
    assert len(_logs(audit.EMPLOYEE_DEACTIVATED)) == 1


def test_nonaktifkan_lewat_get_tidak_diizinkan(auth_client, schedule):
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="K005"))
    emp = _get_employee("K005")
    assert auth_client.get(f"/admin/employees/{emp.id}/deactivate").status_code == 405


def test_aktifkan_kembali_butuh_jadwal_aktif(auth_client, schedule):
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="K006"))
    emp = _get_employee("K006")
    auth_client.post(f"/admin/employees/{emp.id}/deactivate")

    schedule.is_active = False
    db.session.commit()

    res = auth_client.post(f"/admin/employees/{emp.id}/activate", follow_redirects=True)
    assert "belum memiliki jadwal aktif" in res.get_data(as_text=True)
    db.session.refresh(emp)
    assert emp.is_active is False

    schedule.is_active = True
    db.session.commit()
    auth_client.post(f"/admin/employees/{emp.id}/activate")
    db.session.refresh(emp)
    assert emp.is_active is True
    assert len(_logs(audit.EMPLOYEE_ACTIVATED)) == 1


def test_return_to_ke_luar_diabaikan(auth_client, schedule):
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="K007"))
    emp = _get_employee("K007")
    res = auth_client.post(
        f"/admin/employees/{emp.id}/deactivate",
        data={"return_to": "https://situs-jahat.com"},
    )
    assert res.headers["Location"].endswith("/admin/employees/")


def test_karyawan_tidak_ditemukan_404(auth_client):
    assert auth_client.get("/admin/employees/9999/edit").status_code == 404


def test_pencarian_dan_filter(auth_client, schedule):
    db.session.add_all([
        Employee(employee_code="A1", full_name="Andi", department="Sekretariat",
                 work_schedule_id=schedule.id),
        Employee(employee_code="B1", full_name="Bunga", department="Bidang Statistik",
                 work_schedule_id=schedule.id),
        Employee(employee_code="C1", full_name="Candra", department="Sekretariat",
                 work_schedule_id=schedule.id, is_active=False),
    ])
    db.session.commit()

    body = auth_client.get("/admin/employees/?q=bung").get_data(as_text=True)
    assert "Bunga" in body and "Andi" not in body

    body = auth_client.get("/admin/employees/?department=Sekretariat").get_data(as_text=True)
    assert "Andi" in body and "Bunga" not in body and "Candra" not in body

    body = auth_client.get("/admin/employees/?status=inactive").get_data(as_text=True)
    assert "Candra" in body and "Andi" not in body

    body = auth_client.get("/admin/employees/?status=all").get_data(as_text=True)
    assert "Andi" in body and "Candra" in body


def test_daftar_lebih_dari_100_karyawan_terbagi_halaman(auth_client, schedule):
    """PRD: sistem harus mendukung lebih dari 100 karyawan."""
    db.session.add_all(
        Employee(
            employee_code=f"P{i:03d}",
            full_name=f"Pegawai {i:03d}",
            department="Uji",
            work_schedule_id=schedule.id,
        )
        for i in range(120)
    )
    db.session.commit()

    page1 = auth_client.get("/admin/employees/").get_data(as_text=True)
    assert "120 karyawan ditemukan" in page1
    assert "Halaman 1 dari 5" in page1
    assert "Pegawai 000" in page1 and "Pegawai 025" not in page1

    page5 = auth_client.get("/admin/employees/?page=5").get_data(as_text=True)
    assert "Pegawai 119" in page5


def test_nama_dengan_tanda_kutip_tidak_merusak_halaman(auth_client, schedule):
    auth_client.post(
        "/admin/employees/new",
        data=_employee_data(schedule, employee_code="Q1", full_name="Muhammad Ma'ruf"),
    )
    body = auth_client.get("/admin/employees/").get_data(as_text=True)
    assert "Ma\\u0027ruf" in body
    assert "confirm('" not in body


def test_dashboard_menampilkan_ringkasan(auth_client, schedule):
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="D1"))
    body = auth_client.get("/admin").get_data(as_text=True)
    assert "Karyawan aktif" in body
    assert "Jadwal kerja aktif" in body


# ---------------------------------------------------------------- nama ganda

def test_nama_sama_dengan_karyawan_nonaktif_diperingatkan(auth_client, schedule):
    """Regresi uji kantor 7 Okt: Febian terdaftar sebagai 0003 lalu 0006."""
    db.session.add(Employee(employee_code="0003", full_name="Febian Nurwulan", department="APTIKA",
                            work_schedule_id=schedule.id, is_active=False))
    db.session.commit()

    res = auth_client.post("/admin/employees/new", data=_employee_data(
        schedule, employee_code="0006", full_name="  febian   nurwulan "))
    body = res.get_data(as_text=True)
    assert res.status_code == 400
    assert "0003 (nonaktif)" in body
    assert 'name="confirm_duplicate_name"' in body
    assert _get_employee("0006") is None


def test_nama_sama_boleh_jika_dikonfirmasi_orang_berbeda(auth_client, schedule):
    db.session.add(Employee(employee_code="A1", full_name="Muhammad Rizki", department="X",
                            work_schedule_id=schedule.id))
    db.session.commit()
    data = _employee_data(schedule, employee_code="A2", full_name="Muhammad Rizki")
    data["confirm_duplicate_name"] = "1"
    res = auth_client.post("/admin/employees/new", data=data)
    assert res.status_code == 302
    assert _get_employee("A2") is not None
    log = _logs(audit.EMPLOYEE_CREATED)[-1]
    assert "dikonfirmasi admin sebagai orang berbeda" in log.description


def test_ubah_data_sendiri_tidak_dianggap_nama_ganda(auth_client, schedule):
    auth_client.post("/admin/employees/new", data=_employee_data(schedule, employee_code="B1",
                                                                 full_name="Sari Dewi"))
    emp = _get_employee("B1")
    res = auth_client.post(f"/admin/employees/{emp.id}/edit", data=_employee_data(
        schedule, employee_code="B1", full_name="Sari Dewi", department="Umum"))
    assert res.status_code == 302
