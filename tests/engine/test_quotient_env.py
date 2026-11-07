"""The Quotient DSN host must be the compose service name, not the database name.

`push_quotient_env` writes /opt/quotient/.env before `compose up`. The services are reachable on the
compose network by SERVICE NAME (`db`) and by container name (`quotient-database`); the underscored
database name is neither, so `POSTGRES_HOST=quotient_database` made quotient-server crash-loop:

    Failed to connect database! postgres://engineuser:***@quotient_database:5432/engine

and the deploy failed its last phase waiting for a web server that never bound :80
(run-d44ef121). The service name is the durable alias, so that is what these assert.
"""

import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

REPO = Path(__file__).resolve().parents[2]


class QuotientEnvTest(unittest.TestCase):
    def _env_writers(self):
        for rel in ("engine/engine_bootstrap_ops.py", "engine/engine_event_ops.py"):
            yield rel, (REPO / rel).read_text(encoding="utf-8")

    def test_postgres_host_is_the_compose_service_name(self):
        for rel, text in self._env_writers():
            hosts = re.findall(r"POSTGRES_HOST=([^\\\"]+)", text)
            self.assertTrue(hosts, f"{rel} no longer writes POSTGRES_HOST — check the env push")
            for host in hosts:
                self.assertEqual(
                    host, "db",
                    f"{rel} sets POSTGRES_HOST={host!r}; the compose service is `db` and that is "
                    f"the only alias guaranteed on the network (an underscored database name does "
                    f"not resolve, which crash-loops quotient-server)")

    def test_no_underscored_database_host_survives(self):
        for rel, text in self._env_writers():
            self.assertNotIn("quotient_database", text,
                             f"{rel} still references the unresolvable quotient_database alias")
