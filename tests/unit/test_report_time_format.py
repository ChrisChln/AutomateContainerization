from __future__ import annotations

from datetime import UTC, datetime
from zoneinfo import ZoneInfo

import pytest

from tt_auto_bag_scanner.application.import_service import ImportService

NEW_YORK = ZoneInfo("America/New_York")


@pytest.mark.parametrize(
    ("text", "expected_utc"),
    [
        ("11/27/2026 11:03:20 AM", datetime(2026, 11, 27, 16, 3, 20, tzinfo=UTC)),
        ("11/27/2026 01:05:00 PM", datetime(2026, 11, 27, 18, 5, 0, tzinfo=UTC)),
        ("11/27/2026 12:00:00 AM", datetime(2026, 11, 27, 5, 0, 0, tzinfo=UTC)),
        ("2026-11-27 11:03:20", datetime(2026, 11, 27, 16, 3, 20, tzinfo=UTC)),
    ],
)
def test_standard_report_time_text_is_parsed_in_report_timezone(
    text: str, expected_utc: datetime
) -> None:
    assert ImportService._to_utc(text, NEW_YORK) == (expected_utc, None)


@pytest.mark.parametrize(
    "text",
    ["27/11/2026 11:03:20 AM", "11/27/2026 13:03:20 PM", "11/27/26 11:03 AM", "下午 1:05"],
)
def test_other_time_text_stays_invalid(text: str) -> None:
    assert ImportService._to_utc(text, NEW_YORK) == (None, "invalid_time")


def test_dst_ambiguous_standard_time_is_not_guessed() -> None:
    assert ImportService._to_utc("11/01/2026 01:30:00 AM", NEW_YORK) == (
        None,
        "ambiguous_local_time",
    )
