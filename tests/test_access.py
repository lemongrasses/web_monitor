import copy
import unittest
from types import SimpleNamespace
from unittest import mock

from aio_system_dashboard import config
from aio_system_dashboard.config import DEFAULTS, Config, security_enabled
from aio_system_dashboard.web.access import client_allowed, parse_allowed_clients
from aio_system_dashboard.web.auth import Guard
from aio_system_dashboard.web.common import create_base_app


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


def cfg_with(**access):
    data = copy.deepcopy(DEFAULTS)
    data["access"].update(access)
    return Config(data, None)


class SecuritySwitchTest(unittest.TestCase):
    """The master switch of the password and the client list (a development machine: install.sh --dev)."""

    def options(self, *words):
        return mock.patch.object(config, "installed_options", return_value=set(words))

    def test_on_unless_the_installer_switched_it_off(self):
        with self.options():                                   # a source checkout: no options file
            self.assertTrue(security_enabled(cfg_with()))
        with self.options("sysmon", "wifi", "security"):
            self.assertTrue(security_enabled(cfg_with()))
        with self.options("sysmon", "no-boot", "no-security"):
            self.assertFalse(security_enabled(cfg_with()))

    def test_the_config_decides_when_it_says_so(self):
        with self.options("no-security"):
            self.assertTrue(security_enabled(cfg_with(security=True)))
        with self.options():
            self.assertFalse(security_enabled(cfg_with(security=False)))

    def app(self, cfg):
        return create_base_app("t", SimpleNamespace(cfg=cfg)).test_client()

    def test_client_list_is_kept_but_not_applied_when_off(self):
        on = cfg_with(allowed_clients=["192.0.2.0/24"], security=True)
        off = cfg_with(allowed_clients=["192.0.2.0/24"], security=False)
        outside = {"REMOTE_ADDR": "203.0.113.9"}
        self.assertEqual(self.app(on).get("/nothing", environ_base=outside).status_code, 403)
        self.assertEqual(self.app(off).get("/nothing", environ_base=outside).status_code, 404)   # let in
        self.assertEqual(off["access"]["allowed_clients"], ["192.0.2.0/24"])

    def test_guard_switched_off_asks_for_nothing(self):
        guard = Guard("/nonexistent/maint_password", enabled=False)
        self.assertTrue(guard.unlocked())
        self.assertEqual(guard.required(lambda: "done")(), "done")
        self.assertEqual(guard.status()["disabled"], True)
        self.assertTrue(guard.status()["unlocked"])

