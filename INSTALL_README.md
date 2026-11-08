# SmartTrack Academic Admin UI — updated

This replaces the setup wizard with a proper Admin Overview (`/academic/admin`). Management tabs: Courses, Groups & Students, Semesters, Teachers. Previous `/academic/setup` redirects to the Admin Overview for logged-in administrators.

## Install on current working SmartTrack_Upgrade 2

1. Stop the server. Make a backup of the working application folder.
2. Copy the patch files to your existing folder, merging templates and static, replacing matching files.
3. Keep `.env`, `.session_secret` and PostgreSQL database unchanged. No migrations and no package reinstall are needed for this UI patch.
4. Run `source start-env.sh` and `/usr/bin/python3 -m uvicorn app:app --reload`.
5. Log in as Admin and open `/academic` or `/academic/admin`.

This patch only changes academic navigation/UI and the admin overview endpoint; it does not modify academic database schema. Check all workflows against your own PostgreSQL data before relying on them in production.
