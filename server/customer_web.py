#!/usr/bin/env python3
"""Standalone customer accounts and page server; charging remains a frontend demo."""
import argparse
import hashlib
import hmac
import json
import os
import re
import secrets
import sqlite3
import time
import unicodedata
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlsplit

ITERATIONS = 600000
QUESTIONS = ('secret', 'character', 'memory')

def digest(value):
    salt = secrets.token_hex(16)
    hashed = hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return salt + ':' + hashed

def matches(value, stored):
    salt, expected = stored.split(':')
    actual = hashlib.pbkdf2_hmac('sha256', value.encode(), bytes.fromhex(salt), ITERATIONS).hex()
    return hmac.compare_digest(actual, expected)

def answer_text(value):
    return unicodedata.normalize('NFKC', value).strip().casefold()

def token_hash(value):
    return hashlib.sha256(value.encode()).hexdigest()

class App:
    def __init__(self, db, page, insecure=False):
        self.db, self.page, self.insecure = str(db), Path(page), insecure
        Path(db).parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as c:
            c.executescript('''
            CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL, question TEXT NOT NULL, answer TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS sessions(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS resets(token TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires REAL NOT NULL);
            CREATE TABLE IF NOT EXISTS limits(key TEXT PRIMARY KEY, count INTEGER NOT NULL, until REAL NOT NULL);
            ''')
        os.chmod(self.db, 0o600)
        self.dummy = digest('dummy-account-timing-value')
    def connect(self):
        c = sqlite3.connect(self.db, timeout=15)
        c.row_factory = sqlite3.Row
        return c
    def limit(self, keys, maximum=5):
        now = time.time()
        with self.connect() as c:
            c.execute('BEGIN IMMEDIATE')
            c.execute('DELETE FROM limits WHERE until < ?', (now,))
            rows = [c.execute('SELECT count FROM limits WHERE key=?', (k,)).fetchone() for k in keys]
            if any(r and r['count'] >= maximum for r in rows):
                return False
            for key in keys:
                c.execute('INSERT INTO limits VALUES (?,1,?) ON CONFLICT(key) DO UPDATE SET count=count+1', (key, now+600))
        return True
    def session(self, token):
        with self.connect() as c:
            return c.execute('SELECT u.id,u.username FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token=? AND s.expires>?', (token_hash(token),time.time())).fetchone()
    def issue(self, user_id):
        token = secrets.token_urlsafe(32)
        with self.connect() as c:
            c.execute('DELETE FROM sessions WHERE expires<?', (time.time(),))
            c.execute('INSERT INTO sessions VALUES (?,?,?)', (token_hash(token),user_id,time.time()+86400))
        return token

class ApiError(Exception):
    def __init__(self, status, message):
        self.status, self.message = status, message

def handler_for(app):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt, *args):
            # Never log request payloads, tokens, or URL query parameters.
            print('%s %s' % (self.client_address[0], fmt % args), flush=True)
        def log_request(self, code='-', size='-'):
            self.log_message('%s %s %s', self.command, urlsplit(self.path).path, code)
        def send(self, obj, status=200, cookie=None):
            body=json.dumps(obj,ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header('Content-Type','application/json; charset=utf-8')
            self.send_header('Content-Length',str(len(body)))
            self.send_header('Cache-Control','no-store')
            self.send_header('X-Content-Type-Options','nosniff')
            if cookie: self.send_header('Set-Cookie',cookie)
            self.end_headers()
            self.wfile.write(body)
        def cookie(self, token='', expire=False):
            return 'customer_session=%s; Path=/; HttpOnly; SameSite=Strict; Max-Age=%s%s' % (token,0 if expire else 86400,'' if app.insecure else '; Secure')
        def token(self):
            try:
                cookies=SimpleCookie(self.headers.get('Cookie',''))
                return cookies['customer_session'].value if 'customer_session' in cookies else ''
            except Exception: return ''
        def do_GET(self):
            path=urlsplit(self.path).path
            if path in ('/','/charge.html','/index.html'):
                try: body=app.page.read_bytes()
                except OSError: return self.send({'error':'客户页面文件不存在'},503)
                self.send_response(200)
                self.send_header('Content-Type','text/html; charset=utf-8')
                self.send_header('Content-Length',str(len(body)))
                self.send_header('Cache-Control','no-store')
                self.send_header('X-Content-Type-Options','nosniff')
                self.send_header('Referrer-Policy','same-origin')
                self.end_headers(); self.wfile.write(body); return
            if path=='/api/customer/me':
                u=app.session(self.token())
                return self.send({'user':{'username':u['username']}},200) if u else self.send({'error':'请先登录'},401)
            if path=='/api/customer/config':
                return self.send({'insecure_test':app.insecure,'charging_demo':True})
            self.send({'error':'接口不存在'},404)
        def do_POST(self):
            try:
                # Require a same-origin browser JSON request. No CORS support.
                origin=self.headers.get('Origin')
                host=self.headers.get('Host')
                if not origin or urlsplit(origin).netloc!=host or self.headers.get('X-Customer-Request')!='1':
                    raise ApiError(403,'请求来源无效，请从客户页面操作')
                if urlsplit(origin).scheme!='https' and not app.insecure:
                    raise ApiError(403,'账号功能需要HTTPS；HTTP仅允许显式测试模式')
                if self.headers.get('Content-Type','').split(';')[0]!='application/json':
                    raise ApiError(415,'需要JSON请求')
                length=int(self.headers.get('Content-Length','0'))
                if not 0<length<=8192: raise ApiError(400,'请求长度无效')
                self.connection.settimeout(10)
                payload=json.loads(self.rfile.read(length))
                if not isinstance(payload,dict): raise ApiError(400,'请求格式错误')
                path=urlsplit(self.path).path
                if path=='/api/customer/logout':
                    with app.connect() as c: c.execute('DELETE FROM sessions WHERE token=?',(token_hash(self.token()),))
                    return self.send({'ok':True},cookie=self.cookie(expire=True))
                if path not in ('/api/customer/register','/api/customer/login','/api/customer/recovery/verify','/api/customer/recovery/reset'):
                    raise ApiError(404,'接口不存在')
                ip=self.client_address[0]
                if not app.limit(['ip:'+ip],60): raise ApiError(429,'请求过于频繁，请10分钟后重试')
                if path.endswith('/reset'): return self.reset(payload)
                username=payload.get('username','')
                if not isinstance(username,str) or not re.fullmatch('[A-Za-z0-9_]{4,32}',username):
                    raise ApiError(400,'账号必须为4–32位字母、数字或下划线')
                username=username.lower()
                action=path.rsplit('/',1)[-1]
                if not app.limit([action+':account:'+username,action+':ip:'+ip],5 if action!='register' else 10):
                    raise ApiError(429,'尝试次数过多，请10分钟后重试')
                with app.connect() as c: u=c.execute('SELECT * FROM users WHERE username=?',(username,)).fetchone()
                if action=='register':
                    pwd=self.password(payload.get('password'))
                    question,answer=self.answer(payload)
                    with app.connect() as c:
                        try:
                            uid=c.execute('INSERT INTO users(username,password,question,answer) VALUES (?,?,?,?)',(username,digest(pwd),question,digest(answer))).lastrowid
                        except sqlite3.IntegrityError: raise ApiError(409,'该账号无法注册，请换一个账号或登录')
                    return self.send({'ok':True,'user':{'username':username}},201,cookie=self.cookie(app.issue(uid)))
                if action=='login':
                    pwd=self.password(payload.get('password'))
                    good=matches(pwd,u['password'] if u else app.dummy)
                    if not u or not good: raise ApiError(401,'账号或密码错误')
                    with app.connect() as c: c.execute('DELETE FROM limits WHERE key=?',('login:account:'+username,))
                    return self.send({'ok':True,'user':{'username':username}},cookie=self.cookie(app.issue(u['id'])))
                question,answer=self.answer(payload)
                good=matches(answer,u['answer'] if u else app.dummy)
                if not u or not good or u['question']!=question: raise ApiError(401,'账号、安全问题或答案不匹配')
                token=secrets.token_urlsafe(32)
                with app.connect() as c:
                    c.execute('DELETE FROM resets WHERE expires<? OR user_id=?',(time.time(),u['id']))
                    c.execute('INSERT INTO resets VALUES (?,?,?)',(token_hash(token),u['id'],time.time()+300))
                self.send({'ok':True,'reset_token':token})
            except ApiError as e: self.send({'error':e.message},e.status)
            except (ValueError,TypeError,json.JSONDecodeError): self.send({'error':'请求格式错误'},400)
            except Exception:
                self.send({'error':'服务暂时不可用，请稍后重试'},500)
        def password(self,pwd):
            if not isinstance(pwd,str) or not 8<=len(pwd)<=128: raise ApiError(400,'密码长度需要8–128位')
            return pwd
        def answer(self,p):
            q,a=p.get('security_question_id'),p.get('security_answer')
            if q not in QUESTIONS or not isinstance(a,str) or not 2<=len(answer_text(a))<=128:
                raise ApiError(400,'请选择安全问题，答案长度需要2–128位')
            return q,answer_text(a)
        def reset(self,p):
            pwd=self.password(p.get('new_password'))
            token=p.get('reset_token')
            if not isinstance(token,str) or len(token)>128: raise ApiError(400,'重置凭证无效')
            hashed=digest(pwd)
            with app.connect() as c:
                c.execute('BEGIN IMMEDIATE')
                row=c.execute('SELECT * FROM resets WHERE token=? AND expires>?',(token_hash(token),time.time())).fetchone()
                if not row: raise ApiError(401,'重置凭证已失效，请重新验证安全问题')
                uid=row['user_id']
                c.execute('UPDATE users SET password=? WHERE id=?',(hashed,uid))
                c.execute('DELETE FROM resets WHERE user_id=?',(uid,))
                c.execute('DELETE FROM sessions WHERE user_id=?',(uid,))
            self.send({'ok':True},cookie=self.cookie(expire=True))
    return Handler

def main():
    os.umask(0o077)
    p=argparse.ArgumentParser()
    p.add_argument('--host',default='127.0.0.1')
    p.add_argument('--port',type=int,default=8088)
    p.add_argument('--page',default=str(Path(__file__).resolve().parents[1]/'customer'/'index.html'))
    p.add_argument('--db',default=str(Path(__file__).resolve().parent/'customer_data'/'customers.sqlite3'))
    p.add_argument('--allow-insecure-test',action='store_true',help='仅虚构账号HTTP联调，正式部署不要使用')
    a=p.parse_args()
    app=App(a.db,a.page,a.allow_insecure_test)
    server=ThreadingHTTPServer((a.host,a.port),handler_for(app))
    print('Customer accounts running on %s:%s; HTTP test=%s'%(a.host,a.port,a.allow_insecure_test),flush=True)
    try: server.serve_forever()
    except KeyboardInterrupt: pass
    finally: server.server_close()
if __name__=='__main__': main()
