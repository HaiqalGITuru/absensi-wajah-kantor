"""
Objek ekstensi Flask yang dipakai bersama di seluruh aplikasi.

Dipisahkan dari app.py agar models.py dan modul lain dapat mengimpor `db`
tanpa menimbulkan import melingkar.
"""
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect
from sqlalchemy import MetaData
from sqlalchemy.orm import DeclarativeBase

# Konvensi penamaan constraint agar nama index, unique key, dan foreign key
# konsisten dan migrasi Alembic dapat dilacak dengan rapi.
NAMING_CONVENTION = {
    "ix": "ix_%(column_0_label)s",
    "uq": "uq_%(table_name)s_%(column_0_N_name)s",
    "ck": "ck_%(table_name)s_%(constraint_name)s",
    "fk": "fk_%(table_name)s_%(column_0_name)s_%(referred_table_name)s",
    "pk": "pk_%(table_name)s",
}


class Base(DeclarativeBase):
    metadata = MetaData(naming_convention=NAMING_CONVENTION)


db = SQLAlchemy(model_class=Base)
migrate = Migrate()

# Sesi login admin (FR-01)
login_manager = LoginManager()
login_manager.login_view = "auth.login"
login_manager.login_message = "Silakan login untuk membuka halaman admin."
login_manager.login_message_category = "info"
login_manager.session_protection = "strong"

# Perlindungan CSRF untuk semua formulir POST (PRD 9.1)
csrf = CSRFProtect()
