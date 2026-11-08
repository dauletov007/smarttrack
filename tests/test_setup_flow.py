"""Admin management navigation regression tests (no setup wizard)."""
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from fastapi.testclient import TestClient

def test_admin_management_templates_compile():
    for filename in ('app.html','academic_admin.html','academic_courses.html','academic_groups.html','academic_semesters.html',
                     'academic_assessments.html','academic_teachers.html','admin_users.html','admin_activity.html'):
        app.tpl.env.get_template(filename)


def test_sidebar_navigation_is_role_aware_and_not_a_guide():
    shell=(Path(app.ROOT)/'templates'/'app.html').read_text()
    for path in ('/academic/admin','/academic/courses','/academic/groups','/academic/semesters','/academic/teachers',
                 '/academic/assessments','/admin/users','/admin/activity',              # admin
                 '/academic/teacher/assessments','/academic/teacher/gradebook',          # teacher
                 '/academic/my-grade-history','/academic/my-group','/profile'):          # student / shared
        assert path in shell,path
    assert 'Setup guide' not in shell and 'Step 1' not in shell


def test_legacy_setup_is_protected():
    assert TestClient(app.app).get('/academic/setup').status_code==403


def test_assignment_form_has_labelled_fields_and_prerequisite_hint():
    html=(Path(app.ROOT)/'templates/academic_courses.html').read_text()
    for label in ('for="as-subject">Subject','for="as-semester">Semester','for="as-group">Group','for="as-teacher">Teacher',
                  'name="subject_id"','name="semester_id"','name="group_id"','name="teacher_id"','Assign Course'):
        assert label in html,label
    assert '{% if missing %}' in html and "'disabled' if missing" in html


def test_no_setup_guide_template():
    assert not (Path(app.ROOT)/'templates/academic_setup.html').exists()
