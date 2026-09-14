"""Strict cookieless probe for momondo's own official flight-search page.

momondo and KAYAK currently share frontend infrastructure, but this provider
always requests and validates ``www.momondo.com`` independently.  It never
copies a KAYAK quote.  See ``KAYAK_MOMONDO_SOURCES.md`` for the verified
anonymous-access boundary.
"""

from __future__ import annotations

from .kayak_source import _R9AnonymousPageProvider, _Site


MOMONDO_SITE = _Site(
    "momondo", "momondo", "www.momondo.com", "/flight-search", "momondo"
)


class MomondoProvider(_R9AnonymousPageProvider):
    site = MOMONDO_SITE

