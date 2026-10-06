import copy
import unittest

from aio_system_dashboard.actions.registry import ActionError, ActionRegistry
from aio_system_dashboard.config import DEFAULTS, Config


class FakeEvents:
    def add(self, *a, **k):
        pass


def make(controllable=True):
    data = copy.deepcopy(DEFAULTS)
    data["fake"]["enabled"] = True
    data["nav"]["aio_nav_config"] = ""
    data["services"] = {
        "aio_nav": {"label": "AIO NAV", "unit": "aio-nav.service", "controllable": controllable},
        "camera": {"label": "Camera", "unit": "camera.service", "restartable": True},
    }
    return Config(data, None)


class ControlActionsTest(unittest.TestCase):
    def test_targets_are_whitelisted(self):
        reg = ActionRegistry(make(), {}, FakeEvents(), lambda d: {})
        self.assertEqual(set(reg.actions["start_service"].targets), {"aio_nav"})
        self.assertEqual(set(reg.actions["stop_service"].targets), {"aio_nav"})
        self.assertEqual(set(reg.actions["restart_service"].targets), {"aio_nav", "camera"})
        self.assertTrue(reg.actions["stop_service"].confirm)
        self.assertFalse(reg.actions["start_service"].confirm)

    def test_uncontrollable_service_rejected(self):
        reg = ActionRegistry(make(controllable=False), {}, FakeEvents(), lambda d: {})
        with self.assertRaises(ActionError):
            reg.run("stop_service", "aio_nav")
        with self.assertRaises(ActionError):
            reg.run("stop_service", "camera")


if __name__ == "__main__":
    unittest.main()
