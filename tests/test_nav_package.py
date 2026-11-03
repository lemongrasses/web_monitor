"""One dashboard for either navigation package: aio_nav_ros (aio-nav-ros) or aio_core_nav.

Both have <root>/install/<pkg>/share/<pkg>/config/aio_nav.yaml and <root>/install/<pkg>/lib/<pkg>/;
the package is read from the config path, the launcher comes from the package when it has one
(aio_nav_ros's aio-nav) and from the dashboard's deploy/aio-nav otherwise (aio_core_nav)."""
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from aio_system_dashboard import config as config_mod
from aio_system_dashboard.config import (NAV_PACKAGES, aio_nav_output_dir, discover_aio_nav_config,
                                         load_config, nav_package_of, read_aio_nav_params)

LAYOUTS = {"aio_nav_ros": "aio-nav-ros", "aio_core_nav": "aio_core_nav"}   # package -> clone folder


def make_install(home: str, pkg: str, with_wrapper: bool, rate: float = 50.0) -> str:
    root = os.path.join(home, "jetson", LAYOUTS[pkg])
    prefix = os.path.join(root, "install", pkg)
    cfg_dir = os.path.join(prefix, "share", pkg, "config")
    lib = os.path.join(prefix, "lib", pkg)
    os.makedirs(cfg_dir)
    os.makedirs(lib)
    yaml = os.path.join(cfg_dir, "aio_nav.yaml")
    with open(yaml, "w") as f:
        f.write("aio_nav_node:\n  ros__parameters:\n    output_udp: '192.0.2.5:9000'\n"
                f"    output_rate: {rate}\n")
    Path(lib, "aio_nav_node").write_text("#!/bin/sh\n")
    os.chmod(Path(lib, "aio_nav_node"), 0o755)
    if with_wrapper:
        Path(lib, "aio-nav").write_text("#!/bin/sh\n")
        os.chmod(Path(lib, "aio-nav"), 0o755)
    return yaml


class PackageOfTest(unittest.TestCase):
    def test_from_share_folder(self):
        for pkg in NAV_PACKAGES:
            self.assertEqual(nav_package_of(f"/x/install/{pkg}/share/{pkg}/config/aio_nav.yaml"), pkg)
        self.assertEqual(nav_package_of("/x/.local/opt/aio-nav-ros/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml"),
                         "aio_nav_ros")

    def test_from_source_tree(self):      # a symlink install resolves into src/<pkg>/config
        self.assertEqual(nav_package_of("/x/aio_core_nav/src/aio_core_nav/config/aio_nav.yaml"), "aio_core_nav")
        self.assertEqual(nav_package_of("/x/aio-nav-ros/src/aio_nav_ros/config/aio_nav.yaml"), "aio_nav_ros")

    def test_unknown(self):
        self.assertEqual(nav_package_of(""), "")
        self.assertEqual(nav_package_of("/etc/other/aio_nav.yaml"), "")

    def test_output_dir_release_prefix(self):
        self.assertEqual(aio_nav_output_dir("/p/.local/opt/aio-nav-ros/aio_nav_ros/share/aio_nav_ros/config/aio_nav.yaml"),
                         "/p/.local/opt/aio-nav-ros/output")
        self.assertEqual(aio_nav_output_dir("/p/.local/opt/aio_core_nav/aio_core_nav/share/aio_core_nav/config/aio_nav.yaml"),
                         "/p/.local/opt/aio_core_nav/output")


class EitherPackageTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.home = os.path.join(self.tmp.name, "home")
        templates = ("/home/*/*/install/{pkg}/share/{pkg}/config/aio_nav.yaml",)
        globs = tuple(self.tmp.name + t.format(pkg=p) for p in NAV_PACKAGES for t in templates)
        self.patches = [mock.patch.object(config_mod, "AIO_NAV_CONFIG_GLOBS", globs),
                        mock.patch.dict(os.environ, {"AIO_NAV_CONFIG": ""})]
        for p in self.patches:
            p.start()

    def tearDown(self):
        for p in self.patches:
            p.stop()
        self.tmp.cleanup()

    def load(self, **nav):
        cfg = load_config("config/dashboard.yaml")
        cfg.data["nav"].update(nav)
        cfg.refresh_aio_nav()
        return cfg

    def test_aio_nav_ros_uses_its_own_wrapper(self):
        yaml = make_install(self.home, "aio_nav_ros", with_wrapper=True)
        cfg = self.load()
        self.assertEqual(cfg.aio_nav["path"], yaml)
        self.assertEqual(cfg.nav_package(), "aio_nav_ros")
        self.assertEqual(cfg.launcher("aio-nav"), str(Path(yaml).parents[3] / "lib" / "aio_nav_ros" / "aio-nav"))
        self.assertEqual(cfg["data"]["roots"][0]["path"],
                         os.path.join(self.home, "jetson", "aio-nav-ros", "output"))
        self.assertEqual(cfg.ros_mode_info()["package"], "aio_nav_ros")

    def test_aio_core_nav_uses_the_dashboard_wrapper(self):
        yaml = make_install(self.home, "aio_core_nav", with_wrapper=False)
        cfg = self.load()
        self.assertEqual(cfg.aio_nav["path"], yaml)
        self.assertEqual(cfg.nav_package(), "aio_core_nav")
        launcher = cfg.launcher("aio-nav")
        self.assertIsNotNone(launcher)
        self.assertEqual(Path(launcher).name, "aio-nav")
        self.assertEqual(Path(launcher).parent.name, "deploy")   # the dashboard's own, not the package's
        self.assertIsNone(cfg.launcher("aio-nav-dso"))           # DSO wrapper: only aio_nav_ros has it
        self.assertEqual(cfg["data"]["roots"][0]["path"],
                         os.path.join(self.home, "jetson", "aio_core_nav", "output"))

    def test_process_pattern_matches_both(self):
        import re
        pattern = load_config("config/dashboard.yaml")["services"]["aio_nav"]["process_pattern"]
        for pkg in NAV_PACKAGES:
            self.assertTrue(re.search(pattern, f"/home/j/x/install/{pkg}/lib/{pkg}/aio_nav_node --ros-args"), pkg)
        self.assertFalse(re.search(pattern, "/usr/bin/python3 aio_nav_node_helper"))

    def test_both_installed_newest_wins_unless_package_is_set(self):
        ros = make_install(self.home, "aio_nav_ros", with_wrapper=True, rate=20.0)
        core = make_install(self.home, "aio_core_nav", with_wrapper=False, rate=100.0)
        os.utime(ros, (1, 1))                                     # aio_core_nav is the newer install
        self.assertEqual(discover_aio_nav_config(), core)
        self.assertEqual(discover_aio_nav_config("aio_nav_ros"), ros)
        self.assertEqual(discover_aio_nav_config("aio_core_nav"), core)
        cfg = self.load(package="aio_nav_ros")
        self.assertEqual(cfg.aio_nav["path"], ros)
        self.assertEqual(cfg.expected_nav_rate, 20.0)
        self.assertEqual(cfg.nav_package(), "aio_nav_ros")
        cfg = self.load(package="auto")
        self.assertEqual(cfg.nav_package(), "aio_core_nav")
        self.assertEqual(cfg.expected_nav_rate, 100.0)

    def test_package_set_but_not_installed(self):
        make_install(self.home, "aio_core_nav", with_wrapper=False)
        cfg = self.load(package="aio_nav_ros")
        self.assertFalse(cfg.aio_nav["loaded"])
        self.assertIn("aio_nav_ros", cfg.aio_nav.get("error", ""))
        self.assertEqual(read_aio_nav_params("auto", "aio_nav_ros")["path"], "")


if __name__ == "__main__":
    unittest.main()
