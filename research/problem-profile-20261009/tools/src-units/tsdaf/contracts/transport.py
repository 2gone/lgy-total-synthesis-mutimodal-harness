"""What one request to the gateway may weigh, measured rather than guessed.

This is not a schema; it is the one number every lane that assembles a request has to
respect, kept in one place so a second copy of it cannot drift. It lives with the
contracts because a lane may import them and nothing else shared reaches every lane.
"""

from __future__ import annotations

# Measured 2026-09-16 (probe in `.tsdaf-workspace/gateway-body-probe-20260916/`, ledger
# E104). Streamed requests of real Router thumbnails asking a question no model thinks
# about, so neither the ~60 s buffered cut nor the 40 s no-bytes rule is in play:
# 1.01 MB (36 pictures), 2.04 MB (71) and 2.51 MB (88) were answered; 3.00 MB (105),
# 3.05 MB (60) and 4.00 MB (139) broke at the transport with no HTTP status and did not
# recover on a refreshed connection. 3.05 MB failing with 60 pictures while 2.51 MB
# answered with 88 is what says the wall is bytes and not how many pictures travel.
#
# This guard sits 0.4 % under the largest body measured to answer (2,509,132 bytes) and
# 20 % under the smallest measured to fail (3.00 MB). It costs almost nothing to hold --
# it fires before any call, so the three papers of the c3 batch it refused cost 5-8 s
# and no tokens each.
MAX_REQUEST_BYTES = 2_500_000
