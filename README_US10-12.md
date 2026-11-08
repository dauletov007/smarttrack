# SmartTrack US10–12 — Implementation guide

This is an additive upgrade of the existing FastAPI/PostgreSQL US1–9 site.
It is **not** a separate application. No existing users, profiles, or parent links are dropped by the schema migration.

## Install and migrate

1. Back up production PostgreSQL with `pg_dump` before upgrading.
2. Configure `.env` as in the main README (no credentials should be committed).
3. Run:

```bash
python3 -m pip install -r requirements.txt
source start-env.sh
python3 app.py init-db
python3 -m uvicorn app:app --reload
```

`init-db` is repeatable: it adds US10–12 tables without removing US1–9 data. The migration does **not** copy data from SQLite.

## Features

- **US10**: admin-only subject management (create/edit/archive/restore); semesters (draft/active/closed and reopen); groups, student transfers, CSV/XLSX import (email in column A), join requests and bulk approval/rejection; semester/group-specific course offering and assigned teacher; duplicate prevention; teacher replacements with history; teacher workload; subject/semester/group filters.
- **US11**: teachers create assignments or exams with maximum points; assign decimal scores (0–max, two decimal places) to their current group members. Duplicate submissions are rejected.
- **US12**: current assigned teacher may edit previously entered grades in an active semester, with optimistic version control, optional reason and recorded changes (`grade_revisions`). Previous teacher loses access on reassignment.
- **History**: deleting a group removes active group enrollment and association but keeps course offerings, assessments, grades, teacher history and group-code snapshots. Students can see historical grades under **My courses → All my grades**.

## Pending product choices / assumptions

The original US11–12 requirements do not set a scoring scale. This implementation uses **numeric points with up to 2 decimals**, maximum points per assessment, and assignment/exam categories. No overall weighted average or attendance is implemented; those are separate User Stories. Notification delivery (earlier US10 question 23) was not approved and is not included.

## Testing

Run `pytest -q`: unit/smoke/template/access tests work without a running PostgreSQL instance. See `QA_MANUAL.md` for end-to-end integration scenarios requiring a local PostgreSQL database, admin, approved teacher, and student accounts. Tests are **not** a substitute for production data backup or full integration testing.

## Production notes

The application must run with HTTPS (`COOKIE_SECURE=1`) and a strong persistent `SESSION_SECRET`. Use proper database backups, deploy behind a reverse proxy, secure SMTP credentials, and test migrations on a database copy. Public release requires additional security review and abuse throttling. Existing deployment configuration is not bundled or changed.
