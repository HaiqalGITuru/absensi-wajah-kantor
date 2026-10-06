"""Pengujian Tahap 2A: login admin, logout, audit log, dan CSRF (FR-01, FR-14)."""
import pytest

from app import create_app
from conftest import ADMIN_PASSWORD, ADMIN_USERNAME, login
from extensions import db
from models import AuditLog, User
from services import audit_service as audit


def _logs(action):
    return db.session.execute(
        db.select(AuditLog).filter_by(action=action)
    ).scalars().all()


def test_halaman_login_tampil(client):
    res = client.get("/login")
    assert res.status_code == 200
    assert "Login admin" in res.get_data(as_text=True)


def test_password_disimpan_sebagai_hash(admin_user):
    assert admin_user.password_hash != ADMIN_PASSWORD
    assert ADMIN_PASSWORD not in admin_user.password_hash
    assert admin_user.check_password(ADMIN_PASSWORD)
    assert not admin_user.check_password("salah")


def test_akses_admin_tanpa_login_ditolak(client):
    """TC-18: dashboard tidak bisa dibuka tanpa login."""
    res = client.get("/admin")
    assert res.status_code == 302
    assert "/login" in res.headers["Location"]


def test_login_valid_membuka_dashboard(client, admin_user):
    """TC-03: login valid membuka dashboard dan tercatat di audit log."""
    res = login(client, follow_redirects=True)
    assert res.status_code == 200
    assert "Halo, Admin Uji" in res.get_data(as_text=True)
    assert len(_logs(audit.LOGIN_SUCCESS)) == 1


def test_login_password_salah_ditolak(client, admin_user):
    """TC-04: password salah ditolak, password tidak masuk ke log."""
    res = login(client, password="password-yang-salah")
    body = res.get_data(as_text=True)
    assert res.status_code == 401
    assert "Username atau password salah." in body

    gagal = _logs(audit.LOGIN_FAILED)
    assert len(gagal) == 1
    assert "password-yang-salah" not in (gagal[0].description or "")
    assert client.get("/admin").status_code == 302


def test_username_tidak_terdaftar_ditolak(client, admin_user):
    res = login(client, username="tidak_ada")
    assert res.status_code == 401
    assert len(_logs(audit.LOGIN_FAILED)) == 1


def test_admin_nonaktif_tidak_bisa_login(client, admin_user):
    admin_user.is_active = False
    db.session.commit()
    res = login(client)
    assert res.status_code == 401
    assert client.get("/admin").status_code == 302


def test_logout_mengakhiri_sesi(client, admin_user):
    login(client)
    assert client.get("/admin").status_code == 200
    res = client.post("/logout")
    assert res.status_code == 302
    assert client.get("/admin").status_code == 302
    assert len(_logs(audit.LOGOUT)) == 1


def test_logout_lewat_get_tidak_diizinkan(client, admin_user):
    login(client)
    assert client.get("/logout").status_code == 405


@pytest.mark.parametrize(
    "next_url",
    ["https://situs-jahat.com", "//situs-jahat.com", "/\\situs-jahat.com"],
)
def test_redirect_ke_luar_diblokir(client, admin_user, next_url):
    res = client.post(
        f"/login?next={next_url}",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert res.status_code == 302
    assert res.headers["Location"].endswith("/admin")


def test_redirect_internal_diizinkan(client, admin_user):
    res = client.post(
        "/login?next=/admin",
        data={"username": ADMIN_USERNAME, "password": ADMIN_PASSWORD},
    )
    assert res.headers["Location"].endswith("/admin")


def test_csrf_menolak_formulir_tanpa_token():
    """Dengan CSRF aktif, login tanpa token tidak diproses."""
    app = create_app("testing")
    app.config["WTF_CSRF_ENABLED"] = True
    with app.app_context():
        db.create_all()
        user = User(name="A", username=ADMIN_USERNAME, role="admin")
        user.set_password(ADMIN_PASSWORD)
        db.session.add(user)
        db.session.commit()

        client = app.test_client()
        res = login(client)
        assert res.status_code == 302
        assert client.get("/admin").status_code == 302
        assert _logs(audit.LOGIN_SUCCESS) == []

        db.session.remove()
        db.drop_all()


def test_header_keamanan_terpasang(client):
    res = client.get("/")
    assert res.headers["X-Content-Type-Options"] == "nosniff"
    assert res.headers["X-Frame-Options"] == "DENY"


def test_perintah_create_admin(app):
    runner = app.test_cli_runner()
    res = runner.invoke(
        args=[
            "create-admin",
            "--username", "admin_baru",
            "--name", "Admin Baru",
            "--password", "PasswordBaru123",
        ]
    )
    assert res.exit_code == 0, res.output
    user = db.session.execute(
        db.select(User).filter_by(username="admin_baru")
    ).scalar_one()
    assert user.check_password("PasswordBaru123")
    assert len(_logs(audit.ADMIN_CREATED)) == 1


def test_create_admin_menolak_password_pendek(app):
    runner = app.test_cli_runner()
    res = runner.invoke(
        args=["create-admin", "--username", "admin2", "--name", "X", "--password", "pendek"]
    )
    assert res.exit_code != 0
    assert "minimal 10 karakter" in res.output


def test_create_admin_menolak_username_duplikat(app, admin_user):
    runner = app.test_cli_runner()
    res = runner.invoke(
        args=[
            "create-admin",
            "--username", ADMIN_USERNAME,
            "--name", "X",
            "--password", "PasswordPanjang123",
        ]
    )
    assert res.exit_code != 0
    assert "sudah dipakai" in res.output
