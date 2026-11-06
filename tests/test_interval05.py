import math
import unittest
from pmos.pacing import Policy, PolicyError, wait_seconds
from pmos.province import Province

class Interval05Tests(unittest.TestCase):
    def test_boundary_and_shared_lanes(self):
        p=Policy.from_dict({'response_interval_seconds':0.5})
        for lane in (None,'native','report','keepalive'):
            self.assertAlmostEqual(wait_seconds(p,100.49,100,lane=lane),0.01)
            self.assertEqual(wait_seconds(p,100.5,100,lane=lane),0)
    def test_invalid_intervals_still_refused(self):
        for v in (0,0.19,-1,float('nan'),float('inf'),True,'0.5'):
            with self.subTest(v=v), self.assertRaises(PolicyError):
                Policy.from_dict({'response_interval_seconds':v})
    def test_lane_bounds_and_caps_remain(self):
        p=Policy.from_dict({'response_interval_seconds':0.5,'lane_intervals':{'slow':15},'max_per_hour':1})
        self.assertEqual(wait_seconds(p,100.5,100,lane='slow'),14.5)
        self.assertEqual(wait_seconds(p,100.5,100,[100]),3600.5)
        with self.assertRaises(PolicyError):Policy.from_dict({'lane_intervals':{'bad':0.19}})
    def test_live_config_and_keeper_unchanged(self):
        p=Province.load('liaoning').policy
        self.assertEqual(p.response_interval_seconds,0.5)
        self.assertEqual(p.renew_after_idle_seconds,300)
        self.assertEqual(p.request_timeout_seconds,45)
        self.assertEqual(p.min_free_disk_mb,2048)
        self.assertEqual(p.quiet_windows,('23:58-00:10',))
        self.assertIsNone(p.max_per_hour);self.assertIsNone(p.max_per_day)
