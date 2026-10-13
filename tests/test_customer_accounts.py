import importlib.util
import json
from pathlib import Path
import tempfile
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer

spec=importlib.util.spec_from_file_location('customer',Path(__file__).resolve().parents[1]/'server'/'ykc_platform_server.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)

class Accounts(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.db=Path(self.tmp.name)/'users.sqlite3'
        self.page=Path(self.tmp.name)/'charge.html';self.page.write_text('customer test page')
        self.app=m.CustomerAccounts(self.db,self.page,True)
        self.server=ThreadingHTTPServer(('127.0.0.1',0),m.handler_for(self.app))
        self.port=self.server.server_port
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True);self.thread.start()
    def tearDown(self):
        self.server.shutdown();self.server.server_close();self.tmp.cleanup()
    def request(self,path,payload=None,cookie='',origin=True):
        c=HTTPConnection('127.0.0.1',self.port)
        headers={'Cookie':cookie}
        if payload is not None:
            headers.update({'Content-Type':'application/json','X-Customer-Request':'1'})
            if origin: headers['Origin']='http://127.0.0.1:'+str(self.port)
        c.request('GET' if payload is None else 'POST',path,None if payload is None else json.dumps(payload),headers)
        r=c.getresponse();data=json.loads(r.read());status=r.status;newcookie=r.getheader('Set-Cookie','').split(';')[0];c.close()
        return status,data,newcookie
    def test_account_lifecycle_and_persistence(self):
        p={'username':'Customer_1','password':'test-password-123','security_question_id':'movie','security_answer':'My special answer'}
        status,d,cookie=self.request('/api/customer/register',p);self.assertEqual(status,201)
        self.assertEqual(self.app.wallet_snapshot(1)['balance_fen'],100000)
        self.assertEqual(self.request('/api/customer/me',cookie=cookie)[1]['user']['username'],'customer_1')
        self.assertEqual(self.request('/api/customer/register',p)[0],409)
        self.assertEqual(self.request('/api/customer/login',{'username':'customer_1','password':'wrong-password'})[0],401)
        proof={**p,'security_answer':'incorrect'}
        self.assertEqual(self.request('/api/customer/recovery/verify',proof)[0],401)
        status,d,_=self.request('/api/customer/recovery/verify',p);self.assertEqual(status,200)
        reset={'reset_token':d['reset_token'],'new_password':'new-password-456'}
        self.assertEqual(self.request('/api/customer/recovery/reset',reset)[0],200)
        self.assertEqual(self.request('/api/customer/recovery/reset',reset)[0],401)
        self.assertEqual(self.request('/api/customer/me',cookie=cookie)[0],401)
        self.assertEqual(self.request('/api/customer/login',p)[0],401)
        status,_,cookie=self.request('/api/customer/login',{'username':'customer_1','password':'new-password-456'});self.assertEqual(status,200)
        self.assertEqual(self.request('/api/customer/logout',{},cookie)[0],200)
        self.assertEqual(self.request('/api/customer/me',cookie=cookie)[0],401)
        reopened=m.CustomerAccounts(self.db,self.page,True)
        with reopened.connect() as c:
            row=c.execute('SELECT * FROM users').fetchone()
            self.assertTrue(m.matches('new-password-456',row['password']))
            self.assertNotEqual(row['answer'],p['security_answer'])
            self.assertNotEqual(row['password'],reset['new_password'])
    def test_new_questions_and_single_character_answers(self):
        questions = ('movie','book','color','sport','disliked_food','first_phone_last4','respected_person','favorite_number')
        for index, question in enumerate(questions):
            answer = '1234' if question == 'first_phone_last4' else '7'
            payload = {'username':'question_'+str(index),'password':'question-password',
                       'security_question_id':question,'security_answer':answer}
            self.assertEqual(self.request('/api/customer/register',payload)[0],201)
            with self.app.connect() as c:
                row=c.execute('SELECT * FROM users WHERE username=?',(payload['username'],)).fetchone()
                self.assertEqual(row['question'],question)
                self.assertTrue(m.matches(answer,row['answer']))
        status,_,_=self.request('/api/customer/recovery/verify',
            {'username':'question_7','security_question_id':'favorite_number','security_answer':'7'})
        self.assertEqual(status,200)

    def test_origin_and_rate_limit(self):
        p={'username':'unknown','password':'bad-password'}
        self.assertEqual(self.request('/api/customer/login',p,origin=False)[0],403)
        for _ in range(5): self.assertEqual(self.request('/api/customer/login',p)[0],401)
        self.assertEqual(self.request('/api/customer/login',p)[0],429)
    def test_reset_expiry_and_secure_cookie(self):
        with self.app.connect() as c:
            c.execute('INSERT INTO resets VALUES (?,?,?)',(m.token_hash('expired'),1,0))
        self.assertEqual(self.request('/api/customer/recovery/reset',{'reset_token':'expired','new_password':'new-password'})[0],401)
        self.app.insecure=False
        self.assertEqual(self.request('/api/customer/login',{'username':'unknown','password':'password'})[0],403)

if __name__=='__main__': unittest.main()
