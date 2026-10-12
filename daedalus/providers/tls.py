"""One verifying TLS context for the provider clients."""

from __future__ import annotations

import functools
import ssl

import httpx


@functools.cache
def shared_context() -> ssl.SSLContext:
    """The context every provider client verifies with, built the way httpx builds its default.

    Each client used to build its own, and building one reads the whole certificate bundle: about ten
    milliseconds, times every configured endpoint, on every registry made, every reload that changes
    an endpoint and every price refresh. A context is safe to share between clients; what it trusts
    cannot differ between them, since they were all built from the same bundle and the same
    environment.
    """
    return httpx.create_ssl_context()
