"""Fixture bersama untuk pytest. Memakai SQLite di memori, bukan MySQL."""
import pytest

from app import create_app
from extensions import db
from models import User

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
