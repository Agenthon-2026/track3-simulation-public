"""Track-3 wave-1 speed overlay. Loaded automatically by the interpreter
(sitecustomize), before any ABIDES import - no upstream file is edited.

Two measured targets from the cProfile of as06 (kernel 36.3s):

1. Debug/info logging machinery (~0.5s of logging.debug + logger.info):
   disabled outright unless T3_DEBUG=1. Log text is never part of any graded
   artifact - trace.parquet, message_trace.parquet and events.json are built
   from structured data, not from log strings.

2. `abides_core.utils.fmt_ts` (3.6s across 147k calls): pandas Timestamp
   construction + strftime per call, mostly as EAGER arguments to disabled
   debug calls. Replaced by a second-resolution memoised formatter whose
   output is BYTE-IDENTICAL to the original (`pd.Timestamp(ns).strftime(
   "%Y-%m-%d %H:%M:%S")` == UTC wall clock floored to seconds) - so even if
   a string did reach an artifact, nothing changes. Simulated days share
   seconds across thousands of events, so the cache hit rate is high.

The RNG law holds trivially: nothing here touches a random stream, an event
queue, or a numeric value.
"""

import logging
import os

if os.environ.get("T3_DEBUG") != "1":
    logging.disable(logging.INFO)

try:
    from datetime import datetime, timezone

    from abides_core import utils as _utils

    _cache: dict = {}
    _orig_fmt_ts = _utils.fmt_ts

    def _fast_fmt_ts(timestamp) -> str:
        try:
            seconds = int(timestamp) // 1_000_000_000
        except (TypeError, ValueError):
            return _orig_fmt_ts(timestamp)  # e.g. a pd.Timestamp caller
        hit = _cache.get(seconds)
        if hit is None:
            hit = datetime.fromtimestamp(seconds, tz=timezone.utc).strftime(
                "%Y-%m-%d %H:%M:%S")
            _cache[seconds] = hit
        return hit

    _fast_fmt_ts.__doc__ = _utils.fmt_ts.__doc__
    _utils.fmt_ts = _fast_fmt_ts
except Exception:  # the overlay must never be able to break the simulator
    pass
