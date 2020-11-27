"""Local watchdog for Broadlink devices."""
from datetime import timedelta
from itertools import chain
import logging
import socket

import broadlink as blk

from homeassistant.const import CONF_HOST
from homeassistant.core import callback
from homeassistant.helpers import debounce
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator

from .const import DOMAIN
from .helpers import get_ip_or_none

_LOGGER = logging.getLogger(__name__)


class BroadlinkWatchdog:
    """Manages a local watchdog."""

    def __init__(self, hass):
        """Initialize the entity."""
        self.hass = hass
        self.coordinator = DataUpdateCoordinator(
            hass,
            _LOGGER,
            name="watchdog",
            update_method=self.async_keep_alive,
            update_interval=timedelta(minutes=2),
            request_refresh_debouncer=debounce.Debouncer(
                hass, _LOGGER, cooldown=30, immediate=True
            ),
        )
        self._unsubscribe = None

    async def async_setup(self):
        """Set up the watchdog."""
        if self._unsubscribe is None:
            self._unsubscribe = self.coordinator.async_add_listener(self.update)
            await self.coordinator.async_refresh()

    async def async_unload(self):
        """Unload the watchdog."""
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None

    async def async_keep_alive(self):
        """Send packets to keep Broadlink devices awake."""
        hass = self.hass
        broadcast_addrs = hass.data[DOMAIN].config["broadcast_addrs"]
        current_entries = hass.config_entries.async_entries(DOMAIN)

        await hass.async_add_executor_job(
            self.keep_alive, broadcast_addrs, current_entries
        )

    def keep_alive(self, broadcast_addrs, current_entries):
        """Send packets to keep Broadlink devices awake."""
        hosts = {get_ip_or_none(entry.data[CONF_HOST]) for entry in current_entries}
        networks = {socket.inet_aton(bd_addr)[:3] for bd_addr in broadcast_addrs}
        uncovered_hosts = {
            host
            for host in hosts
            if host and socket.inet_aton(host)[:3] not in networks
        }

        for addr in chain(broadcast_addrs, uncovered_hosts):
            try:
                blk.keep_alive(addr)
            except OSError as err:
                _LOGGER.debug("Failed to send watchdog packet to %s: %s", addr, err)
            else:
                _LOGGER.debug("Watchdog packet sent to: %s", addr)

    @callback
    def update(self):
        """Listen for updates.

        This method is only used to activate the update coordinator.
        We do not need a listener because we do not wait for responses.
        """
