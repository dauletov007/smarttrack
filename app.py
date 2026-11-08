"""SmartTrack US1–12 — FastAPI + PostgreSQL. Python 3.9+."""
import os, re, hmac, time, json, ssl, secrets, hashlib, smtplib, logging, argparse, getpass
from contextlib import contextmanager
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from fastapi import FastAPI, Request, Form, HTTPException
from fastapi.exception_handlers import http_exception_handler, request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from urllib.parse import urlparse
from fastapi.responses import RedirectResponse, HTMLResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.sessions import SessionMiddleware
from psycopg import connect
from psycopg.rows import dict_row

ROOT=Path(__file__).resolve().parent
DATABASE_URL=os.environ.get('DATABASE_URL','')
SECRET=os.environ.get('SESSION_SECRET','')
if not SECRET:
    fp=ROOT/'.session_secret'
    if not fp.exists():
        fp.write_text(secrets.token_urlsafe(64));fp.chmod(0o600)
    SECRET=fp.read_text().strip()
app=FastAPI(title='SmartTrack')
app.add_middleware(SessionMiddleware,secret_key=SECRET,same_site='lax',https_only=os.environ.get('COOKIE_SECURE')=='1',max_age=60*60*24)
app.mount('/static',StaticFiles(directory=ROOT/'static'),name='static')
tpl=Jinja2Templates(directory=str(ROOT/'templates'))
def _fmt_time(ts):
    try:return datetime.fromtimestamp(int(ts)).strftime('%Y-%m-%d %H:%M')
    except (TypeError,ValueError,OverflowError,OSError):return '—'
def _fmt_num(v):
    from academic import fmt_num
    return fmt_num(v)
tpl.env.filters['dt']=_fmt_time
tpl.env.globals['asset_v']=hashlib.sha256(b''.join((ROOT/'static'/n).read_bytes() for n in ('style.css','app.js'))).hexdigest()[:10]
tpl.env.filters['num']=_fmt_num
logger=logging.getLogger('smarttrack')

def now(): return int(time.time())
def digest(x): return hashlib.sha256(x.encode()).hexdigest()
def pwd_hash(p):
    salt=secrets.token_bytes(16)
    return 'pbkdf2$600000$'+salt.hex()+'$'+hashlib.pbkdf2_hmac('sha256',p.encode(),salt,600000).hex()
def pwd_valid(p,stored):
    try:
        alg,count,salt,expected=stored.split('$')
        if alg!='pbkdf2' or not 100000<=int(count)<=2000000:return False
        return hmac.compare_digest(hashlib.pbkdf2_hmac('sha256',p.encode(),bytes.fromhex(salt),int(count)),bytes.fromhex(expected))
    except (ValueError,AttributeError,TypeError):return False

@contextmanager
def db():
    if not DATABASE_URL:raise RuntimeError('DATABASE_URL is required. Read README.md')
    with connect(DATABASE_URL,row_factory=dict_row) as conn:
        with conn.cursor() as cur:yield cur
        conn.commit()

def setup():
    with db() as c:
        c.execute('''CREATE TABLE IF NOT EXISTS users (
        id BIGSERIAL PRIMARY KEY,email TEXT NOT NULL UNIQUE,password_hash TEXT NOT NULL,
        full_name TEXT NOT NULL,role TEXT NOT NULL CHECK(role IN ('student','teacher','parent','admin')),
        verified BOOLEAN NOT NULL DEFAULT FALSE,teacher_status TEXT NOT NULL DEFAULT 'none',
        blocked BOOLEAN NOT NULL DEFAULT FALSE,session_version INTEGER NOT NULL DEFAULT 0,
        created_at BIGINT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS profiles(user_id BIGINT PRIMARY KEY REFERENCES users(id) ON DELETE CASCADE,
        university TEXT DEFAULT '',faculty TEXT DEFAULT '',major TEXT DEFAULT '',study_year TEXT DEFAULT '',
        department TEXT DEFAULT '',subjects TEXT DEFAULT '',position TEXT DEFAULT '',relationship TEXT DEFAULT '')''')
        c.execute('''CREATE TABLE IF NOT EXISTS codes(id BIGSERIAL PRIMARY KEY,user_id BIGINT REFERENCES users(id) ON DELETE CASCADE,
        purpose TEXT NOT NULL,code_hash TEXT NOT NULL,expires_at BIGINT NOT NULL,attempts INTEGER NOT NULL DEFAULT 0,
        consumed BOOLEAN NOT NULL DEFAULT FALSE,created_at BIGINT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS invites(id BIGSERIAL PRIMARY KEY,student_id BIGINT NOT NULL REFERENCES users(id),
        code_hash TEXT NOT NULL UNIQUE,expires_at BIGINT NOT NULL,used_by BIGINT REFERENCES users(id),revoked BOOLEAN NOT NULL DEFAULT FALSE)''')
        c.execute('''CREATE TABLE IF NOT EXISTS parent_links(parent_id BIGINT REFERENCES users(id),student_id BIGINT REFERENCES users(id),
        linked_at BIGINT NOT NULL,PRIMARY KEY(parent_id,student_id))''')
        c.execute('''CREATE TABLE IF NOT EXISTS audit(id BIGSERIAL PRIMARY KEY,actor_id BIGINT REFERENCES users(id),
        action TEXT NOT NULL,target_id BIGINT,created_at BIGINT NOT NULL)''')
        c.execute('''CREATE TABLE IF NOT EXISTS login_attempts(email TEXT PRIMARY KEY,count INTEGER NOT NULL DEFAULT 0,
        last_at BIGINT NOT NULL)''')
        from academic import migrate
        migrate(c)

def send_mail(to,subject,body):
    host=os.getenv('SMTP_HOST',''); user=os.getenv('SMTP_USER','');pw=os.getenv('SMTP_PASSWORD','')
    if host and user and pw:
        msg=EmailMessage();msg['From']=os.getenv('SMTP_FROM',user);msg['To']=to;msg['Subject']=subject;msg.set_content(body)
        with smtplib.SMTP(host,int(os.getenv('SMTP_PORT','587')),timeout=15) as s:
            s.starttls(context=ssl.create_default_context());s.login(user,pw);s.send_message(msg)
    elif os.getenv('DEVELOPMENT_EMAIL')=='1':
        print('\n[LOCAL TEST EMAIL] TO:',to,'\nSUBJECT:',subject,'\n'+body+'\n',flush=True)
    else:raise RuntimeError('Email not configured: set SMTP credentials or DEVELOPMENT_EMAIL=1 (local testing only)')

def issue_code(c,uid,purpose,minutes=10):
    c.execute('UPDATE codes SET consumed=TRUE WHERE user_id=%s AND purpose=%s AND NOT consumed',(uid,purpose))
    code=f'{secrets.randbelow(1000000):06d}'
    c.execute('INSERT INTO codes(user_id,purpose,code_hash,expires_at,created_at) VALUES(%s,%s,%s,%s,%s)',
              (uid,purpose,digest(f'{uid}:{purpose}:{code}'),now()+minutes*60,now()))
    return code

def use_code(c,uid,purpose,code):
    row=c.execute('SELECT * FROM codes WHERE user_id=%s AND purpose=%s AND NOT consumed ORDER BY id DESC LIMIT 1 FOR UPDATE',(uid,purpose)).fetchone()
    if not row or row['expires_at']<now() or row['attempts']>=5:return False
    c.execute('UPDATE codes SET attempts=attempts+1 WHERE id=%s',(row['id'],))
    if not hmac.compare_digest(row['code_hash'],digest(f'{uid}:{purpose}:{code.strip()}')):return False
    c.execute('UPDATE codes SET consumed=TRUE WHERE id=%s',(row['id'],));return True

def csrf(req):
    if not req.session.get('csrf'):req.session['csrf']=secrets.token_urlsafe(32)
    return req.session['csrf']
def guard(req,entered):
    if not entered or not hmac.compare_digest(req.session.get('csrf',''),entered):raise HTTPException(403,'Invalid form token')
def current(req):
    uid=req.session.get('uid')
    if not uid:return None
    with db() as c:u=c.execute('SELECT * FROM users WHERE id=%s',(uid,)).fetchone()
    if not u or u['blocked'] or not u['verified'] or u['session_version']!=req.session.get('sv'):return None
    if u['role']=='teacher' and u['teacher_status']!='approved':return None
    return u

def view(req,page,**extra):
    lang=req.session.get('lang','en'); user=current(req)
    error=extra.pop('error','') or req.session.pop('academic_error','')
    notice=extra.pop('notice','') or req.session.pop('academic_notice','')
    draft=req.session.pop('academic_subject_draft',{}) if page=='academic_courses' else {}
    nav_counts,nav_children={},[]
    if user and DATABASE_URL:
        try:nav_counts,nav_children=_nav_context(user)
        except Exception:logger.exception('Navigation context unavailable')
    return tpl.TemplateResponse(request=req,name=page+'.html',context={
        'csrf':csrf(req),'lang':lang,'user':user,'error':error,'notice':notice,
        'subject_draft':draft,'nav_counts':nav_counts,'nav_children':nav_children,**extra})

def _nav_context(user):
    """Read-only sidebar data: attention counters (admin/teacher) and linked children (parent)."""
    with db() as c:
        if user['role']=='admin':
            row=c.execute("""SELECT (SELECT COUNT(*) FROM assessments WHERE status='pending') pending_assessments,
                (SELECT COUNT(*) FROM group_join_requests WHERE status='pending') pending_requests,
                (SELECT COUNT(*) FROM users WHERE role='teacher' AND verified AND teacher_status='pending_approval' AND NOT blocked) pending_teachers""").fetchone()
            return dict(row),[]
        if user['role']=='teacher':
            row=c.execute("""SELECT COUNT(*) rejected_assessments FROM assessments a JOIN course_offerings o ON o.id=a.offering_id
                WHERE o.teacher_id=%s AND a.status='rejected'""",(user['id'],)).fetchone()
            return dict(row),[]
        if user['role']=='parent':
            kids=c.execute("""SELECT u.id,u.full_name FROM parent_links l JOIN users u ON u.id=l.student_id
                WHERE l.parent_id=%s AND u.role='student' ORDER BY u.full_name""",(user['id'],)).fetchall()
            return {},kids
    return {},[]

def _safe_back(req,explicit=None):
    """Local page to return to after a form error (never an external URL)."""
    if explicit and explicit.startswith('/') and not explicit.startswith('//'):return explicit
    referer=urlparse(req.headers.get('referer',''))
    if referer.path.startswith('/') and not referer.path.startswith('//') and referer.netloc in ('',req.url.netloc):
        return referer.path+('?'+referer.query if referer.query else '')
    return '/academic' if req.url.path.startswith('/academic') else '/dashboard'

def _wants_html(req):
    return 'text/html' in req.headers.get('accept','')

ERROR_TITLES={403:'Access denied',404:'Not found',409:'Action not available',400:'Please check the form'}

@app.exception_handler(HTTPException)
async def friendly_academic_error(req:Request, exc:HTTPException):
    # Security denials keep their HTTP status. Correctable form problems (400/404/409/422)
    # on academic/admin forms go back to the page with a readable message instead of JSON.
    if req.method=='POST' and (req.url.path.startswith('/academic/') or req.url.path=='/admin/action') and exc.status_code in (400,404,409,422):
        req.session['academic_error']=str(exc.detail)
        return RedirectResponse(_safe_back(req,getattr(exc,'back',None)),status_code=303)
    if _wants_html(req) and exc.status_code in ERROR_TITLES:
        login=exc.detail=='Login required'
        resp=view(req,'message',heading=ERROR_TITLES[exc.status_code],message=str(exc.detail),
                  next_url='/login' if login else ('/academic' if req.session.get('uid') else '/'))
        resp.status_code=exc.status_code
        return resp
    return await http_exception_handler(req,exc)

@app.exception_handler(RequestValidationError)
async def friendly_validation_error(req:Request, exc:RequestValidationError):
    if req.method=='POST' and (req.url.path.startswith('/academic/') or req.url.path=='/admin/action'):
        req.session['academic_error']='Please fill in all required fields and try again.'
        return RedirectResponse(_safe_back(req),status_code=303)
    return await request_validation_exception_handler(req,exc)

def go(path):return RedirectResponse(path,status_code=303)
def require(req,role=None):
    u=current(req)
    if not u:raise HTTPException(403,'Login required')
    if role and u['role']!=role:raise HTTPException(403,'Not permitted')
    return u
def audit(c,uid,action,target=None,details=''):
    c.execute('INSERT INTO audit(actor_id,action,target_id,created_at,details) VALUES(%s,%s,%s,%s,%s)',(uid,action,target,now(),(details or '')[:500]))

@app.get('/')
def home(req:Request):return view(req,'home')
@app.get('/lang/{lang}')
def setlang(req:Request,lang:str):
    if lang not in ('en','ru'):raise HTTPException(404)
    req.session['lang']=lang
    from urllib.parse import urlparse
    back=req.headers.get('referer','/')
    parsed=urlparse(back)
    return go(parsed.path if parsed.path.startswith('/') and not parsed.path.startswith('//') else '/')
@app.get('/register')
def register_page(req:Request):return view(req,'register')
@app.post('/register')
def register(req:Request,full_name:str=Form(...),email:str=Form(...),password:str=Form(...),role:str=Form(...),invite_code:str=Form(''),csrf_token:str=Form(...)):
    guard(req,csrf_token);email=email.strip().lower();full_name=full_name.strip()
    if role not in ('student','teacher','parent') or not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',email) or len(password)<8 or len(full_name)<2:
        return view(req,'register',error='Please check your name, email, password (8+ characters) and role.')
    with db() as c:
        if c.execute('SELECT id FROM users WHERE email=%s',(email,)).fetchone():return view(req,'register',error='Email already registered.')
        inv=None
        if role=='parent':
            inv=c.execute('SELECT * FROM invites WHERE code_hash=%s AND NOT revoked AND used_by IS NULL AND expires_at>%s FOR UPDATE',(digest(invite_code.strip()),now())).fetchone()
            if not inv:return view(req,'register',error='Invalid or expired student invitation.')
        u=c.execute('INSERT INTO users(email,password_hash,full_name,role,teacher_status,created_at) VALUES(%s,%s,%s,%s,%s,%s) RETURNING id',
          (email,pwd_hash(password),full_name,role,'pending_email' if role=='teacher' else 'none',now())).fetchone()
        uid=u['id'];c.execute('INSERT INTO profiles(user_id) VALUES(%s)',(uid,))
        if inv:
            c.execute('UPDATE invites SET used_by=%s WHERE id=%s',(uid,inv['id']))
            c.execute('INSERT INTO parent_links(parent_id,student_id,linked_at) VALUES(%s,%s,%s)',(uid,inv['student_id'],now()))
        code=issue_code(c,uid,'email')
    try:send_mail(email,'SmartTrack — email verification',f'Your SmartTrack verification code is: {code}\nValid for 10 minutes. Do not share it.')
    except Exception:
        logger.exception('Registration email delivery failed')
        return view(req,'message',heading='Account created',message='Email delivery failed. Check SMTP settings and use Resend code on the verification page.',next_url='/verify?email='+email)
    return go('/verify?email='+email)
@app.get('/verify')
def verify_page(req:Request,email:str=''):return view(req,'verify',email=email)
@app.post('/verify')
def verify(req:Request,email:str=Form(...),code:str=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);email=email.strip().lower()
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE email=%s',(email,)).fetchone()
        if not u or not use_code(c,u['id'],'email',code):return view(req,'verify',email=email,error='Wrong or expired code. Please try again.')
        c.execute('UPDATE users SET verified=TRUE WHERE id=%s',(u['id'],))
        if u['role']=='teacher':
            approval=issue_code(c,u['id'],'teacher',1440)
            c.execute("UPDATE users SET teacher_status='pending_approval' WHERE id=%s",(u['id'],))
        else:approval=None
    if approval:
        admin=os.getenv('ADMIN_EMAIL','')
        if admin:
            try:send_mail(admin,'SmartTrack — teacher approval code',f'TEACHER REQUEST\nName: {u["full_name"]}\nEmail: {email}\nApproval code: {approval}\nValid: 24 hours. Only share with the teacher after checking their identity.')
            except Exception:logger.exception('Admin teacher code email failed')
        return go('/teacher-approval?email='+email)
    return view(req,'message',heading='Email confirmed!',message='You may now log in.',next_url='/login')
@app.post('/resend')
def resend(req:Request,email:str=Form(...),purpose:str=Form('email'),csrf_token:str=Form(...)):
    guard(req,csrf_token);email=email.strip().lower()
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE email=%s',(email,)).fetchone()
        if u and not u['verified'] and purpose=='email':
            recent=c.execute("SELECT created_at FROM codes WHERE user_id=%s AND purpose='email' ORDER BY id DESC LIMIT 1",(u['id'],)).fetchone()
            if recent and now()-recent['created_at']<60:return view(req,'verify',email=email,error='Wait 60 seconds before requesting another code.')
            code=issue_code(c,u['id'],'email')
        else:code=None
    if code:
        try:send_mail(email,'SmartTrack — new verification code',f'Your code: {code}\nValid for 10 minutes.')
        except Exception:logger.exception('Resend failed')
    return view(req,'verify',email=email,notice='If eligible, a new code has been sent.')
@app.get('/teacher-approval')
def teacher_approval_page(req:Request,email:str=''):return view(req,'teacher_approval',email=email)
@app.post('/teacher-approval')
def teacher_approval(req:Request,email:str=Form(...),code:str=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);email=email.strip().lower()
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE email=%s AND role=%s AND verified=TRUE',(email,'teacher')).fetchone()
        if not u or u['teacher_status']!='pending_approval' or not use_code(c,u['id'],'teacher',code):
            return view(req,'teacher_approval',email=email,error='Invalid or expired approval code.')
        c.execute("UPDATE users SET teacher_status='approved' WHERE id=%s",(u['id'],))
    return view(req,'message',heading='Teacher approved',message='Your teacher account is now active. Please log in.',next_url='/login')
@app.get('/login')
def login_page(req:Request):return view(req,'login')
@app.post('/login')
def login(req:Request,email:str=Form(...),password:str=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);email=email.strip().lower()
    with db() as c:
        a=c.execute('SELECT * FROM login_attempts WHERE email=%s',(email,)).fetchone()
        if a and a['count']>=5 and now()-a['last_at']<900:return view(req,'login',error='Too many attempts. Try again in 15 minutes.')
        u=c.execute('SELECT * FROM users WHERE email=%s',(email,)).fetchone()
        valid=bool(u and pwd_valid(password,u['password_hash']))
        if not valid:
            c.execute('INSERT INTO login_attempts(email,count,last_at) VALUES(%s,1,%s) ON CONFLICT(email) DO UPDATE SET count=CASE WHEN login_attempts.last_at<%s THEN 1 ELSE login_attempts.count+1 END,last_at=%s',(email,now(),now()-900,now()))
            return view(req,'login',error='Invalid email or password.')
        c.execute('DELETE FROM login_attempts WHERE email=%s',(email,))
    if u['blocked']:return view(req,'login',error='This account is blocked.')
    if not u['verified']:return go('/verify?email='+email)
    if u['role']=='teacher' and u['teacher_status']!='approved':return go('/teacher-approval?email='+email)
    language=req.session.get('lang','en');req.session.clear();req.session['uid']=u['id'];req.session['sv']=u['session_version'];req.session['lang']=language
    return go('/dashboard')
@app.post('/logout')
def logout(req:Request,csrf_token:str=Form(...)):
    guard(req,csrf_token);req.session.clear();return go('/')
@app.get('/dashboard')
def dashboard(req:Request):
    u=require(req)
    if u['role']=='admin':return go('/academic/admin')
    from academic import overview_data
    with db() as c:
        p=c.execute('SELECT * FROM profiles WHERE user_id=%s',(u['id'],)).fetchone()
        overview=overview_data(c,u)
        links=[];invites=[];stats={};users=[];events=[]
        if u['role']=='student':
            links=c.execute('SELECT u.id,u.full_name,u.email FROM parent_links l JOIN users u ON u.id=l.parent_id WHERE l.student_id=%s',(u['id'],)).fetchall()
            invites=c.execute('SELECT id,expires_at,used_by,revoked FROM invites WHERE student_id=%s ORDER BY id DESC LIMIT 10',(u['id'],)).fetchall()
        if u['role']=='parent':links=c.execute('SELECT u.id,u.full_name,u.email,p.university FROM parent_links l JOIN users u ON u.id=l.student_id LEFT JOIN profiles p ON p.user_id=u.id WHERE l.parent_id=%s',(u['id'],)).fetchall()
        if u['role']=='admin':
            users=c.execute('SELECT id,full_name,email,role,teacher_status,verified,blocked,created_at FROM users ORDER BY id DESC LIMIT 200').fetchall()
            stats={row['role']:row['count'] for row in c.execute('SELECT role,COUNT(*) AS count FROM users GROUP BY role').fetchall()}
            events=c.execute('SELECT a.*,u.email actor FROM audit a LEFT JOIN users u ON a.actor_id=u.id ORDER BY a.id DESC LIMIT 20').fetchall()
    return view(req,'dashboard',page_title='Overview',profile=p,links=links,invites=invites,stats=stats,users=users,events=events,**overview)

@app.get('/admin/users')
def admin_users(req:Request,q:str='',role:str='',status:str=''):
    """US1–9 user administration (moved from the admin dashboard into its own page)."""
    require(req,'admin')
    with db() as c:
        users=c.execute('SELECT id,full_name,email,role,teacher_status,verified,blocked,created_at FROM users ORDER BY id DESC LIMIT 1000').fetchall()
        stats={row['role']:row['count'] for row in c.execute('SELECT role,COUNT(*) AS count FROM users GROUP BY role').fetchall()}
    ql=q.strip().lower()
    def keep(x):
        if ql and ql not in x['full_name'].lower() and ql not in x['email'].lower():return False
        if role and x['role']!=role:return False
        if status=='blocked' and not x['blocked']:return False
        if status=='unverified' and x['verified']:return False
        if status=='pending' and not (x['role']=='teacher' and x['teacher_status']!='approved'):return False
        if status=='active' and (x['blocked'] or not x['verified']):return False
        return True
    return view(req,'admin_users',page_title='Users',users=[x for x in users if keep(x)],total=len(users),stats=stats,
                f={'q':q,'role':role,'status':status})

@app.get('/admin/activity')
def admin_activity(req:Request,q:str='',area:str='',page:int=1):
    require(req,'admin')
    page=max(page,1);per=50
    with db() as c:
        rows=c.execute('''SELECT a.*,u.full_name actor,u.email actor_email,t.full_name target_name FROM audit a
            LEFT JOIN users u ON u.id=a.actor_id
            LEFT JOIN users t ON t.id=a.target_id AND a.action IN ('block','unblock','revoke_teacher','reject_teacher','issue_teacher','approve_teacher','change_role')
            WHERE (%s='' OR a.action LIKE %s) AND (%s='' OR a.action ILIKE %s OR a.details ILIKE %s OR u.full_name ILIKE %s OR u.email ILIKE %s)
            ORDER BY a.id DESC LIMIT %s OFFSET %s''',
            (area,area+'%',q,'%'+q+'%','%'+q+'%','%'+q+'%','%'+q+'%',per+1,(page-1)*per)).fetchall()
    return view(req,'admin_activity',page_title='Activity log',rows=rows[:per],has_more=len(rows)>per,pageno=page,f={'q':q,'area':area})
@app.post('/invite')
def invite(req:Request,csrf_token:str=Form(...)):
    guard(req,csrf_token);u=require(req,'student');code='ST-'+secrets.token_urlsafe(12)
    with db() as c:c.execute('INSERT INTO invites(student_id,code_hash,expires_at) VALUES(%s,%s,%s)',(u['id'],digest(code),now()+7*86400))
    return view(req,'invite',invite_code=code)
@app.post('/invite/revoke')
def revoke_invite(req:Request,id:int=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);u=require(req,'student')
    with db() as c:c.execute('UPDATE invites SET revoked=TRUE WHERE id=%s AND student_id=%s',(id,u['id']))
    return go('/dashboard')
@app.post('/unlink')
def unlink(req:Request,other:int=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);u=require(req)
    with db() as c:
        if u['role']=='student':c.execute('DELETE FROM parent_links WHERE student_id=%s AND parent_id=%s',(u['id'],other))
        elif u['role']=='parent':c.execute('DELETE FROM parent_links WHERE parent_id=%s AND student_id=%s',(u['id'],other))
        else:raise HTTPException(403)
    return go('/dashboard')
@app.get('/profile')
def profile_page(req:Request):
    u=require(req)
    with db() as c:p=c.execute('SELECT * FROM profiles WHERE user_id=%s',(u['id'],)).fetchone()
    return view(req,'profile',profile=p)
@app.post('/profile')
def profile_save(req:Request,full_name:str=Form(...),university:str=Form(''),faculty:str=Form(''),major:str=Form(''),study_year:str=Form(''),department:str=Form(''),subjects:str=Form(''),position:str=Form(''),relationship:str=Form(''),csrf_token:str=Form(...)):
    guard(req,csrf_token);u=require(req)
    if not 2<=len(full_name.strip())<=120: return go('/profile')
    vals=[v.strip()[:200] for v in (university,faculty,major,study_year,department,subjects,position,relationship)]
    with db() as c:
        c.execute('UPDATE users SET full_name=%s WHERE id=%s',(full_name.strip(),u['id']))
        c.execute('''INSERT INTO profiles(user_id,university,faculty,major,study_year,department,subjects,position,relationship)
        VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(user_id) DO UPDATE SET university=excluded.university,faculty=excluded.faculty,major=excluded.major,study_year=excluded.study_year,department=excluded.department,subjects=excluded.subjects,position=excluded.position,relationship=excluded.relationship''',(u['id'],*vals))
    return go('/dashboard')
@app.post('/profile/password')
def change_password(req:Request,old_password:str=Form(...),new_password:str=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);u=require(req)
    if len(new_password)<8 or not pwd_valid(old_password,u['password_hash']):return view(req,'message',heading='Password unchanged',message='Current password incorrect or new password too short.',next_url='/profile')
    with db() as c:c.execute('UPDATE users SET password_hash=%s,session_version=session_version+1 WHERE id=%s',(pwd_hash(new_password),u['id']))
    req.session.clear();return go('/login')
@app.post('/profile/email')
def change_email_request(req:Request,new_email:str=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);u=require(req);new_email=new_email.strip().lower()
    if not re.fullmatch(r'[^\s@]+@[^\s@]+\.[^\s@]+',new_email):return go('/profile')
    with db() as c:
        if c.execute('SELECT id FROM users WHERE email=%s',(new_email,)).fetchone():return view(req,'message',heading='Email unavailable',message='Address already registered.',next_url='/profile')
        code=issue_code(c,u['id'],'change_email');req.session['new_email']=new_email
    try:send_mail(new_email,'SmartTrack — confirm new email',f'Your email change code: {code}\nValid for 10 minutes.')
    except Exception:logger.exception('Change-email delivery failed')
    return go('/change-email')
@app.get('/change-email')
def change_email_page(req:Request):require(req);return view(req,'change_email')
@app.post('/change-email')
def change_email(req:Request,code:str=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);u=require(req);new_email=req.session.get('new_email')
    if not new_email:return go('/profile')
    with db() as c:
        if not use_code(c,u['id'],'change_email',code):return view(req,'change_email',error='Invalid or expired code.')
        if c.execute('SELECT id FROM users WHERE email=%s',(new_email,)).fetchone():return go('/profile')
        c.execute('UPDATE users SET email=%s WHERE id=%s',(new_email,u['id']))
    req.session.pop('new_email',None);return go('/profile')
@app.get('/forgot')
def forgot_page(req:Request):return view(req,'forgot')
@app.post('/forgot')
def forgot(req:Request,email:str=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token);email=email.lower().strip()
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE email=%s AND verified=TRUE',(email,)).fetchone()
        code=issue_code(c,u['id'],'reset') if u else None
    if code:
        try:send_mail(email,'SmartTrack — password reset',f'Your password reset code is: {code}\nExpires in 10 minutes.')
        except Exception:logger.exception('Reset email delivery failed')
    return go('/reset?email='+email)
@app.get('/reset')
def reset_page(req:Request,email:str=''):return view(req,'reset',email=email)
@app.post('/reset')
def reset(req:Request,email:str=Form(...),code:str=Form(...),password:str=Form(...),csrf_token:str=Form(...)):
    guard(req,csrf_token)
    with db() as c:
        u=c.execute('SELECT * FROM users WHERE email=%s',(email.lower().strip(),)).fetchone()
        if not u or len(password)<8 or not use_code(c,u['id'],'reset',code):return view(req,'reset',email=email,error='Invalid code or password (minimum 8 characters).')
        c.execute('UPDATE users SET password_hash=%s,session_version=session_version+1 WHERE id=%s',(pwd_hash(password),u['id']))
    req.session.clear();return go('/login')
@app.post('/admin/action')
def admin_action(req:Request,target_id:int=Form(...),action:str=Form(...),role:str=Form(''),next:str=Form(''),csrf_token:str=Form(...)):
    guard(req,csrf_token);admin=require(req,'admin')
    with db() as c:
        target=c.execute('SELECT * FROM users WHERE id=%s FOR UPDATE',(target_id,)).fetchone()
        if not target or target['role']=='admin':raise HTTPException(403,'Admin cannot be modified')
        if action=='block':c.execute('UPDATE users SET blocked=TRUE,session_version=session_version+1 WHERE id=%s',(target_id,))
        elif action=='unblock':c.execute('UPDATE users SET blocked=FALSE WHERE id=%s',(target_id,))
        elif action=='revoke_teacher' and target['role']=='teacher':c.execute("UPDATE users SET teacher_status='pending_approval',session_version=session_version+1 WHERE id=%s",(target_id,))
        elif action=='approve_teacher' and target['role']=='teacher' and target['verified'] and target['teacher_status']!='approved':
            # Direct approval after the administrator has checked the teacher's identity.
            c.execute("UPDATE codes SET consumed=TRUE WHERE user_id=%s AND purpose='teacher' AND NOT consumed",(target_id,))
            c.execute("UPDATE users SET teacher_status='approved' WHERE id=%s",(target_id,))
        elif action=='reject_teacher' and target['role']=='teacher':c.execute("UPDATE users SET teacher_status='rejected',session_version=session_version+1 WHERE id=%s",(target_id,))
        elif action=='issue_teacher' and target['role']=='teacher' and target['verified']:
            code=issue_code(c,target_id,'teacher',1440)
            c.execute("UPDATE users SET teacher_status='pending_approval' WHERE id=%s",(target_id,))
            email=os.getenv('ADMIN_EMAIL','')
            if email:
                try:send_mail(email,'SmartTrack — teacher approval code',f'Teacher: {target["full_name"]}\nEmail: {target["email"]}\nCode: {code}\nValid for 24 hours. Share only after checking identity.')
                except Exception:logger.exception('Teacher approval email failed')
        elif action=='change_role' and role in ('student','parent'):
            c.execute('UPDATE users SET role=%s,teacher_status=%s,session_version=session_version+1 WHERE id=%s',(role,'none',target_id))
            c.execute('DELETE FROM parent_links WHERE parent_id=%s OR student_id=%s',(target_id,target_id))
        else:raise HTTPException(400,'This action is not available for this account.')
        audit(c,admin['id'],action,target_id,target['email'])
    if action=='approve_teacher':req.session['academic_notice']=target['full_name']+' is now an approved teacher.'
    return go(next if (next.startswith('/academic') or next.startswith('/admin/')) and not next.startswith('//') else '/admin/users')

def create_admin(email):
    email=email.strip().lower();password=getpass.getpass('Admin password (8+ chars): ')
    if len(password)<8:raise ValueError('At least 8 characters')
    with db() as c:
        if c.execute("SELECT id FROM users WHERE role='admin'").fetchone():raise ValueError('Main Admin already exists')
        u=c.execute("INSERT INTO users(email,password_hash,full_name,role,verified,created_at) VALUES(%s,%s,%s,'admin',TRUE,%s) RETURNING id",(email,pwd_hash(password),'Main Admin',now())).fetchone()
        c.execute('INSERT INTO profiles(user_id) VALUES(%s)',(u['id'],))
    print('Admin created successfully.')
from academic import install as install_academic
install_academic(app,db,view,require,guard,now,audit,go)

def check_schema():
    """Warn loudly if `python3 app.py init-db` has not been run after upgrading."""
    if not DATABASE_URL:return
    try:
        with db() as c:
            ok=c.execute("SELECT 1 FROM information_schema.columns WHERE table_name='assessments' AND column_name='status'").fetchone()
        if not ok:
            logger.error('SmartTrack database schema is out of date. Back up the database, then run: /usr/bin/python3 app.py init-db')
            print('\n*** SmartTrack: database schema is out of date. Back up, then run: /usr/bin/python3 app.py init-db ***\n',flush=True)
    except Exception:
        logger.exception('Could not check the database schema')
app.router.on_startup.append(check_schema)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('command',choices=['init-db','create-admin']);a=p.parse_args()
    setup()
    if a.command=='create-admin':create_admin(input('Admin email: '))
    else:print('PostgreSQL tables are ready.')
