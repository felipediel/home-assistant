"""Support for Broadlink devices."""
import asyncio
from functools import partial
import logging

import broadlink as blk
from broadlink.exceptions import (
    AuthenticationError,
    AuthorizationError,
    BroadlinkException,
    ConnectionClosedError,
    NetworkTimeoutError,
)

from homeassistant.const import (
    CONF_DEVICE_CLASS,
    CONF_HOST,
    CONF_MAC,
    CONF_NAME,
    CONF_TIMEOUT,
    CONF_TYPE,
)
from homeassistant.exceptions import ConfigEntryNotReady
from homeassistant.helpers import device_registry as dr

from .const import DEFAULT_PORT, DOMAIN, DOMAINS_AND_TYPES, LIBRARY_URL, SUPPORTED_TYPES
from .updater import get_update_manager

_LOGGER = logging.getLogger(__name__)


def get_domains(device_type):
    """Return the domains available for a device type."""
    return {domain for domain, types in DOMAINS_AND_TYPES if device_type in types}


class BroadlinkDevice:
    """Manages a Broadlink device."""

    def __init__(self, hass, config):
        """Initialize the device."""
        self.hass = hass
        self.config = config
        self.api = None
        self.update_manager = None
        self.fw_version = None
        self.authorized = None
        self.reset_jobs = []

    @property
    def name(self):
        """Return the name of the device."""
        return self.config.title

    @property
    def unique_id(self):
        """Return the unique id of the device."""
        return self.config.unique_id

    @staticmethod
    async def async_update(hass, entry):
        """Update the device and related entities.

        Triggered when the device is renamed on the frontend.
        """
        device_registry = await dr.async_get_registry(hass)
        device_entry = device_registry.async_get_device(
            {(DOMAIN, entry.unique_id)}, set()
        )
        device_registry.async_update_device(device_entry.id, name=entry.title)
        await hass.config_entries.async_reload(entry.entry_id)

    async def async_setup(self):
        """Set up the device and related entities."""
        config = self.config
        name = config.title
        host = (config.data[CONF_HOST], DEFAULT_PORT)
        mac_addr = config.data[CONF_MAC]
        dev_type = config.data[CONF_TYPE]
        timeout = config.data[CONF_TIMEOUT]
        cls_name = config.data.get(CONF_DEVICE_CLASS)

        if cls_name:
            _LOGGER.warning(
                "You have configured an unknown device: %s (%s at %s). "
                "If you manage to make it work, please consider opening "
                "an issue or creating a pull request at %s",
                name,
                hex(dev_type),
                host[0],
                LIBRARY_URL,
            )
            device_class = SUPPORTED_TYPES[cls_name]
            api = device_class(
                host,
                mac_addr,
                dev_type,
                timeout=timeout,
                name=name,
                model=cls_name,
                manufacturer="Unknown",
            )
        else:
            api = blk.gendevice(
                dev_type,
                host,
                mac_addr,
                name=name,
            )
            api.timeout = timeout

        try:
            await self.hass.async_add_executor_job(api.auth)

        except AuthenticationError:
            await self._async_handle_auth_error()
            return False

        except (NetworkTimeoutError, OSError) as err:
            raise ConfigEntryNotReady from err

        except BroadlinkException as err:
            _LOGGER.error(
                "Failed to authenticate to the device at %s: %s", api.host[0], err
            )
            return False

        self.api = api
        self.authorized = True

        update_manager = get_update_manager(self)
        coordinator = update_manager.coordinator
        await coordinator.async_refresh()
        if not coordinator.last_update_success:
            raise ConfigEntryNotReady()

        self.update_manager = update_manager
        self.hass.data[DOMAIN].devices[config.entry_id] = self
        self.reset_jobs.append(config.add_update_listener(self.async_update))

        try:
            self.fw_version = await self.hass.async_add_executor_job(api.get_fwversion)
        except (BroadlinkException, OSError):
            pass

        # Forward entry setup to related domains.
        tasks = (
            self.hass.config_entries.async_forward_entry_setup(config, domain)
            for domain in get_domains(self.api.type)
        )
        for entry_setup in tasks:
            self.hass.async_create_task(entry_setup)

        return True

    async def async_unload(self):
        """Unload the device and related entities."""
        if self.update_manager is None:
            return True

        while self.reset_jobs:
            self.reset_jobs.pop()()

        tasks = (
            self.hass.config_entries.async_forward_entry_unload(self.config, domain)
            for domain in get_domains(self.api.type)
        )
        results = await asyncio.gather(*tasks)
        return all(results)

    async def async_auth(self):
        """Authenticate to the device."""
        try:
            await self.hass.async_add_executor_job(self.api.auth)
        except (BroadlinkException, OSError) as err:
            _LOGGER.debug(
                "Failed to authenticate to the device at %s: %s", self.api.host[0], err
            )
            if isinstance(err, AuthenticationError):
                await self._async_handle_auth_error()
            return False
        return True

    async def async_request(self, function, *args, **kwargs):
        """Send a request to the device."""
        request = partial(function, *args, **kwargs)
        try:
            return await self.hass.async_add_executor_job(request)
        except (AuthorizationError, ConnectionClosedError):
            if not await self.async_auth():
                raise
            return await self.hass.async_add_executor_job(request)

    async def _async_handle_auth_error(self):
        """Handle an authentication error."""
        if self.authorized is False:
            return

        self.authorized = False

        _LOGGER.error(
            "The device at %s is locked for authentication. Follow the configuration flow to unlock it",
            self.config.data[CONF_HOST],
        )

        self.hass.async_create_task(
            self.hass.config_entries.flow.async_init(
                DOMAIN,
                context={"source": "reauth"},
                data={CONF_NAME: self.name, **self.config.data},
            )
        )
