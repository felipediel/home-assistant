"""Constants for the Broadlink integration."""
import broadlink as blk

from homeassistant.components.remote import DOMAIN as REMOTE_DOMAIN
from homeassistant.components.sensor import DOMAIN as SENSOR_DOMAIN
from homeassistant.components.switch import DOMAIN as SWITCH_DOMAIN

DOMAIN = "broadlink"

LIBRARY_URL = "https://github.com/mjg59/python-broadlink"

SUPPORTED_TYPES = {
    "A1": blk.a1,
    "MP1": blk.mp1,
    "RM2": blk.rm,
    "RM4": blk.rm4,
    "SP1": blk.sp1,
    "SP2": blk.sp2,
}

DOMAINS_AND_TYPES = (
    (REMOTE_DOMAIN, ("RM2", "RM4")),
    (SENSOR_DOMAIN, ("A1", "RM2", "RM4")),
    (SWITCH_DOMAIN, ("MP1", "RM2", "RM4", "SP1", "SP2")),
)

DEFAULT_PORT = 80
DEFAULT_TIMEOUT = 5
