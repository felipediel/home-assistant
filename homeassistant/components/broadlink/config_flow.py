"""Config flow for Broadlink devices."""
import errno
from functools import partial
import logging
import socket

import broadlink as blk
from broadlink.exceptions import (
    AuthenticationError,
    BroadlinkException,
    NetworkTimeoutError,
)
import psutil
import voluptuous as vol

from homeassistant import config_entries, data_entry_flow
from homeassistant.const import CONF_HOST, CONF_MAC, CONF_NAME, CONF_TIMEOUT, CONF_TYPE
from homeassistant.helpers import config_validation as cv

from .const import (  # pylint: disable=unused-import
    CONF_LOCK,
    DEFAULT_PORT,
    DEFAULT_TIMEOUT,
    DOMAIN,
    DOMAINS_AND_TYPES,
)
from .helpers import format_mac, get_broadcast_addrs, is_broadcast_addr

_LOGGER = logging.getLogger(__name__)


class BroadlinkFlowHandler(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a Broadlink config flow."""

    CONNECTION_CLASS = config_entries.CONN_CLASS_LOCAL_POLL
    VERSION = 1

    def __init__(self):
        """Initialize the Broadlink flow."""
        self.device = None

    async def async_set_device(self, device, raise_on_progress=True):
        """Define a device for the config flow."""
        supported_types = {
            device_type
            for device_types in DOMAINS_AND_TYPES
            for device_type in device_types[1]
        }
        if device.type not in supported_types:
            _LOGGER.error(
                "Unsupported device: %s. If it worked before, please open "
                "an issue at https://github.com/home-assistant/core/issues",
                hex(device.devtype),
            )
            raise data_entry_flow.AbortFlow("not_supported")

        await self.async_set_unique_id(
            device.mac.hex(), raise_on_progress=raise_on_progress
        )
        self.device = device

        # pylint: disable=no-member # https://github.com/PyCQA/pylint/issues/3167
        self.context["title_placeholders"] = {
            "name": device.name,
            "model": device.model,
            "host": device.host[0],
        }

    async def async_step_user(self, user_input=None):
        """Handle a flow initiated by the user."""
        errors = {}

        if user_input is not None:
            host = user_input.get(CONF_HOST)
            timeout = user_input.get(CONF_TIMEOUT, DEFAULT_TIMEOUT)

            if not host or is_broadcast_addr(host):
                return await self.async_step_discover(
                    {CONF_HOST: host, CONF_TIMEOUT: timeout}
                )

            try:
                hello = partial(blk.hello, host, DEFAULT_PORT, timeout)
                device = await self.hass.async_add_executor_job(hello)

            except NetworkTimeoutError:
                errors["base"] = "cannot_connect"
                err_msg = "Device not found"

            except OSError as err:
                if err.errno in {errno.EINVAL, socket.EAI_NONAME}:
                    errors["base"] = "invalid_host"
                    err_msg = "Invalid hostname or IP address"
                elif err.errno == errno.ENETUNREACH:
                    errors["base"] = "cannot_connect"
                    err_msg = str(err)
                else:
                    errors["base"] = "unknown"
                    err_msg = str(err)

            else:
                device.timeout = timeout

                if self.source != "reauth":
                    await self.async_set_device(device)
                    self._abort_if_unique_id_configured(
                        updates={CONF_HOST: device.host[0], CONF_TIMEOUT: timeout}
                    )
                    return await self.async_step_auth()

                if device.mac == self.device.mac:
                    await self.async_set_device(device, raise_on_progress=False)
                    return await self.async_step_auth()

                errors["base"] = "invalid_host"
                err_msg = (
                    "This is not the device you are looking for. The MAC "
                    f"address must be {format_mac(self.device.mac)}"
                )

            _LOGGER.error("Failed to connect to the device at %s: %s", host, err_msg)

            if self.source in {
                config_entries.SOURCE_IMPORT,
                config_entries.SOURCE_INTEGRATION_DISCOVERY,
            }:
                return self.async_abort(reason=errors["base"])

        data_schema = {
            vol.Optional(CONF_HOST): str,
            vol.Optional(CONF_TIMEOUT, default=DEFAULT_TIMEOUT): cv.positive_int,
        }
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(data_schema),
            errors=errors,
        )

    async def async_step_discover(self, user_input=None):
        """Discover devices on the given networks.

        If the host is empty or 255.255.255.255, discover devices on all
        available networks.
        """
        host = user_input.get(CONF_HOST)
        timeout = user_input.get(CONF_TIMEOUT)
        errors = {}

        if not host or host == "255.255.255.255":
            nics = await self.hass.async_add_executor_job(psutil.net_if_addrs)
            broadcast_addrs = get_broadcast_addrs(nics)

        elif is_broadcast_addr(host):
            broadcast_addrs = [host]

        else:
            return await self.async_step_user(user_input={CONF_HOST: host})

        devices = []
        already_configured = self._async_current_ids(False)
        in_progress = [
            progress["context"].get("unique_id")
            for progress in self._async_in_progress()
        ]

        for addr in broadcast_addrs:
            discover = partial(blk.discover, discover_ip_address=addr, timeout=timeout)
            try:
                new_devices = await self.hass.async_add_executor_job(discover)

            except OSError as err:
                if err.errno == errno.ENETUNREACH:
                    reason = "cannot_connect"
                    err_msg = str(err)
                else:
                    reason = "unknown"
                    err_msg = str(err)

            else:
                new_devices = [
                    device
                    for device in new_devices
                    if device.mac.hex() not in already_configured
                    and device.mac.hex() not in in_progress
                ]
                devices.extend(new_devices)

        if not devices:
            if not errors:
                reason = "no_devices_found"
                err_msg = "No devices found"

            _LOGGER.error("Failed to discover devices: %s", err_msg)
            return self.async_abort(reason=reason)

        if len(devices) == 1:
            return await self.async_step_user(
                user_input={CONF_HOST: devices[0].host[0]}
            )

        if errors:
            _LOGGER.debug("Error during device discovery: %s", err_msg)

        data_schema = {
            vol.Required(CONF_HOST): vol.In(
                {device.host[0]: str(device) for device in devices}
            ),
        }
        return self.async_show_form(
            step_id="discover",
            data_schema=vol.Schema(data_schema),
            errors=errors,
            description_placeholders={
                "num_devices": len(devices),
            },
        )

    async def async_step_auth(self):
        """Authenticate to the device."""
        device = self.device
        errors = {}

        try:
            await self.hass.async_add_executor_job(device.auth)

        except AuthenticationError:
            errors["base"] = "invalid_auth"
            await self.async_set_unique_id(device.mac.hex())
            return await self.async_step_reset(errors=errors)

        except NetworkTimeoutError as err:
            errors["base"] = "cannot_connect"
            err_msg = str(err)

        except BroadlinkException as err:
            errors["base"] = "unknown"
            err_msg = str(err)

        except OSError as err:
            if err.errno == errno.ENETUNREACH:
                errors["base"] = "cannot_connect"
                err_msg = str(err)
            else:
                errors["base"] = "unknown"
                err_msg = str(err)

        else:
            await self.async_set_unique_id(device.mac.hex())
            if self.source in {
                config_entries.SOURCE_IMPORT,
                config_entries.SOURCE_INTEGRATION_DISCOVERY,
            }:
                _LOGGER.warning(
                    "%s (%s at %s) is ready to be configured. Click "
                    "Configuration in the sidebar, click Integrations and "
                    "click Configure on the device to complete the setup",
                    device.name,
                    device.model,
                    device.host[0],
                )

            if device.is_locked:
                return await self.async_step_unlock()
            return await self.async_step_finish()

        await self.async_set_unique_id(device.mac.hex())
        _LOGGER.error(
            "Failed to authenticate to the device at %s: %s", device.host[0], err_msg
        )
        return self.async_show_form(step_id="auth", errors=errors)

    async def async_step_reset(self, user_input=None, errors=None):
        """Guide the user to unlock the device manually.

        We are unable to authenticate because the device is locked.
        The user needs to open the Broadlink app and unlock the device.
        """
        device = self.device

        if user_input is None:
            return self.async_show_form(
                step_id="reset",
                errors=errors,
                description_placeholders={
                    "name": device.name,
                    "model": device.model,
                    "host": device.host[0],
                },
            )

        return await self.async_step_user(
            {CONF_HOST: device.host[0], CONF_TIMEOUT: device.timeout}
        )

    async def async_step_unlock(self, user_input=None):
        """Unlock the device.

        The authentication succeeded, but the device is locked.
        We can offer an unlock to prevent authorization errors.
        """
        device = self.device
        errors = {}

        if user_input is None:
            pass

        elif user_input["unlock"]:
            try:
                await self.hass.async_add_executor_job(device.set_lock, False)

            except NetworkTimeoutError as err:
                errors["base"] = "cannot_connect"
                err_msg = str(err)

            except BroadlinkException as err:
                errors["base"] = "unknown"
                err_msg = str(err)

            except OSError as err:
                if err.errno == errno.ENETUNREACH:
                    errors["base"] = "cannot_connect"
                    err_msg = str(err)
                else:
                    errors["base"] = "unknown"
                    err_msg = str(err)

            else:
                return await self.async_step_finish()

            _LOGGER.error(
                "Failed to unlock the device at %s: %s", device.host[0], err_msg
            )

        else:
            return await self.async_step_finish()

        data_schema = {vol.Required("unlock", default=False): bool}
        return self.async_show_form(
            step_id="unlock",
            errors=errors,
            data_schema=vol.Schema(data_schema),
            description_placeholders={
                "name": device.name,
                "model": device.model,
                "host": device.host[0],
            },
        )

    async def async_step_finish(self, user_input=None):
        """Choose a name for the device and create config entry."""
        device = self.device
        errors = {}

        if self.source == "reauth":
            self._abort_if_unique_id_configured(
                updates={CONF_HOST: device.host[0], CONF_TIMEOUT: device.timeout}
            )

        if user_input is not None:
            return self.async_create_entry(
                title=user_input[CONF_NAME],
                data={
                    CONF_HOST: device.host[0],
                    CONF_MAC: device.mac.hex(),
                    CONF_TYPE: device.devtype,
                    CONF_TIMEOUT: device.timeout,
                },
            )

        data_schema = {vol.Required(CONF_NAME, default=device.name): str}
        return self.async_show_form(
            step_id="finish", data_schema=vol.Schema(data_schema), errors=errors
        )

    async def async_step_import(self, import_info):
        """Import a device."""
        if any(
            import_info[CONF_HOST] == entry.data[CONF_HOST]
            for entry in self._async_current_entries()
        ):
            return self.async_abort(reason="already_configured")
        return await self.async_step_user(import_info)

    async def async_step_integration_discovery(self, discovery_info):
        """Handle a flow initiated by integration discovery."""
        if any(
            discovery_info[CONF_HOST] == entry.data[CONF_HOST]
            for entry in self._async_current_entries()
        ):
            return self.async_abort(reason="already_configured")

        device = blk.gendevice(
            discovery_info[CONF_TYPE],
            (discovery_info[CONF_HOST], DEFAULT_PORT),
            bytes.fromhex(discovery_info[CONF_MAC]),
            name=discovery_info[CONF_NAME],
            is_locked=discovery_info[CONF_LOCK],
        )
        await self.async_set_device(device)
        self._abort_if_unique_id_configured(
            updates={CONF_HOST: device.host[0], CONF_TIMEOUT: device.timeout}
        )
        return await self.async_step_auth()

    async def async_step_reauth(self, data):
        """Reauthenticate to the device."""
        device = blk.gendevice(
            data[CONF_TYPE],
            (data[CONF_HOST], DEFAULT_PORT),
            bytes.fromhex(data[CONF_MAC]),
            name=data[CONF_NAME],
        )
        device.timeout = data[CONF_TIMEOUT]
        await self.async_set_device(device)
        return await self.async_step_reset()
