from typing import List, Optional

from pydantic import BaseModel, Field, field_validator


class ScreenRequest(BaseModel):
    """
    Inputs of the n8n "Applicant Sanctions Screening" form: name (required),
    date of birth, nationality and match threshold. `cnic` and `father_name`
    are kept so existing clients keep working; they are stored with the
    applicant but do not influence matching.
    """
    full_name: str = Field(..., min_length=2, max_length=200)
    dob: Optional[str] = Field(default=None, max_length=30)
    nationality: Optional[str] = Field(default=None, max_length=100)
    # 50..100. Missing or out of range falls back to the default (85), as in the workflow.
    threshold: Optional[float] = None
    cnic: Optional[str] = Field(default=None, max_length=20)
    father_name: Optional[str] = Field(default=None, max_length=200)

    @field_validator("full_name")
    @classmethod
    def name_not_blank(cls, v):
        v = v.strip()
        if len(v) < 2:
            raise ValueError("full_name cannot be blank")
        return v

    @field_validator("dob", "nationality", "cnic", "father_name")
    @classmethod
    def strip_optional(cls, v):
        if v is None:
            return v
        v = v.strip()
        return v or None


class MatchOut(BaseModel):
    list: str
    id: str
    score: float
    matched_name: str
    primary_name: str
    type: str = ""
    programs: str = ""
    dob: str = ""
    dob_year_match: str = "n/a"
    nationality: str = ""
    listed_on: str = ""
    remarks: str = ""
    aliases: List[str] = []


class ArticleOut(BaseModel):
    title: str
    link: str = ""
    published: str = ""
    source: str = ""
    keyword: str = ""


class ScreeningResultOut(BaseModel):
    id: int
    source: str
    matched_entry: Optional[str]
    score: Optional[float]
    status: str
    detail: Optional[str]
    evidence_file: Optional[str]
    checked_at: str
    # kept for backward compatibility with clients built on the earlier schema
    cnic_match: bool = False
    near_miss: bool = False
    # publication date(s) of the list this result was checked against
    list_version: Optional[str] = None
    # new: how many records were screened, and the full match / article detail
    records_screened: Optional[int] = None
    matches: List[MatchOut] = []
    articles: List[ArticleOut] = []


class ScreenResponse(BaseModel):
    applicant_id: int
    full_name: str
    overall_status: str
    results: List[ScreeningResultOut]
    case_ref: Optional[str] = None
    threshold: Optional[float] = None
    records_screened: Optional[int] = None
    sanctions_hit_count: Optional[int] = None
    media_hit_count: Optional[int] = None


class ApplicantSummary(BaseModel):
    id: int
    full_name: str
    cnic: Optional[str]
    submitted_at: str
    overall_status: str
    dob: Optional[str] = None
    nationality: Optional[str] = None
