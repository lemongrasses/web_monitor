import unittest

from aio_system_dashboard.web.access import client_allowed, parse_allowed_clients


class AccessTest(unittest.TestCase):
    def test_empty_allows_all(self):
        self.assertTrue(client_allowed("198.51.100.3", parse_allowed_clients([])))

    def test_allowlist(self):
        nets = parse_allowed_clients(["192.0.2.154", "198.51.100.0/24"])
        self.assertTrue(client_allowed("192.0.2.154", nets))
        self.assertTrue(client_allowed("198.51.100.7", nets))
        self.assertTrue(client_allowed("127.0.0.1", nets))
        self.assertFalse(client_allowed("192.0.2.155", nets))
        self.assertFalse(client_allowed("203.0.113.91", nets))

    def test_bad_entry_skipped(self):
        self.assertEqual(len(parse_allowed_clients(["nonsense", "1.2.3.4"])), 1)
