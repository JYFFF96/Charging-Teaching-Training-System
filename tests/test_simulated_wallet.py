import json
import threading
import unittest
from concurrent.futures import ThreadPoolExecutor
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import test_customer_charging as fixture
m=fixture.m

class Wallet(unittest.TestCase):
    setUp=fixture.Charging.setUp
    tearDown=fixture.Charging.tearDown
    start=fixture.Charging.start
    telemetry=fixture.Charging.telemetry
    def invoice(self,order,raw=123456):
        return {'pile_no':order['pile_no'],'gun_no':'%02d'%order['gun_no'],'serial_no':order['serial_no'],
                'total_amount':raw,'total_energy':25000}
    def adjustment(self,action,amount,rid='request_123',username='alice'):
        return {'username':username,'action':action,'amount':amount,'request_id':rid,'note':'测试调整'}
    def test_initial_bonus_and_restart_only_once(self):
        self.assertEqual(self.app.wallet_snapshot(1)['balance_fen'],100000)
        self.app.ensure_wallets();reopened=m.ChargingApp(self.db,'missing',True)
        self.assertEqual(reopened.wallet_snapshot(1)['balance_fen'],100000)
        self.assertEqual(len(reopened.wallet_snapshot(1)['ledger']),1)
    def test_success_settlement_rounding_duplicate_and_changed_invoice(self):
        order=self.start();w=self.app.wallet_snapshot(1)
        self.assertEqual(w['balance_fen'],100000);self.assertEqual(w['reserved_fen'],100000);self.assertEqual(w['available_fen'],0)
        d=self.invoice(order);self.app.transaction(d);self.app.transaction(d)
        w=self.app.wallet_snapshot(1);self.assertEqual(w['balance_fen'],100000-1235);self.assertEqual(w['reserved_fen'],0)
        self.assertEqual(sum(e['kind']=='charging' for e in w['ledger']),1)
        reopened=m.ChargingApp(self.db,'missing',True);reopened.transaction(d)
        self.assertEqual(reopened.wallet_snapshot(1)['balance_fen'],98765)
        reopened.transaction({**d,'total_amount':234567})
        self.assertEqual(reopened.wallet_snapshot(1)['balance_fen'],98765)
        self.assertEqual(reopened.owned(order['id'],1)['settlement_status'],'review')
    def test_reason5_failure_prompt_and_no_charge(self):
        def reject(frame):
            self.sock.frames.append(frame);seq,_,ftype,data,_=m.try_parse_frame(frame)
            d=m.parse_body(ftype,data);d.update(start_result=0,fail_reason=5)
            self.session.h_33(seq,fixture.body(0x33,d))
        self.sock.sendall=reject
        order=self.start();self.assertEqual(order['status'],'failed');self.assertEqual(order['fail_reason'],5)
        self.assertIn('先拔枪',order['note']);self.assertIn('重新插',order['note'])
        w=self.app.wallet_snapshot(1);self.assertEqual(w['balance_fen'],100000);self.assertEqual(w['reserved_fen'],0)
        self.assertEqual(len(w['ledger']),1)
    def test_admin_adjustments_idempotent_and_available_balance(self):
        p=self.adjustment('credit','25.50')
        with ThreadPoolExecutor(2) as pool:
            results=list(pool.map(lambda _:self.app.admin_adjust(p,'admin_a'),range(2)))
        self.assertEqual(sum(r['duplicate'] for r in results),1)
        self.assertEqual(self.app.wallet_snapshot(1)['balance_fen'],102550)
        self.app.admin_adjust(self.adjustment('debit','2.50','request_456'),'admin_a')
        self.assertEqual(self.app.wallet_snapshot(1)['balance_fen'],102300)
        self.start()
        with self.assertRaises(m.ApiError):self.app.admin_adjust(self.adjustment('debit','24.00','request_789'),'admin_a')
        self.assertEqual(self.app.wallet_snapshot(1)['balance_fen'],102300)
        with self.assertRaises(m.ApiError):self.app.admin_adjust({**p,'amount':'26.00'},'admin_a')
    def test_low_balance_limits_device_credit_and_no_balance_blocks_start(self):
        self.app.admin_adjust(self.adjustment('debit','999.50'),'admin_a')
        order=self.start()
        _,_,_,data,_=m.try_parse_frame(self.sock.frames[0]);self.assertEqual(m.parse_body(0x34,data)['balance'],50)
        self.app.transaction(self.invoice(order,5000))
        self.assertEqual(self.app.wallet_snapshot(1)['balance_fen'],0)
        self.telemetry(1)
        with self.assertRaises(m.ApiError):self.start()
    def test_unknown_reservation_restart_and_legacy_no_retrocharge(self):
        self.sock.reply=False;order=self.start();self.assertEqual(order['status'],'unknown')
        reopened=m.ChargingApp(self.db,'missing',True);self.assertEqual(reopened.wallet_snapshot(1)['reserved_fen'],100000)
        with reopened.connect() as c:c.execute("UPDATE orders SET wallet_managed=0,settlement_status='legacy' WHERE id=?",(order['id'],))
        reopened.transaction(self.invoice(order))
        self.assertEqual(reopened.wallet_snapshot(1)['balance_fen'],100000)
    def test_unassociated_invoice_and_invalid_amounts(self):
        self.app.transaction({'pile_no':fixture.PILE,'gun_no':'01','serial_no':'7'*32,'total_amount':10000,'total_energy':0})
        self.assertEqual(self.app.wallet_snapshot(1)['balance_fen'],100000)
        for amount in ('0','-1','0.001','NaN','1e2','1000001'):
            with self.assertRaises(m.ApiError):self.app.admin_adjust(self.adjustment('credit',amount),'admin_a')
    def test_final_bill_overrun_is_recorded_as_debt(self):
        self.app.admin_adjust(self.adjustment('debit','999.50'),'admin_a')
        order=self.start();self.app.transaction(self.invoice(order,10000))
        self.assertEqual(self.app.wallet_snapshot(1)['balance_fen'],-50)
        self.assertIn('余额为负',self.app.owned(order['id'],1)['note'])

    def test_admin_wallet_http_permissions(self):
        server=ThreadingHTTPServer(('127.0.0.1',0),m.make_api_handler('manager'))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            def request(method,path,token,data=None):
                c=HTTPConnection('127.0.0.1',server.server_port);headers={'Authorization':'Bearer '+token}
                if data is not None:headers['Content-Type']='application/json'
                c.request(method,path,None if data is None else json.dumps(data),headers)
                r=c.getresponse();status=r.status;result=json.loads(r.read());c.close();return status,result
            self.assertEqual(request('GET','/api/customer-wallets','')[0],401)
            self.assertEqual(request('POST','/api/customer-wallets/adjust',self.app.issue(1),self.adjustment('credit','1.00'))[0],401)
            status,d=request('GET','/api/customer-wallets?username=ali','manager');self.assertEqual(status,200);self.assertEqual(len(d['users']),1)
            status,d=request('POST','/api/customer-wallets/adjust','manager',self.adjustment('credit','1.00'))
            self.assertEqual(status,200);self.assertEqual(d['wallet']['balance_fen'],100100)
        finally:server.shutdown();server.server_close()

if __name__=='__main__':unittest.main()
