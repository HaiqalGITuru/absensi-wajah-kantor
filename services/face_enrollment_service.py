"""
Antrean pendaftaran wajah dari panel admin lain (mis. Laravel) lewat API.

Alur:
  API create()  -> status "pending" (kedaluwarsa jika tidak diambil 2 menit)
  kios claim_next() -> "running"; kemajuan lewat update_progress()
  kios finish()  -> "done" | "failed" | "cancelled"
  API cancel()   -> "cancelled" (kios berhenti di pemeriksaan berikutnya)

Hanya SATU permintaan aktif dalam satu waktu: kamera kios hanya satu.
"""
from datetime import timedelta

from extensions import db
from models import FaceEnrollmentRequest, office_now
from services import ValidationError
from services import audit_service as audit
from services.biometric_service import templates_for

PENDING_SECONDS = 120          # menunggu diambil layanan kamera kios
RUNNING_STALE_SECONDS = 300    # dianggap terhenti (mis. layanan kamera mati di tengah)
SAMPLES = 5
ACTIVE = ("pending", "running")
FINAL = ("done", "failed", "cancelled", "expired")


def expire_stale(now=None):
    now = now or office_now()
    changed = False
    rows = db.session.execute(
        db.select(FaceEnrollmentRequest).where(FaceEnrollmentRequest.status.in_(ACTIVE))
    ).scalars().all()
    for r in rows:
        if r.status == "pending" and now >= r.expires_at:
            r.status, r.finished_at = "expired", now
            r.message = ("Layanan kamera kios tidak mengambil permintaan dalam 2 menit. "
                         "Pastikan jalankan_kios.bat berjalan, lalu coba lagi.")
            changed = True
        elif r.status == "running" and r.started_at and \
                (now - r.started_at).total_seconds() > RUNNING_STALE_SECONDS:
            r.status, r.finished_at = "failed", now
            r.message = "Pendaftaran terhenti (layanan kamera kios berhenti di tengah proses)."
            changed = True
    if changed:
        db.session.commit()


def active_request():
    return db.session.execute(
        db.select(FaceEnrollmentRequest).where(FaceEnrollmentRequest.status.in_(ACTIVE))
        .order_by(FaceEnrollmentRequest.id).limit(1)
    ).scalar_one_or_none()


def create(employee, replace_reason=None, via=None, now=None):
    now = now or office_now()
    expire_stale(now)
    if not employee.is_active:
        raise ValidationError({"_": f"Karyawan {employee.employee_code} nonaktif; aktifkan dulu."})
    reason = " ".join((replace_reason or "").split()) or None
    if templates_for(employee) and not reason:
        raise ValidationError({"replace_reason": (
            "Karyawan ini sudah punya wajah terdaftar. Isi alasan daftar ulang (minimal 5 karakter).")})
    if reason is not None and len(reason) < 5:
        raise ValidationError({"replace_reason": "Alasan daftar ulang minimal 5 karakter."})
    busy = active_request()
    if busy is not None:
        raise ValidationError({"_": (
            f"Kios sedang melayani pendaftaran wajah lain ({busy.employee.employee_code}). "
            "Tunggu sampai selesai atau batalkan dulu.")})

    req = FaceEnrollmentRequest(
        employee_id=employee.id, status="pending", replace_reason=reason,
        requested_via=(via or "")[:255] or None, samples_total=SAMPLES, samples_taken=0,
        message="Menunggu layanan kamera kios...",
        expires_at=now + timedelta(seconds=PENDING_SECONDS),
    )
    db.session.add(req)
    db.session.flush()
    audit.log_event(
        audit.FACE_ENROLL_REQUESTED, entity_type="employee", entity_id=employee.id,
        description=(f"Pendaftaran wajah {employee.employee_code} - {employee.full_name} diminta "
                     f"(permintaan #{req.id}{', daftar ulang: ' + reason if reason else ''})."),
        commit=False,
    )
    db.session.commit()
    return req


def get(request_id):
    expire_stale()
    return db.session.get(FaceEnrollmentRequest, request_id)


def cancel(req):
    if req.status not in ACTIVE:
        raise ValidationError({"_": "Permintaan ini sudah selesai dan tidak bisa dibatalkan."})
    req.status, req.finished_at = "cancelled", office_now()
    req.message = "Dibatalkan dari panel admin."
    audit.log_event(
        audit.FACE_ENROLL_CANCELLED, entity_type="employee", entity_id=req.employee_id,
        description=f"Pendaftaran wajah (permintaan #{req.id}) dibatalkan.", commit=False,
    )
    db.session.commit()
    return req


# ------------------------------------------------------------------- sisi kios

def claim_next(now=None):
    now = now or office_now()
    expire_stale(now)
    req = db.session.execute(
        db.select(FaceEnrollmentRequest).filter_by(status="pending")
        .order_by(FaceEnrollmentRequest.id).limit(1)
    ).scalar_one_or_none()
    if req is None:
        return None
    req.status, req.started_at = "running", now
    req.message = "Bersiap di depan kamera..."
    db.session.commit()
    return req


def update_progress(req, taken, total, message):
    req.samples_taken, req.samples_total = int(taken), int(total)
    req.message = (message or "")[:255]
    db.session.commit()


def is_cancelled(req):
    status = db.session.execute(
        db.select(FaceEnrollmentRequest.status).filter_by(id=req.id)).scalar_one_or_none()
    return status == "cancelled"


def finish(req, status, message):
    db.session.refresh(req)
    if req.status == "cancelled" and status != "done":
        return req          # sudah dibatalkan admin; jangan ditimpa
    req.status, req.finished_at = status, office_now()
    req.message = (message or "")[:255]
    db.session.commit()
    return req
