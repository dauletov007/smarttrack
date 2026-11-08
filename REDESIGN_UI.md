# SmartTrack — UI/UX redesign (November 2026)

A calm, Notion-inspired workspace: off-white background, white cards, thin grey borders and a sparing lime accent.
Design tokens live in `static/style.css` (`:root`).

## What changed for each role
- **Shell:** every logged-in page has a 244 px sidebar with role-aware navigation, the active item highlighted, small line icons, the user at the bottom and logout.
  - A 60 px top bar shows breadcrumbs.
  - On phones the sidebar collapses into a menu button.
- **Admin:** Overview, Courses (tabs: Courses / Subjects / Archived), Groups (list → group detail, plus Join requests and Transfers), Semesters, Teachers, Assessments, Users and Activity log.
  - Create and edit forms are modal dialogs.
  - Destructive actions ask for confirmation in a dialog.
- **Teacher:** Overview (courses, students, pending approvals, recent grading activity), My Courses (cards), Assessments, Gradebook and Profile.
  - The course page has three tabs: Students / Assessments / Grades.
  - Grades are corrected with an Edit grade dialog showing student, assessment, current grade and new grade.
- **Student:** Overview, My Courses, Grades (score bars), My Group (current group, membership history, join request) and Profile.
  - The course page has three tabs: Overview / Assessments / Grades.
- **Parent:** Overview (each linked child with group, semester, courses and recent grades), My Child, Courses, Grades and Profile.
  - The child page has three tabs: Courses / Grades / Grade history.
  - Everything is read-only.
- **Public pages:** landing, login, registration, verification, password reset, teacher approval and messages all use one card style.

## Backend: what was and was not touched
The schema, authentication, sessions, CSRF, permissions, grading, approval workflow, grade history and group rules are unchanged.
Additions are read-only and serve the new screens only:
- **New GET pages:**
  - `/admin/users` and `/admin/activity`: the US1–9 user list and audit log, moved from the old dashboard.
  - `/academic/teacher/assessments` and `/academic/teacher/gradebook`.
  - `/academic/my-group` (`/academic/join` now shows the same page).
- **Optional `tab=` query parameters** on Courses, Groups, the course page and the parent page.
- **Navigation changes:**
  - `/dashboard` now shows a per-role Overview; for admins it redirects to `/academic/admin`.
  - After an account action, the admin returns to `/admin/users` (or the page given in `next`).
- **Sidebar data:** `view()` adds attention counters (admin, teacher) and linked children (parent).
- **Shared read queries:** the student course and grade queries moved to module level so the Overview can reuse them. One column (`g.updated_at`) was added to the student course detail query to show grade dates.

No backend defects were found during the redesign.
