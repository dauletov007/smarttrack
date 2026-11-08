# SmartTrack US10–12 — fix and completion (November 2026)

## Defects found in the previous version (reproduced against PostgreSQL)

| Problem | Effect | Fix |
|---|---|---|
| Duplicate group code or semester name | Unhandled `UniqueViolation` → 500 Internal Server Error | Checked up front with a clear message, plus a race-safe fallback |
| Missing/empty form field (e.g. no teacher selected) | Raw JSON 422 page | Redirect back with "Please fill in all required fields" |
| New semesters were `draft`; teachers then got "closed, archived, or deleted" errors | Course looked assigned but nothing could be graded | "Open now" option on creation, explicit Open/Close/Reopen/Make current, accurate messages ("semester is not open yet") |
| Teacher approval only possible via an emailed code | No approved teachers → assignment form hidden | Admin **Approve** button (Teachers page and Users dashboard), audited |
| Assignment form hidden entirely when a prerequisite was missing | Admin could not see what to do | Form always visible, with a one-line "Missing: …" hint linking to the right page |
| Reopening a semester demoted the current semester to draft | Current semester silently locked | Several semesters may be open; a separate "current" flag |
| Group delete was a hard `DELETE` | Join requests cascaded away; courses lost their group link | Soft delete: memberships ended, courses archived, code freed; grades untouched |
| No assessment approval | Requirement missing | Draft → pending → approved/rejected workflow with notes, bulk approval |
| 403/404 shown as JSON | Unfriendly | Friendly HTML error page (status codes unchanged) |
| Grade entry one student at a time | Slow | Batch gradebook entry (all-or-nothing validation) plus per-grade Edit |

## Changed files

- `academic.py` — rewritten US10–12 domain: additive `MIGRATION_V2`, admin pages, assessment approval, batch grade entry, optimistic-lock edits (`WHERE version=…`), course locks that serialise grading with teacher replacement, soft-deleted groups, archived courses, student/parent views derived from real assignments.
- `app.py` — friendly error handlers (HTTP + validation), `audit(..., details)`, `approve_teacher` admin action with safe `next` redirect, `dt`/`num` template filters, startup schema check.
- `templates/` — new compact UI: `academic_layout.html`, `academic_nav.html` (tabs), `academic_admin.html` (Overview), `academic_courses.html`, `academic_groups.html`, `academic_semesters.html`, `academic_teachers.html`, `academic_assessments.html` (new), `academic_offering.html` (roster/assessments/gradebook), `academic_home.html` (teacher/student/parent), `academic_parent.html`, `academic_mygrades.html`, `academic_history.html`, `academic_join.html`; `base.html` (main class block); `dashboard.html` (Approve teacher option, fixed CTA classes). Removed `academic_setup.html`.
- `static/style.css` — US1–9 styles unchanged; old academic/setup-guide CSS replaced by a compact workspace style (same off-white / dark / lime brand).
- `migrations/001_us10_12_tables.sql`, `migrations/002_us10_12_workflow.sql` — the same SQL `init-db` runs.
- `tests/conftest.py`, `tests/helpers.py`, `tests/test_integration.py` (new, 23 PostgreSQL tests); `tests/test_academic.py`, `tests/test_setup_flow.py` updated.
- `README.md` rewritten (start, backup, migrate, restore, tests). Removed outdated `README_FIXES.md`, `README_SETUP_GUIDE.md`, `INSTALL_README.md`, `README_US10-12.md`, `QA_MANUAL.md`.
- `.env`, `.session_secret`, `requirements.txt`, `start-env.sh` — unchanged.

## Test results (actually executed)

Environment: Python 3.9.25, FastAPI 0.128 / Starlette 0.49 / psycopg 3.2, PostgreSQL 16.15 on Linux,
in a disposable `smarttrack_test` database.

- With `SMARTTRACK_TEST_DATABASE_URL`: **52 passed, 0 failed** (29 unit/template + 23 database-backed).
- Without a test database: **29 passed, 23 skipped**.
- The server was also run with uvicorn on a seeded database, and every role's pages were checked in Chromium at desktop and phone widths.

The database tests cover these areas:

- **Subjects:** create, edit, archive/restore, search and duplicate codes.
- **Semesters:** create, open, close, reopen (audited), current semester and duplicates.
- **Groups and students:** groups, enrolment, the one-current-group rule, removal, CSV and XLSX import (including rollback on bad rows), and join requests (single and bulk).
- **Course assignment:** visibility for students, teacher and parent; the same subject in another group; duplicate prevention; unapproved teachers; closed semesters.
- **Access control:** server-side teacher checks and the roster rule.
- **Assessments:** the approval workflow (draft, reject with note, resubmit, approve, bulk).
- **Grades:**
  - Entry validation: negative, over max, non-numeric, 3 decimals, NaN/Infinity, all-or-nothing, duplicates, boundaries and comma decimals.
  - Edit history and optimistic locking.
- **Visibility:** students see only their own grades; parents see their linked child, never unrelated students, read-only, and unlink still works.
- **History preservation:** teacher replacement (former teacher locked out), student transfer, closed-semester protection, group deletion and subject archive, course archive.
- **Security:** CSRF and role checks; friendly error pages.
- **Acceptance:** the full 16-step scenario.
- **Migration:** a database built with the previous schema keeps all users, links, invites, grades and revisions.

## Unresolved issues / decisions to confirm

1. Not run on your Mac, your Python 3.9.6 packages or your real database. Back up and run `init-db` first, as described in the README.
2. Grading scale is the provisional default: numeric points, 0–max, up to 2 decimals. There are no weights or final grades yet.
3. A teacher may **correct** an existing grade of a student who has since left the group (the grade belongs to that course). **New** grades are only allowed for the current roster. Say if corrections should be blocked too.
4. Approved assessment definitions are locked. To change one, reject a pending copy or create a new assessment.
5. Courses can be assigned in a draft semester, but grading starts only when the semester is opened.
6. Excel import supports `.xlsx` and `.csv`, not legacy `.xls`.
7. New academic pages are English only. The RU switch still works for US1–9 pages.
8. There is no full audit-log page. Recent entries appear on Overview, Semesters and the Users dashboard. All events are in the `audit` table.
9. Deleted groups keep their row, renamed `CODE~deleted-ID` so the code can be reused. History screens show the original code from snapshots.
10. The old `.venv` folder (Python 3.12, broken) was left untouched and is not used.
