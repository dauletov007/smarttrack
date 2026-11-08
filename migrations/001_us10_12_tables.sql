-- SmartTrack US10–12 base tables (from the earlier delivery)
-- Generated from academic.py. Idempotent and additive: safe to run more than once.
-- Prefer `python3 app.py init-db`, which runs exactly these statements.
BEGIN;

CREATE TABLE IF NOT EXISTS subjects (
 id BIGSERIAL PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', archived_at BIGINT);

CREATE TABLE IF NOT EXISTS semesters (
 id BIGSERIAL PRIMARY KEY, label TEXT NOT NULL UNIQUE, start_date DATE NOT NULL,
 end_date DATE NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
 CHECK (status IN ('draft','active','closed')), CHECK(end_date >= start_date));

CREATE TABLE IF NOT EXISTS academic_groups (
 id BIGSERIAL PRIMARY KEY, group_code TEXT NOT NULL UNIQUE, created_at BIGINT NOT NULL);

CREATE TABLE IF NOT EXISTS group_memberships (
 id BIGSERIAL PRIMARY KEY, student_id BIGINT NOT NULL REFERENCES users(id),
 group_id BIGINT REFERENCES academic_groups(id) ON DELETE SET NULL,
 group_code_snapshot TEXT NOT NULL, since_at BIGINT NOT NULL, until_at BIGINT,
 CHECK(until_at IS NULL OR until_at >= since_at));

CREATE UNIQUE INDEX IF NOT EXISTS one_current_group_per_student
 ON group_memberships(student_id) WHERE until_at IS NULL;

CREATE TABLE IF NOT EXISTS group_join_requests (
 id BIGSERIAL PRIMARY KEY, student_id BIGINT NOT NULL REFERENCES users(id),
 group_id BIGINT NOT NULL REFERENCES academic_groups(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 created_at BIGINT NOT NULL, reviewed_by BIGINT REFERENCES users(id), reviewed_at BIGINT);

CREATE UNIQUE INDEX IF NOT EXISTS one_pending_join_request
 ON group_join_requests(student_id) WHERE status='pending';

CREATE TABLE IF NOT EXISTS course_offerings (
 id BIGSERIAL PRIMARY KEY, subject_id BIGINT NOT NULL REFERENCES subjects(id),
 semester_id BIGINT NOT NULL REFERENCES semesters(id),
 group_id BIGINT REFERENCES academic_groups(id) ON DELETE SET NULL,
 group_code_snapshot TEXT NOT NULL, teacher_id BIGINT REFERENCES users(id),
 created_at BIGINT NOT NULL, UNIQUE(subject_id, semester_id, group_id));

CREATE TABLE IF NOT EXISTS teacher_assignments_history (
 id BIGSERIAL PRIMARY KEY, offering_id BIGINT NOT NULL REFERENCES course_offerings(id),
 old_teacher_id BIGINT REFERENCES users(id), new_teacher_id BIGINT REFERENCES users(id),
 changed_by BIGINT REFERENCES users(id), changed_at BIGINT NOT NULL);

CREATE TABLE IF NOT EXISTS assessments (
 id BIGSERIAL PRIMARY KEY, offering_id BIGINT NOT NULL REFERENCES course_offerings(id),
 kind TEXT NOT NULL CHECK(kind IN ('assignment','exam')),
 title TEXT NOT NULL, max_score NUMERIC(9,2) NOT NULL CHECK(max_score > 0),
 created_by BIGINT REFERENCES users(id), created_at BIGINT NOT NULL);

CREATE TABLE IF NOT EXISTS grades (
 id BIGSERIAL PRIMARY KEY, assessment_id BIGINT NOT NULL REFERENCES assessments(id),
 student_id BIGINT NOT NULL REFERENCES users(id), score NUMERIC(9,2) NOT NULL CHECK(score >= 0),
 created_by BIGINT REFERENCES users(id), created_at BIGINT NOT NULL,
 updated_at BIGINT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(assessment_id,student_id));

CREATE TABLE IF NOT EXISTS grade_revisions (
 id BIGSERIAL PRIMARY KEY, grade_id BIGINT NOT NULL REFERENCES grades(id),
 old_score NUMERIC(9,2) NOT NULL, new_score NUMERIC(9,2) NOT NULL,
 editor_id BIGINT REFERENCES users(id), edited_at BIGINT NOT NULL, reason TEXT NOT NULL DEFAULT '');

COMMIT;
