import unittest

from pmos.pacing import Policy, PolicyError, wait_seconds


class Interval02Tests(unittest.TestCase):
    def test_fractional_interval_is_shared_from_response_end(self):
        policy = Policy.from_dict({"response_interval_seconds": 0.2})
        for lane in (None, "native", "report", "keepalive"):
            with self.subTest(lane=lane):
                self.assertAlmostEqual(wait_seconds(policy, 100.19, 100, lane=lane), 0.01)
                self.assertEqual(wait_seconds(policy, 100.2, 100, lane=lane), 0)

    def test_invalid_intervals_and_caps_remain_enforced(self):
        for value in (0, 0.19, -1, True, "0.2", float("nan"), float("inf")):
            with self.subTest(value=value), self.assertRaises(PolicyError):
                Policy.from_dict({"response_interval_seconds": value})
        with self.assertRaises(PolicyError):
            Policy.from_dict({"lane_intervals": {"invalid": 0.19}})
        policy = Policy.from_dict({"response_interval_seconds": 0.2,
                                   "lane_intervals": {"slow": 15}, "max_per_hour": 1})
        self.assertAlmostEqual(wait_seconds(policy, 100.2, 100, lane="slow"), 14.8)
        self.assertAlmostEqual(wait_seconds(policy, 100.2, 100, [100]), 3600.8)


if __name__ == "__main__":
    unittest.main()
