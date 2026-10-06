"""
Objek ekstensi Flask yang dipakai bersama di seluruh aplikasi.

Dipisahkan dari app.py agar models.py dan modul lain dapat mengimpor `db`
tanpa menimbulkan import melingkar.
"""
from flask_migrate import Migrate
from flask_sqlalchemy import SQLAlchemy
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
