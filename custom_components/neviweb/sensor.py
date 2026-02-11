"""
Support for Neviweb sensor.

model 125, GT125 gateway
For more details about this platform, please refer to the documentation at  
https://www.sinopetech.com/en/support/#api
"""

import asyncio
import datetime
import logging
from datetime import datetime as dt

import voluptuous as vol
import time

import custom_components.neviweb as neviweb
from . import (SCAN_INTERVAL)
from homeassistant.components.sensor import PLATFORM_SCHEMA, SensorEntity, SensorDeviceClass, SensorStateClass

from homeassistant.const import (
    ATTR_ENTITY_ID,
    STATE_OK,
    UnitOfEnergy,
)

from homeassistant.helpers import (
    config_validation as cv,
    discovery,
    service,
    entity_platform,
    entity_component,
    entity_registry,
    device_registry,
)

from homeassistant.components.binary_sensor import BinarySensorDeviceClass

from datetime import timedelta
from homeassistant.helpers.event import track_time_interval
from homeassistant.helpers.entity import DeviceInfo
from .const import (
    DOMAIN,
    ATTR_LOCAL_SYNC,
    ATTR_MODE,
    ATTR_OCCUPANCY,
    ATTR_STATUS,
    SERVICE_SET_NEVIWEB_STATUS,
)
from .helpers import get_daily_request_count

_LOGGER = logging.getLogger(__name__)

DEFAULT_NAME = 'neviweb sensor'

UPDATE_ATTRIBUTES = []

IMPLEMENTED_THERMOSTAT_MODEL = [740] # DITRA-HEAT-E-RS1

SET_NEVIWEB_STATUS_SCHEMA = vol.Schema(
    {
        vol.Required(ATTR_ENTITY_ID): cv.entity_id,
        vol.Required(ATTR_MODE): vol.In(["home", "away"]),
    }
)

async def async_setup_platform(
    hass,
    config,
    async_add_entities,
    discovery_info=None,
) -> None:
    """Set up the Neviweb sensor."""
    data = hass.data[DOMAIN]["data"]

    # Wait for async migration to be done
    await data.migration_done.wait()

    entities = []
    entities.append(NeviwebDailyRequestSensor(hass))
    for device_info in data.neviweb_client.gateway_data:
        if "signature" in device_info and \
            "model" in device_info["signature"] and \
            device_info["signature"]["model"] in IMPLEMENTED_THERMOSTAT_MODEL:
            device_name = device_info["name"]
            device_sku = device_info["sku"]
            location_id = device_info["location$id"]
            entities.append(NeviwebSensor(data, device_info, device_name, device_sku, location_id))
            entities.append(NeviwebHourlyEnergySensor(hass, data, device_info, device_name, device_sku))
    for device_info in data.neviweb_client.gateway_data2:
        if "signature" in device_info and \
            "model" in device_info["signature"] and \
            device_info["signature"]["model"] in IMPLEMENTED_THERMOSTAT_MODEL:
            device_name = device_info["name"]
            device_sku = device_info["sku"]
            location_id = device_info["location$id"]
            entities.append(NeviwebSensor(data, device_info, device_name, device_sku, location_id))
            entities.append(NeviwebHourlyEnergySensor(hass, data, device_info, device_name, device_sku))

    async_add_entities(entities, True)

    def set_neviweb_status_service(service):
        """Set Neviweb global status, home or away."""
        entity_id = service.data[ATTR_ENTITY_ID]
        value = {}
        for sensor in entities:
            if sensor.entity_id == entity_id:
                value = {"id": sensor.unique_id, "mode": service.data[ATTR_MODE]}
                sensor.set_neviweb_status(value)
                sensor.schedule_update_ha_state(True)
                break

    hass.services.async_register(
        DOMAIN,
        SERVICE_SET_NEVIWEB_STATUS,
        set_neviweb_status_service,
        schema=SET_NEVIWEB_STATUS_SCHEMA,
    )


class NeviwebSensor(SensorEntity):
    """Implementation of a Neviweb sensor."""

    def __init__(self, data, device_info, name, sku, location):
        """Initialize."""
        self._attr_name = name
        self._sku = sku
        self._location = location
        self._client = data.neviweb_client
        self._attr_unique_id = str(device_info["id"])
        self._gateway_status = None
        self._occupancyMode = None
        _LOGGER.debug("Setting up %s: %s", self._attr_name, device_info)

    def update(self):
        """Get the latest data from Neviweb and update the state."""
        start = time.time()
        device_status = self._client.get_device_status(self._attr_unique_id)
        neviweb_status = self._client.get_neviweb_status(self._location)
        end = time.time()
        elapsed = round(end - start, 3)
        _LOGGER.debug("Updating %s (%s sec): %s",
            self._attr_name, elapsed, device_status)
        self._gateway_status = device_status[ATTR_STATUS]
        self._occupancyMode = neviweb_status[ATTR_OCCUPANCY]


class NeviwebHourlyEnergySensor(SensorEntity):
    """Sensor for hourly energy consumption from thermostats."""

    def __init__(self, hass, data, device_info, name, sku):
        """Initialize the daily energy sensor."""
        self._hass = hass
        self._data = data
        self._client = data.neviweb_client
        self._device_id = str(device_info["id"])
        self._attr_name = f"{name} Daily Energy"
        self._sku = sku
        self._attr_native_value = 0.0  # Initialize with 0 to keep entity available
        # Initialize last_reset to midnight UTC
        now_utc = dt.now(datetime.timezone.utc)
        self._attr_last_reset = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
        self._attr_unique_id = f"{self._device_id}_daily_energy"
        self._last_update_hour = -1  # Track the hour of the last update to update once per hour
        _LOGGER.debug("Setting up daily energy sensor for %s: %s", self._attr_name, device_info)

    @property
    def device_info(self) -> DeviceInfo:
        """Return device information to group with thermostat entity."""
        return DeviceInfo(
            identifiers={(DOMAIN, self._device_id)},
            name=self._attr_name.replace(" Hourly Energy", ""),
            manufacturer="Schluter",
            model=self._sku,
        )

    @property
    def native_value(self):
        """Return the cumulative hourly energy consumption in kWh."""
        return self._attr_native_value

    @property
    def native_unit_of_measurement(self):
        """Return the unit of measurement."""
        return UnitOfEnergy.KILO_WATT_HOUR

    @property
    def device_class(self):
        """Return the device class."""
        return SensorDeviceClass.ENERGY

    @property
    def state_class(self):
        """Return the state class indicating this is a total aggregated value."""
        return SensorStateClass.TOTAL

    @property
    def last_reset(self):
        """Return the last reset time (first period in the returned data)."""
        return self._attr_last_reset

    @property
    def icon(self):
        """Return the icon."""
        return "mdi:flash"

    @property
    def extra_state_attributes(self):
        """Return the state attributes."""
        return {
            'sku': self._sku,
            'id': self._device_id,
        }

    def _parse_iso_timestamp(self, iso_str):
        """Parse ISO 8601 timestamp string to datetime object."""
        try:
            # Handle formats like "2026-02-09T02:00:00.000Z"
            if iso_str.endswith('Z'):
                iso_str = iso_str[:-1] + '+00:00'
            # Remove microseconds if needed for parsing
            if '.' in iso_str:
                parts = iso_str.split('.')
                iso_str = parts[0] + '+00:00'
            return dt.fromisoformat(iso_str)
        except Exception as err:
            _LOGGER.error("Failed to parse timestamp %s: %s", iso_str, err)
            return None

    def update(self):
        """Update the daily energy data from the device once per hour (with grace period for API updates)."""
        # Only update if we've entered a new hour AND we're at least 5 minutes into it (UTC)
        # This allows time for the API to provide updated hourly data
        now_utc = dt.now(datetime.timezone.utc)
        current_hour = now_utc.hour
        
        if current_hour == self._last_update_hour:
            return  # Already updated in this hour
        
        # Check if we're at least 5 minutes past the hour boundary
        if now_utc.minute < 5:
            return  # Too early; wait for API to update
        
        try:
            # Get hourly energy consumption stats from API (in Wh)
            device_hourly_stats = self._client.get_device_hourly_stats(self._device_id)
            _LOGGER.debug("Hourly stats for %s: %s", self._attr_name, device_hourly_stats)
            
            if device_hourly_stats is not None and len(device_hourly_stats) > 0:
                # Calculate midnight UTC for filtering
                midnight_utc = now_utc.replace(hour=0, minute=0, second=0, microsecond=0)
                
                # Filter entries from current day (>= midnight UTC) and sum them
                today_entries = []
                for entry in device_hourly_stats:
                    if 'date' in entry:
                        parsed_time = self._parse_iso_timestamp(entry['date'])
                        if parsed_time and parsed_time >= midnight_utc:
                            today_entries.append(entry)
                
                if today_entries:
                    total_wh = sum(entry.get('period', 0) for entry in today_entries)
                    self._attr_native_value = round(total_wh / 1000, 3)
                    self._attr_last_reset = midnight_utc
                    
                    _LOGGER.debug(
                        "Updated cumulative daily energy for %s: %s kWh (%d entries since midnight)",
                        self._attr_name,
                        self._attr_native_value,
                        len(today_entries),
                    )
                else:
                    _LOGGER.debug("No entries from current day for %s", self._attr_name)
            else:
                _LOGGER.debug("No hourly stats available for %s", self._attr_name)
        except Exception as err:
            _LOGGER.error("Error updating daily energy for %s: %s", self._attr_name, err)
        finally:
            self._last_update_hour = current_hour


class NeviwebDailyRequestSensor(SensorEntity):
    """Sensor interne : nombre de requêtes Neviweb130 aujourd'hui."""

    def __init__(self, hass):
        self._hass = hass
        self._attr_name = "Neviweb Daily Requests"
        self._attr_unique_id = f"{DOMAIN}_daily_requests"
        self._notified = False

    @property
    def native_value(self):
        return get_daily_request_count(self._hass)

    @property
    def icon(self):
        return "mdi:counter"

    @property
    def extra_state_attributes(self):
        data = self._hass.data[DOMAIN]["request_data"]
        return {
            "date": data["date"],
            "limit": 30000,
        }

    def update(self):
        """Send notification if we reach limit for request."""
        count = get_daily_request_count(self._hass)

        # Secure limit for notification
        if count > 25000 and not self._notified:
            self._notified = True

            asyncio.run_coroutine_threadsafe(
                self._hass.services.async_call(
                    "persistent_notification",
                    "create",
                    {
                        "title": "Neviweb",
                        "message": f"Warning : {count} today request. Limit : 30000.",
                    },
                ),
                self._hass.loop,
            )

        # Reset du flag si on change de jour
        data = self._hass.data[DOMAIN]["request_data"]
        today = datetime.date.today().isoformat()

        if data["date"] != today:
            self._notified = False
