# SmartTrack US1–12 — repair package (November 2026)

This package is based on the existing US1–12 upgrade, with the original US1–9 code retained.

## Fixed in this revision
- Subject creation: validates each field separately and gives an intelligible error on the Courses page rather than showing a JSON 400 page.
- Course codes may contain spaces, which are normalized to hyphens (`Math 102` → `MATH-102`), along with ASCII letters/numbers and `._+-`.
- Duplicate subject codes are reported clearly, including a race-condition fallback.
- After a failed subject form, entered values are restored on the Courses page.
- Parent dashboard links to a child's courses/grades, and parent can read all recorded grades and revisions for **only linked** students. This repair includes the previously developed parent portal fix.
- Access to grade changes remains limited to the assigned teacher in an active semester; parent access is read-only.

## How to install on the user's Mac

1. Stop Uvicorn with Control+C.
2. Keep **SmartTrack_Final** unchanged, and back up the PostgreSQL database.
3. In `SmartTrack_Upgrade 2` preserve `.env` and `.session_secret` if already present.
4. Extract the patch and replace `app.py`, `academic.py`, `templates/academic_courses.html`, `templates/academic_parent.html`, `templates/academic_home.html`, `templates/dashboard.html`, and `tests/test_academic.py` as provided. Do not replace `.env`.
5. Start with the proven working system Python, not the broken `.venv`:

```sh
cd ~/Documents/"SmartTrack_Upgrade 2"
source start-env.sh
/usr/bin/python3 -m uvicorn app:app --reload
```

6. If new database tables have **not** been initialized, after a database backup stop server, run `/usr/bin/python3 app.py init-db` once, then restart. Do **not** run `create-admin` again.

## Verification
- Admin creates `MATH101`, then `CS 102`: both succeed and second is stored as `CS-102`.
- Creating duplicate `MATH101` shows a friendly message, not a blank JSON page.
- Wrong code or short name shows a field-specific message and remembers entered fields.
- Parent sees only their linked children's courses, scores and change history; unlink revokes access.
- Teacher creates an assessment, enters and edits grade in active semester, while student and parent see updated score.
- No live PostgreSQL end-to-end checks have been completed in this environment. Verify on a backed-up local database.
