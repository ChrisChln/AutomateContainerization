"""Safe WMS/device error categories and redaction helpers."""

from __future__ import annotations

import re


class DeviceActionError(RuntimeError):
    """A bounded device operation failed before a safe result was proven."""


class AmbiguousSubmission(DeviceActionError):
    """The one write may have reached WMS, so it must not be retried."""


class ScreenOffError(DeviceActionError):
    """The PDA display is off, so no page can be read."""


class UnknownPageError(DeviceActionError):
    """The visible application state did not match a known page."""


_SENSITIVE = re.compile(r"(?i)(password|token|cookie|authorization)\s*[:=]\s*[^\s,;]+")


def redact_error_text(value: str) -> str:
    """Remove common credential-like key/value fragments from error details."""
    return _SENSITIVE.sub(r"\1=[REDACTED]", value)

