import unittest

from domain_ops import password_trips_ad_complexity as trips


class ComplexityTrip(unittest.TestCase):
    def test_packet_color_accounts_trip(self):
        self.assertTrue(trips("Red", "Red123!", "Red Team"))
        self.assertTrue(trips("Purple", "Purple123!", "Purple Team"))

    def test_clean_password_passes(self):
        self.assertFalse(trips("blueteam", "n0t_sus!", "Blue Team Lead"))
        self.assertFalse(trips("svc-support", "Xk9#mQ2vLp", "IT Support"))
