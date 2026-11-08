"""Helpers for PostgreSQL-backed integration tests (separate test database only)."""
import html
import os
import re
from urllib.parse import urlparse

import pytest
from fastapi.testclient import TestClient

import app
import academic
from conftest import ORIGINAL_DATABASE_URL

TEST_DB = os.environ.get('SMARTTRACK_TEST_DATABASE_URL', '')
PASSWORD = 'password123'
_HASH = []


def check_safe_test_database():
    if not TEST_DB:
        pytest.skip('Set SMARTTRACK_TEST_DATABASE_URL to a separate, disposable PostgreSQL test database.')
    name = urlparse(TEST_DB).path.lstrip('/')
    if 'test' not in name.lower():
        pytest.exit('Refusing to run: test database name must contain "test" (got %r).' % name, returncode=2)
    if ORIGINAL_DATABASE_URL and urlparse(ORIGINAL_DATABASE_URL).path == urlparse(TEST_DB).path \
            and urlparse(ORIGINAL_DATABASE_URL).hostname == urlparse(TEST_DB).hostname:
        pytest.exit('Refusing to run: the test database is the same as DATABASE_URL.', returncode=2)


def drop_all_tables():
    with app.db() as c:
        for row in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall():
            c.execute('DROP TABLE IF EXISTS "%s" CASCADE' % row['tablename'])


def truncate_all():
    with app.db() as c:
        names = [r['tablename'] for r in c.execute("SELECT tablename FROM pg_tables WHERE schemaname='public'").fetchall()]
        if names:
            c.execute('TRUNCATE %s RESTART IDENTITY CASCADE' % ','.join('"%s"' % n for n in names))


def password_hash():
    if not _HASH:
        _HASH.append(app.pwd_hash(PASSWORD))
    return _HASH[0]


def make_user(email, role, name=None, teacher_status=None, verified=True, blocked=False):
    status = teacher_status or ('approved' if role == 'teacher' else 'none')
    with app.db() as c:
        row = c.execute('''INSERT INTO users(email,password_hash,full_name,role,verified,teacher_status,blocked,created_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s) RETURNING id''',
                        (email, password_hash(), name or email.split('@')[0].title(), role, verified, status, blocked,
                         app.now())).fetchone()
        c.execute('INSERT INTO profiles(user_id) VALUES(%s)', (row['id'],))
    return row['id']


def link_parent(parent_id, student_id):
    with app.db() as c:
        c.execute('INSERT INTO parent_links(parent_id,student_id,linked_at) VALUES(%s,%s,%s)', (parent_id, student_id, app.now()))


def q(sql, args=()):
    with app.db() as c:
        return c.execute(sql, args).fetchall()


def q1(sql, args=()):
    rows = q(sql, args)
    return rows[0] if rows else None


def csrf_of(text):
    m = re.search(r'name="csrf_token"\s+value="([^"]+)"', text)
    assert m, 'no CSRF token on page'
    return m.group(1)


class Session:
    """A logged-in browser session."""

    def __init__(self, email):
        self.email = email
        self.client = TestClient(app.app)
        r = self.client.get('/login')
        r = self.client.post('/login', data={'email': email, 'password': PASSWORD, 'csrf_token': csrf_of(r.text)},
                             follow_redirects=False)
        assert r.status_code == 303 and r.headers['location'] == '/dashboard', (email, r.status_code, r.text[:300])
        self.token = csrf_of(self.client.get('/dashboard').text)

    def get(self, path):
        return self.client.get(path, headers={'accept': 'text/html'})

    def post(self, path, data=None, files=None, referer='/academic', token=True):
        payload = dict(data or {})
        if token:
            payload.setdefault('csrf_token', self.token)
        return self.client.post(path, data=payload, files=files, follow_redirects=False,
                                headers={'referer': 'http://testserver' + referer, 'accept': 'text/html'})

    def submit(self, path, data=None, files=None, referer='/academic'):
        """POST a form and return (status, location, flash_error, flash_notice) after following the redirect."""
        r = self.post(path, data, files, referer)
        if r.status_code != 303:
            return r.status_code, None, None, None
        loc = r.headers['location']
        page = self.client.get(loc, headers={'accept': 'text/html'}).text
        err = re.search(r'<div class="alert">(.*?)</div>', page, re.S)
        ok = re.search(r'<div class="alert good">(.*?)</div>', page, re.S)
        return 303, loc, html.unescape(err.group(1)) if err else None, html.unescape(ok.group(1)) if ok else None


def ids(table, where='TRUE', args=()):
    return [r['id'] for r in q('SELECT id FROM %s WHERE %s ORDER BY id' % (table, where), args)]
