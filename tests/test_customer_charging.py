import importlib.util
from pathlib import Path
import tempfile
import time
import unittest
from concurrent.futures import ThreadPoolExecutor

spec=importlib.util.spec_from_file_location('integrated',Path(__file__).resolve().parents[1]/'server'/'ykc_platform_server.py')
m=importlib.util.module_from_spec(spec);spec.loader.exec_module(m)
PILE='32010200000001'

def body(ftype,values):
    out=b''
    for name,fmt,_ in m.SPECS[ftype][1]:
        n=m._FMT_LEN[fmt];value=values.get(name,0)
        if fmt.startswith('bcd'): out+=m.str_to_bcd(str(value),n)
        elif fmt=='cp56': out+=m.cp56time2a_encode()
        elif fmt.endswith('hex'): out+=bytes.fromhex(str(value).rjust(n*2,'0'))
        elif fmt.startswith('ascii'): out+=str(value).encode().ljust(n,b'\0')[:n]
        else: out+=int(value).to_bytes(n,'little')
    return out

class FakeSocket:
    def __init__(self): self.session=None;self.frames=[];self.reply=True;self.fail=False
    def sendall(self,frame):
        self.frames.append(frame)
        seq,_,ftype,b,_=m.try_parse_frame(frame)
        if self.fail: raise OSError('connection interrupted')
        if self.reply and ftype==0x34:
            d=m.parse_body(ftype,b);d.update(start_result=1,fail_reason=0)
            self.session.h_33(seq,body(0x33,d))
        if self.reply and ftype==0x36:
            d=m.parse_body(ftype,b);d.update(stop_result=1,fail_reason=0)
            # An unrelated gun must not wake this waiter.
            other={**d,'gun_no':'02' if d['gun_no']=='01' else '01'}
            self.session.h_35(seq,body(0x35,other))
            key=(seq,d['gun_no'])
            assert not self.session.customer_stops[key][0].is_set()
            self.session.h_35(seq,body(0x35,d))

class Charging(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=Path(self.tmp.name)/'customers.sqlite3'
        self.app=m.ChargingApp(self.db,Path(self.tmp.name)/'charge.html',True)
        m.CUSTOMER_APP=self.app;m.START_WAIT=.01;m.STOP_WAIT=.01
        m.SessionRegistry.sessions.clear();m.STORE.latest.clear()
        self.sock=FakeSocket();self.session=m.PileSession(self.sock,('127.0.0.1',1234));self.sock.session=self.session
        self.session.pile_no=PILE;self.session.pile_no_bcd=m.str_to_bcd(PILE,7)
        m.SessionRegistry.register(PILE,self.session)
        self.telemetry(1);self.telemetry(2)
    def tearDown(self):
        m.CUSTOMER_APP=None;m.SessionRegistry.sessions.clear();m.STORE.latest.clear();self.tmp.cleanup()
    def telemetry(self,gun,status=2,serial='0',plugged=1):
        d={'pile_no':PILE,'gun_no':'%02d'%gun,'status':status,'serial_no':serial,'gun_plugged':plugged,'hw_fault':0}
        m.STORE.save_realtime(PILE,d['gun_no'],d);self.app.realtime(d)
    def start(self,uid=1,gun=1): return self.app.customer_post('/api/customer/orders',{'pile_no':PILE,'gun_no':gun},uid)['order']
    def test_fast_reply_ownership_and_transaction(self):
        order=self.start();self.assertEqual(order['status'],'charging')
        self.assertEqual(self.app.customer_get('/api/customer/device','/api/customer/device?pile_no='+PILE+'&gun_no=1',1)['availability'],'mine')
        occupied=self.app.customer_get('/api/customer/device','/api/customer/device?pile_no='+PILE+'&gun_no=1',2)
        self.assertEqual(occupied['availability'],'occupied');self.assertNotIn('order',occupied)
        with self.assertRaises(m.ApiError): self.app.owned(order['id'],2)
        with self.assertRaises(m.ApiError): self.start(2)
        self.assertEqual(self.start()['id'],order['id']);self.assertEqual(len(self.sock.frames),1)
        self.telemetry(1,3,order['serial_no'])
        stopped=self.app.customer_post('/api/customer/orders/'+order['id']+'/stop',{},1)['order']
        self.assertEqual(stopped['status'],'stopping')
        self.telemetry(1,2,order['serial_no']);self.assertIsNotNone(self.app.active(PILE,1))
        d={'pile_no':PILE,'gun_no':'01','serial_no':order['serial_no'],'total_amount':12345,'total_energy':20000}
        self.app.transaction(d);self.app.transaction(d)
        self.assertIsNone(self.app.active(PILE,1));self.assertEqual(self.app.owned(order['id'],1)['status'],'finished')
        with self.app.connect() as c: self.assertEqual(c.execute('SELECT count(*) FROM orders').fetchone()[0],1)
    def test_concurrent_users_and_two_guns(self):
        def attempt(uid):
            try: return self.start(uid)
            except m.ApiError as e: return e.status
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(attempt,[1,2]))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1);self.assertIn(409,results)
        second=self.start(2,2);self.assertEqual(second['gun_no'],2)
    def test_timeout_restart_and_late_reply(self):
        self.sock.reply=False
        order=self.start();self.assertEqual(order['status'],'unknown')
        self.app=m.ChargingApp(self.db,'missing',True);m.CUSTOMER_APP=self.app
        with self.assertRaises(m.ApiError): self.start(2)
        self.assertEqual(self.start()['id'],order['id']);self.assertEqual(len(self.sock.frames),1)
        self.app.start_reply({'pile_no':PILE,'gun_no':'01','serial_no':order['serial_no'],'start_result':1})
        self.assertEqual(self.app.owned(order['id'],1)['status'],'charging')
    def test_missing_stale_unplugged_and_unowned_charging(self):
        self.telemetry(1,plugged=0)
        with self.assertRaises(m.ApiError): self.start()
        self.telemetry(1,3,'9999')
        self.assertEqual(self.app.customer_get('/api/customer/device','/api/customer/device?pile_no='+PILE+'&gun_no=1',1)['availability'],'occupied')
        with self.assertRaises(m.ApiError): self.start()
        with m.STORE._lock: m.STORE.latest[(PILE,'01')]=(time.time()-100,{'pile_no':PILE,'status':2,'gun_plugged':1})
        with self.assertRaises(m.ApiError): self.start()
    def test_stop_cannot_control_mismatched_serial(self):
        order=self.start();self.telemetry(1,3,'1234')
        with self.assertRaises(m.ApiError): self.app.customer_post('/api/customer/orders/'+order['id']+'/stop',{},1)
        self.assertEqual(len(self.sock.frames),1)
    def test_send_failure_keeps_occupation(self):
        self.sock.fail=True
        order=self.start();self.assertEqual(order['status'],'unknown')
        with self.assertRaises(m.ApiError): self.start(2)
    def test_start_failure_releases_gun(self):
        self.sock.reply=False;order=self.start()
        self.app.start_reply({'pile_no':PILE,'gun_no':'01','serial_no':order['serial_no'],'start_result':0,'fail_reason':3})
        self.assertIsNone(self.app.active(PILE,1))

if __name__=='__main__':unittest.main()
