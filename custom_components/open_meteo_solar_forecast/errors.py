"""Sanitising error text before it is exposed (0.1.33.4, audit OMSF-018).

aiohttp's ``ClientResponseError`` renders the full request URL, and the
upstream library sends the Open-Meteo API key as a query parameter. Any error
text that reaches an entity attribute (stored in the recorder database and
visible to every Home Assistant user) or the diagnostics export therefore
passes through ``sanitize_error`` first. Logs are left as they are (admin
only, Home Assistant's own handling).

Pure module: no Home Assistant import.
"""

from __future__ import annotations

import re

MAX_ERROR_LENGTH = 200

_URL_QUERY = re.compile(r"(https?://[^\s'\"?#]+)\?[^\s'\"]*", re.IGNORECASE)
_SECRET_PARAM = re.compile(
    r"((?:api[_-]?key|apikey|token|key|password|secret)\s*[=:]\s*)[^\s&'\",;]+",
    re.IGNORECASE,
)


def sanitize_error(error: object) -> str:
    """Error text without URL query strings or secret-looking parameters,
    bounded in length."""
    if isinstance(error, BaseException):
        text = str(error) or type(error).__name__
    else:
        text = str(error)
    text = _URL_QUERY.sub(r"\1?<redacted>", text)
    text = _SECRET_PARAM.sub(r"\1<redacted>", text)
    if len(text) > MAX_ERROR_LENGTH:
        text = text[: MAX_ERROR_LENGTH - 1] + "…"
    return text
