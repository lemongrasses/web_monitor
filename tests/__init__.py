"""Tests never look at this computer's own hotspot service (it may be installed here)."""

from aio_system_dashboard.actions import hotspot_service

hotspot_service.SERVICE = hotspot_service.HotspotService(
    status_file="/nonexistent/aio-hotspot/status.json", config_file="/nonexistent/aio-hotspot/config.env",
    program="/nonexistent/aio-hotspot")
