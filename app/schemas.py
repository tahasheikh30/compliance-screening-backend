from pydantic import BaseModel
from typing import Optional, List


class ScreenRequest(BaseModel):
    full_name: str
    cnic: Optional[str] = None
    father_name: Optional[str] = None


class ScreeningResultOut(BaseModel):
    id: int
    source: str
    matched_entry: Optional[str]
    score: Optional[float]
    status: str
    detail: Optional[str]
    evidence_file: Optional[str]
    checked_at: str


class ScreenResponse(BaseModel):
    applicant_id: int
    full_name: str
    overall_status: str
    results: List[ScreeningResultOut]


class ApplicantSummary(BaseModel):
    id: int
    full_name: str
    cnic: Optional[str]
    submitted_at: str
    overall_status: str
