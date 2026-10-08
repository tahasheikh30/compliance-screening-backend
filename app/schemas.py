import re
from typing import List, Literal, Optional

from pydantic import BaseModel, Field, field_validator

from app.screening import engine

# control and zero width characters have no place in a name and can hide text from a reviewer
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


class ScreenRequest(BaseModel):
    """
    Inputs of the screening form: name (required), date of birth, nationality and
    match threshold, plus `cnic` and `father_name`. The FIA Red Book and NACTA lists
    publish CNIC and father's name, so a CNIC that equals a listed CNIC is reported
    as a match whatever the name looks like, and a matching father's name is shown as
    supporting evidence. A CNIC that is not 13 digits is ignored.
    """
    full_name: str = Field(..., min_length=2, max_length=200)
    dob: Optional[str] = Field(default=None, max_length=30)
    nationality: Optional[str] = Field(default=None, max_length=100)
    # 50..100. Missing or out of range falls back to the default (85), as in the workflow.
    threshold: Optional[float] = None
    cnic: Optional[str] = Field(default=None, max_length=20)
    father_name: Optional[str] = Field(default=None, max_length=200)
    # Province or territory (Punjab, Sindh, KPK, ...). NACTA lists one, so a match can show whether it agrees.
    province: Optional[str] = Field(default=None, max_length=100)
    # Enrol this person in continuous monitoring: they are screened again whenever a watch list changes.
    monitor: bool = False

    @field_validator("full_name")
    @classmethod
    def name_not_blank(cls, v):
        v = _CONTROL.sub("", v).strip()
        if len(v) < 2:
            raise ValueError("full_name cannot be blank")
        try:
            engine.check_screenable(v)
        except engine.UnscreenableName as exc:
            raise ValueError(str(exc)) from None
        return v

    @field_validator("dob", "nationality", "cnic", "father_name", "province")
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
    cnic: str = ""
    father_name: str = ""
    cnic_match: Optional[bool] = None    # None: the applicant gave no CNIC, or the list has none for this person
    father_match: Optional[bool] = None
    province: str = ""
    province_match: Optional[bool] = None   # None: the applicant gave no province, or the list has none for this person


class ArticleOut(BaseModel):
    title: str
    link: str = ""
    published: str = ""
    source: str = ""
    keyword: str = ""


class ListInfoOut(BaseModel):
    list: str
    records: int = 0
    published: Optional[str] = None
    status: str = "OK"  # "OK", or why this list could not be read


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
    matches: List[MatchOut] = []      # best matches first, at most MAX_MATCHES per source
    match_count: Optional[int] = None  # true number of matches found, may exceed len(matches)
    articles: List[ArticleOut] = []
    lists: List[ListInfoOut] = []      # every list behind this source and whether it could be read


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
    monitored: bool = False


class ApplicantSummary(BaseModel):
    id: int
    full_name: str
    cnic: Optional[str]
    submitted_at: str
    overall_status: str
    dob: Optional[str] = None
    nationality: Optional[str] = None
    screened_by: Optional[str] = None   # email of the analyst who ran it (shown to admins, who see everyone's)
    monitored: bool = False
    last_monitored_at: Optional[str] = None


class MeOut(BaseModel):
    id: Optional[str]
    email: str
    role: str       # "user" or "admin"
    status: str     # "pending", "approved", "rejected" or "disabled"


class UserOut(BaseModel):
    id: str
    email: str
    role: str
    status: str
    created_at: str
    decided_at: Optional[str] = None


class UserStatusIn(BaseModel):
    status: Literal["approved", "rejected", "pending", "disabled"]


class UserDeletedOut(BaseModel):
    id: str
    email: str
    sign_in_removed: bool       # the person's sign in account at the authentication service was removed too


class UserRoleIn(BaseModel):
    role: Literal["user", "admin"]


class AuditEntryOut(BaseModel):
    id: int
    at: str
    actor_id: Optional[str] = None
    actor_email: Optional[str] = None
    via: str = "token"
    action: str
    target_type: Optional[str] = None
    target_id: Optional[str] = None
    detail: Optional[dict] = None
    request_id: Optional[str] = None
    ip: Optional[str] = None
    row_hash: str


class AuditPageOut(BaseModel):
    total: int
    entries: List[AuditEntryOut]


class AuditVerifyOut(BaseModel):
    ok: bool
    checked: int
    first_bad_id: Optional[int] = None
    head: Optional[str] = None


class MonitoringIn(BaseModel):
    enabled: bool


class MonitoringOut(BaseModel):
    applicant_id: int
    monitored: bool
    monitored_since: Optional[str] = None
    last_monitored_at: Optional[str] = None
    new_alerts: int = 0       # found by the check that runs at the moment of enrolment


class AlertOut(BaseModel):
    id: int
    applicant_id: int
    applicant_name: str
    source: str
    list: str = ""
    ref: str
    matched_name: Optional[str] = None
    score: Optional[float] = None
    status: Literal["open", "confirmed", "dismissed"]
    created_at: str
    decided_at: Optional[str] = None
    note: Optional[str] = None
    match: Optional[dict] = None   # the full potential match, as in a screening result


class AlertDecisionIn(BaseModel):
    status: Literal["open", "confirmed", "dismissed"]
    note: Optional[str] = Field(default=None, max_length=500)

    @field_validator("note")
    @classmethod
    def clean_note(cls, v):
        v = _CONTROL.sub("", v).strip() if v else v
        return v or None


class SourceMonitoringOut(BaseModel):
    source: str
    last_checked_at: Optional[str] = None
    applicants_checked: Optional[int] = None
    new_alerts: Optional[int] = None


class MonitoringStatusOut(BaseModel):
    enabled: bool
    interval_seconds: float
    monitored_applicants: int
    open_alerts: int
    sources: List[SourceMonitoringOut]


class BatchRowOut(BaseModel):
    row: int                              # the row in the uploaded file
    full_name: str
    # pending: waiting its turn. screened: has a case (applicant_id). invalid: the row itself was unusable.
    # failed: the screening raised an error. Only a screened row has an outcome; nothing else is ever "clear".
    state: Literal["pending", "screened", "invalid", "failed"]
    error: Optional[str] = None
    applicant_id: Optional[int] = None
    overall_status: Optional[str] = None
    sanctions: Optional[int] = None       # potential matches across the sanctions and watch lists
    news: Optional[int] = None            # adverse news articles found
    case_ref: Optional[str] = None
    dob: Optional[str] = None             # what the file said, for the case view (as in the history list)
    nationality: Optional[str] = None


class BatchOut(BaseModel):
    id: int
    filename: str
    status: Literal["running", "done", "cancelled", "interrupted"]
    total: int
    done: int                             # rows no longer pending (screened, invalid or failed)
    threshold: float
    monitor: bool
    created_at: str
    finished_at: Optional[str] = None
    counts: dict
    rows: List[BatchRowOut] = []
