"""Fast QA smoke suite, requires no database. See QA_MANUAL.md for PostgreSQL E2E."""
import io
import zipfile
import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
import sys
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import test_basic  # installs a psycopg fallback when the optional driver is absent
import app
from academic import MIGRATION, score_value, read_student_emails

@pytest.mark.parametrize('input_value,limit,expected',[
    ('0','100','0'),('100','100','100'),('82.75','100','82.75')])
def test_valid_scores(input_value,limit,expected):
    assert str(score_value(input_value,limit))==expected

@pytest.mark.parametrize('value', ['-1','101','NaN','Infinity','abc','22.333'])
def test_invalid_scores(value):
    with pytest.raises(HTTPException) as exc:score_value(value,'100')
    assert exc.value.status_code==400

def test_csv_import_validation():
    assert read_student_emails('students.csv',b'email\nA@example.org\nb@example.org\n')==['a@example.org','b@example.org']
    with pytest.raises(HTTPException):read_student_emails('students.csv',b'a@example.org\na@example.org\n')
    with pytest.raises(HTTPException):read_student_emails('students.txt',b'a@example.org')

def test_xlsx_import_parser():
    sheet='''<?xml version="1.0"?><worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>email</t></is></c></row><row r="2"><c r="A2" t="inlineStr"><is><t>alice@example.com</t></is></c></row></sheetData></worksheet>'''
    buffer=io.BytesIO()
    with zipfile.ZipFile(buffer,'w') as z:z.writestr('xl/worksheets/sheet1.xml',sheet)
    assert read_student_emails('students.xlsx',buffer.getvalue())==['alice@example.com']

def test_academic_routes_require_login():
    client=TestClient(app.app)
    for path in ['/academic','/academic/courses','/academic/groups','/academic/semesters',
                 '/academic/teachers','/academic/offering/1','/academic/my-grade-history']:
        assert client.get(path).status_code==403,path
    assert client.post('/academic/subject',data={'name':'Math','code':'MATH101','csrf_token':'bad'}).status_code==403

def test_schema_preserves_history_and_unique_constraints():
    migration='\n'.join(MIGRATION).lower()
    assert 'on delete set null' in migration
    assert 'unique(subject_id, semester_id, group_id)' in migration
    assert 'unique(assessment_id,student_id)' in migration
    assert 'where until_at is null' in migration


def test_parent_portal_templates_and_routes():
    assert '/academic/parent/{student_id}' in Path(app.ROOT/'academic.py').read_text()
    assert 'parent_links' in Path(app.ROOT/'academic.py').read_text()
    for name in ['academic_parent.html','academic_home.html','dashboard.html']:
        app.tpl.env.get_template(name)
    assert 'href="/academic/parent/{{l.id}}"' in (app.ROOT/'templates/dashboard.html').read_text()
    client=TestClient(app.app)
    assert client.get('/academic/parent/1').status_code==403


def test_parent_access_is_checked_before_grade_queries():
    """Authorization must happen before any child records can be retrieved."""
    import contextlib
    from fastapi import FastAPI
    from academic import install
    queries = []
    class Cursor:
        def execute(self, query, params=None):
            queries.append((query,params))
            return self
        def fetchone(self):
            return None
        def fetchall(self):
            raise AssertionError('Grades must not be read for an unlinked student')
    @contextlib.contextmanager
    def fake_db():
        yield Cursor()
    def fake_require(req,role=None):
        if role and role!='parent':
            raise HTTPException(403)
        return {'id':17,'role':'parent'}
    fake_app = FastAPI()
    install(fake_app,fake_db,lambda *args,**kwargs:None,fake_require,lambda *args:None,
            lambda:0,lambda *args:None,lambda path:path)
    with pytest.raises(HTTPException) as ex:
        next(route.endpoint for route in fake_app.routes
             if getattr(route,'path',None)=='/academic/parent/{student_id}')(object(),123)
    assert ex.value.status_code == 404
    assert len(queries)==1
    assert 'parent_links' in queries[0][0]
    assert queries[0][1] == (17,123)


def test_subject_code_normalization_and_field_specific_feedback():
    from academic import normalize_subject_code, subject_form_error
    assert normalize_subject_code('  math  102 ') == 'MATH-102'
    assert normalize_subject_code('cs+101') == 'CS+101'
    assert subject_form_error('Calculus', 'MATH-102', '') is None
    assert 'name' in subject_form_error('A','CS102','').lower()
    assert 'code' in subject_form_error('Calculus', 'МАТ101','').lower()
    assert 'description' in subject_form_error('Calculus', 'CS102','x'*2001).lower()


def test_subject_create_and_duplicate_are_reported_without_writing():
    from academic import install
    from fastapi import FastAPI
    import contextlib
    from types import SimpleNamespace
    queries=[]
    record={'id':1,'code':'MATH101'}
    class Cursor:
        def execute(self,sql,args=None):
            queries.append((sql,args))
            self.sql=sql
            self.args=args
            return self
        def fetchone(self):
            if 'SELECT id FROM subjects WHERE code=' in self.sql:
                return record if record and self.args[0]==record['code'] else None
            if 'INSERT INTO subjects' in self.sql:return {'id':2}
            return None
    @contextlib.contextmanager
    def fake_db():yield Cursor()
    f=FastAPI()
    install(f,fake_db,lambda *a,**kw:None,
            lambda req,role=None:{'id':4,'role':'admin'},
            lambda req,token:None,lambda:1234,lambda *a:None,lambda path:path)
    endpoint=next(r.endpoint for r in f.routes if getattr(r,'path',None)=='/academic/subject')
    request=SimpleNamespace(session={})
    with pytest.raises(HTTPException) as exc:
        endpoint(request,'Data Structures','MATH101','',0,'ok')
    assert exc.value.status_code==409
    assert 'already exists' in exc.value.detail
    assert not any('INSERT INTO subjects' in q for q,_ in queries)
    record=None
    request.session.clear();queries.clear()
    assert endpoint(request,'Data Structures','cs 102','',0,'ok')=='/academic/courses'
    assert ('CS-102','Data Structures','') in [p for q,p in queries if 'INSERT INTO subjects' in q]
    assert request.session['academic_notice']


def test_form_error_returns_to_courses_instead_of_json():
    from fastapi.testclient import TestClient
    client=TestClient(app.app)
    # Existing route protection remains intact: bad CSRF never becomes a friendly redirect.
    r=client.post('/academic/subject',data={'name':'x','code':'x','csrf_token':'bad'},headers={'referer':'http://127.0.0.1:8000/academic/courses'},follow_redirects=False)
    assert r.status_code==403
    # Explicit 400 raised from an academic form gets a redirect and stores flash feedback.
    from starlette.requests import Request
    import asyncio
    from fastapi import HTTPException
    async def receive():return {'type':'http.request','body':b'','more_body':False}
    scope={'type':'http','method':'POST','path':'/academic/subject','scheme':'http','server':('testserver',80),'client':('testclient',1),'headers':[(b'referer',b'http://testserver/academic/courses')],'query_string':b'', 'http_version':'1.1'}
    req=Request(scope,receive)
    req.scope['session']={}
    result=asyncio.run(app.friendly_academic_error(req,HTTPException(400,'Invalid course code')))
    assert result.status_code==303
    assert result.headers['location']=='/academic/courses'
    assert req.session['academic_error']=='Invalid course code'


def test_sql_migration_files_match_code():
    from academic import MIGRATION_V2
    sql=(app.ROOT/'migrations/002_us10_12_workflow.sql').read_text()
    for statement in MIGRATION_V2:
        assert statement.strip() in sql
    lowered='\n'.join(MIGRATION_V2).lower()
    assert 'drop table' not in lowered and 'drop column' not in lowered
