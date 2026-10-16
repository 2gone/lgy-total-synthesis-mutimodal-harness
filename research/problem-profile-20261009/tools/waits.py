"""How long a model call waits before it is tried again. A rate limit (429), a lost connection, a timeout and a gateway error (5xx)
pass in minutes -- the gateway's limits, the network of the node -- so they are waited out: 30, 60, 120, 240, 300 s, about twelve
minutes in all over six tries. Anything else is tried again soon. TRIES is how many tries a call has."""
TRIES = 6
LONG = (30, 60, 120, 240, 300, 300)


def passing(error):
    """Whether the error is one that passes with time."""
    status = getattr(error, 'status_code', None)
    return status == 429 or (isinstance(status, int) and status >= 500) or type(error).__name__ in ('APIConnectionError', 'APITimeoutError', 'TimeoutError', 'ConnectionError', 'RemoteProtocolError', 'ReadTimeout')


def pause(error, attempt):
    """Seconds to wait after the attempt-th failure (0-based)."""
    return LONG[min(attempt, len(LONG) - 1)] if passing(error) else 8 * (attempt + 1)
