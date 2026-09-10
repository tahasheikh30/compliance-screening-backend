"""
FastAPI backend for the account-opening screening tool.

Run:
    uvicorn app.main:app --reload --port 8000

Endpoints:
    POST /api/screen          -> run all checks for one applicant
    GET  /api/applicants       -> list past screenings
    GET  /api/applicants/{id}  -> full result detail for one applicant
    GET  /api/evidence/{result_id} -> download the evidence PDF for a hit
    POST /api/admin/refresh    -> manually trigger UNSC + FIA cache refresh
"""

from pathlib import Path
from datetime import datetime, timezone

import os

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from app import database as db
from app.config import SCREENSHOT_DIR
from app.schemas import ScreenRequest, ScreenResponse, ScreeningResultOut, ApplicantSummary
from app.screening import unsc, fia_redbook, adverse_media
from app import evidence

app = FastAPI(title="Account Screening API")

# Comma-separated list in env, e.g.:
#   ALLOWED_ORIGINS=https://your-app.vercel.app,https://your-app-git-main.vercel.app
_default_origins = "http://localhost:5173"
allowed_origins = os.environ.get("ALLOWED_ORIGINS", _default_origins).split(",")

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)

MATCH_THRESHOLD = 85   # score >= this => HIT
REVIEW_THRESHOLD = 60  # score >= this => REVIEW (below => CLEAR)


@app.on_event("startup")
def startup():
    db.init_db()


def _status_for(score) -> str:
    if score is None:
        return "CLEAR"
    if score >= MATCH_THRESHOLD:
        return "HIT"
    if score >= REVIEW_THRESHOLD:
        return "REVIEW"
    return "CLEAR"


@app.post("/api/screen", response_model=ScreenResponse)
def screen_applicant(req: ScreenRequest):
    now = datetime.now(timezone.utc).isoformat()
    applicant_id = db.insert_applicant(req.full_name, req.cnic, req.father_name, now, "PENDING")

    results_out = []
    statuses = []

    # --- UNSC ---
    unsc_result = unsc.check(req.full_name, threshold=REVIEW_THRESHOLD)
    status = _status_for(unsc_result["score"])
    statuses.append(status)
    evidence_file = None
    if status == "HIT":
        result_id_placeholder = db.insert_result(
            applicant_id, "UNSC", unsc_result["matched_entry"], unsc_result["score"],
            status, unsc_result["detail"], None, now,
        )
        pdf_path = evidence.generate_evidence_pdf(
            req.full_name, req.cnic, "UNSC Consolidated Sanctions List",
            unsc_result["matched_entry"], unsc_result["score"], unsc_result["source_url"],
            image_path=None,  # UNSC is a static data feed, not a webpage — no screenshot
            result_id=result_id_placeholder,
        )
        evidence_file = pdf_path.name
        with db.get_conn() as conn:
            conn.execute("UPDATE screening_results SET evidence_file = ? WHERE id = ?",
                         (evidence_file, result_id_placeholder))
            conn.commit()
        results_out.append(ScreeningResultOut(
            id=result_id_placeholder, source="UNSC", matched_entry=unsc_result["matched_entry"],
            score=unsc_result["score"], status=status, detail=unsc_result["detail"],
            evidence_file=evidence_file, checked_at=now,
        ))
    else:
        rid = db.insert_result(applicant_id, "UNSC", unsc_result["matched_entry"], unsc_result["score"],
                                status, unsc_result["detail"], None, now)
        results_out.append(ScreeningResultOut(
            id=rid, source="UNSC", matched_entry=unsc_result["matched_entry"],
            score=unsc_result["score"], status=status, detail=unsc_result["detail"],
            evidence_file=None, checked_at=now,
        ))

    # --- FIA Red Book ---
    fia_result = fia_redbook.check(req.full_name, threshold=REVIEW_THRESHOLD)
    status = _status_for(fia_result["score"])
    statuses.append(status)
    if status == "HIT":
        result_id_placeholder = db.insert_result(
            applicant_id, "FIA_REDBOOK", fia_result["matched_entry"], fia_result["score"],
            status, fia_result["detail"], None, now,
        )
        image_path = None
        if fia_result.get("page_number") is not None:
            image_path = SCREENSHOT_DIR / f"fia_page_{result_id_placeholder}.png"
            fia_redbook.render_matched_page(fia_result["page_number"], image_path)
        pdf_path = evidence.generate_evidence_pdf(
            req.full_name, req.cnic, "FIA Red Book",
            fia_result["matched_entry"], fia_result["score"], fia_result["source_url"],
            image_path=image_path, result_id=result_id_placeholder,
        )
        evidence_file = pdf_path.name
        with db.get_conn() as conn:
            conn.execute("UPDATE screening_results SET evidence_file = ? WHERE id = ?",
                         (evidence_file, result_id_placeholder))
            conn.commit()
        results_out.append(ScreeningResultOut(
            id=result_id_placeholder, source="FIA_REDBOOK", matched_entry=fia_result["matched_entry"],
            score=fia_result["score"], status=status, detail=fia_result["detail"],
            evidence_file=evidence_file, checked_at=now,
        ))
    else:
        rid = db.insert_result(applicant_id, "FIA_REDBOOK", fia_result["matched_entry"], fia_result["score"],
                                status, fia_result["detail"], None, now)
        results_out.append(ScreeningResultOut(
            id=rid, source="FIA_REDBOOK", matched_entry=fia_result["matched_entry"],
            score=fia_result["score"], status=status, detail=fia_result["detail"],
            evidence_file=None, checked_at=now,
        ))

    # --- Adverse Media ---
    media_screenshot_path = SCREENSHOT_DIR / f"media_{applicant_id}.png"
    media_result = adverse_media.check(req.full_name, screenshot_out_path=media_screenshot_path)
    media_status = media_result.get("status") or _status_for(media_result.get("score"))
    statuses.append(media_status)
    evidence_file = None
    if media_status in ("HIT", "REVIEW") and media_result.get("screenshot_path"):
        result_id_placeholder = db.insert_result(
            applicant_id, "ADVERSE_MEDIA", media_result.get("matched_entry"), media_result.get("score"),
            media_status, media_result["detail"], None, now,
        )
        pdf_path = evidence.generate_evidence_pdf(
            req.full_name, req.cnic, "Adverse Media Search",
            media_result.get("matched_entry") or "See screenshot — raw search, not a confirmed match",
            media_result.get("score") or 0, media_result.get("source_url"),
            image_path=Path(media_result["screenshot_path"]), result_id=result_id_placeholder,
        )
        evidence_file = pdf_path.name
        with db.get_conn() as conn:
            conn.execute("UPDATE screening_results SET evidence_file = ? WHERE id = ?",
                         (evidence_file, result_id_placeholder))
            conn.commit()
        results_out.append(ScreeningResultOut(
            id=result_id_placeholder, source="ADVERSE_MEDIA", matched_entry=media_result.get("matched_entry"),
            score=media_result.get("score"), status=media_status, detail=media_result["detail"],
            evidence_file=evidence_file, checked_at=now,
        ))
    else:
        rid = db.insert_result(applicant_id, "ADVERSE_MEDIA", media_result.get("matched_entry"),
                                media_result.get("score"), media_status, media_result["detail"], None, now)
        results_out.append(ScreeningResultOut(
            id=rid, source="ADVERSE_MEDIA", matched_entry=media_result.get("matched_entry"),
            score=media_result.get("score"), status=media_status, detail=media_result["detail"],
            evidence_file=None, checked_at=now,
        ))

    # Overall routing
    if "HIT" in statuses:
        overall = "ESCALATE_TO_COMPLIANCE"
    elif "REVIEW" in statuses:
        overall = "MANUAL_REVIEW"
    else:
        overall = "AUTO_CLEAR"
    db.update_applicant_status(applicant_id, overall)

    return ScreenResponse(
        applicant_id=applicant_id, full_name=req.full_name,
        overall_status=overall, results=results_out,
    )


@app.get("/api/applicants", response_model=list[ApplicantSummary])
def list_applicants():
    return db.list_applicants()


@app.get("/api/applicants/{applicant_id}", response_model=ScreenResponse)
def get_applicant(applicant_id: int):
    applicant = db.get_applicant(applicant_id)
    if not applicant:
        raise HTTPException(404, "Applicant not found")
    results = db.get_results_for_applicant(applicant_id)
    return ScreenResponse(
        applicant_id=applicant_id, full_name=applicant["full_name"],
        overall_status=applicant["overall_status"],
        results=[ScreeningResultOut(**r) for r in results],
    )


@app.get("/api/evidence/{result_id}")
def download_evidence(result_id: int):
    result = db.get_result(result_id)
    if not result or not result.get("evidence_file"):
        raise HTTPException(404, "No evidence file for this result")
    path = evidence.EVIDENCE_DIR / result["evidence_file"]
    if not path.exists():
        raise HTTPException(404, "Evidence file missing on disk")
    return FileResponse(path, media_type="application/pdf", filename=result["evidence_file"])


@app.post("/api/admin/refresh-unsc")
def refresh_unsc_cache():
    """Refresh the UNSC sanctions list cache. Safe to call on a daily schedule."""
    return unsc.refresh_cache()


@app.post("/api/admin/fia-redbook/upload")
async def upload_fia_redbook(file: UploadFile = File(...)):
    """
    Upload the FIA Red Book PDF manually (downloaded from fia.gov.pk and
    reviewed) instead of relying on the scraper. Replaces the cached edition.
    """
    if file.content_type != "application/pdf" and not file.filename.lower().endswith(".pdf"):
        raise HTTPException(400, "Please upload a PDF file")
    contents = await file.read()
    result = fia_redbook.ingest_uploaded_pdf(contents)
    return result


@app.post("/api/admin/refresh")
def refresh_caches():
    """
    Legacy combined refresh — attempts the FIA scraper too. Prefer
    /api/admin/refresh-unsc + /api/admin/fia-redbook/upload if you're
    managing the Red Book edition manually.
    """
    unsc_status = unsc.refresh_cache()
    fia_status = fia_redbook.refresh_cache()
    return {"unsc": unsc_status, "fia_redbook": fia_status}


@app.get("/api/health")
def health():
    return {"status": "ok"}
