"""Middleware of stapel-analytics. One, and it reads a cookie.

``AttributionCookieMiddleware`` turns the advertising cookie a marketing
site left on the apex domain into the account's stored attribution
(``attribution.py`` holds the rule and the decode; this holds the placement).

**It works on the way out, not on the way in.** The user of a request is not
a settled fact until the view has run: this fleet authenticates through a
middleware for JWT-carrying requests *and* through DRF authenticators for
others, and the second kind only assigns ``request.user`` inside the view.
A capture in ``process_request`` would therefore see ``AnonymousUser`` on
exactly the doors that matter most. Running after ``get_response`` sees
whichever of the two authenticated the request — and, as a second effect,
sees an attribution the view itself may have just stored, which is what
makes "the explicit one wins" true without this module knowing where the
explicit one is written.

**It cannot fail a request.** Attribution is a marketing record; the request
is the product. Everything here is wrapped, and a failure is logged rather
than raised — the opposite trade (losing responses to keep a marketing row
honest) is not one any deployment would choose.

**Mount it after authentication.** Anywhere later in ``MIDDLEWARE`` than the
authentication middleware; the end of the list is the usual answer.
``analytics.W013`` fires when the cookie is named and this class is not
mounted, because a settings block that does nothing is worse than none.
"""
from __future__ import annotations

import logging

logger = logging.getLogger(__name__)


class AttributionCookieMiddleware:
    """Capture the advertising attribution cookie onto the account."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        self.capture(request)
        return response

    def capture(self, request):
        """The whole body, in a method so a subclass can narrow it."""
        from . import attribution

        try:
            return attribution.capture(request)
        except Exception:  # never let a marketing record cost a response
            logger.exception(
                "analytics: could not capture the attribution cookie "
                "(the request itself is unaffected)"
            )
            return None


__all__ = ["AttributionCookieMiddleware"]
