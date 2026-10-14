import unittest
from concurrent.futures import ThreadPoolExecutor
import test_customer_charging as fixture
m=fixture.m

class AccountOccupancy(unittest.TestCase):
    setUp=fixture.Charging.setUp
    tearDown=fixture.Charging.tearDown
    telemetry=fixture.Charging.telemetry
    start=fixture.Charging.start
    def device(self,gun,uid):
        return self.app.customer_get('/api/customer/device',f'/api/customer/device?pile_no={fixture.PILE}&gun_no={gun}',uid)
    def test_each_unfinished_status_blocks_second_gun(self):
        order=self.app.customer_post('/api/customer/orders',{'pile_no':fixture.PILE,'gun_no':1,'reserve_amount':'100'},1)['order']
        for status in m.ACTIVE_STATES:
            with self.app.connect() as c: c.execute('UPDATE orders SET status=? WHERE id=?',(status,order['id']))
            d=self.device(2,1)
            self.assertEqual(d['availability'],'account_busy');self.assertEqual(d['active_order']['id'],order['id'])
            with self.assertRaises(m.ApiError) as error: self.start(1,2)
            self.assertIn('未结束',error.exception.message if hasattr(error.exception,'message') else str(error.exception))
            self.assertEqual(len(self.sock.frames),1)
        # An unsuccessful order releases the account restriction.
        with self.app.connect() as c: c.execute("UPDATE orders SET status='failed' WHERE id=?",(order['id'],))
        self.assertEqual(self.device(2,1)['availability'],'idle')
        self.assertEqual(self.start(1,2)['gun_no'],2)
    def test_simultaneous_same_user_different_guns(self):
        def attempt(gun):
            try: return self.app.customer_post('/api/customer/orders',{'pile_no':fixture.PILE,'gun_no':gun,'reserve_amount':'100'},1)
            except m.ApiError as e: return e.status
        with ThreadPoolExecutor(2) as pool: results=list(pool.map(attempt,[1,2]))
        self.assertEqual(sum(isinstance(r,dict) for r in results),1)
        self.assertIn(409,results);self.assertEqual(len(self.sock.frames),1)
        self.assertEqual(self.app.wallet_snapshot(1)['reserved_fen'],10000)
    def test_other_account_cannot_view_start_or_stop_occupied_gun(self):
        order=self.start()
        before=self.app.wallet_snapshot(2)
        d=self.device(1,2)
        self.assertEqual(d['availability'],'occupied')
        for key in ['order','active_order','latest','serial_no','username','wallet']: self.assertNotIn(key,d)
        with self.assertRaises(m.ApiError): self.start(2,1)
        with self.assertRaises(m.ApiError): self.app.customer_post('/api/customer/orders/'+order['id']+'/stop',{},2)
        self.assertEqual(self.app.wallet_snapshot(2),before);self.assertEqual(len(self.sock.frames),1)
        with self.app.connect() as c: c.execute("UPDATE orders SET status='finished' WHERE id=?",(order['id'],))
        self.assertEqual(self.device(1,2)['availability'],'idle')
    def test_unassociated_charging_gun_is_private_and_occupied(self):
        self.telemetry(2,3,'external')
        d=self.device(2,1)
        self.assertEqual(d['availability'],'occupied');self.assertNotIn('latest',d)
        with self.assertRaises(m.ApiError): self.start(1,2)
        self.assertEqual(len(self.sock.frames),0)
