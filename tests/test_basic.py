"""Unit tests run without a live PostgreSQL database (HTTP persistence needs integration testing)."""
import sys, types
if 'psycopg' not in sys.modules:
    try:import psycopg
    except ImportError:
        fake=types.ModuleType('psycopg');fake.connect=lambda *args,**kwargs:None
        fake_rows=types.ModuleType('psycopg.rows');fake_rows.dict_row=object()
        sys.modules['psycopg']=fake;sys.modules['psycopg.rows']=fake_rows
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import app
from fastapi.testclient import TestClient

def test_password_hash():
    h=app.pwd_hash('correcthorsebattery');assert app.pwd_valid('correcthorsebattery',h)
    assert not app.pwd_valid('wrong',h)
    assert not app.pwd_valid('whatever','nonsense')

def test_public_pages():
    client=TestClient(app.app)
    for path in ['/','/login','/register','/forgot','/verify?email=a%40b.com']:
        r=client.get(path)
        assert r.status_code==200,(path,r.text[:500])
    assert 'smarttrack' in client.get('/').text.lower()

def test_localization():
    client=TestClient(app.app)
    r=client.get('/lang/ru',follow_redirects=True)
    assert r.status_code==200
    assert 'Студентам' in r.text

def test_post_requires_csrf():
    client=TestClient(app.app)
    assert client.post('/register',data={'email':'u@a.com','password':'12345678','role':'student','full_name':'Test User','csrf_token':'bad'}).status_code==403

def test_templates_compile():
    for name in Path(app.ROOT/'templates').glob('*.html'):
        app.tpl.env.get_template(name.name)
