"""When a row of personal data expires: the course end plus 90 days (system design, section 6.6).

The usage rows, the roster and the keys are personal data, and every writer of those three tables
sets each row's `expires_at` (`rhi_common.tables.EXPIRES_AT`) from the course's end date, which is
configuration: the gateway is given it as `RHI_COURSE_END`, the issuer as well, and the admin CLI
reads it from the configuration in SSM.

- A usage row and a roster row expire at `expires_at(course_end)`: the first second, in UTC, of the
  90th day after the course end.
- A key row expires at the end of its validity, the course end (section 6.4, `rhi_issuer.mint`),
  which is earlier, and never later than `expires_at(course_end)`.

The value is in the form DynamoDB's expiry reads: whole seconds since the epoch, a number.
"""

from __future__ import annotations

import datetime

# Every store keeps personal data until the course end plus this many days (section 6.6).
RETENTION_DAYS = 90


def expires_at(course_end: datetime.date) -> int:
    """When a personal row of a course that ends on that date expires, in whole seconds since the epoch.

    The first second, in UTC, of the day `RETENTION_DAYS` after the course end.
    """
    day = course_end + datetime.timedelta(days=RETENTION_DAYS)
    return int(datetime.datetime.combine(day, datetime.time.min, datetime.UTC).timestamp())
