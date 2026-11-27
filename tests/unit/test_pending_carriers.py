from __future__ import annotations

from datetime import UTC, datetime

from tt_auto_bag_scanner.domain.models import CarrierConfig
from tt_auto_bag_scanner.infrastructure.carrier_config import pending_carrier_pairs


def _config(carrier: str, sort: str) -> CarrierConfig:
    return CarrierConfig(
        carrier_key=carrier,
        display_name=carrier,
        aliases=(),
        sort_code=sort,
        container_no="BAG-SYN-1",
        enabled=True,
        version=1,
        updated_at_utc=datetime(2026, 1, 1, tzinfo=UTC),
    )


def test_pending_pairs_merge_new_report_pairs_and_drop_configured_ones() -> None:
    pending = pending_carrier_pairs(
        [("Carrier A", "SORT-A1")],
        [("carrier a", "sort-a1"), ("Carrier B", "SORT-B1"), ("Carrier C", "SORT-C1")],
        [_config("Carrier C", "sort-c1")],
    )

    assert pending == (("Carrier A", "SORT-A1"), ("Carrier B", "SORT-B1"))


def test_pending_pairs_ignore_blank_values() -> None:
    assert pending_carrier_pairs([], [("", "SORT"), ("Carrier", " ")], []) == ()
