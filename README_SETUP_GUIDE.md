# SmartTrack academic setup guide (US10–12)

Use the same working Python 3.9 and `.env` as your existing site. Do not recreate the database or user accounts. Keep a database backup before running database migrations.

1. Login as Admin and open **Academic → Setup guide** (`/academic/setup`).
2. Create and activate a semester under **Semesters**.
3. Create a group under **Groups & Students** (for example `SE-2401`).
4. Add existing student accounts to that group from the dropdown or import CSV/XLSX of registered student email addresses.
5. Ensure a teacher has registered, verified email, and been approved by the administrator.
6. Create a subject in **Courses**, then assign it to the semester + group + approved teacher.
7. Teacher opens **My courses**, creates assignments/exams and enters or edits grades.
8. Parent sees only linked children's subjects and grades (read-only).

Empty semester/group/teacher choices now show links to the relevant setup page instead of unusable dropdown menus. The semester is created as draft and must be activated for grade entry.

This UI pass is not a substitute for running full PostgreSQL integration tests against a disposable database; do that before production use.
