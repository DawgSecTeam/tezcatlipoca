"""A blue restore is only useful to red if it says WHICH box came back: scoreboard names are
`<box>-<label>`, and the box's ip is what the rotation logic keys on (live: scrim-one, five
restores seen and zero restore-reactions because every event carried ip=None)."""

import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO))

from tlaloc.sensors import Sensors


class _Intel:
    def boxes(self):
        return [{"box": "web01", "ip": "192.168.1.10", "team": "team1"},
                {"box": "db01", "ip": "192.168.1.11", "team": "team1"}]


class _World:
    def __init__(self):
        self.events = []

    def add_blue_event(self, team, service, ip=None):
        self.events.append({"team": team, "service": service, "ip": ip})


class _Client:
    def __init__(self, maps):
        self.maps = list(maps)

    def team_service_map(self):
        return self.maps.pop(0) if len(self.maps) > 1 else self.maps[0]


def _sensors(world, maps):
    s = Sensors({"sensors": {"quotient_enabled": True}}, _Intel(), world=world,
                client=_Client(maps))
    return s


def test_a_restore_is_attributed_to_its_box():
    world = _World()
    sensors = _sensors(world, [{"team1": {"web01-http": False}},
                               {"team1": {"web01-http": True}}])
    sensors.observe()
    sensors.observe()
    assert world.events == [{"team": "team1", "service": "web01-http",
                             "ip": "192.168.1.10"}]


def test_an_unknown_box_name_is_left_unattributed():
    assert _sensors(_World(), [{"team1": {}}])._ip_for("team1", "fw01-icmp") is None
