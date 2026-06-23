"""Human-like pacing between portal requests.

The city's portal can see every time a record is opened. Hitting records
back-to-back at a fixed cadence is exactly the kind of signal automated-scraper
heuristics look for. This module sleeps for a random, variable, *human-sized*
amount of time between record accesses — as if the user were reading each
page before clicking on the next one.

Distribution:
  * Most pauses are drawn from a right-skewed triangular distribution
    between `min_seconds` and `max_seconds` (mode at ~35% of the range).
    That gives plenty of "quick clicks" while still averaging out in
    the middle of the range — similar to how a real person skims some
    records and reads others carefully.
  * With probability `long_pause_chance`, a much longer pause is drawn
    uniformly from [long_pause_min_seconds, long_pause_max_seconds].
    This mimics a human getting distracted, reading an attachment,
    switching tabs, or taking a short break.

Two presets:
  * `sleep_between_records()` — longer, because a real user would read
    the full message thread on a detail page.
  * `sleep_between_pages()`  — shorter, because a real user usually
    just scans a list page to find something.

Both accept a dict of overrides (loaded from `config.json` under the
`human_delay` key) so the pacing is configurable without code changes.

Tuning by the user / the operator:
  * To run faster but riskier, lower `min_seconds` / `max_seconds`.
  * To blend in harder, raise them and/or bump `long_pause_chance`.
  * The DEFAULTS below are tuned for realistic reading time on the
    St. Pete public records portal.
"""
from __future__ import annotations

import logging
import random
import time

log = logging.getLogger(__name__)


RECORD_DEFAULTS = {
    "min_seconds": 20.0,
    "max_seconds": 75.0,
    "long_pause_chance": 0.10,
    "long_pause_min_seconds": 90.0,
    "long_pause_max_seconds": 300.0,
}

PAGE_DEFAULTS = {
    "min_seconds": 7.0,
    "max_seconds": 22.0,
    "long_pause_chance": 0.05,
    "long_pause_min_seconds": 45.0,
    "long_pause_max_seconds": 120.0,
}


def _merged(defaults: dict, overrides: dict | None) -> dict:
    if not overrides:
        return defaults
    out = dict(defaults)
    for k, v in overrides.items():
        if v is not None and k in out:
            out[k] = float(v)
    return out


def _pick_delay(d: dict) -> float:
    if random.random() < d["long_pause_chance"]:
        return random.uniform(d["long_pause_min_seconds"],
                              d["long_pause_max_seconds"])
    a, b = d["min_seconds"], d["max_seconds"]
    if b <= a:
        # Degenerate config — just use the minimum.
        return a
    mode = a + (b - a) * 0.35  # right-skewed: most pauses on the short end
    return random.triangular(a, b, mode)


def _pretty(seconds: float) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s"
    m, s = divmod(seconds, 60)
    return f"{int(m)}m{s:04.1f}s"


def sleep_between_records(overrides: dict | None = None,
                          *, dry_run: bool = False) -> float:
    """Pause as if the user just finished reading one record and is about
    to open another. Returns the chosen delay (seconds)."""
    d = _merged(RECORD_DEFAULTS, overrides)
    delay = _pick_delay(d)
    log.info("Pacing: waiting %s before next record.", _pretty(delay))
    if not dry_run:
        time.sleep(delay)
    return delay


def sleep_between_pages(overrides: dict | None = None,
                        *, dry_run: bool = False) -> float:
    """Pause between list-view pages."""
    d = _merged(PAGE_DEFAULTS, overrides)
    delay = _pick_delay(d)
    log.info("Pacing: waiting %s before next list page.", _pretty(delay))
    if not dry_run:
        time.sleep(delay)
    return delay
