"""The bucket boundaries of the gateway's histograms of what students wait for and of each
stream's speed (system design, sections 7.2 and 7.3).

The gateway counts what students experience in these buckets (`rhi_gateway.experience`), and the
objectives of section 7.3 are read at their floors (`rhi_dashboards.objectives`). A histogram
reads a share exactly only at one of its boundaries, so the configuration takes as a floor only a
boundary of its histogram (`rhi.config`): the two lists are here so that both read the same ones.
"""

from __future__ import annotations

# Tokens per second. 10 and 15 are the floors of the objectives (sections 7.3 and 17): both are
# boundaries, and the buckets are finest around them and across the estimate of section 4.7.
SPEED_BUCKETS = (1, 2.5, 5, 7.5, 10, 12.5, 15, 17.5, 20, 22.5, 25, 30, 35, 40, 45, 50, 60, 80, 100, 150, 200)

# Seconds from a request accepted, for both waits. 5 is the floor of the objective (sections 7.3 and
# 17): it is a boundary, and the buckets are finest below it. The last ones cover a hold (420 seconds
# by default) and a turn that reasons up to the longest request (1,800).
WAIT_BUCKETS = (0.25, 0.5, 1, 1.5, 2, 2.5, 3, 4, 5, 7.5, 10, 15, 20, 30, 45, 60, 90, 120, 180, 240, 300, 420, 600, 1800)
