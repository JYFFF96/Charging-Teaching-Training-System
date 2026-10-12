import json
import threading
import unittest
from http.client import HTTPConnection
from http.server import ThreadingHTTPServer
import test_customer_charging as fixture
m=fixture.m
PILE=fixture.PILE

class Records(unittest.TestCase):
    telemetry=fixture.Charging.telemetry
    start=fixture.Charging.start
    tearDown=fixture.Charging.tearDown
    # Separate fixture setup from the device command regression suite.
    def setUp(self):
        fixture.Charging.setUp(self)
        with self.app.connect() as c:
            c.execute("INSERT INTO users(id,username,password,question,answer) VALUES (1,'alice','unused','movie','unused')")
            c.execute("INSERT INTO users(id,username,password,question,answer) VALUES (2,'bob','unused','book','unused')")
    def test_merged_records_filters_and_totals(self):
        first=self.start(1,1);second=self.start(2,2)
        tx={'pile_no':PILE,'gun_no':'01','serial_no':first['serial_no'],'total_energy':25000,'total_amount':30000,'start_time':'2026-10-12 09:00:00','end_time':'2026-10-12 09:30:00'}
        self.app.transaction(tx);self.app.transaction(tx)
        other={**tx,'gun_no':'03','serial_no':'9'*32,'total_energy':10000,'total_amount':12000}
        self.app.transaction(other)
        result=self.app.admin_records('/api/charging-records')
        self.assertEqual(result['total'],3)
        self.assertEqual(result['summary'],{'final_energy':3.5,'final_amount':4.2})
        result=self.app.admin_records('/api/charging-records?username=ALI&pile_no=320102&gun_no=01&status=finished&date_from=2026-10-12&date_to=2026-10-12')
        self.assertEqual(result['total'],1);self.assertEqual(result['records'][0]['username'],'alice')
        self.assertEqual(result['records'][0]['amount'],3)
        self.assertEqual(self.app.admin_records('/api/charging-records?source=device')['total'],1)
        self.assertEqual(self.app.admin_records('/api/charging-records?status=charging')['total'],1)
        self.assertEqual(self.app.admin_records('/api/charging-records?username=missing')['total'],0)
        self.assertEqual(self.app.admin_records('/api/charging-records?date_to=2026-10-11')['total'],0)
        page=self.app.admin_records('/api/charging-records?page=2&page_size=1')
        self.assertEqual(len(page['records']),1);self.assertEqual(page['total'],3);self.assertEqual(page['pages'],3)
        self.assertNotIn('password',json.dumps(result));self.assertNotIn('answer',json.dumps(result))
    def test_archive_restart_import_and_over_5000(self):
        historical={'pile_no':PILE,'gun_no':'01','serial_no':'8'*32,'total_energy':10000,'total_amount':20000}
        old=m.STORE.transactions
        try:
            m.STORE.transactions=[(0,historical),(0,historical)]
            reopened=m.ChargingApp(self.db,'missing',True)
            self.assertEqual(reopened.admin_records('/api/charging-records')['total'],1)
            with reopened.connect() as c:
                for n in range(5001): reopened._archive_write(c,{**historical,'serial_no':str(n).zfill(32)})
            m.STORE.transactions=[]
            reopened=m.ChargingApp(self.db,'missing',True)
            self.assertEqual(reopened.admin_records('/api/charging-records')['total'],5002)
        finally: m.STORE.transactions=old
    def test_management_authentication_and_customer_isolation(self):
        self.start()
        server=ThreadingHTTPServer(('127.0.0.1',0),m.make_api_handler('management-token'))
        threading.Thread(target=server.serve_forever,daemon=True).start()
        try:
            def get(auth):
                c=HTTPConnection('127.0.0.1',server.server_port)
                c.request('GET','/api/charging-records',headers={'Authorization':'Bearer '+auth})
                r=c.getresponse();status=r.status;payload=json.loads(r.read());c.close();return status,payload
            self.assertEqual(get('')[0],401)
            self.assertEqual(get(self.app.issue(1))[0],401)
            status,payload=get('management-token');self.assertEqual(status,200);self.assertEqual(payload['total'],1)
        finally:server.shutdown();server.server_close()
        with self.assertRaises(m.ApiError):self.app.customer_get('/api/charging-records','/api/charging-records',1)
    def test_invalid_filters(self):
        for query in ('gun_no=0','gun_no=abc','status=bad','source=bad','date_from=not-a-date','date_from=2026-10-13&date_to=2026-10-12','page=abc'):
            with self.assertRaises(m.ApiError): self.app.admin_records('/api/charging-records?'+query)

if __name__=='__main__':unittest.main()
