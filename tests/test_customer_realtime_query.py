import unittest
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch
import test_customer_charging as fixture
m=fixture.m

class RealtimeQuery(unittest.TestCase):
    setUp=fixture.Charging.setUp
    tearDown=fixture.Charging.tearDown
    telemetry=fixture.Charging.telemetry
    def get_device(self):
        return self.app.customer_get('/api/customer/device','/api/customer/device?pile_no='+fixture.PILE+'&gun_no=1',1)
    def missing(self):
        m.STORE.latest.pop((fixture.PILE,'01'),None)
    def test_missing_triggers_query_and_is_throttled_across_tabs(self):
        self.missing();sentinel=object();self.session.pending_ack['13']=sentinel
        with patch.object(m.time,'monotonic',return_value=100):
            with ThreadPoolExecutor(6) as pool: results=list(pool.map(lambda _:self.get_device(),range(12)))
        self.assertTrue(all(d['availability']=='unknown' for d in results))
        self.assertEqual(len(self.sock.frames),1);self.assertIs(self.session.pending_ack['13'],sentinel)
        self.assertEqual(m.try_parse_frame(self.sock.frames[0])[2:4],(0x12,self.session.pile_no_bcd+b'\x01'))
        with patch.object(m.time,'monotonic',return_value=109): self.get_device()
        self.assertEqual(len(self.sock.frames),1)
        with patch.object(m.time,'monotonic',return_value=110): self.get_device()
        self.assertEqual(len(self.sock.frames),2)
        self.telemetry(1);self.assertEqual(self.get_device()['availability'],'idle')
        self.assertEqual(len(self.sock.frames),2)
    def test_stale_cache_and_reconnect_data_require_query(self):
        with m.STORE._lock:
            t,d=m.STORE.latest[(fixture.PILE,'01')];m.STORE.latest[(fixture.PILE,'01')]=(t-100,d)
        self.assertEqual(self.get_device()['availability'],'unknown');self.assertEqual(len(self.sock.frames),1)
        self.telemetry(1)
        self.session.connected_at=(m.datetime.now()+m.timedelta(seconds=1)).isoformat()
        with patch.object(m.time,'monotonic',return_value=m.time.monotonic()+11): self.get_device()
        self.assertEqual(len(self.sock.frames),2)
    def test_fresh_and_offline_do_not_query(self):
        self.assertEqual(self.get_device()['availability'],'idle');self.assertEqual(len(self.sock.frames),0)
        m.SessionRegistry.sessions.clear()
        self.assertEqual(self.get_device()['availability'],'offline');self.assertEqual(len(self.sock.frames),0)
    def test_fast_reply_refreshes_and_send_failure_does_not_allow_start(self):
        self.missing()
        def reply(ftype,b): self.telemetry(1)
        with patch.object(self.session,'send_frame',side_effect=reply):
            self.assertEqual(self.get_device()['availability'],'idle')
        self.missing();self.session.customer_queries.clear();self.session.customer_query_last=None
        with patch.object(self.session,'send_frame',side_effect=OSError):
            self.assertEqual(self.get_device()['availability'],'unknown')
        with self.assertRaises(m.ApiError):
            self.app.customer_post('/api/customer/orders',{'pile_no':fixture.PILE,'gun_no':1},1)
        self.assertIsNone(self.app.active_user(1))
