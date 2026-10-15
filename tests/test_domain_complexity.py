import unittest

from common.constants import is_domain_dependent
from guests.domain_ops import password_trips_ad_complexity as trips


class DomainDependentClassification(unittest.TestCase):
    def test_ad_prefix_and_explicit_set(self):
        self.assertTrue(is_domain_dependent("ad-dns-localhost"))
        self.assertTrue(is_domain_dependent("gpo-persistence-win"))
        self.assertTrue(is_domain_dependent("gpp-cpassword-win"))

    def test_ldap_signing_off_is_dc_only(self):
        # Regression 2026-10-13: seeds the DC's NTDS LDAP-signing params, so it fails rc=1
        # in the pre-promotion pre-domain pass ("NTDS Parameters key not found") unless
        # classified domain-dependent and planted in the final stage.
        self.assertTrue(is_domain_dependent("ldap-signing-off-win"))

    def test_ordinary_configs_are_not_domain_dependent(self):
        self.assertFalse(is_domain_dependent("nginx"))
        self.assertFalse(is_domain_dependent("suid-find"))


class ComplexityTrip(unittest.TestCase):
    def test_packet_color_accounts_trip(self):
        self.assertTrue(trips("Red", "Red123!", "Red Team"))
        self.assertTrue(trips("Purple", "Purple123!", "Purple Team"))

    def test_clean_password_passes(self):
        self.assertFalse(trips("blueteam", "n0t_sus!", "Blue Team Lead"))
        self.assertFalse(trips("svc-support", "Xk9#mQ2vLp", "IT Support"))
