"""The rules every composition root applies to a live provider, written once.

Three composition roots (the route command, the literature composition, the image
composition) each carried a copy of the base-URL check, and the copies drifted: the
route's had been taught to accept a loopback proxy after a caught regression, the other
two refused the same value at configuration time with a message that did not say why
(deep audit L9-03). The decision, made once here: `https` anywhere, or `http` on the
local loopback only -- a local proxy is how a batch will be rate-limited and cached at
scale (step 9), and it never leaves the machine. The path must be the OpenAI-compatible
`/v1`; no user info, query or fragment.

The same happened to the transport: the literature lanes learnt at E64 that the fixed
deployment sits behind a gateway that closes a connection with no response bytes at
about 60-100 s, and moved to streamed calls with a five-minute deadline; the Router
kept its buffered call on the belief that its answers were too small to reach that
ceiling. The 200-paper Router batch of 2026-09-15 (c3) showed the ceiling is about the
model's thinking time, not the answer's size: every cut call died at 60.4-60.8 s with no
HTTP status, whether the request was 171 KB with 7 thumbnails or 1.97 MB with 56, and
every answered call had returned within 50 s. A buffered call is a coin toss on whether
the model thinks for less than a minute; a streamed one is not. So one factory here
builds every live gateway client, streamed, with one deadline.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from urllib.parse import urlsplit

if TYPE_CHECKING:
    from tsdaf.adapters.gemini.interactions import GeminiInteractions

__all__ = (
    "GATEWAY_CLIENT_TIMEOUT_S",
    "LOOPBACK_HOSTS",
    "live_gateway_client",
    "safe_base_url",
)

LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# One streamed turn may take minutes (a fusion answer, a Router turn over fifty
# thumbnails). This bounds the client's network waits and assembly's silence before
# model output starts; that watchdog is then cancelled, not a whole-turn deadline.
GATEWAY_CLIENT_TIMEOUT_S = 300.0


def live_gateway_client(api_key: str, base_url: str) -> GeminiInteractions:
    """The fixed OpenAI-compatible client every live command talks through: streamed,
    with the gateway deadline. Imported lazily so a command without the SDK still fails
    at configuration time with its own message, not at import."""
    from tsdaf.adapters.gemini.interactions import GeminiInteractions

    return GeminiInteractions.from_api_key(
        api_key=api_key, base_url=base_url, stream=True, timeout=GATEWAY_CLIENT_TIMEOUT_S
    )


def safe_base_url(value: str | None) -> str | None:
    """The base URL as given when it is one a live command may use, else None."""
    if value is None or not value or value != value.strip():
        return None
    parsed = urlsplit(value)
    if (
        not (
            parsed.scheme == "https"
            or (parsed.scheme == "http" and parsed.hostname in LOOPBACK_HOSTS)
        )
        or not parsed.netloc
        or parsed.username is not None
        or parsed.password is not None
        or parsed.query
        or parsed.fragment
        or parsed.path != "/v1"
    ):
        return None
    return value
