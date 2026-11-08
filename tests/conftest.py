"""Shared test setup.

Safety: the development/production DATABASE_URL (from .env / start-env.sh) is removed
before the app is imported, so no test can ever read or write it. Database-backed tests
run only against SMARTTRACK_TEST_DATABASE_URL, whose database name must contain "test".
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

ORIGINAL_DATABASE_URL = os.environ.get('DATABASE_URL', '')
os.environ['DATABASE_URL'] = ''
os.environ['DEVELOPMENT_EMAIL'] = '1'
for key in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'ADMIN_EMAIL'):
    os.environ.pop(key, None)
