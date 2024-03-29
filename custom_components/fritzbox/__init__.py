"""Support for AVM FRITZ!SmartHome devices."""
from __future__ import annotations

from abc import ABC, abstractmethod

from pyfritzhome import Fritzhome, FritzhomeDevice, LoginError
from pyfritzhome.devicetypes.fritzhomeentitybase import FritzhomeEntityBase
from requests.exceptions import ConnectionError as RequestConnectionError

from homeassistant.components import automation, script
from homeassistant.components.binary_sensor import DOMAIN as BINARY_SENSOR_DOMAIN
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import (
    CONF_HOST,
    CONF_PASSWORD,
    CONF_USERNAME,
    EVENT_HOMEASSISTANT_STOP,
    UnitOfTemperature,
)
from homeassistant.core import Event, HomeAssistant
from homeassistant.exceptions import ConfigEntryAuthFailed, ConfigEntryNotReady
from homeassistant.helpers.device_registry import (
    DeviceEntry,
    DeviceInfo,
    async_get as dr_async_get,
)
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.entity_component import DATA_INSTANCES, EntityComponent
from homeassistant.helpers.entity_registry import RegistryEntry, async_migrate_entries
from homeassistant.helpers.issue_registry import IssueSeverity, async_create_issue
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CONNECTIONS, CONF_COORDINATOR, DOMAIN, LOGGER, PLATFORMS
from .coordinator import FritzboxDataUpdateCoordinator


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up the AVM FRITZ!SmartHome platforms."""
    fritz = Fritzhome(
        host=entry.data[CONF_HOST],
        user=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
    )

    try:
        await hass.async_add_executor_job(fritz.login)
    except RequestConnectionError as err:
        raise ConfigEntryNotReady from err
    except LoginError as err:
        raise ConfigEntryAuthFailed from err

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN][entry.entry_id] = {
        CONF_CONNECTIONS: fritz,
    }

    has_templates = await hass.async_add_executor_job(fritz.has_templates)
    LOGGER.debug("enable smarthome templates: %s", has_templates)

    coordinator = FritzboxDataUpdateCoordinator(hass, entry, has_templates)

    await coordinator.async_config_entry_first_refresh()

    hass.data[DOMAIN][entry.entry_id][CONF_COORDINATOR] = coordinator

    def _update_unique_id(entry: RegistryEntry) -> dict[str, str] | None:
        """Update unique ID of entity entry."""
        if (
            entry.unit_of_measurement == UnitOfTemperature.CELSIUS
            and "_temperature" not in entry.unique_id
        ):
            new_unique_id = f"{entry.unique_id}_temperature"
            LOGGER.info(
                "Migrating unique_id [%s] to [%s]", entry.unique_id, new_unique_id
            )
            return {"new_unique_id": new_unique_id}

        if entry.domain == BINARY_SENSOR_DOMAIN and "_" not in entry.unique_id:
            new_unique_id = f"{entry.unique_id}_alarm"
            LOGGER.info(
                "Migrating unique_id [%s] to [%s]", entry.unique_id, new_unique_id
            )
            return {"new_unique_id": new_unique_id}
        return None

    await async_migrate_entries(hass, entry.entry_id, _update_unique_id)

    dr = dr_async_get(hass)

    # fetch all sub devices to remove - can be removed in 2024.8
    devices_to_remove: dict[str, str] = {}
    for ain, fritz_device in coordinator.data.devices.items():
        if fritz_device.device_and_unit_id[1]:
            # remove obsolet entries for sub device units
            if (device := dr.async_get_device(identifiers={(DOMAIN, ain)})) is not None:
                devices_to_remove[device.id] = (
                    device.name_by_user or device.name or device.id
                )

    # check their usage and create repair issues - can be removed in 2024.8
    found_issues: dict[str, list[str]] = {}
    automation_component: EntityComponent[automation.AutomationEntity] = hass.data[
        DATA_INSTANCES
    ][automation.DOMAIN]
    script_component: EntityComponent[script.ScriptEntity] = hass.data[DATA_INSTANCES][
        script.DOMAIN
    ]
    for component in (automation_component, script_component):
        for entity in component.entities:
            for removed_device in devices_to_remove:
                if removed_device not in entity.referenced_devices:
                    continue
                if found_issues.get(removed_device) is None:
                    found_issues[removed_device] = []
                found_issues[removed_device].append(entity.entity_id)

    for device_id, issues in found_issues.items():
        async_create_issue(
            hass,
            DOMAIN,
            f"deleted_device_{device_id}",
            is_fixable=False,
            is_persistent=True,
            severity=IssueSeverity.ERROR,
            translation_key="deleted_device",
            translation_placeholders={
                "device_name": devices_to_remove[device_id],
                "device_id": device_id,
                "entities": "\n".join(f"- `{entity}`" for entity in issues),
            },
        )

    # remove these devices - can be removed in 2024.8
    for device_id in devices_to_remove:
        dr.async_remove_device(device_id)

    for ain, fritz_device in coordinator.data.devices.items():
        if fritz_device.device_and_unit_id[1] is None:
            # pre-create main device entries
            dr.async_get_or_create(
                config_entry_id=entry.entry_id,
                name=fritz_device.name,
                identifiers={(DOMAIN, ain)},
                connections={("ain", ain)},
                manufacturer=fritz_device.manufacturer,
                model=fritz_device.productname,
                sw_version=fritz_device.fw_version,
                configuration_url=coordinator.configuration_url,
            )

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    def logout_fritzbox(event: Event) -> None:
        """Close connections to this fritzbox."""
        fritz.logout()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, logout_fritzbox)
    )

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unloading the AVM FRITZ!SmartHome platforms."""
    fritz = hass.data[DOMAIN][entry.entry_id][CONF_CONNECTIONS]
    await hass.async_add_executor_job(fritz.logout)

    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unload_ok:
        hass.data[DOMAIN].pop(entry.entry_id)

    return unload_ok


async def async_remove_config_entry_device(
    hass: HomeAssistant, entry: ConfigEntry, device: DeviceEntry
) -> bool:
    """Remove Fritzbox config entry from a device."""
    coordinator: FritzboxDataUpdateCoordinator = hass.data[DOMAIN][entry.entry_id][
        CONF_COORDINATOR
    ]

    for identifier in device.identifiers:
        if identifier[0] == DOMAIN and (
            identifier[1] in coordinator.data.devices
            or identifier[1] in coordinator.data.templates
        ):
            return False

    return True


class FritzBoxEntity(CoordinatorEntity[FritzboxDataUpdateCoordinator], ABC):
    """Basis FritzBox entity."""

    def __init__(
        self,
        coordinator: FritzboxDataUpdateCoordinator,
        ain: str,
        entity_description: EntityDescription | None = None,
    ) -> None:
        """Initialize the FritzBox entity."""
        super().__init__(coordinator)

        self.ain = ain
        if entity_description is not None:
            self._attr_has_entity_name = True
            self.entity_description = entity_description
            self._attr_unique_id = f"{ain}_{entity_description.key}"
        else:
            self._attr_name = self.data.name
            self._attr_unique_id = ain

    @property
    @abstractmethod
    def data(self) -> FritzhomeEntityBase:
        """Return data object from coordinator."""


class FritzBoxDeviceEntity(FritzBoxEntity):
    """Reflects FritzhomeDevice and uses its attributes to construct FritzBoxDeviceEntity."""

    @property
    def available(self) -> bool:
        """Return if entity is available."""
        return super().available and self.data.present

    @property
    def data(self) -> FritzhomeDevice:
        """Return device data object from coordinator."""
        return self.coordinator.data.devices[self.ain]

    @property
    def device_info(self) -> DeviceInfo:
        """Return device specific attributes."""
        if self.data.device_and_unit_id[1] is not None:
            return DeviceInfo(
                connections={("ain", self.data.device_and_unit_id[0])},
            )

        return DeviceInfo(
            identifiers={(DOMAIN, self.ain)},
            sw_version=self.data.fw_version,
        )
