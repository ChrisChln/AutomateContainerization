from datetime import UTC, datetime

import pytest

from tt_auto_bag_scanner.domain.models import CarrierConfig
from tt_auto_bag_scanner.infrastructure.carrier_config import (
    CarrierConfigError,
    CarrierConfigService,
)


def config(key, display, aliases, sort, container, enabled=True):
    return CarrierConfig(
        carrier_key=key,
        display_name=display,
        aliases=aliases,
        sort_code=sort,
        container_no=container,
        enabled=enabled,
        version=1,
        updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_validation_rejects_duplicate_keys_and_shared_containers():
    service = CarrierConfigService()

    with pytest.raises(CarrierConfigError, match="duplicate"):
        service.validate(
            [
                config("CARRIER-A", "A", ("a",), "SORT-A", "BAG-A"),
                config("CARRIER-A", "A", ("a2",), "SORT-A", "BAG-B"),
            ]
        )
    with pytest.raises(CarrierConfigError, match="shared container"):
        service.validate(
            [
                config("CARRIER-A", "A", ("a",), "SORT-A", "BAG-X"),
                config("CARRIER-B", "B", ("b",), "SORT-B", "BAG-X"),
            ]
        )


def test_alias_requires_unique_enabled_mapping_and_explicit_sort_when_ambiguous():
    service = CarrierConfigService()
    configs = [
        config("CARRIER-A", "A", ("same alias",), "SORT-A", "BAG-A"),
        config("CARRIER-A", "A", ("same alias",), "SORT-B", "BAG-B"),
        config("CARRIER-B", "B", ("same alias",), "SORT-C", "BAG-C"),
    ]

    with pytest.raises(CarrierConfigError, match="ambiguous"):
        service.resolve(" same alias ", configs)
    with pytest.raises(CarrierConfigError, match="ambiguous"):
        service.resolve("A", configs)
    assert service.resolve("same alias", configs, reported_sort_code="SORT-B").sort_code == "SORT-B"
    with pytest.raises(CarrierConfigError, match="not mapped"):
        service.resolve("unconfigured", configs)
