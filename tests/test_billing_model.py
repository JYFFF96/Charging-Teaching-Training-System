import struct
import unittest
from types import SimpleNamespace
import test_customer_charging as fixture
m = fixture.m

class BillingModel(unittest.TestCase):
    def test_wire_rates_and_every_half_hour(self):
        b = m.build_billing_model_body(m.str_to_bcd(fixture.PILE, 7))
        self.assertEqual(b[7:9], m.str_to_bcd('0101', 2))
        self.assertEqual(struct.unpack('<8I', b[9:41]),
                         (120000,40000,100000,40000,80000,40000,40000,40000))
        expected=[]
        for slot in range(48):
            h=slot/2
            expected.append(3 if h<7 or h>=23 else 0 if 18<=h<21 else 1 if 10<=h<12 or 14<=h<18 else 2)
        self.assertEqual(len(b),90)
        self.assertEqual(list(b[42:]),expected)

    def test_old_model_rejected_current_model_accepted(self):
        replies=[]
        session=SimpleNamespace(pile_no=fixture.PILE,pile_no_bcd=m.str_to_bcd(fixture.PILE,7),
                                reply=lambda seq,ftype,b:replies.append((ftype,b)))
        for model,result in [('0100',1),('0101',0)]:
            m.PileSession.h_05(session,1,session.pile_no_bcd+m.str_to_bcd(model,2))
            self.assertEqual(replies[-1][0],0x06)
            self.assertEqual(replies[-1][1][-1],result)
