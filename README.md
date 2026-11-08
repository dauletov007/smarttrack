# SmartTrack — US1–12 (FastAPI + PostgreSQL)

Bilingual (EN/RU) student information system for **Admin, Teacher, Student and Parent**.
US1–9 cover accounts, verification, profiles and parent linking. US10–12 add course management,
assessments with admin approval, grade entry and audited grade editing.

## Start the app (macOS, existing setup)

Use the system Python that already works (3.9.6), not the old `.venv`.

```bash
cd ~/Documents/"SmartTrack_Upgrade 2"
```

```bash
source start-env.sh
```

```bash
/usr/bin/python3 -m uvicorn app:app --reload
```

Open http://127.0.0.1:8000, log in as Admin, then click **Courses** in the top bar.

## Upgrading an existing database (one time)

The migration is **additive**: it only adds columns, constraints and indexes. It never drops or
recreates tables, and it keeps users, password hashes, profiles, parent links, invitations,
existing courses, grades and audit records.

1. Stop the server (Control+C).
2. **Back up** the database (asks for the `smarttrack_user` password from `.env`):

   ```bash
   pg_dump -h localhost -U smarttrack_user -d smarttrack_db -F c -f ~/smarttrack_backup_$(date +%Y%m%d_%H%M).dump
   ```

3. Apply the migration:

   ```bash
   source start-env.sh
   ```

   ```bash
   /usr/bin/python3 app.py init-db
   ```

   It is safe to run again. The same SQL is in `migrations/002_us10_12_workflow.sql` if you prefer `psql`.
4. Start the server as above. If you forget step 3, the server prints
   `database schema is out of date` at startup.

**Restore** (only if something went wrong):

```bash
pg_restore -h localhost -U smarttrack_user -d smarttrack_db --clean --if-exists ~/smarttrack_backup_YYYYMMDD_HHMM.dump
```

### What the migration changes

| Table | Change | Why |
|---|---|---|
| `semesters` | `is_current`, `closed_at` columns, unique index on the current semester | One current semester; several semesters can be open (e.g. a reopened past term) |
| `academic_groups` | `deleted_at` | Groups are soft-deleted so history keeps valid references |
| `course_offerings` | `status` (`active`/`archived`), `archived_at` | Archive a course instead of deleting it |
| `teacher_assignments_history` | `reason` | Why a teacher was replaced |
| `assessments` | `status`, `scale`, `reviewed_by`, `reviewed_at`, `review_note`, `updated_at` | Approval workflow. **Existing assessments become `approved`** so their grades stay editable; new ones start `pending` |
| `grades` | `updated_by` | Who last changed the grade |
| `audit` | `details` | Readable context (e.g. `Fall 2026: closed → active`) |

Foreign keys: grades → assessments → course offerings → subjects/semesters use the default
`NO ACTION`, so PostgreSQL refuses any delete that would orphan grades. The app never hard-deletes
subjects, semesters, groups, courses, assessments or grades.

## Workflow

1. **Admin → Semesters**: create `Fall 2026` with "Open now" ticked (or Open it later).
2. **Admin → Courses**: create subject `MATH101 — Mathematics`.
3. **Admin → Groups**: create `SE-2401`; add students one by one, import CSV/XLSX (emails in column A), or approve join requests.
4. **Admin → Teachers**: approve a pending teacher (or use the US1–9 email code flow).
5. **Admin → Courses → Assign subject to group**: pick Subject, Semester, Group, Teacher → **Assign Course**.
6. **Teacher → Courses**: open the course, see the roster, create an Assignment or Exam (title, max points).
7. **Admin → Assessments**: approve it (or reject it with a note).
8. **Teacher**: click **Grade** on the approved assessment, type scores, **Save grades**; use **Edit grade** to correct one (optional reason; every change is kept in history).
9. **Student → Courses**: sees group, current semester, courses, teachers, assessments and own grades.
10. **Parent → Courses**: sees each linked child's courses and grades, read-only.

Rules enforced on the server:
- Only the teacher currently assigned to a course can create assessments or enter/edit grades.
- Grades need an approved assessment and an open semester. Closed semesters, archived courses or subjects, and deleted groups are read-only.
- Scores are numbers from 0 to the assessment maximum, with up to two decimals.
- New grades can only be entered for students currently on the roster, and duplicate grades are rejected.
- Edits use version numbers, so an edit based on an outdated page is rejected instead of silently overwriting.

## Interface

See `REDESIGN_UI.md` for the redesigned interface (sidebar per role, tabs, dialogs).
Admins land on **Overview**; teachers, students and parents land on their own **Overview** at `/dashboard`.

## Tests

Fast tests (no database needed):

```bash
/usr/bin/python3 -m pytest -q
```

Full database-backed tests need a **separate, disposable** database whose name contains `test`.
The suite refuses to run against any other name, and it never reads the `DATABASE_URL` from `.env`.
It deletes and recreates every table in that test database.

```bash
psql postgres -c "CREATE DATABASE smarttrack_test OWNER smarttrack_user"
```

```bash
SMARTTRACK_TEST_DATABASE_URL="postgresql://smarttrack_user:YOUR_PASSWORD@localhost:5432/smarttrack_test" /usr/bin/python3 -m pytest -q
```

## First-time installation (new machine)

```bash
brew install postgresql@17
```

```bash
brew services start postgresql@17
```

```bash
psql postgres -c "CREATE USER smarttrack_user WITH PASSWORD 'CHOOSE_A_STRONG_PASSWORD'" -c "CREATE DATABASE smarttrack_db OWNER smarttrack_user"
```

Copy `.env.example` to `.env` and fill in `DATABASE_URL` (URL-encode special characters in the password). Then:

```bash
/usr/bin/python3 -m pip install -r requirements.txt
```

```bash
source start-env.sh
```

```bash
/usr/bin/python3 app.py init-db
```

```bash
/usr/bin/python3 app.py create-admin
```

Run `create-admin` only once.

### Email

For local testing, set `DEVELOPMENT_EMAIL=1` and leave SMTP empty: codes are printed in the terminal.
For real email (e.g. Gmail with an App Password), set `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`,
`SMTP_FROM`, `SMTP_PASSWORD` and `ADMIN_EMAIL` in `.env`. Never commit `.env`.

## US1–9 summary

Landing page; Student/Teacher registration, Parent registration by student invitation; email codes;
login/logout with throttling; profiles; teacher approval; one-use 7-day parent invitations and unlinking;
email/password change and reset; Admin user list, search, block/unblock, teacher approval and audit log.

Security: PBKDF2-SHA256 passwords, CSRF tokens on every form, server-side role checks, hashed one-use
codes, session invalidation on password change. This is a local MVP; for public deployment use HTTPS
(`COOKIE_SECURE=1`), a persistent `SESSION_SECRET`, backups and a security review.
