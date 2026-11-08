"""PostgreSQL-backed integration tests for US10–12 (and US1–9 regressions they touch).

Run against a SEPARATE disposable database, never the one in .env:

    SMARTTRACK_TEST_DATABASE_URL=postgresql://USER:PASS@localhost:5432/smarttrack_test python3 -m pytest -q

Every test starts from empty tables in that database.
"""
import io
import zipfile

import pytest

import app
import academic
from helpers import (check_safe_test_database, drop_all_tables, truncate_all, make_user, link_parent, q, q1,
                     Session, ids, TEST_DB)


@pytest.fixture(scope='module', autouse=True)
def database():
    check_safe_test_database()
    previous = app.DATABASE_URL
    app.DATABASE_URL = TEST_DB
    drop_all_tables()
    app.setup()
    app.setup()  # migrations must be repeatable
    yield
    app.DATABASE_URL = previous


@pytest.fixture(autouse=True)
def clean(database):
    truncate_all()
    yield


# --------------------------------------------------------------------------- fixtures
class World:
    """Admin, two approved teachers, three students, two parents; one open semester, subject and group."""

    def __init__(self, with_course=True):
        self.admin_id = make_user('admin@test.io', 'admin', 'Main Admin')
        self.t1 = make_user('john@test.io', 'teacher', 'John Smith')
        self.t2 = make_user('mary@test.io', 'teacher', 'Mary Jones')
        self.s1 = make_user('alice@test.io', 'student', 'Alice Brown')
        self.s2 = make_user('bob@test.io', 'student', "Bob O'Neil")
        self.s3 = make_user('carol@test.io', 'student', 'Carol White')
        self.p1 = make_user('parent@test.io', 'parent', 'Paula Brown')
        self.p2 = make_user('other.parent@test.io', 'parent', 'Oscar White')
        link_parent(self.p1, self.s1)
        link_parent(self.p2, self.s3)
        self.admin = Session('admin@test.io')
        a = self.admin
        assert a.submit('/academic/semester', {'label': 'Fall 2026', 'start_date': '2026-09-01',
                                               'end_date': '2026-12-20', 'activate': '1'})[3]
        assert a.submit('/academic/subject', {'code': 'MATH101', 'name': 'Mathematics'})[3]
        assert a.submit('/academic/group', {'group_code': 'SE-2401'})[3]
        assert a.submit('/academic/group', {'group_code': 'SE-2402'})[3]
        self.sem = ids('semesters')[0]
        self.sub = ids('subjects')[0]
        self.g1, self.g2 = ids('academic_groups')
        for s in (self.s1, self.s2):
            assert a.submit('/academic/group/member', {'student_id': s, 'group_id': self.g1})[3]
        assert a.submit('/academic/group/member', {'student_id': self.s3, 'group_id': self.g2})[3]
        self.offering = None
        if with_course:
            self.offering = self.assign(self.sub, self.g1, self.t1)

    def assign(self, subject, group, teacher, semester=None):
        st, loc, err, ok = self.admin.submit('/academic/offering', {'subject_id': subject, 'semester_id': semester or self.sem,
                                                                    'group_id': group, 'teacher_id': teacher},
                                             referer='/academic/courses')
        assert ok and not err, err
        return ids('course_offerings')[-1]

    def approved_assessment(self, teacher_session, title='Midterm', kind='exam', max_score='100', offering=None):
        o = offering or self.offering
        st, loc, err, ok = teacher_session.submit('/academic/assessment', {'offering_id': o, 'kind': kind, 'title': title,
                                                                           'max_score': max_score, 'submit': '1'})
        assert ok and not err, err
        aid = ids('assessments')[-1]
        assert self.admin.submit('/academic/assessment/review', {'assessment_id': aid, 'decision': 'approved'})[3]
        return aid


def grade_of(assessment, student):
    return q1('SELECT * FROM grades WHERE assessment_id=%s AND student_id=%s', (assessment, student))


# --------------------------------------------------------------------------- US10 subjects
def test_subject_create_edit_archive_search_and_duplicates():
    make_user('admin@test.io', 'admin')
    a = Session('admin@test.io')
    assert a.submit('/academic/subject', {'code': 'math 101', 'name': 'Calculus I', 'description': 'Limits'})[3]
    row = q1('SELECT * FROM subjects')
    assert row['code'] == 'MATH-101' and row['name'] == 'Calculus I'
    # Duplicate code: clear message on the Courses page, not a JSON/400 page.
    st, loc, err, ok = a.submit('/academic/subject', {'code': 'MATH-101', 'name': 'Other'}, referer='/academic/courses')
    assert st == 303 and loc == '/academic/courses' and 'already exists' in err
    assert len(q('SELECT * FROM subjects')) == 1
    # Invalid field
    assert 'name' in a.submit('/academic/subject', {'code': 'CS201', 'name': 'x'})[2].lower()
    # Edit
    assert a.submit('/academic/subject', {'subject_id': row['id'], 'code': 'MATH101', 'name': 'Calculus I (new)',
                                          'description': 'd'})[3]
    assert q1('SELECT code,name FROM subjects') == {'code': 'MATH101', 'name': 'Calculus I (new)'}
    assert a.submit('/academic/subject', {'code': 'CS201', 'name': 'Data Structures'})[3]
    # Search / filter
    page = a.get('/academic/courses?tab=subjects&sq=data').text
    table = page.split('id="subjects-table"')[1].split('</table>')[0]
    assert 'Data Structures' in table and 'Calculus I (new)' not in table
    # Archive and restore
    cs = ids('subjects', "code='CS201'")[0]
    assert 'archived' in a.submit('/academic/subject/archive', {'subject_id': cs})[3]
    assert q1('SELECT archived_at FROM subjects WHERE id=%s', (cs,))['archived_at'] is not None
    assert 'Data Structures' in a.get('/academic/courses?tab=archived').text
    assert a.submit('/academic/subject/archive', {'subject_id': cs, 'restore': 'true'})[3]
    assert q1('SELECT archived_at FROM subjects WHERE id=%s', (cs,))['archived_at'] is None
    # Creating subjects never creates course offerings.
    assert q('SELECT * FROM course_offerings') == []
    assert {r['action'] for r in q('SELECT action FROM audit')} >= {'subject.created', 'subject.updated', 'subject.archived'}


# --------------------------------------------------------------------------- US10 semesters
def test_semester_create_status_close_reopen_and_audit():
    make_user('admin@test.io', 'admin')
    a = Session('admin@test.io')
    assert a.submit('/academic/semester', {'label': 'Spring 2026', 'start_date': '2026-01-10', 'end_date': '2026-05-30'})[3]
    s1 = q1('SELECT * FROM semesters')
    assert s1['status'] == 'draft' and not s1['is_current']
    assert 'already exists' in a.submit('/academic/semester', {'label': 'spring 2026', 'start_date': '2026-01-10',
                                                               'end_date': '2026-05-30'})[2]
    assert 'End date' in a.submit('/academic/semester', {'label': 'Bad', 'start_date': '2026-05-10', 'end_date': '2026-01-01'})[2]
    assert 'valid start' in a.submit('/academic/semester', {'label': 'No dates'})[2]  # missing fields: friendly
    a.submit('/academic/semester/status', {'semester_id': s1['id'], 'status': 'active'})
    assert q1('SELECT status,is_current FROM semesters')['status'] == 'active'
    assert a.submit('/academic/semester', {'label': 'Fall 2026', 'start_date': '2026-09-01', 'end_date': '2026-12-20',
                                           'activate': '1'})[3]
    fall = ids('semesters', "label='Fall 2026'")[0]
    a.submit('/academic/semester/status', {'semester_id': fall, 'status': 'current'})
    assert q1('SELECT label FROM semesters WHERE is_current')['label'] == 'Fall 2026'
    # Close the older semester, then reopen it: the current semester stays open and current.
    a.submit('/academic/semester/status', {'semester_id': s1['id'], 'status': 'closed'})
    assert q1('SELECT status,closed_at FROM semesters WHERE id=%s', (s1['id'],))['status'] == 'closed'
    st, loc, err, ok = a.submit('/academic/semester/status', {'semester_id': s1['id'], 'status': 'active'})
    assert 'reopened' in ok
    assert q1('SELECT status FROM semesters WHERE id=%s', (s1['id'],))['status'] == 'active'
    assert q1('SELECT status,is_current FROM semesters WHERE id=%s', (fall,)) == {'status': 'active', 'is_current': True}
    actions = [r['action'] for r in q('SELECT action FROM audit ORDER BY id')]
    assert 'semester.closed' in actions and 'semester.reopened' in actions
    assert 'Spring 2026' in q1("SELECT details FROM audit WHERE action='semester.reopened'")['details']
    page = a.get('/academic/semesters').text
    assert 'Spring 2026' in page and 'Reopened' in page


# --------------------------------------------------------------------------- US10 groups
def test_group_creation_enrollment_one_current_group_and_removal():
    w = World(with_course=False)
    a = w.admin
    assert 'already exists' in a.submit('/academic/group', {'group_code': 'se-2401'})[2]
    assert 'Group code' in a.submit('/academic/group', {'group_code': '!'})[2]
    assert len(q('SELECT * FROM group_memberships WHERE group_id=%s AND until_at IS NULL', (w.g1,))) == 2
    # Adding a student to another group transfers (only one current group).
    assert 'transferred' in a.submit('/academic/group/member', {'student_id': w.s1, 'group_id': w.g2})[3]
    current = q('SELECT group_id FROM group_memberships WHERE student_id=%s AND until_at IS NULL', (w.s1,))
    assert current == [{'group_id': w.g2}]
    assert len(q('SELECT * FROM group_memberships WHERE student_id=%s', (w.s1,))) == 2
    # Remove
    assert 'removed' in a.submit('/academic/group/member/remove', {'student_id': w.s1})[3]
    assert q('SELECT * FROM group_memberships WHERE student_id=%s AND until_at IS NULL', (w.s1,)) == []
    # Teachers cannot change group membership.
    t = Session('john@test.io')
    assert t.post('/academic/group/member', {'student_id': w.s1, 'group_id': w.g1}).status_code == 403
    assert t.post('/academic/group', {'group_code': 'X-1'}).status_code == 403
    assert t.post('/academic/group/member/remove', {'student_id': w.s2}).status_code == 403
    assert q('SELECT * FROM group_memberships WHERE student_id=%s AND until_at IS NULL', (w.s1,)) == []


def _xlsx(emails):
    rows = ''.join('<row r="%d"><c r="A%d" t="inlineStr"><is><t>%s</t></is></c></row>' % (i + 1, i + 1, e)
                   for i, e in enumerate(['email'] + emails))
    sheet = ('<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData>%s</sheetData></worksheet>' % rows)
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, 'w') as z:
        z.writestr('xl/worksheets/sheet1.xml', sheet)
    return buf.getvalue()


def test_csv_and_excel_import():
    w = World(with_course=False)
    a = w.admin
    new_group = ids('academic_groups')[-1]
    make_user('dan@test.io', 'student', 'Dan')
    st, loc, err, ok = a.submit('/academic/group/import', {'group_id': new_group},
                                files={'upload': ('list.csv', b'email\ndan@test.io\nALICE@test.io\n', 'text/csv')})
    assert ok and '1 added, 1 transferred' in ok, (err, ok)
    assert {r['email'] for r in q('''SELECT u.email FROM group_memberships m JOIN users u ON u.id=m.student_id
        WHERE m.group_id=%s AND m.until_at IS NULL''', (new_group,))} == {'dan@test.io', 'alice@test.io', 'carol@test.io'}
    # XLSX import
    make_user('eve@test.io', 'student', 'Eve')
    st, loc, err, ok = a.submit('/academic/group/import', {'group_id': w.g1},
                                files={'upload': ('list.xlsx', _xlsx(['eve@test.io']), 'application/octet-stream')})
    assert ok and '1 added' in ok
    # Unknown account: whole import rejected, nothing changed.
    before = q('SELECT * FROM group_memberships ORDER BY id')
    st, loc, err, ok = a.submit('/academic/group/import', {'group_id': w.g1},
                                files={'upload': ('l.csv', b'dan@test.io\nnobody@test.io\n', 'text/csv')})
    assert 'nobody@test.io' in err and 'No changes saved' in err
    st, loc, err, ok = a.submit('/academic/group/import', {'group_id': w.g1},
                                files={'upload': ('l.txt', b'dan@test.io', 'text/plain')})
    assert 'CSV or XLSX' in err
    assert q('SELECT * FROM group_memberships ORDER BY id') == before


def test_join_requests_individual_and_bulk():
    w = World(with_course=False)
    a = w.admin
    for e in ('f@test.io', 'g@test.io', 'h@test.io'):
        make_user(e, 'student')
    f, g, h = (Session(e) for e in ('f@test.io', 'g@test.io', 'h@test.io'))
    for s in (f, g, h):
        assert 'Request sent' in s.submit('/academic/group/request', {'group_id': w.g1})[3]
    assert 'pending' in f.submit('/academic/group/request', {'group_id': w.g2}, referer='/academic/join')[2]
    reqs = ids('group_join_requests')
    # Individual rejection
    assert '1 request(s) rejected' in a.submit('/academic/group/requests/review', {'ids': reqs[0], 'decision': 'rejected'})[3]
    # Bulk approval
    assert '2 request(s) approved' in a.submit('/academic/group/requests/review', {'ids': reqs[1:], 'decision': 'approved'})[3]
    statuses = [r['status'] for r in q('SELECT status FROM group_join_requests ORDER BY id')]
    assert statuses == ['rejected', 'approved', 'approved']
    assert len(q('SELECT * FROM group_memberships WHERE group_id=%s AND until_at IS NULL', (w.g1,))) == 4
    # Re-processing is rejected clearly
    assert 'already processed' in a.submit('/academic/group/requests/review', {'ids': reqs[0], 'decision': 'approved'})[2]
    # Student already in a group cannot request
    assert 'already in a group' in g.submit('/academic/group/request', {'group_id': w.g2}, referer='/academic/join')[2]


# --------------------------------------------------------------------------- US10 course assignment
def test_course_assignment_visible_to_group_students_teacher_and_parent():
    w = World()
    o = q1('SELECT * FROM course_offerings')
    assert (o['subject_id'], o['semester_id'], o['group_id'], o['teacher_id']) == (w.sub, w.sem, w.g1, w.t1)
    assert 'MATH101' in w.admin.get('/academic/courses').text
    # Students currently in SE-2401 see Mathematics; student in another group does not.
    for email in ('alice@test.io', 'bob@test.io'):
        page = Session(email).get('/academic').text
        assert 'Mathematics' in page and 'John Smith' in page and 'SE-2401' in page and 'Fall 2026' in page
    assert 'Mathematics' not in Session('carol@test.io').get('/academic').text
    # Teacher sees the course and roster.
    t = Session('john@test.io')
    assert 'Mathematics' in t.get('/academic').text
    page = t.get('/academic/offering/%d' % w.offering).text
    assert 'Alice Brown' in page and 'O&#39;Neil' in page and 'Carol White' not in page
    # Other teacher does not see it.
    assert 'Mathematics' not in Session('mary@test.io').get('/academic').text
    # Parent of Alice sees it.
    assert 'Mathematics' in Session('parent@test.io').get('/academic/parent/%d' % w.s1).text
    # The same subject can go to another group with another teacher.
    w.assign(w.sub, w.g2, w.t2)
    assert 'Mathematics' in Session('carol@test.io').get('/academic').text
    assert len(q('SELECT * FROM course_offerings')) == 2


def test_duplicate_course_assignment_is_rejected_with_clear_message():
    w = World()
    st, loc, err, ok = w.admin.submit('/academic/offering', {'subject_id': w.sub, 'semester_id': w.sem, 'group_id': w.g1,
                                                             'teacher_id': w.t2}, referer='/academic/courses')
    assert st == 303 and loc == '/academic/courses'
    assert 'Course already exists' in err and 'SE-2401' in err
    assert len(q('SELECT * FROM course_offerings')) == 1
    # Missing fields / unapproved teacher / closed semester are friendly errors, never JSON.
    assert 'Select a subject' in w.admin.submit('/academic/offering', {'subject_id': w.sub}, referer='/academic/courses')[2]
    pending = make_user('pending@test.io', 'teacher', 'Pending T', teacher_status='pending_approval')
    assert 'approved teacher' in w.admin.submit('/academic/offering', {'subject_id': w.sub, 'semester_id': w.sem,
                                                                       'group_id': w.g2, 'teacher_id': pending})[2]
    w.admin.submit('/academic/semester/status', {'semester_id': w.sem, 'status': 'closed'})
    assert 'closed' in w.admin.submit('/academic/offering', {'subject_id': w.sub, 'semester_id': w.sem,
                                                             'group_id': w.g2, 'teacher_id': w.t2})[2]
    assert len(q('SELECT * FROM course_offerings')) == 1


def test_admin_can_approve_teacher_directly_and_assign():
    w = World(with_course=False)
    tid = make_user('new.teacher@test.io', 'teacher', 'New Teacher', teacher_status='pending_approval')
    assert 'New Teacher' in w.admin.get('/academic/teachers').text
    st, loc, err, ok = w.admin.submit('/admin/action', {'target_id': tid, 'action': 'approve_teacher',
                                                        'next': '/academic/teachers'})
    assert loc == '/academic/teachers' and 'approved teacher' in ok
    assert q1('SELECT teacher_status FROM users WHERE id=%s', (tid,))['teacher_status'] == 'approved'
    w.assign(w.sub, w.g1, tid)
    assert 'Mathematics' in Session('new.teacher@test.io').get('/academic').text


# --------------------------------------------------------------------------- US11 access + assessments
def test_teacher_access_control_is_enforced_server_side():
    w = World()
    t1, t2 = Session('john@test.io'), Session('mary@test.io')
    aid = w.approved_assessment(t1)
    assert t2.get('/academic/offering/%d' % w.offering).status_code == 403
    assert t2.post('/academic/assessment', {'offering_id': w.offering, 'kind': 'exam', 'title': 'X', 'max_score': '10'}).status_code == 403
    r = t2.post('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1], 'score': ['50']})
    assert r.status_code == 403
    assert t2.post('/academic/grade', {'assessment_id': aid, 'student_id': w.s1, 'score': '50'}).status_code == 403
    # Students, parents and admins cannot enter grades.
    for email in ('alice@test.io', 'parent@test.io', 'admin@test.io'):
        assert Session(email).post('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1], 'score': ['99']}).status_code == 403
    assert q('SELECT * FROM grades') == []
    # Students outside the roster are rejected.
    assert t1.post('/academic/grades', {'assessment_id': aid, 'student_id': [w.s3], 'score': ['50']}).status_code == 403
    assert q('SELECT * FROM grades') == []
    # Admin-only pages are not available to teachers.
    for path in ('/academic/courses', '/academic/groups', '/academic/semesters', '/academic/assessments', '/academic/teachers'):
        assert t1.get(path).status_code == 403


def test_assessment_approval_workflow():
    w = World()
    t = Session('john@test.io')
    # Draft
    t.submit('/academic/assessment', {'offering_id': w.offering, 'kind': 'assignment', 'title': 'HW1', 'max_score': '20',
                                      'submit': '0'})
    a = q1('SELECT * FROM assessments')
    assert a['status'] == 'draft' and a['scale'] == 'points'
    # Grades cannot be entered before approval.
    assert 'not approved' in t.submit('/academic/grades', {'assessment_id': a['id'], 'student_id': [w.s1], 'score': ['10']})[2]
    # Admin cannot review a draft
    assert 'pending' in w.admin.submit('/academic/assessment/review', {'assessment_id': a['id'], 'decision': 'approved'})[2]
    # Submit, reject with note, resubmit with changes, approve.
    t.submit('/academic/assessment/update', {'assessment_id': a['id'], 'kind': 'assignment', 'title': 'HW1',
                                             'max_score': '20', 'submit': '1'})
    assert q1('SELECT status FROM assessments')['status'] == 'pending'
    assert 'HW1' in w.admin.get('/academic/assessments').text
    w.admin.submit('/academic/assessment/review', {'assessment_id': a['id'], 'decision': 'rejected', 'note': 'Use 25 points'})
    assert q1('SELECT status,review_note FROM assessments') == {'status': 'rejected', 'review_note': 'Use 25 points'}
    assert 'Use 25 points' in t.get('/academic/offering/%d?tab=assessments' % w.offering).text
    t.submit('/academic/assessment/update', {'assessment_id': a['id'], 'kind': 'assignment', 'title': 'HW1',
                                             'max_score': '25', 'submit': '1'})
    w.admin.submit('/academic/assessment/review', {'assessment_id': a['id'], 'decision': 'approved'})
    a = q1('SELECT * FROM assessments')
    assert a['status'] == 'approved' and str(a['max_score']) == '25.00' and a['reviewed_by'] == w.admin_id
    # Approved definitions are locked.
    assert 'cannot be changed' in t.submit('/academic/assessment/update', {'assessment_id': a['id'], 'max_score': '50'})[2]
    # Invalid assessment definitions
    assert 'Maximum score' in t.submit('/academic/assessment', {'offering_id': w.offering, 'kind': 'exam', 'title': 'E',
                                                                'max_score': '-5'})[2]
    assert 'Assignment or Exam' in t.submit('/academic/assessment', {'offering_id': w.offering, 'kind': 'quiz',
                                                                     'title': 'E', 'max_score': '5'})[2]
    # Bulk approval
    for title in ('Q1', 'Q2'):
        t.submit('/academic/assessment', {'offering_id': w.offering, 'kind': 'exam', 'title': title, 'max_score': '10'})
    pend = ids('assessments', "status='pending'")
    assert '2 assessment(s) approved' in w.admin.submit('/academic/assessment/review', {'assessment_id': pend, 'decision': 'approved'})[3]


# --------------------------------------------------------------------------- US11 grade entry
def test_grade_entry_and_validation():
    w = World()
    t = Session('john@test.io')
    aid = w.approved_assessment(t, max_score='50')
    back = '/academic/offering/%d?assessment=%d' % (w.offering, aid)
    page = t.get(back).text
    assert 'Save grades' in page and 'Alice Brown' in page
    for bad in ('-1', '50.01', 'abc', '10.123', 'NaN', 'Infinity'):
        st, loc, err, ok = t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1, w.s2], 'score': ['40', bad]})
        assert err and 'Not saved' in err and "O'Neil" in err, (bad, err)
    assert q('SELECT * FROM grades') == []  # all-or-nothing
    assert 'at least one' in t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1, w.s2], 'score': ['', '']})[2]
    st, loc, err, ok = t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1, w.s2], 'score': ['42.5', '']})
    assert ok == '1 grade(s) saved.' and loc == back
    g = grade_of(aid, w.s1)
    assert str(g['score']) == '42.50' and g['version'] == 1 and g['created_by'] == w.t1
    # Comma decimals and boundaries (0 and max) are valid.
    assert t.submit('/academic/grade', {'assessment_id': aid, 'student_id': w.s2, 'score': '0'})[3]
    w.admin.submit('/academic/group/member', {'student_id': w.s3, 'group_id': w.g1})
    assert t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s3], 'score': ['49,75']})[3]
    assert str(grade_of(aid, w.s3)['score']) == '49.75'
    # Duplicate grades are rejected and nothing is overwritten.
    assert 'already has a grade' in t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1], 'score': ['10']})[2]
    assert 'already exists' in t.submit('/academic/grade', {'assessment_id': aid, 'student_id': w.s1, 'score': '10'})[2]
    assert str(grade_of(aid, w.s1)['score']) == '42.50'
    assert len(q('SELECT * FROM grades')) == 3
    # Grades persist and are shown in the gradebook.
    assert '42.5' in t.get('/academic/offering/%d?tab=grades' % w.offering).text


# --------------------------------------------------------------------------- US12 grade edit
def test_grade_edit_history_and_optimistic_locking():
    w = World()
    t = Session('john@test.io')
    aid = w.approved_assessment(t)
    t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1], 'score': ['72.5']})
    g = grade_of(aid, w.s1)
    assert 'Edit grade' in t.get('/academic/offering/%d?assessment=%d' % (w.offering, aid)).text
    st, loc, err, ok = t.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': '80', 'version': g['version'],
                                                         'reason': 'Re-marked question 3'})
    assert 'from 72.5 to 80' in ok
    g2 = grade_of(aid, w.s1)
    assert str(g2['score']) == '80.00' and g2['version'] == 2 and g2['updated_by'] == w.t1
    rev = q1('SELECT * FROM grade_revisions')
    assert (str(rev['old_score']), str(rev['new_score']), rev['editor_id'], rev['reason'], rev['grade_id']) == \
        ('72.50', '80.00', w.t1, 'Re-marked question 3', g['id'])
    assert rev['edited_at'] > 0
    # Stale version (concurrent edit) is rejected, nothing overwritten.
    assert 'changed by someone else' in t.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': '90', 'version': 1})[2]
    assert str(grade_of(aid, w.s1)['score']) == '80.00'
    # Invalid new values
    for bad in ('101', '-3', 'x'):
        assert 'Score must be' in t.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': bad, 'version': 2})[2]
    # Second edit keeps full history
    t.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': '85', 'version': 2})
    assert [str(r['new_score']) for r in q('SELECT new_score FROM grade_revisions ORDER BY id')] == ['80.00', '85.00']
    page = t.get('/academic/grade/%d/history' % g['id']).text
    assert 'Re-marked question 3' in page and '72.5' in page and 'Original entry' in page
    # Students, parents, other teachers cannot edit.
    for email in ('alice@test.io', 'parent@test.io', 'mary@test.io', 'admin@test.io'):
        assert Session(email).post('/academic/grade/edit', {'grade_id': g['id'], 'score': '1', 'version': 3}).status_code == 403
    assert str(grade_of(aid, w.s1)['score']) == '85.00'


# --------------------------------------------------------------------------- visibility
def test_student_sees_only_own_grades():
    w = World()
    t = Session('john@test.io')
    aid = w.approved_assessment(t)
    w.approved_assessment(t, title='Homework', kind='assignment', max_score='10')
    t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1, w.s2], 'score': ['91', '37']})
    alice = Session('alice@test.io')
    page = alice.get('/academic/offering/%d?tab=grades' % w.offering).text
    assert '91' in page and '37' not in page and 'Homework' in page and 'Bob' not in page
    assert '91' in alice.get('/academic/my-grade-history').text
    bob_grade = grade_of(aid, w.s2)['id']
    assert alice.get('/academic/grade/%d/history' % bob_grade).status_code == 403
    # Carol (other group) cannot open the course.
    assert Session('carol@test.io').get('/academic/offering/%d' % w.offering).status_code == 403


def test_parent_sees_linked_child_read_only_and_not_others():
    w = World()
    t = Session('john@test.io')
    aid = w.approved_assessment(t)
    t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1, w.s2], 'score': ['88', '61']})
    g = grade_of(aid, w.s1)
    t.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': '90', 'version': 1, 'reason': 'typo'})
    p = Session('parent@test.io')
    assert 'Alice Brown' in p.get('/academic').text
    page = p.get('/academic/parent/%d' % w.s1).text
    assert 'Mathematics' in page and 'John Smith' in page and 'Midterm' in page and '90' in page and '61' not in page
    assert 'typo' in p.get('/academic/grade/%d/history' % g['id']).text
    # Unrelated student: no data.
    assert p.get('/academic/parent/%d' % w.s2).status_code == 404
    assert p.get('/academic/parent/%d' % w.s3).status_code == 404
    assert p.get('/academic/grade/%d/history' % grade_of(aid, w.s2)['id']).status_code == 404
    assert p.get('/academic/offering/%d' % w.offering).status_code == 403
    # Read-only: no write endpoints.
    assert p.post('/academic/grade/edit', {'grade_id': g['id'], 'score': '100', 'version': 2}).status_code == 403
    assert p.post('/academic/assessment', {'offering_id': w.offering, 'kind': 'exam', 'title': 'x', 'max_score': '1'}).status_code == 403
    # Existing US1–9 unlink still revokes access.
    p.post('/unlink', {'other': w.s1})
    assert p.get('/academic/parent/%d' % w.s1).status_code == 404


# --------------------------------------------------------------------------- historical data
def test_teacher_replacement_keeps_grades_and_revokes_former_teacher():
    w = World()
    t1 = Session('john@test.io')
    aid = w.approved_assessment(t1)
    t1.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1], 'score': ['70']})
    g = grade_of(aid, w.s1)
    st, loc, err, ok = w.admin.submit('/academic/offering/teacher', {'offering_id': w.offering, 'teacher_id': w.t2,
                                                                     'reason': 'Sick leave'})
    assert 'Teacher replaced' in ok
    assert 'already teaches' in w.admin.submit('/academic/offering/teacher', {'offering_id': w.offering, 'teacher_id': w.t2})[2]
    # Former teacher loses all access.
    assert 'Mathematics' not in t1.get('/academic').text
    assert t1.get('/academic/offering/%d' % w.offering).status_code == 403
    assert t1.post('/academic/grade/edit', {'grade_id': g['id'], 'score': '1', 'version': 1}).status_code == 403
    assert t1.post('/academic/grades', {'assessment_id': aid, 'student_id': [w.s2], 'score': ['1']}).status_code == 403
    # New teacher sees existing grades and can correct them.
    t2 = Session('mary@test.io')
    assert '70' in t2.get('/academic/offering/%d?tab=grades' % w.offering).text
    assert t2.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': '75', 'version': 1})[3]
    assert q1('SELECT created_by FROM grades')['created_by'] == w.t1
    hist = q('SELECT old_teacher_id,new_teacher_id,reason FROM teacher_assignments_history ORDER BY id')
    assert hist[-1] == {'old_teacher_id': w.t1, 'new_teacher_id': w.t2, 'reason': 'Sick leave'}
    # Bulk reassignment from Teachers page
    assert '1 course(s) reassigned' in w.admin.submit('/academic/teacher/reassign', {'from_teacher_id': w.t2,
                                                                                     'to_teacher_id': w.t1})[3]
    assert q1('SELECT teacher_id FROM course_offerings')['teacher_id'] == w.t1


def test_student_transfer_preserves_history():
    w = World()
    w.assign(w.sub, w.g2, w.t2)
    t = Session('john@test.io')
    aid = w.approved_assessment(t)
    t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1, w.s2], 'score': ['66', '55']})
    w.admin.submit('/academic/group/member', {'student_id': w.s1, 'group_id': w.g2})
    alice = Session('alice@test.io')
    hist = alice.get('/academic/my-grade-history').text
    assert '66' in hist and 'SE-2401' in hist
    assert 'SE-2402' in alice.get('/academic').text
    # Old course still opens read-only for her own grades.
    assert '66' in alice.get('/academic/offering/%d?tab=grades' % w.offering).text
    # Teacher sees her as a former member; cannot add new grades for her.
    page = t.get('/academic/offering/%d?tab=grades' % w.offering).text
    assert 'left the group' in page and '66' in page
    assert 'left the group' in t.get('/academic/offering/%d?tab=students' % w.offering).text.lower()
    aid2 = w.approved_assessment(t, title='Final')
    assert t.post('/academic/grades', {'assessment_id': aid2, 'student_id': [w.s1], 'score': ['10']}).status_code == 403
    # Parent still sees it.
    assert '66' in Session('parent@test.io').get('/academic/parent/%d?tab=grades' % w.s1).text
    assert str(grade_of(aid, w.s1)['score']) == '66.00'


def test_closed_semester_blocks_grading_and_reopen_restores():
    w = World()
    t = Session('john@test.io')
    aid = w.approved_assessment(t)
    t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1], 'score': ['50']})
    g = grade_of(aid, w.s1)
    w.admin.submit('/academic/semester/status', {'semester_id': w.sem, 'status': 'closed'})
    assert 'closed' in t.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': '60', 'version': 1})[2]
    assert 'closed' in t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s2], 'score': ['60']})[2]
    assert 'closed' in t.submit('/academic/assessment', {'offering_id': w.offering, 'kind': 'exam', 'title': 'x', 'max_score': '5'})[2]
    page = t.get('/academic/offering/%d?assessment=%d' % (w.offering, aid)).text
    assert 'Edit grade' not in page and 'read-only' in page
    assert str(grade_of(aid, w.s1)['score']) == '50.00' and grade_of(aid, w.s2) is None
    # Grades remain visible
    assert '50' in Session('alice@test.io').get('/academic/my-grade-history').text
    w.admin.submit('/academic/semester/status', {'semester_id': w.sem, 'status': 'active'})
    assert q1("SELECT * FROM audit WHERE action='semester.reopened'")['actor_id'] == w.admin_id
    assert t.submit('/academic/grade/edit', {'grade_id': g['id'], 'score': '60', 'version': 1})[3]


def test_group_deletion_and_subject_archive_preserve_history():
    w = World()
    t = Session('john@test.io')
    aid = w.approved_assessment(t)
    t.submit('/academic/grades', {'assessment_id': aid, 'student_id': [w.s1], 'score': ['77']})
    t.submit('/academic/grade/edit', {'grade_id': grade_of(aid, w.s1)['id'], 'score': '78', 'version': 1})
    counts = lambda: [len(q('SELECT * FROM %s' % tb)) for tb in ('course_offerings', 'assessments', 'grades', 'grade_revisions')]
    before = counts()
    st, loc, err, ok = w.admin.submit('/academic/group/delete', {'group_id': w.g1})
    assert 'deleted' in ok
    assert counts() == before
    assert q1('SELECT deleted_at FROM academic_groups WHERE id=%s', (w.g1,))['deleted_at'] is not None
    assert q1('SELECT status,group_id,group_code_snapshot FROM course_offerings') == \
        {'status': 'archived', 'group_id': w.g1, 'group_code_snapshot': 'SE-2401'}
    assert q('SELECT * FROM group_memberships WHERE group_id=%s AND until_at IS NULL', (w.g1,)) == []
    # Code can be reused; history still shows the original code.
    assert w.admin.submit('/academic/group', {'group_code': 'SE-2401'})[3]
    hist = Session('alice@test.io').get('/academic/my-grade-history').text
    assert '78' in hist and 'SE-2401' in hist
    assert '78' in Session('parent@test.io').get('/academic/parent/%d?tab=grades' % w.s1).text
    # Read-only for the teacher now.
    assert 'deleted' in t.submit('/academic/grade/edit', {'grade_id': grade_of(aid, w.s1)['id'], 'score': '1', 'version': 2})[2] \
        or 'archived' in t.submit('/academic/grade/edit', {'grade_id': grade_of(aid, w.s1)['id'], 'score': '1', 'version': 2})[2]
    # Subject archive
    w.admin.submit('/academic/subject/archive', {'subject_id': w.sub})
    assert counts() == before
    assert '78' in Session('alice@test.io').get('/academic/my-grade-history').text
    assert 'Choose an active' in w.admin.submit('/academic/offering', {'subject_id': w.sub, 'semester_id': w.sem,
                                                                       'group_id': w.g2, 'teacher_id': w.t1})[2]


def test_course_archive_and_restore():
    w = World()
    w.admin.submit('/academic/offering/status', {'offering_id': w.offering, 'status': 'archived'})
    assert 'Mathematics' not in Session('alice@test.io').get('/academic').text
    t = Session('john@test.io')
    assert 'archived' in t.get('/academic').text
    w.admin.submit('/academic/offering/status', {'offering_id': w.offering, 'status': 'active'})
    assert 'Mathematics' in Session('alice@test.io').get('/academic').text


# --------------------------------------------------------------------------- security
def test_csrf_and_role_authorization():
    w = World()
    a = w.admin
    for path, data in [('/academic/subject', {'code': 'X101', 'name': 'Xx'}),
                       ('/academic/offering', {'subject_id': w.sub, 'semester_id': w.sem, 'group_id': w.g2, 'teacher_id': w.t2}),
                       ('/academic/semester/status', {'semester_id': w.sem, 'status': 'closed'}),
                       ('/academic/group/delete', {'group_id': w.g1})]:
        assert a.post(path, dict(data, csrf_token='forged')).status_code == 403
        assert a.post(path, data, token=False).status_code in (403, 303)
    assert len(q('SELECT * FROM subjects')) == 1 and len(q('SELECT * FROM course_offerings')) == 1
    assert q1('SELECT status FROM semesters')['status'] == 'active'
    assert q1('SELECT deleted_at FROM academic_groups WHERE id=%s', (w.g1,))['deleted_at'] is None
    # Non-admins cannot use admin endpoints.
    student = Session('alice@test.io')
    for path, data in [('/academic/subject', {'code': 'Y101', 'name': 'Yy'}),
                       ('/academic/semester', {'label': 'S', 'start_date': '2026-01-01', 'end_date': '2026-02-01'}),
                       ('/academic/offering/teacher', {'offering_id': w.offering, 'teacher_id': w.t2}),
                       ('/academic/assessment/review', {'assessment_id': 1, 'decision': 'approved'}),
                       ('/admin/action', {'target_id': w.t2, 'action': 'block'})]:
        assert student.post(path, data).status_code == 403, path
    assert Session('john@test.io').get('/academic/admin').status_code == 403
    assert len(q('SELECT * FROM subjects')) == 1
    # Logged-out
    from fastapi.testclient import TestClient
    anon = TestClient(app.app)
    assert anon.get('/academic/offering/%d' % w.offering).status_code == 403
    r = anon.get('/academic/courses', headers={'accept': 'text/html'})
    assert r.status_code == 403 and 'Log in' in r.text and '"detail"' not in r.text


def test_error_pages_are_friendly_html():
    w = World()
    alice = Session('alice@test.io')
    r = alice.get('/academic/offering/99999')
    assert r.status_code == 404 and '<html' in r.text and '"detail"' not in r.text
    # Validation error (missing field) on an academic form: redirect with message, not JSON.
    r = w.admin.post('/academic/offering/status', {'offering_id': w.offering}, referer='/academic/courses')
    assert r.status_code == 303 and r.headers['location'] == '/academic/courses'


# --------------------------------------------------------------------------- full acceptance scenario
def test_end_to_end_acceptance_scenario():
    admin_id = make_user('admin@test.io', 'admin', 'Main Admin')
    john = make_user('john@test.io', 'teacher', 'John Smith')
    mary = make_user('mary@test.io', 'teacher', 'Mary Jones')
    alice = make_user('alice@test.io', 'student', 'Alice Brown')
    bob = make_user('bob@test.io', 'student', 'Bob Green')
    parent = make_user('parent@test.io', 'parent', 'Paula Brown')
    link_parent(parent, alice)
    a = Session('admin@test.io')
    # 1. active semester
    assert a.submit('/academic/semester', {'label': 'Fall 2026', 'start_date': '2026-09-01', 'end_date': '2026-12-20',
                                           'activate': '1'})[3]
    sem = q1('SELECT * FROM semesters')
    assert sem['status'] == 'active' and sem['is_current']
    # 2. subject
    assert a.submit('/academic/subject', {'code': 'MATH101', 'name': 'Mathematics'})[3]
    sub = ids('subjects')[0]
    # 3. group + students
    a.submit('/academic/group', {'group_code': 'SE-2401'})
    a.submit('/academic/group', {'group_code': 'SE-2402'})
    g1, g2 = ids('academic_groups')
    for s in (alice, bob):
        a.submit('/academic/group/member', {'student_id': s, 'group_id': g1})
    # 4. assign course
    st, loc, err, ok = a.submit('/academic/offering', {'subject_id': sub, 'semester_id': sem['id'], 'group_id': g1,
                                                       'teacher_id': john}, referer='/academic/courses')
    assert 'Course assigned' in ok and 'MATH101' in a.get('/academic/courses').text
    off = ids('course_offerings')[0]
    assert 'Course already exists' in a.submit('/academic/offering', {'subject_id': sub, 'semester_id': sem['id'],
                                                                      'group_id': g1, 'teacher_id': mary})[2]
    # 5. teacher roster
    tj = Session('john@test.io')
    page = tj.get('/academic/offering/%d' % off).text
    assert 'Alice Brown' in page and 'Bob Green' in page
    # 6. assessment
    tj.submit('/academic/assessment', {'offering_id': off, 'kind': 'exam', 'title': 'Midterm', 'max_score': '100', 'submit': '1'})
    aid = ids('assessments')[0]
    assert 'not approved' in tj.submit('/academic/grades', {'assessment_id': aid, 'student_id': [alice], 'score': ['80']})[2]
    # 7. admin approval
    a.submit('/academic/assessment/review', {'assessment_id': aid, 'decision': 'approved'})
    # 8. grades
    assert '2 grade(s) saved' in tj.submit('/academic/grades', {'assessment_id': aid, 'student_id': [alice, bob],
                                                                'score': ['72.5', '88']})[3]
    # unauthorized modification rejected
    tm = Session('mary@test.io')
    ga = grade_of(aid, alice)
    assert tm.post('/academic/grade/edit', {'grade_id': ga['id'], 'score': '100', 'version': 1}).status_code == 403
    # 9. correction
    assert tj.submit('/academic/grade/edit', {'grade_id': ga['id'], 'score': '80', 'version': 1, 'reason': 'Recount'})[3]
    # 10. student view
    sa = Session('alice@test.io')
    assert 'Mathematics' in sa.get('/academic').text
    page = sa.get('/academic/offering/%d?tab=grades' % off).text
    assert '80' in page and '88' not in page
    # 11. parent view
    sp = Session('parent@test.io')
    page = sp.get('/academic/parent/%d' % alice).text
    assert 'Mathematics' in page and '80' in page
    # 12. close semester: editing unavailable
    a.submit('/academic/semester/status', {'semester_id': sem['id'], 'status': 'closed'})
    assert 'closed' in tj.submit('/academic/grade/edit', {'grade_id': ga['id'], 'score': '81', 'version': 2})[2]
    assert 'Edit grade' not in tj.get('/academic/offering/%d?assessment=%d' % (off, aid)).text
    # 13. reopen, audited
    a.submit('/academic/semester/status', {'semester_id': sem['id'], 'status': 'active'})
    assert q1("SELECT actor_id FROM audit WHERE action='semester.reopened'")['actor_id'] == admin_id
    # 14. replace teacher, history retained
    a.submit('/academic/offering/teacher', {'offering_id': off, 'teacher_id': mary})
    assert tj.get('/academic/offering/%d' % off).status_code == 403
    assert '80' in tm.get('/academic/offering/%d?tab=grades' % off).text
    assert len(q('SELECT * FROM grade_revisions')) == 1
    # 15. transfer student, history kept
    a.submit('/academic/group/member', {'student_id': alice, 'group_id': g2})
    assert '80' in sa.get('/academic/my-grade-history').text
    # 16. archive subject and delete group, grades kept
    a.submit('/academic/subject/archive', {'subject_id': sub})
    a.submit('/academic/group/delete', {'group_id': g1})
    assert len(q('SELECT * FROM grades')) == 2 and len(q('SELECT * FROM grade_revisions')) == 1
    assert '80' in sa.get('/academic/my-grade-history').text
    assert '80' in sp.get('/academic/parent/%d?tab=grades' % alice).text
    assert '88' in Session('bob@test.io').get('/academic/my-grade-history').text


# --------------------------------------------------------------------------- migration safety
def test_migration_from_previous_schema_preserves_data(monkeypatch):
    """Simulate a database created by the earlier delivery, then run the new init-db."""
    drop_all_tables()
    monkeypatch.setattr(academic, 'migrate', lambda c: [c.execute(s) for s in academic.MIGRATION])
    app.setup()  # US1–9 tables + version-1 academic tables only
    monkeypatch.undo()
    with app.db() as c:
        assert not c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='assessments' AND column_name='status'").fetchone()
        uid = lambda e, r: c.execute("INSERT INTO users(email,password_hash,full_name,role,verified,teacher_status,created_at) "
                                     "VALUES(%s,'pbkdf2$600000$00$00',%s,%s,TRUE,%s,1) RETURNING id",
                                     (e, e, r, 'approved' if r == 'teacher' else 'none')).fetchone()['id']
        adm, t, s, p = uid('a@x.io', 'admin'), uid('t@x.io', 'teacher'), uid('s@x.io', 'student'), uid('p@x.io', 'parent')
        c.execute('INSERT INTO parent_links VALUES(%s,%s,1)', (p, s))
        c.execute("INSERT INTO invites(student_id,code_hash,expires_at) VALUES(%s,'h',9)", (s,))
        c.execute("INSERT INTO audit(actor_id,action,target_id,created_at) VALUES(%s,'block',%s,1)", (adm, s))
        c.execute("INSERT INTO subjects(code,name) VALUES('OLD101','Old')")
        c.execute("INSERT INTO semesters(label,start_date,end_date,status) VALUES('Old term','2025-01-01','2025-06-01','active')")
        c.execute("INSERT INTO academic_groups(group_code,created_at) VALUES('G1',1)")
        c.execute("INSERT INTO group_memberships(student_id,group_id,group_code_snapshot,since_at) VALUES(%s,1,'G1',1)", (s,))
        c.execute("INSERT INTO course_offerings(subject_id,semester_id,group_id,group_code_snapshot,teacher_id,created_at) VALUES(1,1,1,'G1',%s,1)", (t,))
        c.execute("INSERT INTO assessments(offering_id,kind,title,max_score,created_by,created_at) VALUES(1,'exam','Old exam',100,%s,1)", (t,))
        c.execute("INSERT INTO grades(assessment_id,student_id,score,created_by,created_at,updated_at) VALUES(1,%s,55,%s,1,1)", (s, t))
        c.execute("INSERT INTO grade_revisions(grade_id,old_score,new_score,editor_id,edited_at) VALUES(1,50,55,%s,1)", (t,))
    snapshot = {tb: q('SELECT * FROM %s ORDER BY 1' % tb) for tb in ('users', 'parent_links', 'invites', 'grades', 'grade_revisions')}
    app.setup()
    app.setup()
    for tb, rows in snapshot.items():
        after = q('SELECT * FROM %s ORDER BY 1' % tb)
        assert [{k: r[k] for k in rows[0]} for r in after] == rows, tb
    assert q1('SELECT status FROM assessments')['status'] == 'approved'   # legacy assessments stay usable
    assert q1('SELECT status FROM course_offerings')['status'] == 'active'
    assert q1('SELECT is_current FROM semesters')['is_current'] is True
    assert q1('SELECT details FROM audit')['details'] == ''
    # New assessments default to pending.
    with app.db() as c:
        c.execute("INSERT INTO assessments(offering_id,kind,title,max_score,created_at) VALUES(1,'exam','New',10,1)")
    assert q1("SELECT status FROM assessments WHERE title='New'")['status'] == 'pending'
