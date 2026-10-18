import unittest

from common.constants import is_domain_dependent
from guests.domain_ops import password_trips_ad_complexity as trips


class DomainDependentClassification(unittest.TestCase):

    def test_ldap_signing_off_is_dc_only(self):
        # Regression: seeds the DC's NTDS LDAP-signing params, so it fails rc=1
        # in the pre-promotion pre-domain pass ("NTDS Parameters key not found") unless
        # classified domain-dependent and planted in the final stage.
        self.assertTrue(is_domain_dependent("ldap-signing-off-win"))



class ComplexityTrip(unittest.TestCase):
    def test_packet_color_accounts_trip(self):
        self.assertTrue(trips("Red", "Red123!", "Red Team"))
        self.assertTrue(trips("Purple", "Purple123!", "Purple Team"))

