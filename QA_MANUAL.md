# SmartTrack — Manual PostgreSQL QA for US10–12

**Status: NOT EXECUTED** (no configured PostgreSQL service/credentials in this delivery).

1. Run `python3 app.py init-db` twice on a backed-up database; US1–9 users and parent links remain unchanged.
2. Admin creates Fall 2026 semester, activates it, creates `SE-2401`, and creates subject `MATH101`.
3. Create/approve a teacher account and 2 student accounts; add both students to the group.
4. Admin assigns MATH101 to SE-2401 and teacher. Verify a duplicate offering is rejected.
5. Log in as the teacher and create Assignment / Exam with maximum 100 points.
6. Enter 72.50 points for student A; verify it persists after reload.
7. Try entering 105, -1, nonnumeric and a duplicated grade; all must be rejected.
8. As another teacher or student attempt the same grade POST; verify 403.
9. As the assigned teacher edit 72.50 → 80; verify `grade_revisions` contains both values, editor, and time.
10. Attempt an edit with an old grade version; verify HTTP 409.
11. Admin replaces teacher: previous teacher cannot access course; new teacher can edit existing grade; original creator remains preserved.
12. Admin closes semester: creation and edit of grades must return HTTP 409; after reopening edits work.
13. Move student A to a new group: earlier grades remain in **All my grades**.
14. Delete SE-2401: previous assessment and grade remain available from history; group-code snapshot remains.
15. Test CSV and XLSX import (email column A): valid rows succeed; invalid/missing accounts and duplicates roll back fully.
16. Student requests to join a group; Admin selects multiple pending requests and approves/rejects in one submission.
17. Submit form without a valid CSRF token: HTTP 403; logged-out requests: HTTP 403.
18. Confirm archived subjects cannot receive new grades and blocked users cannot act.

Use a staging copy of the database; do not delete a production group to test retention.
