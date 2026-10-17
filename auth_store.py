"""SQLite users, hashed passwords, expiring sessions, and authentication audit trail."""
from contextlib import contextmanager
from http.cookies import SimpleCookie
from pathlib import Path
import hashlib
import hmac
import re
import secrets
import sqlite3
import time
import unicodedata

PROJECT_NAME='航迹｜航班与空间天气研究系统'
ITERATIONS=600000
SESSION_SECONDS=12*3600
REMEMBER_SECONDS=30*24*3600
DUMMY='pbkdf2_sha256$600000$'+'00'*16+'$'+'00'*32


class AuthError(Exception):
    def __init__(self,message,status=400):self.status=status;super().__init__(message)


def password_hash(password):
    salt=secrets.token_hex(16)
    digest=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),ITERATIONS).hex()
    return f'pbkdf2_sha256${ITERATIONS}${salt}${digest}'


def verify(password,encoded):
    try:
        algorithm,rounds,salt,digest=encoded.split('$')
        if algorithm!='pbkdf2_sha256':return False
        actual=hashlib.pbkdf2_hmac('sha256',password.encode(),bytes.fromhex(salt),int(rounds)).hex()
        return hmac.compare_digest(actual,digest)
    except (ValueError,TypeError):return False


def validate(username,password):
    if not isinstance(username,str) or not isinstance(password,str):raise AuthError('请输入用户名和密码。')
    username=unicodedata.normalize('NFKC',username.strip())
    if not re.fullmatch(r'[A-Za-z0-9_\-\u4e00-\u9fff]{3,32}',username):
        raise AuthError('用户名须为 3—32 位中文、字母、数字、下划线或短横线。')
    if not 10<=len(password)<=128:raise AuthError('密码须为 10—128 个字符。')
    return username


def token_from_cookie(header):
    c=SimpleCookie()
    try:c.load(header or '');return c['flight_session'].value if 'flight_session' in c else ''
    except Exception:return ''


def cookie(token,secure=False,clear=False,max_age=SESSION_SECONDS):
    """Build the session cookie without ever exposing the raw token to JavaScript."""
    age=0 if clear else max(60,min(int(max_age),REMEMBER_SECONDS))
    return f'flight_session={token}; Path=/; Max-Age={age}; HttpOnly; SameSite=Lax'+('; Secure' if secure else '')


class AuthStore:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(exist_ok=True,parents=True)
        with self.db() as db:
            db.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users(
              id INTEGER PRIMARY KEY,username TEXT NOT NULL,username_key TEXT NOT NULL UNIQUE,
              password_hash TEXT NOT NULL,role TEXT NOT NULL DEFAULT 'user',enabled INTEGER NOT NULL DEFAULT 1,
              created_at REAL NOT NULL,register_ip TEXT NOT NULL,last_login_at REAL,last_login_ip TEXT,
              login_count INTEGER NOT NULL DEFAULT 0);
            CREATE TABLE IF NOT EXISTS sessions(
              token_hash TEXT PRIMARY KEY,user_id INTEGER REFERENCES users(id),csrf TEXT NOT NULL,
              created_at REAL NOT NULL,expires_at REAL NOT NULL,last_seen REAL NOT NULL,ip TEXT NOT NULL,user_agent TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS auth_events(
              id INTEGER PRIMARY KEY,created_at REAL NOT NULL,user_id INTEGER,username TEXT NOT NULL,
              action TEXT NOT NULL,success INTEGER NOT NULL,reason TEXT NOT NULL,ip TEXT NOT NULL,user_agent TEXT NOT NULL);
            CREATE INDEX IF NOT EXISTS events_time ON auth_events(created_at);
            CREATE INDEX IF NOT EXISTS events_ip_time ON auth_events(ip,created_at);
            CREATE INDEX IF NOT EXISTS events_user_time ON auth_events(username,created_at);
            ''')

    @contextmanager
    def db(self):
        db=sqlite3.connect(self.path,timeout=15);db.row_factory=sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        try:
            yield db;db.commit()
        except Exception:db.rollback();raise
        finally:db.close()

    def has_admin(self):
        with self.db() as db:
            return bool(db.execute("SELECT 1 FROM users WHERE role='admin' LIMIT 1").fetchone())

    def bootstrap_admin(self,credentials=None,initial_password=None):
        with self.db() as db:
            if db.execute("SELECT 1 FROM users WHERE role='admin'").fetchone():return False
            password=initial_password or secrets.token_urlsafe(20)
            validate('admin',password)
            db.execute('INSERT INTO users(username,username_key,password_hash,role,created_at,register_ip) VALUES(?,?,?,?,?,?)',
                       ('admin','admin',password_hash(password),'admin',time.time(),'local-setup'))
            # Cloud deployments should pass the password as a secret. Local first-run
            # setup keeps the original recoverable credentials-file workflow.
            if credentials and initial_password is None:
                Path(credentials).write_text(f'{PROJECT_NAME}\n管理员用户名：admin\n初始密码：{password}\n\n登录后可在“账号设置”修改密码。请勿将本文件发给普通访客。\n此文件不会通过网站提供下载。\n',encoding='utf-8-sig')
        return True

    @staticmethod
    def public_user(row):
        return {k:row[k] for k in ['id','username','role','enabled','created_at','last_login_at','login_count']}

    def event(self,db,username,action,success,ip,ua,reason='',uid=None):
        db.execute('INSERT INTO auth_events(created_at,user_id,username,action,success,reason,ip,user_agent) VALUES(?,?,?,?,?,?,?,?)',
                   (time.time(),uid,str(username)[:64],action,int(success),reason,str(ip)[:64],str(ua)[:300]))

    def new_session(self,user_id,ip,ua,old='',duration=None):
        token=secrets.token_urlsafe(32);csrf=secrets.token_urlsafe(32);now=time.time()
        ttl=600 if user_id is None else (duration or SESSION_SECONDS)
        ttl=max(60,min(int(ttl),REMEMBER_SECONDS))
        with self.db() as db:
            if old:db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(old.encode()).hexdigest(),))
            db.execute('DELETE FROM sessions WHERE expires_at<?',(now,))
            db.execute('INSERT INTO sessions VALUES(?,?,?,?,?,?,?,?)',
                       (hashlib.sha256(token.encode()).hexdigest(),user_id,csrf,now,now+ttl,now,ip,ua[:300]))
        return token,csrf

    def session(self,token):
        if not token or len(token)>128:return None
        with self.db() as db:
            s=db.execute('SELECT * FROM sessions WHERE token_hash=? AND expires_at>?',
                         (hashlib.sha256(token.encode()).hexdigest(),time.time())).fetchone()
            if not s:return None
            user=None
            if s['user_id']:
                u=db.execute('SELECT * FROM users WHERE id=? AND enabled=1',(s['user_id'],)).fetchone()
                if not u:return None
                user=self.public_user(u)
            if time.time()-s['last_seen']>60:
                db.execute('UPDATE sessions SET last_seen=? WHERE token_hash=?',(time.time(),s['token_hash']))
            return {'csrf':s['csrf'],'user':user,'expires_at':s['expires_at']}

    def register(self,username,password,ip,ua):
        username=validate(username,password)
        with self.db() as db:
            count=db.execute("SELECT COUNT(*) FROM auth_events WHERE action='register' AND success=1 AND ip=? AND created_at>?",(ip,time.time()-3600)).fetchone()[0]
            if count>=10:raise AuthError('当前网络注册过于频繁，请稍后再试。',429)
            try:
                cursor=db.execute('INSERT INTO users(username,username_key,password_hash,created_at,register_ip) VALUES(?,?,?,?,?)',
                                 (username,username.casefold(),password_hash(password),time.time(),ip))
            except sqlite3.IntegrityError:raise AuthError('该用户名已被注册，请换一个用户名。',409) from None
            self.event(db,username,'register',True,ip,ua,uid=cursor.lastrowid)
        return username

    def login(self,username,password,ip,ua):
        if not isinstance(username,str) or not isinstance(password,str) or len(username)>64 or len(password)>128:
            raise AuthError('用户名或密码不正确。',401)
        username=unicodedata.normalize('NFKC',username.strip())
        error=None;user=None
        with self.db() as db:
            count=db.execute("SELECT COUNT(*) FROM auth_events WHERE action='login' AND success=0 AND created_at>? AND (ip=? OR lower(username)=?)",
                             (time.time()-900,ip,username.casefold())).fetchone()[0]
            if count>=10:
                error=AuthError('登录尝试过于频繁，请 15 分钟后再试。',429)
            else:
                row=db.execute('SELECT * FROM users WHERE username_key=?',(username.casefold(),)).fetchone()
                valid=verify(password,row['password_hash'] if row else DUMMY)
                if not row or not valid or not row['enabled']:
                    self.event(db,username,'login',False,ip,ua,'invalid_credentials_or_disabled',row['id'] if row else None)
                    error=AuthError('用户名或密码不正确，或账号已被停用。',401)
                else:
                    db.execute('UPDATE users SET last_login_at=?,last_login_ip=?,login_count=login_count+1 WHERE id=?',(time.time(),ip,row['id']))
                    self.event(db,row['username'],'login',True,ip,ua,uid=row['id'])
                    user=self.public_user(row)
        if error:raise error
        return user

    def logout(self,token,ip,ua):
        s=self.session(token)
        with self.db() as db:
            db.execute('DELETE FROM sessions WHERE token_hash=?',(hashlib.sha256(token.encode()).hexdigest(),))
            if s and s['user']:self.event(db,s['user']['username'],'logout',True,ip,ua,uid=s['user']['id'])

    def change_password(self,user,old,new,ip,ua):
        validate(user['username'],new)
        if not isinstance(old,str) or len(old)>128:raise AuthError('当前密码不正确。')
        if old==new:raise AuthError('新密码不能与当前密码相同。')
        with self.db() as db:
            row=db.execute('SELECT password_hash FROM users WHERE id=?',(user['id'],)).fetchone()
            if not verify(old,row['password_hash']):raise AuthError('当前密码不正确。',401)
            db.execute('UPDATE users SET password_hash=? WHERE id=?',(password_hash(new),user['id']))
            db.execute('DELETE FROM sessions WHERE user_id=?',(user['id'],))
            self.event(db,user['username'],'password_change',True,ip,ua,uid=user['id'])

    def admin_users(self,page=1,search=''):
        where='WHERE username LIKE ?';params=('%'+search[:32]+'%',)
        with self.db() as db:
            total=db.execute('SELECT COUNT(*) FROM users '+where,params).fetchone()[0]
            rows=db.execute('SELECT id,username,role,enabled,created_at,register_ip,last_login_at,last_login_ip,login_count FROM users '+where+' ORDER BY id DESC LIMIT 30 OFFSET ?',params+((page-1)*30,)).fetchall()
            users=[]
            for r in rows:
                u=dict(r);u['active_sessions']=db.execute('SELECT COUNT(*) FROM sessions WHERE user_id=? AND expires_at>? AND last_seen>?',(r['id'],time.time(),time.time()-900)).fetchone()[0];users.append(u)
            summary={'users':db.execute('SELECT COUNT(*) FROM users').fetchone()[0],
                     'enabled':db.execute('SELECT COUNT(*) FROM users WHERE enabled=1').fetchone()[0],
                     'logins24h':db.execute("SELECT COUNT(*) FROM auth_events WHERE action='login' AND success=1 AND created_at>?",(time.time()-86400,)).fetchone()[0],
                     'failures24h':db.execute("SELECT COUNT(*) FROM auth_events WHERE action='login' AND success=0 AND created_at>?",(time.time()-86400,)).fetchone()[0]}
        return {'users':users,'total':total,'page':page,'page_size':30,'summary':summary}

    def admin_events(self,page=1,search=''):
        with self.db() as db:
            params=('%'+search[:32]+'%',)
            total=db.execute('SELECT COUNT(*) FROM auth_events WHERE username LIKE ?',params).fetchone()[0]
            rows=db.execute('SELECT id,created_at,username,action,success,reason,ip,user_agent FROM auth_events WHERE username LIKE ? ORDER BY id DESC LIMIT 50 OFFSET ?',params+((page-1)*50,)).fetchall()
        return {'events':[dict(r) for r in rows],'page':page,'page_size':50,'total':total}

    def set_enabled(self,actor,user_id,enabled,ip,ua):
        with self.db() as db:
            row=db.execute('SELECT * FROM users WHERE id=?',(user_id,)).fetchone()
            if not row:raise AuthError('用户不存在。',404)
            if row['role']=='admin':raise AuthError('不能通过此操作停用管理员。',403)
            db.execute('UPDATE users SET enabled=? WHERE id=?',(int(enabled),user_id))
            if not enabled:db.execute('DELETE FROM sessions WHERE user_id=?',(user_id,))
            self.event(db,actor['username'],'user_enable' if enabled else 'user_disable',True,ip,ua,reason='target:'+row['username'],uid=actor['id'])
