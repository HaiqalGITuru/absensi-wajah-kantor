"""
Laporan kehadiran per periode dan ekspor CSV (Tahap 5B, FR-13). Wajib login admin.
"""
from datetime import datetime

from flask import Blueprint, Response, render_template, request
from flask_login import current_user, login_required

from models import office_now
from services import ValidationError
from services import audit_service as audit
from services import employee_service
from services import report_service as rpt

bp = Blueprint("reports", __name__, url_prefix="/admin/reports")


def _parse(text):
    try:
        return datetime.strptime((text or "").strip(), "%Y-%m-%d").date()
    except ValueError:
        return None


def _filters():
    today = office_now().date()
    start_text = request.args.get("start")
    end_text = request.args.get("end")
    start = _parse(start_text) if start_text else today.replace(day=1)
    end = _parse(end_text) if end_text else today
    return {
        "start": start, "end": end, "today": today,
        "q": request.args.get("q", "").strip(),
        "department": request.args.get("department", "").strip(),
    }


@bp.get("/")
@login_required
def index():
    f = _filters()
    errors, details, summary = {}, [], []
    try:
        start, end = rpt.validate_period(f["start"], f["end"], f["today"])
        details = rpt.detail_rows(start, end, f["q"], f["department"], f["today"])
        summary = rpt.summary_rows(details)
        f["end"] = end
    except ValidationError as exc:
        errors = exc.errors
    totals = {
        key: sum(i[key] for i in summary)
        for key in ("workdays", "present", "late", "late_minutes", "left_early", "absent")
    }
    return render_template(
        "reports.html", f=f, errors=errors, summary=summary, totals=totals,
        departments=employee_service.list_departments(), detail_count=len(details),
    ), (400 if errors else 200)


@bp.get("/export/<kind>.csv")
@login_required
def export(kind):
    if kind not in ("rekap", "detail"):
        return Response("Jenis laporan tidak dikenal.", status=404)
    f = _filters()
    try:
        start, end = rpt.validate_period(f["start"], f["end"], f["today"])
    except ValidationError as exc:
        return Response(" ".join(exc.errors.values()), status=400, mimetype="text/plain")

    details = rpt.detail_rows(start, end, f["q"], f["department"], f["today"])
    if kind == "rekap":
        content, count = rpt.summary_csv(rpt.summary_rows(details)), len({d["employee"].id for d in details})
    else:
        content, count = rpt.detail_csv(details), len(details)

    filters = []
    if f["q"]:
        filters.append(f"cari '{f['q']}'")
    if f["department"]:
        filters.append(f"departemen {f['department']}")
    audit.log_event(
        audit.REPORT_EXPORTED,
        user_id=current_user.id,
        entity_type="report",
        description=(
            f"Ekspor laporan {kind} {start:%d-%m-%Y} s.d. {end:%d-%m-%Y}, {count} baris"
            + (f" ({', '.join(filters)})" if filters else "")
        ),
    )
    filename = f"{kind}_absensi_{start:%Y-%m-%d}_{end:%Y-%m-%d}.csv"
    return Response(
        content.encode("utf-8"),
        mimetype="text/csv",
        headers={
            "Content-Disposition": f'attachment; filename="{filename}"',
            "Cache-Control": "no-store",
        },
    )
