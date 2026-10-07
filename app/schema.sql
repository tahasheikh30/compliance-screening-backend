-- Schema of the screening database (PostgreSQL, hosted on Supabase).
--
-- Every statement is idempotent: the backend runs this file on startup, and it is also what
-- tests use to build a fresh database. The Supabase specific parts (link to auth.users, the
-- sign up trigger, revoking the public API's access) are in supabase/setup.sql.
--
-- Row Level Security is switched on for every table and no policy is created. The backend
-- connects with the database owner role, which is not subject to RLS, so it works as before.
-- Anyone using Supabase's public REST API with the anon key gets nothing.

-- One row per person who signed up. status gates access (a new user is "pending" until an
-- admin approves them); role is "user" or "admin".
CREATE TABLE IF NOT EXISTS profiles (
    id          uuid PRIMARY KEY,
    email       text NOT NULL DEFAULT '',
    status      text NOT NULL DEFAULT 'pending' CHECK (status IN ('pending', 'approved', 'rejected')),
    role        text NOT NULL DEFAULT 'user' CHECK (role IN ('user', 'admin')),
    created_at  timestamptz NOT NULL DEFAULT now(),
    decided_at  timestamptz,
    decided_by  uuid REFERENCES profiles (id) ON DELETE SET NULL
);
CREATE INDEX IF NOT EXISTS idx_profiles_decided_by ON profiles (decided_by);

-- One row per screening request. user_id is the analyst who ran it; if that account is ever
-- deleted the screening stays (user_id becomes NULL and only admins can see it).
CREATE TABLE IF NOT EXISTS applicants (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    user_id           uuid REFERENCES profiles (id) ON DELETE SET NULL,
    full_name         text NOT NULL,
    cnic              text,
    father_name       text,
    dob               text,
    nationality       text,
    threshold         double precision,
    records_screened  integer,
    submitted_at      timestamptz NOT NULL,
    overall_status    text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_applicants_user ON applicants (user_id, id DESC);

-- One row per source checked for that applicant (UNSC / OFAC / UKSL / FIA_REDBOOK / ADVERSE_MEDIA /
-- NACTA). payload holds the full match and article detail; evidence_file is the evidence PDF's name
-- when the screening found something.
CREATE TABLE IF NOT EXISTS screening_results (
    id                bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    applicant_id      bigint NOT NULL REFERENCES applicants (id) ON DELETE CASCADE,
    source            text NOT NULL,
    matched_entry     text,
    score             double precision,
    status            text NOT NULL,
    detail            text,
    evidence_file     text,
    checked_at        timestamptz NOT NULL,
    list_version      text,
    records_screened  integer,
    payload           jsonb
);
CREATE INDEX IF NOT EXISTS idx_results_applicant ON screening_results (applicant_id);

-- The evidence PDF of a screening (at most one per screening), stored in the database so it
-- survives restarts of the web server.
CREATE TABLE IF NOT EXISTS evidence_files (
    applicant_id  bigint PRIMARY KEY REFERENCES applicants (id) ON DELETE CASCADE,
    filename      text NOT NULL,
    content       bytea NOT NULL,
    created_at    timestamptz NOT NULL DEFAULT now()
);

-- The NACTA Proscribed Persons list as uploaded. Only one row (id = 1) can exist: each upload
-- replaces the previous list in a single statement.
CREATE TABLE IF NOT EXISTS nacta_list (
    id           smallint PRIMARY KEY DEFAULT 1 CHECK (id = 1),
    content      bytea NOT NULL,
    filename     text NOT NULL,
    uploaded_at  timestamptz NOT NULL,
    records      integer NOT NULL,
    size         integer NOT NULL,
    sha256       text NOT NULL,
    live         boolean NOT NULL DEFAULT false
);

-- Integrity of the evidence: SHA-256 of the PDF, so a copy that was altered after the screening is detectable.
ALTER TABLE evidence_files ADD COLUMN IF NOT EXISTS sha256 text;

-- Who did what. Append only, and tamper evident: every entry stores the hash of the previous one
-- (prev_hash) and its own (row_hash = SHA-256 of prev_hash plus the entry), so changing or removing any
-- past entry breaks the chain from that point on, which GET /api/admin/audit/verify reports. The entry
-- holds ids and outcomes, never an applicant's name or CNIC. actor_id has no foreign key on purpose: the
-- record must outlive the account.
CREATE TABLE IF NOT EXISTS audit_log (
    id           bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    at           timestamptz NOT NULL,
    actor_id     uuid,
    actor_email  text,
    via          text NOT NULL DEFAULT 'token',
    action       text NOT NULL,
    target_type  text,
    target_id    text,
    detail       jsonb,
    request_id   text,
    ip           text,
    prev_hash    text NOT NULL,
    row_hash     text NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_audit_at ON audit_log (at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_audit_actor ON audit_log (actor_id, id DESC);

CREATE OR REPLACE FUNCTION audit_log_append_only() RETURNS trigger LANGUAGE plpgsql AS $$
BEGIN
    RAISE EXCEPTION 'audit_log is append only';
END;
$$;
DROP TRIGGER IF EXISTS audit_log_no_change ON audit_log;
CREATE TRIGGER audit_log_no_change BEFORE UPDATE OR DELETE ON audit_log
    FOR EACH ROW EXECUTE FUNCTION audit_log_append_only();

-- Continuous monitoring. An applicant is only re-screened when someone enrolled them (monitored); keeping
-- someone under watch is a decision, not a default. monitoring_alerts holds each NEW potential match found
-- by a re-screen: the unique key means a match is raised once, however many times a list changes.
ALTER TABLE applicants ADD COLUMN IF NOT EXISTS monitored          boolean NOT NULL DEFAULT false;
ALTER TABLE applicants ADD COLUMN IF NOT EXISTS monitored_since    timestamptz;
ALTER TABLE applicants ADD COLUMN IF NOT EXISTS last_monitored_at  timestamptz;
CREATE INDEX IF NOT EXISTS idx_applicants_monitored ON applicants (id) WHERE monitored;

CREATE TABLE IF NOT EXISTS monitoring_alerts (
    id             bigint GENERATED ALWAYS AS IDENTITY PRIMARY KEY,
    applicant_id   bigint NOT NULL REFERENCES applicants (id) ON DELETE CASCADE,
    source         text NOT NULL,
    list           text NOT NULL DEFAULT '',
    ref            text NOT NULL,
    matched_name   text,
    score          double precision,
    payload        jsonb,
    status         text NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'confirmed', 'dismissed')),
    created_at     timestamptz NOT NULL DEFAULT now(),
    decided_at     timestamptz,
    decided_by     uuid REFERENCES profiles (id) ON DELETE SET NULL,
    note           text,
    UNIQUE (applicant_id, source, list, ref)
);
CREATE INDEX IF NOT EXISTS idx_alerts_status ON monitoring_alerts (status, id DESC);
CREATE INDEX IF NOT EXISTS idx_alerts_decided_by ON monitoring_alerts (decided_by);

-- The fingerprint of each source the last time every monitored applicant was checked against it.
CREATE TABLE IF NOT EXISTS monitoring_state (
    source       text PRIMARY KEY,
    fingerprint  text NOT NULL,
    checked_at   timestamptz NOT NULL DEFAULT now(),
    rescreened   integer NOT NULL DEFAULT 0,
    new_alerts   integer NOT NULL DEFAULT 0
);

ALTER TABLE profiles          ENABLE ROW LEVEL SECURITY;
ALTER TABLE applicants        ENABLE ROW LEVEL SECURITY;
ALTER TABLE screening_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_files    ENABLE ROW LEVEL SECURITY;
ALTER TABLE nacta_list        ENABLE ROW LEVEL SECURITY;
ALTER TABLE audit_log         ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitoring_alerts ENABLE ROW LEVEL SECURITY;
ALTER TABLE monitoring_state  ENABLE ROW LEVEL SECURITY;
