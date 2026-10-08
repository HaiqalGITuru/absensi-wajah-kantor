"""
Token API baca-saja.

- Token berbentuk "aw_" + 43 karakter acak (256 bit).
- Database hanya menyimpan SHA-256 token dan 10 karakter awal (prefix) untuk
  dikenali di daftar; token asli tidak bisa dipulihkan dari database.
- Setiap token punya masa berlaku (bawaan 90 hari) dan bisa dicabut.
- Pembuatan dan pencabutan tercatat di audit log.
"""
import hashlib
import secrets
from datetime import timedelta

from extensions import db
from models import ApiToken, office_now
from services import ValidationError
from services import audit_service as audit

TOKEN_PREFIX = "aw_"
DEFAULT_DAYS = 90
MAX_DAYS = 365
SCOPES = ("read", "write")
MAX_WRITE_DAYS = 90   # token tulis lebih berisiko: masa berlaku lebih pendek
LAST_USED_RESOLUTION = timedelta(minutes=1)   # batasi penulisan last_used_at


def hash_token(token):
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def create_token(name, actor_id, days=DEFAULT_DAYS, scope="read"):
    """Mengembalikan (token_asli, ApiToken). Token asli hanya ada di sini."""
    name = " ".join((name or "").split())
    errors = {}
    if not 3 <= len(name) <= 100:
        errors["name"] = "Nama pemakai token 3-100 karakter (mis. 'Andi - aplikasi rekap')."
    if scope not in SCOPES:
        errors["scope"] = "Cakupan token harus 'read' atau 'write'."
    limit = MAX_WRITE_DAYS if scope == "write" else MAX_DAYS
    if not 1 <= int(days) <= limit:
        errors["days"] = f"Masa berlaku token {scope} 1-{limit} hari."
    if errors:
        raise ValidationError(errors)

    token = TOKEN_PREFIX + secrets.token_urlsafe(32)
    row = ApiToken(
        name=name, token_hash=hash_token(token), prefix=token[:10], scope=scope,
        created_by_id=actor_id, expires_at=office_now() + timedelta(days=int(days)),
    )
    db.session.add(row)
    db.session.flush()
    audit.log_event(
        audit.API_TOKEN_CREATED, user_id=actor_id, entity_type="api_token", entity_id=row.id,
        description=(f"Token API {scope.upper()} dibuat untuk '{name}' ({row.prefix}…), "
                     f"berlaku {days} hari."),
        commit=False,
    )
    db.session.commit()
    return token, row


def authenticate(token, now=None):
    """Mengembalikan ApiToken yang sah, atau None."""
    if not token or not token.startswith(TOKEN_PREFIX) or len(token) > 100:
        return None
    now = now or office_now()
    row = db.session.execute(
        db.select(ApiToken).filter_by(token_hash=hash_token(token))
    ).scalar_one_or_none()
    if row is None or not row.is_usable(now):
        return None
    if row.last_used_at is None or now - row.last_used_at >= LAST_USED_RESOLUTION:
        row.last_used_at = now
        db.session.commit()
    return row


def revoke(token_id, reason, actor_id):
    reason = " ".join((reason or "").split())
    if len(reason) < 5:
        raise ValidationError({"reason": "Alasan pencabutan wajib diisi (minimal 5 karakter)."})
    row = db.session.get(ApiToken, token_id)
    if row is None:
        raise ValidationError({"_": f"Token dengan id {token_id} tidak ditemukan."})
    if row.revoked_at is not None:
        raise ValidationError({"_": "Token ini sudah dicabut sebelumnya."})
    row.revoked_at = office_now()
    row.revoke_reason = reason[:255]
    audit.log_event(
        audit.API_TOKEN_REVOKED, user_id=actor_id, entity_type="api_token", entity_id=row.id,
        description=f"Token API '{row.name}' ({row.prefix}…) dicabut. Alasan: {reason}",
        commit=False,
    )
    db.session.commit()
    return row


def list_tokens():
    return db.session.execute(db.select(ApiToken).order_by(ApiToken.id)).scalars().all()
