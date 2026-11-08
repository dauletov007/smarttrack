"""SmartTrack US10–12 academic domain: courses, groups, semesters, assessments, grades.

The schema migration is additive and repeatable (safe to run `app.py init-db` many times).
It never drops tables or columns and preserves all US1–9 data and earlier US10–12 data.
"""
import csv
import io
import re
import zipfile
from datetime import date
from decimal import Decimal, InvalidOperation
from typing import List
from urllib.parse import urlencode
from xml.etree import ElementTree as ET

from fastapi import Request, Form, HTTPException, UploadFile, File

try:
    from psycopg.errors import UniqueViolation
except ImportError:  # Unit-test fallback when psycopg is unavailable
    class UniqueViolation(Exception):
        pass


# ---------------------------------------------------------------------------
# Schema
# ---------------------------------------------------------------------------
# Version 1 tables (unchanged from the earlier US10–12 delivery, kept verbatim so
# existing databases are untouched by CREATE TABLE IF NOT EXISTS).
MIGRATION = [
"""CREATE TABLE IF NOT EXISTS subjects (
 id BIGSERIAL PRIMARY KEY, code TEXT NOT NULL UNIQUE, name TEXT NOT NULL,
 description TEXT NOT NULL DEFAULT '', archived_at BIGINT)""",
"""CREATE TABLE IF NOT EXISTS semesters (
 id BIGSERIAL PRIMARY KEY, label TEXT NOT NULL UNIQUE, start_date DATE NOT NULL,
 end_date DATE NOT NULL, status TEXT NOT NULL DEFAULT 'draft'
 CHECK (status IN ('draft','active','closed')), CHECK(end_date >= start_date))""",
"""CREATE TABLE IF NOT EXISTS academic_groups (
 id BIGSERIAL PRIMARY KEY, group_code TEXT NOT NULL UNIQUE, created_at BIGINT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS group_memberships (
 id BIGSERIAL PRIMARY KEY, student_id BIGINT NOT NULL REFERENCES users(id),
 group_id BIGINT REFERENCES academic_groups(id) ON DELETE SET NULL,
 group_code_snapshot TEXT NOT NULL, since_at BIGINT NOT NULL, until_at BIGINT,
 CHECK(until_at IS NULL OR until_at >= since_at))""",
"""CREATE UNIQUE INDEX IF NOT EXISTS one_current_group_per_student
 ON group_memberships(student_id) WHERE until_at IS NULL""",
"""CREATE TABLE IF NOT EXISTS group_join_requests (
 id BIGSERIAL PRIMARY KEY, student_id BIGINT NOT NULL REFERENCES users(id),
 group_id BIGINT NOT NULL REFERENCES academic_groups(id) ON DELETE CASCADE,
 status TEXT NOT NULL DEFAULT 'pending' CHECK(status IN ('pending','approved','rejected')),
 created_at BIGINT NOT NULL, reviewed_by BIGINT REFERENCES users(id), reviewed_at BIGINT)""",
"""CREATE UNIQUE INDEX IF NOT EXISTS one_pending_join_request
 ON group_join_requests(student_id) WHERE status='pending'""",
"""CREATE TABLE IF NOT EXISTS course_offerings (
 id BIGSERIAL PRIMARY KEY, subject_id BIGINT NOT NULL REFERENCES subjects(id),
 semester_id BIGINT NOT NULL REFERENCES semesters(id),
 group_id BIGINT REFERENCES academic_groups(id) ON DELETE SET NULL,
 group_code_snapshot TEXT NOT NULL, teacher_id BIGINT REFERENCES users(id),
 created_at BIGINT NOT NULL, UNIQUE(subject_id, semester_id, group_id))""",
"""CREATE TABLE IF NOT EXISTS teacher_assignments_history (
 id BIGSERIAL PRIMARY KEY, offering_id BIGINT NOT NULL REFERENCES course_offerings(id),
 old_teacher_id BIGINT REFERENCES users(id), new_teacher_id BIGINT REFERENCES users(id),
 changed_by BIGINT REFERENCES users(id), changed_at BIGINT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS assessments (
 id BIGSERIAL PRIMARY KEY, offering_id BIGINT NOT NULL REFERENCES course_offerings(id),
 kind TEXT NOT NULL CHECK(kind IN ('assignment','exam')),
 title TEXT NOT NULL, max_score NUMERIC(9,2) NOT NULL CHECK(max_score > 0),
 created_by BIGINT REFERENCES users(id), created_at BIGINT NOT NULL)""",
"""CREATE TABLE IF NOT EXISTS grades (
 id BIGSERIAL PRIMARY KEY, assessment_id BIGINT NOT NULL REFERENCES assessments(id),
 student_id BIGINT NOT NULL REFERENCES users(id), score NUMERIC(9,2) NOT NULL CHECK(score >= 0),
 created_by BIGINT REFERENCES users(id), created_at BIGINT NOT NULL,
 updated_at BIGINT NOT NULL, version INTEGER NOT NULL DEFAULT 1,
 UNIQUE(assessment_id,student_id))""",
"""CREATE TABLE IF NOT EXISTS grade_revisions (
 id BIGSERIAL PRIMARY KEY, grade_id BIGINT NOT NULL REFERENCES grades(id),
 old_score NUMERIC(9,2) NOT NULL, new_score NUMERIC(9,2) NOT NULL,
 editor_id BIGINT REFERENCES users(id), edited_at BIGINT NOT NULL, reason TEXT NOT NULL DEFAULT '')""",
]

# Version 2: additive columns, constraints and indexes. Every statement is idempotent.
MIGRATION_V2 = [
# Semesters: one "current" semester; several semesters may be open (e.g. a reopened past term).
"ALTER TABLE semesters ADD COLUMN IF NOT EXISTS is_current BOOLEAN NOT NULL DEFAULT FALSE",
"ALTER TABLE semesters ADD COLUMN IF NOT EXISTS closed_at BIGINT",
"CREATE UNIQUE INDEX IF NOT EXISTS one_current_semester ON semesters((TRUE)) WHERE is_current",
"""UPDATE semesters SET is_current=TRUE WHERE id=(SELECT id FROM semesters WHERE status='active'
   ORDER BY start_date DESC, id DESC LIMIT 1) AND NOT EXISTS (SELECT 1 FROM semesters WHERE is_current)""",
# Groups are soft-deleted so memberships, offerings and grades keep a valid reference.
"ALTER TABLE academic_groups ADD COLUMN IF NOT EXISTS deleted_at BIGINT",
# Course offerings can be archived (read-only) instead of deleted.
"ALTER TABLE course_offerings ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'active'",
"ALTER TABLE course_offerings ADD COLUMN IF NOT EXISTS archived_at BIGINT",
"""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='course_offerings_status_check') THEN
   ALTER TABLE course_offerings ADD CONSTRAINT course_offerings_status_check CHECK (status IN ('active','archived'));
   END IF; END $$""",
"ALTER TABLE teacher_assignments_history ADD COLUMN IF NOT EXISTS reason TEXT NOT NULL DEFAULT ''",
# Assessment approval workflow. Assessments that already exist were used for grading,
# so they are recorded as approved; new assessments default to pending.
"ALTER TABLE assessments ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'approved'",
"ALTER TABLE assessments ALTER COLUMN status SET DEFAULT 'pending'",
"""DO $$ BEGIN IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname='assessments_status_check') THEN
   ALTER TABLE assessments ADD CONSTRAINT assessments_status_check CHECK (status IN ('draft','pending','approved','rejected'));
   END IF; END $$""",
"ALTER TABLE assessments ADD COLUMN IF NOT EXISTS scale TEXT NOT NULL DEFAULT 'points'",
"ALTER TABLE assessments ADD COLUMN IF NOT EXISTS reviewed_by BIGINT REFERENCES users(id)",
"ALTER TABLE assessments ADD COLUMN IF NOT EXISTS reviewed_at BIGINT",
"ALTER TABLE assessments ADD COLUMN IF NOT EXISTS review_note TEXT NOT NULL DEFAULT ''",
"ALTER TABLE assessments ADD COLUMN IF NOT EXISTS updated_at BIGINT",
"ALTER TABLE grades ADD COLUMN IF NOT EXISTS updated_by BIGINT REFERENCES users(id)",
"ALTER TABLE audit ADD COLUMN IF NOT EXISTS details TEXT NOT NULL DEFAULT ''",
# Lookup indexes.
"CREATE INDEX IF NOT EXISTS idx_offerings_teacher ON course_offerings(teacher_id)",
"CREATE INDEX IF NOT EXISTS idx_offerings_group ON course_offerings(group_id)",
"CREATE INDEX IF NOT EXISTS idx_memberships_group_current ON group_memberships(group_id) WHERE until_at IS NULL",
"CREATE INDEX IF NOT EXISTS idx_assessments_offering ON assessments(offering_id)",
"CREATE INDEX IF NOT EXISTS idx_grades_student ON grades(student_id)",
"CREATE INDEX IF NOT EXISTS idx_grade_revisions_grade ON grade_revisions(grade_id)",
]


def migrate(c):
    for statement in MIGRATION + MIGRATION_V2:
        c.execute(statement)


# ---------------------------------------------------------------------------
# Validation helpers
# ---------------------------------------------------------------------------
class FormError(HTTPException):
    """A user-correctable form problem. The app shows it as a message on `back`."""
    def __init__(self, status_code, detail, back=None):
        super().__init__(status_code, detail)
        self.back = back


MAX_POINTS = Decimal('10000')


def score_value(s, maximum):
    """Parse a score: numeric, 0..maximum, up to two decimals. Comma decimals accepted."""
    try:
        text = str(s).strip().replace(',', '.')
        if not text:
            raise ValueError
        v = Decimal(text)
        if not v.is_finite() or v.as_tuple().exponent < -2 or not 0 <= v <= Decimal(str(maximum)):
            raise ValueError
        return v
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(400, 'Score must be a number between 0 and %s (up to two decimals).' % fmt_num(maximum))


def max_score_value(s):
    try:
        text = str(s).strip().replace(',', '.')
        v = Decimal(text)
        if not v.is_finite() or v.as_tuple().exponent < -2 or not 0 < v <= MAX_POINTS:
            raise ValueError
        return v
    except (InvalidOperation, ValueError, TypeError):
        raise HTTPException(400, 'Maximum score must be a number above 0 and at most 10000 (up to two decimals).')


def fmt_num(v):
    """82.50 -> 82.5, 100.00 -> 100."""
    if v is None:
        return ''
    try:
        d = Decimal(str(v))
    except InvalidOperation:
        return str(v)
    text = format(d.quantize(Decimal('0.01')), 'f')
    return text.rstrip('0').rstrip('.') if '.' in text else text


def read_student_emails(filename, data):
    if len(data) > 1024 * 1024:
        raise HTTPException(400, 'File must be smaller than 1MB.')
    filename = (filename or '').lower()
    if filename.endswith('.csv'):
        try:
            rows = list(csv.reader(io.StringIO(data.decode('utf-8-sig'))))
        except (UnicodeDecodeError, csv.Error):
            raise HTTPException(400, 'CSV must be UTF-8.')
        values = [x[0].strip().lower() for x in rows if x and x[0].strip()]
    elif filename.endswith('.xlsx'):
        try:
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if len(z.namelist()) > 100 or sum(f.file_size for f in z.infolist()) > 8 * 1024 * 1024:
                    raise ValueError('Oversized spreadsheet')
                ns = {'m': 'http://schemas.openxmlformats.org/spreadsheetml/2006/main'}
                strings = []
                if 'xl/sharedStrings.xml' in z.namelist():
                    root = ET.fromstring(z.read('xl/sharedStrings.xml'))
                    strings = [''.join(t.text or '' for t in x.findall('.//m:t', ns)) for x in root.findall('m:si', ns)]
                sheets = sorted((n for n in z.namelist() if re.fullmatch(r'xl/worksheets/sheet\d+\.xml', n)),
                                key=lambda n: int(re.search(r'(\d+)\.xml$', n).group(1)))
                if not sheets:
                    raise ValueError('No worksheet')
                root = ET.fromstring(z.read(sheets[0]))
                values = []
                for row in root.findall('.//m:sheetData/m:row', ns):
                    cell = next((x for x in row.findall('m:c', ns) if re.fullmatch(r'A\d+', x.attrib.get('r', 'A1'))), None)
                    if cell is None:
                        continue
                    value = cell.find('m:v', ns)
                    if cell.attrib.get('t') == 'inlineStr':
                        txt = ''.join(n.text or '' for n in cell.findall('.//m:t', ns))
                    else:
                        txt = value.text if value is not None else ''
                        if cell.attrib.get('t') == 's':
                            txt = strings[int(txt)]
                    if txt and txt.strip():
                        values.append(txt.strip().lower())
        except (ValueError, KeyError, IndexError, ET.ParseError, zipfile.BadZipFile):
            raise HTTPException(400, 'Invalid XLSX file.')
    else:
        raise HTTPException(400, 'Use a CSV or XLSX file with student emails in the first column.')
    values = [v for v in values if v not in ('email', 'e-mail', 'student email', 'student_email')]
    if len(values) > 500 or not values:
        raise HTTPException(400, 'Import 1–500 student emails.')
    if len(set(values)) != len(values):
        raise HTTPException(400, 'The file contains duplicate emails. No changes saved.')
    bad = [v for v in values if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+', v)]
    if bad:
        raise HTTPException(400, 'Invalid email in file: %s. No changes saved.' % ', '.join(bad[:5]))
    return values


def normalize_subject_code(code):
    """Accept common course-code separators while storing a predictable identifier."""
    return re.sub(r'\s+', '-', (code or '').strip().upper())


def subject_form_error(name, code, description):
    """Return a field-specific message instead of a raw invalid-form response."""
    if not 2 <= len(name) <= 120:
        return 'Subject name must have 2–120 characters.'
    if not re.fullmatch(r'[A-Z0-9_.+\-]{2,32}', code):
        return 'Code must have 2–32 Latin letters, numbers, spaces, or . _ + - (for example CS101 or MATH-102).'
    if len(description) > 2000:
        return 'Description must be at most 2000 characters.'
    return None


def writable_reason(o):
    """Why grades/assessments of an offering are read-only, or None when editable."""
    if o.get('semester_status') == 'closed':
        return 'The semester is closed. Grades are read-only.'
    if o.get('semester_status') != 'active':
        return 'The semester is not open yet. An administrator must open it before grading.'
    if o.get('status', 'active') != 'active':
        return 'This course is archived. Grades are read-only.'
    if o.get('archived_at') is not None:
        return 'The subject is archived. Grades are read-only.'
    if o.get('group_id') is None or o.get('group_deleted_at') is not None:
        return 'The group of this course was deleted. Grades are read-only.'
    return None


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
OFFERING_SQL = '''SELECT o.*, s.status semester_status, s.label semester_label, s.label,
    s.start_date, s.end_date, sub.name subject_name, sub.code subject_code, sub.description,
    sub.archived_at, g.group_code, g.deleted_at group_deleted_at, t.full_name teacher_name
    FROM course_offerings o JOIN semesters s ON s.id=o.semester_id
    JOIN subjects sub ON sub.id=o.subject_id
    LEFT JOIN academic_groups g ON g.id=o.group_id
    LEFT JOIN users t ON t.id=o.teacher_id WHERE o.id=%s'''

APPROVED_TEACHER_SQL = ("SELECT id,full_name FROM users WHERE id=%s AND role='teacher' AND verified "
                        "AND teacher_status='approved' AND NOT blocked")


# ---------------------------------------------------------------------------
# Read-only queries shared by pages
# ---------------------------------------------------------------------------
def student_courses(c, student_id):
    """Courses of the student's current group (derived from real assignments)."""
    return c.execute('''SELECT o.id,sub.code,sub.name,s.label,s.status semester_status,s.is_current,
        o.group_code_snapshot,t.full_name teacher_name,
        (SELECT COUNT(*) FROM assessments a WHERE a.offering_id=o.id AND a.status='approved') assessments
        FROM group_memberships m JOIN course_offerings o ON o.group_id=m.group_id
        JOIN subjects sub ON sub.id=o.subject_id JOIN semesters s ON s.id=o.semester_id
        LEFT JOIN users t ON t.id=o.teacher_id
        WHERE m.student_id=%s AND m.until_at IS NULL AND o.status='active' AND sub.archived_at IS NULL
        ORDER BY s.is_current DESC, s.start_date DESC, sub.code''', (student_id,)).fetchall()

def student_grades(c, student_id):
    """Every grade of the student, including earlier groups and closed semesters."""
    return c.execute('''SELECT g.id grade_id,g.score,g.version,g.updated_at,a.id assessment_id,a.title,a.kind,a.max_score,
        o.id offering_id,sub.code,sub.name,s.label,o.group_code_snapshot,t.full_name teacher_name,
        (SELECT COUNT(*) FROM grade_revisions r WHERE r.grade_id=g.id) revisions
        FROM grades g JOIN assessments a ON a.id=g.assessment_id
        JOIN course_offerings o ON o.id=a.offering_id JOIN subjects sub ON sub.id=o.subject_id
        JOIN semesters s ON s.id=o.semester_id LEFT JOIN users t ON t.id=o.teacher_id
        WHERE g.student_id=%s ORDER BY s.start_date DESC, sub.code, a.id''', (student_id,)).fetchall()

def student_course_detail(c, student_id, offering_ids):
    """Approved assessments of the given offerings with this student's score (or none)."""
    if not offering_ids:
        return {}
    rows = c.execute('''SELECT a.id,a.offering_id,a.title,a.kind,a.max_score,g.id grade_id,g.score,g.updated_at,
        (SELECT COUNT(*) FROM grade_revisions r WHERE r.grade_id=g.id) revisions
        FROM assessments a LEFT JOIN grades g ON g.assessment_id=a.id AND g.student_id=%s
        WHERE a.offering_id=ANY(%s) AND a.status='approved' ORDER BY a.id''', (student_id, list(offering_ids))).fetchall()
    out = {}
    for r in rows:
        out.setdefault(r['offering_id'], []).append(r)
    return out



def current_semester_row(c):
    return c.execute('SELECT * FROM semesters WHERE is_current LIMIT 1').fetchone()


def current_group_row(c, student_id):
    return c.execute('''SELECT g.id,g.group_code,m.since_at FROM group_memberships m JOIN academic_groups g ON g.id=m.group_id
        WHERE m.student_id=%s AND m.until_at IS NULL''', (student_id,)).fetchone()


def teacher_courses(c, teacher_id):
    return c.execute('''SELECT o.id,o.status,sub.code,sub.name,sub.archived_at,s.label,s.status semester_status,
        o.group_code_snapshot,g.deleted_at group_deleted_at,o.group_id,
        (SELECT COUNT(*) FROM group_memberships m JOIN users su ON su.id=m.student_id
           WHERE m.group_id=o.group_id AND m.until_at IS NULL AND NOT su.blocked) students,
        (SELECT COUNT(*) FROM assessments a WHERE a.offering_id=o.id AND a.status='approved') approved,
        (SELECT COUNT(*) FROM assessments a WHERE a.offering_id=o.id AND a.status='pending') pending,
        (SELECT COUNT(*) FROM assessments a WHERE a.offering_id=o.id AND a.status IN ('draft','rejected')) drafts,
        (SELECT COUNT(*) FROM grades gr JOIN assessments a ON a.id=gr.assessment_id WHERE a.offering_id=o.id) grade_count
        FROM course_offerings o JOIN subjects sub ON sub.id=o.subject_id JOIN semesters s ON s.id=o.semester_id
        LEFT JOIN academic_groups g ON g.id=o.group_id WHERE o.teacher_id=%s
        ORDER BY (o.status='active' AND s.status<>'closed') DESC, s.start_date DESC, sub.code''', (teacher_id,)).fetchall()


def overview_data(c, u):
    """Data for the role overview on /dashboard (read-only)."""
    out = {'semester': current_semester_row(c)}
    if u['role'] == 'teacher':
        rows = teacher_courses(c, u['id'])
        active = [r for r in rows if r['status'] == 'active' and r['semester_status'] != 'closed']
        out['courses'] = active
        out['student_total'] = c.execute("""SELECT COUNT(DISTINCT m.student_id) n FROM course_offerings o
            JOIN semesters s ON s.id=o.semester_id JOIN group_memberships m ON m.group_id=o.group_id AND m.until_at IS NULL
            JOIN users su ON su.id=m.student_id AND NOT su.blocked
            WHERE o.teacher_id=%s AND o.status='active' AND s.status<>'closed'""", (u['id'],)).fetchone()['n']
        out['pending_total'] = sum(r['pending'] for r in active)
        out['grades_total'] = sum(r['grade_count'] for r in rows)
        out['activity'] = c.execute('''SELECT a.action,a.details,a.created_at FROM audit a
            WHERE a.actor_id=%s AND (a.action LIKE 'grade.%%' OR a.action LIKE 'assessment.%%')
            ORDER BY a.id DESC LIMIT 8''', (u['id'],)).fetchall()
        out['reviews'] = c.execute('''SELECT a.id,a.title,a.status,a.review_note,a.reviewed_at,a.offering_id,sub.code
            FROM assessments a JOIN course_offerings o ON o.id=a.offering_id JOIN subjects sub ON sub.id=o.subject_id
            WHERE o.teacher_id=%s AND a.reviewed_at IS NOT NULL ORDER BY a.reviewed_at DESC LIMIT 5''', (u['id'],)).fetchall()
    elif u['role'] == 'student':
        out['group'] = current_group_row(c, u['id'])
        out['courses'] = student_courses(c, u['id'])
        out['grades'] = sorted(student_grades(c, u['id']), key=lambda g: g['updated_at'] or 0, reverse=True)
    elif u['role'] == 'parent':
        kids = c.execute('''SELECT u.id,u.full_name,u.email FROM parent_links l JOIN users u ON u.id=l.student_id
            WHERE l.parent_id=%s AND u.role='student' ORDER BY u.full_name''', (u['id'],)).fetchall()
        children = []
        for k in kids:
            grades = sorted(student_grades(c, k['id']), key=lambda g: g['updated_at'] or 0, reverse=True)
            children.append({'id': k['id'], 'full_name': k['full_name'], 'email': k['email'],
                             'group': current_group_row(c, k['id']), 'courses': student_courses(c, k['id']),
                             'grades': grades[:4], 'grade_count': len(grades)})
        out['children'] = children
    return out


def install(app, db, view, require, guard, now, audit, go):
    def admin(req):
        return require(req, 'admin')

    def do_audit(c, u, action, target, details=''):
        if details:
            audit(c, u['id'], action, target, details)
        else:
            audit(c, u['id'], action, target)

    def notice(req, text):
        req.session['academic_notice'] = text

    def load_offering(c, oid, lock=False):
        o = c.execute(OFFERING_SQL + (' FOR UPDATE OF o' if lock else ''), (oid,)).fetchone()
        if not o:
            raise HTTPException(404, 'Course not found')
        return o

    def teacher_offering(c, oid, uid, lock=False):
        o = load_offering(c, oid, lock)
        if o['teacher_id'] != uid:
            raise HTTPException(403, 'Only the teacher currently assigned to this course can do this.')
        return o

    def ensure_writable(o, back):
        reason = writable_reason(o)
        if reason:
            raise FormError(409, reason, back)

    def roster(c, group_id):
        if not group_id:
            return []
        return c.execute('''SELECT u.id,u.email,u.full_name FROM group_memberships m
           JOIN users u ON u.id=m.student_id WHERE m.group_id=%s AND m.until_at IS NULL
           AND u.role='student' AND NOT u.blocked ORDER BY u.full_name,u.id''', (group_id,)).fetchall()

    def on_roster(c, group_id, student_id):
        return bool(group_id) and c.execute('''SELECT 1 FROM group_memberships m JOIN users u ON u.id=m.student_id
            WHERE m.group_id=%s AND m.student_id=%s AND m.until_at IS NULL AND u.role='student' AND NOT u.blocked''',
                                              (group_id, student_id)).fetchone() is not None

    def set_membership(c, student_id, group_id, timestamp):
        """Add or transfer a student. Returns 'added', 'transferred' or 'unchanged'."""
        st = c.execute("SELECT id FROM users WHERE id=%s AND role='student' AND NOT blocked", (student_id,)).fetchone()
        grp = c.execute('SELECT group_code FROM academic_groups WHERE id=%s AND deleted_at IS NULL', (group_id,)).fetchone()
        if not st or not grp:
            raise FormError(400, 'Choose an active student and an existing group.')
        cur = c.execute('SELECT id,group_id FROM group_memberships WHERE student_id=%s AND until_at IS NULL FOR UPDATE',
                        (student_id,)).fetchone()
        if cur and cur['group_id'] == group_id:
            return 'unchanged'
        if cur:
            c.execute('UPDATE group_memberships SET until_at=%s WHERE id=%s', (timestamp, cur['id']))
        c.execute('INSERT INTO group_memberships(student_id,group_id,group_code_snapshot,since_at) VALUES(%s,%s,%s,%s)',
                  (student_id, group_id, grp['group_code'], timestamp))
        return 'transferred' if cur else 'added'

    def current_semester(c):
        return c.execute('SELECT * FROM semesters WHERE is_current LIMIT 1').fetchone()

    def back_url(path, **params):
        params = {k: v for k, v in params.items() if v}
        return path + ('?' + urlencode(params) if params else '')

    # ----------------------------------------------------------------- home
    @app.get('/academic')
    def academic_home(req: Request):
        u = require(req)
        if u['role'] == 'admin':
            return go('/academic/admin')
        with db() as c:
            sem = current_semester(c)
            ctx = {'semester': sem, 'rows': [], 'children': [], 'group': None, 'past': [], 'pending_request': None}
            if u['role'] == 'teacher':
                rows = teacher_courses(c, u['id'])
                ctx['rows'] = [r for r in rows if r['status'] == 'active' and r['semester_status'] != 'closed']
                ctx['past'] = [r for r in rows if not (r['status'] == 'active' and r['semester_status'] != 'closed')]
            elif u['role'] == 'student':
                ctx['group'] = c.execute('''SELECT g.id,g.group_code FROM group_memberships m JOIN academic_groups g ON g.id=m.group_id
                    WHERE m.student_id=%s AND m.until_at IS NULL''', (u['id'],)).fetchone()
                ctx['pending_request'] = c.execute('''SELECT r.id,g.group_code FROM group_join_requests r JOIN academic_groups g ON g.id=r.group_id
                    WHERE r.student_id=%s AND r.status='pending' ''', (u['id'],)).fetchone()
                ctx['rows'] = student_courses(c, u['id'])
                ctx['grades'] = student_grades(c, u['id'])
            elif u['role'] == 'parent':
                ctx['children'] = c.execute('''SELECT child.id,child.full_name,child.email,
                    (SELECT g.group_code FROM group_memberships m JOIN academic_groups g ON g.id=m.group_id
                       WHERE m.student_id=child.id AND m.until_at IS NULL) group_code
                    FROM parent_links l JOIN users child ON child.id=l.student_id
                    WHERE l.parent_id=%s AND child.role='student' ORDER BY child.full_name''', (u['id'],)).fetchall()
            else:
                raise HTTPException(403)
        return view(req, 'academic_home', heading='My Courses', **ctx)

    def c2_revisions(student_id):
        with db() as c:
            return c.execute('''SELECT r.*,u.full_name editor_name,a.title,a.max_score,sub.code,sub.name,g.id grade_id
                FROM grade_revisions r JOIN grades g ON g.id=r.grade_id JOIN assessments a ON a.id=g.assessment_id
                JOIN course_offerings o ON o.id=a.offering_id JOIN subjects sub ON sub.id=o.subject_id
                LEFT JOIN users u ON u.id=r.editor_id WHERE g.student_id=%s ORDER BY r.edited_at DESC, r.id DESC''',
                             (student_id,)).fetchall()

    # --------------------------------------------------------------- parent
    @app.get('/academic/parent/{student_id}')
    def parent_student_academics(req: Request, student_id: int, tab: str = 'courses'):
        parent = require(req, 'parent')
        with db() as c:
            child = c.execute('''SELECT u.id,u.full_name,u.email FROM parent_links l
                JOIN users u ON u.id=l.student_id
                WHERE l.parent_id=%s AND l.student_id=%s AND u.role='student' ''',
                              (parent['id'], student_id)).fetchone()
            if not child:
                # Do not reveal another student's existence or academic records.
                raise HTTPException(404, 'Linked student not found')
            group = c.execute('''SELECT g.group_code FROM group_memberships m JOIN academic_groups g ON g.id=m.group_id
                WHERE m.student_id=%s AND m.until_at IS NULL''', (student_id,)).fetchone()
            courses = student_courses(c, student_id)
            details = student_course_detail(c, student_id, [o['id'] for o in courses])
            grades = student_grades(c, student_id)
            sem = current_semester(c)
        revisions = []
        if grades:
            revisions = c2_revisions(student_id)
        return view(req, 'academic_parent', heading=child['full_name'], child=child, group=group, courses=courses,
                    details=details, grades=grades, semester=sem, revisions=revisions,
                    tab=tab if tab in ('courses', 'grades', 'history') else 'courses')

    # ---------------------------------------------------------------- admin
    @app.get('/academic/setup')
    def retired_setup(req: Request):
        admin(req)
        return go('/academic/admin')

    @app.get('/academic/admin')
    def academic_admin(req: Request):
        admin(req)
        with db() as c:
            one = lambda q: c.execute(q).fetchone()['n']
            counts = {
                'subjects': one('SELECT COUNT(*) AS n FROM subjects WHERE archived_at IS NULL'),
                'offerings': one("SELECT COUNT(*) AS n FROM course_offerings WHERE status='active'"),
                'groups': one('SELECT COUNT(*) AS n FROM academic_groups WHERE deleted_at IS NULL'),
                'students': one('''SELECT COUNT(*) AS n FROM group_memberships m JOIN academic_groups g ON g.id=m.group_id
                                  WHERE m.until_at IS NULL AND g.deleted_at IS NULL'''),
                'unassigned': one('''SELECT COUNT(*) AS n FROM users u WHERE u.role='student' AND NOT u.blocked AND NOT EXISTS
                                  (SELECT 1 FROM group_memberships m WHERE m.student_id=u.id AND m.until_at IS NULL)'''),
                'students_total': one("SELECT COUNT(*) AS n FROM users WHERE role='student' AND NOT blocked"),
                'semesters': one('SELECT COUNT(*) AS n FROM semesters'),
                'teachers': one("SELECT COUNT(*) AS n FROM users WHERE role='teacher' AND verified AND teacher_status='approved' AND NOT blocked"),
                'pending_teachers': one("SELECT COUNT(*) AS n FROM users WHERE role='teacher' AND verified AND teacher_status='pending_approval' AND NOT blocked"),
                'pending_requests': one("SELECT COUNT(*) AS n FROM group_join_requests WHERE status='pending'"),
                'pending_assessments': one("SELECT COUNT(*) AS n FROM assessments WHERE status='pending'"),
            }
            sem = current_semester(c)
            offerings = c.execute('''SELECT o.id,sub.code,sub.name,s.label,s.status semester_status,o.group_code_snapshot,
                t.full_name teacher_name,
                (SELECT COUNT(*) FROM group_memberships m WHERE m.group_id=o.group_id AND m.until_at IS NULL) students,
                (SELECT COUNT(*) FROM assessments a WHERE a.offering_id=o.id AND a.status='approved') assessments
                FROM course_offerings o JOIN subjects sub ON sub.id=o.subject_id
                JOIN semesters s ON s.id=o.semester_id LEFT JOIN users t ON t.id=o.teacher_id
                WHERE o.status='active' ORDER BY o.id DESC LIMIT 8''').fetchall()
            events = c.execute('''SELECT a.*,u.full_name actor FROM audit a LEFT JOIN users u ON u.id=a.actor_id
                ORDER BY a.id DESC LIMIT 10''').fetchall()
        return view(req, 'academic_admin', heading='Overview', section='overview', counts=counts, semester=sem,
                    offerings=offerings, events=events)

    @app.get('/academic/courses')
    def courses(req: Request, q: str = '', semester: int = 0, group: int = 0, teacher: int = 0, status: str = '',
                sq: str = '', sstatus: str = 'active', tab: str = 'courses'):
        admin(req)
        with db() as c:
            subjects_all = c.execute('SELECT * FROM subjects ORDER BY code').fetchall()
            subjects = [s for s in subjects_all
                        if (not sq or sq.lower() in s['code'].lower() or sq.lower() in s['name'].lower())
                        and (sstatus == 'all' or (sstatus == 'archived') == (s['archived_at'] is not None))]
            groups = c.execute('SELECT * FROM academic_groups WHERE deleted_at IS NULL ORDER BY group_code').fetchall()
            semesters = c.execute('SELECT * FROM semesters ORDER BY start_date DESC, id DESC').fetchall()
            teachers = c.execute("""SELECT id,full_name,email FROM users WHERE role='teacher' AND verified
                AND teacher_status='approved' AND NOT blocked ORDER BY full_name""").fetchall()
            like = '%' + q + '%'
            rows = c.execute('''SELECT o.id,o.status,sub.code,sub.name,sub.archived_at,s.label,s.status semester_status,
                    o.group_code_snapshot,g.deleted_at group_deleted_at,t.full_name teacher_name,t.id teacher_id,
                    (t.teacher_status='approved' AND NOT t.blocked) teacher_ok,o.semester_id,o.group_id,
                    (SELECT COUNT(*) FROM group_memberships m WHERE m.group_id=o.group_id AND m.until_at IS NULL) student_count,
                    (SELECT COUNT(*) FROM assessments a WHERE a.offering_id=o.id) assessment_count
                    FROM course_offerings o JOIN subjects sub ON sub.id=o.subject_id
                    JOIN semesters s ON s.id=o.semester_id LEFT JOIN academic_groups g ON g.id=o.group_id
                    LEFT JOIN users t ON t.id=o.teacher_id
                    WHERE (%s='' OR sub.name ILIKE %s OR sub.code ILIKE %s OR o.group_code_snapshot ILIKE %s)
                    AND (%s=0 OR s.id=%s) AND (%s=0 OR o.group_id=%s) AND (%s=0 OR o.teacher_id=%s)
                    AND (%s='' OR (%s='archived' AND (o.status='archived' OR sub.archived_at IS NOT NULL))
                         OR (%s IN ('active','draft','closed') AND s.status=%s AND o.status='active'))
                    ORDER BY s.start_date DESC, sub.code, o.group_code_snapshot''',
                             (q, like, like, like, semester, semester, group, group, teacher, teacher,
                              status, status, status, status)).fetchall()
        missing = []
        if not [s for s in subjects_all if not s['archived_at']]:
            missing.append('subject')
        if not [s for s in semesters if s['status'] != 'closed']:
            missing.append('open semester')
        if not groups:
            missing.append('group')
        if not teachers:
            missing.append('approved teacher')
        return view(req, 'academic_courses', heading='Courses', section='courses', tab=tab if tab in ('courses', 'subjects', 'archived') else 'courses',
                    subjects=subjects, subjects_all=subjects_all, groups=groups, semesters=semesters,
                    teachers=teachers, rows=rows, missing=missing,
                    f={'q': q, 'semester': semester, 'group': group, 'teacher': teacher, 'status': status,
                       'sq': sq, 'sstatus': sstatus})

    @app.post('/academic/subject')
    def save_subject(req: Request, name: str = Form(''), code: str = Form(''), description: str = Form(''),
                     subject_id: int = Form(0), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        name = name.strip()
        code = normalize_subject_code(code)
        description = description.strip()
        error = subject_form_error(name, code, description)
        draft = {'name': name[:120], 'code': code[:32], 'description': description[:2000], 'id': subject_id}
        if error:
            req.session['academic_subject_draft'] = draft
            raise FormError(400, error, '/academic/courses')
        try:
            with db() as c:
                duplicate = c.execute('SELECT id FROM subjects WHERE code=%s AND id<>%s', (code, subject_id)).fetchone()
                if duplicate:
                    req.session['academic_subject_draft'] = draft
                    raise FormError(409, 'Subject code %s already exists. Choose a different code or edit the existing subject.' % code,
                                    '/academic/courses')
                if subject_id:
                    row = c.execute('UPDATE subjects SET name=%s,code=%s,description=%s WHERE id=%s RETURNING id',
                                    (name, code, description, subject_id)).fetchone()
                    if not row:
                        raise FormError(404, 'Subject no longer exists.', '/academic/courses')
                    sid = subject_id
                else:
                    row = c.execute('INSERT INTO subjects(code,name,description) VALUES(%s,%s,%s) RETURNING id',
                                    (code, name, description)).fetchone()
                    sid = row['id']
                do_audit(c, u, 'subject.updated' if subject_id else 'subject.created', sid, code)
        except UniqueViolation:
            req.session['academic_subject_draft'] = draft
            raise FormError(409, 'Subject code %s is already in use. Please choose another code.' % code, '/academic/courses')
        req.session.pop('academic_subject_draft', None)
        notice(req, 'Subject %s saved.' % code)
        return go('/academic/courses')

    @app.post('/academic/subject/archive')
    def archive_subject(req: Request, subject_id: int = Form(...), restore: str = Form('false'), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        restore_flag = restore.lower() in ('1', 'true', 'on', 'yes')
        with db() as c:
            row = c.execute('UPDATE subjects SET archived_at=%s WHERE id=%s RETURNING id,code',
                            (None if restore_flag else now(), subject_id)).fetchone()
            if not row:
                raise FormError(404, 'Subject not found.', '/academic/courses')
            do_audit(c, u, 'subject.restored' if restore_flag else 'subject.archived', subject_id, row['code'])
        notice(req, 'Subject %s %s.' % (row['code'], 'restored' if restore_flag else 'archived; its grades stay in history'))
        return go('/academic/courses')

    @app.post('/academic/offering')
    def assign_course(req: Request, subject_id: int = Form(0), semester_id: int = Form(0),
                      group_id: List[int] = Form([]), teacher_id: List[int] = Form([]), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        back = '/academic/courses'
        if not subject_id or not semester_id or not group_id or not teacher_id:
            raise FormError(400, 'Select a subject, semester, group and teacher.', back)
        if len(group_id) != len(teacher_id) or len(set(group_id)) != len(group_id) or len(group_id) > 100:
            raise FormError(400, 'Specify one distinct group and one teacher per assignment.', back)
        created = []
        try:
            with db() as c:
                subject = c.execute('SELECT id,code,name FROM subjects WHERE id=%s AND archived_at IS NULL', (subject_id,)).fetchone()
                if not subject:
                    raise FormError(400, 'Choose an active (not archived) subject.', back)
                sem = c.execute('SELECT id,label,status FROM semesters WHERE id=%s', (semester_id,)).fetchone()
                if not sem:
                    raise FormError(400, 'Choose a semester.', back)
                if sem['status'] == 'closed':
                    raise FormError(409, 'Semester %s is closed. Reopen it or choose another semester.' % sem['label'], back)
                for gid, tid in zip(group_id, teacher_id):
                    grp = c.execute('SELECT id,group_code FROM academic_groups WHERE id=%s AND deleted_at IS NULL', (gid,)).fetchone()
                    t = c.execute(APPROVED_TEACHER_SQL, (tid,)).fetchone()
                    if not grp:
                        raise FormError(400, 'Choose an existing group.', back)
                    if not t:
                        raise FormError(400, 'Choose an approved teacher.', back)
                    dup = c.execute('SELECT id,status FROM course_offerings WHERE subject_id=%s AND semester_id=%s AND group_id=%s',
                                    (subject_id, semester_id, gid)).fetchone()
                    if dup:
                        extra = ' (it is archived — restore it from the list instead)' if dup['status'] == 'archived' else ''
                        raise FormError(409, 'Course already exists: %s is already assigned to %s in %s%s.' % (
                            subject['code'], grp['group_code'], sem['label'], extra), back)
                    row = c.execute('''INSERT INTO course_offerings(subject_id,semester_id,group_id,group_code_snapshot,teacher_id,created_at)
                            VALUES(%s,%s,%s,%s,%s,%s) RETURNING id''',
                                    (subject_id, semester_id, gid, grp['group_code'], tid, now())).fetchone()
                    c.execute('''INSERT INTO teacher_assignments_history(offering_id,old_teacher_id,new_teacher_id,changed_by,changed_at,reason)
                            VALUES(%s,NULL,%s,%s,%s,'initial assignment')''', (row['id'], tid, u['id'], now()))
                    do_audit(c, u, 'offering.created', row['id'],
                             '%s / %s / %s / %s' % (subject['code'], grp['group_code'], sem['label'], t['full_name']))
                    created.append('%s → %s (%s)' % (subject['code'], grp['group_code'], t['full_name']))
        except UniqueViolation:
            raise FormError(409, 'Course already exists for this subject, group and semester.', back)
        notice(req, 'Course assigned: ' + '; '.join(created) + '.')
        return go('/academic/courses')

    @app.post('/academic/offering/teacher')
    def replace_teacher(req: Request, offering_id: int = Form(...), teacher_id: int = Form(0), reason: str = Form(''),
                        next: str = Form(''), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        back = next if next.startswith('/academic') and not next.startswith('//') else '/academic/courses'
        with db() as c:
            o = c.execute('SELECT id,teacher_id,group_code_snapshot FROM course_offerings WHERE id=%s FOR UPDATE',
                          (offering_id,)).fetchone()
            teacher = c.execute(APPROVED_TEACHER_SQL, (teacher_id,)).fetchone()
            if not o:
                raise FormError(404, 'Course not found.', back)
            if not teacher:
                raise FormError(400, 'Choose an approved teacher.', back)
            if o['teacher_id'] == teacher_id:
                raise FormError(409, '%s already teaches this course.' % teacher['full_name'], back)
            c.execute('UPDATE course_offerings SET teacher_id=%s WHERE id=%s', (teacher_id, offering_id))
            c.execute('''INSERT INTO teacher_assignments_history(offering_id,old_teacher_id,new_teacher_id,changed_by,changed_at,reason)
                VALUES(%s,%s,%s,%s,%s,%s)''', (offering_id, o['teacher_id'], teacher_id, u['id'], now(), reason.strip()[:300]))
            do_audit(c, u, 'offering.teacher_replaced', offering_id, 'new teacher: %s' % teacher['full_name'])
        notice(req, 'Teacher replaced. Existing grades are kept; the previous teacher can no longer edit them.')
        return go(back)

    @app.post('/academic/teacher/reassign')
    def reassign_all(req: Request, from_teacher_id: int = Form(0), to_teacher_id: int = Form(0), csrf_token: str = Form(...)):
        """Teacher replacement across all active, non-closed courses of one teacher."""
        guard(req, csrf_token)
        u = admin(req)
        back = '/academic/teachers'
        if not from_teacher_id or not to_teacher_id or from_teacher_id == to_teacher_id:
            raise FormError(400, 'Choose two different teachers.', back)
        with db() as c:
            teacher = c.execute(APPROVED_TEACHER_SQL, (to_teacher_id,)).fetchone()
            if not teacher:
                raise FormError(400, 'The new teacher must be approved.', back)
            rows = c.execute('''SELECT o.id FROM course_offerings o JOIN semesters s ON s.id=o.semester_id
                WHERE o.teacher_id=%s AND o.status='active' AND s.status<>'closed' FOR UPDATE OF o''', (from_teacher_id,)).fetchall()
            if not rows:
                raise FormError(409, 'That teacher has no active courses to reassign.', back)
            for r in rows:
                c.execute('UPDATE course_offerings SET teacher_id=%s WHERE id=%s', (to_teacher_id, r['id']))
                c.execute('''INSERT INTO teacher_assignments_history(offering_id,old_teacher_id,new_teacher_id,changed_by,changed_at,reason)
                    VALUES(%s,%s,%s,%s,%s,'bulk replacement')''', (r['id'], from_teacher_id, to_teacher_id, u['id'], now()))
                do_audit(c, u, 'offering.teacher_replaced', r['id'], 'new teacher: %s' % teacher['full_name'])
        notice(req, '%d course(s) reassigned to %s.' % (len(rows), teacher['full_name']))
        return go(back)

    @app.post('/academic/offering/status')
    def offering_status(req: Request, offering_id: int = Form(...), status: str = Form(...), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        if status not in ('active', 'archived'):
            raise FormError(400, 'Invalid course status.', '/academic/courses')
        with db() as c:
            row = c.execute('UPDATE course_offerings SET status=%s,archived_at=%s WHERE id=%s RETURNING id',
                            (status, now() if status == 'archived' else None, offering_id)).fetchone()
            if not row:
                raise FormError(404, 'Course not found.', '/academic/courses')
            do_audit(c, u, 'offering.' + ('archived' if status == 'archived' else 'restored'), offering_id)
        notice(req, 'Course archived. Its grades remain in history.' if status == 'archived' else 'Course restored.')
        return go('/academic/courses')

    # ------------------------------------------------------------ semesters
    @app.get('/academic/semesters')
    def semesters_page(req: Request):
        admin(req)
        with db() as c:
            rows = c.execute('''SELECT s.*,(SELECT COUNT(*) FROM course_offerings o WHERE o.semester_id=s.id) courses
                FROM semesters s ORDER BY s.start_date DESC, s.id DESC''').fetchall()
            events = c.execute('''SELECT a.*,u.full_name actor FROM audit a LEFT JOIN users u ON u.id=a.actor_id
                WHERE a.action LIKE 'semester.%%' ORDER BY a.id DESC LIMIT 10''').fetchall()
        return view(req, 'academic_semesters', heading='Semesters', section='semesters', rows=rows, events=events)

    @app.post('/academic/semester')
    def create_semester(req: Request, label: str = Form(''), start_date: str = Form(''), end_date: str = Form(''),
                        activate: str = Form(''), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        back = '/academic/semesters'
        label = label.strip()
        if not 2 <= len(label) <= 80:
            raise FormError(400, 'Semester name must have 2–80 characters.', back)
        try:
            start, end = date.fromisoformat(start_date), date.fromisoformat(end_date)
        except ValueError:
            raise FormError(400, 'Enter valid start and end dates.', back)
        if end < start:
            raise FormError(400, 'End date must be on or after the start date.', back)
        try:
            with db() as c:
                if c.execute('SELECT 1 FROM semesters WHERE lower(label)=lower(%s)', (label,)).fetchone():
                    raise FormError(409, 'Semester "%s" already exists.' % label, back)
                status = 'active' if activate else 'draft'
                row = c.execute('INSERT INTO semesters(label,start_date,end_date,status) VALUES(%s,%s,%s,%s) RETURNING id',
                                (label, start, end, status)).fetchone()
                if activate and not current_semester(c):
                    c.execute('UPDATE semesters SET is_current=TRUE WHERE id=%s', (row['id'],))
                do_audit(c, u, 'semester.created', row['id'], label)
        except UniqueViolation:
            raise FormError(409, 'Semester "%s" already exists.' % label, back)
        notice(req, 'Semester %s created%s.' % (label, ' and opened' if activate else ' as draft'))
        return go(back)

    @app.post('/academic/semester/status')
    def semester_status(req: Request, semester_id: int = Form(...), status: str = Form(...), csrf_token: str = Form(...)):
        """status: active (open/reopen), closed, current (make the current semester)."""
        guard(req, csrf_token)
        u = admin(req)
        back = '/academic/semesters'
        if status not in ('active', 'closed', 'current'):
            raise FormError(400, 'Invalid semester action.', back)
        with db() as c:
            s = c.execute('SELECT * FROM semesters WHERE id=%s FOR UPDATE', (semester_id,)).fetchone()
            if not s:
                raise FormError(404, 'Semester not found.', back)
            if status == 'current':
                if s['status'] == 'closed':
                    raise FormError(409, 'Reopen the semester before making it current.', back)
                c.execute('UPDATE semesters SET is_current=FALSE WHERE is_current AND id<>%s', (semester_id,))
                c.execute("UPDATE semesters SET is_current=TRUE,status='active' WHERE id=%s", (semester_id,))
                action, msg = 'semester.set_current', '%s is now the current semester.'
            elif status == 'closed':
                if s['status'] == 'closed':
                    raise FormError(409, 'Semester is already closed.', back)
                c.execute("UPDATE semesters SET status='closed',closed_at=%s WHERE id=%s", (now(), semester_id))
                action, msg = 'semester.closed', '%s closed. Its grades are now read-only.'
            else:
                if s['status'] == 'active':
                    raise FormError(409, 'Semester is already open.', back)
                c.execute("UPDATE semesters SET status='active',closed_at=NULL WHERE id=%s", (semester_id,))
                if not current_semester(c):
                    c.execute('UPDATE semesters SET is_current=TRUE WHERE id=%s', (semester_id,))
                action = 'semester.reopened' if s['status'] == 'closed' else 'semester.opened'
                msg = '%s reopened. Grade editing is available again.' if s['status'] == 'closed' else '%s opened for grading.'
            do_audit(c, u, action, semester_id, '%s: %s → %s' % (s['label'], s['status'], status))
        notice(req, msg % s['label'])
        return go(back)

    # --------------------------------------------------------------- groups
    @app.get('/academic/groups')
    def groups_page(req: Request, g: int = 0, tab: str = 'groups'):
        admin(req)
        with db() as c:
            groups = c.execute('''SELECT g.id,g.group_code,COUNT(m.id) members,
                (SELECT COUNT(*) FROM course_offerings o WHERE o.group_id=g.id AND o.status='active') courses
                FROM academic_groups g LEFT JOIN group_memberships m ON m.group_id=g.id AND m.until_at IS NULL
                WHERE g.deleted_at IS NULL GROUP BY g.id ORDER BY g.group_code''').fetchall()
            students = c.execute('''SELECT u.id,u.full_name,u.email,gr.group_code FROM users u
                LEFT JOIN group_memberships m ON m.student_id=u.id AND m.until_at IS NULL
                LEFT JOIN academic_groups gr ON gr.id=m.group_id
                WHERE u.role='student' AND NOT u.blocked ORDER BY gr.group_code NULLS FIRST,u.full_name''').fetchall()
            members = c.execute('''SELECT m.student_id,m.group_id,m.since_at,u.full_name,u.email FROM group_memberships m
                JOIN users u ON u.id=m.student_id WHERE m.until_at IS NULL ORDER BY u.full_name''').fetchall()
            requests = c.execute('''SELECT r.id,r.student_id,r.group_id,r.created_at,u.full_name,u.email,g.group_code,
                (SELECT cg.group_code FROM group_memberships m JOIN academic_groups cg ON cg.id=m.group_id
                   WHERE m.student_id=r.student_id AND m.until_at IS NULL) current_group
                FROM group_join_requests r JOIN users u ON u.id=r.student_id JOIN academic_groups g ON g.id=r.group_id
                WHERE r.status='pending' ORDER BY r.created_at''').fetchall()
            transfers = c.execute('''SELECT m.student_id,u.full_name,m.group_code_snapshot old_code,m.until_at,
                (SELECT n.group_code_snapshot FROM group_memberships n WHERE n.student_id=m.student_id AND n.since_at>=m.until_at
                   ORDER BY n.id LIMIT 1) new_code
                FROM group_memberships m JOIN users u ON u.id=m.student_id WHERE m.until_at IS NOT NULL
                ORDER BY m.until_at DESC LIMIT 8''').fetchall()
        selected = next((x for x in groups if x['id'] == g), None) if g else None
        return view(req, 'academic_groups', heading='Groups', section='groups', groups=groups, students=students,
                    tab=tab if tab in ('groups', 'requests', 'transfers') else 'groups',
                    members=members, requests=requests, transfers=transfers, selected=selected)

    @app.post('/academic/group')
    def create_group(req: Request, group_code: str = Form(''), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        code = re.sub(r'\s+', '-', group_code.strip().upper())
        back = '/academic/groups'
        if not re.fullmatch(r'[A-Z0-9_-]{2,32}', code):
            raise FormError(400, 'Group code must have 2–32 Latin letters, numbers, - or _ (for example SE-2401).', back)
        try:
            with db() as c:
                if c.execute('SELECT 1 FROM academic_groups WHERE group_code=%s', (code,)).fetchone():
                    raise FormError(409, 'Group %s already exists.' % code, back)
                row = c.execute('INSERT INTO academic_groups(group_code,created_at) VALUES(%s,%s) RETURNING id',
                                (code, now())).fetchone()
                do_audit(c, u, 'group.created', row['id'], code)
        except UniqueViolation:
            raise FormError(409, 'Group %s already exists.' % code, back)
        notice(req, 'Group %s created.' % code)
        return go('/academic/groups?g=%d' % row['id'])

    @app.post('/academic/group/member')
    def add_member(req: Request, student_id: int = Form(0), group_id: int = Form(0), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        back = '/academic/groups?g=%d' % group_id if group_id else '/academic/groups'
        if not student_id or not group_id:
            raise FormError(400, 'Choose a student and a group.', back)
        with db() as c:
            result = set_membership(c, student_id, group_id, now())
            if result != 'unchanged':
                do_audit(c, u, 'group.member_' + result, student_id, 'group #%d' % group_id)
            c.execute("UPDATE group_join_requests SET status='approved',reviewed_by=%s,reviewed_at=%s WHERE student_id=%s AND status='pending'",
                      (u['id'], now(), student_id))
        notice(req, {'added': 'Student added to the group.', 'transferred': 'Student transferred. Earlier grades are kept.',
                     'unchanged': 'Student is already in this group.'}[result])
        return go(back)

    @app.post('/academic/group/member/remove')
    def remove_member(req: Request, student_id: int = Form(...), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        with db() as c:
            row = c.execute('UPDATE group_memberships SET until_at=%s WHERE student_id=%s AND until_at IS NULL RETURNING group_id',
                            (now(), student_id)).fetchone()
            if not row:
                raise FormError(404, 'The student is not in a group.', '/academic/groups')
            do_audit(c, u, 'group.member_removed', student_id, 'group #%s' % row['group_id'])
        notice(req, 'Student removed from the group. Their grades are kept.')
        return go('/academic/groups?g=%s' % row['group_id'])

    @app.post('/academic/group/import')
    async def import_students(req: Request, group_id: int = Form(0), upload: UploadFile = File(None), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        back = '/academic/groups?g=%d' % group_id if group_id else '/academic/groups'
        if upload is None or not upload.filename:
            raise FormError(400, 'Choose a CSV or XLSX file.', back)
        try:
            values = read_student_emails(upload.filename, await upload.read(1024 * 1024 + 1))
        except HTTPException as e:
            raise FormError(400, e.detail, back)
        with db() as c:
            g = c.execute('SELECT id,group_code FROM academic_groups WHERE id=%s AND deleted_at IS NULL', (group_id,)).fetchone()
            if not g:
                raise FormError(404, 'Choose an existing group.', back)
            students = c.execute("SELECT id,email FROM users WHERE role='student' AND NOT blocked AND email=ANY(%s)",
                                 (values,)).fetchall()
            found = {x['email']: x['id'] for x in students}
            unknown = [v for v in values if v not in found]
            if unknown:
                raise FormError(400, 'Not registered student accounts: %s%s. No changes saved.' % (
                    ', '.join(unknown[:5]), ' …' if len(unknown) > 5 else ''), back)
            stats = {'added': 0, 'transferred': 0, 'unchanged': 0}
            for email in values:
                stats[set_membership(c, found[email], group_id, now())] += 1
            do_audit(c, u, 'group.bulk_import', group_id, '%(added)d added, %(transferred)d transferred' % stats)
        notice(req, 'Import into %s: %d added, %d transferred, %d already members.' % (
            g['group_code'], stats['added'], stats['transferred'], stats['unchanged']))
        return go(back)

    @app.post('/academic/group/delete')
    def delete_group(req: Request, group_id: int = Form(...), csrf_token: str = Form(...)):
        """Soft delete: the group disappears from active lists; history and grades are preserved."""
        guard(req, csrf_token)
        u = admin(req)
        with db() as c:
            row = c.execute('SELECT id,group_code FROM academic_groups WHERE id=%s AND deleted_at IS NULL FOR UPDATE',
                            (group_id,)).fetchone()
            if not row:
                raise FormError(404, 'Group not found.', '/academic/groups')
            t = now()
            c.execute('UPDATE group_memberships SET until_at=%s WHERE group_id=%s AND until_at IS NULL', (t, group_id))
            c.execute("UPDATE group_join_requests SET status='rejected',reviewed_by=%s,reviewed_at=%s WHERE group_id=%s AND status='pending'",
                      (u['id'], t, group_id))
            c.execute("UPDATE course_offerings SET status='archived',archived_at=%s WHERE group_id=%s AND status='active'", (t, group_id))
            # Free the code for reuse; snapshots keep the original code on memberships and courses.
            c.execute("UPDATE academic_groups SET deleted_at=%s,group_code=group_code||'~deleted-'||id WHERE id=%s", (t, group_id))
            do_audit(c, u, 'group.deleted', group_id, row['group_code'])
        notice(req, 'Group %s deleted. Its courses were archived; all grades remain in history.' % row['group_code'])
        return go('/academic/groups')

    @app.get('/academic/join')
    def join_page(req: Request):
        return my_group(req)

    @app.get('/academic/my-group')
    def my_group(req: Request):
        """Student: current group, membership history and join request (read-only except the request form)."""
        u = require(req, 'student')
        with db() as c:
            groups = c.execute('SELECT id,group_code FROM academic_groups WHERE deleted_at IS NULL ORDER BY group_code').fetchall()
            current = current_group_row(c, u['id'])
            pending = c.execute('''SELECT g.group_code,r.created_at FROM group_join_requests r JOIN academic_groups g ON g.id=r.group_id
                WHERE r.student_id=%s AND r.status='pending' ''', (u['id'],)).fetchone()
            history = c.execute('''SELECT group_code_snapshot,since_at,until_at FROM group_memberships
                WHERE student_id=%s ORDER BY since_at DESC, id DESC''', (u['id'],)).fetchall()
            requests = c.execute('''SELECT r.status,r.created_at,r.reviewed_at,g.group_code FROM group_join_requests r
                JOIN academic_groups g ON g.id=r.group_id WHERE r.student_id=%s ORDER BY r.id DESC LIMIT 10''', (u['id'],)).fetchall()
            classmates = c.execute('''SELECT COUNT(*) n FROM group_memberships m WHERE m.group_id=%s AND m.until_at IS NULL''',
                                   (current['id'] if current else 0,)).fetchone()['n']
            courses = student_courses(c, u['id'])
        return view(req, 'academic_join', heading='My Group', groups=groups, current=current, pending=pending,
                    history=history, requests=requests, classmates=classmates, courses=courses)

    @app.post('/academic/group/request')
    def join_request(req: Request, group_id: int = Form(0), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = require(req, 'student')
        back = '/academic/join'
        try:
            with db() as c:
                if c.execute('SELECT id FROM group_memberships WHERE student_id=%s AND until_at IS NULL', (u['id'],)).fetchone():
                    raise FormError(409, 'You are already in a group. Ask the administrator for a transfer.', back)
                if not c.execute('SELECT id FROM academic_groups WHERE id=%s AND deleted_at IS NULL', (group_id,)).fetchone():
                    raise FormError(404, 'Choose an existing group.', back)
                if c.execute("SELECT id FROM group_join_requests WHERE student_id=%s AND status='pending'", (u['id'],)).fetchone():
                    raise FormError(409, 'You already have a pending request.', back)
                c.execute('INSERT INTO group_join_requests(student_id,group_id,created_at) VALUES(%s,%s,%s)', (u['id'], group_id, now()))
        except UniqueViolation:
            raise FormError(409, 'You already have a pending request.', back)
        notice(req, 'Request sent. An administrator will review it.')
        return go('/academic')

    @app.post('/academic/group/requests/review')
    def review_requests(req: Request, ids: List[int] = Form([]), decision: str = Form(''), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        back = '/academic/groups'
        if decision not in ('approved', 'rejected'):
            raise FormError(400, 'Choose Approve or Reject.', back)
        if not ids or len(ids) > 500:
            raise FormError(400, 'Select at least one request.', back)
        with db() as c:
            rows = c.execute("SELECT id,student_id,group_id FROM group_join_requests WHERE id=ANY(%s) AND status='pending' FOR UPDATE",
                             (list(set(ids)),)).fetchall()
            if len(rows) != len(set(ids)):
                raise FormError(409, 'Some requests were already processed. Reload and try again.', back)
            for r in rows:
                if decision == 'approved':
                    set_membership(c, r['student_id'], r['group_id'], now())
                c.execute('UPDATE group_join_requests SET status=%s,reviewed_by=%s,reviewed_at=%s WHERE id=%s',
                          (decision, u['id'], now(), r['id']))
                do_audit(c, u, 'group.request_' + decision, r['id'])
        notice(req, '%d request(s) %s.' % (len(rows), decision))
        return go(back)

    # ------------------------------------------------------------- teachers
    @app.get('/academic/teachers')
    def teachers_page(req: Request):
        admin(req)
        with db() as c:
            rows = c.execute('''SELECT u.id,u.full_name,u.email,u.teacher_status,u.blocked,u.verified,
                COUNT(o.id) FILTER (WHERE o.status='active' AND s.status<>'closed') active_courses,
                COUNT(DISTINCT o.group_id) FILTER (WHERE o.status='active' AND s.status<>'closed') active_groups,
                COALESCE(SUM((SELECT COUNT(*) FROM group_memberships m WHERE m.group_id=o.group_id AND m.until_at IS NULL))
                   FILTER (WHERE o.status='active' AND s.status<>'closed'),0) students
                FROM users u LEFT JOIN course_offerings o ON o.teacher_id=u.id
                LEFT JOIN semesters s ON s.id=o.semester_id
                WHERE u.role='teacher' GROUP BY u.id ORDER BY (u.teacher_status='approved') DESC,u.full_name''').fetchall()
            courses = c.execute('''SELECT o.id,o.teacher_id,o.status,sub.code,sub.name,s.label,s.status semester_status,o.group_code_snapshot
                FROM course_offerings o JOIN subjects sub ON sub.id=o.subject_id JOIN semesters s ON s.id=o.semester_id
                WHERE o.status='active' ORDER BY s.start_date DESC,sub.code''').fetchall()
            history = c.execute('''SELECT h.*,sub.code,o.group_code_snapshot,ot.full_name old_name,nt.full_name new_name
                FROM teacher_assignments_history h JOIN course_offerings o ON o.id=h.offering_id
                JOIN subjects sub ON sub.id=o.subject_id LEFT JOIN users ot ON ot.id=h.old_teacher_id
                LEFT JOIN users nt ON nt.id=h.new_teacher_id WHERE h.old_teacher_id IS NOT NULL
                ORDER BY h.id DESC LIMIT 10''').fetchall()
        approved = [r for r in rows if r['teacher_status'] == 'approved' and not r['blocked'] and r['verified']]
        others = [r for r in rows if r not in approved]
        by_teacher = {}
        for o in courses:
            by_teacher.setdefault(o['teacher_id'], []).append(o)
        return view(req, 'academic_teachers', heading='Teachers', section='teachers', approved=approved, others=others,
                    by_teacher=by_teacher, history=history)

    # ---------------------------------------------------- assessment review
    @app.get('/academic/assessments')
    def assessments_page(req: Request, status: str = 'pending'):
        admin(req)
        if status not in ('pending', 'approved', 'rejected', 'draft', 'all'):
            status = 'pending'
        with db() as c:
            rows = c.execute('''SELECT a.*,sub.code,sub.name,s.label,s.status semester_status,o.group_code_snapshot,
                t.full_name teacher_name,r.full_name reviewer_name,
                (SELECT COUNT(*) FROM grades g WHERE g.assessment_id=a.id) grade_count
                FROM assessments a JOIN course_offerings o ON o.id=a.offering_id JOIN subjects sub ON sub.id=o.subject_id
                JOIN semesters s ON s.id=o.semester_id LEFT JOIN users t ON t.id=a.created_by
                LEFT JOIN users r ON r.id=a.reviewed_by
                WHERE (%s='all' OR a.status=%s) ORDER BY a.id DESC LIMIT 200''', (status, status)).fetchall()
            counts = {r['status']: r['n'] for r in c.execute('SELECT status,COUNT(*) n FROM assessments GROUP BY status').fetchall()}
        return view(req, 'academic_assessments', heading='Assessments', section='assessments', rows=rows, status=status,
                    counts=counts)

    @app.post('/academic/assessment/review')
    def review_assessment(req: Request, assessment_id: List[int] = Form([]), decision: str = Form(''), note: str = Form(''),
                          next: str = Form(''), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = admin(req)
        back = next if next.startswith('/academic') and not next.startswith('//') else '/academic/assessments'
        if decision not in ('approved', 'rejected'):
            raise FormError(400, 'Choose Approve or Reject.', back)
        if not assessment_id:
            raise FormError(400, 'Select at least one assessment.', back)
        if len(note) > 500:
            raise FormError(400, 'Review note must be at most 500 characters.', back)
        with db() as c:
            rows = c.execute("SELECT id,title FROM assessments WHERE id=ANY(%s) AND status='pending' FOR UPDATE",
                             (list(set(assessment_id)),)).fetchall()
            if len(rows) != len(set(assessment_id)):
                raise FormError(409, 'Only pending assessments can be reviewed. Reload and try again.', back)
            for a in rows:
                c.execute('UPDATE assessments SET status=%s,reviewed_by=%s,reviewed_at=%s,review_note=%s WHERE id=%s',
                          (decision, u['id'], now(), note.strip(), a['id']))
                do_audit(c, u, 'assessment.' + decision, a['id'], a['title'])
        notice(req, '%d assessment(s) %s.' % (len(rows), decision))
        return go(back)

    # -------------------------------------------------------- student grades
    @app.get('/academic/my-grade-history')
    def my_grade_history(req: Request):
        u = require(req, 'student')
        with db() as c:
            rows = student_grades(c, u['id'])
        return view(req, 'academic_mygrades', heading='My grades', rows=rows)

    # ----------------------------------------------------- course / gradebook
    @app.get('/academic/offering/{offering_id}')
    def offering_page(req: Request, offering_id: int, assessment: int = 0, tab: str = ''):
        u = require(req)
        if u['role'] not in ('admin', 'teacher', 'student'):
            raise HTTPException(403, 'Not permitted')
        with db() as c:
            o = load_offering(c, offering_id)
            if u['role'] == 'teacher' and o['teacher_id'] != u['id']:
                raise HTTPException(403, 'Only the teacher currently assigned to this course can open it.')
            if u['role'] == 'student':
                member = on_roster(c, o['group_id'], u['id']) and o['status'] == 'active'
                has_grades = c.execute('''SELECT 1 FROM grades g JOIN assessments a ON a.id=g.assessment_id
                    WHERE a.offering_id=%s AND g.student_id=%s LIMIT 1''', (offering_id, u['id'])).fetchone()
                if not member and not has_grades:
                    raise HTTPException(403, 'This course is not assigned to your group.')
                own = student_course_detail(c, u['id'], [offering_id]).get(offering_id, [])
                return view(req, 'academic_offering', heading=o['subject_name'], offering=o, own=own, staff=False,
                            readonly_reason=writable_reason(o), tab=tab if tab in ('overview', 'assessments', 'grades') else 'overview')
            students = roster(c, o['group_id'])
            assessments = c.execute('''SELECT a.*,(SELECT COUNT(*) FROM grades g WHERE g.assessment_id=a.id) grade_count,
                r.full_name reviewer_name FROM assessments a LEFT JOIN users r ON r.id=a.reviewed_by
                WHERE a.offering_id=%s ORDER BY a.id''', (offering_id,)).fetchall()
            grades = c.execute('''SELECT gr.*,u.full_name student_name,
                (SELECT COUNT(*) FROM grade_revisions r WHERE r.grade_id=gr.id) revisions
                FROM grades gr JOIN assessments a ON a.id=gr.assessment_id JOIN users u ON u.id=gr.student_id
                WHERE a.offering_id=%s''', (offering_id,)).fetchall()
            roster_ids = {s['id'] for s in students}
            former = c.execute('''SELECT DISTINCT u.id,u.full_name,u.email FROM grades gr JOIN assessments a ON a.id=gr.assessment_id
                JOIN users u ON u.id=gr.student_id WHERE a.offering_id=%s ORDER BY u.full_name''', (offering_id,)).fetchall()
            former = [s for s in former if s['id'] not in roster_ids]
            teacher_history = c.execute('''SELECT h.*,ot.full_name old_name,nt.full_name new_name FROM teacher_assignments_history h
                LEFT JOIN users ot ON ot.id=h.old_teacher_id LEFT JOIN users nt ON nt.id=h.new_teacher_id
                WHERE h.offering_id=%s ORDER BY h.id DESC''', (offering_id,)).fetchall()
        gmap = {'%s:%s' % (g['assessment_id'], g['student_id']): g for g in grades}
        approved = [a for a in assessments if a['status'] == 'approved']
        selected = next((a for a in approved if a['id'] == assessment), None)
        reason = writable_reason(o)
        is_teacher = u['role'] == 'teacher'
        tab = tab if tab in ('students', 'assessments', 'grades') else ('grades' if assessment else 'students')
        return view(req, 'academic_offering', heading=o['subject_name'], offering=o, staff=True, students=students, tab=tab,
                    former=former, assessments=assessments, approved=approved, gmap=gmap, selected=selected,
                    teacher_history=teacher_history, readonly_reason=reason,
                    can_grade=is_teacher and reason is None,
                    can_define=is_teacher and o['semester_status'] != 'closed' and o['status'] == 'active'
                    and o['archived_at'] is None)

    # ------------------------------------------------------------ assessments
    @app.post('/academic/assessment')
    def create_assessment(req: Request, offering_id: int = Form(...), kind: str = Form(''), title: str = Form(''),
                          max_score: str = Form(''), submit: str = Form('1'), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = require(req, 'teacher')
        back = '/academic/offering/%d' % offering_id
        title = title.strip()
        if kind not in ('assignment', 'exam'):
            raise FormError(400, 'Choose Assignment or Exam.', back)
        if not 1 <= len(title) <= 160:
            raise FormError(400, 'Title must have 1–160 characters.', back)
        try:
            max_val = max_score_value(max_score)
        except HTTPException as e:
            raise FormError(400, e.detail, back)
        status = 'pending' if submit == '1' else 'draft'
        with db() as c:
            o = teacher_offering(c, offering_id, u['id'])
            if o['semester_status'] == 'closed' or o['status'] != 'active' or o['archived_at'] is not None:
                raise FormError(409, writable_reason(o), back)
            a = c.execute('''INSERT INTO assessments(offering_id,kind,title,max_score,scale,status,created_by,created_at,updated_at)
                VALUES(%s,%s,%s,%s,'points',%s,%s,%s,%s) RETURNING id''',
                          (offering_id, kind, title, max_val, status, u['id'], now(), now())).fetchone()
            do_audit(c, u, 'assessment.' + ('submitted' if status == 'pending' else 'drafted'), a['id'], title)
        notice(req, 'Assessment "%s" %s.' % (title, 'sent for administrator approval' if status == 'pending' else 'saved as draft'))
        return go(back)

    @app.post('/academic/assessment/update')
    def update_assessment(req: Request, assessment_id: int = Form(...), kind: str = Form(''), title: str = Form(''),
                          max_score: str = Form(''), submit: str = Form('0'), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = require(req, 'teacher')
        with db() as c:
            a = c.execute('SELECT * FROM assessments WHERE id=%s FOR UPDATE', (assessment_id,)).fetchone()
            if not a:
                raise HTTPException(404, 'Assessment not found')
            back = '/academic/offering/%d' % a['offering_id']
            o = teacher_offering(c, a['offering_id'], u['id'])
            if a['status'] == 'approved':
                raise FormError(409, 'Approved assessments cannot be changed.', back)
            if o['semester_status'] == 'closed' or o['status'] != 'active':
                raise FormError(409, writable_reason(o), back)
            title = title.strip() or a['title']
            kind = kind or a['kind']
            if kind not in ('assignment', 'exam') or len(title) > 160:
                raise FormError(400, 'Choose Assignment or Exam and a title up to 160 characters.', back)
            try:
                max_val = max_score_value(max_score) if max_score.strip() else a['max_score']
            except HTTPException as e:
                raise FormError(400, e.detail, back)
            status = 'pending' if submit == '1' else 'draft'
            c.execute('''UPDATE assessments SET kind=%s,title=%s,max_score=%s,status=%s,updated_at=%s,
                reviewed_by=NULL,reviewed_at=NULL WHERE id=%s''', (kind, title, max_val, status, now(), assessment_id))
            do_audit(c, u, 'assessment.' + ('submitted' if status == 'pending' else 'updated'), assessment_id, title)
        notice(req, 'Assessment %s.' % ('sent for approval' if status == 'pending' else 'saved as draft'))
        return go(back)

    # ------------------------------------------------------------------ grades
    def grade_context(c, assessment_id, uid, back_default='/academic'):
        a = c.execute('SELECT * FROM assessments WHERE id=%s', (assessment_id,)).fetchone()
        if not a:
            raise FormError(404, 'Assessment not found.', back_default)
        back = back_url('/academic/offering/%d' % a['offering_id'], assessment=a['id'])
        o = teacher_offering(c, a['offering_id'], uid, lock=True)
        ensure_writable(o, back)
        if a['status'] != 'approved':
            raise FormError(409, 'This assessment is not approved yet. Grades can be entered after administrator approval.', back)
        return a, o, back

    def insert_grade(c, a, student_id, value, uid):
        row = c.execute('''INSERT INTO grades(assessment_id,student_id,score,created_by,updated_by,created_at,updated_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT (assessment_id,student_id) DO NOTHING RETURNING id''',
                        (a['id'], student_id, value, uid, uid, now(), now())).fetchone()
        return row

    @app.post('/academic/grades')
    def enter_grades(req: Request, assessment_id: int = Form(...), student_id: List[int] = Form([]),
                     score: List[str] = Form([]), csrf_token: str = Form(...)):
        """Batch entry for one assessment. All-or-nothing: any invalid value saves nothing."""
        guard(req, csrf_token)
        u = require(req, 'teacher')
        with db() as c:
            a, o, back = grade_context(c, assessment_id, u['id'])
            if len(student_id) != len(score):
                raise FormError(400, 'Form is incomplete. Reload the page and try again.', back)
            names = {s['id']: s['full_name'] for s in roster(c, o['group_id'])}
            entries, errors = [], []
            for sid, raw in zip(student_id, score):
                if not str(raw).strip():
                    continue
                if sid not in names:
                    raise HTTPException(403, 'Student is not on this course roster.')
                try:
                    entries.append((sid, score_value(raw, a['max_score'])))
                except HTTPException:
                    errors.append('%s: "%s"' % (names[sid], raw))
            if errors:
                raise FormError(400, 'Not saved. Scores must be numbers from 0 to %s — check %s.' % (
                    fmt_num(a['max_score']), '; '.join(errors)), back)
            if not entries:
                raise FormError(400, 'Enter at least one score.', back)
            for sid, value in entries:
                row = insert_grade(c, a, sid, value, u['id'])
                if not row:
                    raise FormError(409, '%s already has a grade for this assessment. Use Edit instead. Nothing was saved.' % names[sid], back)
                do_audit(c, u, 'grade.created', row['id'], '%s: %s' % (a['title'], value))
        notice(req, '%d grade(s) saved.' % len(entries))
        return go(back)

    @app.post('/academic/grade')
    def enter_grade(req: Request, assessment_id: int = Form(...), student_id: int = Form(...), score: str = Form(''),
                    csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = require(req, 'teacher')
        with db() as c:
            a, o, back = grade_context(c, assessment_id, u['id'])
            if not on_roster(c, o['group_id'], student_id):
                raise HTTPException(403, 'Student is not on this course roster.')
            try:
                value = score_value(score, a['max_score'])
            except HTTPException as e:
                raise FormError(400, e.detail, back)
            row = insert_grade(c, a, student_id, value, u['id'])
            if not row:
                raise FormError(409, 'Grade already exists. Use Edit Grade.', back)
            do_audit(c, u, 'grade.created', row['id'], '%s: %s' % (a['title'], value))
        notice(req, 'Grade saved.')
        return go(back)

    @app.post('/academic/grade/edit')
    def edit_grade(req: Request, grade_id: int = Form(...), score: str = Form(''), version: int = Form(0),
                   reason: str = Form(''), csrf_token: str = Form(...)):
        guard(req, csrf_token)
        u = require(req, 'teacher')
        with db() as c:
            grade = c.execute('''SELECT gr.*,a.offering_id,a.max_score,a.title FROM grades gr
                    JOIN assessments a ON a.id=gr.assessment_id WHERE gr.id=%s FOR UPDATE OF gr''', (grade_id,)).fetchone()
            if not grade:
                raise HTTPException(404, 'Grade not found')
            back = back_url('/academic/offering/%d' % grade['offering_id'], assessment=grade['assessment_id'])
            o = teacher_offering(c, grade['offering_id'], u['id'], lock=True)
            ensure_writable(o, back)
            if len(reason) > 500:
                raise FormError(400, 'Reason must be at most 500 characters.', back)
            if grade['version'] != version:
                raise FormError(409, 'This grade was changed by someone else (now %s). Review it and try again.' %
                                fmt_num(grade['score']), back)
            try:
                value = score_value(score, grade['max_score'])
            except HTTPException as e:
                raise FormError(400, e.detail, back)
            if value == grade['score']:
                notice(req, 'Score unchanged.')
                return go(back)
            updated = c.execute('''UPDATE grades SET score=%s,version=version+1,updated_at=%s,updated_by=%s
                WHERE id=%s AND version=%s RETURNING id''', (value, now(), u['id'], grade_id, version)).fetchone()
            if not updated:
                raise FormError(409, 'This grade was changed by someone else. Reload and try again.', back)
            c.execute('INSERT INTO grade_revisions(grade_id,old_score,new_score,editor_id,edited_at,reason) VALUES(%s,%s,%s,%s,%s,%s)',
                      (grade_id, grade['score'], value, u['id'], now(), reason.strip()))
            do_audit(c, u, 'grade.edited', grade_id, '%s → %s' % (fmt_num(grade['score']), fmt_num(value)))
        notice(req, 'Grade updated from %s to %s.' % (fmt_num(grade['score']), fmt_num(value)))
        return go(back)

    @app.get('/academic/grade/{grade_id}/history')
    def grade_history(req: Request, grade_id: int):
        u = require(req)
        with db() as c:
            g = c.execute('''SELECT gr.*,a.title,a.kind,a.max_score,a.offering_id,o.teacher_id,sub.code,sub.name,s.label,
                  st.full_name student_name,cr.full_name creator_name FROM grades gr
                  JOIN assessments a ON a.id=gr.assessment_id JOIN course_offerings o ON o.id=a.offering_id
                  JOIN subjects sub ON sub.id=o.subject_id JOIN semesters s ON s.id=o.semester_id
                  JOIN users st ON st.id=gr.student_id LEFT JOIN users cr ON cr.id=gr.created_by WHERE gr.id=%s''',
                          (grade_id,)).fetchone()
            if not g:
                raise HTTPException(404, 'Grade not found')
            if u['role'] == 'teacher' and g['teacher_id'] != u['id']:
                raise HTTPException(403, 'Not permitted')
            if u['role'] == 'student' and g['student_id'] != u['id']:
                raise HTTPException(403, 'Not permitted')
            if u['role'] == 'parent':
                linked = c.execute('SELECT 1 FROM parent_links WHERE parent_id=%s AND student_id=%s',
                                   (u['id'], g['student_id'])).fetchone()
                if not linked:
                    raise HTTPException(404, 'Linked grade not found')
            if u['role'] not in ('admin', 'student', 'teacher', 'parent'):
                raise HTTPException(403)
            rows = c.execute('''SELECT r.*,u.full_name editor_name FROM grade_revisions r LEFT JOIN users u ON u.id=r.editor_id
                    WHERE grade_id=%s ORDER BY r.id DESC''', (grade_id,)).fetchall()
        back = ('/academic/parent/%d' % g['student_id'] if u['role'] == 'parent'
                else '/academic/offering/%d' % g['offering_id'])
        return view(req, 'academic_history', heading='Grade history', rows=rows, grade=g, grade_id=grade_id, back=back)

    # --------------------------------------------------- teacher read-only pages
    @app.get('/academic/teacher/assessments')
    def teacher_assessments(req: Request, status: str = 'all'):
        u = require(req, 'teacher')
        if status not in ('all', 'draft', 'pending', 'approved', 'rejected'):
            status = 'all'
        with db() as c:
            rows = c.execute('''SELECT a.*,sub.code,sub.name,o.group_code_snapshot,s.label,s.status semester_status,o.status offering_status,
                (SELECT COUNT(*) FROM grades g WHERE g.assessment_id=a.id) grade_count
                FROM assessments a JOIN course_offerings o ON o.id=a.offering_id JOIN subjects sub ON sub.id=o.subject_id
                JOIN semesters s ON s.id=o.semester_id WHERE o.teacher_id=%s ORDER BY a.id DESC''', (u['id'],)).fetchall()
            courses = [o for o in teacher_courses(c, u['id']) if o['status'] == 'active' and o['semester_status'] != 'closed']
        counts = {}
        for r in rows:
            counts[r['status']] = counts.get(r['status'], 0) + 1
        return view(req, 'teacher_assessments', heading='Assessments', status=status, counts=counts, total=len(rows),
                    rows=[r for r in rows if status == 'all' or r['status'] == status], courses=courses)

    @app.get('/academic/teacher/gradebook')
    def teacher_gradebook(req: Request):
        u = require(req, 'teacher')
        with db() as c:
            courses = teacher_courses(c, u['id'])
            recent = c.execute('''SELECT g.id,g.score,g.updated_at,g.version,a.title,a.max_score,a.id assessment_id,
                st.full_name student_name,sub.code,o.id offering_id,o.group_code_snapshot
                FROM grades g JOIN assessments a ON a.id=g.assessment_id JOIN course_offerings o ON o.id=a.offering_id
                JOIN subjects sub ON sub.id=o.subject_id JOIN users st ON st.id=g.student_id
                WHERE o.teacher_id=%s ORDER BY g.updated_at DESC, g.id DESC LIMIT 15''', (u['id'],)).fetchall()
        return view(req, 'teacher_gradebook', heading='Gradebook', courses=courses, recent=recent)
