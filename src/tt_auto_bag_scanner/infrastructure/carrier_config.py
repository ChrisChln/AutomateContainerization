"""Validation and explicit resolution for carrier/sort/container mappings."""

from __future__ import annotations

from collections import defaultdict
from collections.abc import Sequence

from tt_auto_bag_scanner.domain.models import CarrierConfig


class CarrierConfigError(ValueError):
    """Carrier mapping is missing, disabled, duplicate, or ambiguous."""


class CarrierConfigService:
    def validate(self, configs: Sequence[CarrierConfig]) -> None:
        seen_pairs: set[tuple[str, str]] = set()
        containers_by_carrier: dict[str, set[str]] = defaultdict(set)
        for config in configs:
            pair = (self._normalize(config.carrier_key), self._normalize(config.sort_code))
            if pair in seen_pairs:
                raise CarrierConfigError("duplicate carrier and sort configuration")
            seen_pairs.add(pair)
            containers_by_carrier[self._normalize(config.container_no)].add(
                self._normalize(config.carrier_key)
            )
        if any(len(carriers) > 1 for carriers in containers_by_carrier.values()):
            raise CarrierConfigError("shared container across different carriers is not allowed")

    def resolve(
        self,
        carrier_value: str,
        configs: Sequence[CarrierConfig],
        *,
        reported_sort_code: str | None = None,
    ) -> CarrierConfig:
        needle = self._normalize(carrier_value)
        if not needle:
            raise CarrierConfigError("carrier is missing")
        matching = [
            config
            for config in configs
            if needle
            in {
                self._normalize(config.carrier_key),
                self._normalize(config.display_name),
                *(self._normalize(alias) for alias in config.aliases),
            }
        ]
        enabled = [config for config in matching if config.enabled]
        if reported_sort_code:
            sort_key = self._normalize(reported_sort_code)
            enabled = [
                config for config in enabled if self._normalize(config.sort_code) == sort_key
            ]
        unique = {(item.carrier_key, item.sort_code, item.container_no): item for item in enabled}
        if len(unique) == 1:
            return next(iter(unique.values()))
        if len(unique) > 1:
            raise CarrierConfigError("carrier mapping is ambiguous; select an exact sort code")
        if matching and not any(item.enabled for item in matching):
            raise CarrierConfigError("carrier mapping is disabled")
        if matching and reported_sort_code:
            raise CarrierConfigError("reported sort code is not mapped for this carrier")
        if matching:
            raise CarrierConfigError("carrier mapping is ambiguous or disabled")
        raise CarrierConfigError("carrier is not mapped")

    @staticmethod
    def _normalize(value: str) -> str:
        return " ".join(value.strip().split()).casefold()
