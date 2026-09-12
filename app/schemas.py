from pydantic import BaseModel, Field, field_validator
from typing import Optional, List


class ScreenRequest(BaseModel):
    full_name: str = Field(..., min_length=2, max_length=200)
    cnic: Optional[str] = Field(default=None, max_length=20)
    father_name: Optional[str] = Field(default=None, max_length=200)

    @field_validator("full_name")
    @classmethod
    def name_not_blank(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("full_name cannot be blank")
        return v

    @field_validator("cnic", "father_name")
    @classmethod
    def strip_optional(cls, v):
        if v is None:
            return v
        v = v.strip()
        return v or None


class ScreeningResultOut(BaseModel):
    id: int
    source: str
    matched_entry: Optional[str]
    score: Optional[float]
    status: str
    detail: Optional[str]
    evidence_file: Optional[str]
    checked_at: str
    # Additive fields — an exact CNIC match is a distinct, stronger signal
    # than the fuzzy name score (see app/screening/matching.py), and
    # near_miss flags a score that came close to a threshold without
    # crossing it, for audit purposes. Both default False so this stays
    # backward compatible with any client built against the earlier schema.
    cnic_match: bool = False
    near_miss: bool = False


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


class NearMissEntry(BaseModel):
    id: int
    applicant_id: int
    source: str
    matched_entry: Optional[str]
    score: Optional[float]
    threshold: Optional[float]
    detail: Optional[str]
    logged_at: str
