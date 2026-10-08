"""Optional modules: features not every machine needs (research, post-processing, conveniences).

Each module is its own package next to ``aio_system_dashboard`` and is copied to a machine only
when the installer is asked for it (``--with-<name>``), so a product machine does not carry it.
The core never imports a module directly; it only asks here whether one is present and switched
on (``modules.<name>.enabled`` in the config, default on when installed).

A module package provides:
  NAME, TITLE                 short id and the label of its Maintenance page
  DEFAULTS                    its settings (merged under ``modules.<name>`` in the config)
  register(ctx, app)          add its routes and page to the Maintenance app
"""

import copy
import importlib
import importlib.util
import logging
from typing import Dict, List

logger = logging.getLogger(__name__)

KNOWN: Dict[str, str] = {
    "replay": "aio_dashboard_replay",      # Live / Bag replay switch and the bag player
}


def installed(name: str) -> bool:
    pkg = KNOWN.get(name)
    if not pkg:
        return False
    try:
        return importlib.util.find_spec(pkg) is not None
    except (ImportError, ValueError):
        return False


def enabled(cfg_data: Dict, name: str) -> bool:
    settings = (cfg_data.get("modules") or {}).get(name) or {}
    return settings.get("enabled", True) is not False and installed(name)


def settings(cfg_data: Dict, name: str, defaults: Dict) -> Dict:
    """The module's settings: its DEFAULTS with modules.<name> from the config on top."""
    out = copy.deepcopy(defaults)
    out.update((cfg_data.get("modules") or {}).get(name) or {})
    return out


def load(cfg) -> List:
    """Import the modules that are installed and switched on."""
    out = []
    for name, pkg in KNOWN.items():
        if not enabled(cfg.data, name):
            continue
        try:
            out.append(importlib.import_module(pkg))
            logger.info("module %s loaded", name)
        except Exception:                    # a broken module must not take the dashboard down
            logger.exception("module %s could not be loaded", name)
    return out
