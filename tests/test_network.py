"""Internet lamp: connected only when a connection out opens, not because a cable is plugged in."""

import socket
import unittest
from unittest import mock

from aio_system_dashboard.collectors import network

ROUTE = {"interface": "eth0", "gateway": "192.0.2.1"}


class InternetTest(unittest.TestCase):
    def setUp(self):
        self.server = socket.socket()
        self.server.bind(("127.0.0.1", 0))
        self.server.listen(4)
        self.addCleanup(self.server.close)
        self.open = f"127.0.0.1:{self.server.getsockname()[1]}"
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        self.closed = f"127.0.0.1:{closed.getsockname()[1]}"      # bound, not listening: refused
        closed.close()

    def check(self, targets, route=ROUTE):
        with mock.patch.object(network, "default_route", return_value=route):
            return network.internet(targets, timeout_s=1.0)

    def test_first_target_that_answers_wins(self):
        r = self.check([self.closed, self.open])
        self.assertEqual((r["reachable"], r["target"]), (True, self.open))

    def test_no_target_answers(self):
        r = self.check([self.closed])
        self.assertFalse(r["reachable"])
        self.assertIn(self.closed, r["error"])

    def test_a_link_without_a_default_route_is_not_internet(self):
        r = self.check([self.open], route=None)
        self.assertEqual((r["reachable"], r["error"]), (False, "no default route"))

    def test_bad_targets_and_none(self):
        self.assertFalse(self.check(["-x:443", "1.1.1.1:https"])["reachable"])
        self.assertIsNone(self.check([])["reachable"])


if __name__ == "__main__":
    unittest.main()
