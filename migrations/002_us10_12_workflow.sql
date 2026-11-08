-- SmartTrack US10–12 v2: semesters current flag, soft-deleted groups, archived courses, assessment approval, audit details
-- Generated from academic.py. Idempotent and additive: safe to run more than once.
-- Prefer `python3 app.py init-db`, which runs exactly these statements.
BEGIN;

ALTER TABLE semesters ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT FALSE;

ALTER TABLE semesters ADD COLUMN IF NOT EXISTS closed_at BIGINT;

CREATE UNIQUE INDEX IF NOT EXISTS one_current_semester ON semesters((TRUE)) WHERE is_current;

UPDATE semesters SET is_current=TRUE WHERE id=(SELECT id FROM semesters WHERE status='active'
   ORDER BY start_date DESC, id DESC LIMIT 1) AND NOT EXISTS (SELECT 1 FROM semesters WHERE is_current);

ALTER TABLE academic_groups ADD COLUMN IF NOT EXISTS deleted_at BIGINT;

ALTER TABLE course_offerings ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active';

ALTER TABLE course_offerings ADD COLUMN IF NOT EXISTS archived_at BIGINT;

DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='course_offerings_status_check') THEN
   ALTER TABLE course_offerings ADD CONSTRAINT course_offerings_status_check CHECK (status IN ('active','archived'));
   END IF; END $$;

ALTER TABLE teacher_assignments_history ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT '';

ALTER TABLE assessments ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'approved';

ALTER TABLE assessments ALTER COLUMN status SET DEFAULT 'pending';

DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='assessments_status_check') THEN
   ALTER TABLE assessments ADD CONSTRAINT assessments_status_check CHECK (status IN ('draft','pending','approved','rejected'));
   END IF; END $$;

ALTER TABLE assessments ADD COLUMN IF NOT EXISTS scale TEXT NOT NULL DEFAULT 'points';

ALTER TABLE assessments ADD COLUMN IF NOT EXISTS reviewed_by BIGINT REFERENCES users(id);

ALTER TABLE assessments ADD COLUMN IF NOT EXISTS reviewed_at BIGINT;

ALTER TABLE assessments ADD COLUMN IF NOT EXISTS review_note TEXT NOT NULL DEFAULT '';

ALTER TABLE assessments ADD COLUMN IF NOT EXISTS updated_at BIGINT;

ALTER TABLE grades ADD COLUMN IF NOT EXISTS updated_by BIGINT REFERENCES users(id);

ALTER TABLE audit ADD COLUMN IF NOT EXISTS details TEXT NOT NULL DEFAULT '';

CREATE INDEX IF NOT EXISTS idx_offerings_teacher ON course_offerings(teacher_id);

CREATE INDEX IF NOT EXISTS idx_offerings_group ON course_offerings(group_id);

CREATE INDEX IF NOT EXISTS idx_memberships_group_current ON group_memberships(group_id) WHERE until_at IS NULL;

CREATE INDEX IF NOT EXISTS idx_assessments_offering ON assessments(offering_id);

CREATE INDEX IF NOT EXISTS idx_grades_student ON grades(student_id);

CREATE INDEX IF NOT EXISTS idx_grade_revisions_grade ON grade_revisions(grade_id);

COMMIT;
