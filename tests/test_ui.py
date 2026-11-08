"""UI regression tests for the redesigned interface (navigation, links, forms, role access, layout tokens)."""
import re
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import app
from helpers import check_safe_test_database, drop_all_tables, truncate_all, TEST_DB, Session, make_user, q1
from test_integration import World, grade_of

ROOT = Path(app.ROOT)


# ------------------------------------------------------------------ static checks (no database)
def test_css_has_no_oversized_dashboard_typography():
    css = (ROOT / 'static/style.css').read_text()
    sizes = [float(x) for x in re.findall(r'font-size:\s*([\d.]+)px', css)]
    assert sizes and max(sizes) <= 32, max(sizes)
    for low, high in re.findall(r'font-size:\s*clamp\(([\d.]+)px,[^,]+,([\d.]+)px\)', css):
        assert float(high) <= 56  # only the public landing headline uses clamp()
    for token in ('--bg:#F7F7F5', '--surface:#FFFFFF', '--sidebar:#FAFAF9', '--accent:#D9EF92', '--border:#E6E8E3'):
        assert token in css
    assert 'prefers-reduced-motion' in css


def test_every_css_class_used_in_templates_is_defined():
    css = (ROOT / 'static/style.css').read_text()
    defined = set(re.findall(r'\.([a-zA-Z][\w-]*)', css))
    js_or_state = {'userline', 'rq', 'as-check', 'public-body', 'app-body', 'inline', 'origin'}
    missing = set()
    for f in (ROOT / 'templates').glob('*.html'):
        for attr in re.findall(r'class="([^"{}]*)"', f.read_text()):
            for c in attr.split():
                if c not in defined and c not in js_or_state:
                    missing.add((f.name, c))
    assert not missing, sorted(missing)


def test_destructive_forms_use_confirmation_dialog():
    for name in ('academic_groups.html', 'academic_courses.html', 'admin_users.html', 'academic_semesters.html'):
        html = (ROOT / 'templates' / name).read_text()
        assert 'data-confirm=' in html, name
        assert 'onsubmit=' not in html, name


# ------------------------------------------------------------------ database-backed checks
@pytest.fixture(scope='module')
def world():
    check_safe_test_database()
    previous = app.DATABASE_URL
    app.DATABASE_URL = TEST_DB
    drop_all_tables()
    app.setup()
    truncate_all()
    w = World()
    make_user('pending.t@test.io', 'teacher', 'Peter Pending', teacher_status='pending_approval')
    t = Session('john@test.io')
    aid = w.approved_assessment(t)
    t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1, w.s2], 'score': ['72.5', '88']})
    g = grade_of(aid, w.s1)
    t.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': '80', 'version': 1, 'reason': 'Recount'})
    t.submit('/academic/assessment', {'offering_id': w.offering, 'kind': 'assignment', 'title': 'HW draft', 'max_score': '10', 'submit': '0'})
    w.aid = aid
    w.sessions = {'admin': w.admin, 'teacher': t, 'student': Session('alice@test.io'), 'parent': Session('parent@test.io')}
    yield w
    app.DATABASE_URL = previous


START = {'admin': '/academic/admin', 'teacher': '/dashboard', 'student': '/dashboard', 'parent': '/dashboard'}
SIDEBAR = {
    'admin': ['Overview', 'Courses', 'Groups', 'Semesters', 'Teachers', 'Assessments', 'Users', 'Activity log'],
    'teacher': ['Overview', 'My Courses', 'Assessments', 'Gradebook', 'Profile'],
    'student': ['Overview', 'My Courses', 'Grades', 'My Group', 'Profile'],
    'parent': ['Overview', 'My Child', 'Courses', 'Grades', 'Profile'],
}


def sidebar_links(html):
    nav = html.split('class="sb-nav"')[1].split('</nav>')[0]
    return re.findall(r'<a class="sb-link ?(active)?" href="([^"]+)"[^>]*>.*?<span>([^<]+)</span>', nav, re.S)


@pytest.mark.parametrize('role', ['admin', 'teacher', 'student', 'parent'])
def test_sidebar_is_role_aware_with_one_active_item(world, role):
    s = world.sessions[role]
    html = s.get(START[role]).text
    links = sidebar_links(html)
    assert [label for _, _, label in links] == SIDEBAR[role]
    assert [label for active, _, label in links if active] == ['Overview']
    assert html.count('class="sidebar"') == 1 and html.count('class="topbar"') == 1


@pytest.mark.parametrize('role', ['admin', 'teacher', 'student', 'parent'])
def test_every_link_on_every_reachable_page_works(world, role):
    """Crawl all same-site links reachable from the role's start page (two levels deep)."""
    s = world.sessions[role]
    seen, frontier, checked = set(), [START[role]], 0
    for depth in range(3):
        nxt = []
        for path in frontier:
            if path in seen:
                continue
            seen.add(path)
            r = s.get(path)
            assert r.status_code == 200, (role, path, r.status_code)
            checked += 1
            if depth < 2:
                for href in re.findall(r'href="(/[^"#]*)', r.text):
                    href = href.replace('&amp;', '&')
                    if href.startswith(('/static', '/lang/')) or href in seen:
                        continue
                    nxt.append(href)
        frontier = nxt
    assert checked >= 5, checked


@pytest.mark.parametrize('role', ['admin', 'teacher', 'student', 'parent'])
def test_every_form_posts_to_an_existing_route(world, role):
    post_routes = {r.path for r in app.app.routes if 'POST' in getattr(r, 'methods', set())}
    s = world.sessions[role]
    pages = {'admin': ['/academic/admin', '/academic/courses', '/academic/courses?tab=subjects', '/academic/courses?tab=archived',
                       '/academic/groups', '/academic/groups?g=%d' % world.g1, '/academic/groups?tab=requests',
                       '/academic/semesters', '/academic/teachers', '/academic/assessments?status=all', '/admin/users',
                       '/admin/activity', '/academic/offering/%d?tab=assessments' % world.offering],
             'teacher': ['/dashboard', '/academic', '/academic/offering/%d?tab=assessments' % world.offering,
                         '/academic/offering/%d?assessment=%d' % (world.offering, world.aid), '/profile'],
             'student': ['/dashboard', '/academic/my-group', '/profile'],
             'parent': ['/dashboard', '/academic/parent/%d' % world.s1, '/profile']}[role]
    for page in pages:
        html = s.get(page).text
        for tag in re.findall(r'<form[^>]*>', html):
            if 'method="post"' in tag:
                action = re.search(r'action="([^"]+)"', tag).group(1)
                assert action in post_routes, (page, action)
        assert html.count('name="csrf_token"') >= html.count('method="post"'), page


def test_role_based_page_access(world):
    t, st, p = world.sessions['teacher'], world.sessions['student'], world.sessions['parent']
    for path in ('/academic/admin', '/admin/users', '/admin/activity', '/academic/courses', '/academic/assessments'):
        for s in (t, st, p):
            assert s.get(path).status_code == 403, path
    for path in ('/academic/teacher/assessments', '/academic/teacher/gradebook'):
        assert st.get(path).status_code == 403 and p.get(path).status_code == 403
    for path in ('/academic/my-group', '/academic/my-grade-history'):
        assert t.get(path).status_code == 403 and p.get(path).status_code == 403
    assert world.admin.client.get('/dashboard', follow_redirects=False).headers['location'] == '/academic/admin'


def test_dashboards_show_role_content(world):
    a = world.admin.get('/academic/admin').text
    for text in ('Total students', 'Total teachers', 'Active courses', 'Academic groups', 'Current semester', 'Recent activity', 'Fall 2026'):
        assert text in a, text
    t = world.sessions['teacher'].get('/dashboard').text
    for text in ('Assigned courses', 'Students', 'Awaiting approval', 'Recent grading activity', 'Mathematics'):
        assert text in t, text
    s = world.sessions['student'].get('/dashboard').text
    for text in ('Current semester', 'SE-2401', 'Enrolled courses', 'Latest grades', 'Midterm', 'Invite'):
        assert text in s, text
    assert '88' not in s.split('Latest grades')[1].split('</section>')[0]  # Bob's score never shown to Alice
    p = world.sessions['parent'].get('/dashboard').text
    for text in ('Alice Brown', 'SE-2401', 'Fall 2026', 'Midterm', 'Disconnect'):
        assert text in p, text


def test_course_tabs_and_grade_edit_modal(world):
    t = world.sessions['teacher']
    base = '/academic/offering/%d' % world.offering
    assert 'Alice Brown' in t.get(base).text                     # Students tab is the default
    html = t.get(base + '?tab=assessments').text
    assert 'Midterm' in html and 'HW draft' in html and 'Submit for approval' in html
    html = t.get(base + '?assessment=%d' % world.aid).text
    assert 'id="dlg-grade"' in html and 'Current grade' in html and 'New grade' in html and 'name="version"' in html
    s = world.sessions['student'].get(base + '?tab=grades').text
    assert '80' in s and '88' not in s and 'dlg-grade' not in s


def test_parent_child_tabs(world):
    p = world.sessions['parent']
    base = '/academic/parent/%d' % world.s1
    assert 'Mathematics' in p.get(base).text
    assert '80' in p.get(base + '?tab=grades').text
    hist = p.get(base + '?tab=history').text
    assert 'Recount' in hist and '72.5' in hist
    assert 'method="post"' not in hist.split('<main')[1].split('</main>')[0]  # read-only


def test_admin_users_page_keeps_us1_9_actions(world):
    html = world.admin.get('/admin/users').text
    for action in ('value="block"', 'value="approve_teacher"', 'value="issue_teacher"', 'value="reject_teacher"',
                   'value="revoke_teacher"', 'value="change_role"'):
        assert action in html, action
    assert 'Peter Pending' in world.admin.get('/admin/users?status=pending').text
    assert 'Alice Brown' not in world.admin.get('/admin/users?role=teacher').text
    assert 'semester' in world.admin.get('/admin/activity?area=semester').text.lower()


def test_validation_error_keeps_page_and_message(world):
    st, loc, err, ok = world.admin.submit('/academic/offering', {'subject_id': world.sub, 'semester_id': world.sem,
                                                                 'group_id': world.g1, 'teacher_id': world.t2},
                                          referer='/academic/courses')
    assert loc == '/academic/courses' and 'Course already exists' in err
    html = world.admin.get('/academic/courses').text  # flash consumed; page still renders normally
    assert 'id="dlg-assign"' in html
