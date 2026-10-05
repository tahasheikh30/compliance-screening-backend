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

ALTER TABLE profiles          ENABLE ROW LEVEL SECURITY;
ALTER TABLE applicants        ENABLE ROW LEVEL SECURITY;
ALTER TABLE screening_results ENABLE ROW LEVEL SECURITY;
ALTER TABLE evidence_files    ENABLE ROW LEVEL SECURITY;
ALTER TABLE nacta_list        ENABLE ROW LEVEL SECURITY;
