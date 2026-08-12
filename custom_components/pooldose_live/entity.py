"""Basis-Entity für pooldose_live."""

from __future__ import annotations

from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity import EntityDescription
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from pooldose_live.mapping import ResolvedChannel

from .const import DOMAIN, MANUFACTURER
from .coordinator import PooldoseLiveCoordinator


def _device_info(coordinator: PooldoseLiveCoordinator) -> DeviceInfo:
    """Gerätename/-modell erst vorhanden, sobald der erste Snapshot ankam -
    vorher Platzhalter statt eines HTTP-Calls (Konzept §4: Modell/FW sind
    aus den Daten-Keys ableitbar, kein HTTP nötig)."""
    mapping = coordinator.mapping
    return DeviceInfo(
        identifiers={(DOMAIN, coordinator.host)},
        manufacturer=MANUFACTURER,
        model=mapping.model_id if mapping else None,
        name=(f"PoolDose {mapping.model_id}" if mapping else f"PoolDose ({coordinator.host})"),
        sw_version=f"FW{mapping.fw_code}" if mapping else None,
        configuration_url=f"http://{coordinator.host}/index.html",
    )


class PooldoseLiveEntity(CoordinatorEntity[PooldoseLiveCoordinator]):
    """Basisklasse für alle pooldose_live-Entities.

    Ein Kanalname (`channel_name`) identifiziert die Entity gegenüber dem
    Coordinator - der aktuelle Wert wird nicht im Konstruktor eingefroren,
    sondern bei jedem Update über `resolved` neu gelesen.
    """

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: PooldoseLiveCoordinator,
        channel_name: str,
        entity_description: EntityDescription,
    ) -> None:
        super().__init__(coordinator)
        self.entity_description = entity_description
        self._channel_name = channel_name
        self._attr_unique_id = f"{coordinator.host}_{channel_name}"
        self._attr_device_info = _device_info(coordinator)

    @property
    def resolved(self) -> ResolvedChannel | None:
        return self.coordinator.data.get(self._channel_name)

    @property
    def available(self) -> bool:
        """Nicht verfügbar, wenn der Kanal fehlt oder der Coordinator die
        instant_values-Daten als veraltet markiert hat (Staleness-Watchdog,
        Konzept §5.3). Feinere Unterscheidung (z. B. "stale wegen Standby",
        §8.4) ist P3-Scope, hier bewusst noch binär."""
        return super().available and self.resolved is not None and not self.coordinator.is_stale
