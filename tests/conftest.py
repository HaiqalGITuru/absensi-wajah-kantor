"""Fixture bersama untuk pytest. Memakai SQLite di memori, bukan MySQL."""
from datetime import time

import pytest

from app import create_app
from extensions import db
from models import User, WorkSchedule, WorkScheduleDay

ADMIN_USERNAME = "admin_uji"
ADMIN_PASSWORD = "PasswordUji12345"


@pytest.fixture
def app():
    app = create_app("testing")
    with app.app_context():
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def client(app):
    return app.test_client()


@pytest.fixture
def admin_user(app):
    user = User(name="Admin Uji", username=ADMIN_USERNAME, role="admin")
    user.set_password(ADMIN_PASSWORD)
    db.session.add(user)
    db.session.commit()
    return user


def login(client, username=ADMIN_USERNAME, password=ADMIN_PASSWORD, **kwargs):
    return client.post(
        "/login",
        data={"username": username, "password": password},
        **kwargs,
    )


@pytest.fixture
def auth_client(client, admin_user):
    """Klien yang sudah login sebagai admin."""
    login(client)
    return client


@pytest.fixture
def schedule(app):
    """Pola kerja Diskominfo: Senin–Kamis 07:30–16:00, Jumat 07:30–11:30."""
    s = WorkSchedule(name="Jam Kerja Kantor", late_tolerance_minutes=15)
    for weekday in range(4):
        s.days.append(
            WorkScheduleDay(weekday=weekday, start_time=time(7, 30), end_time=time(16, 0))
        )
    s.days.append(WorkScheduleDay(weekday=4, start_time=time(7, 30), end_time=time(11, 30)))
    db.session.add(s)
    db.session.commit()
    return s


@pytest.fixture
def biometric_keys(app, tmp_path):
    """File kunci biometrik di folder sementara pengujian."""
    from services import biometric_service

    app.config["BIOMETRIC_KEY_FILE"] = str(tmp_path / "secrets" / "biometric_keys.json")
    biometric_service.generate_keyring()
    return app.config["BIOMETRIC_KEY_FILE"]


def make_embedding(seed=1, dim=512):
    """Embedding sintetis untuk pengujian (bukan data wajah asli)."""
    import math

    return [math.sin(seed * 1000 + i) for i in range(dim)]
